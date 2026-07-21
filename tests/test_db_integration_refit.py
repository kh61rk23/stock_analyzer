from types import SimpleNamespace

from db_rest_client import DBRestClient
from edinetdb_ingestor import _explicit_date
from edinetdb_ingestor import run_edinetdb_ingest
from real_estate_signal import calc_real_estate_bonus
from dead_ticker_pruner import (
    _candidate_scan_is_reliable,
    _classify_yahoo_chart_response,
    _partition_confirmation,
)


def test_fiscal_year_end_only_uses_explicit_provider_date():
    assert _explicit_date({"submit_date": "2026-06-20", "fiscal_month": 3}) is None
    assert _explicit_date({"period_end": "2026-03-31"}) == "2026-03-31"


def test_real_estate_bonus_is_neutral_without_valid_book_value():
    cfg = SimpleNamespace(RE_BONUS_MIN=-2.0, RE_BONUS_MAX=3.0)
    assert calc_real_estate_bonus([], cfg) == 0.0
    assert calc_real_estate_bonus([{"facility_name": "本社"}], cfg) == 0.0


def test_real_estate_bonus_is_clamped():
    cfg = SimpleNamespace(RE_BONUS_MIN=-2.0, RE_BONUS_MAX=3.0)
    row = {"land_area_m2": 10**6, "commercial_land_price_per_m2": 10**6,
           "book_value_land_m_yen": 1, "book_value_buildings_m_yen": 1}
    assert -2.0 <= calc_real_estate_bonus([row], cfg) <= 3.0


def test_rest_client_does_not_retry_auth_error():
    class Response:
        status_code = 401
        def raise_for_status(self):
            raise RuntimeError("401")
    class Session:
        def __init__(self): self.count = 0
        def get(self, *args, **kwargs): self.count += 1; return Response()
    session = Session()
    client = DBRestClient("https://example.invalid", "key", session=session)
    try:
        client.get("x")
    except RuntimeError:
        pass
    assert session.count == 1


def test_edinet_ingest_skips_cleanly_without_optional_key():
    cfg = SimpleNamespace(ENABLE_EDINETDB_INGEST=True, EDINETDB_API_KEY="")
    result = run_edinetdb_ingest(cfg, doc_index_only=True)
    assert result["skipped"] is True
    assert "API_KEY" in result["reason"]


def test_pruner_never_confirms_unknown_ticker_for_deletion():
    confirmed, revived, unresolved = _partition_confirmation(
        {"DEAD": "dead", "LIVE": "alive", "RETRY": "unknown"}
    )
    assert confirmed == ["DEAD"]
    assert revived == ["LIVE"]
    assert unresolved == ["RETRY"]


def test_pruner_requires_explicit_yahoo_404_to_confirm_dead():
    assert _classify_yahoo_chart_response(404, {}) == "dead"
    assert _classify_yahoo_chart_response(429, {}) == "unknown"
    assert _classify_yahoo_chart_response(200, {"chart": {"result": [{}]}}) == "alive"


def test_pruner_rejects_broad_candidate_scan_as_measurement_failure():
    assert _candidate_scan_is_reliable(10, 1_000)
    assert not _candidate_scan_is_reliable(101, 1_000)
