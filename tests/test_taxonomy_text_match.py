# ==============================================================
# tests/test_taxonomy_text_match.py — 会社→taxonomyノードの語の照合経路
# ==============================================================
# 対象: taxonomy_company_mapper の text_match 経路
#   語の作り方（seed の名称・説明 → 照合語、除外語）／本文・セグメント名の数え方／
#   採用条件／行の形（根拠・承認状態）／MERGE 文（キーと失効）／読み手の優先順位
# ==============================================================
import inspect
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import taxonomy_company_mapper as tcm                                   # noqa: E402
from taxonomy_company_mapper import (build_merge_sql, build_node_terms,  # noqa: E402
                                     build_text_match_rows, count_terms,
                                     load_keyword_rules, mask_phrases,
                                     match_company_nodes, _compile_terms)
from taxonomy_loader import load_seed_batches, select_approved_nodes    # noqa: E402


def _node(nid, ja, desc="", en=""):
    return {"node_id": nid, "node_name_ja": ja, "node_name_en": en,
            "description": desc}


# ── 語の作り方 ───────────────────────────────────────────────

class TestBuildNodeTerms:
    def test_name_split_and_paren(self):
        t = build_node_terms([_node("hbm", "HBM(広帯域メモリ)")])["hbm"]
        assert "HBM" in t and "広帯域メモリ" in t

    def test_name_parts_split_on_nakaguro(self):
        t = build_node_terms([_node("rubber_tire", "ゴム・タイヤ")])["rubber_tire"]
        assert {"ゴム・タイヤ", "ゴム", "タイヤ"} <= set(t)

    def test_description_head_only_and_phrases_dropped(self):
        n = _node("steel", "鉄鋼", "高炉・電炉・特殊鋼（鉄鉱石/原料炭スプレッドと鋼材市況）")
        t = build_node_terms([n], {"min_desc_term_len": 2})["steel"]
        assert {"高炉", "電炉", "特殊鋼"} <= set(t)
        assert not any("鉄鉱石" in x for x in t)      # 括弧内の分析コメントは使わない

    def test_description_with_particles_dropped(self):
        n = _node("cobot", "協働ロボット", "人と同一空間で作業する安全柵不要ロボット")
        assert build_node_terms([n])["cobot"] == ["協働ロボット"]

    def test_short_description_terms_need_min_len(self):
        n = _node("x", "サンプル名", "製造・材料・半導体部材")
        t = build_node_terms([n], {"min_desc_term_len": 3})["x"]
        assert "製造" not in t and "材料" not in t and "半導体部材" in t

    def test_ascii_terms_need_letters(self):
        n = _node("pkg", "先端実装(2.5D/3D)")
        assert build_node_terms([n])["pkg"] == ["先端実装"]

    def test_exclude_global_and_by_node(self):
        nodes = [_node("a", "通信・光通信"), _node("b", "リサイクル")]
        rules = {"exclude_terms": ["通信"], "exclude_terms_by_node": {"b": ["リサイクル"]}}
        t = build_node_terms(nodes, rules)
        assert "通信" not in t["a"] and "光通信" in t["a"]
        assert "b" not in t                           # 語が全部消えたノードは出さない

    def test_longest_first(self):
        t = build_node_terms([_node("p", "パワー半導体・半導体")])["p"]
        assert t[0] == "パワー半導体・半導体"


# ── 数え方 ───────────────────────────────────────────────────

class TestCountTerms:
    def test_fullwidth_normalized(self):
        pat = _compile_terms(["SiC"])
        assert count_terms("ＳｉＣパワーデバイスとＳｉＣ基板", pat) == {"SiC": 2}

    def test_longest_match_not_double_counted(self):
        pat = _compile_terms(["半導体", "パワー半導体"])
        assert count_terms("パワー半導体と半導体", pat) == {"パワー半導体": 1, "半導体": 1}

    def test_ascii_word_boundary_and_case(self):
        pat = _compile_terms(["SDN", "ADAS"])
        assert count_terms("Foo Sdn. Bhd. と ADASX と ADAS", pat) == {"ADAS": 1}

    def test_mask_phrases(self):
        pat = _compile_terms(["外食"])
        assert count_terms("海外食料品の卸売", pat) == {"外食": 1}
        assert count_terms(mask_phrases("海外食料品の卸売", ["海外食"]), pat) == {}


# ── 採用条件 ─────────────────────────────────────────────────

TERMS = {"semi": ["半導体"], "glass": ["セメント", "ガラス", "耐火物"]}


class TestMatchCompanyNodes:
    def test_segment_hit_accepted_with_share(self):
        m = match_company_nodes({}, {"1111": [{"segment_name": "半導体事業", "share": 0.4},
                                              {"segment_name": "その他", "share": 0.6}]}, TERMS)
        ev = m["1111"]["semi"]
        assert ev["seg_share"] == 0.4 and ev["seg_hits"][0][2] == "半導体"

    def test_missing_share_split_equally(self):
        m = match_company_nodes({}, {"1111": [{"segment_name": "半導体", "share": None},
                                              {"segment_name": "その他", "share": None}]}, TERMS)
        assert m["1111"]["semi"]["seg_share"] == 0.5

    def test_text_needs_min_hits(self):
        texts = {"2222": {"事業の内容": "半導体と半導体"}}
        assert match_company_nodes(texts, {}, TERMS, min_text_hits=3) == {}
        texts = {"2222": {"事業の内容": "半導体と半導体と半導体"}}
        assert match_company_nodes(texts, {}, TERMS, min_text_hits=3)["2222"]["semi"]["text_hits"] == 3

    def test_list_of_single_mentions_rejected(self):
        texts = {"8001": {"事業の内容": "取扱品目: セメント、ガラス、耐火物、家具"}}
        assert match_company_nodes(texts, {}, TERMS, 3, 2) == {}
        assert "glass" in match_company_nodes(texts, {}, TERMS, 3, 1)["8001"]

    def test_none_company_skipped(self):
        assert match_company_nodes({}, {None: [{"segment_name": "半導体", "share": 1.0}]},
                                   TERMS) == {}


# ── 行の形 ───────────────────────────────────────────────────

class TestBuildTextMatchRows:
    def _rows(self):
        texts = {"3333": {"事業の内容": "半導体" * 4 + "ガラス" * 3}}
        segs = {"3333": [{"segment_name": "半導体", "share": 0.7}]}
        return build_text_match_rows(match_company_nodes(texts, segs, TERMS), as_of="2026-10-19")

    def test_candidate_not_verified(self):
        for r in self._rows():
            assert r["confidence_type"] == "text_match"
            assert r["verified_flag"] is False and r["auto_derived"] is True
            assert r["confidence"] < 0.7               # セグメント経路（edinet=0.7）より下
            assert r["valid_to"] is None

    def test_relevance_normalized_per_company(self):
        rows = self._rows()
        assert abs(sum(r["relevance_score"] for r in rows) - 1.0) < 1e-3
        by = {r["node_id"]: r for r in rows}
        assert by["semi"]["relevance_score"] > by["glass"]["relevance_score"]

    def test_rationale_keeps_evidence(self):
        by = {r["node_id"]: r for r in self._rows()}
        assert "seg:半導体(70%)[半導体]" in by["semi"]["rationale"]
        assert "text:事業の内容 半導体×4" in by["semi"]["rationale"]
        assert by["glass"]["revenue_share_est"] is None
        assert by["semi"]["revenue_share_est"] == 0.7

    def test_schema_keys_match_table(self):
        # edinet_bq_schema は Colab 前提の import を持つので、定義はテキストとして読む
        src = (REPO / "edinet_bq_schema.py").read_text(encoding="utf-8")
        block = src[src.index('"company_taxonomy_map"'):]
        block = block[:block.index(')"""')]
        for k in self._rows()[0]:
            assert f"{k} " in block, k


# ── MERGE 文 ─────────────────────────────────────────────────

class TestMergeSql:
    def test_key_includes_confidence_type(self):
        sql = build_merge_sql("p", "p.taxonomy._stg", "text_match", "2026-10-18")
        assert "T.confidence_type = 'text_match'" in sql
        assert "WHEN NOT MATCHED BY SOURCE" in sql and "DATE '2026-10-18'" in sql
        assert "valid_to = NULL" in sql

    def test_segment_path_never_retires(self):
        sql = build_merge_sql("p", "p.taxonomy._stg", "edinet")
        assert "T.confidence_type = 'edinet'" in sql
        assert "NOT MATCHED BY SOURCE" not in sql and "valid_to" not in sql

    def test_upsert_uses_load_job_not_streaming(self):
        src = inspect.getsource(tcm.upsert_to_bq)
        assert "load_table_from_file" in src
        assert "insert_rows" not in src


# ── 実 seed と実ルール ───────────────────────────────────────

class TestRealSeedAndRules:
    def test_rules_load_and_node_keys_exist(self):
        rules = load_keyword_rules()
        assert rules.get("sections") and rules.get("min_text_hits")
        nodes, _ = select_approved_nodes(load_seed_batches())
        ids = {n["node_id"] for n in nodes}
        unknown = set(rules.get("exclude_terms_by_node", {})) - ids
        assert not unknown, unknown

    def test_real_terms_cover_most_nodes_and_drop_generic(self):
        rules = load_keyword_rules()
        nodes, _ = select_approved_nodes(load_seed_batches())
        terms = build_node_terms(nodes, rules)
        assert len(terms) >= 250
        assert "通信" not in terms["communications"]
        assert "証券" not in terms.get("securities_brokerage", [])
        assert "リサイクル" not in terms.get("ev_battery", [])
        assert "パワー半導体" in terms["power_semiconductor"]


# ── 読み手との関係 ───────────────────────────────────────────

class TestReaders:
    def test_readers_prefer_verified_then_edinet(self):
        import screener_universe
        import edinet_bq_client
        for src in (inspect.getsource(screener_universe),
                    inspect.getsource(edinet_bq_client.EDINETBQClient.get_taxonomy_scores_bulk)):
            assert "WHEN verified_flag THEN 0" in src
            assert "confidence_type = 'edinet' THEN 1" in src

    def test_readers_skip_retired_rows(self):
        import edinet_bq_client
        src = inspect.getsource(edinet_bq_client.EDINETBQClient.get_taxonomy_scores_bulk)
        assert "valid_to IS NULL OR valid_to >=" in src

    def test_weekly_step_runs_mapper(self):
        wf = (REPO / ".github" / "workflows" / "daily_run.yml").read_text(encoding="utf-8")
        assert "python -u taxonomy_company_mapper.py" in wf


def test_company_key_rejects_nan_and_bad_codes():
    import taxonomy_company_mapper as m
    assert m.company_key("7203") == "7203" and m.company_key("409a") == "409A"
    for bad in (None, float("nan"), "nan", "None", "", "40950", "E05633"):
        assert m.company_key(bad) == ""


def test_fallback_rows_relaxed_then_industry():
    import taxonomy_company_mapper as m
    listed = {"1111": "小売業", "2222": "小売業", "3333": None, "4444": "銀行業", "5555": "小売業"}
    tagged = {"4444"}
    relaxed = {"1111": {"ec_platform": {"text_hits": 2, "text_terms": {"事業の内容": {"EC": 2}},
                                        "seg_share": 0.0, "seg_hits": []},
                        "drugstore": {"text_hits": 1, "text_terms": {"事業の内容": {"ドラッグ": 1}},
                                      "seg_share": 0.0, "seg_hits": []}},
               "5555": {"not_a_node": {"text_hits": 5, "text_terms": {}, "seg_share": 0.0, "seg_hits": []}}}
    known = {"ec_platform", "drugstore", "specialty_retail", "banking"}
    rows = m.build_fallback_rows(listed, tagged, relaxed, {"小売業": "specialty_retail", "銀行業": "banking"},
                                 known, as_of="2026-10-27", max_nodes=3)
    by = {}
    for r in rows:
        by.setdefault(r["company_id"], []).append(r)
    assert [r["node_id"] for r in by["1111"]] == ["ec_platform", "drugstore"]
    assert abs(sum(r["relevance_score"] for r in by["1111"]) - 1.0) < 1e-3
    assert by["1111"][0]["evidence_source"] == "fallback:relaxed"
    # 照合の当たりが無い・知らないノードだけの会社は業種から1つ
    assert [(r["node_id"], r["relevance_score"], r["evidence_source"]) for r in by["2222"]] == \
        [("specialty_retail", 1.0, "fallback:industry")]
    assert by["5555"][0]["node_id"] == "specialty_retail"
    assert "3333" not in by and "4444" not in by          # 業種が無い・すでにタグがある
    assert all(r["confidence_type"] == "fallback" and r["confidence"] == 0.3 for r in rows)


def test_industry_fallback_map_points_to_approved_nodes():
    import json
    import taxonomy_company_mapper as m
    from taxonomy_loader import load_seed_batches, select_approved_nodes
    nodes, _ = select_approved_nodes(load_seed_batches())
    ids = {n["node_id"] for n in nodes}
    fb = json.loads(m.FALLBACK_RULES_PATH.read_text(encoding="utf-8"))
    bad = {k: v for k, v in fb["industry_to_node"].items() if v not in ids}
    assert not bad, bad
    assert len(fb["industry_to_node"]) == 33


def test_segment_rows_dropped_when_sector_differs_from_company():
    import taxonomy_company_mapper as m
    segs = {"7201": [{"segment_name": "Transportation", "share": 0.9}],
            "6758": [{"segment_name": "エレクトロニクス", "share": 1.0}],
            "9999": [{"segment_name": "エレクトロニクス", "share": 1.0}]}
    mapping = {"Transportation": {"sector_code": "1016", "similarity_score": 0.85},
               "エレクトロニクス": {"sector_code": "1016", "similarity_score": 0.9}}
    rows = m.build_company_node_rows(segs, mapping.get, lambda code: ["semiconductor_logic"] if code == "1016" else [],
                                     min_sim=0.75, company_sector={"7201": "1017", "6758": "1016"})
    assert sorted({r["company_id"] for r in rows}) == ["6758", "9999"]     # 業種の分からない 9999 は検査しない


def test_industry_code():
    import taxonomy_company_mapper as m
    assert m.industry_code("電気機器") == "1016" and m.industry_code("輸送用機器") == "1017"
    assert m.industry_code("証券、商品先物取引業") == "1029"
    assert m.industry_code(None) == "" and m.industry_code("不明") == ""


def test_sector_bridge_codes_match_config_names():
    import config
    import size_tier_classifier as s
    want = {"1016": "電気機器", "1018": "精密機器", "1025": "情報・通信業", "1015": "機械", "1017": "輸送用機器",
            "1007": "化学", "1026": "卸売業", "1028": "銀行業", "1033": "サービス業"}
    assert set(s._SECTOR_TO_TAXONOMY) == set(want)
    for code, name in want.items():
        assert config.JAPAN_SECTORS[code]["name_ja"] == name
