# ==============================================================
# tests/test_market_data.py — U-5 market_data.py 新設（L2正規化層）
# ==============================================================
# 目的（要件定義書_JP-US統合_20260726.md §3-3(a) / §6-J2 U-5、
#       HANDOFF_JP-US統合_20260727.md §4 Batch 6）:
#
#   R5-1: fetch_quote() が §3-3(a) の契約辞書を返すこと。
#         **market_cap_b は返さない**（F-4 の根治）。
#   R5-2: 429 リトライ（3s/6s/12s）が fetch_quote に集約され、
#         JP にも適用されること。
#   R5-3: 両スクリーナーの _fetch_ticker_data / _fetch_one が
#         「fetch_quote を呼んで従来キーへ写像する薄い層」に縮退しても、
#         **返り値が U-5 変更前と一致する**こと（等価性・最重要）。
#
#   等価性テストの期待値は、U-5 変更前の実装（stock_screener.py および
#   us_stock_screener.py の旧 _fetch_ticker_data / _fetch_one）を
#   一時スクリプトで逐語コピーして同じ .info モックに対して実行し、
#   得られた**実測値**である（2026-07-28 実測。全14フィクスチャで
#   JP=14/14 一致・US=13/14 一致。唯一の相違は下記 NaN の1件で、
#   test_us_nan_is_normalized_to_none_documented_divergence が凍結する）。
#
#   ネットワークには一切出ない。yfinance は関数内 import であるため、
#   sys.modules["yfinance"] を monkeypatch でフェイクに差し替えて検証する。
# ==============================================================
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import market_data as md            # noqa: E402
import stock_screener as ss         # noqa: E402
import us_stock_screener as us      # noqa: E402


# ==============================================================
# フェイク yfinance
# ==============================================================
class _FakeTicker:
    def __init__(self, info):
        self.info = info


def _install(monkeypatch, info):
    """sys.modules["yfinance"] を、渡した info を返すフェイクに差し替える。"""
    monkeypatch.setitem(
        sys.modules, "yfinance",
        SimpleNamespace(Ticker=lambda ticker: _FakeTicker(info)),
    )


class _FlakyTicker:
    """指定回数だけ例外を投げ、その後 info を返すフェイク。"""

    def __init__(self, info, exc, fail_times, counter):
        counter["calls"] += 1
        if counter["calls"] <= fail_times:
            raise exc
        self.info = info


def _install_flaky(monkeypatch, info, exc, fail_times):
    counter = {"calls": 0}
    monkeypatch.setitem(
        sys.modules, "yfinance",
        SimpleNamespace(
            Ticker=lambda ticker: _FlakyTicker(info, exc, fail_times, counter)
        ),
    )
    return counter


@pytest.fixture
def no_sleep(monkeypatch):
    """time.sleep を記録用フェイクに差し替える（テストを待たせない）。"""
    waits = []
    monkeypatch.setattr(md.time, "sleep", lambda s: waits.append(s))
    return waits


# ==============================================================
# .info フィクスチャ
# ==============================================================
FULL_INFO = {
    "regularMarketPrice": 100.0,
    "currentPrice": 101.0,
    "previousClose": 99.0,
    "longName": "Test Corporation Of Very Long Name Indeed Limited",
    "shortName": "TestCorp",
    "sector": "Technology",
    "industry": "Software - Infrastructure",
    "marketCap": 1_234_500_000_000,
    "averageVolume": 4_500_000,
    "averageVolume10days": 3_900_000,
    "trailingPE": 12.5,
    "priceToBook": 1.8,
    "dividendYield": 2.3,   # yfinance は %値で返す（2026-09-29 に比率化を入口でそろえた）
    "returnOnEquity": 0.15,
    "returnOnAssets": 0.08,
    "operatingMargins": 0.21,
    "grossMargins": 0.44,
    "profitMargins": 0.13,
    "ebitdaMargins": 0.27,
    "revenueGrowth": 0.11,
    "earningsGrowth": 0.19,
    "earningsQuarterlyGrowth": 0.07,
    "totalCash": 5_000_000_000,
    "totalDebt": 2_000_000_000,
    "debtToEquity": 35.2,
    "freeCashflow": 800_000_000,
    "operatingCashflow": 1_500_000_000,
    "netIncomeToCommon": 900_000_000,
    "totalAssets": 20_000_000_000,
    "totalRevenue": 12_000_000_000,
    "stockholdersEquity": 8_000_000_000,
    "bookValue": 55.5,
    "payoutRatio": 0.25,
    "inventory": 3_000_000_000,
    "recommendationMean": 2.1,
    "recommendationKey": "buy",
    "targetMeanPrice": 120.0,
    "numberOfAnalystOpinions": 24,
    "beta": 1.05,
    "52WeekChange": 0.32,
}

MINIMAL_INFO   = {"regularMarketPrice": 50.0}
ZERO_PRICE     = {"regularMarketPrice": 0.0, "targetMeanPrice": 120.0}
NAN_INFO       = {
    "regularMarketPrice": 80.0,
    "recommendationMean": float("nan"),
    "recommendationKey": "hold",
    "trailingPE": float("nan"),
    "marketCap": 7_777_000_000,
    "averageVolume10days": 12_345,
    "totalRevenue": 100.0,
    "inventory": 7.0,
}
NO_PRICE_INFO  = {"trailingPE": 10.0}
US_MINIMAL     = {"currentPrice": 42.0}
US_PREV_CLOSE  = {"previousClose": 7.5, "marketCap": 0}
US_ACCRUAL_INFO = {
    "regularMarketPrice": 10.0,
    "netIncomeToCommon": 500.0,
    "operatingCashflow": 300.0,
    "totalAssets": 1000.0,
    "recommendationKey": None,
    "numberOfAnalystOpinions": 5,
}

US_ENTRY = {"ticker": "TESTUS", "category": "sp500",
            "business_model": "mixed", "in_sp500": True}


# 要件定義書 §3-3(a) が定める返り値キー（メタを含む）
CONTRACT_KEYS = {
    "ticker", "market", "name",
    "market_cap_raw", "current_price", "avg_volume",
    "per", "pbr", "div_yield",
    "roe", "roa", "op_margin", "gross_margin", "net_margin",
    "revenue_growth", "earnings_growth", "earnings_qgrowth",
    "total_cash", "total_debt", "debt_to_equity",
    "free_cashflow", "operating_cashflow", "net_income",
    "total_assets", "total_revenue", "inventory_turnover",
    "rec_mean", "rec_key", "analyst_count",
    "target_price", "analyst_upside",
    "beta", "week52_change",
    "accrual_ratio",
    "_source", "_fetched_at",
}


# ==============================================================
# R5-1: 返り値契約（§3-3(a)）
# ==============================================================
class TestFetchQuoteContract:
    def test_jp_returns_all_contract_keys(self, monkeypatch):
        _install(monkeypatch, FULL_INFO)
        q = md.fetch_quote("7203.T", "JP")
        assert q is not None
        assert CONTRACT_KEYS <= set(q), CONTRACT_KEYS - set(q)
        assert q["ticker"] == "7203.T"
        assert q["market"] == "JP"

    def test_us_returns_all_contract_keys(self, monkeypatch):
        _install(monkeypatch, FULL_INFO)
        q = md.fetch_quote("AAPL", "US")
        assert q is not None
        assert CONTRACT_KEYS <= set(q), CONTRACT_KEYS - set(q)
        assert q["ticker"] == "AAPL"
        assert q["market"] == "US"

    @pytest.mark.parametrize("ticker,market", [("7203.T", "JP"), ("AAPL", "US")])
    def test_market_cap_b_is_not_returned(self, monkeypatch, ticker, market):
        # F-4 の根治: 同名で単位が違うキーを L2 は持たない
        _install(monkeypatch, FULL_INFO)
        q = md.fetch_quote(ticker, market)
        assert "market_cap_b" not in q
        assert q["market_cap_raw"] == 1_234_500_000_000.0   # 現地通貨の生値

    @pytest.mark.parametrize("ticker,market", [("7203.T", "JP"), ("AAPL", "US")])
    def test_analyst_and_risk_fields_fetched_in_both_markets(
            self, monkeypatch, ticker, market):
        # F-1 の根治: JP でも rec_mean 等が必ず取れること
        _install(monkeypatch, FULL_INFO)
        q = md.fetch_quote(ticker, market)
        assert q["rec_mean"] == 2.1
        assert q["rec_key"] == "buy"
        assert q["target_price"] == 120.0
        assert q["analyst_upside"] == pytest.approx(0.2)
        assert q["analyst_count"] == 24
        assert q["beta"] == 1.05
        assert q["week52_change"] == 0.32
        assert q["accrual_ratio"] == pytest.approx(-0.03)

    def test_source_and_fetched_at(self, monkeypatch):
        _install(monkeypatch, FULL_INFO)
        q = md.fetch_quote("7203.T", "JP")
        assert q["_source"] == "yfinance"
        # "YYYY-MM-DD HH:MM:SS"
        assert len(q["_fetched_at"]) == 19
        assert q["_fetched_at"][4] == "-" and q["_fetched_at"][13] == ":"

    def test_raw_info_is_returned_as_copy(self, monkeypatch):
        # F-5 用に生 info を同梱する。ただし呼び出し側の変更が
        # yfinance 側の辞書へ波及しないよう浅いコピーであること。
        _install(monkeypatch, MINIMAL_INFO)
        q = md.fetch_quote("7203.T", "JP")
        assert q["_raw_info"] == MINIMAL_INFO
        assert q["_raw_info"] is not MINIMAL_INFO
        q["_raw_info"]["regularMarketPrice"] = 999.0
        assert MINIMAL_INFO["regularMarketPrice"] == 50.0

    def test_market_argument_is_case_insensitive(self, monkeypatch):
        _install(monkeypatch, FULL_INFO)
        assert md.fetch_quote("AAPL", "us")["market"] == "US"

    def test_nan_is_normalized_to_none(self, monkeypatch):
        _install(monkeypatch, NAN_INFO)
        q = md.fetch_quote("7203.T", "JP")
        assert q["per"] is None
        assert q["rec_mean"] is None

    def test_inventory_turnover_is_unrounded_raw_ratio(self, monkeypatch):
        # 生値を返し、丸めは消費側（JP スクリーナーが round(x, 2)）
        _install(monkeypatch, NAN_INFO)
        q = md.fetch_quote("7203.T", "JP")
        assert q["inventory_turnover"] == pytest.approx(100.0 / 7.0)
        assert round(q["inventory_turnover"], 2) == 14.29

    def test_avg_volume_falls_back_to_10days(self, monkeypatch):
        _install(monkeypatch, NAN_INFO)
        q = md.fetch_quote("7203.T", "JP")
        assert q["avg_volume"] == 12_345


# ==============================================================
# R5-1: 価格が取れない銘柄は None
# ==============================================================
class TestFetchQuotePriceGate:
    @pytest.mark.parametrize("market", ["JP", "US"])
    def test_no_price_returns_none(self, monkeypatch, market):
        _install(monkeypatch, NO_PRICE_INFO)
        assert md.fetch_quote("X", market) is None

    @pytest.mark.parametrize("market", ["JP", "US"])
    def test_empty_info_returns_none(self, monkeypatch, market):
        _install(monkeypatch, {})
        assert md.fetch_quote("X", market) is None

    def test_jp_keeps_zero_regular_market_price(self, monkeypatch):
        # JP 既存挙動: regularMarketPrice=0.0 は「価格なし」ではない
        _install(monkeypatch, ZERO_PRICE)
        q = md.fetch_quote("7203.T", "JP")
        assert q is not None
        assert q["current_price"] == 0.0

    def test_us_rejects_zero_price(self, monkeypatch):
        # US 既存挙動: 3キーとも falsy なら None
        _install(monkeypatch, ZERO_PRICE)
        assert md.fetch_quote("AAPL", "US") is None

    def test_us_uses_previous_close_fallback(self, monkeypatch):
        _install(monkeypatch, US_PREV_CLOSE)
        q = md.fetch_quote("AAPL", "US")
        assert q["current_price"] == 7.5

    def test_price_gate_does_not_retry(self, monkeypatch, no_sleep):
        # 価格なしは即 None。リトライで無駄に待たないこと。
        _install(monkeypatch, NO_PRICE_INFO)
        assert md.fetch_quote("AAPL", "US") is None
        assert no_sleep == []


# ==============================================================
# R5-1: analyst_upside の導出（truthy 判定）
# ==============================================================
class TestAnalystUpside:
    def test_target_120_current_100_is_0_2(self, monkeypatch):
        _install(monkeypatch, {"regularMarketPrice": 100.0,
                               "targetMeanPrice": 120.0})
        assert md.fetch_quote("X", "JP")["analyst_upside"] == pytest.approx(0.2)

    def test_target_zero_is_none(self, monkeypatch):
        # targetMeanPrice=0 は「目標株価なし」→ -100% を算出しない
        _install(monkeypatch, {"regularMarketPrice": 100.0,
                               "targetMeanPrice": 0.0})
        q = md.fetch_quote("X", "JP")
        assert q["target_price"] == 0.0
        assert q["analyst_upside"] is None

    def test_current_zero_is_none(self, monkeypatch):
        _install(monkeypatch, ZERO_PRICE)
        q = md.fetch_quote("X", "JP")
        assert q["current_price"] == 0.0
        assert q["analyst_upside"] is None

    def test_target_missing_is_none(self, monkeypatch):
        _install(monkeypatch, MINIMAL_INFO)
        assert md.fetch_quote("X", "JP")["analyst_upside"] is None


# ==============================================================
# R5-2: リトライ（US 側の実装を JP にも波及させる）
# ==============================================================
class TestRetry:
    def test_429_then_success(self, monkeypatch, no_sleep):
        _install_flaky(monkeypatch, FULL_INFO,
                       RuntimeError("429 Too Many Requests"), fail_times=2)
        q = md.fetch_quote("7203.T", "JP")
        assert q is not None
        assert q["per"] == 12.5
        assert no_sleep == [3, 6]      # (2**0)*3, (2**1)*3

    def test_429_exhausted_returns_none(self, monkeypatch, no_sleep):
        _install_flaky(monkeypatch, FULL_INFO,
                       RuntimeError("429 Too Many Requests"), fail_times=99)
        assert md.fetch_quote("7203.T", "JP") is None
        assert no_sleep == [3, 6, 12]

    def test_generic_error_waits_two_seconds(self, monkeypatch, no_sleep):
        _install_flaky(monkeypatch, FULL_INFO,
                       RuntimeError("boom"), fail_times=1)
        assert md.fetch_quote("AAPL", "US") is not None
        assert no_sleep == [2]

    def test_generic_error_exhausted_returns_none(self, monkeypatch, no_sleep):
        _install_flaky(monkeypatch, FULL_INFO,
                       RuntimeError("boom"), fail_times=99)
        assert md.fetch_quote("AAPL", "US") is None
        assert no_sleep == [2, 2]      # 最終試行は待たずに None

    def test_max_retries_one_means_single_attempt(self, monkeypatch, no_sleep):
        counter = _install_flaky(monkeypatch, FULL_INFO,
                                 RuntimeError("boom"), fail_times=99)
        assert md.fetch_quote("AAPL", "US", max_retries=1) is None
        assert counter["calls"] == 1
        assert no_sleep == []

    def test_retry_applies_to_jp_too(self, monkeypatch, no_sleep):
        # JP は U-5 以前リトライを持っていなかった（意図的な品質改善）
        _install_flaky(monkeypatch, MINIMAL_INFO,
                       RuntimeError("too many requests"), fail_times=1)
        assert ss._fetch_ticker_data("7203.T") is not None
        assert no_sleep == [3]


# ==============================================================
# R5-3: 等価性（最重要）— JP
#   期待値は U-5 変更前の実装を一時スクリプトで実行して得た実測値。
# ==============================================================
JP_FULL = {
    'analyst_count': 24, 'analyst_upside': 0.2, 'avg_volume': 4500000,
    'beta': 1.05, 'current_price': 100.0, 'debt_to_equity': 35.2,
    'earnings_growth': 0.19, 'earnings_qgrowth': 0.07, 'equity_ratio': 55.5,
    'gross_margin': 0.44, 'inventory_turnover': 4.0,
    'market_cap': 1234500000000.0, 'market_cap_b': 12345.0,
    'name': 'Test Corporation Of Very Long Name Indee',
    'net_income': 900000000.0, 'net_margin': 0.13, 'op_margin': 0.21,
    'operating_cashflow': 1500000000.0, 'payout_ratio': 0.25, 'pbr': 1.8,
    'per': 12.5, 'rec_key': 'buy', 'rec_mean': 2.1, 'roe': 0.15,
    'stockholders_equity': 8000000000.0, 'target_price': 120.0,
    'ticker': 'TEST.T', 'total_assets': 20000000000.0,
    'total_cash': 5000000000.0, 'total_debt': 2000000000.0,
    'dividend_yield': 0.023,   # 2026-09-29 追加（比率）
}

JP_MINIMAL = {
    'analyst_count': None, 'analyst_upside': None, 'avg_volume': 0,
    'beta': None, 'current_price': 50.0, 'debt_to_equity': None,
    'earnings_growth': None, 'earnings_qgrowth': None, 'equity_ratio': None,
    'gross_margin': None, 'inventory_turnover': None, 'market_cap': None,
    'market_cap_b': 0.0, 'name': 'TEST.T', 'net_income': None,
    'net_margin': None, 'op_margin': None, 'operating_cashflow': None,
    'payout_ratio': None, 'pbr': None, 'per': None, 'rec_key': '',
    'rec_mean': None, 'roe': None, 'stockholders_equity': None,
    'target_price': None, 'ticker': 'TEST.T', 'total_assets': None,
    'total_cash': None, 'total_debt': None, 'dividend_yield': None,
}

JP_ZERO_PRICE = dict(JP_MINIMAL, current_price=0.0, target_price=120.0)

JP_NAN = {
    'analyst_count': None, 'analyst_upside': None, 'avg_volume': 12345,
    'beta': None, 'current_price': 80.0, 'debt_to_equity': None,
    'earnings_growth': None, 'earnings_qgrowth': None, 'equity_ratio': None,
    'gross_margin': None, 'inventory_turnover': 14.29,
    'market_cap': 7777000000.0, 'market_cap_b': 77.8, 'name': 'TEST.T',
    'net_income': None, 'net_margin': None, 'op_margin': None,
    'operating_cashflow': None, 'payout_ratio': None, 'pbr': None,
    'per': None, 'rec_key': 'hold', 'rec_mean': None, 'roe': None,
    'stockholders_equity': None, 'target_price': None, 'ticker': 'TEST.T',
    'total_assets': None, 'total_cash': None, 'total_debt': None,
    'dividend_yield': None,
}


class TestJpScreenerEquivalence:
    @pytest.mark.parametrize("label,info,expected", [
        ("full",       FULL_INFO,    JP_FULL),
        ("minimal",    MINIMAL_INFO, JP_MINIMAL),
        ("zero_price", ZERO_PRICE,   JP_ZERO_PRICE),
        ("nan",        NAN_INFO,     JP_NAN),
    ])
    def test_fetch_ticker_data_matches_pre_u5(self, monkeypatch, label,
                                              info, expected):
        _install(monkeypatch, info)
        assert ss._fetch_ticker_data("TEST.T") == expected

    @pytest.mark.parametrize("info", [NO_PRICE_INFO, {}])
    def test_fetch_ticker_data_none_cases(self, monkeypatch, info):
        _install(monkeypatch, info)
        assert ss._fetch_ticker_data("TEST.T") is None

    def test_key_set_unchanged(self, monkeypatch):
        # 追加キーも欠落キーも無いこと（_yf_info を足していないことを含む）
        _install(monkeypatch, FULL_INFO)
        assert set(ss._fetch_ticker_data("TEST.T")) == set(JP_FULL)

    def test_yf_info_is_not_stored(self, monkeypatch):
        # F-5 の判断: JP 側には格納しない
        # （enrich_candidates_with_sector の当該 else 節に JP 銘柄は
        #   到達しない／candidate はキー明示列挙で組まれる。詳細は
        #   stock_screener.enrich_candidates_with_sector のコメント）
        _install(monkeypatch, FULL_INFO)
        data = ss._fetch_ticker_data("TEST.T")
        assert "_yf_info" not in data
        assert "_raw_info" not in data


# ==============================================================
# R5-3: 等価性（最重要）— US
# ==============================================================
US_FULL = {
    'accrual_ratio': -0.03, 'analyst_count': 24, 'analyst_upside': 0.2,
    'beta': 1.05, 'business_model': 'mixed', 'category': 'sp500',
    'current_price': 100.0, 'debt_to_equity': 35.2, 'div_yield': 0.023,
    'earnings_growth': 0.19, 'ebitda_margin': 0.27,
    'eps_accel_score_xbrl': None, 'fetch_ok': True,
    'free_cashflow': 800000000.0, 'gross_margin': 0.44, 'in_sp500': True,
    'industry': 'Software - Infrastructure', 'latest_fcf_xbrl': None,
    'market_cap': 1234500000000.0, 'market_cap_b': 1234.5, 'name': 'TestCorp',
    'op_margin': 0.21, 'operating_cashflow': 1500000000.0, 'pbr': 1.8,
    'per': 12.5, 'rec_key': 'buy', 'rec_mean': 2.1, 'revenue_growth': 0.11,
    'roa': 0.08, 'roe': 0.15, 'sector': 'Technology', 'target_price': 120.0,
    'ticker': 'TESTUS', 'total_revenue': 12000000000.0, 'week52_change': 0.32,
}

US_EMPTYISH = {
    'accrual_ratio': None, 'analyst_count': None, 'analyst_upside': None,
    'beta': None, 'business_model': 'mixed', 'category': 'sp500',
    'current_price': None, 'debt_to_equity': None, 'div_yield': None,
    'earnings_growth': None, 'ebitda_margin': None,
    'eps_accel_score_xbrl': None, 'fetch_ok': True, 'free_cashflow': None,
    'gross_margin': None, 'in_sp500': True, 'industry': '',
    'latest_fcf_xbrl': None, 'market_cap': None, 'market_cap_b': None,
    'name': 'TESTUS', 'op_margin': None, 'operating_cashflow': None,
    'pbr': None, 'per': None, 'rec_key': '', 'rec_mean': None,
    'revenue_growth': None, 'roa': None, 'roe': None, 'sector': '',
    'target_price': None, 'ticker': 'TESTUS', 'total_revenue': None,
    'week52_change': None,
}

US_MINIMAL_EXPECTED = dict(US_EMPTYISH, current_price=42.0)
US_PREV_CLOSE_EXPECTED = dict(US_EMPTYISH, current_price=7.5, market_cap=0.0)
US_ACCRUAL_EXPECTED = dict(
    US_EMPTYISH, current_price=10.0, operating_cashflow=300.0,
    accrual_ratio=0.2, analyst_count=5, rec_key=None,
)


class TestUsScreenerEquivalence:
    @pytest.mark.parametrize("label,info,expected", [
        ("full",       FULL_INFO,       US_FULL),
        ("minimal",    US_MINIMAL,      US_MINIMAL_EXPECTED),
        ("prev_close", US_PREV_CLOSE,   US_PREV_CLOSE_EXPECTED),
        ("accrual",    US_ACCRUAL_INFO, US_ACCRUAL_EXPECTED),
    ])
    def test_fetch_one_matches_pre_u5(self, monkeypatch, label, info, expected):
        _install(monkeypatch, info)
        assert us._fetch_one(dict(US_ENTRY)) == expected

    @pytest.mark.parametrize("info", [NO_PRICE_INFO, {}, ZERO_PRICE])
    def test_fetch_one_none_cases(self, monkeypatch, info):
        _install(monkeypatch, info)
        assert us._fetch_one(dict(US_ENTRY)) is None

    def test_key_set_unchanged(self, monkeypatch):
        _install(monkeypatch, FULL_INFO)
        assert set(us._fetch_one(dict(US_ENTRY))) == set(US_FULL)

    def test_entry_derived_fields_still_come_from_entry(self, monkeypatch):
        _install(monkeypatch, FULL_INFO)
        entry = {"ticker": "TESTUS", "category": "biotech",
                 "business_model": "rnd_heavy", "in_sp500": False}
        out = us._fetch_one(entry)
        assert out["category"] == "biotech"
        assert out["business_model"] == "rnd_heavy"
        assert out["in_sp500"] is False
        # sector / industry は entry ではなく .info 由来のまま
        assert out["sector"] == "Technology"

    def test_us_nan_is_normalized_to_none_documented_divergence(
            self, monkeypatch):
        # U-5 で唯一意図的に変わる点:
        #   旧 us_stock_screener._get() は NaN をそのまま返していた。
        #   L2（market_data._clean）は NaN を None に正規化する。
        #   NaN は下流の比較を黙って False にするため、None が正しい。
        #   （JP 側は U-5 以前から NaN → None。US を JP へ揃えた）
        _install(monkeypatch, NAN_INFO)
        out = us._fetch_one(dict(US_ENTRY))
        assert out["per"] is None
        assert out["rec_mean"] is None


# ==============================================================
# F-4: market_cap_b は「同名で単位が違う」— 事実の凍結
# ==============================================================
def test_market_cap_b_units_differ_between_screeners(monkeypatch):
    """
    同じ marketCap に対し JP=億円 / US=十億USD になる。
    U-5 では互換性維持のため是正せず、事実をテストで固定する。
    市場をまたぐ比較には fetch_quote()["market_cap_raw"] を使うこと。
    """
    _install(monkeypatch, FULL_INFO)
    jp = ss._fetch_ticker_data("TEST.T")
    _install(monkeypatch, FULL_INFO)
    usd = us._fetch_one(dict(US_ENTRY))
    _install(monkeypatch, FULL_INFO)
    raw = md.fetch_quote("TEST.T", "JP")["market_cap_raw"]

    assert jp["market_cap_b"] == pytest.approx(raw / 1e8)
    assert usd["market_cap_b"] == pytest.approx(raw / 1e9)
    assert jp["market_cap_b"] != usd["market_cap_b"]


# ==============================================================
# import 時にネットワークへ出ないこと
# ==============================================================
def test_import_has_no_network_side_effects():
    """yfinance は fetch_quote 内で import される（モジュール先頭では import しない）。"""
    src = (REPO / "market_data.py").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    for line in body.splitlines():
        if line.startswith("import ") or line.startswith("from "):
            assert "yfinance" not in line, f"モジュール先頭で import: {line}"
