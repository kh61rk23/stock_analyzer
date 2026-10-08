# ==============================================================
# tests/test_technology_mapper_keywords.py
# technology_mapper の TECH_KEYWORD_MAP 動的構築（seed正本）の純粋ロジック検証。
# TECH_KEYWORD_MAP は config に固定で持たず、承認済みseedから動的構築する。
# ==============================================================
import json

from conftest import load_module

tm = load_module("technology_mapper")


def _write_seed(tmp_path, filename, nodes):
    p = tmp_path / filename
    p.write_text(json.dumps({"_meta": {}, "nodes": nodes}, ensure_ascii=False),
                 encoding="utf-8")
    return p


def test_builds_map_from_seed_dicts(tmp_path):
    """① seed形式のdictリストからnode_id→keywordsのmapが構築される。"""
    _write_seed(tmp_path, "batch1_x.json", [
        {"node_id": "power_semiconductor",
         "node_name_ja": "パワー半導体",
         "node_name_en": "Power Semiconductor",
         "verified_flag": True},
    ])
    m = tm.build_keyword_map_from_seeds(tmp_path)
    assert "power_semiconductor" in m
    kws = m["power_semiconductor"]
    assert "パワー半導体" in kws
    assert "Power Semiconductor" in kws
    # node_id分割トークン
    assert "power" in kws
    assert "semiconductor" in kws


def test_excludes_unverified_nodes(tmp_path):
    """② verified_flag=false のノードは除外される。"""
    _write_seed(tmp_path, "batch1_x.json", [
        {"node_id": "verified_node", "node_name_ja": "承認", "node_name_en": "Verified",
         "verified_flag": True},
        {"node_id": "draft_node", "node_name_ja": "下書き", "node_name_en": "Draft",
         "verified_flag": False},
        {"node_id": "missing_flag_node", "node_name_ja": "無印", "node_name_en": "NoFlag"},
    ])
    m = tm.build_keyword_map_from_seeds(tmp_path)
    assert "verified_node" in m
    assert "draft_node" not in m
    assert "missing_flag_node" not in m


def test_node_id_tokens_drop_two_char_or_less(tmp_path):
    """③ node_id分割で2文字以下のトークンは除外される。"""
    _write_seed(tmp_path, "batch1_x.json", [
        {"node_id": "ai_ml_edge_gpu",
         "node_name_ja": "エッジ推論",
         "node_name_en": "Edge Inference",
         "verified_flag": True},
    ])
    m = tm.build_keyword_map_from_seeds(tmp_path)
    kws = m["ai_ml_edge_gpu"]
    # 2文字以下は除外
    assert "ai" not in kws
    assert "ml" not in kws
    # 3文字以上は残る
    assert "edge" in kws
    assert "gpu" in kws


def test_config_map_takes_priority(tmp_path):
    """④ config側にTECH_KEYWORD_MAPがあればそちらが優先される。"""
    class CfgWithMap:
        TECH_KEYWORD_MAP = {"custom_node": ["custom_kw"]}

    class CfgWithout:
        pass

    assert tm._get_keyword_map(CfgWithMap()) == {"custom_node": ["custom_kw"]}

    # config未定義（空dict）なら seedからの構築にフォールバック
    seed = tm.build_keyword_map_from_seeds()  # 実seed（293ノード）
    fallback = tm._get_keyword_map(CfgWithout())
    assert fallback == seed


def test_empty_seed_dir_returns_empty_without_crash(tmp_path):
    """⑤ seedが読めない場合はクラッシュせず空dictを返す。"""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    m = tm.build_keyword_map_from_seeds(empty_dir)
    assert m == {}
