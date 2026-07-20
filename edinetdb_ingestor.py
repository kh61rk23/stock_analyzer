"""EdinetDB-only ingestion.  No FSA XBRL download or parser is used."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from db_rest_client import DBRestClient
from edinet_bq_client import EDINETBQClient


def _as_date(value):
    if not value:
        return None
    return str(value)[:10]


def _fiscal_year_end(item: dict) -> str | None:
    """Prefer provider period; otherwise derive the latest fiscal month before submit."""
    direct = item.get("fiscal_year_end") or item.get("period_end")
    if direct:
        return _as_date(direct)
    submitted = _as_date(item.get("submit_date") or item.get("disclosure_date"))
    month = item.get("fiscal_month") or item.get("fiscal_year_end_month")
    if not submitted or not month:
        return None
    submitted_date = date.fromisoformat(submitted)
    month = int(month)
    year = submitted_date.year if submitted_date.month >= month else submitted_date.year - 1
    return f"{year:04d}-{month:02d}-{min(submitted_date.day, 28):02d}"


def _items(data):
    return data.get("items", data.get("results", data if isinstance(data, list) else []))


def _doc_rows(disclosures):
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in disclosures:
        doc_id = str(item.get("doc_id") or item.get("document_id") or item.get("id") or "")
        if not doc_id:
            continue
        rows.append({"doc_id": doc_id, "company_code": str(item.get("company_code") or item.get("securities_code") or ""),
                     "edinet_code": item.get("edinet_code") or "", "company_name": item.get("company_name") or "",
                     "doc_type": item.get("doc_type") or "有価証券報告書", "fiscal_year_end": _fiscal_year_end(item),
                     "disclosure_date": _as_date(item.get("submit_date") or item.get("disclosure_date")),
                     "xbrl_available": False, "html_available": False, "fetched_flag": True,
                     "ifrs_flag": bool(item.get("ifrs_flag", False)), "updated_at": now})
    return rows


def run_edinetdb_ingest(cfg, results: dict | None = None, doc_index_only: bool = False) -> dict:
    if not getattr(cfg, "ENABLE_EDINETDB_INGEST", True):
        return {"skipped": True}
    client, bq = DBRestClient.edinetdb(cfg), EDINETBQClient()
    lookback = int(getattr(cfg, "EDINETDB_LOOKBACK_DAYS", 7))
    data = client.get("/disclosures/search", {"days": lookback, "limit": 200})
    disclosures = _items(data)
    doc_rows = _doc_rows(disclosures)
    inserted = bq.insert_rows("doc_index", doc_rows) if doc_rows else True
    # Financial/text data are provider-normalized.  Only columns in the 2026-07-20
    # ground-truth schema are written; unknown provider fields are deliberately ignored.
    fin_count = text_count = 0
    if not doc_index_only:
        for item in disclosures:
            doc_id = str(item.get("doc_id") or item.get("document_id") or item.get("id") or "")
            code = str(item.get("company_code") or item.get("securities_code") or "")
            financial = item.get("financials") or {}
            if doc_id and financial:
                row = {k: financial.get(k) for k in ("net_sales", "operating_income", "ordinary_income", "net_income", "total_assets", "equity", "interest_bearing_debt", "cash_and_equiv", "operating_cf", "investing_cf", "free_cf", "employee_count", "avg_age_years", "avg_annual_salary", "total_director_comp", "max_single_comp", "capex", "depreciation", "roe", "roa", "operating_margin", "accrual_ratio", "fcf_yield", "labor_productivity")}
                row.update({"fin_id": hashlib.sha256(f"{doc_id}:financials".encode()).hexdigest(), "doc_id": doc_id,
                            "company_code": code, "fiscal_year_end": _fiscal_year_end(item),
                            "report_period": financial.get("report_period") or "annual", "currency": financial.get("currency") or "JPY",
                            "source": "edinetdb", "updated_at": datetime.now(timezone.utc).isoformat()})
                fin_count += int(bq.insert_rows("financials", [row]))
            if int(getattr(cfg, "EDINETDB_TEXTBLOCK_EVERY_N", 2)) > 0 and date.today().toordinal() % int(getattr(cfg, "EDINETDB_TEXTBLOCK_EVERY_N", 2)) == 0:
                for section in item.get("text_analysis", []) or []:
                    section_type = section.get("section_type") or "risk"
                    row = {"text_id": hashlib.sha256(f"{doc_id}:{section_type}".encode()).hexdigest(), "doc_id": doc_id,
                           "company_code": code, "fiscal_year_end": _fiscal_year_end(item), "section_type": section_type,
                           "finbert_positive": section.get("finbert_positive"), "finbert_negative": section.get("finbert_negative"),
                           "finbert_neutral": section.get("finbert_neutral"), "yoy_delta": section.get("yoy_delta"),
                           "risk_deterioration_flag": section.get("risk_deterioration_flag"),
                           "risk_category_scores": section.get("risk_category_scores"), "updated_at": datetime.now(timezone.utc).isoformat()}
                    text_count += int(bq.insert_rows("text_analysis", [row]))
    return {"skipped": False, "disclosures": len(disclosures), "doc_index_ok": inserted,
            "financial_rows": fin_count, "text_rows": text_count, "api_calls": client.calls}


if __name__ == "__main__":
    import config
    print(run_edinetdb_ingest(config))
