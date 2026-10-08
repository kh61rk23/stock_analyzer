# 日次メール・Actionsログから見つかった不具合の回帰テスト
import sys
import types
from datetime import date, datetime, timezone

import pandas as pd


# ── 決算サプライズ: 最古の四半期を「直近」として加点していた ────────────
class TestEarningsSurpriseOrder:
    def _fake_yf(self, monkeypatch, frame):
        ticker = types.SimpleNamespace(earnings_history=frame, info={})
        monkeypatch.setitem(sys.modules, "yf_cache",
                            types.SimpleNamespace(Ticker=lambda tk: ticker))

    def _frame(self):
        # yfinance 実物と同じく四半期日付はインデックス・古い順
        frame = pd.DataFrame(
            {"epsActual": [39.22, 115.70, 197.30, 75.34],
             "epsEstimate": [47.33, 97.09, 139.16, 59.22]},
            index=pd.to_datetime(["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]),
        )
        frame.index.name = "quarter"
        return frame

    def test_history_is_newest_first(self, monkeypatch):
        import earnings_surprise as m
        self._fake_yf(monkeypatch, self._frame())
        history = m._fetch_earnings_history("6674.T")
        assert [h["quarter"] for h in history] == [
            "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
        # 直近3四半期が上振れ → +5pt（最古の下振れで -2pt にならないこと）
        pos, neg = m._calc_consecutive(history)
        assert (pos, neg) == (3, 0)
        assert m._calc_screener_bonus(pos, neg) == m.BONUS_CONSEC_POS2

    def test_extended_history_is_newest_first(self, monkeypatch):
        import earnings_surprise as m
        self._fake_yf(monkeypatch, self._frame())
        history = m._fetch_earnings_history_extended("6674.T", n_quarters=8)
        assert history[0]["quarter"] == "2026-06-30"


# ── TDnet: 5桁コードを全部捨てて毎日「解析行数: 0件」だった ─────────────
_TDNET_HTML = """
<table>
<tr><td>時刻</td><td>コード</td><td>会社名</td><td>表題</td><td>XBRL</td><td>上場取引所</td><td>更新履歴</td></tr>
<tr><td>17:00</td><td>51220</td><td>オカモト</td><td><a href="140120260917538105.pdf">自己株式の取得状況および取得終了に関するお知らせ</a></td><td></td><td>東</td><td></td></tr>
<tr><td>15:30</td><td>130A0</td><td>テスト</td><td><a href="140120260917538000.pdf">剰余金の配当に関するお知らせ</a></td><td></td><td>東</td><td></td></tr>
</table>
"""


class TestTdnet:
    def test_parses_five_digit_codes_and_relative_links(self):
        import tdnet_monitor as m
        rows = m._parse_tdnet_rows(_TDNET_HTML, "2026-09-17")
        assert [r["code"] for r in rows] == ["5122", "130A"]
        assert rows[0]["url"] == "140120260917538105.pdf"
        ev = m._build_event(rows[0], "BUYBACK")
        assert ev["ticker"] == "5122.T"
        assert ev["url"] == "https://www.release.tdnet.info/inbs/140120260917538105.pdf"

    def test_target_days_include_previous_business_day(self):
        import tdnet_monitor as m
        # 月曜 JST 09:30 → 金曜と月曜
        assert m._tdnet_target_days(datetime(2026, 9, 21, 0, 30, tzinfo=timezone.utc)) == [
            date(2026, 9, 18), date(2026, 9, 21)]
        # UTC では前日でも JST の日付で決める
        assert m._tdnet_target_days(datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)) == [
            date(2026, 9, 16), date(2026, 9, 17)]

    def test_reads_all_pages(self, monkeypatch):
        import tdnet_monitor as m
        calls = []

        def fake_page(date_str, timeout=15, page=1, quiet=False):
            calls.append(page)
            return {1: "p1", 2: "p2"}.get(page)

        monkeypatch.setattr(m, "_fetch_tdnet_page", fake_page)
        monkeypatch.setattr(m, "_parse_tdnet_rows",
                            lambda html, d: [{"html": html}] * (100 if html == "p1" else 20))
        rows = m._fetch_tdnet_day_rows(date(2026, 9, 16))
        assert len(rows) == 120
        assert calls == [1, 2]


# ── 異常検知: 場中の途中出来高で「急減」HIGH を出していた ─────────────────
class TestPartialSessionVolume:
    def test_session_incomplete(self):
        import anomaly_detector as m
        run = datetime(2026, 9, 17, 4, 50, tzinfo=timezone.utc)   # JST 13:50
        assert m._session_incomplete("3401.T", "2026-09-17", run)
        assert not m._session_incomplete("3401.T", "2026-09-16", run)
        assert m._session_incomplete("CL=F", "2026-09-17", run)
        assert not m._session_incomplete("AAPL", "2026-09-16", run)
        assert not m._session_incomplete(
            "3401.T", "2026-09-17", datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))

    def _hist(self, last_volume):
        vols = [500000.0 + (i % 5) * 1000 for i in range(40)] + [last_volume]
        dates = [f"2026-08-{i:02d}" for i in range(1, 32)] + [f"2026-09-{i:02d}" for i in range(1, 11)]
        dates[-1] = "2026-09-17"
        return {"close": [100.0] * 41, "volume": vols, "dates": dates}

    def test_drop_on_unfinished_bar_is_ignored(self, monkeypatch):
        import anomaly_detector as m
        monkeypatch.setattr(m, "_session_incomplete", lambda tk, d, now=None: True)
        assert m.detect_volume_anomaly("3401.T", self._hist(150000.0)) is None

    def test_spike_on_unfinished_bar_is_kept(self, monkeypatch):
        import anomaly_detector as m
        monkeypatch.setattr(m, "_session_incomplete", lambda tk, d, now=None: True)
        out = m.detect_volume_anomaly("3401.T", self._hist(2500000.0))
        assert out and "急増" in out["summary"]

    def test_drop_on_closed_bar_is_reported(self, monkeypatch):
        import anomaly_detector as m
        monkeypatch.setattr(m, "_session_incomplete", lambda tk, d, now=None: False)
        out = m.detect_volume_anomaly("3401.T", self._hist(150000.0))
        assert out and "急減" in out["summary"]


# ── ニュース: 英字の部分一致による誤マッチと、投資に無関係な注目ニュース ─────
class TestNewsRelevance:
    def test_keyword_hit_uses_word_boundaries(self):
        import news_pipeline as m
        assert not m._keyword_hit("AI", "Japan Air Self-Defense Force said on Tuesday")
        assert not m._keyword_hit("IT", "shared production with it")
        assert not m._keyword_hit("port", "a new report details")
        assert not m._keyword_hit("Fed", "Federal officials")
        assert m._keyword_hit("AI", "米エヌビディアCEO AI開発に")
        assert m._keyword_hit("chip", "memory chips lift Intel")
        assert m._keyword_hit("yen", "Yen weakens to 156-level")
        assert m._keyword_hit("ガソリン", "ガソリン価格が上昇")

    def test_crime_and_weather_are_excluded(self):
        import news_pipeline as m
        assert not m.is_investment_relevant(
            {"title": "洋菓子店4人死亡事件 現場の刃物は死亡役員が持参か 関与捜査"})
        assert not m.is_investment_relevant(
            {"title": "台風25号 あす以降小笠原諸島近海 本州は連休中の天気に影響も"})
        assert not m.is_investment_relevant(
            {"title": "Japan Air Self-Defense Force confirms Global Hawk drone crash"})

    def test_market_and_industry_news_are_kept(self):
        import news_pipeline as m
        for title in ("NY市場 ダウ一時900ドル超の下落 外国為替市場は1ドル156円台",
                      "Yen weakens to 156-level as Fed fulfills rate hike expectations",
                      "Why optical stocks Lumentum and Coherent were the day's biggest movers",
                      "G Mining discovers new gold deposit in Brazil",
                      "SoftBank's record $6.4bn bond sale tests limits of Japan's debt market"):
            assert m.is_investment_relevant({"title": title}), title

    def test_irrelevant_article_is_not_matched_by_substring(self):
        import news_pipeline as m
        out = m.match_sectors(
            [{"title": "Japan Air Self-Defense Force confirms Global Hawk drone crash",
              "summary": "The ministry said the aircraft went down with its report."}],
            m.SECTOR_KEYWORDS, m.MACRO_KEYWORDS)
        assert out[0]["matched_sectors"] == []
        assert out[0]["investment_relevant"] is False

    def test_alert_formatter_skips_irrelevant(self):
        import news_pipeline as m
        crime = {"source": "NHK トップ", "title": "洋菓子店4人死亡事件 現場の刃物は死亡役員が持参か"}
        market = {"source": "NHK ビジネス", "title": "NY市場 ダウ一時900ドル超の下落"}
        text = m.format_news_for_alert(
            {"by_sector": {"1032": [crime, market]}, "top_articles": [crime, market]},
            {"1032": {"composite_score": 90}})
        assert "洋菓子店" not in text
        assert "NY市場" in text


# ── 業種レポート: 主要リスクが有報の定型の前置きになっていた ────────────
class TestRiskHeadings:
    def test_extracts_item_headings(self):
        import edinet_bq_client as m
        eneos = ("３【事業等のリスク】当社グループでは…以下のとおりです。なお、文中の将来に関する事項は…"
                 "判断したものです。 （1）市場リスク・商品価格変動リスク当社グループは、石油製品の販売を"
                 "行っています。「２　サステナビリティ（１）ガバナンス」をご参照ください。"
                 "（2）環境規制に関するリスク当社グループの事業は、規制を受けています。")
        assert m._extract_risk_points(eneos) == ["市場リスク・商品価格変動リスク", "環境規制に関するリスク"]
        komatsu = ("３【事業等のリスク】　当社グループは…次のとおりです。 ＜戦略リスク＞"
                   "(1) 製品・ソリューション戦略　当社グループは、定義しています。 "
                   "(2) 事業環境　当社グループのおかれる事業環境は変動します。")
        assert m._extract_risk_points(komatsu) == ["製品・ソリューション戦略", "事業環境"]
        hitachi = ("（１）リスクマネジメントについて　日立の事業活動は変容しています。"
                   "①リスクマネジメント体制　日立は整備しています。（２）リスク要因　当グループは行っています。"
                   "①経済環境に係るリスク経済の動向　当グループの事業活動は影響を受けます。")
        assert m._extract_risk_points(hitachi) == ["経済環境に係るリスク"]

    def test_falls_back_to_non_boilerplate_sentence(self):
        import edinet_bq_client as m
        text = ("３ 【事業等のリスク】有価証券報告書に記載した事業の状況、経理の状況等に関する事項のうち、"
                "以下のとおりであります。当社は原材料価格の高騰により利益率が低下する可能性があります。")
        assert m._extract_risk_points(text) == ["当社は原材料価格の高騰により利益率が低下する可能性があります。"]


# ── オプション: 取得成功0件の理由が握り潰されていた ─────────────────────
class TestOptionsDiagnostics:
    def test_failure_reason_is_counted(self, monkeypatch):
        import options_strategy_advisor as m

        def boom(tk):
            raise RuntimeError("blocked")

        monkeypatch.setitem(sys.modules, "yf_cache", types.SimpleNamespace(Ticker=boom))
        reasons = {}
        assert m._fetch_metrics(types.SimpleNamespace(), {"ticker": "SPY"}, {}, reasons) is None
        assert reasons == {"例外:RuntimeError": 1}


# ── 業種格付け: 固定閾値で 33業種中30業種が BOT（スクリーナー対象外）だった ─────
class TestSectorTierRank:
    def test_rank_thirds(self, monkeypatch):
        import stock_analyzer_main as main
        monkeypatch.setattr(main, "cfg", types.SimpleNamespace(
            SECTOR_TIER_TOP_SHARE=1 / 3, SECTOR_TIER_BOT_SHARE=1 / 3))
        sectors = {f"{1001 + i}": {"composite_score": float(40 - i)} for i in range(33)}
        tier = main._calc_sector_tier(sectors, {})
        assert sum(t == "TOP" for t in tier.values()) == 11
        assert sum(t == "MID" for t in tier.values()) == 11
        assert sum(t == "BOT" for t in tier.values()) == 11
        # 全業種が45点未満でも上位は TOP（旧: 全部 BOT）
        assert tier["1001"] == "TOP" and tier["1033"] == "BOT"

    def test_momentum_upgrade_still_applies(self, monkeypatch):
        import stock_analyzer_main as main
        monkeypatch.setattr(main, "cfg", types.SimpleNamespace())
        sectors = {c: {"composite_score": s} for c, s in
                   (("A", 90.0), ("B", 50.0), ("C", 10.0))}
        tier = main._calc_sector_tier(
            sectors, {"momentum": {"B": {"slope_per_week": 5.0}}})
        assert tier == {"A": "TOP", "B": "TOP", "C": "BOT"}

    def test_config_uses_shares(self):
        import config
        assert config.SECTOR_TIER_TOP_SHARE == 1 / 3
        assert config.SECTOR_TIER_BOT_SHARE == 1 / 3
        assert not hasattr(config, "SECTOR_TIER_TOP_THRESHOLD")


# ── スクリーナー品質ボーナス: 廃止済みの旧EDINET蓄積DBを読んで毎日 0/10 だった ─────
class TestEdinetQualityFromBundle:
    def _bundle(self):
        return {"analysis": {"credit_score": 85},
                "financials": {"revenue_cagr": 0.10,
                               "net_income": [80.0, 100.0], "equity": [900.0, 1000.0]}}

    def test_analysis_from_bundle(self):
        import screener_feedback_loop as m
        out = m._analysis_from_bundle(self._bundle())
        assert out == {"credit_score": 85, "revenue_growth": 0.10, "roe": 0.1}
        assert m._analysis_from_bundle({}) == {}

    def test_build_cache_uses_bq_bundles(self, monkeypatch):
        import screener_feedback_loop as m
        calls = []

        class FakeClient:
            def get_company_analysis_bundle(self, tickers, max_companies=2):
                calls.append((tuple(tickers), max_companies))
                return {"6504.T": TestEdinetQualityFromBundle()._bundle()}

        monkeypatch.setitem(sys.modules, "edinet_bq_client",
                            types.SimpleNamespace(EDINETBQClient=FakeClient))
        cache = m.build_edinet_quality_cache(types.SimpleNamespace(), ["6504.T", "6594.T"])
        assert list(cache) == ["6504.T"]
        assert cache["6504.T"]["edinet_bonus"] > 0
        assert calls == [(("6504.T", "6594.T"), 2)]


class TestShareholderAnalyzerDisabled:
    def test_flag_off_and_skip_is_logged(self):
        import pathlib
        import config
        assert config.ENABLE_SHAREHOLDER_ANALYZER is False
        src = (pathlib.Path(__file__).resolve().parent.parent / "stock_analyzer_main.py").read_text(encoding="utf-8")
        # 停止していたブロック自体が削除済みで、呼び出しが残っていないこと
        assert "run_shareholder_analyzer(" not in src
