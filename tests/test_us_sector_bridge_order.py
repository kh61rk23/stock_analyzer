"""米国 8-K の決算 → JP業種ブリッジ：業種の採点より前に入力が揃い、読み手の形に合うこと。

11/06 の形（決算76件・screener 照合3件）を模した入力で、業種に信号が出ることを確かめる。
SEC・BQ には触れない。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import signal_dedup
import us_8k_monitor as k8
import us_sector_bridge_enhanced as bridge

MAIN = Path(__file__).resolve().parent.parent / "stock_analyzer_main.py"


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(DATA_DIR=tmp_path, US_WATCHLIST_TICKERS=[], ENABLE_US_8K_MONITOR=True,
                           ENABLE_US_SECTOR_BRIDGE=True)


def _filings(n=76):
    out = []
    for i in range(n):
        out.append({"cik": 1000 + i, "company_name": f"Co{i}", "accession_number": f"0000000000-26-{i:06d}",
                    "filing_date": "2026-11-05", "doc_url": "u", "items": ["2.02", "9.01"]})
    return out


def _write_fundamentals(cfg, ticker, latest, year_ago):
    d = Path(cfg.DATA_DIR) / "us_fundamentals_db"
    d.mkdir(exist_ok=True)
    hist = [{"val": latest}, {"val": 1}, {"val": 1}, {"val": 1}, {"val": year_ago}]
    (d / f"{ticker}.json").write_text(json.dumps({"eps_history": hist}), encoding="utf-8")


@pytest.fixture
def day(cfg, monkeypatch):
    """11/06 の形：決算76件。T0=半導体 beat、T1=半導体 miss。"""
    calls = {"fetch": 0}

    def fake_fetch(target_date):
        calls["fetch"] += 1
        return _filings()

    monkeypatch.setattr(k8, "_fetch_8k_filings", fake_fetch)
    monkeypatch.setattr(k8, "_load_cik_to_ticker_map", lambda c: {1000 + i: f"T{i}" for i in range(76)})
    monkeypatch.setattr(k8, "_notify_fundamental_rotator", lambda c, t: None)
    monkeypatch.setattr(k8, "FETCH_DELAY", 0)
    _write_fundamentals(cfg, "T0", 2.0, 1.0)
    _write_fundamentals(cfg, "T1", 0.5, 1.0)
    mapping = {t: {"cik": 1, "sic": 3674, "sic_desc": "semi", "company_name": t, "jp_sectors": ["1016"]}
               for t in ("T0", "T1")}
    monkeypatch.setattr(bridge, "update_sector_mapping", lambda c, t: mapping)
    monkeypatch.setattr(bridge, "load_sector_mapping", lambda c: mapping)
    return calls


def test_early_fetch_has_earnings_without_screener(cfg, day):
    early = k8.fetch_earnings_early(cfg, {})
    assert len(early["earnings_tickers"]) == 76
    assert early["screener_hits"] == []
    assert early["partial"] is True
    assert not (Path(cfg.DATA_DIR) / k8.CACHE_FILE).exists()   # 後段が screener 照合なしのキャッシュを返さない


def test_later_run_reuses_filings_and_matches_screener(cfg, day):
    results = {}
    results["us_8k"] = k8.fetch_earnings_early(cfg, results)
    results["us_screener"] = {"candidates": [{"ticker": "T0"}, {"ticker": "T5"}, {"ticker": "T9"}]}
    full = k8.run_us_8k_monitor(cfg, results)
    assert day["fetch"] == 1                                   # EDGAR は1回だけ
    assert full["screener_hits"] == ["T0", "T5", "T9"]
    assert len(full["earnings_tickers"]) == 76
    assert "_filings" not in full and "partial" not in full


def test_bridge_makes_signals_in_reader_shape(cfg, day):
    results = {}
    results["us_8k"] = k8.fetch_earnings_early(cfg, results)
    out = bridge.run_us_sector_bridge_earnings(cfg, results)
    assert not out.get("skipped")
    assert out["beat_tickers"] == ["T0"] and out["miss_tickers"] == ["T1"]
    assert out["jp_affected"] == ["1016"]
    for s in out["cross_signals"]:
        # 読み手（業種の横断産業補正）は signal を dict として読む
        tw = s["signal"].get("tailwind", [])
        hw = s["signal"].get("headwind", [])
        assert isinstance(tw, list) and isinstance(hw, list)
        assert s["rule_id"]
    by_dir = {s["direction"]: s for s in out["cross_signals"]}
    assert by_dir["tailwind"]["signal"] == {"tailwind": ["1016"], "headwind": []}
    assert by_dir["headwind"]["signal"] == {"tailwind": [], "headwind": ["1016"]}


def test_signals_reach_the_sector_via_signal_dedup(cfg, day):
    results = {}
    results["us_8k"] = k8.fetch_earnings_early(cfg, results)
    out = bridge.run_us_sector_bridge_earnings(cfg, results)
    results["cross_sector_signals"] = out["cross_signals"]
    got = [(x["sector_code"], x["direction"]) for x in signal_dedup.collect_all_signals(results)
           if x["source"] == "cross_sector"]
    assert ("1016", "tailwind") in got and ("1016", "headwind") in got


def test_bridge_skips_with_reason_when_us_8k_missing_or_skipped(cfg):
    assert bridge.run_us_sector_bridge_earnings(cfg, {})["skipped"] is True
    out = bridge.run_us_sector_bridge_earnings(cfg, {"us_8k": {"skipped": True, "reason": "cik_map not found"}})
    assert out["skipped"] is True and "cik_map" in out["reason"]


def test_ism_part_is_separate_and_needs_us_macro(cfg, day):
    results = {"us_8k": k8.fetch_earnings_early(cfg, {})}
    assert bridge.run_us_sector_bridge_ism(cfg, results) == []     # us_macro が無い間は出ない
    results["us_macro"] = {"score": 1}
    results["macro_snapshot"] = {"us_ism": 54.0}
    sigs = bridge.run_us_sector_bridge_ism(cfg, results)
    assert sigs and sigs[0]["signal"]["tailwind"]


def test_main_runs_bridge_before_sector_scoring_and_ism_after_us_macro():
    src = MAIN.read_text(encoding="utf-8")
    early = src.index("run_us_sector_bridge_earnings(")
    ism = src.index("run_us_sector_bridge_ism(")
    sector_loop = src.index("for code in cfg.IMPLEMENTED_AGENTS:\n        try:")
    dedup = src.index('"signal_dedup",\n                    "/content/signal_dedup.py"')
    us_macro = src.index("run_us_macro_pipeline(")
    us_8k_full = src.index("run_us_8k_monitor(")
    assert early < sector_loop < dedup
    assert us_macro < ism
    assert early < us_8k_full                                       # 8-K 本体は米国スクリーナーの後
    assert len(re.findall(r"fetch_earnings_early\(", src)) == 1
