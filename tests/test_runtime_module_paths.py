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
    """EdinetDB 取込は日次でも既定で有効（ENABLE_EDINETDB_INGEST=True）。

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
    # UTC 00:17 = JST 09:17。UTC 00:00 ちょうどは混雑で数時間遅延するため分をずらす。
    # ⚠️ UTC 00:xx を維持すること。前倒しして 23:xx にすると JST月曜の回が
    #   UTC では日曜になり、下の weekly 判定が一度も成立しなくなる。
    assert 'cron: "17 0 * * 1-6"' in workflow
    assert '$(date -u +%u)" = "1"' in workflow
    assert "-- python -u setup_db_integration_bq.py" in workflow
    # BQスキーマ準備は本体より先。EdinetDB同期そのものは本体側で走る。
    assert workflow.index("setup_db_integration_bq.py") < workflow.index(
        "🚀 Run analyzer")
    assert not (repo / ".github/workflows/edinetdb_ingest.yml").exists()
    assert not (repo / "daily_run.yml").exists()


def test_edinetdb_sync_is_not_duplicated_in_workflow():
    """EdinetDB同期をワークフローから直接呼ばない（1日2回叩かない）

    専用ステップと DAILY_MODULES の両方にあると、専用ステップが日次予算を
    ほぼ使い切ったあとに本体が走り、本体の edinetdb_ingest が予算到達で空振りする。
    """
    import config

    repo = Path(main.__file__).resolve().parent
    workflow = (repo / ".github/workflows/daily_run.yml").read_text(
        encoding="utf-8"
    )
    # コメントでの言及は許容するが、実際の呼び出し行があってはならない
    for call in ("ingest = run_edinetdb_ingest(", "docs = run_doc_indexer("):
        assert call not in workflow, (
            f"{call} がワークフローに残っている。本体側と二重実行になる")
    # 本体側の実行経路は生きていること
    assert "edinetdb_ingest" in config.DAILY_MODULES
    assert "edinet_doc_index" in config.DAILY_MODULES


def test_all_schedulers_are_set_to_0900_jst():
    repo = Path(main.__file__).resolve().parent
    colab = (repo / "colab_analyzer_start_v3.py").read_text(encoding="utf-8")
    assert config.RUN_SCHEDULE_HOUR == 9
    assert "SCHEDULE_HOUR   = 9" in colab


def test_daily_workflow_persists_all_insights():
    repo = Path(main.__file__).resolve().parent
    workflow = (repo / ".github/workflows/daily_run.yml").read_text(encoding="utf-8")
    assert "git add stock_data/insights/ 2>/dev/null || true" in workflow
