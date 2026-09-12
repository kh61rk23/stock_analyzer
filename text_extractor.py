"""Read EdinetDB-derived text signals from BigQuery.

Government EDINET XBRL/HTML downloads were retired by D-①-1.  The ingestor
writes normalised analysis to ``edinet.text_analysis`` every configured run;
this consumer never contacts the government endpoint and therefore keeps
``EDINET_API_KEY`` removed (2026-09-12, EdinetDB only).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


_SECTION_MAX_CHARS = {
    "business": 3000,
    "risk": 5000,
    "strategy": 3000,
    "governance": 2000,
}

_XBRL_ELEMENT_MAP = {
    "business-overview": "business",
    "business_overview": "business",
    "risks": "risk",
    "risk": "risk",
    "mda": "strategy",
    "governance": "governance",
}


def _section_key(name: str) -> str | None:
    """API aliasと日本語の実セクション名を既存4シグナルへ正規化する。"""
    value = str(name or "").strip()
    exact = _XBRL_ELEMENT_MAP.get(value.lower())
    if exact:
        return exact
    if "事業等のリスク" in value or "リスク情報" in value:
        return "risk"
    if "事業の内容" in value:
        return "business"
    if any(token in value for token in (
        "経営方針", "経営環境", "対処すべき課題",
        "経営者による財政状態", "経営成績", "キャッシュ・フロー",
    )):
        return "strategy"
    if "コーポレート・ガバナンス" in value or "企業統治" in value:
        return "governance"
    return None


def _plain_text(value: str) -> str:
    return re.sub(r"[\u3000\s]+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


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
        code = str(securities_code).strip()
        rows = bq.get_xbrl_text_sections(code)
        if not rows:
            return None
        sections: dict[str, str] = {}
        period_end = ""
        for row in rows:
            key = _section_key(row.get("element_name", ""))
            text = _plain_text(str(row.get("text_value") or ""))
            if key and text and key not in sections:
                sections[key] = text[:_SECTION_MAX_CHARS[key]]
            if not period_end:
                period_end = str(row.get("period_end") or "")
        if not sections:
            return None
        return {
            "securities_code": code,
            "company_name": "",
            "period_end": period_end,
            "sections": {key: sections.get(key, "") for key in _SECTION_MAX_CHARS},
            "source": "edinetdb_bq",
            "fetched_at": datetime.now().isoformat(),
        }
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


def search_full_text(
    cfg,
    securities_code: str,
    query: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """EdinetDBからBQへ保存した個社有報全文をキーワード検索する。"""
    if not getattr(cfg, "ENABLE_TEXT_SIGNAL", True):
        return []
    try:
        from edinet_bq_client import EDINETBQClient
        return EDINETBQClient().search_company_text(
            securities_code, query, limit=limit
        )
    except Exception as exc:
        print(f"    ⚠️ [text_extractor] 全文検索失敗 ({securities_code}): {str(exc)[:60]}")
        return []


def batch_extract_sections(cfg, securities_codes: list[str], max_companies: Optional[int] = None) -> dict[str, dict]:
    limit = max_companies or getattr(cfg, "TEXT_SIGNAL_FIRST_RUN_LIMIT", 50)
    output = {}
    for code in list(dict.fromkeys(str(v).strip() for v in securities_codes if v))[:limit]:
        result = extract_sections(cfg, code)
        if result:
            output[code] = result
    return output
