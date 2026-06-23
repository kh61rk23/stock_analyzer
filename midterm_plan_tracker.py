# ==============================================================
# midterm_plan_tracker.py  v1.0  (Wave 7 - Step 3)
# 中期経営計画達成率自動追跡モジュール
# ==============================================================
# 【役割】
#   企業が公表した中期経営計画（中計）の目標値と、
#   EDINETから取得した実績値を比較して達成率を自動計算し、
#   midterm_plan_cache.json に保存する。
#
# 【設計思想】
#   ・中計達成率 = 経営者の約束遂行能力の定量的証拠
#   ・計画最終年に近づくほど未達リスクがスコアに反映される
#   ・達成ペース（trajectory）の計算でガイダンス修正の先行検知が可能
#   ・pearl_score_calculator.py の D3（利益品質）スコアへの
#     フィードバック入力として使用する
#
# 【主要出力キャッシュ】
#   CACHE_DIR/midterm_plan_cache.json
#   TTL: 90日（月次更新）
#   スキーマ:
#     {
#       "last_updated": "YYYY-MM-DD",
#       "version": "1.0",
#       "plans": {
#         "ticker": {
#           "company_code": str,
#           "ticker": str,
#           "plan_name": str,
#           "plan_period": {"start": str, "end": str},  # "YYYY-MM"
#           "targets": {
#             "net_sales":          {"target": float, "unit": "億円"},
#             "operating_income":   {"target": float, "unit": "億円"},
#             "operating_margin":   {"target": float, "unit": "%"},
#             "roe":                {"target": float, "unit": "%"},
#             "roa":                {"target": float, "unit": "%"},
#             "capex":              {"target": float, "unit": "億円"},
#             "dividend_per_share": {"target": float, "unit": "円"},
#           },
#           "actuals": {
#             "net_sales":        {"value": float, "fiscal_year": str},
#             ...
#           },
#           "achievement_rates": {
#             "net_sales":      {"rate": float, "trajectory": str},
#             ...
#           },
#           "overall_score": float,    # 0.0 ~ 100.0 (加重平均達成スコア)
#           "achievement_tier": str,   # "HIGH" / "MID" / "AT_RISK" / "MISS"
#           "years_remaining": float,  # 計画残存年数
#           "trajectory": str,         # "on_track" / "ahead" / "behind" / "critical"
#           "last_verified": str,
#         }
#       },
#       "stats": { ... }
#     }
#
# 【データ取得戦略】
#   Level 1: BigQuery edinet.financials から実績値取得
#   Level 2: EDINETローテーションキャッシュ（edinet_rotation_manager.py生成物）
#   Level 3: yfinance の財務サマリー
#   Level 4: 中計目標は config.py の MIDTERM_PLAN_TARGETS から静的読み込み
#
# 【依存】
#   config.py（BLOCK I-5、Wave 7追加定数参照）
#   BigQuery（edinet.financials）
#   edinet_rotation_manager.py（ローテーションキャッシュ）
#   yfinance（Level 3フォールバック）
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: デフォルト定数
# ==============================================================

_CACHE_FILENAME = "midterm_plan_cache.json"
_DEFAULT_TTL_DAYS = 90

# 達成率ティア定義
_ACHIEVEMENT_TIERS = {
    "HIGH":    0.90,   # 達成率90%以上
    "MID":     0.70,   # 70〜90%
    "AT_RISK": 0.50,   # 50〜70%（最終2年前で未達リスク判定）
    "MISS":    0.0,    # 50%未満
}

# 軌道判定パラメータ
_TRAJECTORY_THRESHOLDS = {
    "ahead":    1.10,  # 進捗率110%超 → 計画超過ペース
    "on_track": 0.90,  # 90〜110% → 概ねオントラック
    "behind":   0.70,  # 70〜90% → 遅れ
    "critical": 0.0,   # 70%未満 → 危機的遅れ
}

# 各指標の重み（overall_score 計算用）
_METRIC_WEIGHTS = {
    "net_sales":        0.25,
    "operating_income": 0.30,
    "operating_margin": 0.20,
    "roe":              0.15,
    "roa":              0.10,
}

# achievement_score への config.py BLOCK I-5 経由の連携定数デフォルト
_DEFAULT_ACHIEVEMENT_HIGH  = 0.90
_DEFAULT_ACHIEVEMENT_RISK  = 0.70

# 中計目標のデフォルトサンプル（config.py の MIDTERM_PLAN_TARGETS に実際の値を格納）
# 形式: {ticker: {plan_name, plan_period, targets}}
_DEFAULT_MIDTERM_PLANS: dict = {
    "6758.T": {
        "plan_name":   "Sony Group 中期経営計画 2026",
        "plan_period": {"start": "2024-04", "end": "2026-03"},
        "targets": {
            "net_sales":        {"target": 130_000.0, "unit": "億円"},
            "operating_income": {"target":  12_000.0, "unit": "億円"},
            "operating_margin": {"target":      9.0,  "unit": "%"},
            "roe":              {"target":     15.0,  "unit": "%"},
            "roa":              {"target":      5.0,  "unit": "%"},
        },
    },
    "6501.T": {
        "plan_name":   "日立製作所 中期経営計画2024",
        "plan_period": {"start": "2022-04", "end": "2024-03"},
        "targets": {
            "net_sales":        {"target": 100_000.0, "unit": "億円"},
            "operating_income": {"target":   7_000.0, "unit": "億円"},
            "operating_margin": {"target":       7.0, "unit": "%"},
            "roe":              {"target":      10.0, "unit": "%"},
        },
    },
    "7203.T": {
        "plan_name":   "トヨタ中期計画",
        "plan_period": {"start": "2024-04", "end": "2027-03"},
        "targets": {
            "net_sales":        {"target": 500_000.0, "unit": "億円"},
            "operating_income": {"target":  40_000.0, "unit": "億円"},
            "operating_margin": {"target":       8.0, "unit": "%"},
            "roe":              {"target":      15.0, "unit": "%"},
        },
    },
    "9432.T": {
        "plan_name":   "NTT中期経営戦略",
        "plan_period": {"start": "2023-04", "end": "2026-03"},
        "targets": {
            "net_sales":        {"target": 145_000.0, "unit": "億円"},
            "operating_income": {"target":  19_000.0, "unit": "億円"},
            "roe":              {"target":      12.0, "unit": "%"},
        },
    },
    "6702.T": {
        "plan_name":   "富士通中計 Fujitsu Uvance",
        "plan_period": {"start": "2023-04", "end": "2025-03"},
        "targets": {
            "net_sales":        {"target": 40_000.0, "unit": "億円"},
            "operating_income": {"target":  3_500.0, "unit": "億円"},
            "operating_margin": {"target":      9.0, "unit": "%"},
            "roe":              {"target":     20.0, "unit": "%"},
        },
    },
}


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / _CACHE_FILENAME


def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        last_updated = data.get("last_updated", "")
        ttl_days = int(getattr(cfg, "MIDTERM_TRACKER_TTL_DAYS", _DEFAULT_TTL_DAYS))
        if last_updated:
            age = (datetime.now() - datetime.strptime(last_updated, "%Y-%m-%d")).days
            if age < ttl_days:
                return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    try:
        data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        p = _cache_path(cfg)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [midterm] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: 実績値取得（マルチレイヤーフォールバック）
# ==============================================================

def _fetch_actuals_from_bq(
    ticker: str,
    company_code: str,
    targets: dict,
    cfg,
) -> dict:
    """
    BigQuery edinet.financials から最新の実績値を取得する。

    Returns:
        {metric_name: {"value": float, "fiscal_year": str}, ...}
    """
    actuals = {}
    try:
        from edinet_bq_client import get_latest_financials
        fin = get_latest_financials(company_code, market="JP")
        if not fin:
            return actuals

        # 指標名 → BQカラム名マッピング
        metric_col_map = {
            "net_sales":        "net_sales",
            "operating_income": "operating_income",
            "operating_margin": "operating_margin",
            "roe":              "roe",
            "roa":              "roa",
            "capex":            "capex",
        }
        fiscal_year = fin.get("report_period", "")
        for metric, col in metric_col_map.items():
            if metric in targets and col in fin and fin[col] is not None:
                val = float(fin[col])
                # 百分率変換（operating_margin / roe / roa は % 表記）
                if metric in ("operating_margin", "roe", "roa"):
                    if abs(val) < 1.0:
                        val = val * 100.0  # 小数 → % 変換
                actuals[metric] = {"value": val, "fiscal_year": fiscal_year}

    except Exception:
        pass
    return actuals


def _fetch_actuals_from_rotation_cache(
    ticker: str,
    targets: dict,
    cfg,
) -> dict:
    """
    edinet_rotation_manager が生成したローテーションキャッシュから実績値を取得する。

    edinet_rotation_manager.py は CACHE_DIR/edinet_rotation_cache.json に
    company_code別の財務データを格納している。
    """
    actuals = {}
    try:
        cache_dir = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
        rotation_cache_path = cache_dir / "edinet_rotation_cache.json"
        if not rotation_cache_path.exists():
            return actuals

        rotation_data = json.loads(rotation_cache_path.read_text(encoding="utf-8"))
        # ticker から company_code を抽出（例: "6758.T" → "6758"）
        company_code = ticker.split(".")[0] if "." in ticker else ticker
        company_data = rotation_data.get("companies", {}).get(company_code, {})
        financials   = company_data.get("financials", {})

        metric_key_map = {
            "net_sales":        ["net_sales", "revenue"],
            "operating_income": ["operating_income"],
            "operating_margin": ["operating_margin"],
            "roe":              ["roe"],
            "roa":              ["roa"],
            "capex":            ["capex"],
        }
        fiscal_year = financials.get("fiscal_year", "")
        for metric, keys in metric_key_map.items():
            if metric not in targets:
                continue
            for key in keys:
                val = financials.get(key)
                if val is not None:
                    fval = float(val)
                    if metric in ("operating_margin", "roe", "roa"):
                        if abs(fval) < 1.0:
                            fval = fval * 100.0
                    actuals[metric] = {"value": fval, "fiscal_year": fiscal_year}
                    break

    except Exception:
        pass
    return actuals


def _fetch_actuals_from_yfinance(
    ticker: str,
    targets: dict,
) -> dict:
    """
    yfinance から最新財務データを取得する（Level 3 フォールバック）。
    """
    actuals = {}
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        if not info:
            return actuals

        today = datetime.now().strftime("%Y-%m")
        yf_map = {
            "net_sales":        "totalRevenue",
            "operating_income": "operatingIncome",
            "operating_margin": "operatingMargins",
            "roe":              "returnOnEquity",
            "roa":              "returnOnAssets",
        }
        for metric, yf_key in yf_map.items():
            if metric not in targets:
                continue
            val = info.get(yf_key)
            if val is None:
                continue
            fval = float(val)
            # yfinance は売上・利益を円ではなく raw で返す場合があるため
            # ターゲットの単位（億円）に合わせてスケーリング
            if metric in ("net_sales", "operating_income") and fval > 1e10:
                fval = fval / 1e8  # 円 → 億円
            elif metric in ("operating_margin", "roe", "roa"):
                if abs(fval) < 1.0:
                    fval = fval * 100.0
            actuals[metric] = {"value": round(fval, 2), "fiscal_year": today}

    except Exception:
        pass
    return actuals


def _fetch_actuals(ticker: str, targets: dict, cfg) -> dict:
    """
    マルチレイヤーフォールバックで実績値を取得する。
    Level 1 → 2 → 3 の順で試行し、最初に成功したデータを返す。
    """
    company_code = ticker.split(".")[0] if "." in ticker else ticker

    # Level 1: BigQuery
    actuals = _fetch_actuals_from_bq(ticker, company_code, targets, cfg)
    if actuals:
        return actuals

    # Level 2: ローテーションキャッシュ
    actuals = _fetch_actuals_from_rotation_cache(ticker, targets, cfg)
    if actuals:
        return actuals

    # Level 3: yfinance
    actuals = _fetch_actuals_from_yfinance(ticker, targets)
    return actuals


# ==============================================================
# BLOCK 3: 達成率・軌道計算
# ==============================================================

def _calc_years_remaining(plan_period: dict) -> float:
    """
    中計の残存年数を計算する。
    $years\\_remaining = \\frac{end\\_date - today}{365}$
    """
    try:
        end_str = plan_period.get("end", "")
        if not end_str:
            return 0.0
        end_date = datetime.strptime(end_str + "-01", "%Y-%m-%d")
        remaining = (end_date - datetime.now()).days / 365.0
        return round(max(0.0, remaining), 2)
    except Exception:
        return 0.0


def _calc_elapsed_ratio(plan_period: dict) -> float:
    """
    中計の経過率（0.0〜1.0）を計算する。
    $elapsed\\_ratio = \\frac{today - start\\_date}{end\\_date - start\\_date}$
    """
    try:
        start_str = plan_period.get("start", "")
        end_str   = plan_period.get("end", "")
        if not start_str or not end_str:
            return 0.5
        start = datetime.strptime(start_str + "-01", "%Y-%m-%d")
        end   = datetime.strptime(end_str   + "-01", "%Y-%m-%d")
        total_days   = max(1, (end - start).days)
        elapsed_days = max(0, (datetime.now() - start).days)
        return round(min(1.0, elapsed_days / total_days), 3)
    except Exception:
        return 0.5


def _calc_achievement_rate(
    target_val: float,
    actual_val: float,
    metric: str,
) -> float:
    """
    目標値に対する実績値の達成率を計算する。
    $achievement\\_rate = actual / target$ （目標が正の場合）
    利益率・ROE等は目標が正の場合のみ計算。
    """
    try:
        if target_val == 0:
            return 0.0
        rate = actual_val / target_val
        # 達成率は 0.0〜2.0 にクランプ（200%超過は計算外）
        return round(min(2.0, max(0.0, rate)), 4)
    except Exception:
        return 0.0


def _judge_trajectory(
    achievement_rate: float,
    elapsed_ratio: float,
) -> str:
    """
    達成率と経過率から軌道（trajectory）を判定する。
    $progress\\_ratio = achievement\\_rate / elapsed\\_ratio$
    （進捗率 = 現状達成率 ÷ 時間的に期待される達成率）
    """
    try:
        if elapsed_ratio <= 0:
            return "on_track"
        progress_ratio = achievement_rate / elapsed_ratio
        if progress_ratio >= _TRAJECTORY_THRESHOLDS["ahead"]:
            return "ahead"
        elif progress_ratio >= _TRAJECTORY_THRESHOLDS["on_track"]:
            return "on_track"
        elif progress_ratio >= _TRAJECTORY_THRESHOLDS["behind"]:
            return "behind"
        else:
            return "critical"
    except Exception:
        return "on_track"


def _calc_overall_score(
    achievement_rates: dict,
    years_remaining: float,
    cfg,
) -> tuple:
    """
    全指標の加重平均から総合達成スコアを計算する。
    $overall\\_score = \\sum_i w_i \\times achievement\\_rate_i \\times 100$
    残存年数が少ないほど未達ペナルティを乗算する。

    Returns:
        (overall_score: float 0〜100, achievement_tier: str)
    """
    try:
        total_weight = 0.0
        weighted_sum = 0.0
        for metric, weight in _METRIC_WEIGHTS.items():
            if metric in achievement_rates:
                rate = achievement_rates[metric].get("rate", 0.0)
                weighted_sum += rate * weight
                total_weight += weight

        if total_weight == 0:
            return 0.0, "AT_RISK"

        raw_score = (weighted_sum / total_weight) * 100.0

        # 残存年数が2年未満かつスコアが低い場合にペナルティ
        risk_threshold = float(getattr(cfg, "MIDTERM_ACHIEVEMENT_RISK", _DEFAULT_ACHIEVEMENT_RISK))
        if years_remaining < 2.0 and raw_score < risk_threshold * 100:
            penalty = max(0.7, years_remaining / 2.0)
            raw_score *= penalty

        score = round(min(100.0, max(0.0, raw_score)), 1)

        # ティア判定
        high_thresh = float(getattr(cfg, "MIDTERM_ACHIEVEMENT_HIGH", _DEFAULT_ACHIEVEMENT_HIGH))
        if score >= high_thresh * 100:
            tier = "HIGH"
        elif score >= risk_threshold * 100:
            tier = "MID"
        elif score >= 50.0:
            tier = "AT_RISK"
        else:
            tier = "MISS"

        return score, tier

    except Exception:
        return 0.0, "AT_RISK"


def _judge_overall_trajectory(achievement_rates: dict) -> str:
    """
    全指標の軌道を統合して全体軌道を判定する。
    過半数の指標が "critical" → "critical"
    """
    trajectories = [
        v.get("trajectory", "on_track")
        for v in achievement_rates.values()
    ]
    if not trajectories:
        return "on_track"
    counts = {t: trajectories.count(t) for t in set(trajectories)}
    majority = max(counts, key=counts.get)
    # "ahead" が過半数の場合は "ahead"
    if counts.get("ahead", 0) > len(trajectories) / 2:
        return "ahead"
    # "critical" が1件でもあれば全体を "critical" に引き上げ
    if "critical" in counts:
        return "critical"
    return majority


# ==============================================================
# BLOCK 4: 1企業の中計評価フロー
# ==============================================================

def _evaluate_single_plan(
    ticker: str,
    plan_def: dict,
    cfg,
) -> dict:
    """
    1企業の中計を評価して結果辞書を返す。

    処理:
      1. 実績値取得（マルチレイヤー）
      2. 指標別達成率・軌道計算
      3. 総合スコア計算
      4. 結果辞書の構築

    Returns:
        完全な plan 評価結果辞書
    """
    plan_period  = plan_def.get("plan_period", {})
    targets      = plan_def.get("targets", {})
    years_rem    = _calc_years_remaining(plan_period)
    elapsed      = _calc_elapsed_ratio(plan_period)

    # 実績値取得
    actuals = _fetch_actuals(ticker, targets, cfg)

    # 指標別達成率・軌道計算
    achievement_rates: dict = {}
    for metric, target_info in targets.items():
        target_val = target_info.get("target")
        if target_val is None:
            continue
        actual_info = actuals.get(metric)
        if actual_info is None:
            achievement_rates[metric] = {
                "rate":       None,
                "trajectory": "unknown",
            }
            continue
        actual_val = actual_info.get("value", 0.0)
        rate       = _calc_achievement_rate(float(target_val), actual_val, metric)
        trajectory = _judge_trajectory(rate, elapsed)
        achievement_rates[metric] = {
            "rate":       rate,
            "trajectory": trajectory,
        }

    # 総合スコア計算
    overall_score, tier = _calc_overall_score(achievement_rates, years_rem, cfg)
    overall_trajectory  = _judge_overall_trajectory(achievement_rates)

    return {
        "ticker":             ticker,
        "company_code":       ticker.split(".")[0] if "." in ticker else ticker,
        "plan_name":          plan_def.get("plan_name", ""),
        "plan_period":        plan_period,
        "targets":            targets,
        "actuals":            actuals,
        "achievement_rates":  achievement_rates,
        "overall_score":      overall_score,
        "achievement_tier":   tier,
        "years_remaining":    years_rem,
        "elapsed_ratio":      elapsed,
        "trajectory":         overall_trajectory,
        "last_verified":      datetime.now().strftime("%Y-%m-%d"),
    }


# ==============================================================
# BLOCK 5: 外部参照用アクセサ
# ==============================================================

def get_midterm_score(ticker: str, cfg=None) -> Optional[float]:
    """
    銘柄の中計達成スコアを midterm_plan_cache.json から取得する。
    Returns: float 0.0〜100.0 または None
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return None
        return cache.get("plans", {}).get(ticker, {}).get("overall_score")
    except Exception:
        return None


def get_midterm_trajectory(ticker: str, cfg=None) -> Optional[str]:
    """
    銘柄の中計軌道（"ahead"/"on_track"/"behind"/"critical"）を返す。
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return None
        return cache.get("plans", {}).get(ticker, {}).get("trajectory")
    except Exception:
        return None


def get_midterm_achievement_tier(ticker: str, cfg=None) -> Optional[str]:
    """
    銘柄の中計達成ティア（"HIGH"/"MID"/"AT_RISK"/"MISS"）を返す。
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return None
        return cache.get("plans", {}).get(ticker, {}).get("achievement_tier")
    except Exception:
        return None


def get_midterm_plan_summary(ticker: str, cfg=None) -> dict:
    """
    中計評価の全データを返す（pearl_score_calculator.py の D3 統合用）。
    Returns: 計画評価辞書 または {}
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return {}
        return cache.get("plans", {}).get(ticker, {})
    except Exception:
        return {}


def get_achievement_rate(cfg, ticker: str):
    """
    中計達成率の平均値を返す（ceo_evaluator [I-3] 軸2の補助情報）。

    ★ 2026-06-11 バグ修正: ceo_evaluator.py が本関数名で呼んでいたが
      実体が存在せず、中計DB優先参照パスが常時フォールバックしていた。
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return None
        plan  = cache.get("plans", {}).get(ticker, {})
        rates = [
            float(v.get("rate"))
            for v in plan.get("achievement_rates", {}).values()
            if isinstance(v, dict) and v.get("rate") is not None
        ]
        if not rates:
            return None
        return round(sum(rates) / len(rates), 4)
    except Exception:
        return None


def get_ceo_axis2_score(cfg, ticker: str) -> float:
    """
    経営者スコア軸2（中計達成・0〜25pt）を返す。
    overall_score（0〜100）の 1/4 スケール変換。

    ★ 2026-06-11 バグ修正: ceo_evaluator.py が本関数名で呼んでいたが
      実体が存在しなかった（未実装の計画API）。
    ※ 当該銘柄の中計データが無い場合は LookupError を送出する
      （呼び出し側の try/except が EDINET 直接取得へフォールバックする設計）。
    """
    cache = _load_cache(cfg)
    plan  = (cache or {}).get("plans", {}).get(ticker)
    if not plan or plan.get("overall_score") is None:
        raise LookupError(f"midterm plan not found: {ticker}")
    score = max(0.0, min(100.0, float(plan["overall_score"])))
    return round(score / 4.0, 1)


def format_midterm_section(plans: dict) -> list:
    """
    週次レポート用の中計達成状況セクション（Markdown行リスト）を生成。

    ★ 2026-06-11 バグ修正: weekly_report_generator.py が本関数名で
      呼んでいたが実体が存在せず、[I-5] セクションが常時空だった。
    """
    if not plans:
        return []
    lines = ["## 🎯 中期経営計画 達成状況 [I-5]", ""]
    lines.append("| 銘柄 | 計画 | 達成スコア | ティア | 軌道 |")
    lines.append("|---|---|---|---|---|")
    _order = {"MISS": 0, "AT_RISK": 1, "MID": 2, "HIGH": 3}
    items = sorted(
        (p for p in plans.values() if isinstance(p, dict)),
        key=lambda p: _order.get(p.get("achievement_tier", "MID"), 2),
    )
    for p in items[:15]:
        lines.append(
            f"| {p.get('ticker', '')} | {str(p.get('plan_name', ''))[:20]} "
            f"| {float(p.get('overall_score', 0) or 0):.0f} "
            f"| {p.get('achievement_tier', '')} "
            f"| {p.get('trajectory', '')} |"
        )
    lines.append("")
    return lines


# ==============================================================
# BLOCK 6: メイン実行フロー
# ==============================================================

def run_midterm_plan_tracker(cfg=None, force_refresh: bool = False) -> dict:
    """
    stock_analyzer_main.py の WEEKLY_MODULES から呼ばれるエントリポイント。

    処理フロー:
      1. キャッシュ有効 → 返却
      2. MIDTERM_PLAN_TARGETS（config）または _DEFAULT_MIDTERM_PLANS から計画一覧取得
      3. 企業ごとに実績値取得・達成率計算
      4. midterm_plan_cache.json に保存

    Returns:
        {
          "plans":     dict,  # ticker別の評価結果
          "stats":     dict,
          "cached":    bool,
          "timestamp": str,
        }
    """
    print("\n  📊 中期経営計画トラッカー（Wave 7 - Step 3）実行中...")

    # ── キャッシュ確認 ──────────────────────────────────────────
    if not force_refresh:
        cached = _load_cache(cfg)
        if cached:
            stats = cached.get("stats", {})
            print(
                f"    ✅ キャッシュ使用: "
                f"企業={stats.get('total_companies', 0)} / "
                f"HIGH={stats.get('high_count', 0)} / "
                f"AT_RISK={stats.get('at_risk_count', 0)}"
            )
            return {
                "plans":     cached.get("plans", {}),
                "stats":     stats,
                "cached":    True,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    # ── 中計一覧取得 ──────────────────────────────────────────
    plan_targets = getattr(cfg, "MIDTERM_PLAN_TARGETS", _DEFAULT_MIDTERM_PLANS)
    if not plan_targets:
        plan_targets = _DEFAULT_MIDTERM_PLANS

    all_plans: dict = {}

    for ticker, plan_def in plan_targets.items():
        print(f"    📋 {ticker} 中計評価中...")
        try:
            result = _evaluate_single_plan(ticker, plan_def, cfg)
            all_plans[ticker] = result
            tier = result.get("achievement_tier", "?")
            score = result.get("overall_score", 0.0)
            traj  = result.get("trajectory", "?")
            print(
                f"       完了: score={score:.1f} "
                f"tier={tier} trajectory={traj} "
                f"残存={result.get('years_remaining', 0):.1f}年"
            )
        except Exception as e:
            print(f"    ⚠️  [{ticker}] 評価エラー: {str(e)[:60]}")

    # ── 統計集計 ─────────────────────────────────────────────
    tier_counts = {"HIGH": 0, "MID": 0, "AT_RISK": 0, "MISS": 0}
    for plan in all_plans.values():
        t = plan.get("achievement_tier", "AT_RISK")
        tier_counts[t] = tier_counts.get(t, 0) + 1

    at_risk_tickers = [
        t for t, p in all_plans.items()
        if p.get("achievement_tier") in ("AT_RISK", "MISS")
    ]

    stats = {
        "total_companies": len(all_plans),
        "high_count":      tier_counts.get("HIGH", 0),
        "mid_count":       tier_counts.get("MID", 0),
        "at_risk_count":   tier_counts.get("AT_RISK", 0) + tier_counts.get("MISS", 0),
        "at_risk_tickers": at_risk_tickers,
        "tier_distribution": tier_counts,
    }

    # ── 保存 ─────────────────────────────────────────────────
    cache_data = {
        "plans":   all_plans,
        "stats":   stats,
        "version": "1.0",
    }
    _save_cache(cfg, cache_data)

    print(
        f"    ✅ 中計トラッカー完了: "
        f"企業={len(all_plans)} / "
        f"HIGH={tier_counts.get('HIGH',0)} / "
        f"AT_RISK={tier_counts.get('AT_RISK',0)+tier_counts.get('MISS',0)}"
    )
    if at_risk_tickers:
        print(f"    ⚠️  未達リスク: {', '.join(at_risk_tickers)}")

    return {
        "plans":     all_plans,
        "stats":     stats,
        "cached":    False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 7: 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  📊 midterm_plan_tracker.py v1.0 単体テスト")
    print("=" * 60)

    result = run_midterm_plan_tracker(cfg=None, force_refresh=True)

    print(f"\n  【テスト結果】")
    print(f"  評価企業数  : {result['stats'].get('total_companies', 0)}")
    print(f"  HIGH        : {result['stats'].get('high_count', 0)}")
    print(f"  AT_RISK     : {result['stats'].get('at_risk_count', 0)}")

    plans = result.get("plans", {})
    if plans:
        print(f"\n  【評価サマリー】")
        for ticker, plan_data in plans.items():
            print(
                f"  {ticker:10s}: "
                f"score={plan_data.get('overall_score', 0):5.1f} "
                f"tier={plan_data.get('achievement_tier', '?'):8s} "
                f"traj={plan_data.get('trajectory', '?'):10s} "
                f"残存={plan_data.get('years_remaining', 0):.1f}年 "
                f"実績取得={len(plan_data.get('actuals', {}))}指標"
            )

    # アクセサテスト
    print(f"\n  【アクセサテスト: 7203.T（トヨタ）】")
    score = get_midterm_score("7203.T")
    tier  = get_midterm_achievement_tier("7203.T")
    traj  = get_midterm_trajectory("7203.T")
    print(f"  overall_score    : {score}")
    print(f"  achievement_tier : {tier}")
    print(f"  trajectory       : {traj}")

    print(f"\n  ✅ 単体テスト完了")