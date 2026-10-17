# obsidian_sync.py — 判断台帳⇔journal同期・審議キュー [DEPT-2]
import json
from pathlib import Path

from conftest import load_module

osync = load_module("obsidian_sync")

LEDGER_MD = """| case_id | ticker | 判断 | conviction | 層 | 未解消指摘 | 状態 | 成績（後日） |
|---|---|---|---|---|---|---|---|
| 2026-06-12_7203T | 7203.T | Buy | 4 | カジノ | 0 | 承認 | |
| 2026-06-12_6501T | 6501.T | Watch | 3 | カジノ | 1 | 承認 | |
| 2026-06-12_8001T | 8001.T | Buy | 5 | NISA | 0 | 承認待ち | |
"""


def _vault(tmp_cfg) -> Path:
    return Path(tmp_cfg.INVESTMENT_DEPT_VAULT_DIR)


class TestLedgerParse:
    def test_rows_and_columns(self, tmp_cfg):
        rows = osync._parse_ledger_rows(LEDGER_MD)
        assert len(rows) == 3
        assert rows[0]["layer"] == "カジノ" and rows[2]["status"] == "承認待ち"


class TestJournalSync:
    def test_only_approved_buy(self, tmp_cfg):
        _vault(tmp_cfg).joinpath("40_判断台帳.md").write_text(
            LEDGER_MD, encoding="utf-8")
        osync.cmd_journal(tmp_cfg)
        j = json.loads((tmp_cfg.DATA_DIR / "investment_journal.json")
                       .read_text(encoding="utf-8"))
        # 承認×Buy の 7203.T のみ（Watch除外・「承認待ち」部分一致除外）
        assert [e["ticker"] for e in j["entries"]] == ["7203.T"]

    def test_idempotent(self, tmp_cfg):
        _vault(tmp_cfg).joinpath("40_判断台帳.md").write_text(
            LEDGER_MD, encoding="utf-8")
        osync.cmd_journal(tmp_cfg)
        osync.cmd_journal(tmp_cfg)
        j = json.loads((tmp_cfg.DATA_DIR / "investment_journal.json")
                       .read_text(encoding="utf-8"))
        assert len(j["entries"]) == 1


class TestFeedback:
    def test_result_flows_back(self, tmp_cfg):
        _vault(tmp_cfg).joinpath("40_判断台帳.md").write_text(
            LEDGER_MD, encoding="utf-8")
        osync.cmd_journal(tmp_cfg)
        p = tmp_cfg.DATA_DIR / "investment_journal.json"
        j = json.loads(p.read_text(encoding="utf-8"))
        j["entries"][0]["result"] = {"alpha_pct": "+12.3%"}
        p.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        osync.cmd_feedback(tmp_cfg)
        assert "+12.3%" in _vault(tmp_cfg).joinpath(
            "40_判断台帳.md").read_text(encoding="utf-8")


class TestQueue:
    def test_pass_filters_only_and_idempotent(self, tmp_cfg):
        """旧来の辞書形式＋pass_filters 明示。後方互換として維持する。

        ※ この形式は実データではない（下の test_real_list_shape 参照）。
        本テストが実データと違う形を固定していたため、リスト形式を読めない
        バグが2026-07-25まで検出されなかった。
        """
        (tmp_cfg.DATA_DIR / "screener_history.json").write_text(json.dumps({
            "2026-06-11": {"candidates": [
                {"ticker": "7203.T", "composite_score": 82.5,
                 "pass_filters": True},
                {"ticker": "9999.T", "composite_score": 90.0,
                 "pass_filters": False}]}}), encoding="utf-8")
        qp = _vault(tmp_cfg) / "15_審議キュー.md"
        qp.write_text("# 審議キュー\n| 追加日 | ticker | 理由 | 優先度 | 状態 |\n"
                      "|---|---|---|---|---|\n| x | 4005.T | 手動 | 低 | 未着手 |\n",
                      encoding="utf-8")
        osync.cmd_queue(tmp_cfg)
        osync.cmd_queue(tmp_cfg)
        q = qp.read_text(encoding="utf-8")
        assert q.count("AUTO-QUEUE:BEGIN") == 1   # 冪等
        assert "7203.T" in q and "9999.T" not in q
        assert "手動" in q                          # 手動行温存

    def test_real_list_shape_populates_queue(self, tmp_cfg):
        """実データ形式（日付の値がリスト・pass_filtersキーなし）で
        審議キューが埋まること。

        append_screener_history は通過銘柄だけを保存し pass_filters を
        残さない。この形式が読めず審議キューが常に0件だった回帰を防ぐ。
        """
        (tmp_cfg.DATA_DIR / "screener_history.json").write_text(json.dumps({
            "2026-07-22": [
                {"ticker": "6674.T", "composite_score": 89.3},
                {"ticker": "6504.T", "composite_score": 88.1}]}),
            encoding="utf-8")
        qp = _vault(tmp_cfg) / "15_審議キュー.md"
        qp.write_text("# 審議キュー\n", encoding="utf-8")
        osync.cmd_queue(tmp_cfg)
        q = qp.read_text(encoding="utf-8")
        assert "6674.T" in q, "実データ形式で審議キューが空になっている"
        assert "6504.T" in q
        # スコア降順で並ぶこと
        assert q.index("6674.T") < q.index("6504.T")


def test_git_retry_gives_up_quickly_after_offline(monkeypatch):
    """一度つながらないと分かったら、同じ回の残りの git は再試行しない。"""
    import types
    import obsidian_sync as osync
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return types.SimpleNamespace(returncode=128, stderr="fatal: Could not resolve host: github.com", stdout="")
    monkeypatch.setattr(osync.subprocess, "run", fake_run)
    monkeypatch.setattr(osync, "_OFFLINE", False)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    osync._git_with_retry(["git", "pull"], 10, tries=6, wait=0)
    assert len(calls) == 6 and osync._OFFLINE is True
    osync._git_with_retry(["git", "push"], 10, tries=6, wait=0)
    assert len(calls) == 7
