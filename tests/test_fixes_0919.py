# 日次メール・Actionsログから見つかった不具合の回帰テスト
import types
from datetime import datetime, timedelta


def _hist(values_by_days_ago, adj=None):
    """{days_ago: score} から score_history 形式を作る。"""
    today = datetime.now().date()
    out = {}
    for ago, score in values_by_days_ago.items():
        entry = {"score": score, "risk": "low"}
        if adj is not None:
            entry["momentum_adj"] = adj.get(ago, 0.0)
        out[(today - timedelta(days=ago)).strftime("%Y-%m-%d")] = {"1016": entry}
    return out


# ── モメンタム補正: 33業種中25〜28業種が毎日 ±10点になっていた ─────────────
class TestMomentum:
    def test_tiers_are_points_per_week(self):
        import momentum_factor as m
        assert m.calc_momentum_adjustment(3.0 / 7, 50)[0] == 0.0     # 週3点 → 横ばい
        assert m.calc_momentum_adjustment(5.0 / 7, 50)[0] == 5.0     # 週5点 → 上昇継続
        assert m.calc_momentum_adjustment(8.0 / 7, 50)[0] == 10.0    # 週8点 → 強い上昇
        assert m.calc_momentum_adjustment(-8.0 / 7, 50)[0] == -10.0

    def test_weekly_dips_do_not_drive_slope(self):
        import momentum_factor as m
        # ほぼ横ばいの88点に、週1回の75点の落ち込みが混じる（9/9・9/12 の実例）
        vals = {ago: 88.0 for ago in range(1, 21)}
        vals[4] = vals[11] = vals[18] = 75.0
        v = m.calc_sector_velocity(_hist(vals), "1016")
        assert abs(v * 7) < 1.0

    def test_uses_actual_days_between_points(self):
        import momentum_factor as m
        # 11日空いた2つの塊（8/27→9/7 のような欠測）: 実日数なら週約6点、番号基準だと過大
        vals = {20: 50.0, 19: 50.0, 18: 50.0, 7: 60.0, 6: 60.0, 5: 60.0}
        v = m.calc_sector_velocity(_hist(vals), "1016")
        assert 4.0 < v * 7 < 8.0

    def test_own_adjustment_is_removed_when_recorded(self):
        import momentum_factor as m
        # 保存スコアは +10 補正込み。補正を差し引けば横ばい
        vals = {ago: (60.0 if ago <= 5 else 50.0) for ago in range(1, 11)}
        adj = {ago: (10.0 if ago <= 5 else 0.0) for ago in range(1, 11)}
        v = m.calc_sector_velocity(_hist(vals, adj), "1016")
        assert abs(v) < 1e-9

    def test_history_records_momentum_adjustment(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "stock_analyzer_main.py").read_text(encoding="utf-8")
        assert '"momentum_adj": float(r.get("momentum_adjustment", 0.0) or 0.0)' in src


# ── ニュースフィード: 0件が続いたフィードの整理と SEC の 403 ─────────────
class TestNewsFeeds:
    def test_dead_feeds_removed(self):
        import news_pipeline as m
        ids = {f["id"] for f in m.RSS_FEEDS}
        for dead in ("reuters_business", "reuters_markets", "ap_business",
                     "nikkei_jp", "bis_press", "opensecrets"):
            assert dead not in ids
        urls = {f["id"]: f["url"] for f in m.RSS_FEEDS}
        assert urls["mof_press"] == "https://www.mof.go.jp/news.rss"
        # Arabian Business は Actions 環境から HTTP 405 で拒否されるため使わない
        assert "arabian_business" not in urls
        assert "globes.co.il/webservice/rss" in urls["globes_en"]

    def test_sec_feed_uses_configured_user_agent(self, monkeypatch):
        import news_pipeline as m
        seen = {}

        def fake_fetch(feed, timeout=15, user_agent=None):
            seen[feed["id"]] = user_agent
            return []

        monkeypatch.setattr(m, "_fetch_feed", fake_fetch)
        monkeypatch.setattr(m.time, "sleep", lambda s: None)
        cfg = types.SimpleNamespace(SEC_USER_AGENT="StockAnalyzer test", NEWS_FEEDS_ENABLED=None)
        m.fetch_all_feeds([f for f in m.RSS_FEEDS if f["id"] in ("sec_edgar_8k", "nhk_top")], cfg)
        assert seen == {"sec_edgar_8k": "StockAnalyzer test", "nhk_top": None}

    def test_shopping_deals_are_not_investment_news(self):
        import news_pipeline as m
        assert not m.is_investment_relevant(
            {"title": "Tenways e-bikes up to $807 off from $1,599, Mammotion LUBA Mini"})
        assert not m.is_investment_relevant({"title": "Anker power station hits new all-time low"})
        assert m.is_investment_relevant({"title": "Tesla stock rises 5% on delivery beat"})


# ── 議員取引: 保存期限で消えた古い届出を毎日取り直して「新規1,775件」 ─────────
class TestPoliticianProcessedIds:
    def test_processed_file_ids_are_persisted_and_skipped(self, monkeypatch, tmp_path):
        import politician_monitor as m
        cfg = types.SimpleNamespace(CACHE_DIR=str(tmp_path), ENABLE_POLITICIAN_MONITOR=True,
                                    POLITICIAN_TARGET_YEARS=[2026])
        old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        monkeypatch.setattr(m, "_load_event_cache", lambda c: {
            "ptr_records": [{"file_id": "A1", "name": "X", "year": 2026}]})
        calls = []

        def detail(file_id, year):
            calls.append(file_id)
            return [{"politician": "X", "asset": "Old Co", "transaction_date": old,
                     "transaction_type": "P", "amount": "$1,001 - $15,000", "file_id": file_id}]

        monkeypatch.setattr(m, "_fetch_ptr_detail", detail)
        monkeypatch.setattr(m, "score_transaction", lambda tx, name, c: {**tx, "score": 10})
        m.run_politician_monitor(cfg)
        m.run_politician_monitor(cfg)
        assert calls == ["A1"]      # 2回目は取り直さない（旧: 取引が期限切れで消え毎回再取得）
        hist = m._load_politician_history(cfg)
        assert "A1" in hist["processed_file_ids"]


# ── 異常検知: 構造的に低い業種が毎日 HIGH の「スコア急変」になっていた ─────────
class TestScoreLevelAnomalies:
    def test_level_only_is_low_and_outlier_is_escalated(self):
        import anomaly_detector as m
        sectors = {f"{1001 + i}": {"composite_score": 30.0 + (i % 5), "sector_name_ja": str(i)}
                   for i in range(30)}
        sectors["1005"] = {"composite_score": 1.0, "sector_name_ja": "繊維"}   # 外れ値
        sectors["1004"] = {"composite_score": 28.0, "sector_name_ja": "食料品"}  # 水準だけ
        out = {a["sector_code"]: a for a in m.detect_score_anomalies({"sectors": sectors})}
        assert out["1004"]["severity"] == "low"
        assert out["1005"]["severity"] == "high"


# ── 候補ビルダー: TDnet の読み取りキーが違い毎日0社だった ─────────────────
class TestCandidateBuilderTdnet:
    def test_reads_events_from_results(self):
        import screener_candidate_builder as m
        out = m._load_tdnet_tickers({"tdnet": {"events": [
            {"ticker": "5122.T", "category": "BUYBACK"},
            {"code": "7287", "category": "DIV_UP"},
            {"ticker": "1234.T", "category": "MGMT_CHANGE"},   # 対象外カテゴリ
        ]}}, None)
        assert out == ["5122.T", "7287.T"]

    def test_falls_back_to_notified_cache(self, tmp_path):
        import json as _json
        import types
        from datetime import date, timedelta
        import screener_candidate_builder as m
        recent = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        old = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")
        (tmp_path / "tdnet_notified.json").write_text(_json.dumps({"notified": [
            f"5122.T_{recent}_BUYBACK", f"9107.T_{recent}_DIV_UP",
            f"1111.T_{old}_BUYBACK", f"2222.T_{recent}_SPLIT",
        ]}), encoding="utf-8")
        cfg = types.SimpleNamespace(DATA_DIR=str(tmp_path))
        assert m._load_tdnet_tickers({}, cfg) == ["5122.T", "9107.T"]

    def test_builder_adds_them(self, tmp_path):
        import json as _json
        import types
        from datetime import date, timedelta
        import screener_candidate_builder as m
        recent = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        (tmp_path / "tdnet_notified.json").write_text(
            _json.dumps({"notified": [f"5122.T_{recent}_BUYBACK"]}), encoding="utf-8")
        cfg = types.SimpleNamespace(DATA_DIR=str(tmp_path), JAPAN_SECTORS={},
                                    DYNAMIC_SCREENER_MAX_ADD=20)
        out = m.build_dynamic_screener_tickers(cfg, {})
        assert out["all_tickers"] == ["5122.T"]
        assert out["dynamic_meta"]["5122.T"]["source"] == "tdnet"


# ── taxonomy 業種スコア: 企業コードの表を業種コードで引いて毎日フォールバック ─────
class TestTaxonomySectorWeights:
    def test_aggregates_by_sector_in_two_queries(self, monkeypatch):
        import sys
        import types as _types
        import taxonomy_hotness as m
        calls = []

        class FakeBQ:
            def get_tickers_by_sector_bulk(self, limit=10):
                calls.append(("tickers", limit))
                return {"1016": ["6501.T", "6758.T"], "1025": ["9432.T"]}

            def get_taxonomy_scores_bulk(self, codes):
                calls.append(("scores", tuple(codes)))
                return {"6501": {"ai_software": 0.6}, "6758": {"ai_software": 0.4},
                        "9432": {"telecom": 0.5}}

        monkeypatch.setitem(sys.modules, "edinet_bq_client",
                            _types.SimpleNamespace(EDINETBQClient=FakeBQ))
        out = m.fetch_sector_node_weights(
            _types.SimpleNamespace(TAXONOMY_SECTOR_TICKER_LIMIT=10), ["1016", "1025"])
        assert out == {"1016": {"ai_software": 0.5}, "1025": {"telecom": 0.5}}
        assert calls == [("tickers", 10), ("scores", ("6501", "6758", "9432"))]

    def test_empty_map_falls_back(self, monkeypatch):
        import sys
        import types as _types
        import taxonomy_hotness as m

        class FakeBQ:
            def get_tickers_by_sector_bulk(self, limit=10):
                return {"1016": ["6501.T"]}

            def get_taxonomy_scores_bulk(self, codes):
                return {}

        monkeypatch.setitem(sys.modules, "edinet_bq_client",
                            _types.SimpleNamespace(EDINETBQClient=FakeBQ))
        assert m.fetch_sector_node_weights(_types.SimpleNamespace(), ["1016"]) == {}

    def test_bulk_query_is_parameterized(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "edinet_bq_client.py").read_text(encoding="utf-8")
        assert "def get_taxonomy_scores_bulk" in src
        assert "company_id IN UNNEST(@codes)" in src


# ── 週次モジュールの読み先・実行順のずれ（9/21 の週次ログ） ─────────────
class TestWeeklyWiring:
    def test_score_history_is_read_from_data_dir(self, tmp_path):
        import json as _json
        import types as _types
        import score_history_io as m
        (tmp_path / "score_history.json").write_text(
            _json.dumps({"2026-09-19": {"1016": {"score": 80.0}}}), encoding="utf-8")
        out = m.load_score_history(_types.SimpleNamespace(DATA_DIR=str(tmp_path)))
        assert out == {"2026-09-19": {"1016": {"score": 80.0}}}

    def test_portfolio_optimizer_reads_configured_holdings(self, tmp_path):
        import types as _types
        import portfolio_optimizer as m
        csv_path = tmp_path / "portfolio_holdings.csv"
        csv_path.write_text("ticker,name,quantity,avg_cost_jpy,sector_code,market\n"
                            "6501.T,日立,100,3000,1016,JP\n", encoding="utf-8")
        cfg = _types.SimpleNamespace(PORTFOLIO_HOLDINGS_PATH=str(csv_path),
                                     DATA_DIR=str(tmp_path / "none"))
        out = m.load_portfolio(cfg)
        assert [h["ticker"] for h in out] == ["6501.T"]

    def test_backtest_records_survive_missing_pass_flag(self):
        from datetime import date, timedelta
        import backtester as m
        entry = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
        history = {entry: [
            {"ticker": "6501.T", "current_price": 3000.0, "composite_score": 70},      # 旧形式
            {"ticker": "7203.T", "current_price": 2500.0, "pass_filters": True},        # 新形式
            {"ticker": "9999.T", "current_price": 100.0, "pass_filters": False},        # 不通過
            {"ticker": "8306.T", "composite_score": 60},                                # 価格なし
        ]}
        got = {r["ticker"] for r in m.extract_backtest_records(history)}
        # 価格なしの行も残す（run_backtester がその日の終値で補う）
        assert got == {"6501.T", "7203.T", "8306.T"}

    def test_screener_history_records_entry_price(self, tmp_path):
        import json as _json
        import types as _types
        import performance_tracker as m
        cfg = _types.SimpleNamespace(DATA_DIR=str(tmp_path))
        m.append_screener_history(cfg, {"candidates": [
            {"ticker": "6501.T", "pass_filters": True, "current_price": 3000.0},
            {"ticker": "9999.T", "pass_filters": False, "current_price": 100.0},
        ]})
        saved = _json.loads((tmp_path / "screener_history.json").read_text(encoding="utf-8"))
        rows = list(saved.values())[0]
        assert [r["ticker"] for r in rows] == ["6501.T"]
        assert rows[0]["current_price"] == 3000.0 and rows[0]["pass_filters"] is True

    def test_diversity_filter_runs_after_screener(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "stock_analyzer_main.py").read_text(encoding="utf-8")
        assert src.index("個別銘柄スクリーニングを実行中") < src.index("run_portfolio_diversity_filter")
