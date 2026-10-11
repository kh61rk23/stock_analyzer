"""arxiv_store: 論文を1論文1行にまとめる・キーワード抽出・md レポートの読み取り・タイトル翻訳。"""
import arxiv_store as st


def _p(pid, cat, score=0, kws=None, title="Tail risk of X"):
    return {"id": pid, "category": cat, "title": title, "summary": "We study.", "authors": "A, B",
            "published": "2026-10-03T12:00:00Z", "url": f"https://arxiv.org/abs/{pid}",
            "investment_score": score, "matched_keywords": kws or []}


def test_keywords_of_drops_multiplier_notes():
    assert st.keywords_of(["[T+3]tail risk", "[S+1]volatility", "[×カテゴリ1.30(q-fin.TR)]", "[S+1]volatility"]) == ["tail risk", "volatility"]


def test_build_rows_merges_categories_and_flags_ideas():
    rows = st.build_rows([_p("2610.1", "q-fin.PM", 4, ["[S+1]news"]), _p("2610.1", "q-fin.ST", 4), _p("2610.2", "econ.EM", 1)],
                         3, "2026-10-05", fetched_at="2026-10-05T00:00:00+00:00", titles_ja={"2610.1": "Xのテールリスク"})
    by = {r["paper_id"]: r for r in rows}
    assert len(rows) == 2
    assert by["2610.1"]["categories"] == "q-fin.PM,q-fin.ST" and by["2610.1"]["is_idea"] is True
    assert by["2610.1"]["title_ja"] == "Xのテールリスク" and by["2610.1"]["keywords"] == "news"
    assert by["2610.2"]["is_idea"] is False and by["2610.2"]["published_date"] == "2026-10-03"


def test_job_id_is_stable_for_same_run():
    rows = st.build_rows([_p("a", "x"), _p("b", "y")], 3, "2026-10-05")
    assert st.job_id_for(rows) == st.job_id_for(list(reversed(rows)))


def test_parse_report_reads_ideas_and_table_rows():
    md = """### [Mixture-of-Experts for Order Execution](https://arxiv.org/abs/2610.03369)
- **カテゴリ**: q-fin.TR
- **著者**: Alexander Ardaiz, Varun Budati
- **スコア**: 5 点
- **マッチ**: [T+3]tail risk, [×カテゴリ1.30(q-fin.TR)]
- **概要**: Deep reinforcement-learning policies...

| カテゴリ | タイトル | スコア |
|---------|---------|-------|
| q-fin.PM | [Optimal portfolio...](https://arxiv.org/abs/2610.00001) | 2 |
"""
    ps = st.parse_report(md)
    assert [p["id"] for p in ps] == ["2610.03369", "2610.00001"]
    assert ps[0]["investment_score"] == 5 and ps[0]["category"] == "q-fin.TR" and st.keywords_of(ps[0]["matched_keywords"]) == ["tail risk"]
    assert ps[1]["investment_score"] == 2 and ps[1]["category"] == "q-fin.PM"


def test_translate_titles_only_with_free_key_and_survives_errors():
    class R:
        def __init__(self, code, body=None):
            self.status_code, self._b = code, body or {}
        def json(self):
            return self._b
    calls = []
    def ok(url, headers, data, timeout):
        calls.append(url)
        texts = [v for k, v in data if k == "text"]
        return R(200, {"translations": [{"text": "訳:" + t} for t in texts]})
    assert st.translate_titles([_p("a", "x")], "pro-key", post=ok) == {}
    assert st.translate_titles([_p("a", "x"), _p("b", "y")], "k:fx", post=ok) == {"a": "訳:Tail risk of X", "b": "訳:Tail risk of X"}
    assert calls[0].startswith("https://api-free.deepl.com")
    assert st.translate_titles([_p("a", "x")], "k:fx", post=lambda *a, **k: R(456)) == {}
