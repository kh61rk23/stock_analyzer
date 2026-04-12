# ==============================================================
# us_fundamental_rotator.py  v1.0  (Phase US-1)
# 米国株 SEC EDGAR XBRL 財務DBローテーター
# ==============================================================
# 【役割】
#   JP の edinet_rotation_manager.py の米国版。
#   S&P500（500社）を 20社/日 × 25日 = 月1周 のペースで
#   SEC EDGAR XBRL API から財務データを取得し、ローカルDBに蓄積する。
#
# 【なぜ最優先か】
#   I-1（EPS加速度スコア）の精度が yfinance の earnings_history より
#   XBRL の us-gaap/EarningsPerShareDiluted の方が大幅に高い。
#   XBRL は四半期ごとの確定値のため、yfinance の estimateと混在しない。
#
# 【データソース（全て無料・APIキー不要）】
#   財務XBRL : https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json
#   企業情報  : https://data.sec.gov/submissions/CIK{10桁0埋め}.json
#   CIK一覧   : https://www.sec.gov/files/company_tickers.json
#
# 【取得内容（per CIK）】
#   EPS     : us-gaap/EarningsPerShareDiluted（四半期）
#   売上高  : us-gaap/Revenues または SalesRevenueNet
#   営業利益: us-gaap/OperatingIncomeLoss
#   純利益  : us-gaap/NetIncomeLoss
#   FCF     : OperatingCashFlow - CapitalExpenditures
#   自己株買: us-gaap/PaymentsForRepurchaseOfCommonStock
#
# 【回転設計】
#   S&P500（500社）優先 → 20社/日 × 25日 = 月1周
#   8-K提出タイミングを submissions から検知 → ウォッチリストに動的追加
#   鮮度管理: 取得後30日は再取得しない（財務は四半期更新）
#
# 【キャッシュ構造】
#   stock_data/data/us_fundamentals_db/{ticker}.json  （銘柄ごと）
#   stock_data/data/us_fundamentals_meta.json          （管理台帳）
#   stock_data/data/us_cik_map.json                    （ticker→CIK変換）
#
# 【screener との連携】
#   us_stock_screener.py の _fetch_ticker_data() が
#   lookup_us_fundamentals(ticker) を呼び出して yfinance を補完する。
#
# 【I-1との連携】
#   calc_eps_accel_score_xbrl(ticker) が XBRL EPS履歴から
#   より高精度な earnings_accel_score を算出する。
#
# 【依存】requests / config.py
# 【呼出】from us_fundamental_rotator import (
#             run_us_fundamental_rotator,
#             lookup_us_fundamentals,
#             calc_eps_accel_score_xbrl,
#         )
# ==============================================================

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

SEC_BASE_URL       = "https://data.sec.gov"
COMPANY_TICKERS_URL = f"{SEC_BASE_URL}/files/company_tickers.json"
SUBMISSIONS_URL    = f"{SEC_BASE_URL}/submissions/CIK{{:010d}}.json"
XBRL_FACTS_URL     = f"{SEC_BASE_URL}/api/xbrl/companyfacts/CIK{{:010d}}.json"

DB_DIR_NAME        = "us_fundamentals_db"
META_FILENAME      = "us_fundamentals_meta.json"
CIK_MAP_FILENAME   = "us_cik_map.json"

CACHE_DAYS         = 30    # 財務データの再取得間隔（日）
DAILY_FETCH_LIMIT  = 20    # 1日あたりの最大取得社数
REQUEST_DELAY      = 0.12  # SEC API のレート制限対策（10req/s上限）
REQUEST_TIMEOUT    = 15

# 取得する XBRL 財務項目の優先順（上から順に試す）
XBRL_FACT_MAP = {
    "eps_diluted": [
        "EarningsPerShareDiluted",
        "EarningsPerShareBasicAndDiluted",
        "EarningsPerShareBasic",
    ],
    "revenue": [
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueGoodsNet",
    ],
    "operating_income": [
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ],
    "net_income": [
        "NetIncomeLoss",
        "NetIncomeLossAvailableToCommonStockholdersDiluted",
    ],
    "operating_cf": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
        "CapitalExpendituresIncurredButNotYetPaid",
    ],
    "buyback": [
        "PaymentsForRepurchaseOfCommonStock",
        "StockRepurchasedAndRetiredDuringPeriodValue",
    ],
    "shares_diluted": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingDiluted",
    ],
}


# ==============================================================
# BLOCK 1: ファイル I/O
# ==============================================================

def _db_dir(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / DB_DIR_NAME


def _meta_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / META_FILENAME


def _cik_map_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CIK_MAP_FILENAME


def load_meta(cfg) -> dict:
    """
    管理台帳を読み込む。
    構造:
      {ticker: {cik, name, last_fetched, next_due, fetch_count, priority}}
    """
    p = _meta_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_meta(cfg, meta: dict) -> None:
    p = _meta_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  us_fundamentals_meta.json 保存失敗: {e}")


def load_cik_map(cfg) -> dict:
    """ticker → CIK の変換マップを読み込む。"""
    p = _cik_map_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cik_map(cfg, cik_map: dict) -> None:
    p = _cik_map_path(cfg)
    try:
        p.write_text(json.dumps(cik_map, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  us_cik_map.json 保存失敗: {e}")


def _ticker_path(cfg, ticker: str) -> Path:
    """個別銘柄の財務DBパスを返す。"""
    safe = ticker.replace("/", "_").replace(".", "_")
    return _db_dir(cfg) / f"{safe}.json"


def save_ticker_db(cfg, ticker: str, data: dict) -> None:
    p = _ticker_path(cfg, ticker)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data["_saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  {ticker}.json 保存失敗: {e}")


def lookup_us_fundamentals(cfg, ticker: str) -> Optional[dict]:
    """
    us_stock_screener.py から呼び出すメインインターフェース。
    ローカルDBから1銘柄の財務データを返す。

    Returns:
        {eps_history, revenue_history, operating_income_history,
         net_income_history, fcf_history, eps_accel_score, ...}
        | None（データなし）
    """
    p = _ticker_path(cfg, ticker)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ==============================================================
# BLOCK 2: CIK 取得とマッピング
# ==============================================================

def _fetch_company_tickers() -> dict:
    """
    SEC の company_tickers.json を取得して ticker→CIK のマッピングを返す。
    全上場企業のCIKを一括取得できる（約15,000社）。

    Returns:
        {ticker_upper: cik_int}
    """
    try:
        import requests
        resp = requests.get(
            COMPANY_TICKERS_URL,
            headers={"User-Agent": "stock_analyzer/1.0 (educational use)"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        # 形式: {"0": {"cik_str":"0001018724","ticker":"AMZN","title":"AMAZON.COM INC"}, ...}
        mapping = {}
        for v in data.values():
            t = v.get("ticker", "").upper()
            c = v.get("cik_str", "")
            if t and c:
                try:
                    mapping[t] = int(c)
                except ValueError:
                    pass
        return mapping
    except Exception as e:
        print(f"    ⚠️  company_tickers.json 取得失敗: {str(e)[:60]}")
        return {}


def get_or_update_cik_map(cfg, force_refresh: bool = False) -> dict:
    """
    CIKマップを取得する。
    存在しない場合 or force_refresh=True の場合はSECから取得して保存。

    Returns:
        {ticker_upper: cik_int}
    """
    cik_map = load_cik_map(cfg)
    if cik_map and not force_refresh:
        return cik_map

    print("    📥 SEC CIK一覧を取得中...")
    new_map = _fetch_company_tickers()
    if new_map:
        save_cik_map(cfg, new_map)
        print(f"    ✅ CIKマップ更新: {len(new_map)}社")
        return new_map
    return cik_map  # 取得失敗時はキャッシュを返す


# ==============================================================
# BLOCK 3: XBRL 財務データ取得
# ==============================================================

def _fetch_submissions(cik: int) -> Optional[dict]:
    """
    submissions/{CIK}.json を取得して最新提出情報を返す。
    8-K の提出タイミング検知に使用する。
    """
    try:
        import requests
        url  = SUBMISSIONS_URL.format(cik)
        resp = requests.get(
            url,
            headers={"User-Agent": "stock_analyzer/1.0 (educational use)"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _fetch_xbrl_facts(cik: int) -> Optional[dict]:
    """
    XBRL companyfacts/{CIK}.json を取得する（全財務データの塊）。
    レスポンスは大きい（数MB）ため、必要項目のみ抽出する。
    """
    try:
        import requests
        url  = XBRL_FACTS_URL.format(cik)
        resp = requests.get(
            url,
            headers={"User-Agent": "stock_analyzer/1.0 (educational use)"},
            timeout=30,  # XBRL は大きいので長め
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _extract_quarterly_series(
    facts_data: dict,
    fact_keys:  list[str],
    n_quarters: int = 12,
) -> list[dict]:
    """
    XBRL facts から指定項目の四半期データを抽出する。

    優先順に fact_keys を試し、最初に見つかったものを使用。

    Returns:
        [{"period": "YYYY-MM-DD", "val": float, "form": "10-Q"/"10-K"}, ...]
        新しい順にソート済み / 最大 n_quarters 件
    """
    us_gaap = (facts_data.get("facts") or {}).get("us-gaap", {})

    for key in fact_keys:
        fact = us_gaap.get(key)
        if not fact:
            continue

        units = fact.get("units", {})
        # EPS は USD/shares、金額は USD
        unit_data = units.get("USD/shares") or units.get("USD")
        if not unit_data:
            continue

        # 四半期（10-Q）と年次（10-K）のデータを収集
        # accn（提出番号）の重複を除き、period_of_report でソート
        seen_period = {}
        for entry in unit_data:
            form   = entry.get("form", "")
            if form not in ("10-Q", "10-K"):
                continue
            end_dt = entry.get("end", "")
            val    = entry.get("val")
            if not end_dt or val is None:
                continue
            # 同一期間は最新提出を採用
            if end_dt not in seen_period:
                seen_period[end_dt] = {"period": end_dt, "val": float(val), "form": form}

        if not seen_period:
            continue

        sorted_data = sorted(seen_period.values(), key=lambda x: x["period"], reverse=True)
        return sorted_data[:n_quarters]

    return []


def _calc_fcf(facts_data: dict, n_quarters: int = 8) -> list[dict]:
    """
    FCF = OperatingCashFlow - CapitalExpenditures を計算する。
    """
    op_cf  = _extract_quarterly_series(facts_data, XBRL_FACT_MAP["operating_cf"],  n_quarters)
    capex  = _extract_quarterly_series(facts_data, XBRL_FACT_MAP["capex"],          n_quarters)

    if not op_cf or not capex:
        return op_cf  # 片方しかない場合は OperatingCF だけ返す

    # 期間が一致するものを紐付けて FCF を計算
    capex_map = {e["period"]: e["val"] for e in capex}
    fcf_data  = []
    for entry in op_cf:
        period = entry["period"]
        cap    = capex_map.get(period)
        if cap is not None:
            fcf_data.append({
                "period": period,
                "val":    round(entry["val"] - abs(cap), 0),
                "form":   entry["form"],
            })
    return fcf_data


def _extract_8k_dates(submissions: dict) -> list[str]:
    """
    submissions から 8-K の提出日一覧を返す（直近10件）。
    8-K = 決算・重要事項の速報開示。
    """
    try:
        recent = submissions.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        dates  = recent.get("filingDate", [])
        result = []
        for f, d in zip(forms, dates):
            if f == "8-K":
                result.append(d)
        return result[:10]
    except Exception:
        return []


# ==============================================================
# BLOCK 4: I-1 EPS 加速度スコア（XBRL版）
# ==============================================================

def calc_eps_accel_score_xbrl(
    eps_history: list[dict],
    n_compare: int = 4,
) -> Optional[float]:
    """
    [I-1強化版] XBRL の四半期 EPS 系列から EPS 加速度スコアを算出する。

    yfinance の earnings_history より精度が高い理由:
      - yfinance は estimate と actual が混在するケースがある
      - XBRL は SEC 提出済みの確定値のみ（restated 含む）

    計算ロジック:
      直近4四半期 vs 1年前4四半期の YoY 成長率を比較。
      成長率が加速している（YoY が拡大中）= 加速シグナル。

    Returns:
        acc_bonus: -10.0〜+10.0（既存の I-1 と同じスケール）
        None: データ不足
    """
    if not eps_history or len(eps_history) < 8:
        return None

    # 新しい順に並んでいると仮定（_extract_quarterly_seriesがそう返す）
    # 直近4四半期の EPS
    recent4  = [e["val"] for e in eps_history[:4]]
    # 1年前の4四半期
    prior4   = [e["val"] for e in eps_history[4:8]]

    if len(recent4) < 4 or len(prior4) < 4:
        return None

    recent_avg = sum(recent4) / 4
    prior_avg  = sum(prior4)  / 4

    if abs(prior_avg) < 0.01:
        return None   # ゼロ除算回避

    # YoY 成長率
    yoy_growth = (recent_avg - prior_avg) / abs(prior_avg)

    # 成長率の加速度（最新Qと4Q前のYoY比較）
    if len(eps_history) >= 5 and abs(eps_history[4]["val"]) > 0.01:
        q1_yoy = (eps_history[0]["val"] - eps_history[4]["val"]) / abs(eps_history[4]["val"])
    else:
        q1_yoy = yoy_growth

    # acc_bonus: yoy_growth と q1_yoy を合算してスケーリング
    raw = (yoy_growth + q1_yoy) * 5.0   # ±20%成長 → ±2pt、±100%成長 → ±10pt
    acc_bonus = round(max(-10.0, min(10.0, raw)), 2)
    return acc_bonus


# ==============================================================
# BLOCK 5: 1銘柄の取得処理
# ==============================================================

def fetch_one_ticker(
    ticker: str,
    cik:    int,
    cfg,
) -> dict:
    """
    1銘柄の XBRL 財務データを取得して DB に保存する。

    Returns:
        {ok: bool, ticker: str, data: dict, error: str}
    """
    try:
        # XBRL facts 取得
        facts_data = _fetch_xbrl_facts(cik)
        time.sleep(REQUEST_DELAY)

        if not facts_data:
            return {"ok": False, "ticker": ticker, "error": "XBRL取得失敗"}

        # submissions 取得（8-K日程検知用）
        submissions = _fetch_submissions(cik)
        time.sleep(REQUEST_DELAY)

        # 財務項目の抽出
        eps_history      = _extract_quarterly_series(facts_data, XBRL_FACT_MAP["eps_diluted"])
        revenue_history  = _extract_quarterly_series(facts_data, XBRL_FACT_MAP["revenue"])
        oi_history       = _extract_quarterly_series(facts_data, XBRL_FACT_MAP["operating_income"])
        ni_history       = _extract_quarterly_series(facts_data, XBRL_FACT_MAP["net_income"])
        fcf_history      = _calc_fcf(facts_data)
        buyback_history  = _extract_quarterly_series(facts_data, XBRL_FACT_MAP["buyback"], n_quarters=8)
        shares_history   = _extract_quarterly_series(facts_data, XBRL_FACT_MAP["shares_diluted"])

        # 8-K 提出日（直近10件）
        recent_8k_dates  = _extract_8k_dates(submissions) if submissions else []

        # I-1 EPS 加速度スコア（XBRL版）
        eps_accel_score = calc_eps_accel_score_xbrl(eps_history)

        # 直近の財務サマリー（スクリーナー用）
        latest_eps = eps_history[0]["val"]     if eps_history else None
        latest_rev = revenue_history[0]["val"] if revenue_history else None
        latest_ni  = ni_history[0]["val"]      if ni_history else None
        latest_fcf = fcf_history[0]["val"]     if fcf_history else None

        result_data = {
            "ticker":             ticker,
            "cik":                cik,
            "eps_history":        eps_history,
            "revenue_history":    revenue_history,
            "operating_income_history": oi_history,
            "net_income_history": ni_history,
            "fcf_history":        fcf_history,
            "buyback_history":    buyback_history,
            "shares_history":     shares_history,
            "recent_8k_dates":    recent_8k_dates,
            "eps_accel_score_xbrl": eps_accel_score,
            "latest_eps":         latest_eps,
            "latest_revenue":     latest_rev,
            "latest_net_income":  latest_ni,
            "latest_fcf":         latest_fcf,
            "data_source":        "sec_xbrl",
        }
        # ── [④] 鮮度情報を付与 ─────────────────────────────────
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
            _cfm = _imp.import_module("cache_freshness_manager")
            result_data = _cfm.enrich_us_fundamental_with_freshness(result_data, cfg)
        except Exception:
            pass   # 鮮度付与失敗は致命的でない

        save_ticker_db(cfg, ticker, result_data)

        return {"ok": True, "ticker": ticker, "data": result_data, "error": ""}

    except Exception as e:
        err = str(e)[:80]
        return {"ok": False, "ticker": ticker, "error": err}


# ==============================================================
# BLOCK 6: 取得キュー構築
# ==============================================================

def _days_since(date_str: Optional[str]) -> int:
    if not date_str:
        return 999
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return (date.today() - d).days
    except Exception:
        return 999


def _build_fetch_queue(cfg, meta: dict, cik_map: dict) -> list[dict]:
    """
    優先順位に従って本日取得すべき銘柄リストを返す。

    優先度:
      1. ウォッチリスト（US_WATCHLIST_TICKERS）
      2. screener 通過候補（us_screener の結果）
      3. S&P500 ローテーション（期限切れ順）
    """
    watchlist   = getattr(cfg, "US_WATCHLIST_TICKERS", [])
    cache_days  = getattr(cfg, "US_FUNDAMENTALS_CACHE_DAYS", CACHE_DAYS)
    limit       = getattr(cfg, "US_DAILY_FETCH_LIMIT", DAILY_FETCH_LIMIT)

    queue = []
    seen  = set()

    def _add(ticker: str, priority: int, reason: str):
        t = ticker.upper()
        if t in seen or t not in cik_map:
            return
        seen.add(t)
        queue.append({"ticker": t, "cik": cik_map[t], "priority": priority, "reason": reason})

    # ── 優先度1: ウォッチリスト ──────────────────────────────
    for t in watchlist:
        entry = meta.get(t.upper(), {})
        if _days_since(entry.get("last_fetched")) >= cache_days:
            _add(t, 1, "watchlist")

    # ── 優先度2: S&P500 ローテーション（期限切れ順） ─────────
    sp500_tickers = getattr(cfg, "SP500_TICKERS", [])
    rotation_targets = []
    for t in sp500_tickers:
        tu = t.upper()
        if tu in seen or tu not in cik_map:
            continue
        entry    = meta.get(tu, {})
        days_old = _days_since(entry.get("last_fetched"))
        if days_old >= cache_days:
            rotation_targets.append((days_old, tu))

    # 古い順（未取得を最優先）
    rotation_targets.sort(key=lambda x: -x[0])
    for days_old, t in rotation_targets:
        if len(queue) >= limit:
            break
        _add(t, 2, f"sp500_rotation({days_old}日経過)")

    queue = queue[:limit]

    p1 = sum(1 for q in queue if q["priority"] == 1)
    p2 = sum(1 for q in queue if q["priority"] == 2)
    print(f"    📋 取得キュー: {len(queue)}社 "
          f"（watchlist:{p1} / sp500:{p2}）")
    return queue


# ==============================================================
# BLOCK 7: 8-K ウォッチリスト動的追加
# ==============================================================

def _detect_recent_8k_tickers(meta: dict, lookback_days: int = 3) -> list[str]:
    """
    meta から直近 lookback_days 日以内に 8-K を提出した銘柄を返す。
    これらは決算発表直後の可能性が高いため優先的に再取得する。
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    refresh_targets = []
    for ticker, entry in meta.items():
        recent_8k = entry.get("recent_8k_dates", [])
        if any(d >= cutoff for d in recent_8k):
            last_fetched = entry.get("last_fetched", "")
            # 最後の取得が8-K提出より古い場合は再取得対象
            if not last_fetched or last_fetched < max(recent_8k):
                refresh_targets.append(ticker)
    return refresh_targets[:5]   # 最大5社/日


# ==============================================================
# BLOCK 8: メタ更新
# ==============================================================

def _update_meta(meta: dict, result: dict) -> None:
    """1銘柄の取得結果を meta に反映する。"""
    ticker = result["ticker"]
    today  = date.today().strftime("%Y-%m-%d")

    entry = meta.get(ticker, {
        "ticker":      ticker,
        "fetch_count": 0,
    })
    entry["fetch_count"] = entry.get("fetch_count", 0) + 1

    if result["ok"]:
        data = result.get("data", {})
        entry["last_fetched"]   = today
        entry["last_status"]    = "ok"
        entry["recent_8k_dates"] = data.get("recent_8k_dates", [])
        entry["eps_latest"]     = data.get("latest_eps")
        next_due = (date.today() + timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%d")
        entry["next_due"]       = next_due
    else:
        entry["last_status"]    = f"error: {result.get('error','')}"
        entry["next_due"]       = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")

    meta[ticker] = entry


# ==============================================================
# BLOCK 9: メインエントリポイント
# ==============================================================
# 「dict または None」という意味
def run_us_fundamental_rotator(cfg, results: dict | None = None) -> dict:
    """
    US 財務 DB ローテーターを実行する。

    stock_analyzer_main.py の edinet_rotation ブロックの直後に呼び出す:
        _ufr = _load_local_module("us_fundamental_rotator", ...)
        ufr_result = _ufr.run_us_fundamental_rotator(cfg, results)
        results["us_fundamentals"] = ufr_result

    処理:
      1. CIK マップの確認・更新（初回のみDL・以降は月1回更新）
      2. 取得キューの構築（優先度順）
      3. 8-K 直後銘柄のウォッチリスト動的追加
      4. XBRL facts を1社ずつ取得
      5. EPS加速度スコア（I-1強化版）を計算
      6. meta.json を更新

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の results dict（screener結果の参照用）

    Returns:
        {
          "ok_count":        int,
          "error_count":     int,
          "db_coverage":     float,   # 取得済み / S&P500 全社数
          "db_total":        int,
          "refresh_8k":      [ticker, ...],  # 8-K直後に再取得した銘柄
          "timestamp":       str,
          "skipped":         bool,
        }
    """
    if not getattr(cfg, "ENABLE_US_FUNDAMENTALS", True):
        return {"skipped": True}

    print("\n  🇺🇸 US財務DBローテーター 開始...")
    t0 = datetime.now()

    # ── CIK マップの取得 ─────────────────────────────────────
    cik_map = load_cik_map(cfg)
    if not cik_map:
        print("    📥 初回: SEC CIK一覧を取得します...")
        cik_map = get_or_update_cik_map(cfg)
        if not cik_map:
            print("    ❌ CIK一覧取得失敗 → スキップ")
            return {"skipped": True, "error": "CIK一覧取得失敗"}
    else:
        # CIKマップは月1回更新（30日以上経過で再取得）
        meta_p = _cik_map_path(cfg)
        if meta_p.exists():
            try:
                mtime_days = (_days_since(
                    datetime.fromtimestamp(meta_p.stat().st_mtime).strftime("%Y-%m-%d")
                ))
                if mtime_days > 30:
                    print("    🔄 CIK一覧を月次更新中...")
                    cik_map = get_or_update_cik_map(cfg, force_refresh=True)
            except Exception:
                pass

    # ── メタ読み込み ─────────────────────────────────────────
    meta = load_meta(cfg)
    print(f"    📚 既存DB: {len(meta)}社管理中")

    # ── 8-K ウォッチ銘柄を動的追加 ───────────────────────────
    refresh_8k = _detect_recent_8k_tickers(meta)
    if refresh_8k:
        print(f"    ⚡ 8-K直後銘柄を優先再取得: {refresh_8k}")

    # ── 取得キュー構築 ────────────────────────────────────────
    # 8-K銘柄をウォッチリストに一時追加
    existing_watchlist = list(getattr(cfg, "US_WATCHLIST_TICKERS", []))
    if refresh_8k:
        cfg.__dict__["US_WATCHLIST_TICKERS"] = refresh_8k + existing_watchlist

    queue = _build_fetch_queue(cfg, meta, cik_map)

    # US screener の通過銘柄も追加（screener_candidates から）
    if results:
        us_screener_res = results.get("us_screener", {})
        us_passed_tickers = [
            c.get("ticker", "") for c in us_screener_res.get("candidates", [])
            if c.get("pass_filters") and c.get("ticker", "").upper() in cik_map
        ][:5]   # 最大5社追加
        if us_passed_tickers:
            existing_cands = {q["ticker"] for q in queue}
            for t in us_passed_tickers:
                if t not in existing_cands:
                    queue.append({
                        "ticker":   t,
                        "cik":      cik_map[t],
                        "priority": 1,
                        "reason":   "us_screener_pass",
                    })

    # ウォッチリストを元に戻す
    cfg.__dict__["US_WATCHLIST_TICKERS"] = existing_watchlist

    if not queue:
        print("  ✅ 本日の取得対象なし（全銘柄が有効期間内）")
        return {
            "skipped":      False,
            "ok_count":     0,
            "error_count":  0,
            "db_coverage":  _calc_coverage(meta, cfg),
            "db_total":     len(meta),
            "refresh_8k":   refresh_8k,
            "timestamp":    t0.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 取得ループ ────────────────────────────────────────────
    ok_count    = 0
    error_count = 0

    for i, item in enumerate(queue, 1):
        ticker   = item["ticker"]
        cik      = item["cik"]
        reason   = item["reason"]
        print(f"    [{i}/{len(queue)}] {ticker} (CIK={cik} / {reason})...")

        result = fetch_one_ticker(ticker, cik, cfg)
        _update_meta(meta, result)

        if result["ok"]:
            ok_count += 1
            data = result["data"]
            eps  = data.get("latest_eps")
            accel = data.get("eps_accel_score_xbrl")
            print(
                f"      ✅ EPS={eps:.2f}" if eps else "      ✅",
                f"加速Sc={accel:+.1f}" if accel is not None else "",
            )
        else:
            error_count += 1
            print(f"      ❌ {result.get('error','')[:60]}")

    # ── meta 保存 ─────────────────────────────────────────────
    save_meta(cfg, meta)

    elapsed   = (datetime.now() - t0).seconds
    coverage  = _calc_coverage(meta, cfg)
    sp500_n   = len(getattr(cfg, "SP500_TICKERS", []))

    print(
        f"\n  🏁 US財務DBローテーター完了（{elapsed}秒）\n"
        f"     取得成功: {ok_count} / 失敗: {error_count}\n"
        f"     DBカバレッジ: {coverage:.1f}% "
        f"（{len(meta)}/{max(sp500_n,1)}社）"
    )

    return {
        "skipped":      False,
        "ok_count":     ok_count,
        "error_count":  error_count,
        "db_coverage":  coverage,
        "db_total":     len(meta),
        "refresh_8k":   refresh_8k,
        "elapsed_sec":  elapsed,
        "timestamp":    t0.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _calc_coverage(meta: dict, cfg) -> float:
    """取得済み / S&P500 全社数 のカバレッジ率を返す。"""
    sp500_n = len(getattr(cfg, "SP500_TICKERS", []))
    if sp500_n == 0:
        return len(meta)
    fetched = sum(1 for v in meta.values() if v.get("last_fetched"))
    return fetched / sp500_n * 100
