"""米国の取得の通知メールの1行（取り直しが0社・新しい四半期なしでも理由が出る）。"""
import us_company_facts as ucf
import us_segments as seg


def test_company_facts_zero_queue_says_why():
    line = ucf.summary_line({"queue": 0, "coverage": {"annual_rows": 51206, "tickers_with_annual": 4325}})
    assert "会社なし" in line and "51,206行" in line


def test_company_facts_counts():
    line = ucf.summary_line({"queue": 143, "ok": 140, "no_facts": 2, "error": 1, "fact_rows_loaded": 28324})
    assert "143社" in line and "成功 140" in line and "失敗 1" in line and "28,324行" in line


def test_segments_no_new_quarter():
    assert "公開待ち 2026q4" in seg.summary_line({"quarters_done": [], "missing": ["2026q4"]})


def test_segments_added():
    assert "2026q4（30,123行）" in seg.summary_line({"quarters_done": ["2026q4"], "rows": 30123})
