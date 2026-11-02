"""reit_ingestor：週1回・回数の上限・REIT の持ち回り（API と BQ は偽物）。"""
import json
from datetime import date
from types import SimpleNamespace

import reit_ingestor as ri


class _Client:
    def __init__(self, budget=99):
        self.budget, self.used, self.last_error, self.paths = budget, 0, None, []

    def _load_ledger(self):
        return {"calls_used": self.used}

    def get(self, path, params=None):
        if self.used >= self.budget:
            self.last_error = "budget"
            return None
        self.used += 1
        self.paths.append((path, (params or {}).get("reit_name")))
        if path == "/reit/demographic-risk":
            return [{"reit_name": f"R{i}"} for i in range(5)]
        if path == "/reit/properties":
            return [{"reit_name": params["reit_name"], "property_name": f"{params['reit_name']}-p{j}"} for j in range(2)]
        return [{"reit_name": "R0", "x": 1}]


class _BQ:
    def __init__(self):
        self.written = None


def test_next_reits_rotates():
    assert ri.next_reits(["a", "b", "c"], 2, 2) == (["c", "a"], 1)
    assert ri.next_reits([], 3, 2) == ([], 0)


def test_run_weekly_with_cap(tmp_path, monkeypatch):
    cfg = SimpleNamespace(DATA_DIR=tmp_path, REIT_CALLS_PER_RUN=7, REIT_PROPERTY_REITS_PER_RUN=12)
    written = {}
    monkeypatch.setattr(ri, "write", lambda bq, p, m: written.update(props=p, metrics=m))
    c = _Client()
    res = ri.run(cfg, client=c, bq=_BQ(), today=date(2026, 10, 31))
    assert res["calls"] == 7 and res["reits"] == ["R0", "R1", "R2"]          # 指標4本＋物件3本
    assert len(written["props"]) == 6 and {m["endpoint"] for m in written["metrics"]} == set(ri.METRIC_ENDPOINTS)
    meta = json.loads((tmp_path / "reit_rotation_meta.json").read_text())
    assert meta["pos"] == 3 and meta["last_run"] == "2026-10-31"
    assert ri.run(cfg, client=_Client(), bq=_BQ(), today=date(2026, 11, 3))["skipped"]     # 週1回
    res2 = ri.run(cfg, client=_Client(), bq=_BQ(), today=date(2026, 11, 7))
    assert res2["reits"] == ["R3", "R4", "R0"]


def test_stops_on_budget(tmp_path, monkeypatch):
    cfg = SimpleNamespace(DATA_DIR=tmp_path)
    monkeypatch.setattr(ri, "write", lambda *a: None)
    res = ri.run(cfg, client=_Client(budget=6), bq=_BQ(), today=date(2026, 10, 31))
    assert res["calls"] == 6 and res["reits"] == ["R0", "R1"]


def test_rows_accepts_endpoint_named_key():
    # /reit/demographic-risk は配列を "demographic_risk" キーで返す（知らないキーでも取り出す）
    data = {"count": 2, "demographic_risk": [{"reit_name": "A"}, {"reit_name": "B"}], "note": "x"}
    assert [r["reit_name"] for r in ri._rows(data)] == ["A", "B"]
    assert ri._rows({"count": 0, "note": "x"}) == []


def test_reit_names_collected_from_any_metric(tmp_path, monkeypatch):
    class C(_Client):
        def get(self, path, params=None):
            r = super().get(path, params)
            if path == "/reit/demographic-risk":
                return {"count": 0, "demographic_risk": []}          # 人口リスクが0件でも
            if path == "/reit/borrowings-summary":
                return {"items": [{"reit_name": "B1"}, {"reit_name": "B2"}]}
            return r
    cfg = SimpleNamespace(DATA_DIR=tmp_path, REIT_CALLS_PER_RUN=6, REIT_PROPERTY_REITS_PER_RUN=12)
    written = {}
    monkeypatch.setattr(ri, "write", lambda bq, p, m: written.update(props=p))
    res = ri.run(cfg, client=C(), bq=_BQ(), today=date(2026, 10, 31))
    assert res["reits"] == ["B1", "B2"] and len(written["props"]) == 4


def test_latest_per_property_keeps_newest_period():
    rows = [{"reit_name": "R", "property_name": "P", "period_end": "2021-01-31", "v": 1},
            {"reit_name": "R", "property_name": "P", "period_end": "2025-07-31", "v": 2},
            {"reit_name": "R", "property_name": "Q", "period_end": "2024-01-31", "v": 3},
            {"reit_name": "R", "property_name": None}]
    out = {r["property_name"]: r["v"] for r in ri.latest_per_property(rows)}
    assert out == {"P": 2, "Q": 3}


def test_names_ignore_plain_name_and_truncation_is_reported(tmp_path, monkeypatch):
    class C(_Client):
        def get(self, path, params=None):
            r = super().get(path, params)
            if path == "/reit/tenant-concentration":
                return {"items": [{"name": "テナントX"}]}             # name だけの行は REIT の一覧に入れない
            if path == "/reit/properties":
                return [{"reit_name": params["reit_name"], "property_name": f"p{j}"} for j in range(ri.PROPERTY_LIMIT)]
            return r
    cfg = SimpleNamespace(DATA_DIR=tmp_path, REIT_CALLS_PER_RUN=6, REIT_PROPERTY_REITS_PER_RUN=12)
    monkeypatch.setattr(ri, "write", lambda *a: None)
    res = ri.run(cfg, client=C(), bq=_BQ(), today=date(2026, 10, 31))
    meta = json.loads((tmp_path / "reit_rotation_meta.json").read_text())
    assert "テナントX" not in meta["reits"]
    assert res["truncated"] == res["reits"] == ["R0", "R1"]
