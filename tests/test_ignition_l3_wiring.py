"""ignition の注文が保存済みの発火結果から出ること・13F の L3 が日本株の候補に米国ティッカーを入れないこと。"""
import json
from types import SimpleNamespace

import ignition_detector as ign
import institutional_signal_scorer as iss
from paper.strategy_adapters import ignition_adapter


def _cfg(tmp_path, **kw):
    return SimpleNamespace(CACHE_DIR=str(tmp_path), SECTOR_REPRESENTATIVE_TICKERS={"1017": ["7203.T"]}, **kw)


def _write_alerts(tmp_path, generated, ttl_expiry):
    (tmp_path / "ignition_alerts.json").write_text(json.dumps(
        {"generated": generated, "alerts": [{"sector_code": "1017", "sector_name": "輸送用機器",
                                             "ttl_expiry": ttl_expiry}]}), encoding="utf-8")


def test_ignition_reads_saved_alerts_when_results_has_none(tmp_path):
    _write_alerts(tmp_path, "2026-11-02", "2026-11-16")
    orders = ignition_adapter(_cfg(tmp_path), {}, {"positions": {}}, "2026-11-05")
    assert [o["ticker"] for o in orders] == ["7203.T"]


def test_ignition_ignores_expired_saved_alerts(tmp_path):
    _write_alerts(tmp_path, "2026-10-01", "2026-10-15")
    assert ignition_adapter(_cfg(tmp_path), {}, {"positions": {}}, "2026-11-05") == []


def test_ignition_results_take_precedence_over_saved(tmp_path):
    _write_alerts(tmp_path, "2026-11-02", "2026-11-16")
    orders = ignition_adapter(_cfg(tmp_path), {"ignition": {"alerts": []}}, {"positions": {}}, "2026-11-05")
    assert orders == []


def test_ignition_threshold_is_reachable():
    import config
    # 本番の入力で届く上限は約2.7。閾値がそれを超えると一度も発火しない
    assert config.IGNITION_MIN_SCORE_LINE <= 2.5
    assert config.IGNITION_MAX_SCORE_LINE_CRITICAL <= 2.7
    assert config.IGNITION_MIN_SCORE_LINE < config.IGNITION_MAX_SCORE_LINE_CRITICAL
    assert ign._DEFAULT_MIN_SCORE_LINE == config.IGNITION_MIN_SCORE_LINE


def test_l3_override_does_not_insert_us_ticker_into_jp_candidates():
    cands = [{"ticker": "6758.T", "sector_code": "1016", "composite_score": 70.0}]
    results = {"screener": {"candidates": cands}}
    scored = [{"ticker": "NFLX", "institution": "X", "tier": 1, "adj_pt": 15.0, "l3_trigger": True}]
    tickers, sectors = iss._apply_l3_override(scored, results, SimpleNamespace())
    assert tickers == ["NFLX"] and sectors == []          # 米国screenerが override_tickers を使う
    assert [c["ticker"] for c in results["screener"]["candidates"]] == ["6758.T"]


def test_l3_override_still_inserts_jp_ticker():
    cands = [{"ticker": "6758.T", "sector_code": "1016", "composite_score": 70.0}]
    results = {"screener": {"candidates": cands}}
    scored = [{"ticker": "9984.T", "institution": "X", "tier": 1, "adj_pt": 15.0, "l3_trigger": True}]
    iss._apply_l3_override(scored, results, SimpleNamespace())
    assert results["screener"]["candidates"][0]["ticker"] == "9984.T"
