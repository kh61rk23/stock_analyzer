# strategy_output_schema.py — Core/Satellite分類 [⑥]
from conftest import load_module

sos = load_module("strategy_output_schema")


class TestStrategyLabel:
    def test_returns_string(self, tmp_cfg):
        label = sos.determine_strategy_label(
            {"ticker": "7203.T", "composite_score": 80.0,
             "quality_score": 75.0, "pearl_score": 0.0}, tmp_cfg)
        assert isinstance(label, str) and label

    def test_enrich_adds_strategy_fields(self, tmp_cfg):
        c = {"ticker": "7203.T", "composite_score": 80.0,
             "quality_score": 75.0}
        out = sos.enrich_candidate_with_strategy(dict(c), tmp_cfg)
        assert "strategy_label" in out

    def test_enrich_all_returns_tuple(self, tmp_cfg):
        cands, summary = sos.enrich_all_candidates(
            [{"ticker": "7203.T", "composite_score": 80.0}], tmp_cfg)
        assert isinstance(cands, list) and isinstance(summary, dict)
