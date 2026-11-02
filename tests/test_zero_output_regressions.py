"""構造的に0件しか出なかった機能の再発防止（萌芽テーマ検知・先行のみ点灯）。"""
from types import SimpleNamespace

import emerging_technology_detector as etd
import taxonomy_hotness as th


def test_emerging_uses_seed_keyword_map_when_config_has_none(monkeypatch):
    # config に TECH_KEYWORD_MAP が無くても、技術マップと同じ seed からキーワード表を作って調べる
    import technology_mapper
    monkeypatch.setattr(technology_mapper, "build_keyword_map_from_seeds",
                        lambda *a, **k: {"solid_state_battery": ["solid-state battery"]})
    papers = [{"title": "A solid-state battery breakthrough", "summary": "", "investment_score": 5}] * 3
    clusters = {"clusters": [{"phase": "acceleration", "avg_score": 0.8, "centroid_nodes": ["solid_state_battery"]}]}
    res = etd.build_emerging_cache({"arxiv": {"papers": papers}, "cluster_phase": clusters}, SimpleNamespace())
    assert not res.get("keyword_map_empty")
    themes = {t["node_id"]: t for t in res["emerging_themes"]}
    assert "solid_state_battery" in themes                     # investment_score を論文の点として読む
    assert themes["solid_state_battery"]["signals"]["arxiv"] > 0.5


def test_emerging_empty_keyword_map_is_marked(monkeypatch):
    import technology_mapper
    monkeypatch.setattr(technology_mapper, "build_keyword_map_from_seeds", lambda *a, **k: {})
    res = etd.build_emerging_cache({}, SimpleNamespace())
    assert res["keyword_map_empty"] and res["emerging_themes"] == []


def _day(lead, news=1):
    return {"n1": {"leading": lead, "news": news, "policy": 0}, "n2": {"leading": 0, "news": news, "policy": 0}}


def test_leading_ignores_days_without_papers_and_carries_forward():
    # 論文は月曜だけ（それ以外は全ノード0）。月曜の n1 は過去の月曜の平常（2件）と同じ → 平常の50
    hist = {}
    for wk in range(4):
        for dow in range(6):
            d = f"2026-09-{1 + wk * 7 + dow:02d}"
            hist[d] = _day(2 if dow == 0 else 0)
    monday = th.compute_node_hotness(_day(2), hist, "2026-09-29")
    assert monday["n1"]["leading"] == 50.0                    # 0の日で基準が薄まって点灯しない
    hist["2026-09-29"] = _day(6)                               # 月曜に平常の3倍
    tue = th.compute_node_hotness(_day(0), hist, "2026-09-30")
    assert tue["n1"]["leading"] == 100.0 and tue["n1"]["raw"]["leading"] == 6   # 翌日も引き継ぐ
    assert th.leading_only_nodes(tue, semi_th=101) == ["n1"]


def test_ignition_category_a_reads_real_rotation_keys():
    import ignition_detector as ign
    bundle = {"sector_rotation": {"quadrants": {"1016": "rising", "1001": "falling"},
                                  "sector_stats": {"1016": {"momentum": 1.2}, "1001": {"momentum": -0.5}}}}
    a = ign._extract_category_a("1016", bundle)
    assert a["score"] == 0.7 and any("初動" in s for s in a["signals"])
    assert ign._extract_category_a("1001", bundle)["score"] == 0.0
    # A が 0.7 まで届けば、B・C と合わせて閾値 3.0 を超えうる（以前は最大 2.6 で理論上発火しなかった）
    assert ign._calc_convergence_score({"score": 0.7}, {"score": 1.0}, {"score": 0.7}) >= 3.0


def test_emerging_old_version_cache_is_ignored(tmp_path):
    import json
    from datetime import datetime
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"generated": datetime.now().isoformat(), "version": "1.0", "emerging_themes": []}))
    assert etd._load_cache(p) is None                     # キーワード表が空だった版の0件は読まない
    p.write_text(json.dumps({"generated": datetime.now().isoformat(), "version": etd._MODULE_VERSION,
                             "emerging_themes": []}))
    assert etd._load_cache(p) is not None
