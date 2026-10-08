# オプション戦略のHV判定と、JPX信用残高の取得先の回帰テスト
import sys
import types

import pytest


# ── オプション: 米国時間外はIVが取れず毎日「提案0件」だった ─────────────
class TestOptionsHvRegime:
    def _closes(self, calm_days=200, shock=False):
        # 穏やかな値動き → 末尾だけ荒くすると高ボラになる
        closes = [100.0 + (i % 3) * 0.1 for i in range(calm_days)]
        if shock:
            closes += [100.0 + (5.0 if i % 2 else -5.0) for i in range(25)]
        else:
            closes += [100.0 + (i % 3) * 0.1 for i in range(25)]
        return closes

    def test_percentile_and_regime(self):
        import options_strategy_advisor as m
        closes = self._closes(shock=True)
        hv = m.calc_hv(closes, 20)
        series = m.calc_hv_series(closes, 20)
        pctl = m.hv_percentile(hv, series)
        assert pctl is not None and pctl > 70
        assert m.classify_hv_regime(pctl) == "rich"
        assert m.classify_hv_regime(5.0) == "cheap"
        assert m.classify_hv_regime(50.0) == "neutral"
        assert m.classify_hv_regime(None) == "unknown"

    def test_metrics_survive_missing_iv(self, monkeypatch):
        pd = pytest.importorskip("pandas")
        import options_strategy_advisor as m
        closes = self._closes(shock=True)
        frame = pd.DataFrame({"Close": closes})
        chain = types.SimpleNamespace(
            calls=pd.DataFrame({"strike": [100.0], "impliedVolatility": [0.0001],
                                "lastPrice": [2.0]}),
            puts=pd.DataFrame({"strike": [100.0], "impliedVolatility": [0.0001],
                               "lastPrice": [2.0]}),
        )
        ticker = types.SimpleNamespace(
            history=lambda period: frame,
            options=["2026-12-18"],
            option_chain=lambda e: chain,
        )
        monkeypatch.setitem(sys.modules, "yf_cache",
                            types.SimpleNamespace(Ticker=lambda tk: ticker))
        reasons = {}
        cfg = types.SimpleNamespace(OPTIONS_HV_WINDOW_DAYS=20,
                                    OPTIONS_HV_RICH_PCTL=70.0, OPTIONS_HV_CHEAP_PCTL=30.0)
        out = m._fetch_metrics(cfg, {"ticker": "SPY", "composite_score": 80}, {}, reasons)
        assert reasons == {}                 # 旧: {"IV欠損": 1} で提案0件
        assert out["iv"] is None             # 時間外のIVは判定に使わない
        assert out["regime"] == "rich" and out["hv_percentile"] > 70

    def test_rationale_mentions_hv(self):
        import options_strategy_advisor as m
        cfg = types.SimpleNamespace(OPTIONS_HV_RICH_PCTL=70.0, OPTIONS_MIN_SCORE_FOR_CSP=70.0)
        out = m.suggest_strategies(
            {"ticker": "SPY", "regime": "rich", "hv": 0.30, "hv_percentile": 85.0,
             "is_portfolio": True, "composite_score": 75}, cfg)
        assert [s["strategy"] for s in out] == ["covered_call", "cash_secured_put"]
        assert "HV=30%" in out[0]["rationale"] and "85分位" in out[0]["rationale"]


# ── JPX: 信用残高CSVが配信終了し、毎週 yfinance の代理値に落ちていた ─────
_JPX_HTML = """
<a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026090400.pdf">9/4</a>
<a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026091100.pdf">9/11</a>
<a href="/markets/statistics-equities/margin/tvdivq0000001rnl-att/t13vrt000001ixeg.pdf">説明</a>
"""


class TestJpxWeeklyPdf:
    def test_discovers_latest_first(self):
        import credit_data as m
        urls = m.discover_jpx_weekly_pdfs(fetch=lambda url: _JPX_HTML)
        assert urls == [
            "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026091100.pdf",
            "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026090400.pdf",
        ]
        assert m.discover_jpx_weekly_pdfs(fetch=lambda url: None) == []

    def test_number_parsing(self):
        import credit_data as m
        assert m._jpx_number("8,900") == 8900
        assert m._jpx_number("▲ 400") == -400
        assert m._jpx_number("0") == 0
        assert m._jpx_number("—") is None

    def test_relative_flags_ignore_zero_short(self):
        import credit_data as m
        cfg = types.SimpleNamespace(CREDIT_SQUEEZE_PCTL=10.0, CREDIT_OVERHEAT_PCTL=90.0,
                                    CREDIT_CAUTION_PCTL=75.0)
        records = [{"margin_ratio": float(i) / 10 + 0.5, "short_shares": 100}
                   for i in range(200)]
        records += [{"margin_ratio": 99.99, "short_shares": 0} for _ in range(50)]
        out = m.assign_relative_flags(records, cfg)
        flags = [r["flag"] for r in out]
        # 売残ゼロは判定対象外、過熱・踏み上げは上位/下位10%程度に収まる
        assert all(r["flag"] == "normal" for r in out if r["short_shares"] == 0)
        assert 15 <= flags.count("overheated") <= 30
        assert 15 <= flags.count("squeeze") <= 30

    def test_absolute_thresholds_when_few_records(self):
        import credit_data as m
        records = [{"margin_ratio": 0.5, "short_shares": 10},
                   {"margin_ratio": 20.0, "short_shares": 10},
                   {"margin_ratio": 3.0, "short_shares": 10}]
        out = m.assign_relative_flags(records, None)
        assert [r["flag"] for r in out] == ["squeeze", "overheated", "normal"]
