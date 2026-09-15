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

    2026-09-11: ここは「既知の有報語を含まなければ除外」という
    ホワイトリスト判定だった。しかし呼び出し側は API に
    `types=yuho` を渡しており**サーバ側で既に絞り込まれている**のに、
    その上で我々が管理していない語彙を当てにいっていたため、
    実測で 9社/9社 が全て除外され doc_index が 2026-04-26 から
    1件も増えていなかった（台帳 2026-09-11: filtered=9）。
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
            # 2026-09-14: 観測語彙は "yuho"（型フィルタは通過）なのに 9社/9社 が
            # 除外されていた＝落ちていたのは書類IDの欠落側だった。汎用キーは
            # EDINET 書類管理番号（S100XXXX 形式）に見えるときだけ採用する。
            _cand = str(_first(item, "id", "doc_code", "code") or "").strip()
            if re.match(r"^S[0-9A-Z]{7}$", _cand):
                doc_id = _cand
        if not doc_id:
            # 2026-09-15: 前日追加の診断で、書類IDが取れなかった行のキーは
            # date / doc_id / fiscal_year / pdf_url / title / type と判明した。
            # doc_id 列はあるのに**値が空**で、9社/9社がここで落ちていた
            # （doc_index が 2026-04-26 から1件も増えていない真因）。
            # EDINET の PDF URL には書類管理番号（S1xxxxxx）が含まれるので、そこから補う。
            _m = re.search(r"(?<![0-9A-Za-z])(S1[0-9A-Z]{6})(?![0-9A-Za-z])",
                           str(item.get("pdf_url") or ""))
            if _m:
                doc_id = _m.group(1)
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
    # 2026-09-10: なぜ0件なのかがログから分からず、doc_index が
    # 2026-04-26 で止まっている原因（API側が空か／型フィルタで落ちているか）
    # を切り分けられなかったため、内訳を数える。
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
            # 2026-09-10: edinetdb_ingestor / fudosan_ingestor は
            # ("budget", "rate_limited") の両方で打ち切るのに、ここだけ
            # "budget" しか見ておらず、429 のサーキットブレーカ作動時に
            # 残り全社を空振りしたまま「巡回済み」にせず抜けていた。
            if getattr(client, "last_error", None) in ("budget", "rate_limited"):
                break
            stats["no_data"] += 1
            continue
        items = _disclosure_items(data)
        built = build_doc_index_rows(data, by_code.get(code, {}),
                                     seen_types=seen_types, seen_keys=seen_keys)
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
