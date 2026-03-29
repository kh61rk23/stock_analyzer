# ==============================================================
# technical_scorer.py  v1.1  (4-1 / B-1)
# RSI / MACD / Bollinger Bands / ATR テクニカルスコア算出
# ==============================================================
# v1.0 → v1.1 変更:
#   MACD crossdown/crossup 検出ロジックを修正
#   hist[-2] >= 0 → hist[-1] < 0 の条件を明示化
#   BB の touch 判定を %B ベースに変更（touch_lower: %B<=0.05, touch_upper: %B>=0.95）
# ==============================================================
# 【composite_score 重み（technical 統合後）】
#   composite_score = sector×0.35 + value×0.25 + quality×0.20 + technical×0.20
# ==============================================================

from __future__ import annotations
import json, math, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

RSI_PERIOD   = 14
MACD_FAST    = 12; MACD_SLOW = 26; MACD_SIGNAL = 9
BB_PERIOD    = 20; BB_STD    = 2.0
MA_SHORT     = 50; MA_LONG   = 200
HISTORY_PERIOD = "2y"
FETCH_DELAY  = 0.25
CACHE_FILENAME = "technical_scorer_cache.json"
CACHE_HOURS  = 24

WEIGHT_SECTOR    = 0.35
WEIGHT_VALUE     = 0.25
WEIGHT_QUALITY   = 0.20
WEIGHT_TECHNICAL = 0.20


# ==============================================================
# BLOCK 1: 純粋テクニカル指標計算
# ==============================================================

def _ema(values: list, period: int) -> list:
    if len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    result = [None] * (period - 1)
    ema = sum(values[:period]) / period
    result.append(ema)
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
        result.append(ema)
    return result

def _sma(values: list, period: int) -> list:
    result = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        result.append(sum(values[i - period + 1:i + 1]) / period)
    return result

def calc_rsi(closes: list, period: int = RSI_PERIOD) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    recent  = changes[-period:]
    avg_gain = sum(max(0, c) for c in recent) / period
    avg_loss = sum(max(0, -c) for c in recent) / period
    if avg_loss < 1e-9:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)

def calc_macd(closes: list) -> Optional[dict]:
    """
    MACD 計算。
    crossup/crossdown: ヒストグラムの符号が直近2本で反転したタイミングを検出。
      crossup  : hist[-2] <= 0 かつ hist[-1] > 0  (ゼロラインを上抜け)
      crossdown: hist[-2] >= 0 かつ hist[-1] < 0  (ゼロラインを下抜け)
    直近5本のうちに符号反転があればフラグを立てる（クロス直後を広く捕捉）。
    """
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 5:
        return None

    ema12 = _ema(closes, MACD_FAST)
    ema26 = _ema(closes, MACD_SLOW)

    macd_line = []
    for e12, e26 in zip(ema12, ema26):
        if e12 is None or e26 is None:
            macd_line.append(None)
        else:
            macd_line.append(e12 - e26)

    valid_macd  = [v for v in macd_line if v is not None]
    signal_vals = _ema(valid_macd, MACD_SIGNAL)
    # signal_vals の長さは valid_macd と同じ
    hist_vals   = []
    for m, s in zip(valid_macd, signal_vals):
        if s is not None:
            hist_vals.append(m - s)

    if len(hist_vals) < 4 or signal_vals[-1] is None:
        return None

    current_macd   = valid_macd[-1]
    current_signal = signal_vals[-1]
    current_hist   = hist_vals[-1]

    # ── クロス検出（直近5本のうちに符号反転があるか）──────────────
    # v1.1 修正: >= 0 / < 0 で明示的に判定し、ゼロ丸め誤差を排除
    crossup   = False
    crossdown = False
    window = hist_vals[-6:-1]  # 直近5本（最新を除く）
    for i in range(len(window) - 1):
        # ゼロライン上抜け: 前 <= 0 かつ 次 > 0
        if window[i] <= 0 and window[i+1] > 0:
            crossup = True
        # ゼロライン下抜け: 前 >= 0 かつ 次 < 0
        if window[i] >= 0 and window[i+1] < 0:
            crossdown = True
    # 最新本も確認
    if len(hist_vals) >= 2:
        if hist_vals[-2] <= 0 and current_hist > 0:
            crossup = True
        if hist_vals[-2] >= 0 and current_hist < 0:
            crossdown = True

    trending_up = (current_macd > 0
                   and len(hist_vals) >= 3
                   and hist_vals[-1] > hist_vals[-3])

    return {
        "macd":        round(current_macd, 4),
        "signal":      round(current_signal, 4),
        "histogram":   round(current_hist, 4),
        "crossup":     crossup,
        "crossdown":   crossdown,
        "trending_up": trending_up,
    }

def calc_bollinger(closes: list, period: int = BB_PERIOD,
                   n_std: float = BB_STD) -> Optional[dict]:
    if len(closes) < period:
        return None
    recent  = closes[-period:]
    middle  = sum(recent) / period
    variance = sum((c - middle) ** 2 for c in recent) / period
    std     = math.sqrt(variance)
    upper   = middle + n_std * std
    lower   = middle - n_std * std
    price   = closes[-1]
    bandwidth = (upper - lower) / middle * 100 if middle > 0 else 0
    if (upper - lower) > 1e-9:
        pct_b = (price - lower) / (upper - lower)
    else:
        pct_b = 0.5
    return {
        "upper":       round(upper, 2),
        "middle":      round(middle, 2),
        "lower":       round(lower, 2),
        "pct_b":       round(pct_b, 3),
        "touch_upper": pct_b >= 0.95,   # v1.1: %B ≥ 0.95
        "touch_lower": pct_b <= 0.05,   # v1.1: %B ≤ 0.05
        "bandwidth":   round(bandwidth, 2),
    }

def calc_ma_cross(closes: list) -> dict:
    if len(closes) < MA_LONG:
        return {"ma50": None, "ma200": None, "above_ma200": None,
                "golden_cross": False, "dead_cross": False}
    ma50  = sum(closes[-MA_SHORT:]) / MA_SHORT
    ma200 = sum(closes[-MA_LONG:]) / MA_LONG
    lookback = min(5, len(closes) - MA_LONG)
    golden = dead = False
    if lookback > 0:
        ma50_prev  = sum(closes[-MA_SHORT - lookback:-lookback]) / MA_SHORT
        ma200_prev = sum(closes[-MA_LONG  - lookback:-lookback]) / MA_LONG
        if ma50_prev < ma200_prev and ma50 >= ma200:
            golden = True
        elif ma50_prev > ma200_prev and ma50 <= ma200:
            dead = True
    return {
        "ma50":         round(ma50, 2),
        "ma200":        round(ma200, 2),
        "above_ma200":  closes[-1] > ma200,
        "golden_cross": golden,
        "dead_cross":   dead,
    }


# ==============================================================
# BLOCK 2: テクニカルスコア算出
# ==============================================================

def calc_technical_score(closes_d: list, closes_w: list,
                          highs_d: list, lows_d: list) -> dict:
    raw_score = 50.0
    signals   = []

    rsi_d = calc_rsi(closes_d)
    if rsi_d is not None:
        if rsi_d < 30:
            raw_score += 15; signals.append(f"RSI売られ過ぎ({rsi_d:.0f}):+15")
        elif rsi_d < 50:
            raw_score +=  5; signals.append(f"RSI底値圏({rsi_d:.0f}):+5")
        elif rsi_d > 70:
            raw_score -= 10; signals.append(f"RSI買われ過ぎ({rsi_d:.0f}):-10")

    rsi_w = calc_rsi(closes_w) if closes_w else None
    if rsi_d is not None and rsi_w is not None and rsi_w > rsi_d:
        raw_score += 5; signals.append(f"週足RSI>日足({rsi_w:.0f}>{rsi_d:.0f}):+5")

    macd = calc_macd(closes_d)
    if macd:
        if macd["crossup"]:
            raw_score += 20; signals.append("MACDクロスアップ:+20")
        elif macd["crossdown"]:
            raw_score -= 20; signals.append("MACDクロスダウン:-20")
        elif macd["trending_up"]:
            raw_score +=  8; signals.append("MACD上昇中:+8")

    bb = calc_bollinger(closes_d)
    if bb:
        if bb["touch_lower"]:
            raw_score += 12; signals.append(f"BB下限(%B={bb['pct_b']:.2f}):+12")
        elif bb["touch_upper"]:
            raw_score -= 12; signals.append(f"BB上限(%B={bb['pct_b']:.2f}):-12")

    ma_cross = calc_ma_cross(closes_d)
    if ma_cross["golden_cross"]:
        raw_score += 10; signals.append("ゴールデンクロス:+10")
    elif ma_cross["dead_cross"]:
        raw_score -= 10; signals.append("デッドクロス:-10")
    elif ma_cross.get("above_ma200") is True:
        raw_score +=  8; signals.append("200日MA上:+8")
    elif ma_cross.get("above_ma200") is False:
        raw_score -=  8; signals.append("200日MA下:-8")

    score = round(max(0, min(100, raw_score)), 1)
    if score >= 70: label = "🟢 テクニカル買いシグナル"
    elif score >= 55: label = "🟡 やや強気"
    elif score >= 45: label = "🟡 中立"
    elif score >= 30: label = "🟠 やや弱気"
    else:             label = "🔴 テクニカル売りシグナル"

    return {"score": score, "rsi_d": rsi_d, "rsi_w": rsi_w,
            "macd": macd, "bb": bb, "ma_cross": ma_cross,
            "signals": signals, "label": label}


# ==============================================================
# BLOCK 3: composite_score 再計算
# ==============================================================

def integrate_technical_score(candidate: dict, tech_result: dict) -> dict:
    """
    元重み: sector×0.40 + value×0.30 + quality×0.30
    新重み: sector×0.35 + value×0.25 + quality×0.20 + technical×0.20
    """
    result    = dict(candidate)
    sector_s  = candidate.get("sector_score",  50.0) or 50.0
    value_s   = candidate.get("value_score",   50.0) or 50.0
    quality_s = candidate.get("quality_score", 50.0) or 50.0
    tech_s    = tech_result.get("score",       50.0) or 50.0

    new_composite = round(
        sector_s   * WEIGHT_SECTOR
        + value_s   * WEIGHT_VALUE
        + quality_s * WEIGHT_QUALITY
        + tech_s    * WEIGHT_TECHNICAL, 1)

    result["technical_score"]        = tech_s
    result["technical_label"]        = tech_result.get("label", "")
    result["technical_signals"]      = tech_result.get("signals", [])
    result["rsi_daily"]              = tech_result.get("rsi_d")
    result["macd_crossup"]           = (tech_result.get("macd") or {}).get("crossup", False)
    result["above_ma200"]            = (tech_result.get("ma_cross") or {}).get("above_ma200")
    result["composite_score_pre_ta"] = candidate.get("composite_score")
    result["composite_score"]        = new_composite
    return result


# ==============================================================
# BLOCK 4: yfinance 取得
# ==============================================================

def _fetch_ohlc(ticker: str) -> Optional[dict]:
    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        hist = t.history(period=HISTORY_PERIOD, auto_adjust=True)
        if hist is None or len(hist) < 30:
            return None
        closes_d = hist["Close"].dropna().tolist()
        highs_d  = hist["High"].dropna().tolist()
        lows_d   = hist["Low"].dropna().tolist()
        closes_w = [closes_d[i] for i in range(4, len(closes_d), 5)]
        return {"closes_d": closes_d, "highs_d": highs_d,
                "lows_d": lows_d, "closes_w": closes_w}
    except Exception:
        return None


# ==============================================================
# BLOCK 5: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME

def _load_cache(cfg) -> dict:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(hours=CACHE_HOURS):
            return data.get("results", {})
    except Exception:
        pass
    return {}

def _save_cache(cfg, results: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"cached_at": datetime.now().isoformat(),
                                  "results": results}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass

def get_technical_bonus(ticker: str, tech_cache: dict) -> float:
    rec = tech_cache.get(ticker)
    if not rec:
        return 50.0
    return float(rec.get("score", 50.0))


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_technical_scorer(cfg, tickers: list) -> dict:
    print(f"\n  📈 テクニカルスコア計算中（{len(tickers)}銘柄）...")
    cached   = _load_cache(cfg)
    to_fetch = [t for t in tickers if t not in cached]
    results  = dict(cached)

    for i, ticker in enumerate(to_fetch, 1):
        ohlc = _fetch_ohlc(ticker)
        if ohlc:
            tech = calc_technical_score(
                ohlc["closes_d"], ohlc["closes_w"],
                ohlc["highs_d"],  ohlc["lows_d"])
            tech["ticker"] = ticker
            results[ticker] = tech
            if abs(tech["score"] - 50) > 15:
                print(f"      {ticker}: score={tech['score']:.0f} {tech['label']}")
        time.sleep(FETCH_DELAY)
        if i % 10 == 0:
            print(f"      ... {i}/{len(to_fetch)}完了")

    _save_cache(cfg, results)
    scored   = [r for r in results.values()
                if isinstance(r, dict) and r.get("score") is not None]
    scored.sort(key=lambda x: x["score"], reverse=True)
    top_buy  = scored[:5]
    top_sell = scored[-5:][::-1]

    buy_names  = " / ".join(f"{r.get('ticker','?')}({r['score']:.0f})" for r in top_buy[:3])
    sell_names = " / ".join(f"{r.get('ticker','?')}({r['score']:.0f})" for r in top_sell[:3])
    print(f"    ✅ テクニカル完了: 買いTOP3={buy_names} / 売りTOP3={sell_names}")
    return {"results": results, "top_buy": top_buy, "top_sell": top_sell,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
