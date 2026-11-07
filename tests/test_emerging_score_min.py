from types import SimpleNamespace

import emerging_technology_detector as ed

KW = {"ai_chip": ["ai chip", "accelerator"], "staffing_temp": ["temp", "人材派遣"]}


def _res(score=5.0):
    return {"arxiv": {"papers": [{"title": "Contemporaneous AI Chip Accelerator Design", "summary": "", "investment_score": score}]},
            "cluster_phase": {}}


def _cfg(**kw):
    base = dict(TECH_KEYWORD_MAP=KW, EMERGING_KEYWORD_SURGE_THRESHOLD=0.5)
    base.update(kw)
    return SimpleNamespace(**base)


def test_paper_only_signal_passes_default_min():
    # 重み固定のため論文だけの最大は 0.3 * 0.8 = 0.24。下限が 0.30 のままだと一度も出ない
    out = ed.build_emerging_cache(_res(), _cfg())["emerging_themes"]
    assert [t["node_id"] for t in out] == ["ai_chip"] and out[0]["emerging_score"] == 0.24
    assert ed.build_emerging_cache(_res(), _cfg(EMERGING_SCORE_MIN=0.30))["emerging_themes"] == []


def test_english_keyword_matches_whole_word_only():
    assert not ed._kw_in("contemporaneous transmission", "temp")
    assert ed._kw_in("temp staffing demand", "temp") and ed._kw_in("ai chips rise", "ai chip")
    assert ed._kw_in("人材派遣の需要", "人材派遣")


def test_top_n_caps_ties():
    kw = {f"n{i}": ["quantum"] for i in range(30)}
    res = {"arxiv": {"papers": [{"title": "quantum", "summary": "", "investment_score": 5}]}, "cluster_phase": {}}
    out = ed.build_emerging_cache(res, _cfg(TECH_KEYWORD_MAP=kw, EMERGING_TOP_N=10))["emerging_themes"]
    assert len(out) == 10 and [t["rank"] for t in out] == list(range(1, 11))
    assert len(ed.build_emerging_cache(res, _cfg(TECH_KEYWORD_MAP=kw, EMERGING_TOP_N=0))["emerging_themes"]) == 30
