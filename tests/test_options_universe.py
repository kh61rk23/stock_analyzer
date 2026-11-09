"""オプション提案のユニバース：保有銘柄が多くても米国スクリーナー上位が入り、米国外の銘柄は入らないこと。"""
from types import SimpleNamespace

import options_strategy_advisor as oa


def _us(n=8):
    cands = [{"ticker": f"U{i}", "composite_score": 90 - i, "pass_filters": True} for i in range(n)]
    cands.insert(0, {"ticker": "005930.KS", "composite_score": 99, "pass_filters": True})   # 米国外
    cands.append({"ticker": "FAIL", "composite_score": 100, "pass_filters": False})          # フィルター落ち
    return {"us_screener": {"candidates": cands}}


def test_screener_top_survives_the_cap_even_with_many_holdings():
    cfg = SimpleNamespace(OPTIONS_INDEX_ETFS=["SPY", "QQQ"], OPTIONS_MAX_UNIVERSE=10)
    uni = oa._build_universe(cfg, _us())
    tickers = [u["ticker"] for u in uni]
    assert len(tickers) == 10
    assert tickers[:2] == ["SPY", "QQQ"]
    assert tickers[2:7] == ["U0", "U1", "U2", "U3", "U4"]
    assert "005930.KS" not in tickers and "FAIL" not in tickers
    assert uni[2]["composite_score"] == 90          # 条件に使う総合点が入っている


def test_holding_in_screener_keeps_its_score():
    cfg = SimpleNamespace(OPTIONS_INDEX_ETFS=[], OPTIONS_MAX_UNIVERSE=50)
    res = {"us_screener": {"candidates": [{"ticker": "NVDA", "composite_score": 77, "pass_filters": False}]}}
    uni = {u["ticker"]: u for u in oa._build_universe(cfg, res)}
    assert uni["NVDA"]["is_portfolio"] is True and uni["NVDA"]["composite_score"] == 77
