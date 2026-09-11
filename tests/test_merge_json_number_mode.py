# MERGE の PARSE_JSON は wide_number_mode='round' でなければならない
#
# 2026-09-11 実測: edinet.financials への MERGE が11社中6社で 400 になり、
# BigQuery のジョブ履歴に残っていた理由は全件これだった。
#   Invalid input: Input number: 0.057161 cannot round-trip through
#   string representation; error in PARSE_JSON expression
# PARSE_JSON の既定は wide_number_mode='exact' で、FLOAT64/NUMERIC へ
# 正確に往復できない数値が **1つでも** あると JSON文書ごと弾く。
# ROE・利益率など小数5〜6桁の財務比率は日常的に該当するため、
# 既定のままでは「たまたま丸い数字の会社だけ入る」DBになる。
# 取り出しは SAFE_CAST(JSON_VALUE(...)) ＝文字列経由なので精度は落ちない。
import json

import pytest

import edinet_bq_client as module


class _Field:
    def __init__(self, name, field_type):
        self.name = name
        self.field_type = field_type


class _Table:
    def __init__(self, schema):
        self.schema = schema


class _Client:
    def __init__(self):
        self.calls = []

    def get_table(self, _ref):
        return _Table([
            _Field("f_id", "STRING"),
            _Field("roe", "FLOAT"),
            _Field("revenue", "INTEGER"),
        ])

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))

        class _Job:
            def result(self_inner):
                return []
        return _Job()


def _bq(fake):
    value = object.__new__(module.EDINETBQClient)
    value.project = "test-project"
    value.dataset = "test-project.edinet"
    value.client = fake
    return value


# 2026-09-11 の本番で実際に弾かれた値
REAL_FAILING_VALUES = [0.057161, -0.137364, 0.081948,
                       60026484.18059, 0.071943, 0.07093]


class TestWideNumberMode:
    def test_merge_sql_sets_round_mode(self):
        fake = _Client()
        _bq(fake).merge_rows("financials",
                             [{"f_id": "a", "roe": 0.057161}], ["f_id"])
        sql = fake.calls[0][0]
        assert "wide_number_mode => 'round'" in sql, (
            "既定の 'exact' のままだと小数の多い財務比率でMERGEが丸ごと400になる")

    def test_parse_json_is_not_left_at_the_default(self):
        fake = _Client()
        _bq(fake).merge_rows("financials",
                             [{"f_id": "a", "roe": 0.1}], ["f_id"])
        sql = fake.calls[0][0]
        assert "PARSE_JSON(@rows_json)" not in sql, "引数なしのPARSE_JSONが残っている"

    @pytest.mark.parametrize("value", REAL_FAILING_VALUES)
    def test_values_that_broke_production_are_sent_intact(self, value):
        """丸めるのはBQ側の解釈であって、送る値は加工しない"""
        fake = _Client()
        _bq(fake).merge_rows("financials",
                             [{"f_id": "a", "roe": value}], ["f_id"])
        payload = json.loads(fake.calls[0][1].query_parameters[0].value)
        assert payload[0]["roe"] == value


class TestUsFundamentalsShareTheFix:
    """同じ書き方をしている us.fundamentals 側も直っていること"""

    def test_us_merge_sql_sets_round_mode(self):
        import us_fundamental_rotator as us
        builder = next(
            (getattr(us, n) for n in dir(us)
             if n.startswith("_") and "merge" in n.lower()
             and callable(getattr(us, n))),
            None)
        assert builder is not None, "MERGE文を組む関数が見つからない"
        sql = builder("test-project.us.fundamentals")
        assert "wide_number_mode => 'round'" in sql
