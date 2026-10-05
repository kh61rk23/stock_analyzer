# 2026-10-05: 週次レポートで動いていなかったシグナルの是正
import json
import types

import latent_pearl_manager as lpm
import pearl_score_calculator as psc
import portfolio_diversity_filter as pdf
import rule_optimizer as ro


def test_d5_valuation():
    v, flags = psc._calc_d5_valuation({"per": 8.0, "pbr": 0.8}, "JP")
    assert v == 100.0
    v2, _ = psc._calc_d5_valuation({"per": 30.0, "pbr": 5.0}, "JP")
    assert v2 == 0.0
    assert psc._calc_d5_valuation({}, "JP")[0] is None


def test_latent_live_catalysts(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "data" / "tdnet_notified.json").write_text(json.dumps(
        {"notified": ["8058.T_2099-01-01_BUYBACK", "8058.T_2000-01-01_DIV_UP", "7203.T_2099-01-01_BUYBACK"]}))
    (tmp_path / "data" / "earnings_surprise_cache.json").write_text(json.dumps({"results": {
        "8058.T": {"conservatism_score": 80, "profit_accel": {"accel": 0.3}}}}))
    (tmp_path / "cache" / "pead_candidates.json").write_text(json.dumps(
        {"8058.T": {"direction": "positive", "last_updated": "2099-01-01"}}))
    cfg = types.SimpleNamespace(DATA_DIR=str(tmp_path / "data"), CACHE_DIR=str(tmp_path / "cache"))
    score, flags = lpm._get_live_catalysts("8058.T", cfg)
    assert score == 8.0 and len(flags) == 3          # 自社株買い3＋サプライズ3＋PEAD2（古い増配は除外）


def test_contraction_reads_real_keys(tmp_path):
    cfg = types.SimpleNamespace(DATA_DIR=str(tmp_path))
    res = {"options_monitor": {"option_data": {"spy": {"pcr_oi": None, "pcr_volume": 1.6}}},
           "geo_risk": {"risk_scores": {"MMR": 43.0, "THA": 39.0}},
           "signal_aggregator": {"sector_signals": {"1": {"signal_score": 60}}}}
    out = ro.evaluate_contraction_flag(res, cfg)
    d = out["detail"]
    assert d["pcr"] == 1.6 and d["geo_risk_score"] == 43.0 and d["geo_risk_country"] == "MMR"
    assert (tmp_path / "contraction_state.json").exists()   # 翌週比較のため保存


def test_holdings_concentration():
    r = pdf.holdings_concentration([
        {"ticker": "A", "sector_name": "卸売業", "market": "JP", "market_value_jpy": 600},
        {"ticker": "B", "sector_name": "銀行業", "market": "JP", "market_value_jpy": 400}])
    assert r["hhi_sector"] == 0.52 and r["top3_share"] == 1.0 and r["market_weights"]["JP"] == 1.0
