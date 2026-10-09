"""bi_export の書き出しロジック（BigQuery は差し替え）。"""
import json
import sys
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bi_export  # noqa: E402


def _table(rows):
    return pa.table({
        "edinet_code": [r[0] for r in rows],
        "fiscal_year": pa.array([r[1] for r in rows], pa.int64()),
        "net_sales": pa.array([r[2] for r in rows], pa.float64()),
    })


class FakeBQ:
    """stage_mart / read_stage / promote_mart / drop_stage の差し替え。呼び出しの順序も記録する。"""

    def __init__(self, monkeypatch):
        self.table = _table([])
        self.calls = []
        monkeypatch.setattr(bi_export, "stage_mart", self.stage)
        monkeypatch.setattr(bi_export, "read_stage", lambda c, st: self.table)
        monkeypatch.setattr(bi_export, "promote_mart", self.promote)
        monkeypatch.setattr(bi_export, "drop_stage", lambda c, st: self.calls.append(("drop", st)))

    def stage(self, client, mart, sql):
        self.calls.append(("stage", mart))
        return {"stage": f"p.bi.{mart}__next", "rows": self.table.num_rows}

    def promote(self, client, mart, stage):
        self.calls.append(("promote", mart))
        return {"table": f"stock-analyzer-edinet.bi.{mart}", "table_bytes": 12345, "table_rows": self.table.num_rows}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bi_export, "OUT_DIR", tmp_path)
    monkeypatch.setattr(bi_export, "MANIFEST", tmp_path / "manifest.json")
    return tmp_path


@pytest.fixture()
def bq(monkeypatch):
    return FakeBQ(monkeypatch)


def test_unchanged_content_is_not_rewritten_even_if_row_order_differs(env, bq):
    rows = [("E2", 2024, 2.0), ("E1", 2024, 1.0), ("E1", 2025, 3.0)]
    bq.table = _table(rows)
    entry, wrote = bi_export.export_mart(None, "m_fin_annual", None)
    assert wrote and entry["rows"] == 3
    assert entry["table"] == "stock-analyzer-edinet.bi.m_fin_annual" and entry["table_bytes"] == 12345
    path = env / "m_fin_annual.parquet"
    mtime = path.stat().st_mtime_ns
    bq.table = _table(list(reversed(rows)))
    entry2, wrote2 = bi_export.export_mart(None, "m_fin_annual", entry)
    assert not wrote2 and path.stat().st_mtime_ns == mtime
    assert entry2["generated_at"] == entry["generated_at"]
    assert [c["type"] for c in entry["columns"]] == ["string", "int64", "double"]
    assert pq.read_table(path).num_rows == 3


def test_unchanged_content_still_refreshes_table_info(env, bq):
    bq.table = _table([("E1", 2024, 1.0)])
    entry, _ = bi_export.export_mart(None, "m_fin_annual", None)
    old = {k: v for k, v in entry.items() if k not in ("table", "table_bytes")}
    entry2, wrote = bi_export.export_mart(None, "m_fin_annual", old)
    assert not wrote and entry2["table"].endswith(".bi.m_fin_annual") and entry2["table_bytes"] == 12345


def test_changed_content_is_rewritten(env, bq):
    bq.table = _table([("E1", 2024, 1.0), ("E2", 2024, 2.0)])
    entry, _ = bi_export.export_mart(None, "m_fin_annual", None)
    bq.table = _table([("E1", 2024, 1.0), ("E2", 2024, 9.0)])
    entry2, wrote = bi_export.export_mart(None, "m_fin_annual", entry)
    assert wrote and entry2["content_sha"] != entry["content_sha"]


def test_empty_or_collapsed_result_is_failure_keeps_old_file_and_old_table(env, bq):
    bq.table = _table([("E%d" % i, 2024, 1.0) for i in range(10)])
    entry, _ = bi_export.export_mart(None, "m_fin_annual", None)
    before = (env / "m_fin_annual.parquet").read_bytes()
    bq.calls.clear()
    for rows in ([], [("E1", 2024, 1.0)]):
        bq.table = _table(rows)
        with pytest.raises(RuntimeError):
            bi_export.export_mart(None, "m_fin_annual", entry)
    assert (env / "m_fin_annual.parquet").read_bytes() == before
    # 失敗した回は本番表へ差し替えず、ステージ表は必ず片付ける
    assert ("promote", "m_fin_annual") not in bq.calls
    assert [c[0] for c in bq.calls].count("drop") == 2


def test_stage_failure_is_not_swallowed(env, monkeypatch):
    def boom(client, mart, sql):
        raise PermissionError("bigquery.tables.create denied")
    monkeypatch.setattr(bi_export, "stage_mart", boom)
    with pytest.raises(PermissionError):
        bi_export.export_mart(None, "m_company", None)
    assert not list(env.glob("*.parquet"))


class FakeMod:
    class EDINETBQClient:
        client = object()


def test_main_returns_nonzero_on_failure(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "edinet_bq_client", FakeMod)
    monkeypatch.setattr(bi_export, "ensure_dataset", lambda c: None)
    def boom(c, m, s):
        raise RuntimeError("bq down")
    monkeypatch.setattr(bi_export, "stage_mart", boom)
    assert bi_export.main(["--only", "m_company"]) == 1
    assert not (env / "manifest.json").exists()


def test_main_fails_without_writing_when_dataset_cannot_be_prepared(env, monkeypatch, bq):
    monkeypatch.setitem(sys.modules, "edinet_bq_client", FakeMod)
    def denied(c):
        raise RuntimeError("権限不足")
    monkeypatch.setattr(bi_export, "ensure_dataset", denied)
    bq.table = _table([("E1", 2024, 1.0)])
    assert bi_export.main(["--only", "m_company"]) == 1
    assert not (env / "manifest.json").exists() and not list(env.glob("*.parquet"))
    assert bq.calls == []


def test_main_writes_manifest_and_templates(env, monkeypatch, bq):
    monkeypatch.setitem(sys.modules, "edinet_bq_client", FakeMod)
    monkeypatch.setattr(bi_export, "ensure_dataset", lambda c: None)
    bq.table = _table([("E1", 2024, 1.0)])
    assert bi_export.main(["--only", "m_company"]) == 0
    m = json.loads((env / "manifest.json").read_text(encoding="utf-8"))
    assert m["marts"][0]["mart"] == "m_company" and m["marts"][0]["rows"] == 1
    assert set(m["marts"][0]) >= {"mart", "rows", "columns", "generated_at", "sql_sha", "content_sha",
                                   "table", "table_bytes"}
    assert (env / "templates" / "finance.json").exists()
    assert (env / "templates" / "geo.json").exists() and (env / "templates" / "realestate.json").exists()


def test_every_mart_has_a_sort_key():
    for mart in bi_export.mart_names():
        assert mart in bi_export.MART_KEYS, f"{mart}: MART_KEYS に主キーが無い（行順の揺れで毎日 parquet が変わる）"


def test_stage_and_promote_issue_expected_bigquery_calls():
    """実際の BQ クライアントの呼び方（DDL・コピー・無料の list_rows）を差し替えて確かめる。"""
    class Job:
        def result(self):
            return self
    class Tbl:
        num_rows, num_bytes = 7, 2048
    class Client:
        def __init__(self):
            self.queries, self.copies, self.listed = [], [], []
        def query(self, sql, location=None, job_config=None):
            self.queries.append((sql, location)); return Job()
        def get_table(self, ref):
            return Tbl()
        def copy_table(self, src, dst, location=None, job_config=None):
            self.copies.append((src, dst, location)); return Job()
        def list_rows(self, ref):
            self.listed.append(ref)
            class It:
                def to_arrow(self, create_bqstorage_client=True):
                    assert create_bqstorage_client is False
                    return _table([("E1", 2024, 1.0)])
            return It()
    c = Client()
    st = bi_export.stage_mart(c, "m_company", "SELECT 1 AS x")
    assert st == {"stage": "stock-analyzer-edinet.bi.m_company__next", "rows": 7}
    ddl, loc = c.queries[0]
    assert ddl.startswith("CREATE OR REPLACE TABLE `stock-analyzer-edinet.bi.m_company__next`") and loc == "asia-northeast1"
    assert ddl.rstrip().endswith("SELECT 1 AS x") and len(c.queries) == 1
    assert bi_export.read_stage(c, st["stage"]).num_rows == 1 and not c.queries[1:]
    info = bi_export.promote_mart(c, "m_company", st["stage"])
    assert c.copies == [("stock-analyzer-edinet.bi.m_company__next", "stock-analyzer-edinet.bi.m_company", "asia-northeast1")]
    assert info == {"table": "stock-analyzer-edinet.bi.m_company", "table_bytes": 2048, "table_rows": 7}
