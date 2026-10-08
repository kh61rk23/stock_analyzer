"""EdinetDB ``/companies/{code}/disclosures`` 由来のdoc_index互換更新。"""

from __future__ import annotations

import json
import re
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


# 有報と判別できる語（この語を含めば採用）
_YUHO_TOKENS = ("yuho", "annual", "有価証券報告", "asr")
# 明らかに有報ではない語（この語を含めば除外）
# ※ EdinetDB の適時開示スラッグ（disclosure_*）や四半期・訂正の類。
_NOT_YUHO_TOKENS = (
    "quarter", "四半期", "semi", "半期", "interim",
    "extraordinary", "臨時", "correction", "訂正",
    "holding", "大量保有", "treasury", "自己株",
    "disclosure_", "monthly", "月次",
)


def is_annual_report(raw_type: str) -> bool:
    """開示種別が有価証券報告書かを判定する。

    呼び出し側は API に `types=yuho` を渡しており**サーバ側で既に絞り込まれている**。
    我々が管理していない語彙のホワイトリストで判定すると全件除外されうるため、
    既知の非有報だけを除外し、未知の語彙はサーバの絞り込みを信じて通す。
    """
    text = str(raw_type or "").lower()
    if not text:
        return True                      # 種別欄が無い＝サーバの絞り込みに従う
    if any(token in text for token in _NOT_YUHO_TOKENS):
        return False
    if any(token in text for token in _YUHO_TOKENS):
        return True
    return True                          # 未知の語彙は落とさない（実測で要確認）


def build_doc_index_rows(
    data,
    company: dict,
    *,
    now: str | None = None,
    seen_types: set | None = None,
    seen_keys: set | None = None,
    doc_id_by_year: dict | None = None,
) -> list[dict]:
    """EdinetDB disclosure objectを既存doc_index列へ変換する純関数。

    seen_types を渡すと、観測した開示種別の文字列を積む。
    語彙が想定と違ったときにログから特定できるようにするため。
    """
    now = now or datetime.now(timezone.utc).isoformat()
    rows = []
    for item in _disclosure_items(data):
        doc_id = str(_first(item, "doc_id", "docId", "docID", "document_id",
                            "documentId", "edinet_doc_id") or "").strip()
        if not doc_id:
            # 書類IDが専用キーで取れないことがある。汎用キーは
            # EDINET 書類管理番号（S100XXXX 形式）に見えるときだけ採用する。
            _cand = str(_first(item, "id", "doc_code", "code") or "").strip()
            if re.match(r"^S[0-9A-Z]{7}$", _cand):
                doc_id = _cand
        if not doc_id:
            # 開示エンドポイントの行のキーは date / doc_id / fiscal_year / pdf_url /
            # title / type で、doc_id 列はあるのに**値が空**で返る。
            # EDINET の PDF URL には書類管理番号（S1xxxxxx）が含まれるので、そこから補う。
            _m = re.search(r"(?<![0-9A-Za-z])(S1[0-9A-Z]{6})(?![0-9A-Za-z])",
                           str(item.get("pdf_url") or ""))
            if _m:
                doc_id = _m.group(1)
        if not doc_id and doc_id_by_year:
            # 同じ企業・年度の書類IDを BQ financials から補う（run_doc_indexer が渡す）
            try:
                doc_id = str(doc_id_by_year.get(int(_first(item, "fiscal_year", "fiscalYear"))) or "")
            except (TypeError, ValueError):
                pass
        raw_type = str(_first(item, "type", "document_type", "doc_type", "category") or "")
        if seen_types is not None:
            seen_types.add(raw_type or "(種別欄なし)")
        if not doc_id:
            if seen_keys is not None:
                seen_keys.update(str(k) for k in item.keys())
                # 値の実物も1件ぶん残す（キー名だけでは空か型違いか判別できない）
                seen_keys.add(f"例:doc_id={item.get('doc_id')!r}")
                seen_keys.add(f"例:pdf_url={str(item.get('pdf_url'))[:70]}")
            continue
        if not is_annual_report(raw_type):
            continue
        rows.append({
            "doc_id": doc_id,
            "company_code": _parse_securities_code(company.get("sec_code")),
            "edinet_code": company.get("edinet_code"),
            "company_name": company.get("name") or company.get("name_ja"),
            "doc_type": "有価証券報告書",
            "fiscal_year_end": _as_date(_first(item, "fiscal_year_end", "period_end")),
            "disclosure_date": _as_date(
                # 実応答の開示日キーは "date"（候補から外すと常に NULL になる）
                _first(item, "published_at", "disclosure_date", "submit_date", "filed_at",
                       "date", "submit_date_time")
            ),
            "xbrl_available": False,
            "html_available": False,
            "fetched_flag": True,
            "fetched_at": now,
            "ifrs_flag": str(company.get("accounting_standard") or "").upper() == "IFRS",
            "updated_at": now,
        })
    return rows


def _load_doc_ids_from_financials(bq, codes) -> dict:
    """{edinet_code: {fiscal_year: doc_id}} を BQ financials から1クエリで作る。

    開示エンドポイントの行は doc_id が空で返る（キー date / doc_id /
    fiscal_year / pdf_url / title / type、doc_id の値だけ空）。同じ企業・年度の
    EDINET 書類IDは財務取込で BQ financials に入っているので、そこから補う。
    """
    out: dict = {}
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return out
    try:
        from google.cloud import bigquery
        sql = f"""
        SELECT edinet_code, fiscal_year, doc_id
        FROM `{bq.dataset}.financials`
        WHERE edinet_code IN UNNEST(@codes)
          AND fiscal_year IS NOT NULL
          AND REGEXP_CONTAINS(doc_id, r'^S1[0-9A-Z]{{6}}$')
        """
        job = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("codes", "STRING", codes)])
        for r in bq.client.query(sql, job_config=job).result():
            out.setdefault(str(r["edinet_code"]), {})[int(r["fiscal_year"])] = str(r["doc_id"])
    except Exception as exc:
        print(f"[doc_indexer] ⚠️ 財務からの書類ID補完に失敗: {str(exc)[:60]}")
    return out


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
    # 対象は「doc_index をまだ取っていない順」に選ぶ。
    # 「最近取得した順」にすると毎回同じ企業を叩くだけで対象が広がらない。
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
    doc_ids = _load_doc_ids_from_financials(bq, targets)

    # 取得窓は days_back（既定7日）ではなく専用設定を使う。
    # 有価証券報告書は**年1回**しか提出されないため、7日窓で types=yuho を
    # 探しても構造的にほぼ常に0件になる。
    # 年次サイクルを跨ぐ既定400日にして、1社1回は必ず拾えるようにする。
    lookback = max(
        int(days_back),
        int(getattr(cfg, "EDINETDB_DOC_INDEX_LOOKBACK_DAYS", 400)),
    )
    since = (date.today() - timedelta(days=max(1, lookback))).isoformat()
    rows = []
    indexed_today = []
    # 0件のとき原因（API側が空か／型フィルタで落ちているか）をログから
    # 切り分けられるよう、内訳を数える。
    stats = {"empty": 0, "filtered": 0, "no_data": 0}
    seen_types: set = set()
    seen_keys: set = set()
    for code in targets:
        data = client.get(
            f"/companies/{code}/disclosures",
            {"since": since, "until": date.today().isoformat(), "types": "yuho"},
        )
        if data is None:
            # 予算枯渇・レート制限・一時障害。取得済み扱いにせず次回に回す。
            # edinetdb_ingestor / fudosan_ingestor と同じく ("budget", "rate_limited")
            # の両方で打ち切る（429 のサーキットブレーカ作動時に残り全社を空振りしない）。
            if getattr(client, "last_error", None) in ("budget", "rate_limited"):
                break
            stats["no_data"] += 1
            continue
        items = _disclosure_items(data)
        built = build_doc_index_rows(data, by_code.get(code, {}),
                                     seen_types=seen_types, seen_keys=seen_keys,
                                     doc_id_by_year=doc_ids.get(code))
        if not items:
            stats["empty"] += 1
        elif not built:
            stats["filtered"] += 1
        rows.extend(built)
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
              "targets": len(targets), "visited": len(indexed_today),
              **stats}
    print(f"[doc_indexer] ✅ EdinetDB由来 {len(rows)}件 MERGE "
          f"（巡回{len(indexed_today)}/{len(targets)}社・窓{lookback}日）")
    if not rows and targets:
        # 0件は「本当に開示が無い」のか「取り込み側で落としている」のかで
        # 対処が正反対になる。内訳を出さないと次回も同じ判断ができない。
        print(f"[doc_indexer] ℹ️ 0件の内訳: API空={stats['empty']}社 / "
              f"型フィルタ除外={stats['filtered']}社 / 応答なし={stats['no_data']}社")
        if seen_types:
            # 語彙が想定と違うと静かに全滅するので、実物を必ず出す。
            print("[doc_indexer] ℹ️ 観測した開示種別: "
                  + ", ".join(sorted(seen_types)[:12]))
        if seen_keys:
            # 書類IDが取れなかった行の実キー。次回ログでキー名を確定させる。
            print("[doc_indexer] ℹ️ 書類ID欠落行のキー: "
                  + ", ".join(sorted(seen_keys)[:20]))
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days-back", type=int, default=1)
    args = parser.parse_args()
    run_doc_indexer(args.days_back)
