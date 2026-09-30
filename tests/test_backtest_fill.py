# 2026-09-29: バックテスト・自己評価が毎週「レコード不足(0件)」だった件
#   current_price は 9/21 以降しか保存されず、28日保有の評価が 10/19 まで出なかった。
#   過去分はその日の終値で補い、価格は銘柄ごとに1回だけ取る。
from datetime import date, timedelta

import backtester as bt
import self_evaluator as se


def _hist(days_ago: int, price=None, pass_filters=None):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    c = {"ticker": "7203.T", "composite_score": 70, "sector_code": "1017"}
    if price is not None:
        c["current_price"] = price
    if pass_filters is not None:
        c["pass_filters"] = pass_filters
    return d, [c]


def test_records_without_price_are_kept():
    h = dict([_hist(40), _hist(41, price=2500.0), _hist(42, pass_filters=False)])
    recs = bt.extract_backtest_records(h)
    assert len(recs) == 2
    assert sorted(r["entry_price"] or 0 for r in recs) == [0, 2500.0]


def test_self_evaluator_accepts_old_rows_without_flag():
    h = dict([_hist(40), _hist(41, pass_filters=False)])
    assert len(se.build_evaluation_batch(h)) == 1


def test_fill_prices_uses_close_on_or_after_date():
    recs = [{"ticker": "A", "entry_date": "2026-08-01", "exit_date": "2026-08-29",
             "entry_price": None, "composite_score": 70.0, "sector_code": "1016",
             "cluster_phase_bonus": 0.0}]
    closes = {"A": {"2026-08-03": 100.0, "2026-08-31": 110.0}}   # 8/1・8/29 は休日
    bench = {"2026-08-03": 40000.0, "2026-08-31": 41000.0}
    bt.fill_prices(recs, closes, bench)
    r = recs[0]
    assert (r["entry_price"], r["exit_price"], r["benchmark_return"]) == (100.0, 110.0, 2.5)
    assert r["entry_price_source"] == "close"
    res = bt.run_backtest_analysis(recs, {})
    assert res["evaluated_count"] == 1
