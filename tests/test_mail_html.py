# 週次・月次メールの共通 HTML 部品と、週次・月次メールの組み立て
import re
from pathlib import Path

import pytest

import mail_html as mh

REAL_WEEKLY = Path("/tmp/ss2/stock_data/insights/weekly/weekly_2026-11-02.md")
REAL_INSIGHTS = Path("/tmp/ss2/stock_data/insights")


def test_md_to_html_list_is_wrapped_and_closed():
    h = mh.md_to_html("- a\n- b\n\n本文")
    assert h.count("<ul") == 1 and h.count("</ul>") == 1 and h.count("<li") == 2
    assert h.index("</ul>") < h.index("本文")


def test_md_to_html_checkbox():
    h = mh.md_to_html("- [ ] 未\n- [x] 済")
    assert "☐ 未" in h and "☑ 済" in h and "[ ]" not in h


def test_md_to_html_table_has_thead_zebra_and_right_align():
    h = mh.md_to_html("| 銘柄 | 値 |\n|---|---|\n| A | 1,234 |\n| B | +5.0% |\n| C | 9 |")
    assert "<thead>" in h and "<th" in h and "overflow-x:auto" in h
    assert "#f9fafb" in h
    assert "text-align:right" in h
    assert h.count("<tr") == 4   # 見出し行 + 3 行


def test_md_to_html_escapes_text():
    h = mh.md_to_html("<script>alert(1)</script> & **ok**")
    assert "<script>" not in h and "&lt;script&gt;" in h and "<b>ok</b>" in h


def test_md_to_html_inline_and_blocks():
    h = mh.md_to_html("### 見出し\n> 引用\n---\n*強調* と `code` と [link](https://example.com/a?b=1&c=2)")
    assert "<h4" in h and "<blockquote" in h and "<hr" in h and "<i>強調</i>" in h
    assert "<code" in h and 'href="https://example.com/a?b=1&amp;c=2"' in h


def test_md_to_html_collapses_blank_lines():
    assert "<br>" not in mh.md_to_html("a\n\n\n\n\nb")


def test_strip_dev_codes_and_relabel():
    f = mh.strip_dev_codes
    assert f("緊急縮退アラート（Wave 6 - 第4セッション）") == "緊急縮退アラート"
    assert f("国別地政学リスク（Wave 6 - G-N）") == "国別地政学リスク"
    assert f("リスク制約フィルター（C-1/C-2）") == "リスク制約フィルター"
    assert f("審議キュー候補（B部門 E3）") == "審議キュー候補"
    assert f("ボラ調整後ランキング TOP10（FE-1）") == "ボラ調整後ランキング TOP10"
    assert f("🟢 C-1: VaR制約") == "🟢 VaR制約"
    assert f("注目決算候補（I-2 保守度×加速スコア）") == "注目決算候補（保守度×加速スコア）"
    assert f("日経225（Inference Engine）") == "日経225（Inference Engine）"
    assert mh.relabel("動的追加 / ↑DYNAMIC") == "自動追加 / ↑自動追加"


def test_fmt_yen():
    assert mh.fmt_yen(4000000000) == "40億円"
    assert mh.fmt_yen(8440000) == "844万円"
    assert mh.fmt_yen(36500000000) == "365億円"
    assert mh.fmt_yen(None) == "—"


def test_page_uses_table_kpi_and_viewport():
    p = mh.page("題", "副題", [("a", "1", "#000"), ("b", "2", None)], "<p>本文</p>", "foot")
    assert "viewport" in p and "max-width:640px" in p and "linear-gradient" in p
    assert "display:flex" not in p and "<table" in p and "@media" in p and "max-width:480px" in p


def test_section_card_severity_color():
    assert "#ef4444" in mh.section_card("t", "b", severity="high")


# ---- 週次 ---------------------------------------------------------------
def test_weekly_screener_table_uses_full_names_and_no_rec_column():
    import weekly_report_generator as w
    cands = [{"ticker": "ZZZZ.T", "name": "Allied Telesis Holdings K.K.", "sector_name": "証券・商品先物取引業",
              "pass_filters": True, "composite_score": 98, "risk_adjusted_score": 94, "per": 9.4, "pbr": 1.4,
              "roe": 17.0, "dividend_yield": 0.039}]
    md = "\n".join(w._section_screener({"screener": {"candidates": cands, "screened_count": 1, "pass_count": 1}}))
    assert "Allied Telesis Holdings K.K." in md and "Allied Teles |" not in md
    assert "証券・商品先物取引業" in md and "推奨" not in md


def test_weekly_cross_signal_suppressed_when_no_types():
    text = (Path(__file__).resolve().parent.parent / "weekly_report_generator.py").read_text(encoding="utf-8")
    assert "if cross_types:" in text


def test_weekly_title_has_no_monthly_marker():
    text = (Path(__file__).resolve().parent.parent / "weekly_report_generator.py").read_text(encoding="utf-8")
    assert "FinBERT ニュース感情分析（月次）" not in text


def test_weekly_html_small_and_cards_for_sample():
    import weekly_report_generator as w
    md = "# T\n\n## 🧠 今週の投資判断サマリー（Inference Engine）\n\n- a\n- b\n\n## 🌐 国別地政学リスク（Wave 6 - G-N）\n\n本文1\n本文2\n"
    h = w.build_weekly_email_html(md, {"report_date": "2026-11-02", "top3_sectors": ["電気機器"], "screener_pass": 5})
    assert "Wave 6" not in h and "G-N" not in h and "国別地政学リスク" in h and "<ul" in h
    assert "display:flex" not in h


@pytest.mark.skipif(not REAL_WEEKLY.exists(), reason="実データの週次 md が無い")
def test_weekly_html_under_100kb_from_real_md():
    import weekly_report_generator as w
    md = REAL_WEEKLY.read_text(encoding="utf-8")
    h = w.build_weekly_email_html(md, {"report_date": "2026-11-02", "top3_sectors": ["電気機器"], "screener_pass": 192})
    assert len(h.encode("utf-8")) < 100_000
    assert "個別銘柄スクリーニング" in h and "ポジションサイジング" in h
    for bad in ("Wave 6", "Wave 7", "(FE-1)", "（FE-1）", "動的追加", "DYNAMIC"):
        assert bad not in h


# ---- 月次 ---------------------------------------------------------------
def test_monthly_html_has_no_md_file_names(tmp_path):
    import monthly_mail as mm
    (tmp_path / "strategy_review_202609.md").write_text(
        "# 月次戦略レビュー\n## ② シグナル淘汰（E4）\n- signal_culling_202609.md: 提案 0件\n- [ ] 確認する\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    (tmp_path / "paper_202609.md").write_text(
        "| pead | 2,000,000 | 2,014,145 | +14,145 | +0.71% | 14 |\n", encoding="utf-8")
    subject, body, n = mm.build("202609", tmp_path)
    assert n == 2 and "2026-09" in subject
    assert not re.search(r"\w+\.md\b", body)
    assert "☐ 確認する" in body and "[ ]" not in body and "<thead>" in body
    assert "提案件数" in body and "+14,145円" in body


@pytest.mark.skipif(not REAL_INSIGHTS.exists(), reason="実データが無い")
def test_monthly_html_real_under_100kb():
    import monthly_mail as mm
    _s, body, n = mm.build("202610", REAL_INSIGHTS)
    assert n > 0 and len(body.encode("utf-8")) < 100_000
    assert not re.search(r"\w+\.md\b", body)


# ---- ヒートマップ ---------------------------------------------------------
def test_heatmap_drops_delta_column_without_history_and_keeps_full_names():
    import screener_sector_heatmap as h
    wc = {"2026-W43": {"1029": 3, "DYNAMIC": 1}, "2026-W44": {"1029": 5}}
    names = {"1029": "証券・商品先物取引業"}
    md = h.format_heatmap_md(wc, {"trend_3w": {}}, {}, names)
    assert "3ヶ月Δ" not in md and "証券・商品先物取引業" in md and "自動追加" in md and "DYNAMIC" not in md
    md2 = h.format_heatmap_md(wc, {"trend_3w": {}},
                              {"1029": {"delta_3m": 6.2, "3m_ago": 50, "current": 56, "trend": "x"}}, names)
    assert "3ヶ月Δ" in md2 and "+6.2" in md2
