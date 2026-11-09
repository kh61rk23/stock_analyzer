"""メールの不備の総点検（2026-11-09）で直した点の固定。"""
import re
import smtplib
from pathlib import Path

import alert_notifier as an
import config
import monthly_mail as mm
import workflow_notify as wn
from paper.weekly_section import headline

REPO = Path(__file__).resolve().parents[1]


class _FakeSMTP:
    def __init__(self, rcpt_codes, data_code=250):
        self.rc, self.dc, self.sent = rcpt_codes, data_code, None

    def ehlo_or_helo_if_needed(self): pass
    def mail(self, f): return 250, b"ok"
    def rset(self): pass
    def rcpt(self, r): return self.rc.get(r, 250), b"x"

    def data(self, raw):
        self.sent = raw
        return self.dc, b"2.0.0 OK 123 abc - gsmtp"


def test_deliver_returns_refused_and_reply():
    smtp = _FakeSMTP({"b@x": 550})
    res = an._deliver(smtp, "a@x", ["a@x", "b@x"], "body")
    assert list(res["refused"]) == ["b@x"] and "gsmtp" in res["reply"]


def test_deliver_all_refused_raises():
    import pytest
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        an._deliver(_FakeSMTP({"a@x": 550}), "a@x", ["a@x"], "body")


def test_partial_refusal_is_failure_without_address(monkeypatch):
    monkeypatch.setattr(an, "_smtp_send_once", lambda *a: {"refused": {"b@x": (550, b"")}, "reply": ""})
    ok, msg = an.send_gmail("a@x", "pw", "a@x, b@x", "s", "<p>x</p>")
    assert not ok and "@" not in msg and "1件" in msg


def test_success_logs_message_id_and_size(monkeypatch, capsys):
    monkeypatch.setattr(an, "_smtp_send_once", lambda *a: {"refused": {}, "reply": "2.0.0 OK q - gsmtp"})
    assert an.send_gmail("a@gmail.com", "pw", "a@gmail.com", "秘密の件名", "<p>x</p>") == (True, "")
    out = capsys.readouterr().out
    assert re.search(r"<[^>]+@gmail\.com>", out) and "KB" in out and "秘密の件名" not in out


def test_split_addrs():
    assert an._split_addrs("a@x, b@y;c@z") == ["a@x", "b@y", "c@z"]


def test_every_emitted_daily_trigger_is_registered():
    src = "".join((REPO / f).read_text(encoding="utf-8") for f in ("alert_notifier.py",))
    emitted = set(re.findall(r'"trigger":\s*"(\w+)"', src))
    assert not [t for t in emitted if t not in config.ALERT_SECTION_TIER]


def test_every_weekly_section_is_registered():
    src = (REPO / "weekly_report_generator.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'add\("(\w+)"', src))
    assert not [k for k in keys if k not in config.WEEKLY_SECTION_TIER]


def test_daily_mail_shrinks_under_limit_and_keeps_tier1():
    big = [{"trigger": "high_dividend", "title": "配当", "body": "\n".join(f"行{i} " + "あ" * 60 for i in range(2500)),
            "severity": "info"},
           {"trigger": "exit_rule_engine", "title": "出口", "body": "A社を売る\nB社", "severity": "high"}]
    out = an.build_message(big)
    assert len(out.encode()) <= an.MAIL_MAX_BYTES and "A社を売る" in out and "省略" in out


def test_daily_mail_escapes_and_first_look_has_detail():
    out = an.build_message([{"trigger": "exit_rule_engine", "title": "出口 <3件>", "body": "7203.T PER<10 & 損切り", "severity": "high"}])
    assert "&lt;3件&gt;" in out and "PER&lt;10 &amp; 損切り" in out
    assert "今日まず見るもの" in out and out.count("7203.T") >= 2


def test_deliberation_marker_survives_cap():
    body = "\n".join(["x"] * 30 + [f"{an.DELIBERATION_QUEUE_MARKER} 1,2"])
    assert an.DELIBERATION_QUEUE_MARKER in an._cap_body(body, 2, {2: 3})


def test_headline_ignores_idle_strategies():
    acc = {"x": {"equity_history": [{"date": "2026-11-02", "equity": 100}, {"date": "2026-11-09", "equity": 90}], "positions": {"a": 1}},
           "y": {"equity_history": [{"date": "2026-11-02", "equity": 100}, {"date": "2026-11-09", "equity": 100}], "positions": {}}}
    h = headline(acc)
    assert "最良 y" not in h and "プラスの戦略なし" in h and "最悪 x" in h


def test_correlation_label_negative_is_not_valid():
    import performance_tracker as pt
    src = (REPO / "performance_tracker.py").read_text(encoding="utf-8")
    assert "逆相関" in src and "corr <= -0.3" in src


def test_inference_wait_condition_matches_trigger():
    import inference_engine as ie

    class C: vix, recession_score, breadth = 25.6, 4.0, 59.4
    t = ie._risk_off_wait_condition(C)
    assert "VIX" in t and "市場幅" not in t


def test_weekly_helpers():
    import weekly_report_generator as w
    assert w._english_blurb("Foo Inc. makes things and sells internationa...").endswith("（英語原文・冒頭のみ）")
    assert w._english_blurb("日本語の概要") == "日本語の概要"
    assert w._sector_ja("1033") == "サービス業"
    assert w._clip("a" * 80, 70).endswith("…")
    r = w._section_inference({"inference": {"summary_md": "**根拠指標**: recession_score: 4.0 / breadth: 59.4\n`[1028 銀行業]` → `[1029 証券]`"}})
    assert "景気後退スコア 4.0" in r[0] and r[1] == "銀行業 → 証券"


def test_weekly_news_shows_pub_date():
    import weekly_report_generator as w
    out = w._section_news({"news": {"top_articles": [{"title": "t", "source": "S", "pub_date": "2026-11-08 09:00"},
                                                      {"title": "u", "source": "S"}]}})
    assert any("（2026-11-08）" in l for l in out) and any("日付不明" in l for l in out)


def test_monthly_tidy_and_tax_line():
    md = "# t\n## ① a\nx\n## ⑤ S2戦略別成績\n| a |\n## 次月\ny"
    assert "S2戦略別成績" not in mm.tidy_for_mail("strategy_review_202610.md", md)
    assert "S2戦略別成績" in mm.tidy_for_mail("strategy_review_202610.md", md, with_paper=False)
    sc = "⚠️ 全カテゴリ判定不能\n| a | b |\n|---|---|\n| k | - |\n提案あり: 0件 / 判定不能: 1件"
    out = mm.tidy_for_mail("signal_culling_202610.md", sc)
    assert "|" not in out and "提案あり" in out
    paper = "## 保有ポジション\n| a |\n|---|\n" + "\n".join(f"| r{i} |" for i in range(40))
    assert "ほか 25 行" in mm.tidy_for_mail("paper_202610.md", paper)
    assert mm.summarize_report("tax_report_2026.md", "相殺可能額: ¥0 → 節税額の目安: ¥0") == ("", False)


def test_step_json_reads_warning_and_error_lines():
    for mark in ("✅", "⏩", "⚠️", "❌"):
        assert wn.step_json([{"highlights": [f'{mark} edinet_gov_docs: {{"range": ["a", "b"]}}']}], "edinet_gov_docs") == {"range": ["a", "b"]}


def test_humanize_highlight():
    out = wn.humanize_highlight("✅ s: {'rows': 2168, 'companies': 5, 'top': [[\"")
    assert "rows 2168" in out and "以降省略" in out and "{" not in out
    assert wn.humanize_highlight("✅ s: 取り直し 3社") == "✅ s: 取り直し 3社"
