"""§13-3 未使用定数29件の削除と新設5機能の回帰テスト。"""

from __future__ import annotations

import ast
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import anomaly_detector as ad  # noqa: E402
import comps_analyzer as ca  # noqa: E402
import policy_radar as pr  # noqa: E402
import policy_tracker as pt  # noqa: E402
import portfolio_optimizer as po  # noqa: E402
import stock_screener as ss  # noqa: E402


LEGACY_CONSTANTS = {
    "academic_conf_tracker.py": ["_ACADEMIC_SCORE_WEIGHTS"],
    "anomaly_detector.py": ["SCORE_CHANGE_THRESHOLD"],
    "anomaly_season.py": ["P_HIGHLY_SIG"],
    "catalyst_calendar.py": ["_CALENDAR_TTL_DAYS"],
    "ceo_evaluator.py": ["TENURE_PROVISIONAL_MAX"],
    "comps_analyzer.py": ["COMPS_VALUE_WEIGHT"],
    "dead_ticker_pruner.py": ["WAIT_SEC"],
    "drift_detector.py": ["_DEFAULT_HALF_LIFE"],
    "earnings_surprise.py": ["SURPRISE_LARGE_POS", "SURPRISE_LARGE_NEG"],
    "edinet_direct_fetcher.py": ["HTML_TYPE"],
    "edinet_rotation_manager.py": ["MARKET_PRIORITY"],
    "finance_logic.py": ["DEFAULT_THRESHOLDS"],
    "ignition_detector.py": ["_DEFAULT_CONVERGENCE_WINDOW"],
    "institutional_13f_monitor.py": ["_QUARTER_END_MONTHS"],
    "jp_us_sector_bridge.py": [
        "INDEPENDENCE_BRIDGE_THRESHOLD", "CROSS_SIZE_BRIDGE_MAX_BOOST",
    ],
    "midterm_plan_tracker.py": ["_ACHIEVEMENT_TIERS"],
    "options_monitor.py": ["VIX_FEAR", "VIX_CALM"],
    "pead_monitor.py": ["_MIN_REVISION_SCORE"],
    "pearl_score_calculator.py": ["_DEFAULT_TIER_THRESHOLDS"],
    "policy_signal_applier.py": ["_DEFAULT_CACHE_LAYERS"],
    "policy_tracker.py": ["_L3_SCORE_THRESHOLD"],
    "portfolio_diversity_filter.py": ["_CACHE_TTL_DAYS"],
    "portfolio_optimizer.py": ["LOOKBACK_DAYS"],
    "self_evaluator.py": ["PEARL_HOLD_DAYS"],
    "size_decay_manager.py": ["_DEFAULT_TTL_DAYS"],
    "stock_screener.py": ["DISCOVERY_SCORE_THRESHOLD"],
}


def _defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_all_29_legacy_constants_are_removed():
    count = 0
    for filename, names in LEGACY_CONSTANTS.items():
        defined = _defined_names(REPO / filename)
        for name in names:
            count += 1
            assert name not in defined, f"{filename}: {name} が残っている"
    assert count == 29


def test_finance_threshold_defaults_remain_available_without_old_constant():
    from finance_logic import get_default_thresholds

    first = get_default_thresholds()
    second = get_default_thresholds()
    assert first["roe_min"] == 0.10
    assert first == second
    assert first is not second


def _score_cfg(tmp_path, threshold=15.0):
    return SimpleNamespace(
        DATA_DIR=tmp_path,
        ALERT_ON_SCORE_JUMP=True,
        ALERT_SCORE_JUMP_THRESHOLD=threshold,
    )


def _write_score_history(tmp_path, payload):
    (tmp_path / "score_history.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_score_jump_uses_latest_observation_and_boundary(tmp_path):
    _write_score_history(tmp_path, {
        "2026-07-20": {"1001": {"score": 30.0}},
        "2026-07-24": {"1001": {"score": 40.0}},
        "2026-07-26": {"1001": {"score": 99.0}},  # 当日既存値は比較対象外
    })
    results = {"sectors": {
        "1001": {"composite_score": 55.0, "sector_name_ja": "水産"}
    }}
    out = ad.detect_score_jumps(
        _score_cfg(tmp_path), results, datetime(2026, 7, 26, 9, 0)
    )
    assert out["previous_date"] == "2026-07-24"
    assert len(out["alerts"]) == 1
    assert out["alerts"][0]["diff"] == 15.0  # 境界を含む


def test_score_jump_config_change_and_missing_history_fail_soft(tmp_path):
    results = {"sectors": {"1001": {"composite_score": 55.0}}}
    missing = ad.detect_score_jumps(
        _score_cfg(tmp_path), results, datetime(2026, 7, 26)
    )
    assert missing["alerts"] == []
    assert missing["reason"] == "score_history missing"

    _write_score_history(tmp_path, {
        "2026-07-25": {"1001": {"score": 40.0}},
    })
    strict = ad.detect_score_jumps(
        _score_cfg(tmp_path, threshold=16.0), results, datetime(2026, 7, 26)
    )
    assert strict["alerts"] == []


def test_score_jump_persistent_dedup(tmp_path):
    _write_score_history(tmp_path, {
        "2026-07-25": {"1001": {"score": 40.0}},
    })
    cfg = _score_cfg(tmp_path)
    results = {"sectors": {"1001": {"composite_score": 60.0}}}
    first = ad.detect_score_jumps(cfg, results, datetime(2026, 7, 26))
    assert len(first["alerts"]) == 1
    ad.mark_score_jumps_notified(cfg, first["alerts"])
    second = ad.detect_score_jumps(cfg, results, datetime(2026, 7, 26))
    assert second["alerts"] == []
    assert second["suppressed_count"] == 1


def _comps_records(target_roe):
    return [
        {"ticker": "A", "per": 10.0, "pbr": 1.0, "roe": target_roe,
         "ev_ebitda": 5.0},
        {"ticker": "B", "per": 20.0, "pbr": 2.0, "roe": 8.0,
         "ev_ebitda": 10.0},
        {"ticker": "C", "per": 30.0, "pbr": 3.0, "roe": 12.0,
         "ev_ebitda": 15.0},
    ]


def test_comps_roe_is_diagnostic_only():
    low = ca.calc_comps_score("A", _comps_records(1.0))
    high = ca.calc_comps_score("A", _comps_records(99.0))
    assert low["roe_rank"] != high["roe_rank"]
    assert low["comps_score"] == high["comps_score"]


def test_valuation_integration_caps_missing_and_config_change():
    default = ca.integrate_valuation_score(50, {"comps_score": 100}, 8.0)
    assert default["comps_delta"] == 10.0
    assert default["valuation_adjustment"] == 10.0
    assert default["value_score"] == 60.0

    only_base = ca.integrate_valuation_score(62, None, None)
    assert only_base["value_score"] == 62.0
    assert only_base["valuation_sources"] == []

    cfg = SimpleNamespace(
        COMPS_VALUE_BLEND_WEIGHT=0.10,
        COMPS_VALUE_DELTA_CAP=10.0,
        VALUATION_TOTAL_ADJUSTMENT_CAP=10.0,
    )
    changed = ca.integrate_valuation_score(50, {"comps_score": 100}, None, cfg)
    assert changed["comps_delta"] == 5.0
    assert changed["value_score"] == 55.0


def test_candidate_valuation_is_idempotent():
    candidate = {
        "base_value_score": 50.0,
        "value_score": 50.0,
        "composite_score": 60.0,
        "size_weights": {"value_score": 0.30},
    }
    ss.apply_valuation_to_candidate(candidate, {"comps_score": 100}, 8.0)
    once = dict(candidate)
    ss.apply_valuation_to_candidate(candidate, {"comps_score": 100}, 8.0)
    assert candidate["value_score"] == once["value_score"]
    assert candidate["composite_score"] == once["composite_score"]


def _pdf_item(source, text="半導体を閣議決定"):
    return {"source_name": source, "text": text, "url": f"https://{source}"}


def test_policy_l3_uses_unique_sources_and_and_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_calc_realization_score", lambda text, source: 0.5)
    cfg = SimpleNamespace(
        CACHE_DIR=tmp_path,
        PT_L3_SOURCE_THRESHOLD=3,
        PT_L3_REALIZATION_SCORE_THRESHOLD=1.5,
    )
    items = [_pdf_item("a"), _pdf_item("b"), _pdf_item("c")]
    assert pt.promote_to_medium_cache_from_pdf(items, cfg, tmp_path) == 2
    saved = json.loads((tmp_path / "policy_medium_cache.json").read_text())
    assert all(s["cumulative_count"] == 3 for s in saved["signals"])
    assert all(s["cumulative_realization_score"] == 1.5 for s in saved["signals"])


def test_policy_l3_duplicate_source_and_setting_change_do_not_promote(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_calc_realization_score", lambda text, source: 0.5)
    cfg = SimpleNamespace(
        CACHE_DIR=tmp_path,
        PT_L3_SOURCE_THRESHOLD=3,
        PT_L3_REALIZATION_SCORE_THRESHOLD=1.6,
    )
    items = [_pdf_item("same"), _pdf_item("same"), _pdf_item("same")]
    assert pt.promote_to_medium_cache_from_pdf(items, cfg, tmp_path) == 0


class _FakeSeries:
    def __init__(self, values):
        self._values = values

    def dropna(self):
        return self

    def tolist(self):
        return list(self._values)


class _FakeHistory:
    def __init__(self, closes):
        self._closes = closes

    def __len__(self):
        return len(self._closes)

    def __getitem__(self, key):
        assert key == "Close"
        return _FakeSeries(self._closes)


def test_portfolio_trading_days_are_converted_and_sliced(monkeypatch):
    calls = {}

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **kwargs):
            calls.update(kwargs)
            return _FakeHistory([100.0 + i for i in range(300)])

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=FakeTicker))
    now = datetime(2026, 7, 26)
    returns = po._fetch_daily_returns("X", 252, 60, now)
    assert len(returns) == 252
    assert po._calendar_days_for_trading_days(252) == math.ceil(252 * 7 / 5) + 14
    assert "period" not in calls
    assert calls["start"] == "2025-07-24"
    assert calls["end"] == "2026-07-27"


def test_portfolio_minimum_observations_and_invalid_window(monkeypatch):
    class FakeTicker:
        def __init__(self, ticker):
            pass

        def history(self, **kwargs):
            return _FakeHistory([100.0 + i for i in range(60)])

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=FakeTicker))
    assert po._fetch_daily_returns("X", 252, 60) is None
    with pytest.raises(ValueError):
        po._validate_return_window(59, 60)


def test_portfolio_entrypoint_forwards_configured_return_window(monkeypatch):
    captured = {}
    monkeypatch.setattr(po, "_load_cache", lambda cfg, lookback, minimum: None)
    monkeypatch.setattr(po, "load_portfolio", lambda cfg: [{"ticker": "X"}])

    def fake_fetch(holdings, lookback, minimum):
        captured.update(lookback=lookback, minimum=minimum)
        return {}

    monkeypatch.setattr(po, "fetch_portfolio_data", fake_fetch)
    cfg = SimpleNamespace(
        PORTFOLIO_LOOKBACK_DAYS=120,
        PORTFOLIO_MIN_RETURN_OBSERVATIONS=45,
    )
    out = po.run_portfolio_optimizer(cfg)
    assert out["skipped"] is True
    assert captured == {"lookback": 120, "minimum": 45}


def test_discovery_percentile_linear_empty_boundary_and_validation():
    scores = list(range(10))
    assert ss._discovery_composite_threshold(scores, 0.80) == pytest.approx(7.2)
    assert ss._discovery_composite_threshold(scores, 0.50) == pytest.approx(4.5)
    assert ss._discovery_composite_threshold([], 0.80) == 70.0
    at_boundary, _ = ss._calc_discovery_score(
        {"market_cap_b": 0, "avg_volume": 0, "analyst_count": 10},
        composite_score=7.2,
        score_threshold=7.2,
    )
    below, _ = ss._calc_discovery_score(
        {"market_cap_b": 0, "avg_volume": 0, "analyst_count": 10},
        composite_score=7.19,
        score_threshold=7.2,
    )
    assert at_boundary == 10.0
    assert below == 0.0
    with pytest.raises(ValueError):
        ss._discovery_composite_threshold(scores, 1.0)
