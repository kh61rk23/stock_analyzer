# news_pipeline — 0件フィードの理由を表示する（2026-09-26）
import types

import news_pipeline as m


def _run(monkeypatch, capsys, resp=None, exc=None):
    import requests

    def fake_get(*_a, **_k):
        if exc:
            raise exc
        return resp
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    feed = {"id": "x", "name": "X", "url": "http://x", "priority": "A", "lang": "en"}
    m.fetch_all_feeds([feed], types.SimpleNamespace(NEWS_FEEDS_ENABLED=None))
    return capsys.readouterr().out


def test_http_error_is_shown(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, resp=types.SimpleNamespace(status_code=403, content=b""))
    assert "X: 0件（HTTP 403）" in out


def test_exception_is_shown(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, exc=TimeoutError())
    assert "X: 0件（TimeoutError）" in out


def test_old_items_only_is_shown(monkeypatch, capsys):
    xml = (b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
           b"<item><title>a</title><link>http://a</link>"
           b"<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></channel></rss>")
    out = _run(monkeypatch, capsys, resp=types.SimpleNamespace(status_code=200, content=xml))
    assert "2日以内の記事なし" in out
