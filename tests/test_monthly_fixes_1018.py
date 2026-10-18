# 月次（メール・monthly_runner・戦略レビュー・ペーパー成績・シグナル淘汰の入力・ルール重みの月次判定）の修正
import ast
import json
import types
from datetime import date
from pathlib import Path

from conftest import load_module

REPO = Path(__file__).parent.parent


# ── monthly_runner: 回数の上限・メールは他が終わってから ─────────────

def _fake_today(mr, d):
    return type("D", (), {"today": staticmethod(lambda: d)})


def _run_month(mr, monkeypatch, tmp_path, fail: set, day: date):
    monkeypatch.setattr(mr, "STATUS_PATH", tmp_path / "monthly_loop_status.json")
    monkeypatch.setattr(mr, "date", _fake_today(mr, day))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        name = Path(cmd[1]).stem
        return types.SimpleNamespace(returncode=1 if name in fail else 0, stdout="", stderr="boom")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    rc = mr.main([])
    st = json.loads((tmp_path / "monthly_loop_status.json").read_text(encoding="utf-8"))
    return rc, st, calls


def test_mail_waits_then_gives_up(monkeypatch, tmp_path):
    mr = load_module("monthly_runner")
    # 1日目: signal_culling が失敗 → メールは待つ
    rc, st, calls = _run_month(mr, monkeypatch, tmp_path, {"signal_culling_report"}, date(2026, 11, 1))
    assert rc == 1
    assert st["steps"]["monthly_mail"].startswith("waiting")
    assert not any("monthly_mail.py" in c[1] for c in calls)
    assert st["attempts"]["signal_culling"] == 1
    # 2日目・3日目も失敗 → 3回目で上限
    _run_month(mr, monkeypatch, tmp_path, {"signal_culling_report"}, date(2026, 11, 2))
    rc, st, calls = _run_month(mr, monkeypatch, tmp_path, {"signal_culling_report"}, date(2026, 11, 3))
    assert st["attempts"]["signal_culling"] == 3
    # 4日目: もう試さず諦め、メールは諦めた旨を付けて送る
    rc, st, calls = _run_month(mr, monkeypatch, tmp_path, {"signal_culling_report"}, date(2026, 11, 4))
    assert st["steps"]["signal_culling"].startswith("gave_up")
    assert not any("signal_culling_report.py" in c[1] for c in calls)
    mail = [c for c in calls if "monthly_mail.py" in c[1]]
    assert mail and mail[0][-2:] == ["--gave-up", "signal_culling"]
    assert st["steps"]["monthly_mail"] == "success" and rc == 0
    # 5日目: 全部済みなので何もしない
    rc, st, calls = _run_month(mr, monkeypatch, tmp_path, set(), date(2026, 11, 5))
    assert calls == [] and rc == 0


def test_mail_blockers():
    mr = load_module("monthly_runner")
    assert mr.mail_blockers({"a": "success", "b": "skipped / x", "c": "gave_up / 3回",
                             "monthly_mail": "waiting"}) == []
    assert mr.mail_blockers({"a": "exit 1 / boom", "b": "success"}) == ["a"]


# ── 月次メール ───────────────────────────────────────────────

def test_monthly_mail_tax_only_in_tax_months(tmp_path):
    mm = load_module("monthly_mail")
    for f in ("strategy_review_202609.md", "tax_report_2026.md", "strategy_review_202610.md"):
        (tmp_path / f).write_text("# x\n", encoding="utf-8")
    t9 = [t for t, _ in mm.report_files(tmp_path, "202609")]      # 10月に送る＝実施月
    t10 = [t for t, _ in mm.report_files(tmp_path, "202610")]     # 11月に送る＝実施月
    (tmp_path / "strategy_review_202611.md").write_text("# x\n", encoding="utf-8")
    t11 = [t for t, _ in mm.report_files(tmp_path, "202611")]     # 12月は実施月でない
    assert any("税" in t for t in t9) and any("税" in t for t in t10)
    assert not any("税" in t for t in t11)


def test_monthly_mail_inline_italic_and_bold():
    mm = load_module("monthly_mail")
    assert mm._inline("*自動では変更しない。* と **太字**") == "<i>自動では変更しない。</i> と <b>太字</b>"
    assert mm._inline("2*3*4") == "2*3*4"


def test_monthly_mail_empty_is_failure_and_mac_does_not_send(tmp_path, monkeypatch):
    mm = load_module("monthly_mail")
    monkeypatch.setattr(mm, "REPO", tmp_path)
    sent = []
    import alert_notifier
    monkeypatch.setattr(alert_notifier, "send_gmail", lambda *a, **k: (sent.append(a), (True, ""))[1])

    class Cfg:
        INSIGHT_DIR = tmp_path

    import sys
    monkeypatch.setitem(sys.modules, "config", Cfg)
    assert mm.main(["202609"]) == 1                       # レポート0本は失敗
    (tmp_path / "strategy_review_202609.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert mm.main(["202609"]) == 0 and sent == []        # Actions の外では送らない
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert mm.main(["202609", "--gave-up", "signal_culling"]) == 0 and len(sent) == 1
    assert "signal_culling" in sent[0][4]


def test_monthly_mail_reads_insight_dir_setting():
    text = (REPO / "monthly_mail.py").read_text(encoding="utf-8")
    assert '"INSIGHTS_DIR"' not in text and '"INSIGHT_DIR"' in text


# ── 月次戦略レビュー ─────────────────────────────────────────

def test_strategy_review_sources():
    sr = load_module("monthly_strategy_review")
    perf = {"log_entries": [
        {"week": "2026-W41", "evaluated": 173, "win_rate_overall": 0.474, "win_rate_score80+": 0.464,
         "win_rate_60_80": 0.511, "win_rate_40_60": 0, "win_rate_sub40": 0, "avg_alpha": -0.062},
        {"week": "2026-W36", "evaluated": 10, "win_rate_overall": 0.5},
    ]}
    tbl = sr._score_band_table(perf, "202610")
    assert len(tbl) == 3 and "2026-W41" in tbl[2] and "47%" in tbl[2]
    assert sr._iso_week_month("2026-W40") == "2026-10"    # 木曜 10/1 → 10月
    recs = [{"deliberated_at": "2026-09-26T03:00:00+00:00", "verdict": "Watch", "approval_status": "承認"},
            {"deliberated_at": "2026-09-01T03:00:00+00:00", "verdict": "Avoid"},
            {"deliberated_at": "2026-10-01T03:00:00+00:00", "verdict": "Buy"}]
    assert sr._count_deliberations_month(recs, "202609") == {
        "count": 2, "by_verdict": {"Watch": 1, "Avoid": 1}, "approved": 1}
    md = sr.build_review({"learning_audit": {"factors_updated": [
        {"factor": "yen_depreciation", "old_weight": 0.95, "new_weight": 1.12}]},
        "deliberations_month": {"count": 2, "by_verdict": {"Watch": 2}, "approved": 0}}, "202609")
    assert "yen_depreciation: 0.95 → 1.12" in md and "当月の審議: 2件" in md


# ── ペーパー成績 ─────────────────────────────────────────────

def test_paper_month_start_positions_and_fills():
    from paper import monthly_report as pm
    hist = [{"date": "2026-09-30", "equity": 2000000.0}, {"date": "2026-10-01", "equity": 2019248.0},
            {"date": "2026-10-17", "equity": 1946918.0}]
    assert pm.month_start_equity(hist, "2026-10") == 2000000.0
    assert pm.month_start_equity(hist[:1], "2026-09") == 2000000.0
    events = [{"type": "init", "strategy": "s", "cash": 100.0},
              {"type": "fill", "strategy": "s", "date": "2026-10-02", "ticker": "A", "side": "BUY", "qty": 1, "price": 10}]
    assert pm.positions_at_month_end(events, "2026-09") == {"s": {}}
    assert pm.positions_at_month_end(events, "2026-10")["s"]["A"]["qty"] == 1
    md = pm.build_report({"s": {"equity_history": hist, "positions": {"A": {"qty": 1, "avg_cost": 10}}}},
                         "202609", None, events=events)
    sect = md[md.index("## 保有"):]
    assert "月末時点" in sect and "| s | A |" not in sect          # 9月末にはまだ持っていない
    assert "| s | 0 |" in md                                     # 9月までの約定は0件


# ── シグナル淘汰の入力（カテゴリパネル）は signal_aggregator の後で書く ──

def test_category_panel_written_after_signal_aggregator():
    src = (REPO / "stock_analyzer_main.py").read_text(encoding="utf-8")
    agg = src.index('results["signal_aggregator"] = sa_result')
    panel = src.index("record_category_panel(cfg, results)")
    assert panel > agg


def test_learning_audit_not_inside_except():
    tree = ast.parse((REPO / "stock_analyzer_main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr == "generate_audit_log":
                    raise AssertionError("learning_audit の生成が except 節の中にある（成功した日に動かない）")


# ── ルール重み・IC の月次判定 ─────────────────────────────────

def test_rule_optimizer_month_end_window(tmp_path):
    ro = load_module("rule_optimizer")
    # 2026-10 の最後の7日間（25〜31日）に入る月曜は 26日だけ
    mondays = [d for d in range(1, 32) if date(2026, 10, d).weekday() == 0]
    assert [d for d in mondays if ro.in_month_end_window(date(2026, 10, d))] == [26]
    # 月末が日曜の月（2026-05-31）でも、その前の月曜（25日）で動く
    assert ro.in_month_end_window(date(2026, 5, 25)) and not ro.in_month_end_window(date(2026, 5, 18))
    cfg = types.SimpleNamespace(DATA_DIR=tmp_path)
    assert not ro.updated_this_month(cfg, date(2026, 10, 26))
    (tmp_path / "rule_optimizer_meta.json").write_text(json.dumps({"last_update_month": "2026-10"}), encoding="utf-8")
    assert ro.updated_this_month(cfg, date(2026, 10, 27))
    assert not ro.updated_this_month(cfg, date(2026, 11, 30))
