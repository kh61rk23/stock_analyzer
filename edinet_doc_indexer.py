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
    # 2026-07-30: 対象選定を「最近取得した順（reverse=True）」から
    # 「doc_index をまだ取っていない順」へ変更。
    # 旧実装は毎回いちばん新しい10社を見に行くため、同じ企業を繰り返し
    # 叩くだけで対象が広がらなかった（doc_index は 2026-04-26 で停止・193件）。
    doc_meta = rotation.get("_doc_index", {})
    if not isinstance(doc_meta, dict):
        doc_meta = {}
    targets = sorted(
        (c for c in rotation if not str(c).startswith("_")),
        key=lambda code: (
            str((doc_meta.get(code) or {}).get("last_indexed") or ""),  # 未取得が先
            str(code),
        ),
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

    # 2026-07-30: 取得窓を days_back（既定7日）から専用設定へ変更。
    # 有価証券報告書は**年1回**しか提出されないため、7日窓で types=yuho を
    # 探しても構造的にほぼ常に0件になっていた（実測: inserted 0 / targets 10）。
    # 年次サイクルを跨ぐ既定400日にして、1社1回は必ず拾えるようにする。
    lookback = max(
        int(days_back),
        int(getattr(cfg, "EDINETDB_DOC_INDEX_LOOKBACK_DAYS", 400)),
    )
    since = (date.today() - timedelta(days=max(1, lookback))).isoformat()
    rows = []
    indexed_today = []
    for code in targets:
        data = client.get(
            f"/companies/{code}/disclosures",
            {"since": since, "until": date.today().isoformat(), "types": "yuho"},
        )
        if data is None:
            # 予算枯渇・一時障害。取得済み扱いにせず次回に回す。
            if getattr(client, "last_error", None) == "budget":
                break
            continue
        rows.extend(build_doc_index_rows(data, by_code.get(code, {})))
        indexed_today.append(code)
    try:
        bq.merge_rows("doc_index", rows, ["doc_id"])
    except Exception as exc:
        return {"skipped": True, "reason": f"MERGE failed: {type(exc).__name__}", "inserted": 0}

    # 巡回状況を記録して、次回は未取得の企業から見に行けるようにする。
    # 0件だった企業も「見に行った」ことは記録する（無限に同じ企業を
    # 叩き続けないため）。
    if indexed_today:
        today_str = date.today().isoformat()
        for code in indexed_today:
            doc_meta[code] = {"last_indexed": today_str}
        rotation["_doc_index"] = doc_meta
        try:
            (data_dir / getattr(cfg, "EDINETDB_ROTATION_META",
                                "edinetdb_rotation_meta.json")).write_text(
                json.dumps(rotation, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            print(f"[doc_indexer] ⚠️ 巡回状況の保存失敗: {str(exc)[:60]}")

    result = {"skipped": False, "inserted": len(rows),
              "targets": len(targets), "visited": len(indexed_today)}
    print(f"[doc_indexer] ✅ EdinetDB由来 {len(rows)}件 MERGE "
          f"（巡回{len(indexed_today)}/{len(targets)}社・窓{lookback}日）")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days-back", type=int, default=1)
    args = parser.parse_args()
    run_doc_indexer(args.days_back)
