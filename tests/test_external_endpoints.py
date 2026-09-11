# 外部エンドポイントの移設・撤去（2026-09-11 の疎通調査より）
#
# 2026-07-14 以降の全実行で 404/403 を出し続けていた4系統を調査した結果:
#   下院 議員取引  … URLが financial-pdfs/ 配下へ移設（明細はPDF化）
#   国務省 渡航警告 … RSSが /_res/rss/TAsTWs.xml へ移設
#   上院 eFD       … efts.senate.gov がDNS不在（実在した記録なし）→ 撤去
#   Google CSE     … APIが403。キー復旧まで無効化
# URLをコードへ直書きしている以上、退行はテストでしか止められない。
import re

import pytest


class TestHouseUrls:
    def test_index_uses_the_current_path(self):
        import politician_monitor as m
        url = m._PTR_INDEX_URL_TMPL.format(year=2026)
        assert "financial-pdfs/2026FD.xml" in url
        assert "ptr-pdfs/2026/2026FD.xml" not in url, "旧パスは2025年分も404"

    def test_detail_points_at_pdf_not_xml(self):
        """個別開示はPDFでしか配信されていない"""
        import politician_monitor as m
        url = m._PTR_DETAIL_URL_TMPL.format(year=2026, fileid="20034201")
        assert url.endswith(".pdf")


class TestPtrPdfParsing:
    """実PDFのテキスト構造（2026-09-11 実測）に対する解析"""

    # 実データそのままの並び。金額が行を跨ぐケースを含む。
    SAMPLE = (
        "Transactions\nID\nOwner Asset\nTransaction\nType\nDate\n"
        "Notification\nDate\nAmount\n"
        "Amazon.com, Inc. - Common Stock\n(AMZN) [ST]\nS (partial)\n"
        "03/16/2026 03/16/2026\n$1,001 - $15,000\n"
        "Ferguson Enterprises Inc.\nStock\n(FERG) [ST]\nP\n"
        "12/12/2025 12/12/2025\n$15,001 -\n$50,000\n"
    )

    def _parse(self):
        import politician_monitor as m
        flat = " ".join(self.SAMPLE.split())
        return list(m._PTR_TX_RE.finditer(flat))

    def test_finds_both_transactions(self):
        assert len(self._parse()) == 2

    def test_partial_sale_is_a_sale(self):
        import politician_monitor as m
        g = self._parse()[0]
        assert g.group(1) == "AMZN"
        assert g.group(3).lower() == "s"

    def test_amount_split_across_lines_is_recovered(self):
        """'$15,001 -' 改行 '$50,000' を1つのレンジとして拾う"""
        g = self._parse()[1]
        amount = " ".join(g.group(7).split())
        assert amount == "$15,001 - $50,000"

    def test_amount_label_maps_to_an_existing_size_score(self):
        import politician_monitor as m
        for label, expect in (("$1,001 - $15,000", 20),
                              ("$15,001 - $50,000", 40),
                              ("$50,001 - $100,000", 60)):
            assert m._parse_size_score(label) == expect

    def test_government_securities_are_not_treated_as_stocks(self):
        """米国債のCUSIP（数字始まり）は銘柄ではない"""
        import politician_monitor as m
        flat = "(91282CGJ4) [GS] P 03/24/2026 04/08/2026 $15,001 - $50,000"
        assert not m._PTR_TX_RE.search(flat)

    def test_transaction_type_matches_what_scoring_expects(self):
        """score_transaction() は 'p'/'s' の完全一致で売買を判定する"""
        import politician_monitor as m
        src = m._PTR_TX_RE.search(" ".join(self.SAMPLE.split()))
        assert src.group(3).lower() in ("p", "s", "e")


class TestSenateRemoved:
    def test_dead_host_is_gone_from_code(self):
        """efts.senate.gov はDNS解決できない。コメント以外に残さない"""
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        for name in ("politician_monitor.py", "politician_signal_scorer.py"):
            for line in (root / name).read_text(encoding="utf-8").splitlines():
                if "efts.senate.gov" in line:
                    assert line.lstrip().startswith("#"), \
                        f"{name} に実コードとして残っている: {line.strip()[:70]}"

    def test_fetch_functions_are_removed(self):
        import politician_monitor as pm
        import politician_signal_scorer as ps
        assert not hasattr(pm, "_fetch_senate_ptr_records")
        assert not hasattr(ps, "_fetch_senate_ptr")

    def test_history_rescoring_is_kept(self):
        """既存履歴の再スコアは残す（取得だけを止めた）"""
        import politician_monitor as pm
        import politician_signal_scorer as ps
        assert hasattr(pm, "_score_senate_transaction")
        assert hasattr(ps, "get_senate_transactions")


class TestStateDeptRss:
    def test_uses_the_current_feed(self):
        import country_risk_updater as c
        assert c.STATE_DEPT_RSS_URL.endswith("/_res/rss/TAsTWs.xml")
        assert "traveladvisories/RSS.xml" not in c.STATE_DEPT_RSS_URL

    def test_parser_handles_the_feed_title_format(self):
        """新フィードは全itemのタイトルに 'Level N' を含む"""
        import country_risk_updater as c
        xml = (
            "<rss><channel>"
            "<item><title>Iraq - Level 4: Do Not Travel</title></item>"
            "<item><title>Suriname - Level 1: Exercise Normal Precautions</title></item>"
            "</channel></rss>"
        )
        levels = c._parse_state_dept_rss(xml)
        assert levels.get("IRQ") == 4


class TestPaidMediaDisabled:
    def test_cse_is_off_until_the_key_is_fixed(self):
        import config
        assert config.ENABLE_PAID_MEDIA_SEARCH is False
