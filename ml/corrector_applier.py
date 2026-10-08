# ==============================================================
# ml/corrector_applier.py — ML補正テーブルの日次適用（R5-1 / S7）
# ==============================================================
# 【役割】Colab（train_meta.ipynb）がエクスポートした補正テーブル
#   （予測十分位 → composite_score 加減点）を日次バッチで適用する。
# 【設計正本】要件定義書 §R5-1（変更禁止の昇格基準）:
#   OOS rank IC >= 0.02 かつ t >= 2.0 → 補正テーブルをJSONエクスポート。
#   日次適用は ML_META_ENABLED=True の場合のみ。**フラグ変更はオーナー承認が必要**。
#   補正上限 = ±ML_META_MAX_PT（±10pt）。
# 【原則】テーブルの昇格基準を満たさないJSONは適用拒否（サイレント適用しない）。
# 補正テーブルJSON形式（train_meta.ipynb が出力）:
#   {
#     "trained_at": "YYYY-MM-DD", "oos_rank_ic": 0.031, "t_stat": 2.4,
#     "n_folds": 5, "feature_names": [...],
#     "decile_adjustments": {"0": -6.0, "1": -4.0, ..., "9": +6.0},
#     "model_coefs": {...}   # 線形近似の係数（decile算出用・LightGBM本体は持ち込まない）
#   }
# ==============================================================

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from finance_logic import as_number as _as_number  # noqa: E402


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def validate_table(table: dict, promote_ic: float = 0.02,
                   promote_t: float = 2.0, max_pt: float = 10.0) -> tuple:
    """
    補正テーブルが昇格基準を満たすか検証する。
    Returns: (ok: bool, reason: str)
    """
    if not isinstance(table, dict):
        return False, "テーブルがdictでない"
    ic = table.get("oos_rank_ic")
    t = table.get("t_stat")
    adj = table.get("decile_adjustments")
    if not isinstance(adj, dict) or not adj:
        return False, "decile_adjustments なし"
    if ic is None or t is None:
        return False, "oos_rank_ic / t_stat なし"
    if ic < promote_ic:
        return False, f"IC {ic:.4f} < 昇格基準 {promote_ic}"
    if t < promote_t:
        return False, f"t {t:.2f} < 昇格基準 {promote_t}"
    for k, v in adj.items():
        if _as_number(v) is None:
            return False, f"decile {k} の補正が数値でない"
        if abs(v) > max_pt:
            return False, f"decile {k} の補正 {v:+.1f}pt が上限 ±{max_pt}pt 超"
    return True, "OK"


def predict_score(row: dict, coefs: dict) -> float:
    """
    線形近似係数で予測値を計算（decile割当のためのランク付け用途のみ）。
    coefs: {"_intercept": b0, "f_<cat>": w, ...}。欠損特徴量は0扱い。
    """
    y = float(coefs.get("_intercept", 0.0))
    for name, w in coefs.items():
        if name == "_intercept":
            continue
        v = _as_number(row.get(name))
        if v is not None:
            y += float(w) * v
    return y


def assign_deciles(preds: dict, n_deciles: int = 10) -> dict:
    """
    {ticker: 予測値} → {ticker: decile(0..n-1)}（0=最低・9=最高）。
    同数の場合は等分割の境界に従う。要素数 < n_deciles でも動作。
    """
    if not preds:
        return {}
    ordered = sorted(preds, key=lambda k: preds[k])
    n = len(ordered)
    out = {}
    for i, tk in enumerate(ordered):
        out[tk] = min(n_deciles - 1, i * n_deciles // n)
    return out


def apply_corrections(candidates: list, deciles: dict, adjustments: dict,
                      max_pt: float = 10.0) -> int:
    """
    screener候補リストへ補正を in-place 適用。
    composite_score += clamp(decile補正, ±max_pt)、0..100へクランプ。
    適用件数を返す。補正値は c["ml_meta_correction"] に記録（監査可能に）。
    """
    applied = 0
    for c in candidates:
        tk = c.get("ticker")
        if tk not in deciles:
            continue
        adj = adjustments.get(str(deciles[tk]))
        adj = _as_number(adj)
        if adj is None or adj == 0:
            continue
        adj = max(-max_pt, min(max_pt, float(adj)))
        old = c.get("composite_score")
        if _as_number(old) is None:
            continue
        c["composite_score"] = round(min(100.0, max(0.0, old + adj)), 1)
        c["ml_meta_correction"] = round(adj, 1)
        applied += 1
    return applied


# ── I/O層 ────────────────────────────────────────────────────

def load_table(cfg) -> dict | None:
    path = Path(getattr(cfg, "ML_META_CORRECTION_PATH",
                        REPO / "data" / "ml_dataset" / "correction_table.json"))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_ml_meta_corrector(cfg, results: dict) -> dict:
    """
    日次バッチ入口（stock_analyzer_main が ML_META_ENABLED=True 時のみ呼ぶ）。
    ①テーブル読込→昇格基準を再検証 ②今日の特徴量行で予測→decile
    ③screener候補の composite_score を補正。
    Returns: {"applied","skipped","reason","table_ic"}
    """
    max_pt = float(getattr(cfg, "ML_META_MAX_PT", 10.0))
    table = load_table(cfg)
    if table is None:
        return {"skipped": True, "applied": 0, "reason": "補正テーブルなし"}
    ok, reason = validate_table(
        table,
        promote_ic=float(getattr(cfg, "ML_META_PROMOTE_IC", 0.02)),
        promote_t=float(getattr(cfg, "ML_META_PROMOTE_T", 2.0)),
        max_pt=max_pt)
    if not ok:
        return {"skipped": True, "applied": 0, "reason": f"昇格基準未達: {reason}"}

    coefs = table.get("model_coefs") or {}
    if not coefs:
        return {"skipped": True, "applied": 0, "reason": "model_coefs なし"}

    # 今日の特徴量（dataset_builder と同じ構築ロジックを共有）
    from ml.dataset_builder import build_feature_rows
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    rows = build_feature_rows(cfg, results, today)
    if not rows:
        return {"skipped": True, "applied": 0, "reason": "特徴量行なし"}

    preds = {r["ticker"]: predict_score(r, coefs) for r in rows}
    deciles = assign_deciles(preds, int(getattr(cfg, "ML_META_N_DECILES", 10)))
    candidates = (results.get("screener") or {}).get("candidates", []) or []
    applied = apply_corrections(candidates, deciles,
                                table.get("decile_adjustments", {}), max_pt)
    return {"skipped": False, "applied": applied,
            "reason": f"decile補正適用 {applied}銘柄",
            "table_ic": table.get("oos_rank_ic")}
