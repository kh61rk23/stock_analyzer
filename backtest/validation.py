# ==============================================================
# backtest/validation.py — 過剰適合防止（Wave R1-1 / S1）
# ==============================================================
# 確定仕様（要件定義書 §R1-1・変更禁止）:
#   - walk-forward: expanding window・分割数 BT_WF_SPLITS=4。
#     各foldのOOS成績を個別表示（IS成績だけの表示は禁止）
#   - 有意性: OOS日次超過リターン（対ベンチマーク）に
#     circular block bootstrap（ブロック長20日・B=500）で
#     「平均超過リターン>0」の片側p値を算出
#   - p>=0.10 → レポートに『統計的裏付けなし』と朱記
# 乱数は seed 固定可能（テスト再現性）。
# ==============================================================

from __future__ import annotations

import random

from . import metrics as M


def split_walk_forward(calendar: list, n_splits: int = 4) -> list:
    """
    expanding window の fold 境界を返す。
    カレンダーを (n_splits+1) 等分し、先頭1区画をウォームアップ（IS最小区間）、
    残り n_splits 区画を順に OOS とする。
    Returns: [{"fold": i, "is_end": date, "oos_start": date, "oos_end": date}]
    """
    n = len(calendar)
    if n < (n_splits + 1) * 2:
        return []
    seg = n // (n_splits + 1)
    folds = []
    for i in range(n_splits):
        oos_s = seg * (i + 1)
        oos_e = seg * (i + 2) - 1 if i < n_splits - 1 else n - 1
        folds.append({"fold": i + 1,
                      "is_end": calendar[oos_s - 1],
                      "oos_start": calendar[oos_s],
                      "oos_end": calendar[oos_e]})
    return folds


def fold_oos_metrics(result, folds: list) -> list:
    """全期間の実行結果から fold 別 OOS 成績を切り出す。"""
    out = []
    for f in folds:
        curve = [p for p in result.equity_curve
                 if f["oos_start"] <= p["date"] <= f["oos_end"]]
        rets = [(d, r) for d, r in result.daily_returns
                if f["oos_start"] <= d <= f["oos_end"]]
        if not curve:
            continue
        total_ret = (curve[-1]["equity"] / curve[0]["equity"] - 1
                     if curve[0]["equity"] > 0 else 0.0)
        out.append({**f,
                    "oos_return": round(total_ret, 4),
                    "oos_sharpe": round(M.sharpe(rets), 3),
                    "oos_max_dd": round(M.max_drawdown(curve), 4),
                    "n_days": len(curve)})
    return out


def excess_returns(strategy_returns: list, benchmark_returns: list) -> list:
    """日付で突合した日次超過リターン（対ベンチマーク）。"""
    bench = dict(benchmark_returns)
    return [r - bench[d] for d, r in strategy_returns if d in bench]


def circular_block_bootstrap_pvalue(excess: list, block: int = 20,
                                    n_boot: int = 500,
                                    seed: int = 42) -> float:
    """
    circular block bootstrap による「平均超過リターン > 0」の片側p値。
    p = ブートストラップ平均が0以下となった割合。
    サンプルが block 長未満なら 1.0（判定不能=裏付けなし扱い）。
    """
    n = len(excess)
    if n < block or n < 2:
        return 1.0
    rng = random.Random(seed)
    n_blocks = (n + block - 1) // block
    count_le0 = 0
    for _ in range(n_boot):
        sample = []
        for _ in range(n_blocks):
            start = rng.randrange(n)
            sample.extend(excess[(start + j) % n] for j in range(block))
        mean = sum(sample[:n]) / n
        if mean <= 0:
            count_le0 += 1
    return count_le0 / n_boot


def validate(result, benchmark_result, calendar: list, cfg=None) -> dict:
    """walk-forward + bootstrap をまとめて実行し検証サマリを返す。"""
    n_splits = getattr(cfg, "BT_WF_SPLITS", 4)
    block = getattr(cfg, "BT_BOOTSTRAP_BLOCK", 20)
    n_boot = getattr(cfg, "BT_BOOTSTRAP_N", 500)
    p_th = getattr(cfg, "BT_P_THRESHOLD", 0.10)

    folds = split_walk_forward(calendar, n_splits)
    fold_stats = fold_oos_metrics(result, folds)

    oos_start = folds[0]["oos_start"] if folds else None
    strat_oos = [(d, r) for d, r in result.daily_returns
                 if oos_start is None or d >= oos_start]
    bench_oos = [(d, r) for d, r in benchmark_result.daily_returns
                 if oos_start is None or d >= oos_start]
    excess = excess_returns(strat_oos, bench_oos)
    p_value = circular_block_bootstrap_pvalue(excess, block, n_boot)

    return {
        "folds": fold_stats,
        "bootstrap_p": round(p_value, 4),
        "statistically_supported": p_value < p_th,
        "n_oos_days": len(excess),
    }
