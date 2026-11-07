"""send_gmail: 一時的な切断は間を空けて再送し、認証失敗は再送しない。"""
import smtplib

import alert_notifier as an


def _patch(monkeypatch, outcomes):
    calls, sleeps = [], []

    def once(f, p, t, raw, attempt):
        calls.append(attempt)
        o = outcomes[len(calls) - 1]
        if o:
            raise o

    monkeypatch.setattr(an, "_smtp_send_once", once)
    monkeypatch.setattr(an.time, "sleep", lambda s: sleeps.append(s))
    return calls, sleeps


def test_retries_after_disconnect_then_succeeds(monkeypatch):
    calls, sleeps = _patch(monkeypatch, [smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), None])
    assert an.send_gmail("a@x", "pw", "b@x", "s", "<p>x</p>") == (True, "")
    assert calls == [0, 1] and sleeps == [5]


def test_gives_up_after_all_attempts_and_reports_size(monkeypatch):
    err = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    calls, sleeps = _patch(monkeypatch, [err, err, err])
    ok, msg = an.send_gmail("a@x", "pw", "b@x", "s", "<p>x</p>")
    assert not ok and "3回試行" in msg and "KB" in msg and "@" not in msg
    assert calls == [0, 1, 2] and sleeps == [5, 20]


def test_auth_failure_is_not_retried(monkeypatch):
    calls, sleeps = _patch(monkeypatch, [smtplib.SMTPAuthenticationError(535, b"bad")])
    ok, msg = an.send_gmail("a@x", "pw", "b@x", "s", "b")
    assert not ok and "認証失敗" in msg and calls == [0] and not sleeps


def test_missing_settings_returns_immediately():
    assert an.send_gmail("", "", "", "s", "b")[0] is False
