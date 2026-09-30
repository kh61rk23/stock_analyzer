# ==============================================================
# market_data.py  v1.0
# ==============================================================
# 【概要】
#   L1（取得）/ L2（正規化）層。yfinance `.info` への唯一の窓口。
#   `fetch_quote(ticker, market)` が `.info` を **1回だけ** 叩き、
#   市場非依存の正規化済み辞書を返す。
#
#   要件定義書_JP-US統合_20260726.md
#     §3-1（4層モデル）/ §3-2（新規モジュールの責務）/ §3-3(a)（返り値契約）
#   HANDOFF_JP-US統合_20260727.md §4 Batch 6（U-5）
#
# 【この層が解消する問題】
#   F-4: `market_cap_b` が JP=億円 / US=十億USD と **同名で単位が違う**。
#        → 本モジュールは `market_cap_b` を **返さない**。現地通貨の生値
#          `market_cap_raw` のみを持つ（単位の是正は消費側の責務）。
#   F-1: JP 側に `recommendationMean` 等が無く revision_score が恒常0だった。
#        → `rec_mean` / `rec_key` / `target_price` / `analyst_upside` /
#          `beta` / `week52_change` / `accrual_ratio` を **両市場で必ず取得**する。
#   品質差: US だけが 429 リトライを持っていた。
#        → リトライを本モジュールへ集約し、JP にも自動的に適用される。
#
# 【この層の責務でないもの】
#   - スコア計算・フィルタ判定（各スクリーナーに残す）
#   - 派生単位キーの生成（`market_cap_b` 等。消費側が自分で行う）
#
# 【設計上の約束】
#   - yfinance は **関数内 import**（import 時にネットワークへ出ない）
#   - 価格が取れない銘柄は None を返す
#   - 生の `.info` は `_raw_info` として同梱する（浅いコピー）
# ==============================================================

from datetime import datetime
from typing import Any, Optional
import time

__all__ = ["fetch_quote", "DEFAULT_MAX_RETRIES", "SOURCE", "SUPPORTED_MARKETS"]


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

SOURCE = "yfinance"
SUPPORTED_MARKETS = ("JP", "US")

# 既存 us_stock_screener._fetch_one の既定値をそのまま持ち込む
DEFAULT_MAX_RETRIES      = 3
RATE_LIMIT_WAIT_BASE_SEC = 3    # (2 ** attempt) * 3 → 3s / 6s / 12s
RETRY_WAIT_SEC           = 2    # 429 以外の一般エラー時の固定待ち


# ==============================================================
# BLOCK 1: 値の正規化ヘルパー
# ==============================================================

def _clean(v: Any) -> Optional[float]:
    """
    数値へ変換する。変換できない値と NaN は None にする。

    正本は stock_screener._fetch_ticker_data の `_clean()`（NaN → None）。
    us_stock_screener._fetch_one の `_get()` は NaN をそのまま通していたが、
    NaN は下流の比較演算を黙って False にするため、こちらへ揃える。
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f   # NaN 判定


def _clean_int(v: Any) -> Optional[int]:
    """整数へ変換する。変換できない値（NaN 含む）は None にする。"""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _is_rate_limited(err: BaseException) -> bool:
    """yfinance の 429（レート制限）由来の例外かどうかを判定する。"""
    s = str(err).lower()
    return ("429" in s) or ("too many" in s)


# ==============================================================
# BLOCK 2: 正規化（.info → L2 契約辞書）
# ==============================================================

def _normalize(ticker: str, market: str, info: dict,
               price_raw: Any) -> dict:
    """
    yfinance `.info` を §3-3(a) の返り値契約へ写像する。

    ここには市場分岐を **置かない**（市場差は取得側＝価格判定のみ）。
    """
    # ── 名称 ────────────────────────────────────────────────
    # JP 既存実装は longName 優先、US 既存実装は shortName 優先だったため
    # 両方を持たせる（どちらを使うかは消費側の従来挙動に委ねる）。
    long_name  = info.get("longName")
    short_name = info.get("shortName")

    # ── 価格 ────────────────────────────────────────────────
    # `or 0` は JP 既存実装の踏襲（regularMarketPrice=0.0 の銘柄で
    # current_price を None ではなく 0.0 にする）。
    current_price = _clean(price_raw or 0)

    # ── 在庫回転率 = 売上高 / 棚卸資産 ─────────────────────
    # 生値（丸めない）。JP 既存実装の round(x, 2) は消費側で行う。
    revenue_raw   = info.get("totalRevenue")
    inventory_raw = info.get("inventory")
    inventory_turnover = None
    if revenue_raw and inventory_raw:
        try:
            inv_f = float(inventory_raw)
            rev_f = float(revenue_raw)
            if inv_f > 0:
                inventory_turnover = rev_f / inv_f
        except (TypeError, ValueError):
            pass

    # ── 出来高 ──────────────────────────────────────────────
    # int() が失敗する値は例外にする（JP 既存実装と同じ＝銘柄ごと None 扱い）。
    avg_volume = int(info.get("averageVolume")
                     or info.get("averageVolume10days")
                     or 0)

    # ── アナリスト ─────────────────────────────────────────
    target_price = _clean(info.get("targetMeanPrice"))
    analyst_upside = None
    # 2026-07-27 設計判断（JP/US 揃え済み）: target_price は truthy で判定する。
    # targetMeanPrice=0 は「目標株価なし」であり、乖離率 -100% を算出しない。
    if current_price and current_price > 0 and target_price:
        analyst_upside = (target_price - current_price) / current_price

    # ── アクルーアル比率 =（純利益 - 営業CF）/ 総資産 ──────
    net_income   = _clean(info.get("netIncomeToCommon"))
    operating_cf = _clean(info.get("operatingCashflow"))
    total_assets = _clean(info.get("totalAssets"))
    accrual_ratio = None
    if (net_income is not None and operating_cf is not None
            and total_assets and total_assets > 0):
        accrual_ratio = (net_income - operating_cf) / total_assets

    return {
        "ticker":  ticker,
        "market":  market,
        "name":       long_name or short_name or ticker,   # JP 既存の優先順
        "short_name": short_name or long_name or ticker,   # US 既存の優先順

        # ── 規模（生値のみ。派生単位キーは持たない ← F-4 の根治）──
        "market_cap_raw": _clean(info.get("marketCap")),
        "current_price":  current_price,
        "avg_volume":     avg_volume,

        # ── バリュエーション（無次元） ──────────────────────
        "per":       _clean(info.get("trailingPE")),
        "pbr":       _clean(info.get("priceToBook")),
        # 2026-09-29: yfinance の dividendYield は %値（2.72 = 2.72%）。ここだけ比率化が
        # 漏れており、スクリーナー経由の配当利回りが100倍（2.72 → 272%表示・4%以上の
        # 高配当判定から漏れる）になっていた。finance_logic と同じ規則で比率にそろえる
        "div_yield": _div_yield_ratio(info),

        # ── 収益性（比率） ──────────────────────────────────
        "roe":           _clean(info.get("returnOnEquity")),
        "roa":           _clean(info.get("returnOnAssets")),
        "op_margin":     _clean(info.get("operatingMargins")),
        "gross_margin":  _clean(info.get("grossMargins")),
        "net_margin":    _clean(info.get("profitMargins")),
        "ebitda_margin": _clean(info.get("ebitdaMargins")),

        # ── 成長（比率） ────────────────────────────────────
        "revenue_growth":   _clean(info.get("revenueGrowth")),
        "earnings_growth":  _clean(info.get("earningsGrowth")),
        "earnings_qgrowth": _clean(info.get("earningsQuarterlyGrowth")),

        # ── 財務（生値） ────────────────────────────────────
        "total_cash":          _clean(info.get("totalCash")),
        "total_debt":          _clean(info.get("totalDebt")),
        "debt_to_equity":      _clean(info.get("debtToEquity")),
        "free_cashflow":       _clean(info.get("freeCashflow")),
        "operating_cashflow":  operating_cf,
        "net_income":          net_income,
        "total_assets":        total_assets,
        "total_revenue":       _clean(revenue_raw),
        "stockholders_equity": _clean(info.get("stockholdersEquity")),
        "book_value":          _clean(info.get("bookValue")),
        "payout_ratio":        _clean(info.get("payoutRatio")),
        "inventory_turnover":  inventory_turnover,

        # ── アナリスト（★ F-1 の根治: 両市場で必ず取得する）──
        "rec_mean":       _clean(info.get("recommendationMean")),
        "rec_key":        info.get("recommendationKey") or "",   # str のまま
        "analyst_count":  _clean_int(info.get("numberOfAnalystOpinions")),
        "target_price":   target_price,
        "analyst_upside": analyst_upside,

        # ── リスク・モメンタム ──────────────────────────────
        "beta":          _clean(info.get("beta")),
        "week52_change": _clean(info.get("52WeekChange")),

        # ── 導出 ────────────────────────────────────────────
        "accrual_ratio": accrual_ratio,

        # ── 分類（yfinance 由来の生ラベル） ────────────────
        "sector":   info.get("sector", ""),
        "industry": info.get("industry", ""),

        # ── メタ ────────────────────────────────────────────
        "_source":     SOURCE,
        "_fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # F-5 用。生 info をそのまま参照したい消費側（セクター解決など）向け。
        "_raw_info":   dict(info),
    }


# ==============================================================
# BLOCK 3: 公開API
# ==============================================================

def _div_yield_ratio(info: dict):
    try:
        from finance_logic import _yf_yield_ratio
        return _yf_yield_ratio(info or {})
    except Exception:
        return None


def fetch_quote(ticker: str, market: str, *,
                cfg: Any = None,
                max_retries: int = DEFAULT_MAX_RETRIES) -> Optional[dict]:
    """
    yfinance `.info` を1回だけ叩き、市場非依存の正規化済み辞書を返す。

    Args:
        ticker      : "7203.T"（JP）/ "AAPL"（US）
        market      : "JP" | "US"（大文字小文字は問わない）
        cfg         : 将来 MARKET_PROFILE（U-8）を参照するための予約引数。
                      現時点では未使用（既定挙動を変えないため）。
        max_retries : 例外時の試行回数。既定3（us_stock_screener の従来値）。

    Returns:
        §3-3(a) の契約辞書（`market_cap_b` は **含まない**）。
        価格が取れない・取得に失敗した場合は None。

    リトライ（元は us_stock_screener._fetch_one のみが持っていた）:
        429 / "too many" → (2 ** attempt) * 3 秒待って再試行（3s / 6s / 12s）
        それ以外          → 2 秒待って再試行（最終試行なら None）
    """
    mkt = (market or "").upper()
    attempts = max(1, int(max_retries))

    for attempt in range(attempts):
        try:
            import yf_cache as yf  # type: ignore

            info = yf.Ticker(ticker).info or {}
            if not info:
                return None

            price_raw = (info.get("regularMarketPrice")
                         or info.get("currentPrice")
                         or info.get("previousClose"))

            # ── 価格判定は市場ごとの既存挙動を厳密に保存する ──────
            # JP（stock_screener._fetch_ticker_data）:
            #   regularMarketPrice が None のときだけ「価格なし」。
            #   0.0 は有効値として通す（tests/test_jp_analyst_revision.py
            #   test_upside_none_when_current_price_zero が凍結している）。
            # US（us_stock_screener._fetch_one）:
            #   3キーのいずれも truthy でなければ「価格なし」。
            # 統合時にどちらかへ寄せると片方の既存挙動が変わるため、
            # ここは意図的に市場分岐を残している。
            if mkt == "JP":
                if info.get("regularMarketPrice") is None:
                    return None
            elif not price_raw:
                return None

            return _normalize(ticker, mkt, info, price_raw)

        except Exception as e:
            if _is_rate_limited(e):
                time.sleep((2 ** attempt) * RATE_LIMIT_WAIT_BASE_SEC)
            elif attempt < attempts - 1:
                time.sleep(RETRY_WAIT_SEC)
            else:
                return None

    return None
