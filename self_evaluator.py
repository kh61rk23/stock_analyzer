# ==============================================================
# self_evaluator.py  v1.1
# パフォーマンス自己検証 — screener予測精度の継続的測定
# ==============================================================
# 【役割】
#   screener_history.json に蓄積された「過去の予測」と
#   実際の株価リターンを照合し、システムの予測精度を自動検証する。
#   結果を performance_log.json に蓄積して llm_rule_generator の
#   入力データとする。
#
# 【backtester.py との違い】
#   backtester.py:    「screenerの条件全体」のパフォーマンスを測定
#   self_evaluator.py: 「なぜ外れたか」のファクター分析まで踏み込む
#                       → rule_optimizer / llm_rule_generator への連鎖入力
#
# 【分析内容】
#   A. スコア帯別的中率の推移
#      score80+ / score60-80 / score40-60 / score40- で分類し
#      各帯の「ベンチマーク超過勝率」を週次で追跡
#
#   B. 「なぜ外れたか」ファクター分析（miss_analysis）
#      外れた銘柄（予測高スコア × 実際低リターン）について:
#        - 当時発動していたマクロシグナルを記録
#        - 「円安シグナル発動時に輸出株を買ったが外れた」パターンを検出
#        - factor_breakdown の items と照合して責任ファクターを特定
#
#   C. 業種別的中率
#      どの業種のスクリーニングが最も精度が高いか
#
#   D. スコアドリフト検出
#      同じ銘柄が複数週にわたって通過し続けている場合、
#      その後のリターンパターンを分析（過最適化の検出）
#
# 【performance_log.json スキーマ】
#   {
#     "log_entries": [
#       {
#         "week":            "2026-W12",
#         "evaluated":       int,   # 評価銘柄数
#         "win_rate_overall": float,
#         "win_rate_high":   float,  # score80+
#         "avg_alpha":       float,
#         "miss_factors":    [{"factor":str, "miss_count":int, "miss_rate":float}],
#         "sector_win_rates": {sector_code: float},
#         "timestamp":       str,
#       },
#       ...   # 最大52エントリ（1年分）保持
#     ],
#     "summary": {
#       "total_evaluated":    int,
#       "lifetime_win_rate":  float,
#       "best_sector":        str,
#       "worst_factor":       str,   # 最も外れに寄与したファクター
#       "trend":              str,   # "improving" / "stable" / "degrading"
#     }
#   }
#
# 【依存】screener_history.json / score_history.json / yfinance
# ==============================================================

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

import score_history_io

HOLD_DAYS        = 28     # ホールド期間
LOOKBACK_WEEKS   = 8      # 直近N週分を分析
MIN_SAMPLES      = 5      # 統計的有意性の最低サンプル数
BENCHMARK_TICKER = "^N225"
FETCH_DELAY      = 0.25
LOG_FILENAME     = "performance_log.json"
MAX_LOG_ENTRIES  = 52     # 1年分保持

# ── Pearl バックテスト用定数 ──────────────────────────
PEARL_BACKTEST_FILENAME  = "pearl_backtest_log.json"
PEARL_BACKTEST_MAX_ENTRY = 52           # 1年分保持

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'SE_PEARL_HIT_ALPHA_THRESHOLD': 'PEARL_HIT_ALPHA_THRESHOLD',
    'B4_SELF_EVALUATOR_HOLD_DAYS': 'HOLD_DAYS',
    'B4_SELF_EVALUATOR_LOOKBACK_WEEKS': 'LOOKBACK_WEEKS',
    'B5_SELF_EVALUATOR_FETCH_DELAY': 'FETCH_DELAY',
    'B6_SELF_EVALUATOR_MIN_SAMPLES': 'MIN_SAMPLES',
    'B6_SELF_EVALUATOR_MAX_LOG_ENTRIES': 'MAX_LOG_ENTRIES',
    'B6_SELF_EVALUATOR_PEARL_BACKTEST_MAX_ENTRY': 'PEARL_BACKTEST_MAX_ENTRY',
}

PEARL_HIT_ALPHA_THRESHOLD = 3.0        # 的中とみなすα（%）
PEARL_TRACK_TIERS = ["SS", "S", "A"]   # 追跡対象 Pearl Tier

# ==============================================================
# BLOCK 1: データ読み込み
# ==============================================================

def _load_screener_history(cfg) -> dict:
    for p in [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "screener_history.json",
        Path("stock_data/data/screener_history.json"),
    ]:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _load_score_history(cfg) -> dict:
    """公開済み関数名を維持し、完全同一だった共通I/Oへ委譲する。"""
    return score_history_io.load_score_history(cfg)


def _load_perf_log(cfg) -> dict:
    p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / LOG_FILENAME
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"log_entries": [], "summary": {}}


def _save_perf_log(cfg, log: dict) -> None:
    p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / LOG_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    # 最大エントリ数を維持
    log["log_entries"] = log.get("log_entries", [])[-MAX_LOG_ENTRIES:]
    log["updated_at"] = datetime.now().isoformat()
    p.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


# ==============================================================
# BLOCK 2: リターン計算ユーティリティ
# ==============================================================

def _fetch_return(ticker: str, entry_date: str, exit_date: str) -> Optional[float]:
    """entry → exit のリターン%を yfinance から取得する。"""
    try:
        import yf_cache as yf
        start = (datetime.strptime(entry_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
        end   = (datetime.strptime(exit_date,  "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")
        hist  = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
        if hist is None or len(hist) < 2:
            return None
        hist.index = hist.index.tz_localize(None) if hasattr(hist.index, "tz_localize") else hist.index

        def _get_price(date_str: str) -> Optional[float]:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            for offset in range(5):
                d = (dt + timedelta(days=offset)).strftime("%Y-%m-%d")
                row = hist[hist.index.strftime("%Y-%m-%d") == d]
                if not row.empty:
                    return float(row["Close"].iloc[0])
            return None

        ep = _get_price(entry_date)
        xp = _get_price(exit_date)
        if ep and xp and ep > 0:
            return round((xp - ep) / ep * 100, 3)
    except Exception:
        pass
    return None


def _fetch_benchmark_return(entry_date: str, exit_date: str) -> Optional[float]:
    return _fetch_return(BENCHMARK_TICKER, entry_date, exit_date)


# ==============================================================
# BLOCK 3: 週次評価バッチ構築
# ==============================================================

def build_evaluation_batch(
    screener_history: dict,
    hold_days: int = HOLD_DAYS,
    lookback_weeks: int = LOOKBACK_WEEKS,
) -> list:
    """
    評価対象レコードを構築する（通過済み + ホールド期間終了済みのもの）。

    Returns:
        [{"ticker", "entry_date", "exit_date", "entry_price",
          "composite_score", "sector_code", "macro_signals", ...}]
    """
    today    = date.today()
    cutoff   = today - timedelta(weeks=lookback_weeks)
    exit_min = today - timedelta(days=hold_days)

    records = []
    for date_str, candidates in screener_history.items():
        try:
            entry_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if entry_dt < cutoff or entry_dt > exit_min:
            continue
        if not isinstance(candidates, list):
            continue

        for c in candidates:
            # screener_history には通過銘柄しか保存されず、pass_filters 欄の無い古い行もある。
            # 欄の無い行や current_price の無い行を落とすと有効レコードが0件になるため、
            # 明示的に False の行だけ除外し、価格は終値で補う。
            if not isinstance(c, dict) or c.get("pass_filters") is False:
                continue
            ticker    = c.get("ticker", "")
            price     = c.get("current_price") or c.get("entry_price")
            score     = c.get("composite_score", 0) or 0
            sector    = c.get("sector_code", "")
            m_signals = c.get("macro_signals", [])
            if not ticker:
                continue
            exit_dt   = entry_dt + timedelta(days=hold_days)
            records.append({
                "ticker":          ticker,
                "entry_date":      date_str,
                "exit_date":       exit_dt.strftime("%Y-%m-%d"),
                "entry_price":     float(price) if price else None,
                "composite_score": float(score),
                "sector_code":     sector,
                "macro_signals":   m_signals,
                "value_score":     c.get("value_score", 50) or 50,
                "quality_score":   c.get("quality_score", 50) or 50,
                "technical_score": c.get("technical_score", 50) or 50,
                # ── Pearl 関連フィールド ────────────────────────
                "pearl_tier":          c.get("pearl_tier"),
                "pearl_score":         float(c.get("pearl_score", 0) or 0),
                "latent_pearl_score":  float(c.get("latent_pearl_score", 0) or 0),
                "independence_score":  float(c.get("independence_score", 0) or 0),
                "catalyst_flags":      c.get("catalyst_flags", []),
                "midterm_trajectory":  c.get("midterm_trajectory", ""),
                "midterm_score":       float(c.get("midterm_overall_score", 0) or 0),
                "is_latent_pearl":     bool(c.get("is_latent_pearl", False)),
            })
    return records


# ==============================================================
# BLOCK 4: スコア因子別ミス分析
# ==============================================================

def analyze_miss_factors(evaluated: list) -> list:
    """
    「外れた予測」（高スコア × 低リターン）から責任ファクターを特定する。

    外れの定義: composite_score >= 65 かつ alpha < -3%（ベンチマーク3%以上下回った）

    Returns:
        [{"factor":str, "miss_count":int, "total_fired":int, "miss_rate":float,
          "avg_alpha_when_fired":float}]
    """
    # ファクターごとの発動回数と外れ回数を集計
    factor_stats: dict[str, dict] = {}

    for r in evaluated:
        score = r.get("composite_score", 0)
        alpha = r.get("alpha", 0) or 0
        is_miss = (score >= 65 and alpha < -3)

        for sig in r.get("macro_signals", []):
            factor = sig.get("factor") or sig.get("label", "")
            if not factor:
                continue
            s = factor_stats.setdefault(factor, {
                "miss_count": 0, "fire_count": 0, "alpha_sum": 0
            })
            s["fire_count"] += 1
            s["alpha_sum"]  += alpha
            if is_miss:
                s["miss_count"] += 1

    results = []
    for factor, s in factor_stats.items():
        if s["fire_count"] < 3:
            continue
        miss_rate = round(s["miss_count"] / s["fire_count"], 3)
        avg_alpha = round(s["alpha_sum"] / s["fire_count"], 3)
        results.append({
            "factor":            factor,
            "miss_count":        s["miss_count"],
            "total_fired":       s["fire_count"],
            "miss_rate":         miss_rate,
            "avg_alpha_when_fired": avg_alpha,
        })

    # miss_rate が高い順にソート
    return sorted(results, key=lambda x: -x["miss_rate"])


def analyze_score_component_impact(evaluated: list) -> dict:
    """
    スコアの4成分（sector/value/quality/technical）と実際のリターンの相関を計算する。
    「どのスコア成分が最も予測精度に寄与しているか」を明らかにする。

    Returns:
        {"sector_score":corr, "value_score":corr, "quality_score":corr, "technical_score":corr}
    """
    def _pearson(xs: list, ys: list) -> Optional[float]:
        n = min(len(xs), len(ys))
        if n < 5:
            return None
        xs, ys = xs[:n], ys[:n]
        mx, my = sum(xs)/n, sum(ys)/n
        num = sum((x-mx)*(y-my) for x,y in zip(xs, ys))
        sx  = math.sqrt(sum((x-mx)**2 for x in xs))
        sy  = math.sqrt(sum((y-my)**2 for y in ys))
        if sx < 1e-9 or sy < 1e-9:
            return None
        return round(num/(sx*sy), 4)

    rets = [r.get("alpha", 0) or 0 for r in evaluated]
    return {
        comp: _pearson([r.get(comp, 50) or 50 for r in evaluated], rets)
        for comp in ("sector_score", "value_score", "quality_score", "technical_score")
    }


# ==============================================================
# BLOCK 5: 週次サマリー生成
# ==============================================================

def _win_rate(returns: list, benchmark: float = 0) -> Optional[float]:
    if not returns:
        return None
    return round(sum(1 for r in returns if r > benchmark) / len(returns), 3)


def _avg(vals: list) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return round(sum(clean)/len(clean), 3) if clean else None


def build_weekly_summary(
    evaluated:    list,
    week_str:     str,
    avg_benchmark: float,
) -> dict:
    """
    1週分の評価結果から summary dict を構築する。
    """
    rets    = [r.get("alpha", 0) or 0 for r in evaluated]
    scores  = [r.get("composite_score", 50) for r in evaluated]

    # スコア帯別
    high   = [r.get("alpha",0) or 0 for r in evaluated if r.get("composite_score",0) >= 80]
    mid_hi = [r.get("alpha",0) or 0 for r in evaluated if 60 <= r.get("composite_score",0) < 80]
    mid_lo = [r.get("alpha",0) or 0 for r in evaluated if 40 <= r.get("composite_score",0) < 60]
    low    = [r.get("alpha",0) or 0 for r in evaluated if r.get("composite_score",0) < 40]

    # 業種別
    sector_groups: dict[str, list] = {}
    for r in evaluated:
        sector_groups.setdefault(r.get("sector_code","?"), []).append(r.get("alpha",0) or 0)
    sector_win_rates = {
        code: _win_rate(rets_s) or 0
        for code, rets_s in sector_groups.items()
        if len(rets_s) >= 2
    }

    miss_factors    = analyze_miss_factors(evaluated)
    component_corrs = analyze_score_component_impact(evaluated)

    return {
        "week":              week_str,
        "evaluated":         len(evaluated),
        "avg_benchmark":     round(avg_benchmark, 3),
        "win_rate_overall":  _win_rate(rets) or 0,
        "win_rate_score80+": _win_rate(high)  or 0,
        "win_rate_60_80":    _win_rate(mid_hi) or 0,
        "win_rate_40_60":    _win_rate(mid_lo) or 0,
        "win_rate_sub40":    _win_rate(low)    or 0,
        "avg_alpha":         _avg(rets) or 0,
        "avg_alpha_high":    _avg(high) or 0,
        "miss_factors":      miss_factors[:5],
        "sector_win_rates":  sector_win_rates,
        "component_corrs":   component_corrs,
        "n_high_score_miss": sum(1 for r in evaluated
                                  if r.get("composite_score",0) >= 65 and (r.get("alpha",0) or 0) < -3),
        # ── Pearl / 潜伏Pearl 別パフォーマンス ──────────
        "pearl_stats":       _calc_pearl_win_stats(evaluated),
        "latent_pearl_stats": _calc_latent_pearl_win_stats(evaluated),
        "timestamp":         datetime.now().isoformat(),
    }

def _calc_pearl_win_stats(evaluated: list) -> dict:
    """
    Pearl Tier 別の的中率・平均αを計算する。

    $hit\\_rate = \\frac{|\\{r: r.alpha \\geq PEARL\\_HIT\\_ALPHA\\_THRESHOLD\\}|}{N}$

    Returns:
        {
          "SS": {"n": int, "hit_rate": float, "avg_alpha": float},
          "S":  {...},
          "A":  {...},
          "all_pearl": {...},
        }
    """
    result: dict = {}
    for tier in PEARL_TRACK_TIERS + ["all_pearl"]:
        if tier == "all_pearl":
            group = [r for r in evaluated if r.get("pearl_tier") in PEARL_TRACK_TIERS]
        else:
            group = [r for r in evaluated if r.get("pearl_tier") == tier]
        if not group:
            result[tier] = {"n": 0, "hit_rate": None, "avg_alpha": None}
            continue
        alphas   = [float(r.get("alpha", 0) or 0) for r in group]
        hit_rate = sum(1 for a in alphas if a >= PEARL_HIT_ALPHA_THRESHOLD) / len(alphas)
        result[tier] = {
            "n":         len(group),
            "hit_rate":  round(hit_rate, 4),
            "avg_alpha": round(sum(alphas) / len(alphas), 3),
        }
    return result


def _calc_latent_pearl_win_stats(evaluated: list) -> dict:
    """
    潜伏銘柄（is_latent_pearl == True）の的中率・平均αを計算する。
    independence_score・catalyst の存在別にもブレークダウンする。

    $hit\\_rate_{latent} = \\frac{|\\{r: r.is\\_latent\\_pearl \\wedge r.alpha \\geq threshold\\}|}{N_{latent}}$

    Returns:
        {
          "n_latent":              int,
          "hit_rate":              float,
          "avg_alpha":             float,
          "vs_non_latent_alpha":   float,    # 非潜伏との平均αの差
          "high_indep_hit_rate":   float,    # independence_score>=65 のサブセット
          "catalyst_hit_rate":     float,    # catalyst_flags 有りのサブセット
          "midterm_ahead_hit_rate": float,   # midterm_trajectory=="ahead" のサブセット
        }
    """
    latent = [r for r in evaluated if r.get("is_latent_pearl")]
    non_latent = [r for r in evaluated if not r.get("is_latent_pearl")]

    if not latent:
        return {
            "n_latent":               0,
            "hit_rate":               None,
            "avg_alpha":              None,
            "vs_non_latent_alpha":    None,
            "high_indep_hit_rate":    None,
            "catalyst_hit_rate":      None,
            "midterm_ahead_hit_rate": None,
        }

    def _stats(group: list) -> tuple:
        if not group:
            return None, None
        alphas = [float(r.get("alpha", 0) or 0) for r in group]
        hr = sum(1 for a in alphas if a >= PEARL_HIT_ALPHA_THRESHOLD) / len(alphas)
        return round(hr, 4), round(sum(alphas) / len(alphas), 3)

    hr_all, avg_all = _stats(latent)
    _, avg_non = _stats(non_latent) if non_latent else (None, None)

    # サブセット
    high_indep = [r for r in latent if float(r.get("independence_score", 0)) >= 65.0]
    hr_hi, _   = _stats(high_indep)

    has_cat    = [r for r in latent if r.get("catalyst_flags")]
    hr_cat, _  = _stats(has_cat)

    ahead      = [r for r in latent if r.get("midterm_trajectory") == "ahead"]
    hr_mt, _   = _stats(ahead)

    return {
        "n_latent":               len(latent),
        "hit_rate":               hr_all,
        "avg_alpha":              avg_all,
        "vs_non_latent_alpha":    round(
            (avg_all or 0) - (avg_non or 0), 3
        ) if avg_all is not None and avg_non is not None else None,
        "high_indep_hit_rate":    hr_hi,
        "catalyst_hit_rate":      hr_cat,
        "midterm_ahead_hit_rate": hr_mt,
    }

# ==============================================================
# BLOCK 6: ログサマリー更新
# ==============================================================

def update_log_summary(log: dict) -> dict:
    """
    全ログエントリから通算サマリーを再計算する。
    llm_rule_generator が参照する重要なフィールド。
    """
    entries = log.get("log_entries", [])
    if not entries:
        log["summary"] = {}
        return log

    all_win_rates = [e.get("win_rate_overall", 0.5) for e in entries]
    all_alphas    = [e.get("avg_alpha", 0) for e in entries]

    # 最も外れに寄与したファクター（通算miss_rate最大）
    factor_agg: dict[str, list] = {}
    for e in entries:
        for mf in e.get("miss_factors", []):
            factor_agg.setdefault(mf["factor"], []).append(mf["miss_rate"])
    worst_factor = max(factor_agg.items(),
                       key=lambda x: sum(x[1])/len(x[1]), default=("N/A", [0]))

    # 業種別通算勝率
    sector_agg: dict[str, list] = {}
    for e in entries:
        for code, wr in e.get("sector_win_rates", {}).items():
            sector_agg.setdefault(code, []).append(wr)
    sector_avg = {code: round(sum(v)/len(v), 3) for code, v in sector_agg.items() if v}
    best_sector  = max(sector_avg.items(), key=lambda x: x[1], default=("N/A", 0))
    worst_sector = min(sector_avg.items(), key=lambda x: x[1], default=("N/A", 0))

    # トレンド判定（直近4週 vs 過去4週）
    trend = "stable"
    if len(all_win_rates) >= 8:
        recent = sum(all_win_rates[-4:]) / 4
        older  = sum(all_win_rates[-8:-4]) / 4
        if recent > older + 0.05:
            trend = "improving"
        elif recent < older - 0.05:
            trend = "degrading"

    # component_corrs の通算（どのスコア成分が最も予測に効くか）
    comp_agg: dict[str, list] = {}
    for e in entries:
        for comp, corr in e.get("component_corrs", {}).items():
            if corr is not None:
                comp_agg.setdefault(comp, []).append(corr)
    comp_avg = {k: round(sum(v)/len(v), 4) for k, v in comp_agg.items() if v}
    best_component = max(comp_avg.items(), key=lambda x: x[1], default=("N/A", 0))

    log["summary"] = {
        "total_evaluated":    sum(e.get("evaluated", 0) for e in entries),
        "lifetime_win_rate":  round(sum(all_win_rates) / len(all_win_rates), 3),
        "lifetime_avg_alpha": round(sum(all_alphas) / len(all_alphas), 3),
        "best_sector":        best_sector[0],
        "best_sector_wr":     best_sector[1],
        "worst_sector":       worst_sector[0],
        "worst_sector_wr":    worst_sector[1],
        "worst_factor":       worst_factor[0],
        "worst_factor_miss_rate": round(sum(worst_factor[1])/len(worst_factor[1]), 3) if worst_factor[1] else 0,
        "best_score_component": best_component[0],
        "trend":              trend,
        "n_weeks_evaluated":  len(entries),
    }
    return log


# ==============================================================
# BLOCK 7: Markdownレポート生成
# ==============================================================

def format_self_eval_report(log: dict, latest_entry: Optional[dict]) -> str:
    """weekly_report 向けの自己評価レポートを生成する。"""
    summary = log.get("summary", {})
    entry   = latest_entry or {}

    trend_icon = {"improving": "📈", "degrading": "📉", "stable": "➡️"}.get(
        summary.get("trend", "stable"), "➡️")

    lines = [
        "## 🔍 screener自己検証レポート（Self Evaluator）",
        "",
        f"**通算評価銘柄数**: {summary.get('total_evaluated', 0)}件  "
        f"**生涯勝率**: {summary.get('lifetime_win_rate', 0):.1%}  "
        f"**生涯αリターン**: {summary.get('lifetime_avg_alpha', 0):+.2f}%",
        f"**精度トレンド**: {trend_icon} {summary.get('trend', '—')}",
        "",
    ]

    if entry.get("evaluated", 0) > 0:
        lines += [
            f"### 今週のサマリー（{entry.get('week','N/A')}）",
            "",
            f"| スコア帯 | 勝率 |",
            f"|---------|-----|",
            f"| score≥80 | {entry.get('win_rate_score80+', 0):.1%} |",
            f"| 60〜80   | {entry.get('win_rate_60_80', 0):.1%} |",
            f"| 40〜60   | {entry.get('win_rate_40_60', 0):.1%} |",
            f"| <40      | {entry.get('win_rate_sub40', 0):.1%} |",
            f"| **全体** | **{entry.get('win_rate_overall', 0):.1%}** |",
            "",
        ]

        miss_f = entry.get("miss_factors", [])
        if miss_f:
            lines.append("### ⚠️ 外れパターン分析（高スコア予測が外れた要因）")
            lines.append("")
            for mf in miss_f[:3]:
                lines.append(
                    f"  - **{mf['factor']}**: 発動{mf['total_fired']}件中"
                    f"{mf['miss_count']}件ミス（{mf['miss_rate']:.0%}）"
                    f"  平均α={mf['avg_alpha_when_fired']:+.2f}%")
            lines.append("")

        comp = entry.get("component_corrs", {})
        # ── Pearl / 潜伏Pearl バックテスト結果 ──────────
        pearl_stats  = entry.get("pearl_stats", {})
        latent_stats = entry.get("latent_pearl_stats", {})

        if pearl_stats or latent_stats:
            lines.append("### 💎 Pearl / 潜伏Pearl バックテスト（Wave 7）")
            lines.append("")

            any_pearl = any(
                (pearl_stats.get(t) or {}).get("n", 0) > 0
                for t in ["SS", "S", "A", "all_pearl"]
            )
            if any_pearl:
                lines.append("**Pearl Tier別的中率（α≥3%を的中と定義）:**")
                lines.append("")
                lines.append("| Tier | 件数 | 的中率 | 平均α |")
                lines.append("|------|------|-------|------|")
                for tier in ["SS", "S", "A", "all_pearl"]:
                    ps = pearl_stats.get(tier, {})
                    n  = ps.get("n", 0)
                    if n == 0:
                        continue
                    hr  = ps.get("hit_rate")
                    avg = ps.get("avg_alpha")
                    hr_str  = f"{hr:.1%}"   if hr  is not None else "—"
                    avg_str = f"{avg:+.2f}%" if avg is not None else "—"
                    tier_label = "**全Pearl**" if tier == "all_pearl" else tier
                    lines.append(f"| {tier_label} | {n} | {hr_str} | {avg_str} |")
                lines.append("")

            n_latent = latent_stats.get("n_latent", 0)
            if n_latent > 0:
                lp_hr  = latent_stats.get("hit_rate")
                lp_alp = latent_stats.get("avg_alpha")
                vs_non = latent_stats.get("vs_non_latent_alpha")
                hi_hr  = latent_stats.get("high_indep_hit_rate")
                cat_hr = latent_stats.get("catalyst_hit_rate")
                mt_hr  = latent_stats.get("midterm_ahead_hit_rate")

                lp_hr_str  = f"{lp_hr:.1%}"   if lp_hr  is not None else "—"
                lp_alp_str = f"{lp_alp:+.2f}%" if lp_alp is not None else "—"
                vs_str     = f"{vs_non:+.3f}%" if vs_non is not None else "—"

                lines.append(f"**潜伏Pearl（is_latent_pearl=True）: {n_latent}件**")
                lines.append("")
                lines.append(f"  - 的中率: **{lp_hr_str}** / 平均α: **{lp_alp_str}**")
                lines.append(f"  - 非潜伏との平均α差: **{vs_str}**")
                if hi_hr  is not None: lines.append(f"  - 高独立性（indep≥65）的中率: {hi_hr:.1%}")
                if cat_hr is not None: lines.append(f"  - カタリスト有り的中率: {cat_hr:.1%}")
                if mt_hr  is not None: lines.append(f"  - 中計ahead的中率: {mt_hr:.1%}")
                lines.append("")
                if   lp_hr is not None and lp_hr >= 0.55:
                    lines.append("  🎯 **潜伏Pearlモデルは有効**（的中率55%超）")
                elif lp_hr is not None and lp_hr <  0.40:
                    lines.append("  ⚠️ **潜伏Pearlモデル要改善**（的中率40%未満）")
                else:
                    lines.append("  ➡️ 潜伏Pearlモデルは標準的（40〜55%）")
                lines.append("")
        if comp:
            lines.append("### 📊 スコア成分 × リターン相関")
            lines.append("")
            for k, v in sorted(comp.items(), key=lambda x: -(x[1] or 0))[:4]:
                if v is not None:
                    lines.append(f"  - {k}: r={v:+.3f}")
            lines.append("")

    if summary.get("worst_factor") and summary["worst_factor"] != "N/A":
        lines += [
            f"**改善推奨**: `{summary['worst_factor']}` ファクターの"
            f"ミス率が {summary.get('worst_factor_miss_rate', 0):.0%} と高い。"
            f"`rule_optimizer.py` での重み調整を推奨。",
            "",
        ]

    lines += [
        "> このデータは llm_rule_generator.py と rule_optimizer.py の",
        "> 自動改善サイクルへの入力として使用されます。",
    ]
    return "\n".join(lines)


# ==============================================================
# BLOCK 8: メインエントリポイント
# ==============================================================

def run_self_evaluator(cfg) -> dict:
    """
    パフォーマンス自己検証を実行し performance_log.json を更新する。

    stock_analyzer_main.py から週次で呼び出す:
        _se = _load_local_module("self_evaluator", ...)
        se_result = _se.run_self_evaluator(cfg)
        results["self_evaluator"] = se_result

    Returns:
        {
          "latest_entry": dict,
          "summary":      dict,
          "report_md":    str,
          "updated":      bool,
          "timestamp":    str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🔍 self_evaluator 実行中...")

    history = _load_screener_history(cfg)
    if not history:
        print("    ⚠️  screener_history.json なし → スキップ")
        return {"skipped": True, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    records = build_evaluation_batch(history)
    if len(records) < MIN_SAMPLES:
        print(f"    ⚠️  有効レコード {len(records)}件 < {MIN_SAMPLES} → スキップ（蓄積中）")
        return {
            "skipped": True, "reason": f"レコード不足({len(records)}件)",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    print(f"    対象: {len(records)}レコード 出口価格取得中...")

    # リターン計算（ベンチマーク含む）
    evaluated   = []
    bm_rets     = []
    n_success   = 0

    # 1件ごとに yfinance を呼ぶと件数×2回になるため、銘柄ごとに期間全体を1回で取り、
    # 取得時点・出口・日経平均を同じデータから引く。
    from backtester import fetch_close_map, fill_prices, _price_window
    _start, _end = _price_window(records)
    _bench_t = BENCHMARK_TICKER
    _closes = fetch_close_map({r["ticker"] for r in records} | {_bench_t}, _start, _end)
    fill_prices(records, _closes, _closes.get(_bench_t, {}))

    for r in records:
        ep, xp = r.get("entry_price"), r.get("exit_price")
        ret = round((xp - ep) / ep * 100, 3) if ep and xp else None
        bm  = r.get("benchmark_return")
        bm_rets.append(bm or 0)
        if ret is None:
            continue
        n_success += 1
        r["return_pct"] = ret
        r["alpha"]      = round(ret - (bm or 0), 3)
        evaluated.append(r)
        time.sleep(FETCH_DELAY)

    if len(evaluated) < MIN_SAMPLES:
        print(f"    ⚠️  リターン取得成功 {len(evaluated)}件 < {MIN_SAMPLES}")
        return {"skipped": True, "reason": "リターン取得不足",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    avg_bm = sum(bm_rets) / len(bm_rets) if bm_rets else 0

    # 週次サマリー
    week_str = datetime.now().strftime("%Y-W%V")
    entry    = build_weekly_summary(evaluated, week_str, avg_bm)

    print(f"    勝率: {entry['win_rate_overall']:.1%}  "
          f"avg_α: {entry['avg_alpha']:+.2f}%  "
          f"score80+勝率: {entry['win_rate_score80+']: .1%}")

    if entry.get("miss_factors"):
        top_miss = entry["miss_factors"][0]
        print(f"    ⚠️  外れファクター: {top_miss['factor']} "
              f"({top_miss['miss_rate']:.0%} miss)")

    # ログ更新
    log = _load_perf_log(cfg)
    # 同週のエントリがあれば上書き、なければ追加
    existing_weeks = [e["week"] for e in log.get("log_entries", [])]
    if week_str in existing_weeks:
        idx = existing_weeks.index(week_str)
        log["log_entries"][idx] = entry
    else:
        log.setdefault("log_entries", []).append(entry)

    log = update_log_summary(log)
    _save_perf_log(cfg, log)

    print(f"    ✅ 自己検証完了: performance_log.json 更新 "
          f"({len(log['log_entries'])}週分蓄積)")

    report_md = format_self_eval_report(log, entry)

    # ── Pearl バックテスト本格評価 ──────────────────────
    # Pearl / 潜伏Pearl の的中率を pearl_backtest_log.json に蓄積する
    pearl_bt_result: dict = {}
    # Pearl 条件に当たらない場合も下の戻り値で参照されるため先に初期化する
    # （未定義だと UnboundLocalError）
    bt_log: dict = {}
    try:
        if getattr(cfg, "ENABLE_LATENT_PEARL_MANAGER", False) or \
           any(r.get("pearl_tier") for r in evaluated):

            # ① 当週の Pearl 統計
            pearl_stats  = entry.get("pearl_stats", {})
            latent_stats = entry.get("latent_pearl_stats", {})

            # ② pearl_backtest_log.json の読み込み・更新
            bt_log_path = (
                Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
                / PEARL_BACKTEST_FILENAME
            )
            bt_log: dict = {}
            if bt_log_path.exists():
                try:
                    bt_log = json.loads(bt_log_path.read_text(encoding="utf-8"))
                except Exception:
                    bt_log = {}
            bt_log.setdefault("entries", [])

            bt_entry = {
                "week":              week_str,
                "pearl_stats":       pearl_stats,
                "latent_pearl_stats": latent_stats,
                "n_latent_evaluated": latent_stats.get("n_latent", 0),
                "latent_hit_rate":   latent_stats.get("hit_rate"),
                "latent_avg_alpha":  latent_stats.get("avg_alpha"),
                "vs_non_latent":     latent_stats.get("vs_non_latent_alpha"),
                "high_indep_hr":     latent_stats.get("high_indep_hit_rate"),
                "catalyst_hr":       latent_stats.get("catalyst_hit_rate"),
                "midterm_ahead_hr":  latent_stats.get("midterm_ahead_hit_rate"),
                "timestamp":         datetime.now().isoformat(),
            }

            # 同週エントリは上書き
            existing_bt_weeks = [e.get("week") for e in bt_log["entries"]]
            if week_str in existing_bt_weeks:
                bt_log["entries"][existing_bt_weeks.index(week_str)] = bt_entry
            else:
                bt_log["entries"].append(bt_entry)

            # 最大エントリ数を超えた場合は古いものを削除
            if len(bt_log["entries"]) > PEARL_BACKTEST_MAX_ENTRY:
                bt_log["entries"] = bt_log["entries"][-PEARL_BACKTEST_MAX_ENTRY:]

            # ③ サマリー更新
            # $lifetime\\_latent\\_hr = avg(hit\\_rate_i)$ for all entries with n_latent >= MIN_SAMPLES
            valid_entries = [
                e for e in bt_log["entries"]
                if e.get("n_latent_evaluated", 0) >= MIN_SAMPLES
                and e.get("latent_hit_rate") is not None
            ]
            if valid_entries:
                hrs       = [e["latent_hit_rate"] for e in valid_entries]
                avg_alphas = [e["latent_avg_alpha"] for e in valid_entries if e.get("latent_avg_alpha") is not None]
                vs_nonlat  = [e["vs_non_latent"] for e in valid_entries if e.get("vs_non_latent") is not None]

                # トレンド判定: 直近4週 vs 直前4週
                recent  = hrs[-4:] if len(hrs) >= 4 else hrs
                earlier = hrs[-8:-4] if len(hrs) >= 8 else []
                if earlier:
                    trend = (
                        "improving" if sum(recent)/len(recent) > sum(earlier)/len(earlier) + 0.02
                        else "degrading" if sum(recent)/len(recent) < sum(earlier)/len(earlier) - 0.02
                        else "stable"
                    )
                else:
                    trend = "accumulating"

                bt_log["summary"] = {
                    "total_weeks_evaluated":  len(valid_entries),
                    "lifetime_latent_hr":     round(sum(hrs) / len(hrs), 4),
                    "avg_latent_alpha":       round(sum(avg_alphas) / len(avg_alphas), 3) if avg_alphas else None,
                    "avg_vs_non_latent":      round(sum(vs_nonlat) / len(vs_nonlat), 3) if vs_nonlat else None,
                    "trend":                  trend,
                    "last_updated":           datetime.now().strftime("%Y-%m-%d"),
                }

            bt_log_path.write_text(
                json.dumps(bt_log, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            pearl_bt_result = bt_entry
            hr_str = f"{latent_stats.get('hit_rate', 0):.1%}" if latent_stats.get("hit_rate") is not None else "N/A"
            alp_str = f"{latent_stats.get('avg_alpha', 0):+.2f}%" if latent_stats.get("avg_alpha") is not None else "N/A"
            print(
                f"    💎 Pearl BT: 潜伏={latent_stats.get('n_latent', 0)}件 "
                f"的中率={hr_str} avg_α={alp_str} "
                f"vs非潜伏α={latent_stats.get('vs_non_latent_alpha', 0):+.3f}%"
                if latent_stats.get("vs_non_latent_alpha") is not None
                else f"    💎 Pearl BT: 潜伏={latent_stats.get('n_latent', 0)}件 的中率={hr_str} avg_α={alp_str}"
            )

    except Exception as _bt_e:
        print(f"    ⚠️  Pearl BT 更新エラー（スキップ）: {str(_bt_e)[:80]}")

    return {
        "latest_entry": entry,
        "summary":      log["summary"],
        "log_entries":  log["log_entries"][-4:],   # 直近4週のみ返す（サイズ削減）
        "report_md":    report_md,
        "updated":      True,
        "pearl_backtest":  pearl_bt_result,
        "pearl_bt_summary": bt_log.get("summary", {}) if isinstance(bt_log, dict) else {},
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
