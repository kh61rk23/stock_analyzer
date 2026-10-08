# options_strategy_advisor.py — 戦略ルール [OPT-1]（ネットワーク不要の純粋部のみ）
from conftest import load_module

opt = load_module("options_strategy_advisor")


class TestHV:
    def test_constant_price_zero_vol(self):
        assert opt.calc_hv([100.0] * 30) == 0.0

    def test_insufficient_data(self):
        assert opt.calc_hv([100, 101], window=20) is None


class TestRegime:
    # 実行時刻（米国時間外）ではATMのIVが取れないため、
    # IV/HV比ではなく HV の過去1年分位で判定する。
    def test_rich_cheap_neutral(self):
        assert opt.classify_hv_regime(85.0) == "rich"
        assert opt.classify_hv_regime(12.0) == "cheap"
        assert opt.classify_hv_regime(50.0) == "neutral"
        assert opt.classify_hv_regime(None) == "unknown"


class TestExpectedMove:
    def test_basic(self):
        assert opt.expected_move_pct(10.0, 200.0) == 5.0


class TestSuggestStrategies:
    BASE = {"ticker": "AAPL", "spot": 200.0, "iv": None, "hv": 0.25,
            "hv_percentile": 85.0,
            "expiry": "2026-07-17", "expected_move_pct": 5.0,
            "event_before_expiry": None, "bearish_context": False}

    def test_rich_portfolio_covered_call(self):
        m = {**self.BASE, "regime": "rich", "is_portfolio": True,
             "composite_score": 50}
        names = [s["strategy"] for s in opt.suggest_strategies(m)]
        assert names == ["covered_call"]

    def test_rich_high_score_csp(self):
        m = {**self.BASE, "regime": "rich", "is_portfolio": False,
             "composite_score": 80}
        names = [s["strategy"] for s in opt.suggest_strategies(m)]
        assert names == ["cash_secured_put"]

    def test_cheap_high_score_long_call(self):
        m = {**self.BASE, "regime": "cheap", "is_portfolio": False,
             "composite_score": 80, "iv": 0.15}
        names = [s["strategy"] for s in opt.suggest_strategies(m)]
        assert names == ["long_call"]

    def test_cheap_bearish_portfolio_protective_put(self):
        m = {**self.BASE, "regime": "cheap", "is_portfolio": True,
             "composite_score": 40, "bearish_context": True, "iv": 0.15}
        names = [s["strategy"] for s in opt.suggest_strategies(m)]
        assert names == ["protective_put"]

    def test_neutral_no_suggestion(self):
        m = {**self.BASE, "regime": "neutral", "is_portfolio": True,
             "composite_score": 90}
        assert opt.suggest_strategies(m) == []

    def test_event_warning_attached(self):
        m = {**self.BASE, "regime": "rich", "is_portfolio": True,
             "composite_score": 50,
             "event_before_expiry": {"label": "決算", "date": "2026-07-01"}}
        s = opt.suggest_strategies(m)[0]
        assert any("跨ぎ" in w for w in s["warnings"])

    def test_guardrail_no_naked_short(self):
        # どの条件でも裸売り系戦略名が出ないこと（鉄則8）
        for regime in ("rich", "cheap", "neutral"):
            for pf in (True, False):
                m = {**self.BASE, "regime": regime, "is_portfolio": pf,
                     "composite_score": 95}
                for s in opt.suggest_strategies(m):
                    assert s["strategy"] in (
                        "covered_call", "cash_secured_put",
                        "long_call", "protective_put")
                    assert s["guardrail_ok"]
