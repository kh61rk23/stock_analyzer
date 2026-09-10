# BQ課金は「クエリ本数」で決まる — N+1を先読みで畳めていること
#
# 2026-09-10 の課金実測（INFORMATION_SCHEMA.JOBS_BY_PROJECT）:
#   482クエリ/実行・6.9GB課金/日。うち340本が「1銘柄1クエリ」のN+1。
#   BigQuery は**参照テーブル1つにつき最低10MB**を課金するため、
#   実データが0行でも 2テーブルJOIN なら1本20MB取られる。
#   つまり費用はデータ量ではなく本数で決まり、束ねる以外に手が無い。
# 無料枠は月1TB。ここが緩むと課金が発生しうるため本数を固定で守る。
import types

import pytest

import edinet_bq_client as ebc
import geo_signal


class _FakeRow(dict):
    def items(self):
        return dict.items(self)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _FakeBQ:
    """query() の呼び出し回数を数える最小のBQクライアント"""

    def __init__(self, rows=None):
        self.calls = []
        self._rows = rows or []

    def query(self, sql, job_config=None):
        self.calls.append(sql)
        return _FakeResult(self._rows)


def _client(fake):
    c = ebc.EDINETBQClient.__new__(ebc.EDINETBQClient)
    c.client = fake
    c.project = "p"
    c.dataset = "edinet"
    return c


class TestHoldingsPrefetch:
    def test_one_query_for_many_tickers(self):
        fake = _FakeBQ()
        c = _client(fake)
        n = c.prefetch_real_estate_holdings([f"{6000 + i}.T" for i in range(40)])
        assert len(fake.calls) == 1, "銘柄ごとにクエリを撃っている"
        assert n == 40

    def test_codes_without_rows_are_cached_as_empty(self):
        """空リストを入れないと後続が毎回キャッシュミスして元に戻る"""
        fake = _FakeBQ()
        c = _client(fake)
        c.prefetch_real_estate_holdings(["6501.T", "7203.T"])
        before = len(fake.calls)
        assert c.get_real_estate_holdings("6501.T") == []
        assert c.get_real_estate_holdings("7203.T") == []
        assert len(fake.calls) == before, "先読み後に単体クエリが走っている"

    def test_failure_does_not_poison_the_cache(self):
        class _Boom(_FakeBQ):
            def query(self, sql, job_config=None):
                raise RuntimeError("BQ down")

        c = _client(_Boom())
        assert c.prefetch_real_estate_holdings(["6501.T"]) == 0
        assert "6501" not in ebc._RE_HOLDINGS_CACHE

    def test_empty_input_makes_no_query(self):
        fake = _FakeBQ()
        assert _client(fake).prefetch_real_estate_holdings([]) == 0
        assert fake.calls == []


class TestFinancialsPrefetch:
    def test_one_query_for_many_tickers(self):
        fake = _FakeBQ()
        c = _client(fake)
        n = c.prefetch_latest_financials([f"{6000 + i}.T" for i in range(40)])
        assert len(fake.calls) == 1
        assert n == 40

    def test_prefetched_codes_do_not_query_again(self):
        fake = _FakeBQ([_FakeRow({"company_code": "6501", "net_sales": 1})])
        c = _client(fake)
        c.prefetch_latest_financials(["6501.T", "7203.T"])
        before = len(fake.calls)
        assert c.get_financials_single("6501.T")["net_sales"] == 1
        assert c.get_latest_financials("7203.T") == {}
        assert len(fake.calls) == before, "先読み後に単体クエリが走っている"

    def test_uses_query_parameters_not_string_interpolation(self):
        fake = _FakeBQ()
        c = _client(fake)
        c.prefetch_latest_financials(["6501.T"])
        assert "@codes" in fake.calls[0]
        assert "'6501'" not in fake.calls[0]


class TestExposurePrefetch:
    def test_one_query_for_many_tickers(self, monkeypatch):
        fake = _FakeBQ()
        monkeypatch.setattr(geo_signal, "_get_raw_bq", lambda *_a, **_k: fake)
        n = geo_signal.prefetch_exposures([f"{6000 + i}.T" for i in range(40)],
                                          market="JP")
        assert len(fake.calls) == 1
        assert n == 40

    def test_prefetched_tickers_do_not_query_again(self, monkeypatch):
        fake = _FakeBQ()
        monkeypatch.setattr(geo_signal, "_get_raw_bq", lambda *_a, **_k: fake)
        geo_signal.prefetch_exposures(["6501.T", "7203.T"], market="JP")
        before = len(fake.calls)
        assert geo_signal._get_exposure_list("6501.T", "JP") == []
        assert len(fake.calls) == before

    def test_no_bq_client_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(geo_signal, "_get_raw_bq", lambda *_a, **_k: None)
        assert geo_signal.prefetch_exposures(["6501.T"], market="JP") == 0
