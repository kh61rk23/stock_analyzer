"""actions_log: 出力はファイルへ、ログには要約だけ。"""
import sys

import actions_log as al


def test_mask_line_hides_long_numbers_keeps_line_numbers():
    s = al.mask_line('File "x.py", line 1234, in f: 総資産 12,345,678円 7203.T score 81.25')
    assert "line 1234" in s
    assert "12,345,678" not in s and "7203" not in s and "81.25" not in s


def test_summarize_success_has_no_content():
    lines = ["✅ 保有 7203 トヨタ 1,000,000円", "⚠️ x", "plain"]
    out = al.summarize(lines, 0, 12.3, "analyzer")
    assert len(out) == 1
    assert "成功" in out[0] and "3行" in out[0] and "トヨタ" not in out[0]


def test_summarize_failure_shows_masked_error_tail():
    lines = ["ok"] * 5 + ["Traceback (most recent call last):", "ValueError: 資産 9,999,999 が不正"]
    out = al.summarize(lines, 1, 1.0, "s")
    assert out[0].startswith("❌")
    assert any("ValueError" in l for l in out)
    assert not any("9,999,999" in l for l in out)


def test_main_writes_log_and_prints_summary_only(tmp_path, capsys, monkeypatch):
    summ = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    code = ("import os,sys;print('✅ secret 1,234,567円');"
            "open(os.environ['GITHUB_STEP_SUMMARY'],'a').write('child summary 7203');sys.exit(0)")
    rc = al.main(["--workflow", "daily_run", "--step", "s", "--base", str(tmp_path / "sd"),
                  "--", sys.executable, "-c", code])
    assert rc == 0
    out = capsys.readouterr().out
    assert "secret" not in out and "1,234,567" not in out and "出力 1行" in out
    logs = list((tmp_path / "sd" / "logs").glob("actions_daily_run_*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "secret 1,234,567" in text and "child summary 7203" in text
    s = summ.read_text(encoding="utf-8")
    assert "7203" not in s and "成功" in s


def test_main_propagates_exit_code(tmp_path, capsys):
    rc = al.main(["--workflow", "w", "--step", "s", "--base", str(tmp_path),
                  "--", sys.executable, "-c", "import sys;sys.exit(4)"])
    assert rc == 4
