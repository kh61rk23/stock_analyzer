# ==============================================================
# tests/test_deliberation_scheduler.py — R3-2 B部門審議準備の受入テスト
# ==============================================================
# 受入条件（要件定義書 §R3-2 軽量版）:
#   対象選定ロジック・起動プロンプト生成（相互不可視・D-12モデル既定）の検証。
#   1回の完走実績は健太のCowork環境で確認（実走はサブスク内）。
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from deliberation_scheduler import (build_launch_prompts,             # noqa: E402
                                    parse_deliberated_tickers,
                                    select_deliberation_target)


class TestTargetSelection:
    def test_picks_highest_unreviewed(self):
        cands = [
            {"ticker": "A.T", "composite_score": 80},
            {"ticker": "B.T", "composite_score": 90},
            {"ticker": "C.T", "composite_score": 70},
        ]
        t = select_deliberation_target(cands, already=set())
        assert t["ticker"] == "B.T"                    # 最高スコア

    def test_skips_already_deliberated(self):
        cands = [
            {"ticker": "A.T", "composite_score": 80},
            {"ticker": "B.T", "composite_score": 90},
        ]
        t = select_deliberation_target(cands, already={"B.T"})
        assert t["ticker"] == "A.T"                    # B審議済み→次点

    def test_none_when_all_reviewed(self):
        cands = [{"ticker": "A.T", "composite_score": 80}]
        assert select_deliberation_target(cands, already={"A.T"}) is None

    def test_none_on_empty(self):
        assert select_deliberation_target([], set()) is None

    def test_ignores_missing_score(self):
        cands = [{"ticker": "A.T"}, {"ticker": "B.T", "composite_score": 50}]
        assert select_deliberation_target(cands, set())["ticker"] == "B.T"


class TestLedgerParse:
    def test_extract_tickers(self):
        text = "| 2026-07-01 | [[7203.T]] | Buy |\n| 2026-06-15 | [[6758]] | Watch |"
        s = parse_deliberated_tickers(text)
        # 4桁だけの表記はスクリーナー候補と突き合わせられるよう .T を付ける
        assert "7203.T" in s and "6758.T" in s

    def test_real_ledger_reads_only_ticker_column(self):
        """2026-09-29: 状態欄の 2026 / DV-8 / D-03 を拾い、543A.T を落としていた"""
        text = (
            "| case_id | ticker | 判断 | conviction | 層 | 未解消指摘 | 状態 |\n"
            "|---|---|---|---|---|---|---|\n"
            "| 2026-07-07_1605T | 1605.T | Watch | 2 | カジノ | 0 | **承認（2026-07-08）**DV-8 D-03 DEPT |\n"
            "| 2026-07-07_D-03 | —（制度決議） | 見送り | 4 | — | 0 | 承認 |\n"
            "| 2026-07-29_543AT | 543A.T | Watch | 2 | カジノ | 10 | 承認待ち |\n"
            "| 2026-08-04_SOXL | SOXL | Avoid | 3 | カジノ | 8 | 承認待ち |\n")
        assert parse_deliberated_tickers(text) == {"1605.T", "543A.T", "SOXL"}

    def test_empty(self):
        assert parse_deliberated_tickers("") == set()


class TestLaunchPrompts:
    def test_contains_four_agents_and_models(self):
        md = build_launch_prompts("2026-07-10_7203T", "7203.T")
        # 4エージェント全部
        assert "①ブル" in md and "②ベア" in md
        assert "③デビル" in md and "④レポート" in md
        # D-12 モデル既定: ブル/ベア=Sonnet 5、デビル/レポーター=Opus 4.8
        assert "Sonnet 5" in md and "Opus 4.8" in md
        # 相互不可視の明示
        assert "相互不可視" in md or "他アナリストのメモは見ない" in md
        # case_id が展開されている
        assert "2026-07-10_7203T" in md

    def test_order_constraints_present(self):
        md = build_launch_prompts("c1", "AAPL")
        assert "並列可" in md          # ①②並列
        assert "①②完了後" in md        # ③
        assert "③完了後" in md          # ④

    def test_output_names_match_vault_rules(self):
        # vault の部則（B1〜B3）と devil_calibration の glob は bull/bear/devil.md 前提
        md = build_launch_prompts("c1", "AAPL")
        for name in ("c1/bull.md", "c1/bear.md", "c1/devil.md"):
            assert f"20_審議/{name}" in md
        assert "01_ブル" not in md and "03_デビル" not in md
