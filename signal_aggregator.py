# ==============================================================
# signal_aggregator.py  v1.0  (4-9)
# 全ファクター統合スコア（0〜100）生成
# ==============================================================
# 【役割】
#   全モジュールの出力を受け取り、「今この業種を買うべき総合シグナル」を
#   0〜100 スコア1本で表現する。LLM不要・純粋なif-elif連鎖で実装。
#
# 【統合するシグナル源（9カテゴリ）】
#   A. セクタースコア        - composite_score（基幹指標）
#   B. マクロシグナル        - MACRO_RULES 発動数と重み
#   C. テクニカルスコア      - RSI/MACD/BB/MA200
#   D. 市場幅指標            - market_breadth (AD線・新高値比率)
#   E. オプションセンチメント - options_monitor PCR/VIX
#   F. 季節アノマリー        - anomaly_season 月次t検定
#   G. JP×US先行シグナル     - jp_us_sector_bridge lag相関
#   H. テーマETF熱量         - thematic_etf_tracker heat_score
#   I. 債券イールドカーブ     - bond_commodity recession_score
#
# 【重み体系】
#   デフォルト重みは config.py に定義。
#   rule_optimizer.py が月次で更新した weights があれば上書き。
#
# 【出力スキーマ】
#   {
#     "sector_signals": {
#       "1016": {
#         "signal_score":   78.5,    # 0〜100 総合スコア
#         "signal_label":   "🟢 買いシグナル",
#         "components":     {A:xx, B:xx, ...},  # 各カテゴリの寄与
#         "top_reasons":    ["MACD上昇中", "新高値比率72%", ...],
#         "risk_flags":     ["VIX急騰中", ...],
#       },
#     },
#     "market_regime":  str,   # "リスクオン" / "中立" / "リスクオフ"
#     "top_buy":        [sector_code, ...],
#     "top_avoid":      [sector_code, ...],
#     "timestamp":      str,
#   }
#
# 【依存】results dict（全モジュールの結果を統合）/ config.py
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

# ── デフォルト重み（rule_optimizer が上書き可能） ─────────────
DEFAULT_WEIGHTS = {
    "sector_score":        0.21,   # A: composite_score ※0.28→0.26→0.23→0.21（Wave R2-6: Vへ0.02移譲。決定4=33業種は粗フィルタ化への段階移行）
    "macro_signals":       0.13,   # B: MACRO_RULES シグナル ※0.17→0.15→0.13（Wave 7調整）
    "technical_score":     0.15,   # C: テクニカル
    "market_breadth":      0.10,   # D: 市場幅
    "options_sentiment":   0.08,   # E: オプション恐怖/強欲
    "anomaly_season":      0.03,   # F: 季節アノマリー
    "us_lead_signal":      0.03,   # G: JP×US先行シグナル ※0.07→0.05→0.03（Wave 7調整）
    "etf_heat":            0.01,   # H: テーマETF熱量 ※0.03→0.02→0.01（Wave 4調整）
    "yield_curve":         0.01,   # I: イールドカーブ
    "pearl_signal":        0.04,   # J: 光る原石スコア（Wave 0 - Pearl Ph1）
    "independence_signal": 0.04,   # K: Independence Score（Wave 3 - Independence Ph1）
    "politician_signal":   0.02,   # L: 議員取引シグナル ※0.03→0.02（Wave 4調整）
    "institutional_signal": 0.01,  # M: 機関投資家13Fシグナル（Wave 4 - Policy Ph3）
    "geo_signal":          0.04,   # N: 地政学×権益統合シグナル（Wave 6 - G-X）
    "cluster_phase_bonus": 0.01,  # Wave 6 - B: 技術クラスターフェーズボーナス
    "hiring_signal":       0.01,   # O: 人材投資シグナル（Wave 6 - I-1）
    "exec_transition":     0.01,   # P: 経営方針転換シグナル（Wave 6 - I-3）
    "facility_signal":     0.01,   # Q: 設備投資シグナル（Wave 6 - I-4）
    "standardization_signal": 0.01,  # R: 標準化動向シグナル（Wave 7）
    "academic_signal":        0.01,  # S: 学術カンファレンスシグナル（Wave 7）
    "midterm_signal":         0.01,  # T: 中計達成率シグナル（Wave 7）
    "taxonomy_size_bonus":    0.01,  # U: taxonomy×size交差評価ボーナス（Wave 7）
    "taxonomy_signal":        0.02,  # V: taxonomy主スコア＝node_hotness（Wave R2-6/T-5・初期重み=他21カテゴリ平均(約0.045)の50%≒0.02。E4のIC判定で昇降格）
}
# 重み合計検証:
# 0.21+0.13+0.15+0.10+0.08+0.03+0.03+0.01+0.01+0.04+0.04+0.02+0.01+0.04+0.01+0.01+0.01+0.01+0.01+0.01+0.01+0.01+0.02 = 1.00

# ── 重み合計バリデーション（合計 ≠ 1.00 なら起動時に警告） ──────
_WEIGHT_SUM = round(sum(DEFAULT_WEIGHTS.values()), 10)
assert abs(_WEIGHT_SUM - 1.0) < 1e-9, (
    f"[signal_aggregator] DEFAULT_WEIGHTS の合計が 1.00 ではありません: "
    f"{_WEIGHT_SUM:.10f}"
)

# シグナルスコアのラベル
def _signal_label(score: float) -> str:
    if score >= 72:   return "🟢 強い買いシグナル"
    if score >= 60:   return "🟢 買いシグナル"
    if score >= 52:   return "🟡 やや強気"
    if score >= 45:   return "⚪ 中立"
    if score >= 35:   return "🟠 やや弱気"
    if score >= 25:   return "🔴 売りシグナル"
    return "🔴 強い売りシグナル"


# ==============================================================
# BLOCK 1: 各カテゴリからスコアを抽出
# ==============================================================

def _extract_sector_score(sector_results: dict, code: str) -> Optional[float]:
    """A: composite_score を 0〜100 に正規化して返す。"""
    data = sector_results.get(code)
    if not isinstance(data, dict):
        return None
    score = data.get("composite_score")
    return float(score) if score is not None else None


def _extract_macro_score(sector_results: dict, code: str) -> Optional[float]:
    """
    B: macro_signals から追い風・逆風の数 × 重みで 0〜100 スコアを算出。
    追い風1件=+10pt / 逆風1件=-10pt / 中心50pt スタート。
    """
    data = sector_results.get(code)
    if not isinstance(data, dict):
        return None
    signals = data.get("macro_signals", [])
    tailwind = sum(1 for s in signals if s.get("signal") == "tailwind")
    headwind = sum(1 for s in signals if s.get("signal") == "headwind")
    raw = 50 + (tailwind - headwind) * 10
    return round(max(0, min(100, raw)), 1)


def _extract_technical_score(results: dict, code: str) -> Optional[float]:
    """C: technical_scorer キャッシュ → 業種代表銘柄のテクニカルスコア平均。"""
    # sector_resultsにtechnical調整が入っていれば直接使う
    sector_data = results.get("sectors", {}).get(code, {})
    if isinstance(sector_data, dict) and sector_data.get("technical_score"):
        return float(sector_data["technical_score"])
    # screenerキャッシュから業種代表銘柄のスコアを平均
    ts_results = results.get("technical_scorer", {}).get("results", {})
    if not ts_results:
        return None
    # JP_US_BRIDGE に載っている業種代表ティッカーを参照
    sector_tickers_map = {
        "1016": ["6758.T","6501.T"], "1017": ["7203.T","7267.T"],
        "1028": ["8306.T","8316.T"], "1008": ["4502.T","4523.T"],
        "1015": ["6301.T","6326.T"], "1025": ["9432.T","9984.T"],
        "1020": ["9501.T","9502.T"], "1032": ["8801.T","8802.T"],
    }
    tickers = sector_tickers_map.get(code, [])
    scores  = [ts_results[t]["score"] for t in tickers if ts_results.get(t, {}).get("score")]
    return round(sum(scores)/len(scores), 1) if scores else None


def _extract_breadth_score(results: dict, code: str) -> Optional[float]:
    """D: market_breadth の JP スコアを業種共通で使う（+/- 補正あり）。"""
    mb = results.get("market_breadth", {})
    if not mb:
        return None
    base  = mb.get("breadth_score_jp", 50.0)
    # ダイバージェンス発生時は上位業種に追加ペナルティ
    div   = mb.get("divergence", {})
    adj   = mb.get("macro_adjustment", {})
    delta = adj.get(code, 0)
    return round(max(0, min(100, base + delta)), 1)


def _extract_options_score(results: dict, code: str) -> Optional[float]:
    """
    E: options_monitor センチメントスコアを業種ごとに変換。
    恐怖時（低スコア）はディフェンシブに有利、楽観時は景気敏感に有利。
    """
    om = results.get("options_monitor", {})
    if not om:
        return None
    sent_score = om.get("sentiment", {}).get("score", 50.0)
    adj        = om.get("adjustments", {}).get(code, 0)
    # オプション補正を business_base に加算
    business_base = 50.0
    return round(max(0, min(100, business_base + adj + (sent_score - 50) * 0.2)), 1)


def _extract_anomaly_score(results: dict, code: str) -> Optional[float]:
    """F: anomaly_season の現在月スコアを返す（中立50 + 補正）。"""
    as_r = results.get("anomaly_season", {})
    if not as_r:
        return None
    adj = as_r.get("adjustments", {}).get(code, 0)
    return round(max(0, min(100, 50 + adj * 5)), 1)   # adj±3pt → score 35〜65


def _extract_us_lead_score(results: dict, code: str) -> Optional[float]:
    """
    G: JP×US先行シグナル補正 → スコア変換。
    Wave 3 - Size Ph3: cross_size_signal の業種別強度を追加合算する。
    score = base_bridge_score * 0.6 + cross_size_score * 0.4
    """
    bridge = results.get("jp_us_bridge", {})
    if not bridge:
        return None
    adj        = bridge.get("lead_adjustments", {}).get(code, 0)
    phase_data = bridge.get("rotation_phases", {}).get(code, {})
    phase      = phase_data.get("phase", "LAGGING")
    phase_bonus = {
        "LEADING": 15, "IMPROVING": 10, "WEAKENING": -10, "LAGGING": -15,
    }.get(phase, 0)
    bridge_score = round(max(0, min(100, 50 + adj * 5 + phase_bonus)), 1)

    # ── Wave 3 - Size Ph3: cross_size_signal を合算 ────────────
    try:
        from cross_size_signal import aggregate_cross_size_score as _css_score
        cross_score = _css_score(code, results)
        # 60%: 既存 bridge / 40%: cross_size
        combined = round(bridge_score * 0.6 + cross_score * 0.4, 1)
        return max(0.0, min(100.0, combined))
    except Exception:
        return bridge_score


def _extract_etf_heat_score(results: dict, code: str) -> Optional[float]:
    """H: thematic_etf_tracker の heat_score を JP 業種にマッピング。"""
    etf = results.get("etf_tracker", {})
    if not etf:
        return None
    # テーマ → JP業種コードの大まかなマッピング
    theme_sector_map = {
        "AI": ["1016", "1025"], "semiconductor": ["1016"],
        "clean_energy": ["1020", "1009"], "biotech": ["1008"],
        "robotics": ["1015", "1016"], "fintech": ["1028", "1029"],
    }
    scores = []
    for theme_result in etf.get("themes", []):
        name       = theme_result.get("theme_name", "").lower()
        heat       = theme_result.get("heat_score", 50)
        for keyword, codes in theme_sector_map.items():
            if keyword in name and code in codes:
                scores.append(heat)
    return round(sum(scores)/len(scores), 1) if scores else None


def _extract_yield_curve_score(results: dict) -> Optional[float]:
    """I: bond_commodity の recession_score（低いほど良い）を逆転してスコア化。"""
    bc = results.get("bond_commodity", {})
    if not bc or not isinstance(bc, dict):
        return None
    # ★修正: run_bond_commodity_pipeline() はトップレベルに recession_score を返す
    # "yield_curve" というキーは存在しない（正しくは "curve_analysis"）
    rec_score = bc.get("recession_score")
    if rec_score is None:
        # フォールバック: curve_analysis 内も探す
        curve = bc.get("curve_analysis", {})
        if isinstance(curve, dict):
            rec_score = curve.get("recession_score")
    if rec_score is None:
        return None
    # recession_score が低い（=景気後退リスク低）→ signal_score が高い
    return round(max(0, min(100, 100 - float(rec_score))), 1)

def _extract_pearl_score(results: dict, sector_code: str):
    """
    [Pearl Ph1] screener 通過銘柄の pearl_score をセクター別に集計する。
    対象セクターの通過銘柄が存在しない / pearl_score 未計算の場合は None を返す。
    """
    try:
        screener = results.get("screener", {})
        if not screener or screener.get("skipped"):
            return None
        passed = screener.get("passed", screener.get("candidates", []))
        pearls = [
            float(c["pearl_score"])
            for c in passed
            if str(c.get("sector_code", "")) == str(sector_code)
            and c.get("pearl_score") is not None
            and float(c.get("pearl_score", 0)) > 0
        ]
        if not pearls:
            return None
        return round(sum(pearls) / len(pearls), 1)
    except Exception:
        return None

def _extract_independence_score(results: dict, code: str) -> Optional[float]:
    """
    K: Independence Score（Wave 3 - Independence Ph1）→ セクター別スコア。

    screener 通過銘柄の independence_score 平均を返す。
    候補なし / 計算未実施の場合は cross_size_signal のスコアでフォールバック。

    Returns:
        float: 0〜100（独立性が高いほど高スコア）| None
    """
    try:
        # ── ① screener 候補の independence_score 平均 ──────────
        screener = results.get("screener", {})
        passed   = screener.get("passed", screener.get("candidates", []))
        ind_scores = [
            float(c["independence_score"])
            for c in passed
            if str(c.get("sector_code", "")) == str(code)
            and c.get("independence_score") is not None
            and float(c.get("independence_score", 0)) > 0
        ]
        if ind_scores:
            return round(sum(ind_scores) / len(ind_scores), 1)

        # ── ② フォールバック: cross_size_signal の intensity ────
        try:
            from cross_size_signal import aggregate_cross_size_score as _css
            return _css(code, results)
        except Exception:
            return None

    except Exception:
        return None


def _extract_politician_score(results: dict, code: str) -> Optional[float]:
    """
    L: 議員取引シグナル（Wave 3 - Policy Ph2）→ セクター別スコア。

    policy_signal_applier の effective_boosts / L4 sector_boosts を
    0〜100 スコアに変換する。
    score = 50 + effective\\_adj \\Times 2.0（adj range: -25〜+25 → score 0〜100）

    Returns:
        float: 0〜100（議員が買い越している業種ほど高スコア）| None
    """
    try:
        sc = str(code)

        # ── policy_signal_applier の有効ブーストを取得 ──────────
        pol_result = results.get("policy_signals", {})
        if pol_result:
            eff_boosts = pol_result.get("effective_boosts", {})
            l4_boosts  = pol_result.get("l4_sector_boosts", {})
            adj = float(eff_boosts.get(sc, 0.0))
            l4  = float(l4_boosts.get(sc, 0.0))
            # L4 Override は通常ブーストより優先・加算
            total_adj = adj + l4
            if total_adj != 0.0:
                return round(max(0, min(100, 50 + total_adj * 2.0)), 1)

        # ── フォールバック: politician_monitor の jp_sector_signals ─
        # ★ 2026-06-10 バグ修正: main の書き込みキーは "politician_monitor"
        pol_mon = results.get("politician_monitor", results.get("politician", {}))
        if pol_mon:
            raw_signals = pol_mon.get("jp_sector_signals", {})
            delta = float(raw_signals.get(sc, 0.0))
            if delta != 0.0:
                return round(max(0, min(100, 50 + delta * 3.0)), 1)

        return None

    except Exception:
        return None

def _extract_institutional_score(results: dict, code: str) -> Optional[float]:
    """
    M: 機関投資家13Fシグナル（Wave 4 - Policy Ph3）→ セクター別スコア。

    institutional_signal_scorer の sector_boosts を 0〜100 スコアに変換する。
    score = 50 + boost \\Times 2.0（boost range: -15〜+15 → score 20〜80）

    Tier 1（Buffett）の L3 Override 発動時は +10pt 上乗せ。

    Returns:
        float: 0〜100 | None（データなし時）
    """
    try:
        sc = str(code)

        # ── institutional_signal_scorer の sector_boosts を参照 ──
        inst_scorer = results.get("institutional_scorer", {})
        if inst_scorer:
            boosts     = inst_scorer.get("sector_boosts", {})
            boost      = float(boosts.get(sc, 0.0))
            l3_sectors = inst_scorer.get("override_sectors", [])

            if boost == 0.0 and sc not in l3_sectors:
                return None

            base_score = round(max(0.0, min(100.0, 50.0 + boost * 2.0)), 1)

            # L3 Override 業種は +10pt 上乗せ（screener順位変更の影響を反映）
            if sc in l3_sectors:
                base_score = round(min(100.0, base_score + 10.0), 1)

            return base_score

        # ── フォールバック: institutional_13f の sector_boosts を直接参照 ──
        inst_13f = results.get("institutional_13f", {})
        if inst_13f:
            boosts = inst_13f.get("sector_boosts", {})
            boost  = float(boosts.get(sc, 0.0))
            if boost != 0.0:
                return round(max(0.0, min(100.0, 50.0 + boost * 2.0)), 1)

        return None

    except Exception:
        return None

def _extract_hiring_score(results: dict, code: str) -> Optional[float]:
    """
    O: 人材投資シグナル（Wave 6 - I-1）→ セクター別スコア。

    job_posting_monitor の sector_boosts を 0〜100 スコアに変換する。
    score = 50 + boost \\Times 2.0（boost range: 0〜15 → score 50〜80）

    急増カテゴリなし → None（重み計算から除外）

    Returns:
        float: 0〜100 | None（データなし時）
    """
    try:
        sc  = str(code)
        jpm = results.get("job_posting", {})
        if not jpm or jpm.get("skipped"):
            return None

        boosts = jpm.get("sector_boosts", {})
        boost  = float(boosts.get(sc, 0.0))

        if boost == 0.0:
            return None

        return round(max(0.0, min(100.0, 50.0 + boost * 2.0)), 1)

    except Exception:
        return None


def _extract_exec_transition_score(results: dict, code: str) -> Optional[float]:
    """
    P: 経営方針転換シグナル（Wave 6 - I-3）→ セクター別スコア。

    executive_transition_tracker の sector_boosts を 0〜100 スコアに変換する。
    score = 50 + boost \\Times 2.0（boost range: 0〜15 → score 50〜80）

    転換シグナルなし → None（重み計算から除外）

    Returns:
        float: 0〜100 | None（データなし時）
    """
    try:
        sc  = str(code)
        ett = results.get("executive_transition", {})
        if not ett or ett.get("skipped"):
            return None

        boosts = ett.get("sector_boosts", {})
        boost  = float(boosts.get(sc, 0.0))

        if boost == 0.0:
            return None

        return round(max(0.0, min(100.0, 50.0 + boost * 2.0)), 1)

    except Exception:
        return None


def _extract_facility_score(results: dict, code: str) -> Optional[float]:
    """
    Q: 設備投資シグナル（Wave 6 - I-4）→ セクター別スコア。

    facility_expansion_monitor の sector_boosts を 0〜100 スコアに変換する。
    score = 50 + boost \\Times 2.0（boost range: 0〜15 → score 50〜80）

    着工シグナルなし → None（重み計算から除外）

    Returns:
        float: 0〜100 | None（データなし時）
    """
    try:
        sc  = str(code)
        fem = results.get("facility_expansion", {})
        if not fem or fem.get("skipped"):
            return None

        boosts = fem.get("sector_boosts", {})
        boost  = float(boosts.get(sc, 0.0))

        if boost == 0.0:
            return None

        return round(max(0.0, min(100.0, 50.0 + boost * 2.0)), 1)

    except Exception:
        return None

def _extract_geo_score(results: dict, code: str) -> float | None:
    """
    N: 地政学×権益統合シグナル（Wave 6 - G-X）→ セクター別スコア。

    geo_signal.aggregate_geo_signal() を呼び出して
    業種全体の geo_impact_bonus 平均を 0〜100 スコアに変換する。

    score = 50 + avg\\_geo\\_impact\\_bonus \times (50 / GEO\\_IMPACT\\_MAX)

    フォールバック優先順:
      1. geo_signal.aggregate_geo_signal()（BQ + screener 結果）
      2. results["sectors"][code] の geo_impact_bonus（build_result が格納）
      3. None（データなし）

    Returns:
        float: 0〜100 | None（データなし時）
    """
    try:
        # ── ① geo_signal モジュール経由（BQ 参照・最優先）────────
        try:
            import sys as _sys_gs, importlib as _imp_gs
            for _p in ["/content"]:
                if _p not in _sys_gs.path:
                    _sys_gs.path.insert(0, _p)
            _gs = _imp_gs.import_module("geo_signal")
            import config as _cfg_gs
            score = _gs.aggregate_geo_signal(
                sector_code = str(code),
                results     = results,
                cfg         = _cfg_gs,
                market      = "JP",
            )
            if score is not None:
                return float(score)
        except Exception:
            pass

        # ── ② results["sectors"] の geo_impact_bonus フォールバック ─
        sector_data = results.get("sectors", {}).get(str(code), {})
        if isinstance(sector_data, dict):
            bonus = sector_data.get("geo_impact_bonus")
            if bonus is not None:
                try:
                    _b = float(bonus)
                    # score = 50 + bonus \times (50 / 15.0)
                    score = round(
                        max(0.0, min(100.0, 50.0 + _b * (50.0 / 15.0))),
                        1,
                    )
                    return score
                except (TypeError, ValueError):
                    pass

        return None

    except Exception:
        return None

# ==============================================================
# BLOCK 1-B: Wave 7 シグナル抽出関数
# ==============================================================

def _extract_standardization_signal(results: dict, code: str) -> float | None:
    """
    R: 標準化動向シグナル（industry_standard_tracker.py）を抽出する。

    results["wave7_trackers"]["standardization"] の company_scores から
    当該業種に関連する企業の standardization_score 平均を返す。

    std\\_signal = \\frac{\\sum_{t \\in sector\\_tickers} standardization\\_score_t}{N}

    Returns:
        float 0.0〜100.0 または None
    """
    try:
        if not getattr(results.get("_cfg"), "ENABLE_STANDARDIZATION_TRACKER", False):
            return None
        std_result  = results.get("wave7_trackers", {}).get("standardization", {})
        co_scores   = std_result.get("company_scores", {})
        if not co_scores:
            return None

        # 業種コード → taxonomy_nodes でフィルタ
        sector_tickers = _get_tickers_for_sector(results, code)
        if not sector_tickers:
            return None

        vals = [
            float(co_scores[t]["standardization_score"])
            for t in sector_tickers
            if t in co_scores and co_scores[t].get("standardization_score") is not None
        ]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 1)
    except Exception:
        return None


def _extract_academic_signal(results: dict, code: str) -> float | None:
    """
    S: 学術カンファレンスシグナル（academic_conf_tracker.py）を抽出する。

    results["wave7_trackers"]["academic"] の company_scores から
    当該業種銘柄の academic_score 平均を返す。

    Returns:
        float 0.0〜100.0 または None
    """
    try:
        if not getattr(results.get("_cfg"), "ENABLE_ACADEMIC_CONF_TRACKER", False):
            return None
        ac_result  = results.get("wave7_trackers", {}).get("academic", {})
        co_scores  = ac_result.get("company_scores", {})
        if not co_scores:
            return None

        sector_tickers = _get_tickers_for_sector(results, code)
        if not sector_tickers:
            return None

        vals = [
            float(co_scores[t]["academic_score"])
            for t in sector_tickers
            if t in co_scores and co_scores[t].get("academic_score") is not None
        ]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 1)
    except Exception:
        return None


def _extract_midterm_signal(results: dict, code: str) -> float | None:
    """
    T: 中計達成率シグナル（midterm_plan_tracker.py）を抽出する。

    results["wave7_trackers"]["midterm"] の plans から
    当該業種銘柄の overall_score 平均を返す。
    trajectory == "critical" の銘柄はペナルティとして 0pt に変換。

    Returns:
        float 0.0〜100.0 または None
    """
    try:
        if not getattr(results.get("_cfg"), "ENABLE_MIDTERM_PLAN_TRACKER", False):
            return None
        mt_result = results.get("wave7_trackers", {}).get("midterm", {})
        plans     = mt_result.get("plans", {})
        if not plans:
            return None

        sector_tickers = _get_tickers_for_sector(results, code)
        if not sector_tickers:
            return None

        vals = []
        for t in sector_tickers:
            plan = plans.get(t)
            if not plan:
                continue
            score    = float(plan.get("overall_score", 50.0))
            traj     = plan.get("trajectory", "on_track")
            # trajectory == "critical" → 0pt（ペナルティ）
            if traj == "critical":
                score = 0.0
            elif traj == "ahead":
                score = min(100.0, score * 1.10)
            vals.append(score)

        if not vals:
            return None
        return round(sum(vals) / len(vals), 1)
    except Exception:
        return None


def _extract_taxonomy_size_bonus(results: dict, code: str) -> float | None:
    """
    U: taxonomy×size 交差評価ボーナス（size_tier_classifier.py Wave 7）を抽出する。

    size_tier_master.json の taxonomy_size_correction の平均から
    ボーナスポイントに変換する。

    bonus = (avg\\_correction - 1.0) \\times 50 + 50
    補正係数 1.0 → 50pt（中立）/ 1.2 → 60pt / 0.8 → 40pt

    Returns:
        float 0.0〜100.0 または None
    """
    try:
        sector_tickers = _get_tickers_for_sector(results, code)
        if not sector_tickers:
            return None

        # size_tier_master.json から taxonomy_size_correction を取得
        import json as _j
        from pathlib import Path as _p
        _cfg = results.get("_cfg")
        cache_dir = _p(getattr(_cfg, "CACHE_DIR", "stock_data/cache"))
        master_p  = cache_dir / "size_tier_master.json"
        if not master_p.exists():
            return None

        master = _j.loads(master_p.read_text(encoding="utf-8"))
        corrections = []
        for mkey in ("jp", "us"):
            for t in sector_tickers:
                entry = master.get(mkey, {}).get(t)
                if entry and entry.get("taxonomy_size_correction") is not None:
                    corrections.append(float(entry["taxonomy_size_correction"]))

        if not corrections:
            return None

        avg_corr = sum(corrections) / len(corrections)
        # score = (avg\\_corr - 1.0) \\times 50 + 50
        score = (avg_corr - 1.0) * 50.0 + 50.0
        return round(min(100.0, max(0.0, score)), 1)
    except Exception:
        return None


def _extract_taxonomy_signal(results: dict, code: str) -> float | None:
    """
    V: taxonomy主スコア（Wave R2-6 / T-5）を抽出する。

    taxonomy_hotness.py（T-4）が算出した node_hotness を業種→ノード橋渡し
    （size_tier_classifier._SECTOR_TO_TAXONOMY）で業種スコア化した
    results["taxonomy_signal"]["sector_scores"] を読む。
    決定4（HANDOFF_2026-06-30 §4-2）: 技術ツリーを主スコアへ段階移行し
    33業種は粗フィルタ化する — その第一歩としてカテゴリVを新設。

    Returns:
        float 0.0〜100.0 または None（taxonomy未算出・対象外業種）
    """
    try:
        scores = (results.get("taxonomy_signal") or {}).get("sector_scores") or {}
        v = scores.get(str(code))
        return round(float(v), 1) if v is not None else None
    except Exception:
        return None


def _get_tickers_for_sector(results: dict, sector_code: str) -> list:
    """
    指定業種コードに属するティッカーリストを screener 結果から取得する。
    signal_aggregator の Wave 7 抽出関数が共通で使用するユーティリティ。

    Returns:
        [ticker, ...]
    """
    try:
        passed = results.get("screener", {}).get("passed", [])
        tickers = [
            c.get("ticker") or c.get("Symbol", "")
            for c in passed
            if str(c.get("sector_code", "")) == str(sector_code)
            and (c.get("ticker") or c.get("Symbol"))
        ]
        return list(dict.fromkeys(tickers))  # 順序保持で重複排除
    except Exception:
        return []

# ==============================================================
# BLOCK 1.5: ローテーションブースト（Wave 0 - Idea2-B）
# ==============================================================

def _apply_rotation_boost(
    signal_score: float,
    sector_code:  str,
    results:      dict,
) -> float:
    """
    sector_rotation_detector の象限に基づき signal_score を補正する。

      rising  → +2.0 pt （底値からの反転 → 積極的買い）
      peak    → -1.0 pt （高値圏 → 過熱注意）
      trough  → +1.0 pt （底値圏 → 底打ち期待）
      falling →  0.0 pt （下落中 → 待ち、変更なし）

    ★ try/except で全体を保護し、失敗時は元の signal_score をそのまま返す。
    ★ sector_rotation_detector が未実行（results に "sector_rotation" がない）
      場合もブーストなし（0.0）で安全に通過する。
    """
    try:
        rotation = results.get("sector_rotation", {})
        if not rotation:
            return signal_score
        # insufficient_data フラグが True の場合はブーストしない
        if rotation.get("summary", {}).get("insufficient_data", False):
            return signal_score
        quadrant = rotation.get("quadrants", {}).get(sector_code)
        boost_map = {
            "rising":  +2.0,
            "peak":    -1.0,
            "trough":  +1.0,
            "falling":  0.0,
        }
        boost = boost_map.get(quadrant, 0.0)
        if boost == 0.0:
            return signal_score
        return round(max(0.0, min(100.0, signal_score + boost)), 1)
    except Exception:
        return signal_score

# ==============================================================
# BLOCK 1.7: Wave 2 - Size Ph2: Micro/Nano sector_score ウェイト低減
# ==============================================================

def _get_sector_size_weight_multiplier(
    sector_code: str,
    results:     dict,
) -> float:
    """
    screener 通過銘柄の size_tier 分布から sector_score ウェイト乗数を返す。

    【設計根拠】
    sector_score は大型株・業種代表銘柄の平均から算出されるため、
    Micro/Nano 銘柄が多いセクターでは sector_score の代表性が低下する。
    通過銘柄の中で Micro/Nano が占める割合に応じて乗数を低減し、
    calc_signal_score() の weighted_sum / weight_used による再正規化で
    他カテゴリのウェイト比率が自動的に上昇する。

    【判定ロジック】
    Micro/Nano 比率 ≥ 50% → get_sector_score_weight_multiplier("Micro") = 0.65
    Micro/Nano 比率 ≥ 20% → get_sector_score_weight_multiplier("Small") = 0.90
    その他                 → 1.00（変更なし）

    Args:
        sector_code: "1026" 等の JP/US セクターコード
        results:     run_signal_aggregator() に渡される全モジュール結果辞書

    Returns:
        float: sector_score ウェイト乗数（0.65〜1.00）
        ※ 取得失敗時は常に 1.00 を返す（安全サイドのフォールバック）
    """
    try:
        from size_decay_manager import get_sector_score_weight_multiplier as _get_mul

        screener = results.get("screener", {})
        if not screener or screener.get("skipped"):
            return 1.0

        # passed → candidates の順にフォールバック
        all_cands = screener.get("passed") or screener.get("candidates", [])

        # 当該セクターの通過銘柄のみ対象
        sector_cands = [
            c for c in all_cands
            if str(c.get("sector_code", "")) == str(sector_code)
            and c.get("pass_filters", False)
        ]
        if not sector_cands:
            return 1.0

        # size_tier 分布を集計
        total = len(sector_cands)
        micro_nano = sum(
            1 for c in sector_cands
            if c.get("size_tier", "Mid") in ("Micro", "Nano")
        )
        ratio = micro_nano / total

        # 閾値判定
        if ratio >= 0.50:
            mul = _get_mul("Micro")    # 0.65
        elif ratio >= 0.20:
            mul = _get_mul("Small")   # 0.90
        else:
            mul = 1.00

        return float(mul)

    except Exception:
        return 1.00

def _extract_cluster_score(row: dict, cfg) -> float:
    """
    Wave 6 - B: 技術クラスターフェーズボーナスを返す。

    cluster_phase_cache.json から sector_code に対応するクラスターの
    phase_bonus を取得して返す。

    cluster\\_phase\\_bonus \\in [CLUSTER\\_BONUS\\_MIN(-3), CLUSTER\\_BONUS\\_MAX(+8)]

    Args:
        row: シグナル行 dict。"sector_code" / "market" キーを使用。
        cfg: config モジュール。

    Returns:
        float: phase_bonus（キャッシュ未存在・エラー時は 0.0）
    """
    try:
        sector_code = str(row.get("sector_code", "") or "")
        market      = str(row.get("market", "JP")  or "JP")
        if not sector_code:
            return 0.0

        from technology_cluster_engine import get_cluster_phase_bonus
        bonus, phase, cluster_id = get_cluster_phase_bonus(
            sector_code=sector_code,
            market=market,
            cfg=cfg,
        )
        if bonus != 0.0:
            print(
                f"    🔬 クラスターボーナス [{sector_code}] "
                f"phase={phase} cluster={cluster_id}: {bonus:+.2f}pt"
            )
        return float(bonus)
    except Exception:
        return 0.0

# ==============================================================
# BLOCK 2: 統合スコア計算
# ==============================================================

def calc_signal_score(
    code:     str,
    results:  dict,
    weights:  dict,
) -> dict:
    """
    1業種の統合シグナルスコアを計算する。

    Returns:
        {
          "signal_score": float,
          "signal_label": str,
          "components":   dict,
          "top_reasons":  list,
          "risk_flags":   list,
          "data_coverage": float,  # 利用できたカテゴリの割合
        }
    """
    extractors = {
        "sector_score":        lambda: _extract_sector_score(results.get("sectors", {}), code),
        "macro_signals":       lambda: _extract_macro_score(results.get("sectors", {}), code),
        "technical_score":     lambda: _extract_technical_score(results, code),
        "market_breadth":      lambda: _extract_breadth_score(results, code),
        "options_sentiment":   lambda: _extract_options_score(results, code),
        "anomaly_season":      lambda: _extract_anomaly_score(results, code),
        "us_lead_signal":      lambda: _extract_us_lead_score(results, code),
        "etf_heat":            lambda: _extract_etf_heat_score(results, code),
        "yield_curve":         lambda: _extract_yield_curve_score(results),
        "pearl_signal":        lambda: _extract_pearl_score(results, code),
        "independence_signal": lambda: _extract_independence_score(results, code),  # Wave 3 K
        "politician_signal":   lambda: _extract_politician_score(results, code),    # Wave 3 L
        "institutional_signal": lambda: _extract_institutional_score(results, code), # Wave 4 M
        "geo_signal": lambda: _extract_geo_score(results, code),  # N: 地政学×権益統合シグナル（Wave 6 - G-X）
        "cluster_phase_bonus": lambda: _extract_cluster_score(results, code),   # Wave 6 - B: 技術クラスターフェーズボーナス
        "hiring_signal": lambda: _extract_hiring_score(results, code),   # O: 人材投資シグナル（Wave 6 - I-1）
        "exec_transition": lambda: _extract_exec_transition_score(results, code),   # P: 経営方針転換シグナル（Wave 6 - I-3）
        "facility_signal": lambda: _extract_facility_score(results, code),   # Q: 設備投資シグナル（Wave 6 - I-4）
        "standardization_signal": lambda: _extract_standardization_signal(results, code),  # R: 標準化（Wave 7）
        "academic_signal":        lambda: _extract_academic_signal(results, code),          # S: 学術（Wave 7）
        "midterm_signal":         lambda: _extract_midterm_signal(results, code),           # T: 中計（Wave 7）
        "taxonomy_size_bonus":    lambda: _extract_taxonomy_size_bonus(results, code),      # U: taxonomy×size（Wave 7）
        "taxonomy_signal":        lambda: _extract_taxonomy_signal(results, code),          # V: taxonomy主スコア（Wave R2-6/T-5）
    }

    components    = {}
    weighted_sum  = 0.0
    weight_used   = 0.0
    top_reasons   = []
    risk_flags    = []

    # ── [Wave 2 - Size Ph2] Micro/Nano比率 → sector_score ウェイト乗数取得 ──
    # ★ try/except は _get_sector_size_weight_multiplier() 内で吸収済み
    _sector_score_mul = _get_sector_size_weight_multiplier(code, results)

    for cat, extractor in extractors.items():
        try:
            score = extractor()
        except Exception:
            score = None

        w = weights.get(cat, DEFAULT_WEIGHTS.get(cat, 0.05))

        # Wave 2 - Size Ph2: Micro/Nano 比率が高いセクターの
        # sector_score ウェイトを自動低減する。
        # weight_used による再正規化で他カテゴリのウェイト比率が自動上昇する。
        if cat == "sector_score" and _sector_score_mul < 1.0:
            w = round(w * _sector_score_mul, 4)

        if score is not None:
            components[cat] = round(score, 1)
            weighted_sum  += score * w
            weight_used   += w

            # 理由・リスクフラグ収集
            if score >= 70 and cat in (
                "sector_score", "technical_score",
                "macro_signals", "us_lead_signal",
                "institutional_signal",              # Wave 4 M
                "geo_signal",                        # Wave 6 N
                "hiring_signal",                     # Wave 6 O
                "exec_transition",                   # Wave 6 P
                "facility_signal",                   # Wave 6 Q
                "standardization_signal",            # Wave 7 R
                "academic_signal",                   # Wave 7 S
                "midterm_signal",                    # Wave 7 T
                "taxonomy_size_bonus",               # Wave 7 U
                "taxonomy_signal",                   # Wave R2-6 V
            ):
                label_map = {
                    "sector_score":        f"セクタースコア高({score:.0f})",
                    "technical_score":     f"テクニカル買い({score:.0f})",
                    "macro_signals":       f"マクロ追い風({score:.0f})",
                    "us_lead_signal":      f"US先行強気({score:.0f})",
                    "institutional_signal": f"機関投資家買越({score:.0f})",
                    "geo_signal":          f"地政学追い風({score:.0f})",
                    "hiring_signal":       f"人材投資強気({score:.0f})",
                    "exec_transition":     f"経営方針転換({score:.0f})",
                    "facility_signal":     f"設備投資強気({score:.0f})",
                    "standardization_signal": f"標準化リード({score:.0f})",
                    "academic_signal":        f"学術発表強気({score:.0f})",
                    "midterm_signal":         f"中計達成追い風({score:.0f})",
                    "taxonomy_size_bonus":    f"規模×業種補正({score:.0f})",
                    "taxonomy_signal":        f"技術テーマ加熱({score:.0f})",
                }
                reason = label_map.get(cat)
                if reason:
                    top_reasons.append(reason)
            if score <= 30 and cat in (
                "sector_score", "market_breadth",
                "yield_curve",
                "institutional_signal",              # Wave 4 M
                "geo_signal",                        # Wave 6 N
            ):
                label_map = {
                    "sector_score":        f"セクタースコア低({score:.0f})",
                    "market_breadth":      f"市場幅悪化({score:.0f})",
                    "yield_curve":         f"景気後退リスク高({score:.0f})",
                    "institutional_signal": f"機関投資家売越({score:.0f})",
                    "midterm_signal":  f"中計未達リスク({score:.0f})",
                    "geo_signal":          f"地政学逆風({score:.0f})",
                    "hiring_signal":       f"人材投資弱気({score:.0f})",
                    "exec_transition":     f"経営方針転換({score:.0f})",
                    "facility_signal":     f"設備投資弱気({score:.0f})",
                }
                risk_flags.append(label_map.get(cat, f"{cat}={score:.0f}"))
            components[cat] = None   # データなし

    if weight_used < 1e-6:
        return {
            "signal_score": 50.0, "signal_label": "⚪ データ不足",
            "components": components, "top_reasons": [], "risk_flags": [],
            "data_coverage": 0.0,
        }

    signal_score   = round(weighted_sum / weight_used, 1)
    # ── Wave 6 - 第4セッション: 緊急縮退補正（カテゴリ単位） ─────
    # rule_optimizer.evaluate_contraction_flag() の判定結果が
    # results["rule_optimizer"]["contraction_flag"] に格納されている場合のみ適用。
    # 防御セクターには補正を適用せずボーナスを加算する（G-2/G-3/G-4）。
    # ★ try/except 厳重保護: 縮退ロジック未実行・cfg未配置時も動作に影響しない
    try:
        import sys as _sys_ca, importlib as _imp_ca
        from pathlib import Path as _pl_ca
        _sys_ca.path.insert(0, str(_pl_ca(__file__).parent))
        _ro_mod = _imp_ca.import_module("rule_optimizer")
        _contraction = (
            results.get("rule_optimizer", {}).get("contraction_flag", {})
        )
        if _contraction and _contraction.get("mode", "Normal") != "Normal":
            # sector_name を取得（コードから逆引き）
            _cfg_mod = _imp_ca.import_module("config")
            _sname = (
                _cfg_mod.JAPAN_SECTORS.get(code, {}).get("name_ja", "")
                if hasattr(_cfg_mod, "JAPAN_SECTORS") else ""
            )
            # カテゴリ別スコアに縮退係数を適用
            components = _ro_mod.apply_contraction_to_scores(
                scores      = components,
                contraction = _contraction,
                cfg         = _cfg_mod,
                sector_code = str(code),
                sector_name = _sname,
                market      = "JP",
            )
            # 縮退補正後のカテゴリ値で weighted_sum を再計算
            _new_wsum  = 0.0
            _new_wused = 0.0
            for _cat, _val in components.items():
                _w = weights.get(_cat, 0.0)
                if _val is not None:
                    try:
                        _new_wsum  += float(_val) * _w
                        _new_wused += _w
                    except (TypeError, ValueError):
                        pass
            if _new_wused > 1e-6:
                signal_score = round(_new_wsum / _new_wused, 1)
    except Exception:
        pass   # 縮退補正失敗時は元の signal_score を維持
    # Wave 0 Idea2-B: ローテーションブーストを適用（try/except は関数内で保護済み）
    signal_score = _apply_rotation_boost(signal_score, code, results)
    # ── Wave 7: taxonomy×size 交差評価ボーナスの直接補正 ─────────
    # _extract_taxonomy_size_bonus() は extractors 経由で weighted_sum に
    # 組み込まれているが、Feature Flag が OFF の場合でも
    # size_tier_master.json が存在すれば補正を直接適用する。
    # final\\_score = signal\\_score + \\min(3.0, (avg\\_corr - 1.0) \\times 10)
    try:
        _cfg_w7 = results.get("_cfg")
        if not getattr(_cfg_w7, "ENABLE_SIZE_DECAY_MANAGER", True):
            pass  # Feature Flag OFF → スキップ
        else:
            import json as _jw7
            from pathlib import Path as _pw7
            _tax_master_p = (
                _pw7(getattr(_cfg_w7, "CACHE_DIR", "stock_data/cache"))
                / "size_tier_master.json"
            ) if _cfg_w7 else None
            if _tax_master_p and _tax_master_p.exists():
                _tax_master = _jw7.loads(_tax_master_p.read_text(encoding="utf-8"))
                _sec_tickers = _get_tickers_for_sector(results, code)
                _corrs = []
                for _mkey in ("jp", "us"):
                    for _tk in _sec_tickers:
                        _te = _tax_master.get(_mkey, {}).get(_tk)
                        if _te and _te.get("taxonomy_size_correction") is not None:
                            _corrs.append(float(_te["taxonomy_size_correction"]))
                if _corrs:
                    _avg_corr = sum(_corrs) / len(_corrs)
                    # 補正係数が 1.0 を超える場合のみボーナス加算
                    # tax\\_boost = \\min(3.0, (avg\\_corr - 1.0) \\times 10)
                    _tax_boost = round(min(3.0, max(-3.0, (_avg_corr - 1.0) * 10.0)), 2)
                    if abs(_tax_boost) >= 0.1:
                        signal_score = round(
                            min(100.0, max(0.0, signal_score + _tax_boost)), 1
                        )
    except Exception:
        pass  # taxonomy×size 補正失敗時は元の signal_score を維持
    data_coverage  = round(weight_used / sum(DEFAULT_WEIGHTS.values()), 3)

    # ── Wave 6 - 第4セッション: 防御セクター最終保護 ─────────────
    # apply_contraction_to_scores() で補正済みだが、
    # 最終スコアとして apply_defensive_protection() で下限保護を掛ける。
    _is_defensive_flag = False
    try:
        _contraction_for_dp = (
            results.get("rule_optimizer", {}).get("contraction_flag", {})
        )
        if _contraction_for_dp and _contraction_for_dp.get("mode", "Normal") != "Normal":
            import sys as _sys_dp, importlib as _imp_dp
            from pathlib import Path as _pl_dp
            _sys_dp.path.insert(0, str(_pl_dp(__file__).parent))
            _ro_dp  = _imp_dp.import_module("rule_optimizer")
            _cfg_dp = _imp_dp.import_module("config")
            _sname_dp = (
                _cfg_dp.JAPAN_SECTORS.get(code, {}).get("name_ja", "")
                if hasattr(_cfg_dp, "JAPAN_SECTORS") else ""
            )
            _is_defensive_flag = _ro_dp.is_defensive_sector(
                sector_code = str(code),
                sector_name = _sname_dp,
                market      = "JP",
                cfg         = _cfg_dp,
            )
            signal_score = _ro_dp.apply_defensive_protection(
                signal_score = signal_score,
                contraction  = _contraction_for_dp,
                cfg          = _cfg_dp,
                sector_code  = str(code),
                sector_name  = _sname_dp,
                market       = "JP",
            )
    except Exception:
        pass

    return {
        "signal_score":           signal_score,
        "signal_label":           _signal_label(signal_score),
        "components":             components,
        "top_reasons":            top_reasons[:3],
        "risk_flags":             risk_flags[:3],
        "data_coverage":          data_coverage,
        "size_weight_multiplier": _sector_score_mul,  # Wave 2 - Size Ph2
        "is_defensive":           _is_defensive_flag,  # Wave 6 - 第4セッション
        "contraction_mode":       (
            results.get("rule_optimizer", {})
            .get("contraction_flag", {})
            .get("mode", "Normal")
        ),
    }


# ==============================================================
# BLOCK 3: 市場レジーム判定
# ==============================================================

def detect_market_regime(results: dict) -> str:
    """
    複数指標から市場全体のレジームを判定する。

    Returns:
        "リスクオン強" / "リスクオン" / "中立" / "リスクオフ" / "リスクオフ強"
    """
    scores = []

    # VIX
    om = results.get("options_monitor", {})
    vix_val = om.get("vix_data", {}).get("vix", {}).get("current")
    if vix_val:
        scores.append(max(0, min(100, (35 - float(vix_val)) / 25 * 100)))
    # ── [US-4] VIX term structure スコアを追加 ───────────────
    ts_score = results.get("options_monitor", {}).get("term_score")
    if ts_score is not None:
        scores.append(float(ts_score))   # ★ e_scores → scores に統一
    # 市場幅スコア
    mb_score = results.get("market_breadth", {}).get("breadth_score_jp")
    if mb_score is not None:
        scores.append(float(mb_score))

    # 債券: recession_score 逆転
    yc_score = _extract_yield_curve_score(results)
    if yc_score is not None:
        scores.append(yc_score)

    # センチメント
    sent = results.get("options_monitor", {}).get("sentiment", {}).get("score")
    if sent is not None:
        scores.append(float(sent))

    if not scores:
        return "中立"

    avg = sum(scores) / len(scores)
    if avg >= 70:   return "リスクオン強"
    if avg >= 57:   return "リスクオン"
    if avg >= 43:   return "中立"
    if avg >= 30:   return "リスクオフ"
    return "リスクオフ強"


# ==============================================================
# BLOCK 4: サマリーテキスト生成
# ==============================================================

def format_aggregator_report(result: dict, sector_names: dict) -> str:
    """weekly_report 向け Markdown を生成する。"""
    regime     = result.get("market_regime", "中立")
    top_buy    = result.get("top_buy", [])
    top_avoid  = result.get("top_avoid", [])
    signals    = result.get("sector_signals", {})

    regime_icon = {"リスクオン強": "🟢", "リスクオン": "🟢",
                   "中立": "🟡", "リスクオフ": "🔴", "リスクオフ強": "🔴"}.get(regime, "🟡")

    lines = [
        "## 🎯 総合投資シグナル（Signal Aggregator）",
        "",
        f"**市場レジーム**: {regime_icon} {regime}",
        "",
    ]

    if top_buy:
        lines.append("### 📈 総合シグナル上位（買い候補）")
        lines.append("")
        lines.append("| 業種 | シグナルスコア | 評価 | 主な理由 |")
        lines.append("|------|-------------|------|--------|")
        for code in top_buy[:6]:
            d    = signals.get(code, {})
            name = sector_names.get(code, code)
            sc   = d.get("signal_score", 50)
            lb   = d.get("signal_label", "")
            rsns = " / ".join(d.get("top_reasons", [])[:2])
            lines.append(f"| {name[:10]} | **{sc:.0f}** | {lb} | {rsns} |")
        lines.append("")

    if top_avoid:
        lines.append("### 📉 総合シグナル下位（回避推奨）")
        lines.append("")
        for code in top_avoid[:3]:
            d    = signals.get(code, {})
            name = sector_names.get(code, code)
            sc   = d.get("signal_score", 50)
            flags = " / ".join(d.get("risk_flags", [])[:2])
            lines.append(f"  🔴 {name}({code}): {sc:.0f}点 — {flags}")
        lines.append("")

# ── Wave 6 - G-X: 地政学シグナルサマリー行 ──────────────────
    try:
        _geo_top = [
            (code, d)
            for code, d in result.get("sector_signals", {}).items()
            if isinstance(d, dict)
            and isinstance(d.get("components", {}).get("geo_signal"), float)
            and abs(d["components"]["geo_signal"] - 50.0) >= 5.0
        ]
        if _geo_top:
            _geo_top.sort(
                key=lambda x: abs(x[1]["components"]["geo_signal"] - 50.0),
                reverse=True,
            )
            lines.append("")
            lines.append("### 🌐 地政学シグナル（上位業種）")
            for _gc, _gd in _geo_top[:5]:
                _gscore = _gd["components"]["geo_signal"]
                _gname  = sector_names.get(_gc, _gc)
                _icon   = (
                    "🟢" if _gscore >= 55
                    else ("🔴" if _gscore <= 45 else "⚪")
                )
                lines.append(
                    f"- {_icon} {_gname}({_gc}): "
                    f"geo_score={_gscore:.0f}/100"
                )
    except Exception:
        pass

    lines += [
        "> ⚙️ スコアはセクター(30%)/マクロ(18%)/テクニカル(15%)/市場幅(10%)/",
        "> オプション(8%)/先行シグナル(7%)/アノマリー(6%)/ETF(3%)/債券(3%) の加重平均",
        "> rule_optimizer.py が月次で重みを自動調整します。",
    ]

    return "\n".join(lines)

def calc_total_signal_score(results: dict, cfg) -> dict:
    """
    [5人統合エンジン最終形] 全シグナルを統合した total_signal_score を算出する。

    入力: run_all_tasks() の results dict
    出力: {
      "total_signal_score": float,  # 0〜100
      "components":         dict,   # 各コンポーネントのスコアと重み
      "signal_label":       str,    # "強い買いシグナル" 等
      "recommended_action": str,    # "BUY" / "WATCH" / "HOLD" / "REDUCE"
    }
    """
    # ── コンポーネントスコア取得 ──────────────────────────
    # D-1: 経済サイクルフェーズスコア（象限BなどをスコアにマップP）
    ec = results.get("economic_cycle", {})
    phase_score_map = {"B": 75, "A": 60, "D": 40, "C": 25}
    macro_phase_score = phase_score_map.get(ec.get("phase", "B"), 50)
    # 債務サイクル補正（D-2）
    dc = results.get("debt_cycle", {})
    dc_adj = {70: -10, 30: +5}.get(  # 簡易マッピング
        max(k for k in [70, 30, 0] if dc.get("debt_cycle_score", 50) >= k), 0
    )
    macro_phase_score = max(0, min(100, macro_phase_score + dc_adj))

    # sector_composite: TOP_TIERセクターの平均スコア
    sectors = results.get("sectors", {})
    tier    = results.get("sector_tier", {})
    top_scores = [
        v.get("composite_score", 50)
        for k, v in sectors.items()
        if tier.get(k) == "TOP" and isinstance(v, dict)
    ]
    sector_composite = sum(top_scores) / len(top_scores) if top_scores else 50.0

    # momentum_score: momentum_factor の上位スコア平均
    mom = results.get("momentum", {})
    if isinstance(mom, dict):
        mom_scores = [v.get("momentum_score", 50) for v in mom.values()
                      if isinstance(v, dict)]
        momentum_score = sum(mom_scores[:3]) / 3 if mom_scores else 50.0
    else:
        momentum_score = 50.0

    # earnings_acceleration: screener通過銘柄のacc_bonus平均
    screener = results.get("screener", {})
    cands    = [c for c in screener.get("candidates", []) if c.get("pass_filters")]
    if cands:
        acc_scores = [(c.get("acc_bonus", 0) or 0) for c in cands]
        # acc_bonus は -10〜+10 → 0〜100に正規化
        earnings_acceleration = 50.0 + (sum(acc_scores) / len(acc_scores)) * 5
        earnings_acceleration = max(0, min(100, earnings_acceleration))
    else:
        earnings_acceleration = 50.0

    # quality_score: screener通過銘柄のquality_score平均 + P-2補正
    if cands:
        quality_score = sum(c.get("quality_score", 50) or 50 for c in cands) / len(cands)
    else:
        quality_score = 50.0

    # technical_score: screener通過銘柄のtechnical_score平均
    if cands:
        tech_vals = [c.get("technical_score", 50) or 50 for c in cands]
        technical_score = sum(tech_vals) / len(tech_vals)
    else:
        technical_score = 50.0

    # activist_potential: activist_top の平均activist_score
    activist_top = screener.get("activist_top", [])
    if activist_top:
        activist_potential = sum(c.get("activist_score", 0) or 0
                                 for c in activist_top) / len(activist_top)
    else:
        activist_potential = 0.0

    # ── geo_signal スコア取得（Wave 6 - G-X）────────────────────
    # signal_aggregator の sector_signals から geo_signal スコアを集約する。
    # ENABLE_GEO_SIGNAL=False または BQデータ未蓄積時は None → 重み計算から除外。
    # ★ try/except 保護: geo_signal 未接続時も total_signal_score に影響しない
    geo_signal_score: float | None = None
    try:
        import importlib as _imp_tss, sys as _sys_tss
        from pathlib import Path as _pl_tss
        for _p in ["/content", str(_pl_tss.Path(__file__).parent)]:
            if _p not in _sys_tss.path:
                _sys_tss.path.insert(0, _p)
        _cfg_tss = _imp_tss.import_module("config")
        if getattr(_cfg_tss, "ENABLE_GEO_SIGNAL", False):
            _gs_tss = _imp_tss.import_module("geo_signal")
            # TOP_TIERセクターの geo_signal スコア平均を取得
            _geo_scores = []
            for _k, _v in sectors.items():
                if not isinstance(_v, dict):
                    continue
                if tier.get(_k) != "TOP":
                    continue
                _gs_sc = _gs_tss.aggregate_geo_signal(
                    sector_code = str(_k),
                    results     = results,
                    cfg         = _cfg_tss,
                    market      = "JP",
                )
                if _gs_sc is not None:
                    _geo_scores.append(float(_gs_sc))
            if _geo_scores:
                geo_signal_score = round(
                    sum(_geo_scores) / len(_geo_scores), 1
                )
    except Exception:
        pass   # 取得失敗時は None のまま → 重み計算から自動除外

    # ── 重み（rule_optimizer が自動調整する変数から読む）────────
    # _signal_weights を優先参照（rule_optimizer の update_signal_weights が更新）
    ro_all     = results.get("_macro_weights") or {}
    ro_weights = ro_all.get("_signal_weights", ro_all)
    w_macro    = ro_weights.get("macro_phase_weight",      0.20)
    w_sector   = ro_weights.get("sector_composite_weight", 0.20)
    w_mom      = ro_weights.get("momentum_weight",         0.15)
    w_accel    = ro_weights.get("earnings_accel_weight",   0.15)
    w_quality  = ro_weights.get("quality_weight",          0.10)
    w_tech     = ro_weights.get("technical_weight",        0.10)
    w_activist = ro_weights.get("activist_weight",         0.10)
    # Wave 6 - G-X: geo_signal ウェイト（ENABLE_GEO_SIGNAL=False 時は 0.0）
    w_geo      = (
        ro_weights.get("geo_signal_weight", 0.04)
        if geo_signal_score is not None
        else 0.0
    )

    # 重みの合計が1.0になるよう正規化
    # ★ geo_signal_score が None の場合 w_geo=0.0 なので既存7変数で正規化される
    total_w = (
        w_macro + w_sector + w_mom + w_accel
        + w_quality + w_tech + w_activist + w_geo
    )
    if total_w > 0:
        w_macro    /= total_w
        w_sector   /= total_w
        w_mom      /= total_w
        w_accel    /= total_w
        w_quality  /= total_w
        w_tech     /= total_w
        w_activist /= total_w
        w_geo      /= total_w

    # ── 統合スコア算出 ──────────────────────────────────────
    # geo_signal_score が None の場合は w_geo=0.0 のため乗算しても影響ゼロ
    total_signal_score = round(
        macro_phase_score       * w_macro
        + sector_composite      * w_sector
        + momentum_score        * w_mom
        + earnings_acceleration * w_accel
        + quality_score         * w_quality
        + technical_score       * w_tech
        + activist_potential    * w_activist
        + (geo_signal_score or 50.0) * w_geo,
        1,
    )

    # ── Wave 6 - 第4セッション: total_signal_score への縮退補正 ──
    # sector_signals の縮退とは独立したポートフォリオ全体レベルの補正。
    # contraction_flag が Emergency の場合、total_signal_score 全体を
    # 縮退係数で抑制する（防御セクター比率が高い場合は補正を緩和）。
    try:
        _cont_tss = (
            results.get("rule_optimizer", {})
            .get("contraction_flag", {})
        )
        _mode_tss = _cont_tss.get("mode", "Normal")
        if _mode_tss != "Normal":
            import sys as _sys_tss2, importlib as _imp_tss2
            from pathlib import Path as _pl_tss2
            _sys_tss2.path.insert(0, str(_pl_tss2(__file__).parent))
            _cfg_tss2 = _imp_tss2.import_module("config")
            _ro_tss2  = _imp_tss2.import_module("rule_optimizer")
            _coeff_tss = float(_cont_tss.get("coefficient", 1.00))

            # 防御セクター比率に応じて補正を緩和（0〜1の緩和率）
            # 防御セクターが多いほど total_signal_score の縮退を弱める
            _sa_res    = results.get("signal_aggregator", {})
            _def_secs  = _sa_res.get("defensive_sectors", [])
            _all_secs  = list(_sa_res.get("sector_signals", {}).keys())
            _def_ratio = (
                len(_def_secs) / len(_all_secs)
                if _all_secs else 0.0
            )
            # 最終係数: 防御比率で線形補間（全防御=補正なし、全非防御=係数そのまま）
            _final_coeff = _coeff_tss + (1.0 - _coeff_tss) * _def_ratio
            _final_coeff = max(0.30, min(1.00, _final_coeff))

            # adjusted\_total = 50 + (total - 50) \times final\_coeff
            total_signal_score = round(
                max(0.0, min(100.0,
                    50.0 + (total_signal_score - 50.0) * _final_coeff
                )),
                1,
            )
            print(
                f"    🔴 [縮退補正] {_mode_tss} / "
                f"coeff={_coeff_tss:.2f} → final={_final_coeff:.2f} "
                f"/ total_signal_score={total_signal_score:.1f}"
            )
    except Exception:
        pass   # 縮退補正失敗は total_signal_score をそのまま使用

    # ── 判断ラベル ─────────────────────────────────────
    if total_signal_score >= 70:
        label = "🟢 強い買いシグナル"
        action = "BUY"
    elif total_signal_score >= 58:
        label = "📈 買い検討シグナル"
        action = "WATCH"
    elif total_signal_score >= 42:
        label = "➡️ 中立（様子見）"
        action = "HOLD"
    elif total_signal_score >= 30:
        label = "📉 軽減シグナル"
        action = "REDUCE"
    else:
        label = "🔴 回避シグナル"
        action = "AVOID"

    return {
        "total_signal_score": total_signal_score,
        "components": {
            "macro_phase_score":     {"score": macro_phase_score,               "weight": round(w_macro,    3)},
            "sector_composite":      {"score": round(sector_composite,    1),   "weight": round(w_sector,   3)},
            "momentum_score":        {"score": round(momentum_score,      1),   "weight": round(w_mom,      3)},
            "earnings_acceleration": {"score": round(earnings_acceleration, 1), "weight": round(w_accel,    3)},
            "quality_score":         {"score": round(quality_score,       1),   "weight": round(w_quality,  3)},
            "technical_score":       {"score": round(technical_score,     1),   "weight": round(w_tech,     3)},
            "activist_potential":    {"score": round(activist_potential,  1),   "weight": round(w_activist, 3)},
            "geo_signal":            {"score": geo_signal_score,                "weight": round(w_geo,      3)},
        },
        "signal_label":       label,
        "recommended_action": action,
        "d1_phase":           ec.get("phase", "?"),
        "d2_cycle_score":     dc.get("debt_cycle_score", 50),
        # Wave 6 - 第4セッション: 縮退情報
        "contraction_mode":   (
            results.get("rule_optimizer", {})
            .get("contraction_flag", {})
            .get("mode", "Normal")
        ),
        "contraction_coefficient": (
            results.get("rule_optimizer", {})
            .get("contraction_flag", {})
            .get("coefficient", 1.00)
        ),
        "timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_signal_aggregator(cfg, results: dict) -> dict:
    """
    全モジュール出力を統合して業種別シグナルスコアを算出する。

    stock_analyzer_main.py から以下のように呼び出す:
        _sa = _load_local_module("signal_aggregator", ...)
        sa_result = _sa.run_signal_aggregator(cfg, results)
        results["signal_aggregator"] = sa_result

    Returns:
        {
          "sector_signals": {code: signal_dict},
          "market_regime":  str,
          "top_buy":        [code, ...],
          "top_avoid":      [code, ...],
          "summary_text":   str,
          "weights_used":   dict,
          "timestamp":      str,
        }
    """
    print("\n  🎯 シグナルアグリゲーター実行中...")

    # rule_optimizerの重みがあれば使用
    weights = dict(DEFAULT_WEIGHTS)
    ro_weights = results.get("_macro_weights", {})
    if ro_weights:
        for factor, entry in ro_weights.items():
            w = entry.get("weight", 1.0) if isinstance(entry, dict) else float(entry)
            # factor名 → カテゴリ名の変換
            cat_map = {
                "interest_rate_rise":    "macro_signals",
                "yen_depreciation":      "macro_signals",
                "ai_semiconductor_boom": "etf_heat",
                # Wave 6 - G-X: 地政学リスク上昇 → geo_signal ウェイト連動
                "geopolitical_risk":     "geo_signal",
                "oil_price_rise":        "geo_signal",
            }
            cat = cat_map.get(factor)
            if cat:
                weights[cat] = round(weights.get(cat, 0.1) * w, 4)

    # 業種名マップ
    sector_names = {
        code: info.get("name_ja", code)
        for code, info in getattr(cfg, "JAPAN_SECTORS", {}).items()
    }

    sector_results = results.get("sectors", {})
    if not sector_results:
        print("    ⚠️  sector_results なし → スキップ")
        return {
            "skipped": True,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # 各業種のシグナルスコアを計算
    sector_signals = {}
    for code in sector_results:
        sector_signals[code] = calc_signal_score(code, results, weights)

    # ランキング
    ranked = sorted(sector_signals.items(),
                    key=lambda x: x[1].get("signal_score", 50), reverse=True)
    top_buy   = [c for c, d in ranked if d.get("signal_score", 50) >= 60][:6]
    top_avoid = [c for c, d in ranked if d.get("signal_score", 50) <= 40][-3:]

    # 市場レジーム
    regime = detect_market_regime(results)

    # 上位/下位を表示
    for code, d in ranked[:3]:
        name = sector_names.get(code, code)
        print(f"      🟢 {name}: {d['signal_score']:.0f}pt {d['signal_label']}")
    for code, d in ranked[-3:]:
        name = sector_names.get(code, code)
        print(f"      🔴 {name}: {d['signal_score']:.0f}pt {d['signal_label']}")

    print(f"    市場レジーム: {regime}")

    aggregated = {
        "sector_signals":  sector_signals,
        "market_regime":   regime,
        "top_buy":         top_buy,
        "top_avoid":       top_avoid,
        "weights_used":    weights,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # Wave 6 - 第4セッション: 縮退サマリー
        "contraction_mode":  results.get("rule_optimizer", {})
                             .get("contraction_flag", {})
                             .get("mode", "Normal"),
        "contraction_detail": results.get("rule_optimizer", {})
                              .get("contraction_flag", {}),
        "defensive_sectors":  [
            code for code, d in sector_signals.items()
            if isinstance(d, dict) and d.get("is_defensive")
        ],
    }
    aggregated["summary_text"] = format_aggregator_report(aggregated, sector_names)

    print(f"    ✅ シグナル集計完了: {len(sector_signals)}業種 / 買い{len(top_buy)} 回避{len(top_avoid)}")
    return aggregated
