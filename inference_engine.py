# ==============================================================
# inference_engine.py  v1.0  (4-10)
# 条件分岐推論エンジン — 「今週何をすべきか」を出力
# ==============================================================
# 【役割】
#   「スコアがなぜその値か」ではなく「今何をすべきか」を
#   MACRO_RULES の条件を組み合わせた決定木で自動判断する。
#   LLM不要・純粋な if-elif 連鎖で実装。
#
# 【推論ルール体系（3階層）】
#
#   Layer 1: 市場フェーズ判定（最初に確定）
#     → リスクオン / 中立 / リスクオフ / 危機的状況
#
#   Layer 2: マクロシナリオ判定（Layer1 条件下で評価）
#     → 金利上昇局面 / 金利低下局面 / 円安継続 / 円高転換
#        資源高 / AI・テクブーム / 逆イールド解消 etc.
#
#   Layer 3: 具体的投資アクション（Layer1+2 の組み合わせ）
#     → 「輸出×金融クロスが最強」「エネルギー×素材を優先」etc.
#
# 【判断ロジックの例】
#   IF 逆イールド解消 AND 円安継続:
#     → アクション「輸出×金融のクロス戦略が有効」
#        理由「金利正常化で銀行収益改善＋円安で輸出採算向上」
#
#   IF VIX > 30 AND PCR > 1.2:
#     → アクション「逆張り仕込みの好機。ディフェンシブ先行で待機」
#        理由「市場の恐怖が過剰。歴史的に底値から4〜6週以内に回復」
#
#   IF ArXiv熱量急上昇 AND ETF出来高未増:
#     → アクション「3〜4週後の先行取得を検討」
#        理由「学術界の注目度は株式市場への反映より早い傾向」
#
# 【出力スキーマ】
#   {
#     "market_phase":      str,    # "リスクオン" / "危機的状況" etc.
#     "active_scenarios":  [ScenarioResult, ...],
#     "primary_action":    ActionResult,   # 最優先推奨アクション
#     "sector_priorities": [code, ...],    # 今週注目すべき業種順
#     "time_horizon":      str,            # "即時" / "2〜4週" / "3〜6ヶ月"
#     "summary_md":        str,            # weekly_report 用 Markdown
#     "timestamp":         str,
#   }
#
# 【依存】全モジュールの results dict / signal_aggregator の出力
# ==============================================================

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

# ── マクロ条件の評価閾値 ───────────────────────────────────────

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'IE_THRESHOLDS': 'THRESHOLDS',
}

THRESHOLDS = {
    "vix_fear":       25.0,   # VIX > 25 → 恐怖
    "vix_crisis":     35.0,   # VIX > 35 → 危機
    "vix_calm":       15.0,   # VIX < 15 → 楽観
    "pcr_fear":        1.2,   # PCR > 1.2 → 悲観過多
    "pcr_greed":       0.6,   # PCR < 0.6 → 楽観過多
    "us10y_high":      4.5,   # 米10年債 > 4.5% → 高金利
    "us10y_low":       3.5,   # 米10年債 < 3.5% → 低金利
    "yen_weak":      148.0,   # USD/JPY > 148 → 円安
    "yen_strong":    135.0,   # USD/JPY < 135 → 円高
    "oil_high":       85.0,   # WTI > 85 → 原油高
    "recession_risk":  60,    # recession_score > 60 → 後退リスク
    "recession_low":   30,    # recession_score < 30 → リスク低
    "breadth_healthy": 65,    # market_breadth > 65 → 健全
    "breadth_weak":    35,    # market_breadth < 35 → 悪化
    "arxiv_hot":       70,    # ETF heat_score > 70 → AI過熱
}


# ==============================================================
# BLOCK 1: 条件評価ヘルパー
# ==============================================================

class _Ctx:
    """results dict から各指標を安全に取り出すコンテキスト。"""

    def __init__(self, results: dict):
        self.results = results
        snap = results.get("macro_snapshot", {})
        self.usdjpy   = self._f(snap.get("usdjpy"))
        self.us10y    = self._f(snap.get("us10y"))
        self.oil      = self._f(snap.get("oil_wti"))
        self.vix      = self._f(snap.get("vix"))
        self.sp500    = self._f(snap.get("sp500"))
        self.nikkei   = self._f(snap.get("nikkei"))

        om = results.get("options_monitor", {})
        self.pcr      = self._f(om.get("option_data", {}).get("spy", {}).get("pcr_oi"))
        self.iv_rank  = self._f(om.get("vix_data", {}).get("vix", {}).get("iv_rank"))
        self.sent_score = self._f(om.get("sentiment", {}).get("score"))

        bc = results.get("bond_commodity", {})
        # ★修正①: "yield_curve" → "curve_analysis"（bond_commodity_pipeline の実際のキー名）
        yc = bc.get("curve_analysis", {}) if isinstance(bc, dict) else {}
        # ★修正②: recession_score はトップレベルを優先、curve_analysis をフォールバック
        _rec = bc.get("recession_score") if isinstance(bc, dict) else None
        if _rec is None and isinstance(yc, dict):
            _rec = yc.get("recession_score")
        self.recession_score = self._f(_rec)
        # ★修正③: "t10y2y" → "spread_2y10y"（curve_analysis の実際のキー名）
        self.t10y2y = self._f(yc.get("spread_2y10y") if isinstance(yc, dict) else None)

        mb = results.get("market_breadth", {})
        self.breadth  = self._f(mb.get("breadth_score_jp"))

        # ETF / ArXiv
        etf = results.get("etf_tracker", {})
        # ★修正: "top_themes" は上位N件のみ。全テーマから AI 熱量を拾うため "themes" を使う
        themes = etf.get("themes", []) if isinstance(etf, dict) else []
        ai_themes = [t for t in themes if "ai" in t.get("theme_name","").lower()
                     or "semiconductor" in t.get("theme_name","").lower()]
        self.ai_heat  = max((t.get("heat_score", 50) for t in ai_themes), default=50)

        # signal_aggregator の市場レジーム
        sa = results.get("signal_aggregator", {})
        self.regime   = sa.get("market_regime", "中立")
        self.top_buy  = sa.get("top_buy", [])

    @staticmethod
    def _f(v) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


# ==============================================================
# BLOCK 2: 市場フェーズ判定（Layer 1）
# ==============================================================

def _detect_phase(ctx: _Ctx) -> tuple[str, str]:
    """
    市場フェーズを判定する。

    Returns:
        (phase_code, phase_label)
    """
    t = THRESHOLDS

    # 危機的状況
    if (ctx.vix and ctx.vix > t["vix_crisis"]) or \
       (ctx.recession_score and ctx.recession_score > 80):
        return ("crisis", "⚠️ 危機的状況（防衛優先）")

    # リスクオフ
    if (ctx.vix and ctx.vix > t["vix_fear"]) or \
       (ctx.recession_score and ctx.recession_score > t["recession_risk"]) or \
       (ctx.breadth and ctx.breadth < t["breadth_weak"]):
        return ("risk_off", "🔴 リスクオフ（慎重モード）")

    # リスクオン強
    if (ctx.vix and ctx.vix < t["vix_calm"]) and \
       (ctx.breadth and ctx.breadth > t["breadth_healthy"]):
        return ("risk_on_strong", "🟢 リスクオン強（積極モード）")

    # リスクオン
    if ctx.regime in ("リスクオン強", "リスクオン"):
        return ("risk_on", "🟢 リスクオン（標準モード）")

    return ("neutral", "🟡 中立（選別モード）")


# ==============================================================
# BLOCK 3: マクロシナリオ判定（Layer 2）
# ==============================================================

def _evaluate_scenarios(ctx: _Ctx) -> list:
    """
    アクティブなマクロシナリオをリストアップする。

    Returns:
        [{"scenario_id", "label", "confidence", "sectors"}]
    """
    t = THRESHOLDS
    scenarios = []

    def _add(sid, label, confidence, sectors, rationale=""):
        scenarios.append({
            "scenario_id": sid,
            "label":       label,
            "confidence":  round(confidence, 2),
            "sectors":     sectors,
            "rationale":   rationale,
        })

    # ── 逆イールド解消 AND 円安継続 ────────────────────────────
    yield_normalizing = (ctx.t10y2y is not None and ctx.t10y2y > -0.1 and
                         ctx.recession_score is not None and ctx.recession_score < 40)
    yen_weak = ctx.usdjpy is not None and ctx.usdjpy > t["yen_weak"]
    if yield_normalizing and yen_weak:
        conf = min(0.95, 0.5 + (ctx.usdjpy - t["yen_weak"]) / 20 * 0.3)
        _add("yield_norm_yen_weak",
             "逆イールド解消 × 円安継続",
             conf,
             ["1028", "1029", "1017", "1016"],
             "金利正常化で銀行収益改善 ＋ 円安で輸出採算向上。金融×輸出のクロス戦略が有効。")

    # ── 原油高 AND インフレ再燃 ──────────────────────────────
    oil_high = ctx.oil is not None and ctx.oil > t["oil_high"]
    rate_high = ctx.us10y is not None and ctx.us10y > t["us10y_high"]
    if oil_high and rate_high:
        conf = min(0.90, 0.4 + (ctx.oil - t["oil_high"]) / 30 * 0.4)
        _add("oil_inflation",
             "原油高 × インフレ再燃",
             conf,
             ["1009", "1012", "1013", "1026"],
             "エネルギー・素材セクターの収益改善期待。商社は資源価格上昇で利益増。")

    # ── VIX極度恐怖 AND PCR高い → 逆張り好機 ─────────────────
    fear_extreme = (ctx.vix is not None and ctx.vix > t["vix_fear"]) and \
                   (ctx.pcr is not None and ctx.pcr > t["pcr_fear"])
    if fear_extreme:
        conf = min(0.85, 0.5 + (ctx.vix - t["vix_fear"]) / 20 * 0.35)
        _add("contrarian_buy",
             "市場恐怖過多 → 逆張り仕込みの好機",
             conf,
             ["1008", "1020", "1004"],
             "VIXとPCRが同時高値を示している。歴史的に底値から4〜6週で回復する確率が高い。")

    # ── AI・半導体ブーム継続（ArXiv熱量高 AND ETF過熱前）──────
    ai_arxiv_hot  = ctx.ai_heat > t["arxiv_hot"]
    # ETF出来高増加チェック（market_breadth で代替）
    etf_volume_ok = ctx.breadth is not None and ctx.breadth > 50
    if ai_arxiv_hot and etf_volume_ok:
        conf = min(0.80, 0.4 + (ctx.ai_heat - t["arxiv_hot"]) / 30 * 0.4)
        _add("ai_boom_early",
             "AI・半導体ブーム継続（市場が気づく前）",
             conf,
             ["1016", "1025", "1018"],
             "ArXiv論文の熱量がETF出来高増加に先行する傾向。3〜4週後の先行取得を検討。")

    # ── 円高転換 × 景気後退リスク高 ──────────────────────────
    yen_strong = ctx.usdjpy is not None and ctx.usdjpy < t["yen_strong"]
    recession_high = ctx.recession_score is not None and ctx.recession_score > t["recession_risk"]
    if yen_strong and recession_high:
        conf = min(0.88, 0.5 + (t["yen_strong"] - ctx.usdjpy) / 15 * 0.3)
        _add("yen_strong_recession",
             "円高転換 × 景気後退リスク上昇",
             conf,
             ["1008", "1020", "1004", "1027"],
             "輸出企業の採算悪化。内需ディフェンシブへのシフトを検討。")

    # ── 金利低下局面 × 不動産・建設に追い風 ─────────────────
    rate_low = ctx.us10y is not None and ctx.us10y < t["us10y_low"]
    if rate_low:
        conf = min(0.78, 0.4 + (t["us10y_low"] - ctx.us10y) / 2.0 * 0.4)
        _add("rate_down_real_estate",
             "金利低下 × 不動産・建設に追い風",
             conf,
             ["1032", "1003", "1021"],
             "住宅ローン金利低下で不動産需要回復期待。建設・陸運も資金調達コスト低下。")

    # 確信度の高い順にソート
    return sorted(scenarios, key=lambda x: -x["confidence"])


# ==============================================================
# BLOCK 4: 投資アクション決定（Layer 3）
# ==============================================================

def _decide_primary_action(
    phase_code: str,
    scenarios:  list,
    ctx:        _Ctx,
) -> dict:
    """
    市場フェーズ × シナリオから最優先アクションを決定する。

    Returns:
        {
          "action":       str,   # アクション本文
          "time_horizon": str,   # 時間軸
          "confidence":   float, # 確信度 0〜1
          "key_metrics":  dict,  # 判断根拠となった数値
        }
    """
    top_scenario = scenarios[0] if scenarios else None

    # ── 危機的状況 ───────────────────────────────────────────
    if phase_code == "crisis":
        return {
            "action": "全ポジションのリスク管理を最優先。新規エントリー控え。"
                      "医薬品・公益セクターへの縮退と現金比率引き上げを検討。",
            "time_horizon": "即時対応",
            "confidence": 0.92,
            "key_metrics": {"VIX": ctx.vix, "recession_score": ctx.recession_score},
        }

    # ── リスクオフ ────────────────────────────────────────────
    if phase_code == "risk_off":
        if top_scenario and top_scenario["scenario_id"] == "contrarian_buy":
            return {
                "action": "逆張り仕込みの好機。ディフェンシブ先行エントリー後、"
                          "VIX低下を確認してから景気敏感株に段階的に移行する。",
                "time_horizon": "2〜4週で段階的に",
                "confidence": top_scenario["confidence"] * 0.85,
                "key_metrics": {"VIX": ctx.vix, "PCR": ctx.pcr,
                                "breadth": ctx.breadth},
            }
        return {
            "action": "現金・ディフェンシブ比率を高める。景気敏感株の新規エントリーは"
                      "市場幅が50以上に回復するまで待機。",
            "time_horizon": "様子見（2〜6週）",
            "confidence": 0.78,
            "key_metrics": {"recession_score": ctx.recession_score,
                            "breadth": ctx.breadth},
        }

    # ── リスクオン強 ──────────────────────────────────────────
    if phase_code == "risk_on_strong":
        if top_scenario and top_scenario["scenario_id"] == "yield_norm_yen_weak":
            return {
                "action": f"輸出×金融のクロス戦略が有効。"
                          f"円安（USD/JPY {ctx.usdjpy:.0f}）＋逆イールド解消で、"
                          f"銀行(1028)・自動車(1017)・電気機器(1016)の選別仕込みを推奨。",
                "time_horizon": "3〜6ヶ月の中期保有",
                "confidence": top_scenario["confidence"],
                "key_metrics": {"USD/JPY": ctx.usdjpy, "US10Y": ctx.us10y,
                                "T10Y2Y": ctx.t10y2y},
            }
        return {
            "action": "リスクオンが強い状況。高スコア業種の積極的なポジション拡大が有効。"
                      f"市場幅({ctx.breadth:.0f})とVIX({ctx.vix:.1f})が良好。",
            "time_horizon": "1〜3ヶ月",
            "confidence": 0.72,
            "key_metrics": {"breadth": ctx.breadth, "VIX": ctx.vix},
        }

    # ── 中立 ────────────────────────────────────────────────
    if top_scenario:
        ts = top_scenario
        return {
            "action": f"中立局面だが「{ts['label']}」シグナルが最も強い。"
                      f"{', '.join(ts['sectors'][:3])} の選別投資を優先。",
            "time_horizon": "2〜4週",
            "confidence": ts["confidence"] * 0.75,
            "key_metrics": {"top_scenario": ts["scenario_id"]},
        }

    return {
        "action": "明確なシグナルなし。スコア上位業種の維持と様子見が無難。",
        "time_horizon": "様子見（1〜2週）",
        "confidence": 0.50,
        "key_metrics": {},
    }


# ==============================================================
# BLOCK 5: Markdown生成
# ==============================================================

def _fmt_conf(c: float) -> str:
    if c >= 0.80: return "🟢 高"
    if c >= 0.60: return "🟡 中"
    return "🟠 低"


def format_inference_report(result: dict) -> str:
    """weekly_report の「今週の投資判断サマリー」セクションを生成する。"""
    phase   = result.get("phase_label", "不明")
    action  = result.get("primary_action", {})
    scen    = result.get("active_scenarios", [])
    top_sec = result.get("sector_priorities", [])

    lines = [
        "## 🧠 今週の投資判断サマリー（Inference Engine）",
        "",
        f"**市場フェーズ**: {phase}",
        "",
        "### 🎯 推奨アクション",
        "",
        f"> {action.get('action', 'N/A')}",
        "",
        f"**時間軸**: {action.get('time_horizon', '—')}  "
        f"**確信度**: {_fmt_conf(action.get('confidence', 0.5))} "
        f"({action.get('confidence', 0.5):.0%})",
        "",
    ]

    # 根拠数値
    km = action.get("key_metrics", {})
    if km:
        metrics_str = " / ".join(f"{k}: {v:.1f}" if isinstance(v, float) else f"{k}: {v}"
                                  for k, v in km.items() if v is not None)
        lines.append(f"**根拠指標**: {metrics_str}")
        lines.append("")

    # アクティブシナリオ
    if scen:
        lines += ["### 📋 アクティブシナリオ", ""]
        for s in scen[:4]:
            lines.append(
                f"  {_fmt_conf(s['confidence'])} **{s['label']}** "
                f"({s['confidence']:.0%}) — {s['rationale'][:60]}")
        lines.append("")

    # 注目業種
    if top_sec:
        lines.append("### 🏭 今週の注目業種")
        lines.append("")
        lines.append(" → ".join(f"`[{c}]`" for c in top_sec[:5]))
        lines.append("")

    lines += [
        "---",
        "*本サマリーはルールベース推論エンジンによる自動生成です。最終判断は必ずご自身でご確認ください。*",
    ]

    return "\n".join(lines)


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_inference_engine(cfg, results: dict) -> dict:
    """
    推論エンジンを実行して「今週の投資判断サマリー」を生成する。

    stock_analyzer_main.py から weekly_report 直前に呼び出す:
        _ie = _load_local_module("inference_engine", ...)
        ie_result = _ie.run_inference_engine(cfg, results)
        results["inference"] = ie_result

    Returns:
        {
          "phase_code":       str,
          "phase_label":      str,
          "active_scenarios": list,
          "primary_action":   dict,
          "sector_priorities": list,
          "summary_md":       str,
          "timestamp":        str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🧠 推論エンジン実行中...")

    ctx = _Ctx(results)
    # ── [US-4] term structure による crisis 判定強化 ────────
    # ★ 2026-06-11 バグ修正: モジュール関数内で未定義の self に代入しており
    #   毎回 NameError → 推論エンジン全体が常時失敗していた。
    #   ctx 属性に格納し、crisis_signal 時はフェーズを crisis に
    #   強制する [US-4] の設計意図をここで実装する。
    options_mon = results.get("options_monitor", {})
    ctx.crisis_override = bool(options_mon.get("crisis_signal"))
    ctx.term_score      = options_mon.get("term_score", 50.0)

    # Layer 1: 市場フェーズ
    phase_code, phase_label = _detect_phase(ctx)
    if ctx.crisis_override and phase_code != "crisis":
        phase_code  = "crisis"
        phase_label = "⚠️ 危機的状況（オプション期間構造シグナルによる強制判定）"
    print(f"    市場フェーズ: {phase_label}")

    # Layer 2: マクロシナリオ
    scenarios = _evaluate_scenarios(ctx)
    if scenarios:
        top = scenarios[0]
        print(f"    主シナリオ: {top['label']} (確信度 {top['confidence']:.0%})")

    # Layer 3: 投資アクション
    action = _decide_primary_action(phase_code, scenarios, ctx)
    print(f"    推奨: {action['action'][:60]}...")

    # 注目業種（signal_aggregator の top_buy を優先、シナリオの sectors を補完）
    sa_top = results.get("signal_aggregator", {}).get("top_buy", [])
    scen_secs = [s for sc in scenarios[:2] for s in sc.get("sectors", [])]
    # 重複排除しつつ signal_aggregator 優先
    sector_priorities = []
    for c in sa_top + scen_secs:
        if c not in sector_priorities:
            sector_priorities.append(c)

    result = {
        "phase_code":        phase_code,
        "phase_label":       phase_label,
        "active_scenarios":  scenarios,
        "primary_action":    action,
        "sector_priorities": sector_priorities[:8],
        "timestamp":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["summary_md"] = format_inference_report(result)

    print(f"    ✅ 推論完了: フェーズ={phase_label} / シナリオ{len(scenarios)}件")
    return result
