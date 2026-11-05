"""edinet_gov_text: 有報の CSV（type=5）から節の本文を取り出す。技術マップの照合が本文を2か所から読む。"""
import io
import zipfile
from pathlib import Path

import edinet_gov_text as g


def _zip(rows, name="XBRL_TO_CSV/jpcrp030000-asr-001_E00001-000_2026-03-31_01_2026-06-24.csv"):
    head = '"要素ID"\t"項目名"\t"コンテキストID"\t"相対年度"\t"連結・個別"\t"期間・時点"\t"ユニットID"\t"単位"\t"値"'
    body = "\n".join("\t".join(f'"{c}"' for c in r) for r in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, (head + "\n" + body).encode("utf-16"))
        z.writestr("XBRL_TO_CSV/jpaud-aai-cc-001.csv", (head + "\n").encode("utf-16"))
    return buf.getvalue()


def test_blocks_only_known_sections_first_occurrence_and_cut():
    rows = [["jpcrp_cor:DescriptionOfBusinessTextBlock", "事業の内容", "FilingDateInstant", "", "", "", "", "", "半導体製造装置" * 5],
            ["jpcrp_cor:DescriptionOfBusinessTextBlock", "事業の内容", "Other", "", "", "", "", "", "二つ目は捨てる"],
            ["jpcrp_cor:ResearchAndDevelopmentActivitiesTextBlock", "研究開発", "FilingDateInstant", "", "", "", "", "", "－"],
            ["jpcrp_cor:CareerSummaryInformationAboutDirectorsAndCorporateAuditorsTextBlock", "略歴", "x", "", "", "", "", "", "略歴は取らない"],
            ["jpcrp_cor:MajorShareholdersTextBlock", "大株主", "FilingDateInstant", "", "", "", "", "", "日本マスタートラスト"]]
    out = g.blocks_from_zip(_zip(rows), max_chars=10)
    assert [(s, len(t)) for _, s, t in out] == [("事業の内容", 10), ("大株主の状況", 10)]


def test_mapper_reads_both_text_sources_and_target_sql_excludes_funds():
    src = (Path(g.__file__).parent / "taxonomy_company_mapper.py").read_text(encoding="utf-8")
    assert "edinet.text_analysis" in src and "edinet.gov_text_blocks" in src
    assert "fund_code IS NULL" in g.TARGET_SQL and "ordinance_code = '010'" in g.TARGET_SQL


def test_segment_text_block_prefers_consolidated():
    rows = [["jpcrp_cor:NotesSegmentInformationEtcFinancialStatementsTextBlock", "セグメント", "x", "", "個別", "", "", "", "個別の注記"],
            ["jpcrp_cor:NotesSegmentInformationEtcConsolidatedFinancialStatementsTextBlock", "セグメント", "x", "", "連結", "", "", "",
             "自動車事業 金融事業"]]
    out = g.blocks_from_zip(_zip(rows), max_chars=100)
    assert out == [("jpcrp_cor:NotesSegmentInformationEtcConsolidatedFinancialStatementsTextBlock", "セグメント情報", "自動車事業 金融事業")]


def test_segment_revenue_current_year_members_only():
    E = "jpcrp_cor:RevenuesFromExternalCustomers"
    rows = [[E, "外部顧客への売上高", "CurrentYearDuration_jpcrp030000-asr_E02144-000AutomotiveReportableSegmentMember", "当期", "連結", "期間", "JPY", "円", "40,000"],
            [E, "外部顧客への売上高", "CurrentYearDuration_jpcrp030000-asr_E02144-000FinancialServicesReportableSegmentMember", "当期", "連結", "期間", "JPY", "円", "5000"],
            [E, "外部顧客への売上高", "CurrentYearDuration_jpcrp_cor:ReportableSegmentsMember", "当期", "連結", "期間", "JPY", "円", "45000"],
            [E, "外部顧客への売上高", "Prior1YearDuration_jpcrp030000-asr_E02144-000AutomotiveReportableSegmentMember", "前期", "連結", "期間", "JPY", "円", "1"],
            ["jpigp_cor:RevenueFromExternalCustomersIFRS", "外部顧客", "CurrentYearDuration_jpcrp030000-asr_E1-000ChemicalsSegmentMember", "当期", "連結", "期間", "JPY", "円", "7"]]
    out = g.segment_revenue_from_zip(_zip(rows))
    assert [(g.segment_name(x["member"]), x["value"]) for x in out] == [("Automotive", 40000.0), ("Financial Services", 5000.0), ("Chemicals", 7.0)]


def test_segment_revenue_ifrs_and_plural_members():
    rows = [["jpigp_cor:SalesToExternalCustomersIFRS", "外部顧客", "CurrentYearDuration_jpcrp030000-asr_E02166-000GameAndNetworkServicesReportableSegmentMember", "当期", "その他", "期間", "JPY", "円", "100"],
            ["jpcrp_cor:RevenuesFromExternalCustomers", "外部顧客", "CurrentYearDuration_jpcrp030000-asr_E00790-000ElectronicsMaterialsReportableSegmentsMember", "当期", "その他", "期間", "JPY", "円", "50"],
            ["jpcrp_cor:RevenuesFromExternalCustomers", "外部顧客", "CurrentYearDuration_jpcrp_cor:ReportableSegmentsMember", "当期", "その他", "期間", "JPY", "円", "150"],
            ["jpigp_cor:IntersegmentSalesIFRS", "内部", "CurrentYearDuration_jpcrp030000-asr_E02166-000GameAndNetworkServicesReportableSegmentMember", "当期", "その他", "期間", "JPY", "円", "9"]]
    out = g.segment_revenue_from_zip(_zip(rows))
    assert [(g.segment_name(x["member"]), x["value"]) for x in out] == [("Game And Network Services", 100.0), ("Electronics Materials", 50.0)]


def test_large_holdings_table_function_filters_dates_before_grouping():
    import edinet_gov_docs as gd
    sql = gd.large_holdings_tf_sql("p")
    assert sql.startswith("CREATE OR REPLACE TABLE FUNCTION `p.edinet.large_holdings_between`(a DATE, b DATE)")
    assert "doc_type_code IN ('350','360','370') AND submit_date BETWEEN a AND b)" in sql


# ---- 本文なし・失敗・取り直し ----
class _Rows(list):
    def result(self):
        return self


class _Client:
    project = "p"

    def __init__(self, ids=None, boom=False):
        self.ids, self.boom, self.n = ids, boom, 0

    def query(self, sql, **kw):
        self.n += 1
        if self.boom:
            raise RuntimeError("quota")
        return _Rows(type("R", (), {"doc_id": d}) for d in (self.ids or []))


def _sets(done, prev=(), seg=(), segt=()):
    return {"done": set(done), "prev_done": set(prev), "seg_done": set(seg), "seg_text_done": set(segt)}


def test_requeue_rowless_puts_done_docs_without_rows_back_once():
    st, s = {}, _sets(["A", "B", "C"], prev=["P", "Q"], seg=["A", "B", "C"], segt=["A", "B", "C"])
    assert g.requeue_rowless(_Client(["A", "C", "P"]), st, s) == 2
    assert s["done"] == {"A", "C"} and s["seg_done"] == {"A", "C"} and s["seg_text_done"] == {"A", "C"}
    assert s["prev_done"] == {"P"}
    assert st["reconcile_version"] == g.RECONCILE_VERSION
    c = _Client(["A"])                                       # 版が済みなら表を読まず、何も戻さない
    assert g.requeue_rowless(c, st, s) == 0 and c.n == 0 and s["done"] == {"A", "C"}


def test_requeue_rowless_keeps_everything_when_table_unreadable_or_empty():
    for c in (_Client(boom=True), _Client([])):
        st, s = {}, _sets(["A"], prev=["P"])
        assert g.requeue_rowless(c, st, s) == 0
        assert s["done"] == {"A"} and s["prev_done"] == {"P"} and "reconcile_version" not in st


def test_summary_line_separates_failures_from_no_text():
    line = g.summary_line({"docs": 10, "new_docs": 4, "prev_docs": 3, "seg_only_docs": 3, "rows": 50, "seg_rows": 7,
                           "no_text": 1, "gone": 2, "errors": 3, "requeued": 5, "remaining": 9})
    assert "本文なし 1" in line and "書類側で取れない 2" in line and "取得失敗 3" in line and "取り直しに戻した 5" in line
    assert "セグメントのみ 3" in line


def _run_with(monkeypatch, tmp_path, responses, done=(), seg_text=(), seg_rev=()):
    """run() を、BQ と HTTP を差し替えて動かす。responses は doc_id → (HTTP 状態, 中身)。"""
    import json as _json
    import sys
    import types
    import bq_storage
    import edinet_gov_api as ega
    monkeypatch.setattr(g, "_append", lambda *a, **k: None)
    monkeypatch.setattr(g, "ensure_table", lambda c: None)
    monkeypatch.setattr(g, "ensure_seg_table", lambda c: None)
    monkeypatch.setattr(g, "_client", lambda cfg: _Client(list(done)))
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    monkeypatch.setattr(bq_storage, "measure", lambda c: {"tables": {}, "total_gb": 1.0})
    monkeypatch.setattr(bq_storage, "record", lambda *a: None)
    monkeypatch.setattr(bq_storage, "allowed", lambda *a: True)
    docs = [type("T", (), {"doc_id": d, "edinet_code": "E" + d, "sec": "1000", "fye": None, "d": None}) for d in responses]
    cl = _Client(list(done))
    cl.query = lambda sql, **k: _Rows(docs if "ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY d DESC" in sql else
                                      ([] if "= 2" in sql else [type("R", (), {"doc_id": d}) for d in done]))
    monkeypatch.setattr(g, "_client", lambda cfg: cl)

    def get(url, params=None, timeout=None):
        code, body = responses[url.rsplit("/", 1)[1]]
        return types.SimpleNamespace(status_code=code, content=body, json=lambda: _json.loads(body))
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=get))
    cfg = types.SimpleNamespace(EDINET_GOV_API_KEY="k", DATA_DIR=str(tmp_path), EDINET_GOV_MIN_INTERVAL_SEC=0)
    path = tmp_path / "edinet_gov_text_state.json"
    path.write_text(_json.dumps({"doc_ids": sorted(done), "seg_text_doc_ids": sorted(seg_text), "seg_rev_doc_ids": sorted(seg_rev),
                                 "seg_rev_version": g.SEG_REV_VERSION, "reconcile_version": g.RECONCILE_VERSION}), encoding="utf-8")
    st = g.run(cfg)
    return st, _json.loads(path.read_text(encoding="utf-8"))


def test_run_counts_gone_empty_error_and_segment_only_separately(monkeypatch, tmp_path):
    rows = [["jpcrp_cor:DescriptionOfBusinessTextBlock", "事業", "x", "", "", "", "", "", "本文"]]
    nothing = _zip([["jpcrp_cor:Other", "他", "x", "", "", "", "", "", "関係ない"]])
    gone = b'{"metadata":{"status":"404","message":"Not Found"}}'
    st, state = _run_with(monkeypatch, tmp_path,
                          {"NEW": (200, _zip(rows)), "EMPTY": (200, nothing), "GONE": (404, gone), "FAIL": (500, b"x"),
                           "SEGONLY": (200, nothing)},
                          done=["SEGONLY"], seg_text=[], seg_rev=["SEGONLY"])
    assert (st["new_docs"], st["no_text"], st["gone"], st["errors"], st["seg_only_docs"]) == (2, 1, 1, 1, 1)
    assert "FAIL" not in state["doc_ids"]                    # 取得の失敗は済みにしない
    assert "GONE" in state["doc_ids"] and "EMPTY" in state["doc_ids"]
