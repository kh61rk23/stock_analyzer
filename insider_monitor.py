# ==============================================================
# insider_monitor.py  v2.0  (4-2 / A-3 / U-6)
# EDINET 大量保有報告書（日本）専用 + SEC Form 4（米国）は委譲
# インサイダー・大株主の買い動向を screener ボーナスに統合
# ==============================================================
# 【役割】
#   役員・大株主の株式取得情報を自動収集し、
#   「役員が自社株を買っている = 内部者が上昇を確信している」
#   シグナルを screener の quality_score に反映する。
#
# 【米国株の扱い（U-6 / F-3 / J-02決定=a: 一本化）】
#   本ファイルは JP 大量保有報告専用。米国株は Form 4 XML を正式パースする
#   `us_insider_form4_scraper.get_insider_summary()` へ委譲する。
#   yfinance insider_transactions の簡易パースを本ファイルに再び持たないこと
#   （同一データソースの二重実装になり、簡易版では US が0件になる）。
#
# 【データソース】
#   米国株: us_insider_form4_scraper.get_insider_summary() に委譲
#     （SEC Form 4 XML 正式パース。CIK解決・レート制限・キャッシュは
#      sec_client.py 経由。insider_form4_cache.json・TTL1日を
#      us_stock_screener.py の calc_insider_bonus 呼び出し箇所 の直接呼び出しと共有する）
#
#   日本株: EDINET 大量保有報告書
#     edinet_client.py の get_analysis() 経由で取得済みのデータを参照
#     → 大量保有報告（5%超の株式取得）を検出
#
# 【screener ボーナス計算】
#   JP側（大量保有報告、5%以上取得）: _calc_bonus() が +4pt を算出
#   US側（Form 4）: us_insider_form4_scraper 側で -5.0〜+5.0pt を算出
#   最終 bonuses[ticker] は両者の合算値
#
# 【出力スキーマ】（キー構成は v1.0 から不変。R6-2）
#   {
#     "us_filings":   {ticker: Summary},        # 米国 Form 4（銘柄別集約サマリー）
#     "jp_holdings":  {ticker: [Holding, ...]}, # 日本 大量保有
#     "bonuses":      {ticker: float},           # screener ボーナス（JP+US合算）
#     "alert_items":  [str, ...],                # アラート文字列
#     "total_us":     int,
#     "total_jp":     int,
#     "timestamp":    str,
#   }
#   Summary = us_insider_form4_scraper.get_insider_summary() の返り値そのもの
#     {ticker, insider_bonus, buy_count, sell_count, buyer_roles,
#      net_buy_value, source, summary, fetched_at}
#     ※ Filing のリストではなく銘柄別サマリー（R6-2）。
#       format_insider_alert() / get_insider_bonus() はどちらも
#       us_filings の値を直接読まない。
#   Holding = {ticker, holder_name, ownership_pct, report_date}
#
# 【依存】us_insider_form4_scraper.py（米国）/ yfinance（日本）/ config.py
# 【呼出】from insider_monitor import run_insider_monitor, get_insider_bonus
# ==============================================================

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

from config_sync import sync_module_constants
import us_insider_form4_scraper

# ── 定数 ──────────────────────────────────────────────────────
CONSECUTIVE_THRESH = 2      # 連続買いと見なす最低件数
CACHE_HOURS        = 24 * 7 # 週次データ = 7日キャッシュ
CACHE_FILENAME     = "insider_monitor_cache.json"
FETCH_DELAY        = 0.12   # yfinance 呼び出し間隔（JP大量保有取得用）

# screener ボーナス（JP側 _calc_bonus() が使用。US側は
# us_insider_form4_scraper 側で独自に -5.0〜+5.0pt を算出する）
BONUS_BUY_SINGLE   =  3.0
BONUS_BUY_CONSEC   =  6.0
PENALTY_SELL_SINGLE = -3.0
PENALTY_SELL_CONSEC = -6.0
BONUS_LARGE_HOLDING =  4.0

# 米国側取得件数の上限（処理時間制限。旧 US 経路から踏襲）
US_TICKER_LIMIT = 30

_CFG_CONST_MAP = {
    "IM_CONSECUTIVE_THRESH": "CONSECUTIVE_THRESH",
    "IM_BONUS_BUY_SINGLE": "BONUS_BUY_SINGLE",
    "IM_BONUS_BUY_CONSEC": "BONUS_BUY_CONSEC",
    "IM_PENALTY_SELL_SINGLE": "PENALTY_SELL_SINGLE",
    "IM_PENALTY_SELL_CONSEC": "PENALTY_SELL_CONSEC",
    "IM_BONUS_LARGE_HOLDING": "BONUS_LARGE_HOLDING",
    'B4_INSIDER_MONITOR_CACHE_HOURS': 'CACHE_HOURS',
    'B5_INSIDER_MONITOR_FETCH_DELAY': 'FETCH_DELAY',
}


# ==============================================================
# BLOCK 1: EDINET 大量保有報告書（日本株）
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
        import yf_cache as yf
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
# BLOCK 2: ボーナス算出
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
# BLOCK 3: キャッシュ管理
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
# BLOCK 4: アラートテキスト生成
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
# BLOCK 5: メインエントリポイント
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
          "us_filings":  {ticker: Summary},        # R6-2: 値の形が変わった（本体docstring参照）
          "jp_holdings": {ticker: [Holding, ...]},
          "bonuses":     {ticker: float},
          "alert_items": [str, ...],
          "total_us":    int,
          "total_jp":    int,
          "timestamp":   str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
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

    us_tickers = [t for t in tickers if not t.endswith(".T")]
    jp_tickers = [t for t in tickers if t.endswith(".T")]

    # ── 米国株: us_insider_form4_scraper へ委譲（U-6・F-3根治）───
    # Form 4 XML の正式パース・CIK解決・レート制限・キャッシュ
    # （insider_form4_cache.json・TTL1日）は us_insider_form4_scraper /
    # sec_client 側が担う。us_stock_screener.py の calc_insider_bonus 呼び出し箇所 の calc_insider_bonus
    # 直接呼び出しとキャッシュを共有するため、二重取得は発生しない（R6-4）。
    print(f"    米国株インサイダー取引取得中 ({len(us_tickers)}銘柄)...")
    us_filings: dict = {}
    for ticker in us_tickers[:US_TICKER_LIMIT]:
        try:
            summary = us_insider_form4_scraper.get_insider_summary(ticker, cfg=cfg)
        except Exception:
            summary = None
        if summary and (summary.get("buy_count", 0) or summary.get("sell_count", 0)):
            us_filings[ticker] = summary

    # ── 日本株: yfinance major_holders ───────────────────────
    print(f"    日本株大量保有取得中 ({len(jp_tickers)}銘柄)...")
    jp_holdings = _extract_jp_large_holdings(cfg, jp_tickers[:30])

    # ── ボーナス計算 ─────────────────────────────────────────
    # JP: _calc_bonus() のロジックは変更せず、us_filings=[] を渡すことで
    #     大量保有報告分（+4pt）のみを算出させる（R6-2）。
    # US: us_insider_form4_scraper が算出済みの insider_bonus（-5.0〜+5.0）
    #     をそのまま加算する。
    bonuses = {}
    alert_items = []

    for ticker in set(list(us_filings.keys()) + list(jp_holdings.keys())):
        jp_bonus = _calc_bonus([], jp_holdings.get(ticker, []))
        us_summary = us_filings.get(ticker, {})
        us_bonus = float(us_summary.get("insider_bonus", 0.0))
        bonus = round(jp_bonus + us_bonus, 1)

        if bonus != 0:
            bonuses[ticker] = bonus
            sign = "買い🟢" if bonus > 0 else "売り🔴"
            count_us = int(us_summary.get("buy_count", 0)) + int(us_summary.get("sell_count", 0))
            count_jp = len(jp_holdings.get(ticker, []))
            alert_items.append(
                f"{ticker}: {sign} bonus={bonus:+.1f}pt "
                f"(US:{count_us}件 JP:{count_jp}件)"
            )

    total_us = sum(
        int(v.get("buy_count", 0)) + int(v.get("sell_count", 0))
        for v in us_filings.values()
    )
    total_jp = sum(len(v) for v in jp_holdings.values())

    print(f"    ✅ 完了: 米国{total_us}件 / 日本大量保有{total_jp}件 / ボーナス適用{len(bonuses)}銘柄")

    result = {
        "us_filings":  us_filings,
        "jp_holdings": jp_holdings,
        "bonuses":     bonuses,
        "alert_items": sorted(alert_items, key=lambda x: abs(float(
            re.search(r'bonus=([+-]?\d+\.?\d*)', x).group(1)) if re.search(r'bonus=([+-]?\d+\.?\d*)', x) else 0
        ), reverse=True),
        "total_us":    total_us,
        "total_jp":    total_jp,
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_cache(cfg, result)
    return result
