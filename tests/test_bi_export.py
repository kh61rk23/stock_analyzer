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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bi_export, "OUT_DIR", tmp_path)
    monkeypatch.setattr(bi_export, "MANIFEST", tmp_path / "manifest.json")
    return tmp_path


def test_unchanged_content_is_not_rewritten_even_if_row_order_differs(env, monkeypatch):
    rows = [("E2", 2024, 2.0), ("E1", 2024, 1.0), ("E1", 2025, 3.0)]
    monkeypatch.setattr(bi_export, "run_query", lambda c, s, d: _table(rows))
    entry, wrote = bi_export.export_mart(None, "m_fin_annual", None)
    assert wrote and entry["rows"] == 3
    path = env / "m_fin_annual.parquet"
    mtime = path.stat().st_mtime_ns
    monkeypatch.setattr(bi_export, "run_query", lambda c, s, d: _table(list(reversed(rows))))
    entry2, wrote2 = bi_export.export_mart(None, "m_fin_annual", entry)
    assert not wrote2 and path.stat().st_mtime_ns == mtime
    assert entry2["generated_at"] == entry["generated_at"]
    assert [c["type"] for c in entry["columns"]] == ["string", "int64", "double"]
    assert pq.read_table(path).num_rows == 3


def test_changed_content_is_rewritten(env, monkeypatch):
    monkeypatch.setattr(bi_export, "run_query", lambda c, s, d: _table([("E1", 2024, 1.0), ("E2", 2024, 2.0)]))
    entry, _ = bi_export.export_mart(None, "m_fin_annual", None)
    monkeypatch.setattr(bi_export, "run_query", lambda c, s, d: _table([("E1", 2024, 1.0), ("E2", 2024, 9.0)]))
    entry2, wrote = bi_export.export_mart(None, "m_fin_annual", entry)
    assert wrote and entry2["content_sha"] != entry["content_sha"]


def test_empty_or_collapsed_result_is_failure_and_keeps_old_file(env, monkeypatch):
    monkeypatch.setattr(bi_export, "run_query", lambda c, s, d: _table([("E%d" % i, 2024, 1.0) for i in range(10)]))
    entry, _ = bi_export.export_mart(None, "m_fin_annual", None)
    before = (env / "m_fin_annual.parquet").read_bytes()
    monkeypatch.setattr(bi_export, "run_query", lambda c, s, d: _table([]))
    with pytest.raises(RuntimeError):
        bi_export.export_mart(None, "m_fin_annual", entry)
    monkeypatch.setattr(bi_export, "run_query", lambda c, s, d: _table([("E1", 2024, 1.0)]))
    with pytest.raises(RuntimeError):
        bi_export.export_mart(None, "m_fin_annual", entry)
    assert (env / "m_fin_annual.parquet").read_bytes() == before


def test_main_returns_nonzero_on_failure(env, monkeypatch):
    class FakeMod:
        class EDINETBQClient:
            client = object()
    monkeypatch.setitem(sys.modules, "edinet_bq_client", FakeMod)
    def boom(c, s, d):
        raise RuntimeError("bq down")
    monkeypatch.setattr(bi_export, "run_query", boom)
    assert bi_export.main(["--only", "m_company"]) == 1
    assert not (env / "manifest.json").exists()


def test_main_writes_manifest_and_templates(env, monkeypatch):
    class FakeMod:
        class EDINETBQClient:
            client = object()
    monkeypatch.setitem(sys.modules, "edinet_bq_client", FakeMod)
    monkeypatch.setattr(bi_export, "run_query", lambda c, s, d: _table([("E1", 2024, 1.0)]))
    assert bi_export.main(["--only", "m_company"]) == 0
    m = json.loads((env / "manifest.json").read_text(encoding="utf-8"))
    assert m["marts"][0]["mart"] == "m_company" and m["marts"][0]["rows"] == 1
    assert set(m["marts"][0]) >= {"mart", "rows", "columns", "generated_at", "sql_sha", "content_sha"}
    assert (env / "templates" / "finance.json").exists()
