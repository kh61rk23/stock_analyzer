import json
from datetime import datetime
from types import SimpleNamespace

import thematic_etf_tracker as te


def test_arxiv_down_stops_early_and_uses_recent_cache(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(te, "_arxiv_query", lambda *a, **k: calls.append(1) or (None, []))
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    (tmp_path / te._ARXIV_THEME_CACHE).write_text(
        json.dumps({"date": "2026-10-16", "themes": {"quantum": {"recent_4w": 9}}}), encoding="utf-8")
    cfg = SimpleNamespace(CACHE_DIR=str(tmp_path))
    out = te.fetch_arxiv_theme_counts(cfg, today=datetime(2026, 10, 17))
    assert out == {"quantum": {"recent_4w": 9}}
    assert len(calls) == 1


def test_arxiv_down_with_stale_cache_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "_arxiv_query", lambda *a, **k: (None, []))
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    (tmp_path / te._ARXIV_THEME_CACHE).write_text(
        json.dumps({"date": "2026-09-01", "themes": {"quantum": {}}}), encoding="utf-8")
    assert te.fetch_arxiv_theme_counts(SimpleNamespace(CACHE_DIR=str(tmp_path)),
                                       today=datetime(2026, 10, 17)) == {}
