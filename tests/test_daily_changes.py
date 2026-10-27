"""daily_changes: 前の営業日との通過銘柄の差（新規・脱落・点の動き）と、メールの行。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_changes as dc


def _row(score, **asp):
    r = {"name": "会社", "composite_score": score}
    r.update(asp)
    return r


def test_diff_new_gone_moves():
    today = {"A": _row(80, value_score=30, quality_score=10), "B": _row(60, value_score=10), "C": _row(50)}
    prev = {"B": _row(50, value_score=4), "C": _row(48), "D": _row(70)}
    d = dc.diff(today, prev, {"D": ["PER=40 > 30"]}, move_pt=5)
    assert [t for t, _r, _w in d["new"]] == ["A"] and d["new"][0][2] == "割安"
    assert d["gone"] == [("D", prev["D"], "PER=40 > 30")]
    assert [(t, delta, why) for t, _r, delta, why in d["moves"]] == [("B", 10.0, "割安 +6")]
    assert (d["n_new"], d["n_gone"], d["n_move"]) == (1, 1, 1)


def test_fail_reasons_jp_and_us():
    assert dc.fail_reasons_of({"fail_reasons": ["FCF<0"]}) == ["FCF<0"]
    assert dc.fail_reasons_of({"filter_detail": {"per": "NG(45)", "roe": "OK(12)"}}) == ["per NG(45)"]
    assert dc.fail_reasons_of({}) == []


def test_build_alert_uses_results_and_previous_rows():
    class Cfg:
        ENABLE_DAILY_CHANGES = True
    results = {
        "screener": {"candidates": [
            {"ticker": "7203.T", "name": "トヨタ", "composite_score": 70, "value_score": 20, "pass_filters": True},
            {"ticker": "6758.T", "name": "ソニー", "composite_score": 40, "pass_filters": False,
             "filter_detail": {"per": "NG(60)"}}]},
        "us_screener": {"candidates": []},
    }
    prev = [{"market": "JP", "snapshot_date": "2026-10-26", "ticker": "6758.T", "name": "ソニー", "composite_score": 65},
            {"market": "JP", "snapshot_date": "2026-10-26", "ticker": "7203.T", "name": "トヨタ", "composite_score": 62,
             "value_score": 12}]
    a = dc.build_alert(Cfg, results, prev_rows=prev, today="2026-10-27")
    assert a["trigger"] == "daily_changes"
    body = a["body"]
    assert "前回 2026-10-26 から：新しく通過 0社・外れた 1社・点が大きく動いた 1社" in body
    assert "⤵️ ソニー（6758.T） 前回 65点（per NG(60)）" in body
    assert "⬆️ トヨタ（7203.T） 70点（+8・割安 +8）" in body
    assert "米国株" not in body


def test_build_alert_off_or_empty():
    class Off:
        ENABLE_DAILY_CHANGES = False
    assert dc.build_alert(Off, {}, prev_rows=[]) is None

    class On:
        ENABLE_DAILY_CHANGES = True
    assert dc.build_alert(On, {}, prev_rows=[]) is None
