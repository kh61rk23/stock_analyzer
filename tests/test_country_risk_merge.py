# country_risk_updater._build_risk_merge_query — BQ MERGE クエリ生成（純粋関数）
# 回帰固定: 型付き配列リテラル UNNEST(ARRAY<STRUCT<fields>>[ (values) ]) を使うこと。
#   UNNEST([ STRUCT <fields> ]) の形は BigQuery が構文エラー
#   （Expected "<" but got identifier）にし、国別リスク更新が 0 件になる。
from conftest import load_module

cru = load_module("country_risk_updater")


_ROW = {
    "country_code":           "USA",
    "risk_date":              "2026-06-25",
    "gdelt_instability":      1.5,
    "foreign_ministry_level": 2,
    "state_dept_level":       1,
    "composite_risk_score":   55.0,
}


class TestBuildRiskMergeQuery:
    def test_empty_rows_returns_empty(self):
        assert cru._build_risk_merge_query("proj", []) == ""

    def test_typed_struct_array_syntax(self):
        sql = cru._build_risk_merge_query("proj", [_ROW])
        # 旧バグ（STRUCT の後に "<" が無い）を恒久的に防ぐ
        assert "UNNEST(ARRAY<STRUCT<" in sql
        assert ">>[" in sql
        # 型付き STRUCT は < と > が同数で閉じる（ARRAY< と STRUCT< の2つ）
        assert sql.count("<") == sql.count(">")

    def test_field_declarations_present(self):
        sql = cru._build_risk_merge_query("proj", [_ROW])
        for decl in (
            "country_code STRING",
            "risk_date DATE",
            "gdelt_instability FLOAT64",
            "foreign_ministry_level INT64",
            "state_dept_level INT64",
            "composite_risk_score FLOAT64",
            "updated_at TIMESTAMP",
        ):
            assert decl in sql

    def test_value_row_rendered(self):
        sql = cru._build_risk_merge_query("proj", [_ROW])
        assert "'USA'" in sql
        assert "DATE('2026-06-25')" in sql
        assert "proj.geopolitics.country_risk_daily" in sql

    def test_single_quote_is_escaped(self):
        row = dict(_ROW, country_code="O'Brien")
        sql = cru._build_risk_merge_query("proj", [row])
        assert "O\\'Brien" in sql
