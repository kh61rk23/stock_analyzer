"""master_tables：ノードの大中小の段階の列・業種マスタ・元の名前の view。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import master_tables as mt


NODES = [
    {"node_id": "semi", "parent_id": None, "node_level": 1, "node_name_ja": "半導体"},
    {"node_id": "design", "parent_id": "semi", "node_level": 2, "node_name_ja": "半導体設計"},
    {"node_id": "logic", "parent_id": "semi", "node_level": 2, "node_name_ja": "ロジック"},
    {"node_id": "dc_ai", "parent_id": "logic", "node_level": 3, "node_name_ja": "DC向けAIチップ"},
    {"node_id": "orphan", "parent_id": "nope", "node_level": 2, "node_name_ja": "迷子"},
]


def test_node_paths_fill_upper_levels():
    p = mt.node_paths(NODES)
    assert p["dc_ai"] == {"level_name_ja": "小分類", "l1_id": "semi", "l1_name_ja": "半導体", "l2_id": "logic",
                          "l2_name_ja": "ロジック", "l3_id": "dc_ai", "l3_name_ja": "DC向けAIチップ",
                          "path_name_ja": "半導体 > ロジック > DC向けAIチップ"}
    assert p["design"]["l3_id"] is None and p["design"]["level_name_ja"] == "中分類"
    assert p["orphan"]["l1_id"] is None and p["orphan"]["l2_id"] == "orphan"


def test_node_paths_survive_cycles():
    loop = [{"node_id": "a", "parent_id": "b", "node_level": 2}, {"node_id": "b", "parent_id": "a", "node_level": 1}]
    assert mt.node_paths(loop)["a"]["l1_id"] == "b"


def test_industry_rows_and_view():
    import config
    rows = mt.industry_rows(config.JAPAN_SECTORS)
    assert len(rows) == 33 and rows[15] == {"sector_code": "1016", "name_ja": "電気機器",
                                           "name_en": rows[15]["name_en"], "group_ja": rows[15]["group_ja"]}
    sql = mt.legacy_view_sql("p")
    assert sql.startswith("CREATE OR REPLACE VIEW `p.taxonomy.taxonomy_node`") and "`p.master.tax_node`" in sql
    assert "l1_id" not in sql                                  # 元の名前の view は元の列だけ


def test_loader_rows_carry_levels():
    from taxonomy_loader import build_bq_rows, build_merge_sql
    rows = build_bq_rows(NODES[:3], as_of="2026-10-28")
    assert rows[2]["l2_id"] == "logic" and rows[2]["level_name_ja"] == "中分類"
    sql = build_merge_sql("p")
    assert "`p.master.tax_node`" in sql and "l3_name_ja = S.l3_name_ja" in sql


def test_level_table_sql_has_upper_ids():
    l1 = mt.level_table_sql("p", 1)
    assert "`p.master.tax_node_l1`" in l1 and "n.node_id AS l1_id," in l1 and "WHERE n.node_level = 1" in l1
    l3 = mt.level_table_sql("p", 3)
    assert "n.node_id AS l3_id, l2_id, l1_id" in l3 and "0 AS child_count" in l3 and "小分類マスタ" in l3
    assert "c.node_level = 3" in mt.level_table_sql("p", 2)
    assert set(mt.LEVEL_TABLES) == {1, 2, 3} and [x["level"] for x in mt.LEVELS] == [1, 2, 3]


def test_seed_has_three_levels_only():
    import glob, json
    for p in glob.glob(os.path.join(os.path.dirname(mt.__file__), "data", "taxonomy_seed", "*.json")):
        for n in json.load(open(p, encoding="utf-8")).get("nodes") or []:
            assert n["node_level"] in (1, 2, 3), (p, n["node_id"])
