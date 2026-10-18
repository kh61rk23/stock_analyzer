from pathlib import Path

import obsidian_sync as osy
import weekly_feedback_runner as wfr


def test_obsidian_sync_defaults_to_mirror_outside_worktree(tmp_path, monkeypatch):
    monkeypatch.delenv("STOCK_ANALYZER_BASE", raising=False)
    monkeypatch.setattr(osy, "MIRROR_BASE", tmp_path / "mirror")
    cfg = osy._load_config()
    assert Path(cfg.DRIVE_BASE) == tmp_path / "mirror"
    assert Path(cfg.DATA_DIR) == tmp_path / "mirror" / "data"
    assert osy.REPO_ROOT not in Path(cfg.DRIVE_BASE).parents
    assert "bi" in osy.MIRROR_SUBDIRS


def test_obsidian_sync_respects_env_base(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_ANALYZER_BASE", str(tmp_path / "x"))
    assert Path(osy._load_config().DRIVE_BASE) == tmp_path / "x"


def test_weekly_status_file_follows_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_ANALYZER_BASE", str(tmp_path))
    assert wfr.status_file() == tmp_path / "weekly_loop_status.json"
    assert wfr.status_file(tmp_path / "repo") == tmp_path / "repo" / "stock_data" / "weekly_loop_status.json"
    monkeypatch.delenv("STOCK_ANALYZER_BASE")
    assert wfr.status_file().parent.name == "stock_data"
