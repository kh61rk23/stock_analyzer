# ==============================================================
# tests/test_monthly_review.py — R3-4 月次戦略レビューの受入テスト
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from monthly_strategy_review import (_count_ledger_month, _paper_month_table,  # noqa: E402
                                     _score_band_table, build_review)


class TestScoreBands:
    def test_renders_bands(self):
        pl = {"score_bands": {"80-100": {"n": 10, "hit_rate": 0.7, "avg_alpha": 3.2}}}
        out = "\n".join(_score_band_table(pl))
        assert "80-100" in out and "70%" in out and "+3.2%" in out

    def test_no_data(self):
        assert "データなし" in "\n".join(_score_band_table({}))


class TestPaperMonth:
    def test_month_filter(self):
        acc = {"s1": {"equity_history": [
            {"date": "2026-07-01", "equity": 1000000},
            {"date": "2026-07-31", "equity": 1050000},
            {"date": "2026-08-01", "equity": 900000}]}}
        out = "\n".join(_paper_month_table(acc, "202607"))
        assert "s1" in out and "+5.00%" in out       # 8月分は除外

    def test_no_data(self):
        assert "データなし" in "\n".join(_paper_month_table({}, "202607"))


class TestLedgerCount:
    def test_counts_month_entries(self):
        text = "| 2026-07-01 | A | Buy |\n| 2026-07-15 | B |\n| 2026-06-20 | C |"
        assert _count_ledger_month(text, "202607") == 2

    def test_empty(self):
        assert _count_ledger_month("", "202607") == 0


class TestBuildReview:
    def test_full_review_structure(self):
        sources = {
            "performance_log": {"score_bands": {
                "80-100": {"n": 5, "hit_rate": 0.6, "avg_alpha": 2.0}}},
            "signal_culling": {"proposals": "3", "undecidable": "5"},
            "ledger_count": 2,
            "learning_audit": {"changes": [
                {"rule": "pearl", "old_weight": 0.9, "new_weight": 1.1}]},
            "paper_accounts": {"s1": {"equity_history": [
                {"date": "2026-07-01", "equity": 1000000},
                {"date": "2026-07-31", "equity": 1020000}]}},
        }
        md = build_review(sources, "202607")
        assert "月次戦略レビュー 2026-07" in md
        for sec in ("① スコア帯別", "② シグナル淘汰", "③ 審議台帳",
                    "④ 重み変更", "⑤ S2戦略別"):
            assert sec in md
        assert "pearl" in md and "3件" in md
        assert "LLM不使用" in md

    def test_all_empty_is_graceful(self):
        md = build_review({}, "202607")
        assert "月次戦略レビュー" in md
        assert md.count("データなし") >= 3           # 複数セクションで欠損表示
