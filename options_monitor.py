# ==============================================================
# options_monitor.py  v1.0  (4-5 / A-1)
# オプション市場データ分析（IV・Put/Call比率・市場センチメント）
# ==============================================================
# 【役割】
#   yfinance のオプションチェーンから IV（インプライドボラティリティ）と
#   Put/Call 比率（PCR）を取得し、市場参加者の「恐怖感・強欲度」を
#   0〜100 のセンチメントスコアで数値化する。
#
# 【対象】
#   日本市場: 日経225先物代理 (^N225) / EWJ ETF
#   米国市場: SPY（S&P500 ETF）/ QQQ（ナスダック） / VIX
#
# 【計算指標】
#   A. Put/Call 比率（PCR）
#      PCR = 総プット OI（建玉） / 総コール OI
#      PCR > 1.2: 悲観過多 → 逆張り買いシグナル
#      PCR < 0.6: 楽観過多 → 天井シグナル
#
#   B. IV スキュー（恐怖度）
#      OTM プット IV - OTM コール IV
#      スキューが大きい = 下落リスクへの警戒が高い
#
#   C. VIX との比較（VIX レベル × トレンド）
#      VIX > 30: 極度の恐怖 → 逆張り底値
#      VIX < 15: 楽観 → 天井注意
#
#   D. IV パーセンタイル（IV Rank）
#      現在の IV が過去52週の何パーセンタイルか
#      IV Rank > 50% = オプション売りの好機 / 市場不確実性高
#
# 【センチメントスコア（0〜100）】
#   低スコア（0〜30） : 過度な恐怖 → 逆張り買いシグナル（底値圏）
#   中スコア（30〜70）: 中立
#   高スコア（70〜100）: 過度な楽観 → 天井シグナル（天井圏）
#
# 【composite_score への影響】
#   センチメント低（恐怖）: ディフェンシブ業種 +5pt、景気敏感 -3pt
#   センチメント高（楽観）: 景気敏感業種 +3pt、ディフェンシブ -2pt
#   極端値（< 20 or > 80）: 全業種にシグナル強化ボーナス追加
#
# 【依存】yfinance のみ（APIキー不要）
# 【呼出】from options_monitor import run_options_monitor
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config_sync import sync_module_constants

# ── 定数 ──────────────────────────────────────────────────────
# 対象ティッカー
OPTIONS_TICKERS = {
    "spy":  "SPY",     # S&P500 ETF（最も流動性が高い）
    "qqq":  "QQQ",     # ナスダック100 ETF
    "ewj":  "EWJ",     # 日本株 ETF（米国上場）
}
VIX_TICKER    = "^VIX"
VVIX_TICKER   = "^VVIX"   # VIX of VIX（ボラティリティのボラティリティ）

# Put/Call 比率のしきい値
PCR_FEAR_THRESH = 1.2   # この値超 → 悲観過多（逆張り買い）
PCR_GREED_THRESH = 0.6  # この値未満 → 楽観過多（天井注意）

# VIX しきい値
VIX_EXTREME_FEAR  = 30  # 極度の恐怖

CACHE_FILENAME  = "options_monitor_cache.json"
CACHE_HOURS     = 4     # オプションデータは4時間キャッシュ（日中変動が大きい）
# ── [Phase US-4] VIX term structure ティッカー ───────────────
VIX9D_TICKER   = "^VIX9D"    # 9日VIX（超短期）
VIX3M_TICKER   = "^VIX3M"    # 3ヶ月VIX
VIX6M_TICKER   = "^VIX6M"    # 6ヶ月VIX
SKEW_TICKER    = "^SKEW"     # CBOE SKEW指数（テールリスク）

# term structure のコンタンゴ/バックワーデーション閾値
TERM_CONTANGO_RATIO  = 0.90  # VIX9D/VIX < 0.90 → 通常（コンタンゴ）
TERM_BACKWD_RATIO    = 1.10  # VIX9D/VIX > 1.10 → バックワーデーション（即時リスクオフ）

# SKEW 指数閾値
SKEW_EXTREME = 145   # 145超 = テールリスク極大（ブラックスワン警戒）
SKEW_ELEVATED =130  # 130超 = テールリスク高め

# 個別株IV急騰しきい値（IV Rank）
IV_SPIKE_THRESHOLD = 70    # IV Rank 70%ile超 = IV急騰（イベント前シグナル）

# term_structure_score の signal_aggregator カテゴリE寄与
TERM_SCORE_WEIGHT   = 0.20  # カテゴリEの既存VIXスコア(0.35)に補完
FETCH_DELAY     = 0.4

# センチメントスコアによる業種補正マップ
# (センチメント低=恐怖時, センチメント高=楽観時)
FEAR_TAILWIND_SECTORS  = ["1008", "1020", "1004"]   # 医薬・電力・食料（ディフェンシブ）
FEAR_HEADWIND_SECTORS  = ["1016", "1017", "1022"]   # 電気機器・自動車・海運（景気敏感）
GREED_TAILWIND_SECTORS = ["1016", "1017", "1025"]   # 景気敏感・情報
GREED_HEADWIND_SECTORS = ["1008", "1020"]            # ディフェンシブ（相対劣後）

_CFG_CONST_MAP = {
    "OM_PCR_FEAR_THRESH": "PCR_FEAR_THRESH",
    "OM_PCR_GREED_THRESH": "PCR_GREED_THRESH",
    "OM_TERM_CONTANGO_RATIO": "TERM_CONTANGO_RATIO",
    "OM_TERM_BACKWD_RATIO": "TERM_BACKWD_RATIO",
    "OM_TERM_SCORE_WEIGHT": "TERM_SCORE_WEIGHT",
    'B4_OPTIONS_MONITOR_CACHE_HOURS': 'CACHE_HOURS',
    'B5_OPTIONS_MONITOR_FETCH_DELAY': 'FETCH_DELAY',
    'B6_OPTIONS_MONITOR_VIX_EXTREME_FEAR': 'VIX_EXTREME_FEAR',
    'B6_OPTIONS_MONITOR_SKEW_EXTREME': 'SKEW_EXTREME',
    'B6_OPTIONS_MONITOR_SKEW_ELEVATED': 'SKEW_ELEVATED',
}


# ==============================================================
# BLOCK 1: オプションチェーン取得
# ==============================================================

def _fetch_option_chain(ticker: str) -> Optional[dict]:
    """
    yfinance からオプションチェーンを取得し
    PCR・IV を計算する。

    Returns:
        {
          "ticker":      str,
          "pcr_oi":      float,   # OI ベース PCR
          "pcr_volume":  float,   # Volume ベース PCR
          "avg_call_iv": float,   # 平均コール IV
          "avg_put_iv":  float,   # 平均プット IV
          "iv_skew":     float,   # プット IV - コール IV
          "total_call_oi": int,
          "total_put_oi":  int,
          "expiry_used":   str,
        }
    """
    try:
        import yf_cache as yf

        t = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return None

        # 30〜60日先の満期を選択（流動性が高い）
        today = datetime.now().date()
        target_expiry = None
        for exp in expirations:
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                days_to_exp = (exp_date - today).days
                if 20 <= days_to_exp <= 65:
                    target_expiry = exp
                    break
            except ValueError:
                continue

        if not target_expiry:
            target_expiry = expirations[min(1, len(expirations)-1)]

        chain = t.option_chain(target_expiry)
        calls = chain.calls
        puts  = chain.puts

        if calls is None or puts is None or calls.empty or puts.empty:
            return None

        # OI（建玉）の合計
        total_call_oi = int(calls["openInterest"].fillna(0).sum())
        total_put_oi  = int(puts["openInterest"].fillna(0).sum())
        total_call_vol = int(calls["volume"].fillna(0).sum())
        total_put_vol  = int(puts["volume"].fillna(0).sum())

        pcr_oi     = round(total_put_oi / total_call_oi, 4) if total_call_oi > 0 else None
        pcr_volume = round(total_put_vol / total_call_vol, 4) if total_call_vol > 0 else None

        # IV（インプライドボラティリティ）の平均
        avg_call_iv = calls["impliedVolatility"].replace(0, None).dropna().mean()
        avg_put_iv  = puts["impliedVolatility"].replace(0, None).dropna().mean()

        if avg_call_iv and avg_put_iv:
            iv_skew = round(float(avg_put_iv - avg_call_iv) * 100, 2)  # %表示
        else:
            iv_skew = None

        return {
            "ticker":          ticker,
            "pcr_oi":          pcr_oi,
            "pcr_volume":      pcr_volume,
            "avg_call_iv":     round(float(avg_call_iv) * 100, 2) if avg_call_iv else None,
            "avg_put_iv":      round(float(avg_put_iv)  * 100, 2) if avg_put_iv  else None,
            "iv_skew":         iv_skew,
            "total_call_oi":   total_call_oi,
            "total_put_oi":    total_put_oi,
            "expiry_used":     target_expiry,
        }
    except Exception:
        return None


def _fetch_vix_data() -> dict:
    """
    VIX と VVIX の現在値・52週レンジを取得する。
    """
    try:
        import yf_cache as yf

        result = {}
        for name, ticker in [("vix", VIX_TICKER), ("vvix", VVIX_TICKER)]:
            try:
                t    = yf.Ticker(ticker)
                hist = t.history(period="1y", auto_adjust=True)
                if hist is None or hist.empty:
                    continue
                closes = hist["Close"].dropna().tolist()
                current = closes[-1]
                high_52w = max(closes)
                low_52w  = min(closes)
                # IV Rank = 現在値が52週レンジの何パーセンタイルか
                iv_rank = (current - low_52w) / (high_52w - low_52w) * 100 if (high_52w - low_52w) > 0 else 50

                # 30日前との比較（トレンド）
                trend = None
                if len(closes) >= 30:
                    past_30 = closes[-30]
                    trend = "rising" if current > past_30 * 1.05 else (
                             "falling" if current < past_30 * 0.95 else "stable")

                result[name] = {
                    "current":  round(current, 2),
                    "high_52w": round(high_52w, 2),
                    "low_52w":  round(low_52w, 2),
                    "iv_rank":  round(iv_rank, 1),
                    "trend":    trend,
                }
                time.sleep(FETCH_DELAY)
            except Exception:
                continue

        return result
    except Exception:
        return {}


def _fetch_vix_term_structure() -> dict:
    """
    [Phase US-4] VIX term structure を取得する。

    VIX9D / VIX / VIX3M / VIX6M を yfinance で取得し、
    term structure の形状（コンタンゴ / バックワーデーション）を判定する。

    Returns:
        {
          "vix9d":       float | None,
          "vix":         float | None,
          "vix3m":       float | None,
          "vix6m":       float | None,
          "ts_ratio_9d": float,   # VIX9D / VIX（< 1.0 = コンタンゴ）
          "ts_ratio_3m": float,   # VIX3M / VIX
          "shape":       "contango" | "backwardation" | "flat",
          "crisis_signal": bool,  # 即時リスクオフシグナル
          "term_score":  float,   # 0〜100（高=安心/低=警戒）
        }
    """
    try:
        import yf_cache as yf
        tickers = {
            "vix9d": VIX9D_TICKER,
            "vix":   VIX_TICKER,
            "vix3m": VIX3M_TICKER,
            "vix6m": VIX6M_TICKER,
        }
        values = {}
        for name, ticker in tickers.items():
            try:
                info = yf.Ticker(ticker).info or {}
                price = (info.get("regularMarketPrice") or
                         info.get("currentPrice") or
                         info.get("previousClose"))
                values[name] = round(float(price), 2) if price else None
                time.sleep(0.1)
            except Exception:
                values[name] = None

        vix9d = values.get("vix9d")
        vix   = values.get("vix")
        vix3m = values.get("vix3m")
        vix6m = values.get("vix6m")

        # term structure 判定
        ts_ratio_9d = round(vix9d / vix, 4) if (vix9d and vix and vix > 0) else None
        ts_ratio_3m = round(vix3m / vix, 4) if (vix3m and vix and vix > 0) else None

        # 形状判定
        if ts_ratio_9d is None:
            shape = "unknown"
        elif ts_ratio_9d >= TERM_BACKWD_RATIO:
            shape = "backwardation"   # 短期 > 長期 → 即時パニック
        elif ts_ratio_9d <= TERM_CONTANGO_RATIO:
            shape = "contango"        # 短期 < 長期 → 通常
        else:
            shape = "flat"

        # 即時リスクオフシグナル
        crisis_signal = (
            shape == "backwardation" or
            (vix is not None and vix >= VIX_EXTREME_FEAR)
        )

        # term_score（0〜100 / 高=安心）
        if ts_ratio_9d is not None:
            # ratio 0.80 = score 100（完全コンタンゴ）/ ratio 1.20 = score 0（強バックワーデーション）
            term_score = max(0.0, min(100.0, (TERM_BACKWD_RATIO - ts_ratio_9d) /
                                     (TERM_BACKWD_RATIO - TERM_CONTANGO_RATIO) * 100))
        else:
            term_score = 50.0

        return {
            "vix9d":         vix9d,
            "vix":           vix,
            "vix3m":         vix3m,
            "vix6m":         vix6m,
            "ts_ratio_9d":   ts_ratio_9d,
            "ts_ratio_3m":   ts_ratio_3m,
            "shape":         shape,
            "crisis_signal": crisis_signal,
            "term_score":    round(term_score, 1),
        }
    except Exception as e:
        print(f"    ⚠️  VIX term structure 取得失敗: {str(e)[:50]}")
        return {"shape": "unknown", "crisis_signal": False, "term_score": 50.0}


def _fetch_skew_index() -> dict:
    """
    [Phase US-4] CBOE SKEW 指数を取得する。

    SKEW は OTM プット需要から計算される「テールリスク」の市場評価。
    通常 100〜150 の範囲。145 超 = ブラックスワン警戒水準。

    Returns:
        {
          "value":        float | None,
          "level":        "extreme" | "elevated" | "normal",
          "tail_risk":    "high" | "medium" | "low",
          "skew_score":   float,   # 0〜100（100=テールリスク無し）
        }
    """
    try:
        import yf_cache as yf
        info  = yf.Ticker(SKEW_TICKER).info or {}
        price = (info.get("regularMarketPrice") or
                 info.get("previousClose"))
        if not price:
            return {"value": None, "level": "unknown", "tail_risk": "unknown", "skew_score": 50.0}

        value = round(float(price), 2)
        if value >= SKEW_EXTREME:
            level      = "extreme"
            tail_risk  = "high"
            skew_score = max(0.0, 100 - (value - 100) / 0.6)   # 145 → ~17
        elif value >= SKEW_ELEVATED:
            level      = "elevated"
            tail_risk  = "medium"
            skew_score = max(20.0, 100 - (value - 100) / 0.45)
        else:
            level      = "normal"
            tail_risk  = "low"
            skew_score = min(100.0, 100 - (value - 100) / 0.3)

        return {
            "value":      value,
            "level":      level,
            "tail_risk":  tail_risk,
            "skew_score": round(skew_score, 1),
        }
    except Exception:
        return {"value": None, "level": "unknown", "tail_risk": "unknown", "skew_score": 50.0}


def detect_iv_spikes(
    screener_tickers: list,
    threshold: int = IV_SPIKE_THRESHOLD,
) -> list[dict]:
    """
    [Phase US-4] screener 通過銘柄の個別 IV を取得し、IV急騰銘柄を検知する。

    決算前・重要イベント前の IV 急騰 = イベントドリブン戦略のトリガー。
    us_8k_monitor.py の screener_hits と組み合わせて使用する。

    Args:
        screener_tickers: 対象ティッカーリスト（上限10社・API負荷対策）
        threshold:        IV Rank がこれ超 = 急騰判定

    Returns:
        [{
          "ticker":    str,
          "iv_rank":   float,
          "avg_iv":    float,
          "signal":    "iv_spike",
          "note":      str,
        }, ...]
    """
    try:
        import yf_cache as yf
    except ImportError:
        return []

    spike_list = []
    for ticker in screener_tickers[:10]:
        try:
            t    = yf.Ticker(ticker)
            exp_dates = t.options
            if not exp_dates:
                continue
            # 直近の満期を使用
            chain = t.option_chain(exp_dates[0])
            calls = chain.calls
            if calls is None or calls.empty:
                continue

            iv_vals = calls["impliedVolatility"].replace(0, None).dropna().tolist()
            if not iv_vals:
                continue

            avg_iv = sum(iv_vals) / len(iv_vals) * 100

            # yfinance では historical IV が取れないため
            # 過去1年のヒストリカルボラティリティで IV Rank を近似する
            hist   = t.history(period="1y", auto_adjust=True)
            if hist is None or hist.empty:
                continue
            closes = hist["Close"].dropna().tolist()
            if len(closes) < 20:
                continue

            import math
            returns = [(closes[i] - closes[i-1]) / closes[i-1]
                       for i in range(1, len(closes))]
            mean_r = sum(returns) / len(returns)
            std_r  = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns))
            hv_ann = std_r * math.sqrt(252) * 100  # 年率換算%

            # IV / HV の比（1.5超 = IV 過剰プレミアム）
            iv_hv_ratio = avg_iv / hv_ann if hv_ann > 0 else 1.0

            # IV Rank の近似（IV/HV比 1.0 → 50%ile / 2.0 → 100%ile）
            iv_rank_approx = min(100, max(0, (iv_hv_ratio - 0.5) / 1.5 * 100))

            if iv_rank_approx >= threshold:
                spike_list.append({
                    "ticker":      ticker,
                    "iv_rank":     round(iv_rank_approx, 1),
                    "avg_iv":      round(avg_iv, 1),
                    "hv_annual":   round(hv_ann, 1),
                    "iv_hv_ratio": round(iv_hv_ratio, 2),
                    "signal":      "iv_spike",
                    "note":        f"IV{avg_iv:.0f}% / HV{hv_ann:.0f}% / ratio={iv_hv_ratio:.2f}",
                })
            time.sleep(FETCH_DELAY)
        except Exception:
            continue

    return sorted(spike_list, key=lambda x: x["iv_rank"], reverse=True)

# ==============================================================
# BLOCK 2: センチメントスコア算出
# ==============================================================

def calc_sentiment_score(
    option_data: dict,
    vix_data:    dict,
) -> dict:
    """
    オプションデータと VIX からセンチメントスコア（0〜100）を算出する。

    スコアが低いほど「恐怖・悲観」、高いほど「楽観・強欲」。
    Fear & Greed Index に相当する独自指標。

    Returns:
        {
          "score":       float,   # 0〜100
          "label":       str,     # "極度の恐怖" / "恐怖" / "中立" / "楽観" / "極度の楽観"
          "components":  dict,    # 各指標の寄与
          "signal":      str,     # "contrarian_buy" / "neutral" / "contrarian_sell"
        }
    """
    components = {}
    raw_scores = []

    # ── VIX スコア（逆数: VIX 高 → 恐怖 → スコア低）────────────────
    vix_info = vix_data.get("vix", {})
    vix_val  = vix_info.get("current")
    if vix_val is not None:
        # VIX 10 → score 100（楽観）/ VIX 40 → score 0（恐怖）
        vix_score = max(0, min(100, (40 - vix_val) / 30 * 100))
        raw_scores.append(("vix", vix_score, 0.35))
        components["vix"] = {
            "value": vix_val,
            "score": round(vix_score, 1),
            "weight": 0.35,
        }

    # ── IV Rank スコア（逆数: IV高 → 不確実性 → スコア低）────────────
    iv_rank = vix_info.get("iv_rank")
    if iv_rank is not None:
        iv_score = max(0, min(100, 100 - iv_rank))
        raw_scores.append(("iv_rank", iv_score, 0.20))
        components["iv_rank"] = {
            "value": iv_rank,
            "score": round(iv_score, 1),
            "weight": 0.20,
        }

    # ── PCR スコア（逆数: PCR高 → 悲観 → スコア低）────────────────
    spy_data = option_data.get("spy", {})
    pcr      = spy_data.get("pcr_oi")
    if pcr is not None:
        # PCR 0.4 → score 100 / PCR 1.5 → score 0
        pcr_score = max(0, min(100, (1.5 - pcr) / 1.1 * 100))
        raw_scores.append(("pcr", pcr_score, 0.30))
        components["pcr"] = {
            "value": round(pcr, 3),
            "score": round(pcr_score, 1),
            "weight": 0.30,
        }

    # ── IV スキュー スコア（逆数: スキュー大 → 下落恐怖 → スコア低）────
    iv_skew = spy_data.get("iv_skew")
    if iv_skew is not None:
        # スキュー 0% → score 100 / スキュー 10% → score 0
        skew_score = max(0, min(100, (10 - iv_skew) / 10 * 100))
        raw_scores.append(("iv_skew", skew_score, 0.15))
        components["iv_skew"] = {
            "value": round(iv_skew, 2),
            "score": round(skew_score, 1),
            "weight": 0.15,
        }
    # ── [Phase US-4] term structure スコアを追加 ──────────────
    ts_data = vix_data.get("term_structure", {})
    ts_score = ts_data.get("term_score")
    if ts_score is not None:
        raw_scores.append(("term_structure", ts_score, TERM_SCORE_WEIGHT))
        components["term_structure"] = {
            "value": ts_data.get("ts_ratio_9d"),
            "score": round(ts_score, 1),
            "shape": ts_data.get("shape", "unknown"),
            "weight": TERM_SCORE_WEIGHT,
        }

    # ── [Phase US-4] SKEW スコアを追加 ────────────────────────
    skew_data = vix_data.get("skew", {})
    skew_score = skew_data.get("skew_score")
    if skew_score is not None:
        raw_scores.append(("skew", skew_score, 0.10))
        components["skew"] = {
            "value":  skew_data.get("value"),
            "score":  round(skew_score, 1),
            "level":  skew_data.get("level", "normal"),
            "weight": 0.10,
        }

    if not raw_scores:
        return {"score": 50.0, "label": "データ不足", "components": {}, "signal": "neutral"}

    # 加重平均
    total_weight = sum(w for _, _, w in raw_scores)
    score = sum(s * w for _, s, w in raw_scores) / total_weight if total_weight > 0 else 50
    score = round(score, 1)

    # ラベル
    if score < 20:
        label  = "😱 極度の恐怖（逆張り買いの好機）"
        signal = "contrarian_buy"
    elif score < 40:
        label  = "😨 恐怖"
        signal = "contrarian_buy"
    elif score < 60:
        label  = "😐 中立"
        signal = "neutral"
    elif score < 80:
        label  = "😊 楽観"
        signal = "contrarian_sell"
    else:
        label  = "🤑 極度の楽観（天井注意）"
        signal = "contrarian_sell"

    return {
        "score":      score,
        "label":      label,
        "signal":     signal,
        "components": components,
    }


# ==============================================================
# BLOCK 3: composite_score 補正量計算
# ==============================================================

def calc_options_adjustments(sentiment: dict, sector_results: dict) -> dict:
    """
    センチメントスコアから業種 composite_score への補正量を計算する。

    恐怖時 → ディフェンシブ優位 / 楽観時 → 景気敏感優位
    """
    score  = sentiment.get("score", 50)
    signal = sentiment.get("signal", "neutral")
    adjustments = {}

    if signal == "contrarian_buy":
        # 恐怖時: ディフェンシブに追い風、景気敏感に逆風
        magnitude = min(5.0, (40 - score) / 40 * 5)   # 最大5pt
        for code in FEAR_TAILWIND_SECTORS:
            if code in sector_results:
                adjustments[code] = adjustments.get(code, 0) + round(magnitude, 1)
        for code in FEAR_HEADWIND_SECTORS:
            if code in sector_results:
                adjustments[code] = adjustments.get(code, 0) - round(magnitude * 0.6, 1)

    elif signal == "contrarian_sell":
        # 楽観時: 景気敏感に追い風、ディフェンシブに逆風
        magnitude = min(3.0, (score - 60) / 40 * 3)   # 最大3pt
        for code in GREED_TAILWIND_SECTORS:
            if code in sector_results:
                adjustments[code] = adjustments.get(code, 0) + round(magnitude, 1)
        for code in GREED_HEADWIND_SECTORS:
            if code in sector_results:
                adjustments[code] = adjustments.get(code, 0) - round(magnitude * 0.5, 1)

    # ゼロをフィルタ
    return {k: round(v, 1) for k, v in adjustments.items() if v != 0}


# ==============================================================
# BLOCK 4: テキスト整形
# ==============================================================

def format_options_report(result: dict) -> str:
    """週次レポート・アラート向けテキストを生成する。"""
    sentiment = result.get("sentiment", {})
    vix       = result.get("vix_data", {}).get("vix", {})
    spy       = result.get("option_data", {}).get("spy", {})

    score  = sentiment.get("score", 50)
    label  = sentiment.get("label", "N/A")
    signal = sentiment.get("signal", "neutral")

    lines = [
        "## 📊 オプション市場センチメント",
        "",
        f"**センチメントスコア**: {score:.0f}/100 — {label}",
        "",
    ]

    # VIX
    if vix.get("current"):
        vix_val  = vix["current"]
        iv_rank  = vix.get("iv_rank", 0)
        trend_s  = {"rising": "↗上昇中", "falling": "↘低下中", "stable": "→安定"}.get(vix.get("trend",""), "")
        lines.append(f"  VIX: {vix_val:.1f} ({trend_s}) | IV Rank: {iv_rank:.0f}%ile")

    # PCR
    if spy.get("pcr_oi"):
        pcr = spy["pcr_oi"]
        pcr_icon = "🔴 悲観過多" if pcr > PCR_FEAR_THRESH else (
                   "🟢 楽観過多" if pcr < PCR_GREED_THRESH else "⚪ 中立")
        lines.append(f"  PCR(OI): {pcr:.3f} {pcr_icon}")

    # IV Skew
    if spy.get("iv_skew") is not None:
        lines.append(f"  IVスキュー: {spy['iv_skew']:.1f}% "
                     f"({'プット保険需要高' if spy['iv_skew'] > 5 else '正常範囲'})")

    lines.append("")

    # シグナル解釈
    if signal == "contrarian_buy":
        lines.append("  💡 逆張り示唆: 市場の悲観が過剰です。底値圏の可能性があります。")
        lines.append("     ディフェンシブ業種が相対的に優位になる傾向があります。")
    elif signal == "contrarian_sell":
        lines.append("  ⚠️ 天井注意: 市場の楽観が過剰です。利確の検討も考えられます。")
        lines.append("     景気敏感株のモメンタムは継続しますが上値余地に注意が必要です。")
    else:
        lines.append("  ➡️ センチメント中立: 特別なシグナルなし。")

    return "\n".join(lines)


# ==============================================================
# BLOCK 5: キャッシュ管理
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
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_options_monitor(cfg, sector_results: Optional[dict] = None) -> dict:
    """
    オプション市場データを取得してセンチメントスコアと補正量を返す。

    stock_analyzer_main.py から日次で呼び出す:
        _om = _load_local_module("options_monitor", ...)
        om_result = _om.run_options_monitor(cfg, results.get("sectors",{}))
        results["options_monitor"] = om_result
        # セクタースコアに補正を適用
        for code, delta in om_result.get("adjustments",{}).items():
            if code in results["sectors"] and delta != 0:
                results["sectors"][code]["composite_score"] = round(
                    max(-30, min(100,
                        results["sectors"][code].get("composite_score",0) + delta)), 1)
                results["sectors"][code]["options_adjustment"] = delta

    Returns:
        {
          "option_data":  {ticker: option_chain_result},
          "vix_data":     {vix/vvix: vix_info},
          "sentiment":    {"score", "label", "signal", "components"},
          "adjustments":  {sector_code: delta},
          "summary_text": str,
          "timestamp":    str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📊 オプション市場センチメント分析開始...")

    cached = _load_cache(cfg)
    if cached:
        s = cached.get("sentiment", {})
        print(f"    ✅ キャッシュ使用（センチメント: {s.get('score',50):.0f}/100 {s.get('label','')}）")
        return cached

    # VIX データ取得
    print("    VIX / VVIX 取得中...")
    vix_data = _fetch_vix_data()
    # ── [Phase US-4] VIX term structure & SKEW ───────────────
    # 2026-07-25: BLOCK US-4 の3フラグ（ENABLE_VIX_TERM_STRUCTURE /
    # ENABLE_SKEW_INDEX / ENABLE_IV_SPIKE_DETECT）を配線。実装は揃っていたのに
    # 有効フラグが誰にも読まれず、False にしても外部取得が止まらなかった。
    # 無効時は空dictを入れる（下流は .get() で読むため式は壊れない）。
    _ts_on   = getattr(cfg, "ENABLE_VIX_TERM_STRUCTURE", True)
    _skew_on = getattr(cfg, "ENABLE_SKEW_INDEX", True)
    if _ts_on or _skew_on:
        print("    VIX term structure / SKEW 取得中...")
    ts_data   = _fetch_vix_term_structure() if _ts_on else {}
    skew_data = _fetch_skew_index() if _skew_on else {}
    vix_data["term_structure"] = ts_data
    vix_data["skew"]           = skew_data

    # crisis_signal 即時判定（バックワーデーション = リスクオフ強）
    if ts_data.get("crisis_signal"):
        shape = ts_data.get("shape", "")
        vix9d = ts_data.get("vix9d", "N/A")
        vix_c = ts_data.get("vix", "N/A")
        print(f"    ⚠️  [US-4] VIX バックワーデーション検知: "
              f"VIX9D={vix9d} / VIX={vix_c} (ratio={ts_data.get('ts_ratio_9d','?')})")
    else:
        ts_shape = ts_data.get("shape", "unknown")
        ts_score = ts_data.get("term_score", 50)
        print(f"    term structure: {ts_shape} (score={ts_score:.0f})")

    skew_val = skew_data.get("value")
    if skew_val:
        print(f"    SKEW: {skew_val:.0f} ({skew_data.get('level','?')})")
    if vix_data.get("vix"):
        vix_val = vix_data["vix"]["current"]
        iv_rank = vix_data["vix"]["iv_rank"]
        print(f"    VIX: {vix_val:.1f} | IV Rank: {iv_rank:.0f}%ile")

    # オプションチェーン取得（SPY / QQQ / EWJ）
    print("    オプションチェーン取得中...")
    option_data = {}
    for name, ticker in OPTIONS_TICKERS.items():
        data = _fetch_option_chain(ticker)
        if data:
            option_data[name] = data
            pcr = data.get("pcr_oi")
            if pcr:
                icon = "🔴" if pcr > PCR_FEAR_THRESH else ("🟢" if pcr < PCR_GREED_THRESH else "⚪")
                print(f"    {name.upper()} PCR: {pcr:.3f} {icon}")
        time.sleep(FETCH_DELAY)

    # センチメントスコア算出
    sentiment = calc_sentiment_score(option_data, vix_data)
    print(f"    センチメント: {sentiment['score']:.0f}/100 — {sentiment['label']}")

    # composite_score 補正量
    adjustments = {}
    if sector_results:
        adjustments = calc_options_adjustments(sentiment, sector_results)
        if adjustments:
            adj_str = " / ".join(f"{k}:{v:+.0f}" for k, v in list(adjustments.items())[:4])
            print(f"    業種補正: {adj_str}")

    # ── [Phase US-4] IV急騰銘柄検知 ─────────────────────────
    iv_spikes = []
    if sector_results:
        # sector_results から US screener 通過銘柄を取得（パスされた場合）
        us_tickers = []
        if isinstance(sector_results, dict) and "_us_screener_tickers" in sector_results:
            us_tickers = sector_results["_us_screener_tickers"][:10]
        if us_tickers and getattr(cfg, "ENABLE_IV_SPIKE_DETECT", True):
            print(f"    IV急騰チェック: {len(us_tickers)}銘柄...")
            # 閾値も config を正本にする（従来はモジュール定数が既定値を
            # 固定しており、config.IV_SPIKE_THRESHOLD が効かなかった）。
            iv_spikes = detect_iv_spikes(
                us_tickers,
                threshold=int(getattr(cfg, "IV_SPIKE_THRESHOLD",
                                      IV_SPIKE_THRESHOLD)),
            )
            if iv_spikes:
                print(f"    ⚡ IV急騰銘柄: {[s['ticker'] for s in iv_spikes[:3]]}")

    result = {
        "option_data":  option_data,
        "vix_data":     vix_data,
        "sentiment":    sentiment,
        "adjustments":  adjustments,
        "term_structure":   ts_data,          # ★ US-4追加
        "skew":             skew_data,        # ★ US-4追加
        "iv_spikes":        iv_spikes,        # ★ US-4追加
        "term_score":       ts_data.get("term_score", 50.0),  # ★ signal_aggregator用
        "crisis_signal":    ts_data.get("crisis_signal", False),  # ★ inference_engine用
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["summary_text"] = format_options_report(result)

    _save_cache(cfg, result)
    print(f"    ✅ オプション分析完了: signal={sentiment['signal']}")
    return result
