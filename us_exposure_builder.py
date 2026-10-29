# ==============================================================
# us_exposure_builder.py — 米国上場会社の国別売上（10-K の XBRL 地域別セグメント）を権益の表へ
# ==============================================================
# 【入力】us.segment_financials（axis='Geographical'・通年 qtrs=4）の国コードの member と、
#   同じ決算期の売上（us.financials_annual.revenue）。国コード（US・CN・JP など2文字）の member だけ使い、
#   地域（Europe・AsiaPacific など）は国に割れないので使わない。
# 【出力】geopolitics.company_country_exposure（market='US'・source='sec_xbrl'・SALES_NETWORK）。
#   毎回作り直す：source='sec_xbrl' の行を消してから入れる（load job は無料・DELETE は1本）。
# 【読む量】US の表をまとめて読むクエリ1本（約 100MB）。1社ずつは引かない。
# 【使い方】python us_exposure_builder.py [--dry]
# ==============================================================
from __future__ import annotations

import argparse
import io
import json
import sys
import uuid
from datetime import date, datetime, timezone

SOURCE = "sec_xbrl"
MIN_SHARE = 0.03
MAX_RATIO = 1.2           # 国の売上が通期の売上をこれ以上超える組は期や単位の食い違いとみて捨てる
TABLE = "geopolitics.company_country_exposure"

# 2文字の国コード → 3文字（country_risk_daily と同じ ISO 3166-1 alpha-3）
ISO3 = {
    "US": "USA", "CA": "CAN", "MX": "MEX", "BR": "BRA", "AR": "ARG", "CL": "CHL", "CO": "COL", "PE": "PER",
    "GB": "GBR", "IE": "IRL", "DE": "DEU", "FR": "FRA", "IT": "ITA", "ES": "ESP", "PT": "PRT", "NL": "NLD",
    "BE": "BEL", "LU": "LUX", "CH": "CHE", "AT": "AUT", "SE": "SWE", "NO": "NOR", "DK": "DNK", "FI": "FIN",
    "PL": "POL", "CZ": "CZE", "HU": "HUN", "RO": "ROU", "GR": "GRC", "TR": "TUR", "RU": "RUS", "UA": "UKR",
    "IL": "ISR", "SA": "SAU", "AE": "ARE", "QA": "QAT", "KW": "KWT", "EG": "EGY", "ZA": "ZAF", "NG": "NGA",
    "KE": "KEN", "MA": "MAR", "CN": "CHN", "HK": "HKG", "TW": "TWN", "JP": "JPN", "KR": "KOR", "IN": "IND",
    "SG": "SGP", "MY": "MYS", "TH": "THA", "ID": "IDN", "PH": "PHL", "VN": "VNM", "AU": "AUS", "NZ": "NZL",
    "PK": "PAK", "BD": "BGD", "KZ": "KAZ", "PR": "PRI", "BM": "BMU", "KY": "CYM", "PA": "PAN", "CR": "CRI",
    "DO": "DOM", "GT": "GTM", "VE": "VEN", "EC": "ECU", "UY": "URY", "IS": "ISL", "EE": "EST", "LT": "LTU",
    "LV": "LVA", "SK": "SVK", "SI": "SVN", "HR": "HRV", "RS": "SRB", "BG": "BGR", "CY": "CYP", "MT": "MLT",
}

SQL = """
WITH g AS (
  SELECT cik, member, period_end, fy, filed, tag, val
  FROM `{p}.us.segment_financials`
  WHERE axis = 'Geographical' AND qtrs = 4 AND uom = 'USD' AND val > 0
    AND REGEXP_CONTAINS(member, r'^[A-Z]{{2}}$')
    AND tag IN ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues',
                'RevenueFromContractWithCustomerIncludingAssessedTax')
),
latest AS (   -- 会社ごとに一番新しい通期
  SELECT cik, period_end FROM g
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cik ORDER BY period_end DESC, filed DESC) = 1
),
per_country AS (   -- 同じ国・同じ期で複数のタグ・提出があれば、新しい提出の大きい値
  SELECT g.cik, g.member, g.period_end, ANY_VALUE(g.fy) fy, MAX(g.val) val
  FROM g JOIN latest USING (cik, period_end)
  GROUP BY 1, 2, 3
)
SELECT p.ticker, r.cik, r.member, r.period_end, r.fy, r.val, f.revenue
FROM per_country r
JOIN `{p}.us.company_profile` p ON p.cik = r.cik
JOIN `{p}.us.financials_annual` f ON f.cik = r.cik AND f.period_end = r.period_end
WHERE p.ticker IS NOT NULL AND f.revenue > 0
"""


def build_rows(records: list[dict], today: str | None = None) -> list[dict]:
    """クエリの行 → company_country_exposure の行（純関数）。"""
    today = today or date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    out, seen = [], set()
    for r in records:
        iso3 = ISO3.get(str(r.get("member") or ""))
        rev, val = r.get("revenue") or 0, r.get("val") or 0
        if not iso3 or rev <= 0 or val / rev > MAX_RATIO:
            continue
        share = round(min(1.0, val / rev), 4)
        key = (r["ticker"], iso3)
        if share < MIN_SHARE or key in seen:
            continue
        seen.add(key)
        out.append({
            "exposure_id": str(uuid.uuid4()), "company_id": r["ticker"], "ticker": r["ticker"], "market": "US",
            "country_code": iso3, "exposure_type": "SALES_NETWORK", "exposure_strength": share,
            "commodity_code": None, "revenue_share_est": share, "employee_share_est": None, "asset_share_est": None,
            "source": SOURCE, "evidence_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={r['cik']}&type=10-K",
            "rationale": f"10-K XBRL 地域別売上 FY{r.get('fy')}（{r.get('period_end')}）: {r['member']} {share:.1%}",
            "auto_derived": True, "verified_flag": False, "review_flag": False,
            "valid_from": today, "valid_to": None, "updated_at": now,
        })
    return out


def fetch(client) -> list[dict]:
    return [dict(r.items()) for r in client.query(SQL.format(p=client.project), location="US").result()]


def write(client, rows: list[dict]) -> int:
    from google.cloud import bigquery
    p = client.project
    client.query(f"DELETE FROM `{p}.{TABLE}` WHERE source = @s",
                 job_config=bigquery.QueryJobConfig(query_parameters=[
                     bigquery.ScalarQueryParameter("s", "STRING", SOURCE)]),
                 location="asia-northeast1").result()
    if not rows:
        return 0
    buf = io.BytesIO("\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows).encode("utf-8"))
    client.load_table_from_file(buf, f"{p}.{TABLE}", location="asia-northeast1", job_config=bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND)).result()
    return len(rows)


def run(cfg=None, dry: bool = False, client=None) -> dict:
    if client is None:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
    rows = build_rows(fetch(client))
    n = 0 if dry else write(client, rows)
    return {"rows": len(rows), "companies": len({r["ticker"] for r in rows}),
            "countries": len({r["country_code"] for r in rows}), "written": n}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    print(f"✅ us_exposure_builder: {run(dry=a.dry)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
