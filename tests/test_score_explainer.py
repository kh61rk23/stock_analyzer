# score_explainer.py — 根拠文生成（explain_score は2026-06-11復旧分の回帰固定）
from conftest import load_module

se = load_module("score_explainer")


class TestExplainScore:
    def test_cached_reason_reused(self):
        out = se.explain_score("1016", 50.0, {"reason_text": "既存根拠"}, {})
        assert out == "既存根拠"

    def test_generates_from_macro_signals(self):
        out = se.explain_score(
            "1016", 72.5,
            {"macro_signals": [{"signal": "tailwind", "label": "円安",
                                "trigger": "USDJPY155"}]},
            {"usdjpy": 155.0})
        assert isinstance(out, str) and "円安" in out

    def test_empty_inputs_no_crash(self):
        out = se.explain_score("1016", 0.0, None, None)
        assert isinstance(out, str)


class TestBuildReasonText:
    def test_tailwind_and_headwind(self):
        out = se.build_reason_text(
            "1016", 60.0,
            [{"signal": "tailwind", "label": "A", "trigger": ""},
             {"signal": "headwind", "label": "B", "trigger": ""}])
        assert "A" in out and "B" in out

    def test_sector_signals_format(self):
        out = se.build_reason_text(
            "1016", 60.0,
            [{"label": "C", "trigger_value": "+5%",
              "sector_signals": {"1016": "tailwind"}}])
        assert "C" in out
