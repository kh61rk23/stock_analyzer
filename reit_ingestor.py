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


def collect(client, meta: dict, per_run: int, as_of: str) -> tuple[list[dict], list[dict], dict]:
    """API を呼んで (物件の行, 指標の行, 新しい meta) を返す。予算切れ・失敗はそこで止める。"""
    now = datetime.now(timezone.utc).isoformat()
    metrics: list[dict] = []
    names = list(meta.get("reits") or [])
    for key, (path, params) in METRIC_ENDPOINTS.items():
        rows = _rows(client.get(path, params))
        if client.last_error in STOP_ERRORS:
            break
        for r in rows:
            metrics.append({"endpoint": key, "reit_name": reit_name_of(r), "as_of": as_of, "fetched_at": now,
                            "payload": json.dumps(r, ensure_ascii=False)})
            # REIT の一覧はどの指標の応答からも集める（1窓口が0件・失敗でも物件の取得が止まらないように）
            if strict_reit_name_of(r):
                names.append(strict_reit_name_of(r))
    names = sorted(set(n for n in names if n))
    picked, pos = next_reits(names, int(meta.get("pos", 0)), per_run)
    props: list[dict] = []
    done = 0
    truncated: list[str] = []
    for name in picked:
        rows = _rows(client.get("/reit/properties", {"reit_name": name, "limit": PROPERTY_LIMIT}))
        if client.last_error in STOP_ERRORS:
            break
        done += 1
        # 決算期ごとの行が返るので、上限ちょうどなら物件か最新期が欠けている可能性がある（続きを取る引数は未確認）
        if len(rows) >= PROPERTY_LIMIT:
            truncated.append(name)
        for r in latest_per_property(rows):
            pn = property_name_of(r)
            if pn:
                props.append({"reit_name": reit_name_of(r) or name, "property_name": pn, "as_of": as_of,
                              "fetched_at": now, "payload": json.dumps(r, ensure_ascii=False)})
    new_pos = (int(meta.get("pos", 0)) + done) % len(names) if names else 0
    return props, metrics, {**meta, "reits": names, "pos": new_pos, "last_run": as_of,
                            "last_reits": picked[:done], "last_truncated": truncated}


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
    per_run = max(0, min(int(getattr(cfg, "REIT_PROPERTY_REITS_PER_RUN", 12)), cap - len(METRIC_ENDPOINTS)))
    props, metrics, new_meta = collect(client, meta, per_run, today.isoformat())
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
            "truncated": new_meta.get("last_truncated") or []}
