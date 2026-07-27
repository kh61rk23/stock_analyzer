"""
U-6（インサイダー統合 / F-3根治）の回帰テスト。

背景（要件定義書_JP-US統合_20260726.md §6 U-6 / J-02=a 承認済み）:
  同一データソース（SEC Form 4）に insider_monitor.py の簡易版
  （_parse_form4_simple 等）と us_insider_form4_scraper.py の完全版が
  並存し、パイプラインには貧弱な簡易版だけが配線されていた
  （週次台帳で2回連続 US:0件）。
  insider_monitor.py は JP 大量保有報告専用に縮退し、米国株は
  us_insider_form4_scraper.get_insider_summary() へ委譲する。

検証内容（R6-1〜R6-6 に対応）:
  1. insider_monitor から _parse_form4_simple / _get_cik /
     _fetch_form4_rss が削除されていること。
  2. insider_monitor のソースに sec.gov への直接アクセスが
     残っていないこと（機械検査）。
  3. run_insider_monitor() の返り値が旧7キーをすべて持つこと
     （us_insider_form4_scraper をモックして検証）。
  4. us_filings / total_us が us_insider_form4_scraper の結果を
     反映していること。
  5. bonuses に JP・US 両方の銘柄が含まれること。
  6. JP の _calc_bonus() の挙動が変わっていないこと
     （固定入力の特性化テスト。期待値は実測値）。
  7. format_insider_alert() が新しい us_filings の形で例外を出さないこと。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import insider_monitor as im

REPO = Path(__file__).resolve().parent.parent


def _source() -> str:
    return (REPO / "insider_monitor.py").read_text(encoding="utf-8")


# ==============================================================
# 1. US 経路の関数が削除されていること（R6-1）
# ==============================================================

def test_parse_form4_simple_removed():
    assert not hasattr(im, "_parse_form4_simple")


def test_get_cik_removed():
    assert not hasattr(im, "_get_cik")


def test_fetch_form4_rss_removed():
    assert not hasattr(im, "_fetch_form4_rss")


# ==============================================================
# 2. sec.gov への直接アクセスが残っていないこと（機械検査）
# ==============================================================

def test_no_direct_sec_gov_access_in_source():
    src = _source()
    assert "sec.gov" not in src


def test_no_raw_http_call_pattern_in_source():
    """requests.get / urllib による直叩きパターンが残っていないこと。"""
    src = _source()
    pattern = re.compile(
        r"requests\.(?:get|post|request)\(|urllib\.request\.urlopen\(|urlopen\("
    )
    assert not pattern.search(src)


def test_delegates_to_us_insider_form4_scraper():
    """米国側は us_insider_form4_scraper に委譲していること。"""
    assert hasattr(im, "us_insider_form4_scraper")
    src = _source()
    assert "us_insider_form4_scraper.get_insider_summary" in src


# ==============================================================
# 共通フィクスチャ
# ==============================================================

@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(
        DATA_DIR=str(tmp_path / "data"),
        SECTOR_REPRESENTATIVE_TICKERS={},
        EDINET_SCREENER_CANDIDATES=[],
    )


def _fake_get_insider_summary_factory(canned: dict):
    """ticker -> summary dict のマップから get_insider_summary 互換関数を作る。"""
    def _fake(ticker, days=None, cfg=None):
        return canned.get(ticker, {
            "ticker": ticker,
            "insider_bonus": 0.0,
            "buy_count": 0,
            "sell_count": 0,
            "buyer_roles": [],
            "net_buy_value": 0.0,
            "source": "edgar",
            "summary": "取引なし",
            "fetched_at": "2026-07-28T00:00:00",
        })
    return _fake


def _install_fake_yfinance_major_holders(monkeypatch, pct_by_ticker: dict):
    """
    _extract_jp_large_holdings が呼ぶ `import yfinance as yf` を
    sys.modules 経由でフェイクに差し替える。

    pct_by_ticker: {"7203.T": 0.06, ...}  # 0.06 = 6%
    """
    import sys

    class _FakeTicker:
        def __init__(self, ticker):
            self._ticker = ticker

        @property
        def major_holders(self):
            pct = pct_by_ticker.get(self._ticker)
            if pct is None:
                return pd.DataFrame()
            return pd.DataFrame([[pct]])

    fake_yf = SimpleNamespace(Ticker=lambda ticker: _FakeTicker(ticker))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)


# ==============================================================
# 3〜5. run_insider_monitor() の返り値契約（R6-2）
# ==============================================================

EXPECTED_KEYS = {
    "us_filings", "jp_holdings", "bonuses", "alert_items",
    "total_us", "total_jp", "timestamp",
}


def test_run_insider_monitor_returns_seven_keys(monkeypatch, cfg):
    monkeypatch.setattr(
        im.us_insider_form4_scraper, "get_insider_summary",
        _fake_get_insider_summary_factory({}),
    )
    _install_fake_yfinance_major_holders(monkeypatch, {})

    result = im.run_insider_monitor(cfg, ["AAPL", "7203.T"])

    # _save_cache() が result に "cached_at" を追記するため部分集合で判定する
    # （キャッシュ書き込みの副作用は U-6 のスコープ外・既存挙動）。
    assert EXPECTED_KEYS.issubset(result.keys())


def test_us_filings_and_total_us_reflect_scraper_result(monkeypatch, cfg):
    canned = {
        "AAPL": {
            "ticker": "AAPL", "insider_bonus": 3.0,
            "buy_count": 2, "sell_count": 0,
            "buyer_roles": ["ceo"], "net_buy_value": 500_000.0,
            "source": "edgar", "summary": "購入2件", "fetched_at": "x",
        },
        "MSFT": {
            "ticker": "MSFT", "insider_bonus": 0.0,
            "buy_count": 0, "sell_count": 0,
            "buyer_roles": [], "net_buy_value": 0.0,
            "source": "edgar", "summary": "取引なし", "fetched_at": "x",
        },
    }
    monkeypatch.setattr(
        im.us_insider_form4_scraper, "get_insider_summary",
        _fake_get_insider_summary_factory(canned),
    )
    _install_fake_yfinance_major_holders(monkeypatch, {})

    result = im.run_insider_monitor(cfg, ["AAPL", "MSFT"])

    # 取引なし（buy_count=sell_count=0）の MSFT は us_filings に含めない
    assert "AAPL" in result["us_filings"]
    assert "MSFT" not in result["us_filings"]
    assert result["us_filings"]["AAPL"]["insider_bonus"] == 3.0
    assert result["us_filings"]["AAPL"]["buy_count"] == 2
    assert result["total_us"] == 2   # AAPL の buy_count(2) + sell_count(0)


def test_bonuses_include_both_jp_and_us_tickers(monkeypatch, cfg):
    canned = {
        "AAPL": {
            "ticker": "AAPL", "insider_bonus": 3.0,
            "buy_count": 1, "sell_count": 0,
            "buyer_roles": ["ceo"], "net_buy_value": 100_000.0,
            "source": "edgar", "summary": "購入1件", "fetched_at": "x",
        },
    }
    monkeypatch.setattr(
        im.us_insider_form4_scraper, "get_insider_summary",
        _fake_get_insider_summary_factory(canned),
    )
    _install_fake_yfinance_major_holders(monkeypatch, {"7203.T": 0.06})

    result = im.run_insider_monitor(cfg, ["AAPL", "7203.T"])

    assert "AAPL" in result["bonuses"]
    assert result["bonuses"]["AAPL"] == 3.0
    assert "7203.T" in result["bonuses"]
    assert result["bonuses"]["7203.T"] == 4.0   # JP 大量保有ボーナス（_calc_bonus 実測値）
    assert result["total_jp"] == 1


# ==============================================================
# 6. JP _calc_bonus() の挙動が変わっていないこと（特性化テスト）
# ==============================================================
# 期待値は本ファイルの assert を実際に実行して得た実測値。
# run_insider_monitor は _calc_bonus(us_filings=[], jp_holdings) の形で
# 呼び出す（US側は us_insider_form4_scraper の insider_bonus を別途加算）。

def test_calc_bonus_jp_large_holding_unchanged():
    bonus = im._calc_bonus([], [{"holder_name": "test", "ownership_pct": 6.0,
                                  "report_date": "2026-07-28"}])
    assert bonus == 4.0


def test_calc_bonus_jp_no_holding_unchanged():
    bonus = im._calc_bonus([], [])
    assert bonus == 0.0


def test_calc_bonus_jp_below_threshold_unchanged():
    bonus = im._calc_bonus([], [{"holder_name": "test", "ownership_pct": 4.0,
                                  "report_date": "2026-07-28"}])
    assert bonus == 0.0


def test_calc_bonus_signature_and_logic_unchanged():
    """
    _calc_bonus() 自体は米国リストも従来どおり受け付け、
    ロジック（買い/売りカウント・クランプ）が変わっていないこと。
    """
    bonus_buy_single_plus_jp = im._calc_bonus(
        [{"transaction_type": "buy"}], [{"ownership_pct": 6.0}])
    assert bonus_buy_single_plus_jp == 6.0   # 3.0(単発買い) + 4.0(JP大量保有) - clamp前

    bonus_consec_clamped = im._calc_bonus(
        [{"transaction_type": "buy"}, {"transaction_type": "buy"},
         {"transaction_type": "sell"}, {"transaction_type": "sell"}],
        [],
    )
    assert bonus_consec_clamped == 0.0   # +6(連続買い) -6(連続売り) = 0


# ==============================================================
# 7. format_insider_alert() が新しい us_filings の形で例外を出さないこと
# ==============================================================

def test_format_insider_alert_with_new_us_filings_shape():
    result = {
        "us_filings": {
            "AAPL": {
                "ticker": "AAPL", "insider_bonus": 3.0,
                "buy_count": 2, "sell_count": 0,
                "buyer_roles": ["ceo"], "net_buy_value": 500_000.0,
                "source": "edgar", "summary": "購入2件", "fetched_at": "x",
            },
        },
        "jp_holdings": {},
        "bonuses": {"AAPL": 3.0},
        "alert_items": ["AAPL: 買い🟢 bonus=+3.0pt (US:2件 JP:0件)"],
        "total_us": 2,
        "total_jp": 0,
        "timestamp": "2026-07-28 00:00:00",
    }
    text = im.format_insider_alert(result)
    assert "AAPL" in text
    assert "米2件" in text


def test_format_insider_alert_empty_result_no_exception():
    result = {
        "us_filings": {}, "jp_holdings": {}, "bonuses": {},
        "alert_items": [], "total_us": 0, "total_jp": 0,
        "timestamp": "2026-07-28 00:00:00",
    }
    text = im.format_insider_alert(result)
    assert "注目すべき取引なし" in text


def test_get_insider_bonus_unaffected_by_us_filings_shape():
    cache = {"bonuses": {"AAPL": 3.5}, "us_filings": {"AAPL": {"insider_bonus": 3.0}}}
    assert im.get_insider_bonus("AAPL", cache) == 3.5
    assert im.get_insider_bonus("MSFT", cache) == 0.0
