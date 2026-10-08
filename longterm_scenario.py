# ==============================================================
# longterm_scenario.py  v1.0  (R-5 / 両部篤モデル)
# 10年単位シナリオプランナー
# ==============================================================
# 【役割】
#   「5〜10年後の産業地図」から逆算して今の投資先を決める。
#   3軸 × 3状態 = 9シナリオ × 33業種の長期スコアマトリクスを生成し、
#   現在のマクロ環境から確率ウェイトを付与して期待値スコアを算出する。
#
# 【3軸の定義】
#   tech  : テクノロジー普及シナリオ（fast/standard/slow）
#   geo   : 地政学シナリオ（cooperation/decoupling/conflict）
#   japan : 日本マクロシナリオ（revival/stagnation/transform）
#
# 【確率ウェイトの更新トリガー】
#   自動: ArXiv AI論文急増 → tech="fast" 確率up
#         recession_score高 → japan="stagnation" 確率up
#         BDI急落           → geo="conflict" 確率up
#         日銀政策変更検知   → japan ウェイト更新
#   手動: config.py の SCENARIO_WEIGHTS を直接編集（年2回推奨）
#
# 【短期スコアとの統合（デュアル評価）】
#   composite_score（短期）× longterm_scenario_score（長期）
#   → 投資スタンス: "core_hold"/"trade"/"accumulate"/"avoid"
#
# 【出力スキーマ】
#   {
#     "sector_longterm_scores": {sector_code: score(0〜100)},
#     "scenario_weights":       {シナリオkey: weight(0〜1)},
#     "dominant_scenario":      str,
#     "investment_stances":     {sector_code: stance_dict},
#     "auto_triggers":          [trigger_dict, ...],
#     "summary_text":           str,
#     "timestamp":              str,
#   }
#
# 【依存】config.py / results dict（arxiv/bond_commodity/economic_cycle）
# ==============================================================

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: シナリオ定義
# ==============================================================

SCENARIO_AXES = {
    "tech": {
        "fast":     "AI高速普及（〜2030年）",
        "standard": "AI標準普及（〜2035年）",
        "slow":     "AI低速普及（〜2040年以降）",
    },
    "geo": {
        "cooperation": "地政学協調（グローバル維持）",
        "decoupling":  "地政学分断（デカップリング）",
        "conflict":    "地政学対立激化（台湾有事等）",
    },
    "japan": {
        "revival":    "日本復活（賃上げ→消費→金利正常化）",
        "stagnation": "日本停滞（人口減少・生産性低下）",
        "transform":  "日本構造転換（移民・規制緩和）",
    },
}

# 9シナリオのキー（tech_geo_japan の組み合わせ）
SCENARIO_KEYS = [
    f"{t}_{g}_{j}"
    for t in ("fast", "standard", "slow")
    for g in ("cooperation", "decoupling", "conflict")
    for j in ("revival", "stagnation", "transform")
]

# デフォルトの確率ウェイト（合計が1.0になるよう正規化）
# config.py の SCENARIO_WEIGHTS_INIT で上書き可能

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'LS_DEFAULT_WEIGHTS': 'DEFAULT_WEIGHTS',
    'LS_STANCE_THRESHOLDS': 'STANCE_THRESHOLDS',
    'B6_LONGTERM_SCENARIO_LONGTERM_HOLD_STANCE_RELAX_PT': 'LONGTERM_HOLD_STANCE_RELAX_PT',
}

DEFAULT_WEIGHTS = {k: 1.0 / 9 for k in SCENARIO_KEYS}   # 均等スタート

# 各シナリオ × 業種の長期スコア補正値（-25〜+25pt）
# config.py の LONGTERM_SECTOR_MATRIX で上書き可能
# 形式: {scenario_key: {sector_code: delta_pt}}
DEFAULT_SECTOR_MATRIX: dict[str, dict[str, float]] = {
    # ── 高速普及 × 協調 × 復活 ────────────────────────────────
    "fast_cooperation_revival": {
        "1016": 25, "1025": 22, "1018": 18, "1015": 15, "1033": 12,
        "1007": 10, "1028": 8,  "1027": 8,
        "1022": -8, "1009": -10, "1005": -8,
    },
    # ── 高速普及 × 分断 × 復活 ────────────────────────────────
    "fast_decoupling_revival": {
        "1016": 20, "1025": 20, "1018": 15, "1015": 12, "1033": 10,
        "1028": 10, "1027": 8,
        "1022": -15, "1009": -8, "1026": -8,
    },
    # ── 高速普及 × 対立 × 停滞 ────────────────────────────────
    "fast_conflict_stagnation": {
        "1016": 10, "1018": 12, "1015": 10,
        "1022": -18, "1009": -12, "1026": -15, "1027": -10,
        "1028": -8,
    },
    # ── 標準普及 × 協調 × 復活 ────────────────────────────────
    "standard_cooperation_revival": {
        "1016": 15, "1025": 15, "1015": 12, "1033": 10, "1028": 12,
        "1027": 10, "1032": 8,
        "1022": -5, "1009": -5,
    },
    # ── 標準普及 × 分断 × 停滞 ────────────────────────────────
    "standard_decoupling_stagnation": {
        "1016": 8,  "1018": 10, "1028": 5,
        "1022": -10, "1026": -8, "1027": -8, "1009": -6,
    },
    # ── 標準普及 × 協調 × 構造転換 ───────────────────────────
    "standard_cooperation_transform": {
        "1016": 18, "1025": 15, "1033": 15, "1027": 12, "1032": 10,
        "1028": 8,
        "1009": -5, "1005": -5,
    },
    # ── 低速普及 × 分断 × 停滞 ────────────────────────────────
    "slow_decoupling_stagnation": {
        "1009": 10, "1012": 8, "1004": 5, "1028": 5,
        "1016": -10, "1025": -12, "1033": -8, "1015": -8,
    },
    # ── 低速普及 × 対立 × 停滞 ────────────────────────────────
    "slow_conflict_stagnation": {
        "1009": 15, "1012": 10, "1013": 8,
        "1016": -15, "1025": -15, "1022": -20, "1027": -12,
    },
    # ── 低速普及 × 対立 × 構造転換 ───────────────────────────
    "slow_conflict_transform": {
        "1009": 12, "1012": 8, "1015": 10,
        "1016": -10, "1025": -10, "1022": -15,
    },
}


# ==============================================================
# BLOCK 1: ファイル I/O
# ==============================================================

LONGTERM_FILE = "longterm_scenario_latest.json"


def _data_dir(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data"))


def load_longterm_scenario(cfg) -> dict:
    p = _data_dir(cfg) / LONGTERM_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_longterm_scenario(cfg, result: dict) -> None:
    p = _data_dir(cfg) / LONGTERM_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  longterm_scenario_latest.json 保存失敗: {e}")


# ==============================================================
# BLOCK 2: シナリオ確率ウェイトの取得と正規化
# ==============================================================

def _load_scenario_weights(cfg) -> dict:
    """
    確率ウェイトを取得する。
    優先順: config.SCENARIO_WEIGHTS → longterm_scenario_latest.json → デフォルト均等分布
    """
    # config から上書きウェイトを取得
    cfg_weights = getattr(cfg, "SCENARIO_WEIGHTS_INIT", {})
    if cfg_weights:
        weights = {**DEFAULT_WEIGHTS, **cfg_weights}
    else:
        # 前回保存値を継続使用
        prev = load_longterm_scenario(cfg)
        if prev.get("scenario_weights"):
            weights = prev["scenario_weights"]
        else:
            weights = dict(DEFAULT_WEIGHTS)

    return _normalize_weights(weights)


def _normalize_weights(weights: dict) -> dict:
    """全ウェイトの合計が1.0になるよう正規化する。"""
    total = sum(v for v in weights.values() if v is not None and v >= 0)
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: round(v / total, 6) for k, v in weights.items() if v is not None}


# ==============================================================
# BLOCK 3: 自動更新トリガー
# ==============================================================

def _apply_auto_triggers(weights: dict, results: dict, cfg) -> tuple[dict, list]:
    """
    マクロ環境の変化からシナリオ確率ウェイトを自動調整する。

    トリガー:
      1. ArXiv AI論文急増 → tech="fast" 確率up (+0.05 / 各"fast_*" に加算)
      2. recession_score高（>=60） → japan="stagnation" 確率up
      3. BDI相当の海運指標急落 → geo="conflict" 確率up
      4. macro_snapshot の逆イールド → japan="stagnation" 確率up

    Returns:
        (adjusted_weights, trigger_log)
    """
    adjusted = dict(weights)
    triggers = []

    # ── トリガー1: ArXiv AI論文急増 ──────────────────────────
    arxiv_res = results.get("arxiv", {})
    if isinstance(arxiv_res, dict):
        new_papers = arxiv_res.get("new_papers", 0)
        arxiv_threshold = getattr(cfg, "ARXIV_INVESTMENT_THRESHOLD", 5)
        if new_papers >= arxiv_threshold * 2:   # 通常の2倍以上の新着
            boost = 0.05
            for k in SCENARIO_KEYS:
                if k.startswith("fast_"):
                    adjusted[k] = adjusted.get(k, 0) + boost / 3
                elif k.startswith("slow_"):
                    adjusted[k] = max(0, adjusted.get(k, 0) - boost / 3)
            triggers.append({
                "trigger": "arxiv_ai_surge",
                "desc":    f"ArXiv新着{new_papers}件 → AI高速普及シナリオ確率+{boost*100:.0f}%",
                "axis":    "tech",
                "direction": "fast",
            })

    # ── トリガー2: recession_score高 → 停滞シナリオ up ──────
    bc = results.get("bond_commodity", {})
    rec_score = bc.get("recession_score", 30) if isinstance(bc, dict) else 30
    if rec_score >= 60:
        boost = min(0.06, (rec_score - 60) / 100)
        for k in SCENARIO_KEYS:
            if "_stagnation" in k:
                adjusted[k] = adjusted.get(k, 0) + boost / 3
            elif "_revival" in k:
                adjusted[k] = max(0, adjusted.get(k, 0) - boost / 3 * 0.5)
        triggers.append({
            "trigger":   "high_recession_risk",
            "desc":      f"リセッションスコア{rec_score}/100 → 停滞シナリオ確率+{boost*100:.0f}%",
            "axis":      "japan",
            "direction": "stagnation",
        })

    # ── トリガー3: 逆イールド → 分断・停滞 up ───────────────
    macro = results.get("macro_snapshot", {})
    spread = None
    if isinstance(bc, dict):
        ca = bc.get("curve_analysis", {})
        spread = ca.get("spread_2y10y")
    if spread is None:
        spread = macro.get("spread_2y10y")
    if spread is not None and spread < -0.30:   # 逆イールド深化
        boost = 0.03
        for k in SCENARIO_KEYS:
            if "_decoupling_" in k or "_conflict_" in k:
                adjusted[k] = adjusted.get(k, 0) + boost / 6
        triggers.append({
            "trigger":   "inverted_yield",
            "desc":      f"逆イールド深化（spread={spread:.2f}%）→ 分断シナリオ確率up",
            "axis":      "geo",
            "direction": "decoupling",
        })

    # ── トリガー4: VIX高 → 対立シナリオ up ─────────────────
    vix = macro.get("vix")
    if vix and vix >= 30:
        boost = min(0.05, (vix - 30) / 200)
        for k in SCENARIO_KEYS:
            if "_conflict_" in k:
                adjusted[k] = adjusted.get(k, 0) + boost / 3
        triggers.append({
            "trigger":   "high_vix",
            "desc":      f"VIX={vix:.0f} → 地政学対立シナリオ確率up",
            "axis":      "geo",
            "direction": "conflict",
        })

    return _normalize_weights(adjusted), triggers


# ==============================================================
# BLOCK 4: 長期スコアマトリクス計算
# ==============================================================

def _get_sector_matrix(cfg) -> dict:
    """シナリオ×業種スコアマトリクスを返す（config で上書き可能）。"""
    cfg_matrix = getattr(cfg, "LONGTERM_SECTOR_MATRIX", {})
    matrix = dict(DEFAULT_SECTOR_MATRIX)
    matrix.update(cfg_matrix)
    return matrix


def calc_longterm_sector_scores(
    weights: dict,
    sector_codes: list,
    cfg = None,
) -> dict:
    """
    確率ウェイト × シナリオ別業種スコアの期待値を計算する。

    期待値スコア = Σ(weight_i × delta_i) を 0〜100 に正規化。
    正規化基準: 全シナリオで平均的な業種を 50pt とする。

    Args:
        weights:      シナリオ確率ウェイト dict
        sector_codes: 評価対象の業種コードリスト（config の JAPAN_SECTORS）
        cfg:          config モジュール

    Returns:
        {sector_code: longterm_score(0〜100)}
    """
    matrix = _get_sector_matrix(cfg)

    # 各業種の期待値（加重平均delta）を計算
    expected_deltas = {}
    for code in sector_codes:
        weighted_delta = 0.0
        for scenario_key, w in weights.items():
            delta = matrix.get(scenario_key, {}).get(code, 0.0)
            weighted_delta += float(w) * float(delta)
        expected_deltas[code] = round(weighted_delta, 2)

    # -25〜+25 → 0〜100 に正規化（0delta = 50pt）
    scores = {}
    for code, delta in expected_deltas.items():
        score = 50.0 + delta * 2.0   # 1pt delta = 2pt score変化
        scores[code] = round(max(0.0, min(100.0, score)), 1)

    return scores


# ==============================================================
# BLOCK 5: 投資スタンス判定（デュアル評価）
# ==============================================================

STANCE_THRESHOLDS = {
    "long_high":  65.0,   # longterm_score がこれ以上 = 長期有望
    "short_high": 60.0,   # composite_score がこれ以上 = 短期有望
}

# [P-3] 長期保有候補がいる業種で long_high を緩める幅（pt）。
# 既定値は config.LONGTERM_HOLD_STANCE_RELAX_PT が正本。
LONGTERM_HOLD_STANCE_RELAX_PT = 5.0


def _count_longterm_holds_by_sector(results: dict) -> dict:
    """業種コード → その業種の長期保有候補（[P-3]）の社数。

    is_longterm_hold は screener が銘柄単位に付けるため、
    業種単位のスタンス判定に渡すには集計が要る。
    screener が未実行・候補ゼロなら空dictを返す（呼び出し側は0扱い）。
    """
    counts: dict = {}
    screener = results.get("screener", {})
    if not isinstance(screener, dict):
        return counts
    for c in screener.get("candidates", []) or []:
        if not isinstance(c, dict):
            continue
        if not c.get("is_longterm_hold"):
            continue
        if not c.get("pass_filters", True):
            continue
        code = str(c.get("sector_code", "") or "")
        if code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def determine_investment_stance(
    sector_code:           str,
    longterm_score:        float,
    composite_score:       float,
    is_longterm_hold_flag: bool = False,
    longterm_hold_count:   int  = 0,
    cfg = None,
) -> dict:
    """
    長期スコア × 短期スコアからデュアル評価を行い投資スタンスを判定する。

    ★ [P-3] 長期保有候補フラグをスタンス分岐に使う。

      【組み込み方】P-3 候補がその業種に存在する場合、長期側の閾値
      （long_high）を LONGTERM_HOLD_STANCE_RELAX_PT だけ緩める。
      理由: longterm_score は業種の10年シナリオという「マクロ・構造の見立て」だが、
      P-3 は「自己資本60%以上×増益基調×ROE10%以上」を満たす企業が実在するという
      **銘柄レベルの実証**である。見立てに実物の裏付けが付くなら長期側の確信を
      1段上げてよい、という考え方。短期側（short_high）には影響させない。

      効果の例（long_high=65 / relax=5 の場合）:
        長期62pt × 短期65pt × P-3あり → trade ではなく core_hold
        長期62pt × 短期50pt × P-3あり → avoid ではなく accumulate
        P-3なしの業種は緩めない

    Args:
        is_longterm_hold_flag: その業種に P-3 候補が存在するか
        longterm_hold_count:   P-3 候補の社数（reason の根拠表示に使う）
        cfg:                   config モジュール（緩和幅の正本）

    Returns:
        {
          "stance":    "core_hold" / "trade" / "accumulate" / "avoid",
          "label_ja":  str,
          "reason":    str,
          "icon":      str,
          "long_high_applied": float,   # 実際に使った長期閾値
          "longterm_hold_boost": bool,  # P-3 による緩和が効いたか
        }
    """
    long_high = float(STANCE_THRESHOLDS["long_high"])
    relax = float(getattr(cfg, "LONGTERM_HOLD_STANCE_RELAX_PT",
                          LONGTERM_HOLD_STANCE_RELAX_PT))
    if is_longterm_hold_flag:
        long_high -= relax

    lt_high = longterm_score  >= long_high
    st_high = composite_score >= STANCE_THRESHOLDS["short_high"]

    # 緩和がなければ長期低と判定されていたか（＝P-3が結論を動かしたか）
    boosted = bool(
        is_longterm_hold_flag
        and lt_high
        and longterm_score < float(STANCE_THRESHOLDS["long_high"])
    )
    # 根拠の追記。P-3 が結論を動かした時だけ理由に出す（ノイズを避ける）
    p3_note = (f"／P-3長期保有候補{longterm_hold_count}社により長期閾値"
               f"{relax:.0f}pt緩和" if boosted else "")

    meta = {"long_high_applied": round(long_high, 1),
            "longterm_hold_boost": boosted}

    if lt_high and st_high:
        return {
            "stance":   "core_hold",
            "label_ja": "🔵 コア長期保有",
            "reason":   f"長期{longterm_score:.0f}pt × 短期{composite_score:.0f}pt → 双方有望{p3_note}",
            "icon":     "🔵",
            **meta,
        }
    elif st_high and not lt_high:
        return {
            "stance":   "trade",
            "label_ja": "🟡 短期トレード",
            "reason":   f"短期{composite_score:.0f}pt高×長期{longterm_score:.0f}pt低 → 利食い後縮小",
            "icon":     "🟡",
            **meta,
        }
    elif lt_high and not st_high:
        return {
            "stance":   "accumulate",
            "label_ja": "🟢 仕込みフェーズ",
            "reason":   f"長期{longterm_score:.0f}pt高×短期{composite_score:.0f}pt低 → 下落時積み増し{p3_note}",
            "icon":     "🟢",
            **meta,
        }
    else:
        return {
            "stance":   "avoid",
            "label_ja": "⚪ 回避",
            "reason":   f"長期{longterm_score:.0f}pt × 短期{composite_score:.0f}pt → 双方低",
            "icon":     "⚪",
            **meta,
        }


# ==============================================================
# BLOCK 6: サマリーテキスト生成
# ==============================================================

def _format_summary(
    weights:              dict,
    longterm_scores:      dict,
    investment_stances:   dict,
    auto_triggers:        list,
    cfg = None,
) -> str:
    """週次レポート向けサマリーテキストを生成する。"""
    japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})

    # 支配的シナリオ（最高ウェイト）
    dominant = max(weights.items(), key=lambda x: x[1])
    dominant_key   = dominant[0]
    dominant_w     = dominant[1]
    parts          = dominant_key.split("_", 2)
    tech_label  = SCENARIO_AXES["tech"].get(parts[0], parts[0]) if len(parts) > 0 else ""
    geo_label   = SCENARIO_AXES["geo"].get(parts[1], parts[1])  if len(parts) > 1 else ""
    japan_label = SCENARIO_AXES["japan"].get(parts[2], parts[2]) if len(parts) > 2 else ""

    lines = [
        "## 🔭 10年シナリオプランナー（R-5）",
        "",
        f"**支配的シナリオ**: {tech_label} × {geo_label} × {japan_label}",
        f"（確率ウェイト: {dominant_w*100:.1f}%）",
        "",
    ]

    # 自動トリガー
    if auto_triggers:
        lines.append("### ⚡ 自動更新トリガー")
        lines.append("")
        for t in auto_triggers[:4]:
            lines.append(f"- {t['desc']}")
        lines.append("")

    # 長期スコア上位・下位
    sorted_scores = sorted(
        [(c, s) for c, s in longterm_scores.items()],
        key=lambda x: x[1], reverse=True
    )
    lines.append("### 📈 長期有望業種（10年スコア上位）")
    lines.append("")
    for code, score in sorted_scores[:5]:
        name    = japan_sectors.get(code, {}).get("name_ja", code)
        stance  = investment_stances.get(code, {}).get("icon", "")
        lines.append(f"- {stance} **{name}**: {score:.0f}pt")
    lines.append("")

    lines.append("### 📉 長期不利業種（10年スコア下位）")
    lines.append("")
    for code, score in sorted_scores[-5:]:
        name = japan_sectors.get(code, {}).get("name_ja", code)
        lines.append(f"- ⚪ **{name}**: {score:.0f}pt")
    lines.append("")

    # 投資スタンス集計
    stance_counts = {}
    for _, stance_dict in investment_stances.items():
        s = stance_dict.get("stance", "avoid")
        stance_counts[s] = stance_counts.get(s, 0) + 1

    lines.append("### 🎯 投資スタンス分布")
    lines.append("")
    icon_map = {
        "core_hold":  "🔵 コア長期保有",
        "trade":      "🟡 短期トレード",
        "accumulate": "🟢 仕込みフェーズ",
        "avoid":      "⚪ 回避",
    }
    for stance_key, label in icon_map.items():
        cnt = stance_counts.get(stance_key, 0)
        if cnt > 0:
            lines.append(f"- {label}: {cnt}業種")
    lines.append("")

    return "\n".join(lines)


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_longterm_scenario(cfg, results: dict = None) -> dict:
    """
    10年シナリオプランナーを実行する。

    stock_analyzer_main.py から以下のように呼び出す（月次 or 週次）:
        _ls = _load_local_module("longterm_scenario", ...)
        ls_result = _ls.run_longterm_scenario(cfg, results)
        results["longterm_scenario"] = ls_result

    処理:
      1. シナリオ確率ウェイトの取得（config → 前回保存値 → デフォルト）
      2. マクロ環境から自動トリガー適用
      3. 33業種の長期スコア期待値を計算
      4. 短期 composite_score と組み合わせてデュアル評価
      5. longterm_scenario_latest.json に保存

    Returns:
        上記スキーマの dict
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_LONGTERM_SCENARIO", True):
        return {"skipped": True}

    print("\n  🔭 10年シナリオプランナー 実行中...")
    t0 = datetime.now()

    results = results or {}

    # ── Step1: 確率ウェイト取得 ──────────────────────────────
    weights = _load_scenario_weights(cfg)

    # ── Step2: 自動トリガー適用 ──────────────────────────────
    weights, auto_triggers = _apply_auto_triggers(weights, results, cfg)

    # ── Step3: 業種コードリスト取得 ──────────────────────────
    japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})
    sector_codes  = list(japan_sectors.keys()) if japan_sectors else [
        str(1000 + i) for i in range(1, 34)
    ]

    # ── Step4: 長期スコア計算 ────────────────────────────────
    longterm_scores = calc_longterm_sector_scores(weights, sector_codes, cfg)

    # ── Step5: 支配的シナリオ特定 ────────────────────────────
    dominant_scenario = max(weights.items(), key=lambda x: x[1])[0]

    # ── Step6: 投資スタンス判定（短期スコアと統合）──────────
    # ★ is_longterm_hold（[P-3] 片山晃モデル）は screener が **銘柄単位** に
    #   付けるもので、業種の dict（sector_results[code]）には存在しない
    #   （そこから読むと常に False）。
    #   業種単位では「その業種に長期保有候補が何社いるか」で表現する。
    sector_results  = results.get("sectors", {})
    lt_counts = _count_longterm_holds_by_sector(results)
    investment_stances = {}
    for code in sector_codes:
        lt_score = longterm_scores.get(code, 50.0)
        sec_data = sector_results.get(code, {})
        st_score = sec_data.get("composite_score", 50.0) if isinstance(sec_data, dict) else 50.0
        lt_n     = lt_counts.get(str(code), 0)
        is_lt    = lt_n > 0

        stance = determine_investment_stance(
            code, lt_score, st_score, is_lt,
            longterm_hold_count=lt_n, cfg=cfg,
        )
        investment_stances[code] = {
            "sector_code":      code,
            "longterm_score":   lt_score,
            "composite_score":  round(st_score, 1),
            "longterm_hold_count": lt_n,
            **stance,
        }

    # ── Step7: サマリーテキスト ──────────────────────────────
    summary_text = _format_summary(
        weights, longterm_scores, investment_stances, auto_triggers, cfg
    )

    elapsed = (datetime.now() - t0).seconds
    core_hold_count = sum(
        1 for s in investment_stances.values()
        if s.get("stance") == "core_hold"
    )
    print(
        f"    ✅ シナリオプランナー完了（{elapsed}秒）: "
        f"支配的={dominant_scenario} "
        f"| コア保有対象={core_hold_count}業種"
    )
    if auto_triggers:
        print(f"    ⚡ 自動更新トリガー {len(auto_triggers)}件発動")

    result = {
        "sector_longterm_scores": longterm_scores,
        "scenario_weights":       weights,
        "dominant_scenario":      dominant_scenario,
        "investment_stances":     investment_stances,
        "auto_triggers":          auto_triggers,
        "summary_text":           summary_text,
        "timestamp":              t0.strftime("%Y-%m-%d %H:%M:%S"),
        "skipped":                False,
    }

    save_longterm_scenario(cfg, result)
    return result


# ==============================================================
# BLOCK 8: screener 銘柄への長期スコア付与
# ==============================================================

def enrich_candidates_with_longterm(
    candidates:       list,
    longterm_result:  dict,
) -> list:
    """
    screener の candidate 辞書に longterm_scenario_score / investment_stance を付与する。

    stock_analyzer_main.py の screener 完了後に呼び出す:
        _ls.enrich_candidates_with_longterm(
            candidates      = screen_result["candidates"],
            longterm_result = results.get("longterm_scenario", {}),
        )
    """
    if not longterm_result or longterm_result.get("skipped"):
        return candidates

    lt_scores = longterm_result.get("sector_longterm_scores", {})
    stances   = longterm_result.get("investment_stances", {})

    for c in candidates:
        code = c.get("sector_code", "")
        if not code:
            continue
        lt_score = lt_scores.get(code)
        stance   = stances.get(code, {})

        c["longterm_scenario_score"] = lt_score
        c["investment_stance"]       = stance.get("stance")
        c["investment_stance_label"] = stance.get("label_ja", "")

    return candidates
