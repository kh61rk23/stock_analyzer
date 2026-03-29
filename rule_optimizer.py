# ==============================================================
# rule_optimizer.py  v1.0  (4-8 / 段階2)
# マクロファクター重みの自動調整
# ==============================================================
# 【役割】
#   「円安が発動した週に輸出業種は実際に上昇したか？」を
#   score_history.json + screener_history.json から自動検証し、
#   MACRO_RULES の各ファクターへの信頼度（重み）を指数移動平均で更新する。
#
# 【更新式（EMA: 指数移動平均）】
#   new_weight = old_weight × 0.9 + correlation × 0.1
#   correlation = -1〜+1（ファクター発動 vs 業種リターンのPearson相関）
#
# 【ファイル構成】
#   保存先: stock_data/data/rule_weights.json（毎月末に更新）
#   内容:
#   {
#     "interest_rate_rise": {
#       "weight":     1.08,      # 現在の信頼度（初期値 1.0）
#       "correlation_history": [...],  # 過去12回の相関値
#       "last_updated": "2026-03-01",
#       "hit_count":  7,         # 発動してから業種が動いた回数
#       "fire_count": 9,         # 発動した回数
#     },
#     ...
#   }
#
# 【composite_score への影響】
#   evaluate_macro_signals() の戻り値に weight が適用され、
#   tailwind/headwind の +10pt/-10pt を重みでスケーリングする:
#     actual_pts = 10 × weight  （weight=1.2 → +12pt）
#
# 【設計原則】
#   - config.py の MACRO_RULES は編集しない（定性的な定義は固定）
#   - 重みだけを rule_weights.json で管理（定量的な更新のみ）
#   - 重みの上下限: 0.3 〜 2.0（暴走防止）
#   - 月末のみ更新（週次では更新しない：ノイズ低減）
#
# 【依存】config.py / score_history.json / screener_history.json
# 【呼出】from rule_optimizer import run_rule_optimizer, load_weights,
#                                    apply_weight_to_signal
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
WEIGHT_FILE        = "rule_weights.json"
WEIGHT_MIN         = 0.3    # 重みの下限（完全無効化しない）
WEIGHT_MAX         = 2.0    # 重みの上限（過大評価防止）
WEIGHT_INIT        = 1.0    # 初期重み
EMA_ALPHA          = 0.1    # EMA の更新率（0.1 = 過去90%・今回10%）
LOOKBACK_WEEKS     = 12     # 相関計算期間（直近12週）
MIN_FIRE_COUNT     = 3      # 最低発動回数（これ未満は更新しない）
CORR_SCALE         = 0.5    # 相関値 → 重み変化のスケール係数
                             # +1.0の相関 → +0.5の重み増加を上限とする

# ファクター発動判定のしきい値（evaluate_macro_signals と整合）
FACTOR_THRESHOLDS = {
    "interest_rate_rise": {"key": "us10y",   "op": ">",  "val": 4.0},
    "interest_rate_fall": {"key": "us10y",   "op": "<=", "val": 4.0},
    "yen_depreciation":   {"key": "usdjpy",  "op": ">",  "val": 140},
    "yen_appreciation":   {"key": "usdjpy",  "op": "<=", "val": 140},
    "oil_price_rise":     {"key": "oil_wti", "op": ">",  "val": 80},
    "us_recession_risk":  {"key": "vix",     "op": ">",  "val": 25},
    "ai_semiconductor_boom": {"key": "sp500", "op": "trend_up", "val": 0},
    "high_inflation":     {"key": "us10y",   "op": ">",  "val": 4.5},
    "geopolitical_risk":  {"key": "vix",     "op": ">",  "val": 28},
}


# ==============================================================
# BLOCK 1: score_history.json 読み込みと週次集計
# ==============================================================

def load_score_history(cfg) -> dict:
    """
    score_history.json を読み込む。

    Returns:
        {"YYYY-MM-DD": {"1001": {"score": float}, ...}, ...}
    """
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
        / "insights" / "score_history.json",
        Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
        / "score_history.json",
        Path("stock_data/insights/score_history.json"),
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _date_to_week(d: str) -> str:
    """YYYY-MM-DD → YYYY-W{ISO週番号}"""
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return dt.strftime("%Y-W%V")
    except ValueError:
        return ""


def calc_weekly_returns(score_history: dict) -> dict:
    """
    score_history から業種ごとの週次リターン（スコア変化率）を計算する。

    Returns:
        {
          "YYYY-W01": {
            "sector_returns": {"1001": 2.5, "1016": -1.2, ...},
            "macro_snapshot": {...},  # その週の末尾スナップショット
          }
        }
    """
    if not score_history:
        return {}

    # 日付昇順でソート
    dates = sorted(score_history.keys())
    # 週ごとにグループ化
    week_dates: dict[str, list] = {}
    for d in dates:
        w = _date_to_week(d)
        if w:
            week_dates.setdefault(w, []).append(d)

    weeks = sorted(week_dates.keys())
    weekly = {}

    for i in range(1, len(weeks)):
        prev_week = weeks[i - 1]
        curr_week = weeks[i]

        prev_dates = week_dates[prev_week]
        curr_dates = week_dates[curr_week]

        # 週末の値（最後の営業日）を使用
        prev_day_data = score_history.get(prev_dates[-1], {})
        curr_day_data = score_history.get(curr_dates[-1], {})

        if not prev_day_data or not curr_day_data:
            continue

        sector_returns = {}
        for code in curr_day_data:
            prev_score = None
            curr_score = None
            if isinstance(prev_day_data.get(code), dict):
                prev_score = prev_day_data[code].get("score")
            elif isinstance(prev_day_data.get(code), (int, float)):
                prev_score = prev_day_data[code]

            if isinstance(curr_day_data.get(code), dict):
                curr_score = curr_day_data[code].get("score")
            elif isinstance(curr_day_data.get(code), (int, float)):
                curr_score = curr_day_data[code]

            if prev_score is not None and curr_score is not None and abs(prev_score) > 0.01:
                sector_returns[code] = round(
                    (curr_score - prev_score) / abs(prev_score) * 100, 3)

        if sector_returns:
            weekly[curr_week] = {
                "sector_returns": sector_returns,
                "date_range":     (prev_dates[-1], curr_dates[-1]),
            }

    return weekly


# ==============================================================
# BLOCK 2: ファクター発動判定
# ==============================================================

def _check_factor_fired(factor: str, macro_snapshot: Optional[dict]) -> bool:
    """
    マクロスナップショットからファクターが発動していたか判定する。
    score_history に macro_snapshot が含まれない場合は False を返す。
    """
    if not macro_snapshot:
        return False
    thresh = FACTOR_THRESHOLDS.get(factor)
    if not thresh:
        return False

    key  = thresh["key"]
    op   = thresh["op"]
    val  = thresh["val"]
    mval = macro_snapshot.get(key)

    if mval is None:
        return False

    try:
        mval_f = float(mval)
        if op == ">":   return mval_f > val
        if op == ">=":  return mval_f >= val
        if op == "<":   return mval_f < val
        if op == "<=":  return mval_f <= val
        if op == "trend_up": return mval_f > 0   # 単純な正値チェック
    except (TypeError, ValueError):
        pass
    return False


# ==============================================================
# BLOCK 3: 相関係数計算（numpy 不使用）
# ==============================================================

def _pearson(xs: list, ys: list) -> Optional[float]:
    """Pearson 相関係数を純粋実装で計算する。"""
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs = xs[:n]; ys = ys[:n]
    mx = sum(xs) / n; my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx  = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy  = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx < 1e-9 or sy < 1e-9:
        return None
    return round(num / (sx * sy), 4)


def calc_factor_correlations(
    cfg,
    weekly_returns: dict,
    score_history:  dict,
    lookback_weeks: int = LOOKBACK_WEEKS,
) -> dict:
    """
    各マクロファクターと業種リターンの相関を計算する。

    Returns:
        {
          "interest_rate_rise": {
            "tailwind_corr":  {"1028": 0.42, "1029": 0.38, ...},
            "headwind_corr":  {"1032": -0.35, ...},
            "avg_tailwind":   0.40,   # 追い風業種の平均相関
            "avg_headwind":   -0.33,  # 逆風業種の平均相関（負が望ましい）
            "overall_score":  0.37,   # 総合的な予測精度
            "fire_count":     8,      # 発動週数
          },
          ...
        }
    """
    macro_rules = getattr(cfg, "MACRO_RULES", {})
    weeks = sorted(weekly_returns.keys())[-lookback_weeks:]

    results = {}

    for factor, thresh in FACTOR_THRESHOLDS.items():
        rule = macro_rules.get(factor, {})
        tailwind_codes = rule.get("signal", {}).get("tailwind", [])
        headwind_codes = rule.get("signal", {}).get("headwind", [])

        if not tailwind_codes and not headwind_codes:
            continue

        # ファクター発動フラグと業種リターンの時系列を構築
        factor_flags = []
        code_returns: dict[str, list] = {}

        for week in weeks:
            wd = weekly_returns[week]
            # macro_snapshot が score_history にあれば参照
            # ない場合はファクターフラグを 0 とする（保守的）
            snap = wd.get("macro_snapshot", {})
            flag = 1.0 if _check_factor_fired(factor, snap) else 0.0
            factor_flags.append(flag)

            for code in tailwind_codes + headwind_codes:
                ret = wd.get("sector_returns", {}).get(code)
                code_returns.setdefault(code, []).append(
                    ret if ret is not None else 0.0)

        fire_count = sum(1 for f in factor_flags if f > 0)
        if fire_count < MIN_FIRE_COUNT:
            continue

        tailwind_corrs = {}
        for code in tailwind_codes:
            rets = code_returns.get(code, [])
            if len(rets) >= 3:
                r = _pearson(factor_flags, rets)
                if r is not None:
                    tailwind_corrs[code] = r

        headwind_corrs = {}
        for code in headwind_codes:
            rets = code_returns.get(code, [])
            if len(rets) >= 3:
                r = _pearson(factor_flags, rets)
                if r is not None:
                    # 逆風業種は負の相関が期待される → 正の値に変換して評価
                    headwind_corrs[code] = r

        avg_tw = (sum(tailwind_corrs.values()) / len(tailwind_corrs)
                  if tailwind_corrs else None)
        avg_hw = (sum(abs(v) for v in headwind_corrs.values()) / len(headwind_corrs)
                  if headwind_corrs else None)   # 絶対値で評価

        # 総合スコア: 追い風相関 + 逆風相関（絶対値）の平均
        parts = [v for v in [avg_tw, avg_hw] if v is not None]
        overall = sum(parts) / len(parts) if parts else 0.0

        results[factor] = {
            "tailwind_corr": tailwind_corrs,
            "headwind_corr": headwind_corrs,
            "avg_tailwind":  round(avg_tw, 4) if avg_tw is not None else None,
            "avg_headwind":  round(avg_hw, 4) if avg_hw is not None else None,
            "overall_score": round(overall, 4),
            "fire_count":    fire_count,
        }

    return results


# ==============================================================
# BLOCK 4: 重みの更新（EMA）
# ==============================================================

def update_weights(
    current_weights: dict,
    factor_correlations: dict,
) -> dict:
    """
    EMA 方式で各ファクターの重みを更新する。

    new_weight = old_weight × 0.9 + overall_score × CORR_SCALE × 0.1 + 0.1
    （相関が高いほど重みが増加、低いほど緩やかに減少）

    Args:
        current_weights:    現在の rule_weights.json の内容
        factor_correlations: calc_factor_correlations() の結果

    Returns:
        更新後の weights dict
    """
    today = datetime.now().strftime("%Y-%m-%d")
    new_weights = {}

    all_factors = set(list(current_weights.keys()) + list(factor_correlations.keys()))

    for factor in all_factors:
        old_entry = current_weights.get(factor, {})
        old_weight = float(old_entry.get("weight", WEIGHT_INIT))
        corr_data  = factor_correlations.get(factor, {})
        overall    = corr_data.get("overall_score", None)
        fire_count = corr_data.get("fire_count", 0)
        hist       = old_entry.get("correlation_history", [])

        if overall is not None and fire_count >= MIN_FIRE_COUNT:
            # EMA 更新
            signal_contribution = overall * CORR_SCALE * EMA_ALPHA
            new_w = old_weight * (1 - EMA_ALPHA) + signal_contribution + EMA_ALPHA * WEIGHT_INIT
            new_w = round(max(WEIGHT_MIN, min(WEIGHT_MAX, new_w)), 4)
            hist  = (hist + [round(overall, 4)])[-12:]   # 直近12回を保持
        else:
            # データ不足 → 重みを初期値方向に緩やかに回帰
            new_w = round(old_weight * 0.99 + WEIGHT_INIT * 0.01, 4)
            new_w = max(WEIGHT_MIN, min(WEIGHT_MAX, new_w))

        new_weights[factor] = {
            "weight":               new_w,
            "prev_weight":          round(old_weight, 4),
            "delta":                round(new_w - old_weight, 4),
            "overall_score":        round(overall, 4) if overall else None,
            "correlation_history":  hist,
            "fire_count":           fire_count,
            "last_updated":         today,
        }

    return new_weights


# ==============================================================
# BLOCK 5: weights.json 保存・読み込み
# ==============================================================

def _weights_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / WEIGHT_FILE

def load_weights(cfg) -> dict:
    """rule_weights.json を読み込む。なければ空辞書を返す。"""
    try:
        p = _weights_path(cfg)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def save_weights(cfg, weights: dict) -> None:
    try:
        p = _weights_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(weights, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  rule_weights.json 保存失敗: {e}")


def apply_weight_to_signal(factor: str, base_pts: float, weights: dict) -> float:
    """
    evaluate_macro_signals() から呼び出す。
    ファクターの重みを適用してスコア加算値をスケーリングする。

    Args:
        factor:   "interest_rate_rise" 等
        base_pts: 通常の加算点（通常 10.0）
        weights:  load_weights() の戻り値

    Returns:
        float: 重み適用後の加算点
    """
    w = weights.get(factor, {}).get("weight", WEIGHT_INIT) if weights else WEIGHT_INIT
    return round(base_pts * w, 2)


# ==============================================================
# BLOCK 6: レポート生成
# ==============================================================

def format_weight_report(old_weights: dict, new_weights: dict) -> str:
    """週次レポート向けの重み更新サマリーを生成する。"""
    lines = [
        "## ⚖️ ルール重み自動更新レポート",
        "",
        "| ファクター | 旧重み | 新重み | 変化 | 相関スコア |",
        "|-----------|-------|-------|------|-----------|",
    ]

    for factor, entry in sorted(new_weights.items(),
                                  key=lambda x: abs(x[1].get("delta", 0)),
                                  reverse=True):
        old_w   = entry.get("prev_weight", 1.0)
        new_w   = entry.get("weight", 1.0)
        delta   = entry.get("delta", 0)
        overall = entry.get("overall_score")
        sign    = "🔺" if delta > 0.01 else ("🔻" if delta < -0.01 else "➡️")
        corr_s  = f"{overall:.3f}" if overall is not None else "N/A"
        lines.append(
            f"| {factor[:22]} | {old_w:.3f} | {new_w:.3f} "
            f"| {sign}{delta:+.3f} | {corr_s} |"
        )

    lines += [
        "",
        "> 重みは毎月末に更新されます。",
        "> 更新式: new_weight = old × 0.9 + correlation × 0.05 + 0.1",
    ]
    return "\n".join(lines)


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_rule_optimizer(cfg, force: bool = False) -> dict:
    """
    月末（または force=True）のときにルール重みを更新する。

    stock_analyzer_main.py から週次で呼び出す:
        _ro = _load_local_module("rule_optimizer", ...)
        ro_result = _ro.run_rule_optimizer(cfg)
        results["rule_optimizer"] = ro_result

    Returns:
        {
          "updated":      bool,      # 今回更新したか
          "weights":      dict,      # 新しい重み
          "old_weights":  dict,      # 更新前の重み
          "report_md":    str,       # Markdown レポート
          "timestamp":    str,
        }
    """
    today = date.today()
    is_month_end = (today == (today.replace(day=1) + timedelta(days=32)).replace(day=1)
                    - timedelta(days=1))

    if not force and not is_month_end:
        old_weights = load_weights(cfg)
        print("  ⏩ ルールオプティマイザー: 月末以外はスキップ")
        return {
            "updated":  False,
            "weights":  old_weights,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    print("\n  ⚖️  ルール重み最適化開始...")

    # score_history 読み込み
    score_history = load_score_history(cfg)
    if len(score_history) < LOOKBACK_WEEKS:
        print(f"    ⚠️  score_history が不足（{len(score_history)}週分 < {LOOKBACK_WEEKS}週）")
        print("    ⏩ スキップ（データ蓄積中）")
        return {
            "updated":  False,
            "weights":  load_weights(cfg),
            "reason":   f"score_history不足: {len(score_history)}週分",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # 週次リターン計算
    weekly_returns = calc_weekly_returns(score_history)
    print(f"    週次リターン計算完了: {len(weekly_returns)}週分")

    # ファクター×業種 相関計算
    factor_correlations = calc_factor_correlations(cfg, weekly_returns, score_history)
    print(f"    ファクター相関計算完了: {len(factor_correlations)}ファクター分析")

    for factor, data in sorted(factor_correlations.items(),
                                key=lambda x: x[1].get("overall_score", 0),
                                reverse=True)[:5]:
        print(f"      {factor[:25]}: overall={data.get('overall_score',0):.3f} "
              f"fires={data.get('fire_count',0)}")

    # 重み更新
    old_weights = load_weights(cfg)
    new_weights = update_weights(old_weights, factor_correlations)

    # 変化が大きいファクターを表示
    changed = [(f, d) for f, d in new_weights.items() if abs(d.get("delta", 0)) > 0.01]
    if changed:
        print(f"    重み更新: {len(changed)}ファクター変化あり")
        for factor, d in sorted(changed, key=lambda x: abs(x[1].get("delta", 0)),
                                  reverse=True)[:5]:
            print(f"      {factor[:25]}: {d.get('prev_weight',1):.3f} → "
                  f"{d.get('weight',1):.3f} ({d.get('delta',0):+.3f})")
    else:
        print("    重み更新: 有意な変化なし（安定稼働中）")

    save_weights(cfg, new_weights)
    report_md = format_weight_report(old_weights, new_weights)

    print(f"    ✅ ルール最適化完了: rule_weights.json 更新")

    return {
        "updated":     True,
        "weights":     new_weights,
        "old_weights": old_weights,
        "factor_correlations": factor_correlations,
        "report_md":   report_md,
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
