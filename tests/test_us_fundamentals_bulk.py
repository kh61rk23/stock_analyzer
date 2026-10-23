"""us_fundamental_rotator: BigQuery の us.fundamentals は1回の実行で1本だけ読む（銘柄ごとに引かない）。"""
import sys
import types
from datetime import date

import us_fundamental_rotator as ufr


def _row(t, d, eps):
    return {"ticker": t, "cik": 1, "period_end": d, "revenue": 10.0, "operating_income": 2.0, "net_income": 1.0,
            "eps_diluted": eps, "fcf": 1.5, "buyback": 0.0, "updated_at": None}


def test_one_query_for_many_tickers(monkeypatch):
    calls = []
    rows = [_row("AAPL", date(2026, 3, 31), 0.4), _row("AAPL", date(2026, 6, 30), 0.5)]

    class _Client:
        def query(self, sql):
            calls.append(sql)
            return types.SimpleNamespace(result=lambda: rows)

    fake = types.ModuleType("edinet_bq_client")
    fake.EDINETBQClient = lambda: types.SimpleNamespace(project="p", client=_Client())
    monkeypatch.setitem(sys.modules, "edinet_bq_client", fake)
    monkeypatch.setattr(ufr, "_BQ_ALL_ROWS", {})
    got = [ufr._lookup_us_fundamentals_bq(t) for t in ("AAPL", "MSFT", "aapl", "NVDA")]
    assert len(calls) == 1                               # 何銘柄引いてもクエリは1本
    assert got[0] is not None and got[1] is None and got[2] is not None and got[3] is None
    assert ufr._BQ_ALL_ROWS["fundamentals"]["AAPL"][0]["period_end"] == date(2026, 6, 30)   # 新しい順
