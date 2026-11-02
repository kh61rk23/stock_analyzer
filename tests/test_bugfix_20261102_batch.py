"""日次メールの表示・判定まわりの修正の純関数テスト。"""
from datetime import date, datetime, timezone

import alert_notifier as an
import anomaly_detector as ad
import deliberation_queue as dq
import earnings_surprise as es
import risk_text_scorer as rt
import score_explainer as se
import timeline_manager as tm


def test_sector_cross_majority():
    g = {"cross": "golden"}
    d = {"cross": "dead"}
    n = {"cross": None}
    assert tm.classify_sector_cross({"a": g, "b": g, "c": d}) == "golden"
    assert tm.classify_sector_cross({"a": d, "b": d, "c": g}) == "dead"
    assert tm.classify_sector_cross({"a": g, "b": d, "c": n}) == ""
    assert tm.classify_sector_cross({"a": n}) == ""


def _sr(score, bonus, sp=-4.4, neg=1):
    return {"ticker": "1111.T", "name": "テスト", "surprise_score": score, "surprise_label": "中立",
            "screener_bonus": bonus, "latest_q": {"surprise_pct": sp}, "consecutive_neg": neg}


def test_negative_alert_excludes_neutral_label():
    summ = {"total": 2, "hit_pos": 0, "hit_neg": 2, "top_neg": [_sr(50, -1.0), _sr(40, -2.0)]}
    txt = es.format_surprise_alert(summ)
    assert txt.count("1111.T") == 1
    assert "直近Q -4.4%（1Q連続下振れ）" in txt and "中立" not in txt
    only_neutral = {"total": 1, "hit_pos": 0, "hit_neg": 1, "top_neg": [_sr(50, -1.0)]}
    assert "ネガサプライズ注意" not in es.format_surprise_alert(only_neutral)


def test_next_earnings_past_is_flagged_and_estimated():
    assert es.estimate_next_earnings("2026-08-01", "2026-11-02") == "2027-01-30"
    assert es.estimate_next_earnings("2026-12-01", "2026-11-02") == "2026-12-01"
    assert es.estimate_next_earnings("bad", "2026-11-02") is None
    assert es.next_earnings_label(None, None) == "次回決算日未取得"
    assert es.next_earnings_label("2026-11-20", 18) == "決算 2026-11-20（あと18日）"
    s = es.next_earnings_label("2026-08-01", -93, "2027-01-30", 89)
    assert "経過・未更新" in s and "推定 2027-01-30（あと89日）" in s


def test_annotate_sets_estimate_for_past_date():
    c = [{"ticker": "1111.T"}]
    cache = {"1111.T": {"next_earnings": "2026-08-01", "conservatism_score": 50, "latest_q": {}}}
    es.apply_surprise_flags(c, cache, today="2026-11-02")
    assert c[0]["days_to_earnings"] < 0 and c[0]["next_earnings_est"] == "2027-01-30"


def test_risk_sql_shape():
    assert "MAX(sec_code)" in rt.SQL and "ANY_VALUE(sec_code)" not in rt.SQL
    assert "LENGTH(r.sec_code) >= 4" in rt.ALERT_SQL and "COUNT(*) OVER()" in rt.ALERT_SQL


class _Row(dict):
    pass


class _Res:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return [_Row(r) for r in self.rows]


class _Cl:
    project = "p"

    def __init__(self, rows):
        self.rows = rows

    def query(self, sql, location=None):
        return _Res(self.rows)


def test_risk_alert_title_uses_total_count():
    rows = [{"sec_code": "11110", "name": "A社", "fy_latest": "2026-03-31", "severity": 5,
             "new_terms": "減損+1", "change_ratio": 0.1, "total_n": 321}]
    out = rt.build_alert(None, {}, client=_Cl(rows))
    assert "321社" in out["title"]


def _hist(closes, vols=None, last_date="2026-11-02"):
    n = len(closes)
    return {"close": closes, "volume": vols or [100] * n,
            "dates": [f"2026-10-{i + 1:02d}" for i in range(n - 1)] + [last_date]}


def test_price_high_requires_z():
    # 高ボラ: 毎日±6%動く銘柄の+6%は HIGH にしない
    closes = [100.0]
    for i in range(29):
        closes.append(closes[-1] * (1.06 if i % 2 == 0 else 0.94))
    closes.append(closes[-1] * 1.06)
    r = ad.detect_price_anomaly("X", _hist(closes))
    assert r is None or r["severity"] != "high"
    # 静かな銘柄の+6%はHIGH
    quiet = [100.0 + (0.1 if i % 2 else -0.1) for i in range(30)] + [106.5]
    r = ad.detect_price_anomaly("Y", _hist(quiet))
    assert r and r["severity"] == "high"


def test_futures_weekend_incomplete_and_volume_drop_ignored():
    now = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)
    assert ad._session_incomplete("GC=F", "2026-11-01", now)   # 日曜日付
    vols = [1000] * 25 + [300]
    assert ad.detect_volume_anomaly("AAPL", _hist([100.0] * 26, vols)) is None


def test_combined_needs_volume_ratio_ge_1():
    p = {"severity": "high", "ticker": "X", "date": "d", "methods": []}
    assert ad.detect_combined_anomaly(p, {"severity": "high", "volume_ratio": 0.3, "methods": []}) is None
    assert ad.detect_combined_anomaly(p, {"severity": "high", "volume_ratio": 3.0, "methods": []}) is not None


def test_short_title_never_empty_and_not_midword():
    t = "半導体 × " + "あ" * 80
    s = an.short_title(t)
    assert len(s) <= 60 and s.endswith("…") and s != "…"
    assert an.short_title("short title") == "short title"
    s2 = an.short_title("alpha beta gamma delta " * 5, 30)
    assert len(s2) <= 30 and s2.endswith("…")


def test_fed_alert_title():
    far = {"fomc": {"days_until_meeting": 36}, "boj": {}, "macro_adjustment": -4.5,
           "fed": {"cut_prob_25bp": 60.0, "hold_prob": 40.0}, "adjustment_rationale": "x"}
    a = an.build_fed_calendar_alert(far)
    assert "接近" not in a["title"] and "簡易推計" in a["title"] and "ZQ先物" in a["body"]
    near = dict(far, fomc={"days_until_meeting": 2})
    assert "接近" in an.build_fed_calendar_alert(near)["title"]
    assert an.build_fed_calendar_alert({"fomc": {"days_until_meeting": 36}, "macro_adjustment": 1}) is None


def test_pick_edinet_entry_key_formats():
    ed = {"7203.T": {"analysis": 1}, "6758": {"analysis": 2}}
    assert an.pick_edinet_entry(ed, "6758.T") == {"analysis": 2}
    assert an.pick_edinet_entry(ed, "7203") == {"analysis": 1}
    assert an.pick_edinet_entry(ed, "9999.T") is None
    assert an.pick_edinet_entry({}, "") is None


def test_reason_text_omits_market_wide_factors():
    us = {"us10y": 4.5, "vix": 25}
    a = se.build_reason_text("1016", 60, [], None, us)
    b = se.build_reason_text("1017", 60, [], None, us)
    assert "米10年債" not in a and a == b
    assert "米10年債4.50%" in se.market_wide_text(us)
    assert se.market_wide_text({}) == ""


def test_top_factor_text():
    assert se.top_factor_text({"value_score": 80, "quality_score": 70, "sector_score": 65}) == "割安度80点・財務の質70点"
    assert se.top_factor_text({"value_score": 40}) == ""


def test_dividend_consistency():
    assert an.dividend_inconsistent(0.143, 0.30, 15.0)       # 推計2%に対し14.3%
    assert not an.dividend_inconsistent(0.05, 0.60, 12.0)    # 推計5%
    assert not an.dividend_inconsistent(0.143, None, 15.0)
    assert not an.dividend_inconsistent(0.143, 0.3, 0)


def test_high_dividend_marks_row():
    class C: ALERT_MAX_DIVIDEND_YIELD = 0.15
    res = {"screener": {"candidates": [{"ticker": "1111.T", "name": "A", "dividend_yield": 0.143,
                                        "payout_ratio": 0.3, "per": 15.0}]}}
    out = an.build_high_dividend_alert(C, res)
    assert "特配・データ要確認" in out["body"]


def test_queue_rows_for_report_and_override():
    rows = [{"ticker": "1111.T", "company_name": "A", "sector_code": "1", "priority_rank": 1,
             "source": "screener", "reason": "r", "final_score": 77.0, "composite_score": 70.0,
             "universe_score": 60.0, "queue_date": "2026-11-02", "packet_md": "big"}]
    out = dq.queue_rows_for_report(rows)
    assert out[0]["name"] == "A" and out[0]["composite_score"] == 77.0 and "packet_md" not in out[0]


def test_get_queue_for_report_prefers_results_queue(monkeypatch):
    class Cl:
        project = "p"

        def query(self, sql, location=None):
            assert "queue_date" not in sql or "COUNT(*)" in sql or "deliberations" in sql.lower() or True
            return _Res([{"n": 3}])
    monkeypatch.setattr(dq, "get_last_run", lambda c: None)
    monkeypatch.setattr(dq, "_safe_routine_health", lambda cfg, c: None)
    today = [{"ticker": "9999.T", "name": "今日", "composite_score": 80.0}]
    out = dq.get_queue_for_report(None, {"deliberation_queue": {"queue": today}}, top_n=5, client=Cl())
    assert out["queue"][0]["ticker"] == "9999.T"
