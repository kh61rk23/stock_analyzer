"""U-8: config BLOCK 61 MARKET_PROFILE の契約・挙動不変テスト。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import screener_common as sc  # noqa: E402


REFERENCE_FIELDS = ("size_thresholds", "slippage_bps", "defensive_sectors")


def test_market_profile_has_jp_us_contract():
    assert set(config.MARKET_PROFILE) == {"JP", "US"}
    expected_fields = {
        "suffix", "revision_multiplier", "revision_cap", "size_thresholds",
        "slippage_bps", "defensive_sectors", "sector_taxonomy",
    }
    assert set(config.MARKET_PROFILE["JP"]) == expected_fields
    assert set(config.MARKET_PROFILE["US"]) == expected_fields


def test_market_profile_reference_names_exist_in_config():
    for profile in config.MARKET_PROFILE.values():
        for field in REFERENCE_FIELDS:
            setting_name = profile[field]
            assert isinstance(setting_name, str)
            assert hasattr(config, setting_name), f"config.{setting_name} が存在しない"


def test_market_profile_is_defined_once_in_block_61():
    source = (REPO / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = [
        node for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and (
            isinstance(getattr(node, "target", None), ast.Name)
            and node.target.id == "MARKET_PROFILE"
            or any(
                isinstance(target, ast.Name) and target.id == "MARKET_PROFILE"
                for target in getattr(node, "targets", [])
            )
        )
    ]
    assert len(definitions) == 1
    assert "BLOCK 61: 市場別プロファイル索引" in source


def test_revision_score_uses_runtime_market_profile():
    profiles = {
        market: dict(profile) for market, profile in config.MARKET_PROFILE.items()
    }
    profiles["US"]["revision_multiplier"] = 4.0
    profiles["US"]["revision_cap"] = 1.5
    cfg = SimpleNamespace(
        MARKET_PROFILE=profiles,
        ANALYST_REVISION_WINDOW_DAYS=30,
        PEAD_TRIGGER_MIN_REVISION=0.3,
    )
    assert sc.calc_revision_score("AAPL", {"rec_mean": 1.0}, "US", cfg) == 1.5


def test_existing_jp_us_revision_outputs_are_unchanged():
    cfg = SimpleNamespace(
        MARKET_PROFILE=config.MARKET_PROFILE,
        ANALYST_REVISION_WINDOW_DAYS=30,
        PEAD_TRIGGER_MIN_REVISION=0.3,
    )
    assert sc.calc_revision_score("6758.T", {"rec_mean": 1.0}, "JP", cfg) == 3.0
    assert sc.calc_revision_score("AAPL", {"rec_mean": 1.0}, "US", cfg) == 5.0
    assert sc.calc_revision_score("X", {"rec_mean": 1.0}, "EU", cfg) == 3.0
