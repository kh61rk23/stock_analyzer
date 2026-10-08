# congress_monitor
#   ・キー未設定時に RSS のニュース見出しを「法案」として扱わない
#   ・逆風法案（base_score がマイナス定義）を二重反転して追い風にしない
#   ・同じ種別の法案を本数分足して業種加点を上限に張り付かせない
import json
import types

import config
import congress_monitor as cm


def _cfg(tmp_path):
    return types.SimpleNamespace(
        CACHE_DIR=tmp_path, POLICY_SECTOR_IMPACT_MAP=config.POLICY_SECTOR_IMPACT_MAP,
        CONGRESS_STAGE_MULTIPLIER=config.CONGRESS_STAGE_MULTIPLIER,
        SCORE_MAX_POLICY_L4_ADJ_W4=25.0, CONGRESS_API_KEY="", CONGRESS_SESSION=119)


def test_headwind_bills_are_negative(tmp_path):
    bills = [{"stage": "signed", "bill_types": ["tariff_steel"]},
             {"stage": "signed", "bill_types": ["financial_regulation"]}]
    b = cm._calc_sector_boosts(bills, _cfg(tmp_path))
    assert b["1012"] == -10.0 and b["1028"] == -6.0


def test_same_type_bills_do_not_stack(tmp_path):
    bills = [{"stage": "committee", "bill_types": ["infrastructure"]}] * 20 + \
            [{"stage": "passed", "bill_types": ["infrastructure"]}]
    b = cm._calc_sector_boosts(bills, _cfg(tmp_path))
    assert b["1003"] == 7.2, "最も進んだ1本（passed 8×0.9）だけを使う"


def test_word_boundary_keywords(tmp_path):
    cfg = _cfg(tmp_path)
    assert cm._classify_bill_type("Greenville Post Office naming", "", cfg) == []
    assert cm._classify_bill_type("CHIPS and Science Act amendments", "", cfg) == ["semiconductor_subsidy"]


def test_uses_official_api_with_demo_key_and_drops_rss_cache(tmp_path, monkeypatch):
    d = tmp_path / "congress"
    d.mkdir()
    (d / "congress_bill_cache.json").write_text(json.dumps({"bills": {
        "RSS_1": {"source": "rss_fallback", "title": "Taiwan Plans Record Defense Budget"}}}),
        encoding="utf-8")
    seen = {}

    def fake_fetch(api_key, congress=119, limit=250, timeout=30):
        seen["key"] = api_key
        return [{"bill_id": "S1071/119", "title": "National Defense Authorization Act",
                 "latest_action": "Became Public Law", "stage": "signed"}]
    monkeypatch.setattr(cm, "_fetch_recent_bills", fake_fetch)
    monkeypatch.setattr(cm, "_congress_cache_dir", lambda cfg: d)
    r = cm.run_congress_monitor(_cfg(tmp_path))
    assert r["fetched_count"] == 1 and r["bill_count"] == 1
    assert r["total_tracked"] == 1, "RSS 由来の偽法案がキャッシュに残っている"
    assert r["sector_boosts"]["1016"] == 10.0


def test_job_surge_needs_baseline():
    """求人票: 前回件数が無いのに『急増』扱いしない"""
    import job_posting_monitor as jp
    cfg = types.SimpleNamespace(JOB_SURGE_THRESHOLD_RATIO=1.5, JOB_SURGE_SCORE_BONUS=5.0,
                                JOB_SURGE_SECTOR_MAP={"ai_ml": ["1025"]})
    assert jp._detect_surge({"ai_ml": 24}, {}, cfg) == ([], {})
    assert jp._detect_surge({"ai_ml": 24}, {"ai_ml": 10}, cfg)[0] == ["ai_ml"]
