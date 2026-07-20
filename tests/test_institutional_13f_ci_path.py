"""13F weeklyステップのGitHub Actionsパス契約を検証する。"""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import yaml

from institutional_13f_monitor import _inst_cache_dir


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "daily_run.yml"


def _daily_steps() -> list[dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["run-analyzer"]["steps"]


def test_weekly_13f_step_sets_writable_stock_analyzer_base():
    step = next(
        item
        for item in _daily_steps()
        if "institutional 13F monitor" in item.get("name", "")
    )

    assert step["env"]["STOCK_ANALYZER_BASE"] == "${{ github.workspace }}/stock_data"


def test_13f_cache_uses_configured_cache_dir(tmp_path):
    cfg = SimpleNamespace(CACHE_DIR=tmp_path / "cache")

    cache_dir = _inst_cache_dir(cfg)

    assert cache_dir == tmp_path / "cache" / "institutional"
    assert cache_dir.is_dir()


def test_config_without_env_uses_repo_stock_data_outside_colab():
    env = os.environ.copy()
    env.pop("STOCK_ANALYZER_BASE", None)
    code = "import config; print(config.DRIVE_BASE)"

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    expected = REPO_ROOT / "stock_data"
    assert Path(result.stdout.strip()) == expected
