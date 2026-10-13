from types import SimpleNamespace

import stock_real_estate as sre

CFG = SimpleNamespace(RE_BONUS_MIN=-2.0, RE_BONUS_MAX=3.0, JAPAN_SECTORS={
    "1029": {"name_ja": "証券・商品先物取引業"}, "1032": {"name_ja": "不動産業"}})


def test_sector_code_absorbs_comma_variant():
    """edinet.company の「証券、商品先物取引業」も 1029 に当たる。"""
    assert sre.sector_code_of("証券、商品先物取引業", CFG) == "1029"
    assert sre.sector_code_of("不動産業", CFG) == "1032"
    assert sre.sector_code_of(None, CFG) is None


def test_build_by_ticker_keeps_alphanumeric_codes_and_sector():
    rows = [
        {"ticker": "8801.T", "industry": "不動産業", "adj_pbr": 0.8, "asset_value_ratio": 0.2,
         "re_unrealized_gain": 1e11, "re_gain_flag": None, "area_risk": 0.1, "name": "三井不動産"},
        {"ticker": None, "sec_code4": "136A", "industry": "証券、商品先物取引業", "adj_pbr": None,
         "asset_value_ratio": None, "re_unrealized_gain": None, "re_gain_flag": None, "area_risk": 0.5},
    ]
    out = sre.build_by_ticker(rows, CFG)
    assert out["8801.T"]["sector_code"] == "1032" and out["8801.T"]["basis"] == "valuation"
    assert out["136A.T"] == {**out["136A.T"], "bonus": -0.5, "basis": "area_only", "sector_code": "1029"}
    s = sre.summarize(out)
    assert s["count"] == 2 and s["basis_counts"] == {"valuation": 1, "area_only": 1} and s["nonzero"] == 2
