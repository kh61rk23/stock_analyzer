# edinet_bq_client.get_real_estate_holdings — N+1の緩和
#
# 2026-09-09: 業種分析から1銘柄ずつ呼ばれ(業種あたり最大5社×33業種＝
# 最大165クエリ)、そのたびに47,455行のテーブルをスキャンしていた。
# プロセス内キャッシュと QUALIFY 化で往復とスキャンを減らす。
import threading

import pytest

import edinet_bq_client as ebc


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _Row(dict):
    def items(self):
        return dict(self).items()


@pytest.fixture
def client(monkeypatch):
    calls = {"n": 0, "sql": []}

    class _FakeBQ:
        def query(self, sql, job_config=None):
            calls["n"] += 1
            calls["sql"].append(sql)
            return _FakeQueryJob([
                _Row({"edinet_code": "E00052", "fiscal_year": 2026,
                      "facility_name": "本社"}),
            ])

    obj = object.__new__(ebc.EDINETBQClient)
    obj.client = _FakeBQ()
    obj.project = "proj"
    obj.dataset = "proj.edinet"
    ebc._RE_HOLDINGS_CACHE.clear()
    monkeypatch.setattr(ebc, "bigquery", ebc.bigquery)
    obj._calls = calls
    return obj


class TestCache:
    def test_same_code_queries_once(self, client):
        for _ in range(5):
            client.get_real_estate_holdings("1801.T")
        assert client._calls["n"] == 1, "同じ銘柄で毎回クエリしている"

    def test_different_codes_query_separately(self, client):
        client.get_real_estate_holdings("1801.T")
        client.get_real_estate_holdings("1802.T")
        assert client._calls["n"] == 2

    def test_ticker_suffix_is_normalized(self, client):
        """1801.T と 1801 は同じ銘柄なのでキャッシュを共有する"""
        client.get_real_estate_holdings("1801.T")
        client.get_real_estate_holdings("1801")
        assert client._calls["n"] == 1

    def test_returns_a_copy_so_callers_cannot_corrupt_cache(self, client):
        a = client.get_real_estate_holdings("1801.T")
        a.append({"injected": True})
        b = client.get_real_estate_holdings("1801.T")
        assert len(b) == 1, "呼び出し側の変更がキャッシュへ波及している"

    def test_empty_code_does_not_query(self, client):
        assert client.get_real_estate_holdings("") == []
        assert client.get_real_estate_holdings("....") == []
        assert client._calls["n"] == 0

    def test_cache_is_thread_safe(self, client):
        """業種分析は並列実行されるため同時アクセスされる"""
        errors = []

        def _worker():
            try:
                for _ in range(20):
                    client.get_real_estate_holdings("1801.T")
            except Exception as exc:      # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert client._calls["n"] == 1, "並列アクセスで重複クエリが走っている"


class TestQuery:
    def test_uses_qualify_instead_of_correlated_subquery(self, client):
        """相関サブクエリはテーブルをもう一度スキャンするため使わない"""
        client.get_real_estate_holdings("1801.T")
        sql = client._calls["sql"][0]
        assert "QUALIFY" in sql
        assert "SELECT MAX(h2.fiscal_year)" not in sql

    def test_joins_edinet_and_real_estate(self, client):
        """EDINET側(company)と不動産側(holdings)は1クエリで突き合わせる"""
        client.get_real_estate_holdings("1801.T")
        sql = client._calls["sql"][0]
        assert "real_estate.corporate_holdings_p1" in sql
        assert "edinet.company" in sql
        assert "USING (edinet_code)" in sql
