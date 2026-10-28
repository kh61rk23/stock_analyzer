"""地政学の権益の抽出（edinet_exposure_extractor）が読む節の名前が、有報本文の取り込みの節の名前と一致すること。
名前がずれると本文を1つも読めず、権益が手書きの十数社のままになる。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import edinet_gov_text


def test_target_sections_exist_in_text_blocks():
    known = set(edinet_gov_text.SECTIONS.values())
    assert config.GEO_EDINET_TARGET_SECTIONS
    assert set(config.GEO_EDINET_TARGET_SECTIONS) <= known, set(config.GEO_EDINET_TARGET_SECTIONS) - known


def test_all_listed_by_default():
    assert config.GEO_EDINET_ALL_LISTED is True


def test_stops_instead_of_per_ticker_queries_when_prefetch_fails(monkeypatch):
    import edinet_exposure_extractor as x
    calls = []
    monkeypatch.setattr(x, "prefetch_text_sections", lambda *a, **k: None)
    monkeypatch.setattr(x, "_get_edinet_text_sections", lambda *a, **k: calls.append(1) or {})
    for n in (50, 3):                                  # 社数が少なくても1社ずつには落とさない
        r = x.run_edinet_exposure_extractor(config, target_tickers=[f"{1000 + i}.T" for i in range(n)], dry_run=True)
        assert r["success"] is False and "まとめて読めなかった" in r["error"] and calls == []
