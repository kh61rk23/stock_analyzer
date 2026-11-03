"""13F の追跡対象（提出主体の CIK）が config の Tier 表と噛み合っていることを固定する。"""

import config
from institutional_13f_monitor import _KNOWN_CIKS, _get_13f_filings


def _selected():
    tier_map = config.INSTITUTION_TIER_MAP
    return {
        cik for cik, info in _KNOWN_CIKS.items()
        if info["name"] in tier_map or any(info["name"].startswith(k) for k in tier_map)
    }


def test_every_known_cik_is_selected_by_tier_map():
    # 一部だけ一致すると残りは黙って対象外になるので、全件が選ばれることを確認する
    assert _selected() == set(_KNOWN_CIKS)


def test_moved_filers_use_current_ciks():
    assert "0002026053" in _KNOWN_CIKS            # Pershing Square Inc.
    assert "0002100119" in _KNOWN_CIKS            # Vanguard Capital Management
    assert "0002100121" in _KNOWN_CIKS            # Vanguard Portfolio Management
    assert "0000102909" not in _KNOWN_CIKS        # Vanguard Group は 2025-12期で提出終了
    assert "0001336528" not in _KNOWN_CIKS        # Pershing Square Capital Management は 13F-NT のみ


def test_filings_ignore_amendments_and_notices():
    sub = {"filings": {"recent": {
        "form": ["13F-NT", "13F-HR/A", "13F-HR", "13F-HR"],
        "accessionNumber": ["a", "b", "c", "d"],
        "reportDate": ["2026-06-30", "2026-03-31", "2026-03-31", "2025-12-31"],
    }}}
    assert _get_13f_filings(sub, 2) == [("c", "2026-03-31"), ("d", "2025-12-31")]
