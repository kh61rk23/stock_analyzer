"""notify_text: 取得メールの文面。英語のキーや JSON を出さず、警告には影響と次の動きを書く。"""
import notify_text as nt
import workflow_notify as wn
from datetime import datetime


def _rec(step, *hl, rc=0):
    return {"step": step, "rc": rc, "seconds": 5, "highlights": list(hl)}


def test_every_workflow_step_has_description():
    for s in ("jp_filings", "edinet_gov_docs", "edinet_gov_text", "risk_text_scorer", "sec_filings", "price_universe",
              "us_company_facts", "us_segments", "us_exposure_builder", "us_taxonomy_mapper", "market_positioning"):
        assert nt.describe(s)[1], s
    for m in ("edinetdb_ingest", "edinet_doc_index", "reit_ingest", "fudosan_ingest", "edinetdb_extras", "seisakudb",
              "rental_property_fv", "edinet_gov_fv"):
        assert nt.MODULES[m][1]


def test_edinet_docs_success_sentence_has_no_json():
    e = nt.explain_step(_rec("edinet_gov_docs", '✅ edinet_gov_docs: {"days": 7, "docs": 1922, "fact_docs": 47, "facts": 1232, '
                            '"errors": 0, "facts_paused": false, "oldest": "2016-11-10", "budget_out": false, "range": ["2026-11-04", "2026-11-10"]}'))
    t = e["lines"][0][1]
    assert "7日分" in t and "1,922件" in t and "{" not in t and "facts" not in t and e["mark"] == "✅"


def test_edinet_docs_backfill_end_is_not_a_warning():
    e = nt.explain_step(_rec("edinet_gov_docs", '✅ edinet_gov_docs: {"days": 3, "docs": 10, "errors": 0, "backfill_end": "2016-11-09"}'))
    assert e["mark"] == "✅" and any("過去分の取り込みは完了" in t for _, t in e["lines"])


def test_edinet_docs_errors_explain_impact_and_next():
    e = nt.explain_step(_rec("edinet_gov_docs", "❌ 一覧 2026-11-03: API 429 Too Many",
                             '⚠️ edinet_gov_docs: {"days": 1, "errors": 1, "error_reasons": "API 429 x1", "api_stopped": "x", "api_fatal": true}'))
    texts = " ".join(t for _, t in e["lines"])
    assert "回数制限" in texts and "影響" in texts and "次" in texts and e["mark"] == "⚠️"


def test_day_error_lines_collapse_without_summary():
    e = nt.explain_step(_rec("edinet_gov_docs", "❌ 一覧 2016-11-09: API 404 Not Found", "❌ 一覧 2016-11-08: API 404 Not Found"))
    assert len(e["lines"]) == 1 and "2日" in e["lines"][0][1]


def test_seisakudb_429_and_budget():
    stop = nt.explain_step(_rec("jp_filings", "⚠️ [warn] seisakudb: 直近500件 / 個社216社 / 経済安保114件 / API 547回 / 停止: HTTP 429（レート上限）"))
    txt = " ".join(t for _, t in stop["lines"])
    assert "429" in txt and "次の回で続き" in txt and stop["mark"] == "⚠️"
    ok = nt.explain_step(_rec("jp_filings", "✅ [success] seisakudb: 直近500件 / 個社305社 / 経済安保114件 / API 950回 / 停止: budget"))
    assert ok["mark"] == "✅" and any("正常" in t for _, t in ok["lines"])


def test_jp_filings_summary_names_module_in_japanese():
    e = nt.explain_step(_rec("jp_filings", "⚠️ jp_filings: 6モジュール（成功4・見送り1・警告1・失敗0）・要確認 seisakudb → jp_filings_2026-11-10.json"))
    assert "政策DB" in e["lines"][0][1] and "seisakudb" not in e["lines"][0][1]


def test_price_universe_and_python_repr_dict():
    e = nt.explain_step(_rec("price_universe", "✅ price_universe_jp: success jp 取得3817/3820（99.9%・下限90%） 未取得3 dead198 行3307 122.1秒"))
    assert "日本株" in e["lines"][0][1] and "3,817銘柄" in e["lines"][0][1]
    e = nt.explain_step(_rec("us_exposure_builder", "✅ us_exposure_builder: {'rows': 2168, 'companies': 1159, 'countries': 67, 'written': 2168}"))
    assert "1,159社" in e["lines"][0][1]


def test_unknown_line_passes_through_and_mail_uses_it():
    e = nt.explain_step(_rec("zzz", "⚠️ 何か起きた"))
    assert e["lines"] == [("⚠️", "何か起きた")]
    subj, body = wn.build_mail("t", "w", [_rec("edinet_gov_docs", '✅ edinet_gov_docs: {"days": 1, "docs": 2}')], {}, {}, None, "",
                               datetime(2026, 11, 10))
    assert "何をする段" in body and "政府 EDINET" in body and "✅" in subj
