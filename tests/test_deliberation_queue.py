"""審議キュー（日次・週次メール表示 ＋ 隔週ジョブ選定）の回帰テスト。

2026-07-28 追加。

このキューの最重要の性質は
  「メールに出る先頭銘柄」＝「隔週ジョブが実際に選定する銘柄」
が常に一致することである。両者がずれると、健太がメールで見た銘柄と
実際に審議パケットが作られる銘柄が食い違い、運用が壊れる。
そのため select_deliberation_target は build_deliberation_queue の
先頭要素を返す委譲になっており、ここでその同値性を検査する。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import deliberation_scheduler as ds  # noqa: E402


CANDS = [
    {"ticker": "6674.T", "composite_score": 89.3, "name": "GS Yuasa"},
    {"ticker": "6504.T", "composite_score": 88.1, "name": "Fuji Electric"},
    {"ticker": "6702.T", "composite_score": 86.7, "name": "Fujitsu"},
    {"ticker": "7203.T", "composite_score": 95.0, "name": "Toyota"},
    {"ticker": "9999.T", "composite_score": None, "name": "スコア欠損"},
    {"ticker": "", "composite_score": 50.0, "name": "ticker欠損"},
]


# ── 並び順と除外 ──────────────────────────────────────────────

def test_queue_is_sorted_by_score_desc():
    q = ds.build_deliberation_queue(CANDS, already=set(), top_n=5)
    scores = [c["composite_score"] for c in q]
    assert scores == sorted(scores, reverse=True)
    assert q[0]["ticker"] == "7203.T"


def test_queue_excludes_already_deliberated():
    q = ds.build_deliberation_queue(CANDS, already={"7203.T", "6674.T"}, top_n=5)
    tickers = [c["ticker"] for c in q]
    assert "7203.T" not in tickers
    assert "6674.T" not in tickers
    assert q[0]["ticker"] == "6504.T"


def test_queue_drops_rows_without_ticker_or_score():
    q = ds.build_deliberation_queue(CANDS, already=set(), top_n=10)
    tickers = [c["ticker"] for c in q]
    assert "9999.T" not in tickers      # スコア欠損
    assert "" not in tickers            # ticker欠損
    assert len(q) == 4


def test_queue_respects_top_n():
    assert len(ds.build_deliberation_queue(CANDS, set(), top_n=2)) == 2
    assert ds.build_deliberation_queue(CANDS, set(), top_n=0) == []


def test_queue_empty_when_all_deliberated():
    already = {c["ticker"] for c in CANDS if c["ticker"]}
    assert ds.build_deliberation_queue(CANDS, already, top_n=5) == []


# ── ★ 最重要: メール表示と隔週ジョブの選定が一致すること ──────────

def test_selected_target_is_head_of_queue():
    for already in (set(), {"7203.T"}, {"7203.T", "6674.T"}):
        q = ds.build_deliberation_queue(CANDS, already, top_n=5)
        target = ds.select_deliberation_target(CANDS, already)
        if not q:
            assert target is None
        else:
            assert target is not None
            assert target["ticker"] == q[0]["ticker"], (
                "メール先頭と選定対象がずれている")


def test_selected_target_is_none_when_queue_empty():
    already = {c["ticker"] for c in CANDS if c["ticker"]}
    assert ds.select_deliberation_target(CANDS, already) is None


# ── 審議済み集合の解決（出所の優先順）──────────────────────────

class _Cfg:
    def __init__(self, data_dir, vault=""):
        self.DATA_DIR = str(data_dir)
        self.INVESTMENT_DEPT_VAULT_DIR = str(vault)
        self.DELIBERATION_INTERVAL_DAYS = 14


def test_resolve_prefers_ledger_over_mirror_and_journal(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "40_判断台帳.md").write_text(
        "| 2026-07-01 | [[7203.T]] | Buy |", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "deliberated_tickers.json").write_text(
        json.dumps({"tickers": ["9984.T"]}), encoding="utf-8")

    tickers, source = ds.resolve_deliberated(_Cfg(data, vault))
    assert source == "台帳(正本)"
    assert "7203.T" in tickers


def test_resolve_falls_back_to_mirror(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "deliberated_tickers.json").write_text(
        json.dumps({"tickers": ["9984.T", "6501.T"]}), encoding="utf-8")

    tickers, source = ds.resolve_deliberated(_Cfg(data, tmp_path / "nope"))
    assert source == "ミラー"
    assert tickers == {"9984.T", "6501.T"}


def test_resolve_falls_back_to_journal(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "investment_journal.json").write_text(
        json.dumps({"entries": [{"ticker": "6501.T"}, {"ticker": "6758.T"}]}),
        encoding="utf-8")

    tickers, source = ds.resolve_deliberated(_Cfg(data, tmp_path / "nope"))
    assert source == "journal(部分)"
    assert tickers == {"6501.T", "6758.T"}


def test_resolve_returns_empty_when_nothing_available(tmp_path):
    tickers, source = ds.resolve_deliberated(_Cfg(tmp_path, tmp_path / "nope"))
    assert tickers == set()
    assert source == "なし"


# ── レポート用ラッパー ────────────────────────────────────────

def test_get_queue_for_report_marks_partial_source(tmp_path):
    """正本を参照できないときは partial=True になること。

    これが False のまま出ると「審議済みが混じり得る」ことが
    メールから読み取れなくなる。
    """
    data = tmp_path / "data"
    data.mkdir()
    (data / "investment_journal.json").write_text(
        json.dumps({"entries": [{"ticker": "6501.T"}]}), encoding="utf-8")

    out = ds.get_queue_for_report(
        _Cfg(data, tmp_path / "nope"),
        {"screener": {"candidates": CANDS}}, top_n=3)

    assert out["partial"] is True
    assert out["source"] == "journal(部分)"
    assert len(out["queue"]) == 3
    assert out["queue"][0]["ticker"] == "7203.T"


def test_get_queue_for_report_uses_results_candidates(tmp_path):
    """results があれば screener_history を読まずにそちらを使うこと。"""
    out = ds.get_queue_for_report(
        _Cfg(tmp_path, tmp_path / "nope"),
        {"screener": {"candidates": CANDS}}, top_n=2)
    assert [c["ticker"] for c in out["queue"]] == ["7203.T", "6674.T"]


def test_export_mirror_roundtrip(tmp_path):
    """vault が読めるとき、ミラーが書かれて再読み込みできること。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "40_判断台帳.md").write_text("[[7203.T]] [[6501.T]]", encoding="utf-8")
    data = tmp_path / "data"
    cfg = _Cfg(data, vault)

    res = ds.export_deliberated_mirror(cfg)
    assert res["skipped"] is False
    assert res["count"] > 0

    # vault を外してもミラーから読めること
    tickers, source = ds.resolve_deliberated(_Cfg(data, tmp_path / "nope"))
    assert source == "ミラー"
    assert "7203.T" in tickers


def test_export_mirror_skips_without_vault(tmp_path):
    res = ds.export_deliberated_mirror(_Cfg(tmp_path, tmp_path / "nope"))
    assert res["skipped"] is True
    assert res["count"] == 0


# ── 機械可読マーカー（Gmail MCP から予定タスクが抽出する） ────────
#
# このマーカーの文字列・書式が変わると、
# ~/.claude/scheduled-tasks/stock-analyzer-deliberation/SKILL.md の
# 抽出が黙って失敗する（メールは届くのに審議が走らない）。
# 変更を検知するためにここで固定する。

import re  # noqa: E402

MARKER_RE = re.compile(r"DELIBERATION_QUEUE_V1:\s*([A-Za-z0-9.,\-]+)")


def test_marker_constant_is_stable():
    from alert_notifier import DELIBERATION_QUEUE_MARKER
    assert DELIBERATION_QUEUE_MARKER == "DELIBERATION_QUEUE_V1:"


def test_daily_mail_contains_parsable_marker(tmp_path):
    """日次メール本文からティッカー列を復元できること。"""
    import alert_notifier as an

    class Cfg(_Cfg):
        ALERT_ON_DELIBERATION_QUEUE = True
        DELIBERATION_QUEUE_TOP_N = 3

    data = tmp_path / "data"
    data.mkdir()
    cfg = Cfg(data, tmp_path / "nope")

    alerts = an.extract_alerts(cfg, {"screener": {"candidates": CANDS}})
    body = next((a["body"] for a in alerts
                 if a.get("trigger") == "deliberation_queue"), "")
    assert body, "審議キューのアラートが生成されていない"

    m = MARKER_RE.search(body)
    assert m, "機械可読マーカーが本文に無い＝予定タスクが抽出できない"
    tickers = m.group(1).split(",")
    assert tickers == ["7203.T", "6674.T", "6504.T"]


def test_weekly_report_contains_parsable_marker(tmp_path):
    """週次レポートからも同じ形式で復元できること。"""
    import weekly_report_generator as w

    class Cfg(_Cfg):
        DELIBERATION_QUEUE_TOP_N = 2

    lines = w._section_deliberation_queue(
        {"screener": {"candidates": CANDS}}, Cfg(tmp_path, tmp_path / "nope"))
    m = MARKER_RE.search("\n".join(lines))
    assert m, "週次レポートに機械可読マーカーが無い"
    assert m.group(1).split(",") == ["7203.T", "6674.T"]


def test_marker_head_matches_selected_target(tmp_path):
    """★ マーカー先頭 == 隔週ジョブが選定する銘柄 であること。

    ここがずれると、メールで見た銘柄と実際に審議される銘柄が食い違う。
    """
    import alert_notifier as an

    class Cfg(_Cfg):
        ALERT_ON_DELIBERATION_QUEUE = True
        DELIBERATION_QUEUE_TOP_N = 5

    data = tmp_path / "data"
    data.mkdir()
    cfg = Cfg(data, tmp_path / "nope")

    alerts = an.extract_alerts(cfg, {"screener": {"candidates": CANDS}})
    body = next(a["body"] for a in alerts
                if a.get("trigger") == "deliberation_queue")
    head = MARKER_RE.search(body).group(1).split(",")[0]

    already, _ = ds.resolve_deliberated(cfg)
    target = ds.select_deliberation_target(CANDS, already)
    assert target["ticker"] == head
