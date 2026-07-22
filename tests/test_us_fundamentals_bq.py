"""us.fundamentals への変換・主キー・MERGE 契約を検証する。"""

import pandas as pd

from conftest import load_module
from us_bq_schema import DDL


ufr = load_module("us_fundamental_rotator")


def test_quarterly_histories_are_flattened_on_ticker_and_period_end():
    data = {
        "cik": 320193,
        "fetched_at": "2026-07-21T00:00:00+00:00",
        "revenue_history": [
            {"period": "2025-06-30", "val": 94.0},
            {"period": "2025-03-31", "val": 90.0},
        ],
        "eps_history": [{"period": "2025-06-30", "val": 1.5}],
        "fcf_history": [{"period": "2025-06-30", "val": 20.0}],
        "buyback_history": [{"period": "not-a-date", "val": 2.0}],
    }

    rows = ufr._us_fundamentals_to_bq_rows("aapl", data)

    assert [(r["ticker"], r["period_end"]) for r in rows] == [
        ("AAPL", "2025-03-31"), ("AAPL", "2025-06-30")
    ]
    june = rows[1]
    assert june["f_id"] == "AAPL_2025-06-30"
    assert june["cik"] == 320193
    assert june["fiscal_period"] == "2025Q2"
    assert june["revenue"] == 94.0
    assert june["eps_diluted"] == 1.5
    assert june["fcf"] == 20.0
    assert june["source"] == "sec_edgar"


def test_rows_have_unique_primary_keys_and_null_for_missing_metrics():
    rows = ufr._us_fundamentals_to_bq_rows(
        "MSFT",
        {"net_income_history": [{"period": "2025-09-30", "val": "12.5"}]},
    )

    assert len(rows) == len({(r["ticker"], r["period_end"]) for r in rows})
    assert rows[0]["f_id"] == "MSFT_2025-09-30"
    assert rows[0]["net_income"] == 12.5
    assert rows[0]["revenue"] is None


def test_yahoo_fallback_rows_keep_their_source():
    rows = ufr._us_fundamentals_to_bq_rows(
        "BAESY",
        {
            "cik": 895564,
            "data_source": "yahoo_finance_fallback",
            "revenue_history": [{"period": "2025-06-30", "val": 12.5}],
        },
    )

    assert rows[0]["source"] == "yahoo_finance"


def test_yahoo_annual_fallback_is_labelled_as_fiscal_year():
    rows = ufr._us_fundamentals_to_bq_rows(
        "BAESY",
        {
            "cik": 895564,
            "data_source": "yahoo_finance_fallback",
            "statement_period": "annual",
            "revenue_history": [{"period": "2025-12-31", "val": 12.5}],
        },
    )

    assert rows[0]["fiscal_period"] == "2025FY"


def test_yahoo_series_tries_secondary_label_when_primary_is_all_nan():
    statement = pd.DataFrame(
        {pd.Timestamp("2025-06-30"): [float("nan"), 1.25]},
        index=["Diluted EPS", "Basic EPS"],
    )

    result = ufr._yahoo_statement_series(statement, ["Diluted EPS", "Basic EPS"])

    assert result == [{"period": "2025-06-30", "val": 1.25, "form": "YF"}]


def test_sec_404_uses_yahoo_quarterly_fallback(monkeypatch, tmp_cfg):
    fallback = {
        "ticker": "BAESY",
        "cik": 895564,
        "eps_history": [{"period": "2025-06-30", "val": 1.2}],
        "revenue_history": [{"period": "2025-06-30", "val": 10.0}],
        "data_source": "yahoo_finance_fallback",
    }
    saved = {}
    monkeypatch.setattr(ufr, "_fetch_xbrl_facts", lambda cik, cfg: None)
    monkeypatch.setattr(ufr, "_fetch_yahoo_fundamentals", lambda ticker, cik: fallback)
    monkeypatch.setattr(ufr.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        ufr, "save_ticker_db", lambda cfg, ticker, data: saved.update({ticker: data})
    )

    result = ufr.fetch_one_ticker("BAESY", 895564, tmp_cfg)

    assert result["ok"] is True
    assert result["data"]["data_source"] == "yahoo_finance_fallback"
    assert saved["BAESY"] is fallback


def test_bq_quarter_rows_restore_the_existing_screener_history_contract():
    restored = ufr._bq_rows_to_fundamentals([
        {"ticker": "AAPL", "cik": 320193, "period_end": "2025-06-30",
         "revenue": 94.0, "eps_diluted": 1.5, "updated_at": "2026-07-21T00:00:00Z"},
        {"ticker": "AAPL", "cik": 320193, "period_end": "2025-03-31",
         "revenue": 90.0, "eps_diluted": 1.2, "updated_at": "2026-07-21T00:00:00Z"},
    ])

    assert restored["revenue_history"] == [
        {"period": "2025-06-30", "val": 94.0},
        {"period": "2025-03-31", "val": 90.0},
    ]
    assert restored["latest_eps"] == 1.5
    assert restored["data_source"] == "sec_edgar_bq"


def test_merge_sql_uses_the_declared_primary_key():
    sql = ufr._us_fundamentals_merge_sql("project.us.fundamentals")

    assert "MERGE `project.us.fundamentals`" in sql
    assert "ON T.ticker = S.ticker AND T.period_end = S.period_end" in sql
    assert "WHEN MATCHED THEN UPDATE SET" in sql


def test_schema_migrates_an_existing_wide_fundamentals_table():
    ddl = "\n".join(DDL)
    assert "ADD COLUMN IF NOT EXISTS period_end DATE" in ddl
    assert "ADD COLUMN IF NOT EXISTS f_id STRING" in ddl
    assert "cik               INT64" in ddl
