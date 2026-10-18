import math

from backtest import hidden_feature_ic as H


def test_parse_series_drops_nan_and_broken_items():
    s = "2026-01-01:0.1:100;2026-01-08:nan:100;broken;2026-01-15:-0.05:90;2026-01-22:x:1"
    assert H.parse_series(s) == [("2026-01-01", 0.1, 100), ("2026-01-15", -0.05, 90)]
    assert H.parse_series(None) == []


def test_summarize_ic_uses_overlap_lags_and_judges():
    series = [(f"d{i}", 0.05 + 0.03 * math.sin(i / 6), 100) for i in range(40)]
    s20 = H.summarize_ic(series, 20)
    s60 = H.summarize_ic(series, 60)
    assert s20["dates"] == 40 and s20["pos_share"] == 1.0
    assert s60["t_nw"] < s20["t_nw"]          # 重なりが長いほど t は割り引かれる
    assert H.judge_ic(s20) == "採用候補"
    assert H.judge_ic({"mean": 0.05, "t_nw": 1.5}) == "見送り"
    assert H.judge_ic({"mean": -0.05, "t_nw": 3.0}) == "見送り"
    assert H.judge_ic(H.summarize_ic([], 20)) == "見送り"


def test_alert_stat_and_judge():
    row = {"events": "100", "days": "25", "mean_mkt": "0.02", "hit_mkt": "0.6", "med_mkt": "0.015",
           "day_mean_mkt": "0.02", "day_sd_mkt": "0.04", "events_sec": "0"}
    st = H.alert_stat(row, "mkt")
    assert abs(st["t_day"] - 0.02 / (0.04 / 5)) < 1e-9
    assert H.judge_alert(st) == "採用候補"
    neg = {**st, "mean_excess": -0.02, "t_day": -3.0, "hit_rate": 0.3}
    assert H.judge_alert(neg).startswith("逆向き")
    assert H.judge_alert({**st, "events": 5}).startswith("判定不能")
    assert H.alert_stat(row, "sec")["events"] == 0
    assert H.judge_alert(H.alert_stat(row, "sec")).startswith("判定不能")


def test_alerts_report_groups_by_kind_and_key():
    rows = [{"kind": "section_severity", "k": "a|high", "events": 40, "days": 10, "mean_mkt": 0.01, "hit_mkt": 0.4,
             "day_mean_mkt": 0.01, "day_sd_mkt": 0.05, "events_sec": 40, "mean_sec": 0.0, "hit_sec": 0.5,
             "day_mean_sec": 0.0, "day_sd_sec": 0.05}]
    out = H.alerts_report(rows)
    assert out["section_severity"]["a|high"]["mkt"]["judge"] == "見送り"


def test_sql_is_asof_safe_and_read_only():
    f, a = H.funding_sql(), H.alerts_sql()
    assert "public_funding_rank" not in f                    # 最新分しか無い表は使わない
    assert f"INTERVAL {H.FUNDING_LAG_DAYS} DAY) <= a.t" in f  # 知り得た日以降だけ
    assert "c.trade_date > x.d" in a                         # 通知日の翌営業日から
    for sql in (f, a):
        assert "alert_items_v" in sql or "public_funding" in sql
        assert all(w not in sql.upper() for w in ("INSERT ", "DELETE ", "UPDATE ", "DROP ", "MERGE "))
    assert "alert_items`" not in a                           # 必ず _v ビュー
