# ==============================================================
# support_resistance.py  v1.0  (4-6 / B-2)
# サポート・レジスタンスライン自動検出
# ==============================================================
# 【役割】
#   過去の株価データから支持線・抵抗線を自動検出し、
#   現在値の位置（52週高値・安値・VWAP 比）を数値化する。
#
# 【検出ロジック】
#   scipy 不使用 — 純粋実装のピーク検出アルゴリズム:
#     局所最大: prices[i] > prices[i-1] かつ prices[i] > prices[i+1]
#     かつ前後 window 本の中で最大値（ノイズ除去）
#
# 【主要指標】
#   A. 52週高値・安値・現在値の位置スコア
#      位置スコア = (現在値 - 52週安値) / (52週高値 - 52週安値) × 100
#      → 0 = 52週安値圏 / 100 = 52週高値圏
#
#   B. ブレイクアウト判定
#      現在値 ≥ 52週高値 × 0.95 → ブレイクアウト候補（+位置スコア追加）
#      現在値 ≤ 52週安値 × 1.10 → 底値圏候補（逆張り +位置スコア）
#
#   C. VWAP（出来高加重平均価格）
#      直近20日の価格 × 出来高 の加重平均
#      現在値 > VWAP → 強気トレンド / 現在値 < VWAP → 弱気トレンド
#
#   D. 抵抗線クラスター
#      高値ピークの価格帯をクラスタリング → 主要抵抗線を識別
#
# 【position_score（0〜100）の計算】
#   ブレイクアウト候補（位置 > 95%）: +20pt
#   上昇トレンド圏（位置 60〜95%）: +8pt
#   中立圏（位置 30〜60%）: 0pt
#   底値圏（位置 < 10%）:   +12pt（逆張り買いシグナル）
#   VWAP 上: +5pt / VWAP 下: -5pt
#   主要抵抗線突破: +10pt（直近で高値を更新した場合）
#
# 【screener 統合】
#   technical_scorer.py の integrate_technical_score() 後に
#   position_score を technical_score に加算して composite_score を再計算
#   → technical_score の補完指標として位置付ける
#
# 【依存】yfinance のみ（scipy 不使用）
# 【呼出】from support_resistance import run_support_resistance, get_sr_bonus
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
LOOKBACK_52W      = 252     # 52週 = 252営業日
LOOKBACK_VWAP     = 20      # VWAP 計算期間（日）
PEAK_WINDOW       = 5       # ピーク検出の前後ウィンドウ幅
MIN_PEAK_DISTANCE = 10      # ピーク間の最小距離（ノイズ除去）
CLUSTER_RANGE_PCT = 0.015   # ピーククラスタリング: ±1.5%以内を同じ水準とみなす

# ブレイクアウト・底値判定
BREAKOUT_THRESH   = 0.95    # 52週高値の95%以上
BOTTOM_THRESH     = 1.10    # 52週安値の110%以下

# position_score への加算値
BONUS_BREAKOUT    = 20.0
BONUS_UPPER_ZONE  =  8.0    # 位置 60〜95%
BONUS_BOTTOM_ZONE = 12.0    # 逆張り候補
BONUS_VWAP_ABOVE  =  5.0
PENALTY_VWAP_BELOW = -5.0
BONUS_RESISTANCE_BREAK = 10.0

FETCH_DELAY    = 0.3
CACHE_FILENAME = "support_resistance_cache.json"
CACHE_HOURS    = 24


# ==============================================================
# BLOCK 1: 純粋ピーク検出（scipy.signal.find_peaks 代替）
# ==============================================================

def find_peaks_pure(values: list, window: int = PEAK_WINDOW,
                     min_distance: int = MIN_PEAK_DISTANCE) -> list:
    """
    局所最大値のインデックスリストを返す。
    scipy.signal.find_peaks() の簡易代替。

    Args:
        values:       価格リスト
        window:       前後 N 本の中で最大値であることを要求
        min_distance: ピーク間の最小間隔（ノイズ除去）

    Returns:
        [idx, ...] 降順ソート済み
    """
    n     = len(values)
    peaks = []

    for i in range(window, n - window):
        v = values[i]
        is_peak = all(v >= values[i - j] for j in range(1, window + 1)) and \
                  all(v >= values[i + j] for j in range(1, window + 1))
        if not is_peak:
            continue
        # 最小間隔フィルタ（直前のピークと近すぎる場合は高い方を残す）
        if peaks and (i - peaks[-1]) < min_distance:
            if values[i] > values[peaks[-1]]:
                peaks[-1] = i   # 置き換え
        else:
            peaks.append(i)

    return sorted(peaks, key=lambda x: values[x], reverse=True)


def find_troughs_pure(values: list, window: int = PEAK_WINDOW,
                       min_distance: int = MIN_PEAK_DISTANCE) -> list:
    """局所最小値のインデックスリストを返す。"""
    inverted = [-v for v in values]
    return find_peaks_pure(inverted, window, min_distance)


# ==============================================================
# BLOCK 2: 主要指標計算
# ==============================================================

def calc_position_metrics(closes: list, highs: list, lows: list,
                           volumes: list) -> Optional[dict]:
    """
    52週高値・安値・VWAP・位置スコアを計算する。

    Returns:
        {
          "current":       float,   # 現在値
          "high_52w":      float,   # 52週高値
          "low_52w":       float,   # 52週安値
          "position_pct":  float,   # 位置スコア 0〜100
          "vwap_20d":      float,   # 20日 VWAP
          "above_vwap":    bool,
          "breakout":      bool,    # ブレイクアウト
          "near_bottom":   bool,    # 底値圏
        }
    """
    if not closes or len(closes) < 30:
        return None

    lookback = min(LOOKBACK_52W, len(closes))
    recent_c = closes[-lookback:]
    recent_h = highs[-lookback:]  if highs  else recent_c
    recent_l = lows[-lookback:]   if lows   else recent_c

    high_52w = max(recent_h)
    low_52w  = min(recent_l)
    current  = closes[-1]

    # 位置スコア（0〜100）
    rng = high_52w - low_52w
    if rng > 0:
        position_pct = round((current - low_52w) / rng * 100, 1)
    else:
        position_pct = 50.0

    # VWAP（直近20日）
    vwap = None
    if volumes and len(volumes) >= LOOKBACK_VWAP:
        vw_closes  = closes[-LOOKBACK_VWAP:]
        vw_volumes = volumes[-LOOKBACK_VWAP:]
        typical    = [(h + l + c) / 3 for h, l, c
                      in zip(highs[-LOOKBACK_VWAP:], lows[-LOOKBACK_VWAP:], vw_closes)]
        total_vol  = sum(vw_volumes)
        if total_vol > 0:
            vwap = sum(t * v for t, v in zip(typical, vw_volumes)) / total_vol

    return {
        "current":      round(current, 2),
        "high_52w":     round(high_52w, 2),
        "low_52w":      round(low_52w, 2),
        "position_pct": position_pct,
        "vwap_20d":     round(vwap, 2) if vwap else None,
        "above_vwap":   current > vwap if vwap else None,
        "breakout":     current >= high_52w * BREAKOUT_THRESH,
        "near_bottom":  current <= low_52w * BOTTOM_THRESH,
    }


def calc_resistance_levels(highs: list, window: int = PEAK_WINDOW) -> list:
    """
    高値ピークのクラスタリングから主要抵抗線を抽出する。

    Returns:
        [{"price": float, "strength": int, "pct_above": float}, ...]
        strength = そのクラスターに含まれるピーク数（多いほど強い抵抗）
        pct_above = 現在値からの乖離率%
    """
    if not highs or len(highs) < window * 2 + 1:
        return []

    peak_indices = find_peaks_pure(highs, window=window)
    if not peak_indices:
        return []

    peak_prices = [highs[i] for i in peak_indices]
    current     = highs[-1]

    # 価格クラスタリング（±1.5%を同一水準とみなす）
    clusters = []
    for price in sorted(peak_prices, reverse=True):
        merged = False
        for c in clusters:
            if abs(price - c["center"]) / c["center"] <= CLUSTER_RANGE_PCT:
                c["prices"].append(price)
                c["center"] = sum(c["prices"]) / len(c["prices"])
                merged = True
                break
        if not merged:
            clusters.append({"center": price, "prices": [price]})

    result = []
    for c in sorted(clusters, key=lambda x: -x["center"])[:5]:
        pct_above = (c["center"] - current) / current * 100
        result.append({
            "price":     round(c["center"], 2),
            "strength":  len(c["prices"]),
            "pct_above": round(pct_above, 2),
        })

    return result


# ==============================================================
# BLOCK 3: position_score 算出
# ==============================================================

def calc_position_score(metrics: dict, resistance_levels: list,
                         prev_high_52w: Optional[float] = None) -> dict:
    """
    位置指標から position_score（0〜100）を算出する。

    position_score は technical_score に加算されて composite_score に反映される。

    Returns:
        {
          "position_score":  float,   # 0〜100
          "signals":         list,    # 発動シグナルのリスト
          "label":           str,
          "sr_bonus":        float,   # technical_scoreへの加算値（-10〜+20）
        }
    """
    if not metrics:
        return {"position_score": 50.0, "signals": [], "label": "データ不足", "sr_bonus": 0.0}

    raw_score = 50.0
    signals   = []
    pct       = metrics.get("position_pct", 50)

    # ── 位置ゾーン判定 ─────────────────────────────────────
    if metrics.get("breakout"):
        raw_score += BONUS_BREAKOUT
        signals.append(f"🚀 ブレイクアウト候補(位置{pct:.0f}%):+{BONUS_BREAKOUT:.0f}")
    elif pct >= 60:
        raw_score += BONUS_UPPER_ZONE
        signals.append(f"📈 上昇トレンド圏(位置{pct:.0f}%):+{BONUS_UPPER_ZONE:.0f}")
    elif metrics.get("near_bottom"):
        raw_score += BONUS_BOTTOM_ZONE
        signals.append(f"🎯 底値圏逆張り(位置{pct:.0f}%):+{BONUS_BOTTOM_ZONE:.0f}")
    elif pct <= 20:
        raw_score -= 8
        signals.append(f"📉 下落圏(位置{pct:.0f}%):-8")

    # ── VWAP 判定 ───────────────────────────────────────────
    above_vwap = metrics.get("above_vwap")
    vwap       = metrics.get("vwap_20d")
    if above_vwap is True:
        raw_score += BONUS_VWAP_ABOVE
        signals.append(f"VWAP上({vwap:.0f}円):+{BONUS_VWAP_ABOVE:.0f}")
    elif above_vwap is False:
        raw_score += PENALTY_VWAP_BELOW
        signals.append(f"VWAP下({vwap:.0f}円):{PENALTY_VWAP_BELOW:.0f}")

    # ── 抵抗線突破チェック ───────────────────────────────────
    # 直前の52週高値 vs 現在高値で「新高値更新」を確認
    if prev_high_52w and metrics.get("high_52w", 0) > prev_high_52w * 1.001:
        raw_score += BONUS_RESISTANCE_BREAK
        signals.append(f"🔥 52週高値更新:+{BONUS_RESISTANCE_BREAK:.0f}")

    # 主要抵抗線の直下にある場合は注意フラグ
    for lvl in resistance_levels[:2]:
        pct_above = lvl.get("pct_above", 99)
        if 0 < pct_above < 3:
            raw_score -= 5
            signals.append(f"⚠️ 抵抗線直下({lvl['price']:.0f}):-5")
            break

    score = round(max(0, min(100, raw_score)), 1)
    sr_bonus = round(score - 50, 1)  # 50を中心とした乖離をボーナスとして使用
    sr_bonus = round(max(-10, min(20, sr_bonus * 0.4)), 1)  # スケール調整

    if score >= 70:   label = "🚀 強いポジション"
    elif score >= 55: label = "📈 良好なポジション"
    elif score >= 45: label = "➡️ 中立"
    elif score >= 30: label = "📉 弱いポジション"
    else:             label = "⚠️ 下落圏"

    return {
        "position_score":     score,
        "signals":            signals,
        "label":              label,
        "sr_bonus":           sr_bonus,
        "position_pct":       pct,
        "resistance_levels":  resistance_levels[:3],
    }


# ==============================================================
# BLOCK 4: yfinance データ取得
# ==============================================================

def _fetch_ohlcv(ticker: str) -> Optional[dict]:
    """yfinance から 52週分の OHLCV を取得する。"""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="2y", auto_adjust=True)
        if hist is None or len(hist) < 60:
            return None
        return {
            "closes":  hist["Close"].dropna().tolist(),
            "highs":   hist["High"].dropna().tolist(),
            "lows":    hist["Low"].dropna().tolist(),
            "volumes": hist["Volume"].dropna().tolist(),
        }
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
        if datetime.now() - datetime.fromisoformat(data.get("cached_at", "2000-01-01")) \
                < timedelta(hours=CACHE_HOURS):
            return data.get("results", {})
    except Exception:
        pass
    return {}

def _save_cache(cfg, results: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"cached_at": datetime.now().isoformat(), "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def get_sr_bonus(ticker: str, sr_cache: dict) -> float:
    """stock_screener.py から呼び出す統合インターフェース。"""
    rec = sr_cache.get(ticker, {})
    return float(rec.get("sr_bonus", 0.0))


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_support_resistance(cfg, tickers: list) -> dict:
    """
    screener 通過銘柄のサポート・レジスタンス分析を一括実行する。

    stock_analyzer_main.py から screener ブロック後に呼び出す:
        _sr = _load_local_module("support_resistance", ...)
        sr_result = _sr.run_support_resistance(cfg, screener_tickers)
        results["support_resistance"] = sr_result

    Returns:
        {"results": {ticker: sr_result}, "top_breakout": [...], "timestamp": str}
    """
    print(f"\n  📐 サポート・レジスタンス分析開始（{len(tickers)}銘柄）...")

    cached   = _load_cache(cfg)
    to_fetch = [t for t in tickers if t not in cached]
    results  = dict(cached)

    for ticker in to_fetch:
        ohlcv = _fetch_ohlcv(ticker)
        if not ohlcv:
            continue

        closes  = ohlcv["closes"]
        highs   = ohlcv["highs"]
        lows    = ohlcv["lows"]
        volumes = ohlcv["volumes"]

        metrics    = calc_position_metrics(closes, highs, lows, volumes)
        resistance = calc_resistance_levels(highs)
        sr_data    = calc_position_score(metrics, resistance)

        if metrics:
            sr_data.update(metrics)
        sr_data["ticker"] = ticker
        results[ticker] = sr_data

        if abs(sr_data.get("sr_bonus", 0)) > 3:
            print(f"      {ticker}: pos={sr_data.get('position_pct',0):.0f}% "
                  f"bonus={sr_data.get('sr_bonus',0):+.1f}pt {sr_data.get('label','')}")
        time.sleep(FETCH_DELAY)

    _save_cache(cfg, results)

    top_breakout = sorted(
        [r for r in results.values() if isinstance(r, dict) and r.get("breakout")],
        key=lambda x: x.get("position_score", 0), reverse=True)[:5]

    print(f"    ✅ SR分析完了: {len(results)}銘柄 / ブレイクアウト候補{len(top_breakout)}件")
    return {"results": results, "top_breakout": top_breakout,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
