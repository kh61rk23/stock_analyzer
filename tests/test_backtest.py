# ==============================================================
# tests/test_backtest.py — S1統合バックテスト基盤の受入テスト（R1-1）
# ==============================================================
# 受入条件（要件定義書 §R1-1）:
#   ① bh_benchmark と現物累積リターンの一致（合成データで±0.5%以内を検証。
#      実1306はネットワーク必須のため cli --verify-benchmark で手動確認）
#   ② コストモデルの単体テスト（NISA/特定の税差・年内損益通算）
#   ③ screener_topN の walk-forward レポートが生成される
#   ④ pytest 全通過
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from backtest import metrics as M                     # noqa: E402
from backtest import validation as V                  # noqa: E402
from backtest.cost_model import (TaxLedger, dividend_net, fill_price,      # noqa: E402
                                 is_jp_ticker, short_carry_cost)
from backtest.data_feed import InMemoryFeed           # noqa: E402
from backtest.engine import Engine                    # noqa: E402
from backtest.options_model import (bs_price, check_margin, greeks,        # noqa: E402
                                    hv20, implied_vol_or_fallback)
from backtest.strategies import (BuyAndHoldBenchmark, PeadPlaybook,        # noqa: E402
                                 ScreenerTopN)
from backtest.strategy_api import Context, Order      # noqa: E402


# ── 合成データ生成 ──────────────────────────────────────────

def make_prices(ticker="1306.T", days=300, start_price=100.0,
                daily_ret=0.001, start_year=2025):
    """営業日近似（月-金相当で単純連番の日付・年またぎあり）の合成価格。"""
    rows, price = [], start_price
    y, m, d = start_year, 1, 1
    for i in range(days):
        d += 1
        if d > 28:
            d, m = 1, m + 1
            if m > 12:
                m, y = 1, y + 1
        o = price
        price = price * (1 + daily_ret)
        rows.append({"date": f"{y:04d}-{m:02d}-{d:02d}",
                     "open": round(o, 6), "high": round(max(o, price), 6),
                     "low": round(min(o, price), 6),
                     "close": round(price, 6), "volume": 1000})
    return {ticker: rows}


# ── コストモデル（受入条件②） ────────────────────────────────

class TestCostModel:
    def test_slippage_direction(self):
        # BUYは高く、SELLは安く約定（JP=20bps）
        assert fill_price(1000.0, "BUY", "7203.T") == pytest.approx(1002.0)
        assert fill_price(1000.0, "SELL", "7203.T") == pytest.approx(998.0)

    def test_slippage_us_10bps(self):
        assert fill_price(100.0, "BUY", "AAPL") == pytest.approx(100.10)
        assert is_jp_ticker("7203.T") and not is_jp_ticker("AAPL")

    def test_tax_nisa_vs_tokutei(self):
        led = TaxLedger()
        led.add_realized(2026, 100_000, "特定")
        led.add_realized(2026, 100_000, "NISA")     # NISAは通算にも入らない
        assert led.settle_year(2026) == pytest.approx(100_000 * 0.20315)

    def test_tax_yearly_offset(self):
        # 年内損益通算: +30万 と -10万 → 20万に課税
        led = TaxLedger()
        led.add_realized(2026, 300_000, "特定")
        led.add_realized(2026, -100_000, "特定")
        assert led.settle_year(2026) == pytest.approx(200_000 * 0.20315)

    def test_tax_no_negative(self):
        led = TaxLedger()
        led.add_realized(2026, -50_000, "特定")
        assert led.settle_year(2026) == 0.0

    def test_tax_no_double_settle(self):
        led = TaxLedger()
        led.add_realized(2026, 100_000, "特定")
        led.settle_year(2026)
        assert led.settle_year(2026) == 0.0

    def test_tax_cross_year_no_offset(self):
        # 年をまたぐ損失は通算されない（年内のみ）
        led = TaxLedger()
        led.add_realized(2025, -100_000, "特定")
        led.add_realized(2026, 100_000, "特定")
        assert led.settle_year(2025) == 0.0
        assert led.settle_year(2026) == pytest.approx(100_000 * 0.20315)

    def test_dividend_net(self):
        assert dividend_net(1000, "NISA") == 1000
        assert dividend_net(1000, "特定") == pytest.approx(1000 * (1 - 0.20315))

    def test_short_carry(self):
        # 100万の空売り1日 = 100万×(1.1%+4%)/365
        assert short_carry_cost(1_000_000, 1) == pytest.approx(
            1_000_000 * 0.051 / 365)


# ── エンジン（受入条件①相当・合成データ） ────────────────────

class TestEngine:
    def test_bh_benchmark_tracks_underlying(self):
        """買い持ちの最終リターンが現物累積リターンと±0.5%以内（NISA・配当なし）。"""
        prices = make_prices(days=250, daily_ret=0.0008)
        feed = InMemoryFeed(prices)
        res = Engine(feed, BuyAndHoldBenchmark("1306.T"),
                     1_000_000, "NISA").run()
        rows = prices["1306.T"]
        # エンジンは2日目始値で98%投入（スリッページ20bps込み）
        underlying = rows[-1]["close"] / rows[1]["open"] - 1
        engine_ret = res.final_equity / 1_000_000 - 1
        # 投下比率98%・スリッページ分を補正した理論値と比較
        expected = 0.98 / 1.002 * (1 + underlying) + 0.02 - 1
        assert abs(engine_ret - expected) < 0.005

    def test_adjusted_price_feed_does_not_double_count_dividends(self, tmp_path, monkeypatch):
        """auto_adjust済み価格ではDividendsをEngineへ重ねて渡さない。"""
        import pandas as pd
        from backtest.data_feed import YFinanceFeed

        index = pd.to_datetime(["2026-01-05", "2026-01-06"])
        history = pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [101.0, 102.0],
                "Low": [99.0, 100.0],
                "Close": [100.5, 101.5],
                "Volume": [1000, 1000],
                "Dividends": [5.0, 0.0],
            },
            index=index,
        )

        class FakeTicker:
            def history(self, **_kwargs):
                return history

        import yfinance as yf

        monkeypatch.setattr(yf, "Ticker", lambda _ticker: FakeTicker())
        feed = YFinanceFeed.__new__(YFinanceFeed)
        rows, dividends = feed._load(
            "1306.T", "2026-01-01", "2026-02-01", tmp_path
        )

        assert len(rows) == 2
        assert dividends == {}

    def test_golden_two_days(self):
        """2営業日ゴールデン: 手計算と完全一致。"""
        prices = {"7203.T": [
            {"date": "2026-01-05", "open": 1000, "high": 1010, "low": 990,
             "close": 1005, "volume": 1},
            {"date": "2026-01-06", "open": 1010, "high": 1020, "low": 1000,
             "close": 1015, "volume": 1},
        ]}

        class OneShot:
            strategy_id = "oneshot"
            universe = ["7203.T"]
            done = False

            def generate_orders(self, ctx):
                if self.done:
                    return []
                self.done = True
                return [Order(ticker="7203.T", side="BUY", qty=100)]

        res = Engine(InMemoryFeed(prices), OneShot(), 200_000, "NISA").run()
        # 1/6始値1010×1.002=1012.02 で100株 → cash=200000-101202=98798
        # equity = 98798 + 100×1015 = 200298
        assert res.final_equity == pytest.approx(200_298.0)
        assert res.equity_curve[-1]["cash"] == pytest.approx(98_798.0)

    def test_sell_realizes_tax(self):
        """特定口座: 売却益に20.315%（最終日に控除）。"""
        prices = {"7203.T": [
            {"date": "2026-01-05", "open": 1000, "high": 1, "low": 1, "close": 1000, "volume": 1},
            {"date": "2026-01-06", "open": 1000, "high": 1, "low": 1, "close": 1000, "volume": 1},
            {"date": "2026-01-07", "open": 1200, "high": 1, "low": 1, "close": 1200, "volume": 1},
        ]}

        class BuyThenSell:
            strategy_id = "bts"
            universe = ["7203.T"]

            def generate_orders(self, ctx):
                if ctx.date == "2026-01-05":
                    return [Order(ticker="7203.T", side="BUY", qty=100)]
                if ctx.date == "2026-01-06":
                    return [Order(ticker="7203.T", side="SELL", qty=100)]
                return []

        res = Engine(InMemoryFeed(prices), BuyThenSell(), 200_000, "特定").run()
        buy_px, sell_px = 1000 * 1.002, 1200 * 0.998
        pnl = (sell_px - buy_px) * 100
        assert res.taxes_paid == pytest.approx(pnl * 0.20315, rel=1e-6)

    def test_no_short_when_disallowed(self):
        prices = make_prices("7203.T", days=5)

        class SellOnly:
            strategy_id = "sellonly"
            universe = ["7203.T"]

            def generate_orders(self, ctx):
                return [Order(ticker="7203.T", side="SELL", qty=100)]

        res = Engine(InMemoryFeed(prices), SellOnly(), 100_000, "NISA",
                     allow_short=False).run()
        assert res.trades == []          # 保有ゼロのSELLは全て切詰め→非約定

    def test_event_from_dividend(self):
        prices = {"1306.T": [
            {"date": "2026-01-05", "open": 100, "high": 1, "low": 1, "close": 100, "volume": 1},
            {"date": "2026-01-06", "open": 100, "high": 1, "low": 1, "close": 100, "volume": 1},
            {"date": "2026-01-07", "open": 100, "high": 1, "low": 1, "close": 100, "volume": 1},
        ]}
        divs = {"1306.T": {"2026-01-07": 5.0}}

        class BuyOnce:
            strategy_id = "b1"
            universe = ["1306.T"]
            done = False

            def generate_orders(self, ctx):
                if self.done:
                    return []
                self.done = True
                return [Order(ticker="1306.T", side="BUY", qty=10)]

        res = Engine(InMemoryFeed(prices, dividends=divs), BuyOnce(),
                     10_000, "NISA").run()
        # 配当 5円×10株 = 50円 がNISA非課税で加算されている
        assert any(abs(p["cash"] - (10_000 - 10 * 100 * 1.002 + 50)) < 1e-6
                   for p in res.equity_curve)


# ── 戦略・validation（受入条件③） ───────────────────────────

class TestStrategiesAndValidation:
    def _sector_feed(self, days=300):
        prices = {}
        prices.update(make_prices("A.T", days, 100, 0.002))   # 強い
        prices.update(make_prices("B.T", days, 100, 0.0))     # 横ばい
        prices.update(make_prices("1306.T", days, 100, 0.0005))
        dates = [r["date"] for r in prices["A.T"]]
        signals = {dates[0]: {"score_history": {"10": {"score": 80},
                                                "20": {"score": 40}}}}
        return InMemoryFeed(prices, signals), dates

    def test_screener_topn_walkforward_report(self, tmp_path):
        feed, dates = self._sector_feed()
        strat = ScreenerTopN({"10": ["A.T"], "20": ["B.T"]}, top_n=1)
        res = Engine(feed, strat, 1_000_000, "NISA").run()
        bench = Engine(feed, BuyAndHoldBenchmark("1306.T"), 1_000_000,
                       "NISA").run()
        val = V.validate(res, bench, feed.calendar())
        assert len(val["folds"]) == 4                    # BT_WF_SPLITS=4
        assert all("oos_return" in f for f in val["folds"])
        assert 0.0 <= val["bootstrap_p"] <= 1.0
        # レポートMD生成（cli.render_report を直接検証）
        from backtest.cli import render_report
        md = render_report("screener_topN", M.summarize(res), val,
                           dates[0], dates[-1])
        out = tmp_path / "report.md"
        out.write_text(md, encoding="utf-8")
        assert "walk-forward" in md and "bootstrap" in md
        assert out.exists()

    def test_bootstrap_detects_positive_alpha(self):
        # 明確に正の超過リターン → p小 / ゼロ近傍ノイズ → p大
        import random
        rng = random.Random(0)
        pos = [(f"d{i}", 0.001 + rng.gauss(0, 0.001)) for i in range(200)]
        zero = [(f"d{i}", rng.gauss(0, 0.002)) for i in range(200)]
        bench = [(f"d{i}", 0.0) for i in range(200)]
        p_pos = V.circular_block_bootstrap_pvalue(
            V.excess_returns(pos, bench))
        p_zero = V.circular_block_bootstrap_pvalue(
            V.excess_returns(zero, bench))
        assert p_pos < 0.10
        assert p_zero > p_pos

    def test_pead_playbook_entry_exit(self):
        prices = make_prices("X.T", days=60, daily_ret=0.005)  # 急騰→TP到達
        dates = [r["date"] for r in prices["X.T"]]
        signals = {dates[0]: {"pead": [{"ticker": "X.T", "surprise_pct": 8}]}}
        feed = InMemoryFeed(prices, signals)
        strat = PeadPlaybook()
        res = Engine(feed, strat, 1_000_000, "NISA").run()
        sides = [t["side"] for t in res.trades]
        assert "BUY" in sides and "SELL" in sides        # entry→TP exit
        closed = [t for t in res.trades if t["pnl"] is not None]
        assert closed and closed[0]["pnl"] > 0

    def test_walk_forward_split_boundaries(self):
        cal = [f"2026-{m:02d}-{d:02d}" for m in range(1, 11) for d in range(1, 11)]
        folds = V.split_walk_forward(cal, 4)
        assert len(folds) == 4
        assert folds[0]["oos_start"] > cal[0]            # 先頭はウォームアップ
        assert folds[-1]["oos_end"] == cal[-1]
        for i in range(1, 4):                            # OOSは連続・重複なし
            assert folds[i]["oos_start"] > folds[i - 1]["oos_end"]


# ── オプションモデル ─────────────────────────────────────────

class TestOptionsModel:
    def test_bs_put_call_parity(self):
        S, K, T, r, sig = 100, 100, 0.25, 0.01, 0.3
        c = bs_price(S, K, T, r, sig, "call")
        p = bs_price(S, K, T, r, sig, "put")
        import math
        assert c - p == pytest.approx(S - K * math.exp(-r * T), abs=1e-9)

    def test_greeks_signs(self):
        g_call = greeks(100, 100, 0.25, 0.01, 0.3, "call")
        g_put = greeks(100, 100, 0.25, 0.01, 0.3, "put")
        assert 0 < g_call["delta"] < 1
        assert -1 < g_put["delta"] < 0
        assert g_call["gamma"] > 0 and g_call["vega"] > 0
        assert g_call["theta"] < 0

    def test_iv_fallback_hv20(self):
        closes = [100 * (1.01 ** i) for i in range(30)]
        iv = implied_vol_or_fallback(None, closes)
        assert iv == pytest.approx(hv20(closes) * 1.1)
        assert implied_vol_or_fallback(0.5, closes) == 0.5

    def test_naked_sell_raises(self):
        with pytest.raises(ValueError):                  # 裸プット売り
            check_margin("SELL", "put", 1, 100, cash=5_000)
        with pytest.raises(ValueError):                  # 裸コール売り
            check_margin("SELL", "call", 1, 100, cash=1e9, underlying_qty=0)
        # カバードコール/CSPはOK
        check_margin("SELL", "put", 1, 100, cash=10_000)
        check_margin("SELL", "call", 1, 100, cash=0, underlying_qty=100)


# ── metrics ──────────────────────────────────────────────────

class TestMetrics:
    def test_max_drawdown(self):
        curve = [{"date": "d", "equity": e, "cash": 0, "invested": e}
                 for e in [100, 120, 90, 110, 130]]
        assert M.max_drawdown(curve) == pytest.approx(0.25)   # 120→90

    def test_trade_stats_pf(self):
        trades = [{"pnl": 100}, {"pnl": 50}, {"pnl": -50}, {"pnl": None}]
        ts = M.trade_stats(trades)
        assert ts["win_rate"] == pytest.approx(2 / 3)
        assert ts["profit_factor"] == pytest.approx(3.0)
