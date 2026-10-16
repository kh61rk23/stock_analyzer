"""月次：月初の数日を逃しても追いつく・抜けた過去月を見つける。"""
from datetime import date

import monthly_runner as mr


def test_missing_months_finds_gap(tmp_path):
    for m in ("202609", "202607"):
        (tmp_path / f"strategy_review_{m}.md").write_text("x", encoding="utf-8")
    assert mr.missing_months(date(2026, 10, 17), tmp_path) == ["202608"]
    assert mr.missing_months(date(2026, 1, 5), tmp_path) == ["202510", "202511", "202512"]


def test_after_month_start_runs_when_incomplete(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mr, "STATUS_PATH", tmp_path / "monthly_loop_status.json")
    monkeypatch.setattr(mr, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 9, 10))}))
    rc = mr.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "未完了のため追いつく" in out and "月次ランナー開始: 対象月=202608" in out
