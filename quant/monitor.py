# ==============================================================
# quant/monitor.py — 採用後の実績の見張り
# ==============================================================
# 【役割】採用した戦略の実績（採用後の日次損益）が、検証のときの成績から見て「ありそうな範囲」かを毎日調べ、
#   外れていたら停止（suspend）を返す。純関数。
# 【判定】
#   1. 実績の日数が QUANT_MONITOR_MIN_DAYS 未満なら insufficient（まだ判断しない）。
#   2. 実績の累積が、検証の日次損益を実績と同じ日数で再標本（定常ブートストラップ）した累積の
#      QUANT_MONITOR_Q 分位より低ければ suspend（検証ならまず起きない負け方）。
#   3. 実績の最大の下げが、検証の最大の下げ×QUANT_MONITOR_DD_MULT を超えたら suspend。
#   両方に当てはまれば理由を両方書く。乱数は QUANT_BOOT_SEED で固定。
# ==============================================================
from __future__ import annotations

import numpy as np
import pandas as pd

from quant import settings as S
from quant import stats


def _clean(s) -> pd.Series:
    return pd.Series(s, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()


def check(track, candidate_oos, cfg) -> dict:
    """戻りは {"state": "ok"|"suspend"|"insufficient", "reason", "live_days", "live_return", "expected_q05"}。

    track＝採用後の実績の日次損益、candidate_oos＝検証区間の日次損益（どちらも Series）。
    live_return は実績の複利の累積、expected_q05 は検証を同じ日数で再標本した累積の下側の分位（QUANT_MONITOR_Q）。
    """
    live = _clean(track)
    oos = _clean(candidate_oos)
    n = int(live.size)
    live_return = float(np.prod(1.0 + live.to_numpy()) - 1.0) if n else None
    min_days = int(S.get(cfg, "QUANT_MONITOR_MIN_DAYS"))
    if n < min_days:
        return {"state": "insufficient", "reason": f"実績が{n}日で、判断に必要な{min_days}日に届かない",
                "live_days": n, "live_return": live_return, "expected_q05": None}
    if oos.size < 2:
        raise ValueError(f"monitor.check: 検証の損益が少なすぎて比べられない（{oos.size}日）")

    q = float(S.get(cfg, "QUANT_MONITOR_Q"))
    paths = stats.stationary_bootstrap(
        oos.to_numpy(), int(S.get(cfg, "QUANT_BOOT_N")), float(S.get(cfg, "QUANT_BOOT_MEAN_BLOCK")),
        int(S.get(cfg, "QUANT_BOOT_SEED")), length=n)
    cum = np.prod(1.0 + paths, axis=1) - 1.0
    expected = float(np.quantile(cum, q))

    reasons = []
    if live_return < expected:
        reasons.append(f"実績の累積 {live_return * 100:.1f}% が、検証なら{(1 - q) * 100:.0f}%の確率で上回る水準 "
                       f"{expected * 100:.1f}% を下回っている")
    live_dd = stats.max_drawdown(live.to_numpy())
    oos_dd = stats.max_drawdown(oos.to_numpy())
    mult = float(S.get(cfg, "QUANT_MONITOR_DD_MULT"))
    if live_dd > oos_dd * mult:
        reasons.append(f"実績の最大の下げ {live_dd * 100:.1f}% が、検証の最大の下げ {oos_dd * 100:.1f}% の{mult:g}倍を超えている")
    if reasons:
        return {"state": "suspend", "reason": "。".join(reasons), "live_days": n,
                "live_return": live_return, "expected_q05": expected}
    return {"state": "ok", "reason": "実績は検証から見て想定の範囲内", "live_days": n,
            "live_return": live_return, "expected_q05": expected}
