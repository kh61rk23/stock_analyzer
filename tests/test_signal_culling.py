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

from signal_culling_report import (build_judgments, judge_daily_ics,      # noqa: E402
                                   render_report, spearman)


def make_panel(n_days=80, n_sectors=12, seed=7):
    """
    正相関(pos_cat)・負相関(neg_cat)・無相関(noise_cat)の3カテゴリを持つ
    合成パネルと20日先リターンを生成する。80日 ÷ 先20営業日 = 独立な4日 >= 3。
    """
    rng = random.Random(seed)
    panel, fwd = [], {}
    for di in range(n_days):
        d = f"2026-{di // 28 + 1:02d}-{di % 28 + 1:02d}"
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

    def test_insufficient_days_is_undecidable(self):
        panel, fwd = make_panel(n_days=20)   # 独立な日数 1 < 3
        j = build_judgments(panel, fwd, WEIGHTS)
        assert all(v["verdict"] == "判定不能" for v in j.values())
        # 判定不能は重みを変えない
        assert all(v["proposed_weight"] == WEIGHTS[c]
                   for c, v in j.items())

    def test_no_returns_is_undecidable(self):
        panel, _ = make_panel()
        j = build_judgments(panel, {}, WEIGHTS)
        assert all(v["verdict"] == "判定不能" for v in j.values())

    def test_market_wide_category_is_undecidable(self):
        """全業種で同じ値のカテゴリは、日ごとの業種横断の相関が取れない。市場の上げ下げと並んだだけで効いたと出さない"""
        panel, fwd = make_panel()
        for i, row in enumerate(panel):
            for comps in row["sectors"].values():
                comps["market_cat"] = 50 + (i % 20)
        j = build_judgments(panel, fwd, {**WEIGHTS, "market_cat": 0.1})
        assert j["market_cat"]["verdict"] == "判定不能" and j["market_cat"]["ic"] is None

    def test_t_uses_independent_days_not_overlapping_days(self):
        """同じ日ごとの IC でも、独立な日数（評価日数÷20）で t を作る。日数をそのまま使うと約 sqrt(20) 倍になる"""
        ics = [0.1, 0.2] * 40
        j = judge_daily_ics(ics, 0.1)
        n = len(ics)
        sd = (sum((x - 0.15) ** 2 for x in ics) / (n - 1)) ** 0.5
        assert j["t"] == pytest.approx(0.15 / (sd / (n / 20) ** 0.5), abs=0.01)
        assert j["t"] < 0.15 / (sd / n ** 0.5) / 4

    def test_fwd_return_uses_previous_trading_day_for_holiday(self, monkeypatch):
        """パネルの日付が休場日でも、直前の営業日の終値を起点に先リターンを作る"""
        import datetime as dt
        import types

        import pandas as pd
        import signal_culling_report as sc

        days = [dt.date(2026, 10, d) for d in range(1, 31) if dt.date(2026, 10, d).weekday() < 5]
        df = pd.DataFrame({"Close": [100.0 + i for i in range(len(days))]}, index=pd.to_datetime(days))

        class T:
            def __init__(self, t):
                pass

            def history(self, **kw):
                return df

        monkeypatch.setitem(sys.modules, "yf_cache", types.SimpleNamespace(Ticker=T))
        cfg = types.SimpleNamespace(SECTOR_REPRESENTATIVE_TICKERS={"1001": ["X.T"]})
        out = sc.fetch_forward_returns([{"date": "2026-10-03", "sectors": {"1001": {}}}], cfg)   # 土曜
        assert ("2026-10-03", "1001") in out


class TestPureFunctions:
    def test_spearman_perfect(self):
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
        assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_spearman_degenerate(self):
        assert spearman([1, 1, 1], [1, 2, 3]) is None      # 分散ゼロ
        assert spearman([1, 2], [1, 2]) is None            # n<3

    def test_judge_thresholds(self):
        # t>=2.0 → +20% / t<=-2.0 → ゼロ化 / 中間 → 維持（80日・独立4日）
        up = [0.3 + (i % 5) * 0.01 for i in range(80)]
        assert judge_daily_ics(up, 0.1)["verdict"] == "重み+20%提案"
        assert judge_daily_ics([-x for x in up], 0.1)["verdict"] == "重みゼロ化提案"
        flat = [0.1 if i % 2 else -0.1 for i in range(80)]
        assert judge_daily_ics(flat, 0.1)["verdict"] == "現状維持"
        assert judge_daily_ics(up[:59], 0.1)["verdict"] == "判定不能"     # 独立な日数 2.95 < 3


class TestReport:
    def test_render_contains_required_columns(self):
        panel, fwd = make_panel()
        j = build_judgments(panel, fwd, WEIGHTS)
        md = render_report(j, WEIGHTS, "202607")
        # 必須列: 現行重み・提案重み・IC・t・N（要件定義書 §R1-4）
        assert "| 現行重み | 提案重み | IC | t | 評価日数 |" in md.replace("カテゴリ | ", "")
        assert "自動では変更しない" in md
        assert "pos_cat" in md and "neg_cat" in md
