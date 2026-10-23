"""tech_profile: 技術ノードの熱度づけ・候補のまとめ・メールの節（BigQuery には触らない）。"""
from types import SimpleNamespace

import tech_profile as tp

HOT = {"semi_cap": {"combined": 82.0, "leading": 70, "semi_leading": 40},
       "power": {"combined": 40.0, "leading": 10, "semi_leading": 50}}
PROF = {"8035": [{"node_id": "semi_cap", "node_name_ja": "半導体製造装置", "relevance_score": 0.9},
                 {"node_id": "power", "node_name_ja": "電力", "relevance_score": 0.3}],
        "6857": [{"node_id": "semi_cap", "node_name_ja": "半導体製造装置", "relevance_score": 0.7}]}


def test_annotate_uses_combined_and_marks_leading_only():
    out = tp.annotate(PROF["8035"], HOT, ["semi_cap"])
    assert out[0]["hotness"] == 82.0 and out[0]["leading_only"] is True
    assert out[1]["hotness"] == 40.0 and out[1]["leading_only"] is False
    assert tp.annotate([{"node_id": "x", "relevance_score": 1}], {}, [])[0]["hotness"] is None


def test_digest_orders_nodes_by_heat_and_candidates_by_relevance():
    cands = [{"ticker": "6857.T", "name": "アドバンテスト"}, {"ticker": "8035.T", "name": "東京エレクトロン"},
             {"ticker": "9999.T", "name": "ノードなし"}]
    d = tp.digest(cands, PROF, HOT, ["semi_cap"], top_nodes=5, per_node=5)
    assert (d["candidates"], d["covered"]) == (3, 2)
    assert [n["node_id"] for n in d["nodes"]] == ["semi_cap", "power"]
    assert [c["ticker"] for c in d["nodes"][0]["candidates"]] == ["8035.T", "6857.T"]


def test_mail_section_is_rendered():
    import alert_notifier as an
    cfg = SimpleNamespace()
    tm = tp.digest([{"ticker": "8035.T", "name": "東京エレクトロン"}], PROF, HOT, ["semi_cap"])
    alerts = an.extract_alerts(cfg, {"tech_map": tm})
    sec = next(a for a in alerts if a["trigger"] == "tech_map")
    assert "半導体製造装置: 熱度82・先行だけ点灯" in sec["body"] and "東京エレクトロン（8035.T・関与0.90）" in sec["body"]


def test_packet_has_tech_section_even_without_data(monkeypatch):
    import agent_packet_exporter as ape
    monkeypatch.setattr(tp, "packet_lines", lambda cfg, t: ["", "## ⑬ 技術ノード（技術マップ）", "- データなし"])
    text = ape.build_packet_text("8035.T")
    assert "## ⑬ 技術ノード（技術マップ）" in text


def test_segment_path_drops_weak_matches():
    import taxonomy_company_mapper as m
    segs = {"8035": [{"segment_name": "半導体製造装置", "share": 0.9}, {"segment_name": "ＦＰＤ製造装置", "share": 0.1}]}
    sims = {"半導体製造装置": 0.91, "ＦＰＤ製造装置": 0.57}
    rows = m.build_company_node_rows(segs, lambda n: {"sector_code": "3650" if n.startswith("半") else "3650x",
                                                     "similarity_score": sims[n]},
                                     lambda sec: ["semi_cap"] if sec == "3650" else ["image_sensor", "mmwave_radar"],
                                     min_sim=0.75)
    assert {r["node_id"] for r in rows} == {"semi_cap"}       # 弱い当て方（0.57）の配下ノードは付けない
