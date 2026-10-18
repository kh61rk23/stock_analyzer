"""BQ への追記は load job（無料）だけ。ストリーミング挿入が呼ばれず、同じ内容は同じ job_id になる。"""
import re
import types
from pathlib import Path

import pytest
from google.api_core.exceptions import Conflict

import edinet_bq_client as bqc

REPO = Path(__file__).parent.parent


class FakeJob:
    def result(self):
        return self


class FakeClient:
    project = "p"

    def __init__(self, conflict_state=None):
        self.loads = []
        self.streamed = []
        self.conflict_state = conflict_state

    def load_table_from_json(self, rows, ref, job_id=None, job_config=None, location=None, **kw):
        self.loads.append({"rows": list(rows), "ref": ref, "job_id": job_id, "cfg": job_config})
        if self.conflict_state is not None:
            raise Conflict("exists")
        return FakeJob()

    def get_job(self, jid, location=None):
        st = self.conflict_state
        return types.SimpleNamespace(state="DONE", error_result=None if st == "ok" else {"reason": "x"})

    def insert_rows(self, *a, **k):
        self.streamed.append(a)
        raise AssertionError("streaming insert は使わない")

    insert_rows_json = insert_rows


class FakeBQ:
    project = "p"
    dataset = "p.edinet"

    def __init__(self, client):
        self.client = client


@pytest.fixture
def fake(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(bqc, "EDINETBQClient", lambda *a, **k: FakeBQ(c))
    return c


def _assert_loaded(c, table_suffix):
    assert not c.streamed
    assert len(c.loads) == 1
    assert c.loads[0]["ref"].endswith(table_suffix)
    assert c.loads[0]["cfg"].write_disposition == "WRITE_APPEND"
    return c.loads[0]["job_id"]


def _client_with(fake_client):
    cli = bqc.EDINETBQClient.__new__(bqc.EDINETBQClient)
    cli.client, cli.project, cli.dataset = fake_client, "p", "p.edinet"
    return cli


def test_load_rows_job_id_ignores_time_columns_and_order():
    a = [{"k": 1, "updated_at": "t1"}, {"k": 2, "updated_at": "t1"}]
    b = [{"k": 2, "updated_at": "t2"}, {"k": 1, "updated_at": "t2"}]
    assert bqc.load_job_id("p.d.t", a) == bqc.load_job_id("p.d.t", b)
    assert bqc.load_job_id("p.d.t", a) != bqc.load_job_id("p.d.u", a)
    assert bqc.load_job_id("p.d.t", a) != bqc.load_job_id("p.d.t", [{"k": 3}])


def test_load_rows_conflict_success_is_noop():
    c = FakeClient("ok")
    assert bqc.load_rows(c, "p.d.t", [{"k": 1}]) is True
    assert len(c.loads) == 1


class RetryClient(FakeClient):
    """state_by_id: job_id 末尾の接尾辞 -> ok / bad / None(新規で成功)。"""

    def __init__(self, existing):
        super().__init__()
        self.existing = existing

    def load_table_from_json(self, rows, ref, job_id=None, job_config=None, location=None, **kw):
        self.loads.append({"job_id": job_id})
        if job_id in self.existing:
            raise Conflict("exists")
        return FakeJob()

    def get_job(self, jid, location=None):
        return types.SimpleNamespace(state="DONE", error_result=None if self.existing[jid] == "ok" else {"reason": "x"})


def test_load_rows_retries_with_suffix_after_failed_job():
    rows = [{"k": 1}]
    base = bqc.load_job_id("p.d.t", rows)
    c = RetryClient({base: "bad"})
    assert bqc.load_rows(c, "p.d.t", rows) is True
    assert [x["job_id"] for x in c.loads] == [base, base + "_r1"]


def test_load_rows_uses_succeeded_branch_without_writing_again():
    rows = [{"k": 1}]
    base = bqc.load_job_id("p.d.t", rows)
    c = RetryClient({base: "bad", base + "_r1": "ok"})
    assert bqc.load_rows(c, "p.d.t", rows) is True
    assert [x["job_id"] for x in c.loads] == [base, base + "_r1"]


def test_load_rows_gives_up_after_three_retries():
    rows = [{"k": 1}]
    base = bqc.load_job_id("p.d.t", rows)
    c = RetryClient({base: "bad", base + "_r1": "bad", base + "_r2": "bad", base + "_r3": "bad"})
    assert bqc.load_rows(c, "p.d.t", rows) is False
    assert len(c.loads) == 4


def test_module_insert_rows_uses_load(fake):
    assert bqc.insert_rows(rows=[{"a": 1}], table_ref="us.institutional_positions", project_id="p")
    _assert_loaded(fake, "p.us.institutional_positions")


def test_client_insert_rows_and_policy_support_use_load():
    c = FakeClient()
    assert _client_with(c).insert_policy_support_rows([{"a": 1}])
    _assert_loaded(c, ".policy_support")


def test_upsert_ticker_subtheme_mapping_uses_load():
    rows = [{"ticker": "6758.T", "sector_code": "1016", "subtheme": "AI"}]
    c = FakeClient()
    assert _client_with(c).upsert_ticker_subtheme_mapping(rows)
    j1 = _assert_loaded(c, "ticker_subtheme_mapping")
    c2 = FakeClient()
    _client_with(c2).upsert_ticker_subtheme_mapping(rows)
    assert c2.loads[0]["job_id"] == j1


def test_stock_snapshot_writer_uses_load(fake, monkeypatch):
    import stock_snapshot_writer as w
    rows = [{"snapshot_date": "2026-10-01", "ticker": "A", "ingested_at": "t"}]
    monkeypatch.setattr(w, "build_snapshot_rows", lambda *a, **k: list(rows))
    cfg = types.SimpleNamespace()
    res = {"screener": {"candidates": [1]}, "us_screener": {"candidates": []}}
    out = w.run_stock_snapshot_writer(cfg, res)
    assert out["inserted"] is True
    j = _assert_loaded(fake, "analytics.stock_snapshot")
    fake.loads.clear()
    rows[0]["ingested_at"] = "later"
    w.run_stock_snapshot_writer(cfg, res)
    assert fake.loads[0]["job_id"] == j


def test_price_history_writer_uses_load(fake, monkeypatch):
    import price_history_writer as w
    import yf_cache
    rows = [{"trade_date": "2026-10-01", "ticker": "A", "close": 1.0, "ingested_at": "t"}]
    monkeypatch.setattr(yf_cache, "collected_prices", lambda: {"A": object()})
    monkeypatch.setattr(yf_cache, "stats", lambda: {"info_miss": 0, "history_miss": 0, "download_miss": 0})
    monkeypatch.setattr(w, "build_price_rows", lambda *a, **k: list(rows))
    out = w.run_price_history_writer(types.SimpleNamespace(), {})
    assert out["inserted"] is True
    j = _assert_loaded(fake, "analytics.price_daily")
    fake.loads.clear()
    rows[0]["ingested_at"] = "later"
    w.run_price_history_writer(types.SimpleNamespace(), {})
    assert fake.loads[0]["job_id"] == j


def test_job_posting_monitor_uses_load(fake, monkeypatch, tmp_path):
    import job_posting_monitor as m
    monkeypatch.setattr(m, "_scrape_wantedly", lambda *a, **k: [{"title": "AI engineer"}])
    monkeypatch.setattr(m, "_scrape_green", lambda *a, **k: [])
    monkeypatch.setattr(m, "_count_keywords", lambda *a, **k: {"ai": 3})
    monkeypatch.setattr(m, "_detect_surge", lambda *a, **k: ([], {}))
    cfg = types.SimpleNamespace(CACHE_DIR=str(tmp_path), JOB_TECH_KEYWORDS={"ai": ["AI"]})
    m.run_job_posting_monitor(cfg, {})
    j = _assert_loaded(fake, "us.job_signal_cache")
    for p in (tmp_path / "hiring").glob("*"):
        p.unlink()
    fake.loads.clear()
    m.run_job_posting_monitor(cfg, {})
    assert fake.loads[0]["job_id"] == j


def test_cluster_graph_exporter_uses_load(fake):
    import cluster_graph_exporter as ex
    rows = [{"company_code": "1", "market": "JP", "node_id": "n", "as_of_date": "2026-10-01", "updated_at": "a"}]
    ok, n = ex._write_to_bq(rows, types.SimpleNamespace())
    assert ok and n == 1
    j = _assert_loaded(fake, "taxonomy.company_taxonomy_map")
    fake.loads.clear()
    rows[0]["updated_at"] = "b"
    ex._write_to_bq(rows, types.SimpleNamespace())
    assert fake.loads[0]["job_id"] == j


def test_13f_monitor_writes_through_load_helper():
    src = (REPO / "institutional_13f_monitor.py").read_text(encoding="utf-8")
    assert "_bq.insert_rows(" in src and "insert_rows_json" not in src


def test_no_streaming_insert_calls_remain():
    pat = re.compile(r"\.insert_rows_json\(|\bclient\.insert_rows\(")
    bad = []
    for p in REPO.glob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line) and not line.lstrip().startswith("#"):
                bad.append(f"{p.name}:{i}")
    assert not bad, bad


class LocClient(FakeClient):
    """get_dataset でデータセットの location を返し、load / get_job に渡った location を記録する。"""

    def __init__(self, loc, conflict_state=None):
        super().__init__(conflict_state)
        self.loc = loc
        self.locs = []

    def get_dataset(self, key):
        return types.SimpleNamespace(location=self.loc)

    def load_table_from_json(self, rows, ref, job_id=None, job_config=None, location=None, **kw):
        self.locs.append(("load", location))
        return super().load_table_from_json(rows, ref, job_id=job_id, job_config=job_config, **kw)

    def get_job(self, jid, location=None):
        self.locs.append(("get", location))
        return super().get_job(jid)


def test_load_rows_uses_dataset_location():
    # 東京のデータセットへ US の job で投げると「Dataset が見つからない」で必ず失敗する
    bqc._DATASET_LOCATION.clear()
    c = LocClient("asia-northeast1")
    assert bqc.load_rows(c, "p.analytics_loc_t.t", [{"k": 1}]) is True
    assert c.locs == [("load", "asia-northeast1")]
    c2 = LocClient("asia-northeast1", conflict_state="ok")
    assert bqc.load_rows(c2, "p.analytics_loc_t.t", [{"k": 2}]) is True
    assert ("get", "asia-northeast1") in c2.locs


def test_dataset_location_falls_back_to_none():
    bqc._DATASET_LOCATION.clear()
    assert bqc.dataset_location(FakeClient(), "p.nodata.t") is None   # get_dataset が無い偽物
    assert bqc.dataset_location(FakeClient(), "t") is None
