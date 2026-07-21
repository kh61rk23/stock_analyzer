from types import SimpleNamespace

import text_extractor as module
from text_extractor import _XBRL_ELEMENT_MAP, _section_key


def test_edinetdb_section_contract_maps_to_existing_signal_keys():
    assert _XBRL_ELEMENT_MAP["business-overview"] == "business"
    assert _XBRL_ELEMENT_MAP["risks"] == "risk"
    assert _XBRL_ELEMENT_MAP["mda"] == "strategy"
    assert _XBRL_ELEMENT_MAP["governance"] == "governance"


def test_japanese_full_text_section_names_map_to_existing_signal_keys():
    assert _section_key("事業等のリスク") == "risk"
    assert _section_key("事業の内容") == "business"
    assert _section_key("経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析") == "strategy"
    assert _section_key("コーポレート・ガバナンスの状況等") == "governance"


def test_public_full_text_search_delegates_to_bq(monkeypatch):
    class FakeBQ:
        def search_company_text(self, code, query, *, limit):
            return [{"code": code, "query": query, "limit": limit}]

    monkeypatch.setattr("edinet_bq_client.EDINETBQClient", lambda: FakeBQ())
    rows = module.search_full_text(
        SimpleNamespace(ENABLE_TEXT_SIGNAL=True), "7203", "半導体", limit=12
    )
    assert rows == [{"code": "7203", "query": "半導体", "limit": 12}]
