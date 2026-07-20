# ==============================================================
# backtest/metrics.py — 成績指標（Wave R1-1 / S1）
# ==============================================================
# レポート必須項目（要件定義書 §R1-1 validation）:
#   CAGR / 年率ボラ / Sharpe(rf=0) / 最大DD / 勝率 / Profit Factor /
#   取引数 / 期間エクスポージャ
# 純粋関数のみ（I/Oなし）。
# ==============================================================

from __future__ import annotations

import math

TRADING_DAYS = 252


def cagr(equity_curve: list) -> float:
    if len(equity_curve) < 2:
        return 0.0
    start, end = equity_curve[0]["equity"], equity_curve[-1]["equity"]
    if start <= 0 or end <= 0:
        return 0.0
    years = len(equity_curve) / TRADING_DAYS
    return (end / start) ** (1 / years) - 1 if years > 0 else 0.0


def annual_volatility(daily_returns: list) -> float:
    rets = [r for _, r in daily_returns]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def sharpe(daily_returns: list) -> float:
    """rf=0 の年率Sharpe。"""
    rets = [r for _, r in daily_returns]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    vol = annual_volatility(daily_returns)
    return (mean * TRADING_DAYS) / vol if vol > 0 else 0.0


def max_drawdown(equity_curve: list) -> float:
    peak, mdd = 0.0, 0.0
    for p in equity_curve:
        e = p["equity"]
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, 1 - e / peak)
    return mdd


def trade_stats(trades: list) -> dict:
    """クローズ取引（pnl記録あり）から勝率・PF・取引数を計算。"""
    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [t["pnl"] for t in closed if t["pnl"] < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "n_trades": len(trades),
        "n_closed": len(closed),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0
                         else (float("inf") if gross_win > 0 else 0.0),
    }


def exposure(equity_curve: list) -> float:
    """期間エクスポージャ = 平均(投資額/資産)。"""
    vals = [abs(p["invested"]) / p["equity"]
            for p in equity_curve if p["equity"] > 0]
    return sum(vals) / len(vals) if vals else 0.0


def summarize(result) -> dict:
    """BacktestResult → 指標一式の辞書。"""
    ts = trade_stats(result.trades)
    return {
        "cagr": round(cagr(result.equity_curve), 4),
        "annual_vol": round(annual_volatility(result.daily_returns), 4),
        "sharpe": round(sharpe(result.daily_returns), 3),
        "max_drawdown": round(max_drawdown(result.equity_curve), 4),
        "win_rate": round(ts["win_rate"], 3),
        "profit_factor": (round(ts["profit_factor"], 3)
                          if ts["profit_factor"] != float("inf") else "inf"),
        "n_trades": ts["n_trades"],
        "n_closed": ts["n_closed"],
        "exposure": round(exposure(result.equity_curve), 3),
        "taxes_paid": round(result.taxes_paid, 2),
        "final_equity": round(result.final_equity, 2),
    }
