# ==============================================================
# backtest/engine.py — イベントドリブン日次エンジン（Wave R1-1 / S1）
# ==============================================================
# 要件定義書 §R1-1 の日次ループ:
#   for date in calendar:
#     1) 前日発注分を当日始値で約定（共通スキーマ②）
#     2) ポジション評価・equity curve更新（配当・空売りコスト含む）
#     3) strategy.generate_orders(ctx) → 翌日分の注文キューへ
# 税は TaxLedger で年内損益通算し、年末（と最終日）にcashから控除。
# ==============================================================

from __future__ import annotations

import math

from .cost_model import (TaxLedger, commission, dividend_net, fill_price,
                         short_carry_cost)
from .strategy_api import Context, Order, Position


class BacktestResult:
    def __init__(self):
        self.equity_curve: list = []   # [{date, equity, cash, invested}]
        self.trades: list = []         # [{date, ticker, side, qty, price, pnl}]
        self.daily_returns: list = []  # [(date, ret)]
        self.taxes_paid: float = 0.0

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1]["equity"] if self.equity_curve else 0.0


class Engine:
    """
    1戦略×1口座のバックテストエンジン。

    Args:
        feed:          data_feed.InMemoryFeed / YFinanceFeed
        strategy:      strategy_api.Strategy 実装
        initial_cash:  初期資金
        account_type:  "特定" | "NISA"（税計算）
        allow_short:   False（既定）なら保有超のSELLは保有数に切詰め
        cfg:           config（コスト定数の参照元・省略時は既定値）
    """

    def __init__(self, feed, strategy, initial_cash: float = 2_000_000,
                 account_type: str = "特定", allow_short: bool = False,
                 cfg=None, history_days: int = 60):
        self.feed = feed
        self.strategy = strategy
        self.cash = float(initial_cash)
        self.account_type = account_type
        self.allow_short = allow_short
        self.cfg = cfg
        self.history_days = history_days
        self.positions: dict[str, Position] = {}
        self.pending: list[Order] = []
        self.tax = TaxLedger(cfg)
        self.result = BacktestResult()

    # ── 約定処理 ─────────────────────────────────────────────

    def _execute(self, order: Order, date: str) -> None:
        bar = self.feed.bar(order.ticker, date)
        if not bar:
            return                                   # 当日データなし→注文失効
        qty = order.qty
        if qty is None:                              # weight指定 → qty換算
            ref = bar["open"]
            qty = math.floor(max(0.0, order.weight) * self._equity(date) / ref) if ref > 0 else 0
        if qty <= 0:
            return
        price = fill_price(bar["open"], order.side, order.ticker, self.cfg)
        pos = self.positions.get(order.ticker)

        if order.side == "BUY":
            fee = commission(price * qty, self.cfg)
            affordable = math.floor((self.cash - fee) / price) if price > 0 else 0
            qty = min(qty, max(0, affordable))
            if qty <= 0:
                return
            cost = price * qty + commission(price * qty, self.cfg)
            self.cash -= cost
            if pos and pos.qty >= 0:
                total = pos.qty + qty
                pos.avg_cost = (pos.avg_cost * pos.qty + price * qty) / total
                pos.qty = total
            elif pos and pos.qty < 0:               # ショート買戻し
                cover = min(qty, -pos.qty)
                pnl = (pos.avg_cost - price) * cover
                self.tax.add_realized(int(date[:4]), pnl, self.account_type)
                self.result.trades.append({"date": date, "ticker": order.ticker,
                                           "side": "BUY", "qty": cover,
                                           "price": round(price, 4),
                                           "pnl": round(pnl, 2)})
                pos.qty += cover
                if pos.qty == 0:
                    del self.positions[order.ticker]
                return
            else:
                self.positions[order.ticker] = Position(
                    order.ticker, qty, price, self.account_type)
            self.result.trades.append({"date": date, "ticker": order.ticker,
                                       "side": "BUY", "qty": qty,
                                       "price": round(price, 4), "pnl": None})

        else:  # SELL
            held = pos.qty if pos else 0
            if not self.allow_short:
                qty = min(qty, max(0, held))
                if qty <= 0:
                    return
            proceeds = price * qty - commission(price * qty, self.cfg)
            self.cash += proceeds
            if held > 0:                             # ロング決済
                closed = min(qty, held)
                pnl = (price - pos.avg_cost) * closed
                self.tax.add_realized(int(date[:4]), pnl, self.account_type)
                self.result.trades.append({"date": date, "ticker": order.ticker,
                                           "side": "SELL", "qty": closed,
                                           "price": round(price, 4),
                                           "pnl": round(pnl, 2)})
                pos.qty -= closed
                remainder = qty - closed
                if pos.qty == 0:
                    del self.positions[order.ticker]
                if remainder > 0 and self.allow_short:
                    self.positions[order.ticker] = Position(
                        order.ticker, -remainder, price, self.account_type)
            else:                                    # 新規ショート
                if pos:
                    total = -pos.qty + qty
                    pos.avg_cost = (pos.avg_cost * (-pos.qty) + price * qty) / total
                    pos.qty = -total
                else:
                    self.positions[order.ticker] = Position(
                        order.ticker, -qty, price, self.account_type)
                self.result.trades.append({"date": date, "ticker": order.ticker,
                                           "side": "SELL", "qty": qty,
                                           "price": round(price, 4), "pnl": None})

    # ── 評価 ────────────────────────────────────────────────

    def _mark_price(self, ticker: str, date: str) -> float:
        bar = self.feed.bar(ticker, date)
        if bar:
            return bar["close"]
        hist = self.feed.history(ticker, date, 1)
        return hist[-1]["close"] if hist else 0.0

    def _invested(self, date: str) -> float:
        return sum(self._mark_price(t, date) * p.qty
                   for t, p in self.positions.items())

    def _equity(self, date: str) -> float:
        return self.cash + self._invested(date)

    # ── メインループ ─────────────────────────────────────────

    def run(self, start: str = None, end: str = None) -> BacktestResult:
        cal = [d for d in self.feed.calendar()
               if (start is None or d >= start) and (end is None or d <= end)]
        prev_equity, prev_year = None, None

        for date in cal:
            year = int(date[:4])
            if prev_year is not None and year != prev_year:
                tax = self.tax.settle_year(prev_year)    # 年末課税（損益通算後）
                self.cash -= tax
                self.result.taxes_paid += tax
            prev_year = year

            # 1) 前日発注分を当日始値で約定
            for order in self.pending:
                self._execute(order, date)
            self.pending = []

            # 2) 配当・空売りコスト・評価
            for t, p in list(self.positions.items()):
                div = self.feed.dividend(t, date)
                if div > 0 and p.qty > 0:
                    self.cash += dividend_net(div * p.qty, p.account_type, self.cfg)
                if p.is_short:
                    self.cash -= short_carry_cost(
                        self._mark_price(t, date) * (-p.qty), 1, self.cfg)

            equity = self._equity(date)
            invested = self._invested(date)
            self.result.equity_curve.append({"date": date,
                                             "equity": round(equity, 2),
                                             "cash": round(self.cash, 2),
                                             "invested": round(invested, 2)})
            if prev_equity is not None and prev_equity > 0:
                self.result.daily_returns.append(
                    (date, equity / prev_equity - 1.0))
            prev_equity = equity

            # 3) 戦略呼び出し → 翌日キュー
            ctx = Context(
                date=date,
                prices={t: self.feed.history(t, date, self.history_days)
                        for t in self._universe()},
                signals=self.feed.signals_at(date),
                positions=dict(self.positions),
                cash=self.cash,
                equity=equity,
            )
            try:
                self.pending = list(self.strategy.generate_orders(ctx) or [])
            except Exception:
                self.pending = []                    # 戦略例外で全体を止めない

        if prev_year is not None:
            tax = self.tax.settle_year(prev_year)      # 最終年の課税
            self.cash -= tax
            self.result.taxes_paid += tax
            if self.result.equity_curve:
                last = self.result.equity_curve[-1]
                last["equity"] = round(last["equity"] - tax, 2)
                last["cash"] = round(last["cash"] - tax, 2)
        return self.result

    def _universe(self) -> list:
        uni = set(self.positions)
        uni.update(getattr(self.strategy, "universe", []) or [])
        return sorted(uni)
