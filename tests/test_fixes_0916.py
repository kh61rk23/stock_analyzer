# 2026-09-16: EdinetDB の分析利用・書類一覧（doc_index）・NEDO採択の不具合の回帰テスト
import pathlib
import re
import types

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (ROOT / name).read_text(encoding="utf-8")


def _code_lines(name):
    return "\n".join(line for line in _src(name).splitlines()
                     if not line.lstrip().startswith("#"))


# ── EdinetDB 財務が1年分に潰れていた ─────────────────────────────
class TestEdinetBundleYears:
    def test_bundle_partitions_by_fiscal_year(self):
        """fiscal_year_end が全行 NULL のため全年度が1パーティションに潰れていた"""
        src = _code_lines("edinet_bq_client.py")
        assert "COALESCE(f.fiscal_year, EXTRACT(YEAR FROM f.fiscal_year_end))" in src

    def test_latest_financials_not_ordered_by_null_column(self):
        """最新年度の選択が NULL 列の並びに依存していた"""
        src = _code_lines("edinet_bq_client.py")
        assert not re.search(r"ORDER BY\s+fiscal_year_end\s+DESC\s*(,\s*section_type)?\s*$", src, re.M)
        assert "COALESCE(fiscal_year, EXTRACT(YEAR FROM fiscal_year_end)) DESC" in src

    def test_sector_report_does_not_print_none(self):
        src = _code_lines("base_agent.py")
        assert "gov.get('submit_date') or 'N/A'" in src
        assert "gov.get('period_end') or 'N/A'" in src

    def test_data_bridge_has_no_hardcoded_api_key(self):
        assert not re.search(r"edb_[0-9A-Za-z]{16,}", _src("viewer/data_bridge.py"))


# ── 書類一覧（doc_index）が 2026-04-26 から増えていなかった ───────────
class TestDocIndexFromFinancials:
    def test_map_edinetdb_doc_index_uses_real_doc_id(self):
        import edinetdb_ingestor as m
        row = {"doc_id": "S100XUEF", "submit_date_time": "2026-03-26T15:00:00",
               "accounting_standard": "IFRS"}
        out = m.map_edinetdb_doc_index(row, {"name": "テスト"}, edinet_code="E02416",
                                       company_code="7203", now="2026-09-16T00:00:00")
        assert out["doc_id"] == "S100XUEF"
        assert out["disclosure_date"] == "2026-03-26"
        assert out["ifrs_flag"] is True
        assert out["doc_type"] == "有価証券報告書"
        assert out["edinet_code"] == "E02416"

    def test_map_edinetdb_doc_index_skips_synthetic_ids(self):
        import edinetdb_ingestor as m
        assert m.map_edinetdb_doc_index({"doc_id": "E02416_2025"}, {}) is None
        assert m.map_edinetdb_doc_index({}, {}) is None

    def test_ingestor_writes_doc_index(self):
        src = _code_lines("edinetdb_ingestor.py")
        assert 'merge_rows("doc_index", doc_rows, ["doc_id"])' in src
        assert '"doc_index_rows": doc_index_rows' in src

    def test_doc_indexer_fills_empty_doc_id_from_financials(self):
        import edinet_doc_indexer as m
        item = {"date": "2026-03-26", "doc_id": "", "fiscal_year": 2025,
                "pdf_url": "", "title": "有価証券報告書", "type": "yuho"}
        company = {"sec_code": "72030", "edinet_code": "E02144", "name": "テスト"}
        rows = m.build_doc_index_rows([item], company, now="2026-09-16T00:00:00",
                                      doc_id_by_year={2025: "S100ABCD"})
        assert len(rows) == 1
        assert rows[0]["doc_id"] == "S100ABCD"
        assert rows[0]["company_code"] == "7203"
        assert str(rows[0]["disclosure_date"])[:10] == "2026-03-26"

    def test_doc_indexer_without_map_still_skips(self):
        import edinet_doc_indexer as m
        item = {"date": "2026-03-26", "doc_id": "", "fiscal_year": 2025, "type": "yuho"}
        assert m.build_doc_index_rows([item], {"sec_code": "72030"}) == []


# ── NEDO 採択が常に0件だった ─────────────────────────────────────
_DECISION_HTML = """
<html><body>
<h1>決定 2026年度「AI活用基盤の研究開発」に係る実施体制の決定について</h1>
<p class="text-right">2026年9月9日</p>
<h2>1．実施予定先</h2>
<p>株式会社アトム<br>KDDI株式会社（幹事）<br>国立大学法人東京大学</p>
<h2>2．お問い合わせ先</h2>
<p>株式会社ダミー</p>
<a href="/content/100999999.pdf">資料1：実施予定先一覧</a>
<a href="/content/100999998.pdf">審査委員一覧</a>
</body></html>
"""


class TestNedoDecisionPages:
    def test_discover_decision_urls(self):
        import nedo_selection_scraper as m
        html = ('<a href="/koubo/CD3_100434.html">a</a>'
                '<a href="/koubo/CD3_100434.html">dup</a>'
                '<a href="/koubo/SR3_100022.html">b</a>'
                '<a href="/koubo/other.html">x</a>')
        urls = m.discover_decision_urls(max_pages=5, fetch=lambda url: html)
        assert urls == ["https://www.nedo.go.jp/koubo/CD3_100434.html",
                        "https://www.nedo.go.jp/koubo/SR3_100022.html"]
        assert m.discover_decision_urls(max_pages=1, fetch=lambda url: html) == urls[:1]
        assert m.discover_decision_urls(fetch=lambda url: None) == []

    def test_parse_decision_page_heading_and_pdf(self, monkeypatch):
        import nedo_selection_scraper as m
        monkeypatch.setattr(m, "_pdf_text", lambda data: "日立建機株式会社\n株式会社メルカリ")
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return b"%PDF"

        recs = m.parse_nedo_decision_page(
            _DECISION_HTML, "https://www.nedo.go.jp/koubo/CD3_100434.html", fake_fetch)
        names = [r["raw_company_name"] for r in recs]
        assert "株式会社アトム" in names
        assert "KDDI株式会社" in names
        assert "日立建機株式会社" in names and "株式会社メルカリ" in names
        assert "株式会社ダミー" not in names          # 次の見出し以降は拾わない
        assert len(fetched) == 1 and fetched[0].endswith("100999999.pdf")  # 審査委員は除外
        assert all(r["adoption_date"] == "2026-09-09" for r in recs)
        assert all(r["project_name"].startswith("2026年度") for r in recs)

    def test_to_jp_code(self):
        import nedo_selection_scraper as m
        assert m._to_jp_code("6501.T") == "6501"
        assert m._to_jp_code("130a.t") == "130A"
        assert m._to_jp_code("7203") == "7203"
        assert m._to_jp_code("AAPL") == ""
        assert m._to_jp_code(None) == ""

    def test_name_map_uses_company_table_and_4digit_codes(self):
        import nedo_selection_scraper as m
        seen_sql = []

        def query_to_df(sql):
            seen_sql.append(sql)
            return pd.DataFrame([{"securities_code": "6758", "company_name": "ソニーグループ株式会社"}])

        bq = types.SimpleNamespace(dataset="edinet", query_to_df=query_to_df)
        results = {"screener": {"candidates": [
            {"ticker": "6501.T", "name": "株式会社日立製作所"},
            {"ticker": "AAPL", "name": "Apple Inc."},
        ]}}
        name_map = m._build_company_name_map(results, bq_client=bq)
        assert "6501" in name_map.values()
        assert "6758" in name_map.values()
        assert all(re.fullmatch(r"[0-9][0-9A-Z]{3}", v) for v in name_map.values())
        assert "edinet.company" in seen_sql[0]

    def test_short_names_do_not_partial_match(self):
        import nedo_selection_scraper as m
        name_map = {m._normalize_company_name("アトム"): "1111"}
        res = m.match_company_to_code("株式会社アトムリビンテック", name_map)
        assert res["match_method"] != "partial"

    def test_scraper_merges_instead_of_appending(self):
        src = re.sub(r"\s+", " ", _code_lines("nedo_selection_scraper.py"))
        assert 'merge_rows( "policy_support", bq_rows, ["source_url", "raw_company_name"] )' in src

    def test_config_has_decision_page_limit(self):
        import config
        assert getattr(config, "NEDO_DECISION_MAX_PAGES", None) == 20 or \
            getattr(getattr(config, "Config", object), "NEDO_DECISION_MAX_PAGES", None) == 20


class TestPolicySupportLookup:
    def _client(self, captured):
        import edinet_bq_client as m
        client = object.__new__(m.EDINETBQClient)
        client.dataset = "edinet"

        def query_to_df(sql):
            captured.append(sql)
            return pd.DataFrame([
                {"code_key": "6501", "securities_code": "6501.T", "project_name": "A",
                 "adoption_date": "2026-09-09", "project_type": "ai_iot",
                 "match_confidence": 1.0},
                {"code_key": "7203", "securities_code": "7203", "project_name": "B",
                 "adoption_date": "2026-09-01", "project_type": "general",
                 "match_confidence": 0.85},
            ])

        client.query_to_df = query_to_df
        return client

    def test_ticker_suffix_is_normalized(self):
        """base_agent は "6501.T" のまま渡すため、4桁格納行と一致しなかった"""
        captured = []
        client = self._client(captured)
        rows = client.get_policy_support_adoptions("6501.T")
        assert [r["project_name"] for r in rows] == ["A"]
        assert "code_key" not in rows[0]
        assert [r["project_name"] for r in client.get_policy_support_adoptions("7203")] == ["B"]

    def test_single_query_for_all_tickers(self):
        """業種ごとに銘柄別で引くと BQ が本数分課金されるため1本に集約する"""
        captured = []
        client = self._client(captured)
        for tk in ("6501.T", "7203.T", "9984.T", "6501"):
            client.get_policy_support_adoptions(tk)
        assert len(captured) == 1

    def test_empty_code_skips_query(self):
        captured = []
        assert self._client(captured).get_policy_support_adoptions("") == []
        assert captured == []

    def test_query_failure_is_not_retried_per_ticker(self):
        import edinet_bq_client as m
        client = object.__new__(m.EDINETBQClient)
        client.dataset = "edinet"
        calls = []

        def boom(sql):
            calls.append(sql)
            raise RuntimeError("bq down")

        client.query_to_df = boom
        assert client.get_policy_support_adoptions("6501.T") == []
        assert client.get_policy_support_adoptions("7203.T") == []
        assert len(calls) == 1
