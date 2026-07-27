# ==============================================================
# strategy_output_schema.py  v1.0  (⑥ 戦略別出力スキーマ)
# ==============================================================
# 【役割】
#   screener の candidate 辞書に戦略別の固定フィールドを付与する。
#   "strategy_label" によってコア/サテライトを分類し、
#   hold_weeks / entry_trigger / exit_trigger を付与する。
#
# 【戦略分類】
#   core              → 長期保有候補（12週以上）
#   satellite_event   → イベントドリブン（4週）
#   satellite_smallcap → 小型株・特殊状況（6週）
#   avoid             → 除外
#
# 【付与フィールド】
#   strategy_label   : "core"/"satellite_event"/"satellite_smallcap"/"avoid"
#   hold_weeks       : 想定保有期間（週）
#   entry_trigger    : エントリー条件の説明文
#   exit_trigger     : エグジット条件の説明文
#   conviction       : "high"/"medium"/"low"（I-4 連動）
#
# 【適用タイミング】
#   stock_screener.py の最終ランキング後に候補リストに一括付与する。
#   または main.py の screener 完了後ブロックで適用。
#
# 【依存】config.py
# ==============================================================

from __future__ import annotations

from typing import Optional


# ==============================================================
# BLOCK 0: スキーマ定義
# ==============================================================

STRATEGY_SCHEMAS = {
    "core": {
        "hold_weeks":     12,
        "entry_trigger":  "sector_tier=TOP かつ composite_score≥70 かつ acc_bonus≥+5pt",
        "exit_trigger":   "composite_score < 55 または sector_tier=BOT",
        "conviction_min": "medium",
    },
    "satellite_event": {
        "hold_weeks":     4,
        "entry_trigger":  "決算サプライズ/8-K検知/TDnet重要開示 かつ signal_count≥3",
        "exit_trigger":   "イベント後4週経過 または composite_score < 50",
        "conviction_min": "low",
    },
    "satellite_smallcap": {
        "hold_weeks":     6,
        "entry_trigger":  "discovery_score=100 または activist_score≥70",
        "exit_trigger":   "catalyst消化 または composite_score < 45",
        "conviction_min": "low",
    },
    "avoid": {
        "hold_weeks":     0,
        "entry_trigger":  "エントリー不可",
        "exit_trigger":   "保有中の場合は利確/損切を検討",
        "conviction_min": "low",
    },
}

# 戦略分類の判定閾値
CORE_COMPOSITE_THRESHOLD      = 70.0   # composite_score がこれ以上でコア候補
CORE_LONGTERM_THRESHOLD       = 65.0   # longterm_scenario_score がこれ以上
SATELLITE_EVENT_SIGNAL_MIN    = 3      # イベントの独立根拠の最小数
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B6_STRATEGY_OUTPUT_SCHEMA_SATELLITE_SMALLCAP_DISCOVERY': 'SATELLITE_SMALLCAP_DISCOVERY',
}

SATELLITE_SMALLCAP_DISCOVERY  = 80     # discovery_score がこれ以上で小型特殊
AVOID_COMPOSITE_THRESHOLD     = 45.0   # composite_score がこれ未満で回避


# ==============================================================
# BLOCK 1: 戦略ラベルの判定
# ==============================================================

def _count_event_signals(candidate: dict) -> int:
    """
    候補銘柄のイベントシグナル独立根拠数を数える。

    カウント対象:
      - earnings_surprise_score > 0
      - tdnet_flag = True
      - 8-K 検知（in_8k_hits）
      - activist_score > 50
    """
    count = 0
    if (candidate.get("earnings_surprise_score") or 0) > 0:
        count += 1
    if candidate.get("tdnet_flag") or candidate.get("tdnet_score", 0) > 0:
        count += 1
    if candidate.get("in_8k_hits"):
        count += 1
    if (candidate.get("activist_score") or 0) > 50:
        count += 1
    if (candidate.get("acc_bonus") or 0) > 7:
        count += 1
    return count


def determine_strategy_label(candidate: dict, cfg=None) -> str:
    """
    候補銘柄1件の戦略ラベルを判定する。

    判定優先順:
      1. composite_score が低すぎる → avoid
      2. 長期×短期ともに高い → core
      3. イベント根拠が複数ある → satellite_event
      4. discovery_score / activist が高い → satellite_smallcap
      5. それ以外でも通過 → satellite_event（デフォルト）
    """
    composite   = candidate.get("composite_score") or 0.0
    lt_score    = candidate.get("longterm_scenario_score") or 0.0
    disc_score  = candidate.get("discovery_score") or 0
    activist    = candidate.get("activist_score") or 0
    event_count = _count_event_signals(candidate)
    tier        = candidate.get("tier", "MID")

    core_th     = getattr(cfg, "CORE_COMPOSITE_THRESHOLD",   CORE_COMPOSITE_THRESHOLD)   if cfg else CORE_COMPOSITE_THRESHOLD
    lt_th       = getattr(cfg, "CORE_LONGTERM_THRESHOLD",    CORE_LONGTERM_THRESHOLD)    if cfg else CORE_LONGTERM_THRESHOLD
    ev_min      = getattr(cfg, "SATELLITE_EVENT_SIGNAL_MIN", SATELLITE_EVENT_SIGNAL_MIN) if cfg else SATELLITE_EVENT_SIGNAL_MIN
    disc_th     = getattr(cfg, "SATELLITE_SMALLCAP_DISCOVERY", SATELLITE_SMALLCAP_DISCOVERY) if cfg else SATELLITE_SMALLCAP_DISCOVERY
    avoid_th    = getattr(cfg, "AVOID_COMPOSITE_THRESHOLD",  AVOID_COMPOSITE_THRESHOLD)  if cfg else AVOID_COMPOSITE_THRESHOLD

    # ① avoid
    if composite < avoid_th or tier == "BOT":
        return "avoid"

    # ② core（両方向で高評価）
    if composite >= core_th and (lt_score >= lt_th or tier == "TOP"):
        return "core"

    # ③ satellite_event（イベント根拠が多い）
    if event_count >= ev_min:
        return "satellite_event"

    # ④ satellite_smallcap（小型・特殊状況）
    if disc_score >= disc_th or activist >= 70:
        return "satellite_smallcap"

    # ⑤ デフォルト（通過しているがコア要件未満）
    if composite >= core_th * 0.8:
        return "satellite_event"

    return "avoid"


def _determine_conviction(candidate: dict) -> str:
    """
    候補銘柄の確信度ラベルを返す。
    I-4 の conviction_level と連動する。
    """
    level = candidate.get("conviction_level") or 0
    score = candidate.get("conviction_score") or 0.0

    if level >= 4 or score >= 75:
        return "high"
    elif level >= 2 or score >= 50:
        return "medium"
    return "low"


# ==============================================================
# BLOCK 2: スキーマフィールドの付与
# ==============================================================

def enrich_candidate_with_strategy(candidate: dict, cfg=None) -> dict:
    """
    candidate 辞書に戦略スキーマフィールドを付与する。

    Returns:
        戦略フィールドを追加した candidate dict（in-place 更新）
    """
    label  = determine_strategy_label(candidate, cfg)
    schema = STRATEGY_SCHEMAS.get(label, STRATEGY_SCHEMAS["avoid"])

    candidate["strategy_label"] = label
    candidate["hold_weeks"]     = schema["hold_weeks"]
    candidate["entry_trigger"]  = schema["entry_trigger"]
    candidate["exit_trigger"]   = schema["exit_trigger"]
    candidate["conviction"]     = _determine_conviction(candidate)

    # コア戦略には macro_phase を付与
    if label == "core":
        candidate["macro_phase"] = candidate.get("_macro_phase", "B")

    # サテライトイベントにはシグナル数を付与
    if label == "satellite_event":
        candidate["signal_count"] = _count_event_signals(candidate)

    # サテライト小型株には catalyst を付与
    if label == "satellite_smallcap":
        catalyst = []
        if (candidate.get("activist_score") or 0) >= 70:
            catalyst.append("アクティビスト保有検知")
        if candidate.get("buyback_detected"):
            catalyst.append("自社株買い検知")
        if (candidate.get("discovery_score") or 0) >= 100:
            catalyst.append("特別発見スコア")
        candidate["catalyst"] = " / ".join(catalyst) if catalyst else "特殊状況"

    return candidate


def enrich_all_candidates(candidates: list, cfg=None) -> tuple[list, dict]:
    """
    screener の全候補銘柄に戦略スキーマを一括付与する。

    Returns:
        (enriched_candidates, strategy_summary)
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    core_list     = []
    event_list    = []
    smallcap_list = []
    avoid_list    = []

    for c in candidates:
        enrich_candidate_with_strategy(c, cfg)
        label = c.get("strategy_label", "avoid")
        if label == "core":
            core_list.append(c.get("ticker", ""))
        elif label == "satellite_event":
            event_list.append(c.get("ticker", ""))
        elif label == "satellite_smallcap":
            smallcap_list.append(c.get("ticker", ""))
        else:
            avoid_list.append(c.get("ticker", ""))

    summary = {
        "core":               core_list,
        "satellite_event":    event_list,
        "satellite_smallcap": smallcap_list,
        "avoid":              avoid_list,
        "core_top":           core_list[:5],
        "satellite_top":      (event_list + smallcap_list)[:5],
    }

    print(f"    📊 [⑥] 戦略分類: "
          f"コア{len(core_list)} / "
          f"イベント{len(event_list)} / "
          f"小型{len(smallcap_list)} / "
          f"回避{len(avoid_list)}")

    return candidates, summary


# ==============================================================
# BLOCK 3: weekly_report 向けフォーマット
# ==============================================================

def format_strategy_section(
    candidates:       list,
    strategy_summary: dict,
    sector_names:     dict = None,
) -> list:
    """
    戦略別セクションを Markdown リスト形式で生成する。
    weekly_report_generator.py の _section_screener() から呼び出す。
    """
    lines = []
    sector_names = sector_names or {}

    # ── コア長期保有 ─────────────────────────────────────────
    core_tickers = strategy_summary.get("core", [])
    if core_tickers:
        lines += ["### 🔵 コア長期保有候補", ""]
        core_cands = [c for c in candidates if c.get("strategy_label") == "core"]
        for c in core_cands[:5]:
            sec_name = sector_names.get(c.get("sector_code", ""), "")
            lines.append(
                f"- **{c.get('ticker','')}** "
                f"（{sec_name}）"
                f" score={c.get('composite_score',0):.0f} "
                f"/ LT={c.get('longterm_scenario_score','-')}"
                f" / 保有目安: {c.get('hold_weeks',12)}週"
            )
        lines.append("")

    # ── サテライト（イベント）────────────────────────────────
    ev_tickers = strategy_summary.get("satellite_event", [])
    if ev_tickers:
        lines += ["### 🟡 サテライト（イベントドリブン）候補", ""]
        ev_cands = [c for c in candidates if c.get("strategy_label") == "satellite_event"]
        for c in ev_cands[:5]:
            sec_name = sector_names.get(c.get("sector_code", ""), "")
            lines.append(
                f"- **{c.get('ticker','')}** "
                f"（{sec_name}）"
                f" 根拠{c.get('signal_count',0)}件"
                f" / 保有目安: {c.get('hold_weeks',4)}週"
                f" / {c.get('conviction','')}確信度"
            )
        lines.append("")

    # ── サテライト（小型株・特殊）────────────────────────────
    sc_tickers = strategy_summary.get("satellite_smallcap", [])
    if sc_tickers:
        lines += ["### 🟢 サテライト（小型株・特殊状況）候補", ""]
        sc_cands = [c for c in candidates if c.get("strategy_label") == "satellite_smallcap"]
        for c in sc_cands[:5]:
            lines.append(
                f"- **{c.get('ticker','')}**"
                f" catalyst={c.get('catalyst','')}"
                f" / discovery={c.get('discovery_score',0)}"
                f" / 保有目安: {c.get('hold_weeks',6)}週"
            )
        lines.append("")

    return lines
