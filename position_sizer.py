# ==============================================================
# position_sizer.py  v1.0  (3-3 / C-3)
# ATRベースのポジションサイズ計算
# ==============================================================
# 【役割】
#   「総資産の1%を1回の取引リスクとして許容する」という
#   プロのポジションサイジング原則を自動算出する。
#
# 【計算ロジック】
#   ATR（Average True Range）= 過去14日の価格変動幅の平均
#
#   ポジションサイズ（株数）=
#     (総資産 × リスク許容率) / (ATR × ATRマルチプライヤー)
#
#   逆指値価格 = 現在価格 - (ATR × ATRマルチプライヤー)
#   (ATRマルチプライヤー = デフォルト2.0 = 2ATR下に逆指値)
#
# 【計算例】
#   総資産1000万円、リスク1%、ATR=250円、マルチプライヤー2.0
#   → 1回の最大損失 = 1000万 × 0.01 = 10万円
#   → 損失幅 = 250円 × 2.0 = 500円/株
#   → ポジションサイズ = 10万 ÷ 500 = 200株
#   → 投資額 = 200株 × 現在株価
#
# 【出力スキーマ（run_position_sizer の戻り値）】
#   {
#     "total_assets_jpy":  float,   # 入力した総資産額
#     "risk_per_trade_pct": float,  # リスク許容率（デフォルト1%）
#     "positions": [
#       {
#         "ticker":          str,
#         "name":            str,
#         "current_price":   float,
#         "atr14":           float,   # 14日ATR
#         "atr_pct":         float,   # ATR / 現在価格 × 100
#         "stop_loss_price": float,   # 逆指値価格
#         "stop_loss_pct":   float,   # 逆指値距離%
#         "suggested_shares": int,    # 推奨株数
#         "suggested_lot":   int,     # 推奨単元数（100株単位）
#         "position_value":  float,   # 投資額（円）
#         "position_pct":    float,   # 総資産に対する比率%
#         "risk_amount":     float,   # 実際のリスク額（円）
#         "risk_reward_ratio": float, # リスクリワード（target設定時）
#         "sector_score":    float,   # セクタースコア（sector_results から）
#       },
#       ...
#     ],
#     "summary_text":  str,
#     "timestamp":     str,
#   }
#
# 【依存】yfinance / config.py / portfolio_holdings.csv
# 【呼出】from position_sizer import run_position_sizer
# ==============================================================

from __future__ import annotations

import csv
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── デフォルトパラメータ ─────────────────────────────────────
ATR_PERIOD         = 14     # ATR計算期間（日）

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'PS_ATR_MULTIPLIER': 'ATR_MULTIPLIER',
    'PS_CONVICTION_THRESHOLDS': 'CONVICTION_THRESHOLDS',
    'PS_CONVICTION_MULTIPLIERS': 'CONVICTION_MULTIPLIERS',
    'B4_POSITION_SIZER_ATR_PERIOD': 'ATR_PERIOD',
    'B5_POSITION_SIZER_FETCH_DELAY': 'FETCH_DELAY',
    'B6_POSITION_SIZER_RISK_PCT_DEFAULT': 'RISK_PCT_DEFAULT',
    'B6_POSITION_SIZER_MIN_POSITION_PCT': 'MIN_POSITION_PCT',
    'B6_POSITION_SIZER_MAX_POSITION_PCT': 'MAX_POSITION_PCT',
    'B6_POSITION_SIZER_LOT_SIZE_JP': 'LOT_SIZE_JP',
    'B6_POSITION_SIZER_CONVICTION_PCT_RANGE': 'CONVICTION_PCT_RANGE',
    'B6_POSITION_SIZER_REGIME_SIZE_MATRIX': 'REGIME_SIZE_MATRIX',
    'B6_POSITION_SIZER_TRADING_DAYS_V2': '_TRADING_DAYS_V2',
}

ATR_MULTIPLIER     = 2.0    # 逆指値 = 現在価格 - ATR × この倍率
RISK_PCT_DEFAULT   = 0.01   # 1トレードあたりのリスク許容率（1%）
MIN_POSITION_PCT   = 0.02   # 最小ポジション比率（2%）
MAX_POSITION_PCT   = 0.15   # 最大ポジション比率（15%）
LOT_SIZE_JP        = 100    # 日本株の単元株数
FETCH_DELAY        = 0.3    # yfinance レート制限

# [I-4] 確信度レベル設定
CONVICTION_THRESHOLDS = [90, 75, 60, 45]   # L5/L4/L3/L2の下限（L1は45未満）
CONVICTION_MULTIPLIERS = {5: 1.5, 4: 1.0, 3: 0.7, 2: 0.4, 1: 0.0}
CONVICTION_PCT_RANGE = {
    5: (0.10, 0.15),   # L5: 10〜15%
    4: (0.06, 0.10),   # L4: 6〜10%
    3: (0.03, 0.06),   # L3: 3〜6%
    2: (0.01, 0.03),   # L2: 1〜3%
    1: (0.00, 0.00),   # L1: 見送り
}
# ポートフォリオ制約
MAX_SINGLE_STOCK_PCT  = 0.15   # 単一銘柄上限15%
MAX_SINGLE_SECTOR_PCT = 0.25   # 単一業種上限25%
MAX_LEVEL5_TOTAL_PCT  = 0.40   # Level5合計上限40%

# regime_state → サイジング係数
REGIME_SIZE_MATRIX = {
    0:  1.0,    # リスクオン強 → フルサイズ
    1:  0.75,   # 移行期       → 75%
    2:  0.50,   # リスクオフ   → 50%
    3:  0.0,    # 危機          → 新規停止
}

# ==============================================================
# BLOCK 1: ATR 計算
# ==============================================================

def calc_atr(ticker: str, period: int = ATR_PERIOD) -> Optional[dict]:
    """
    yfinance から ATR（Average True Range）を計算する。

    True Range = max(
        High - Low,
        |High - 前日Close|,
        |Low  - 前日Close|
    )
    ATR = 過去 period 日の True Range の単純平均

    Returns:
        {
          "atr14":         float,   # ATR（円 or ドル）
          "atr_pct":       float,   # ATR / 現在価格 × 100
          "current_price": float,
          "price_52w_high": float,
          "price_52w_low":  float,
        }
        取得失敗時は None
    """
    try:
        import yf_cache as yf

        end   = datetime.now()
        start = end - timedelta(days=period * 3)  # 余裕を持って取得

        hist = yf.Ticker(ticker).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=True,
        )
        if hist is None or len(hist) < period + 1:
            return None

        highs  = hist["High"].tolist()
        lows   = hist["Low"].tolist()
        closes = hist["Close"].tolist()

        # True Range を計算
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i]  - lows[i],
                abs(highs[i]  - closes[i-1]),
                abs(lows[i]   - closes[i-1]),
            )
            tr_list.append(tr)

        if len(tr_list) < period:
            return None

        atr14 = round(sum(tr_list[-period:]) / period, 2)
        current_price = round(closes[-1], 2)
        atr_pct = round(atr14 / current_price * 100, 2) if current_price > 0 else None

        return {
            "atr14":          atr14,
            "atr_pct":        atr_pct,
            "current_price":  current_price,
            "price_52w_high": round(max(highs[-252:]), 2) if len(highs) >= 10 else None,
            "price_52w_low":  round(min(lows[-252:]), 2)  if len(lows) >= 10 else None,
        }
    except Exception:
        return None

# ==============================================================
# BLOCK 0: [I-4] 確信度スコア計算
# ==============================================================

def calc_conviction_score(candidate: dict, cfg=None) -> dict:
    """
    [I-4] composite_score × 複数ファクターから確信度スコア（0〜100）を算出する。

    conviction_score =
        composite_score           × 0.20
        + earnings_accel_score    × 0.20  （I-1: EPS加速度）
        + business_quality_score  × 0.15  （P-2: 事業モデル質）
        + conservatism_score      × 0.15  （I-2: 経営者保守度）
        + activist_score          × 0.10  （M-3: アクティビスト候補）
        + governance_score        × 0.10  （M-2: ガバナンス / データなし時は中立）
        + ceo_score               × 0.10  （I-3: 経営者スコア）

    Args:
        candidate : screener の candidate 辞書
        cfg       : config モジュール（重みのカスタマイズ用）

    Returns:
        {
          "conviction_score": float,   # 0〜100
          "conviction_level": int,     # 1〜5
          "conviction_label": str,
          "components":       dict,    # 各ファクターのスコアと重み
          "recommended_pct":  float,   # 推奨ポジション比率（上限値）
        }
    """
    # ── 各ファクターのスコア取得 ──────────────────────────
    composite    = float(candidate.get("composite_score", 50) or 50)

    # I-1: acc_bonus (-10〜+10) → 0〜100に正規化
    acc_bonus    = float(candidate.get("acc_bonus", 0) or 0)
    accel_score  = max(0.0, min(100.0, 50.0 + acc_bonus * 5))

    # P-2: bq_bonus (-20〜+20) → 0〜100に正規化
    bq_bonus     = float(candidate.get("bq_bonus", 0) or 0)
    bq_score     = max(0.0, min(100.0, 50.0 + bq_bonus * 2.5))

    # I-2: conservatism_score（0〜100のまま）
    conservatism = float(candidate.get("conservatism_score", 50) or 50)

    # M-3: activist_score（0〜100のまま）
    activist     = float(candidate.get("activist_score", 0) or 0)

    # M-2: governance（暫定: activist_score × 0.8 で近似 / M-2実装後に更新）
    governance   = activist * 0.8

    # I-3: ceo_score（0〜100のまま）
    ceo          = float(candidate.get("ceo_score", 50) or 50)

    # ── 重み（config からカスタマイズ可能）──────────────
    if cfg:
        w_composite   = getattr(cfg, "CONVICTION_W_COMPOSITE",   0.20)
        w_accel       = getattr(cfg, "CONVICTION_W_ACCEL",        0.20)
        w_bq          = getattr(cfg, "CONVICTION_W_BQ",           0.15)
        w_conservatism= getattr(cfg, "CONVICTION_W_CONSERVATISM", 0.15)
        w_activist    = getattr(cfg, "CONVICTION_W_ACTIVIST",     0.10)
        w_governance  = getattr(cfg, "CONVICTION_W_GOVERNANCE",   0.10)
        w_ceo         = getattr(cfg, "CONVICTION_W_CEO",          0.10)
    else:
        w_composite, w_accel, w_bq = 0.20, 0.20, 0.15
        w_conservatism, w_activist = 0.15, 0.10
        w_governance, w_ceo = 0.10, 0.10

    # 重みの正規化（合計が1.0になるよう）
    total_w = w_composite + w_accel + w_bq + w_conservatism + w_activist + w_governance + w_ceo
    if total_w > 0:
        w_composite    /= total_w
        w_accel        /= total_w
        w_bq           /= total_w
        w_conservatism /= total_w
        w_activist     /= total_w
        w_governance   /= total_w
        w_ceo          /= total_w

    conviction_score = round(
        composite    * w_composite
        + accel_score  * w_accel
        + bq_score     * w_bq
        + conservatism * w_conservatism
        + activist     * w_activist
        + governance   * w_governance
        + ceo          * w_ceo,
        1,
    )
    conviction_score = max(0.0, min(100.0, conviction_score))

    # ── 確信度レベル判定 ──────────────────────────────────
    # 2026-07-25: BLOCK I-4 の ENABLE_CONVICTION_SIZING を配線。
    # CONVICTION_THRESHOLDS は読まれていたのに、機能そのものの有効フラグだけが
    # 誰にも読まれず、False にしても確信度別サイジングが効き続けていた。
    # 無効時は全銘柄を Level3（中確信・通常扱い）に固定し、確信度による
    # ポジション比率の増減を行わない。
    if cfg is not None and not getattr(cfg, "ENABLE_CONVICTION_SIZING", True):
        conviction_score = 50.0
        thresholds = [101, 101, 0, 0]     # 常に Level3 になる閾値
    else:
        thresholds = getattr(cfg, "CONVICTION_THRESHOLDS", CONVICTION_THRESHOLDS) if cfg else CONVICTION_THRESHOLDS
    if   conviction_score >= thresholds[0]: level = 5   # 90以上
    elif conviction_score >= thresholds[1]: level = 4   # 75以上
    elif conviction_score >= thresholds[2]: level = 3   # 60以上
    elif conviction_score >= thresholds[3]: level = 2   # 45以上
    else:                                   level = 1   # 45未満

    label_map = {
        5: "🌟 Level5（最高確信・集中投資）",
        4: "📈 Level4（高確信・通常投資）",
        3: "➡️ Level3（中確信・少額投資）",
        2: "⚠️  Level2（低確信・最小投資）",
        1: "🔴 Level1（投資見送り）",
    }

    # 推奨ポジション比率（上限値を返す）
    pct_range = CONVICTION_PCT_RANGE.get(level, (0.0, 0.0))
    recommended_pct = pct_range[1]   # 上限値

    return {
        "conviction_score": conviction_score,
        "conviction_level": level,
        "conviction_label": label_map[level],
        "recommended_pct":  recommended_pct,
        "components": {
            "composite_score":    {"score": round(composite, 1),    "weight": round(w_composite,    3)},
            "accel_score":        {"score": round(accel_score, 1),  "weight": round(w_accel,        3)},
            "bq_score":           {"score": round(bq_score, 1),     "weight": round(w_bq,           3)},
            "conservatism_score": {"score": round(conservatism, 1), "weight": round(w_conservatism, 3)},
            "activist_score":     {"score": round(activist, 1),     "weight": round(w_activist,     3)},
            "governance_score":   {"score": round(governance, 1),   "weight": round(w_governance,   3)},
            "ceo_score":          {"score": round(ceo, 1),          "weight": round(w_ceo,          3)},
        },
    }


def _get_regime_multiplier(results: dict, cfg=None) -> tuple[float, int]:
    """
    regime_detector.py または economic_cycle から regime_state を取得し、
    サイジング係数を返す。

    Returns:
        (multiplier: float, regime_state: int)
    """
    # regime_detector.py が存在する場合はその state を参照
    regime_result = results.get("regime", {}) if results else {}
    if isinstance(regime_result, dict) and "current_state" in regime_result:
        state = int(regime_result["current_state"])
    else:
        # フォールバック: economic_cycle の phase から簡易推定
        ec = (results or {}).get("economic_cycle", {})
        phase_to_state = {"B": 0, "A": 1, "D": 2, "C": 3}
        state = phase_to_state.get(ec.get("phase", "B"), 0)

        # recession_score が高い場合は状態を悪化方向に補正
        bc = (results or {}).get("bond_commodity", {})
        rec_score = bc.get("recession_score", 0) if isinstance(bc, dict) else 0
        if rec_score >= 70 and state < 3:
            state = min(3, state + 1)

    matrix = getattr(cfg, "REGIME_SIZE_MATRIX", REGIME_SIZE_MATRIX) if cfg else REGIME_SIZE_MATRIX
    multiplier = matrix.get(state, 1.0)
    return multiplier, state

# ==============================================================
# BLOCK 2: ポジションサイズ計算
# ==============================================================

def calc_position_size(
    atr_data:        dict,
    total_assets:    float,
    risk_pct:        float = RISK_PCT_DEFAULT,
    atr_multiplier:  float = ATR_MULTIPLIER,
    lot_size:        int   = LOT_SIZE_JP,
    target_price:    Optional[float] = None,
    conviction_data: Optional[dict] = None,   # ★ I-4追加
    regime_multiplier: float = 1.0,           # ★ I-4追加
) -> dict:
    """
    ATR データからポジションサイズを計算する。

    Args:
        atr_data:       calc_atr() の戻り値
        total_assets:   総資産額（円）
        risk_pct:       1トレードのリスク許容率（0.01 = 1%）
        atr_multiplier: 逆指値 = 現在価格 - ATR × この倍率
        lot_size:       単元株数（日本株は100）
        target_price:   目標価格（指定時のみリスクリワード計算）

    Returns:
        計算結果辞書
    """
    price       = atr_data["current_price"]
    atr         = atr_data["atr14"]

    # 1トレードの最大リスク額
    risk_amount = total_assets * risk_pct

    # 1株あたりの損失幅
    loss_per_share = atr * atr_multiplier
    if loss_per_share <= 0:
        return {"error": "ATR計算エラー"}

    # 推奨株数（連続値）
    raw_shares = risk_amount / loss_per_share

    # ── [I-4] 確信度マルチプライヤーの適用 ──────────────────
    conviction_level = 4   # デフォルト（データなし時）
    conviction_score = 75.0
    if conviction_data:
        conviction_level = conviction_data.get("conviction_level", 4)
        conviction_score = conviction_data.get("conviction_score", 75.0)
    cv_multiplier = CONVICTION_MULTIPLIERS.get(conviction_level, 1.0)

    # レジーム補正 × 確信度補正
    combined_multiplier = cv_multiplier * regime_multiplier
    raw_shares = raw_shares * combined_multiplier

    # Level1（見送り）は株数ゼロ
    if conviction_level == 1:
        raw_shares = 0

    # 日本株は単元（100株）単位に切り捨て
    suggested_lot    = max(1, int(raw_shares / lot_size)) if raw_shares > 0 else 0
    suggested_shares = suggested_lot * lot_size

    # ポジション価値と比率
    position_value = suggested_shares * price
    position_pct   = round(position_value / total_assets * 100, 1)

    # 上限・下限クランプ
    if position_pct > MAX_POSITION_PCT * 100:
        suggested_lot    = max(1, int(total_assets * MAX_POSITION_PCT / price / lot_size))
        suggested_shares = suggested_lot * lot_size
        position_value   = suggested_shares * price
        position_pct     = round(position_value / total_assets * 100, 1)
        capped = True
    elif position_pct < MIN_POSITION_PCT * 100:
        suggested_lot    = max(1, int(total_assets * MIN_POSITION_PCT / price / lot_size))
        suggested_shares = suggested_lot * lot_size
        position_value   = suggested_shares * price
        position_pct     = round(position_value / total_assets * 100, 1)
        capped = True
    else:
        capped = False

    # 実際リスク額（クランプ後）
    actual_risk    = suggested_shares * loss_per_share
    actual_risk_pct = round(actual_risk / total_assets * 100, 2)

    # 逆指値価格
    stop_loss      = round(price - loss_per_share, 1)
    stop_loss_pct  = round(loss_per_share / price * 100, 1)

    # リスクリワード
    rrr = None
    if target_price and target_price > price:
        reward = target_price - price
        rrr    = round(reward / loss_per_share, 2)

    return {
        "stop_loss_price":    stop_loss,
        "stop_loss_pct":      stop_loss_pct,
        "suggested_shares":   suggested_shares,
        "suggested_lot":      suggested_lot,
        "position_value":     round(position_value, 0),
        "position_pct":       position_pct,
        "risk_amount":        round(actual_risk, 0),
        "risk_pct_actual":    actual_risk_pct,
        "risk_reward_ratio":  rrr,
        "capped":             capped,
        "loss_per_share":     round(loss_per_share, 1),
        # [I-4] 確信度追加フィールド
        "conviction_level":   conviction_level,
        "conviction_score":   conviction_score,
        "conviction_multiplier": combined_multiplier,
        "regime_multiplier":  regime_multiplier,
    }


# ==============================================================
# BLOCK 2b: ポジションサイジング v2（Wave R1-5 / E5）
# ==============================================================
# 設計正本: 要件定義書_統合改修_20260705.md §R1-5（確定アルゴリズム・変更禁止）
#   1) 基礎サイズ（v1と同じ）: risk_amount = 総資産×1% ; qty = risk / (2×ATR14)
#   2) conviction係数: × CONVICTION_MULT[conviction]（無審議銘柄は3扱い）
#   3) 相関ペナルティ: 既存ポートフォリオとの平均60日相関 ρ̄>0.6 → ×0.7
#   4) ポートフォリオボラ制約: σ_p=sqrt(wᵀΣw) > VOL_TARGET_ANNUAL(0.15=D-15)
#      なら超過分だけ比例縮小
#   5) 上限: 1銘柄 ≤ 総資産×10% / カジノ枠戦略 ≤ カジノ枠×25%
# 出力は「推奨qty＋各ステップの調整内訳」（健太が調整根拠を追えること）。
# 境界: 相関行列が特異/データ欠損時はステップ3,4をスキップし警告。
# v1（calc_position_size）は残置・既存呼び出しに影響なし。
# ==============================================================

_TRADING_DAYS_V2 = 252


def _corr60(a: list, b: list) -> Optional[float]:
    """直近60日リターンのピアソン相関。データ不足/分散ゼロは None。"""
    n = min(len(a), len(b), 60)
    if n < 20:
        return None
    x, y = a[-n:], b[-n:]
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((v - mx) ** 2 for v in x)
    vy = sum((v - my) ** 2 for v in y)
    if vx <= 0 or vy <= 0:
        return None
    cov = sum((p - mx) * (q - my) for p, q in zip(x, y))
    return cov / (vx * vy) ** 0.5


def _portfolio_vol_annual(weights: list, returns: list) -> Optional[float]:
    """
    σ_p = sqrt(wᵀΣw)（Σ=直近60日の日次共分散を年率化）。
    returns: 各資産の日次リターン列（長さはmin60に揃える）。
    """
    n_assets = len(weights)
    if n_assets == 0 or n_assets != len(returns):
        return None
    n = min([len(r) for r in returns] + [60])
    if n < 20:
        return None
    series = [r[-n:] for r in returns]
    means = [sum(s) / n for s in series]
    var_p = 0.0
    for i in range(n_assets):
        for j in range(n_assets):
            cov_ij = sum((series[i][k] - means[i]) * (series[j][k] - means[j])
                         for k in range(n)) / (n - 1)
            var_p += weights[i] * weights[j] * cov_ij
    if var_p < 0:
        return None
    return math.sqrt(var_p * _TRADING_DAYS_V2)


def calc_position_size_v2(
    total_assets:      float,
    price:             float,
    atr14:             float,
    conviction:        Optional[int] = None,     # B部門conviction(1-5)。Noneは3扱い
    candidate_returns: Optional[list] = None,    # 候補銘柄の日次リターン列（60日）
    portfolio_returns: Optional[dict] = None,    # {ticker: 日次リターン列}
    portfolio_weights: Optional[dict] = None,    # {ticker: 総資産比weight}
    is_casino:         bool = False,
    lot_size:          int = LOT_SIZE_JP,
    cfg=None,
) -> dict:
    """
    確定5ステップのサイジング。Returns:
      {"qty", "position_value", "position_pct", "steps": [...], "warnings": [...]}
    steps の各要素: {"step", "label", "qty", "note"}（注文票reason欄に転記可能）
    """
    conv_mult_table = getattr(cfg, "CONVICTION_MULT",
                              {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.25, 5: 1.5})
    corr_th = getattr(cfg, "CORR_PENALTY_TH", 0.6)
    corr_mult = getattr(cfg, "CORR_PENALTY_MULT", 0.7)
    vol_target = getattr(cfg, "VOL_TARGET_ANNUAL", 0.15)
    max_single_pct = getattr(cfg, "MAX_SINGLE_POS_PCT", 10.0) / 100.0
    casino_single_pct = getattr(cfg, "CASINO_SINGLE_POS_PCT", 25.0) / 100.0
    casino_pct = getattr(cfg, "CASINO_BUDGET_PCT", 15.0) / 100.0
    casino_cap = getattr(cfg, "CASINO_BUDGET_CAP_YEN", 2_000_000)

    steps, warnings = [], []
    if price <= 0 or atr14 <= 0 or total_assets <= 0:
        return {"error": "入力不正（price/atr14/total_assets は正の値）",
                "qty": 0, "steps": [], "warnings": []}

    # ── 1) 基礎サイズ ──
    risk_amount = total_assets * 0.01
    qty = risk_amount / (2.0 * atr14)
    steps.append({"step": 1, "label": "基礎サイズ(リスク1%/2ATR)",
                  "qty": round(qty, 1),
                  "note": f"risk={risk_amount:,.0f} ATR14={atr14:.1f}"})

    # ── 2) conviction係数 ──
    level = conviction if conviction in conv_mult_table else 3
    mult = conv_mult_table[level]
    qty *= mult
    steps.append({"step": 2, "label": f"conviction={level} (×{mult})",
                  "qty": round(qty, 1),
                  "note": "無審議銘柄は3扱い" if conviction is None else ""})

    # ── 3) 相関ペナルティ ──
    rho_values = []
    if candidate_returns and portfolio_returns:
        for t, rets in portfolio_returns.items():
            c = _corr60(candidate_returns, rets)
            if c is not None:
                rho_values.append(c)
    if rho_values:
        rho_bar = sum(rho_values) / len(rho_values)
        if rho_bar > corr_th:
            qty *= corr_mult
            steps.append({"step": 3, "label": f"相関ペナルティ(ρ̄={rho_bar:.2f}>"
                          f"{corr_th} ×{corr_mult})", "qty": round(qty, 1),
                          "note": f"対象{len(rho_values)}銘柄"})
        else:
            steps.append({"step": 3, "label": f"相関ペナルティなし(ρ̄={rho_bar:.2f})",
                          "qty": round(qty, 1), "note": ""})
    else:
        warnings.append("step3スキップ: 相関データ不足（60日リターン未取得）")
        steps.append({"step": 3, "label": "相関ペナルティ: スキップ",
                      "qty": round(qty, 1), "note": "データ欠損"})

    # ── 4) ポートフォリオボラ制約 ──
    vol_checked = False
    if candidate_returns and portfolio_returns and portfolio_weights:
        tickers = [t for t in portfolio_returns if t in portfolio_weights]
        cand_w = qty * price / total_assets
        weights = [portfolio_weights[t] for t in tickers] + [cand_w]
        rets = [portfolio_returns[t] for t in tickers] + [candidate_returns]
        sigma_p = _portfolio_vol_annual(weights, rets)
        if sigma_p is not None:
            vol_checked = True
            if sigma_p > vol_target and sigma_p > 0:
                scale = vol_target / sigma_p
                qty *= scale
                steps.append({"step": 4,
                              "label": f"ボラ制約(σ_p={sigma_p:.1%}>"
                              f"{vol_target:.0%} ×{scale:.2f})",
                              "qty": round(qty, 1), "note": "超過分を比例縮小"})
            else:
                steps.append({"step": 4, "label": f"ボラ制約OK(σ_p={sigma_p:.1%})",
                              "qty": round(qty, 1), "note": ""})
    if not vol_checked:
        warnings.append("step4スキップ: 共分散計算不能（データ欠損/特異）")
        steps.append({"step": 4, "label": "ボラ制約: スキップ",
                      "qty": round(qty, 1), "note": "データ欠損"})

    # ── 5) 上限 ──
    cap_value = total_assets * max_single_pct
    cap_label = f"1銘柄≤総資産{max_single_pct:.0%}"
    if is_casino:
        casino_budget = min(total_assets * casino_pct, casino_cap)
        cap_value = min(cap_value, casino_budget * casino_single_pct)
        cap_label = f"カジノ枠×{casino_single_pct:.0%}(枠={casino_budget:,.0f})"
    if qty * price > cap_value:
        qty = cap_value / price
        steps.append({"step": 5, "label": f"上限クランプ({cap_label})",
                      "qty": round(qty, 1), "note": ""})
    else:
        steps.append({"step": 5, "label": f"上限OK({cap_label})",
                      "qty": round(qty, 1), "note": ""})

    # 単元株に切り捨て
    final_qty = int(qty / lot_size) * lot_size if lot_size > 1 else int(qty)
    value = final_qty * price
    return {
        "qty": final_qty,
        "position_value": round(value, 0),
        "position_pct": round(value / total_assets * 100, 2),
        "steps": steps,
        "warnings": warnings,
    }


# ==============================================================
# BLOCK 3: ポートフォリオ読み込み
# ==============================================================

def _load_portfolio(cfg) -> list:
    """portfolio_holdings.csv を読み込む。"""
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "portfolio_holdings.csv",
        Path(__file__).parent / "data" / "portfolio_holdings.csv",
        Path(__file__).parent / "portfolio_holdings.csv",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            holdings = []
            with open(p, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    t = row.get("ticker", "").strip()
                    if not t:
                        continue
                    holdings.append({
                        "ticker":       t,
                        "name":         row.get("name", t),
                        "sector_code":  row.get("sector_code", ""),
                        "market":       row.get("market", "JP"),
                        "quantity":     float(row.get("quantity", 0) or 0),
                        "avg_cost_jpy": float(row.get("avg_cost_jpy", 0) or 0),
                    })
            return holdings
        except Exception:
            pass
    return []


# ==============================================================
# BLOCK 4: テキスト整形
# ==============================================================

def _fmt_yen(v: float) -> str:
    if abs(v) >= 1e8:
        return f"{v/1e8:.1f}億円"
    elif abs(v) >= 1e4:
        return f"{v/1e4:.0f}万円"
    return f"{v:.0f}円"


def format_position_report(result: dict) -> str:
    """週次レポート・アラート向けテキストを生成する。"""
    total = result.get("total_assets_jpy", 0)
    risk  = result.get("risk_per_trade_pct", 1.0)
    positions = result.get("positions", [])

    lines = [
        "## 💼 ポジションサイジング（ATRベース）",
        "",
        f"**総資産**: {_fmt_yen(total)}  "
        f"**リスク許容**: {risk*100:.1f}%/トレード  "
        f"**ATR期間**: {ATR_PERIOD}日  "
        f"**倍率**: {ATR_MULTIPLIER}×",
        "",
        "| 銘柄 | 現在値 | 推奨株数 | 投資額 | 確信Lv | 推奨% |",
        "|------|--------|---------|--------|-------|------|",
    ]

    for pos in positions:
        if pos.get("error"):
            continue
        price  = pos.get("current_price", 0)
        shares = pos.get("suggested_shares", 0)
        val    = pos.get("position_value", 0)
        cap    = "⚠️" if pos.get("capped") else ""

        cv_level = pos.get("conviction_level", "-")
        cv_pct   = pos.get("recommended_pct", 0) or 0
        cv_label_short = {5: "L5🌟", 4: "L4📈", 3: "L3➡️", 2: "L2⚠️", 1: "L1🔴"}.get(cv_level, "?")
        lines.append(
            f"| {pos['name'][:14]}({pos['ticker']}) "
            f"| {price:,.0f}円 "
            f"| {shares:,}株{cap} "
            f"| {_fmt_yen(val)} "
            f"| {cv_label_short} "
            f"| {cv_pct*100:.0f}% |"
        )

    lines.append("")
    lines.append(
        "> 逆指値 = 現在価格 - ATR×2。推奨株数はリスク1%ルール基準。"
        "⚠️ は15%上限/2%下限でクランプ済み。"
    )

    return "\n".join(lines)


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_position_sizer(
    cfg,
    total_assets_jpy:  float,
    risk_pct:          float = RISK_PCT_DEFAULT,
    atr_multiplier:    float = ATR_MULTIPLIER,
    sector_results:    Optional[dict] = None,
    tickers_override:  Optional[list] = None,
    screener_result:   Optional[dict] = None,   # ★ I-4追加
    results:           Optional[dict] = None,   # ★ I-4追加（regime取得用）
) -> dict:
    """
    ポートフォリオ全銘柄のポジションサイズを計算する。

    Args:
        cfg:               config モジュール
        total_assets_jpy:  総資産額（円）。config.TOTAL_ASSETS_JPY があればそちらを使う
        risk_pct:          リスク許容率（デフォルト 0.01 = 1%）
        atr_multiplier:    逆指値 = 現在価格 - ATR × この倍率
        sector_results:    results["sectors"]（セクタースコア付与用）
        tickers_override:  ポートフォリオCSV以外のティッカーを使う場合

    Returns:
        ポジションサイジング結果 dict
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    # config からの総資産上書き
    total_assets = getattr(cfg, "TOTAL_ASSETS_JPY", total_assets_jpy)

    # ── [I-4] レジーム係数を取得 ─────────────────────────────
    regime_multiplier, regime_state = _get_regime_multiplier(results, cfg)
    if regime_state == 3:
        print(f"    ⚠️  [I-4] 危機レジーム検知（state={regime_state}）: 新規投資係数=0.0")
    elif regime_multiplier < 1.0:
        print(f"    📊 [I-4] レジーム補正: state={regime_state} → サイズ×{regime_multiplier}")

    # screener 結果から conviction_score を銘柄ごとに取得
    conviction_map: dict[str, dict] = {}
    if screener_result:
        for c in screener_result.get("candidates", []):
            tk = c.get("ticker", "")
            if tk:
                conviction_map[tk] = calc_conviction_score(c, cfg)

    print(f"\n  💼 ポジションサイジング開始（総資産: {_fmt_yen(total_assets)}）...")

    # 対象銘柄を決定
    if tickers_override:
        holdings = [{"ticker": t, "name": t, "sector_code": "", "market": "JP", "quantity": 0, "avg_cost_jpy": 0}
                    for t in tickers_override]
    else:
        holdings = _load_portfolio(cfg)

    if not holdings:
        print("    ⚠️  portfolio_holdings.csv が見つかりません")
        return {
            "skipped":          True,
            "total_assets_jpy": total_assets,
            "positions":        [],
            "summary_text":     "💼 ポジションサイジング: portfolio_holdings.csv なし",
            "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    print(f"    対象: {len(holdings)}銘柄")

    positions = []
    for h in holdings:
        ticker = h["ticker"]
        print(f"      {ticker} ATR取得中...", end=" ")

        atr_data = calc_atr(ticker)
        if not atr_data:
            print("取得失敗")
            positions.append({
                "ticker": ticker, "name": h["name"],
                "error": "ATR取得失敗",
            })
            time.sleep(FETCH_DELAY)
            continue

        # ★ 2026-06-11 バグ修正: 旧実装は未定義変数 _cv_data を参照して
        #   毎回 NameError。事前構築済みの conviction_map から取得する。
        _cv_data = conviction_map.get(ticker)
        sizing = calc_position_size(
            atr_data       = atr_data,
            total_assets   = total_assets,
            risk_pct       = risk_pct,
            atr_multiplier = atr_multiplier,
            lot_size       = 100 if h.get("market") == "JP" else 1,
            conviction_data    = _cv_data,       # ★ I-4追加
            regime_multiplier  = regime_multiplier, # ★ I-4追加
        )

        # セクタースコアを付与
        sector_score = None
        if sector_results and h.get("sector_code"):
            sec_data = sector_results.get(h["sector_code"], {})
            if isinstance(sec_data, dict):
                sector_score = sec_data.get("composite_score")

        position = {
            "ticker":         ticker,
            "name":           h["name"][:20],
            "sector_code":    h.get("sector_code", ""),
            "market":         h.get("market", "JP"),
            "quantity":       h.get("quantity", 0),
            "avg_cost_jpy":   h.get("avg_cost_jpy", 0),
            "sector_score":   sector_score,
            **atr_data,
            **sizing,
            # [I-4] 確信度フィールド
            "conviction_score": conviction_map.get(ticker, {}).get("conviction_score", 75.0),
            "conviction_level": conviction_map.get(ticker, {}).get("conviction_level", 4),
            "conviction_label": conviction_map.get(ticker, {}).get("conviction_label", ""),
            "recommended_pct":  conviction_map.get(ticker, {}).get("recommended_pct", 0.08),
            "regime_state":     regime_state,
        }
        positions.append(position)

        print(
            f"ATR={atr_data['atr14']:.0f}円 "
            f"逆指値={sizing.get('stop_loss_price', 0):.0f}円 "
            f"推奨{sizing.get('suggested_shares', 0):,}株"
        )
        time.sleep(FETCH_DELAY)

    result = {
        "total_assets_jpy":  total_assets,
        "risk_per_trade_pct": risk_pct,
        "atr_multiplier":    atr_multiplier,
        "positions":         positions,
        "timestamp":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["summary_text"] = format_position_report(result)

    # CSV 出力
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        out_dir  = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "dashboard"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"position_sizing_{today}.csv"

        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            fieldnames = [
                "ticker", "name", "sector_code", "current_price",
                "atr14", "atr_pct", "stop_loss_price", "stop_loss_pct",
                "suggested_shares", "suggested_lot", "position_value",
                "position_pct", "risk_amount", "risk_pct_actual",
                "risk_reward_ratio", "sector_score",
                "conviction_score", "conviction_level",
                "recommended_pct", "regime_state",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows([p for p in positions if not p.get("error")])

        result["csv_path"] = str(out_path)
        print(f"    💾 position_sizing_{today}.csv 出力完了")
    except Exception as e:
        print(f"    ⚠️  CSV出力失敗: {e}")

    valid = [p for p in positions if not p.get("error")]
    print(f"    ✅ ポジションサイジング完了: {len(valid)}/{len(holdings)}銘柄")
    return result
