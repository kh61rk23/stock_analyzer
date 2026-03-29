# ==============================================================
# volume_profile.py  v1.0  (4-7 / B-3)
# 出来高プロファイル分析（Volume Profile）
# ==============================================================
# 【役割】
#   価格帯別の出来高分布を分析し、
#   「市場参加者が最もアクティブだった価格帯（価値帯）」を特定する。
#   POC (Point of Control) = 最大出来高価格帯 = 強いサポート/レジスタンス
#
# 【計算ロジック】
#   1. 過去 N 日の日次 OHLCV を価格帯（M バケット）に分割
#   2. 各日の典型価格 (high+low+close)/3 を計算
#   3. 典型価格に対応するバケットに出来高を加算
#   4. 全バケットの出来高分布から:
#      POC:  最大出来高のバケット中心価格
#      VAH:  Value Area High（上位70%の出来高を含む上限）
#      VAL:  Value Area Low（上位70%の出来高を含む下限）
#      HVN:  High Volume Node（出来高 > 平均 × 1.5 のバケット）
#      LVN:  Low Volume Node（出来高 < 平均 × 0.5 の薄い価格帯）
#
# 【vp_score（0〜100）の計算】
#   現在値がバリューエリア内（VAL ≤ 価格 ≤ VAH）:  +0〜+5pt（中立）
#   現在値が VAH を上抜け（上昇継続の可能性）:      +10pt
#   現在値が VAL を下抜け（下落継続の可能性）:      -10pt
#   POC からの乖離が大きい（回帰圧力あり）:
#     POC より +5%以上高い:                         -8pt（割高）
#     POC より -5%以上安い:                         +8pt（割安・戻り期待）
#   HVN にいる（出来高のサポートが厚い）:            +5pt
#   LVN にいる（サポートが薄く急変動しやすい）:      -5pt
#
# 【screener 統合】
#   support_resistance.py の sr_bonus と同様に
#   technical_score の補完指標として加算
#   vp_score はデルタ（0基準の乖離）として technical_bonus に追加
#
# 【依存】yfinance のみ（pandas・numpy 不使用可）
# 【呼出】from volume_profile import run_volume_profile, get_vp_bonus
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
VP_LOOKBACK_DAYS  = 60      # 出来高プロファイル計算期間（直近N日）
VP_NUM_BUCKETS    = 30      # 価格帯の分割数（30バケット）
VALUE_AREA_PCT    = 0.70    # バリューエリア: 全出来高の70%を含む範囲
HVN_MULTIPLIER    = 1.5     # 平均出来高 × この倍数以上 = HVN
LVN_MULTIPLIER    = 0.5     # 平均出来高 × この倍数以下 = LVN

# vp_score ボーナス値
BONUS_ABOVE_VAH   = 10.0    # バリューエリア上抜け
PENALTY_BELOW_VAL = -10.0   # バリューエリア下抜け
BONUS_POC_CHEAP   =  8.0    # POCより-5%以上安い（割安）
PENALTY_POC_DEAR  = -8.0    # POCより+5%以上高い（割高）
BONUS_HVN         =  5.0    # HVN にいる
PENALTY_LVN       = -5.0    # LVN にいる（急変動リスク）

POC_DEVIATION_THRESH = 0.05  # POC 乖離率の閾値（5%）

FETCH_DELAY    = 0.3
CACHE_FILENAME = "volume_profile_cache.json"
CACHE_HOURS    = 24


# ==============================================================
# BLOCK 1: 出来高プロファイル計算（コア）
# ==============================================================

def calc_volume_profile(closes: list, highs: list, lows: list,
                         volumes: list, n_buckets: int = VP_NUM_BUCKETS) -> Optional[dict]:
    """
    OHLCV データから出来高プロファイルを計算する。

    Returns:
        {
          "poc_price":    float,   # Point of Control（最大出来高価格帯）
          "vah_price":    float,   # Value Area High（上限）
          "val_price":    float,   # Value Area Low（下限）
          "hvn_prices":   list,    # High Volume Node の価格帯リスト
          "lvn_prices":   list,    # Low Volume Node の価格帯リスト
          "price_range":  tuple,   # (min_price, max_price)
          "buckets":      list,    # 全バケット [{center, volume}, ...]
          "current_zone": str,     # "above_vah" / "in_va" / "below_val"
        }
    """
    n = min(len(closes), len(highs), len(lows), len(volumes), VP_LOOKBACK_DAYS)
    if n < 10:
        return None

    r_closes  = closes[-n:]
    r_highs   = highs[-n:]
    r_lows    = lows[-n:]
    r_volumes = volumes[-n:]

    # 価格レンジ
    price_max = max(r_highs)
    price_min = min(r_lows)
    if price_max <= price_min:
        return None

    bucket_size = (price_max - price_min) / n_buckets

    # バケット初期化
    buckets = [{"center": price_min + (i + 0.5) * bucket_size, "volume": 0.0}
               for i in range(n_buckets)]

    # 各日のボリュームを典型価格のバケットに振り分け
    for i in range(n):
        typical = (r_highs[i] + r_lows[i] + r_closes[i]) / 3
        idx = int((typical - price_min) / bucket_size)
        idx = max(0, min(n_buckets - 1, idx))
        buckets[idx]["volume"] += r_volumes[i]

    # POC: 最大出来高バケット
    poc_bucket = max(buckets, key=lambda b: b["volume"])
    poc_price  = poc_bucket["center"]

    # Value Area（全出来高の70%を含む範囲）
    total_vol = sum(b["volume"] for b in buckets)
    target_vol = total_vol * VALUE_AREA_PCT

    # POC から上下に広げてバリューエリアを決定
    poc_idx = buckets.index(poc_bucket)
    va_indices = {poc_idx}
    va_vol = poc_bucket["volume"]
    lo, hi = poc_idx, poc_idx

    while va_vol < target_vol:
        can_up   = hi < n_buckets - 1
        can_down = lo > 0
        if not can_up and not can_down:
            break
        up_vol   = buckets[hi + 1]["volume"] if can_up   else -1
        down_vol = buckets[lo - 1]["volume"] if can_down else -1
        if up_vol >= down_vol:
            hi += 1
            va_indices.add(hi)
            va_vol += buckets[hi]["volume"]
        else:
            lo -= 1
            va_indices.add(lo)
            va_vol += buckets[lo]["volume"]

    vah_price = buckets[hi]["center"] + bucket_size / 2
    val_price = buckets[lo]["center"] - bucket_size / 2

    # HVN / LVN 識別
    avg_vol = total_vol / n_buckets if n_buckets > 0 else 0
    hvn_prices = [b["center"] for b in buckets if b["volume"] >= avg_vol * HVN_MULTIPLIER]
    lvn_prices = [b["center"] for b in buckets if 0 < b["volume"] <= avg_vol * LVN_MULTIPLIER]

    # 現在値のゾーン
    current = r_closes[-1]
    if current > vah_price:
        current_zone = "above_vah"
    elif current < val_price:
        current_zone = "below_val"
    else:
        current_zone = "in_va"

    return {
        "poc_price":   round(poc_price, 2),
        "vah_price":   round(vah_price, 2),
        "val_price":   round(val_price, 2),
        "hvn_prices":  [round(p, 2) for p in sorted(hvn_prices)],
        "lvn_prices":  [round(p, 2) for p in sorted(lvn_prices)],
        "price_range": (round(price_min, 2), round(price_max, 2)),
        "buckets":     [{"center": round(b["center"], 2),
                         "volume": round(b["volume"], 0)} for b in buckets],
        "current_zone": current_zone,
        "current_price": round(current, 2),
    }


# ==============================================================
# BLOCK 2: vp_score 算出
# ==============================================================

def calc_vp_score(vp_data: dict) -> dict:
    """
    出来高プロファイルから vp_score（0〜100）を算出する。

    Returns:
        {
          "vp_score":    float,   # 0〜100
          "vp_bonus":    float,   # technical_score への加算値（-10〜+10）
          "signals":     list,
          "label":       str,
          "poc_gap_pct": float,   # 現在値と POC の乖離率%
        }
    """
    if not vp_data:
        return {"vp_score": 50.0, "vp_bonus": 0.0, "signals": [], "label": "データ不足",
                "poc_gap_pct": 0.0}

    current     = vp_data.get("current_price", 0)
    poc_price   = vp_data.get("poc_price", current)
    vah_price   = vp_data.get("vah_price", current)
    val_price   = vp_data.get("val_price", current)
    hvn_prices  = vp_data.get("hvn_prices", [])
    lvn_prices  = vp_data.get("lvn_prices", [])
    zone        = vp_data.get("current_zone", "in_va")

    raw_score = 50.0
    signals   = []

    # ── ゾーン判定 ────────────────────────────────────────
    if zone == "above_vah":
        raw_score += BONUS_ABOVE_VAH
        signals.append(f"VAH上抜け(VAH={vah_price:.0f}):+{BONUS_ABOVE_VAH:.0f}")
    elif zone == "below_val":
        raw_score += PENALTY_BELOW_VAL
        signals.append(f"VAL下抜け(VAL={val_price:.0f}):{PENALTY_BELOW_VAL:.0f}")
    else:
        signals.append(f"バリューエリア内(VAL={val_price:.0f}〜VAH={vah_price:.0f})")

    # ── POC 乖離率 ────────────────────────────────────────
    poc_gap_pct = (current - poc_price) / poc_price * 100 if poc_price > 0 else 0
    if poc_gap_pct <= -POC_DEVIATION_THRESH * 100:
        raw_score += BONUS_POC_CHEAP
        signals.append(f"POC下方乖離{poc_gap_pct:+.1f}%(戻り期待):+{BONUS_POC_CHEAP:.0f}")
    elif poc_gap_pct >= POC_DEVIATION_THRESH * 100:
        raw_score += PENALTY_POC_DEAR
        signals.append(f"POC上方乖離{poc_gap_pct:+.1f}%(割高):{PENALTY_POC_DEAR:.0f}")

    # ── HVN / LVN ─────────────────────────────────────────
    # 現在値に近い HVN / LVN を確認（±3%以内）
    in_hvn = any(abs(current - p) / p < 0.03 for p in hvn_prices if p > 0)
    in_lvn = any(abs(current - p) / p < 0.03 for p in lvn_prices if p > 0)

    if in_hvn:
        raw_score += BONUS_HVN
        signals.append(f"HVN付近（出来高厚いサポート）:+{BONUS_HVN:.0f}")
    if in_lvn:
        raw_score += PENALTY_LVN
        signals.append(f"LVN付近（出来高薄く急変動リスク）:{PENALTY_LVN:.0f}")

    score    = round(max(0, min(100, raw_score)), 1)
    vp_bonus = round((score - 50) * 0.2, 1)   # ±10pt にスケール
    vp_bonus = round(max(-10, min(10, vp_bonus)), 1)

    if score >= 65:   label = "🟢 VP良好（出来高サポート強）"
    elif score >= 50: label = "🟡 VP中立"
    elif score >= 35: label = "🟠 VP注意"
    else:             label = "🔴 VP弱気（サポート薄）"

    return {
        "vp_score":    score,
        "vp_bonus":    vp_bonus,
        "signals":     signals,
        "label":       label,
        "poc_gap_pct": round(poc_gap_pct, 2),
        "poc_price":   poc_price,
        "vah_price":   vah_price,
        "val_price":   val_price,
    }


# ==============================================================
# BLOCK 3: yfinance データ取得
# ==============================================================

def _fetch_ohlcv(ticker: str) -> Optional[dict]:
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="6mo", auto_adjust=True)
        if hist is None or len(hist) < 20:
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
# BLOCK 4: キャッシュ管理
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

def get_vp_bonus(ticker: str, vp_cache: dict) -> float:
    """stock_screener.py から呼び出す統合インターフェース。"""
    rec = vp_cache.get(ticker, {})
    return float(rec.get("vp_bonus", 0.0))


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_volume_profile(cfg, tickers: list) -> dict:
    """
    screener 通過銘柄の出来高プロファイルを一括分析する。

    stock_analyzer_main.py から screener ブロック後に呼び出す:
        _vp = _load_local_module("volume_profile", ...)
        vp_result = _vp.run_volume_profile(cfg, screener_tickers)
        results["volume_profile"] = vp_result

    Returns:
        {"results": {ticker: vp_result}, "top_poc_cheap": [...], "timestamp": str}
    """
    print(f"\n  📊 出来高プロファイル分析開始（{len(tickers)}銘柄）...")

    cached   = _load_cache(cfg)
    to_fetch = [t for t in tickers if t not in cached]
    results  = dict(cached)

    for ticker in to_fetch:
        ohlcv = _fetch_ohlcv(ticker)
        if not ohlcv:
            continue

        vp_raw = calc_volume_profile(
            ohlcv["closes"], ohlcv["highs"], ohlcv["lows"], ohlcv["volumes"])
        vp_scored = calc_vp_score(vp_raw)

        if vp_raw:
            vp_scored.update({k: v for k, v in vp_raw.items()
                               if k not in ("buckets",)})   # バケット詳細は省略
        vp_scored["ticker"] = ticker
        results[ticker] = vp_scored

        if abs(vp_scored.get("vp_bonus", 0)) > 2:
            print(f"      {ticker}: poc_gap={vp_scored.get('poc_gap_pct',0):+.1f}% "
                  f"zone={vp_scored.get('current_zone','?')} "
                  f"bonus={vp_scored.get('vp_bonus',0):+.1f}pt")
        time.sleep(FETCH_DELAY)

    _save_cache(cfg, results)

    top_poc_cheap = sorted(
        [r for r in results.values()
         if isinstance(r, dict) and r.get("poc_gap_pct", 0) < -3],
        key=lambda x: x.get("poc_gap_pct", 0))[:5]

    print(f"    ✅ VP分析完了: {len(results)}銘柄 / "
          f"POC割安候補{len(top_poc_cheap)}件")
    return {"results": results, "top_poc_cheap": top_poc_cheap,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
