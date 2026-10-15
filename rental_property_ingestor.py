# ==============================================================
# rental_property_ingestor.py
# 有報の注記「賃貸等不動産関係」（貸借対照表計上額・期末時価）を EdinetDB から取り、BQ に置く
# ==============================================================
# 【取得元】
#   EdinetDB REST `GET /v1/companies/{edinet_code}/real-estate`（1社1リクエスト）。
#   応答の real_estate_notes は年度ごとの配列（1回で過去15年度ほど返る）。各年度に
#     bs（土地・建物・投資不動産の貸借対照表計上額）と lease_disclosure（賃貸等不動産の簿価・期末時価・
#     賃貸損益・算定方法・書類ID）を持つ。金額は百万円に揃えられている。注記の無い年度は lease_disclosure が無い。
#   期末時価は会社が算定・鑑定した値で市場価格ではない。EdinetDB 側で時価÷簿価が 0.1 未満・30 超、
#   15兆円超の値は NULL にされている。
# 【書き込み】
#   real_estate.rental_property_fv（キー edinet_code, fiscal_year）へ、その回に取った全社分を1本の MERGE で書く
#   （会社ごとに MERGE を撃つと本数がそのまま費用になるため）。提出日と会計基準はビュー
#   real_estate.rental_property_fv_v で edinet.doc_index・edinet.company から付ける。
#   「取れたのに書けなかった」は失敗として返す。
# 【対象と順番】
#   保有不動産（corporate_holdings_p1）に土地簿価のある会社と不動産業（1032）を対象にし、
#   未取得の会社 → 前回取得より後に有価証券報告書が出た会社、の順に RENTAL_FV_DAILY_BUDGET 社まで。
#   取得状況は stock_data/data/rental_property_state.json に残す。
# 【予算】
#   EdinetDB の日次枠（EDINETDB_DAILY_BUDGET）を日次取り込みと共有する。日次取り込みの直前に
#   RENTAL_FV_DAILY_BUDGET 回までを使い、残りを日次取り込みが使う。枠切れ（last_error="budget"）で打ち切る。
# 【段階】
#   ENABLE_RENTAL_PROPERTY_FV が False の間は、RENTAL_FV_TRIAL_LIMIT 社だけを1回試す（検証用）。
#   試した結果を確認し、健太の承認後に True にして定常で回す。
# 【実行】 python3 rental_property_ingestor.py [--limit N] [--probe E03856]
# ==============================================================
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROBE_CODE = "E03856"

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS `{project}.real_estate.rental_property_fv` (
  edinet_code STRING NOT NULL,
  fiscal_year INT64 NOT NULL,
  doc_id STRING,
  book_value_m_yen FLOAT64,
  fair_value_m_yen FLOAT64,
  fv_bv_ratio FLOAT64,
  book_value_gap_m_yen FLOAT64,
  rental_revenue_m_yen FLOAT64,
  rental_expense_m_yen FLOAT64,
  rental_profit_m_yen FLOAT64,
  valuation_method STRING,
  region_description STRING,
  extraction_status STRING,
  bs_land_m_yen FLOAT64,
  bs_buildings_m_yen FLOAT64,
  bs_investment_property_m_yen FLOAT64,
  source STRING,
  updated_at TIMESTAMP
)
OPTIONS (description = '有報の注記「賃貸等不動産関係」（簿価・期末時価・賃貸損益）と貸借対照表の土地・建物・投資不動産。EdinetDB real-estate 由来・百万円。書き手 rental_property_ingestor.py')
"""

VIEW_DDL = """
CREATE OR REPLACE VIEW `{project}.real_estate.rental_property_fv_v`
OPTIONS (description = 'rental_property_fv に有報の提出日（doc_index）・会計基準（company）を付けたビュー。時点整合の検証は disclosure_date 以降だけ使う')
AS
SELECT f.*, d.disclosure_date, c.accounting_standard, SUBSTR(c.sec_code, 1, 4) AS code4
FROM `{project}.real_estate.rental_property_fv` AS f
LEFT JOIN (SELECT doc_id, MIN(disclosure_date) AS disclosure_date FROM `{project}.edinet.doc_index` GROUP BY doc_id) AS d
  ON d.doc_id = f.doc_id
LEFT JOIN `{project}.edinet.company` AS c ON c.edinet_code = f.edinet_code
"""

TARGET_SQL = """
SELECT edinet_code FROM (
  SELECT DISTINCT h.edinet_code FROM `{project}.real_estate.corporate_holdings_p1` AS h
  WHERE h.book_value_land_m_yen > 0
  UNION DISTINCT
  SELECT edinet_code FROM `{project}.edinet.company_master` WHERE sector_code = '1032' AND NOT IFNULL(is_delisted, FALSE)
)
"""

LATEST_DOC_SQL = """
SELECT edinet_code, MAX(disclosure_date) AS latest
FROM `{project}.edinet.doc_index`
WHERE doc_type = '有価証券報告書' AND edinet_code IN UNNEST(@codes)
GROUP BY edinet_code
"""


# ── 純関数 ────────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x


def map_real_estate(edinet_code: str, data: dict, now: str | None = None) -> list[dict]:
    """real-estate の応答 → rental_property_fv の行（年度ごと）。注記も貸借対照表の値も無い年度は入れない。"""
    now = now or datetime.now(timezone.utc).isoformat()
    out = []
    for n in (data or {}).get("real_estate_notes") or []:
        fy = n.get("fiscal_year")
        if fy is None:
            continue
        le = n.get("lease_disclosure") or {}
        bs = n.get("bs") or {}
        row = {
            "edinet_code": edinet_code, "fiscal_year": int(fy),
            "doc_id": le.get("doc_id"),
            "book_value_m_yen": _f(le.get("book_value_m_yen")),
            "fair_value_m_yen": _f(le.get("fair_value_m_yen")),
            "fv_bv_ratio": _f(le.get("fv_bv_ratio")),
            "book_value_gap_m_yen": _f(le.get("book_value_gap_m_yen")),
            "rental_revenue_m_yen": _f(le.get("rental_revenue_m_yen")),
            "rental_expense_m_yen": _f(le.get("rental_expense_m_yen")),
            "rental_profit_m_yen": _f(le.get("rental_profit_m_yen")),
            "valuation_method": le.get("valuation_method"),
            "region_description": le.get("region_description"),
            "extraction_status": le.get("extraction_status") or ("no_note" if not le else None),
            "bs_land_m_yen": _f(bs.get("land_m_yen")),
            "bs_buildings_m_yen": _f(bs.get("buildings_m_yen")),
            "bs_investment_property_m_yen": _f(bs.get("investment_property_m_yen")),
            "source": "edinetdb_real_estate", "updated_at": now,
        }
        values = [row[k] for k in ("book_value_m_yen", "fair_value_m_yen", "bs_land_m_yen",
                                   "bs_buildings_m_yen", "bs_investment_property_m_yen")]
        if any(v is not None for v in values):
            out.append(row)
    return out


def pick_targets(candidates: list[str], state: dict, latest_doc: dict, limit: int) -> list[str]:
    """未取得の会社 → 前回取得より後に有報が出た会社、の順で limit 社（純関数）。"""
    never = [c for c in candidates if c not in state]
    stale = [c for c in candidates if c in state and latest_doc.get(c)
             and str(latest_doc[c]) > str(state[c].get("fetched", ""))]
    stale.sort(key=lambda c: str(state[c].get("fetched", "")))
    return (never + stale)[:max(0, int(limit))]


# ── I/O ─────────────────────────────────────────────────────

def _client(cfg):
    from db_rest_client import DBRestClient
    api_key = str(getattr(cfg, "EDINETDB_API_KEY", "") or "").strip()
    if not api_key:
        return None
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    return DBRestClient(
        getattr(cfg, "EDINETDB_BASE_URL", "https://edinetdb.jp/v1"), api_key,
        data_dir / "edinetdb_cache", int(getattr(cfg, "EDINETDB_DAILY_BUDGET", 97)), "edinetdb",
    )


def _state_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "rental_property_state.json"


def probe(cfg, edinet_code: str) -> int:
    """1社分の生の応答を stock_data/data/edinetdb_real_estate_probe_<code>.json に保存する。"""
    client = _client(cfg)
    if client is None:
        print("❌ EDINETDB_API_KEY がない")
        return 1
    data = client.get(f"/companies/{edinet_code}/real-estate")
    if data is None:
        print(f"❌ 取得できない（last_error={getattr(client, 'last_error', None)}）")
        return 1
    out = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / f"edinetdb_real_estate_probe_{edinet_code}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"fetched": date.today().isoformat(), "edinet_code": edinet_code, "data": data},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ 保存: {out}")
    return 0


def ensure_table(bq) -> None:
    try:
        bq.client.get_table(f"{bq.project}.real_estate.rental_property_fv")
    except Exception:
        bq.client.query(TABLE_DDL.format(project=bq.project)).result()
        bq.client.query(VIEW_DDL.format(project=bq.project)).result()
        print("    🗂 real_estate.rental_property_fv とビューを作成")


def run(cfg, limit: int | None = None) -> dict:
    """対象を選んで取り、1本の MERGE で書く。日次処理は EdinetDB 取り込みの直前に呼ぶ。"""
    from google.cloud import bigquery
    enabled = bool(getattr(cfg, "ENABLE_RENTAL_PROPERTY_FV", False))
    state_path = _state_path(cfg)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    meta = state.setdefault("_meta", {})
    companies = {k: v for k, v in state.items() if not k.startswith("_")}
    if limit is None:
        if enabled:
            limit = int(getattr(cfg, "RENTAL_FV_DAILY_BUDGET", 15))
        elif not meta.get("trial_done"):
            limit = int(getattr(cfg, "RENTAL_FV_TRIAL_LIMIT", 10))
        else:
            return {"skipped": True, "reason": "ENABLE_RENTAL_PROPERTY_FV=False（試行済み・承認待ち）"}
    client = _client(cfg)
    if client is None:
        return {"skipped": True, "reason": "EDINETDB_API_KEY missing"}
    from edinet_bq_client import EDINETBQClient
    bq = EDINETBQClient()
    ensure_table(bq)
    cands = sorted({r.edinet_code for r in bq.client.query(TARGET_SQL.format(project=bq.project)).result()})
    latest = {r.edinet_code: str(r.latest) for r in bq.client.query(
        LATEST_DOC_SQL.format(project=bq.project),
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("codes", "STRING", [c for c in cands if c in companies])])).result()} \
        if companies else {}
    targets = pick_targets(cands, companies, latest, limit)
    rows, fetched, empty, failed = [], [], [], []
    today = date.today().isoformat()
    for code in targets:
        data = client.get(f"/companies/{code}/real-estate")
        if data is None:
            if getattr(client, "last_error", None) == "budget":
                break
            failed.append(code)
            continue
        got = map_real_estate(code, data)
        rows.extend(got)
        (fetched if any(r["fair_value_m_yen"] is not None for r in got) else empty).append(code)
        state[code] = {"fetched": today, "years": len(got),
                       "fv_years": sum(1 for r in got if r["fair_value_m_yen"] is not None)}
    written = False
    if rows:
        written = bool(bq.merge_rows("rental_property_fv", rows, ["edinet_code", "fiscal_year"], dataset="real_estate"))
    if not enabled and getattr(client, "last_error", None) != "budget":
        meta["trial_done"] = today
    meta["last_run"] = today
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if rows and not written:
        for code in fetched + empty:
            state.pop(code, None)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    info = {"enabled": enabled, "candidates": len(cands), "targets": len(targets), "with_fair_value": len(fetched),
            "no_fair_value": len(empty), "failed": len(failed), "rows": len(rows), "written": written,
            "remaining_never": sum(1 for c in cands if c not in state),
            "budget_stop": getattr(client, "last_error", None) == "budget"}
    print(f"  🏢 賃貸等不動産: {info}")
    if rows and not written:
        info["error"] = "取れたのに BQ に書けなかった"
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="EDINET_CODE", help="1社分の生の応答を保存する")
    ap.add_argument("--limit", type=int, help="この回に取る社数（既定は設定値）")
    args = ap.parse_args(argv)
    import config
    if args.probe:
        return probe(config, args.probe)
    info = run(config, args.limit)
    return 1 if info.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
