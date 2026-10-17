import edinet_gov_xbrl as eg

XML = '''<xbrli:xbrl>
<jpigp_cor:InvestmentPropertyIFRS contextRef="CurrentYearInstant" unitRef="JPY" decimals="-6">123000000</jpigp_cor:InvestmentPropertyIFRS>
<jpigp_cor:NotesInvestmentPropertyIFRSTextBlock contextRef="CurrentYearDuration">&lt;p&gt;公正価値 456&lt;/p&gt;</jpigp_cor:NotesInvestmentPropertyIFRSTextBlock>
<jppfs_cor:Land contextRef="CurrentYearInstant" unitRef="JPY">9</jppfs_cor:Land>
</xbrli:xbrl>'''


def test_facts_pick_investment_property_elements_only():
    f = eg.facts_from_xbrl(XML)
    assert [x["element"] for x in f] == ["jpigp_cor:InvestmentPropertyIFRS", "jpigp_cor:NotesInvestmentPropertyIFRSTextBlock"]
    assert f[0]["unitRef"] == "JPY" and f[0]["value"] == "123000000" and f[1]["is_text_block"] is True


def test_probe_without_key_does_nothing(monkeypatch):
    monkeypatch.delenv("EDINET_GOV_API_KEY", raising=False)
    assert eg.probe(object(), ["E03516"]) == 0
