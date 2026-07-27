# ==============================================================
# learning_audit_log.py  v1.0  (⑤ 学習ループ監査ログ)
# ==============================================================
# 【役割】
#   毎月末の rule_optimizer 実行後に「何を学習して何を変えたか」を
#   人間可読な監査ログとして出力する。
#   "manual_review_needed" フラグが True の場合、週次レポートに
#   「⚠️ ルール見直し推奨」セクションを冒頭表示する。
#
# 【生成ファイル】
#   stock_data/data/learning_audit_YYYY-MM.json
#
# 【出力スキーマ】
#   {
#     "period": "2026-03",
#     "factors_updated": [
#       {
#         "factor":     "yen_depreciation",
#         "old_weight": 0.95,
#         "new_weight": 1.12,
#         "delta":      +0.17,
#         "reason":     "輸出業種リターンとの相関: r=0.68（12週）",
#         "suggestion": "円安シグナルの閾値を140→135に下げることを検討",
#         "ic_current": 0.31,
#       }
#     ],
#     "underperforming_rules": ["us_recession_risk"],
#     "stable_signals":        ["yen_depreciation", "machine_tool_orders_up"],
#     "manual_review_needed":  true,
#     "review_reason":         str,
#     "ic_summary":            {factor: ic_value},
#     "generated_at":          str,
#   }
#
# 【依存】rule_optimizer の戻り値 / ic_tracker.json / config.py
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

AUDIT_FILE_PREFIX = "learning_audit_"
AUDIT_FILE_SUFFIX = ".json"

# 変更量がこれ以上の場合に「大きな変更」とみなす

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'LAL_SIGNIFICANT_WEIGHT_DELTA': 'SIGNIFICANT_WEIGHT_DELTA',
    'LAL_IC_UNDERPERFORM_THRESHOLD': 'IC_UNDERPERFORM_THRESHOLD',
    'B6_LEARNING_AUDIT_LOG_MANUAL_REVIEW_TRIGGER': 'MANUAL_REVIEW_TRIGGER',
}

SIGNIFICANT_WEIGHT_DELTA = 0.15

# IC がこれ未満のルールを「underperforming」とみなす
IC_UNDERPERFORM_THRESHOLD = 0.05

# IC_STABLE_THRESHOLD は削除（2026-07-25 バッチ4）。
# 「IC がこれ以上なら stable」という定義だったが、このモジュールは
# 安定判定を自前で行わず alpha_attributer が算出済みの stable_signals を
# 読むだけで、この定数を参照するコードは存在しなかった。
# 安定判定の正本は config.IC_STABLE_MIN_RATE（IC>=0 だった週の比率 0.80）で、
# 意味も基準も別物のため、紛らわしい残骸として削除する。

# 見直し推奨の閾値（underperforming が何件以上で manual_review_needed=True）
MANUAL_REVIEW_TRIGGER = 2


# ==============================================================
# BLOCK 1: ファイル I/O
# ==============================================================

def _data_dir(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data"))


def _audit_file_path(cfg, period: str) -> Path:
    """期間（"YYYY-MM"）に対応する監査ログのパスを返す。"""
    return _data_dir(cfg) / f"{AUDIT_FILE_PREFIX}{period}{AUDIT_FILE_SUFFIX}"


def load_latest_audit(cfg) -> Optional[dict]:
    """最新の監査ログを返す。なければ None。"""
    today  = date.today()
    period = f"{today.year}-{today.month:02d}"
    p = _audit_file_path(cfg, period)
    if not p.exists():
        # 前月分も確認
        if today.month == 1:
            prev = f"{today.year-1}-12"
        else:
            prev = f"{today.year}-{today.month-1:02d}"
        p = _audit_file_path(cfg, prev)

    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def save_audit(cfg, period: str, audit: dict) -> None:
    p = _audit_file_path(cfg, period)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"    📋 監査ログ保存: {p.name}")
    except Exception as e:
        print(f"    ⚠️  監査ログ保存失敗: {e}")


# ==============================================================
# BLOCK 2: IC トラッカーの読み込み
# ==============================================================

def _load_ic_tracker(cfg) -> dict:
    # 2026-07-25: BLOCK A-1 の ENABLE_IC_TRACKER を配線。
    # ic_tracker.json の読み込みと IC 分析の実装はあるのに、有効フラグだけが
    # 誰にも読まれず、False にしても IC ベースの見直し推奨が出続けていた。
    # 無効時は空dictを返す（_analyze_ic は空入力で空結果を返すため安全）。
    if not getattr(cfg, "ENABLE_IC_TRACKER", True):
        return {}
    p = _data_dir(cfg) / "ic_tracker.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data.get("latest", {})
        except Exception:
            pass
    return {}


def _load_rule_weights_prev(cfg) -> dict:
    """rule_weights.json の前回バックアップを読み込む。"""
    p = _data_dir(cfg) / "rule_weights_prev.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_rule_weights_current(cfg) -> dict:
    """rule_weights.json の現在値を読み込む。"""
    p = _data_dir(cfg) / "rule_weights.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ==============================================================
# BLOCK 3: 変更分析
# ==============================================================

def _analyze_weight_changes(
    prev_weights:    dict,
    current_weights: dict,
) -> list[dict]:
    """
    前回と現在の重みを比較して更新されたファクターのリストを返す。
    """
    updates = []
    all_factors = set(prev_weights.keys()) | set(current_weights.keys())

    for factor in sorted(all_factors):
        old_w = float(prev_weights.get(factor, {}).get("weight", 1.0)
                      if isinstance(prev_weights.get(factor), dict)
                      else prev_weights.get(factor, 1.0))
        new_w = float(current_weights.get(factor, {}).get("weight", 1.0)
                      if isinstance(current_weights.get(factor), dict)
                      else current_weights.get(factor, 1.0))
        delta = round(new_w - old_w, 4)

        if abs(delta) < 0.001:
            continue   # 変化なしはスキップ

        # 変更の理由を推定
        if delta > 0:
            reason = f"シグナルの有効性改善 → 重みを {old_w:.3f} → {new_w:.3f} に増加"
        else:
            reason = f"シグナルのIC低下 → 重みを {old_w:.3f} → {new_w:.3f} に減少"

        # 閾値変更の提案（大きな変更の場合のみ）
        suggestion = ""
        if abs(delta) >= SIGNIFICANT_WEIGHT_DELTA:
            if "yen" in factor.lower() or "usdjpy" in factor.lower():
                suggestion = "円安シグナルの閾値を見直すことを検討（140→135など）"
            elif "recession" in factor.lower():
                suggestion = "リセッションシグナルの発動条件を見直すことを検討"
            elif delta < 0:
                suggestion = f"{factor} シグナルの廃止または条件強化を検討"

        updates.append({
            "factor":     factor,
            "old_weight": round(old_w, 4),
            "new_weight": round(new_w, 4),
            "delta":      delta,
            "reason":     reason,
            "suggestion": suggestion,
        })

    return updates


def _analyze_ic(ic_tracker_latest: dict) -> tuple[list, list, dict]:
    """
    IC トラッカーから underperforming / stable シグナルを特定する。

    Returns:
        (underperforming: list, stable: list, ic_summary: dict)
    """
    underperforming = ic_tracker_latest.get("review_signals", [])
    stable          = ic_tracker_latest.get("stable_signals", [])
    history         = ic_tracker_latest.get("weekly_ic_history", {})

    ic_summary = {}
    for sig, ic_series in history.items():
        if ic_series:
            avg_ic = sum(ic_series) / len(ic_series)
            ic_summary[sig] = round(avg_ic, 4)

    return underperforming, stable, ic_summary


def _generate_suggestion(factor: str, delta: float, ic: Optional[float]) -> str:
    """ファクター名・重み変化・IC から提案文を生成する。"""
    if ic is not None and ic < IC_UNDERPERFORM_THRESHOLD:
        return f"ICが{ic:.3f}と低水準 → {factor}シグナルを一時停止し、条件を見直すことを推奨"
    if delta < -SIGNIFICANT_WEIGHT_DELTA:
        return f"重みが大幅減少（Δ{delta:+.3f}）→ {factor}のトリガー条件の見直しを検討"
    if delta > SIGNIFICANT_WEIGHT_DELTA:
        return f"重みが大幅増加（Δ{delta:+.3f}）→ 過学習リスクに注意（上限: 2.0）"
    return ""


# ==============================================================
# BLOCK 4: 監査ログ生成
# ==============================================================

def generate_audit_log(
    cfg,
    ro_result:      dict = None,
    results:        dict = None,
) -> dict:
    """
    rule_optimizer の実行結果から監査ログを生成する。

    Args:
        cfg:       config モジュール
        ro_result: run_rule_optimizer() の戻り値
        results:   run_all_tasks() の results dict（IC トラッカー参照用）

    Returns:
        監査ログ dict（スキーマは上部参照）
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    today  = date.today()
    period = f"{today.year}-{today.month:02d}"

    # ── データ読み込み ────────────────────────────────────────
    ic_latest  = _load_ic_tracker(cfg)
    prev_w     = _load_rule_weights_prev(cfg)
    current_w  = _load_rule_weights_current(cfg)

    # ro_result からも重み情報を補完
    if ro_result and isinstance(ro_result, dict):
        if not current_w:
            current_w = ro_result.get("new_weights", {}) or ro_result.get("weights", {})
        if not prev_w:
            prev_w = ro_result.get("old_weights", {}) or {}

    # ── 変更分析 ─────────────────────────────────────────────
    factors_updated = _analyze_weight_changes(prev_w, current_w)
    underperforming, stable_signals, ic_summary = _analyze_ic(ic_latest)

    # IC情報をfactors_updatedに付与
    for fu in factors_updated:
        factor = fu["factor"]
        ic_val = ic_summary.get(factor)
        fu["ic_current"] = ic_val
        if not fu["suggestion"]:
            fu["suggestion"] = _generate_suggestion(factor, fu["delta"], ic_val)

    # ── manual_review_needed の判定 ──────────────────────────
    manual_review_needed = False
    review_reasons = []

    n_significant = sum(1 for f in factors_updated if abs(f["delta"]) >= SIGNIFICANT_WEIGHT_DELTA)
    if len(underperforming) >= MANUAL_REVIEW_TRIGGER:
        manual_review_needed = True
        review_reasons.append(
            f"underperformingシグナル{len(underperforming)}件 "
            f"（{', '.join(underperforming[:3])}）"
        )
    if n_significant >= 3:
        manual_review_needed = True
        review_reasons.append(f"大幅重み変更{n_significant}件（±{SIGNIFICANT_WEIGHT_DELTA}以上）")

    review_reason = " / ".join(review_reasons) if review_reasons else "自動更新の範囲内（レビュー不要）"

    # ── 監査ログ組み立て ─────────────────────────────────────
    audit = {
        "period":                period,
        "factors_updated":       factors_updated,
        "underperforming_rules": underperforming,
        "stable_signals":        stable_signals,
        "manual_review_needed":  manual_review_needed,
        "review_reason":         review_reason,
        "ic_summary":            ic_summary,
        "stats": {
            "total_factors_checked":  len(current_w),
            "factors_changed":        len(factors_updated),
            "significant_changes":    n_significant,
            "underperforming_count":  len(underperforming),
            "stable_count":           len(stable_signals),
        },
        "generated_at":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    save_audit(cfg, period, audit)

    if manual_review_needed:
        print(f"    ⚠️  [⑤] 手動レビュー推奨: {review_reason}")
    else:
        print(f"    ✅ [⑤] 監査ログ生成: 変更{len(factors_updated)}件 / レビュー不要")

    return audit


# ==============================================================
# BLOCK 5: weekly_report 向けフォーマット
# ==============================================================

def format_audit_for_report(audit: dict) -> str:
    """
    監査ログを weekly_report_generator.py 向けのMarkdownに変換する。
    """
    if not audit:
        return ""

    lines = []

    if audit.get("manual_review_needed"):
        lines += [
            "## ⚠️ ルール見直し推奨（learning_audit）",
            "",
            f"**理由**: {audit.get('review_reason', '')}",
            "",
        ]
    else:
        lines += [
            "## ✅ ルール学習サマリー（learning_audit）",
            "",
        ]

    stats = audit.get("stats", {})
    lines += [
        f"- 確認ファクター数: {stats.get('total_factors_checked', 0)}",
        f"- 重み変更: {stats.get('factors_changed', 0)}件"
        f"（大幅変更: {stats.get('significant_changes', 0)}件）",
        f"- underperforming: {stats.get('underperforming_count', 0)}件",
        f"- stable: {stats.get('stable_count', 0)}件",
        "",
    ]

    # 大幅変更の詳細
    sig_factors = [f for f in audit.get("factors_updated", [])
                   if abs(f.get("delta", 0)) >= SIGNIFICANT_WEIGHT_DELTA]
    if sig_factors:
        lines.append("### 📊 主な重み変更")
        lines.append("")
        for f in sig_factors[:5]:
            lines.append(
                f"- **{f['factor']}**: "
                f"{f['old_weight']:.3f} → {f['new_weight']:.3f} "
                f"（{f['delta']:+.3f}）"
                + (f" — {f['suggestion']}" if f.get("suggestion") else "")
            )
        lines.append("")

    # underperforming
    under = audit.get("underperforming_rules", [])
    if under:
        lines.append(f"### 📉 見直し推奨シグナル: {', '.join(under[:5])}")
        lines.append("")

    return "\n".join(lines)
