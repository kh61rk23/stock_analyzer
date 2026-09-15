# 2026-09-15 の日次メール・Actionsログから見つかった不具合の回帰テスト
import json
import pathlib
import types
from datetime import datetime, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(name):
    return (ROOT / name).read_text(encoding="utf-8")


def _mdy(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime("%m/%d/%Y")


def _iso(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ── 議員取引の履歴が毎回空になっていた ─────────────────────────
class TestPoliticianHistoryDates:
    def test_to_iso_date_normalizes_pdf_format(self):
        import politician_monitor as m
        assert m._to_iso_date("03/16/2026") == "2026-03-16"
        assert m._to_iso_date("2026-03-16") == "2026-03-16"
        assert m._to_iso_date("") == ""

    def test_recent_mdy_transactions_survive_save(self, tmp_path):
        """"MM/DD/YYYY" と ISO の文字列比較で全件が期限切れ扱いになっていた"""
        import politician_monitor as m
        cfg = types.SimpleNamespace(CACHE_DIR=str(tmp_path),
                                    POLITICIAN_HISTORY_MAX_ROWS=5000,
                                    POLITICIAN_CACHE_TTL_DAYS=180)
        m._save_politician_history(cfg, {"transactions": [
            {"transaction_date": _mdy(10), "asset": "A"},
            {"transaction_date": _mdy(400), "asset": "B"},
        ]})
        kept = m._load_politician_history(cfg)["transactions"]
        assert [t["asset"] for t in kept] == ["A"], "最近の取引まで消えている"

    def test_pdf_parser_stores_iso_dates(self):
        assert '"transaction_date": _to_iso_date(tx_date)' in _src("politician_monitor.py")


class TestPoliticianNewTransactions:
    def _tx(self, date="2026-09-01", asset="Microsoft Corporation"):
        return {"politician": "Josh Gottheimer", "asset": asset,
                "transaction_date": date, "transaction_type": "p",
                "amount": "$1,001 - $15,000"}

    def test_duplicates_within_the_same_batch_are_collapsed(self):
        """修正届出の再掲が同じ回に入ると二重に『新規』になっていた"""
        import politician_monitor as m
        new = m.detect_new_transactions([self._tx(), self._tx()], [])
        assert len(new) == 1

    def test_distinct_trades_are_kept(self):
        import politician_monitor as m
        new = m.detect_new_transactions(
            [self._tx("2026-09-01"), self._tx("2026-09-02")], [])
        assert len(new) == 2

    def test_existing_history_still_filters(self):
        import politician_monitor as m
        assert m.detect_new_transactions([self._tx()], [self._tx()]) == []

    def test_alert_recency_setting_exists_and_is_used(self):
        import config
        assert config.POLITICIAN_ALERT_MAX_AGE_DAYS == 60
        assert "POLITICIAN_ALERT_MAX_AGE_DAYS" in _src("politician_monitor.py")

    def test_alert_shows_trade_date(self):
        assert "取引日 {_td}" in _src("alert_notifier.py")


# ── 配当利回り（data_bridge 経由の日本株） ───────────────────────
class TestDividendYieldRatio:
    def test_sub_one_percent_is_not_read_as_ratio(self):
        """SoftBank Group の 0.19% が 19.0% になっていた"""
        import finance_logic as f
        assert f.compute_div_yield({"dividendYield": 0.19}, True, 20000) == pytest.approx(0.0019)

    def test_above_one_percent_is_scaled(self):
        import finance_logic as f
        assert f.compute_div_yield({"dividendYield": 2.62}, True, 2000) == pytest.approx(0.0262)

    def test_trailing_ratio_field_is_preferred(self):
        import finance_logic as f
        got = f.compute_div_yield(
            {"trailingAnnualDividendYield": 0.041, "dividendYield": 99.0}, True, 1000)
        assert got == pytest.approx(0.041)

    def test_us_dividend_rate_path_is_unchanged(self):
        import finance_logic as f
        assert f.compute_div_yield({"dividendRate": 1.0}, False, 100) == pytest.approx(0.01)

    def test_us_fallback_uses_percent_semantics(self):
        import finance_logic as f
        got = f.compute_div_yield({"dividendYield": 0.41}, False, None)
        assert got == pytest.approx(0.0041), "0.41% を 41% と読んでいる"

    def test_alpha_vantage_helper_is_unchanged(self):
        """fix_div_yield は Alpha Vantage の比率値にも使う"""
        import finance_logic as f
        assert f.fix_div_yield(0.0436) == pytest.approx(0.0436)
        assert f.fix_div_yield(4.36) == pytest.approx(0.0436)


# ── doc_index: doc_id 列はあるが値が空 ─────────────────────────
class TestDocIndexPdfUrl:
    COMPANY = {"edinet_code": "E1", "sec_code": "10000", "name": "T"}
    NOW = "2026-09-15T00:00:00+00:00"

    def test_doc_id_recovered_from_pdf_url(self):
        import edinet_doc_indexer as d
        rows = d.build_doc_index_rows({"disclosures": [{
            "doc_id": None, "type": "yuho", "date": "2026-06-20",
            "fiscal_year": 2025, "title": "有価証券報告書",
            "pdf_url": "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100W1AB.pdf",
        }]}, self.COMPANY, now=self.NOW)
        assert [r["doc_id"] for r in rows] == ["S100W1AB"]

    def test_unrelated_url_does_not_invent_an_id(self):
        import edinet_doc_indexer as d
        keys = set()
        rows = d.build_doc_index_rows({"disclosures": [{
            "doc_id": "", "type": "yuho",
            "pdf_url": "https://example.invalid/SEARCHDOCUMENT/file.pdf",
        }]}, self.COMPANY, now=self.NOW, seen_keys=keys)
        assert rows == []
        assert any(k.startswith("例:doc_id=") for k in keys), "欠落値の実物が記録されていない"
