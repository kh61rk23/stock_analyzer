"""DB統合W3/W5のBigQueryスキーマを冪等に作成・拡張する。"""

from __future__ import annotations

from edinet_bq_client import EDINETBQClient


def _statements(project: str) -> list[str]:
    return [
        f'CREATE SCHEMA IF NOT EXISTS `{project}.real_estate` OPTIONS(location="asia-northeast1")',
        f"""CREATE TABLE IF NOT EXISTS `{project}.edinet.company` (
          edinet_code STRING NOT NULL, sec_code STRING, name STRING, name_en STRING,
          industry STRING, listing_status STRING, is_delisted BOOL,
          accounting_standard STRING, credit_score INT64, corporate_number STRING,
          last_fetched_at TIMESTAMP, updated_at TIMESTAMP
        )""",
        f"ALTER TABLE `{project}.edinet.financials` ADD COLUMN IF NOT EXISTS edinet_code STRING",
        f"ALTER TABLE `{project}.edinet.financials` ADD COLUMN IF NOT EXISTS fiscal_year INT64",
        f"ALTER TABLE `{project}.edinet.text_analysis` ADD COLUMN IF NOT EXISTS raw_text STRING",
        f"ALTER TABLE `{project}.edinet.text_analysis` ADD COLUMN IF NOT EXISTS source STRING",
        f"""CREATE TABLE IF NOT EXISTS `{project}.real_estate.corporate_holdings_p1` (
          edinet_code STRING NOT NULL,
          company_name STRING,
          fiscal_year INT64 NOT NULL,
          facility_name STRING NOT NULL,
          prefecture STRING,
          municipality_code STRING,
          book_value_land_m_yen FLOAT64,
          book_value_buildings_m_yen FLOAT64,
          land_area_m2 FLOAT64,
          commercial_land_price_per_m2 FLOAT64,
          residential_land_price_per_m2 FLOAT64,
          industrial_land_price_per_m2 FLOAT64,
          population_current INT64,
          population_2050 INT64,
          population_change_pct FLOAT64,
          flood_risk_area_pct FLOAT64,
          landslide_risk_area_pct FLOAT64,
          source STRING,
          updated_at TIMESTAMP
        ) CLUSTER BY edinet_code, fiscal_year""",
        f"""CREATE TABLE IF NOT EXISTS `{project}.real_estate.area_profile_p1` (
          municipality_code STRING NOT NULL,
          prefecture_name STRING,
          municipality_name STRING,
          total_transactions INT64,
          median_price FLOAT64,
          latest_land_price FLOAT64,
          land_price_yoy FLOAT64,
          avg_land_price_commercial FLOAT64,
          avg_land_price_residential FLOAT64,
          land_price_yoy_change FLOAT64,
          flood_risk_area_pct FLOAT64,
          latest_avg_price_condo FLOAT64,
          hospital_count INT64,
          source STRING,
          updated_at TIMESTAMP
        )""",
    ]


def setup() -> None:
    bq = EDINETBQClient()
    for index, sql in enumerate(_statements(bq.project), 1):
        bq.client.query(sql).result()
        print(f"✅ DB integration schema {index}/{len(_statements(bq.project))}")


if __name__ == "__main__":
    setup()
