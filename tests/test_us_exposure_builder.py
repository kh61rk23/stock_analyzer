"""us_exposure_builder：XBRL の地域別売上 → 権益の行。sec_geographic_extractor の読み出しは1本だけ。"""
import sec_geographic_extractor as sge
import us_exposure_builder as ub


def _r(member, val, revenue=1000, ticker="AAA"):
    return {"ticker": ticker, "cik": 1, "member": member, "period_end": "2026-06-30", "fy": 2026, "val": val, "revenue": revenue}


def test_build_rows_keeps_countries_with_share():
    rows = ub.build_rows([_r("US", 600), _r("CN", 200), _r("Europe", 200), _r("JP", 10), _r("DE", 5000)], today="2026-10-29")
    assert [(r["country_code"], r["exposure_strength"]) for r in rows] == [("USA", 0.6), ("CHN", 0.2)]
    assert all(r["market"] == "US" and r["source"] == "sec_xbrl" and r["exposure_type"] == "SALES_NETWORK" for r in rows)
    assert "CN 20.0%" in rows[1]["rationale"]


def test_reader_loads_once(monkeypatch):
    calls = []
    monkeypatch.setattr(sge, "_US_GEO_CACHE", None)
    monkeypatch.setattr(sge, "_load_us_geo_exposure", lambda pid: calls.append(pid) or {"AAA": [{"country_code": "CHN"}]})
    assert sge.get_us_geo_exposure("AAA")[0]["country_code"] == "CHN"
    assert sge.get_us_geo_exposure("BBB") == [] and sge.get_us_geo_exposure("AAA")
    assert len(calls) == 1


def test_us_sic_map_rows():
    import us_taxonomy_mapper as um
    by_code, by_major = um.load_map()
    assert um.node_for_sic("3674", by_code, by_major) == "semiconductor_design"
    assert um.node_for_sic("6022", by_code, by_major) == "banking"            # 上2桁で引く
    assert um.node_for_sic(None, by_code, by_major) is None and um.node_for_sic("0000", by_code, by_major) is None
    rows = um.build_rows([{"ticker": "NVDA", "cik": 1, "sic": "3674", "sic_description": "Semiconductors"},
                          {"ticker": "NVDA", "cik": 1, "sic": "3674"}, {"ticker": "X", "sic": "9999"}],
                         by_code, by_major, known_nodes={"semiconductor_design"}, as_of="2026-10-29")
    assert [(r["company_id"], r["node_id"], r["confidence_type"]) for r in rows] == [("NVDA", "semiconductor_design", "us_sic")]


def test_us_sic_map_uses_approved_nodes():
    import us_taxonomy_mapper as um
    from taxonomy_loader import load_seed_batches, select_approved_nodes
    known = {n["node_id"] for n in select_approved_nodes(load_seed_batches())[0]}
    by_code, by_major = um.load_map()
    assert set(by_code.values()) | set(by_major.values()) <= known
