"""クラウド審議の vault ミラー・承認の書き戻し・ontology 変換の回帰テスト（一時ディレクトリのみ）。"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import deliberation_queue as dq  # noqa: E402
import obsidian_sync as osy  # noqa: E402
import ontology_exporter as oe  # noqa: E402

LEDGER = """# 判断台帳

| case_id | ticker | 判断 | conviction | 層 | 未解消指摘 | 状態 | 成績（後日） |
|---|---|---|---|---|---|---|---|
| 2026-09-27_3099T | 3099.T | Watch | 3 | カジノ | 3 | **承認（2026-09-29）**＝全件承認 | |
| 2026-08-04_SOXL | SOXL | Avoid | 3 | カジノ | 8 | 承認待ち | |
"""


def rec(cid="2026-10-18_7203T", **kw):
    d = {"case_id": cid, "ticker": "7203.T", "company_name": "トヨタ", "verdict": "Watch",
         "conviction": 3, "layer": "カジノ", "devil_unresolved": 2, "report_md": "# レポート\n本文",
         "bull_memo": "bull", "bear_memo": "bear", "devil_memo": "devil", "packet_md": "pk",
         "approval_status": None, "approved_at": None, "holding_intent": None}
    d.update(kw)
    return d


# ── export の JSON 形 ───────────────────────────────────────

def test_rows_to_export_shape():
    out = dq.rows_to_export([{"case_id": "c", "kpi": '{"① x": "1"}',
                              "deliberated_at": datetime(2026, 10, 18, 1, 2), "approved_at": date(2026, 9, 29)}])
    assert list(out[0]) == list(dq.EXPORT_COLUMNS)
    assert out[0]["kpi"] == {"① x": "1"}
    assert out[0]["deliberated_at"].startswith("2026-10-18")
    assert out[0]["approved_at"] == "2026-09-29"
    assert out[0]["bull_memo"] is None
    json.dumps(out, ensure_ascii=False)


class FakeResult(list):
    def result(self):
        return self


class FakeClient:
    project = "p"

    def __init__(self, rows):
        self.rows, self.sqls = rows, []

    def query(self, sql, **kw):
        self.sqls.append(sql)
        return FakeResult(SimpleNamespace(items=lambda r=r: r.items()) for r in self.rows)


def test_export_results_one_query_and_file(tmp_path):
    cl = FakeClient([{"case_id": "2026-10-18_7203T", "ticker": "7203.T", "kpi": None}])
    res = dq.export_results(SimpleNamespace(DATA_DIR=tmp_path), cl)
    assert res["exported"] == 1 and len(cl.sqls) == 1
    data = json.loads((tmp_path / dq.EXPORT_FILE).read_text(encoding="utf-8"))
    assert data["count"] == 1 and data["deliberations"][0]["case_id"] == "2026-10-18_7203T"


def test_export_results_zero_rows_keeps_file(tmp_path):
    (tmp_path / dq.EXPORT_FILE).write_text("keep", encoding="utf-8")
    res = dq.export_results(SimpleNamespace(DATA_DIR=tmp_path), FakeClient([]))
    assert res["exported"] == 0
    assert (tmp_path / dq.EXPORT_FILE).read_text(encoding="utf-8") == "keep"


# ── 承認パース ──────────────────────────────────────────────

def test_parse_approval_variants():
    p = osy.parse_approval
    assert p("**承認（2026-09-29）**＝全件承認（利用者指示）") == ("承認", "2026-09-29")
    assert p("承認待ち") == (None, None)
    assert p("承認済み") == ("承認", None)
    assert p("否認（2026-10-01）＝理由") == ("否認", "2026-10-01")
    assert p("却下") == ("否認", None)
    assert p("保留（2026-10-02）") == ("保留", "2026-10-02")
    assert p("見送り（2026-10-03）") == (None, None)   # 結論は承認／否認／保留の3つだけ
    assert p("") == (None, None)


def test_build_approvals_skips_pending():
    a = osy.build_approvals(LEDGER)
    assert a == {"2026-09-27_3099T": {"approval_status": "承認", "approved_at": "2026-09-29"}}


# ── apply_approvals の差分抽出 ──────────────────────────────

def test_diff_approvals():
    appr = {"a": {"approval_status": "承認", "approved_at": "2026-09-29"},
            "b": {"approval_status": "承認", "approved_at": "2026-09-29"},
            "c": {"approval_status": "否認", "approved_at": None},
            "gone": {"approval_status": "承認", "approved_at": "2026-09-29"}}
    cur = {"a": {"approval_status": "承認", "approved_at": date(2026, 9, 29)},
           "b": {"approval_status": None, "approved_at": None},
           "c": {"approval_status": "承認", "approved_at": date(2026, 9, 1)}}
    d = dq.diff_approvals(appr, cur)
    assert [x["case_id"] for x in d] == ["b", "c"]
    assert d[0]["approved_at"] == date(2026, 9, 29)


def test_apply_approvals_no_file_no_bq(tmp_path):
    res = dq.apply_approvals(SimpleNamespace(DATA_DIR=tmp_path), client=object())
    assert res["applied"] == 0


# ── vault 書き込み ──────────────────────────────────────────

def test_ledger_row_format():
    row = osy.ledger_row_for(rec())
    assert row == "| 2026-10-18_7203T | 7203.T | Watch | 3 | カジノ | 2 | 承認待ち | |"
    r2 = osy.ledger_row_for(rec(approval_status="承認", approved_at="2026-10-20T00:00:00+00:00",
                                holding_intent="NISA|復活で買付"))
    assert "承認（2026-10-20）／保有意図: NISA／復活で買付" in r2
    parsed = osy._parse_ledger_rows(r2)
    assert parsed and parsed[0]["status"].startswith("承認（2026-10-20）")


def test_mirror_writes_only_new(tmp_path):
    b = tmp_path
    (b / "40_判断台帳.md").write_text(LEDGER, encoding="utf-8")
    old_report = b / "30_レポート" / "2026-09-27_3099T_判断レポート.md"
    old_report.parent.mkdir()
    old_report.write_text("人間が編集済み", encoding="utf-8")
    recs = [rec("2026-09-27_3099T"), rec()]
    st = osy.mirror_deliberations_to_vault(recs, b, today="20261018")
    assert old_report.read_text(encoding="utf-8") == "人間が編集済み"
    assert (b / "30_レポート" / "2026-10-18_7203T_判断レポート.md").read_text(encoding="utf-8") == "# レポート\n本文"
    for n in ("bull", "bear", "devil", "packet"):
        assert (b / "20_審議" / "2026-10-18_7203T" / f"{n}.md").exists()
    text = (b / "40_判断台帳.md").read_text(encoding="utf-8")
    assert text.count("2026-09-27_3099T") == 1       # 既存行は増えない・書き換えない
    assert text.rstrip().endswith("| 2026-10-18_7203T | 7203.T | Watch | 3 | カジノ | 2 | 承認待ち | |")
    assert (b / "40_判断台帳.md.bak_20261018").read_text(encoding="utf-8") == LEDGER
    assert st["ledger_rows"] == 1
    # 冪等: 2回目は何も書かない
    before = text
    st2 = osy.mirror_deliberations_to_vault(recs, b, today="20261019")
    assert (b / "40_判断台帳.md").read_text(encoding="utf-8") == before
    assert not (b / "40_判断台帳.md.bak_20261019").exists()
    assert st2["ledger_rows"] == 0 and st2["reports"] == 0


def test_mirror_inserts_after_last_table_row(tmp_path):
    (tmp_path / "40_判断台帳.md").write_text(LEDGER + "\n## 備考\nテキスト\n", encoding="utf-8")
    osy.mirror_deliberations_to_vault([rec()], tmp_path, today="20261018")
    lines = (tmp_path / "40_判断台帳.md").read_text(encoding="utf-8").splitlines()
    i = next(n for n, l in enumerate(lines) if "7203T" in l)
    assert lines[i - 1].startswith("| 2026-08-04_SOXL")
    assert "## 備考" in lines


# ── ontology ────────────────────────────────────────────────

def test_ontology_decision_events_from_db(tmp_path):
    (tmp_path / "invest_deliberations.json").write_text(json.dumps({"deliberations": [
        rec(approval_status="承認"), rec("2026-10-18_6758T", ticker="6758.T", conviction=None),
        {"case_id": "", "ticker": "x"}]}), encoding="utf-8")
    ev = oe.build_decision_events(SimpleNamespace(DATA_DIR=tmp_path, INVESTMENT_DEPT_VAULT_DIR=str(tmp_path / "none")))
    assert len(ev) == 2
    e = ev[0]
    assert set(e) == {"case_id", "ticker", "verdict", "conviction", "layer", "open_issues",
                      "status", "result", "decided_at", "ingested_at"}
    assert e["status"] == "承認" and e["decided_at"] == "2026-10-18" and e["open_issues"] == 2
    assert ev[1]["status"] == "承認待ち" and ev[1]["conviction"] is None


def test_ontology_db_preferred_ledger_fills_gap(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "40_判断台帳.md").write_text(LEDGER, encoding="utf-8")
    (tmp_path / "invest_deliberations.json").write_text(json.dumps({"deliberations": [
        rec("2026-09-27_3099T", approval_status="承認")]}), encoding="utf-8")
    ev = oe.build_decision_events(SimpleNamespace(DATA_DIR=tmp_path, INVESTMENT_DEPT_VAULT_DIR=str(vault)))
    by = {e["case_id"]: e for e in ev}
    assert set(by) == {"2026-09-27_3099T", "2026-08-04_SOXL"}
    assert by["2026-09-27_3099T"]["status"] == "承認"          # DB 優先（台帳の長い文面ではない）
    assert by["2026-08-04_SOXL"]["status"] == "承認待ち"


def test_ontology_no_sources(tmp_path):
    assert oe.build_decision_events(SimpleNamespace(DATA_DIR=tmp_path, INVESTMENT_DEPT_VAULT_DIR=str(tmp_path / "x"))) == []
