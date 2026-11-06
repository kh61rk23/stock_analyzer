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


def test_run_records_404_so_queue_advances(monkeypatch, tmp_path):
    """404（EdinetDB に注記なし）の会社は状態に残し、翌日の未取得の先頭に居座らない。"""
    import json
    import sys
    from types import ModuleType, SimpleNamespace

    class FakeClient:
        last_error = None

        def get(self, path):
            code = path.split("/")[2]
            if code == "E1":
                self.last_error = "not_found"
                return None
            if code == "E2":
                self.last_error = "http_500"
                return None
            self.last_error = None
            return {"real_estate_notes": [{"fiscal_year": 2025, "bs": {"land_m_yen": 5}, "lease_disclosure": {}}]}

    class FakeRows:
        def __init__(self, rows):
            self._rows = rows

        def result(self):
            return self._rows

    class FakeBQ:
        project = "p"

        def __init__(self):
            self.client = SimpleNamespace(
                get_table=lambda *_: True,
                query=lambda *a, **k: FakeRows([SimpleNamespace(edinet_code=c) for c in ("E1", "E2", "E3")]))

        def merge_rows(self, *a, **k):
            return True

    bq_mod = ModuleType("edinet_bq_client")
    bq_mod.EDINETBQClient = FakeBQ
    monkeypatch.setitem(sys.modules, "edinet_bq_client", bq_mod)
    gov = ModuleType("edinet_gov_xbrl")
    gov.ok_codes = lambda cfg: set()
    monkeypatch.setitem(sys.modules, "edinet_gov_xbrl", gov)
    monkeypatch.setattr(rp, "_client", lambda cfg: FakeClient())
    cfg = SimpleNamespace(DATA_DIR=str(tmp_path), ENABLE_RENTAL_PROPERTY_FV=True, RENTAL_FV_DAILY_BUDGET=3,
                          RENTAL_FV_PRIORITY_CODES=())
    info = rp.run(cfg)
    assert info["no_data"] == 1 and info["failed"] == 1
    state = json.loads((tmp_path / "rental_property_state.json").read_text(encoding="utf-8"))
    assert state["E1"]["status"] == "no_data"
    assert "E2" not in state            # 一時障害は翌日やり直す
    assert "E3" in state
    assert info["remaining_never"] == 1
