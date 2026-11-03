# 株式分割・併合による price_daily の基準混在の検知（純粋関数）と取り直し・書き込み抑止
from datetime import date

import pandas as pd

import price_history_writer as w
import price_universe as pu

# 6574.T（10:1 併合）: 表は旧基準 ≈102 と新基準 1020 が混ざる。取れた系列は全日 調整済み
FETCHED = {"6574.T": {"2026-10-22": 1020.0, "2026-10-23": 1020.0, "2026-10-26": 990.0, "2026-10-30": 1281.0},
           "7203.T": {"2026-10-22": 3000.0, "2026-10-23": 3010.0}}
STORED = {"6574.T": {"2026-10-22": 102.0, "2026-10-23": 102.0, "2026-10-26": 99.0},
          "7203.T": {"2026-10-22": 3000.0, "2026-10-23": 3010.0}}


def test_detect_consolidation_flags_only_the_split_ticker():
    out = pu.detect_split_mismatch(FETCHED, STORED)
    assert set(out) == {"6574.T"}
    assert abs(out["6574.T"] - 0.1) < 0.01


def test_detect_forward_split_ratio_above_one():
    out = pu.detect_split_mismatch({"X": {"a": 500.0, "b": 510.0}}, {"X": {"a": 1000.0, "b": 1020.0}})
    assert abs(out["X"] - 2.0) < 0.05


def test_detect_ignores_small_gap_no_overlap_and_odd_ratio():
    assert pu.detect_split_mismatch({"X": {"a": 100.0}}, {"X": {"a": 103.0}}) == {}      # 配当調整程度
    assert pu.detect_split_mismatch({"X": {"a": 100.0}}, {"X": {"b": 1000.0}}) == {}     # 重なる日なし
    assert pu.detect_split_mismatch({"X": {"a": 100.0}}, {"X": {"a": 150.0}}) == {}      # 1.5 は対象外
    assert pu.detect_split_mismatch({"X": {"a": 100.0}}, {}) == {}


def test_detect_tolerances_small_vs_large():
    assert pu.detect_split_mismatch({"X": {"a": 100.0}}, {"X": {"a": 205.0}})            # 2 に +2.5%
    assert not pu.detect_split_mismatch({"X": {"a": 100.0}}, {"X": {"a": 220.0}})        # 2 に +10%（小比は ±6%）
    assert pu.detect_split_mismatch({"X": {"a": 100.0}}, {"X": {"a": 1100.0}})           # 10 に +10%（大比は ±12%）


def test_detect_uses_median_so_one_bad_day_does_not_decide():
    f = {"X": {"a": 100.0, "b": 100.0, "c": 100.0}}
    s = {"X": {"a": 1000.0, "b": 100.0, "c": 100.0}}
    assert pu.detect_split_mismatch(f, s) == {}


def test_find_split_jumps_6574_like_alternating_series():
    jumps = [("6574.T", "2026-10-23", 102.0, 1020.0), ("6574.T", "2026-10-26", 1020.0, 99.0),
             ("7203.T", "2026-10-23", 3000.0, 1800.0),      # 1.67 倍の急落は分割ではない
             ("9999.T", "2026-10-23", 100.0, None)]
    out = pu.find_split_jumps(jumps)
    assert set(out) == {"6574.T"}
    assert [d for d, _ in out["6574.T"]] == ["2026-10-23", "2026-10-26"]
    assert out["6574.T"][0][1] > 9 and out["6574.T"][1][1] < 0.11


def test_split_jump_sql_filters_by_ratio_bounds():
    class C:
        project = "p"
    sql = pu.split_jump_sql(C())
    assert "price_daily_latest" in sql and "@since" in sql and "LAG(close)" in sql


class _FakeYF:
    def __init__(self):
        self.calls = []

    def download(self, tickers, **kw):
        self.calls.append((list(tickers), kw))
        idx = pd.to_datetime(["2026-10-22", "2026-10-23"])
        cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
        return pd.DataFrame(1020.0, index=idx, columns=cols)


def test_refetch_uses_unadjusted_flag_caps_and_appends_all_dates(monkeypatch):
    loaded = []
    monkeypatch.setattr(pu, "load_rows", lambda c, rows: loaded.extend(rows) or len(rows))
    monkeypatch.setattr(pu, "SPLIT_REFETCH_MAX", 1)
    monkeypatch.setattr(pu, "CHUNK_PAUSE_SEC", 0)
    yf = _FakeYF()
    res = pu.refetch_split_tickers(object(), yf, ["B.T", "A.T"], date(2026, 11, 3))
    assert res["fixed"] == ["A.T"] and res["deferred"] == ["B.T"]
    assert yf.calls[0][1]["auto_adjust"] is False and "start" in yf.calls[0][1]
    assert {r["trade_date"] for r in loaded} == {"2026-10-22", "2026-10-23"}


def test_writer_drops_split_mismatched_ticker(monkeypatch):
    rows = [{"ticker": "6574.T", "trade_date": "2026-10-23", "close": 1020.0},
            {"ticker": "7203.T", "trade_date": "2026-10-23", "close": 3010.0}]
    monkeypatch.setattr(pu, "fetch_stored_closes", lambda c, t, s: {
        "6574.T": {"2026-10-23": 102.0}, "7203.T": {"2026-10-23": 3010.0}})
    kept, flagged = w.drop_split_mismatched(rows, client=object())
    assert [r["ticker"] for r in kept] == ["7203.T"] and set(flagged) == {"6574.T"}
