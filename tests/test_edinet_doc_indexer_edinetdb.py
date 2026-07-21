from conftest import load_module


module = load_module("edinet_doc_indexer")


def test_build_doc_index_rows_maps_yuho_contract():
    rows = module.build_doc_index_rows(
        {"disclosures": [{
            "doc_id": "S100TEST",
            "type": "yuho",
            "published_at": "2026-06-20T09:00:00+09:00",
            "fiscal_year_end": "2026-03-31",
        }]},
        {
            "edinet_code": "E02144", "sec_code": "72030", "name": "トヨタ自動車",
            "accounting_standard": "JP",
        },
        now="2026-07-21T00:00:00+00:00",
    )
    assert rows == [{
        "doc_id": "S100TEST",
        "company_code": "7203",
        "edinet_code": "E02144",
        "company_name": "トヨタ自動車",
        "doc_type": "有価証券報告書",
        "fiscal_year_end": "2026-03-31",
        "disclosure_date": "2026-06-20",
        "xbrl_available": False,
        "html_available": False,
        "fetched_flag": True,
        "fetched_at": "2026-07-21T00:00:00+00:00",
        "ifrs_flag": False,
        "updated_at": "2026-07-21T00:00:00+00:00",
    }]
