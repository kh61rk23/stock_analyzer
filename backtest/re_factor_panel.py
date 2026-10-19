# ==============================================================
# backtest/re_factor_panel.py — 含み益ファクターの検証用パネル（会社×年度）
# ==============================================================
# 【中身】1行＝1社×1年度（決算期末の年）。保有不動産のある会社だけ。
#   - 含み益・時価の目安：その年度の保有不動産行と、同じ行の参照地価（年次）から、BIマート m_re_holdings と
#     同じ定義（工業→商業→住宅の参照地価・補正係数 cal・土地簿価のある施設だけで比較・簿価20倍超の施設を除外）で出す。
#   - 開示時価（real_estate.rental_property_fv）がある会社年度は、m_screener と同じ規則で推定の賃貸分を開示の含み益に
#     置き換えた値を gain_m_yen に入れる（推定だけの値は gain_est_m_yen）。使う開示は同じ年度、無ければ前年度。
#   - 純資産・現金・有利子負債：edinet.financials の通期（同じ年度の最新更新行）。単位は円。
#   - 流通株式数：edinet.financial_items の float_shares（発行済−自己株式・期末）。
#   - 提出日：edinet.doc_index の有価証券報告書の提出日（決算期末で突き合わせ）。無い年度は空のまま残し、
#     使う側で「決算期末＋3か月」とみなす。
#   - 業種コード：edinet.company_master（東証33業種）。上場廃止の会社も残す（生存者バイアスを減らすため）。
# 【使い方】
#   BQ に入れる手段がある環境：python3 -m backtest.re_factor_panel --out stock_data/data/backtest/re_factor_panel.csv
#   BQ MCP で SQL を流した結果（JSON）がある環境：--from-mcp-json <page1> <page2> …
# ==============================================================
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PANEL_SQL = """
WITH cal AS (
  SELECT * FROM UNNEST([
    STRUCT('industrial' AS price_kind, 'metro' AS region, 1.02 AS k),
    STRUCT('commercial' AS price_kind, 'metro' AS region, 0.77 AS k)
  ])
),
h AS (
  SELECT edinet_code, fiscal_year, book_value_land_m_yen AS bl, book_value_buildings_m_yen AS bb,
    CASE WHEN industrial_land_price_per_m2 IS NOT NULL THEN 'industrial'
         WHEN commercial_land_price_per_m2 IS NOT NULL THEN 'commercial'
         WHEN residential_land_price_per_m2 IS NOT NULL THEN 'residential' END AS price_kind,
    IF(SUBSTR(municipality_code, 1, 2) IN ('11', '12', '13', '14', '21', '23', '24', '26', '27', '28', '29'), 'metro', 'other') AS region,
    IF(land_area_m2 > 0 AND COALESCE(industrial_land_price_per_m2, commercial_land_price_per_m2, residential_land_price_per_m2) IS NOT NULL,
       land_area_m2 * COALESCE(industrial_land_price_per_m2, commercial_land_price_per_m2, residential_land_price_per_m2) / 1000000, NULL) AS est_raw
  FROM `{project}.real_estate.corporate_holdings_p1`
),
h2 AS (
  SELECT h.*, IF(COALESCE(bl, 0) > 0 AND est_raw > 20 * bl, NULL, est_raw * COALESCE(cal.k, 1.0)) AS est,
            IF(COALESCE(bl, 0) > 0 AND est_raw > 20 * bl, 1, 0) AS excl
  FROM h LEFT JOIN cal USING (price_kind, region)
),
re AS (
  SELECT edinet_code, fiscal_year,
    SUM(est) AS land_value_est_m_yen,
    SUM(IF(bl > 0 AND est IS NOT NULL, est - bl, NULL)) AS gain_est_m_yen,
    SUM(IF(bl > 0 AND est IS NOT NULL, bl, NULL)) AS matched_book_m_yen,
    SUM(IFNULL(bl, 0)) + SUM(IFNULL(bb, 0)) AS book_total_m_yen,
    SUM(excl) AS excluded_count
  FROM h2 GROUP BY edinet_code, fiscal_year
),
fv AS (
  SELECT edinet_code, fiscal_year, source, book_value_m_yen AS rb, fair_value_m_yen AS rf
  FROM `{project}.real_estate.rental_property_fv`
  WHERE fair_value_m_yen > 0 AND book_value_m_yen > 0 AND fair_value_m_yen / book_value_m_yen BETWEEN 0.1 AND 30
),
keys AS (
  SELECT edinet_code, fiscal_year FROM re
  UNION DISTINCT
  SELECT edinet_code, fiscal_year FROM fv
),
-- 同じ年度の開示（同じ有報）を優先し、無ければ前年度（先に提出済み）を使う。後の年度は使わない（先読みになるため）
rental AS (
  SELECT k.edinet_code, k.fiscal_year, f.fiscal_year AS rental_fv_year, f.source AS rental_source, f.rb, f.rf
  FROM keys AS k
  JOIN fv AS f ON f.edinet_code = k.edinet_code AND f.fiscal_year BETWEEN k.fiscal_year - 1 AND k.fiscal_year
  QUALIFY ROW_NUMBER() OVER (PARTITION BY k.edinet_code, k.fiscal_year
                             ORDER BY f.fiscal_year DESC, IF(f.source = 'edinet_gov_xbrl', 0, 1)) = 1
),
mix AS (
  SELECT k.edinet_code, k.fiscal_year, r.land_value_est_m_yen, r.gain_est_m_yen, r.matched_book_m_yen, r.excluded_count,
    t.rental_fv_year, t.rental_source, t.rf - t.rb AS rental_gain_m_yen,
    IF(t.edinet_code IS NULL, NULL, IF(r.book_total_m_yen > 0, LEAST(1, t.rb / r.book_total_m_yen), 1)) AS rental_share
  FROM keys AS k
  LEFT JOIN re AS r ON r.edinet_code = k.edinet_code AND r.fiscal_year = k.fiscal_year
  LEFT JOIN rental AS t ON t.edinet_code = k.edinet_code AND t.fiscal_year = k.fiscal_year
),
blend AS (
  SELECT *,
    CASE WHEN rental_share IS NULL THEN gain_est_m_yen
         WHEN gain_est_m_yen IS NULL OR rental_share >= 1 THEN rental_gain_m_yen
         ELSE gain_est_m_yen * (1 - rental_share) + rental_gain_m_yen END AS gain_m_yen,
    CASE WHEN rental_share IS NULL THEN IF(gain_est_m_yen IS NULL, NULL, 'estimate')
         WHEN gain_est_m_yen IS NULL OR rental_share >= 1 THEN 'disclosed'
         ELSE 'estimate+disclosed' END AS gain_basis
  FROM mix
),
fin AS (
  SELECT edinet_code, fiscal_year, fiscal_year_end, equity, cash_and_equiv, interest_bearing_debt
  FROM `{project}.edinet.financials`
  WHERE report_period = 'FY'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY edinet_code, fiscal_year ORDER BY updated_at DESC) = 1
),
sh AS (
  SELECT edinet_code, fiscal_year, value AS float_shares
  FROM `{project}.edinet.financial_items`
  WHERE item = 'float_shares'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY edinet_code, fiscal_year ORDER BY updated_at DESC) = 1
),
doc AS (
  SELECT edinet_code, fiscal_year_end, MIN(disclosure_date) AS disclosure_date
  FROM `{project}.edinet.doc_index`
  WHERE doc_type = '有価証券報告書'
  GROUP BY edinet_code, fiscal_year_end
)
SELECT r.edinet_code, SUBSTR(c.sec_code, 1, 4) AS code4, c.sector_code, c.is_delisted, r.fiscal_year,
       f.fiscal_year_end, d.disclosure_date, f.equity, f.cash_and_equiv, f.interest_bearing_debt, s.float_shares,
       ROUND(r.land_value_est_m_yen, 1) AS land_value_est_m_yen, ROUND(r.gain_m_yen, 1) AS gain_m_yen,
       ROUND(r.matched_book_m_yen, 1) AS matched_book_m_yen, r.excluded_count,
       ROUND(r.gain_est_m_yen, 1) AS gain_est_m_yen, ROUND(r.rental_gain_m_yen, 1) AS rental_gain_m_yen,
       ROUND(r.rental_share, 4) AS rental_share, r.rental_fv_year, r.rental_source, r.gain_basis
FROM blend AS r
JOIN `{project}.edinet.company_master` AS c ON c.edinet_code = r.edinet_code
LEFT JOIN fin AS f ON f.edinet_code = r.edinet_code AND f.fiscal_year = r.fiscal_year
LEFT JOIN sh AS s ON s.edinet_code = r.edinet_code AND s.fiscal_year = r.fiscal_year
LEFT JOIN doc AS d ON d.edinet_code = r.edinet_code AND d.fiscal_year_end = f.fiscal_year_end
WHERE r.fiscal_year BETWEEN 2016 AND 2026 AND c.sec_code IS NOT NULL AND r.gain_m_yen IS NOT NULL
ORDER BY code4, r.fiscal_year
"""

COLUMNS = ["edinet_code", "code4", "sector_code", "is_delisted", "fiscal_year", "fiscal_year_end",
           "disclosure_date", "equity", "cash_and_equiv", "interest_bearing_debt", "float_shares",
           "land_value_est_m_yen", "gain_m_yen", "matched_book_m_yen", "excluded_count",
           "gain_est_m_yen", "rental_gain_m_yen", "rental_share", "rental_fv_year", "rental_source", "gain_basis"]


def rows_from_mcp_json(path: Path) -> list[dict]:
    """BQ MCP の execute_sql 結果（schema.fields と rows[].f[].v）を dict の行にする（純関数）。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    names = [f["name"] for f in d["schema"]["fields"]]
    return [{n: c.get("v") for n, c in zip(names, r["f"])} for r in d.get("rows", [])]


def write_csv(rows: list[dict], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="stock_data/data/backtest/re_factor_panel.csv")
    ap.add_argument("--from-mcp-json", nargs="+", help="BQ MCP の結果JSON（ページごとに複数可）から作る")
    ap.add_argument("--print-sql", action="store_true")
    args = ap.parse_args(argv)
    if args.print_sql:
        print(PANEL_SQL.format(project="stock-analyzer-edinet"))
        return 0
    if args.from_mcp_json:
        rows = [r for p in args.from_mcp_json for r in rows_from_mcp_json(Path(p))]
    else:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
        rows = [dict(r.items()) for r in client.query(PANEL_SQL.format(project=client.project)).result()]
    n = write_csv(rows, Path(args.out))
    print(f"✅ {args.out}: {n}行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
