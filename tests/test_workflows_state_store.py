"""workflow の配線: 全 yaml が読める・状態を持ち越す workflow は prepare → save → commit・artifacts を出さない・
Python の出力は actions_log.py 経由（ログに中身を出さない）。"""
import re
from pathlib import Path

import pytest
import yaml

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"
STATEFUL = ("daily_run.yml", "price_universe.yml", "us_company_facts.yml", "manual_task.yml",
            "edinetdb_web_backfill.yml", "fetch_jp_filings.yml", "edinet_gov_backfill.yml", "sec_filings.yml", "market_positioning.yml")


def _load(name):
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _main_job(d):
    """本処理のジョブ。daily_run・fetch_jp_filings は前に「今日の実行が要るか」の gate ジョブがあるので、それを除いた1つ。"""
    (job,) = [j for k, j in d["jobs"].items() if k != "gate"]
    return job


@pytest.mark.parametrize("path", sorted(WF.glob("*.yml")), ids=lambda p: p.name)
def test_all_workflows_parse(path):
    assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)


@pytest.mark.parametrize("name", STATEFUL)
def test_stateful_workflow_prepare_save_commit_order(name):
    d = _load(name)
    job = _main_job(d)
    assert job["env"]["STATE_STORE_MODE"] == "${{ vars.STATE_STORE_MODE || 'bq' }}"
    steps = job["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    i_prep = next(i for i, r in enumerate(runs) if "state_store.py prepare" in r)
    i_save = next(i for i, r in enumerate(runs) if "state_store.py save" in r)
    i_commit = next(i for i, r in enumerate(runs) if "git commit" in r)
    assert i_prep < i_save < i_commit
    # 準備は checkout の後・本処理より前
    i_checkout = next(i for i, s in enumerate(steps) if "actions/checkout" in str(s.get("uses", "")))
    first_work = next(i for i, r in enumerate(runs) if "actions_log.py" in r)
    assert i_checkout < i_prep < first_work
    # dual では save の失敗で止めない・git の commit は bq のときだけ飛ばす
    assert '[ "$STATE_STORE_MODE" = "dual" ]' in runs[i_save]
    assert steps[i_save]["if"].startswith("always()") and "!= 'git'" in steps[i_save]["if"]
    assert 'if [ "$STATE_STORE_MODE" = "bq" ]' in runs[i_commit]
    assert "exit 0" in runs[i_commit].split("git config")[0]
    # git の commit・push の流れはそのまま（-X theirs で3回再試行）
    assert "-X theirs origin main" in runs[i_commit] and "for i in 1 2 3" in runs[i_commit]


@pytest.mark.parametrize("name", STATEFUL)
def test_no_artifacts_and_python_output_goes_to_file(name):
    d = _load(name)
    job = _main_job(d)
    for s in job["steps"]:
        assert "upload-artifact" not in str(s.get("uses", ""))
        run = str(s.get("run", ""))
        if not run or s.get("if") is False or str(s.get("if", "")).startswith("false"):
            continue
        for line in run.splitlines():
            line = line.strip()
            # 本処理の Python は actions_log.py 経由。state_store・小さな判定（python3 -c）は要約しか出さない
            if re.match(r"^python3? -u ", line) and "actions_log.py" not in line:
                in_task_script = name == "manual_task.yml" and "task.sh" in run
                assert in_task_script, f"{name}: {line}"
            assert "2>&1" not in line or "actions_log.py" in line or "/dev/null" in line, f"{name}: {line}"


def test_daily_marks_written_before_save():
    d = _load("daily_run.yml")
    steps = d["jobs"]["run-analyzer"]["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    i_mark = next(i for i, r in enumerate(runs) if "last_weekly_run.json" in r and ">" in r)
    i_save = next(i for i, r in enumerate(runs) if "state_store.py save" in r)
    assert i_mark < i_save
    assert "--exclude stock_data/bi/" in runs[i_save]


def test_daily_run_gate_and_triggers():
    d = _load("daily_run.yml")
    crons = [c["cron"] for c in d[True]["schedule"]]
    assert crons[0] == "17 0 * * 1-6" and all(c.split()[4] == "1-6" for c in crons)
    # 取得 workflow の終わりに連鎖して起動する
    assert d[True]["workflow_run"]["workflows"] == ["日本企業の開示データ取得"]
    assert d[True]["workflow_run"]["types"] == ["completed"]
    gate = d["jobs"]["gate"]["steps"][-1]["run"]
    assert "tools/run_gate.py --workflow daily_run.yml" in gate and '--job "📈 アナライザー実行"' in gate
    assert "--skip-if-running fetch_jp_filings.yml" in gate and "--skip-weekday 7" in gate
    main = _main_job(d)
    assert main["needs"] == "gate" and main["if"] == "needs.gate.outputs.run == 'true'"
    assert main["name"] == "📈 アナライザー実行"                 # gate がこの名前のジョブの結論で数える
    mode = next(s for s in main["steps"] if s.get("id") == "set-mode")["run"]
    assert 'TZ=Asia/Tokyo date +%u' in mode                       # 月曜の weekly 判定は日本時間


def test_daily_run_reads_jp_filings_from_fetch_workflow():
    d = _load("daily_run.yml")
    an = next(s for s in _main_job(d)["steps"] if s.get("id") == "analyzer")
    assert an["env"]["STOCK_ANALYZER_FETCH_EXTERNAL"] == "jp_filings"
    f = _load("fetch_jp_filings.yml")
    assert f["name"] == "日本企業の開示データ取得"                 # daily_run の workflow_run がこの名前で連鎖する
    # EdinetDB の更新（毎日 08:00 JST）の後・分析の前。UTC 23 時台の日〜金 = JST 08 時台の月〜土
    for c in [c["cron"] for c in f[True]["schedule"]]:
        assert int(c.split()[1]) == 23 and c.split()[4] == "0-5"
    gate = f["jobs"]["gate"]["steps"][-1]["run"]
    assert '--job "📥 取得"' in gate and '--skip-if-done "daily_run.yml:📈 アナライザー実行"' in gate
    assert _main_job(f)["name"] == "📥 取得"
    assert "fetch_runner.py jp_filings" in str(_main_job(f)["steps"])
