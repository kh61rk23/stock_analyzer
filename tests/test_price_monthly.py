"""price_monthly: 日足を月足に畳む処理（SQL の形・境目・読み手の期間との整合・失敗時に日足を消さない）。BQ には触らない。"""
import re
from datetime import date
from types import SimpleNamespace

import pytest

import bq_compact as bc
import config as cfg_mod
import price_universe as pu

CFG = SimpleNamespace(BQ_COMPACT_MAX_BYTES=1024 ** 3, PRICE_DAILY_KEEP_MONTHS=24, PRICE_MONTHLY_MIN_KEEP_MONTHS=22,
                      PRICE_MONTHLY_MAX_MONTHS_PER_RUN=3, PRICE_MONTHLY_SEAM_MAX_TICKERS=50)


# ── 境目 ──
def test_boundary_is_calendar_month_start_keep_months_back():
    assert bc.fold_boundary(date(2026, 11, 7), 24) == date(2024, 11, 1)
    assert bc.fold_boundary(date(2026, 1, 31), 24) == date(2024, 1, 1)
    assert bc.fold_boundary(date(2026, 3, 1), 26) == date(2024, 1, 1)
    assert bc.add_months(date(2026, 1, 15), -1) == date(2025, 12, 1)


def test_keep_period_covers_every_reader_and_split_refetch():
    longest_reader_days = 440      # 米国スクリーナーの価格日数（SCREEN_UNIVERSE_PRICE_DAYS の既定）が日足の読み手の最長
    keep, floor = cfg_mod.PRICE_DAILY_KEEP_MONTHS, cfg_mod.PRICE_MONTHLY_MIN_KEEP_MONTHS
    assert keep >= floor
    assert keep * 30 > longest_reader_days + 120           # 読み手より4か月以上長い
    # 分割の取り直しは暦で SPLIT_REFETCH_DAYS*1.5+7 日ぶんの日足を書き直す。下限はそれより長い（畳んだ月に届かない）
    assert floor * 30 > int(pu.SPLIT_REFETCH_DAYS * 1.5) + 7
    # 今日が月初でも、境目より後の日足は floor か月ぶん（最短でも floor*28 日）残る
    assert (date(2026, 11, 1) - bc.fold_boundary(date(2026, 11, 1), floor)).days >= floor * 28


def test_bq_readers_stay_inside_daily_window():
    files = ["paper/position_var.py", "bi/marts/m_screener.sql", "bi/marts/m_screener_us.sql", "bi/marts/m_sector_perf.sql",
             "bi/marts/m_market_breadth.sql", "bi/marts/m_price_monthly.sql"]
    for f in files:
        text = open(f, encoding="utf-8").read()
        days = [int(x) for x in re.findall(r"INTERVAL (\d+) DAY", text)] + [int(x) for x in re.findall(r"LOOKBACK_DAYS = (\d+)", text)]
        assert days and max(days) <= 440, f
    # 推奨のその後（pick_outcomes）は選んだ日から約130日前まで＋60営業日先
    assert cfg_mod.PRICE_DAILY_KEEP_MONTHS * 30 > 130 + 10 + 100


# ── SQL の形 ──
def test_insert_builds_ohlcv_from_deduped_daily_of_one_month():
    sql = bc.monthly_insert_sql("p", date(2024, 10, 1))
    assert "INSERT INTO `p.analytics.price_monthly`" in sql
    assert "trade_date >= DATE '2024-10-01' AND trade_date < DATE '2024-11-01'" in sql
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, trade_date ORDER BY ingested_at DESC) = 1" in sql
    assert "ARRAY_AGG(open IGNORE NULLS ORDER BY trade_date ASC LIMIT 1)" in sql          # 始値=月初
    assert "ARRAY_AGG(close IGNORE NULLS ORDER BY trade_date DESC LIMIT 1)" in sql        # 終値=月末
    assert "MAX(high)" in sql and "MIN(low)" in sql and "SUM(volume)" in sql
    assert "GROUP BY 1, 2" in sql
    assert "trade_date < DATE '2025-01-01'" in bc.monthly_insert_sql("p", date(2024, 12, 1))


def test_ddl_partition_cluster_and_function_avoids_double_count():
    t, fn = bc.price_monthly_ddl("p")
    assert "CREATE TABLE IF NOT EXISTS `p.analytics.price_monthly`" in t
    assert "PARTITION BY DATE_TRUNC(month_start, MONTH) CLUSTER BY ticker" in t
    assert "month_start DATE NOT NULL" in t and "ticker STRING NOT NULL" in t and "adj_basis_date DATE" in t
    assert "CREATE OR REPLACE TABLE FUNCTION `p.analytics.price_history_mixed`(start_date DATE, end_date DATE)" in fn
    assert "NOT EXISTS (SELECT 1 FROM mo WHERE mo.ticker = dy.ticker AND mo.month_start = DATE_TRUNC(dy.trade_date, MONTH))" in fn
    assert "PARTITION BY ticker, trade_date ORDER BY ingested_at DESC" in fn
    assert "WHERE trade_date BETWEEN start_date AND end_date" in fn


def test_daily_delete_is_whole_month_only():
    sql = bc.daily_delete_sql("p", date(2024, 10, 1))
    assert sql.startswith("DELETE FROM `p.analytics.price_daily`")
    assert "trade_date >= DATE '2024-10-01' AND trade_date < DATE '2024-11-01'" in sql
    assert "ticker" not in sql and "ingested_at" not in sql      # 部分削除（DML 課金）にしない


def test_pending_sql_reads_metadata_only_and_before_boundary():
    sql = bc.pending_months_sql("p", date(2024, 11, 1))
    assert "INFORMATION_SCHEMA.PARTITIONS" in sql and "HAVING m < DATE '2024-11-01'" in sql


# ── 分割の基準ずれ ──
def _row(t, m_close, d_close, m_vol, d_vol):
    return {"ticker": t, "m_close": m_close, "d_close": d_close, "m_vol": m_vol, "d_vol": d_vol}


def test_seam_split_detected_with_volume_corroboration():
    # 2:1 分割。古い基準の月足は終値が2倍・1日あたり出来高が半分
    assert bc.plan_seam_rescale([_row("7203.T", 2000, 1005, 500, 1000)])["ratios"] == {"7203.T": 2.0}
    # 併合（5:1）。月足側の終値が 1/5・出来高が5倍
    assert bc.plan_seam_rescale([_row("A", 200, 1000, 5000, 1000)])["ratios"] == {"A": pytest.approx(0.2)}


def test_seam_ignores_real_crash_and_normal_moves():
    # 終値が半分になったが出来高は同程度（分割なら出来高も動くはず）→ 暴落とみて直さない
    p = bc.plan_seam_rescale([_row("X", 2000, 1000, 1000, 1000)])
    assert p["ratios"] == {} and p["skipped"] == [("X", "volume")]
    assert bc.plan_seam_rescale([_row("Y", 1100, 1000, 1000, 1000)])["ratios"] == {}      # 10%の動き
    assert bc.plan_seam_rescale([_row("Z", 2000, 1000, None, 1000)])["skipped"] == [("Z", "no_volume")]


def test_seam_refuses_when_too_many_and_rejects_bad_ticker():
    rows = [_row(f"{1000 + i}.T", 2000, 1000, 500, 1000) for i in range(6)]
    p = bc.plan_seam_rescale(rows, max_tickers=5)
    assert p["refused"] and p["ratios"] == {}
    assert bc.plan_seam_rescale([_row("A'; DROP", 2000, 1000, 500, 1000)])["skipped"][0][1] == "ticker"
    with pytest.raises(ValueError):
        bc.monthly_rescale_sql("p", {"A'; DROP": 2.0})


def test_rescale_sql_divides_prices_and_multiplies_volume():
    sql = bc.monthly_rescale_sql("p", {"7203.T": 2.0})
    assert "UPDATE `p.analytics.price_monthly` AS m" in sql
    assert "open = m.open / r.ratio" in sql and "close = m.close / r.ratio" in sql and "volume = m.volume * r.ratio" in sql
    assert "STRUCT('7203.T' AS ticker, 2.0 AS ratio)" in sql and "WHERE m.ticker = r.ticker" in sql


# ── 実行の順序と失敗時 ──
class _Job:
    def __init__(self, rows, dry, big=False):
        self._rows = rows
        self.total_bytes_processed = 2 * 1024 ** 3 if big else (5_000_000 if dry else 0)
        self.total_bytes_billed = 5_000_000

    def result(self):
        return self._rows


class _Client:
    """流した SQL を記録する。verify・big・seam・last で確かめの結果・見積もり超過・境目の跳びを差し替える。"""
    project = "p"

    def __init__(self, months=((date(2024, 8, 1), 100), (date(2024, 9, 1), 100)), verify=None, big=None, seam=None, last=None):
        self.months, self.log, self.big = months, [], big
        self.verify = verify or {"monthly_n": 7, "daily_n": 7, "late_n": 0}
        self.seam, self.last = seam or [], last

    def get_table(self, ref):
        return object()

    def query(self, sql, job_config=None, location=None):
        dry = bool(getattr(job_config, "dry_run", False))
        if dry and self.big and self.big in sql:
            return _Job([], True, big=True)
        if not dry:
            self.log.append(sql)
        if "INFORMATION_SCHEMA.PARTITIONS" in sql:
            return _Job([{"m": m, "n": n} for m, n in self.months], dry)
        if sql.startswith("SELECT MAX(month_start)"):
            return _Job([{"l": self.last}] if self.last else [], dry)
        if "AS m_close" in sql:
            return _Job(self.seam, dry)
        if "AS monthly_n" in sql:
            return _Job([self.verify], dry)
        return _Job([], dry)


def _kinds(log):
    out = []
    for s in log:
        for k, key in (("create_t", "CREATE TABLE"), ("create_f", "CREATE OR REPLACE TABLE FUNCTION"),
                       ("mdel", "DELETE FROM `p.analytics.price_monthly`"), ("ins", "INSERT INTO"), ("verify", "AS monthly_n"),
                       ("ddel", "DELETE FROM `p.analytics.price_daily`"), ("rescale", "UPDATE `p.analytics.price_monthly`")):
            if key in s:
                out.append(k)
                break
    return out


def test_run_folds_oldest_first_and_deletes_daily_only_after_verify():
    c = _Client()
    r = bc.run_price_monthly(c, CFG, today=date(2026, 11, 7))
    assert r["folded"] == ["2024-08-01", "2024-09-01"] and r["boundary"] == "2024-11-01"
    assert _kinds(c.log) == ["create_t", "create_f", "mdel", "ins", "verify", "ddel", "mdel", "ins", "verify", "ddel"]


def test_run_limits_months_per_run():
    c = _Client(months=tuple((bc.add_months(date(2024, 1, 1), i), 10) for i in range(6)))
    r = bc.run_price_monthly(c, SimpleNamespace(**{**CFG.__dict__, "PRICE_MONTHLY_MAX_MONTHS_PER_RUN": 2}), today=date(2026, 11, 7))
    assert r["folded"] == ["2024-01-01", "2024-02-01"] and len(r["pending"]) == 6


@pytest.mark.parametrize("verify", [{"monthly_n": 6, "daily_n": 7, "late_n": 0}, {"monthly_n": 7, "daily_n": 7, "late_n": 3},
                                    {"monthly_n": 0, "daily_n": 0, "late_n": 0}])
def test_failed_verify_never_deletes_daily(verify):
    c = _Client(verify=verify)
    with pytest.raises(bc.PriceMonthlyError):
        bc.run_price_monthly(c, CFG, today=date(2026, 11, 7))
    assert "ddel" not in _kinds(c.log)


def test_estimate_over_limit_stops_before_any_delete():
    c = _Client(big="INSERT INTO")
    with pytest.raises(bc.PriceMonthlyError):
        bc.run_price_monthly(c, CFG, today=date(2026, 11, 7))
    assert "ddel" not in _kinds(c.log) and "ins" not in _kinds(c.log)


def test_nothing_pending_does_no_writes_besides_ddl():
    c = _Client(months=())
    r = bc.run_price_monthly(c, CFG, today=date(2026, 11, 7))
    assert r["folded"] == [] and set(_kinds(c.log)) <= {"create_t", "create_f"}


def test_dry_run_writes_nothing():
    c = _Client()
    r = bc.run_price_monthly(c, CFG, dry_run=True, today=date(2026, 11, 7))
    assert len(c.log) == 1 and "INFORMATION_SCHEMA" in c.log[0] and r["folded"] == [] and len(r["pending"]) == 2


def test_keep_below_floor_is_refused():
    with pytest.raises(bc.PriceMonthlyError):
        bc.run_price_monthly(_Client(), CFG, today=date(2026, 11, 7), keep_months=12)


def test_seam_rescale_runs_before_folding():
    c = _Client(seam=[_row("7203.T", 2000, 1000, 500, 1000)], last=date(2024, 7, 1))
    r = bc.run_price_monthly(c, CFG, today=date(2026, 11, 7))
    kinds = _kinds(c.log)
    assert kinds.index("rescale") < kinds.index("ins") and r["seam"]["fixed"] == [("7203.T", 2.0)]
