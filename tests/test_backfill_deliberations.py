"""tools/backfill_deliberations.py の台帳パース・行組み立て・SQL 生成の単体テスト。"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "backfill_deliberations_cli", Path(__file__).resolve().parent.parent / "tools" / "backfill_deliberations.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

LEDGER = """# 判断台帳

| case_id | ticker | 判断 | conviction | 層 | 未解消指摘 | 状態 | 成績（後日） |
|---|---|---|---|---|---|---|---|
| 2026-07-07_1605T | 1605.T | Watch | 2 | カジノ | 0 | **承認（2026-07-08）**＝実行=Watch維持 | |
| 2026-07-07_D-03 | —（制度決議） | 見送り（再審議条件a-e） | 4 | — | 0 | **承認（2026-07-12）** | |
| 2026-07-29_543AT | 543A.T | Watch | 2 | カジノ | 10 | 承認待ち | |
| 2026-08-04_SOXL | SOXL | Avoid | 3 | カジノ | 8 | 承認待ち | |
| 2026-09-27_646AT | 646A.T | Watch | 2 | 判定保留 | 3 | **承認（2026-09-29）**＝全件承認 | |
"""


def test_parse_ledger_keeps_codes_and_skips_non_ticker_rows():
    keep, skipped = bf.parse_ledger(LEDGER)
    assert [e["case_id"] for e in keep] == ["2026-07-07_1605T", "2026-07-29_543AT", "2026-08-04_SOXL",
                                            "2026-09-27_646AT"]
    assert [s["case_id"] for s in skipped] == ["2026-07-07_D-03"]
    e = keep[0]
    assert (e["verdict"], e["conviction"], e["unresolved"]) == ("Watch", 2, 0)
    assert e["approval_status"] == "承認" and e["approved_at"] == "2026-07-08"
    assert keep[1]["approval_status"] == "承認待ち" and keep[1]["approved_at"] is None
    assert keep[2]["ticker"] == "SOXL"           # 米国 ticker も通る


def test_parse_ledger_empty_and_header_only():
    assert bf.parse_ledger("") == ([], [])
    assert bf.parse_ledger("| case_id | ticker |\n|---|---|\n") == ([], [])


def test_build_row_uses_report_memos_and_ledger():
    entry = bf.parse_ledger(LEDGER)[0][0]
    report = "---\ncompany: ＩＮＰＥＸ\nbull_conviction: 3\nbear_conviction: 2\n---\n本文"
    memos = {"devil.md": "## 未解消の重大指摘\n【反転級】a\n```yaml\nagent: devil\nunresolved_critical: 0\n```"}
    row = bf.build_row(entry, report, memos)
    assert row["case_id"] == "2026-07-07_1605T" and row["run_env"] == "backfill"
    assert row["company_name"] == "ＩＮＰＥＸ" and row["bull_conviction"] == 3 and row["bear_conviction"] == 2
    assert row["devil_unresolved"] == 0 and row["devil_reversal"] == 1
    assert row["approved_by"] == "健太" and row["approved_at"] == "2026-07-08T00:00:00+09:00"
    assert row["review_on"] == "2026-10-06"
    assert row["deliberated_at"].startswith("2026-07-07T12:00")
    assert row["bull_memo"] is None and row["report_md"] == report


def test_build_row_pending_has_no_approval():
    entry = bf.parse_ledger(LEDGER)[0][1]
    row = bf.build_row(entry, None, {})
    assert row["approval_status"] == "承認待ち" and row["approved_by"] is None and row["review_on"] is None
    assert row["report_md"] is None and row["kpi"] is None


def test_sql_string_raw_when_possible_and_escaped_otherwise():
    assert bf.sql_string("a\n\\d \"q\" 'q'") == 'r"""a\n\\d "q" \'q\'"""'
    assert bf.sql_string('x"""y') == "r'''x\"\"\"y'''"
    out = bf.sql_string('x"""y\'\'\'z')
    assert out.startswith('"') and "\\n" not in out and '\\"' in out
    assert bf.sql_string("end\"") .startswith("r'''")           # 末尾が引用符でも区切りを選び直す
    assert bf.sql_string("tail\\").startswith('"')              # 末尾バックスラッシュは通常の引用符で書く


def test_insert_sql_is_idempotent_and_complete():
    entry = bf.parse_ledger(LEDGER)[0][0]
    sql = bf.insert_sql(bf.build_row(entry, "x", {}), "proj")
    assert sql.startswith("INSERT INTO `proj.invest.deliberations`")
    assert "WHERE NOT EXISTS" in sql and "2026-07-07_1605T" in sql
    assert "CAST(NULL AS JSON)" in sql and "TIMESTAMP r" in sql
    for c in bf.COLUMNS:
        assert c in sql.split("SELECT")[0]


def test_fingerprint_lengths():
    fp = bf.fingerprint({"report_md": "abc", "bull_memo": None})
    assert fp["report_md"]["len"] == 3 and len(fp["report_md"]["md5"]) == 32 and fp["bull_memo"] is None
