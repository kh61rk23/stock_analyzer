# 米国の一次採点の純粋ロジック: 業種対応・F スコア・指標・採点（時価総額で足切りしない）・二次評価への受け渡し
from datetime import date, timedelta

import screener_universe as su
import us_screener_universe as usu

ASOF = date(2026, 11, 1)


class Cfg:
    SCREEN_UNIVERSE_WEIGHTS = {a: 1.0 for a in su.ASPECTS}
    US_SCREEN_UNIVERSE_MIN_TURNOVER = 500_000.0


def _closes(n=260, start=100.0, step=0.1):
    return [start + step * i for i in range(n)]


def _price_row(t, turnover=5e6, **kw):
    cl = _closes()
    d0 = ASOF - timedelta(days=1)
    base = {"ticker": t, "closes": cl, "dates": [d0 - timedelta(days=len(cl) - 1 - i) for i in range(len(cl))],
            "last_date": d0, "max_d": d0, "vol_1d": 1e6, "vol_avg20": 1e6, "turnover_20d": turnover,
            "short_qty": None, "si_change_pct": None, "days_to_cover": None, "geo_risk": None}
    base.update(kw)
    return base


def _fy(rev, oi, ni, ta=1000.0, year=2025):
    return {"period_end": date(year, 12, 31), "revenue": rev, "operating_income": oi, "net_income": ni,
            "total_assets": ta, "total_liabilities": 400.0, "equity": 600.0, "cash": 100.0, "debt_short": 10.0,
            "debt_long": 100.0, "operating_cf": ni * 1.2, "capex": -20.0, "buyback": 0.0, "dividends_paid": 0.0,
            "gross_profit": rev * 0.4, "depreciation": 20.0, "income_tax": 10.0, "pretax_income": oi,
            "interest_expense": 5.0, "shares_diluted": 100.0, "assets_current": 500.0, "liabilities_current": 200.0}


def _fund_row(t, **kw):
    base = {"ticker": t, "cik": 1, "name": t, "exchange": "NYSE", "sic": "3674", "sic_description": "Semis",
            "shares_outstanding": 100.0, "ttm_period_end": None, "bs_period_end": None,
            "fy": [_fy(1000, 200, 150), _fy(900, 150, 100, year=2024)],
            "eps_accel_score_xbrl": None, "n13d": None, "k8_items": None, "themes": None}
    base.update(kw)
    return base


def _ctx():
    return {"sector_scores": {"XLK": 60.0}, "hotness": {}, "leading_only": [], "market": {},
            "accrual_fn": None, "tech_fn": lambda c, w, a, b: {"score": 55.0}, "weekly_fn": lambda c: c}


class TestSicToEtf:
    def test_maps_and_unknown(self):
        assert usu.sic_to_etf("3674") == "XLK"
        assert usu.sic_to_etf("2834") == "XLV"
        assert usu.sic_to_etf("6798") == "XLRE"
        assert usu.sic_to_etf("") is None
        assert usu.sic_to_etf(None) is None


class TestSymbol:
    def test_us_symbol(self):
        assert usu.is_us_symbol("AAPL") and usu.is_us_symbol("BRK-B")
        assert not usu.is_us_symbol("7203.T") and not usu.is_us_symbol("BRK.B")


class TestK8Red:
    def test_counts_distinct_red_items(self):
        assert usu.k8_red_count("1.01,2.06,2.06,4.02,9.01") == 2
        assert usu.k8_red_count(None) == 0


class TestPiotroski:
    def test_needs_two_years(self):
        assert usu.piotroski([_fy(1, 1, 1)]) is None

    def test_improving_company_scores_high(self):
        s = usu.piotroski([_fy(1000, 200, 150), _fy(900, 150, 100, year=2024)])
        assert s is not None and s >= 6


class TestFundamentalFeatures:
    def test_no_marketcap_cutoff_and_values(self):
        p = _fund_row("X")
        f = usu.fundamental_features(p, p["fy"], 10.0, ASOF)
        assert f["market_cap"] == 1000.0
        assert round(f["earnings_yield"], 3) == 0.15
        assert f["sales_growth_1y"] is not None and f["pbr"] is not None

    def test_stale_annual_is_ignored(self):
        old = [_fy(1000, 200, 150, year=2020)]
        f = usu.fundamental_features(_fund_row("X", fy=old), old, 10.0, ASOF)
        assert f["roic"] is None and f["sales_growth_1y"] is None


class TestBuildRecords:
    def test_small_cap_kept_illiquid_and_non_company_excluded(self):
        prices = [_price_row("TINY"), _price_row("ILLQ", turnover=1000.0), _price_row("ETFX")]
        funds = [_fund_row("TINY", shares_outstanding=1_000.0), _fund_row("ILLQ")]
        recs, excl = usu.build_records(prices, funds, Cfg, _ctx(), ASOF)
        assert [r["ticker"] for r in recs] == ["TINY"]
        assert excl == {"low_turnover": 1, "not_company": 1}
        assert recs[0]["market_cap_usd_b"] is not None   # 時価総額は記録するが除外には使わない

    def test_events_only_added_when_present(self):
        prices = [_price_row("A"), _price_row("B")]
        funds = [_fund_row("A"), _fund_row("B", n13d=2, k8_items="2.06", eps_accel_score_xbrl=10.0)]
        recs, _ = usu.build_records(prices, funds, Cfg, _ctx(), ASOF)
        a, b = {r["ticker"]: r for r in recs}["A"], {r["ticker"]: r for r in recs}["B"]
        assert a["scores"]["flow"] == [] and len(b["scores"]["flow"]) == 1
        assert b["k8_red"] == 1 and b["scores"]["quality"][0] < 50 and b["scores"]["growth"] == [100.0]


class TestScoreAndStage2:
    def test_scores_all_aspects_and_rank_columns_cover_flow(self):
        prices = [_price_row(t, short_qty=q, days_to_cover=d) for t, q, d in (("A", 1.0, 1.0), ("B", 90.0, 9.0))]
        funds = [_fund_row("A"), _fund_row("B")]
        recs, _ = usu.build_records(prices, funds, Cfg, _ctx(), ASOF)
        scored = su.score_universe(recs, Cfg.SCREEN_UNIVERSE_WEIGHTS, usu.RANK_COLUMNS)
        by = {r["ticker"]: r for r in scored}
        assert set(by["A"]["aspects"]) == set(su.ASPECTS)
        assert by["A"]["aspects"]["flow"] > by["B"]["aspects"]["flow"]   # 空売り残が少ないほど良い

    def test_stage2_entries_keep_meta_and_overseas(self):
        meta = {"NVDA": {"ticker": "NVDA", "category": "ai_semicon", "business_model": "hw", "description": "d",
                         "in_sp500": True}, "7203.T": {"ticker": "7203.T", "category": "adr"}}
        out = usu.stage2_entries(["NVDA", "ZZZ", "NVDA"], meta, ["7203.T"])
        assert [e["ticker"] for e in out] == ["NVDA", "ZZZ", "7203.T"]
        assert out[0]["category"] == "ai_semicon" and out[1]["category"] == "universe"

    def test_output_rows_use_cik_and_usd(self):
        recs, _ = usu.build_records([_price_row("A")], [_fund_row("A")], Cfg, _ctx(), ASOF)
        scored = su.score_universe(recs, Cfg.SCREEN_UNIVERSE_WEIGHTS, usu.RANK_COLUMNS)
        rows = usu.output_rows(scored, "2026-11-01", {"A"})
        assert rows[0]["cik"] == "1" and "edinet_code" not in rows[0] and "market_cap_b" not in rows[0]
        assert "market_cap_usd_b" in rows[0]
        assert {f.name for f in usu._schema()} >= {"cik", "market_cap_usd_b"}
