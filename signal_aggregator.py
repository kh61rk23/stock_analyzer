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
from pathlib import Path
from typing import Optional

# ── デフォルト重み（rule_optimizer が上書き可能） ─────────────
DEFAULT_WEIGHTS = {
    "sector_score":      0.30,   # A: composite_score（最重要）
    "macro_signals":     0.18,   # B: MACRO_RULES シグナル
    "technical_score":   0.15,   # C: テクニカル
    "market_breadth":    0.10,   # D: 市場幅
    "options_sentiment": 0.08,   # E: オプション恐怖/強欲
    "anomaly_season":    0.06,   # F: 季節アノマリー
    "us_lead_signal":    0.07,   # G: JP×US先行シグナル
    "etf_heat":          0.03,   # H: テーマETF熱量（間接）
    "yield_curve":       0.03,   # I: イールドカーブ
}

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
    """G: jp_us_sector_bridge の先行シグナル補正 → スコア変換。"""
    bridge = results.get("jp_us_bridge", {})
    if not bridge:
        return None
    adj = bridge.get("lead_adjustments", {}).get(code, 0)
    phase_data = bridge.get("rotation_phases", {}).get(code, {})
    phase = phase_data.get("phase", "LAGGING")
    phase_bonus = {"LEADING": 15, "IMPROVING": 10, "WEAKENING": -10, "LAGGING": -15}.get(phase, 0)
    return round(max(0, min(100, 50 + adj * 5 + phase_bonus)), 1)


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
        "sector_score":      lambda: _extract_sector_score(results.get("sectors", {}), code),
        "macro_signals":     lambda: _extract_macro_score(results.get("sectors", {}), code),
        "technical_score":   lambda: _extract_technical_score(results, code),
        "market_breadth":    lambda: _extract_breadth_score(results, code),
        "options_sentiment": lambda: _extract_options_score(results, code),
        "anomaly_season":    lambda: _extract_anomaly_score(results, code),
        "us_lead_signal":    lambda: _extract_us_lead_score(results, code),
        "etf_heat":          lambda: _extract_etf_heat_score(results, code),
        "yield_curve":       lambda: _extract_yield_curve_score(results),
    }

    components    = {}
    weighted_sum  = 0.0
    weight_used   = 0.0
    top_reasons   = []
    risk_flags    = []

    for cat, extractor in extractors.items():
        try:
            score = extractor()
        except Exception:
            score = None

        w = weights.get(cat, DEFAULT_WEIGHTS.get(cat, 0.05))

        if score is not None:
            components[cat] = round(score, 1)
            weighted_sum  += score * w
            weight_used   += w

            # 理由・リスクフラグ収集
            if score >= 70 and cat in ("sector_score", "technical_score", "macro_signals", "us_lead_signal"):
                label_map = {
                    "sector_score":    f"セクタースコア高({score:.0f})",
                    "technical_score": f"テクニカル買い({score:.0f})",
                    "macro_signals":   f"マクロ追い風({score:.0f})",
                    "us_lead_signal":  f"US先行強気({score:.0f})",
                }
                top_reasons.append(label_map[cat])
            if score <= 30 and cat in ("sector_score", "market_breadth", "yield_curve"):
                label_map = {
                    "sector_score":  f"セクタースコア低({score:.0f})",
                    "market_breadth": f"市場幅悪化({score:.0f})",
                    "yield_curve":   f"景気後退リスク高({score:.0f})",
                }
                risk_flags.append(label_map.get(cat, f"{cat}={score:.0f}"))
        else:
            components[cat] = None   # データなし

    if weight_used < 1e-6:
        return {
            "signal_score": 50.0, "signal_label": "⚪ データ不足",
            "components": components, "top_reasons": [], "risk_flags": [],
            "data_coverage": 0.0,
        }

    signal_score   = round(weighted_sum / weight_used, 1)
    data_coverage  = round(weight_used / sum(DEFAULT_WEIGHTS.values()), 3)

    return {
        "signal_score":   signal_score,
        "signal_label":   _signal_label(signal_score),
        "components":     components,
        "top_reasons":    top_reasons[:3],
        "risk_flags":     risk_flags[:3],
        "data_coverage":  data_coverage,
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

    # ── 重み（rule_optimizer が自動調整する変数から読む）────
    # _signal_weights を優先参照（rule_optimizer の update_signal_weights が更新）
    ro_all     = results.get("_macro_weights") or {}
    ro_weights = ro_all.get("_signal_weights", ro_all)  # _signal_weights サブキーを優先
    w_macro    = ro_weights.get("macro_phase_weight",      0.20)
    w_sector   = ro_weights.get("sector_composite_weight", 0.20)
    w_mom      = ro_weights.get("momentum_weight",         0.15)
    w_accel    = ro_weights.get("earnings_accel_weight",   0.15)
    w_quality  = ro_weights.get("quality_weight",          0.10)
    w_tech     = ro_weights.get("technical_weight",        0.10)
    w_activist = ro_weights.get("activist_weight",         0.10)

    # 重みの合計が1.0になるよう正規化
    total_w = w_macro + w_sector + w_mom + w_accel + w_quality + w_tech + w_activist
    if total_w > 0:
        w_macro    /= total_w
        w_sector   /= total_w
        w_mom      /= total_w
        w_accel    /= total_w
        w_quality  /= total_w
        w_tech     /= total_w
        w_activist /= total_w

    # ── 統合スコア算出 ──────────────────────────────────
    total_signal_score = round(
        macro_phase_score   * w_macro
        + sector_composite  * w_sector
        + momentum_score    * w_mom
        + earnings_acceleration * w_accel
        + quality_score     * w_quality
        + technical_score   * w_tech
        + activist_potential * w_activist,
        1,
    )

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
            "macro_phase_score":      {"score": macro_phase_score,    "weight": round(w_macro,   3)},
            "sector_composite":       {"score": round(sector_composite,1), "weight": round(w_sector,  3)},
            "momentum_score":         {"score": round(momentum_score,  1), "weight": round(w_mom,     3)},
            "earnings_acceleration":  {"score": round(earnings_acceleration, 1), "weight": round(w_accel,   3)},
            "quality_score":          {"score": round(quality_score,   1), "weight": round(w_quality, 3)},
            "technical_score":        {"score": round(technical_score, 1), "weight": round(w_tech,    3)},
            "activist_potential":     {"score": round(activist_potential,1),"weight": round(w_activist,3)},
        },
        "signal_label":       label,
        "recommended_action": action,
        "d1_phase":           ec.get("phase", "?"),
        "d2_cycle_score":     dc.get("debt_cycle_score", 50),
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
                "interest_rate_rise": "macro_signals",
                "yen_depreciation":   "macro_signals",
                "ai_semiconductor_boom": "etf_heat",
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
        "sector_signals": sector_signals,
        "market_regime":  regime,
        "top_buy":        top_buy,
        "top_avoid":      top_avoid,
        "weights_used":   weights,
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    aggregated["summary_text"] = format_aggregator_report(aggregated, sector_names)

    print(f"    ✅ シグナル集計完了: {len(sector_signals)}業種 / 買い{len(top_buy)} 回避{len(top_avoid)}")
    return aggregated
