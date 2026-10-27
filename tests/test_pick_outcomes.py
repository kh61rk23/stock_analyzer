"""pick_outcomes: 成績表の集計・月の扱い・MERGE 文の形と、月1回だけ出すこと。"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pick_outcomes as po


def _r(source="jp_screener", rank=1, new=True, r5=0.02, e5=0.01, r20=None, e20=None):
    return {"source": source, "ticker": "X", "name": "X社", "rank": rank, "is_new": new,
            "ret_5d": r5, "ex_5d": e5, "ret_20d": r20, "ex_20d": e20, "ret_60d": None, "ex_60d": None}


def test_band_of():
    assert po.band_of(1) == "上位10" and po.band_of(10) == "上位10"
    assert po.band_of(11) == "11〜25位" and po.band_of(26) == "26位〜" and po.band_of(500) == "26位〜"


def test_summarize_counts_bands_new_and_hits():
    rows = [_r(rank=1, e5=0.02), _r(rank=2, new=False, e5=-0.01), _r(rank=30, e5=0.03, r20=0.1, e20=0.05)]
    s = po.summarize(rows)
    top_all = s[("jp_screener", "上位10", False)][5]
    assert top_all["n"] == 2 and abs(top_all["ex"] - 0.005) < 1e-9 and top_all["hit"] == 0.5
    assert s[("jp_screener", "上位10", True)][5]["n"] == 1             # 新しく選ばれた日だけ
    assert s[("jp_screener", "全体", False)][5]["n"] == 3
    assert s[("jp_screener", "上位10", False)][20]["n"] == 0            # 20日後はまだ
    assert s[("jp_screener", "26位〜", False)][20]["avg"] == 0.1


def test_report_lines_mentions_sources_and_pending():
    s = po.summarize([_r(), _r(source="us_screener", rank=12)])
    text = "\n".join(po.report_lines("2026-10", s, [], []))
    assert "日本株（二次評価の通過）" in text and "米国株（スクリーナーの通過）" in text
    assert "5日 +2.0%（超過 +1.0%・勝ち 100%・1件）" in text
    assert "全上場" not in text                                        # 記録の無い選び方は出さない


def test_previous_month():
    assert po.previous_month(date(2026, 11, 2)) == (date(2026, 10, 1), date(2026, 11, 1), "2026-10")
    assert po.previous_month(date(2027, 1, 5))[2] == "2026-12"


def test_merge_sql_has_every_column_and_keys():
    sql = po.build_merge_sql("p")
    for name, _t, _m in po.SCHEMA:
        assert f"S.{name}" in sql
    assert "ON T.pick_date = S.pick_date AND T.source = S.source AND T.ticker = S.ticker" in sql
    for h in po.HORIZONS:
        assert f"e.ei + {h}" in sql and f"e.bi + {h}" in sql
    assert "price_daily_latest" in sql and "stock_snapshot" in sql and "screener_universe_daily" in sql


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return [_Row(r) for r in self.rows]


class _Row(dict):
    pass


class _Client:
    project = "p"

    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    def query(self, *a, **k):
        self.calls += 1
        return _Rows(self.rows)


def test_monthly_report_once_per_month(tmp_path, monkeypatch):
    client = _Client([_r(), _r(rank=40, new=False)])
    monkeypatch.setattr(po, "_client", lambda cfg: client)

    class Cfg:
        DATA_DIR = str(tmp_path)
        ENABLE_PICK_OUTCOMES = True

    a = po.monthly_report(Cfg, date(2026, 11, 2))
    assert a["trigger"] == "pick_outcomes_monthly" and "2026-10" in a["title"]
    assert json.loads((tmp_path / "pick_outcomes_state.json").read_text())["last_report_month"] == "2026-10"
    assert po.monthly_report(Cfg, date(2026, 11, 3)) is None and client.calls == 1
    assert po.monthly_report(Cfg, date(2026, 12, 1))["title"].endswith("（2026-11 の成績表）")


def test_monthly_report_off():
    class Off:
        ENABLE_PICK_OUTCOMES = False
    assert po.monthly_report(Off, date(2026, 11, 2)) is None
    assert po.update(Off)["skipped"]


def test_price_universe_always_fetches_benchmarks():
    import price_universe as pu
    assert "1306.T" in pu.BENCHMARKS["jp"] and "SPY" in pu.BENCHMARKS["us"]
