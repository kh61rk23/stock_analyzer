"""Idempotent BigQuery schema for corporate real-estate enrichment."""
from __future__ import annotations

DDL = [
    "CREATE SCHEMA IF NOT EXISTS `{project}.real_estate` OPTIONS(location='asia-northeast1')",
    """CREATE TABLE IF NOT EXISTS `{project}.real_estate.corporate_holdings` (
      company_code STRING NOT NULL, property_id STRING NOT NULL, property_name STRING,
      prefecture STRING, municipality STRING, latitude FLOAT64, longitude FLOAT64,
      land_area_sqm FLOAT64, book_value_jpy FLOAT64, estimated_land_value_jpy FLOAT64,
      source STRING, as_of_date DATE, updated_at TIMESTAMP
    ) PARTITION BY as_of_date CLUSTER BY company_code""",
    """CREATE TABLE IF NOT EXISTS `{project}.real_estate.area_profile` (
      area_code STRING NOT NULL, as_of_date DATE NOT NULL, flood_risk_score FLOAT64,
      population_decline_score FLOAT64, updated_at TIMESTAMP
    ) PARTITION BY as_of_date CLUSTER BY area_code""",
]


def create_all():
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient()
    for ddl in DDL:
        client.client.query(ddl.replace("{project}", client.project)).result()


if __name__ == "__main__":
    create_all()
