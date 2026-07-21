"""db_rest_client の予算計算と HTTP 契約をネットワークなしで検証する。"""

import os

import pytest

import db_rest_client as module


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_budget_remaining_uses_the_stricter_of_local_and_remote_limits():
    assert module.budget_remaining(2, 5) == 3
    assert module.budget_remaining(2, 5, 1) == 1
    assert module.budget_remaining(5, 5, 99) == 0
    assert module.budget_remaining(0, 5, 0) == 0


def test_get_persists_calls_and_stops_at_usage_remaining(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/usage"):
            return _Response(200, {"data": {"daily_remaining": 1}})
        return _Response(200, {"data": {"ok": True}, "meta": {}})

    monkeypatch.setattr(module.requests, "get", fake_get)
    client = module.DBRestClient("https://example.test/v1", "key", tmp_path, 5, "testdb")

    assert client.get("/companies", {"code": "E00001"}) == {"ok": True}
    assert client.get("/companies", {"code": "E00002"}) is None
    assert len(calls) == 2
    assert calls[0][1]["headers"]["X-API-Key"] == "key"

    ledger = next(tmp_path.glob("daily_*.json")).read_text(encoding="utf-8")
    assert '"calls_used": 2' in ledger
    assert '"remote_daily_remaining": 0' in ledger


def test_unauthorized_response_is_not_retried(tmp_path, monkeypatch):
    called = 0

    def fake_get(*_args, **_kwargs):
        nonlocal called
        called += 1
        return _Response(401)

    monkeypatch.setattr(module.requests, "get", fake_get)
    client = module.DBRestClient("https://example.test/v1", "key", tmp_path, 5, "testdb")

    assert client.usage() is None
    assert called == 1
    assert client.calls == 1


def test_retryable_server_error_uses_exponential_backoff(tmp_path, monkeypatch):
    statuses = iter([503, 503, 200])
    waits = []

    monkeypatch.setattr(
        module.requests,
        "get",
        lambda *_args, **_kwargs: _Response(next(statuses), {"data": {"daily_remaining": 3}}),
    )
    monkeypatch.setattr(module.time, "sleep", waits.append)
    client = module.DBRestClient("https://example.test/v1", "key", tmp_path, 5, "testdb")

    assert client.usage() == {"daily_remaining": 3}
    assert waits == [1, 2]
    assert client.calls == 3


def test_provider_without_usage_endpoint_does_not_probe_it(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        return _Response(200, {"data": [{"ok": True}]})

    monkeypatch.setattr(module.requests, "get", fake_get)
    client = module.DBRestClient(
        "https://example.test/v1", "key", tmp_path, 5, "fudosandb",
        usage_endpoint=None,
    )
    assert client.get("/area-profile/13101") == [{"ok": True}]
    assert calls == ["https://example.test/v1/area-profile/13101"]
    assert client.calls == 1


def test_retry_stops_before_exceeding_local_daily_budget(tmp_path, monkeypatch):
    called = 0

    def fake_get(*_args, **_kwargs):
        nonlocal called
        called += 1
        return _Response(503)

    monkeypatch.setattr(module.requests, "get", fake_get)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    client = module.DBRestClient(
        "https://example.test/v1", "key", tmp_path, 1, "fudosandb",
        usage_endpoint=None,
    )

    assert client.get("/area-profile/13101") is None
    assert called == 1
    assert client.calls == 1


def test_live_usage_returns_daily_remaining_when_key_is_available(tmp_path):
    """CIではキーが無いのでskip。ローカルだけで実 API 契約を確認する。"""
    api_key = os.getenv("EDINETDB_API_KEY", "").strip()
    if not api_key:
        pytest.skip("EDINETDB_API_KEY がないCIでは実API往復を行わない")

    usage = module.DBRestClient(
        "https://edinetdb.jp/v1", api_key, tmp_path, 5, "edinetdb"
    ).usage()
    assert usage is not None
    assert "daily_remaining" in usage
