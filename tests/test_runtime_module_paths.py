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


def test_edinet_ingest_is_enabled_by_default():
    """2026-07-30（健太決定）: 日次でも EdinetDB を叩く方針へ変更。

    旧テスト名 test_edinet_ingest_flag_is_opt_in は
    「既定 False（opt-in）」を固定していたが、その前提自体が覆った。
    月次コール枠を使い切ったら edinetdb_ingestor 側が自動スキップするため、
    既定 True でも枠を超過しない設計になっている。
    """
    assert config.ENABLE_EDINETDB_INGEST is True


def test_edinet_ingest_flag_can_be_disabled(monkeypatch):
    """環境変数で明示的に止められること（非常口が残っていること）。"""
    monkeypatch.setenv("ENABLE_EDINETDB_INGEST", "0")
    import importlib
    reloaded = importlib.reload(config)
    try:
        assert reloaded.ENABLE_EDINETDB_INGEST is False
    finally:
        monkeypatch.delenv("ENABLE_EDINETDB_INGEST", raising=False)
        importlib.reload(config)


def test_daily_workflow_runs_edinet_after_provider_refresh():
    repo = Path(main.__file__).resolve().parent
    workflow = (repo / ".github/workflows/daily_run.yml").read_text(
        encoding="utf-8"
    )
    # UTC 00:00 = JST 09:00。日曜のみ停止（Actions無料枠の節約。
    # 土 09:00 JST は米国金曜の引け後にあたるため存置する）。
    # 月曜は 1-6 に含まれるので weekly 昇格の判定は従来どおり効く。
    assert 'cron: "0 0 * * 1-6"' in workflow
    assert '$(date -u +%u)" = "1"' in workflow
    assert 'ENABLE_EDINETDB_INGEST: "1"' in workflow
    assert "python setup_db_integration_bq.py" in workflow
    assert "run_edinetdb_ingest" in workflow
    assert "run_doc_indexer" in workflow
    assert workflow.index("run_edinetdb_ingest") < workflow.index("🚀 Run analyzer")
    assert not (repo / ".github/workflows/edinetdb_ingest.yml").exists()
    assert not (repo / "daily_run.yml").exists()


def test_all_schedulers_are_set_to_0900_jst():
    repo = Path(main.__file__).resolve().parent
    colab = (repo / "colab_analyzer_start_v3.py").read_text(encoding="utf-8")
    assert config.RUN_SCHEDULE_HOUR == 9
    assert "SCHEDULE_HOUR   = 9" in colab


def test_daily_workflow_persists_all_insights():
    repo = Path(main.__file__).resolve().parent
    workflow = (repo / ".github/workflows/daily_run.yml").read_text(encoding="utf-8")
    assert "git add stock_data/insights/ 2>/dev/null || true" in workflow
