# size_decay_manager.py — Size Tier分類・tier別ウェイト（2026-06-11復旧分の回帰固定）
from conftest import load_module

sdm = load_module("size_decay_manager")


class TestClassify:
    def test_large_jp(self):
        assert sdm.classify_size_tier(40e12, "JP") == "Large"

    def test_nano_none(self):
        assert sdm.classify_size_tier(None, "JP") == "Nano"

    def test_nano_zero(self):
        assert sdm.classify_size_tier(0, "JP") == "Nano"


class TestResolveSizeTier:
    """★ 2026-06-11 バグ修正（未実装API）の回帰テスト"""
    def test_raw_priority(self):
        assert sdm.resolve_size_tier(ticker="7203.T",
                                     market_cap_raw=40e12, market="JP") == "Large"

    def test_fallback_mid_when_unknown(self):
        # 時価総額不明はNanoでなくMid（極端ウェイト回避の安全側選択）
        assert sdm.resolve_size_tier(ticker="X") == "Mid"

    def test_b_unit_used_when_raw_missing(self):
        tier = sdm.resolve_size_tier(market_cap_b=0.5, market="JP")
        assert tier in ("Small", "Micro", "Nano")


class TestTierComposite:
    def test_weights_sum_to_one(self):
        for tier in ("Large", "Mid", "Small", "Micro", "Nano"):
            w = sdm.get_composite_weights(tier)
            assert abs(sum(w.values()) - 1.0) < 0.01, tier

    def test_composite_bounds(self):
        comp, w = sdm.calc_tier_composite_score(60, 50, 70, "Large")
        assert 0.0 <= comp <= 100.0
        assert set(w) == {"sector_score", "value_score", "quality_score"}

    def test_unknown_tier_falls_back_to_mid(self):
        _, w = sdm.calc_tier_composite_score(60, 50, 70, "存在しないTier")
        assert w == sdm.get_composite_weights("Mid")
