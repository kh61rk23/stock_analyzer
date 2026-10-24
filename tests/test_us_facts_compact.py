"""us_company_facts: 縦持ちの値は tag_id で持ち、項目名は us.fact_tags に分ける。"""
import us_company_facts as ucf


def test_compact_row_assigns_stable_ids_and_collects_new_tags():
    tags = ucf.TagIds()
    tags.ids = {("us-gaap", "Revenues"): 7}
    tags.next = 8
    a = ucf.compact_row({"cik": 1, "taxonomy": "us-gaap", "tag": "Revenues", "val": 1.0}, tags)
    b = ucf.compact_row({"cik": 1, "taxonomy": "dei", "tag": "EntityCommonStockSharesOutstanding", "val": 2.0}, tags)
    c = ucf.compact_row({"cik": 2, "taxonomy": "dei", "tag": "EntityCommonStockSharesOutstanding", "val": 3.0}, tags)
    assert a == {"cik": 1, "val": 1.0, "tag_id": 7}
    assert b["tag_id"] == c["tag_id"] == 8 and "tag" not in b and "taxonomy" not in b
    assert tags.pending == [{"tag_id": 8, "taxonomy": "dei", "tag": "EntityCommonStockSharesOutstanding"}]


def test_schema_and_names():
    names = [f.name for f in ucf.facts_schema()]
    assert "tag_id" in names and "tag" not in names and "taxonomy" not in names
    assert ucf.FACTS_TABLE == "company_facts_v2" and ucf.FACTS_VIEW == "company_facts" and ucf.TAGS_TABLE == "fact_tags"
