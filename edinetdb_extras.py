# ==============================================================
# edinetdb_extras.py — EdinetDB API のまだ取っていない窓口を、1日 EDINETDB_EXTRAS_DAILY_CALLS 回の枠で順番に取る
# ==============================================================
# 【窓口】ENDPOINTS（窓口 → 取り直すまでの日数）。役割が他の取り込みと重なる窓口は取らない
#   （shareholders＝政府 EDINET の大量保有 edinet.large_holdings、facilities＝不動産DB と有報の本文の「主要な設備」）。
#   earnings 決算短信（速報業績）／cross-shareholdings 政策保有株の銘柄別／subsidiaries 子会社・関係会社／
#   main-customers 主要販売先（載っている会社は少ない。無ければ 404＝データ無しとして記録）
# 【順番】保有銘柄 → スクリーナー候補 → 業種の代表 → その他の上場会社の順に、窓口ごとに「未取得か期限切れ」を並べる。
#   同じ会社の中では ENDPOINTS の並び順。1日の枠を使い切ったらその日は終わり（残りは翌日）。
# 【枠】API の1日100回（EDINETDB_DAILY_BUDGET）を本体の取り込み（edinetdb_ingestor）と分け合う。
#   本体は EDINETDB_EXTRAS_DAILY_CALLS を残して止まり（_w3_company_budget）、この取り込みは本体の後に動く。
#   回数は db_rest_client の日次台帳（edinetdb_cache/daily_<日付>.json）を本体と共有する。
# 【表】edinet.edinetdb_extras（load job で追記）：endpoint・edinet_code・fetched_at・payload（JSON。返事の data そのまま）。
#   SQL は JSON 関数で引く（例: JSON_QUERY_ARRAY(payload) で配列を行に開く）。
# 【進み具合】stock_data/data/edinetdb_extras_state.json（{窓口: {EDINETコード: {"at": 日付, "status": ok|no_data}}}）
# 【容量】全体が BQ_STORAGE_STOP_GB、またはこの表が EDINETDB_EXTRAS_MAX_GB を超えたら取らない。
# ==============================================================
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

ENDPOINTS = {"earnings": 30, "cross-shareholdings": 180, "subsidiaries": 180, "main-customers": 180}
DATASET, TABLE, LOCATION = "edinet", "edinetdb_extras", "asia-northeast1"
SCHEMA = [("endpoint", "STRING"), ("edinet_code", "STRING"), ("fetched_at", "TIMESTAMP"), ("payload", "JSON")]


def plan(companies_by_tier: list[list[str]], state: dict, today: date, limit: int,
         endpoints: dict = ENDPOINTS) -> list[tuple[str, str]]:
    """[(窓口, EDINETコード)] を取る順に limit 件（純関数）。未取得・期限切れだけ。"""
    out, seen = [], set()
    for tier in companies_by_tier:
        for code in tier:
            if code in seen:
                continue
            seen.add(code)
            for ep, days in endpoints.items():
                rec = (state.get(ep) or {}).get(code) or {}
                try:
                    age = (today - date.fromisoformat(rec["at"])).days
                except (KeyError, ValueError, TypeError):
                    age = None
                if age is None or age >= days:
                    out.append((ep, code))
                    if len(out) >= limit:
                        return out
    return out


def _tiers(cfg, companies: list[dict]) -> list[list[str]]:
    """保有 → スクリーナー候補 → 業種の代表 → その他（上場・EDINET コードあり）。"""
    import edinetdb_ingestor as ing
    listed = [c for c in companies if c.get("edinet_code") and not c.get("is_delisted")]
    by_sec = {str(c.get("sec_code") or "")[:4]: c["edinet_code"] for c in listed if c.get("sec_code")}
    tiers = []
    for tickers in ing._priority_tickers(cfg):
        tiers.append([by_sec[str(t).split(".")[0][:4]] for t in tickers if str(t).split(".")[0][:4] in by_sec])
    tiers.append([c["edinet_code"] for c in listed])
    return tiers


def _ensure_table(client) -> None:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    ref = f"{client.project}.{DATASET}.{TABLE}"
    try:
        client.get_table(ref)
    except NotFound:
        t = bigquery.Table(ref, schema=[bigquery.SchemaField(n, ty) for n, ty in SCHEMA])
        t.clustering_fields = ["endpoint", "edinet_code"]
        client.create_table(t)


def _append(client, rows: list[dict]) -> None:
    from google.cloud import bigquery
    if not rows:
        return
    cfg = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                                 write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                                 schema=[bigquery.SchemaField(n, t) for n, t in SCHEMA])
    client.load_table_from_json(rows, f"{client.project}.{DATASET}.{TABLE}", job_config=cfg, location=LOCATION).result()


def run(cfg) -> dict:
    limit = int(getattr(cfg, "EDINETDB_EXTRAS_DAILY_CALLS", 20))
    if limit <= 0 or not str(getattr(cfg, "EDINETDB_API_KEY", "") or "").strip():
        return {"skipped": True, "reason": "枠0 または EDINETDB_API_KEY なし"}
    import bq_storage
    import state_store
    from db_rest_client import DBRestClient
    client = state_store.make_client(cfg)
    _ensure_table(client)
    m = bq_storage.measure(client)
    mine = m["tables"].get(f"{DATASET}.{TABLE}", 0.0)
    if not bq_storage.allowed(cfg, m) or mine >= float(getattr(cfg, "EDINETDB_EXTRAS_MAX_GB", 0.3)):
        return {"skipped": True, "reason": f"保存量の上限（全体 {m['total_gb']}GB・この表 {mine:.3f}GB）"}
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    try:
        companies = json.loads((data_dir / "edinetdb_company_cache.json").read_text(encoding="utf-8")).get("companies") or []
    except (OSError, ValueError):
        companies = []
    path = data_dir / "edinetdb_extras_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    today = date.today()
    todo = plan(_tiers(cfg, companies), state, today, limit)
    api = DBRestClient.edinetdb(cfg)
    rows, stat = [], {"planned": len(todo), "ok": 0, "no_data": 0, "stopped": None, "by_endpoint": {}}
    now = datetime.now(timezone.utc).isoformat()
    for ep, code in todo:
        data = api.get(f"/companies/{code}/{ep}")
        if data is None:
            if api.last_error == "not_found":
                state.setdefault(ep, {})[code] = {"at": today.isoformat(), "status": "no_data"}
                stat["no_data"] += 1
                continue
            stat["stopped"] = api.last_error          # 枠切れ・429・通信の失敗はそこで止めて翌日にやり直す
            break
        rows.append({"endpoint": ep, "edinet_code": code, "fetched_at": now, "payload": data})
        state.setdefault(ep, {})[code] = {"at": today.isoformat(), "status": "ok"}
        stat["ok"] += 1
        stat["by_endpoint"][ep] = stat["by_endpoint"].get(ep, 0) + 1
    _append(client, rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    listed = sum(1 for c in companies if c.get("edinet_code") and not c.get("is_delisted"))
    stat["progress"] = {ep: f"{len(state.get(ep) or {})}/{listed}" for ep in ENDPOINTS}
    return stat
