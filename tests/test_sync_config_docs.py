"""tools/sync_config_docs.py: 02 → 構成管理資料/（git 管理外）→ BigQuery。commit・push はしない。"""
import importlib.util
import unicodedata
from pathlib import Path

import pytest

import state_store as ss

_spec = importlib.util.spec_from_file_location(
    "sync_config_docs_cli", Path(__file__).resolve().parent.parent / "tools" / "sync_config_docs.py")
scd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scd)


def _w(root: Path, rel: str, text: str = "x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    src = tmp_path / "02"
    _w(src, "00_README.md", "readme")
    _w(src, "01_設計書/SPEC_core.md", "spec")
    _w(src, "09_認証情報_同期対象外/key.md", "secret place")
    _w(src, "foo_key.json", "{}")
    (src / "a.numbers").write_bytes(b"\0")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(scd, "REPO", repo)
    monkeypatch.setattr(scd, "DST", repo / scd.DST_NAME)
    calls = []

    def fake_save(root, paths, *, source, delete_missing=True, cfg=None, backend=None):
        calls.append((Path(root), tuple(paths), source, delete_missing))
        return ss.SaveResult(scanned=2, uploaded=2)
    monkeypatch.setattr(ss, "save_scoped", fake_save)
    return src, repo, calls


def test_dry_run_copies_nothing(env):
    src, repo, calls = env
    assert scd.main(["--src", str(src)]) == 0
    assert not (repo / scd.DST_NAME).exists() and calls == []


def test_apply_copies_and_saves_scope_without_tombstones(env):
    src, repo, calls = env
    assert scd.main(["--src", str(src), "--apply"]) == 0
    dst = repo / scd.DST_NAME
    assert (dst / "01_設計書/SPEC_core.md").read_text(encoding="utf-8") == "spec"
    assert not (dst / "09_認証情報_同期対象外").exists() and not (dst / "foo_key.json").exists()
    assert not (dst / "a.numbers").exists()
    assert calls == [(repo, ("構成管理資料/",), "docs-sync", False)]


def test_prune_removes_orphans_and_marks_deletions(env):
    src, repo, calls = env
    _w(repo, "構成管理資料/old/gone.md")
    assert scd.main(["--src", str(src), "--apply", "--prune"]) == 0
    assert not (repo / "構成管理資料/old").exists()
    assert calls[0][3] is True


def test_secret_aborts_before_copy(env):
    src, repo, calls = env
    _w(src, "02_台帳/leak.md", "-----BEGIN PRIVATE KEY-----")
    assert scd.main(["--src", str(src), "--apply"]) == 2
    assert not (repo / scd.DST_NAME).exists() and calls == []


def test_no_bq_credentials_copies_then_exits_nonzero(env, monkeypatch, capsys):
    src, repo, _ = env

    def no_auth(*a, **k):
        raise SystemExit("❌ BQ のプロジェクトが決まらない")
    monkeypatch.setattr(ss, "save_scoped", no_auth)
    assert scd.main(["--src", str(src), "--apply"]) == 7
    assert (repo / "構成管理資料/00_README.md").exists()
    assert "BQ に保存できなかった" in capsys.readouterr().out


def test_empty_source_aborts(tmp_path, env):
    _, _, calls = env
    empty = tmp_path / "empty02"
    empty.mkdir()
    assert scd.main(["--src", str(empty), "--apply"]) == 1 and calls == []


def test_normalize_names_makes_nfc(tmp_path):
    nfd = unicodedata.normalize("NFD", "設計書_ガイド.md")
    d = tmp_path / unicodedata.normalize("NFD", "資料フォルダ_データ")
    d.mkdir()
    (d / nfd).write_text("x", encoding="utf-8")
    scd.normalize_names(tmp_path)
    names = [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")]
    assert all(n == unicodedata.normalize("NFC", n) for n in names)
    assert len(names) == 2
