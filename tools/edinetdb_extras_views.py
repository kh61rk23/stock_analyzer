"""EdinetDB の追加の窓口（edinet.edinetdb_extras・JSON）を、SQL で引ける view に分ける。

  edinet.v_extras_earnings          決算短信（1社1決算1行）
  edinet.v_extras_cross_shareholdings 政策保有株（1社1銘柄1行）
  edinet.v_extras_subsidiaries      関係会社（1社1子会社1行・国つき）
  edinet.v_extras_main_customers    主要販売先（1社1販売先1行）
同じ会社・窓口は取り直した最新の1件（fetched_at）だけを使う。実行: python3 tools/edinetdb_extras_views.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LATEST = ("SELECT edinet_code, fetched_at, payload FROM `{p}.edinet.edinetdb_extras` WHERE endpoint = '{ep}' "
          "QUALIFY ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY fetched_at DESC) = 1")

VIEWS = {
    "v_extras_earnings": ("決算短信（EdinetDB earnings）", "earnings", "JSON_QUERY_ARRAY(l.payload, '$.earnings')", """
      JSON_VALUE(x, '$.fiscal_year_end') fiscal_year_end, JSON_VALUE(x, '$.quarter') quarter,
      JSON_VALUE(x, '$.disclosure_date') disclosure_date, JSON_VALUE(x, '$.accounting_standard') accounting_standard,
      SAFE_CAST(JSON_VALUE(x, '$.revenue') AS FLOAT64) revenue, SAFE_CAST(JSON_VALUE(x, '$.operating_income') AS FLOAT64) operating_income,
      SAFE_CAST(JSON_VALUE(x, '$.net_income') AS FLOAT64) net_income, SAFE_CAST(JSON_VALUE(x, '$.diluted_eps') AS FLOAT64) diluted_eps,
      SAFE_CAST(JSON_VALUE(x, '$.dividend_per_share') AS FLOAT64) dividend_per_share,
      SAFE_CAST(JSON_VALUE(x, '$.effective_forecast_dividend_per_share') AS FLOAT64) forecast_dividend_per_share,
      SAFE_CAST(JSON_VALUE(x, '$.cf_operating') AS FLOAT64) cf_operating,
      SAFE_CAST(JSON_VALUE(x, '$.forecast_revenue') AS FLOAT64) forecast_revenue,
      SAFE_CAST(JSON_VALUE(x, '$.forecast_operating_income') AS FLOAT64) forecast_operating_income,
      SAFE_CAST(JSON_VALUE(x, '$.is_correction') AS BOOL) is_correction"""),
    "v_extras_cross_shareholdings": ("政策保有株（EdinetDB cross-shareholdings）", "cross-shareholdings", "JSON_QUERY_ARRAY(l.payload)", """
      SAFE_CAST(JSON_VALUE(x, '$.fiscalYear') AS INT64) fiscal_year, SAFE_CAST(JSON_VALUE(x, '$.rank') AS INT64) rank,
      JSON_VALUE(x, '$.issuerName') issuer_name, JSON_VALUE(x, '$.securityCode') security_code,
      SAFE_CAST(JSON_VALUE(x, '$.sharesHeld') AS FLOAT64) shares_held, SAFE_CAST(JSON_VALUE(x, '$.bookValue') AS FLOAT64) book_value_yen,
      JSON_VALUE(x, '$.purposeText') purpose, JSON_VALUE(x, '$.docID') doc_id"""),
    "v_extras_subsidiaries": ("関係会社（EdinetDB subsidiaries）", "subsidiaries", "JSON_QUERY_ARRAY(l.payload)", """
      JSON_VALUE(x, '$.subsidiaryName') subsidiary_name, JSON_VALUE(x, '$.relationType') relation_type,
      JSON_VALUE(x, '$.countryClass') country_class, JSON_VALUE(x, '$.subsidiaryCountry') country,
      JSON_VALUE(x, '$.subsidiaryBusiness') business, SAFE_CAST(JSON_VALUE(x, '$.isConsolidated') AS BOOL) is_consolidated,
      SAFE_CAST(JSON_VALUE(x, '$.votingRightsPct') AS FLOAT64) voting_rights_pct,
      SAFE_CAST(JSON_VALUE(x, '$.subsidiaryCapitalYen') AS FLOAT64) capital_yen"""),
    "v_extras_main_customers": ("主要販売先（EdinetDB main-customers）", "main-customers", "JSON_QUERY_ARRAY(l.payload)", """
      SAFE_CAST(JSON_VALUE(x, '$.fiscalYear') AS INT64) fiscal_year, JSON_VALUE(x, '$.customerName') customer_name,
      JSON_VALUE(x, '$.customerCountry') customer_country, JSON_VALUE(x, '$.segment') segment,
      SAFE_CAST(JSON_VALUE(x, '$.salesAmountYen') AS FLOAT64) sales_yen, SAFE_CAST(JSON_VALUE(x, '$.salesSharePct') AS FLOAT64) sales_share_pct,
      JSON_VALUE(x, '$.confidence') confidence, JSON_VALUE(x, '$.evidenceQuote') evidence"""),
}


def view_sql(p: str, name: str) -> str:
    desc, ep, arr, cols = VIEWS[name]
    return (f"CREATE OR REPLACE VIEW `{p}.edinet.{name}` OPTIONS (description = '{desc}・tools/edinetdb_extras_views.py') AS\n"
            f"WITH l AS ({LATEST.format(p=p, ep=ep)})\n"
            f"SELECT l.edinet_code, c.sec_code, c.name AS company_name, l.fetched_at,{cols}\n"
            f"FROM l LEFT JOIN `{p}.edinet.company` c USING (edinet_code), UNNEST({arr}) x")


def main() -> int:
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    for name in VIEWS:
        client.query(view_sql(client.project, name), location="asia-northeast1").result()
        n = list(client.query(f"SELECT COUNT(*) n FROM `{client.project}.edinet.{name}`").result())[0].n
        print(f"✅ edinet.{name}: {n}行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
