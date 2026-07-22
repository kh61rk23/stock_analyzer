"""GitHub Actionsのリポジトリ相対モジュール解決を検証する。"""

from pathlib import Path

import config
import stock_analyzer_main as main


def test_nested_runtime_modules_resolve_from_repository(tmp_path):
    paper = main._load_local_module(
        "paper_broker",
        "/content/paper/paper_broker.py",
        tmp_path / "missing" / "paper_broker.py",
    )
    dataset = main._load_local_module(
        "dataset_builder",
        "/content/ml/dataset_builder.py",
        tmp_path / "missing" / "dataset_builder.py",
    )

    assert paper is not None
    assert Path(paper.__file__).resolve() == (
        Path(main.__file__).resolve().parent / "paper" / "paper_broker.py"
    )
    assert callable(paper.run_paper_trading)
    assert dataset is not None
    assert Path(dataset.__file__).resolve() == (
        Path(main.__file__).resolve().parent / "ml" / "dataset_builder.py"
    )
    assert callable(dataset.snapshot)


def test_report_writers_do_not_target_legacy_root_insights():
    repo = Path(main.__file__).resolve().parent
    writers = (
        "allocation_meta.py",
        "monthly_strategy_review.py",
        "signal_culling_report.py",
        "commodity_rotation.py",
        "tax_optimizer.py",
        "backtest/cli.py",
        "paper/monthly_report.py",
    )
    for relative in writers:
        source = (repo / relative).read_text(encoding="utf-8")
        assert 'REPO / "insights"' not in source
        assert '_REPO / "insights"' not in source
        assert "INSIGHT_DIR" in source


def test_edinet_ingest_flag_is_opt_in(monkeypatch):
    monkeypatch.delenv("ENABLE_EDINETDB_INGEST", raising=False)
    assert config.ENABLE_EDINETDB_INGEST is False


def test_edinet_workflow_runs_after_provider_refresh():
    repo = Path(main.__file__).resolve().parent
    workflow = (repo / ".github/workflows/edinetdb_ingest.yml").read_text(
        encoding="utf-8"
    )
    assert 'cron: "30 23 * * *"' in workflow
    assert 'ENABLE_EDINETDB_INGEST: "1"' in workflow
    assert "python setup_db_integration_bq.py" in workflow
    assert "run_edinetdb_ingest" in workflow
    assert "run_doc_indexer" in workflow


def test_daily_workflow_persists_all_insights():
    repo = Path(main.__file__).resolve().parent
    workflow = (repo / ".github/workflows/daily_run.yml").read_text(encoding="utf-8")
    assert "git add stock_data/insights/ 2>/dev/null || true" in workflow
