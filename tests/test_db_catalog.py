# 2026-10-03: 自然言語→SQL 用の DB構造台帳
import db_catalog_gen as g


def test_render_contains_tables_items_and_examples():
    schemas = {"edinet": {
        "financial_items": {"columns": [{"name": "item", "type": "STRING"}], "row_count": 10, "last_mod": None},
        "empty_table": {"columns": [{"name": "x", "type": "STRING"}], "row_count": 0, "last_mod": None}}}
    md = g.render(schemas)
    assert "`edinet.financial_items`" in md and "empty_table" not in md
    assert "`rnd_expenses` 研究開発費（円）" in md
    assert "```sql" in md and "asia-northeast1" in md
