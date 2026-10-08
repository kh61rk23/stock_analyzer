# ==============================================================
# tests/test_enable_flags_wiring.py
# ENABLE_* フラグが実装から実際に読まれていることの保証
# ==============================================================
# 【防ぐもの】config.py の ENABLE_* が定義だけされて実装から読まれず、
# 「True にしたのに動かない／False にしたのに止まらない」状態になること
# （config_audit 検査E と対応）。典型例:
#   - 似た名前の二重フラグがあり、実装は片方しか読まない
#   - ENABLE_D3_* が False でも実装側が常時適用してしまう
#
# 本テストは「フラグ名がその実装ファイルのソースに現れること」を検査する。
# 完全な動作保証ではないが、配線を剥がす変更を機械的に検出できる。
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

# (フラグ名, そのフラグを読むべきファイル)
WIRED_FLAGS = [
    ("ENABLE_PERFORMANCE_TRACKER",     "stock_analyzer_main.py"),
    ("ENABLE_ACTIVIST_FILTER",         "tdnet_monitor.py"),
    ("ENABLE_CONVICTION_SIZING",       "position_sizer.py"),
    ("ENABLE_INVESTMENT_JOURNAL",      "obsidian_sync.py"),
    ("ENABLE_VIX_TERM_STRUCTURE",      "options_monitor.py"),
    ("ENABLE_SKEW_INDEX",              "options_monitor.py"),
    ("ENABLE_IV_SPIKE_DETECT",         "options_monitor.py"),
    ("ENABLE_EARNINGS_ACCELERATION",   "signal_aggregator.py"),
    ("ENABLE_CONSERVATISM_SCORE",      "earnings_surprise.py"),
    ("ENABLE_IC_TRACKER",              "learning_audit_log.py"),
    ("ENABLE_D3_EDINET_SEGMENT",       "pearl_score_calculator.py"),
    ("ENABLE_D3_MIDTERM_INTEGRATION",  "pearl_score_calculator.py"),
    ("ENABLE_D3_CATALYST_INTEGRATION", "pearl_score_calculator.py"),
    ("ENABLE_LATENT_PEARL_BONUS",      "pearl_score_calculator.py"),
    # 先行セッションで配線したぶん（剥がれ検知のため一緒に見る）
    ("ENABLE_GEO_SIGNAL",              "base_agent.py"),
    ("ENABLE_JOGMEC_SCRAPER",          "geo_exposure_runner.py"),
    ("ENABLE_SEC_GEO_EXTRACTOR",       "geo_exposure_runner.py"),
    ("ENABLE_EDINET_EXPOSURE_EXTRACTOR", "geo_exposure_runner.py"),
    ("ENABLE_TAXONOMY_LOADER",         "taxonomy_loader.py"),
    ("ENABLE_TAXONOMY_COMPANY_MAPPER", "taxonomy_company_mapper.py"),
    ("ENABLE_INVESTMENT_DEPT",         "agent_packet_exporter.py"),
    ("ENABLE_TAX_OPTIMIZER",           "monthly_runner.py"),
    ("ENABLE_COMMODITY_ROTATION",      "monthly_runner.py"),
    ("ENABLE_ALLOCATION_META",         "monthly_runner.py"),
]

# 実装が存在せずフラグだけが先行しているもの（配線できない・意図的に除外）
# 実装した時点でこのリストから WIRED_FLAGS へ移すこと。
UNIMPLEMENTED_FLAGS = {
    # Treasury API から treasury_debt を作る処理が無い（読み手だけ存在）
    "ENABLE_TREASURY_DATA",
    # analytics.company_scores への書き込み処理が無い
    "ENABLE_GEO_SCORE_PERSIST",
}


def _cfg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(REPO / "config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("flag,filename", WIRED_FLAGS)
def test_flag_is_read_by_implementation(flag, filename):
    """フラグが実装ファイルから参照されていること。"""
    p = REPO / filename
    assert p.exists(), f"{filename} が存在しない"
    src = p.read_text(encoding="utf-8", errors="replace")
    assert flag in src, (
        f"{flag} が {filename} から読まれていない。"
        f"config に定義だけあって実装が無視する状態に戻っている"
    )


@pytest.mark.parametrize("flag,_f", WIRED_FLAGS)
def test_flag_exists_in_config(flag, _f):
    """配線対象のフラグが config.py に実在すること。"""
    assert hasattr(_cfg(), flag), f"config.py に {flag} が無い"


def test_no_duplicate_midterm_flag():
    """二重フラグの罠が復活していないこと。

    ENABLE_MIDTERM_TRACKER（旧名・誰も読まない）と
    ENABLE_MIDTERM_PLAN_TRACKER（正本）が同居すると、
    片方を切り替えても効かない事故が再発する。
    """
    cfg = _cfg()
    assert hasattr(cfg, "ENABLE_MIDTERM_PLAN_TRACKER"), "正本フラグが消えている"
    assert not hasattr(cfg, "ENABLE_MIDTERM_TRACKER"), (
        "旧名 ENABLE_MIDTERM_TRACKER が復活している"
        "（正本は ENABLE_MIDTERM_PLAN_TRACKER）"
    )


def test_unimplemented_flags_are_documented():
    """実装が無いフラグは config 側にその旨が書かれていること。"""
    src = (REPO / "config.py").read_text(encoding="utf-8")
    for flag in UNIMPLEMENTED_FLAGS:
        assert flag in src, f"config.py に {flag} が無い"
        idx = src.index(flag)
        window = src[max(0, idx - 700):idx + 100]
        assert "未実装" in window, (
            f"{flag} は実装が存在しないため、その旨を config.py に"
            f"明記しておくこと（誤って True にして期待外れになるのを防ぐ）"
        )
