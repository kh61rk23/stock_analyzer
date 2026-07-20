# ==============================================================
# backtest/strategies.py — 参照戦略3本（Wave R1-1 / S1）
# ==============================================================
# 要件定義書 §R1-1「初期実装する参照戦略3本」:
#   bh_benchmark  : TOPIX ETF 1306 買い持ち（受入テスト用）
#   screener_topN : composite_score 上位N=5・月次リバランス
#   pead_playbook : S5（イベントドリブン）の雛形
# いずれも strategy_api.Strategy 契約を実装（S2ペーパーと共用可能）。
# ==============================================================

from __future__ import annotations

from .strategy_api import Context, Order


class BuyAndHoldBenchmark:
    """初日に全力買い→保有し続ける。受入条件①の照合対象。"""
    strategy_id = "bh_benchmark"

    def __init__(self, ticker: str = "1306.T"):
        self.ticker = ticker
        self.universe = [ticker]
        self._bought = False

    def generate_orders(self, ctx: Context) -> list:
        if self._bought or ctx.positions.get(self.ticker):
            return []
        self._bought = True
        return [Order(ticker=self.ticker, side="BUY", weight=0.98,
                      tag="bh_initial")]        # 2%はスリッページ・端数バッファ


class ScreenerTopN:
    """
    composite_score 上位N業種の代表銘柄を等ウェイト保有・月次リバランス。

    signals["score_history"] = {sector_code: {"score": float, ...}}
    （data_feed が score_history.json の当日以前最新スナップショットを供給）。
    業種→銘柄の変換は sector_map（config.SECTOR_REPRESENTATIVE_TICKERS 相当:
    {sector_code: [ticker, ...]} の先頭銘柄）で行う。
    """
    strategy_id = "screener_topN"

    def __init__(self, sector_map: dict, top_n: int = 5):
        self.sector_map = {k: v for k, v in (sector_map or {}).items() if v}
        self.top_n = top_n
        self.universe = sorted({v[0] for v in self.sector_map.values()})
        self._last_rebalance_month = None

    def _targets(self, ctx: Context) -> list:
        scores = ctx.signals.get("score_history") or {}
        ranked = sorted(
            ((code, (rec.get("score") if isinstance(rec, dict) else rec) or 0)
             for code, rec in scores.items() if code in self.sector_map),
            key=lambda x: -x[1])
        return [self.sector_map[code][0] for code, _ in ranked[: self.top_n]]

    def generate_orders(self, ctx: Context) -> list:
        month = ctx.date[:7]
        if month == self._last_rebalance_month:
            return []
        targets = self._targets(ctx)
        if not targets:
            return []
        self._last_rebalance_month = month
        orders = []
        for t, pos in ctx.positions.items():        # 対象外は全売却
            if t not in targets and pos.qty > 0:
                orders.append(Order(ticker=t, side="SELL", qty=pos.qty,
                                    tag="rebalance_exit"))
        w = 0.95 / len(targets)                      # 5%は現金バッファ
        for t in targets:
            if t not in ctx.positions:
                orders.append(Order(ticker=t, side="BUY", weight=w,
                                    tag="rebalance_entry"))
        return orders


class PeadPlaybook:
    """
    S5プレイブックの雛形（pead_long_v1 相当）:
      entry: signals["pead"] の surprise_pct >= 5 → 翌日始値で買い
      exit : +15%利確 / -7%損切り / 40営業日で手仕舞い
      同時保有上限 3
    """
    strategy_id = "pead_playbook"

    def __init__(self, surprise_min: float = 5.0, take_profit: float = 0.15,
                 stop_loss: float = -0.07, hold_days_max: int = 40,
                 max_concurrent: int = 3, size_weight: float = 0.05):
        self.surprise_min = surprise_min
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.hold_days_max = hold_days_max
        self.max_concurrent = max_concurrent
        self.size_weight = size_weight
        self.universe: list = []
        self._entry: dict = {}          # {ticker: {"date": str, "days": int}}

    def generate_orders(self, ctx: Context) -> list:
        orders = []
        # ── exit判定 ──
        for t, pos in ctx.positions.items():
            if pos.qty <= 0:
                continue
            info = self._entry.setdefault(t, {"date": ctx.date, "days": 0})
            info["days"] += 1
            price = ctx.last_close(t)
            if price is None or pos.avg_cost <= 0:
                continue
            ret = price / pos.avg_cost - 1
            if (ret >= self.take_profit or ret <= self.stop_loss
                    or info["days"] >= self.hold_days_max):
                orders.append(Order(ticker=t, side="SELL", qty=pos.qty,
                                    tag=f"pead_exit({ret:+.1%},{info['days']}d)"))
                self._entry.pop(t, None)
        # ── entry判定 ──
        held = sum(1 for p in ctx.positions.values() if p.qty > 0)
        slots = self.max_concurrent - held
        for ev in (ctx.signals.get("pead") or []):
            if slots <= 0:
                break
            t = ev.get("ticker")
            if not t or t in ctx.positions:
                continue
            if float(ev.get("surprise_pct", 0)) >= self.surprise_min:
                if t not in self.universe:
                    self.universe.append(t)
                orders.append(Order(ticker=t, side="BUY",
                                    weight=self.size_weight, tag="pead_entry"))
                self._entry[t] = {"date": ctx.date, "days": 0}
                slots -= 1
        return orders
