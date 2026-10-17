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


# ── 公正価値・帳簿価額（実際の有報の注記テキスト。J.フロント・三井物産・NTT の3通りの書き方） ──
TEXT_JFR = (
    '17．投資不動産 (1）増減表 投資不動産の取得原価、減価償却累計額及び減損損失累計額並びに帳簿価額の増減及び各連結会計'
    '年度末における公正価値は、以下のとおりであります。 取得原価 自己所有資産 使用権資産 合計 百万円 百万円 百万円 2'
    '024年３月１日 178,300 43,358 221,658 取得 1,255 452 1,708 売却又は処分 △3'
    '85 － △385 科目振替 287 28 315 その他（注） △77 △814 △891 2025年２月28日 17'
    '9,380 43,025 222,405 取得 2,437 － 2,437 売却又は処分 △832 － △832 売却目'
    '的で保有する資産への振替 △1,527 － △1,527 科目振替 215 91 306 その他（注） － 4,936 '
    '4,936 2026年２月28日 179,673 48,053 227,727 （注） 主に条件変更による変動が含まれて'
    'おります。 減価償却累計額及び減損損失累計額 自己所有資産 使用権資産 合計 百万円 百万円 百万円 2024年３月１日'
    ' △20,406 △19,267 △39,673 減価償却費 △1,899 △3,307 △5,207 減損損失 △22'
    '3 △459 △683 売却又は処分 365 － 365 科目振替 － △28 △28 その他 － △1 △1 2025'
    '年２月28日 △22,164 △23,064 △45,229 減価償却費 △1,837 △3,370 △5,207 減損'
    '損失 △29 △226 △256 売却又は処分 379 － 379 売却目的で保有する資産への振替 103 － 103 '
    '科目振替 △330 － △330 2026年２月28日 △23,877 △26,661 △50,539 帳簿価額及び公正'
    '価値 （単位：百万円） 前連結会計年度 （2025年２月28日） 当連結会計年度 （2026年２月28日） 帳簿価額 公'
    '正価値 帳簿価額 公正価値 投資不動産 177,176 298,490 177,187 314,421 投資不動産の公正'
    '価値は、当該不動産が所在する地域及び評価される不動産の種類に関する最近の鑑定経験を持ち、公認の専門的資格を有する社外の不'
    '動産鑑定士による不動産鑑定評価等に基づいており、その評価は、当該不動産の所在する国の評価基準に従い類似資産の取引価格を反'
    '映した市場証拠に基づいております。 投資不動産の公正価値ヒエラルキーは、観察可能でないインプットを含むことからレベル３に'
    '分類しております。なお、公正価値ヒエラルキーについては注記「39．金融商品」に記載しております。 (2）投資不動産からの'
    '収益及び費用 （単位：百万円） 前連結会計年度 （自 2024年３月１日 至 2025年２月28日） 当連結会計年度 （'
    '自 2025年３月１日 至 2026年２月28日） 賃貸料収入 22,258 23,120 直接営業費 12,287 1'
    '2,702 投資不動産からの賃貸料収入及びそれに伴って発生する直接営業費の金額は、それぞれ連結 損益計算書の「売上収益」'
    '及び「売上原価」に含まれております。 （3）コミットメント 投資不動産の購入に関するコミットメントについては、注記「42'
    '．コミットメント」をご 参照下さい。'
)
TEXT_MITSUI = (
    '12．投資不動産 前連結会計年度及び当連結会計年度における、投資不動産の取得原価、減価償却累計額及び減損損失累計額、帳簿'
    '価額及び公正価値は以下のとおりです。 〔取得原価、減価償却累計額及び減損損失累計額〕 取得原価 （百万円） 減価償却累計'
    '額及び減損損失累計額 （百万円） 前連結会計年度期首 （2024年4月1日） 376,478 94,225 前連結会計年'
    '度末 （2025年3月31日） 281,061 68,717 当連結会計年度末 （2026年3月31日） 250,174'
    ' 64,823 〔帳簿価額及び公正価値〕 帳簿価額 （百万円） 公正価値 （百万円） 前連結会計年度末 （2025年3月'
    '31日） 212,344 522,387 当連結会計年度末 （2026年3月31日） 185,351 520,668 前'
    '連結会計年度における投資不動産の処分による帳簿価額の減少額は77,787百万円です。当連結会計年度における投資不動産の処'
    '分による帳簿価額の増減額に金額的重要性があるものはありません。 前連結会計年度における投資不動産に係る賃貸料収入は25,'
    '369百万円、賃貸料収入に付随して発生した直接的な費用に金額的重要性はありません。当連結会計年度における投資不動産に係る'
    '賃貸料収入は22,131百万円、賃貸料収入に付随して発生した直接的な費用に金額的重要性はありません。 主要な投資不動産の'
    '公正価値は、所在する地域及び評価される不動産の種類に関する最近の鑑定経験を有し、かつ不動産鑑定士等の公認された適切な専門'
    '家としての資格を有する独立的鑑定人による評価、または当該評価に調整を加えたものを用いており、レベル3に分類しています。当'
    '該評価は、各物件の予想される賃料及び割引率等の重要な観察不能なインプットをもとに主にインカム・アプローチにより算定されて'
    'おり、当該不動産の所在する国の評価基準に基づいています。'
)
TEXT_NTT = (
    '3.8．投資不動産 (会計方針) 概要 投資不動産とは、賃貸収益又は売却益等のキャピタル・ゲイン、もしくはその両方を得る'
    'ことを目的として保有する不動産です。通常の営業過程で販売するものや、商品又はサービスの製造・販売、もしくはその他の管理目'
    '的で使用する不動産は含まれていません。 測定 NTTグループは投資不動産の当初認識後の測定について、取得原価から減価償却'
    '累計額及び減損損失累計額を控除した原価モデルを採用しています。 土地を除く投資不動産は、見積耐用年数にわたり、主として定'
    '額法により減価償却を行っており、見積耐用年数は、2～50年です。減価償却方法、耐用年数及び残存価額は期末日において見直し'
    'を行い、変更がある場合は、会計上の見積りの変更として将来に向かって適用しています。 (見積り及び見積りを伴う判断) 投資'
    '不動産の耐用年数に関する見積りを行っています。 (1)増減表 投資不動産の帳簿価額の増減及び取得原価、減価償却累計額及び'
    '減損損失累計額 帳簿価額 (単位:百万円) 前連結会計年度 (2024年4月 1日から 2025年3月31日まで) 当連'
    '結会計年度 (2025年4月 1日から 2026年3月31日まで) 期首残高 1,305,219 1,341,188 取'
    '得 108,857 195,490 売却又は処分 △35,745 △18,428 科目振替 2,575 4,919 減価'
    '償却 △27,351 △29,418 減損 △1,548 △1,796 外貨換算調整額 △3,832 20,642 売却'
    '目的で保有する資産への振替 △5,430 △572 その他 △1,557 11,767 期末残高 1,341,188 1'
    ',523,792 (注)減価償却は、連結損益計算書の「減価償却費」に、減損は「減損損失」にそれぞれ含まれています。 取得'
    '原価、減価償却累計額及び減損損失累計額 (単位:百万円) 取得原価 減価償却累計額及び減損損失累計額 前連結会計年度末('
    '2025年3月31日) 1,850,916 509,728 当連結会計年度末(2026年3月31日) 2,056,889'
    ' 533,097 NTTグループは、賃貸オフィスビル等を有しています。 投資不動産は、概ね独立したキャッシュ・インフロー'
    'を生成させるものとして識別される資産グループの最小単位を基礎としてグルーピングを行っています。 (2)減損損失 (会計方'
    '針) 「注記3.5．有形固定資産 (2)減損損失」の会計方針と同様です。 (見積り及び見積りを伴う判断) 投資不動産の減'
    '損に関する見積りを行っています。 前連結会計年度及び当連結会計年度において、投資不動産に係る重要な減損損失は発生していま'
    'せん。 (3)公正価値 投資不動産の公正価値 (単位:百万円) 前連結会計年度末 (2025年3月31日) 当連結会計年'
    '度末 (2026年3月31日) 公正価値 2,816,219 3,080,454 投資不動産の公正価値は、主として独立し'
    'た不動産鑑定の専門家による評価額であり、割引キャッシュ・フロー法による評価額又は観察可能な類似資産の市場取引価格等に基づ'
    'いています。 投資不動産の公正価値の測定に使用されるインプットの区分(参照:注記1.3．重要性のある会計方針 (3)公正'
    '価値)はレベル3に分類しています。 (4)投資不動産からの収益及び費用 (単位:百万円) 前連結会計年度 (2024年4'
    '月 1日から 2025年3月31日まで) 当連結会計年度 (2025年4月 1日から 2026年3月31日まで) 賃貸収'
    '益 129,805 133,901 賃貸費用 87,124 89,635 利益 42,681 44,266 投資不動産に'
    '係る売却益(△売却損) 15,859 28,785 投資不動産からの賃貸収益は連結損益計算書の「営業収益」に含まれていま'
    'す。 賃貸費用は賃貸収益に対応する費用(減価償却費、保繕費、保険料、租税公課等)であり、連結損益計算書の「経費」、「減価'
    '償却費」及び「租税公課」に含まれています。'
)


def test_fair_value_horizontal_table_jfr():
    # 増減表にも帳簿価額 177,187 が出るが、公正価値は後ろの「帳簿価額 公正価値」の表から拾う
    assert eg.fair_value_from_text(TEXT_JFR, [177176, 177187]) == [298490.0, 314421.0]


def test_fair_value_row_table_mitsui():
    assert eg.fair_value_from_text(TEXT_MITSUI, [212344, 185351]) == [522387.0, 520668.0]


def test_fair_value_only_row_ntt_has_no_book_value_in_table():
    assert eg.fair_value_from_text(TEXT_NTT, [1341188, 1523792]) == [2816219.0, 3080454.0]
    assert eg.fair_value_from_text(TEXT_NTT, [1523792]) == [3080454.0]


def test_fair_value_minimal_tables_ratio_negative_and_missing():
    t = "帳簿価額 公正価値 前期末 1,000 1,500 当期末 2,000 2,400"
    assert eg.fair_value_from_text(t, [1000, 2000]) == [1500.0, 2400.0]
    # 帳簿価額の 0.3〜20 倍の外（21倍・0.1倍）は捨てる
    assert eg.fair_value_from_text("公正価値は 帳簿価額 1,000 21,000", [1000]) is None
    assert eg.fair_value_from_text("公正価値は 帳簿価額 1,000 100", [1000]) is None
    # △ はマイナス＝範囲外で捨てる
    assert eg.fair_value_from_text("公正価値 1,000 △1,500", [1000]) is None
    # 公正価値の語が無い・帳簿価額が無い
    assert eg.fair_value_from_text("帳簿価額 1,000 1,500", [1000]) is None
    assert eg.fair_value_from_text("公正価値 1,000 1,500", []) is None
    # 片方の期末だけ取れる
    assert eg.fair_value_from_text("公正価値 帳簿価額 1,000 1,500 帳簿価額 9,999", [1000, 7777]) == [1500.0, None]


def _facts(prior, cur, text, scale=None):
    mk = lambda ctx, v: {"element": "jpigp_cor:InvestmentPropertyIFRS", "contextRef": ctx, "value": v, "scale": scale}
    return [mk("Prior1YearInstant", prior), mk("CurrentYearInstant", cur),
            {"element": "jpigp_cor:NotesInvestmentPropertyConsolidatedFinancialStatementsIFRSTextBlock",
             "contextRef": "CurrentYearDuration", "value": text[:600], "text": text, "is_text_block": True}]


def test_build_row_uses_current_year_and_million_yen():
    st, row = eg.build_row("E02513", "S1", "2026-03-31", _facts("212344000000", "185351000000", TEXT_MITSUI), now="N")
    assert st == "ok"
    assert row["fiscal_year"] == 2026 and row["book_value_m_yen"] == 185351.0 and row["fair_value_m_yen"] == 520668.0
    assert row["source"] == "edinet_gov_xbrl" and row["fv_bv_ratio"] == round(520668 / 185351, 4)
    # インライン XBRL は scale=6 で百万円単位の表記
    st, row = eg.build_row("E02513", "S1", "2026-03-31", _facts("212,344", "185,351", TEXT_MITSUI, scale="6"))
    assert st == "ok" and row["book_value_m_yen"] == 185351.0


def test_build_row_statuses():
    assert eg.build_row("E1", "S", "2026-03-31", [])[0] == "no_property"
    assert eg.build_row("E1", "S", "2026-03-31", _facts(None, "0", "公正価値"))[0] == "no_property"
    assert eg.build_row("E1", "S", "2026-03-31", _facts("1000000", "2000000000000", "説明のみ"))[0] == "no_fv"


def test_pick_targets_skips_same_doc_and_retries_error():
    latest = {"E1": "D1", "E2": "D2", "E3": "D3", "E4": "D4"}
    state = {"E1": {"status": "ok", "doc_id": "D1"}, "E2": {"status": "no_property", "doc_id": "OLD"},
             "E3": {"status": "error", "doc_id": "D3"}}
    assert eg.pick_targets(latest, state, 10) == ["E4", "E2", "E3"]
    assert eg.pick_targets(latest, state, 2) == ["E4", "E2"]


def test_ingest_without_key_does_nothing(monkeypatch):
    monkeypatch.delenv("EDINET_GOV_API_KEY", raising=False)
    assert eg.ingest(object())["skipped"] is True


def test_ok_codes_reads_ledger(tmp_path):
    import json
    class Cfg:
        DATA_DIR = str(tmp_path)
    (tmp_path / "edinet_gov_fv_state.json").write_text(json.dumps(
        {"E1": {"status": "ok"}, "E2": {"status": "no_fv"}}), encoding="utf-8")
    assert eg.ok_codes(Cfg) == {"E1"}
    assert eg.ok_codes(type("C", (), {"DATA_DIR": str(tmp_path / "none")})) == set()


def test_merge_sql_for_rental_property_fv_row(monkeypatch):
    """MERGE の SQL を組み、キー・列・ソース式が実テーブルの型どおりになる（BQ MCP の dryRun でも通した形）。"""
    from types import SimpleNamespace
    from edinet_bq_client import EDINETBQClient
    schema = [SimpleNamespace(name=n, field_type=t) for n, t in [
        ("edinet_code", "STRING"), ("fiscal_year", "INTEGER"), ("doc_id", "STRING"), ("book_value_m_yen", "FLOAT"),
        ("fair_value_m_yen", "FLOAT"), ("fv_bv_ratio", "FLOAT"), ("book_value_gap_m_yen", "FLOAT"),
        ("bs_investment_property_m_yen", "FLOAT"), ("extraction_status", "STRING"), ("source", "STRING"),
        ("updated_at", "TIMESTAMP")]]
    seen = {}

    class _Job:
        def result(self):
            return []

    class _Client:
        def get_table(self, ref):
            return SimpleNamespace(schema=schema)

        def query(self, sql, job_config=None):
            seen["sql"] = sql
            return _Job()

    bq = EDINETBQClient.__new__(EDINETBQClient)
    bq.client, bq.project = _Client(), "p"
    _st, row = eg.build_row("E02513", "S1", "2026-03-31", _facts("212344000000", "185351000000", TEXT_MITSUI), now="2026-10-18T00:00:00+00:00")
    assert bq.merge_rows("rental_property_fv", [row], ["edinet_code", "fiscal_year"], dataset="real_estate") is True
    sql = seen["sql"]
    assert "MERGE `p.real_estate.rental_property_fv` AS T" in sql
    assert "T.`edinet_code` = S.`edinet_code` AND T.`fiscal_year` = S.`fiscal_year`" in sql
    assert "SAFE_CAST(JSON_VALUE(row, '$.fair_value_m_yen') AS FLOAT64) AS `fair_value_m_yen`" in sql
    assert "SAFE_CAST(JSON_VALUE(row, '$.updated_at') AS TIMESTAMP)" in sql
