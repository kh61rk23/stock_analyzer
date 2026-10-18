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


def test_13f_runs_only_from_main_not_from_a_workflow_step():
    # 13F は本体が四半期の提出期間の判定つきで呼ぶ。専用の事前ステップを置くと二重に走る
    steps = _daily_steps()
    assert not any("13F" in item.get("name", "") for item in steps)
    assert not any("run_institutional_13f_monitor" in item.get("run", "") for item in steps)
    main_src = (REPO_ROOT / "stock_analyzer_main.py").read_text(encoding="utf-8")
    assert "run_institutional_13f_monitor" in main_src


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
