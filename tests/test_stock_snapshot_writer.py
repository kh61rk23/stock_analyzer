# stock_snapshot_writer — スクリーナー結果の日次スナップショット（DB移管 Ph1）
import stock_snapshot_writer as w

DATE = "2026-09-09"


def _cand(**kw):
    base = {"ticker": "6501.T", "name": "日立", "composite_score": 72.2,
            "pass_filters": True}
    base.update(kw)
    return base


class TestBuildRows:
    def test_only_passing_candidates_are_kept(self):
        rows = w.build_snapshot_rows(
            [_cand(), _cand(ticker="9999.T", pass_filters=False)],
            "JP", DATE)
        assert [r["ticker"] for r in rows] == ["6501.T"]

    def test_missing_pass_filters_is_treated_as_passing(self):
        """履歴形式には pass_filters が無い。載っている＝通過とみなす"""
        c = _cand()
        del c["pass_filters"]
        rows = w.build_snapshot_rows([c], "JP", DATE)
        assert len(rows) == 1
        assert rows[0]["pass_filters"] is True

    def test_duplicate_tickers_are_deduplicated(self):
        rows = w.build_snapshot_rows([_cand(), _cand()], "JP", DATE)
        assert len(rows) == 1

    def test_rows_without_ticker_are_dropped(self):
        rows = w.build_snapshot_rows([_cand(ticker=""), _cand(ticker=None)],
                                     "JP", DATE)
        assert rows == []


class TestFieldMapping:
    def test_technical_indicators_come_from_caches(self):
        """mdにしか無かった指標がスナップショットに載ること（本移管の主目的）"""
        tech = {"6501.T": {"rsi_d": 58.6, "score": 61.0,
                           "macd": {"crossup": True},
                           "ma_cross": {"above_ma200": True},
                           "signals": ["MACD上昇中:+8", "200日MA上:+8"]}}
        sr = {"6501.T": {"above_vwap": True, "vwap_20d": 4210.5,
                         "position_pct": 73.0}}
        row = w.build_snapshot_rows([_cand()], "JP", DATE, tech, sr)[0]

        assert row["rsi14"] == 58.6
        assert row["macd_crossup"] is True
        assert row["above_ma200"] is True
        assert row["above_vwap"] is True
        assert row["vwap_20d"] == 4210.5
        assert row["week52_position_pct"] == 73.0
        assert row["technical_score"] == 61.0
        assert "MACD上昇中" in row["signal_text"]

    def test_absent_caches_yield_null_not_false(self):
        """未取得(None)と False を取り違えない"""
        row = w.build_snapshot_rows([_cand()], "JP", DATE)[0]
        assert row["macd_crossup"] is None
        assert row["above_vwap"] is None
        assert row["rsi14"] is None

    def test_financial_fields_absent_from_history_are_captured(self):
        row = w.build_snapshot_rows([_cand(
            fcf_margin=2.3, debt_equity=70.47, equity_ratio=48.1,
            div_yield=2.7, roic=8.2, sector_score=56.8,
        )], "JP", DATE)[0]
        for k, v in [("fcf_margin", 2.3), ("debt_equity", 70.47),
                     ("equity_ratio", 48.1), ("dividend_yield", 2.7),
                     ("roic", 8.2), ("sector_score", 56.8)]:
            assert row[k] == v, f"{k} が欠落"

    def test_non_numeric_and_nan_become_null(self):
        row = w.build_snapshot_rows([_cand(
            per="", pbr="N/A", roe=float("nan"), market_cap_b=float("inf"),
        )], "JP", DATE)[0]
        assert row["per"] is None
        assert row["pbr"] is None
        assert row["roe"] is None, "NaN は BQ が受け付けないため None にする"
        assert row["market_cap_b"] is None

    def test_yfinance_only_fields_are_filled_from_detail_cache(self):
        """候補dictに無い財務指標を stock_detail_report の取得結果で補う

        ROIC / FCFマージン / D-E比率 / 自己資本比率 / 配当利回りは
        screener の候補dictに存在せず、md生成時に yfinance から
        取っていた。補完しないとBQでNULLのままになる。
        """
        detail = {"6501.T": {"roic": 8.2, "fcf_margin": 2.3,
                             "debt_equity": 70.47, "equity_ratio": 48.1,
                             "div_yield": 2.7, "price": 4210.0,
                             "recommendation": "buy"}}
        row = w.build_snapshot_rows([_cand()], "JP", DATE,
                                    detail_cache=detail)[0]
        assert row["roic"] == 8.2
        assert row["fcf_margin"] == 2.3
        assert row["debt_equity"] == 70.47
        assert row["equity_ratio"] == 48.1
        assert row["dividend_yield"] == 2.7
        assert row["current_price"] == 4210.0
        assert row["analyst_reco"] == "buy"

    def test_candidate_value_wins_over_detail_cache(self):
        """候補dict側が正本。detailは欠損の補完にのみ使う"""
        row = w.build_snapshot_rows(
            [_cand(per=10.0)], "JP", DATE,
            detail_cache={"6501.T": {"per": 99.9}})[0]
        assert row["per"] == 10.0

    def test_market_and_date_are_stamped(self):
        jp = w.build_snapshot_rows([_cand()], "JP", DATE)[0]
        us = w.build_snapshot_rows([_cand(ticker="AAPL")], "US", DATE)[0]
        assert jp["market"] == "JP" and us["market"] == "US"
        assert jp["snapshot_date"] == DATE


class TestRunner:
    def test_both_markets_are_written(self, monkeypatch):
        sent = {}

        def fake_insert(rows=None, table_ref=""):
            sent["rows"] = rows
            sent["table"] = table_ref
            return True

        import edinet_bq_client
        monkeypatch.setattr(edinet_bq_client, "insert_rows", fake_insert)

        res = w.run_stock_snapshot_writer(object(), {
            "screener":    {"candidates": [_cand()]},
            "us_screener": {"candidates": [_cand(ticker="AAPL")]},
        })

        assert res["jp"] == 1 and res["us"] == 1
        assert res["inserted"] is True
        assert sent["table"] == "analytics.stock_snapshot"
        assert {r["market"] for r in sent["rows"]} == {"JP", "US"}

    def test_no_candidates_is_skipped_not_failed(self):
        res = w.run_stock_snapshot_writer(object(), {"screener": {}})
        assert res["skipped"] is True
        assert res["inserted"] is False

    def test_bq_failure_does_not_raise(self, monkeypatch):
        import edinet_bq_client

        def boom(**_kw):
            raise RuntimeError("BQ down")

        monkeypatch.setattr(edinet_bq_client, "insert_rows", boom)
        res = w.run_stock_snapshot_writer(object(), {
            "screener": {"candidates": [_cand()]}})
        assert res["inserted"] is False
        assert res["skipped"] is False
