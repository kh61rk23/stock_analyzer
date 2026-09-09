# ==============================================================
# market_breadth.py  v1.0  (3-7 / B-4)
# 市場幅指標（マーケットブレス）自動計算
# ==============================================================
# 【役割】
#   「指数は上昇しているが、実際に上がっているのは一部の銘柄だけ」
#   という天井サインを数値で検出する。
#
# 【計算指標】
#   A. 騰落線（Advance-Decline Line）
#      今日の値上がり銘柄数 - 値下がり銘柄数
#      累積値が指数と逆方向 = ダイバージェンス（天井/底シグナル）
#
#   B. 新高値/新安値比率（52週）
#      新高値銘柄数 / (新高値+新安値) × 100
#      < 30% = 上昇の質が悪い
#
#   C. 200日MA上の銘柄比率（マーケット健全度）
#      > 70% = 健全な強気相場
#      < 30% = 弱気相場・底値圏の可能性
#
#   D. RSI > 50 の銘柄比率（モメンタム健全度）
#
# 【市場幅スコア（0〜100）】
#   以上4指標を加重合算:
#     AD比率(30%) + 新高値比率(25%) + MA200比率(30%) + RSI50比率(15%)
#
# 【composite_score への影響】
#   市場幅スコア >= 70: 全JP業種に +3pt（健全な強気相場）
#   市場幅スコア <= 30: 全JP業種に -5pt（広範な弱気シグナル）
#   ダイバージェンス検出時: 上位業種に -3pt（天井シグナル）
#
# 【対象銘柄】
#   JP: JP_SECTOR_TICKERS の全銘柄（約160銘柄 / 東証プライム代表）
#   US: S&P500 代表 100銘柄（sp500.json から先頭100）
#   PF: portfolio_holdings.csv の保有銘柄（重複排除）
#
# 【yfinance バッチ取得で高速化】
#   yf.download(tickers, period="1y", group_by="ticker") で
#   一括取得 → 個別ループより大幅に高速
#
# 【処理時間目安】
#   JP 160銘柄 バッチ取得: ~30秒
#   US 100銘柄 バッチ取得: ~20秒
#   合計: ~60秒/週（24時間キャッシュあり / 2回目以降+1秒）
#
# 【依存】yfinance / config.py
# 【呼出】from market_breadth import run_market_breadth
# ==============================================================

from __future__ import annotations

from config_sync import sync_module_constants

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
# 市場幅スコアのcomposite_score補正
SCORE_HEALTHY_THRESH  = 70.0   # >= 70: 健全強気 → +3pt
SCORE_WEAK_THRESH     = 30.0   # <= 30: 広範弱気 → -5pt
SCORE_HEALTHY_BONUS   =  3.0
SCORE_WEAK_PENALTY    = -5.0
DIVERGENCE_PENALTY    = -3.0   # ダイバージェンス検出時（上位業種のみ）

# 指標の重み（合計1.0）
WEIGHT_AD_RATIO     = 0.30
WEIGHT_NH_RATIO     = 0.25
WEIGHT_MA200_RATIO  = 0.30
WEIGHT_RSI50_RATIO  = 0.15

# MA / RSI 期間
MA200_PERIOD   = 200
RSI_PERIOD     = 14
NH_NL_PERIOD   = 252   # 52週 = 252営業日

# データ取得期間（MA200計算に充分な期間）
HISTORY_PERIOD = "13mo"   # 約260営業日

CACHE_FILENAME = "market_breadth_cache.json"
CACHE_HOURS    = 24   # 日次データ / 1日キャッシュ

# US銘柄は計算量削減のため先頭N件のみ使用
US_SAMPLE_SIZE = 100

# S&P500 デフォルト（custom_universe/sp500.json が読めない場合）
SP500_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "BRK-B", "JPM",
    "JNJ", "V", "PG", "HD", "MA", "UNH", "COST", "ABBV", "MRK", "LLY",
    "CVX", "PEP", "KO", "AVGO", "TMO", "WMT", "BAC", "MCD", "DHR",
    "CSCO", "ORCL", "ACN", "ABT", "TXN", "NEE", "PM", "CRM", "ADBE",
    "INTC", "AMD", "QCOM", "T", "GE", "HON", "GS", "UPS", "IBM",
    "CAT", "BA", "MMM", "RTX", "LMT", "SPGI", "BLK", "CB", "AXP",
    "SCHW", "USB", "C", "WFC", "MS", "GILD", "AMGN", "BMY", "BIIB",
    "VRTX", "REGN", "ZTS", "SYK", "MDT", "BSX", "ELV", "CI", "HUM",
    "CVS", "MCK", "AIG", "MET", "PRU", "AFL", "PLD", "AMT", "EQIX",
    "PSA", "DLR", "O", "SPG", "VTR", "EXC", "SRE", "D", "SO", "AEP",
    "XEL", "DUK", "ED", "ES", "ETR", "WEC", "PPL", "EIX", "CMS", "NI",
]


# ==============================================================
# BLOCK 1: 銘柄リスト収集
# ==============================================================


# ── config を正本とする定数の対応表（2026-07-25 SSOT移設）──
# config.py BLOCK 59 が正本。run_* の冒頭で一度だけ取り込む。
# 値は移設時点の実装値と同一のため挙動は変わらない。
_CFG_CONST_MAP = {
    "MB_SCORE_HEALTHY_THRESH": "SCORE_HEALTHY_THRESH",
    "MB_SCORE_WEAK_THRESH": "SCORE_WEAK_THRESH",
    "MB_SCORE_HEALTHY_BONUS": "SCORE_HEALTHY_BONUS",
    "MB_SCORE_WEAK_PENALTY": "SCORE_WEAK_PENALTY",
    "MB_DIVERGENCE_PENALTY": "DIVERGENCE_PENALTY",
    "MB_WEIGHT_AD_RATIO": "WEIGHT_AD_RATIO",
    "MB_WEIGHT_NH_RATIO": "WEIGHT_NH_RATIO",
    "MB_WEIGHT_MA200_RATIO": "WEIGHT_MA200_RATIO",
    "MB_WEIGHT_RSI50_RATIO": "WEIGHT_RSI50_RATIO",
    "MB_MA200_PERIOD": "MA200_PERIOD",
    "MB_RSI_PERIOD": "RSI_PERIOD",
    "MB_NH_NL_PERIOD": "NH_NL_PERIOD",
    "MB_CACHE_HOURS": "CACHE_HOURS",
    'B4_MARKET_BREADTH_HISTORY_PERIOD': 'HISTORY_PERIOD',
    'B6_MARKET_BREADTH_US_SAMPLE_SIZE': 'US_SAMPLE_SIZE',
}

def _collect_jp_tickers(cfg) -> list:
    """
    JP_SECTOR_TICKERS（stock_screener.py）と
    SECTOR_REPRESENTATIVE_TICKERS（config.py）から
    全 JP ティッカーを収集して重複排除する。
    """
    tickers = set()

    # config.py の代表銘柄
    rep = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
    for ts in rep.values():
        tickers.update(ts)

    # stock_screener.py の全銘柄（動的ロード）
    try:
        import importlib, sys
        sys.path.insert(0, str(Path(__file__).parent))
        ss = importlib.import_module("stock_screener")
        for ts in ss.JP_SECTOR_TICKERS.values():
            tickers.update(ts)
    except Exception:
        pass

    return sorted(tickers)


def _collect_us_tickers(cfg) -> list:
    """
    custom_universe/sp500.json から S&P500 銘柄を取得する。
    読み込み失敗時は SP500_FALLBACK を使用。
    """
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
        / "data" / "custom_universe" / "sp500.json",
        Path(__file__).parent / "data" / "custom_universe" / "sp500.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                tickers = data.get("tickers", [])
                if tickers:
                    return tickers[:US_SAMPLE_SIZE]
            except Exception:
                pass
    return SP500_FALLBACK[:US_SAMPLE_SIZE]


def _collect_portfolio_tickers(cfg) -> list:
    """portfolio_holdings.csv の保有銘柄ティッカーを返す。"""
    import csv
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "portfolio_holdings.csv",
        Path(__file__).parent / "portfolio_holdings.csv",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            result = []
            with open(p, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    t = row.get("ticker", "").strip()
                    if t:
                        result.append(t)
            return result
        except Exception:
            pass
    return []


# ==============================================================
# BLOCK 2: yfinance バッチ取得（高速化のコア）
# ==============================================================

def _batch_fetch_prices(tickers: list, period: str = HISTORY_PERIOD) -> dict:
    """
    yf.download() で複数銘柄の価格を一括取得する。

    個別 yf.Ticker(t).history() × N回 より大幅に高速。

    Returns:
        {ticker: {"close": [float,...], "high": [float,...], "low": [float,...]}}
        取得失敗の銘柄は含まれない。
    """
    try:
        import yf_cache as yf
    except ImportError:
        return {}

    if not tickers:
        return {}

    # 50銘柄ずつバッチ処理（yfinanceの安定性のため）
    BATCH_SIZE = 50
    all_data = {}

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i+BATCH_SIZE]
        batch_str = " ".join(batch)
        try:
            df = yf.download(
                tickers    = batch_str,
                period     = period,
                auto_adjust= True,
                progress   = False,
                threads    = True,    # 並列取得で高速化
                group_by   = "ticker",
            )
            if df is None or df.empty:
                continue

            # 1銘柄の場合 MultiIndex にならないので別処理
            if len(batch) == 1:
                t = batch[0]
                if "Close" in df.columns:
                    closes = df["Close"].dropna().tolist()
                    highs  = df["High"].dropna().tolist()
                    lows   = df["Low"].dropna().tolist()
                    if len(closes) >= 20:
                        all_data[t] = {
                            "close": closes,
                            "high":  highs,
                            "low":   lows,
                        }
            else:
                # group_by="ticker" の MultiIndex は (Level0=Ticker, Level1=OHLCV)。
                # df["Close"][t] は level0 に "Close" が無く KeyError になり、
                # 下の except で全銘柄が黙って捨てられていた
                # （2026-09-08 修正。anomaly_detector と同型）。
                for t in batch:
                    try:
                        sub    = df[t]
                        closes = sub["Close"].dropna().tolist()
                        highs  = sub["High"].dropna().tolist()
                        lows   = sub["Low"].dropna().tolist()
                        if len(closes) >= 20:
                            all_data[t] = {
                                "close": closes,
                                "high":  highs,
                                "low":   lows,
                            }
                    except (KeyError, TypeError):
                        continue

        except Exception as e:
            print(f"    ⚠️  バッチ取得失敗（{i//BATCH_SIZE+1}件目）: {str(e)[:60]}")
            continue

        time.sleep(0.5)   # バッチ間のレート制限対策

    return all_data


# ==============================================================
# BLOCK 3: 各指標の計算
# ==============================================================

def _calc_rsi(closes: list, period: int = RSI_PERIOD) -> Optional[float]:
    """直近 RSI を計算する（Wilder法）。"""
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    recent  = changes[-period:]
    gains   = [max(0, c) for c in recent]
    losses  = [max(0, -c) for c in recent]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss < 1e-9:
        return 100.0
    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 1)


def _is_above_ma200(closes: list) -> Optional[bool]:
    """直近終値が200日MA上かどうかを返す。"""
    if len(closes) < MA200_PERIOD:
        return None
    ma200 = sum(closes[-MA200_PERIOD:]) / MA200_PERIOD
    return closes[-1] > ma200


def _is_new_high_52w(closes: list, highs: list) -> Optional[bool]:
    """直近高値が52週高値かどうか（新高値銘柄の判定）。"""
    if len(highs) < min(NH_NL_PERIOD, 20):
        return None
    period_highs = highs[-min(NH_NL_PERIOD, len(highs)):]
    return highs[-1] >= max(period_highs[:-1]) if len(period_highs) > 1 else None


def _is_new_low_52w(closes: list, lows: list) -> Optional[bool]:
    """直近安値が52週安値かどうか（新安値銘柄の判定）。"""
    if len(lows) < min(NH_NL_PERIOD, 20):
        return None
    period_lows = lows[-min(NH_NL_PERIOD, len(lows)):]
    return lows[-1] <= min(period_lows[:-1]) if len(period_lows) > 1 else None


def calc_breadth_metrics(price_data: dict) -> dict:
    """
    全銘柄の価格データから市場幅指標を計算する。

    Returns:
        {
          "total":          int,    # 有効銘柄数
          "advance":        int,    # 値上がり銘柄数（前日比プラス）
          "decline":        int,    # 値下がり銘柄数
          "unchanged":      int,    # 変わらず
          "ad_ratio":       float,  # 値上がり / (値上がり + 値下がり) × 100
          "new_high":       int,    # 52週新高値銘柄数
          "new_low":        int,    # 52週新安値銘柄数
          "nh_ratio":       float,  # 新高値 / (新高値 + 新安値) × 100
          "above_ma200":    int,    # 200日MA上の銘柄数
          "ma200_ratio":    float,  # MA上銘柄 / 全銘柄 × 100
          "rsi_above_50":   int,    # RSI > 50 の銘柄数
          "rsi50_ratio":    float,
          "ad_cumulative":  int,    # 騰落線の今日の加算値（advance - decline）
        }
    """
    total = 0
    advance = decline = unchanged = 0
    new_high = new_low = 0
    above_ma200 = rsi_above_50 = 0
    nh_denominator = 0

    for ticker, data in price_data.items():
        closes = data.get("close", [])
        highs  = data.get("high", [])
        lows   = data.get("low", [])

        if len(closes) < 2:
            continue
        total += 1

        # 騰落判定（前日比）
        ret = closes[-1] - closes[-2]
        if ret > 0:
            advance += 1
        elif ret < 0:
            decline += 1
        else:
            unchanged += 1

        # 200日MA
        above = _is_above_ma200(closes)
        if above is True:
            above_ma200 += 1

        # RSI
        rsi = _calc_rsi(closes)
        if rsi is not None and rsi > 50:
            rsi_above_50 += 1

        # 52週新高値/新安値
        nh = _is_new_high_52w(closes, highs)
        nl = _is_new_low_52w(closes, lows)
        if nh is not None or nl is not None:
            nh_denominator += 1
            if nh:
                new_high += 1
            if nl:
                new_low += 1

    if total == 0:
        return {"total": 0, "error": "データなし"}

    ad_sum = advance + decline
    nh_sum = new_high + new_low

    return {
        "total":         total,
        "advance":       advance,
        "decline":       decline,
        "unchanged":     unchanged,
        "ad_ratio":      round(advance / ad_sum * 100, 1) if ad_sum > 0 else 50.0,
        "new_high":      new_high,
        "new_low":       new_low,
        "nh_ratio":      round(new_high / nh_sum * 100, 1) if nh_sum > 0 else 50.0,
        "above_ma200":   above_ma200,
        "ma200_ratio":   round(above_ma200 / total * 100, 1),
        "rsi_above_50":  rsi_above_50,
        "rsi50_ratio":   round(rsi_above_50 / total * 100, 1),
        "ad_cumulative": advance - decline,
    }


# ==============================================================
# BLOCK 4: 市場幅スコア合成・ダイバージェンス検出
# ==============================================================

def calc_breadth_score(metrics: dict) -> float:
    """
    4指標の加重平均から市場幅スコア（0〜100）を算出する。

    各指標を 0〜100 に正規化してから加重合算する。
    """
    if metrics.get("error") or metrics.get("total", 0) == 0:
        return 50.0   # データなし → 中立

    # AD比率: 0〜100%をそのまま使用
    ad_score   = metrics.get("ad_ratio", 50.0)

    # 新高値比率: 0〜100%をそのまま使用
    nh_score   = metrics.get("nh_ratio", 50.0)

    # MA200比率: 0〜100%をそのまま使用
    ma_score   = metrics.get("ma200_ratio", 50.0)

    # RSI50比率: 0〜100%をそのまま使用
    rsi_score  = metrics.get("rsi50_ratio", 50.0)

    score = (
        ad_score  * WEIGHT_AD_RATIO
        + nh_score  * WEIGHT_NH_RATIO
        + ma_score  * WEIGHT_MA200_RATIO
        + rsi_score * WEIGHT_RSI50_RATIO
    )
    return round(score, 1)


def detect_divergence(
    jp_metrics:     dict,
    nikkei_ret_pct: Optional[float],
) -> dict:
    """
    騰落線ダイバージェンスを検出する。

    ダイバージェンスの条件:
      指数（日経）が前日比プラス  かつ  AD線（advance - decline）がマイナス
      → 指数上昇の裏で下落銘柄が多い = 上昇の質が悪い = 天井サイン

    Returns:
        {"detected": bool, "description": str, "severity": "high"/"medium"/"none"}
    """
    if jp_metrics.get("error"):
        return {"detected": False, "description": "", "severity": "none"}

    ad_net   = jp_metrics.get("ad_cumulative", 0)
    nh_ratio = jp_metrics.get("nh_ratio", 50.0)
    ma_ratio = jp_metrics.get("ma200_ratio", 50.0)

    # ケース1: 指数上昇 × AD線下降（最も強いシグナル）
    if nikkei_ret_pct is not None and nikkei_ret_pct > 0.5 and ad_net < -20:
        return {
            "detected":    True,
            "description": f"日経+{nikkei_ret_pct:.1f}%上昇中だが下落銘柄が多数（AD={ad_net:+d}）",
            "severity":    "high",
        }

    # ケース2: 新高値が少ない（30%未満）× MA200上銘柄が減少（50%未満）
    if nh_ratio < 30 and ma_ratio < 50:
        return {
            "detected":    True,
            "description": f"新高値銘柄{nh_ratio:.0f}% / MA200上銘柄{ma_ratio:.0f}%（上昇の質が低下）",
            "severity":    "medium",
        }

    return {"detected": False, "description": "", "severity": "none"}


# ==============================================================
# BLOCK 5: composite_score 補正量計算
# ==============================================================

def calc_breadth_adjustments(
    breadth_score:  float,
    divergence:     dict,
    sector_results: dict,
) -> dict:
    """
    市場幅スコアから業種 composite_score への補正量を計算する。

    Args:
        breadth_score:  calc_breadth_score() の出力（0〜100）
        divergence:     detect_divergence() の出力
        sector_results: results["sectors"]

    Returns:
        {sector_code: delta_float}
    """
    adjustments = {}

    # 全業種に一律適用
    base_adj = 0.0
    if breadth_score >= SCORE_HEALTHY_THRESH:
        base_adj = SCORE_HEALTHY_BONUS
    elif breadth_score <= SCORE_WEAK_THRESH:
        base_adj = SCORE_WEAK_PENALTY

    for code in sector_results:
        adjustments[code] = base_adj

    # ダイバージェンス: 上位業種（高スコア）への追加ペナルティ
    if divergence.get("detected") and divergence.get("severity") == "high":
        sorted_sectors = sorted(
            sector_results.items(),
            key=lambda x: x[1].get("composite_score", 0) if isinstance(x[1], dict) else 0,
            reverse=True,
        )
        # 上位5業種に追加ペナルティ（「伸び過ぎ」の業種が最も危険）
        for code, _ in sorted_sectors[:5]:
            adjustments[code] = adjustments.get(code, 0) + DIVERGENCE_PENALTY

    return {k: round(v, 1) for k, v in adjustments.items() if v != 0}


# ==============================================================
# BLOCK 6: テキスト整形
# ==============================================================

def _breadth_label(score: float) -> str:
    if score >= 70:
        return "🟢 健全（強気相場）"
    elif score >= 55:
        return "🟡 やや健全"
    elif score >= 45:
        return "🟡 中立"
    elif score >= 30:
        return "🟠 やや悪化"
    else:
        return "🔴 市場幅悪化（要注意）"


def format_breadth_report(result: dict) -> str:
    """週次レポート向けテキストを生成する。"""
    jp  = result.get("jp", {})
    us  = result.get("us", {})
    pf  = result.get("portfolio", {})
    score = result.get("breadth_score_jp", 50.0)
    div   = result.get("divergence", {})

    lines = [
        "## 📊 市場幅指標（マーケットブレス）",
        "",
        f"**総合スコア（JP）**: {score:.0f}/100 — {_breadth_label(score)}",
        "",
        "### 🇯🇵 東証プライム代表銘柄",
    ]

    if jp.get("total", 0) > 0:
        lines += [
            f"  値上がり/値下がり: {jp['advance']}/{jp['decline']}銘柄"
            f"（AD比率 {jp['ad_ratio']:.0f}%）",
            f"  52週新高値/新安値: {jp['new_high']}/{jp['new_low']}銘柄"
            f"（新高値比 {jp['nh_ratio']:.0f}%）",
            f"  200日MA上: {jp['above_ma200']}/{jp['total']}銘柄"
            f"（{jp['ma200_ratio']:.0f}%）",
            f"  RSI>50: {jp['rsi_above_50']}/{jp['total']}銘柄"
            f"（{jp['rsi50_ratio']:.0f}%）",
        ]

    if us.get("total", 0) > 0:
        us_score = result.get("breadth_score_us", 50.0)
        lines += [
            "",
            f"### 🇺🇸 S&P500（代表{us['total']}銘柄）",
            f"  AD比率 {us['ad_ratio']:.0f}% | "
            f"MA200上 {us['ma200_ratio']:.0f}% | "
            f"新高値比 {us['nh_ratio']:.0f}% | "
            f"スコア {us_score:.0f}/100",
        ]

    if pf.get("total", 0) > 0:
        pf_score = result.get("breadth_score_pf", 50.0)
        lines += [
            "",
            f"### 💼 保有ポートフォリオ（{pf['total']}銘柄）",
            f"  AD比率 {pf['ad_ratio']:.0f}% | "
            f"MA200上 {pf['ma200_ratio']:.0f}% | "
            f"スコア {pf_score:.0f}/100",
        ]

    if div.get("detected"):
        lines += [
            "",
            f"### ⚠️ ダイバージェンス検出（{div['severity'].upper()}）",
            f"  {div['description']}",
        ]

    adj = result.get("macro_adjustment", {})
    if adj:
        lines += ["", f"**composite_score 補正**: {len(adj)}業種に適用（±{abs(list(adj.values())[0])}pt）"]

    return "\n".join(lines)


# ==============================================================
# BLOCK 7: キャッシュ管理
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
# BLOCK 8: メインエントリポイント
# ==============================================================

def run_market_breadth(cfg, sector_results: Optional[dict] = None,
                       macro_snapshot: Optional[dict] = None) -> dict:
    """
    市場幅指標を計算して composite_score 補正量を返す。

    stock_analyzer_main.py から以下のように呼び出す:
        _mb = _load_local_module("market_breadth", ...)
        mb_result = _mb.run_market_breadth(
            cfg, results.get("sectors", {}), results.get("macro_snapshot", {})
        )
        results["market_breadth"] = mb_result
        # 補正を sectors に適用
        mb_adj = mb_result.get("macro_adjustment", {})
        for code, delta in mb_adj.items():
            if code in results["sectors"] and delta != 0:
                results["sectors"][code]["composite_score"] = round(
                    max(-30, min(100,
                        results["sectors"][code].get("composite_score", 0) + delta
                    )), 1
                )
                results["sectors"][code]["breadth_adjustment"] = delta

    Returns:
        {
          "jp":               dict,   # JP市場幅指標
          "us":               dict,   # US市場幅指標
          "portfolio":        dict,   # ポートフォリオ市場幅指標
          "breadth_score_jp": float,  # JP市場幅スコア(0〜100)
          "breadth_score_us": float,
          "breadth_score_pf": float,
          "divergence":       dict,   # ダイバージェンス検出結果
          "macro_adjustment": dict,   # {sector_code: delta}
          "summary_text":     str,
          "timestamp":        str,
        }
    """
    # 2026-07-25: 閾値・重みを config.py BLOCK 59 から取り込む
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)

    print("\n  📊 市場幅指標（マーケットブレス）計算開始...")

    cached = _load_cache(cfg)
    if cached:
        print(f"    ✅ キャッシュ使用（{cached.get('cached_at','')[:10]}）")
        return cached

    # ── 銘柄リスト収集 ──────────────────────────────────────
    jp_tickers = _collect_jp_tickers(cfg)
    us_tickers = _collect_us_tickers(cfg)
    pf_tickers = _collect_portfolio_tickers(cfg)

    print(f"    対象: JP={len(jp_tickers)} US={len(us_tickers)} PF={len(pf_tickers)}銘柄")

    # ── バッチ価格取得 ───────────────────────────────────────
    print("    JP銘柄 一括取得中（約30秒）...")
    jp_prices = _batch_fetch_prices(jp_tickers)
    print(f"    JP取得完了: {len(jp_prices)}/{len(jp_tickers)}銘柄")

    print("    US銘柄 一括取得中（約20秒）...")
    us_prices = _batch_fetch_prices(us_tickers)
    print(f"    US取得完了: {len(us_prices)}/{len(us_tickers)}銘柄")

    # PF銘柄は jp/us と重複している可能性があるため差分のみ取得
    pf_only = [t for t in pf_tickers if t not in jp_prices and t not in us_prices]
    if pf_only:
        pf_extra = _batch_fetch_prices(pf_only)
    else:
        pf_extra = {}

    # PF用: jp + us + pf_extra の和集合
    pf_prices = {}
    for t in pf_tickers:
        if t in jp_prices:
            pf_prices[t] = jp_prices[t]
        elif t in us_prices:
            pf_prices[t] = us_prices[t]
        elif t in pf_extra:
            pf_prices[t] = pf_extra[t]

    # ── 各指標の計算 ─────────────────────────────────────────
    jp_metrics = calc_breadth_metrics(jp_prices)
    us_metrics = calc_breadth_metrics(us_prices)
    pf_metrics = calc_breadth_metrics(pf_prices) if pf_prices else {"total": 0}

    score_jp = calc_breadth_score(jp_metrics)
    score_us = calc_breadth_score(us_metrics)
    score_pf = calc_breadth_score(pf_metrics) if pf_metrics.get("total", 0) > 0 else 50.0

    print(f"    市場幅スコア: JP={score_jp:.0f} US={score_us:.0f} "
          + (f"PF={score_pf:.0f}" if pf_metrics.get("total", 0) > 0 else ""))

    # ── ダイバージェンス検出 ──────────────────────────────────
    nikkei_ret = None
    if macro_snapshot:
        nikkei_prev = macro_snapshot.get("nikkei_prev")
        nikkei_curr = macro_snapshot.get("nikkei")
        if nikkei_prev and nikkei_curr:
            nikkei_ret = (nikkei_curr - nikkei_prev) / nikkei_prev * 100

    divergence = detect_divergence(jp_metrics, nikkei_ret)
    if divergence["detected"]:
        print(f"    ⚠️ ダイバージェンス: {divergence['description']}")

    # ── composite_score 補正量計算 ────────────────────────────
    macro_adj = {}
    if sector_results:
        macro_adj = calc_breadth_adjustments(score_jp, divergence, sector_results)
        if macro_adj:
            adj_val = list(macro_adj.values())[0]
            sign = "+" if adj_val >= 0 else ""
            print(f"    📊 市場幅補正: {len(macro_adj)}業種 × {sign}{adj_val:.0f}pt")

    result = {
        "jp":               jp_metrics,
        "us":               us_metrics,
        "portfolio":        pf_metrics,
        "breadth_score_jp": score_jp,
        "breadth_score_us": score_us,
        "breadth_score_pf": score_pf,
        "divergence":       divergence,
        "macro_adjustment": macro_adj,
        "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["summary_text"] = format_breadth_report(result)

    _save_cache(cfg, result)
    print(f"    ✅ 市場幅指標完了: JP {_breadth_label(score_jp)}")
    return result
