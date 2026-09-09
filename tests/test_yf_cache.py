# yf_cache — yfinance の実行内キャッシュ（重複取得の一掃）
import sys
import types

import pytest


@pytest.fixture
def yfc(monkeypatch):
    """yfinance をモックに差し替えた yf_cache を返す"""
    calls = {"download": 0, "ticker": 0, "info": 0, "history": 0}

    class _DF:
        """DataFrame の最小代用（copy と empty だけ持つ）"""

        def __init__(self, tag):
            self.tag = tag
            self.empty = False

        def copy(self):
            return _DF(self.tag)

    class _FakeTicker:
        def __init__(self, symbol, *a, **k):
            calls["ticker"] += 1
            self.symbol = symbol
            self.some_other_attr = f"raw:{symbol}"

        @property
        def info(self):
            calls["info"] += 1
            return {"symbol": self.symbol, "n": calls["info"]}

        def history(self, **kw):
            calls["history"] += 1
            return _DF(f"{self.symbol}:{sorted(kw.items())}")

    fake = types.ModuleType("yfinance")
    fake.Ticker = _FakeTicker

    def _download(tickers, **kw):
        calls["download"] += 1
        return _DF(f"dl:{tickers}")

    fake.download = _download
    monkeypatch.setitem(sys.modules, "yfinance", fake)

    sys.modules.pop("yf_cache", None)
    import yf_cache
    yf_cache.clear()
    yf_cache._calls = calls
    yield yf_cache
    sys.modules.pop("yf_cache", None)


class TestInfoCache:
    def test_same_ticker_info_fetched_once(self, yfc):
        for _ in range(5):
            yfc.Ticker("6501.T").info
        assert yfc._calls["info"] == 1, "同一銘柄の .info を複数回取得している"
        assert yfc.stats()["info_hit"] == 4

    def test_different_tickers_are_separate(self, yfc):
        yfc.Ticker("6501.T").info
        yfc.Ticker("7203.T").info
        assert yfc._calls["info"] == 2

    def test_underlying_ticker_object_is_reused(self, yfc):
        for _ in range(3):
            yfc.Ticker("6501.T").info
        assert yfc._calls["ticker"] == 1, "本物のTickerを毎回生成している"


class TestHistoryCache:
    def test_same_params_fetched_once(self, yfc):
        for _ in range(4):
            yfc.Ticker("6501.T").history(period="1y", interval="1d")
        assert yfc._calls["history"] == 1
        assert yfc.stats()["history_hit"] == 3

    def test_different_params_are_separate(self, yfc):
        t = yfc.Ticker("6501.T")
        t.history(period="1y", interval="1d")
        t.history(period="6mo", interval="1d")
        assert yfc._calls["history"] == 2

    def test_returns_a_copy_so_callers_cannot_corrupt_cache(self, yfc):
        a = yfc.Ticker("6501.T").history(period="1y")
        b = yfc.Ticker("6501.T").history(period="1y")
        assert a is not b, "同一オブジェクトを返すと呼び出し側の加工が波及する"


class TestDownloadCache:
    def test_same_request_fetched_once(self, yfc):
        for _ in range(3):
            yfc.download(["AAPL", "MSFT"], period="1mo", interval="1d")
        assert yfc._calls["download"] == 1

    def test_ticker_order_is_normalized(self, yfc):
        yfc.download(["AAPL", "MSFT"], period="1mo")
        yfc.download(["MSFT", "AAPL"], period="1mo")
        assert yfc._calls["download"] == 1, "銘柄の順序違いで取り直している"

    def test_string_and_list_forms_match(self, yfc):
        yfc.download("AAPL MSFT", period="1mo")
        yfc.download(["AAPL", "MSFT"], period="1mo")
        assert yfc._calls["download"] == 1

    def test_different_period_is_separate(self, yfc):
        yfc.download(["AAPL"], period="1mo")
        yfc.download(["AAPL"], period="1y")
        assert yfc._calls["download"] == 2


class TestTransparency:
    def test_unwrapped_attributes_delegate_to_real_ticker(self, yfc):
        """financials 等をラップしていなくても素通しで使える"""
        assert yfc.Ticker("6501.T").some_other_attr == "raw:6501.T"

    def test_unhashable_kwargs_do_not_crash(self, yfc):
        yfc.download(["AAPL"], period="1mo", extra=["a", "b"])
        yfc.download(["AAPL"], period="1mo", extra=["a", "b"])
        assert yfc._calls["download"] == 1

    def test_bare_instance_does_not_deadlock(self, yfc):
        """__init__ を経ずに作られても無限再帰・デッドロックしない

        _real() は self.ticker を解決するので、それ自体が __getattr__ に
        戻ると _LOCK（再入不可）を二重取得してハングする。
        内部属性は委譲せず AttributeError で打ち切ること。
        """
        bare = yfc.Ticker.__new__(yfc.Ticker)
        with pytest.raises(AttributeError):
            bare.info
        with pytest.raises(AttributeError):
            bare.ticker

    def test_clear_resets_everything(self, yfc):
        yfc.Ticker("6501.T").info
        yfc.clear()
        yfc.Ticker("6501.T").info
        assert yfc._calls["info"] == 2
        assert yfc.stats()["info_hit"] == 0
