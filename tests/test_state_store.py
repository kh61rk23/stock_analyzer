"""state_store の純関数と、偽の BQ を使った save → restore の往復。"""
import base64
import json
import time
from pathlib import Path

import random

import pytest

import state_store as ss


class FakeBackend:
    """load job（NDJSON の追記）と tabledata.list（並び順の読み出し）だけを真似る。"""

    def __init__(self):
        self.rows = []
        self.jobs = []
        self.ensured = 0
        self.table_ref = "proj.private.state_files"

    def ensure(self):
        self.ensured += 1

    def iter_meta(self):
        for i, r in enumerate(self.rows):
            yield i, {k: r.get(k) for k in ss.META_FIELDS}

    def iter_full(self):
        for i, r in enumerate(self.rows):
            yield i, dict(r)

    def fetch_row(self, index):
        return dict(self.rows[index]) if 0 <= index < len(self.rows) else None

    def load_file(self, path, job_id):
        if job_id in self.jobs:
            return True
        self.jobs.append(job_id)
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("content") is not None:
                r["content"] = base64.b64decode(r["content"])
            self.rows.append(r)
        return True


def _write(root: Path, rel: str, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


PATHS = ("stock_data/", "data/portfolio_holdings.csv")
EXCL = ("stock_data/models/", "__pycache__/", ".DS_Store")


# ── 純関数 ─────────────────────────────────────────

def test_is_excluded_prefix_name_and_dir_segment():
    assert ss.is_excluded("stock_data/models/x.bin", EXCL)
    assert ss.is_excluded("stock_data/data/__pycache__/a.pyc", EXCL)
    assert ss.is_excluded("stock_data/.DS_Store", EXCL)
    assert ss.is_excluded(ss.BASELINE_NAME, EXCL)
    assert not ss.is_excluded("stock_data/data/models.json", EXCL)
    assert not ss.is_excluded("stock_data/data/a.json", EXCL)


def test_in_scope():
    assert ss.in_scope("stock_data/a/b.json", PATHS)
    assert ss.in_scope("data/portfolio_holdings.csv", PATHS)
    assert not ss.in_scope("data/portfolio_summary.json", PATHS)
    assert not ss.in_scope("stock_data_x/a", PATHS)


def test_scan_walks_dirs_files_and_excludes(tmp_path):
    _write(tmp_path, "stock_data/data/a.json", b"{}")
    _write(tmp_path, "stock_data/models/big.bin", b"x" * 10)
    _write(tmp_path, "stock_data/.DS_Store", b"x")
    _write(tmp_path, "data/portfolio_holdings.csv", b"t,q\n")
    _write(tmp_path, "data/other.csv", b"no")
    got = ss.scan(tmp_path, PATHS, EXCL)
    assert set(got) == {"stock_data/data/a.json", "data/portfolio_holdings.csv"}
    assert got["stock_data/data/a.json"]["sha256"] == ss.sha256_bytes(b"{}")
    assert got["data/portfolio_holdings.csv"]["size"] == 4


def test_split_chunks_and_assemble_roundtrip():
    import gzip
    data = bytes(range(256)) * 1000
    gz = gzip.compress(data, mtime=0)
    chunks = ss.split_chunks(gz, 1000)
    assert len(chunks) == -(-len(gz) // 1000)
    assert b"".join(chunks) == gz
    parts = dict(enumerate(chunks))
    assert ss.assemble(parts, len(chunks), ss.sha256_bytes(data)) == data
    # 欠け・sha 不一致は None
    del parts[1]
    assert ss.assemble(parts, len(chunks), ss.sha256_bytes(data)) is None
    assert ss.assemble(dict(enumerate(chunks)), len(chunks), "0" * 64) is None
    assert ss.split_chunks(b"", 10) == [b""]
    with pytest.raises(ValueError):
        ss.split_chunks(b"a", 0)


def test_build_rows_splits_and_tombstone():
    data = random.Random(1).randbytes(20000)
    rows = ss.build_rows("stock_data/x", data, "v1", "2026-10-20T00:00:00Z", "r", "src", 1024)
    assert len(rows) > 1
    assert [r["part"] for r in rows] == list(range(len(rows)))
    assert all(r["parts"] == len(rows) and r["sha256"] == ss.sha256_bytes(data) for r in rows)
    parts = {r["part"]: base64.b64decode(r["content"]) for r in rows}
    assert ss.assemble(parts, len(rows), rows[0]["sha256"]) == data
    t = ss.build_rows("stock_data/x", None, "v2", "2026-10-20T00:00:00Z", "", "src", 1024)
    assert len(t) == 1 and t[0]["deleted"] is True and t[0]["content"] is None


def test_latest_versions_picks_newest_and_breaks_ties_by_version():
    rows = [
        {"path": "a", "version_id": "v1", "saved_at": "2026-10-01T00:00:00Z", "sha256": "s1", "parts": 1},
        {"path": "a", "version_id": "v3", "saved_at": "2026-10-03T00:00:00Z", "sha256": "s3", "parts": 1},
        {"path": "a", "version_id": "v2", "saved_at": "2026-10-02T00:00:00Z", "sha256": "s2", "parts": 1},
        {"path": "b", "version_id": "aa", "saved_at": "2026-10-02T00:00:00Z", "sha256": "x", "parts": 1},
        {"path": "b", "version_id": "bb", "saved_at": "2026-10-02T00:00:00Z", "sha256": None,
         "deleted": True, "parts": 0},
    ]
    got = ss.latest_versions(rows)
    assert got["a"]["sha256"] == "s3"
    assert got["b"]["version_id"] == "bb" and got["b"]["deleted"]


def _lv(sha, at, deleted=False):
    return {"sha256": sha, "saved_at": at, "deleted": deleted, "version_id": "v", "parts": 1}


def test_plan_changes_basic_upload_delete_and_same():
    local = {"stock_data/a": {"sha256": "A2"}, "stock_data/b": {"sha256": "B"}, "stock_data/new": {"sha256": "N"}}
    latest = {"stock_data/a": _lv("A1", 10), "stock_data/b": _lv("B", 10), "stock_data/gone": _lv("G", 10),
              "stock_data/dead": _lv(None, 10, deleted=True)}
    plan = ss.plan_changes(local, latest, None, None, scope_paths=PATHS)
    assert plan["upload"] == ["stock_data/a", "stock_data/new"]
    assert plan["delete"] == ["stock_data/gone"]


def test_plan_changes_does_not_overwrite_newer_save_from_other_run():
    # この実行は a を触っていない（基準と同じ）。別の実行が開始後に a を保存した → 上書きしない
    local = {"stock_data/a": {"sha256": "OLD"}, "stock_data/b": {"sha256": "MINE"}}
    latest = {"stock_data/a": _lv("NEW", 200), "stock_data/b": _lv("THEIRS", 200)}
    base = {"stock_data/a": "OLD", "stock_data/b": "OLD_B"}
    plan = ss.plan_changes(local, latest, base, 100.0, scope_paths=PATHS)
    # b はこの実行が変えた → 最後に保存した方が勝つ
    assert plan["upload"] == ["stock_data/b"]
    assert plan["skipped_newer"] == 1


def test_plan_changes_syncs_git_content_when_bq_is_older_than_baseline():
    # dual: Mac が git に push した新しい中身。BQ の版は実行開始より前 → 手元を保存して揃える
    local = {"stock_data/data/invest_approvals.json": {"sha256": "GIT"}}
    latest = {"stock_data/data/invest_approvals.json": _lv("OLD", 50)}
    base = {"stock_data/data/invest_approvals.json": "GIT"}
    plan = ss.plan_changes(local, latest, base, 100.0, scope_paths=PATHS)
    assert plan["upload"] == ["stock_data/data/invest_approvals.json"]


def test_plan_changes_scope_limits_tombstones():
    local = {"stock_data/data/x.json": {"sha256": "X"}}
    latest = {"stock_data/data/x.json": _lv("X", 1), "stock_data/data/y.json": _lv("Y", 1),
              "data/portfolio_holdings.csv": _lv("H", 1)}
    plan = ss.plan_changes(local, latest, None, None, scope_paths=("stock_data/data/x.json",))
    assert plan == {"upload": [], "delete": [], "skipped_newer": 0}
    # 除外（その回だけの --exclude）に当たるパスは削除の印を付けない
    plan = ss.plan_changes({}, {"stock_data/bi/m.parquet": _lv("M", 1)}, None, None,
                           scope_paths=PATHS, excludes=("stock_data/bi/",))
    assert plan["delete"] == []


def test_make_version_id_is_content_based():
    e = [("a", "1"), ("b", "2")]
    v1 = ss.make_version_id("daily_run", "123-1", e, "t1")
    assert v1 == ss.make_version_id("daily_run", "123-1", list(reversed(e)), "t2")
    assert v1 != ss.make_version_id("daily_run", "124-1", e, "t1")
    # run_id の無い手元の実行は時刻で分かれる
    assert ss.make_version_id("local", "", e, "t1") != ss.make_version_id("local", "", e, "t2")


def test_baseline_roundtrip(tmp_path):
    p = tmp_path / ss.BASELINE_NAME
    ss.write_baseline(p, {"a": {"sha256": "x", "size": 1}}, created_at=123.0)
    assert ss.read_baseline(p) == ({"a": "x"}, 123.0)
    assert ss.read_baseline(tmp_path / "none.json") == (None, None)


def test_dest_for_strip():
    assert ss.dest_for("stock_data/logs/a.log", Path("/m"), "stock_data/") == Path("/m/logs/a.log")
    assert ss.dest_for("data/x.csv", Path("/r")) == Path("/r/data/x.csv")


def test_resolve_mode(monkeypatch):
    class C:
        STATE_STORE_MODE = "dual"
    monkeypatch.delenv("STATE_STORE_MODE", raising=False)
    assert ss.resolve_mode(C) == "dual"
    monkeypatch.setenv("STATE_STORE_MODE", "BQ")
    assert ss.resolve_mode(C) == "bq"
    monkeypatch.setenv("STATE_STORE_MODE", "nonsense")
    assert ss.resolve_mode(C) == "git"


# ── 偽の BQ での往復 ─────────────────────────────────

def test_save_restore_roundtrip_with_chunks_deletes_and_versions(tmp_path):
    src = tmp_path / "src"
    big = random.Random(2).randbytes(100000)   # gzip しても縮まないので複数断片になる
    _write(src, "stock_data/data/a.json", b'{"a": 1}')
    _write(src, "stock_data/bi/m.parquet", big)
    _write(src, "stock_data/models/skip.bin", b"skip")
    _write(src, "data/portfolio_holdings.csv", b"t,q\n")
    be = FakeBackend()
    r1 = ss.save(be, src, PATHS, EXCL, source="t", run_id="1", chunk_bytes=4096, batch_bytes=50000)
    assert r1.ok and r1.uploaded == 3 and r1.deleted == 0 and r1.jobs >= 2
    assert not any(r["path"].startswith("stock_data/models/") for r in be.rows)
    assert max(r["parts"] for r in be.rows) > 1

    # 2回目: 変わらなければ何も書かない
    n = len(be.rows)
    r2 = ss.save(be, src, PATHS, EXCL, source="t", run_id="2", chunk_bytes=4096)
    assert r2.uploaded == 0 and r2.deleted == 0 and len(be.rows) == n

    # 1つ変えて1つ消す
    _write(src, "stock_data/data/a.json", b'{"a": 2}')
    (src / "data/portfolio_holdings.csv").unlink()
    r3 = ss.save(be, src, PATHS, EXCL, source="t", run_id="3", chunk_bytes=4096)
    assert r3.uploaded == 1 and r3.deleted == 1

    # 空のフォルダへ戻す（変わったパスが少ないので行番号で取り出す）
    dst = tmp_path / "dst"
    rr = ss.restore(be, dst)
    assert rr.bad == 0 and rr.written == 2 and rr.mode == "indexed"
    assert (dst / "stock_data/data/a.json").read_bytes() == b'{"a": 2}'
    assert (dst / "stock_data/bi/m.parquet").read_bytes() == big
    assert not (dst / "data/portfolio_holdings.csv").exists()

    # 同じ中身はもう書かない。削除の印は remove_deleted のときだけ手元から消す
    _write(dst, "data/portfolio_holdings.csv", b"stale")
    rr2 = ss.restore(be, dst)
    assert rr2.written == 0 and rr2.unchanged == 2 and (dst / "data/portfolio_holdings.csv").exists()
    rr3 = ss.restore(be, dst, remove_deleted=True)
    assert rr3.removed == 1 and not (dst / "data/portfolio_holdings.csv").exists()


def test_restore_prefix_strip_and_full_fallback(tmp_path):
    src = tmp_path / "src"
    for i in range(5):
        _write(src, f"stock_data/logs/actions_daily_{i}.log", f"log{i}".encode())
    _write(src, "stock_data/data/keep.json", b"k")
    be = FakeBackend()
    ss.save(be, src, PATHS, EXCL, source="t", run_id="1")
    mirror = tmp_path / "mirror"
    old = ss.INDEXED_FETCH_MAX
    ss.INDEXED_FETCH_MAX = 2
    try:
        rr = ss.restore(be, mirror, ["stock_data/logs/actions_"], strip="stock_data/")
    finally:
        ss.INDEXED_FETCH_MAX = old
    assert rr.mode == "full" and rr.written == 5
    assert (mirror / "logs/actions_daily_3.log").read_bytes() == b"log3"
    assert not (mirror / "data/keep.json").exists()


def test_restore_falls_back_when_row_order_changed(tmp_path):
    src = tmp_path / "src"
    _write(src, "stock_data/a", b"A")
    _write(src, "stock_data/b", b"B")
    be = FakeBackend()
    ss.save(be, src, PATHS, EXCL, source="t", run_id="1")
    real_fetch = be.fetch_row
    be.fetch_row = lambda i: real_fetch(len(be.rows) - 1 - i)   # 並びが入れ替わった
    rr = ss.restore(be, tmp_path / "dst")
    assert rr.mode == "full" and rr.written == 2 and rr.bad == 0


def test_save_with_baseline_skips_paths_saved_later_by_other_run(tmp_path):
    root = tmp_path / "r"
    _write(root, "stock_data/a", b"old")
    _write(root, "stock_data/b", b"old")
    be = FakeBackend()
    ss.save(be, root, PATHS, EXCL, source="t", run_id="0")
    bl = root / ss.BASELINE_NAME
    ss.write_baseline(bl, ss.scan(root, PATHS, EXCL), created_at=time.time() - 1)
    # 別の実行が a を新しく保存
    other = tmp_path / "o"
    _write(other, "stock_data/a", b"theirs")
    ss.save(be, other, ("stock_data/a",), EXCL, source="other", run_id="9")
    # この実行は b だけ変えた
    _write(root, "stock_data/b", b"mine")
    r = ss.save(be, root, PATHS, EXCL, source="t", run_id="1", baseline_path=bl)
    assert r.uploaded == 1 and r.skipped_newer == 1
    latest = ss.latest_versions(r for _, r in be.iter_meta())
    assert latest["stock_data/a"]["sha256"] == ss.sha256_bytes(b"theirs")
    assert latest["stock_data/b"]["sha256"] == ss.sha256_bytes(b"mine")
    # baseline ファイル自身は保存しない
    assert not any(r["path"].endswith(ss.BASELINE_NAME) for r in be.rows)


def test_main_git_mode_does_nothing(monkeypatch, capsys):
    monkeypatch.setenv("STATE_STORE_MODE", "git")
    assert ss.main(["save"]) == 0
    assert ss.main(["prepare"]) == 0
    out = capsys.readouterr().out
    assert "STATE_STORE_MODE=git" in out


class _RowsClient:
    """list_rows(start_index, max_results) だけを持つ偽の BQ クライアント。返す順をばらつかせる。"""

    def __init__(self, n):
        self.rows = [{"path": f"p{i}", "part": 0} for i in range(n)]

    def list_rows(self, table, selected_fields=None, start_index=0, max_results=None, page_size=None):
        import random
        import time as _t
        _t.sleep(random.random() / 200)
        return [_Row(r) for r in self.rows[start_index:start_index + max_results]]


class _Row(dict):
    pass


def test_bq_iter_full_parallel_keeps_table_order(monkeypatch):
    monkeypatch.setattr(ss, "FULL_FETCH_SPAN", 7)
    monkeypatch.setattr(ss, "FULL_FETCH_WORKERS", 4)
    b = object.__new__(ss.BQBackend)
    b.client, b.table_ref, b._schema = _RowsClient(53), "t", []
    b.table_info = lambda: {"num_rows": 53}
    got = list(b.iter_full())
    assert [i for i, _ in got] == list(range(53))
    assert [r["path"] for _, r in got] == [f"p{i}" for i in range(53)]
