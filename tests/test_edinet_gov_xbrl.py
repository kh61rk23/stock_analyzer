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


def _zip(files: dict) -> bytes:
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, body in files.items():
            z.writestr(n, body)
    return buf.getvalue()


def test_zip_prefers_instance_then_inline_xbrl():
    facts, fmt = eg.facts_from_zip(_zip({"XBRL/PublicDoc/a.xbrl": XML}))
    assert fmt == "xbrl" and len(facts) == 2
    htm = ('<ix:nonFraction name="jpigp_cor:InvestmentPropertyIFRS" contextRef="CurrentYearInstant" '
           'unitRef="JPY" decimals="-6" scale="6"><span>1,234</span></ix:nonFraction>'
           '<ix:nonFraction name="jppfs_cor:Land" contextRef="CurrentYearInstant">5</ix:nonFraction>')
    facts, fmt = eg.facts_from_zip(_zip({"XBRL/PublicDoc/0101010_honbun_ixbrl.htm": htm}))
    assert fmt == "ixbrl" and [f["element"] for f in facts] == ["jpigp_cor:InvestmentPropertyIFRS"]
    assert facts[0]["value"] == "1,234" and facts[0]["scale"] == "6"
