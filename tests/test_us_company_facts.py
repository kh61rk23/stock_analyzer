"""us_company_facts: SEC companyfacts から年次財務・企業情報・TTM を作る純関数と、取得キュー・間隔の契約。"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sec_client  # noqa: E402
import us_company_facts as u  # noqa: E402


def e(end, val, start=None, form="10-K", filed=None, fp="FY"):
    d = {"end": end, "val": val, "form": form, "filed": filed or end, "fp": fp}
    if start:
        d["start"] = start
    return d


def facts(**tags):
    """tags: {tag: {unit: [entries]}}"""
    return {"entityName": "ACME CORP", "facts": {"us-gaap": {t: {"units": units} for t, units in tags.items()}}}


# ---- タグの優先順（年度ごと）-------------------------------------------------

def test_tag_priority_is_per_year_not_per_series():
    # Revenues は 2018 年で止まり、以降は RevenueFromContract… だけ（AAPL・MSFT 型）
    f = facts(
        Revenues={"USD": [e("2018-09-29", 265e9, "2017-10-01", filed="2018-11-05")]},
        RevenueFromContractWithCustomerExcludingAssessedTax={"USD": [
            e("2018-09-29", 999.0, "2017-10-01", filed="2018-11-05"),
            e("2025-09-27", 416e9, "2024-09-29", filed="2025-10-31"),
        ]},
    )
    rows = u.build_annual_rows("AAPL", 1, f, "now", date(2026, 10, 10))
    by = {r["period_end"]: r for r in rows}
    assert by["2025-09-27"]["revenue"] == 416e9
    assert by["2025-09-27"]["revenue_tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    # 同じ年に両方あるときは優先順（Revenues）
    assert by["2018-09-29"]["revenue"] == 265e9 and by["2018-09-29"]["revenue_tag"] == "Revenues"


def test_priority_reverse_case_nvda():
    f = facts(
        Revenues={"USD": [e("2026-01-25", 215e9, "2025-01-27")]},
        RevenueFromContractWithCustomerExcludingAssessedTax={"USD": [e("2022-01-30", 26e9, "2021-02-01")]},
    )
    by = {r["period_end"]: r for r in u.build_annual_rows("NVDA", 1, f, "now", date(2026, 10, 10))}
    assert by["2026-01-25"]["revenue"] == 215e9 and by["2022-01-30"]["revenue"] == 26e9


# ---- 年度の選び方 ---------------------------------------------------------

def test_only_full_year_10k_entries_and_latest_filing_wins():
    f = facts(NetIncomeLoss={"USD": [
        e("2025-12-31", 100.0, "2025-01-01", filed="2026-02-01"),
        e("2025-12-31", 90.0, "2025-01-01", filed="2025-11-01"),      # 古い提出（訂正前）
        e("2025-09-30", 70.0, "2025-01-01", form="10-Q", fp="Q3"),    # 10-Q の累計は年次にしない
        e("2025-12-31", 25.0, "2025-10-01", filed="2026-02-01"),      # 3か月（Q4 単独）は除く
    ]})
    rows = u.build_annual_rows("X", 1, f, "now", date(2026, 10, 10))
    assert [(r["period_end"], r["net_income"]) for r in rows] == [("2025-12-31", 100.0)]


def test_52_53_week_year_is_accepted_and_old_years_dropped():
    f = facts(NetIncomeLoss={"USD": [e("2026-01-31", 5.0, "2025-02-02"), e("2005-12-31", 1.0, "2005-01-01")]})
    rows = u.build_annual_rows("X", 1, f, "now", date(2026, 10, 10))
    assert [r["period_end"] for r in rows] == ["2026-01-31"]


# ---- 単位 --------------------------------------------------------------

def test_non_usd_units_are_ignored():
    f = facts(Revenues={"CAD": [e("2025-12-31", 5.0, "2025-01-01")]})
    assert u.build_annual_rows("X", 1, f, "now", date(2026, 10, 10)) == []


def test_eps_uses_usd_per_share_unit():
    f = facts(EarningsPerShareDiluted={"USD/shares": [e("2025-12-31", 6.7, "2025-01-01")]},
              NetIncomeLoss={"USD": [e("2025-12-31", 10.0, "2025-01-01")]})
    assert u.build_annual_rows("X", 1, f, "now", date(2026, 10, 10))[0]["eps_diluted"] == 6.7


# ---- 欠損時 ------------------------------------------------------------

def test_liabilities_derived_from_assets_minus_equity_when_missing():
    f = facts(Assets={"USD": [e("2025-12-31", 100.0)]},
              StockholdersEquity={"USD": [e("2025-12-31", 40.0)]},
              StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest={"USD": [e("2025-12-31", 45.0)]})
    r = u.build_annual_rows("X", 1, f, "now", date(2026, 10, 10))[0]
    assert r["total_liabilities"] == 55.0 and r["equity"] == 40.0 and r["revenue"] is None


def test_equity_falls_back_to_including_nci():
    f = facts(Assets={"USD": [e("2025-12-31", 100.0)]},
              StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest={"USD": [e("2025-12-31", 45.0)]})
    assert u.build_annual_rows("X", 1, f, "now", date(2026, 10, 10))[0]["equity"] == 45.0


def test_debt_sums_parts_when_no_total_and_long_excludes_current():
    f = facts(
        ShortTermBorrowings={"USD": [e("2025-12-31", 3.0)]}, CommercialPaper={"USD": [e("2025-12-31", 2.0)]},
        LongTermDebtCurrent={"USD": [e("2025-12-31", 5.0)]},
        LongTermDebt={"USD": [e("2025-12-31", 50.0)]},
        Assets={"USD": [e("2025-12-31", 1.0)]},
    )
    r = u.build_annual_rows("X", 1, f, "now", date(2026, 10, 10))[0]
    assert r["debt_short"] == 10.0 and r["debt_long"] == 45.0


def test_no_facts_gives_status_and_keeps_subs_info():
    subs = {"name": "TRUST X", "sic": "6221", "sicDescription": "Commodity Contracts Brokers & Dealers"}
    p = u.build_profile("MANA", 9, "Nasdaq", None, subs, [], "now")
    assert p["status"] == "no_facts" and p["name"] == "TRUST X" and p["sic_description"].startswith("Commodity")


def test_profile_without_annual_usd_rows():
    f = facts(Revenues={"CAD": [e("2025-12-31", 5.0, "2025-01-01")]})
    assert u.build_profile("X", 1, "NYSE", f, {"name": "X"}, [], "now")["status"] == "no_annual_usd"


# ---- 発行済株式数 ---------------------------------------------------------

def test_shares_prefers_latest_date_and_falls_back_to_weighted_average():
    f = facts()
    f["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"end": "2026-02-01", "val": 100.0, "filed": "2026-02-10"},
        {"end": "2026-07-20", "val": 120.0, "filed": "2026-07-25"}]}}}
    assert u.pick_shares(f)["shares_outstanding"] == 120.0
    g = facts(CommonStockSharesOutstanding={"shares": [e("2026-06-30", 12.2e9, form="10-Q", fp="Q2")]})
    assert u.pick_shares(g) == {"shares_outstanding": 12.2e9, "shares_as_of": "2026-06-30", "shares_source": "us_gaap_balance"}
    h = facts(WeightedAverageNumberOfDilutedSharesOutstanding={"shares": [e("2025-12-31", 7.0, "2025-01-01")]})
    assert u.pick_shares(h)["shares_source"] == "weighted_avg_diluted"
    assert u.pick_shares(facts())["shares_outstanding"] is None


# ---- TTM --------------------------------------------------------------

def test_ttm_is_fy_plus_ytd_minus_prior_ytd_and_uses_newest_tag():
    f = facts(
        Revenues={"USD": [e("2018-12-31", 1.0, "2018-01-01")]},   # 古いタグ
        RevenueFromContractWithCustomerExcludingAssessedTax={"USD": [
            e("2025-12-31", 400.0, "2025-01-01"),
            e("2026-06-30", 230.0, "2026-01-01", form="10-Q", fp="Q2"),
            e("2025-06-30", 190.0, "2025-01-01", form="10-Q", fp="Q2"),
        ]},
    )
    t = u.compute_ttm(f)
    assert t["ttm_period_end"] == "2026-06-30" and t["ttm_revenue"] == 440.0


def test_ttm_equals_fy_when_no_later_10q_and_none_when_prior_missing():
    f = facts(NetIncomeLoss={"USD": [e("2025-12-31", 40.0, "2025-01-01")]})
    assert u.compute_ttm(f)["ttm_net_income"] == 40.0
    g = facts(NetIncomeLoss={"USD": [e("2025-12-31", 40.0, "2025-01-01"),
                                     e("2026-03-31", 12.0, "2026-01-01", form="10-Q", fp="Q1")]})
    assert u.compute_ttm(g)["ttm_period_end"] is None   # 前年同期が無ければ組まない


# ---- daily-index・キュー・書込 -----------------------------------------------

IDX = """Description: x
Last Data Received: Oct 8, 2026

CIK|Company Name|Form Type|Date Filed|File Name
--------------------------------------------------------------------------------
320193|APPLE INC|10-Q|20261008|edgar/data/320193/a.txt
320193|APPLE INC|8-K|20261008|edgar/data/320193/b.txt
789019|MICROSOFT CORP|10-K|20261008|edgar/data/789019/c.txt
5|SOMEONE|424B2|20261008|edgar/data/5/d.txt
"""


def test_parse_daily_index_only_periodic_forms():
    assert u.parse_daily_index(IDX) == {320193, 789019}


def test_index_days_and_pointer_rules():
    today = date(2026, 10, 12)  # 月曜
    assert u.index_days_to_fetch("2026-10-08", today) == [date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 11)]
    # 金曜(10/9)が未公開(missing)なら、直近3日以内なので進めず翌回に再試行する
    assert u.advance_pointer({"2026-10-09": "missing", "2026-10-10": "missing", "2026-10-11": "missing"}, today) is None
    # 週末の欠落は進める。古い平日の欠落（祝日）も進める
    assert u.advance_pointer({"2026-10-05": "missing", "2026-10-10": "missing"}, today) == "2026-10-10"
    assert u.advance_pointer({"2026-10-08": "ok", "2026-10-09": "ok", "2026-10-10": "missing"}, today) == "2026-10-10"


def _uni(*pairs):
    return {t: {"cik": c, "name": t, "exchange": "NYSE"} for t, c in pairs}


def test_select_queue_order_and_caps():
    today = date(2026, 10, 10)
    uni = _uni(("AAA", 1), ("BBB", 2), ("CCC", 3), ("DDD", 4), ("DDD2", 4))
    prof = {
        "AAA": {"fetched_at": "2026-10-09T00:00:00+00:00", "status": "ok", "facts_loaded_at": "x"},
        "BBB": {"fetched_at": "2026-01-01T00:00:00+00:00", "status": "ok", "facts_loaded_at": "x"},      # 古い
        "DDD": {"fetched_at": "2026-10-01T00:00:00+00:00", "status": "ok", "facts_loaded_at": "x"},
    }
    q = u.select_queue(uni, prof, {1}, today, "update")
    assert [(x["cik"], x["reason"]) for x in q] == [(1, "new_filing"), (3, "never_fetched"), (4, "never_fetched"), (2, "stale_rotation")]
    assert next(x for x in q if x["cik"] == 4)["tickers"] == ["DDD", "DDD2"]   # 同じ CIK は1回の取得で全 ticker
    b = u.select_queue(uni, prof, set(), today, "backfill")
    assert [x["cik"] for x in b] == [3, 4]                                      # backfill は未取得だけ


def test_no_facts_company_is_not_retried_every_day():
    today = date(2026, 10, 10)
    uni = _uni(("TRU", 7))
    prof = {"TRU": {"fetched_at": "2026-01-01T00:00:00+00:00", "status": "no_facts"}}
    assert [x["cik"] for x in u.select_queue(uni, prof, set(), today, "update")] == [7]   # 30日経てば再確認
    prof["TRU"]["fetched_at"] = "2026-10-05T00:00:00+00:00"
    assert u.select_queue(uni, prof, set(), today, "update") == []


def test_merge_replaces_whole_ticker_and_keeps_others():
    op = [{"ticker": "A"}, {"ticker": "B"}]
    oa = [{"ticker": "A", "period_end": "2024-12-31"}, {"ticker": "A", "period_end": "2023-12-31"}, {"ticker": "B", "period_end": "2024-12-31"}]
    np_ = [{"ticker": "A"}, {"ticker": "C"}]
    na = [{"ticker": "A", "period_end": "2025-12-31"}, {"ticker": "C", "period_end": "2025-12-31"}]
    p, a = u.merge_rows(op, oa, np_, na)
    assert [x["ticker"] for x in p] == ["A", "B", "C"]
    assert [(x["ticker"], x["period_end"]) for x in a] == [("A", "2025-12-31"), ("B", "2024-12-31"), ("C", "2025-12-31")]


def test_write_guard_blocks_shrinking_overwrite():
    with pytest.raises(RuntimeError):
        u.write_table(None, "t", [], [{"a": 1}], previous_count=100)


# ---- 取得間隔・判定 -------------------------------------------------------

def test_request_interval_stays_under_sec_limit():
    assert u.REQUEST_INTERVAL_SEC >= 0.1            # SEC は秒間10件まで
    old = sec_client.RATE_LIMIT_RPS
    try:
        u.configure_sec_rate()
        assert sec_client.RATE_LIMIT_RPS <= 10
        assert 1.0 / sec_client.RATE_LIMIT_RPS >= 0.1
    finally:
        sec_client.RATE_LIMIT_RPS = old


def test_judge_never_reports_total_failure_as_success():
    base = {"ok": 0, "no_facts": 0, "error": 12, "attempted": 12, "aborted": "連続失敗"}
    assert u.judge(base)[0] == "error"
    assert u.judge({**base, "ok": 30, "error": 2, "attempted": 32})[0] == "warn"
    assert u.judge({"ok": 5, "no_facts": 1, "error": 0, "attempted": 6, "aborted": ""})[0] == "success"
    assert u.judge({"ok": 0, "no_facts": 0, "error": 0, "attempted": 0, "aborted": ""})[0] == "success"  # 対象なし


# ---- 全タグの縦持ち表 ---------------------------------------------------------

def _facts_payload():
    def en(end, val, filed, accn, start=None, form="10-K", frame=None):
        d = {"end": end, "val": val, "filed": filed, "accn": accn, "form": form, "fy": 2025, "fp": "FY"}
        if start:
            d["start"] = start
        if frame:
            d["frame"] = frame
        return d
    return {"facts": {
        "us-gaap": {"Revenues": {"units": {"USD": [
            en("2024-12-31", 100.0, "2025-02-01", "A1", "2024-01-01"),
            en("2024-12-31", 100.0, "2026-02-01", "A2", "2024-01-01", frame="CY2024"),    # 翌年の10-Kの比較列（同値）
            en("2024-12-31", 101.0, "2026-03-01", "A3", "2024-01-01", form="10-K/A"),     # 訂正（値が違う）
            en("2015-12-31", 5.0, "2016-02-01", "A0", "2015-01-01"),                      # 5年より古い
            en("2026-09-30", 7.0, "2026-10-10", "A9", "2026-07-01", form="10-Q"),         # 当日提出は取り込まない
        ]}}},
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [en("2026-01-31", 1e6, "2026-02-01", "A2")]}}},
    }}


def test_fact_rows_dedupe_keep_first_filing_and_drop_old_and_today():
    rows, wm = u.build_fact_rows(1, _facts_payload(), date(2026, 10, 10))
    rev = [r for r in rows if r["tag"] == "Revenues"]
    assert sorted((r["val"], r["accn"]) for r in rev) == [(100.0, "A1"), (101.0, "A3")]
    assert next(r for r in rev if r["accn"] == "A1")["frame"] == "CY2024"          # 重複側の frame を引き継ぐ
    assert {r["taxonomy"] for r in rows} == {"us-gaap", "dei"}                      # 全 namespace
    assert wm == "2026-03-01"                                                       # 当日(10/10)分は含めない


def test_fact_rows_incremental_does_not_reinsert_restated_comparatives():
    rows, _ = u.build_fact_rows(1, _facts_payload(), date(2026, 10, 10), filed_after="2026-02-01")
    # 2026-02-01 提出の再掲（同値）は最初の提出(2025-02-01)に畳まれているので新しい行にならない
    assert [(r["tag"], r["accn"]) for r in rows] == [("Revenues", "A3")]


def test_fact_rows_empty_and_bad_values():
    assert u.build_fact_rows(1, None, date(2026, 10, 10)) == ([], None)
    f = {"facts": {"us-gaap": {"X": {"units": {"USD": [{"end": "2025-12-31", "val": "n/a", "filed": "2026-01-01"}]}}}}}
    assert u.build_fact_rows(1, f, date(2026, 10, 10))[0] == []


def test_facts_job_id_is_stable_and_order_free():
    a = u.facts_job_id([(2, None, "2026-01-01"), (1, "2025-01-01", "2026-01-01")])
    assert a == u.facts_job_id([(1, "2025-01-01", "2026-01-01"), (2, None, "2026-01-01")])
    assert a != u.facts_job_id([(1, "2025-01-01", "2026-01-02"), (2, None, "2026-01-01")])


def test_submissions_summary_takes_everything_flat():
    subs = {"entityType": "operating", "category": "Large accelerated filer", "stateOfIncorporation": "CA",
            "tickers": ["AAPL"], "exchanges": ["Nasdaq"], "addresses": {"business": {"city": "CUPERTINO", "stateOrCountry": "CA"}},
            "formerNames": [{"name": "APPLE COMPUTER INC", "from": "1994-01-26T05:00:00.000Z", "to": "2007-01-04T05:00:00.000Z"}],
            "filings": {"recent": {"form": ["10-Q", "8-K", "10-K", "10-Q"], "filingDate": ["2026-07-31", "2026-08-01", "2025-10-31", "2026-05-01"],
                                   "accessionNumber": ["q", "k8", "k", "q0"]}}}
    s = u.submissions_summary(subs)
    assert s["category"] == "Large accelerated filer" and s["business_city"] == "CUPERTINO"
    assert s["latest_10k_filed"] == "2025-10-31" and s["latest_10k_accn"] == "k" and s["latest_10q_filed"] == "2026-07-31"
    assert s["latest_filing_any"] == "2026-08-01" and '"2007-01-04"' in s["former_names"]
    assert u.submissions_summary(None)["entity_type"] is None


def test_legacy_profile_without_facts_is_requeued():
    uni = _uni(("AAA", 1))
    prof = {"AAA": {"fetched_at": "2026-10-09T00:00:00+00:00", "status": "ok"}}     # 全タグ表の導入前の行
    assert [x["reason"] for x in u.select_queue(uni, prof, set(), date(2026, 10, 10), "backfill")] == ["never_fetched"]


def test_extra_annual_columns_present():
    f = facts(GrossProfit={"USD": [e("2025-12-31", 40.0, "2025-01-01")]},
              AssetsCurrent={"USD": [e("2025-12-31", 70.0)]}, Assets={"USD": [e("2025-12-31", 100.0)]})
    r = u.build_annual_rows("A", 1, f, "now", date(2026, 10, 10))[0]
    assert r["gross_profit"] == 40.0 and r["assets_current"] == 70.0
