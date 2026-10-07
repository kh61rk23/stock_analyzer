# ==============================================================
# us_8k_monitor.py  v1.0  (Phase US-3)
# SEC EDGAR 8-K リアルタイム監視
# ==============================================================
# 【役割】
#   前日提出の米国企業 8-K を監視し、
#   决算発表（Item 2.02）・CEO交代（Item 5.02）・M&A（Item 8.01）を
#   us_screener の対象銘柄に絞って検知する。
#
# 【処理フロー】
#   1. SEC EDGAR full-text search から前日の 8-K を取得
#   2. CIK → ticker に変換（sec_client.cik_map() の共通キャッシュを利用）
#   3. ticker が us_screener 通過候補かチェック
#   4. Item 2.02 の場合 → earnings_surprise.py の優先キューに追加
#   5. us_fundamental_rotator の優先再取得キューに追加
#   6. alert_notifier に "8K_EVENT" アラートを発行
#
# 【取得先（無料・APIキー不要）】
#   EDGAR full-text search:
#     https://efts.sec.gov/LATEST/search-index?q="8-K"&dateRange=custom&startdt=YYYY-MM-DD
#   8-K 本文（item検知用）:
#     https://www.sec.gov/Archives/edgar/{path}
#
# 【出力スキーマ（results["us_8k"]）】
#   {
#     "events": [
#       {
#         "ticker":       "AAPL",
#         "cik":          320193,
#         "company_name": "Apple Inc.",
#         "items":        ["2.02"],
#         "event_types":  ["earnings"],
#         "filing_date":  "2026-04-01",
#         "doc_url":      str,
#         "priority":     "high"/"medium"/"low",
#         "in_screener":  bool,
#       }, ...
#     ],
#     "earnings_tickers":    [ticker, ...],
#     "ceo_change_tickers":  [ticker, ...],
#     "ma_tickers":          [ticker, ...],
#     "screener_hits":       [ticker, ...],
#     "total":               int,
#     "timestamp":           str,
#   }
#
# 【依存】sec_client.py / config.py
# 2026-07-27是正（Wave J1 / U-1c）: SEC EDGAR への HTTP 呼び出し・User-Agent・
# CIK 解決は sec_client.py（U-1a 新設）に一本化した。CIK→ticker の逆引きは
# 従来 us_cik_map.json（US-1/us_fundamental_rotator.py が生成）を再利用して
# いたが、sec_client.cik_map() の共通キャッシュに切替えたため us_8k_monitor.py
# 単独で us_fundamental_rotator の実行順に依存しなくなった。
# ==============================================================

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import sec_client


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

EDGAR_SEARCH_URL  = "https://efts.sec.gov/LATEST/search-index"
EDGAR_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{:010d}.json"

CACHE_FILE         = "us_8k_cache.json"
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_US_8K_MONITOR_CACHE_VALID_HOURS': 'CACHE_VALID_HOURS',
    'B5_US_8K_MONITOR_REQUEST_TIMEOUT': 'REQUEST_TIMEOUT',
    'B5_US_8K_MONITOR_FETCH_DELAY': 'FETCH_DELAY',
    'B6_US_8K_MONITOR_MAX_8K_PER_DAY': 'MAX_8K_PER_DAY',
}

CACHE_VALID_HOURS  = 20    # 当日分は1日キャッシュ
REQUEST_TIMEOUT    = 10
FETCH_DELAY        = 0.15  # SEC レート制限（10req/s）
MAX_8K_PER_DAY     = 200   # 1日の最大取得件数

# Item番号 → イベント種別マッピング
ITEM_EVENT_MAP = {
    "2.02": "earnings",          # 業績発表（決算）
    "5.02": "ceo_change",        # 役員変更（CEO・CFO交代）
    "5.01": "board_change",      # 取締役変更
    "8.01": "ma_other",          # M&A・事業再編・その他重要事項
    "1.01": "material_agreement",# 重要契約
    "2.01": "asset_acquisition", # 資産取得・売却
    "4.02": "accounting_error",  # 会計誤謬（マイナスシグナル）
    "9.01": "financial_statements",  # 財務諸表（添付）
}

# イベント種別ごとの優先度
ITEM_PRIORITY = {
    "earnings":           "high",
    "ceo_change":         "high",
    "ma_other":           "medium",
    "asset_acquisition":  "medium",
    "material_agreement": "low",
    "board_change":       "low",
    "accounting_error":   "high",   # 会計誤謬は緊急
    "financial_statements": "low",
}


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

# 2026-07-13是正（I-4同型バグ）: SECは連絡先を含まないUser-Agentを403で拒否する。
# 旧 "stock_analyzer/1.0 (educational use)" は拒否対象だった。
# us_fundamental_rotator / institutional_13f_monitor と同じ SEC_USER_AGENT 規約に統一。
# 2026-07-27是正（U-1c）: 実体は sec_client.user_agent() に一本化。
# 引数なし呼び出し時の解決順（環境変数 → import config → 固定フォールバック）は
# sec_client.user_agent(cfg=None) と完全に同一のため、既存の回帰テスト
# （tests/test_sec_user_agent.py）の期待値はそのまま成立する。
def _sec_user_agent() -> str:
    return sec_client.user_agent()


def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILE


def _load_cache(cfg, target_date: str) -> Optional[dict]:
    """当日分のキャッシュが有効なら返す。"""
    p = _cache_path(cfg)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at  = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        cache_date = data.get("target_date", "")
        if (datetime.now() - cached_at < timedelta(hours=CACHE_VALID_HOURS)
                and cache_date == target_date):
            return data
    except Exception:
        pass
    return None


def _save_cache(cfg, cache: dict) -> None:
    p = _cache_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        cache["cached_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  us_8k_cache 保存失敗: {e}")


# ==============================================================
# BLOCK 2: EDGAR 8-K 取得
# ==============================================================

def _fetch_8k_filings(target_date: str) -> list[dict]:
    """
    EDGAR full-text search から指定日の 8-K 提出一覧を取得する。

    Args:
        target_date: "YYYY-MM-DD" 形式（通常は前日）

    Returns:
        [{cik, company_name, accession_number, filing_date, doc_url}, ...]
    """
    try:
        params = {
            "q":          '"8-K"',
            "dateRange":  "custom",
            "startdt":    target_date,
            "enddt":      target_date,
            "forms":      "8-K",
            "_source":    "filing_date,entity_name,file_num,period_of_report,biz_location",
            "from":       "0",
            "size":       str(MAX_8K_PER_DAY),
        }
        url  = f"{EDGAR_SEARCH_URL}?{urlencode(params)}"
        data = sec_client.get_json(url, timeout=REQUEST_TIMEOUT)
        if not data:
            return []

        hits  = data.get("hits", {}).get("hits", [])
        results = []
        for hit in hits:
            src = hit.get("_source", {})
            cik_raw  = src.get("file_num", "") or ""
            # file_num は "0000320193-24-000001" 形式から CIK を抽出
            entity   = src.get("entity_name", "")
            # 2026-10-06: EFTS の _id は "受付番号:ファイル名"（例 0001832483-26-000040:serv-20261005.htm）。
            #   旧実装は _id をそのまま受付番号として索引URLを作り 403 になっていた。受付番号は adsh、
            #   会社の CIK は ciks[0]（受付番号の先頭は提出代行者の CIK のことがある）を使う。
            accession = src.get("adsh") or hit.get("_id", "").split(":")[0]
            ciks = src.get("ciks") or []
            cik_str  = (ciks[0] if ciks else accession.split("-")[0]).lstrip("0")
            try:
                cik = int(cik_str)
            except ValueError:
                cik = 0

            if cik == 0:
                continue

            # ドキュメントURL
            acc_no_dash = accession.replace("-", "")
            doc_url     = (f"{EDGAR_ARCHIVE_URL}/data/{cik}/"
                           f"{acc_no_dash}/{accession}-index.htm")

            results.append({
                "cik":              cik,
                "company_name":     entity,
                "accession_number": accession,
                "filing_date":      target_date,
                "doc_url":          doc_url,
                "items":            list(src.get("items") or []),
            })

        return results

    except Exception as e:
        print(f"    ⚠️  EDGAR 8-K 取得失敗: {str(e)[:60]}")
        return []


# ==============================================================
# BLOCK 3: Item 番号の検知
# ==============================================================

def _fetch_8k_items(cik: int, accession_number: str) -> list[str]:
    """
    8-K 本文から Item 番号を抽出する。

    Returns:
        ["2.02", "9.01", ...] 形式の Item 番号リスト
    """
    try:
        acc_no_dash = accession_number.replace("-", "")
        # インデックスページを取得
        idx_url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                   f"{acc_no_dash}/{accession_number}-index.htm")
        resp = sec_client.get(idx_url, accept="text/html", timeout=REQUEST_TIMEOUT)
        if resp is None:
            return []

        text = resp.text
        # "Item X.XX" 形式を正規表現で抽出
        items = re.findall(r'Item\s+(\d+\.\d+)', text, re.IGNORECASE)
        # 重複除去
        return list(dict.fromkeys(items))

    except Exception:
        return []


# ==============================================================
# BLOCK 4: CIK → Ticker 変換
# ==============================================================

def _load_cik_to_ticker_map(cfg) -> dict:
    """
    sec_client.cik_map()（{TICKER: "0000320193"} 形式・共通キャッシュ）から
    {cik_int: ticker} の逆引きマップを作る。

    2026-07-27是正（U-1c）: 従来は us_fundamental_rotator.py が書き出す
    us_cik_map.json（{ticker: cik_int}）を直接読んでいたため、当モジュール
    単体では実行順序に依存していた（rotator 未実行だとスキップされる）。
    sec_client 側の共通 CIK マップに切替えたことでこの依存が解消された。
    """
    try:
        ticker_to_cik = sec_client.cik_map(cfg=cfg)
        result = {}
        for ticker, cik_str in ticker_to_cik.items():
            try:
                result[int(cik_str)] = ticker
            except (TypeError, ValueError):
                continue
        return result
    except Exception:
        return {}


# ==============================================================
# BLOCK 5: screener 対象銘柄との照合
# ==============================================================

def _get_screener_tickers(results: dict) -> set:
    """
    results["us_screener"] から screener 通過候補の ticker セットを返す。
    """
    tickers = set()
    us_screener = results.get("us_screener", {})
    for c in us_screener.get("candidates", []):
        t = c.get("ticker", "")
        if t:
            tickers.add(t.upper())
    return tickers


# ==============================================================
# BLOCK 6: イベント分類と優先度付け
# ==============================================================

def _classify_event(items: list[str]) -> tuple[list[str], str]:
    """
    Item 番号リストからイベント種別と優先度を判定する。

    Returns:
        (event_types: list, priority: "high"/"medium"/"low")
    """
    event_types = []
    max_priority = "low"
    priority_rank = {"high": 2, "medium": 1, "low": 0}

    for item in items:
        etype = ITEM_EVENT_MAP.get(item)
        if etype:
            event_types.append(etype)
            p = ITEM_PRIORITY.get(etype, "low")
            if priority_rank[p] > priority_rank[max_priority]:
                max_priority = p

    return event_types, max_priority


# ==============================================================
# BLOCK 7: us_fundamental_rotator への通知
# ==============================================================

def _notify_fundamental_rotator(cfg, priority_tickers: list) -> None:
    """
    8-K 検知銘柄を us_fundamental_rotator の優先再取得キューに追加する。

    実装: us_fundamentals_meta.json の next_due を今日に更新する
    （次回の rotator 実行時に自動的に優先取得される）。
    """
    meta_path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "us_fundamentals_meta.json"
    if not meta_path.exists() or not priority_tickers:
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        today = date.today().strftime("%Y-%m-%d")
        updated = 0
        for ticker in priority_tickers:
            t = ticker.upper()
            if t in meta:
                meta[t]["next_due"] = today   # 今日に設定して優先再取得
                meta[t]["8k_trigger"] = True
                updated += 1
            else:
                # meta に存在しない場合は新規登録
                meta[t] = {
                    "ticker":     t,
                    "next_due":   today,
                    "last_status": "pending_8k",
                    "8k_trigger": True,
                }
                updated += 1
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if updated:
            print(f"    📥 us_fundamental_rotator 優先キュー更新: {updated}社")
    except Exception as e:
        print(f"    ⚠️  fundamental_rotator 通知失敗: {e}")


# ==============================================================
# BLOCK 8: メインエントリポイント
# ==============================================================

def run_us_8k_monitor(cfg, results: dict = None) -> dict:
    """
    SEC EDGAR 8-K リアルタイム監視を実行する。

    stock_analyzer_main.py の edinet_rotation / us_fundamentals ブロックの
    直後に DAILY_MODULES として呼び出す:
        _8k = _load_local_module("us_8k_monitor", ...)
        k8_result = _8k.run_us_8k_monitor(cfg, results)
        results["us_8k"] = k8_result

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の results dict
                  results["us_screener"] からscreener通過銘柄を照合

    Returns:
        上記スキーマの dict
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_US_8K_MONITOR", True):
        return {"skipped": True}

    results = results or {}

    # 監視対象日（前営業日。土→金・日→金を考慮）
    today     = date.today()
    weekday   = today.weekday()
    if weekday == 0:      # 月曜 → 金曜
        delta = 3
    elif weekday == 6:    # 日曜 → 金曜
        delta = 2
    else:
        delta = 1
    target_date = (today - timedelta(days=delta)).strftime("%Y-%m-%d")

    print(f"\n  📋 US 8-K モニター 開始（{target_date} 提出分）...")
    t0 = datetime.now()

    # ── Step1: キャッシュ確認 ─────────────────────────────────
    cached = _load_cache(cfg, target_date)
    if cached:
        n = cached.get("total", 0)
        print(f"    ✅ キャッシュ使用（{n}件 / 有効期間内）")
        return {**cached, "cached": True}

    # ── Step2: CIK→Ticker マップ読み込み ─────────────────────
    cik_to_ticker = _load_cik_to_ticker_map(cfg)
    if not cik_to_ticker:
        print("    ⚠️  SEC CIKマップが取得できません（sec_client.cik_map() が空）")
        return {"skipped": True, "reason": "cik_map not found"}

    # ── Step3: screener 通過銘柄セット取得 ───────────────────
    screener_tickers = _get_screener_tickers(results)
    # ウォッチリストも対象に含める
    watchlist = set(t.upper() for t in getattr(cfg, "US_WATCHLIST_TICKERS", []))
    target_set = screener_tickers | watchlist

    # ── Step4: EDGAR から 8-K 一覧を取得 ─────────────────────
    print(f"    📥 EDGAR 8-K 取得中（{target_date}）...")
    filings = _fetch_8k_filings(target_date)
    print(f"    取得: {len(filings)}件")

    if not filings:
        print("    ⚠️  8-K が0件（市場休場 or 取得失敗）")
        result = {
            "events":            [],
            "earnings_tickers":  [],
            "ceo_change_tickers": [],
            "ma_tickers":        [],
            "screener_hits":     [],
            "total":             0,
            "target_date":       target_date,
            "cached":            False,
            "timestamp":         t0.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_cache(cfg, result)
        return result

    # ── Step5: CIK→ticker変換・Item検知・分類 ───────────────
    events            = []
    earnings_tickers  = []
    ceo_change_tickers = []
    ma_tickers        = []
    screener_hits     = []
    priority_for_rotator = []

    # item 検知はウォッチリスト・screener 銘柄のみ行う（API負荷削減）
    max_item_fetch = getattr(cfg, "US_8K_MAX_ITEM_FETCH", 30)
    item_fetched   = 0

    for filing in filings:
        cik    = filing["cik"]
        ticker = cik_to_ticker.get(cik, "").upper()
        if not ticker:
            continue   # ticker が不明な場合はスキップ

        # Item 検知（対象銘柄のみ実行・上限あり）
        # 2026-10-06: EFTS の検索結果に Item 番号（例 "2.02"）が入っているのでそれを使う。
        #   無いときだけ従来どおり対象銘柄の索引ページを読む。
        items = list(filing.get("items") or [])
        is_target = (ticker in target_set)
        if not items and is_target and item_fetched < max_item_fetch:
            items = _fetch_8k_items(cik, filing["accession_number"])
            item_fetched += 1
            time.sleep(FETCH_DELAY)

        event_types, priority = _classify_event(items)

        ev = {
            "ticker":       ticker,
            "cik":          cik,
            "company_name": filing["company_name"],
            "items":        items,
            "event_types":  event_types,
            "filing_date":  filing["filing_date"],
            "doc_url":      filing["doc_url"],
            "priority":     priority,
            "in_screener":  ticker in screener_tickers,
            "in_watchlist": ticker in watchlist,
        }
        events.append(ev)

        # カテゴリ別リストへ追加
        if "earnings" in event_types:
            earnings_tickers.append(ticker)
        if "ceo_change" in event_types:
            ceo_change_tickers.append(ticker)
        if "ma_other" in event_types or "asset_acquisition" in event_types:
            ma_tickers.append(ticker)
        if is_target:
            screener_hits.append(ticker)
            priority_for_rotator.append(ticker)

    # ── Step6: us_fundamental_rotator に優先再取得を通知 ──────
    if priority_for_rotator:
        _notify_fundamental_rotator(cfg, priority_for_rotator)

    # 重複除去
    earnings_tickers   = list(dict.fromkeys(earnings_tickers))
    ceo_change_tickers = list(dict.fromkeys(ceo_change_tickers))
    ma_tickers         = list(dict.fromkeys(ma_tickers))
    screener_hits      = list(dict.fromkeys(screener_hits))

    elapsed = (datetime.now() - t0).seconds
    print(
        f"    ✅ 8-K モニター完了（{elapsed}秒）: "
        f"決算{len(earnings_tickers)} / "
        f"CEO交代{len(ceo_change_tickers)} / "
        f"M&A{len(ma_tickers)} / "
        f"screener照合{len(screener_hits)}"
    )
    if screener_hits:
        print(f"    ⚡ screener対象: {screener_hits[:5]}")

    result = {
        "events":             events,
        "earnings_tickers":   earnings_tickers,
        "ceo_change_tickers": ceo_change_tickers,
        "ma_tickers":         ma_tickers,
        "screener_hits":      screener_hits,
        "total":              len(events),
        "target_date":        target_date,
        "cached":             False,
        "timestamp":          t0.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_cache(cfg, result)
    return result
