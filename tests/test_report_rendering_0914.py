# 2026-09-14 の日次・週次メールで見つかった表示とロジックの不具合の回帰テスト
#
# 実際にメール本文で観測した症状:
#   日次: 異常検知「-1138.0%」（本文は -11.38%）/ 0.3倍を「出来高急増」/
#         SoftBank Group の配当利回り「17.0%」
#   週次: Pearl ランキングの重複掲載（HESAY×3 等）/ ROE「1430.0%」/
#         米国セクター表が「|  | 0 | N/A |  |」の空行 / 台帳が前回日付のまま /
#         地政学節がフッターの後ろ / 「USD/JPY: 154.482%」/ 配当「利回262.0%」/
#         日次「FOMC 1日後」と週次「2026-09-17（3日後）」の矛盾
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (ROOT / name).read_text(encoding="utf-8")


def _block(src, start, end_marker="\ndef "):
    i = src.index(start)
    j = src.find(end_marker, i + 1)
    return src[i: j if j != -1 else len(src)]


# ── 日次メール ──────────────────────────────────────────────
class TestDailyAnomalyFormatting:
    def test_price_change_is_not_multiplied_again(self):
        """anomaly_detector は price_change を既に %値で格納する"""
        src = _src("alert_notifier.py")
        assert "a['price_change']*100" not in src
        assert "chg*100" not in src

    def test_detector_stores_percent(self):
        assert "round(last_chg * 100, 2)" in _src("anomaly_detector.py")

    def test_volume_decrease_is_not_labelled_as_surge(self):
        assert "急減" in _src("alert_notifier.py")
        assert "急減" in _src("anomaly_detector.py")


class TestDividendYieldUnit:
    """yfinance の dividendYield は %値（2.62 = 2.62%）で返る"""

    def test_base_agent_does_not_treat_percent_as_ratio(self):
        src = _src("base_agent.py")
        assert '_fv("dividendYield", 0.0, 0.30)' not in src
        assert "trailingAnnualDividendYield" in src

    def test_dividend_calendar_prefers_ratio_field(self):
        src = _src("dividend_calendar.py")
        assert "trailingAnnualDividendYield" in src
        assert "float(_pct_yield) / 100.0" in src

    def test_ticker_is_not_printed_twice_when_name_is_missing(self):
        src = _src("dividend_calendar.py")
        assert "{ev['ticker']} {ev['name'][:14]}" not in src


# ── FOMC 日程 ───────────────────────────────────────────────
# FRB公式 2026: 1/27-28, 3/17-18, 4/28-29, 6/16-17, 7/28-29, 9/15-16, 10/27-28, 12/8-9
OFFICIAL_STARTS = ["2026-01-27", "2026-03-17", "2026-04-28", "2026-06-16",
                   "2026-07-28", "2026-09-15", "2026-10-27", "2026-12-08"]
OFFICIAL_DECISIONS = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
                      "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]


class TestFomcSingleSource:
    def test_fed_calendar_matches_official_schedule(self):
        import fed_calendar_fetcher as f
        assert f.FOMC_MEETINGS_2026 == OFFICIAL_STARTS

    def test_catalyst_calendar_derives_decision_dates(self):
        import catalyst_calendar as c
        assert c._fomc_decision_dates_2026() == OFFICIAL_DECISIONS

    def test_catalyst_uses_the_derived_list(self):
        import catalyst_calendar as c
        assert c._FED_FOMC_2026 == OFFICIAL_DECISIONS, \
            "日次と週次で FOMC の日付が食い違う状態に戻っている"


# ── 週次メール ──────────────────────────────────────────────
class TestPearlDedupe:
    def test_same_ticker_is_listed_once(self, monkeypatch):
        import pearl_score_calculator as psc
        import weekly_report_generator as w

        row = lambda t, tier, score: {
            "ticker": t, "pearl_tier": tier, "pearl_score": score,
            "pearl_bonus": 5.0, "d1_score": 20, "d3_score": 60, "d4_score": 50}
        monkeypatch.setattr(psc, "load_pearl_candidates", lambda cfg=None: [
            row("HESAY", "A", 66.0), row("HESAY", "A", 66.0), row("HESAY", "A", 64.3),
            row("MSGM", "S", 70.1), row("MSGM", "S", 70.1),
        ])
        out = w._section_pearl_ranking({}, types.SimpleNamespace())
        table = [l for l in out if l.startswith("| ")]
        assert sum(" HESAY " in l for l in table) == 1
        assert sum(" MSGM " in l for l in table) == 1
        assert any("計2銘柄" in l for l in out), "発見件数が重複ぶん水増しされている"


class TestRoeUnit:
    def test_screener_stores_roe_as_percent(self):
        assert 'round(data["roe"] * 100, 1)' in _src("stock_screener.py")

    def test_weekly_report_does_not_multiply_roe_again(self):
        src = _src("weekly_report_generator.py")
        assert '_pct(c.get("roe"))' not in src
        assert 'c["roe"] >= 0.10' not in src
        assert "c['roe']*100" not in src


class TestUsSectorTable:
    def test_nested_pipeline_output_is_rendered(self):
        import weekly_report_generator as w
        out = w._section_us_sectors({"us_sectors": {
            "top3": ["XLE"], "bottom3": ["XLY"],
            "sector_list": [{
                "sector": {"etf": "XLE", "name_ja": "エネルギー"},
                "score_result": {"score": 50, "risk_level": "low",
                                 "etf_data": {"ret_1m": 3.2}},
            }],
        }})
        body = "\n".join(out)
        assert "エネルギー（XLE）" in body
        assert "+3.2%" in body
        assert "low" in body
        assert "|  | 0 | N/A |" not in body, "空行が並ぶ状態に戻っている"

    def test_ranking_label_matches_what_is_sorted(self):
        import weekly_report_generator as w
        out = w._section_us_sectors({"us_sectors": {
            "top3": ["XLE"], "bottom3": ["XLY"], "sector_list": []}})
        body = "\n".join(out)
        assert "週間パフォーマンス" not in body


class TestLedgerLabel:
    def test_ledger_is_labelled_as_last_finalized_run(self, monkeypatch):
        import module_ledger as ml
        monkeypatch.setattr(ml, "load_latest_ledger", lambda cfg: {
            "date": "2026-09-12",
            "stats": {"success": 66, "error": 0, "skipped": 2},
            "modules": {}, "alerts": []})
        body = "\n".join(ml.format_ledger_section(None))
        assert "直近の確定台帳（2026-09-12 実行分）" in body


class TestWeeklyLayoutAndOrder:
    def test_footer_comes_after_geo_section(self):
        src = _src("weekly_report_generator.py")
        assert src.index("*自動生成: stock_analyzer_main.py") > \
            src.index("G-X: 資源権益・地政学影響セクション")

    def test_fx_rate_has_no_percent_sign(self):
        src = _src("weekly_report_generator.py")
        assert "{val}%{chg_s}" not in src

    def test_weekly_report_runs_after_the_modules_it_reads(self):
        """signal_aggregator / job_posting / ontology_export より後で生成すること"""
        src = _src("stock_analyzer_main.py")
        gen = src.index('"weekly_report_generator",')
        for dep in ('_in_mode("signal_aggregator")', '_in_mode("job_posting")',
                    '_in_mode("facility_expansion")', '_in_mode("ontology_export")'):
            assert gen > src.index(dep), f"{dep} より前で週次レポートを生成している"
        assert gen < src.index("[OBS-1] モジュール実行台帳の確定")

    def test_retired_edinet_api_is_not_listed_as_a_source(self):
        for name in ("alert_notifier.py", "weekly_report_generator.py"):
            for line in _src(name).splitlines():
                if "データソース" in line:
                    assert "金融庁EDINET" not in line, f"{name}: {line.strip()[:60]}"


# ── ログ由来 ────────────────────────────────────────────────
class TestLogDerivedFixes:
    def test_cluster_exporter_writes_to_taxonomy_dataset(self):
        src = _src("cluster_graph_exporter.py")
        assert 'getattr(cfg, "BQ_DATASET"' not in src, \
            "EdinetDB用の既定データセット(edinet)を流用して404になる"
        assert 'dataset = "taxonomy"' in src

    def test_analyze_sections_uses_shared_transformers_import(self):
        block = _block(_src("finbert_analyzer.py"), "def _get_or_init_pipeline")
        # 修正理由のコメントに旧コードの文字列を残しているので、コード行だけを見る
        code = "\n".join(l for l in block.splitlines()
                         if not l.lstrip().startswith("#"))
        assert "from transformers import pipeline" not in code
        assert "_try_import_transformers()" in code
        assert "return_all_scores" not in code

    def test_doc_indexer_accepts_edinet_doc_id_under_generic_key(self):
        import edinet_doc_indexer as d
        rows = d.build_doc_index_rows(
            {"disclosures": [{"id": "S100ABCD", "type": "yuho",
                              "published_at": "2026-06-20T09:00:00+09:00"}]},
            {"edinet_code": "E1", "sec_code": "10000", "name": "T"},
            now="2026-09-14T00:00:00+00:00")
        assert [r["doc_id"] for r in rows] == ["S100ABCD"]

    def test_doc_indexer_rejects_non_edinet_ids_and_records_keys(self):
        import edinet_doc_indexer as d
        keys = set()
        rows = d.build_doc_index_rows(
            {"disclosures": [{"id": "12345", "type": "yuho"}]},
            {"edinet_code": "E1", "sec_code": "10000", "name": "T"},
            now="2026-09-14T00:00:00+00:00", seen_keys=keys)
        assert rows == [], "EDINET書類番号でない id を主キーにしている"
        assert {"id", "type"} <= keys

    def test_gemini_warning_is_emitted_once(self):
        src = _src("segment_mapper.py")
        assert "_GEMINI_WARNED" in src

    def test_arxiv_trend_uses_full_population(self):
        import thematic_etf_tracker as t
        got = t._extract_papers({"papers": [{"t": 1}],
                                 "all_papers": [{"t": 1}, {"t": 2}]})
        assert len(got) == 2, "既読除外後の新着だけでトレンドを計算している"


class TestCatalystCacheInvalidation:
    """FOMC 元リストが変わったら期限内のキャッシュでも捨てること"""

    def _cfg(self, tmp_path):
        return types.SimpleNamespace(CACHE_DIR=str(tmp_path))

    def test_fresh_cache_with_current_source_is_reused(self, tmp_path):
        import catalyst_calendar as c
        cfg = self._cfg(tmp_path)
        c._save_calendar_cache({"global_events": []}, cfg)
        assert c._load_calendar_cache(cfg), "同じ元リストのキャッシュが使われない"

    def test_cache_from_old_fomc_list_is_discarded(self, tmp_path):
        import json
        from datetime import datetime
        import catalyst_calendar as c
        cfg = self._cfg(tmp_path)
        # 9/14 にコミットされた形（fomc_source を持たない旧キャッシュ）
        (tmp_path / "catalyst_calendar.json").write_text(json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d"),
            "global_events": [{"date": "2026-09-17", "type": "fed_fomc"}],
        }), encoding="utf-8")
        assert c._load_calendar_cache(cfg) == {}, \
            "旧日付入りのキャッシュが翌日の実行で再利用される"

    def test_cache_with_different_fomc_list_is_discarded(self, tmp_path):
        import json
        from datetime import datetime
        import catalyst_calendar as c
        cfg = self._cfg(tmp_path)
        (tmp_path / "catalyst_calendar.json").write_text(json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d"),
            "fomc_source": ["2026-09-17"],
        }), encoding="utf-8")
        assert c._load_calendar_cache(cfg) == {}
