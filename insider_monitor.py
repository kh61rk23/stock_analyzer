# ==============================================================
# insider_monitor.py  v1.0  (4-2 / A-3)
# SEC Form 4 RSS（米国）+ EDINET 大量保有報告書（日本）
# インサイダー・大株主の買い動向を screener ボーナスに統合
# ==============================================================
# 【役割】
#   役員・大株主の株式取得情報を自動収集し、
#   「役員が自社株を買っている = 内部者が上昇を確信している」
#   シグナルを screener の quality_score に反映する。
#
# 【データソース】
#   米国株: SEC EDGAR RSS (Form 4)
#     https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany
#       &type=4&dateb=&owner=include&count=40&search_text=
#     → APIキー不要・無料（レート制限: 10req/秒）
#
#   日本株: EDINET 大量保有報告書
#     edinet_client.py の get_analysis() 経由で取得済みのデータを参照
#     → 大量保有報告（5%超の株式取得）を検出
#
# 【screener ボーナス計算】
#   役員買い（直近30日以内）:
#     1件: quality_score +3pt
#     2件以上（連続買い）: +6pt
#   役員売り（直近30日以内）:
#     1件: quality_score -3pt
#     2件以上: -6pt
#   大量保有報告（5%以上取得）: +4pt
#
# 【出力スキーマ】
#   {
#     "us_filings":   {ticker: [Filing, ...]},  # 米国 Form 4
#     "jp_holdings":  {ticker: [Holding, ...]}, # 日本 大量保有
#     "bonuses":      {ticker: float},           # screener ボーナス
#     "alert_items":  [str, ...],                # アラート文字列
#     "total_us":     int,
#     "total_jp":     int,
#     "timestamp":    str,
#   }
#   Filing = {ticker, filer_name, transaction_type, shares, date, url}
#   Holding = {ticker, holder_name, ownership_pct, report_date}
#
# 【依存】requests / config.py（SEC_USER_AGENT）
# 【呼出】from insider_monitor import run_insider_monitor, get_insider_bonus
# ==============================================================

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
LOOKBACK_DAYS      = 30     # 直近N日のインサイダー取引を参照
CONSECUTIVE_THRESH = 2      # 連続買いと見なす最低件数
CACHE_HOURS        = 24 * 7 # 週次データ = 7日キャッシュ
CACHE_FILENAME     = "insider_monitor_cache.json"
FETCH_DELAY        = 0.12   # SEC レート制限対策（10req/秒→0.1秒間隔）

# screener ボーナス
BONUS_BUY_SINGLE   =  3.0
BONUS_BUY_CONSEC   =  6.0
PENALTY_SELL_SINGLE = -3.0
PENALTY_SELL_CONSEC = -6.0
BONUS_LARGE_HOLDING =  4.0

# SEC EDGAR Form 4 検索 URL
SEC_FORM4_BASE = (
    "https://efts.sec.gov/LATEST/search-index?q=%22Form+4%22"
    "&dateRange=custom&startdt={start}&enddt={end}"
    "&forms=4&hits.hits.total.value=true&hits.hits._source.period_of_report=true"
    "&hits.hits._source.entity_name=true"
)
SEC_EDGAR_COMPANY = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&CIK={cik}&type=4&dateb=&owner=include"
    "&count=10&search_text="
)
SEC_TICKER_CIK_URL = "https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&forms=4"


# ==============================================================
# BLOCK 1: SEC EDGAR Form 4 取得（米国株）
# ==============================================================

def _get_cik(ticker: str, user_agent: str) -> Optional[str]:
    """
    ティッカーから CIK 番号を取得する。
    SEC の company_tickers.json を利用（日次更新・高速）。
    """
    try:
        import requests
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index?q=%22{t}%22&forms=4".format(t=ticker),
            headers=headers, timeout=8,
        )
        if r.status_code != 200:
            return None
        # company_tickers.json: {idx: {cik_str, ticker, title}}
        # より確実な方法: company_tickers_exchange.json
        r2 = requests.get(
            "https://www.sec.gov/files/company_tickers_exchange.json",
            headers=headers, timeout=10,
        )
        if r2.status_code != 200:
            return None
        data = r2.json()
        fields = data.get("fields", [])
        rows   = data.get("data", [])
        try:
            ticker_idx = fields.index("ticker")
            cik_idx    = fields.index("cik")
        except ValueError:
            return None
        for row in rows:
            if row[ticker_idx].upper() == ticker.upper():
                return str(row[cik_idx]).zfill(10)
        return None
    except Exception:
        return None


def _fetch_form4_rss(ticker: str, cik: str, user_agent: str) -> list:
    """
    SEC EDGAR から Form 4（役員取引）を RSS / JSON で取得する。

    Returns:
        [{"filer_name", "transaction_type", "shares", "date", "url"}, ...]
    """
    try:
        import requests
        headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip"}
        cutoff  = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        today   = datetime.now().strftime("%Y-%m-%d")

        # EDGAR full-text search API
        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{cik}%22&forms=4&dateRange=custom"
            f"&startdt={cutoff}&enddt={today}"
        )
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return []

        data    = r.json()
        hits    = data.get("hits", {}).get("hits", [])
        filings = []

        for hit in hits[:10]:  # 最大10件
            src  = hit.get("_source", {})
            period = src.get("period_of_report", "")
            entity = src.get("entity_name", "")
            fid    = hit.get("_id", "")
            filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4"

            # 取引タイプはXML詳細を読まないと不明 → RSIの傾向でシグナル判定
            filings.append({
                "filer_name":        entity,
                "transaction_type":  "buy_signal",  # 詳細XML取得は省略
                "shares":            0,
                "date":              period,
                "url":               filing_url,
                "ticker":            ticker,
            })
            time.sleep(FETCH_DELAY)

        return filings

    except Exception:
        return []


def _parse_form4_simple(ticker: str, user_agent: str) -> list:
    """
    EDGAR EFTS API を使って Form 4 の提出件数から
    インサイダー活動の有無を判定する（軽量版）。

    詳細な取引タイプ（買い/売り）の取得は XML パースが必要で
    時間がかかるため、直近の Form 4 提出数をシグナルとして使用する:
      30日以内に Form 4 が多い = インサイダー活動あり（買いか売りかは不確定）
    より確実な情報は yfinance の insider_purchases / insider_transactions から取得可能。
    """
    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        info = t.info or {}

        # yfinance のインサイダー情報（米国株のみ有効）
        purchases = getattr(t, "insider_purchases", None)
        filings   = []

        # insider_transactions が DataFrame として返される場合
        try:
            txn = t.insider_transactions
            if txn is not None and not txn.empty:
                cutoff_dt = datetime.now() - timedelta(days=LOOKBACK_DAYS)
                for _, row in txn.iterrows():
                    start_date = row.get("Start Date")
                    if start_date is None:
                        continue
                    # Timestamp → date
                    try:
                        txn_date = start_date.to_pydatetime().date()
                    except AttributeError:
                        continue
                    if txn_date < cutoff_dt.date():
                        continue

                    txn_type = str(row.get("Text", "")).lower()
                    shares   = abs(int(row.get("Shares", 0) or 0))
                    value    = row.get("Value")

                    # 買いシグナル判定
                    is_buy = any(kw in txn_type for kw in
                                 ["purchase", "buy", "acquisition", "grant"])
                    is_sell = any(kw in txn_type for kw in
                                  ["sale", "sell", "disposition"])

                    filings.append({
                        "filer_name":       str(row.get("Insider", "")),
                        "transaction_type": "buy" if is_buy else ("sell" if is_sell else "other"),
                        "shares":           shares,
                        "date":             str(txn_date),
                        "url":              "",
                        "ticker":           ticker,
                    })
        except Exception:
            pass

        return filings
    except Exception:
        return []


# ==============================================================
# BLOCK 2: EDINET 大量保有報告書（日本株）
# ==============================================================

def _extract_jp_large_holdings(cfg, tickers: list) -> dict:
    """
    edinet_rotation_manager の蓄積 DB から
    大量保有報告に関連する情報を抽出する。

    EDINET の analysis には risk_factors が含まれており、
    大量保有者の情報が記載されている場合がある（完全自動化は困難）。
    yfinance の majorHolders / institutionalHolders から代替取得する。

    Returns:
        {ticker: [{"holder_name", "ownership_pct", "report_date"}]}
    """
    holdings = {}

    try:
        import yfinance as yf
        for ticker in tickers:
            if not ticker.endswith(".T"):
                continue
            try:
                t   = yf.Ticker(ticker)
                # 主要株主情報
                mh  = t.major_holders
                if mh is not None and not mh.empty:
                    # 1行目: % of Shares Held by All Insider
                    insider_pct = None
                    try:
                        insider_pct = float(mh.iloc[0, 0]) * 100
                    except Exception:
                        pass

                    if insider_pct and insider_pct >= 5.0:
                        holdings.setdefault(ticker, []).append({
                            "holder_name":  "インサイダー合計",
                            "ownership_pct": round(insider_pct, 2),
                            "report_date":   datetime.now().strftime("%Y-%m-%d"),
                            "source":        "yfinance_major_holders",
                        })
                time.sleep(FETCH_DELAY)
            except Exception:
                continue
    except Exception:
        pass

    return holdings


# ==============================================================
# BLOCK 3: ボーナス算出
# ==============================================================

def _calc_bonus(us_filings: list, jp_holdings: list) -> float:
    """
    米国インサイダー取引 + 日本大量保有から screener ボーナスを算出する。

    Returns:
        float: quality_score への加算値（-6 〜 +6）
    """
    bonus = 0.0

    # 米国: 買い/売りカウント
    buys  = [f for f in us_filings if f.get("transaction_type") == "buy"]
    sells = [f for f in us_filings if f.get("transaction_type") == "sell"]

    if len(buys) >= CONSECUTIVE_THRESH:
        bonus += BONUS_BUY_CONSEC
    elif len(buys) == 1:
        bonus += BONUS_BUY_SINGLE

    if len(sells) >= CONSECUTIVE_THRESH:
        bonus += PENALTY_SELL_CONSEC
    elif len(sells) == 1:
        bonus += PENALTY_SELL_SINGLE

    # 日本: 大量保有（5%以上）
    large = [h for h in jp_holdings if h.get("ownership_pct", 0) >= 5.0]
    if large:
        bonus += BONUS_LARGE_HOLDING

    return round(max(-6.0, min(6.0, bonus)), 1)


def get_insider_bonus(ticker: str, insider_cache: dict) -> float:
    """
    stock_screener.py から呼び出すインターフェース。
    キャッシュからボーナス値を返す。
    """
    bonuses = insider_cache.get("bonuses", {})
    return float(bonuses.get(ticker, 0.0))


# ==============================================================
# BLOCK 4: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME

def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(hours=CACHE_HOURS):
            return data
    except Exception:
        pass
    return None

def _save_cache(cfg, data: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        data["cached_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 5: アラートテキスト生成
# ==============================================================

def format_insider_alert(result: dict, top_n: int = 5) -> str:
    """
    インサイダー取引アラートのテキストを生成する。
    """
    alert_items = result.get("alert_items", [])
    total_us    = result.get("total_us", 0)
    total_jp    = result.get("total_jp", 0)

    lines = [f"🔍 インサイダー取引モニター（米{total_us}件 / JP大量保有{total_jp}件）"]
    for item in alert_items[:top_n]:
        lines.append(f"  {item}")

    if not alert_items:
        lines.append("  直近30日: 注目すべき取引なし")

    return "\n".join(lines)


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_insider_monitor(cfg, tickers: Optional[list] = None) -> dict:
    """
    インサイダー取引・大量保有情報を収集してボーナスを算出する。

    stock_analyzer_main.py から週次で呼び出す:
        _im = _load_local_module("insider_monitor", ...)
        im_result = _im.run_insider_monitor(cfg, screener_tickers)
        results["insider_monitor"] = im_result

    Args:
        cfg:     config モジュール
        tickers: 対象ティッカー（screener 通過銘柄）。None なら代表銘柄を使用

    Returns:
        {
          "us_filings":  {ticker: [Filing, ...]},
          "jp_holdings": {ticker: [Holding, ...]},
          "bonuses":     {ticker: float},
          "alert_items": [str, ...],
          "total_us":    int,
          "total_jp":    int,
          "timestamp":   str,
        }
    """
    print("\n  🔍 インサイダー取引モニター開始...")

    cached = _load_cache(cfg)
    if cached:
        print(f"    ✅ キャッシュ使用 (US:{cached.get('total_us',0)}件 "
              f"JP:{cached.get('total_jp',0)}件)")
        return cached

    # 対象銘柄の決定
    if not tickers:
        rep = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
        tickers = [t for ts in rep.values() for t in ts[:2]]
        edinet_cands = getattr(cfg, "EDINET_SCREENER_CANDIDATES", [])
        tickers = list(set(tickers + edinet_cands))

    user_agent = getattr(cfg, "SEC_USER_AGENT", "StockAnalyzer research@example.com")

    us_tickers = [t for t in tickers if not t.endswith(".T")]
    jp_tickers = [t for t in tickers if t.endswith(".T")]

    # ── 米国株: yfinance insider_transactions ────────────────
    print(f"    米国株インサイダー取引取得中 ({len(us_tickers)}銘柄)...")
    us_filings: dict = {}
    for ticker in us_tickers[:30]:   # 上限30銘柄（処理時間制限）
        filings = _parse_form4_simple(ticker, user_agent)
        if filings:
            us_filings[ticker] = filings
        time.sleep(FETCH_DELAY)

    # ── 日本株: yfinance major_holders ───────────────────────
    print(f"    日本株大量保有取得中 ({len(jp_tickers)}銘柄)...")
    jp_holdings = _extract_jp_large_holdings(cfg, jp_tickers[:30])

    # ── ボーナス計算 ─────────────────────────────────────────
    bonuses = {}
    alert_items = []

    for ticker in set(list(us_filings.keys()) + list(jp_holdings.keys())):
        bonus = _calc_bonus(
            us_filings.get(ticker, []),
            jp_holdings.get(ticker, []),
        )
        if bonus != 0:
            bonuses[ticker] = bonus
            sign = "買い🟢" if bonus > 0 else "売り🔴"
            count_us = len(us_filings.get(ticker, []))
            count_jp = len(jp_holdings.get(ticker, []))
            alert_items.append(
                f"{ticker}: {sign} bonus={bonus:+.0f}pt "
                f"(US:{count_us}件 JP:{count_jp}件)"
            )

    total_us = sum(len(v) for v in us_filings.values())
    total_jp = sum(len(v) for v in jp_holdings.values())

    print(f"    ✅ 完了: 米国{total_us}件 / 日本大量保有{total_jp}件 / ボーナス適用{len(bonuses)}銘柄")

    result = {
        "us_filings":  us_filings,
        "jp_holdings": jp_holdings,
        "bonuses":     bonuses,
        "alert_items": sorted(alert_items, key=lambda x: abs(float(
            re.search(r'bonus=([+-]?\d+)', x).group(1)) if re.search(r'bonus=([+-]?\d+)', x) else 0
        ), reverse=True),
        "total_us":    total_us,
        "total_jp":    total_jp,
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_cache(cfg, result)
    return result
