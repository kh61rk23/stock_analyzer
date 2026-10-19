"""geo_signal の GEO_TAXONOMY_SOURCE（legacy / seed）切り替えのテスト。"""
import datetime
import types

import pytest

import config
import geo_signal


class _FakeWrapper:
    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def get_taxonomy_scores_bulk(self, codes):
        self.calls.append(list(codes))
        return {c: self.scores[c] for c in codes if c in self.scores}


def _cfg(source, propagation=None):
    return types.SimpleNamespace(
        GEO_TAXONOMY_SOURCE=source,
        GEO_TAXONOMY_PROPAGATION=propagation or {},
        GEO_IMPACT_SCALE=0.30,
        GEO_IMPACT_MAX=15.0,
        GEO_RISK_CACHE_TTL_SEC=86400,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    geo_signal._TAXONOMY_SCORE_CACHE.clear()
    geo_signal._TAXONOMY_SCORE_FETCHED.clear()
    monkeypatch.setattr(geo_signal, "_BQ_WRAPPER_CACHE", None)
    monkeypatch.setattr(geo_signal, "_CACHE_STAMP", 10 ** 12)
    monkeypatch.setattr(geo_signal, "_CACHE_DATE", datetime.date.today().isoformat())
    yield
    geo_signal._TAXONOMY_SCORE_CACHE.clear()
    geo_signal._TAXONOMY_SCORE_FETCHED.clear()


EXPOSURE = [{"commodity_code": "lithium", "exposure_strength": 0.5,
             "composite_risk_score": 60.0, "country_code": "CHL"}]


def test_default_source_is_legacy():
    assert config.GEO_TAXONOMY_SOURCE == "legacy"


def test_legacy_propagation_unchanged(monkeypatch):
    cfg = _cfg("legacy", {"lithium": ["ev_battery"]})
    # 電気機器(1016)は ev_battery の手書き表に載っている → 関連性 1.0
    v = geo_signal._calc_taxonomy_propagation(EXPOSURE, "1016", "JP", cfg, ticker="6501.T")
    assert v == round(0.5 * 0.6 * 0.30 * 0.3 * 1.0, 4)
    # ticker を渡しても legacy では関与度を見ない
    geo_signal._TAXONOMY_SCORE_CACHE["6501"] = {"ev_battery": 0.0}
    assert geo_signal._calc_taxonomy_propagation(EXPOSURE, "1016", "JP", cfg, ticker="6501.T") == v


def test_seed_commodity_map_is_reverse_of_related_commodities():
    m = geo_signal._seed_commodity_nodes()
    assert "oil_gas_upstream" in m["crude_oil"]
    assert all(m[k] == sorted(set(m[k])) for k in m)
    # 手書き辞書にあった実在しないノードは seed には出てこない
    flat = {n for v in m.values() for n in v}
    assert "stainless_steel" not in flat and "petrochemical" not in flat


def test_seed_uses_company_scores(monkeypatch):
    monkeypatch.setattr(geo_signal, "_seed_commodity_nodes", lambda: {"lithium": ["a", "b"]})
    geo_signal._TAXONOMY_SCORE_CACHE["6501"] = {"a": 0.5}
    v = geo_signal._calc_taxonomy_propagation(
        EXPOSURE, "9999", "JP", _cfg("seed"), ticker="6501.T")
    # a は 0.5・b は行が無い(0)
    assert v == round(0.5 * 0.6 * 0.30 * 0.3 * 0.5, 4)


def test_seed_falls_back_to_legacy_table_without_rows(monkeypatch):
    monkeypatch.setattr(geo_signal, "_seed_commodity_nodes", lambda: {"lithium": ["ev_battery"]})
    v = geo_signal._calc_taxonomy_propagation(
        EXPOSURE, "1016", "JP", _cfg("seed"), ticker="6501.T")
    assert v == round(0.5 * 0.6 * 0.30 * 0.3 * 1.0, 4)


def test_prefetch_taxonomy_scores_one_query_and_remembers_empty(monkeypatch):
    w = _FakeWrapper({"6501": {"x": 0.4}})
    monkeypatch.setattr(geo_signal, "_BQ_WRAPPER_CACHE", w)
    n = geo_signal.prefetch_taxonomy_scores(["6501.T", "7203.T"], _cfg("seed"))
    assert n == 1 and len(w.calls) == 1
    assert geo_signal._TAXONOMY_SCORE_CACHE == {"6501": {"x": 0.4}}
    geo_signal.prefetch_taxonomy_scores(["6501.T", "7203.T"], _cfg("seed"))
    assert len(w.calls) == 1, "行の無い銘柄も引き済みとして再問い合わせしない"


def test_prefetch_taxonomy_scores_noop_in_legacy(monkeypatch):
    w = _FakeWrapper({"6501": {"x": 0.4}})
    monkeypatch.setattr(geo_signal, "_BQ_WRAPPER_CACHE", w)
    assert geo_signal.prefetch_taxonomy_scores(["6501.T"], _cfg("legacy")) == 0
    assert w.calls == []


def test_prefetch_exposures_loads_taxonomy_once_for_jp_seed(monkeypatch):
    w = _FakeWrapper({"6501": {"x": 0.4}})
    monkeypatch.setattr(geo_signal, "_BQ_WRAPPER_CACHE", w)
    monkeypatch.setattr(geo_signal, "_get_raw_bq", lambda *_a, **_k: None)
    geo_signal.prefetch_exposures(["6501.T", "7203.T"], market="JP", cfg=_cfg("seed"))
    assert len(w.calls) == 1
    geo_signal.prefetch_exposures(["6501.T"], market="US", cfg=_cfg("seed"))
    assert len(w.calls) == 1, "US では taxonomy を引かない"


def test_calc_bonus_does_not_query_bq_in_loop(monkeypatch):
    w = _FakeWrapper({"6501": {"ev_battery": 1.0}})
    monkeypatch.setattr(geo_signal, "_BQ_WRAPPER_CACHE", w)
    monkeypatch.setattr(geo_signal, "_seed_commodity_nodes", lambda: {"lithium": ["ev_battery"]})
    monkeypatch.setattr(geo_signal, "_get_risk_scores_for_countries", lambda *_a, **_k: {"CHL": 60.0})
    cfg = _cfg("seed")
    cfg.ENABLE_GEO_SIGNAL = True
    cfg.GEO_IMPACT_MIN = -15.0
    cfg.GEO_COMMODITY_DIRECTION_JP = {}
    geo_signal.prefetch_taxonomy_scores(["6501.T"], cfg)
    before = len(w.calls)
    for _ in range(5):
        geo_signal.calc_geo_impact_bonus_jp("6501.T", "1016", cfg,
                                            prefetched_exposures=EXPOSURE)
    assert len(w.calls) == before
