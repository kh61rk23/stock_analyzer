"""DB統合向けBigQuery MERGE・全文検索契約を検証する。"""

import json

from types import SimpleNamespace

import edinet_bq_client as module


class _Row(dict):
    pass


class _Job:
    def __init__(self, rows=()):
        self._rows = rows

    def result(self):
        return self._rows


class _Client:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return _Job(self.rows)

    def get_table(self, _table_ref):
        fields = [
            SimpleNamespace(name="text_id", field_type="STRING"),
            SimpleNamespace(name="raw_text", field_type="STRING"),
            SimpleNamespace(name="updated_at", field_type="TIMESTAMP"),
        ]
        return SimpleNamespace(schema=fields)


def _bq(fake_client):
    value = object.__new__(module.EDINETBQClient)
    value.project = "test-project"
    value.dataset = "test-project.edinet"
    value.client = fake_client
    return value


def test_merge_rows_uses_keyed_bigquery_merge_and_parameterized_json():
    fake = _Client()
    bq = _bq(fake)

    assert bq.merge_rows(
        "text_analysis",
        [{"text_id": "id-1", "raw_text": "全文", "updated_at": "2026-07-21T00:00:00Z"}],
        ["text_id"],
    ) is True

    sql, config = fake.calls[0]
    assert "MERGE `test-project.edinet.text_analysis`" in sql
    assert "ON T.`text_id` = S.`text_id`" in sql
    assert "WHEN MATCHED THEN UPDATE SET" in sql
    assert config.query_parameters[0].name == "rows_json"


def test_company_full_text_search_is_parameterized_and_bounded():
    fake = _Client([_Row(section_type="事業等のリスク", raw_text="半導体不足")])
    bq = _bq(fake)

    rows = bq.search_company_text("7203.T", "半導体", limit=999)

    assert rows == [{"section_type": "事業等のリスク", "raw_text": "半導体不足"}]
    sql, config = fake.calls[0]
    assert "NORMALIZE_AND_CASEFOLD(@query)" in sql
    params = {parameter.name: parameter.value for parameter in config.query_parameters}
    assert params == {"company_code": "7203", "query": "半導体", "limit": 200}


def test_merge_rows_deduplicates_source_keys_with_last_row_winning():
    fake = _Client()
    bq = _bq(fake)

    bq.merge_rows("text_analysis", [
        {"text_id": "same", "raw_text": "old"},
        {"text_id": "same", "raw_text": "new"},
    ], ["text_id"])

    payload = json.loads(fake.calls[0][1].query_parameters[0].value)
    assert payload == [{"text_id": "same", "raw_text": "new"}]
