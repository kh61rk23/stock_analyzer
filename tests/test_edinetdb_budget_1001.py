# 2026-10-01: EdinetDB の呼び出しが1日50回前後で止まっていた件
#   ・日次予算46は「月1,450回」という誤った前提（実際は1日100回・月100×日数）
#   ・計画は1社3回で見積もるが text-blocks は2回に1回なので枠が余っていた
from types import SimpleNamespace

import config
import edinetdb_ingestor as module


def test_budget_matches_free_plan():
    assert 90 <= config.EDINETDB_DAILY_BUDGET <= 100
    assert config.EDINETDB_DAILY_BUDGET * 31 <= 3100


def test_leftover_calls_are_used_for_more_companies(tmp_path, monkeypatch):
    companies = [{"edinet_code": f"E{i:05d}", "sec_code": f"{1300 + i}0",
                  "name": f"社{i}"} for i in range(30)]

    class FakeRest:
        def __init__(self, *_a, **_k):
            self.calls = 0
            self.daily_budget = 30
            self.last_error = None

        def usage(self):
            return {"daily_remaining": 100}

        def get(self, path, params=None):
            self.calls += 1
            if path == "/companies":
                return companies
            if path.endswith("/financials"):
                return [{"fiscal_year": 2025, "revenue": 100}]
            if path.endswith("/segments"):
                return []
            raise AssertionError(path)   # fetch_count=1 なので text-blocks は呼ばない

    class FakeBQ:
        def merge_rows(self, *_a, **_k):
            return True

    meta = {c["edinet_code"]: {"last_fetched": "2020-01-01", "fetch_count": 1}
            for c in companies}
    module._save_json(tmp_path / "rotation.json", meta)
    monkeypatch.setattr(module, "DBRestClient", FakeRest)
    monkeypatch.setattr("edinet_bq_client.EDINETBQClient", FakeBQ)
    monkeypatch.setattr(module, "_bq_ingested_latest", lambda cfg: {})
    cfg = SimpleNamespace(
        ENABLE_EDINETDB_INGEST=True, EDINETDB_API_KEY="key",
        EDINETDB_DAILY_BUDGET=30, EDINETDB_DOC_INDEX_RESERVE=11,
        EDINETDB_REFRESH_DAYS=180, EDINETDB_TEXTBLOCK_EVERY_N=2,
        EDINETDB_ROTATION_META="rotation.json", EDINETDB_WEB_PRECHECK=False,
        DATA_DIR=tmp_path, SECTOR_REPRESENTATIVE_TICKERS={},
    )
    r = module.run_edinetdb_ingest(cfg)
    # 計画は (30-2-11)//3 = 5社だが、1社2回なので 9社（30−1−11=18回）まで取れる
    assert r["success"] == 9
    # doc_index 用の11回は残す
    assert 30 - r["api_calls"] >= 11


def test_interest_pattern_has_no_catastrophic_backtracking():
    # 2026-10-01: 月次 geo_exposure が15分で打ち切られた原因
    import time
    import edinet_exposure_extractor as ex
    text = ("当社はチリ" + "あ" * 60 + "。") * 300 + "当社はチリの銅鉱山の権益を保有。"
    t0 = time.time()
    recs = ex._extract_exposure_from_text(text, "8058.T", "事業等のリスク", min_share=0.03)
    assert time.time() - t0 < 2
    assert ("CHL", "copper") in {(r["country_code"], r["commodity_code"]) for r in recs}


def test_arxiv_query_uses_title_abstract_and_categories():
    import thematic_etf_tracker as t
    q = t.build_arxiv_query(["fuel cell"], ["physics.chem-ph"], "20260901", "20260928")
    assert 'ti:"fuel cell" OR abs:"fuel cell"' in q and "cat:physics.chem-ph" in q
    assert "all:" not in q
    for th in t.THEME_DEFINITIONS:       # 全テーマに分野カテゴリがあり、広すぎる語は使わない
        assert th.get("arxiv_api_cats")
        assert not {"SMR", "transformer", "energy storage", "satellite"} & set(th["arxiv_keywords"])


def test_small_sample_theme_score_is_pulled_to_neutral(monkeypatch):
    import thematic_etf_tracker as t
    monkeypatch.setattr(t, "_fetch_etf_momentum", lambda tk: None)
    monkeypatch.setattr(t.time, "sleep", lambda s: None)
    th = t.THEME_DEFINITIONS[0]
    few = t._score_theme(th, None, api_counts={th["id"]: {"recent_4w": 4, "prev_4w": 0, "growth_rate": 80.0}})
    many = t._score_theme(th, None, api_counts={th["id"]: {"recent_4w": 400, "prev_4w": 200, "growth_rate": 80.0}})
    assert 50 < few["arxiv_trend_score"] < many["arxiv_trend_score"]


def test_profit_acceleration_and_flags():
    import earnings_surprise as es
    ann = [("2026-03-31", 1100), ("2025-03-31", 1000)]
    # 日本株に多い「直近期と前年同期の2列だけ」でも日付で対応させる
    pa = es.calc_profit_acceleration([("2026-06-30", 150), ("2025-06-30", 100)], ann)
    assert (pa["q_yoy"], pa["annual_yoy"], pa["accel"]) == (0.5, 0.1, 0.4)
    assert es.calc_profit_acceleration([("2026-06-30", 150), ("2026-03-31", 100)], ann) is None
    assert es.calc_profit_acceleration([("2026-06-30", -5), ("2025-06-30", 100)], ann) is None   # 赤字
    cache = {
        "A.T": {"profit_accel": pa, "conservatism_score": 80, "latest_q": {"surprise_pct": 5}},
        # 増益でも加速していない（旧式だと増益率×3で「加速」扱いになっていた）
        "B.T": {"profit_accel": {"q_yoy": 0.1, "annual_yoy": 0.12, "accel": -0.02},
                "conservatism_score": 80, "latest_q": {"surprise_pct": 5}},
        "D.T": {"profit_accel": None, "conservatism_score": 20, "consecutive_neg": 2},
    }
    # C.T は分析結果が無い: スクリーナーの仮判定（保守度0扱いのネガ）を残さない
    cands = [{"ticker": t, "acc_bonus": 7, "is_negative_risk": True} for t in ("A.T", "B.T", "C.T", "D.T")]
    n = es.apply_surprise_flags(cands, cache, today="2026-10-01")
    flags = {c["ticker"]: (c["is_high_prob_surprise"], c["is_negative_risk"]) for c in cands}
    assert flags == {"A.T": (True, False), "B.T": (False, False),
                     "C.T": (False, False), "D.T": (False, True)}
    assert n == {"high_prob": 1, "negative": 1}
