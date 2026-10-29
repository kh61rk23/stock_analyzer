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
