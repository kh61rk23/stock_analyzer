"""EdinetDB ``/companies/{code}/disclosures`` 由来のdoc_index互換更新。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from db_rest_client import DBRestClient


def _first(mapping: dict, *keys):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _as_date(value) -> str | None:
    value = str(value or "")[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _parse_securities_code(value) -> str:
    value = str(value or "").strip()
    return value[:4] if len(value) == 5 and value.endswith("0") else value


def _disclosure_items(data) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("disclosures", "items", "results", "yuho"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def build_doc_index_rows(
    data,
    company: dict,
    *,
    now: str | None = None,
) -> list[dict]:
    """EdinetDB disclosure objectを既存doc_index列へ変換する純関数。"""
    now = now or datetime.now(timezone.utc).isoformat()
    rows = []
    for item in _disclosure_items(data):
        doc_id = str(_first(item, "doc_id", "docId", "docID") or "").strip()
        if not doc_id:
            continue
        raw_type = str(_first(item, "type", "document_type", "doc_type", "category") or "")
        if raw_type and not any(token in raw_type.lower() for token in ("yuho", "annual", "有価証券")):
            continue
        rows.append({
            "doc_id": doc_id,
            "company_code": _parse_securities_code(company.get("sec_code")),
            "edinet_code": company.get("edinet_code"),
            "company_name": company.get("name") or company.get("name_ja"),
            "doc_type": "有価証券報告書",
            "fiscal_year_end": _as_date(_first(item, "fiscal_year_end", "period_end")),
            "disclosure_date": _as_date(
                _first(item, "published_at", "disclosure_date", "submit_date", "filed_at")
            ),
            "xbrl_available": False,
            "html_available": False,
            "fetched_flag": True,
            "fetched_at": now,
            "ifrs_flag": str(company.get("accounting_standard") or "").upper() == "IFRS",
            "updated_at": now,
        })
    return rows


def run_doc_indexer(days_back: int = 1, cfg=None) -> dict:
    """直近W3対象の有報開示を取得してMERGE。欠鍵・API/BQ障害はskipする。"""
    if cfg is None:
        import config as cfg
    api_key = str(getattr(cfg, "EDINETDB_API_KEY", "") or "").strip()
    if not api_key:
        result = {"skipped": True, "reason": "EDINETDB_API_KEY missing", "inserted": 0}
        print(f"[doc_indexer] ⏭️ {result['reason']}")
        return result

    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    try:
        companies_cache = json.loads(
            (data_dir / "edinetdb_company_cache.json").read_text(encoding="utf-8")
        )
        companies = companies_cache.get("companies", [])
        rotation = json.loads(
            (data_dir / getattr(cfg, "EDINETDB_ROTATION_META", "edinetdb_rotation_meta.json"))
            .read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return {"skipped": True, "reason": "W3 cache unavailable", "inserted": 0}
    by_code = {str(item.get("edinet_code")): item for item in companies}
    targets = sorted(
        rotation,
        key=lambda code: str((rotation.get(code) or {}).get("last_fetched") or ""),
        reverse=True,
    )[:max(0, int(getattr(cfg, "EDINETDB_DOC_INDEX_LIMIT", 10)))]
    client = DBRestClient(
        getattr(cfg, "EDINETDB_BASE_URL", "https://edinetdb.jp/v1"), api_key,
        data_dir / "edinetdb_cache", int(getattr(cfg, "EDINETDB_DAILY_BUDGET", 90)),
        "edinetdb",
    )
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
    except Exception as exc:
        return {"skipped": True, "reason": f"BQ unavailable: {type(exc).__name__}", "inserted": 0}

    since = (date.today() - timedelta(days=max(1, int(days_back)))).isoformat()
    rows = []
    for code in targets:
        data = client.get(
            f"/companies/{code}/disclosures",
            {"since": since, "until": date.today().isoformat(), "types": "yuho"},
        )
        rows.extend(build_doc_index_rows(data, by_code.get(code, {})))
    try:
        bq.merge_rows("doc_index", rows, ["doc_id"])
    except Exception as exc:
        return {"skipped": True, "reason": f"MERGE failed: {type(exc).__name__}", "inserted": 0}
    result = {"skipped": False, "inserted": len(rows), "targets": len(targets)}
    print(f"[doc_indexer] ✅ EdinetDB由来 {len(rows)}件 MERGE")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days-back", type=int, default=1)
    args = parser.parse_args()
    run_doc_indexer(args.days_back)
