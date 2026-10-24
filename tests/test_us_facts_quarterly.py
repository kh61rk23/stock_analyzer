"""us_facts_quarterly: company_facts から四半期 EPS（Q4 は通期－3四半期）と直近12か月 FCF を作る。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import us_facts_quarterly as q

D = date


def _msft_like_eps():
    """6月決算。10-Q は3か月と累計、10-K は通期だけ。"""
    f = []
    for fy, (q1, q2, q3, full) in {2025: (3.30, 3.23, 3.46, 13.64), 2026: (3.72, 5.15, 4.27, 17.95)}.items():
        s = D(fy - 1, 7, 1)
        f += [(s, D(fy - 1, 9, 30), q1), (D(fy - 1, 10, 1), D(fy - 1, 12, 31), q2), (s, D(fy - 1, 12, 31), q1 + q2),
              (D(fy, 1, 1), D(fy, 3, 31), q3), (s, D(fy, 3, 31), q1 + q2 + q3), (s, D(fy, 6, 30), full)]
    return f


def test_quarterly_eps_uses_3_month_values_and_derives_q4():
    h = q.quarterly_eps(_msft_like_eps())
    assert [x["period"] for x in h] == ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
                                        "2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30"]
    assert h[0]["val"] == round(17.95 - (3.72 + 5.15 + 4.27), 4)
    assert h[1]["val"] == 4.27      # 累計（9か月）ではなく3か月の値
    assert h[4]["val"] == round(13.64 - (3.30 + 3.23 + 3.46), 4)


def test_q4_not_derived_without_three_quarters():
    f = [(D(2025, 7, 1), D(2025, 9, 30), 1.0), (D(2024, 7, 1), D(2025, 6, 30), 4.0)]
    assert [x["period"] for x in q.quarterly_eps(f)] == ["2025-09-30"]


def test_ttm_from_fiscal_year_and_ytd():
    # 今期9か月 300・前の通期 350・前年同期9か月 250 → 400
    f = [(D(2024, 7, 1), D(2025, 6, 30), 350.0), (D(2025, 7, 1), D(2026, 3, 31), 300.0),
         (D(2024, 7, 1), D(2025, 3, 31), 250.0), (D(2025, 7, 1), D(2025, 12, 31), 180.0)]
    assert q.ttm(f) == (D(2026, 3, 31), 400.0)


def test_ttm_latest_is_fiscal_year():
    f = [(D(2025, 7, 1), D(2026, 6, 30), 500.0), (D(2025, 7, 1), D(2026, 3, 31), 300.0)]
    assert q.ttm(f) == (D(2026, 6, 30), 500.0)


def test_ttm_none_without_prior():
    assert q.ttm([(D(2025, 7, 1), D(2026, 3, 31), 300.0)]) is None
    assert q.ttm([]) is None


def _rows(tag, facts, cik=1):
    return [{"cik": cik, "tag": tag, "s": s, "e": e, "v": v, "filed": e} for s, e, v in facts]


def test_complement_builds_eps_accel_and_fcf():
    ocf = [(D(2024, 7, 1), D(2025, 6, 30), 350.0), (D(2025, 7, 1), D(2026, 3, 31), 300.0),
           (D(2024, 7, 1), D(2025, 3, 31), 250.0)]
    cap = [(D(2024, 7, 1), D(2025, 6, 30), 50.0), (D(2025, 7, 1), D(2026, 3, 31), 60.0),
           (D(2024, 7, 1), D(2025, 3, 31), 40.0)]
    rows = (_rows("EarningsPerShareDiluted", _msft_like_eps())
            + _rows("NetCashProvidedByUsedInOperatingActivities", ocf)
            + _rows("PaymentsToAcquirePropertyPlantAndEquipment", cap))
    out = q.complement_from_facts(rows)
    assert len(out["eps_history"]) == 8 and out["eps_accel_score_xbrl"] is not None
    # 営業 CF の12か月 400、設備投資の12か月 50+60-40=70 → FCF 330
    assert out["latest_fcf"] == 330.0 and out["fcf_basis"] == "ttm"
    assert out["data_source"] == "sec_company_facts_bq"


def test_complement_no_capex_reported_means_zero():
    ocf = [(D(2025, 1, 1), D(2025, 12, 31), 100.0)]
    out = q.complement_from_facts(_rows("NetCashProvidedByUsedInOperatingActivities", ocf))
    assert out["latest_fcf"] == 100.0 and out["eps_history"] == [] and out["eps_accel_score_xbrl"] is None


def test_complement_capex_missing_for_period_gives_no_fcf():
    ocf = [(D(2025, 1, 1), D(2025, 12, 31), 100.0)]
    cap = [(D(2023, 1, 1), D(2023, 12, 31), 10.0)]
    rows = _rows("NetCashProvidedByUsedInOperatingActivities", ocf) + _rows("PaymentsToAcquirePropertyPlantAndEquipment", cap)
    assert q.complement_from_facts(rows) is None


def test_lookup_reads_once_and_maps_ticker(monkeypatch):
    calls = []

    def fake_load(cfg):
        calls.append(1)
        return {"ticker": {"BRK-B": 7}, "facts": {7: _rows("EarningsPerShareDiluted", _msft_like_eps(), cik=7)}, "done": {}}

    monkeypatch.setattr(q, "_load", fake_load)

    class Cfg:
        US_FACTS_COMPLEMENT_ENABLED = True

    out = q.lookup(Cfg, "brk.b")
    assert out["ticker"] == "BRK-B" and len(out["eps_history"]) == 8
    assert q.lookup(Cfg, "ZZZZ") is None

    class Off:
        US_FACTS_COMPLEMENT_ENABLED = False

    assert q.lookup(Off, "BRK-B") is None
