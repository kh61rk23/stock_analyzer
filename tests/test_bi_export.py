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


def export_mart(bq, mart, prev):
    """1マート分の準備→差し替え→書き出しをまとめて行う（main の流れの縮小版）。"""
    plan = bi_export.prepare_mart(None, mart, prev)
    entry = bi_export.finalize_entry(plan, bq.promote(None, mart, plan["stage"]))
    bi_export.write_outputs([plan], {mart: entry})
    return entry, plan["parquet"] is not None


def test_unchanged_content_is_not_rewritten_even_if_row_order_differs(env, bq):
    rows = [("E2", 2024, 2.0), ("E1", 2024, 1.0), ("E1", 2025, 3.0)]
    bq.table = _table(rows)
    entry, wrote = export_mart(bq, "m_fin_annual", None)
    assert wrote and entry["rows"] == 3
    assert entry["table"] == "stock-analyzer-edinet.bi.m_fin_annual" and entry["table_bytes"] == 12345
    path = env / "m_fin_annual.parquet"
    mtime = path.stat().st_mtime_ns
    bq.table = _table(list(reversed(rows)))
    entry2, wrote2 = export_mart(bq, "m_fin_annual", entry)
    assert not wrote2 and path.stat().st_mtime_ns == mtime
    assert entry2["generated_at"] == entry["generated_at"]
    assert [c["type"] for c in entry["columns"]] == ["string", "int64", "double"]
    assert pq.read_table(path).num_rows == 3


def test_unchanged_content_still_refreshes_table_info(env, bq):
    bq.table = _table([("E1", 2024, 1.0)])
    entry, _ = export_mart(bq, "m_fin_annual", None)
    old = {k: v for k, v in entry.items() if k not in ("table", "table_bytes")}
    entry2, wrote = export_mart(bq, "m_fin_annual", old)
    assert not wrote and entry2["table"].endswith(".bi.m_fin_annual") and entry2["table_bytes"] == 12345


def test_changed_content_is_rewritten(env, bq):
    bq.table = _table([("E1", 2024, 1.0), ("E2", 2024, 2.0)])
    entry, _ = export_mart(bq, "m_fin_annual", None)
    bq.table = _table([("E1", 2024, 1.0), ("E2", 2024, 9.0)])
    entry2, wrote = export_mart(bq, "m_fin_annual", entry)
    assert wrote and entry2["content_sha"] != entry["content_sha"]


def test_empty_or_collapsed_result_is_failure_keeps_old_file_and_old_table(env, bq):
    bq.table = _table([("E%d" % i, 2024, 1.0) for i in range(10)])
    entry, _ = export_mart(bq, "m_fin_annual", None)
    before = (env / "m_fin_annual.parquet").read_bytes()
    bq.calls.clear()
    for rows in ([], [("E1", 2024, 1.0)]):
        bq.table = _table(rows)
        with pytest.raises(RuntimeError):
            export_mart(bq, "m_fin_annual", entry)
    assert (env / "m_fin_annual.parquet").read_bytes() == before
    # 失敗した回は本番表へ差し替えず、ステージ表は必ず片付ける
    assert ("promote", "m_fin_annual") not in bq.calls
    assert [c[0] for c in bq.calls].count("drop") == 2


def test_stage_failure_is_not_swallowed(env, monkeypatch):
    def boom(client, mart, sql):
        raise PermissionError("bigquery.tables.create denied")
    monkeypatch.setattr(bi_export, "stage_mart", boom)
    with pytest.raises(PermissionError):
        bi_export.prepare_mart(None, "m_company", None)
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


# ── 全部成功か、全部なし ─────────────────────────────────────────────

THREE_MARTS = ["m_company", "m_fin_annual", "m_country_profile"]
THREE = ["--only", "m_company", "--only", "m_fin_annual", "--only", "m_country_profile"]


@pytest.fixture()
def main_env(env, monkeypatch, bq):
    monkeypatch.setitem(sys.modules, "edinet_bq_client", FakeMod)
    monkeypatch.setattr(bi_export, "ensure_dataset", lambda c: None)
    monkeypatch.setattr(bi_export, "refresh_source", lambda c, s: 1)
    # 合成表は edinet_code 列しか持たないので、並べ替えキーをそれに合わせる
    monkeypatch.setattr(bi_export, "MART_KEYS", {m: ["edinet_code"] for m in THREE_MARTS})
    return env


def _snapshot(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def _seed_previous_run(bq, main_env):
    """前回成功した状態（3マートの parquet と manifest と templates）を作って返す。"""
    bq.table = _table([("E%d" % i, 2024, 1.0) for i in range(10)])
    assert bi_export.main(THREE) == 0
    bq.calls.clear()
    return _snapshot(main_env)


def test_failure_in_middle_mart_changes_no_table_and_no_file(main_env, bq, monkeypatch, capsys):
    before = _seed_previous_run(bq, main_env)
    real_stage = bq.stage

    def stage(client, mart, sql):
        if mart == "m_fin_annual":
            raise RuntimeError("bq down")
        return real_stage(client, mart, sql)
    monkeypatch.setattr(bi_export, "stage_mart", stage)
    bq.table = _table([("E%d" % i, 2024, 9.0) for i in range(10)])  # 中身が変わる回でも書き出さない
    assert bi_export.main(THREE) == 1
    assert not [c for c in bq.calls if c[0] == "promote"]
    assert _snapshot(main_env) == before
    out = capsys.readouterr().out
    assert "m_fin_annual" in out and "変更していない" in out


def test_row_check_failure_in_one_mart_blocks_all(main_env, bq, monkeypatch, capsys):
    before = _seed_previous_run(bq, main_env)
    tables = {"m_company": _table([("E%d" % i, 2024, 5.0) for i in range(10)]),
              "m_fin_annual": _table([("E1", 2024, 1.0)]),  # 10行 → 1行の急減
              "m_country_profile": _table([("E%d" % i, 2024, 5.0) for i in range(10)])}
    current = {}
    real_stage = bq.stage

    def stage(client, mart, sql):
        current["m"] = mart
        bq.table = tables[mart]
        return real_stage(client, mart, sql)
    monkeypatch.setattr(bi_export, "stage_mart", stage)
    monkeypatch.setattr(bi_export, "read_stage", lambda c, st: bq.table)
    assert bi_export.main(THREE) == 1
    assert not [c for c in bq.calls if c[0] == "promote"]
    assert _snapshot(main_env) == before
    assert "m_fin_annual" in capsys.readouterr().out
    # ステージ表は3本とも片付ける
    assert [c[0] for c in bq.calls].count("drop") == 3


def test_all_marts_are_staged_and_checked_before_any_promotion(main_env, bq):
    bq.table = _table([("E1", 2024, 1.0)])
    assert bi_export.main(THREE) == 0
    kinds = [c[0] for c in bq.calls if c[0] in ("stage", "promote")]
    assert kinds == ["stage"] * 3 + ["promote"] * 3
    assert (main_env / "manifest.json").exists() and (main_env / "templates").is_dir()
    assert len(list(main_env.glob("*.parquet"))) == 3


def test_promotion_failure_leaves_files_untouched(main_env, bq, monkeypatch, capsys):
    before = _seed_previous_run(bq, main_env)
    n = {"k": 0}
    real = bq.promote

    def promote(client, mart, stage):
        n["k"] += 1
        if n["k"] == 2:
            raise RuntimeError("copy failed")
        return real(client, mart, stage)
    monkeypatch.setattr(bi_export, "promote_mart", promote)
    bq.table = _table([("E%d" % i, 2024, 7.0) for i in range(10)])
    assert bi_export.main(THREE) == 1
    assert _snapshot(main_env) == before
    out = capsys.readouterr().out
    assert "差し替えに失敗" in out and [c[0] for c in bq.calls].count("drop") == 3


def test_invalid_templates_stop_before_touching_bigquery(env, monkeypatch, bq):
    monkeypatch.setitem(sys.modules, "edinet_bq_client", FakeMod)
    def bad():
        raise RuntimeError("テンプレート検査NG")
    monkeypatch.setattr(bi_export, "check_templates", bad)
    bq.table = _table([("E1", 2024, 1.0)])
    assert bi_export.main(THREE) == 1
    assert bq.calls == [] and not list(env.iterdir())


def test_source_refresh_failure_blocks_everything(env, monkeypatch, bq, capsys):
    monkeypatch.setitem(sys.modules, "edinet_bq_client", FakeMod)
    monkeypatch.setattr(bi_export, "ensure_dataset", lambda c: None)
    monkeypatch.setattr(bi_export, "sources_for", lambda names: [{"source": "p.ontology.t", "dest": "src_t", "columns": []}])
    def boom(c, s):
        raise RuntimeError("signal_event が空")
    monkeypatch.setattr(bi_export, "refresh_source", boom)
    bq.table = _table([("E1", 2024, 1.0)])
    assert bi_export.main(THREE) == 1
    assert bq.calls == [] and not list(env.iterdir())


def test_write_failure_removes_temp_files_and_keeps_old_ones(env, bq, monkeypatch):
    bq.table = _table([("E1", 2024, 1.0), ("E2", 2024, 2.0)])
    entry, _ = export_mart(bq, "m_fin_annual", None)
    before = _snapshot(env)
    bq.table = _table([("E1", 2024, 1.0), ("E2", 2024, 3.0)])
    plan = bi_export.prepare_mart(None, "m_fin_annual", entry)
    assert plan["parquet"] is not None
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(bi_export.json, "dumps", boom)
    with pytest.raises(OSError):
        bi_export.write_outputs([plan], {"m_fin_annual": entry})
    assert _snapshot(env) == before and not list(env.glob("*.tmp"))


def test_refresh_source_copies_us_table_with_free_jobs():
    pytest.importorskip("google.cloud.bigquery")
    src = {"source": "p.ontology.signal_event", "dest": "src_signal_event", "columns": ["ticker", "direction"]}
    seen = {}

    class Job:
        def result(self):
            return self

    class Client:
        def list_rows(self, ref):
            seen["list"] = ref

            class It:
                def to_arrow(self, create_bqstorage_client=True):
                    assert create_bqstorage_client is False
                    return pa.table({"ticker": ["1.T", "2.T"], "direction": ["bullish", "bearish"], "extra": [1, 2]})
            return It()

        def query(self, *a, **k):
            raise AssertionError("クエリを使わない（費用を増やさない）")

        def load_table_from_file(self, f, dest, job_config=None, location=None):
            seen["dest"], seen["loc"] = dest, location
            seen["cfg"] = job_config
            seen["cols"] = pq.read_table(f).column_names
            return Job()
    assert bi_export.refresh_source(Client(), src) == 2
    assert seen["list"] == "p.ontology.signal_event"
    assert seen["dest"] == "stock-analyzer-edinet.bi.src_signal_event" and seen["loc"] == "asia-northeast1"
    assert seen["cols"] == ["ticker", "direction"]
    assert seen["cfg"].write_disposition == "WRITE_TRUNCATE"


def test_refresh_source_rejects_empty_source():
    pytest.importorskip("google.cloud.bigquery")
    class Client:
        def list_rows(self, ref):
            class It:
                def to_arrow(self, create_bqstorage_client=True):
                    return pa.table({"ticker": pa.array([], pa.string())})
            return It()
    with pytest.raises(RuntimeError):
        bi_export.refresh_source(Client(), {"source": "p.o.t", "dest": "src_t", "columns": ["ticker"]})
