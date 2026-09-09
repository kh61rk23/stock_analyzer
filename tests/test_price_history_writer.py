# price_history_writer — yf_cache が集めた日足の BQ 永続化
from datetime import date, timedelta

import pandas as pd
import pytest

import price_history_writer as w

TODAY = date.today()


def _df(days=5, close_base=100.0, with_volume=True):
    idx = pd.to_datetime([TODAY - timedelta(days=i) for i in range(days)][::-1])
    data = {
        "Open":  [close_base + i for i in range(days)],
        "High":  [close_base + i + 2 for i in range(days)],
        "Low":   [close_base + i - 2 for i in range(days)],
        "Close": [close_base + i + 1 for i in range(days)],
    }
    if with_volume:
        data["Volume"] = [1_000_000 + i for i in range(days)]
    return pd.DataFrame(data, index=idx)


class TestBuildRows:
    def test_maps_ohlcv_columns(self):
        rows = w.build_price_rows({"6501.T": _df(1)}, TODAY - timedelta(days=7))
        assert len(rows) == 1
        r = rows[0]
        assert r["ticker"] == "6501.T"
        assert r["trade_date"] == TODAY.isoformat()
        assert r["open"] == 100.0 and r["close"] == 101.0
        assert r["high"] == 102.0 and r["low"] == 98.0
        assert r["volume"] == 1_000_000
        assert r["source"] == "yfinance"

    def test_rows_older_than_since_are_dropped(self):
        """全期間を毎回入れるとテーブルが肥大するため直近だけ拾う"""
        rows = w.build_price_rows({"6501.T": _df(30)},
                                  TODAY - timedelta(days=3))
        assert len(rows) == 4, "since より前の行が混じっている"

    def test_multiple_tickers(self):
        rows = w.build_price_rows(
            {"6501.T": _df(2), "AAPL": _df(2, 200.0)},
            TODAY - timedelta(days=7))
        assert {r["ticker"] for r in rows} == {"6501.T", "AAPL"}
        assert len(rows) == 4

    def test_duplicate_ticker_date_is_deduplicated(self):
        df = _df(3)
        rows = w.build_price_rows({"6501.T": df}, TODAY - timedelta(days=7))
        pairs = [(r["ticker"], r["trade_date"]) for r in rows]
        assert len(pairs) == len(set(pairs))

    def test_missing_volume_column_still_writes(self):
        rows = w.build_price_rows({"6501.T": _df(1, with_volume=False)},
                                  TODAY - timedelta(days=7))
        assert len(rows) == 1
        assert rows[0]["volume"] is None

    def test_rows_without_close_are_dropped(self):
        df = _df(2)
        df.loc[:, "Close"] = [float("nan"), float("nan")]
        rows = w.build_price_rows({"6501.T": df}, TODAY - timedelta(days=7))
        assert rows == [], "終値が無い行は保存しない"

    def test_nan_and_inf_become_null(self):
        df = _df(1)
        df["Volume"] = df["Volume"].astype(float)
        df.loc[:, "Volume"] = [float("inf")]
        df.loc[:, "High"] = [float("nan")]
        rows = w.build_price_rows({"6501.T": df}, TODAY - timedelta(days=7))
        assert rows[0]["volume"] is None, "Inf は BQ が受け付けない"
        assert rows[0]["high"] is None

    def test_empty_and_none_frames_are_skipped(self):
        rows = w.build_price_rows(
            {"A": None, "B": pd.DataFrame(), "C": _df(1)},
            TODAY - timedelta(days=7))
        assert [r["ticker"] for r in rows] == ["C"]

    def test_max_rows_caps_output(self):
        rows = w.build_price_rows({"6501.T": _df(50)},
                                  TODAY - timedelta(days=60), max_rows=10)
        assert len(rows) == 10


class TestRunner:
    def test_writes_what_the_cache_collected(self, monkeypatch):
        sent = {}

        def fake_insert(rows=None, table_ref=""):
            sent["rows"] = rows
            sent["table"] = table_ref
            return True

        import edinet_bq_client
        import yf_cache
        monkeypatch.setattr(edinet_bq_client, "insert_rows", fake_insert)
        monkeypatch.setattr(yf_cache, "collected_prices",
                            lambda: {"6501.T": _df(2)})

        res = w.run_price_history_writer(object(), {})

        assert res["inserted"] is True
        assert res["tickers"] == 1
        assert sent["table"] == "analytics.price_daily"
        assert len(sent["rows"]) == 2

    def test_no_collected_prices_is_skipped(self, monkeypatch):
        import yf_cache
        monkeypatch.setattr(yf_cache, "collected_prices", lambda: {})
        res = w.run_price_history_writer(object(), {})
        assert res["skipped"] is True
        assert res["inserted"] is False

    def test_bq_failure_does_not_raise(self, monkeypatch):
        import edinet_bq_client
        import yf_cache

        def boom(**_kw):
            raise RuntimeError("BQ down")

        monkeypatch.setattr(edinet_bq_client, "insert_rows", boom)
        monkeypatch.setattr(yf_cache, "collected_prices",
                            lambda: {"6501.T": _df(1)})
        res = w.run_price_history_writer(object(), {})
        assert res["inserted"] is False
        assert res["skipped"] is False


class TestCacheIntegration:
    def test_collected_prices_returns_daily_history(self, monkeypatch):
        """yf_cache が history() で取った日足を拾えること"""
        import sys
        import types

        fake = types.ModuleType("yfinance")

        class _T:
            def __init__(self, sym, *a, **k):
                self.sym = sym

            def history(self, **kw):
                return _df(3)

        fake.Ticker = _T
        fake.download = lambda *a, **k: pd.DataFrame()
        monkeypatch.setitem(sys.modules, "yfinance", fake)

        import yf_cache
        yf_cache.clear()
        yf_cache.Ticker("6501.T").history(period="1mo", interval="1d")

        prices = yf_cache.collected_prices()
        assert "6501.T" in prices
        rows = w.build_price_rows(prices, TODAY - timedelta(days=7))
        assert len(rows) == 3

    def test_intraday_history_is_not_persisted(self, monkeypatch):
        """日足以外は price_daily に入れない"""
        import sys
        import types

        fake = types.ModuleType("yfinance")

        class _T:
            def __init__(self, sym, *a, **k):
                pass

            def history(self, **kw):
                return _df(3)

        fake.Ticker = _T
        fake.download = lambda *a, **k: pd.DataFrame()
        monkeypatch.setitem(sys.modules, "yfinance", fake)

        import yf_cache
        yf_cache.clear()
        yf_cache.Ticker("6501.T").history(period="1d", interval="5m")

        assert yf_cache.collected_prices() == {}
