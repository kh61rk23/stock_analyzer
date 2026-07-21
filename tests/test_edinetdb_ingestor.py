"""EdinetDBの優先選定と既存BQスキーマ写像を検証する。"""

from datetime import date
from types import SimpleNamespace

from conftest import load_module


module = load_module("edinetdb_ingestor")


def test_select_ingest_targets_prioritizes_and_deduplicates_with_budget():
    selected = module.select_ingest_targets(
        ["E00001", "E00002", "E00003"],
        ["E00003", "E00004"],
        ["E00005"],
        ["E00006"],
        {},
        budget=7,
        calls_per_company=3,
        today=date(2026, 7, 21),
    )
    assert selected == ["E00001", "E00002"]


def test_budget_smaller_than_one_company_issues_no_target():
    assert module.select_ingest_targets(
        ["E00001"], [], [], [], {}, budget=2, calls_per_company=3
    ) == []


def test_w3_reserves_usage_and_doc_index_calls():
    cfg = SimpleNamespace(EDINETDB_DOC_INDEX_RESERVE=11)
    assert module._w3_company_budget(cfg, 90) == 77


def test_explicit_budget_five_plans_one_company_without_network(tmp_path):
    (tmp_path / "portfolio_holdings.csv").write_text(
        "ticker,market\n7203.T,JP\n6758.T,JP\n", encoding="utf-8"
    )
    cfg = SimpleNamespace(
        ENABLE_EDINETDB_INGEST=True,
        EDINETDB_DAILY_BUDGET=90,
        EDINETDB_REFRESH_DAYS=180,
        DATA_DIR=tmp_path,
        SECTOR_REPRESENTATIVE_TICKERS={},
    )

    result = module.run_edinetdb_ingest(cfg, budget=5, dry_run=True)

    assert result["targets"] == ["7203.T"]
    assert result["budget"] == 5


def test_select_ingest_targets_skips_fresh_company():
    selected = module.select_ingest_targets(
        ["E00001", "E00002"], [], [], [],
        {"E00001": {"last_fetched": "2026-07-20"}},
        budget=6,
        calls_per_company=3,
        refresh_days=180,
        today=date(2026, 7, 21),
    )
    assert selected == ["E00002"]


def test_p4_rotation_uses_oldest_fetch_first():
    selected = module.select_ingest_targets(
        [], [], [], ["E_NEW", "E_OLD"],
        {
            "E_NEW": {"last_fetched": "2025-10-01"},
            "E_OLD": {"last_fetched": "2025-01-01"},
        },
        budget=3,
        calls_per_company=3,
        refresh_days=1,
        today=date(2026, 7, 21),
    )
    assert selected == ["E_OLD"]


def test_financial_mapping_uses_openapi_fields_without_guessing_fye():
    row = module.map_edinetdb_financials(
        {
            "fiscal_year": 2025,
            "submit_date": "2025-06-20",
            "revenue": 1000,
            "operating_income": 100,
            "ordinary_income": 90,
            "net_income": 60,
            "total_assets": 2000,
            "net_assets": 800,
            "cash": 200,
            "cf_operating": 150,
            "cf_investing": -40,
            "ibd_current": 10,
            "ibd_noncurrent": 90,
            "num_employees": 20,
            "roe_official": 0.075,
        },
        edinet_code="E02144",
        company_code="7203",
        now="2026-07-21T00:00:00+00:00",
    )
    assert row["fin_id"] == "E02144_2025"
    assert row["fiscal_year"] == 2025
    assert row["fiscal_year_end"] is None
    assert row["net_sales"] == 1000.0
    assert row["interest_bearing_debt"] == 100.0
    assert row["free_cf"] == 110.0
    assert row["source"] == "edinetdb"


def test_segment_mapping_writes_both_existing_tables():
    segments, finance = module.map_edinetdb_segments(
        "E02144",
        [{
            "segmentName": "自動車",
            "segmentType": "reportable",
            "fiscalYear": 2025,
            "revenue": 500,
            "operatingIncome": 50,
            "revenueShare": 50.0,
            "oiMargin": 10.0,
        }],
        2025,
        company_code="7203",
        now="2026-07-21T00:00:00+00:00",
    )
    assert len(segments) == len(finance) == 1
    assert segments[0]["segment_name_norm"] == "自動車"
    assert finance[0]["segment_id"] == segments[0]["segment_id"]
    assert finance[0]["revenue_ratio"] == 0.5
    assert finance[0]["operating_margin"] == 0.1


def test_text_block_mapping_preserves_raw_text():
    rows = module.map_edinetdb_text_blocks(
        "E02144", [{"section": "事業等のリスク", "text": "原材料価格が変動する。"}], 2025,
        company_code="7203", now="2026-07-21T00:00:00+00:00",
    )
    assert rows[0]["raw_text"] == "原材料価格が変動する。"
    assert rows[0]["source"] == "edinetdb"


def test_text_block_request_fetches_all_sections_in_full():
    assert module.text_block_params(2025) == {"full": "true", "fiscal_year": 2025}
    assert module.text_block_params(None) == {"full": "true"}


def test_ingest_calls_real_individual_contracts_and_merges_all_tables(
    tmp_path, monkeypatch
):
    class FakeRest:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0
            self.requests = []
            captured["rest"] = self

        def usage(self):
            self.calls += 1
            return {"daily_remaining": 20, "monthly_remaining": 200}

        def get(self, path, params=None):
            self.calls += 1
            self.requests.append((path, params))
            if path == "/companies":
                return [{"edinet_code": "E02144", "sec_code": "72030", "name": "トヨタ"}]
            if path.endswith("/financials"):
                return [{"fiscal_year": 2025, "revenue": 100, "net_income": 10}]
            if path.endswith("/segments"):
                return [{"segmentName": "自動車", "fiscalYear": 2025, "revenue": 100}]
            if path.endswith("/text-blocks"):
                return [{"section": "事業等のリスク", "text": "半導体不足"}]
            raise AssertionError(path)

    class FakeBQ:
        def __init__(self):
            self.merges = []
            captured["bq"] = self

        def merge_rows(self, table, rows, keys, dataset="edinet"):
            self.merges.append((dataset, table, rows, keys))
            return True

    captured = {}
    monkeypatch.setattr(module, "DBRestClient", FakeRest)
    monkeypatch.setattr("edinet_bq_client.EDINETBQClient", FakeBQ)
    cfg = SimpleNamespace(
        ENABLE_EDINETDB_INGEST=True,
        EDINETDB_API_KEY="key",
        EDINETDB_DAILY_BUDGET=20,
        EDINETDB_MONTHLY_GUARD=200,
        EDINETDB_REFRESH_DAYS=180,
        EDINETDB_TEXTBLOCK_EVERY_N=1,
        EDINETDB_ROTATION_META="rotation.json",
        EDINETDB_BASE_URL="https://example.test/v1",
        DATA_DIR=tmp_path,
        SECTOR_REPRESENTATIVE_TICKERS={"auto": ["7203.T"]},
    )

    result = module.run_edinetdb_ingest(cfg, target_limit=1)

    assert result["success"] == 1
    requests = captured["rest"].requests
    assert [path for path, _params in requests] == [
        "/companies",
        "/companies/E02144/financials",
        "/companies/E02144/segments",
        "/companies/E02144/text-blocks",
    ]
    assert requests[-1][1] == {"full": "true", "fiscal_year": 2025}
    assert [table for _dataset, table, _rows, _keys in captured["bq"].merges] == [
        "company", "financials", "segments", "segment_finance", "text_analysis"
    ]


def test_ingest_does_not_advance_rotation_when_full_text_fetch_fails(
    tmp_path, monkeypatch
):
    class FakeRest:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0

        def usage(self):
            return {"daily_remaining": 20}

        def get(self, path, params=None):
            if path == "/companies":
                return [{"edinet_code": "E02144", "sec_code": "72030", "name": "トヨタ"}]
            if path.endswith("/financials"):
                return [{"fiscal_year": 2025, "revenue": 100}]
            if path.endswith("/segments"):
                return []
            if path.endswith("/text-blocks"):
                return None
            raise AssertionError(path)

    class FakeBQ:
        def merge_rows(self, *_args, **_kwargs):
            return True

    monkeypatch.setattr(module, "DBRestClient", FakeRest)
    monkeypatch.setattr("edinet_bq_client.EDINETBQClient", FakeBQ)
    cfg = SimpleNamespace(
        ENABLE_EDINETDB_INGEST=True,
        EDINETDB_API_KEY="key",
        EDINETDB_DAILY_BUDGET=20,
        EDINETDB_REFRESH_DAYS=180,
        EDINETDB_TEXTBLOCK_EVERY_N=1,
        EDINETDB_ROTATION_META="rotation.json",
        DATA_DIR=tmp_path,
        SECTOR_REPRESENTATIVE_TICKERS={"auto": ["7203.T"]},
    )

    result = module.run_edinetdb_ingest(cfg, target_limit=1)

    assert result["success"] == 0
    assert result["failure"] == 1
    assert "E02144" not in module._load_json(tmp_path / "rotation.json", {})
