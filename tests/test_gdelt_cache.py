# country_risk_updater — GDELT「最後に取れた値」永続キャッシュ（純粋ロジック）
# 2026-07-17追加（Issue J-1）:
#   GitHub Actions共有IPがGDELTに恒常的にブロックされ全件429で全滅する日が
#   常態化したため、取得失敗時は永続キャッシュ値を減衰付きで代用する。
#   本テストは decayed_score() 等の純粋関数のみを対象とし、ネットワーク・
#   ファイルI/Oには依存しない（cfg.CACHE_DIR は tmp_cfg でtmp_pathに隔離）。
from datetime import date

from conftest import load_module

cru = load_module("country_risk_updater")


class TestDecayedScore:
    def test_decay_4_days(self):
        # score=50, 4日前 → 50 × (1 - 0.05×4) = 50 × 0.8 = 40
        assert cru.decayed_score(50.0, 4) == 40.0

    def test_decay_zero_days_no_change(self):
        assert cru.decayed_score(50.0, 0) == 50.0

    def test_decay_lower_bound_clamped_to_zero(self):
        # rate×days_old > 1 となる極端な経過日数でも下限0でクランプ
        assert cru.decayed_score(50.0, 100) == 0.0

    def test_decay_custom_rate(self):
        # rate=0.1, 3日前 → 50 × (1 - 0.3) = 35
        assert cru.decayed_score(50.0, 3, rate=0.1) == 35.0


class TestGdeltCachedOrFallback:
    def test_no_cache_entry_returns_minus_one(self):
        score, days_old = cru._gdelt_cached_or_fallback({}, "IRN", date(2026, 7, 17))
        assert score == -1.0
        assert days_old is None

    def test_cache_hit_returns_decayed_score(self):
        cache = {"IRN": {"score": 50.0, "as_of": "2026-07-13"}}
        # 2026-07-17 - 2026-07-13 = 4日前
        score, days_old = cru._gdelt_cached_or_fallback(cache, "IRN", date(2026, 7, 17))
        assert days_old == 4
        assert score == 40.0

    def test_malformed_cache_entry_returns_minus_one(self):
        cache = {"IRN": {"score": 50.0}}  # as_of 欠落
        score, days_old = cru._gdelt_cached_or_fallback(cache, "IRN", date(2026, 7, 17))
        assert score == -1.0
        assert days_old is None


class TestUpdateGdeltCache:
    def test_success_updates_cache(self):
        cache: dict = {}
        cru._update_gdelt_cache(cache, "USA", 42.0, date(2026, 7, 17))
        assert cache["USA"] == {"score": 42.0, "as_of": "2026-07-17"}

    def test_update_overwrites_existing_entry(self):
        cache = {"USA": {"score": 10.0, "as_of": "2026-07-01"}}
        cru._update_gdelt_cache(cache, "USA", 55.5, date(2026, 7, 17))
        assert cache["USA"] == {"score": 55.5, "as_of": "2026-07-17"}


class TestGdeltCacheFileRoundtrip:
    def test_save_then_load_roundtrip(self, tmp_cfg):
        cache = {"USA": {"score": 42.0, "as_of": "2026-07-17"}}
        cru._save_gdelt_cache(tmp_cfg, cache)
        loaded = cru._load_gdelt_cache(tmp_cfg)
        assert loaded == cache

    def test_load_missing_file_returns_empty_dict(self, tmp_cfg):
        assert cru._load_gdelt_cache(tmp_cfg) == {}
