"""U-7残とBatch 10の統計共通化に対する等価性・委譲テスト。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import finance_logic  # noqa: E402
import jp_us_sector_bridge as bridge  # noqa: E402
import jp_us_sector_correlation as correlation  # noqa: E402
import score_history_io  # noqa: E402
import screener_sector_heatmap as heatmap  # noqa: E402
import sector_rotation_detector as rotation  # noqa: E402
import self_evaluator  # noqa: E402


HISTORY_LOADERS = (
    bridge._load_score_history,
    heatmap._load_score_history,
    self_evaluator._load_score_history,
)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_score_history_prefers_data_dir_parent_insights(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = SimpleNamespace(DATA_DIR=tmp_path / "custom" / "data")
    preferred = {"2026-07-27": {"1001": {"score": 61.0}}}
    fallback = {"2026-07-26": {"1001": {"score": 50.0}}}
    _write_json(tmp_path / "custom" / "insights" / "score_history.json", preferred)
    _write_json(tmp_path / "stock_data" / "insights" / "score_history.json", fallback)

    assert score_history_io.load_score_history(cfg) == preferred
    assert all(loader(cfg) == preferred for loader in HISTORY_LOADERS)


def test_score_history_corrupt_first_candidate_falls_through(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = SimpleNamespace(DATA_DIR=tmp_path / "custom" / "data")
    first = tmp_path / "custom" / "insights" / "score_history.json"
    first.parent.mkdir(parents=True)
    first.write_text("{broken", encoding="utf-8")
    fallback = {"2026-07-27": {"1016": {"score": 72.0}}}
    _write_json(tmp_path / "stock_data" / "insights" / "score_history.json", fallback)

    assert score_history_io.load_score_history(cfg) == fallback


def test_score_history_missing_returns_empty_dict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = SimpleNamespace(DATA_DIR=tmp_path / "custom" / "data")
    assert score_history_io.load_score_history(cfg) == {}
    assert all(loader(cfg) == {} for loader in HISTORY_LOADERS)


def test_only_identical_three_loaders_delegate_to_common_module():
    for module in (bridge, heatmap, self_evaluator):
        source = (REPO / f"{module.__name__}.py").read_text(encoding="utf-8")
        assert "return score_history_io.load_score_history(cfg)" in source
    for filename in (
        "ml_score_corrector.py", "score_prior_manager.py",
        "sector_rotation_detector.py", "jp_us_sector_correlation.py",
    ):
        source = (REPO / filename).read_text(encoding="utf-8")
        assert "score_history_io" not in source


@pytest.mark.parametrize("values,expected", [
    ([], 0.0),
    ([1.0, 2.0], 0.0),
    ([1.0, 2.0, 3.0], 1.0),
    ([3.0, 2.0, 1.0], -1.0),
    ([5.0, 5.0, 5.0], 0.0),
    ([1.2, 1.8, 2.9, 4.1], 0.98),
])
def test_linear_slope_characterization(values, expected):
    assert finance_logic.linear_slope(values) == pytest.approx(expected)
    assert rotation._calc_slope(values) == pytest.approx(expected)
    assert correlation._calc_trend(values) == pytest.approx(expected)
