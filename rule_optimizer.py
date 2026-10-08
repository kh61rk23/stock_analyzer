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

import finance_logic
from config_sync import sync_module_constants

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
# [統合エンジン] signal_aggregator の total_signal_score 用重みのデフォルト値
# これは MACRO_RULES ファクターとは独立して管理する
DEFAULT_SIGNAL_WEIGHTS: dict = {
    "macro_phase_weight":      0.20,
    "sector_composite_weight": 0.20,
    "momentum_weight":         0.15,
    "earnings_accel_weight":   0.15,
    "quality_weight":          0.10,
    "technical_weight":        0.10,
    "activist_weight":         0.10,
}
# signal_weight の更新レート（MACRO_RULESより保守的にする）
SIGNAL_WEIGHT_EMA_ALPHA = 0.05   # 月次更新・変化を緩やかに
SIGNAL_WEIGHT_MIN       = 0.05
SIGNAL_WEIGHT_MAX       = 0.40

# ── [FE-F / S4-F] ファクターモメンタム定数 ─────────────
# FACTOR_WEIGHTS_FILE : ファクター重みの保存ファイル名
# FACTOR_WEIGHT_EMA   : ファクター重み EMA 更新率（週次）
# FACTOR_LOOKBACK_DEF : ファクターリターン計算のデフォルトルックバック週数
FACTOR_WEIGHTS_FILE = "factor_weights.json"
FACTOR_WEIGHT_EMA   = 0.10   # 週次更新・MACRO_RULES と同じ更新率
FACTOR_LOOKBACK_DEF = 12     # デフォルトルックバック期間（12週）

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

# config.py BLOCK 59 の値を実行時に取り込む。閾値の参照箇所は変更しない。
_CFG_CONST_MAP = {
    "RO_WEIGHT_MIN": "WEIGHT_MIN",
    "RO_WEIGHT_MAX": "WEIGHT_MAX",
    "RO_WEIGHT_INIT": "WEIGHT_INIT",
    "RO_DEFAULT_SIGNAL_WEIGHTS": "DEFAULT_SIGNAL_WEIGHTS",
    "RO_SIGNAL_WEIGHT_EMA_ALPHA": "SIGNAL_WEIGHT_EMA_ALPHA",
    "RO_SIGNAL_WEIGHT_MIN": "SIGNAL_WEIGHT_MIN",
    "RO_SIGNAL_WEIGHT_MAX": "SIGNAL_WEIGHT_MAX",
    "RO_FACTOR_WEIGHT_EMA": "FACTOR_WEIGHT_EMA",
    'B4_RULE_OPTIMIZER_LOOKBACK_WEEKS': 'LOOKBACK_WEEKS',
    'B4_RULE_OPTIMIZER_FACTOR_LOOKBACK_DEF': 'FACTOR_LOOKBACK_DEF',
    'B6_RULE_OPTIMIZER_EMA_ALPHA': 'EMA_ALPHA',
    'B6_RULE_OPTIMIZER_MIN_FIRE_COUNT': 'MIN_FIRE_COUNT',
    'B6_RULE_OPTIMIZER_CORR_SCALE': 'CORR_SCALE',
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
    """Pearson 相関係数を純粋実装で計算する。共通実装は finance_logic.pearson（U-7）。"""
    return finance_logic.pearson(xs, ys, min_n=3, round_digits=4)


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


def update_signal_weights(cfg, factor_correlations: dict) -> dict:
    """
    [統合エンジン] signal_aggregator の total_signal_score 用重みを更新する。

    MACRO_RULES の重み（update_weights）とは独立して管理する。
    rule_weights.json の "_signal_weights" キーに格納する。

    更新ロジック:
      各 signal_weight は MACRO_RULES のうち関連ファクターの
      overall_score の加重平均でEMA更新する。
      データ不足時はデフォルト値に緩やかに回帰する。

    Args:
        cfg                 : config モジュール
        factor_correlations : calc_factor_correlations() の戻り値

    Returns:
        更新後の signal_weights dict（DEFAULT_SIGNAL_WEIGHTS と同形式）
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # 現在の signal_weights を rule_weights.json から読み込む
    all_weights = load_weights(cfg)
    current_sw  = all_weights.get("_signal_weights", {})

    # デフォルト値のコピーを初期値として使用
    new_sw = dict(DEFAULT_SIGNAL_WEIGHTS)

    # MACRO_RULES の相関スコアと signal_weight の対応マッピング
    # 「どのファクター群が高い相関を示したか」で signal_weight を調整
    SIGNAL_FACTOR_MAP = {
        "macro_phase_weight":      ["interest_rate_rise", "interest_rate_fall",
                                    "yen_depreciation", "yen_appreciation"],
        "sector_composite_weight": ["oil_price_rise", "high_inflation", "geopolitical_risk"],
        "momentum_weight":         ["ai_semiconductor_boom", "yen_depreciation"],
        "earnings_accel_weight":   ["ai_semiconductor_boom"],
        "quality_weight":          ["interest_rate_rise", "yen_appreciation"],
        "technical_weight":        ["us_recession_risk", "geopolitical_risk"],
        "activist_weight":         ["interest_rate_rise", "yen_depreciation"],
    }

    for sw_key, related_factors in SIGNAL_FACTOR_MAP.items():
        old_w = float(current_sw.get(sw_key, DEFAULT_SIGNAL_WEIGHTS.get(sw_key, 0.10)))
        default_w = DEFAULT_SIGNAL_WEIGHTS.get(sw_key, 0.10)

        # 関連ファクターの overall_score 平均を計算
        scores = [
            factor_correlations[f]["overall_score"]
            for f in related_factors
            if f in factor_correlations and factor_correlations[f].get("fire_count", 0) >= MIN_FIRE_COUNT
        ]

        if scores:
            avg_score = sum(scores) / len(scores)
            # EMA 更新（保守的なアルファ）
            new_w = old_w * (1 - SIGNAL_WEIGHT_EMA_ALPHA) + avg_score * CORR_SCALE * SIGNAL_WEIGHT_EMA_ALPHA + SIGNAL_WEIGHT_EMA_ALPHA * default_w
        else:
            # データ不足 → デフォルトに緩やかに回帰
            new_w = old_w * 0.99 + default_w * 0.01

        new_sw[sw_key] = round(max(SIGNAL_WEIGHT_MIN, min(SIGNAL_WEIGHT_MAX, new_w)), 4)

    # 全重みの合計が1.0になるよう正規化
    total = sum(new_sw.values())
    if total > 0:
        new_sw = {k: round(v / total, 4) for k, v in new_sw.items()}

    new_sw["_updated_at"] = today
    return new_sw


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
# BLOCK 5.5: ファクターモメンタム（FE-F / S4-F）
# ==============================================================
# F-2: 週次ファクターリターン計算（4ファクター対応）
# F-3: ルックバック期間加重平均
# F-4: HHI 上限管理
# F-5: HMM レジーム連動縮退

def _load_factor_weights(cfg) -> dict:
    """
    factor_weights.json を読み込む。
    なければ config.FACTOR_WEIGHTS_DEFAULT を初期値として返す。

    Returns:
        {factor_name: {"weight": float, "return_history": [...], ...}}
    """
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    p = data_dir / FACTOR_WEIGHTS_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 初期値: config.FACTOR_WEIGHTS_DEFAULT
    defaults = getattr(cfg, "FACTOR_WEIGHTS_DEFAULT", {
        "momentum_12m":      0.30,
        "value_hml":         0.25,
        "low_volatility":    0.20,
        "profitability_rmw": 0.25,
    })
    return {
        name: {
            "weight":         w,
            "return_history": [],
            "last_updated":   "",
        }
        for name, w in defaults.items()
    }


def _save_factor_weights(cfg, fw: dict) -> None:
    """factor_weights.json を保存する。失敗は警告のみ。"""
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    p = data_dir / FACTOR_WEIGHTS_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(fw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [factor_weights] 保存失敗: {str(e)[:60]}")


def calc_weekly_factor_returns(
    score_history: dict,
    cfg,
    n_weeks: int = FACTOR_LOOKBACK_DEF,
) -> dict[str, list[float]]:
    """
    F-2: score_history から4ファクターの週次リターンを計算する。

    各ファクターの計算:
      momentum_12m     : 全業種スコアを昇順ソートし上位1/3 - 下位1/3の平均差
                         $r_{mom} = mean(top\\_third) - mean(bottom\\_third)$
      value_hml        : PBR逆数（スコアをプロキシ）で同様に計算
                         ※ score_history に value_score がある場合はそれを使用
      low_volatility   : スコアの業種間標準偏差の逆数（低分散 = 低ボラ優位）
                         $r_{lowvol} = -std(weekly\\_score\\_changes)$
      profitability_rmw: edinet_credit_bonus / bm_bonus を収益性プロキシとして使用
                         データなし時は 0.0

    Returns:
        {factor_name: [weekly_return, ...]}  直近 n_weeks 件（昇順）
    """
    factor_returns: dict[str, list[float]] = {
        "momentum_12m":      [],
        "value_hml":         [],
        "low_volatility":    [],
        "profitability_rmw": [],
    }

    # 週次スコアを昇順ソートで抽出
    all_dates = sorted(score_history.keys())
    if len(all_dates) < 2:
        return factor_returns

    # 7日ごとにサンプリング
    weekly_snapshots: list[dict] = []
    seen_weeks: set = set()
    for d in reversed(all_dates):
        try:
            dt       = datetime.strptime(d, "%Y-%m-%d")
            week_key = f"{dt.isocalendar()[0]}W{dt.isocalendar()[1]}"
        except Exception:
            continue
        if week_key in seen_weeks:
            continue
        seen_weeks.add(week_key)
        weekly_snapshots.append({"date": d, "data": score_history[d]})
        if len(weekly_snapshots) >= n_weeks + 1:
            break

    # 昇順に戻す
    weekly_snapshots.sort(key=lambda x: x["date"])
    if len(weekly_snapshots) < 2:
        return factor_returns

    for i in range(1, len(weekly_snapshots)):
        prev_snap = weekly_snapshots[i - 1]["data"]
        curr_snap = weekly_snapshots[i]["data"]

        if not isinstance(prev_snap, dict) or not isinstance(curr_snap, dict):
            continue

        # 両スナップショットに共通する業種コードのスコアを取得
        codes = set(prev_snap.keys()) & set(curr_snap.keys())
        if len(codes) < 4:
            continue

        prev_scores = {}
        curr_scores = {}
        for code in codes:
            p_entry = prev_snap[code]
            c_entry = curr_snap[code]
            try:
                p_s = float(p_entry["score"] if isinstance(p_entry, dict)
                            else p_entry)
                c_s = float(c_entry["score"] if isinstance(c_entry, dict)
                            else c_entry)
                prev_scores[code] = p_s
                curr_scores[code] = c_s
            except (TypeError, ValueError):
                pass

        if len(prev_scores) < 4:
            continue

        # 週次変化率
        changes = {
            code: curr_scores[code] - prev_scores[code]
            for code in prev_scores
        }
        sorted_by_prev = sorted(prev_scores.items(), key=lambda x: x[1])
        n = len(sorted_by_prev)
        third = max(1, n // 3)

        # F-2a: momentum_12m (上位1/3 - 下位1/3の変化差)
        top_codes    = [c for c, _ in sorted_by_prev[-third:]]
        bottom_codes = [c for c, _ in sorted_by_prev[:third]]
        top_ret    = sum(changes.get(c, 0) for c in top_codes)    / len(top_codes)
        bottom_ret = sum(changes.get(c, 0) for c in bottom_codes) / len(bottom_codes)
        factor_returns["momentum_12m"].append(round(top_ret - bottom_ret, 4))

        # F-2b: value_hml (low_score = 割安 → 上昇期待)
        # prev_score が低い業種ほど「割安」とみなし、上昇が大きければ有効
        value_top    = [c for c, _ in sorted_by_prev[:third]]   # 低スコア=割安
        value_bottom = [c for c, _ in sorted_by_prev[-third:]]  # 高スコア=割高
        v_top_ret    = sum(changes.get(c, 0) for c in value_top)    / len(value_top)
        v_bottom_ret = sum(changes.get(c, 0) for c in value_bottom) / len(value_bottom)
        factor_returns["value_hml"].append(round(v_top_ret - v_bottom_ret, 4))

        # F-2c: low_volatility (変化の標準偏差の逆符号)
        # 変化のばらつきが小さい週 = 低ボラ環境 = low_vol ファクターに有利
        all_changes = list(changes.values())
        mean_chg    = sum(all_changes) / len(all_changes)
        var_chg     = sum((c - mean_chg) ** 2 for c in all_changes) / len(all_changes)
        std_chg     = math.sqrt(var_chg) if var_chg > 0 else 0.0
        # std が小さい = 低ボラ = +リターン、std が大きい = -リターン
        lowvol_ret  = -std_chg   # 低ボラ週ほど正のリターン
        factor_returns["low_volatility"].append(round(lowvol_ret, 4))

        # F-2d: profitability_rmw (edinet_credit_bonus をプロキシ)
        # curr_snap に edinet_credit_bonus がある業種の平均変化
        rmw_values = []
        for code in codes:
            c_entry = curr_snap.get(code, {})
            if isinstance(c_entry, dict):
                bonus = c_entry.get("edinet_credit_bonus")
                if bonus is not None:
                    rmw_values.append(
                        (float(bonus), changes.get(code, 0))
                    )
        if rmw_values:
            high_rmw = sorted(rmw_values, key=lambda x: x[0], reverse=True)
            top_rmw  = [r for _, r in high_rmw[:third]]
            bot_rmw  = [r for _, r in high_rmw[-third:]]
            if top_rmw and bot_rmw:
                rmw_ret = (sum(top_rmw) / len(top_rmw)
                           - sum(bot_rmw) / len(bot_rmw))
                factor_returns["profitability_rmw"].append(round(rmw_ret, 4))
            else:
                factor_returns["profitability_rmw"].append(0.0)
        else:
            factor_returns["profitability_rmw"].append(0.0)

    return factor_returns


def _calc_lookback_weighted_return(
    returns: list[float],
    mu: float = None,
) -> float:
    """
    F-3: ルックバック期間加重平均リターンを計算する。

    $w_t = exp(-\\mu \times (T - t))$
    直近ほど高い重みを付与する指数減衰。

    Args:
        returns: 週次リターンリスト（昇順）
        mu:      減衰係数（None のとき FACTOR_LOOKBACK_DECAY_MU を使用）

    Returns:
        float: 加重平均リターン
    """
    if not returns:
        return 0.0

    if mu is None:
        mu = 0.05   # FACTOR_LOOKBACK_DECAY_MU のデフォルト値

    n       = len(returns)
    weights = [math.exp(-mu * (n - 1 - i)) for i in range(n)]
    total_w = sum(weights)
    if total_w == 0:
        return 0.0

    return round(
        sum(w * r for w, r in zip(weights, returns)) / total_w, 4
    )


def _calc_factor_hhi(weights: dict[str, float]) -> float:
    """
    F-4: ファクター重みの集中度 HHI を計算する。

    $HHI = \\sum_{i} w_i^2$

    Returns:
        float: HHI [0.0, 1.0]
    """
    total = sum(weights.values())
    if total == 0:
        return 0.0
    normed = {k: v / total for k, v in weights.items()}
    return round(sum(v ** 2 for v in normed.values()), 4)


def _apply_hhi_rebalance(
    weights:  dict[str, float],
    hhi_max:  float,
    max_iter: int = 10,
) -> dict[str, float]:
    """
    F-4: HHI が上限を超えた場合、均等化方向にリバランスする。

    $w_i' = w_i \times (1 - \alpha) + uniform \times \alpha$
    $\alpha = 0.2$ (1回あたり20%均等化)

    Returns:
        dict: リバランス後の重み
    """
    for _ in range(max_iter):
        hhi = _calc_factor_hhi(weights)
        if hhi <= hhi_max:
            break
        n       = len(weights)
        uniform = 1.0 / n if n > 0 else 0.0
        weights = {
            k: round(v * 0.8 + uniform * 0.2, 4)
            for k, v in weights.items()
        }
    return weights


def _apply_regime_shrink(
    weights: dict[str, float],
    regime:  str,
    cfg,
) -> dict[str, float]:
    """
    F-5: HMM レジームに応じてファクター重みを縮退させる。

    FACTOR_REGIME_SHRINK で定義した係数を適用:
      bull/normal: 変更なし
      bear:        momentum_12m を 0.70 に縮退
      crisis:      momentum_12m を 0.40 に縮退（低ボラに移行）

    Returns:
        dict: 縮退後の重み（正規化済み）
    """
    regime_shrink = getattr(cfg, "FACTOR_REGIME_SHRINK", {
        "bull":   1.00,
        "normal": 1.00,
        "bear":   0.70,
        "crisis": 0.40,
    })

    shrink = regime_shrink.get(regime, 1.00)
    if shrink == 1.00:
        return weights

    result = dict(weights)
    if "momentum_12m" in result:
        result["momentum_12m"] = round(result["momentum_12m"] * shrink, 4)
        # 縮退した分を low_volatility に振り替え（低ボラへの移行）
        freed = weights.get("momentum_12m", 0.0) * (1.0 - shrink)
        if "low_volatility" in result:
            result["low_volatility"] = round(
                result["low_volatility"] + freed, 4
            )

    # 正規化
    total = sum(result.values())
    if total > 0:
        result = {k: round(v / total, 4) for k, v in result.items()}

    return result


def run_factor_momentum_optimizer(cfg, score_history: dict) -> dict:
    """
    F-2〜F-5 を統合した週次ファクターリターン計算・重み更新のメイン関数。

    stock_analyzer_main.py の run_rule_optimizer 呼び出し後に
    続けて呼び出される（または run_rule_optimizer 内部から呼び出す）。

    Args:
        cfg:           config モジュール
        score_history: DATA_DIR/score_history.json の内容

    Returns:
        {
          "factor_weights":      dict,   # 更新後のファクター重み
          "factor_returns":      dict,   # 週次リターン系列
          "weighted_returns":    dict,   # 加重平均リターン
          "hhi_before":          float,
          "hhi_after":           float,
          "regime":              str,
          "regime_applied":      bool,
          "rebalanced":          bool,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("    📊 [F-2〜F-5] ファクターモメンタム最適化...")

    try:
        # ── F-2: 週次ファクターリターン計算 ─────────────────────
        factor_returns = calc_weekly_factor_returns(score_history, cfg)
        n_rets = {k: len(v) for k, v in factor_returns.items()}
        print(f"      週次リターン: {n_rets}")

        # ── F-3: ルックバック加重平均 ─────────────────────────
        mu = float(getattr(cfg, "FACTOR_LOOKBACK_DECAY_MU", 0.05))
        weighted_returns = {
            fname: _calc_lookback_weighted_return(returns, mu)
            for fname, returns in factor_returns.items()
        }
        print(f"      加重平均: "
              f"{', '.join(f'{k}={v:+.4f}' for k,v in weighted_returns.items())}")

        # ── ファクター重み読み込み ─────────────────────────────
        fw = _load_factor_weights(cfg)

        # EMA 更新: 加重平均リターンを使って重みを更新
        hhi_max   = float(getattr(cfg, "FACTOR_HHI_MAX", 0.35))
        curr_weights: dict[str, float] = {}

        for fname, fw_data in fw.items():
            old_w = float(fw_data.get("weight", 0.25))
            w_ret = weighted_returns.get(fname, 0.0)
            # リターンが正 → 重みを増加、負 → 重みを減少
            new_w = round(
                max(0.05, min(0.60,
                    old_w * (1 - FACTOR_WEIGHT_EMA)
                    + (old_w + w_ret * CORR_SCALE) * FACTOR_WEIGHT_EMA
                )), 4
            )
            fw[fname]["weight"]       = new_w
            fw[fname]["prev_weight"]  = old_w
            fw[fname]["delta"]        = round(new_w - old_w, 4)
            fw[fname]["weighted_return"] = w_ret
            # リターン履歴を追記（最大52週保持）
            hist = fw[fname].get("return_history", [])
            hist.append({
                "date":   datetime.now().strftime("%Y-%m-%d"),
                "return": w_ret,
            })
            fw[fname]["return_history"] = hist[-52:]
            fw[fname]["last_updated"]   = datetime.now().strftime("%Y-%m-%d")
            curr_weights[fname] = new_w

        # ── F-4: HHI 上限管理 ────────────────────────────────
        hhi_before = _calc_factor_hhi(curr_weights)
        rebalanced = False
        if hhi_before > hhi_max:
            print(
                f"      ⚠️  HHI={hhi_before:.4f} > {hhi_max} → リバランス実行"
            )
            curr_weights = _apply_hhi_rebalance(curr_weights, hhi_max)
            for fname in curr_weights:
                if fname in fw:
                    fw[fname]["weight"]      = curr_weights[fname]
                    fw[fname]["hhi_rebalanced"] = True
            rebalanced = True
        hhi_after = _calc_factor_hhi(curr_weights)

        # ── F-5: HMM レジーム連動縮退 ───────────────────────
        regime         = "normal"
        regime_applied = False
        try:
            import sys as _sys, importlib as _imp, pathlib as _pl
            _sys.path.insert(0, str(_pl.Path(__file__).parent))
            _hmm = _imp.import_module("hmm_regime_detector")
            if hasattr(_hmm, "get_current_regime"):
                regime = _hmm.get_current_regime(cfg) or "normal"
        except Exception:
            pass   # HMM 未配置時はデフォルトレジームを使用

        if regime in ("bear", "crisis"):
            print(f"      ⚡ HMMレジーム={regime} → momentum縮退適用")
            curr_weights = _apply_regime_shrink(curr_weights, regime, cfg)
            for fname in curr_weights:
                if fname in fw:
                    fw[fname]["weight"]         = curr_weights[fname]
                    fw[fname]["regime_applied"]  = regime
            regime_applied = True

        # ── 保存 ─────────────────────────────────────────────
        _save_factor_weights(cfg, fw)

        result = {
            "factor_weights":   curr_weights,
            "factor_returns":   {k: v[-12:] for k, v in factor_returns.items()},
            "weighted_returns": weighted_returns,
            "hhi_before":       hhi_before,
            "hhi_after":        hhi_after,
            "regime":           regime,
            "regime_applied":   regime_applied,
            "rebalanced":       rebalanced,
        }

        print(
            f"      ✅ ファクター重み更新完了: "
            f"HHI {hhi_before:.3f}→{hhi_after:.3f} "
            f"regime={regime} "
            f"rebalanced={rebalanced}"
        )
        return result

    except Exception as e:
        print(f"    ⚠️  [factor_momentum] エラー（スキップ）: {str(e)[:80]}")
        return {
            "factor_weights":   {},
            "factor_returns":   {},
            "weighted_returns": {},
            "hhi_before":       0.0,
            "hhi_after":        0.0,
            "regime":           "normal",
            "regime_applied":   False,
            "rebalanced":       False,
        }

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
        if factor.startswith("_"):
            continue
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

# ==============================================================
# BLOCK 7.5: 緊急縮退フラグ判定・防御セクター相対維持ロジック
# ==============================================================
# 【設計方針】
#   G-1: evaluate_contraction_flag()  → results から複合条件を評価してモード判定
#   G-2: apply_contraction_to_scores() → カテゴリスコアに縮退係数を一括適用
#   G-3: is_defensive_sector()         → JP/US 両市場で防御セクターか判定
#   G-4: apply_defensive_protection()  → 防御セクターのスコアを相対維持
#   G-5: save_contraction_history()    → 縮退状態を JSONL で永続化
#   ※ config.py BLOCK 27 の定数群を参照する。
# ==============================================================


def evaluate_contraction_flag(results: dict, cfg) -> dict:
    """
    G-1: リスクオフ局面の複合条件を評価し、縮退モードを判定する。

    判定ロジック:
      各条件が成立するたびに triggered_count をインクリメント。
      CONTRACTION_WARNING_TRIGGER_COUNT 以上  → "Warning"
      CONTRACTION_EMERGENCY_TRIGGER_COUNT 以上 → "Emergency"
      それ未満                                → "Normal"

    Args:
        results: run_all_tasks() の results dict
        cfg:     config モジュール

    Returns:
        {
          "mode":            str,   # "Normal" / "Warning" / "Emergency"
          "triggered_count": int,   # 成立条件数
          "triggered_flags": list,  # 成立した条件名リスト
          "detail":          dict,  # 各条件の評価値
          "coefficient":     float, # モード別・縮退係数（0.65〜1.00）
          "timestamp":       str,
        }
    """
    triggered_count = 0
    triggered_flags: list = []
    detail: dict = {}

    # ── 閾値を config から取得（BLOCK 27 定数） ─────────────────
    vix_thresh     = float(getattr(cfg, "CONTRACTION_VIX_THRESHOLD",       30.0))
    us10y_rise     = float(getattr(cfg, "CONTRACTION_US10Y_RISE_PCT",       0.30))
    breadth_min    = float(getattr(cfg, "CONTRACTION_BREADTH_SCORE_MIN",    35.0))
    pcr_thresh     = float(getattr(cfg, "CONTRACTION_PCR_THRESHOLD",        1.40))
    geo_thresh     = float(getattr(cfg, "CONTRACTION_GEO_RISK_SCORE",       70.0))
    score_drop     = float(getattr(cfg, "CONTRACTION_SCORE_DROP_THRESHOLD", 12.0))
    warn_count     = int(getattr(cfg,   "CONTRACTION_WARNING_TRIGGER_COUNT",   2))
    emerg_count    = int(getattr(cfg,   "CONTRACTION_EMERGENCY_TRIGGER_COUNT", 4))

    # ── 条件 1: VIX >= vix_thresh ────────────────────────────────
    try:
        vix_val = (
            results.get("options_monitor", {})
            .get("vix_data", {})
            .get("vix", {})
            .get("current")
        )
        detail["vix"] = vix_val
        if vix_val is not None and float(vix_val) >= vix_thresh:
            triggered_count += 1
            triggered_flags.append(f"VIX={float(vix_val):.1f}>={vix_thresh}")
    except Exception:
        detail["vix"] = None

    # ── 条件 2: 米10年債利回り前週比 >= us10y_rise(%) ────────────
    try:
        bond_res   = results.get("bond_commodity", {})
        us10y_now  = bond_res.get("us10y_yield")
        us10y_prev = bond_res.get("us10y_yield_prev")
        detail["us10y_rise"] = None
        # bond_commodity には利回りが入らないため、
        # rates_monitor（FRED DGS10）の前週比（%pt）を使う
        if us10y_now is None or us10y_prev is None:
            try:
                from rates_monitor import get_rates
                _u = (get_rates(cfg).get("us") or {}).get("10年") or {}
                if _u.get("w1") is not None:
                    us10y_now, us10y_prev = _u["value"], _u["value"] - _u["w1"]
            except Exception:
                pass
        if us10y_now is not None and us10y_prev is not None:
            rise = float(us10y_now) - float(us10y_prev)
            detail["us10y_rise"] = round(rise, 4)
            if rise >= us10y_rise:
                triggered_count += 1
                triggered_flags.append(
                    f"US10Y+{rise:.2f}%>={us10y_rise}%"
                )
    except Exception:
        detail["us10y_rise"] = None

    # ── 条件 3: 市場幅スコア <= breadth_min ──────────────────────
    try:
        breadth_score = results.get("market_breadth", {}).get("breadth_score_jp")
        detail["breadth_score_jp"] = breadth_score
        if breadth_score is not None and float(breadth_score) <= breadth_min:
            triggered_count += 1
            triggered_flags.append(
                f"breadth={float(breadth_score):.1f}<={breadth_min}"
            )
    except Exception:
        detail["breadth_score_jp"] = None

    # ── 条件 4: PCR >= pcr_thresh ────────────────────────────────
    try:
        pcr_val = (
            results.get("options_monitor", {})
            .get("pcr_data", {})
            .get("pcr_current")
        )
        # 実際の格納先は option_data.spy.pcr_oi（建玉が0なら出来高ベース）
        if pcr_val is None:
            _spy = (results.get("options_monitor", {}).get("option_data") or {}).get("spy") or {}
            pcr_val = _spy.get("pcr_oi") or _spy.get("pcr_volume")
        detail["pcr"] = pcr_val
        if pcr_val is not None and float(pcr_val) >= pcr_thresh:
            triggered_count += 1
            triggered_flags.append(f"PCR={float(pcr_val):.2f}>={pcr_thresh}")
    except Exception:
        detail["pcr"] = None

    # ── 条件 5: 地政学リスクスコア >= geo_thresh ─────────────────
    try:
        geo_score = None
        # geo_risk / country_risk のいずれかから取得を試みる
        _geo_res = results.get("geo_risk", results.get("country_risk", {}))
        if isinstance(_geo_res, dict):
            geo_score = _geo_res.get("risk_score") or _geo_res.get("geo_risk_score")
            # 実際の結果は国別の risk_scores。監視国のうち最も高い国のスコアを使う
            _rs = _geo_res.get("risk_scores") or {}
            if geo_score is None and isinstance(_rs, dict) and _rs:
                _top = max(_rs.items(), key=lambda kv: float(kv[1] or 0))
                geo_score = round(float(_top[1] or 0), 1)
                detail["geo_risk_country"] = _top[0]
        detail["geo_risk_score"] = geo_score
        if geo_score is not None and float(geo_score) >= geo_thresh:
            triggered_count += 1
            triggered_flags.append(
                f"geo_risk={float(geo_score):.1f}>={geo_thresh}"
            )
    except Exception:
        detail["geo_risk_score"] = None

    # ── 条件 6: 上位業種スコア平均の週次急落 >= score_drop ───────
    try:
        sig_res = results.get("signal_aggregator", {})
        sector_signals = sig_res.get("sector_signals", {})
        scores_now = [
            float(v.get("signal_score", 50))
            for v in sector_signals.values()
            if isinstance(v, dict)
        ]
        # score_history から前週の上位平均を取得（存在すれば）
        # results（実行中のメモリ）は翌週には残らないため、前週の値はファイルに残し
        # 6日以上前の値と比べる
        from pathlib import Path as _P
        _sp = _P(getattr(cfg, "DATA_DIR", "stock_data/data")) / "contraction_state.json"
        try:
            _st = json.loads(_sp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _st = {}
        score_hist = results.get("_score_history_prev_avg")
        if score_hist is None:
            _old = [v for d, v in sorted((_st.get("top_avg") or {}).items())
                    if (datetime.now() - datetime.strptime(d, "%Y-%m-%d")).days >= 6]
            score_hist = _old[-1] if _old else None
        detail["score_drop"] = None
        if scores_now:
            avg_now = sum(sorted(scores_now, reverse=True)[:6]) / min(6, len(scores_now))
            if score_hist is not None:
                drop = float(score_hist) - avg_now
                detail["score_drop"] = round(drop, 2)
                if drop >= score_drop:
                    triggered_count += 1
                    triggered_flags.append(
                        f"score_drop={drop:.1f}>={score_drop}"
                    )
            # 前週平均を results に格納（次週参照用）
            results["_score_history_prev_avg"] = round(avg_now, 2)
            try:
                _hist = dict(_st.get("top_avg") or {})
                _hist[datetime.now().strftime("%Y-%m-%d")] = round(avg_now, 2)
                _st["top_avg"] = dict(sorted(_hist.items())[-60:])
                _sp.parent.mkdir(parents=True, exist_ok=True)
                _sp.write_text(json.dumps(_st, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                pass
    except Exception:
        detail["score_drop"] = None

    # ── モード判定 ───────────────────────────────────────────────
    if triggered_count >= emerg_count:
        mode = "Emergency"
    elif triggered_count >= warn_count:
        mode = "Warning"
    else:
        mode = "Normal"

    coeff_map = getattr(
        cfg,
        "CONTRACTION_MODE_COEFFICIENTS",
        {"Normal": 1.00, "Warning": 0.85, "Emergency": 0.65},
    )
    coefficient = float(coeff_map.get(mode, 1.00))

    return {
        "mode":            mode,
        "triggered_count": triggered_count,
        "triggered_flags": triggered_flags,
        "detail":          detail,
        "coefficient":     coefficient,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def is_defensive_sector(
    sector_code: str,
    sector_name: str = "",
    market: str = "JP",
    cfg=None,
) -> bool:
    """
    G-3: 業種コード / 業種名 / GICS名 から防御セクターか判定する。

    判定優先順:
      1. JP市場: DEFENSIVE_SECTOR_CODES_JP にコードが含まれるか
      2. US市場: DEFENSIVE_GICS_SECTORS_US に名称が含まれるか
      3. 共通: DEFENSIVE_SECTOR_KEYWORDS にキーワードが含まれるか

    Args:
        sector_code: JP33業種コード ("1008") / GICSセクター名 ("Health Care")
        sector_name: sector_name_ja 等の表示名（マッチング補完用）
        market:      "JP" / "US"
        cfg:         config モジュール（None時はデフォルト定義を使用）

    Returns:
        bool: 防御セクターなら True
    """
    # config 定数を取得（cfg なし時はデフォルト）
    jp_codes = (
        getattr(cfg, "DEFENSIVE_SECTOR_CODES_JP", [])
        if cfg is not None
        else [
            "1007", "1008", "1026", "1022",
            "1023", "1024", "1009", "1028", "1029",
        ]
    )
    us_gics = (
        getattr(cfg, "DEFENSIVE_GICS_SECTORS_US", [])
        if cfg is not None
        else [
            "Consumer Staples", "Health Care",
            "Utilities", "Communication Services",
        ]
    )
    keywords = (
        getattr(cfg, "DEFENSIVE_SECTOR_KEYWORDS", [])
        if cfg is not None
        else [
            "医薬品", "電力", "ガス", "食料", "食品", "生活",
            "公益", "通信", "インフラ", "保険",
            "Healthcare", "Utilities", "Staples", "Pharma",
        ]
    )

    code_str = str(sector_code).strip()
    name_str = str(sector_name).strip()

    # 1. JP コードマッチ
    if market == "JP" and code_str in jp_codes:
        return True

    # 2. US GICSマッチ
    if market == "US" and code_str in us_gics:
        return True

    # 3. キーワードマッチ（sector_code または sector_name に含まれる）
    combined = code_str + " " + name_str
    for kw in keywords:
        if kw in combined:
            return True

    return False


def apply_contraction_to_scores(
    scores: dict,
    contraction: dict,
    cfg,
    sector_code: str = "",
    sector_name: str = "",
    market: str = "JP",
) -> dict:
    """
    G-2: カテゴリ別スコア dict に縮退係数を適用する。
    防御セクターの場合は係数を適用せず、相対ボーナスを加算する。

    Args:
        scores:      {"sector_score": 72.0, "macro_signals": 65.0, ...}
        contraction: evaluate_contraction_flag() の戻り値
        cfg:         config モジュール
        sector_code: 業種コード（防御判定に使用）
        sector_name: 業種名（防御判定に使用）
        market:      "JP" / "US"

    Returns:
        補正後の scores dict（元の dict は変更しない）
    """
    mode        = contraction.get("mode", "Normal")
    coefficient = contraction.get("coefficient", 1.00)

    # Normal ならスコアを変更しない
    if mode == "Normal":
        return dict(scores)

    # 防御セクターか判定
    defensive = is_defensive_sector(
        sector_code=sector_code,
        sector_name=sector_name,
        market=market,
        cfg=cfg,
    )

    result = {}

    if defensive:
        # 防御セクター: 係数を適用せず相対ボーナスを加算
        bonus_map = {
            "Warning":   float(getattr(cfg, "DEFENSIVE_RELATIVE_BONUS_WARNING",   3.0)),
            "Emergency": float(getattr(cfg, "DEFENSIVE_RELATIVE_BONUS_EMERGENCY", 7.0)),
        }
        bonus = bonus_map.get(mode, 0.0)
        for cat, val in scores.items():
            if val is None:
                result[cat] = val
                continue
            try:
                result[cat] = round(min(100.0, float(val) + bonus), 2)
            except (TypeError, ValueError):
                result[cat] = val
        return result

    # 非防御セクター: カテゴリ別感度係数 × モード係数を適用
    cat_sensitivity = getattr(cfg, "CONTRACTION_CATEGORY_SENSITIVITY", {})

    for cat, val in scores.items():
        if val is None:
            result[cat] = val
            continue
        try:
            sensitivity = float(cat_sensitivity.get(cat, 1.00))
            # 有効係数 = coefficient ^ sensitivity（感度が高いほどより強く縮退）
            # 簡易実装: coefficient × (1.0 - (sensitivity - 1.0) × 0.5)
            effective_coeff = coefficient * (
                1.0 - (sensitivity - 1.0) * 0.5
            )
            effective_coeff = max(0.30, min(1.00, effective_coeff))
            # スコアは中心50点から縮退させる
            # $adjusted = 50 + (val - 50) \times effective\_coeff$
            adjusted = 50.0 + (float(val) - 50.0) * effective_coeff
            result[cat] = round(max(0.0, min(100.0, adjusted)), 2)
        except (TypeError, ValueError):
            result[cat] = val

    return result


def apply_defensive_protection(
    signal_score: float,
    contraction: dict,
    cfg,
    sector_code: str = "",
    sector_name: str = "",
    market: str = "JP",
) -> float:
    """
    G-4: 最終 signal_score に対して防御セクター保護を適用する。
    apply_contraction_to_scores() でカテゴリ単位に補正済みの場合でも、
    最終スコアの下限保護として使用できる。

    防御セクター かつ Emergency 時は
    DEFENSIVE_PROTECTION_MULTIPLIER でスコアを保護する。

    Args:
        signal_score: 縮退補正前の signal_score（0〜100）
        contraction:  evaluate_contraction_flag() の戻り値
        cfg:          config モジュール

    Returns:
        float: 保護適用後の signal_score
    """
    mode = contraction.get("mode", "Normal")
    if mode == "Normal":
        return signal_score

    defensive = is_defensive_sector(
        sector_code=sector_code,
        sector_name=sector_name,
        market=market,
        cfg=cfg,
    )
    if not defensive:
        return signal_score

    multiplier = float(
        getattr(cfg, "DEFENSIVE_PROTECTION_MULTIPLIER", 1.00)
    )
    bonus_map = {
        "Warning":   float(getattr(cfg, "DEFENSIVE_RELATIVE_BONUS_WARNING",   3.0)),
        "Emergency": float(getattr(cfg, "DEFENSIVE_RELATIVE_BONUS_EMERGENCY", 7.0)),
    }
    bonus = bonus_map.get(mode, 0.0)
    protected = round(
        min(100.0, signal_score * multiplier + bonus), 1
    )
    return protected


def save_contraction_history(
    cfg,
    contraction: dict,
) -> None:
    """
    G-6: 縮退状態を CACHE_DIR/contraction/contraction_history.jsonl に追記する。
    1行1JSON（JSON Lines形式）。ファイルが存在しない場合は新規作成する。
    書き込み失敗は警告のみ（システム全体を止めない）。
    """
    try:
        cache_dir  = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
        hist_fname = getattr(cfg, "CONTRACTION_HISTORY_PATH", "contraction_history.jsonl")
        hist_path  = cache_dir / "contraction" / hist_fname
        hist_path.parent.mkdir(parents=True, exist_ok=True)

        record = {
            "date":            datetime.now().strftime("%Y-%m-%d"),
            "mode":            contraction.get("mode", "Normal"),
            "triggered_count": contraction.get("triggered_count", 0),
            "triggered_flags": contraction.get("triggered_flags", []),
            "coefficient":     contraction.get("coefficient", 1.00),
        }
        with hist_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"    ⚠️  縮退履歴保存失敗（スキップ）: {str(e)[:60]}")

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
          "contraction_flag":   dict,  # 緊急縮退フラグ判定結果
          "contraction_mode":   str,   # "Normal" / "Warning" / "Emergency"
          "contraction_detail": dict,  # 各条件の評価値
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    today = date.today()
    is_month_end = (today == (today.replace(day=1) + timedelta(days=32)).replace(day=1)
                    - timedelta(days=1))

    if not force and not is_month_end:
        old_weights = load_weights(cfg)
        print("  ⏩ ルールオプティマイザー: 月末以外はスキップ")
        return {
            "updated":          False,
            "weights":          old_weights,
            "contraction_flag": {
                "mode": "Normal", "triggered_count": 0,
                "triggered_flags": [], "detail": {}, "coefficient": 1.00,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "contraction_mode":   "Normal",
            "contraction_detail": {},
            "timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    print("\n  ⚖️  ルール重み最適化開始...")

    # score_history 読み込み
    score_history = load_score_history(cfg)
    if len(score_history) < LOOKBACK_WEEKS * 7:
        print(f"    ⚠️  score_history が不足（{len(score_history)}週分 < {LOOKBACK_WEEKS}週）")
        print("    ⏩ スキップ（データ蓄積中）")
        return {
            "updated":            False,
            "weights":            load_weights(cfg),
            "reason":             f"score_history不足: {len(score_history)}週分",
            "contraction_flag": {
                "mode": "Normal", "triggered_count": 0,
                "triggered_flags": [], "detail": {}, "coefficient": 1.00,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "contraction_mode":   "Normal",
            "contraction_detail": {},
            "timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

    # ── [R-3] investment_journal の miss_factor を重みヒントとして適用 ──
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, str(Path(__file__).parent))
        _ij = _imp.import_module("investment_journal")
        hints = _ij.get_miss_factor_weight_hints(cfg)
        if hints:
            print(f"    📔 miss_factor重みヒント: {len(hints)}件")
            for factor_key, adj in hints.items():
                if factor_key in new_weights:
                    old_w = new_weights[factor_key].get("weight", 1.0)
                    new_w = round(max(0.3, min(2.0, old_w + adj)), 4)
                    new_weights[factor_key]["weight"] = new_w
                    new_weights[factor_key]["journal_hint"] = adj
                    print(f"      {factor_key}: {old_w:.3f} → {new_w:.3f} "
                          f"(journal hint: {adj:+.3f})")
    except Exception as _e:
        pass  # journal連携失敗は最適化を止めない

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

    # ── [RESEARCH-2] クラウディングスコアによる自動重み削減 ──────────
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, str(Path(__file__).parent))
        _cfm    = _imp.import_module("factor_crowding_monitor")
        _c_hist = _cfm._load_crowding_history(cfg)
        if _c_hist:
            _latest_week = max(_c_hist.keys())
            _latest      = _c_hist[_latest_week]
            _c_score     = _latest.get("crowding_score", 0)
            _trigger     = getattr(cfg, "CROWDING_REDUCTION_TRIGGER", 80)
            _rate        = getattr(cfg, "CROWDING_REDUCTION_RATE", 0.85)

            if _c_score >= _trigger:
                print(f"    ⚠️  [RESEARCH-2] クラウディングスコア{_c_score} "
                      f"→ 全ファクター重みを{_rate:.0%}に削減")
                _reduced = 0
                for _fk in list(new_weights.keys()):
                    if _fk.startswith("_"):
                        continue   # _signal_weights等の内部キーはスキップ
                    _old_w = new_weights[_fk].get("weight", 1.0) if isinstance(new_weights[_fk], dict) else float(new_weights[_fk])
                    _new_w = round(max(WEIGHT_MIN, _old_w * _rate), 4)
                    if isinstance(new_weights[_fk], dict):
                        new_weights[_fk]["weight"]             = _new_w
                        new_weights[_fk]["crowding_reduced"]   = True
                        new_weights[_fk]["crowding_score"]     = _c_score
                    else:
                        new_weights[_fk] = _new_w
                    _reduced += 1
                print(f"      削減対象: {_reduced}ファクター")

            # アンワインドアラート時は追加削減（最も危険なタイミング）
            if _latest.get("unwind_alert"):
                print("    🚨 [RESEARCH-2] 解消売りアラート → 追加削減（×0.90）")
                for _fk in list(new_weights.keys()):
                    if _fk.startswith("_"):
                        continue
                    _entry = new_weights[_fk]
                    if isinstance(_entry, dict):
                        _old_w = _entry.get("weight", 1.0)
                        _entry["weight"] = round(max(WEIGHT_MIN, _old_w * 0.90), 4)
                        _entry["unwind_reduced"] = True
    except Exception as _re2e:
        pass   # クラウディング連携失敗は最適化を止めない

    # [統合エンジン] signal_weights を rule_weights.json の "_signal_weights" に保存
    try:
        new_signal_weights = update_signal_weights(cfg, factor_correlations)
        # rule_weights.json に "_signal_weights" として追記
        all_w = load_weights(cfg)
        all_w["_signal_weights"] = new_signal_weights
        save_weights(cfg, all_w)
        print(f"    ✅ signal_weights 更新: "
              f"{[f'{k}:{v:.3f}' for k,v in new_signal_weights.items() if not k.startswith('_')]}")
    except Exception as _se:
        print(f"    ⚠️  signal_weights 更新失敗: {_se}")
    # ── [A-1] IC トラッカー更新（安定有効シグナルを特定） ──────
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, str(Path(__file__).parent))
        _aa = _imp.import_module("alpha_attributer")
        _attr_data = _aa.load_attribution(cfg)
        if _attr_data.get("signals"):
            _ic_result = _aa.update_ic_tracker(cfg, _attr_data)
            if _ic_result.get("stable_signals"):
                print(f"    ✅ [A-1] 安定有効シグナル: "
                      f"{_ic_result['stable_signals'][:3]}")
            if _ic_result.get("review_signals"):
                print(f"    ⚠️  [A-1] 見直し推奨: "
                      f"{_ic_result['review_signals'][:3]}")
            # 見直し推奨シグナルの重みを自動削減
            for sig in _ic_result.get("review_signals", []):
                if sig in new_weights:
                    old_w = new_weights[sig].get("weight", 1.0)
                    new_w = round(max(WEIGHT_MIN, old_w * 0.90), 4)
                    new_weights[sig]["weight"]          = new_w
                    new_weights[sig]["ic_review_flag"]  = True
                    new_weights[sig]["ic_auto_reduced"]  = True
                    print(f"      [A-1] {sig}: {old_w:.3f} → {new_w:.3f} （IC連続負）")
    except Exception as _ae:
        pass

    report_md = format_weight_report(old_weights, new_weights)

# ── 簡易縮退判定（月末更新時） ──────
    # score_history の最新スナップショットから VIX・市場幅を簡易チェックする。
    # 本格的な縮退評価は run_all_tasks() 内で signal_aggregator 呼び出し直前に
    # evaluate_contraction_flag(results, cfg) が実施するため、ここでは
    # score_history 内に格納されているマクロ指標を代用する。
    _contraction_result_monthly: dict = {
        "mode": "Normal", "triggered_count": 0,
        "triggered_flags": [], "detail": {}, "coefficient": 1.00,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        _latest_date = sorted(score_history.keys())[-1] if score_history else None
        _latest_snap = score_history.get(_latest_date, {}) if _latest_date else {}
        _dummy_results_monthly = {
            "options_monitor": {
                "vix_data": {
                    "vix": {"current": _latest_snap.get("vix")}
                },
                "pcr_data": {
                    "pcr_current": _latest_snap.get("pcr")
                },
            },
            "market_breadth": {
                "breadth_score_jp": _latest_snap.get("breadth_score_jp"),
            },
            "bond_commodity": {
                "us10y_yield":      _latest_snap.get("us10y"),
                "us10y_yield_prev": _latest_snap.get("us10y_prev"),
            },
        }
        _contraction_result_monthly = evaluate_contraction_flag(
            _dummy_results_monthly, cfg
        )
        save_contraction_history(cfg, _contraction_result_monthly)
        if _contraction_result_monthly.get("mode") != "Normal":
            print(
                f"    🔴 [縮退判定・月末] "
                f"モード={_contraction_result_monthly['mode']} "
                f"/ triggers={_contraction_result_monthly['triggered_count']}"
            )
    except Exception as _ce_monthly:
        pass   # 縮退判定失敗は最適化全体を止めない

    # ── [FE-F / S4-F] ファクターモメンタム最適化（F-2〜F-5）──
    _factor_result: dict = {}
    try:
        _factor_result = run_factor_momentum_optimizer(cfg, score_history)
    except Exception as _fme:
        print(f"    ⚠️  ファクターモメンタム最適化エラー（スキップ）: "
              f"{str(_fme)[:60]}")

    print(f"    ✅ ルール最適化完了: rule_weights.json 更新")

    return {
        "updated":             True,
        "weights":             new_weights,
        "old_weights":         old_weights,
        "factor_correlations": factor_correlations,
        "report_md":           report_md,
        "factor_momentum":     _factor_result,           # F-2〜F-5
        "contraction_flag":    _contraction_result_monthly,
        "contraction_mode":    _contraction_result_monthly.get("mode", "Normal"),
        "contraction_detail":  _contraction_result_monthly.get("detail", {}),
        "timestamp":           datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
