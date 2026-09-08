# base_agent.resolve_sector_tickers — 全業種ticker解決のBQ問い合わせ回数
import base_agent


class _FakeBQ:
    """get_tickers_by_sector_bulk の呼ばれ方だけを見る差し替え用"""

    def __init__(self, payload):
        self.payload = payload
        self.bulk_calls = 0
        self.single_calls = 0

    def get_tickers_by_sector_bulk(self, limit=10):
        self.bulk_calls += 1
        return self.payload

    def get_tickers_for_sector(self, sector_code, limit=5):
        self.single_calls += 1
        return []


def _install(monkeypatch, fake):
    monkeypatch.setattr(base_agent, "_BQClient", lambda: fake)
    monkeypatch.setattr(base_agent, "_SECTOR_TICKER_CACHE", None)


def test_bulk_query_runs_once_for_all_sectors(monkeypatch):
    """33業種を引いてもBQへの問い合わせは1回だけ"""
    fake = _FakeBQ({
        "1003": ["1801.T", "1802.T", "1812.T"],
        "1004": ["2501.T", "2502.T"],
    })
    _install(monkeypatch, fake)

    for _ in range(3):
        assert base_agent.resolve_sector_tickers("1003", None, limit=5) == [
            "1801.T", "1802.T", "1812.T"]
        assert base_agent.resolve_sector_tickers("1004", None, limit=5) == [
            "2501.T", "2502.T"]

    assert fake.bulk_calls == 1
    assert fake.single_calls == 0


def test_limit_is_applied_per_sector(monkeypatch):
    fake = _FakeBQ({"1003": ["1801.T", "1802.T", "1812.T", "1820.T"]})
    _install(monkeypatch, fake)

    assert base_agent.resolve_sector_tickers("1003", None, limit=2) == [
        "1801.T", "1802.T"]


def test_unknown_sector_falls_through_to_other_sources(monkeypatch):
    """BQに無い業種はBQ結果を返さず、後段のフォールバックに委ねる"""
    fake = _FakeBQ({"1003": ["1801.T"]})
    _install(monkeypatch, fake)

    out = base_agent.resolve_sector_tickers("9999", None, limit=5)
    assert "1801.T" not in out
