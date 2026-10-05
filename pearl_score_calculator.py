# ==============================================================
# pearl_score_calculator.py  v1.1  (Wave 7 - Step 9)
# 光る原石スコア計算共通ライブラリ
# ==============================================================
# 【役割】
#   JP株・US株両方の screener から呼ばれる 5次元スコア計算エンジン。
#   Ph1 では D1（方向性）・D3（利益品質）・D4（発見ギャップ）の
#   3次元のみを有効化し、D2・D5 は将来拡張用プレースホルダーとする。
#
# 【スコア構造 (Ph2)】
#   $pearl\_score = D1 \times 0.30 + D3 \times 0.35 + D4 \times 0.20 + D5 \times 0.15$
#
#   D1: 方向性スコア（0〜100）
#     52週騰落率 / EPS加速度 / アナリスト目標乖離 / 低ボラ上昇ボーナス
#
#   D3: 利益品質スコア（0〜100）
#     アクルーアル品質 / Beneish M-Score proxy 3変数版(FE-3) / FCF品質 /
#     ROE水準 / 粗利率品質
#
#   D4: 発見ギャップスコア（0〜100）
#     アナリストカバレッジ / 時価総額による未発見度 / discovery_score活用
#
#   D5: 逆張りシグナルスコア（0〜100）（Wave 2 - Pearl Ph2）
#     OLS残差ε（実績リターン - β×市場リターン）で逆張り機会を定量化
#
#   D2: 持続性（Ph2未実装 → None）
#
# 【Beneish M-Score proxy (FE-3) について】
#   簡易 M-Score: $M_{proxy} = -4.84 + 4.679 \times TATA + 0.892 \times SGI$
#   TATA = accrual\_ratio（本ライブラリでは edinet/yfinance アクルーアル比率）
#   SGI  = 1 + revenue\_growth
#   $M_{proxy} > -2.22$ → 利益操作リスクあり（D3 にペナルティ）
#
# 【Pearl ティア → quality_score ボーナス】
#   SS (score >= 80): +15pt    ← LINE即時通知対象
#   S  (score >= 70): +10pt
#   A  (score >= 60): +5pt
#   B  (score >= 50): +2pt
#   None (score < 50): 0pt
#
# 【生成キャッシュ】
#   CACHE_DIR/pearl_candidates.json   TTL: 7日,  日次
#   CACHE_DIR/pearl_history.json      TTL: 180日, 日次
#   CACHE_DIR/direction_momentum.json TTL: 90日, 四半期
#   CACHE_DIR/smart_money_trail.json  TTL: 90日, 日次
#
# 【呼び出しパターン（stock_screener / us_stock_screener）】
#   from pearl_score_calculator import calc_pearl_score, save_pearl_candidate
#   pearl = calc_pearl_score(data, cfg, market="JP")
#   result["pearl_score"] = pearl["pearl_score"]
#   result["pearl_bonus"] = pearl["pearl_bonus"]
#   result["pearl_tier"]  = pearl["pearl_tier"]
#   result["quality_score"] = min(100, result["quality_score"] + pearl["pearl_bonus"])
#
# 【依存】
#   config.py（BLOCK 24定数参照）
#   追加ライブラリなし（標準ライブラリのみ）
#   ★ try/except で全ブロックを保護。失敗時はゼロスコアを返す。
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: デフォルト定数（config.py が読めない場合のフォールバック）
# ==============================================================

# Wave 3 - Pearl Ph3: D2（Smart Money）を追加し5次元ウェイトを再調整
# $pearl\_score = D1 \times 0.25 + D2 \times 0.15 + D3 \times 0.30$
#              $+ D4 \times 0.15 + D5 \times 0.15$

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'PSC_DEFAULT_WEIGHTS': '_DEFAULT_WEIGHTS',
    'PSC_M_SCORE_PENALTY_THRESHOLD': '_M_SCORE_PENALTY_THRESHOLD',
    'PSC_M_SCORE_MEDIUM_THRESHOLD': '_M_SCORE_MEDIUM_THRESHOLD',
    'PSC_M_SCORE_STRONG_MULTIPLIER': '_M_SCORE_STRONG_MULTIPLIER',
    'PSC_M_SCORE_MEDIUM_MULTIPLIER': '_M_SCORE_MEDIUM_MULTIPLIER',
    'PSC_DEFAULT_TIER_BONUS': '_DEFAULT_TIER_BONUS',
    'B6_PEARL_SCORE_CALCULATOR_D2_CONFIDENCE_DISCOUNT_US': '_D2_CONFIDENCE_DISCOUNT_US',
    'B6_PEARL_SCORE_CALCULATOR_MARKET_RETURN_JP': '_MARKET_RETURN_JP',
    'B6_PEARL_SCORE_CALCULATOR_MARKET_RETURN_US': '_MARKET_RETURN_US',
}

_DEFAULT_WEIGHTS = {
    "D1": 0.25,   # 方向性（Ph2: 0.30 → Ph3: 0.25）
    "D2": 0.15,   # Smart Money（Ph3新設）
    "D3": 0.30,   # 利益品質（Ph2: 0.35 → Ph3: 0.30）
    "D4": 0.15,   # 発見ギャップ（Ph2: 0.20 → Ph3: 0.15）
    "D5": 0.15,   # 逆張りシグナル（変更なし）
}

# US 13F データの信頼度割引係数（45日遅延分を考慮）
_D2_CONFIDENCE_DISCOUNT_US = 0.70

# M-Score 3変数版 ペナルティ閾値デフォルト（config.py BLOCK 29 で上書き可能）
_M_SCORE_PENALTY_THRESHOLD  = -1.78   # 強ペナルティ閾値（Beneish 1999）
_M_SCORE_MEDIUM_THRESHOLD   = -2.22   # 中ペナルティ閾値（Beneish 1999）
_M_SCORE_STRONG_MULTIPLIER  = 0.50    # D3スコアを半減
_M_SCORE_MEDIUM_MULTIPLIER  = 0.75    # D3スコアを0.75倍

# D5 計算用: 市場リターン前提値（年率）
_MARKET_RETURN_JP = 0.10   # TOPIX 想定年率リターン
_MARKET_RETURN_US = 0.15   # S&P500 想定年率リターン

_DEFAULT_TIER_BONUS = {
    "SS": 15.0,
    "S":  10.0,
    "A":   5.0,
    "B":   2.0,
}

# キャッシュファイル名
_CACHE_FILES = {
    "candidates":        "pearl_candidates.json",
    "history":           "pearl_history.json",
    "direction":         "direction_momentum.json",
    "smart_money":       "smart_money_trail.json",
}

# ==============================================================
# BLOCK 0-B: Wave 7 追加定数（EDINETセグメント統合・潜伏銘柄補正）
# ==============================================================

# D3 への EDINETセグメント文脈スコアの加算上限
_D3_EDINET_SEGMENT_BONUS_MAX = 15.0

# D3 への中計スコア連携ボーナス上限
_D3_MIDTERM_BONUS_MAX = 10.0

# D3 へのカタリストフラグ連携ボーナス上限
_D3_CATALYST_BONUS_MAX = 8.0

# 潜伏Pearl昇格銘柄への pearl_score 上乗せ上限
_LATENT_PEARL_PROMOTION_BONUS_MAX = 8.0

# EDINETセグメント文脈評価キーワード（収益性関連）
_EDINET_SEGMENT_POSITIVE_KEYWORDS: list = [
    "増収", "増益", "営業利益率改善", "高収益",
    "利益率向上", "コスト削減", "構造改革",
    "セグメント利益増", "黒字転換", "事業利益",
]
_EDINET_SEGMENT_NEGATIVE_KEYWORDS: list = [
    "減益", "赤字", "営業損失", "のれん償却",
    "減損", "特別損失", "収益悪化", "セグメント損失",
]

# ==============================================================
# BLOCK 1: D1スコア — 方向性（Directionality）
# ==============================================================
# 【構成】
#   52週騰落率           : max 40pt
#   EPS加速度            : max 30pt
#   アナリスト目標乖離    : max 20pt
#   低ボラ上昇ボーナス    : max 10pt
# ==============================================================

def _calc_d1_direction(data: dict) -> tuple[float, list[str]]:
    """
    方向性スコア（D1）を計算する。

    Args:
        data: yfinance / _fetch_ticker_data() の取得データ辞書

    Returns:
        (d1_score: float 0〜100, flags: list[str])
    """
    try:
        score = 0.0
        flags: list[str] = []

        # ── ① 52週騰落率（week52_change）: max 40pt ───────────
        w52 = data.get("week52_change")
        if w52 is not None:
            try:
                w52 = float(w52)
                if w52 >= 0.50:
                    score += 40.0
                    flags.append(f"52週+{w52*100:.0f}%↑↑（急騰）")
                elif w52 >= 0.30:
                    score += 35.0
                    flags.append(f"52週+{w52*100:.0f}%↑↑")
                elif w52 >= 0.15:
                    score += 25.0
                    flags.append(f"52週+{w52*100:.0f}%↑")
                elif w52 >= 0.05:
                    score += 15.0
                    flags.append(f"52週+{w52*100:.0f}%微増")
                elif w52 >= -0.05:
                    score += 8.0
                    flags.append(f"52週±横ばい")
                elif w52 >= -0.20:
                    score += 3.0
                else:
                    score += 0.0
                    flags.append(f"52週{w52*100:.0f}%↓（弱い）")
            except (TypeError, ValueError):
                pass

        # ── ② EPS加速度: max 30pt ────────────────────────────
        # 直接渡された acceleration フィールド or 生の成長率から計算
        # 2026-10-01: 旧式の「四半期成長率×4 − 年率」は、yfinance の2つがどちらも
        # 前年同期比でほぼ同値のため実質「増益率×3」だった。四半期の前年同期比 −
        # 通期の前年比（earnings_surprise.calc_profit_acceleration）に統一する。
        accel = data.get("earnings_acceleration")
        if accel is None:
            pa = data.get("profit_accel")
            if pa is None and data.get("ticker") and not data.get("_offline"):
                try:
                    import earnings_surprise as _es
                    pa = _es._fetch_profit_acceleration(str(data["ticker"]))
                except Exception:
                    pa = None
            accel = (pa or {}).get("accel")

        if accel is not None:
            try:
                accel = float(accel)
                if accel > 0.50:
                    score += 30.0
                    flags.append(f"EPS加速+{accel*100:.0f}%↑↑（強加速）")
                elif accel > 0.30:
                    score += 25.0
                    flags.append(f"EPS加速+{accel*100:.0f}%↑")
                elif accel > 0.15:
                    score += 18.0
                    flags.append(f"EPS加速+{accel*100:.0f}%")
                elif accel > 0.05:
                    score += 10.0
                    flags.append(f"EPS微加速+{accel*100:.0f}%")
                elif accel < -0.30:
                    flags.append(f"EPS減速{accel*100:.0f}%")
            except (TypeError, ValueError):
                pass

        # ── ③ アナリスト目標乖離（analyst_upside）: max 20pt ──
        upside = data.get("analyst_upside")
        if upside is not None:
            try:
                upside = float(upside)
                if upside >= 0.40:
                    score += 20.0
                    flags.append(f"目標乖離+{upside*100:.0f}%（大幅上昇余地）")
                elif upside >= 0.25:
                    score += 15.0
                    flags.append(f"目標乖離+{upside*100:.0f}%")
                elif upside >= 0.10:
                    score += 8.0
                    flags.append(f"目標乖離+{upside*100:.0f}%")
                elif upside >= 0.00:
                    score += 3.0
                elif upside < -0.15:
                    flags.append(f"目標割れ{upside*100:.0f}%")
            except (TypeError, ValueError):
                pass

        # ── ④ 低ボラ上昇ボーナス（Beta補正）: max 10pt ─────────
        beta = data.get("beta")
        w52_ok = (w52 is not None and w52 > 0) if 'w52' in dir() else False
        if w52_ok and beta is not None:
            try:
                beta = float(beta)
                if 0.3 <= beta <= 0.9:
                    # 低ボラで上昇 = 本物のモメンタム
                    score += 10.0
                    flags.append(f"低ボラ上昇β={beta:.2f}✅")
                elif beta <= 1.2:
                    score += 5.0
            except (TypeError, ValueError):
                pass

        return round(min(100.0, max(0.0, score)), 1), flags

    except Exception:
        return 0.0, ["D1計算エラー"]


# ==============================================================
# BLOCK 2: D3スコア — 利益品質・M-Score proxy（Earnings Quality/FE-3）
# ==============================================================
# 【構成】
#   アクルーアル品質（TATA）: max 35pt（+ ペナルティあり）
#   FCF品質（CFO品質）       : max 25pt
#   ROE水準                  : max 20pt
#   粗利率品質               : max 20pt
#
# 【M-Score proxy 3変数版 (FE-3 Wave 2)】
#   $M_{3var} = -4.84 + 0.920 \times DSRI + 0.528 \times GMI + 0.404 \times ACQI$
#   DSRI : Days Sales Receivable Index proxy（売上成長→売掛金増加リスク）
#   GMI  : Gross Margin Index proxy（粗利率水準から低下リスクを近似）
#   ACQI : Asset Quality Index proxy（流動比率から資産品質を近似）
#   $M_{3var} > -1.78$ → 強ペナルティ（D3 × 0.50）
#   $-1.78 \geq M_{3var} > -2.22$ → 中ペナルティ（D3 × 0.75）
# ==============================================================

def _calc_m_score_3var(data: dict, cfg=None) -> tuple[float, bool, bool, str]:
    """
    Beneish M-Score 3変数 proxy を計算する（Wave 2 - FE-3）。

    yfinance で取得可能なフィールドから DSRI・GMI・ACQI を近似する。

    【DSRI proxy】
      売上成長率から売掛金増加リスクを近似。
      $DSRI_{proxy} = \\max(0.8, 1.0 + \\max(0, revenue\\_growth) \times 0.4)$

    【GMI proxy】
      粗利率水準から利益品質低下リスクを近似。
      $GMI_{proxy} = 1.10$ if $gross\\_margin < 0.20$,
      $GMI_{proxy} = 1.00$ if $0.20 \\leq gross\\_margin < 0.40$,
      $GMI_{proxy} = 0.95$ if $gross\\_margin \\geq 0.40$

    【ACQI proxy】
      流動比率/当座比率から資産品質を近似。
      $ACQI_{proxy} = \\min(2.0, 2.0 / \\max(0.5, quick\\_ratio))$

    Args:
        data: screener データ辞書
        cfg:  config モジュール（None 可）

    Returns:
        (m_score: float, is_strong_risk: bool, is_medium_risk: bool, flag: str)
    """
    try:
        # ── DSRI proxy ────────────────────────────────────────
        rg = data.get("revenue_growth")
        rg_f = float(rg) if rg is not None else 0.0
        dsri = max(0.8, 1.0 + max(0.0, rg_f) * 0.4)

        # ── GMI proxy ─────────────────────────────────────────
        gm = data.get("gross_margin")
        if gm is not None:
            gm_f = float(gm)
            if gm_f >= 0.40:
                gmi = 0.95
            elif gm_f >= 0.20:
                gmi = 1.00
            else:
                gmi = 1.10
        else:
            gmi = 1.00   # 不明時はニュートラル

        # ── ACQI proxy ─────────────────────────────────────────
        qr = data.get("quick_ratio")
        cr = data.get("current_ratio")
        if qr is not None:
            acqi = min(2.0, 2.0 / max(0.5, float(qr)))
        elif cr is not None:
            acqi = min(2.0, 2.0 / max(0.5, float(cr)))
        else:
            acqi = 1.00   # 不明時はニュートラル

        # ── M-Score 3変数 計算 ────────────────────────────────
        # $M_{3var} = -4.84 + 0.920 \times DSRI + 0.528 \times GMI + 0.404 \times ACQI$
        m_score = -4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * acqi

        # ── 閾値判定（config.py BLOCK 29 参照） ───────────────
        strong_thr = float(getattr(cfg, "M_SCORE_PENALTY_THRESHOLD",
                                   _M_SCORE_PENALTY_THRESHOLD))
        medium_thr = float(getattr(cfg, "M_SCORE_MEDIUM_THRESHOLD",
                                   _M_SCORE_MEDIUM_THRESHOLD))

        is_strong = m_score > strong_thr   # > -1.78
        is_medium = m_score > medium_thr   # > -2.22（かつ ≤ -1.78 の範囲）

        if is_strong:
            flag = (f"M-Score3v={m_score:.2f}（強操作リスク⚠️ D3×0.50）"
                    f" DSRI={dsri:.2f} GMI={gmi:.2f} ACQI={acqi:.2f}")
        elif is_medium:
            flag = (f"M-Score3v={m_score:.2f}（中操作リスク⚠️ D3×0.75）"
                    f" DSRI={dsri:.2f} GMI={gmi:.2f} ACQI={acqi:.2f}")
        else:
            flag = f"M-Score3v={m_score:.2f}（正常✅）"

        return round(m_score, 3), is_strong, is_medium, flag

    except Exception:
        return 0.0, False, False, ""


def _calc_d3_earnings_quality(data: dict, cfg=None) -> tuple[float, list[str], float]:
    """
    利益品質スコア（D3）を計算する。

    Returns:
        (d3_score: float 0〜100, flags: list[str], m_score_3v: float)
    """
    try:
        score = 0.0
        flags: list[str] = []

        acr = data.get("accrual_ratio")
        roe = data.get("roe")
        fcf = data.get("free_cashflow")
        ocf = data.get("operating_cashflow") or data.get("operatingCashflow")
        gross_margin = data.get("gross_margin")
        revenue_growth = data.get("revenue_growth")

        # ── ① アクルーアル品質（TATA proxy）: max 35pt ──────────
        # M-Score 3変数版（FE-3 Wave 2）を先行計算し、後段でペナルティ乗数を適用
        m_score_3v, m_is_strong, m_is_medium, m_flag = _calc_m_score_3var(
            data, cfg=cfg   # cfg はクロージャ外のため None（閾値デフォルト使用）
        )
        if m_flag:
            flags.append(m_flag)

        if acr is not None:
            try:
                acr = float(acr)
                if acr < -0.05:
                    score += 35.0
                    flags.append(f"アクルーアル優良({acr*100:+.1f}%)✅")
                elif acr < -0.02:
                    score += 25.0
                    flags.append(f"アクルーアル良好({acr*100:+.1f}%)")
                elif acr < 0.02:
                    score += 15.0
                elif acr < 0.05:
                    score += 5.0
                    flags.append(f"アクルーアル注意({acr*100:+.1f}%)")
                else:
                    score -= 20.0
                    flags.append(f"⚠️ アクルーアル危険({acr*100:+.1f}%)→利益品質低")
            except (TypeError, ValueError):
                pass

        # ── ② FCF品質（CFO > NI品質）: max 25pt ────────────────
        try:
            if fcf is not None and float(fcf) > 0:
                score += 15.0
                flags.append("FCF黒字✅")
                # OCF も正で FCF より大きい = 設備投資を回収できている
                if ocf is not None and float(ocf) > float(fcf):
                    score += 10.0
                    flags.append("OCF>FCF（投資回収良）✅")
            elif fcf is not None and float(fcf) < 0:
                flags.append("FCF赤字⚠️")
        except (TypeError, ValueError):
            pass

        # ── ③ ROE水準: max 20pt ──────────────────────────────
        if roe is not None:
            try:
                roe_f = float(roe)
                roe_pct = roe_f * 100 if abs(roe_f) < 1 else roe_f
                if roe_pct >= 20.0:
                    score += 20.0
                    flags.append(f"高ROE({roe_pct:.1f}%)✅")
                elif roe_pct >= 15.0:
                    score += 16.0
                    flags.append(f"ROE({roe_pct:.1f}%)")
                elif roe_pct >= 10.0:
                    score += 10.0
                    flags.append(f"ROE({roe_pct:.1f}%)")
                elif roe_pct >= 5.0:
                    score += 4.0
                else:
                    flags.append(f"低ROE({roe_pct:.1f}%)")
            except (TypeError, ValueError):
                pass

        # ── ④ 粗利率品質（gross_margin）: max 20pt ───────────────
        if gross_margin is not None:
            try:
                gm = float(gross_margin)
                if gm >= 0.70:
                    score += 20.0
                    flags.append(f"高粗利率({gm*100:.0f}%)✅")
                elif gm >= 0.50:
                    score += 15.0
                    flags.append(f"粗利率({gm*100:.0f}%)")
                elif gm >= 0.30:
                    score += 8.0
                elif gm >= 0.15:
                    score += 3.0
            except (TypeError, ValueError):
                pass

        # ── M-Score 3変数 乗数ペナルティ（FE-3 Wave 2） ──────────
        # 加算ペナルティではなく乗数方式で D3 全体を減衰させる。
        # $M_{3var} > -1.78$ → score × 0.50（強ペナルティ）
        # $-1.78 \geq M_{3var} > -2.22$ → score × 0.75（中ペナルティ）
        pre_penalty_score = round(min(100.0, max(0.0, score)), 1)
        if m_is_strong:
            score = pre_penalty_score * _M_SCORE_STRONG_MULTIPLIER
            flags.append(f"⚠️ M-Score強ペナルティ適用（×{_M_SCORE_STRONG_MULTIPLIER}）")
        elif m_is_medium:
            score = pre_penalty_score * _M_SCORE_MEDIUM_MULTIPLIER
            flags.append(f"⚠️ M-Score中ペナルティ適用（×{_M_SCORE_MEDIUM_MULTIPLIER}）")

# ── Wave 7: EDINETセグメント文脈スコアの統合 ──────────
        # latent_pearl_manager / independence_score_calculator の
        # カタリストデータを D3 に反映する。
        # $D3_{final} = D3_{base} + bonus_{segment} + bonus_{catalyst} + bonus_{midterm}$

        # 2026-07-25: Wave7 の D3統合フラグ3本を配線。
        # 実装（_calc_edinet_segment_bonus / _calc_catalyst_bonus /
        # _calc_midterm_bonus）はここに揃っていたのに、config の
        # ENABLE_D3_* が誰にも読まれず「常時適用」になっていた。
        # フラグ値は現在の挙動（＝常時適用）に合わせて True へ揃えてあるため、
        # 本配線でスコアは変わらない。切りたい時に初めて効くようになる。
        _d3_seg_on = getattr(cfg, "ENABLE_D3_EDINET_SEGMENT", True)
        _d3_cat_on = getattr(cfg, "ENABLE_D3_CATALYST_INTEGRATION", True)
        _d3_mid_on = getattr(cfg, "ENABLE_D3_MIDTERM_INTEGRATION", True)

        # (1) EDINETセグメント文脈ボーナス
        try:
            segment_bonus = _calc_edinet_segment_bonus(data, cfg) if _d3_seg_on else 0.0
            if segment_bonus > 0:
                score += segment_bonus
                flags.append(
                    f"EDINETセグメント文脈+{segment_bonus:.1f}pt✅"
                )
            elif segment_bonus < 0:
                score += segment_bonus
                flags.append(
                    f"EDINETセグメント文脈{segment_bonus:.1f}pt⚠️"
                )
        except Exception:
            pass

        # (2) カタリストフラグ連携ボーナス
        try:
            catalyst_bonus = _calc_catalyst_bonus(data, cfg) if _d3_cat_on else 0.0
            if catalyst_bonus > 0:
                score += catalyst_bonus
                flags.append(f"カタリスト連携+{catalyst_bonus:.1f}pt✅")
        except Exception:
            pass

        # (3) 中計達成率連携ボーナス
        try:
            midterm_bonus = _calc_midterm_bonus(data, cfg) if _d3_mid_on else 0.0
            if midterm_bonus > 0:
                score += midterm_bonus
                flags.append(f"中計達成連携+{midterm_bonus:.1f}pt✅")
            elif midterm_bonus < 0:
                score += midterm_bonus
                flags.append(f"中計未達ペナルティ{midterm_bonus:.1f}pt⚠️")
        except Exception:
            pass

        return round(min(100.0, max(0.0, score)), 1), flags, m_score_3v

    except Exception:
        return 0.0, ["D3計算エラー"], 0.0

# ==============================================================
# BLOCK 2-B: D3補助関数（Wave 7 - EDINETセグメント統合）
# ==============================================================

def _calc_edinet_segment_bonus(data: dict, cfg=None) -> float:
    """
    data に含まれる EDINET セグメントテキストから
    収益性キーワードを検出し、D3 への加算ボーナスを返す。

    $bonus_{segment} = \\sum_{k \\in positive} 1.5 - \\sum_{k \\in negative} 2.0$
    上限: $+{_D3\\_EDINET\\_SEGMENT\\_BONUS\\_MAX}$pt
    下限: $-10.0$pt

    Args:
        data: screener データ辞書（"edinet_segment_text" キーを参照）

    Returns:
        float: ボーナス値（正=加算、負=ペナルティ）
    """
    try:
        segment_text = str(data.get("edinet_segment_text", "") or "")
        if not segment_text:
            return 0.0

        positive_score = sum(
            1.5 for kw in _EDINET_SEGMENT_POSITIVE_KEYWORDS
            if kw in segment_text
        )
        negative_score = sum(
            2.0 for kw in _EDINET_SEGMENT_NEGATIVE_KEYWORDS
            if kw in segment_text
        )
        raw = positive_score - negative_score
        return round(
            min(float(getattr(cfg, "D3_EDINET_SEGMENT_BONUS_MAX",
                              _D3_EDINET_SEGMENT_BONUS_MAX)),
                max(-10.0, raw)), 2
        )
    except Exception:
        return 0.0


def _calc_catalyst_bonus(data: dict, cfg=None) -> float:
    """
    independence_score_calculator.py が生成した
    catalyst_score を D3 ボーナスに変換する。

    $bonus_{catalyst} = (catalyst\\_score / 10) \\times D3\\_CATALYST\\_BONUS\\_MAX$

    data に "catalyst_score" キーが含まれている場合はそれを優先。
    なければ independence_scores.json から読み込む。

    Returns:
        float: 0.0 〜 _D3_CATALYST_BONUS_MAX
    """
    try:
        # data に直接含まれている場合（screener 側で事前付与）
        catalyst_score = data.get("catalyst_score")
        if catalyst_score is None:
            # independence_scores.json から取得
            ticker = data.get("ticker", data.get("Symbol", ""))
            if ticker and cfg is not None:
                try:
                    from independence_score_calculator import get_catalyst_flags
                    cache_dir = __import__("pathlib").Path(
                        getattr(cfg, "CACHE_DIR", "stock_data/cache")
                    )
                    ind_p = cache_dir / "independence_scores.json"
                    if ind_p.exists():
                        ind_data = __import__("json").loads(
                            ind_p.read_text(encoding="utf-8")
                        )
                        entry = ind_data.get("scores", {}).get(ticker, {})
                        catalyst_score = float(entry.get("catalyst_score", 0.0))
                except Exception:
                    catalyst_score = 0.0
            else:
                catalyst_score = 0.0

        cat = float(catalyst_score or 0.0)
        _cat_max = float(getattr(cfg, "D3_CATALYST_BONUS_MAX",
                                 _D3_CATALYST_BONUS_MAX))
        bonus = (cat / 10.0) * _cat_max
        return round(min(_cat_max, max(0.0, bonus)), 2)
    except Exception:
        return 0.0


def _calc_midterm_bonus(data: dict, cfg=None) -> float:
    """
    midterm_plan_tracker.py の overall_score と trajectory から
    D3 への加算ボーナスまたはペナルティを計算する。

    $bonus_{midterm} = (overall\\_score / 100 - 0.5) \\times 2 \\times D3\\_MIDTERM\\_BONUS\\_MAX$
    trajectory == "critical" → 追加 -5pt
    trajectory == "ahead"    → 追加 +3pt

    data に "midterm_overall_score" / "midterm_trajectory" が
    含まれている場合はそれを優先。

    Returns:
        float: -(_D3_MIDTERM_BONUS_MAX + 5) 〜 _D3_MIDTERM_BONUS_MAX + 3
    """
    try:
        midterm_score    = data.get("midterm_overall_score")
        midterm_traj     = data.get("midterm_trajectory")

        if midterm_score is None:
            ticker = data.get("ticker", data.get("Symbol", ""))
            if ticker and cfg is not None:
                try:
                    import json as _json
                    from pathlib import Path as _Path
                    p = _Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "midterm_plan_cache.json"
                    if p.exists():
                        mt_data  = _json.loads(p.read_text(encoding="utf-8"))
                        mt_entry = mt_data.get("plans", {}).get(ticker, {})
                        midterm_score = float(mt_entry.get("overall_score", 50.0))
                        midterm_traj  = mt_entry.get("trajectory", "on_track")
                except Exception:
                    midterm_score = 50.0
                    midterm_traj  = "on_track"
            else:
                return 0.0

        score = float(midterm_score or 50.0)
        # $base = (score/100 - 0.5) \\times 2 \\times max$
        _mid_max = float(getattr(cfg, "D3_MIDTERM_BONUS_MAX",
                                 _D3_MIDTERM_BONUS_MAX))
        base_bonus = ((score / 100.0) - 0.5) * 2.0 * _mid_max

        # trajectory 補正
        traj_adj = 0.0
        if midterm_traj == "critical":
            traj_adj = -5.0
        elif midterm_traj == "ahead":
            traj_adj = +3.0
        elif midterm_traj == "behind":
            traj_adj = -2.0

        total = base_bonus + traj_adj
        return round(
            min(_mid_max + 3.0, max(-(_mid_max + 5.0), total)),
            2,
        )
    except Exception:
        return 0.0

# ==============================================================
# BLOCK 3: D4スコア — 発見ギャップ（Discovery Gap）
# ==============================================================
# 【構成】
#   アナリストカバレッジ     : max 50pt（少ないほど高スコア）
#   時価総額による未発見度   : max 30pt（小さいほど高スコア）
#   discovery_score活用     : max 20pt（[S-3]スコア）
# ==============================================================

def _calc_d4_discovery_gap(data: dict, market: str = "JP") -> tuple[float, list[str]]:
    """
    発見ギャップスコア（D4）を計算する。

    Args:
        data:   screenerデータ辞書
        market: "JP" または "US"（時価総額の単位が異なる）

    Returns:
        (d4_score: float 0〜100, flags: list[str])
    """
    try:
        score = 0.0
        flags: list[str] = []

        analyst_count  = data.get("analyst_count")
        market_cap_b   = data.get("market_cap_b")   # JP: 億円
        market_cap_usd = data.get("market_cap")      # US: USD（raw）
        discovery_sc   = data.get("discovery_score") # [S-3] 未発見スコア

        # ── ① アナリストカバレッジ: max 50pt ─────────────────────
        if analyst_count is not None:
            try:
                ac = int(analyst_count)
                if ac <= 0:
                    score += 50.0
                    flags.append("アナリスト0人（完全未発見）⭐")
                elif ac <= 1:
                    score += 45.0
                    flags.append(f"アナリスト{ac}人（ほぼ未発見）⭐")
                elif ac <= 3:
                    score += 35.0
                    flags.append(f"アナリスト{ac}人（超少数）")
                elif ac <= 5:
                    score += 20.0
                    flags.append(f"アナリスト{ac}人（少数）")
                elif ac <= 10:
                    score += 8.0
                    flags.append(f"アナリスト{ac}人")
                else:
                    flags.append(f"アナリスト{ac}人（高カバレッジ）")
            except (TypeError, ValueError):
                pass

        # ── ② 時価総額による未発見度: max 30pt ──────────────────
        # JP: market_cap_b（億円）、US: market_cap（USD）から億円換算
        mcap_b = None
        if market_cap_b is not None:
            try:
                mcap_b = float(market_cap_b)
            except (TypeError, ValueError):
                pass
        elif market_cap_usd is not None and market == "US":
            try:
                # USD → 億円換算（1USD ≈ 150円として近似）
                mcap_b = float(market_cap_usd) / 1e8 / 150 * 1e4
            except (TypeError, ValueError):
                pass

        if mcap_b is not None:
            if mcap_b <= 50:
                score += 30.0
                flags.append(f"時価総額{mcap_b:.0f}億（Nano/超未発見）⭐")
            elif mcap_b <= 100:
                score += 25.0
                flags.append(f"時価総額{mcap_b:.0f}億（Micro/未発見）")
            elif mcap_b <= 300:
                score += 18.0
                flags.append(f"時価総額{mcap_b:.0f}億（Small/やや未発見）")
            elif mcap_b <= 500:
                score += 10.0
                flags.append(f"時価総額{mcap_b:.0f}億（小型）")
            elif mcap_b <= 1000:
                score += 4.0
            else:
                flags.append(f"時価総額{mcap_b:.0f}億（大型・発見済）")

        # ── ③ discovery_score（[S-3]）の活用: max 20pt ───────────
        if discovery_sc is not None:
            try:
                ds = float(discovery_sc)
                if ds >= 80:
                    score += 20.0
                    flags.append(f"discovery_score{ds:.0f}（最高）⭐")
                elif ds >= 60:
                    score += 12.0
                    flags.append(f"discovery_score{ds:.0f}（高）")
                elif ds >= 40:
                    score += 6.0
                    flags.append(f"discovery_score{ds:.0f}（中）")
                elif ds >= 20:
                    score += 2.0
            except (TypeError, ValueError):
                pass

        return round(min(100.0, max(0.0, score)), 1), flags

    except Exception:
        return 0.0, ["D4計算エラー"]


# ==============================================================
# BLOCK 4: D2スコア — Smart Money（Wave 3 - Pearl Ph3）
# ==============================================================
# 【構成】
#   機関投資家保有率スコア     : max 40pt
#   アナリスト upside スコア  : max 30pt
#   推奨コンセンサススコア     : max 30pt
#
# 【US 13F confidence_discount = 0.70 適用】
#   $D2_{US} = D2_{raw} \times 0.70$
#   13F 提出は四半期遅延（最大45日）のため信頼度を割引く。
#   $D2_{JP}$ は割引なし（適時開示ベース）。
#
# 【スコアリング根拠】
#   機関投資家保有率 ≥ 80%  → 40pt（強い Smart Money 集積）
#   アナリスト upside ≥ 30% → 30pt（強い上値余地）
#   rec_mean ≤ 1.5           → 30pt（Strong Buy コンセンサス）
# ==============================================================

def _calc_d2_smart_money(
    data:   dict,
    market: str = "JP",
    cfg=None,
) -> tuple[float, list[str]]:
    """
    D2: Smart Money スコアを算出する（Wave 3 - Pearl Ph3）。

    Args:
        data:   screener / base_agent のデータ辞書
        market: "JP" | "US"（"US" の場合 confidence_discount=0.70 を適用）
        cfg:    config モジュール（None 可）

    Returns:
        (d2_score: float 0〜100, flags: list[str])
    """
    try:
        score = 0.0
        flags: list[str] = []

        # US 13F confidence_discount 係数
        confidence = float(getattr(
            cfg, "D2_CONFIDENCE_DISCOUNT_US",
            _D2_CONFIDENCE_DISCOUNT_US
        )) if market == "US" else 1.0

        # ── ① 機関投資家保有率（max 40pt）──────────────────────
        inst = data.get("inst_ownership")
        if inst is not None:
            inst_f = float(inst)
            if inst_f >= 0.80:
                score += 40.0
                flags.append(f"機関保有率{inst_f*100:.0f}%⭐")
            elif inst_f >= 0.65:
                score += 30.0
                flags.append(f"機関保有率{inst_f*100:.0f}%")
            elif inst_f >= 0.50:
                score += 20.0
                flags.append(f"機関保有率{inst_f*100:.0f}%")
            elif inst_f >= 0.35:
                score += 10.0

        # ── ② アナリスト upside（max 30pt）──────────────────────
        # analyst_upside = (target_price / current_price) - 1
        upside = data.get("analyst_upside")
        if upside is not None:
            up_f = float(upside)
            if up_f >= 0.30:
                score += 30.0
                flags.append(f"analyst upside+{up_f*100:.0f}%⭐")
            elif up_f >= 0.15:
                score += 20.0
                flags.append(f"analyst upside+{up_f*100:.0f}%")
            elif up_f >= 0.05:
                score += 10.0
            elif up_f < -0.10:
                # ダウンサイドリスク → ペナルティ
                score -= 10.0
                flags.append(f"⚠️ analyst downside{up_f*100:.0f}%")

        # ── ③ 推奨コンセンサス（max 30pt）───────────────────────
        # rec_mean: 1.0=Strong Buy 〜 5.0=Strong Sell（yfinance）
        rec_mean = data.get("rec_mean")
        if rec_mean is not None:
            rm = float(rec_mean)
            if rm <= 1.5:
                score += 30.0
                flags.append(f"Strong Buy推奨(rec={rm:.1f})⭐")
            elif rm <= 2.0:
                score += 20.0
                flags.append(f"Buy推奨(rec={rm:.1f})")
            elif rm <= 2.5:
                score += 10.0
            elif rm > 3.5:
                score -= 10.0
                flags.append(f"⚠️ Sell傾向(rec={rm:.1f})")

        # ── US 13F confidence_discount 適用 ──────────────────────
        raw_score = round(min(100.0, max(0.0, score)), 1)
        if market == "US" and confidence < 1.0 and raw_score > 0:
            score = raw_score * confidence
            flags.append(
                f"13F confidence_discount×{confidence:.1f}"
                f"（{raw_score:.0f}→{score:.0f}pt）"
            )
        else:
            score = raw_score

        return round(min(100.0, max(0.0, score)), 1), flags

    except Exception:
        return 0.0, []


# ==============================================================
# BLOCK 5: D5スコア — 逆張りシグナル（Contrarian Signal / Wave 2 - Pearl Ph2）
# ==============================================================
# 【構成】
#   OLS残差ε（CAPM簡易版）         : max 80pt
#   β水準ボーナス（低ボラ割負け）  : max 20pt
#
# 【計算式】
#   $\epsilon_{OLS} = R_i - \hat{\beta} \times R_m$
#   $R_i$    : week52\_change（銘柄の52週リターン）
#   $\hat{\beta}$ : data["beta"]（yfinance取得値）
#   $R_m$    : 市場リターン前提（JP=0.10, US=0.15）
#
# 【スコアリング】
#   $\epsilon < -0.30$ → 90〜100pt（強逆張りシグナル）
#   $\epsilon < -0.15$ → 70〜80pt
#   $\epsilon < -0.05$ → 50〜60pt
#   $|\epsilon| \leq 0.05$ → 40pt（市場並み）
#   $\epsilon \geq 0.05$ → 20〜30pt（既発見・逆張り効果薄）
#   $\epsilon \geq 0.20$ → 0〜10pt（大幅超過・反転リスク）
# ==============================================================

def _calc_d5_valuation(data: dict, market: str = "JP") -> tuple[float | None, list[str]]:
    """割安度（バリュエーション）スコア（0〜100）。2026-10-05 新設（健太指示）。

    PER（低いほど高得点: 8倍以下=100・30倍以上=0）、PBR（0.8倍以下=100・5倍以上=0）、
    FCF利回り（8%以上=100・0%以下=0）のうち取れたものの平均。どれも無ければ None。
    PER/PBR が負（赤字・債務超過）は対象外。
    """
    parts, flags = [], []
    try:
        per = data.get("per")
        if per is not None and float(per) > 0:
            v = max(0.0, min(100.0, (30.0 - float(per)) / 22.0 * 100))
            parts.append(v)
            flags.append(f"PER {float(per):.1f}倍→{v:.0f}点")
        pbr = data.get("pbr")
        if pbr is not None and float(pbr) > 0:
            v = max(0.0, min(100.0, (5.0 - float(pbr)) / 4.2 * 100))
            parts.append(v)
            flags.append(f"PBR {float(pbr):.2f}倍→{v:.0f}点")
        fy = data.get("fcf_yield")
        if fy is None and data.get("free_cashflow") and data.get("market_cap"):
            try:
                fy = float(data["free_cashflow"]) / float(data["market_cap"])
            except (TypeError, ValueError, ZeroDivisionError):
                fy = None
        if fy is not None:
            v = max(0.0, min(100.0, float(fy) / 0.08 * 100))
            parts.append(v)
            flags.append(f"FCF利回り {float(fy) * 100:.1f}%→{v:.0f}点")
    except (TypeError, ValueError):
        pass
    if not parts:
        return None, ["割安度: PER/PBR/FCF利回り未取得"]
    return round(sum(parts) / len(parts), 1), flags


def _calc_d5_contrarian(
    data:   dict,
    market: str = "JP",
) -> tuple[float, list[str]]:
    """
    逆張りシグナルスコア（D5）を計算する（Wave 2 - Pearl Ph2）。

    OLS残差ε = 実績リターン − β×市場リターン で、
    市場期待比での割負け度合いを定量化する。
    値が負（割負け）ほど逆張り機会として高スコアを付与する。

    Args:
        data:   screener データ辞書（week52_change / beta を使用）
        market: "JP" or "US"（市場リターン前提値の切替）

    Returns:
        (d5_score: float 0〜100, flags: list[str])
    """
    try:
        score = 0.0
        flags: list[str] = []

        w52  = data.get("week52_change")
        beta = data.get("beta")

        # ── データ不足時: ニュートラルスコアを返す ───────────────
        if w52 is None:
            return 40.0, ["D5: week52_change未取得（ニュートラル）"]

        r_i = float(w52)

        # ── 市場リターン前提（年率）────────────────────────────
        r_m = _MARKET_RETURN_US if market == "US" else _MARKET_RETURN_JP

        # ── β取得（なければ1.0と仮定）─────────────────────────
        if beta is not None:
            try:
                b = float(beta)
                # 異常値除去: β が -2〜5 の範囲外は 1.0 に補正
                b = b if -2.0 <= b <= 5.0 else 1.0
            except (TypeError, ValueError):
                b = 1.0
        else:
            b = 1.0

        # ── OLS残差ε計算 ──────────────────────────────────────
        # $\epsilon = R_i - \hat{\beta} \times R_m$
        epsilon = r_i - b * r_m

        # ── ① OLS残差εによるスコアリング: max 80pt ───────────
        if epsilon < -0.30:
            score += 80.0
            flags.append(
                f"OLS残差ε={epsilon*100:+.1f}%（強逆張りシグナル⭐）"
            )
        elif epsilon < -0.15:
            score += 65.0
            flags.append(
                f"OLS残差ε={epsilon*100:+.1f}%（逆張りシグナル）"
            )
        elif epsilon < -0.05:
            score += 50.0
            flags.append(
                f"OLS残差ε={epsilon*100:+.1f}%（小幅割負け）"
            )
        elif epsilon <= 0.05:
            score += 40.0
            flags.append(f"OLS残差ε={epsilon*100:+.1f}%（市場並み）")
        elif epsilon < 0.20:
            score += 25.0
            flags.append(
                f"OLS残差ε={epsilon*100:+.1f}%（市場超過・逆張り効果薄）"
            )
        else:
            score += 8.0
            flags.append(
                f"OLS残差ε={epsilon*100:+.1f}%（大幅超過・反転リスク⚠️）"
            )

        # ── ② β水準ボーナス（低ボラ割負け = より純粋な逆張り）: max 20pt ──
        # 低βで大幅割負けしている場合は個別要因の下落 → 回帰余地大
        if epsilon < -0.10 and b is not None:
            try:
                if 0.3 <= b <= 0.9:
                    score += 20.0
                    flags.append(f"低ボラ割負けβ={b:.2f}（純逆張り）⭐")
                elif 0.9 < b <= 1.3:
                    score += 10.0
                    flags.append(f"β={b:.2f}（逆張り）")
            except (TypeError, ValueError):
                pass

        return round(min(100.0, max(0.0, score)), 1), flags

    except Exception:
        return 40.0, ["D5計算エラー（ニュートラル）"]


# ==============================================================
# BLOCK 6: ティア判定・ボーナス計算
# ==============================================================

def _classify_pearl_tier(
    pearl_score: float,
    cfg=None,
) -> tuple[Optional[str], float]:
    """
    Pearl Score からティアとボーナスを決定する。

    Returns:
        (tier: str|None, bonus: float)
    """
    try:
        # config から閾値を取得（なければデフォルト）
        ss_min = float(getattr(cfg, "PEARL_TIER_SS_MIN", 80.0))
        s_min  = float(getattr(cfg, "PEARL_TIER_S_MIN",  70.0))
        a_min  = float(getattr(cfg, "PEARL_TIER_A_MIN",  60.0))
        b_min  = float(getattr(cfg, "PEARL_TIER_B_MIN",  50.0))
        bonus_map = getattr(cfg, "PEARL_TIER_BONUS", _DEFAULT_TIER_BONUS)
        cap = float(getattr(cfg, "PEARL_BONUS_CAP", 15.0))

        if pearl_score >= ss_min:
            tier = "SS"
        elif pearl_score >= s_min:
            tier = "S"
        elif pearl_score >= a_min:
            tier = "A"
        elif pearl_score >= b_min:
            tier = "B"
        else:
            tier = None

        raw_bonus = float(bonus_map.get(tier, 0.0)) if tier else 0.0
        bonus = round(min(cap, max(0.0, raw_bonus)), 1)
        return tier, bonus

    except Exception:
        return None, 0.0


# ==============================================================
# BLOCK 7: キャッシュ管理
# ==============================================================

def _cache_dir(cfg) -> Path:
    """キャッシュディレクトリパスを返す（存在しなければ作成）。"""
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def save_pearl_candidate(
    ticker:       str,
    pearl_result: dict,
    cfg=None,
) -> None:
    """
    高スコア銘柄（Tier B以上）を pearl_candidates.json に追記する。
    TTL: PEARL_CACHE_TTL_DAYS（デフォルト7日）

    Args:
        ticker:       銘柄コード（例: "6758.T" / "NVDA"）
        pearl_result: calc_pearl_score() の返り値
        cfg:          config モジュール
    """
    try:
        tier = pearl_result.get("pearl_tier")
        if tier is None:
            return   # Tier なし（score < 50）は記録しない

        ttl_days = int(getattr(cfg, "PEARL_CACHE_TTL_DAYS", 7))
        cutoff   = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
        today    = datetime.now().strftime("%Y-%m-%d")
        p        = _cache_dir(cfg) / _CACHE_FILES["candidates"]

        existing: list = []
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                # TTL を超えた古いエントリを除去
                existing = [
                    e for e in raw if e.get("date", "0000") >= cutoff
                ]
            except Exception:
                pass

        entry = {
            "ticker":      ticker,
            "date":        today,
            "pearl_score": pearl_result.get("pearl_score", 0),
            "pearl_tier":  tier,
            "pearl_bonus": pearl_result.get("pearl_bonus", 0),
            "d1_score":    pearl_result.get("d1_score"),
            "d3_score":    pearl_result.get("d3_score"),
            "d4_score":    pearl_result.get("d4_score"),
            "d2_score":    pearl_result.get("d2_score"),
            "d5_score":    pearl_result.get("d5_score"),
        }
        # 同一銘柄・同日の重複を除去して追加
        existing = [e for e in existing
                    if not (e["ticker"] == ticker and e["date"] == today)]
        existing.append(entry)

        # Tier SS/S を先頭に並べてソート
        tier_order = {"SS": 0, "S": 1, "A": 2, "B": 3}
        existing.sort(key=lambda x: (
            tier_order.get(x.get("pearl_tier", "B"), 9),
            -x.get("pearl_score", 0)
        ))

        p.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def append_pearl_history(
    ticker:       str,
    pearl_result: dict,
    cfg=None,
) -> None:
    """
    pearl_history.json にスコア履歴を追記する。
    TTL: PEARL_HISTORY_TTL_DAYS（デフォルト180日）
    """
    try:
        ttl_days = int(getattr(cfg, "PEARL_HISTORY_TTL_DAYS", 180))
        cutoff   = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
        today    = datetime.now().strftime("%Y-%m-%d")
        p        = _cache_dir(cfg) / _CACHE_FILES["history"]

        history: dict = {}
        if p.exists():
            try:
                history = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass

        if ticker not in history:
            history[ticker] = []

        # 古いエントリを除去
        history[ticker] = [
            e for e in history[ticker] if e.get("date", "0000") >= cutoff
        ]
        # 同日の重複を除去して追加
        history[ticker] = [e for e in history[ticker] if e.get("date") != today]
        history[ticker].append({
            "date":        today,
            "pearl_score": pearl_result.get("pearl_score", 0),
            "pearl_tier":  pearl_result.get("pearl_tier"),
            "d1_score":    pearl_result.get("d1_score"),
            "d3_score":    pearl_result.get("d3_score"),
            "d4_score":    pearl_result.get("d4_score"),
        })

        p.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def update_direction_momentum(
    ticker:       str,
    d1_score:     float,
    cfg=None,
) -> None:
    """
    direction_momentum.json に D1スコアの時系列を記録する。
    TTL: PEARL_DIRECTION_TTL_DAYS（デフォルト90日）
    """
    try:
        ttl_days = int(getattr(cfg, "PEARL_DIRECTION_TTL_DAYS", 90))
        cutoff   = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
        today    = datetime.now().strftime("%Y-%m-%d")
        p        = _cache_dir(cfg) / _CACHE_FILES["direction"]

        data: dict = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass

        if ticker not in data:
            data[ticker] = {}

        # TTL を超えた日付を除去
        data[ticker] = {
            d: v for d, v in data[ticker].items() if d >= cutoff
        }
        data[ticker][today] = round(d1_score, 1)

        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def update_smart_money_trail(
    ticker:        str,
    analyst_upside: Optional[float],
    cfg=None,
) -> None:
    """
    smart_money_trail.json にアナリスト目標乖離の時系列を記録する。
    Ph1 では analyst_upside の推移でスマートマネー動向を近似する。
    TTL: PEARL_SMART_MONEY_TTL_DAYS（デフォルト90日）
    """
    try:
        if analyst_upside is None:
            return
        ttl_days = int(getattr(cfg, "PEARL_SMART_MONEY_TTL_DAYS", 90))
        cutoff   = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
        today    = datetime.now().strftime("%Y-%m-%d")
        p        = _cache_dir(cfg) / _CACHE_FILES["smart_money"]

        data: dict = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass

        if ticker not in data:
            data[ticker] = {}

        data[ticker] = {
            d: v for d, v in data[ticker].items() if d >= cutoff
        }
        data[ticker][today] = round(float(analyst_upside), 4)

        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ==============================================================
# BLOCK 8: メインエントリポイント
# ==============================================================

def calc_pearl_score(
    data:   dict,
    cfg=None,
    market: str = "JP",
    ticker: str = "",
    save_cache: bool = True,
) -> dict:
    """
    Pearl Score（光る原石スコア）を計算して返す。

    stock_screener.py の _calc_scores() 末尾から呼ばれる。

    Args:
        data:       screener/yfinance取得データ辞書
        cfg:        config モジュール（None 可）
        market:     "JP" or "US"
        ticker:     銘柄コード（キャッシュ保存用。空文字の場合は保存なし）
        save_cache: True のとき pearl_candidates / pearl_history に保存

    Returns:
        {
          "pearl_score":   float,        # 総合Pearl Score（0〜100）
          "pearl_tier":    str|None,     # "SS"/"S"/"A"/"B"/None
          "pearl_bonus":   float,        # quality_score への加算ボーナス（pt）
          "d1_score":      float,        # 方向性スコア
          "d3_score":      float,        # 利益品質スコア
          "d4_score":      float,        # 発見ギャップスコア
          "d2_score":      None,         # Ph1未実装
          "d5_score":      None,         # Ph1未実装
          "flags":         list[str],    # スコア根拠フラグリスト
          "m_score_proxy": float,        # M-Score proxy値（参考）
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    try:
        if not getattr(cfg, "ENABLE_PEARL_SCORE", True):
            return _zero_result()

        # ── 次元重みの取得（5次元 / Wave 3 - Pearl Ph3）────────────
        weights = getattr(cfg, "PEARL_DIMENSION_WEIGHTS", _DEFAULT_WEIGHTS)
        w1 = float(weights.get("D1", 0.25))
        w2 = float(weights.get("D2", 0.15))   # Wave 3 追加
        w3 = float(weights.get("D3", 0.30))
        w4 = float(weights.get("D4", 0.15))
        w5 = float(weights.get("D5", 0.15))

        # ── 各次元スコアの計算 ────────────────────────────────────
        d1_score, d1_flags  = _calc_d1_direction(data)
        d2_score, d2_flags  = _calc_d2_smart_money(data, market, cfg)  # Wave 3 Ph3
        d3_score, d3_flags, m_score_3v = _calc_d3_earnings_quality(data, cfg)
        d4_score, d4_flags  = _calc_d4_discovery_gap(data, market)
        d5_score, d5_flags  = _calc_d5_contrarian(data, market)
        # 2026-10-05: D5 ＝ 割安度（バリュエーション）50% ＋ 逆張り（市場比の割負け）50%
        _val, _val_flags = _calc_d5_valuation(data, market)
        if _val is not None:
            d5_score = round(0.5 * _val + 0.5 * d5_score, 1)
            d5_flags = [f"割安度{_val:.0f}点（{' / '.join(_val_flags)}）"] + d5_flags

        # ── Ph3 総合スコア計算（5次元・D2実装版）─────────────────
        # $pearl\_score = D1 \times w1 + D2 \times w2 + D3 \times w3$
        #              $+ D4 \times w4 + D5 \times w5$
        pearl_score = round(
            d1_score * w1
            + d2_score * w2
            + d3_score * w3
            + d4_score * w4
            + d5_score * w5,
            1,
        )
        pearl_score = min(100.0, max(0.0, pearl_score))

        # ── Wave 7: 潜伏Pearl昇格銘柄へのボーナス付与 ────────
        # latent_pearl_manager.py で promotion_eligible == True の銘柄は
        # pearl_score に上乗せボーナスを加算する。
        # $pearl_{final} = pearl_{base} + latent\\_bonus$
        # $latent\\_bonus = (latent\\_score / 100) \\times LATENT\\_PEARL\\_PROMOTION\\_BONUS\\_MAX$
        try:
            _ticker = ticker or data.get("ticker", data.get("Symbol", ""))
            # 2026-07-25: ENABLE_LATENT_PEARL_BONUS を配線（D3三種と同じ理由）。
            # 上限値も config.LATENT_PEARL_PROMOTION_BONUS_MAX を正本にする。
            # 従来はモジュール定数 _LATENT_PEARL_PROMOTION_BONUS_MAX が固定で、
            # config 側の同名定数が誰にも読まれていなかった（SSOT違反）。
            if (_ticker and cfg is not None
                    and getattr(cfg, "ENABLE_LATENT_PEARL_BONUS", True)):
                import json as _json
                from pathlib import Path as _Path
                lp_path = _Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "latent_pearl_cache.json"
                if lp_path.exists():
                    lp_data  = _json.loads(lp_path.read_text(encoding="utf-8"))
                    lp_entry = lp_data.get("latent_pearls", {}).get(_ticker, {})
                    if lp_entry.get("promotion_eligible"):
                        lp_score    = float(lp_entry.get("latent_pearl_score", 0.0))
                        _lp_max = float(getattr(
                            cfg, "LATENT_PEARL_PROMOTION_BONUS_MAX",
                            _LATENT_PEARL_PROMOTION_BONUS_MAX))
                        latent_bonus = round((lp_score / 100.0) * _lp_max, 2)
                        pearl_score = min(100.0, pearl_score + latent_bonus)
                        # all_flags は後段で定義されるため、
                        # Wave 7ボーナスフラグは d3_flags に一時格納する
                        d3_flags.append(
                            f"[Wave7] 潜伏Pearl昇格ボーナス+{latent_bonus:.1f}pt💎"
                        )
        except Exception:
            pass

        # ── ティア・ボーナス判定 ──────────────────────────────────
        tier, bonus = _classify_pearl_tier(pearl_score, cfg)

        # ── フラグ統合 ────────────────────────────────────────────
        all_flags = []
        if d1_flags:
            all_flags.extend([f"[D1] {f}" for f in d1_flags])
        if d2_flags:
            all_flags.extend([f"[D2] {f}" for f in d2_flags])   # Wave 3 Ph3
        if d3_flags:
            all_flags.extend([f"[D3] {f}" for f in d3_flags])
        if d4_flags:
            all_flags.extend([f"[D4] {f}" for f in d4_flags])
        if d5_flags:
            all_flags.extend([f"[D5] {f}" for f in d5_flags])

        result = {
            "pearl_score":   pearl_score,
            "pearl_tier":    tier,
            "pearl_bonus":   bonus,
            "d1_score":      d1_score,
            "d2_score":      d2_score,   # Wave 3 - Pearl Ph3 実装
            "d3_score":      d3_score,
            "d4_score":      d4_score,
            "d5_score":      d5_score,
            "flags":         all_flags,
            "m_score_proxy": m_score_3v,
        }

        # ── キャッシュ保存（ticker がある場合のみ）─────────────────
        if save_cache and ticker and cfg is not None:
            try:
                save_pearl_candidate(ticker, result, cfg)
                append_pearl_history(ticker, result, cfg)
                update_direction_momentum(ticker, d1_score, cfg)
                update_smart_money_trail(
                    ticker, data.get("analyst_upside"), cfg
                )
            except Exception:
                pass

        return result

    except Exception as e:
        return _zero_result(error=str(e)[:80])


def _zero_result(error: str = "") -> dict:
    """エラー時・スキップ時のゼロ値を返す。"""
    return {
        "pearl_score":   0.0,
        "pearl_tier":    None,
        "pearl_bonus":   0.0,
        "d1_score":      0.0,
        "d3_score":      0.0,
        "d4_score":      0.0,
        "d2_score":      None,
        "d5_score":      0.0,    # Ph2実装済み。エラー時は0.0
        "flags":         [f"Pearl計算スキップ: {error}"] if error else [],
        "m_score_proxy": 0.0,
        "edinet_segment_bonus": 0.0,
        "catalyst_bonus":       0.0,
        "midterm_bonus":        0.0,
        "latent_pearl_bonus":   0.0,
    }


# ==============================================================
# BLOCK 9: ユーティリティ — pearl_candidates.json 読み込み
# ==============================================================

def load_pearl_candidates(cfg=None) -> list:
    """
    weekly_report_generator._section_pearl_ranking() から呼ばれる。
    pearl_candidates.json から有効期限内の高スコア銘柄リストを返す。

    Returns:
        [{"ticker": str, "pearl_score": float, "pearl_tier": str, ...}, ...]
        Tier SS → S → A → B → pearl_score 降順でソート済み
    """
    try:
        if cfg is None:
            return []
        ttl_days = int(getattr(cfg, "PEARL_CACHE_TTL_DAYS", 7))
        cutoff   = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
        p        = _cache_dir(cfg) / _CACHE_FILES["candidates"]
        if not p.exists():
            return []
        raw = json.loads(p.read_text(encoding="utf-8"))
        valid = [e for e in raw if e.get("date", "0000") >= cutoff]
        tier_order = {"SS": 0, "S": 1, "A": 2, "B": 3}
        valid.sort(key=lambda x: (
            tier_order.get(x.get("pearl_tier", "B"), 9),
            -x.get("pearl_score", 0),
        ))
        return valid
    except Exception:
        return []


# ==============================================================
# BLOCK 10: 単体テスト
# ==============================================================

if __name__ == "__main__":
    import sys

    # ダミーデータ（JP株の典型的な小型成長株）
    test_data_jp = {
        "week52_change":    0.38,       # 38%上昇
        "earnings_growth":  0.12,       # 年次12%増益
        "earnings_qgrowth": 0.20,       # 四半期20%増益
        "analyst_upside":   0.28,       # 目標株価28%割れ
        "beta":             0.75,       # 低ボラ
        "accrual_ratio":   -0.03,       # アクルーアル良好
        "free_cashflow":    500_000_000, # 5億円FCF黒字
        "operating_cashflow": 700_000_000,
        "roe":              0.18,       # ROE 18%
        "gross_margin":     0.45,       # 粗利率45%
        "revenue_growth":   0.15,       # 売上15%成長
        "analyst_count":    3,          # アナリスト3人
        "market_cap_b":     280.0,      # 時価総額280億円
        "discovery_score":  65.0,       # [S-3] スコア
    }

    print("=" * 60)
    print("  🔮 pearl_score_calculator.py v1.0 単体テスト")
    print("=" * 60)

    result = calc_pearl_score(
        data      = test_data_jp,
        cfg       = None,
        market    = "JP",
        ticker    = "TEST.T",
        save_cache= False,
    )

    print(f"\n  Pearl Score : {result['pearl_score']:.1f}点")
    print(f"  Pearl Tier  : {result['pearl_tier'] or '(対象外)'}")
    print(f"  Pearl Bonus : +{result['pearl_bonus']:.1f}pt (quality_scoreへ)")
    print(f"  D1（方向性）  : {result['d1_score']:.1f}点")
    print(f"  D3（利益品質）: {result['d3_score']:.1f}点")
    print(f"  D4（発見Gap） : {result['d4_score']:.1f}点")
    print(f"  M-Score proxy: {result['m_score_proxy']:.3f}"
          f" ({'操作リスク⚠️' if result['m_score_proxy'] > -2.22 else '正常✅'})")
    print(f"\n  根拠フラグ:")
    for f in result["flags"]:
        print(f"    • {f}")

    # ダミーデータ（US株のSaaS典型）
    test_data_us = {
        "week52_change":    0.65,
        "earnings_growth":  None,
        "earnings_qgrowth": 0.45,
        "analyst_upside":   0.35,
        "beta":             1.3,
        "accrual_ratio":    0.02,
        "free_cashflow":    -50_000_000,
        "roe":              0.05,
        "gross_margin":     0.72,
        "revenue_growth":   0.35,
        "analyst_count":    8,
        "market_cap":       5_000_000_000,  # 50億USD
        "discovery_score":  None,
    }
    result_us = calc_pearl_score(test_data_us, cfg=None, market="US",
                                  ticker="TEST_US", save_cache=False)
    print(f"\n  [US株テスト] Pearl Score: {result_us['pearl_score']:.1f}点 "
          f"Tier:{result_us['pearl_tier'] or '(対象外)'}")
