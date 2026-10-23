"""sec_filings: 日次の提出一覧・8-K の項目・13D/13G の XML の読み取り（SEC・BigQuery には触らない）。"""
from datetime import date

import sec_filings as s

MASTER = """Description:           Daily Index of EDGAR Dissemination Feed by Company Name
CIK|Company Name|Form Type|Date Filed|File Name
--------------------------------------------------------------------------------
1000209|MEDALLION FINANCIAL CORP|8-K|20261021|edgar/data/1000209/0001193125-26-427818.txt
1004989|SPAR Group, Inc.|SCHEDULE 13G|20261021|edgar/data/1004989/0002157290-26-000002.txt
bad|line|x|y|z
"""


def test_parse_master_and_urls():
    rows, names = s.parse_master(MASTER, date(2026, 10, 21))
    assert [r["form"] for r in rows] == ["8-K", "SCHEDULE 13G"]
    assert rows[0] == {"filed_date": "2026-10-21", "cik": 1000209, "form": "8-K",
                       "accession": "0001193125-26-427818", "items": None}
    assert names[1004989] == "SPAR Group, Inc."
    assert s.index_url(date(2026, 10, 21)).endswith("/daily-index/2026/QTR4/master.20261021.idx")
    assert s.filing_index_url(1000209, "0001193125-26-427818").endswith(
        "/data/1000209/000119312526427818/0001193125-26-427818-index.htm")


def test_items_and_xml_link():
    html = "<td>Item 9.01: Financial Statements</td><td>Item 2.02: Results</td><td>Item 2.02</td>"
    assert s.items_from_index(html) == "2.02,9.01"
    assert s.items_from_index("no items") is None
    idx = ('<a href="/Archives/edgar/data/1/2/xslSCHEDULE_13G_X02/primary_doc.xml">x</a>'
           '<a href="/Archives/edgar/data/1/2/primary_doc.xml">y</a>')
    assert s.xml_doc_from_index(idx) == "/Archives/edgar/data/1/2/primary_doc.xml"


def test_parse_13dg_per_reporting_person():
    xml = """<edgarSubmission><formData><coverPageHeader>
      <securitiesClassTitle>Common Stock</securitiesClassTitle>
      <eventDateRequiresFilingThisStatement>05/31/2026</eventDateRequiresFilingThisStatement>
      <issuerInfo><issuerCik>0001004989</issuerCik><issuerName>Test Corp</issuerName>
      <issuerCusipNumber>784933103</issuerCusipNumber></issuerInfo>
      <designateRulePursuantThisScheduleFiled>Rule 13d-1(c)</designateRulePursuantThisScheduleFiled>
    </coverPageHeader>
    <coverPageHeaderReportingPersonDetails><reportingPersonName>Fund A</reportingPersonName>
      <citizenshipOrOrganization>DE</citizenshipOrOrganization><reportingPersonBeneficiallyOwnedNumberOfShares>
      <soleVotingPower>100.00</soleVotingPower><sharedVotingPower>0.00</sharedVotingPower>
      <soleDispositivePower>100.00</soleDispositivePower><sharedDispositivePower>0.00</sharedDispositivePower>
      </reportingPersonBeneficiallyOwnedNumberOfShares>
      <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>1,000.00</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
      <classPercent>7.5</classPercent><typeOfReportingPerson>IA</typeOfReportingPerson>
    </coverPageHeaderReportingPersonDetails>
    <coverPageHeaderReportingPersonDetails><reportingPersonName>Person B</reportingPersonName>
      <classPercent>5.1</classPercent><typeOfReportingPerson>IN</typeOfReportingPerson>
    </coverPageHeaderReportingPersonDetails></formData></edgarSubmission>"""
    meta = {"filed_date": "2026-10-21", "accession": "A-1", "form": "SCHEDULE 13G", "cik": 2157290}
    rows = s.parse_13dg(xml, meta)
    assert [(r["person_name"], r["percent"], r["person_type"]) for r in rows] == [
        ("Fund A", 7.5, "IA"), ("Person B", 5.1, "IN")]
    a = rows[0]
    assert a["issuer_cik"] == 1004989 and a["issuer_cusip"] == "784933103" and a["event_date"] == "2026-05-31"
    assert a["shares"] == 1000.0 and a["sole_voting"] == 100.0 and a["filer_cik"] == 2157290
