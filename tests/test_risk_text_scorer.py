"""risk_text_scorer：前期と今期の「事業等のリスク」を比べて悪化の印を付ける。"""
import risk_text_scorer as rt


def test_new_severe_term_flags():
    prev = "当社は為替変動のリスクがあります。原材料価格の上昇リスクがあります。"
    cur = prev + "当社は継続企業の前提に重要な疑義を生じさせるような事象が存在しております。減損損失を計上しました。"
    sc = rt.compare(prev, cur)
    assert sc["flag"] and sc["new_terms"]["継続企業の前提"] == 1 and sc["severity"] == 12 and sc["new_sentences"] == 1   # 20文字未満の文は数えない


def test_same_text_no_flag():
    t = "当社は減損のリスクがあります。訴訟のリスクがあります。" * 3
    sc = rt.compare(t, t)
    assert not sc["flag"] and sc["severity"] == 0 and sc["change_ratio"] == 0.0


def test_rows_need_two_years():
    recs = [{"edinet_code": "E1", "sec_code": "11110", "fiscal_year_end": "2026-03-31", "doc_id": "d2", "raw_text": "債務超過になりました。"},
            {"edinet_code": "E1", "sec_code": "11110", "fiscal_year_end": "2025-03-31", "doc_id": "d1", "raw_text": "特になし。"},
            {"edinet_code": "E2", "sec_code": "22220", "fiscal_year_end": "2026-03-31", "doc_id": "d3", "raw_text": "債務超過"}]
    rows = rt.build_rows(recs)
    assert [(r["edinet_code"], r["flag"], r["fy_prev"]) for r in rows] == [("E1", True, "2025-03-31")]
