# 高・最高警戒のアラートが日次メールに載る（LINE 廃止後の送り先）
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import alert_notifier as an  # noqa: E402

RESULTS = {
    "catalyst": {"triple_tickers": [{"ticker": "AAA", "catalyst_count": 3, "min_days": 5, "types": ["x", "y", "z"]}]},
    "institutional_13f": {"new_tier1": [{"institution": "B", "ticker": "BBB", "name": "n",
                                          "value_usd": 2_000_000_000, "shares": 10}], "quarter": "2026Q3"},
    "ignition": {"alerts": [
        {"sector_code": "1", "sector_name": "S1", "convergence_score": 3.2},
        {"sector_code": "2", "sector_name": "S2", "convergence_score": 3.8, "is_critical": True}]},
}


def test_high_alerts_are_for_gmail_and_not_duplicated():
    alerts = an.extract_alerts(types.SimpleNamespace(), RESULTS)
    by = {a["trigger"]: a for a in alerts}
    for t in ("wave3_triple_catalyst", "buffett_new_position",
              "wave2_ignition_convergence", "wave2_ignition_critical"):
        assert by[t]["channel"] == "gmail", t
    assert not [a for a in alerts if a.get("channel") == "unsent"]
    triggers = [a["trigger"] for a in alerts if a.get("trigger")]
    assert len(triggers) == len(set(triggers))
    # 同じ業種が通常と最高警戒の両方に出ない
    assert "S2" not in by["wave2_ignition_convergence"]["body"]


def test_run_notifier_mails_high_alerts(monkeypatch):
    sent = {}
    monkeypatch.setattr(an, "send_gmail", lambda f, p, t, subj, html: sent.update(html=html) or (True, ""))
    cfg = types.SimpleNamespace(ALERT_GMAIL_FROM="a", ALERT_GMAIL_PASSWORD="b", ALERT_GMAIL_TO="c",
                                ENABLE_ALERT_STORE=False)
    out = an.run_alert_notifier(cfg, RESULTS)
    assert out["alerts_sent"] == 1
    assert "Triple Catalyst" in sent["html"] and "Ignition" in sent["html"] and "Buffett" in sent["html"]
