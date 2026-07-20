"""P1 corporate-holding enrichment from FudosanDB into BigQuery."""
from __future__ import annotations

from datetime import datetime, timezone
from db_rest_client import DBRestClient
from edinet_bq_client import EDINETBQClient


def run_fudosan_ingest(cfg, results: dict | None = None) -> dict:
    if not getattr(cfg, "ENABLE_FUDOSANDB_INGEST", True):
        return {"skipped": True}
    targets = list(getattr(cfg, "FUDOSANDB_P1_TICKERS", []))
    client, bq = DBRestClient.fudosandb(cfg), EDINETBQClient()
    written = 0
    for ticker in targets[:client.daily_budget]:
        data = client.get("/corporate-real-estate/holdings/search", {"ticker": ticker})
        for item in data.get("items", data if isinstance(data, list) else []):
            row = {"company_code": str(item.get("company_code") or ticker).replace(".T", ""),
                   "property_id": str(item.get("property_id") or item.get("id") or ""), "property_name": item.get("property_name"),
                   "prefecture": item.get("prefecture"), "municipality": item.get("municipality"),
                   "latitude": item.get("latitude"), "longitude": item.get("longitude"), "land_area_sqm": item.get("land_area_sqm"),
                   "book_value_jpy": item.get("book_value_jpy"), "estimated_land_value_jpy": item.get("estimated_land_value_jpy"),
                   "source": "fudosandb", "as_of_date": str(item.get("as_of_date") or datetime.now().date())[:10],
                   "updated_at": datetime.now(timezone.utc).isoformat()}
            if row["property_id"]:
                written += int(bq.insert_rows("corporate_holdings", [row], dataset="real_estate"))
    return {"skipped": False, "rows": written, "api_calls": client.calls}
