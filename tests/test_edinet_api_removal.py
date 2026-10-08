# 政府EDINET APIの撤去 と CSEスキップの可視化
#
# 金融庁 EDINET API（EDINET_API_KEY）は直取得の経路が
# ENABLE_EDINET_ROTATION / ENABLE_EDINET_DIRECT_FETCH /
# ENABLE_EXECUTIVE_TRANSITION の全てで停止済みでキーも撤去済み。EdinetDBへ一本化。
# api_key_health に「⬜ 未設定」として出続けないことを確かめる。
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestGovEdinetKeyRemoved:
    def test_config_no_longer_defines_it(self):
        import config
        assert not hasattr(config, "EDINET_API_KEY"), \
            "config に政府EDINETキーが残っている"

    def test_edinetdb_key_is_kept(self):
        """一本化先は残すこと"""
        import config
        assert hasattr(config, "EDINETDB_API_KEY")

    def test_runner_does_not_pass_it_through(self):
        src = (ROOT / "run_headless.py").read_text(encoding="utf-8")
        assert '"EDINET_API_KEY"' not in src

    def test_workflow_does_not_inject_the_secret(self):
        wf = (ROOT / ".github/workflows/daily_run.yml").read_text(encoding="utf-8")
        for line in wf.splitlines():
            if "EDINET_API_KEY" in line and "EDINETDB_API_KEY" not in line:
                pytest.fail(f"workflow に残っている: {line.strip()[:70]}")

    def test_health_check_no_longer_probes_it(self):
        import api_key_health as kh
        assert not hasattr(kh, "_probe_edinet")

    def test_health_check_still_probes_edinetdb(self):
        import api_key_health as kh
        assert hasattr(kh, "_probe_edinetdb")


class TestEstatProbeTimeout:
    """e-Stat は応答が遅く、既定タイムアウトだと本体は取得できてもプローブが ⚠️ になる"""

    def test_slow_timeout_is_defined_and_longer(self):
        import api_key_health as kh
        assert kh.PROBE_TIMEOUT_SLOW > kh.PROBE_TIMEOUT

    def test_estat_probe_uses_the_slow_timeout(self):
        src = (ROOT / "api_key_health.py").read_text(encoding="utf-8")
        block = src[src.index("def _probe_estat"):]
        block = block[:block.index("\ndef ", 1)]
        assert "PROBE_TIMEOUT_SLOW" in block, \
            "e-Stat が既定の短いタイムアウトのまま"


class TestPaidMediaSkipIsVisible:
    def test_bloomberg_is_removed_from_sources(self):
        """Bloomberg は CSE 経由で取得できないため対象外"""
        import paid_media_searcher as pm
        ids = [s["id"] for s in pm.PAID_MEDIA_SOURCES]
        assert "bloomberg" not in ids
        assert ids, "全媒体が消えている"

    def test_no_source_targets_bloomberg_domain(self):
        import paid_media_searcher as pm
        assert all("bloomberg" not in s.get("site", "")
                   for s in pm.PAID_MEDIA_SOURCES)

    def test_news_pipeline_logs_the_skip(self):
        """無効時は関数ごと呼ばれないため、呼び出し側で出す必要がある"""
        src = (ROOT / "news_pipeline.py").read_text(encoding="utf-8")
        gate = src[src.index("有料メディア記事を Google CSE で補完"):]
        gate = gate[:1200]
        assert "if not getattr(cfg, \"ENABLE_PAID_MEDIA_SEARCH\", False):" in gate
        assert "スキップ" in gate, "スキップした事実がログに出ない"
