"""Read EdinetDB-derived text signals from BigQuery.

Government EDINET XBRL/HTML downloads were retired by D-①-1.  The ingestor
writes normalised analysis to ``edinet.text_analysis`` every configured run;
this consumer never contacts the government endpoint and therefore keeps
``EDINET_API_KEY`` dormant.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


def _cache_path(cfg, code: str) -> Path:
    return Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "text_extractor" / f"{code}.json"


def _load_cache(cfg, code: str) -> Optional[dict]:
    path = _cache_path(cfg, code)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if datetime.now() - datetime.fromisoformat(value["fetched_at"]) <= timedelta(days=getattr(cfg, "TEXT_SIGNAL_TTL_DAYS", 30)):
            return value
    except Exception:
        pass
    return None


def _save_cache(cfg, value: dict) -> None:
    try:
        path = _cache_path(cfg, value["securities_code"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _extract_from_bq(securities_code: str) -> Optional[dict]:
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
        code = str(securities_code).replace("'", "")
        sql = f"""
        SELECT section_type, finbert_positive, finbert_negative, finbert_neutral,
               fiscal_year_end
        FROM `{bq.dataset}.text_analysis`
        WHERE company_code = '{code}'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY section_type ORDER BY updated_at DESC) = 1
        """
        frame = bq.query_to_df(sql)
        if frame.empty:
            return None
        sections = {str(r.section_type): json.dumps({
            "positive": r.finbert_positive, "negative": r.finbert_negative,
            "neutral": r.finbert_neutral}, ensure_ascii=False)
            for r in frame.itertuples()}
        return {"securities_code": code, "company_name": "", "period_end": str(frame.iloc[0].get("fiscal_year_end", "")),
                "sections": {key: sections.get(key, "") for key in ("business", "risk", "strategy", "governance")},
                "source": "edinetdb_bq", "fetched_at": datetime.now().isoformat()}
    except Exception as exc:
        print(f"    ⚠️ [text_extractor] BQ read failed ({securities_code}): {str(exc)[:60]}")
        return None


def extract_sections(cfg, securities_code: str) -> Optional[dict]:
    if not getattr(cfg, "ENABLE_TEXT_SIGNAL", True):
        return None
    code = str(securities_code).strip()
    if not code:
        return None
    return _load_cache(cfg, code) or _store_if_present(cfg, _extract_from_bq(code))


def _store_if_present(cfg, value: Optional[dict]) -> Optional[dict]:
    if value:
        _save_cache(cfg, value)
    return value


def batch_extract_sections(cfg, securities_codes: list[str], max_companies: Optional[int] = None) -> dict[str, dict]:
    limit = max_companies or getattr(cfg, "TEXT_SIGNAL_FIRST_RUN_LIMIT", 50)
    output = {}
    for code in list(dict.fromkeys(str(v).strip() for v in securities_codes if v))[:limit]:
        result = extract_sections(cfg, code)
        if result:
            output[code] = result
    return output
