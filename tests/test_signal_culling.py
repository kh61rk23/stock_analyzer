# ==============================================================
# tests/test_signal_culling.py — E4シグナル淘汰の受入テスト（R1-4）
# ==============================================================
# 受入条件（要件定義書 §R1-4）:
#   合成データ（正/負/無相関の3シグナル）で判定が正しく分かれる pytest
# ==============================================================
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from signal_culling_report import (build_judgments, judge_category,      # noqa: E402
                                   render_report, spearman, t_stat)


def make_panel(n_days=10, n_sectors=12, seed=7):
    """
    正相関(pos_cat)・負相関(neg_cat)・無相関(noise_cat)の3カテゴリを持つ
    合成パネルと20日先リターンを生成する。N = 10日×12業種 = 120 >= 60。
    """
    rng = random.Random(seed)
    panel, fwd = [], {}
    for di in range(n_days):
        d = f"2026-06-{di + 1:02d}"
        sectors = {}
        for si in range(n_sectors):
            code = f"10{si:02d}"
            ret = rng.gauss(0, 0.05)
            sectors[code] = {
                "pos_cat": 50 + ret * 400 + rng.gauss(0, 1),   # リターンと正相関
                "neg_cat": 50 - ret * 400 + rng.gauss(0, 1),   # 負相関
                "noise_cat": rng.uniform(0, 100),              # 無相関
            }
            fwd[(d, code)] = ret
        panel.append({"date": d, "sectors": sectors})
    return panel, fwd


WEIGHTS = {"pos_cat": 0.10, "neg_cat": 0.10, "noise_cat": 0.10}


class TestJudgments:
    def test_three_signals_split_correctly(self):
        panel, fwd = make_panel()
        j = build_judgments(panel, fwd, WEIGHTS)
        assert j["pos_cat"]["verdict"] == "重み+20%提案"
        assert j["pos_cat"]["proposed_weight"] == pytest.approx(0.12)
        assert j["neg_cat"]["verdict"] == "重みゼロ化提案"
        assert j["neg_cat"]["proposed_weight"] == 0.0
        assert j["noise_cat"]["verdict"] in ("現状維持", "判定不能")
        assert j["noise_cat"]["proposed_weight"] == pytest.approx(0.10)

    def test_insufficient_n_is_undecidable(self):
        panel, fwd = make_panel(n_days=2, n_sectors=5)   # N=10 < 60
        j = build_judgments(panel, fwd, WEIGHTS)
        assert all(v["verdict"] == "判定不能" for v in j.values())
        # 判定不能は重みを変えない
        assert all(v["proposed_weight"] == WEIGHTS[c]
                   for c, v in j.items())

    def test_no_returns_is_undecidable(self):
        panel, _ = make_panel()
        j = build_judgments(panel, {}, WEIGHTS)
        assert all(v["verdict"] == "判定不能" for v in j.values())


class TestPureFunctions:
    def test_spearman_perfect(self):
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
        assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_spearman_degenerate(self):
        assert spearman([1, 1, 1], [1, 2, 3]) is None      # 分散ゼロ
        assert spearman([1, 2], [1, 2]) is None            # n<3

    def test_t_stat_monotonic(self):
        assert t_stat(0.3, 100) > t_stat(0.1, 100)
        assert t_stat(0.1, 400) > t_stat(0.1, 100)
        assert t_stat(-0.3, 100) < 0

    def test_judge_thresholds(self):
        # t>=2.0 → +20% / t<=-2.0 → ゼロ化 / 中間 → 維持
        assert judge_category(0.5, 100, 0.1)["verdict"] == "重み+20%提案"
        assert judge_category(-0.5, 100, 0.1)["verdict"] == "重みゼロ化提案"
        assert judge_category(0.01, 100, 0.1)["verdict"] == "現状維持"
        assert judge_category(0.5, 59, 0.1)["verdict"] == "判定不能"


class TestReport:
    def test_render_contains_required_columns(self):
        panel, fwd = make_panel()
        j = build_judgments(panel, fwd, WEIGHTS)
        md = render_report(j, WEIGHTS, "202607")
        # 必須列: 現行重み・提案重み・IC・t・N（要件定義書 §R1-4）
        assert "| 現行重み | 提案重み | IC | t | N |" in md.replace("カテゴリ | ", "")
        assert "自動では変更しない" in md
        assert "pos_cat" in md and "neg_cat" in md
