# ==============================================================
# deliberation_queue.py — 投資審議キュー（BigQuery invest.deliberation_queue）
# ==============================================================
# 【役割】スクリーナーの最終候補と保有銘柄から「次に審議へ上げる銘柄」を日次で決め、
#   BQ の審議キューに書く。クラウド routine（tools/invest_queue.py claim）と
#   日次メール（get_queue_for_report）が同じキューを読む。
# 【流れ】build_queue（純粋関数）→ attach_packets（上位だけパケット生成）
#         → write_queue（load job・1日1回・同日置き換え）
# 【キューに入れる規則】
#   - 最終候補を final_score 降順に並べ、未審議／前回から DELIB_RECHECK_DAYS 超／
#     前回審議時より final_score が DELIB_RESCORE_JUMP 以上上がった銘柄を入れる
#   - コア層（NISA積立・恩株・放置大型株）は入れない（部則7）
#   - 非コアの保有銘柄は holding_review として DELIB_RECHECK_DAYS ごとに入れる
#   - 1日 DELIB_QUEUE_SIZE 件
# 【BQ】読み書きは各1〜3本。銘柄ごとにクエリしない（課金ガード）。
# ==============================================================
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from invest_bq_schema import DATASET, DELIB_TABLE, LATEST_VIEW, LOCATION, QUEUE_TABLE, RUNS_TABLE

# 保有メモ・口座区分にこれらがあればコア層とみなす（部則7: NISA積立・恩株・放置大型株）
CORE_ACCOUNT_MARKERS = ("NISA", "ニーサ")
CORE_MEMO_MARKERS = ("恩株", "コア", "放置", "積立")

STATUS_PENDING = "pending"
JST = timezone(timedelta(hours=9))


def today_jst() -> date:
    return datetime.now(JST).date()


# ── 純粋ロジック ──────────────────────────────────────────────

def _cfg(cfg, name: str, default):
    return getattr(cfg, name, default)


def _to_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _f(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def is_core_holding(h: dict) -> bool:
    """保有行がコア層か。口座区分に NISA、またはメモに恩株・コア・放置・積立を含む。"""
    acct = str(h.get("account_type") or "")
    memo = str(h.get("memo") or "")
    return (any(m in acct for m in CORE_ACCOUNT_MARKERS)
            or any(m in memo for m in CORE_MEMO_MARKERS))


def _final_score(c: dict) -> float | None:
    v = _f(c.get("final_score"))
    return v if v is not None else _f(c.get("composite_score"))


def _factor_scores(c: dict) -> dict:
    fs = c.get("factor_scores")
    if isinstance(fs, dict):
        return fs
    keys = ("base_score", "value_score", "quality_score", "discovery_score")
    return {k: c[k] for k in keys if c.get(k) is not None}


def _factor_reason(fs: dict) -> str:
    items = sorted(((k, _f(v)) for k, v in fs.items() if _f(v) is not None),
                   key=lambda kv: -kv[1])[:2]
    return "・".join(f"{k.replace('_score', '')} {v:.0f}" for k, v in items)


def _decide(c: dict, prev: dict | None, today: date, recheck_days: int,
            jump: float) -> str | None:
    """審議へ上げる理由を返す。上げないなら None。"""
    last = _to_date((prev or {}).get("deliberated_at"))
    if prev is None or last is None:
        return "未審議"
    age = (today - last).days
    if age > recheck_days:
        return f"前回審議から{age}日（{(prev.get('verdict') or '判定不明')}）"
    now, then = _final_score(c), _f(prev.get("final_score"))
    if now is not None and then is not None and now - then >= jump:
        return f"点数急上昇（{then:.1f}→{now:.1f}）"
    return None


def _row(c: dict, source: str, reason: str, prev: dict | None, today: date) -> dict:
    fs = _factor_scores(c)
    fin = _final_score(c)
    last_day = _to_date((prev or {}).get("deliberated_at"))
    tail = _factor_reason(fs)
    head = f"final {fin:.1f}" if fin is not None else "点数なし"
    return {
        "queue_date": today.isoformat(),
        "ticker": c.get("ticker"),
        "edinet_code": c.get("edinet_code") or None,
        "company_name": c.get("company_name") or c.get("name") or None,
        "sector_code": str(c.get("sector_code") or "") or None,
        "priority_rank": 0,
        "source": source,
        "reason": " / ".join(x for x in (reason, head, tail) if x),
        "universe_score": _f(c.get("universe_score")),
        "final_score": fin,
        "composite_score": _f(c.get("composite_score")),
        "factor_scores": fs or None,
        "last_deliberated_at": last_day.isoformat() if last_day else None,
        "last_verdict": (prev or {}).get("verdict") or None,
        "packet_md": None,
        "status": STATUS_PENDING,
        "claimed_at": None,
        "case_id": None,
        "updated_at": None,
    }


def build_queue(candidates: list, latest_deliberations: dict, holdings: list,
                cfg, today: date) -> list:
    """審議キューの行を優先順に返す（純粋関数・I/Oなし）。

    Args:
        candidates: スクリーナーの最終候補 [{"ticker","final_score"|"composite_score",...}]
        latest_deliberations: {ticker: {"deliberated_at","verdict","final_score"}}
            invest.deliberations_latest 由来。final_score は前回審議時の点数（無ければ None）
        holdings: 保有行 [{"ticker","account_type","memo",...}]
        cfg: DELIB_QUEUE_SIZE / DELIB_RECHECK_DAYS / DELIB_RESCORE_JUMP /
             DELIB_HOLDING_MAX を getattr で読む
    """
    size = int(_cfg(cfg, "DELIB_QUEUE_SIZE", 10))
    recheck = int(_cfg(cfg, "DELIB_RECHECK_DAYS", 90))
    jump = float(_cfg(cfg, "DELIB_RESCORE_JUMP", 15))
    hold_max = int(_cfg(cfg, "DELIB_HOLDING_MAX", 3))
    latest = latest_deliberations or {}

    core = {h.get("ticker") for h in holdings or [] if h.get("ticker") and is_core_holding(h)}
    held = {h["ticker"]: h for h in holdings or []
            if h.get("ticker") and h["ticker"] not in core}
    by_ticker = {c.get("ticker"): c for c in candidates or [] if c.get("ticker")}

    out: list = []
    seen: set = set()

    # 保有銘柄の定期見直し（候補に点数があればそれも載せる）
    reviews = []
    for t, h in held.items():
        prev = latest.get(t)
        last = _to_date((prev or {}).get("deliberated_at"))
        if last is not None and (today - last).days <= recheck:
            continue
        why = "保有の定期見直し・未審議" if last is None else f"保有の定期見直し・前回から{(today - last).days}日"
        merged = {**h, **by_ticker.get(t, {})}
        reviews.append(_row(merged, "holding_review", why, prev, today))
    reviews.sort(key=lambda r: -(r["final_score"] if r["final_score"] is not None else -1))
    for r in reviews[:max(0, hold_max)]:
        out.append(r)
        seen.add(r["ticker"])

    ranked = sorted(
        (c for c in candidates or [] if c.get("ticker") and _final_score(c) is not None),
        key=lambda c: (-_final_score(c), -(_f(c.get("base_score")) or 0)))
    for c in ranked:
        if len(out) >= size:
            break
        t = c["ticker"]
        if t in core or t in seen:
            continue
        why = _decide(c, latest.get(t), today, recheck, jump)
        if why is None:
            continue
        out.append(_row(c, "screener", why, latest.get(t), today))
        seen.add(t)

    out = out[:max(0, size)]
    for i, r in enumerate(out, 1):
        r["priority_rank"] = i
    return out


def attach_packets(rows: list, top_n: int, packet_fn) -> dict:
    """上位 top_n 件に packet_md を付ける。生成に失敗した銘柄は packet_md=None のまま、
    失敗理由を返す（claim 側が「パケット無し」を検知して自前生成に回れるように）。"""
    failed: dict = {}
    for r in rows[:max(0, int(top_n))]:
        try:
            r["packet_md"] = packet_fn(r["ticker"])
        except Exception as e:  # パケット1件の失敗でキュー全体を止めない。理由は呼び出し側が表示する
            failed[r["ticker"]] = str(e)[:120]
    return {"ok": sum(1 for r in rows[:top_n] if r.get("packet_md")), "failed": failed}


# ── BQ I/O ────────────────────────────────────────────────────

def _client():
    from edinet_bq_client import EDINETBQClient
    return EDINETBQClient().client


def _fq(client, name: str) -> str:
    return f"`{client.project}.{DATASET}.{name}`"


def queue_schema():
    from google.cloud.bigquery import SchemaField as F
    return [F("queue_date", "DATE", "REQUIRED"), F("ticker", "STRING", "REQUIRED"),
            F("edinet_code", "STRING"), F("company_name", "STRING"), F("sector_code", "STRING"),
            F("priority_rank", "INT64"), F("source", "STRING"), F("reason", "STRING"),
            F("universe_score", "FLOAT64"), F("final_score", "FLOAT64"), F("composite_score", "FLOAT64"),
            F("factor_scores", "JSON"), F("last_deliberated_at", "DATE"), F("last_verdict", "STRING"),
            F("packet_md", "STRING"), F("status", "STRING"), F("claimed_at", "TIMESTAMP"),
            F("case_id", "STRING"), F("updated_at", "TIMESTAMP")]


def load_latest_deliberations(client) -> dict:
    """銘柄ごとの最新審議と、そのときの final_score（キュー行から引く）を1本のクエリで返す。"""
    sql = f"""
    SELECT d.ticker, DATE(d.deliberated_at, 'Asia/Tokyo') AS deliberated_at, d.verdict, d.layer,
           q.final_score
    FROM {_fq(client, LATEST_VIEW)} d
    LEFT JOIN (SELECT case_id, MAX(final_score) AS final_score
               FROM {_fq(client, QUEUE_TABLE)} WHERE case_id IS NOT NULL GROUP BY case_id) q
      ON q.case_id = d.case_id"""
    out = {}
    for r in client.query(sql, location=LOCATION).result():
        out[r["ticker"]] = {"deliberated_at": r["deliberated_at"], "verdict": r["verdict"],
                            "layer": r["layer"], "final_score": r["final_score"]}
    return out


def _json_safe(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def write_queue(client, rows: list, today: date, tmp_dir: Path) -> dict:
    """当日分のキューを置き換える（load job・WRITE_TRUNCATE をパーティション指定）。

    同日に既に claimed/done/skipped になった行は審議の進行状況なので消さない。
    その銘柄は新しい行から外し、既存行をそのまま残して書き直す。
    """
    from google.cloud import bigquery
    day = today.isoformat()
    kept_rows = []
    sql = (f"SELECT * FROM {_fq(client, QUEUE_TABLE)} "
           f"WHERE queue_date = @d AND status != '{STATUS_PENDING}'")
    cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", day)])
    for r in client.query(sql, job_config=cfg, location=LOCATION).result():
        d = {k: _json_safe(v) for k, v in dict(r.items()).items()}
        if isinstance(d.get("factor_scores"), str):
            d["factor_scores"] = json.loads(d["factor_scores"])
        kept_rows.append(d)
    kept = {r["ticker"] for r in kept_rows}
    now = datetime.now(timezone.utc).isoformat()
    fresh = [dict(r, updated_at=now) for r in rows if r["ticker"] not in kept]
    allrows = kept_rows + fresh
    if not allrows:
        return {"written": 0, "kept": 0}

    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / "deliberation_queue.ndjson"
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in allrows), encoding="utf-8")
    ref = f"{client.project}.{DATASET}.{QUEUE_TABLE}${day.replace('-', '')}"
    try:
        with open(tmp, "rb") as fh:
            client.load_table_from_file(fh, ref, location=LOCATION, job_config=bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON, schema=queue_schema(),
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)).result()
    finally:
        tmp.unlink(missing_ok=True)
    return {"written": len(fresh), "kept": len(kept_rows)}


def load_holdings(cfg) -> list:
    """保有行（data/portfolio_holdings.csv）。無ければ空（キューは保有見直し無しで作る）。"""
    import csv
    for base in (Path(__file__).resolve().parent / "data",):
        p = base / "portfolio_holdings.csv"
        if not p.exists():
            continue
        for enc in ("utf-8", "cp932"):
            try:
                with open(p, encoding=enc) as f:
                    return list(csv.DictReader(f))
            except UnicodeDecodeError:
                continue
    return []


def run_daily(cfg, candidates: list, today: date | None = None, client=None) -> dict:
    """日次: キューを作って BQ へ書く。失敗は例外のまま上げる（呼び出し側が失敗として扱う）。"""
    today = today or today_jst()
    client = client or _client()
    from invest_bq_schema import ensure
    ensure(client)
    latest = load_latest_deliberations(client)
    rows = build_queue(candidates, latest, load_holdings(cfg), cfg, today)
    pk = {"ok": 0, "failed": {}}
    if rows:
        import agent_packet_exporter as ape
        pk = attach_packets(rows, int(_cfg(cfg, "DELIB_PACKET_TOP_N", 5)),
                            lambda t: ape.build_packet_text(t))
    res = write_queue(client, rows, today,
                      Path(_cfg(cfg, "DATA_DIR", "stock_data/data")) / "tmp")
    res.update(queued=len(rows), packets=pk["ok"], packet_failed=pk["failed"])
    return res


# ── 審議結果の書き出しと承認の書き戻し ─────────────────────────
# 流れ: Mac の obsidian_sync approvals が台帳の承認を invest_approvals.json に書いて push
#   → ここの apply_approvals が差分だけ invest.deliberations へ反映
#   → export_results が全件を invest_deliberations.json に書く（Actions が commit し、
#     Mac の obsidian_sync deliberations が vault へミラーする）。
# 承認はエージェントが作らない。人間が台帳に書いた内容の転記だけを行う（部則5）。

EXPORT_FILE = "invest_deliberations.json"
APPROVALS_FILE = "invest_approvals.json"
APPROVED_BY = "健太（台帳）"
EXPORT_COLUMNS = ("case_id", "ticker", "company_name", "deliberated_at", "verdict", "conviction",
                  "layer", "devil_unresolved", "devil_reversal", "kpi", "bull_memo", "bear_memo",
                  "devil_memo", "report_md", "packet_md", "run_env", "approval_status",
                  "approved_at", "holding_intent")


def _data_dir(cfg) -> Path:
    return Path(_cfg(cfg, "DATA_DIR", "stock_data/data"))


def rows_to_export(rows) -> list:
    """BQ の行（dict 化済み）を JSON 化できる形にする。kpi は JSON 文字列なら dict に戻す。"""
    out = []
    for r in rows:
        d = {k: _json_safe(r.get(k)) for k in EXPORT_COLUMNS}
        if isinstance(d.get("kpi"), str):
            try:
                d["kpi"] = json.loads(d["kpi"])
            except ValueError:
                pass
        out.append(d)
    return out


def export_results(cfg, client=None) -> dict:
    """invest.deliberations の全件を1クエリで読み、DATA_DIR/invest_deliberations.json に書く。

    0件のときは既存ファイルを空で潰さない。"""
    client = client or _client()
    cols = ", ".join("TO_JSON_STRING(kpi) AS kpi" if c == "kpi" else c for c in EXPORT_COLUMNS)
    sql = f"SELECT {cols} FROM {_fq(client, DELIB_TABLE)} ORDER BY deliberated_at, case_id"
    rows = rows_to_export(dict(r.items()) for r in client.query(sql, location=LOCATION).result())
    if not rows:
        return {"exported": 0, "skipped": "invest.deliberations が0件"}
    path = _data_dir(cfg) / EXPORT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "exported_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"{DATASET}.{DELIB_TABLE}",
        "count": len(rows),
        "deliberations": rows,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"exported": len(rows), "path": str(path)}


def load_approvals_file(cfg) -> dict:
    """invest_approvals.json の approvals（{case_id: {approval_status, approved_at}}）。無ければ空。"""
    try:
        raw = json.loads((_data_dir(cfg) / APPROVALS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    a = raw.get("approvals") if isinstance(raw, dict) else None
    return a if isinstance(a, dict) else {}


def diff_approvals(approvals: dict, current: dict) -> list:
    """台帳側の承認と DB の現状の差分だけを返す（純粋関数）。

    Args:
        approvals: {case_id: {"approval_status", "approved_at"(YYYY-MM-DD|None)}}
        current:   {case_id: {"approval_status", "approved_at"}}（DB に存在する case のみ）
    DB に無い case（過去の台帳だけの case）は対象外。状態も日付も同じなら出さない。
    """
    out = []
    for cid in sorted(approvals):
        if cid not in current:
            continue
        want = approvals[cid] or {}
        status = want.get("approval_status")
        if not status:
            continue
        day = _to_date(want.get("approved_at"))
        have = current[cid] or {}
        if have.get("approval_status") == status and (
                day is None or _to_date(have.get("approved_at")) == day):
            continue
        out.append({"case_id": cid, "approval_status": status, "approved_at": day})
    return out


def apply_approvals(cfg, client=None) -> dict:
    """invest_approvals.json と invest.deliberations の差分だけを1本の MERGE で反映する。

    読みは1本（対象 case の現状）、書きは差分があるときだけ1本。値はクエリパラメータで渡す。"""
    approvals = load_approvals_file(cfg)
    if not approvals:
        return {"applied": 0, "skipped": "invest_approvals.json が無い／空"}
    from google.cloud import bigquery
    client = client or _client()
    ids = sorted(approvals)
    q = (f"SELECT case_id, approval_status, DATE(approved_at) AS approved_at "
         f"FROM {_fq(client, DELIB_TABLE)} WHERE case_id IN UNNEST(@ids)")
    job = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)])
    current = {r["case_id"]: {"approval_status": r["approval_status"], "approved_at": r["approved_at"]}
               for r in client.query(q, job_config=job, location=LOCATION).result()}
    diff = diff_approvals(approvals, current)
    if not diff:
        return {"applied": 0, "checked": len(current)}
    structs = [bigquery.StructQueryParameter(
        None,
        bigquery.ScalarQueryParameter("case_id", "STRING", d["case_id"]),
        bigquery.ScalarQueryParameter("approval_status", "STRING", d["approval_status"]),
        bigquery.ScalarQueryParameter("approved_at", "DATE", d["approved_at"]))
        for d in diff]
    sql = f"""
    MERGE {_fq(client, DELIB_TABLE)} T
    USING (SELECT case_id, approval_status, approved_at FROM UNNEST(@rows)) S
    ON T.case_id = S.case_id
    WHEN MATCHED THEN UPDATE SET
      approval_status = S.approval_status,
      approved_by = @by,
      approved_at = IF(S.approved_at IS NULL, T.approved_at, TIMESTAMP(S.approved_at))"""
    cfgq = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("rows", "STRUCT", structs),
        bigquery.ScalarQueryParameter("by", "STRING", APPROVED_BY)])
    client.query(sql, job_config=cfgq, location=LOCATION).result()
    return {"applied": len(diff), "cases": [d["case_id"] for d in diff]}


def run_exports(cfg, client=None) -> dict:
    """承認の反映 → 全件書き出しの順に実行する。片方の失敗がもう片方を止めない。
    戻り値の errors が空でなければ呼び出し側は warn にする。"""
    res: dict = {"errors": {}}
    try:
        client = client or _client()
    except Exception as e:
        return {"errors": {"client": str(e)[:120]}}
    for name, fn in (("apply_approvals", apply_approvals), ("export_results", export_results)):
        try:
            res[name] = fn(cfg, client)
        except Exception as e:  # 失敗は台帳に warn で残す。run_daily の結果は壊さない
            res["errors"][name] = str(e)[:120]
    return res


# ── メール用の読み出し ────────────────────────────────────────

def get_last_run(client) -> dict | None:
    sql = (f"SELECT run_id, started_at, status, completed_count, error "
           f"FROM {_fq(client, RUNS_TABLE)} ORDER BY started_at DESC LIMIT 1")
    for r in client.query(sql, location=LOCATION).result():
        return dict(r.items())
    return None


def get_queue_for_report(cfg, results: dict = None, top_n: int = 5, client=None) -> dict:
    """deliberation_scheduler.get_queue_for_report と同じ戻り値で、読み元を BQ に切り替えた版。

    キューは最新の queue_date の pending を priority_rank 順に返す。
    BQ が読めないときは scheduler 版（台帳・ミラー）に落とし、source にその旨を出す。
    last_run は直近の routine 実行（失敗時はメールに理由を出す用）。
    """
    try:
        client = client or _client()
        sql = f"""
        SELECT ticker, company_name AS name, sector_code, priority_rank, source, reason,
               final_score, composite_score, universe_score, queue_date
        FROM {_fq(client, QUEUE_TABLE)}
        WHERE queue_date = (SELECT MAX(queue_date) FROM {_fq(client, QUEUE_TABLE)})
          AND status = '{STATUS_PENDING}'
        ORDER BY priority_rank LIMIT {max(1, int(top_n))}"""
        queue = []
        for r in client.query(sql, location=LOCATION).result():
            d = dict(r.items())
            d["composite_score"] = d.get("final_score") if d.get("final_score") is not None \
                else d.get("composite_score")
            queue.append(d)
        cnt = list(client.query(
            f"SELECT COUNT(*) AS n FROM {_fq(client, LATEST_VIEW)}", location=LOCATION).result())[0]["n"]
        return {"queue": queue, "source": "BQ invest.deliberation_queue", "already_count": int(cnt),
                "partial": False, "mirror_age_days": None,
                "interval_days": int(_cfg(cfg, "DELIBERATION_INTERVAL_DAYS", 14)),
                "last_run": get_last_run(client)}
    except Exception as e:
        print(f"  ⚠️ 審議キューをBQから読めない（台帳・ミラーに切替）: {str(e)[:100]}")
        from deliberation_scheduler import get_queue_for_report as legacy
        out = legacy(cfg, results, top_n)
        out["source"] = f"{out.get('source', '')}（BQ読込失敗のため代替）"
        out["last_run"] = None
        return out
