import json
from datetime import date
from types import SimpleNamespace

import fudosan_ingestor as module
from fudosan_ingestor import _items, map_area_profile, map_corporate_holding, plan_fudosan_targets


def test_holding_mapping_requires_merge_key_and_preserves_measured_fields():
    assert map_corporate_holding({"edinet_code": "E1"}) is None
    row = map_corporate_holding({
        "edinet_code": "E1", "fiscal_year": 2025, "facility_name": "本社",
        "municipality_code": "13101", "land_area_m2": 10,
        "commercial_land_price_per_m2": 20,
    }, now="2026-07-21T00:00:00+00:00")
    assert (row["edinet_code"], row["fiscal_year"], row["facility_name"]) == ("E1", 2025, "本社")
    assert row["source"] == "fudosandb"


def test_area_profile_maps_current_official_docs_fields():
    row = map_area_profile({"total_transactions": 3, "median_price": 12.5}, "13101")
    assert row["municipality_code"] == "13101"
    assert row["total_transactions"] == 3
    assert row["median_price"] == 12.5


def test_page_object_holding_response_is_unwrapped_without_changing_rows():
    assert _items({"items": [{"edinet_code": "E1"}]}) == [{"edinet_code": "E1"}]


def test_target_plan_prioritizes_portfolio_and_obeys_two_call_budget(tmp_path):
    cfg = SimpleNamespace(
        PORTFOLIO_HOLDINGS_PATH=tmp_path / "missing.csv",
        SCREENER_HISTORY_PATH=tmp_path / "missing.json",
        SECTOR_REPRESENTATIVE_TICKERS={"1": ["2222.T"]},
        FUDOSAN_REFRESH_DAYS=365,
    )
    companies = [
        {"edinet_code": "E2", "sec_code": "22220"},
        {"edinet_code": "E3", "sec_code": "33330"},
    ]
    targets = plan_fudosan_targets(companies, cfg, {}, 2, today=date(2026, 7, 21))
    assert targets == ["E2"]


def test_company_dry_run_resolves_name_to_edinet_code(tmp_path):
    (tmp_path / "edinetdb_company_cache.json").write_text(json.dumps({
        "companies": [
            {"edinet_code": "E02144", "sec_code": "72030", "name": "トヨタ自動車"},
            {"edinet_code": "E00001", "sec_code": "11110", "name": "別会社"},
        ],
    }), encoding="utf-8")
    cfg = SimpleNamespace(
        ENABLE_FUDOSAN_INGEST=True,
        FUDOSAN_DAILY_BUDGET=900,
        FUDOSAN_BACKFILL_CUTOFF="2099-12-31",
        FUDOSAN_REFRESH_DAYS=365,
        FUDOSAN_ROTATION_META="rotation.json",
        DATA_DIR=tmp_path,
    )

    result = module.run_fudosan_ingest(cfg, dry_run=True, company="トヨタ")

    assert result["targets"] == ["E02144"]


def test_company_dry_run_is_network_free_before_company_cache_exists(tmp_path):
    cfg = SimpleNamespace(
        ENABLE_FUDOSAN_INGEST=True,
        FUDOSAN_DAILY_BUDGET=900,
        FUDOSAN_BACKFILL_CUTOFF="2099-12-31",
        DATA_DIR=tmp_path,
    )

    result = module.run_fudosan_ingest(cfg, dry_run=True, company="トヨタ")

    assert result["targets"] == ["トヨタ"]
    assert result["identifier"] == "company query (company cache未生成)"


def test_failed_area_profile_keeps_company_due_for_retry(tmp_path, monkeypatch):
    (tmp_path / "edinetdb_company_cache.json").write_text(json.dumps({
        "companies": [{"edinet_code": "E1", "sec_code": "11110"}],
    }), encoding="utf-8")

    class FakeRest:
        def __init__(self, *_args, **_kwargs):
            pass

        def get(self, path, params=None):
            if path == "/corporate/real-estate-holdings":
                return [{
                    "edinet_code": "E1", "fiscal_year": 2025,
                    "facility_name": "本社", "municipality_code": "13101",
                }]
            if path == "/area-profile/13101":
                return None
            raise AssertionError(path)

    class FakeBQ:
        def merge_rows(self, *_args, **_kwargs):
            return True

    monkeypatch.setattr(module, "DBRestClient", FakeRest)
    monkeypatch.setattr("edinet_bq_client.EDINETBQClient", FakeBQ)
    cfg = SimpleNamespace(
        ENABLE_FUDOSAN_INGEST=True,
        FUDOSAN_API_KEY="key",
        FUDOSAN_DAILY_BUDGET=10,
        FUDOSAN_BACKFILL_CUTOFF="2099-12-31",
        FUDOSAN_REFRESH_DAYS=365,
        FUDOSAN_ROTATION_META="fudosan_rotation.json",
        DATA_DIR=tmp_path,
        SECTOR_REPRESENTATIVE_TICKERS={"x": ["1111.T"]},
    )

    result = module.run_fudosan_ingest(cfg, target_limit=1)

    meta = json.loads((tmp_path / "fudosan_rotation.json").read_text(encoding="utf-8"))
    assert result["success"] == 0
    assert result["failure"] == 1
    assert "E1" not in meta
    assert "13101" not in meta["_area_profiles"]
