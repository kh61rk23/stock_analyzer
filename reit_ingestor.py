# ==============================================================
# reit_ingestor.py — 不動産DB の J-REIT（物件・REIT ごとの指標）を BigQuery へ
# ==============================================================
# 【回数】不動産DB は保有不動産の取り込み（fudosan_ingestor）と同じ1日の枠（FUDOSAN_POST_CUTOFF_BUDGET＝90回）を
#   同じ台帳（fudosandb_cache）で数える。REIT は週1回だけ・REIT_CALLS_PER_RUN 回まで使い、残りを保有不動産が使う。
#   1回の内訳：REIT ごとの指標 4本（人口リスク・借入・テナント集中・鑑定の下落）＋ 物件を REIT_PROPERTY_REITS_PER_RUN 本の
#   REIT ぶん（REIT の一覧を持ち回り。73本を約6週で一巡）。
# 【出力】
#   real_estate.reit_properties … 1物件1行（reit_name・property_name・as_of・payload JSON）。取り直したら上書き（MERGE）
#   real_estate.reit_metrics    … 1回の取得・1窓口・1REIT 1行（endpoint・reit_name・as_of・payload JSON）。追記
#   応答の列は80を超えて増減するので、payload に丸ごと残し、SQL で JSON_VALUE して使う。
# 【進み具合】<DATA_DIR>/reit_rotation_meta.json（last_run・次の REIT の位置・REIT の一覧）
# ==============================================================
from __future__ import annotations

import io
import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

METRIC_ENDPOINTS = {
    "demographic_risk": ("/reit/demographic-risk", {"limit": 500}),
    "borrowings_summary": ("/reit/borrowings-summary", {"limit": 500}),
    "tenant_concentration": ("/reit/tenant-concentration", {"limit": 500}),
    "impairment_alerts": ("/reit/appraisal-impairment-alerts", {"limit": 1000}),
}
DATASET = "real_estate"
STOP_ERRORS = ("budget", "rate_limited", "auth", "no_api_key")
PROPS_TABLE = "reit_properties"
# /reit/properties の limit の上限（不動産DB の仕様で最大100）。上限を超える値は拒否されることがある
PROPERTY_LIMIT = 100
METRICS_TABLE = "reit_metrics"
# 区画の割り方の判定：最初の100件のうち用途・所在地が埋まっている行がこの割合以上なら、その列で割る
SPLIT_FILL_RATIO = 0.5
# 最初の100件に出なかった都道府県のうち、物件が多い所。100件の枠を同じ県が占めると他県が1件も見えないので、末尾で足す
MAJOR_PREFECTURES = ("東京都", "神奈川県", "大阪府", "愛知県", "福岡県", "北海道", "埼玉県", "千葉県", "兵庫県", "京都府")
_PREF_RE = re.compile(r"^(北海道|東京都|京都府|大阪府|.{2,3}?県)")
_CITY_RE = re.compile(r"^(?:.+?郡.+?[町村]|.+?[市区町村])")


def _rows(data) -> list[dict]:
    """応答から行の配列を取り出す。

    不動産DB は窓口ごとに配列のキー名が違う（/reit/demographic-risk は "demographic_risk"、
    /reit/properties は "properties" 等）。知っているキーが無ければ、辞書の配列の値を1つだけ持つ応答とみなして
    それを使う。キー名の一覧だけで判定すると、知らないキーの窓口が黙って0件になり、REIT の一覧が空のまま
    物件を1件も取らなくなる。
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("items", "results", "properties", "reits", "rows", "data"):
            if isinstance(data.get(k), list):
                return [r for r in data[k] if isinstance(r, dict)]
        lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        if len(lists) == 1:
            return [r for r in lists[0] if isinstance(r, dict)]
    return []


def latest_per_property(rows: list[dict]) -> list[dict]:
    """同じ物件の決算期ごとの行から、決算期（period_end）が最も新しい1行だけ残す（純関数）。

    /reit/properties は過去の有価証券報告書の行も返す。MERGE 側は fetched_at で1行に絞るが、同じ取得の
    行は fetched_at が同じなので、どの期が残るかが不定になる。ここで最新期に揃えておく。
    """
    best: dict[tuple, dict] = {}
    for r in rows:
        pn = property_name_of(r)
        if not pn:
            continue
        key = (reit_name_of(r), pn)
        cur = best.get(key)
        if cur is None or str(r.get("period_end") or "") > str(cur.get("period_end") or ""):
            best[key] = r
    return list(best.values())


def reit_name_of(r: dict) -> str | None:
    return r.get("reit_name") or r.get("reitName") or r.get("investment_corporation") or r.get("name")


def strict_reit_name_of(r: dict) -> str | None:
    """REIT の一覧に入れる名前。name は使わない（テナント集中の行はテナント名、鑑定の下落の行は物件名のことがあり、
    混ざると存在しない REIT の物件を取りに行って1日の枠を無駄に使う）。"""
    return r.get("reit_name") or r.get("reitName") or r.get("investment_corporation")


def property_name_of(r: dict) -> str | None:
    return r.get("property_name") or r.get("propertyName") or r.get("name")


def prefecture_of(r: dict) -> str | None:
    m = _PREF_RE.match(str(r.get("location") or "").strip())
    return m.group(1) if m else None


def _use_type_of(r: dict) -> str | None:
    v = str(r.get("use_type") or "").strip()
    return v or None


def plan_cells(rows: list[dict]) -> list[dict]:
    """100件で切れた REIT の取り直し区画を、最初の100件から決める（純関数）。

    用途の列が埋まっていれば用途で、無ければ所在地が埋まっていれば都道府県で割る。どちらも空なら [] （割れない）。
    エリアは、最初の100件に出た都道府県（多い順）のあとに、出なかった主要な都道府県（MAJOR_PREFECTURES）を足す。
    用途の絞り込みは部分一致なので、別の値を含む値（共同住宅 ⊃ 住宅）は取り直さない（含まれる側で拾える）。
    """
    n = len(rows)
    uts = Counter(v for v in map(_use_type_of, rows) if v)
    prs = Counter(v for v in map(prefecture_of, rows) if v)
    if n and sum(uts.values()) >= n * SPLIT_FILL_RATIO:
        vals = [v for v, _ in uts.most_common() if not any(o != v and o in v for o in uts)]
        return [{"asset_type": v} for v in vals]
    if n and sum(prs.values()) >= n * SPLIT_FILL_RATIO:
        order = [v for v, _ in prs.most_common()]
        return [{"area": v} for v in order + [p for p in MAJOR_PREFECTURES if p not in prs]]
    return []


def child_cells(cell: dict, rows: list[dict]) -> list[dict]:
    """1区画がまた100件のとき、さらに割った区画（純関数）。用途で割った区画はエリアで、エリアは一段細かいエリアで割る。

    都道府県 → 市区郡 →（市の下の）区。東京23区のように区で割った区画、所在地が空の区画は割れず [] （取り切れない）。
    """
    if "area" not in cell:
        prs = Counter(v for v in map(prefecture_of, rows) if v)
        if prs and sum(prs.values()) >= len(rows) * SPLIT_FILL_RATIO:
            return [{**cell, "area": p} for p, _ in prs.most_common()]
        return []
    area = cell["area"]
    if area.endswith("区"):
        return []
    sub: Counter = Counter()
    for r in rows:
        loc = str(r.get("location") or "").strip()
        if not loc.startswith(area):
            continue
        m = _CITY_RE.match(loc[len(area):])
        if not m:
            continue
        piece = m.group(0)
        if area.endswith("市"):
            if not piece.endswith("区"):
                continue
        sub[area + piece] += 1
    return [{**cell, "area": a} for a, _ in sub.most_common()]


def _cell_params(name: str, cell: dict) -> dict:
    return {"reit_name": name, "limit": PROPERTY_LIMIT, **{k: v for k, v in cell.items() if v}}


def _fetch_properties(client, name: str, cell: dict | None = None) -> list[dict]:
    return _rows(client.get("/reit/properties", _cell_params(name, cell or {})))


def _split_step(client, name: str, st: dict, rows_out: list[dict]) -> bool:
    """取り直しを1回進める。API を呼んだら True、呼べなかった（一覧が空）なら False。止まる失敗は client.last_error に残る。"""
    names = set(st.get("names") or [])
    if st.get("queue") is None:
        rows = _fetch_properties(client, name)                      # 計画に要る最初の100件を取り直す
        if client.last_error in STOP_ERRORS:
            return True
        rows_out.extend(rows)
        names |= {property_name_of(r) for r in rows if property_name_of(r)}
        st["base"] = len(names)
        st["queue"] = plan_cells(rows) if len(rows) >= PROPERTY_LIMIT else []
        if not st["queue"]:
            st["unresolved"] = len(rows) >= PROPERTY_LIMIT
        st["names"] = sorted(names)
        return True
    if not st["queue"]:
        return False
    cell = st["queue"].pop(0)
    rows = _fetch_properties(client, name, cell)
    if client.last_error in STOP_ERRORS:
        st["queue"].insert(0, cell)                                 # 取れていないので次の回にやり直す
        return True
    rows_out.extend(rows)
    names |= {property_name_of(r) for r in rows if property_name_of(r)}
    if len(rows) >= PROPERTY_LIMIT:
        kids = child_cells(cell, rows)
        if kids:
            st["queue"] = kids + st["queue"]
        else:
            st["unresolved"] = True                                 # これ以上割れない区画が100件
    st["names"] = sorted(names)
    return True


def due(meta: dict, today: date, every_days: int) -> bool:
    last = meta.get("last_run")
    return not last or (today - date.fromisoformat(last)).days >= every_days


def next_reits(names: list[str], pos: int, n: int) -> tuple[list[str], int]:
    """持ち回りで次の n 本と、次の位置（純関数）。"""
    if not names:
        return [], 0
    pos %= len(names)
    picked = [names[(pos + i) % len(names)] for i in range(min(n, len(names)))]
    return picked, (pos + len(picked)) % len(names)


def _seed_split(meta: dict) -> dict:
    """split の状態を取り出す。無ければ、前回までに「100件で切れた」と記録された REIT（last_truncated）から作る。"""
    split = {k: dict(v) for k, v in (meta.get("split") or {}).items()}
    if "split" not in meta:
        for n in meta.get("last_truncated") or []:
            split[n] = {"queue": None}
    return split


def has_pending_split(meta: dict) -> bool:
    return any(st.get("queue") is None or st["queue"] for st in _seed_split(meta).values())


def _incomplete(split: dict, done: dict) -> list[str]:
    """まだ取り切れていない REIT（取り直しの途中、または割れない区画が100件のまま終わった）。"""
    return sorted(set(split) | {n for n, d in done.items() if d.get("unresolved")})


def collect(client, meta: dict, per_run: int, as_of: str, cap: int | None = None,
            refresh_days: int = 180) -> tuple[list[dict], list[dict], dict]:
    """API を呼んで (物件の行, 指標の行, 新しい meta) を返す。予算切れ・失敗はそこで止める。

    cap は1回の API 回数の上限。指標と物件の REIT に使った残りを、100件で切れた REIT の取り直しに使う。
    """
    now = datetime.now(timezone.utc).isoformat()
    metrics: list[dict] = []
    names = list(meta.get("reits") or [])
    calls = 0
    stopped = False
    for key, (path, params) in METRIC_ENDPOINTS.items():
        rows = _rows(client.get(path, params))
        calls += 1
        if client.last_error in STOP_ERRORS:
            stopped = True
            break
        for r in rows:
            metrics.append({"endpoint": key, "reit_name": reit_name_of(r), "as_of": as_of, "fetched_at": now,
                            "payload": json.dumps(r, ensure_ascii=False)})
            # REIT の一覧はどの指標の応答からも集める（1窓口が0件・失敗でも物件の取得が止まらないように）
            if strict_reit_name_of(r):
                names.append(strict_reit_name_of(r))
    names = sorted(set(n for n in names if n))
    picked, pos = next_reits(names, int(meta.get("pos", 0)), per_run)
    split = _seed_split(meta)
    split_done = {k: dict(v) for k, v in (meta.get("split_done") or {}).items()}
    props: list[dict] = []
    done = 0
    for name in ([] if stopped else picked):
        rows = _fetch_properties(client, name)
        calls += 1
        if client.last_error in STOP_ERRORS:
            stopped = True
            break
        done += 1
        # 決算期ごとの行が返るので、上限ちょうどなら物件か最新期が欠けている可能性がある。続きは用途・エリアで割って取り直す
        if len(rows) >= PROPERTY_LIMIT and name not in split and not _fresh(split_done.get(name), as_of, refresh_days):
            split[name] = {"queue": plan_cells(rows), "base": len({property_name_of(r) for r in rows}),
                           "names": sorted({property_name_of(r) for r in rows if property_name_of(r)})}
            split_done.pop(name, None)
            if not split[name]["queue"]:
                split[name]["unresolved"] = True
        for r in latest_per_property(rows):
            pn = property_name_of(r)
            if pn:
                props.append({"reit_name": reit_name_of(r) or name, "property_name": pn, "as_of": as_of,
                              "fetched_at": now, "payload": json.dumps(r, ensure_ascii=False)})
    new_pos = (int(meta.get("pos", 0)) + done) % len(names) if names else 0
    # 取り直し：REIT ごとに最初の区画から順に、回数の残りが続く限り進める
    budget = max(0, (cap if cap is not None else calls) - calls)
    split_calls = 0
    split_rows: dict[str, list[dict]] = {}
    for name in sorted(split):
        st = split[name]
        while not stopped and split_calls < budget and (st.get("queue") is None or st["queue"]):
            _split_step(client, name, st, split_rows.setdefault(name, []))
            split_calls += 1
            if client.last_error in STOP_ERRORS:
                stopped = True
        if st.get("queue") == []:
            total = len(st.get("names") or [])
            split_done[name] = {"total": total, "base": int(st.get("base") or 0), "as_of": as_of,
                                "unresolved": bool(st.get("unresolved"))}
            del split[name]
    completed = sorted(n for n in split_done if split_done[n].get("as_of") == as_of and n not in split)
    for name, rows in split_rows.items():
        for r in latest_per_property(rows):
            pn = property_name_of(r)
            if pn:
                props.append({"reit_name": reit_name_of(r) or name, "property_name": pn, "as_of": as_of,
                              "fetched_at": now, "payload": json.dumps(r, ensure_ascii=False)})
    props = _dedupe_props(props)
    return props, metrics, {**meta, "reits": names, "pos": new_pos, "last_run": as_of,
                            "last_reits": picked[:done], "last_truncated": _incomplete(split, split_done),
                            "split": split, "split_done": split_done, "last_split_calls": split_calls,
                            "last_split_completed": completed}


def _fresh(d: dict | None, as_of: str, days: int) -> bool:
    """取り直しが済んで日が浅い REIT は、100件で切れていても取り直さない（有報は年1〜2回しか変わらない）。"""
    if not d or not d.get("as_of"):
        return False
    try:
        return (date.fromisoformat(as_of) - date.fromisoformat(d["as_of"])).days < days
    except ValueError:
        return False


def _dedupe_props(props: list[dict]) -> list[dict]:
    """(REIT, 物件名) で1行にする。区画をまたいで同じ物件が来たら、決算期が新しい行を残す（同じ期なら先の行）。"""
    best: dict[tuple, dict] = {}
    for p in props:
        key = (p["reit_name"], p["property_name"])
        cur = best.get(key)
        if cur is None or _period_of(p) > _period_of(cur):
            best[key] = p
    return list(best.values())


def _period_of(p: dict) -> str:
    try:
        return str(json.loads(p["payload"]).get("period_end") or "")
    except (ValueError, TypeError):
        return ""


def _calls_used(client) -> int:
    try:
        return int(client._load_ledger().get("calls_used", 0) or 0)
    except Exception:
        return 0


def _load(bq, table: str, rows: list[dict], schema, disposition) -> None:
    from google.cloud import bigquery
    buf = io.BytesIO("\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8"))
    bq.load_table_from_file(buf, f"{bq.project}.{DATASET}.{table}", location="asia-northeast1",
                            job_config=bigquery.LoadJobConfig(schema=schema, write_disposition=disposition,
                                                              source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON)).result()


def write(bq, props: list[dict], metrics: list[dict]) -> None:
    from google.cloud import bigquery
    S = bigquery.SchemaField
    W = bigquery.WriteDisposition
    if metrics:
        _load(bq, METRICS_TABLE, metrics, [S("endpoint", "STRING"), S("reit_name", "STRING"), S("as_of", "DATE"),
                                           S("fetched_at", "TIMESTAMP"), S("payload", "STRING")], W.WRITE_APPEND)
    if props:
        schema = [S("reit_name", "STRING"), S("property_name", "STRING"), S("as_of", "DATE"),
                  S("fetched_at", "TIMESTAMP"), S("payload", "STRING")]
        _load(bq, "_staging_reit_properties", props, schema, W.WRITE_TRUNCATE)
        bq.query(f"""CREATE TABLE IF NOT EXISTS `{bq.project}.{DATASET}.{PROPS_TABLE}`
          (reit_name STRING, property_name STRING, as_of DATE, fetched_at TIMESTAMP, payload STRING);
          MERGE `{bq.project}.{DATASET}.{PROPS_TABLE}` T
          USING (SELECT * FROM `{bq.project}.{DATASET}._staging_reit_properties`
                 QUALIFY ROW_NUMBER() OVER (PARTITION BY reit_name, property_name ORDER BY fetched_at DESC) = 1) S
          ON T.reit_name = S.reit_name AND T.property_name = S.property_name
          WHEN MATCHED THEN UPDATE SET as_of = S.as_of, fetched_at = S.fetched_at, payload = S.payload
          WHEN NOT MATCHED THEN INSERT ROW""", location="asia-northeast1").result()


def run(cfg, client=None, bq=None, today: date | None = None) -> dict:
    if not getattr(cfg, "ENABLE_REIT_INGEST", True):
        return {"skipped": True, "reason": "disabled"}
    today = today or date.today()
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    meta_path = data_dir / "reit_rotation_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if not due(meta, today, int(getattr(cfg, "REIT_INGEST_EVERY_DAYS", 7))):
        return {"skipped": True, "reason": f"週1回（前回 {meta.get('last_run')}）"}
    if client is None:
        if not str(getattr(cfg, "FUDOSAN_API_KEY", "") or "").strip():
            return {"skipped": True, "reason": "FUDOSAN_API_KEY missing"}
        from db_rest_client import DBRestClient
        import fudosan_ingestor
        client = DBRestClient(getattr(cfg, "FUDOSAN_BASE_URL", "https://fudosandb.jp/v1"), cfg.FUDOSAN_API_KEY,
                              data_dir / "fudosandb_cache", fudosan_ingestor._effective_daily_budget(cfg), "fudosandb",
                              usage_endpoint=None)
    start = _calls_used(client)
    cap = int(getattr(cfg, "REIT_CALLS_PER_RUN", 16))
    # 100件で切れた REIT の取り直しが残っている間は、その回数ぶん物件の REIT の本数を減らす
    reserve = min(int(getattr(cfg, "REIT_SPLIT_CALLS_PER_RUN", 6)), max(0, cap - len(METRIC_ENDPOINTS))) \
        if has_pending_split(meta) else 0
    per_run = max(0, min(int(getattr(cfg, "REIT_PROPERTY_REITS_PER_RUN", 12)), cap - len(METRIC_ENDPOINTS) - reserve))
    props, metrics, new_meta = collect(client, meta, per_run, today.isoformat(), cap=cap,
                                       refresh_days=int(getattr(cfg, "REIT_SPLIT_REFRESH_DAYS", 180)))
    if not props and not metrics:
        return {"skipped": True, "reason": f"取れなかった（{client.last_error or '応答なし'}）",
                "calls": _calls_used(client) - start}
    if bq is None:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient().client
    write(bq, props, metrics)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(new_meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"properties": len(props), "metrics": len(metrics), "reits": new_meta.get("last_reits"),
            "reits_total": len(new_meta.get("reits") or []), "calls": _calls_used(client) - start,
            "truncated": new_meta.get("last_truncated") or [],
            "split_calls": new_meta.get("last_split_calls", 0),
            "split_completed": new_meta.get("last_split_completed") or [],
            "split_gained": sum(max(0, d.get("total", 0) - d.get("base", 0))
                                for d in (new_meta.get("split_done") or {}).values())}
