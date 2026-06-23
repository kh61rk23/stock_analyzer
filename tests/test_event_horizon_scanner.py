# event_horizon_scanner.py — イベント事前アラート [EVT-1]
from datetime import datetime, timedelta

from conftest import load_module

evt = load_module("event_horizon_scanner")


class TestNthFriday:
    def test_known_dates(self):
        assert evt.nth_friday(2026, 6, 2) == "2026-06-12"   # 日経SQ
        assert evt.nth_friday(2026, 6, 3) == "2026-06-19"   # 米OPEX

    def test_sq_events_major_flag(self):
        evs = evt.build_sq_events(6)
        majors = [e for e in evs if "メジャー" in e["label"]]
        assert all(e["date"][5:7] in ("03", "06", "09", "12") for e in majors)


class TestFilterHorizon:
    def _ev(self, days, label="X", imp="medium"):
        d = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        return {"date": d, "label": label, "importance": imp, "type": "macro"}

    def test_window_and_sort(self):
        evs = [self._ev(10), self._ev(3, "B"), self._ev(0, "A", "high"),
               self._ev(-1, "過去")]
        out = evt.filter_horizon(evs, 5)
        assert [e["label"] for e in out] == ["A", "B"]     # 窓内のみ・昇順
        assert out[0]["days_until"] == 0

    def test_dedup(self):
        evs = [self._ev(2, "同一"), self._ev(2, "同一")]
        assert len(evt.filter_horizon(evs, 5)) == 1


class TestBuildAlerts:
    def test_high_individual_and_digest(self):
        upcoming = [
            {"date": "2026-06-17", "days_until": 1, "label": "日銀",
             "importance": "high"},
            {"date": "2026-06-19", "days_until": 3, "label": "OPEX",
             "importance": "medium"},
        ]
        alerts = evt.build_alerts(upcoming)
        assert alerts[0]["severity"] == "high"      # 1日前のhighはhigh
        assert "日銀" in alerts[0]["title"]
        assert alerts[1]["trigger"] == "event_horizon_digest"

    def test_empty(self):
        assert evt.build_alerts([]) == []
