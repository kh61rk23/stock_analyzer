# 2026-09-29: メールの各節（テーマETF熱量・高配当・決算サプライズ・配当利回りの単位）
import types

import alert_notifier as an
import earnings_surprise as es
import market_data as md
import thematic_etf_tracker as te


def test_arxiv_growth_is_smoothed():
    assert te.arxiv_growth(741, 592) == round(((746) / (597) - 1) * 100, 1)
    assert te.arxiv_growth(13, 6) < 117, "少数件の伸び率が極端にならない"
    assert te.arxiv_growth(0, 0) == 0.0


def test_theme_uses_api_counts(monkeypatch):
    monkeypatch.setattr(te, "_fetch_etf_momentum", lambda t: None)
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    th = te.THEME_DEFINITIONS[0]
    api = {th["id"]: {"recent_4w": 755, "prev_4w": 662, "growth_rate": 13.9,
                      "papers": [{"title": "QBX", "published": "2026-09-25"}]}}
    r = te._score_theme(th, None, cfg=None, api_counts=api)
    assert r["arxiv_detail"]["source"] == "arxiv_api" and r["arxiv_trend_score"] != 50.0
    al = an.extract_alerts(types.SimpleNamespace(), {"etf_tracker": {
        "themes": [dict(r, signal="warming")], "top_themes": [dict(r, signal="warming")]}})
    body = next(a["body"] for a in al if a["trigger"] == "etf_theme")
    assert "直近4週755件" in body and "QBX" in body


def test_div_yield_is_ratio():
    """yfinance の dividendYield は %値（2.72 = 2.72%）"""
    assert abs(md._div_yield_ratio({"dividendYield": 2.72}) - 0.0272) < 1e-9
    assert md._div_yield_ratio({"trailingAnnualDividendYield": 0.0237, "dividendYield": 2.72}) == 0.0237


def test_high_dividend_scans_all_candidates():
    cfg = types.SimpleNamespace(ALERT_MIN_DIVIDEND_YIELD=0.04)
    res = {"screener": {"candidates": [
               {"ticker": "9434.T", "name": "ソフトバンク", "dividend_yield": 0.041, "payout_ratio": 0.76,
                "sector_name": "情報・通信業"},
               {"ticker": "8306.T", "name": "MUFG", "dividend_yield": 0.027},
               {"ticker": "X.T", "name": "異常値", "dividend_yield": 0.40}]},
           "us_screener": {"candidates": [
               {"ticker": "MO", "name": "Altria", "div_yield": 0.0646, "payout_ratio": 0.89, "sector": "Consumer"}]}}
    found = an.collect_high_dividend(cfg, res)
    assert [v["ticker"] for v in found["JP"]] == ["9434.T"]
    assert [v["ticker"] for v in found["US"]] == ["MO"]
    al = an.build_high_dividend_alert(cfg, res)
    assert al["title"].startswith("💰 高配当銘柄 日本1社・米国1社")


def test_surprise_flags_use_todays_analysis_and_give_reason():
    cands = [{"ticker": "6504.T", "acc_bonus": 8.0}, {"ticker": "Z.T", "acc_bonus": -9.0}]
    cache = {"6504.T": {"conservatism_score": 75, "avg_beat_pct": 17.8, "beat_count": 3,
                        "profit_accel": {"q_yoy": 0.4, "annual_yoy": 0.1, "accel": 0.3},
                        "consecutive_pos": 3, "next_earnings": "2026-10-29"},
             "Z.T": {"conservatism_score": 20, "avg_beat_pct": -8.0, "beat_count": 0,
                     "consecutive_neg": 2, "next_earnings": None}}
    n = es.apply_surprise_flags(cands, cache, today="2026-09-29")
    assert n == {"high_prob": 1, "negative": 1}
    assert cands[0]["days_to_earnings"] == 30 and "3期連続上振れ" in cands[0]["surprise_reason"]
    al = an.extract_alerts(types.SimpleNamespace(), {"screener": {"candidates": [
        dict(cands[0], pass_filters=True, name="富士電機"), dict(cands[1], name="Z")]}})
    body = next(a["body"] for a in al if a["trigger"] == "high_prob_surprise")
    assert "決算 2026-10-29（あと30日）" in body


def test_earnings_cache_is_per_ticker(tmp_path):
    cfg = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    es._save_cache(cfg, {"A": {"analyzed_at": "2026-09-28T10:00:00", "profit_accel": None},
                         "B": {"analyzed_at": "2026-08-01T10:00:00", "profit_accel": None}})
    got = es._load_cache(cfg, max_age_days=10 ** 4)
    assert set(got) == {"A", "B"}
    assert "B" not in es._load_cache(cfg), "7日より古い銘柄は取り直す"
