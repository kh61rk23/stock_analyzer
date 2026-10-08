# BQへ書くはずのモジュールが実際に実行されるかの配線契約
# 3系統の配線漏れ（空テーブルになる原因）を固定する。
import json
from pathlib import Path

import config
from conftest import load_module

REPO = Path(config.__file__).resolve().parent


def _in_mode(key, mode):
    """stock_analyzer_main._in_mode と同じ判定"""
    daily = set(config.DAILY_MODULES)
    weekly = set(config.WEEKLY_MODULES)
    if mode == "all":
        return True
    if mode == "daily":
        return key in daily
    if mode == "weekly":
        return key in daily or key in weekly
    return True


class TestWeeklyGateKeys:
    """mode名を渡すと常にFalseになる誤用（_in_mode("weekly")）の再発防止"""

    def test_mode_names_are_not_module_keys(self):
        for name in ("weekly", "daily", "all"):
            assert name not in config.DAILY_MODULES
            assert name not in config.WEEKLY_MODULES

    def test_main_never_gates_on_a_mode_name(self):
        src = (REPO / "stock_analyzer_main.py").read_text(encoding="utf-8")
        # "all" も許可キー（13F・deep_dive_trigger が _in_mode("all") で使う）
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for name in ("weekly", "daily", "all"):
            assert f'_in_mode("{name}")' not in code, (
                f'_in_mode("{name}") はモード名でありモジュールキーではない。'
                "判定が常に False になり、そのブロックは一度も実行されない"
            )

    def test_every_gate_key_is_registered_or_retired(self):
        """_in_mode に渡すキーは DAILY/WEEKLY_MODULES のどちらかにあること。

        どちらにも無いキーは日次・週次とも常に False＝一度も動かない。
        意図して止めている（廃止済み）ものだけを下に列挙する。
        """
        import re
        src = (REPO / "stock_analyzer_main.py").read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        keys = set(re.findall(r'_in_mode\("([^"]+)"\)', code))
        retired = {"deep_dive_trigger",          # 受け手の EDINET ローテ廃止
                   "facility_expansion", "job_posting",
                   "jp_us_sector_correlation"}   # 廃止済み
        registered = set(config.DAILY_MODULES) | set(config.WEEKLY_MODULES)
        assert sorted(keys - registered - retired) == []

    def test_bq_writing_monitors_run_in_weekly(self):
        # BQ us.job_signal_cache / executive_transitions / facility_signals の
        # 唯一の書き込み経路。weekly で通らなければ永久に0行になる。
        # job_posting / facility_expansion は廃止済み（一覧から除外）
        for key in ("executive_transition",):
            assert _in_mode(key, "weekly") is True, f"{key} が週次で実行されない"
            assert _in_mode(key, "daily") is False, f"{key} は日次では走らない想定"
        for key in ("job_posting", "facility_expansion"):
            assert _in_mode(key, "weekly") is False, f"{key} は廃止済みなのに動いている"


class TestNedoWiring:
    """edinet.policy_support の書き込み経路（producer）の配線"""

    def test_nedo_is_registered_and_enabled(self):
        assert _in_mode("nedo", "weekly") is True
        assert getattr(config, "ENABLE_NEDO_SCRAPER", False) is True

    def test_main_calls_the_scraper_entrypoint(self):
        src = (REPO / "stock_analyzer_main.py").read_text(encoding="utf-8")
        assert "run_nedo_selection_scraper" in src, (
            "読む側 base_agent.get_policy_support_bonus() だけが配線され、"
            "書く側が呼ばれないと policy_support は0行のままになる"
        )

    def test_results_key_is_under_contract(self):
        rs = load_module("results_schema")
        assert "nedo_selection" in rs.SCHEMA


class TestOntologySignalEvents:
    """ontology.signal_event が0行だった構造不一致の再発防止"""

    def test_screener_history_shape_is_a_list_per_date(self, tmp_path):
        oe = load_module("ontology_exporter")

        class _Cfg:
            DATA_DIR = tmp_path
            INSIGHT_DIR = tmp_path
            CACHE_DIR = tmp_path

        # 実データと同じ「日付→通過銘柄リスト」形式
        (tmp_path / "screener_history.json").write_text(json.dumps({
            "2026-09-08": [
                {"ticker": "6501.T", "composite_score": 72.2,
                 "sector_code": "1016"},
            ]
        }), encoding="utf-8")

        rows = oe.build_signal_events(_Cfg)
        assert len(rows) == 1, "list 形式の履歴からイベントが作られていない"
        assert rows[0]["ticker"] == "6501.T"
        assert rows[0]["signal_category"] == "screener_pass"

    def test_dict_shape_still_supported(self, tmp_path):
        """旧 {"candidates": [...]} 形式でも壊れないこと"""
        oe = load_module("ontology_exporter")

        class _Cfg:
            DATA_DIR = tmp_path
            INSIGHT_DIR = tmp_path
            CACHE_DIR = tmp_path

        (tmp_path / "screener_history.json").write_text(json.dumps({
            "2026-09-08": {"candidates": [
                {"ticker": "7203.T", "composite_score": 60.0,
                 "pass_filters": True},
            ]}
        }), encoding="utf-8")

        rows = oe.build_signal_events(_Cfg)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "7203.T"
