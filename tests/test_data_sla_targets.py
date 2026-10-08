# data_sla_check の検査対象が産出側の実パスと一致していること
#
# 既定パスが実在しないファイルを指すと、毎日「SLA違反」を出し続ける。
#   news       : 実体は news_cache/news_{日付}.json（ディレクトリ）。
#                `news_cache.json` は生成されない
#   financials : CACHE_DIR/edinet は退役済み（ENABLE_EDINET_ROTATION=False）
# 監視が実態からずれると常時アラート化して本物の違反を隠す。
import json
import types
from datetime import datetime, timedelta

import pytest

import data_reliability as dr


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "data"
    c = tmp_path / "cache"
    d.mkdir()
    c.mkdir()
    return types.SimpleNamespace(DATA_DIR=str(d), CACHE_DIR=str(c),
                                 DATA_SLA_DAYS={"price": 2, "financials": 100,
                                                "news": 3},
                                 DATA_SLA_TARGETS={},
                                 _data=d, _cache=c)


def _fresh(path, content="{}"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestDefaultTargets:
    def test_all_green_when_real_artifacts_exist(self, cfg):
        _fresh(cfg._data / "score_history.json")
        _fresh(cfg._data / "news_cache" / "news_2026-09-10.json")
        _fresh(cfg._data / "edinetdb_rotation_meta.json")
        r = dr.run_data_sla_check(cfg, {})
        assert r["violation_count"] == 0, r["violations"]
        assert r["checked"] == 3

    def test_news_target_is_the_directory_not_a_flat_file(self, cfg):
        """news_cache.json というファイルは産出側が作らない"""
        _fresh(cfg._data / "score_history.json")
        _fresh(cfg._data / "edinetdb_rotation_meta.json")
        _fresh(cfg._data / "news_cache.json")      # 旧・誤ったパス
        r = dr.run_data_sla_check(cfg, {})
        kinds = {v["kind"] for v in r["violations"]}
        assert "news" in kinds, "旧パスを見てしまっている"

    def test_financials_uses_the_current_ingestor_state(self, cfg):
        """退役した CACHE_DIR/edinet ではなく現行の状態ファイルを見る"""
        _fresh(cfg._data / "score_history.json")
        _fresh(cfg._data / "news_cache" / "news_2026-09-10.json")
        (cfg._cache / "edinet").mkdir()            # 旧パスだけ用意
        r = dr.run_data_sla_check(cfg, {})
        kinds = {v["kind"] for v in r["violations"]}
        assert "financials" in kinds, "旧パスで緑になっている"


class TestStatusIsDistinguished:
    def test_missing_and_stale_are_separate_statuses(self, cfg):
        old = datetime.now() - timedelta(days=400)
        p = cfg._data / "edinetdb_rotation_meta.json"
        _fresh(p)
        import os
        os.utime(p, (old.timestamp(), old.timestamp()))
        r = dr.run_data_sla_check(cfg, {})
        by_kind = {v["kind"]: v["status"] for v in r["violations"]}
        assert by_kind.get("financials") == "stale"
        assert by_kind.get("price") == "missing"
        assert by_kind.get("news") == "missing"
