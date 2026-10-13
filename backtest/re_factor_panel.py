# ==============================================================
# backtest/re_factor_panel.py — 含み益ファクターの検証用パネル（会社×年度）
# ==============================================================
# 【中身】1行＝1社×1年度（決算期末の年）。保有不動産のある会社だけ。
#   - 含み益・時価の目安：その年度の保有不動産行と、同じ行の参照地価（年次）から、BIマート m_re_holdings と
#     同じ定義（工業→商業→住宅の参照地価・土地簿価のある施設だけで比較・簿価20倍超の施設を除外）で出す。
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
WITH h AS (
  SELECT edinet_code, fiscal_year, book_value_land_m_yen AS bl,
    IF(land_area_m2 > 0 AND COALESCE(industrial_land_price_per_m2, commercial_land_price_per_m2, residential_land_price_per_m2) IS NOT NULL,
       land_area_m2 * COALESCE(industrial_land_price_per_m2, commercial_land_price_per_m2, residential_land_price_per_m2) / 1000000, NULL) AS est_raw
  FROM `{project}.real_estate.corporate_holdings_p1`
),
h2 AS (
  SELECT *, IF(COALESCE(bl, 0) > 0 AND est_raw > 20 * bl, NULL, est_raw) AS est,
            IF(COALESCE(bl, 0) > 0 AND est_raw > 20 * bl, 1, 0) AS excl
  FROM h
),
re AS (
  SELECT edinet_code, fiscal_year,
    SUM(est) AS land_value_est_m_yen,
    SUM(IF(bl > 0 AND est IS NOT NULL, est - bl, NULL)) AS gain_m_yen,
    SUM(IF(bl > 0 AND est IS NOT NULL, bl, NULL)) AS matched_book_m_yen,
    SUM(excl) AS excluded_count
  FROM h2 GROUP BY edinet_code, fiscal_year
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
       r.land_value_est_m_yen, r.gain_m_yen, r.matched_book_m_yen, r.excluded_count
FROM re AS r
JOIN `{project}.edinet.company_master` AS c ON c.edinet_code = r.edinet_code
LEFT JOIN fin AS f ON f.edinet_code = r.edinet_code AND f.fiscal_year = r.fiscal_year
LEFT JOIN sh AS s ON s.edinet_code = r.edinet_code AND s.fiscal_year = r.fiscal_year
LEFT JOIN doc AS d ON d.edinet_code = r.edinet_code AND d.fiscal_year_end = f.fiscal_year_end
WHERE r.fiscal_year BETWEEN 2016 AND 2026 AND c.sec_code IS NOT NULL AND r.gain_m_yen IS NOT NULL
ORDER BY code4, r.fiscal_year
"""

COLUMNS = ["edinet_code", "code4", "sector_code", "is_delisted", "fiscal_year", "fiscal_year_end",
           "disclosure_date", "equity", "cash_and_equiv", "interest_bearing_debt", "float_shares",
           "land_value_est_m_yen", "gain_m_yen", "matched_book_m_yen", "excluded_count"]


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
