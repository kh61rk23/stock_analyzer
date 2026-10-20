import json
from pathlib import Path

import rental_property_ingestor as rp

# 三井不動産（E03856）の EdinetDB 応答のうち、テストに使う3年度だけを抜いた写し（stock_data は git に無い）
PROBE = Path(__file__).resolve().parent / "fixtures" / "edinetdb_real_estate_E03856_min.json"


def test_map_real_estate_from_saved_response():
    """保存した実応答（三井不動産 E03856）から年度ごとの行を作る。注記の無い年度は貸借対照表の値だけ、空の年度は捨てる。"""
    data = json.loads(PROBE.read_text(encoding="utf-8"))["data"]
    rows = rp.map_real_estate("E03856", data, now="2026-10-15T00:00:00Z")
    by = {r["fiscal_year"]: r for r in rows}
    assert by[2026]["book_value_m_yen"] == 4484015.0 and by[2026]["fair_value_m_yen"] == 9494335.0
    assert by[2026]["doc_id"] == "S100YBLA" and by[2026]["extraction_status"] == "ok"
    assert by[2015]["fair_value_m_yen"] is None and by[2015]["bs_land_m_yen"] == 1839707.0
    assert by[2015]["extraction_status"] == "no_note"
    assert 2014 not in by
    assert all(r["fv_bv_ratio"] is None or 0.1 <= r["fv_bv_ratio"] <= 30 for r in rows)


def test_map_handles_empty_and_odd_payloads():
    assert rp.map_real_estate("E1", {}) == []
    assert rp.map_real_estate("E1", {"real_estate_notes": [{"fiscal_year": None, "bs": {"land_m_yen": 1}}]}) == []


def test_pick_targets_new_first_then_refiled():
    state = {"E2": {"fetched": "2026-06-01"}, "E3": {"fetched": "2026-07-10"}, "E4": {"fetched": "2026-05-01"}}
    latest = {"E2": "2026-06-20", "E3": "2026-06-25", "E4": "2026-06-28"}
    assert rp.pick_targets(["E1", "E2", "E3", "E4"], state, latest, 10) == ["E1", "E4", "E2"]
    assert rp.pick_targets(["E1", "E2", "E3", "E4"], state, latest, 2) == ["E1", "E4"]
