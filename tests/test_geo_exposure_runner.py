# ==============================================================
# tests/test_geo_exposure_runner.py
# 地政学シグナル供給側ランナーの実行間隔判定
# ==============================================================
# 【目的】geopolitics.company_country_exposure を埋める3抽出器
# （jogmec / sec_geo / edinet_exposure）を ENABLE_* と INTERVAL_DAYS に従って
# 呼び、間隔判定が正しく効くこと（＝毎日叩かないこと）を保証する。
# ==============================================================
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from geo_exposure_runner import EXTRACTORS, due                    # noqa: E402

TODAY = date(2026, 7, 25)


class TestDueInterval:
    def test_first_run_always_due(self):
        """記録が無い初回は必ず実行する（テーブルが空のままになるため）。"""
        ok, why = due({}, "jogmec", 30, TODAY)
        assert ok and "初回" in why

    def test_not_due_within_interval(self):
        state = {"jogmec": {"last_run": "2026-07-20"}}
        ok, why = due(state, "jogmec", 30, TODAY)
        assert not ok
        assert "5日" in why

    def test_due_at_exactly_interval(self):
        """境界: ちょうど間隔日数に達したら実行する。"""
        state = {"sec": {"last_run": "2026-06-25"}}
        ok, _ = due(state, "sec", 30, TODAY)
        assert ok

    def test_due_after_interval(self):
        state = {"edinet": {"last_run": "2026-05-01"}}
        ok, why = due(state, "edinet", 30, TODAY)
        assert ok and "経過" in why

    def test_corrupt_state_treated_as_first_run(self):
        """壊れた状態ファイルで止まらず、初回扱いで実行すること。"""
        for bad in ({"jogmec": {}}, {"jogmec": {"last_run": "not-a-date"}},
                    {"jogmec": None}):
            ok, _ = due(bad, "jogmec", 30, TODAY)
            assert ok

    def test_keys_are_independent(self):
        """1本走っても他の2本の間隔判定に影響しないこと。"""
        state = {"jogmec": {"last_run": "2026-07-25"}}
        assert not due(state, "jogmec", 30, TODAY)[0]
        assert due(state, "sec", 30, TODAY)[0]
        assert due(state, "edinet", 30, TODAY)[0]


class TestExtractorRegistry:
    def test_covers_three_producers(self):
        assert {e[0] for e in EXTRACTORS} == {"jogmec", "sec", "edinet"}

    def test_each_entry_names_existing_module_and_flag(self):
        """登録した関数名・モジュールが実在すること（幻のAPI検出）。"""
        import importlib
        for key, mod_name, fn_name, enable_flag, interval_flag, _ in EXTRACTORS:
            mod = importlib.import_module(mod_name)
            assert hasattr(mod, fn_name), f"{mod_name}.{fn_name} が無い"
            assert enable_flag.startswith("ENABLE_")
            assert interval_flag.endswith("_DAYS")

    def test_flags_exist_in_config(self):
        """ENABLE_* / INTERVAL_DAYS が config.py に実在すること。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "config", str(REPO / "config.py"))
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        for _, _, _, enable_flag, interval_flag, _ in EXTRACTORS:
            assert hasattr(cfg, enable_flag), f"config に {enable_flag} が無い"
            assert hasattr(cfg, interval_flag), f"config に {interval_flag} が無い"
