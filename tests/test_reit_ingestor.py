"""reit_ingestor：週1回・回数の上限・REIT の持ち回り（API と BQ は偽物）。"""
import json
from datetime import date, timedelta
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


# ---- 100件で切れた REIT の取り直し（用途・エリアで割る） ----

def _prop(reit, name, loc=None, use=None, period="2025-07-31"):
    return {"reit_name": reit, "property_name": name, "location": loc, "use_type": use, "period_end": period}


class _Catalog:
    """物件の全体を持つ偽の API。limit=100 で切り、asset_type は部分一致・area は所在地の部分一致で絞る。"""

    def __init__(self, catalog, budget=999):
        self.catalog, self.budget, self.used, self.last_error, self.calls = catalog, budget, 0, None, []

    def _load_ledger(self):
        return {"calls_used": self.used}

    def get(self, path, params=None):
        params = params or {}
        self.last_error = None
        if self.used >= self.budget:
            self.last_error = "budget"
            return None
        self.used += 1
        if path != "/reit/properties":
            return []
        self.calls.append({k: v for k, v in params.items() if k != "limit"})
        rows = [r for r in self.catalog if r["reit_name"] == params["reit_name"]]
        if params.get("asset_type"):
            rows = [r for r in rows if params["asset_type"] in (r["use_type"] or "")]
        if params.get("area"):
            rows = [r for r in rows if params["area"] in (r["location"] or "")]
        return rows[:params["limit"]]


def test_plan_cells_by_use_type_or_area():
    use_rows = [_prop("R", f"o{i}", use="オフィス") for i in range(5)] + \
               [_prop("R", f"h{i}", use="共同住宅") for i in range(3)] + [_prop("R", "x", use="住宅")]
    assert [c["asset_type"] for c in ri.plan_cells(use_rows)] == ["オフィス", "住宅"]   # 共同住宅は住宅の絞り込みに入る
    area_rows = [_prop("R", f"t{i}", loc="東京都港区南青山") for i in range(4)] + [_prop("R", "o", loc="大阪府大阪市北区")]
    plan = ri.plan_cells(area_rows)
    assert plan[:2] == [{"area": "東京都"}, {"area": "大阪府"}]
    assert {"area": "愛知県"} in plan and plan.count({"area": "東京都"}) == 1      # 見えなかった主要な県を足す・重複なし
    assert ri.plan_cells([_prop("R", "n")] * 3) == []                              # 用途も所在地も空なら割れない
    assert ri.prefecture_of({"location": "神奈川県横浜市中区"}) == "神奈川県"
    assert ri.prefecture_of({"location": "京都府京都市"}) == "京都府"
    assert ri.prefecture_of({"location": "(住居表示）"}) is None


def test_child_cells_descend_prefecture_city_ward():
    rows = [_prop("R", "a", loc="東京都港区高輪"), _prop("R", "b", loc="東京都新宿区西新宿"),
            _prop("R", "c", loc="東京都港区芝"), _prop("R", "d", loc="東京都国分寺市本町")]
    assert ri.child_cells({"area": "東京都"}, rows) == [{"area": "東京都港区"}, {"area": "東京都新宿区"},
                                                        {"area": "東京都国分寺市"}]
    assert ri.child_cells({"area": "東京都港区"}, rows) == []                       # 区より細かくは割らない
    osaka = [_prop("R", "a", loc="大阪府大阪市都島区毛馬町"), _prop("R", "b", loc="大阪府堺市堺区")]
    assert ri.child_cells({"area": "大阪府"}, osaka) == [{"area": "大阪府大阪市"}, {"area": "大阪府堺市"}]
    assert ri.child_cells({"area": "大阪府大阪市"}, osaka) == [{"area": "大阪府大阪市都島区"}]
    # 用途で割った区画は、まずエリアで割る
    assert ri.child_cells({"asset_type": "オフィス"}, rows)[0] == {"asset_type": "オフィス", "area": "東京都"}
    assert ri.child_cells({"asset_type": "オフィス"}, [_prop("R", "n")]) == []


def _area_catalog():
    cat = [_prop("A", f"tokyo-{i}", loc=f"東京都{'港区' if i % 2 else '新宿区'}{i}") for i in range(130)]
    cat += [_prop("A", f"osaka-{i}", loc="大阪府大阪市北区") for i in range(15)]
    cat += [_prop("A", "tokyo-0", loc="東京都新宿区0", period="2024-07-31")]      # 同じ物件の古い期
    return cat


def test_split_by_area_gets_everything_and_dedupes(tmp_path, monkeypatch):
    c = _Catalog(_area_catalog())
    cfg = SimpleNamespace(DATA_DIR=tmp_path, REIT_CALLS_PER_RUN=40, REIT_PROPERTY_REITS_PER_RUN=12,
                          REIT_SPLIT_CALLS_PER_RUN=40)
    written = {}
    monkeypatch.setattr(ri, "write", lambda bq, p, m: written.update(props=p))
    props, metrics, meta = ri.collect(c, {"reits": ["A"]}, 1, "2026-11-06", cap=40)
    keys = [(p["reit_name"], p["property_name"]) for p in props]
    assert len(keys) == len(set(keys)) == 145                                       # 重複なし・全件
    assert meta["split"] == {} and meta["last_truncated"] == []
    assert meta["split_done"]["A"] == {"total": 145, "base": 100, "as_of": "2026-11-06", "unresolved": False}
    areas = [x.get("area") for x in c.calls]
    assert "東京都港区" in areas and "東京都新宿区" in areas    # 東京は100件超なので区で割った
    # 新しい期の行が残る
    old = [p for p in props if p["property_name"] == "tokyo-0"]
    assert len(old) == 1 and json.loads(old[0]["payload"])["period_end"] == "2025-07-31"


def test_split_by_use_type_when_location_missing():
    cat = [_prop("O", f"o{i}", use="オフィス") for i in range(54)] + \
          [_prop("O", f"r{i}", use="共同住宅") for i in range(45)] + [_prop("O", f"s{i}", use="商業施設") for i in range(21)]
    c = _Catalog(cat)
    props, _, meta = ri.collect(c, {"reits": ["O"]}, 1, "2026-11-06", cap=20)
    assert len(props) == 120 and meta["split_done"]["O"]["total"] == 120
    assert [x.get("asset_type") for x in c.calls[1:]] == ["オフィス", "共同住宅", "商業施設"]


def test_split_respects_cap_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "write", lambda *a: None)
    cfg = SimpleNamespace(DATA_DIR=tmp_path, REIT_CALLS_PER_RUN=4 + 1 + 2, REIT_PROPERTY_REITS_PER_RUN=12,
                          REIT_SPLIT_CALLS_PER_RUN=2)

    class C(_Catalog):
        def get(self, path, params=None):
            if path != "/reit/properties":
                self.used += 1
                return [{"reit_name": "A"}] if path == "/reit/demographic-risk" else []
            return super().get(path, params)

    c1 = C(_area_catalog())
    res = ri.run(cfg, client=c1, bq=_BQ(), today=date(2026, 11, 6))
    assert res["calls"] == 7 and res["split_calls"] == 2                          # 指標4＋物件1＋取り直し2＝上限7
    assert res["truncated"] == ["A"]
    meta = json.loads((tmp_path / "reit_rotation_meta.json").read_text())
    assert meta["split"]["A"]["queue"]
    # 次の回：取り直しが残っているので物件の REIT は cap-4-2=1 本のまま、取り直しを続きから進める
    done_calls = 0
    for week in range(1, 8):
        r = ri.run(cfg, client=C(_area_catalog()), bq=_BQ(), today=date(2026, 11, 6) + timedelta(days=7 * week))
        done_calls += r["split_calls"]
        if not r["truncated"]:
            break
    assert not r["truncated"] and r["split_completed"] == ["A"] and r["split_gained"] == 45
    assert done_calls <= 14


def test_split_stops_on_budget_and_keeps_queue():
    c = _Catalog(_area_catalog(), budget=6)         # 指標4＋最初の100件1＋取り直し1で尽きる
    props, _, meta = ri.collect(c, {"reits": ["A"]}, 1, "2026-11-06", cap=40)
    assert c.last_error == "budget"
    assert meta["last_truncated"] == ["A"] and meta["split"]["A"]["queue"]         # 取れなかった区画は次の回に残る


def test_unresolvable_cell_is_reported():
    c = _Catalog([_prop("N", f"p{i}") for i in range(130)])
    _, _, meta = ri.collect(c, {"reits": ["N"]}, 1, "2026-11-06", cap=20)
    assert meta["last_truncated"] == ["N"] and meta["split_done"]["N"]["unresolved"]
    assert len(c.calls) == 1                                                        # 割れないので無駄に呼ばない


def test_seed_from_previous_last_truncated_and_refresh_days():
    c = _Catalog(_area_catalog())
    meta = {"reits": ["A", "B"], "pos": 1, "last_truncated": ["A"]}                 # 初回に切れた記録だけある
    assert ri.has_pending_split(meta)
    _, _, new = ri.collect(c, meta, 0, "2026-11-06", cap=40)                         # 物件の REIT は0本・取り直しだけ
    assert new["split_done"]["A"]["total"] == 145 and new["last_truncated"] == []
    assert not ri.has_pending_split(new)
    # 済んで日が浅い REIT は、また100件で切れても割り直さない
    c2 = _Catalog(_area_catalog())
    _, _, again = ri.collect(c2, new, 1, "2026-12-06", cap=40)
    assert len(c2.calls) == 1 and again["split"] == {}
