from types import SimpleNamespace

from db_rest_client import DBRestClient
from edinetdb_ingestor import _fiscal_year_end
from edinetdb_ingestor import run_edinetdb_ingest
from real_estate_signal import calc_real_estate_bonus


def test_fiscal_year_end_uses_provider_month_and_submit_date():
    assert _fiscal_year_end({"submit_date": "2026-06-20", "fiscal_month": 3}) == "2026-03-20"
    assert _fiscal_year_end({"period_end": "2026-03-31"}) == "2026-03-31"


def test_real_estate_bonus_is_neutral_without_valid_book_value():
    assert calc_real_estate_bonus(None, None, None) == 0.0
    assert calc_real_estate_bonus(100, 100, 0) == 0.0


def test_real_estate_bonus_is_clamped():
    assert -2.0 <= calc_real_estate_bonus(10**12, 0, 1, 2, 2) <= 3.0


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
