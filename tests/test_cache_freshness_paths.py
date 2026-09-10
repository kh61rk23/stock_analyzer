# cache_freshness が実際の産出物を見ていること
#
# 2026-09-10: 監視側のパス・時刻キーが産出側と5件ずれており、
# 2026-07-01 から71日連続で「強制再取得推奨」が出続けていた。
# 常時アラート＝情報ゼロなので、実態に合っていることを固定する。
import json
import types

import pytest

import cache_freshness_manager as cfm


@pytest.fixture
def dirs(tmp_path):
    d = tmp_path / "data"
    c = tmp_path / "cache"
    d.mkdir()
    c.mkdir()
    return types.SimpleNamespace(DATA_DIR=str(d), CACHE_DIR=str(c),
                                 _data=d, _cache=c)


def _report(cfg):
    return cfm.get_freshness_report(cfg, print_summary=False)


class TestTimestampKeys:
    def test_underscore_cached_at_is_recognized(self, dirs):
        """macro_snapshot_prev の実キーは `_cached_at`（先頭アンダースコア）"""
        (dirs._data / "macro_snapshot_prev.json").write_text(json.dumps(
            {"usdjpy": 153.4, "_cached_at": "2026-09-10 09:00:00"}),
            encoding="utf-8")
        r = _report(dirs)["macro_snapshot"]
        assert r["saved_at"] == "2026-09-10 09:00:00"
        assert not r["missing"]

    def test_underscore_fetched_at_is_recognized(self, dirs):
        """us_macro_cache の実キーは `_fetched_at`"""
        (dirs._cache / "us_macro_cache.json").write_text(json.dumps(
            {"composite_us_macro_score": 55, "_fetched_at": "2026-09-10 09:00:00"}),
            encoding="utf-8")
        assert _report(dirs)["us_macro"]["saved_at"] == "2026-09-10 09:00:00"

    def test_file_without_any_timestamp_falls_back_to_mtime(self, dirs):
        """時刻キーが無い形式でも「鮮度0」と誤判定しない"""
        (dirs._data / "macro_snapshot_prev.json").write_text(
            json.dumps({"usdjpy": 153.4}), encoding="utf-8")
        r = _report(dirs)["macro_snapshot"]
        assert r["saved_at"] is not None, "mtime フォールバックが効いていない"
        assert r["freshness"] > 0.5, "たった今書いたのに陳腐化扱い"


class TestSearchDirs:
    def test_cache_dir_is_searched_not_only_data_dir(self, dirs):
        """policy_radar_cache.json は CACHE_DIR 側に置かれる"""
        (dirs._cache / "policy_radar_cache.json").write_text(json.dumps(
            {"timestamp": "2026-09-10 09:00:00"}), encoding="utf-8")
        r = _report(dirs)["policy_radar"]
        assert not r["missing"], "CACHE_DIR を探していない"

    def test_gov_cache_glob_picks_the_newest(self, dirs):
        gov = dirs._data / "gov_cache"
        gov.mkdir()
        (gov / "data_0003_202608.json").write_text("{}", encoding="utf-8")
        (gov / "data_0003_202609.json").write_text("{}", encoding="utf-8")
        r = _report(dirs)["gov_data"]
        assert not r["missing"], "cpi.json 固定では実ファイルに当たらない"


class TestMissingVsStale:
    def test_absent_file_is_missing_not_force_refresh(self, dirs):
        """書き手が未実行なだけのものを「強制再取得推奨」に混ぜない"""
        r = _report(dirs)["rule_weights"]
        assert r["missing"] is True
        assert r["force_refresh"] is False
        assert r["stale"] is False


class TestHalfLifeMatchesCadence:
    @pytest.mark.parametrize("key", ["options_monitor", "macro_snapshot",
                                     "policy_radar", "us_macro"])
    def test_daily_modules_have_at_least_daily_half_life(self, key):
        """日次更新のキャッシュに24h未満の半減期を与えると常時アラートになる

        鮮度チェックは実行の序盤に走り、その時点のキャッシュは前回実行
        （＝約24時間前）のもの。半減期がそれより短いと構造的に
        必ず「強制再取得推奨」へ落ちる。
        """
        assert cfm.DEFAULT_HALF_LIFE_HOURS[key] >= 24.0


class TestNoGhostEntries:
    def test_every_watched_cache_has_a_half_life(self):
        for key in cfm.CACHE_FILE_MAP:
            assert key in cfm.DEFAULT_HALF_LIFE_HOURS, f"{key} の半減期が未定義"

    def test_us_sector_mapping_is_not_watched(self):
        """書き手が存在しないので監視対象から外してある"""
        assert "us_sector_mapping" not in cfm.CACHE_FILE_MAP
