"""tools/integration_check：本番の出力の異常を検出できること（入力は小さな偽データ）。"""
import importlib.util
from datetime import date
from pathlib import Path

spec = importlib.util.spec_from_file_location("ic", Path(__file__).resolve().parents[1] / "tools" / "integration_check.py")
ic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ic)


def setup_function():
    ic.FINDINGS.clear()


def test_detects_out_of_range_and_default_scores():
    ic.check_us_sector_scores({"sectors": {"XLC": {"composite_score": -55.0}, "XLE": {"composite_score": 50.0},
                                           "XLV": {"composite_score": 50.0}, "XLP": {"composite_score": 50.0}}})
    names = [f[0] for f in ic.FINDINGS]
    assert names.count("us_sector_scores") == 2


def test_detects_dcf_and_backtest_and_paper_lot():
    ic.check_dcf({"results": {"X.T": {"inputs": {"current_price": 100}, "dcf": {"intrinsic_per_share": 1000}}}})
    ic.check_backtest({"overall": {"max_drawdown": -100.0}})
    ic.check_paper({"_pending": {"s": [{"ticker": "A.T", "created": "2026-10-20"}]}},
                   [{"type": "fill", "strategy": "s", "ticker": "A.T", "qty": 13, "date": "2026-10-01"}],
                   date(2026, 11, 2))
    names = {f[0] for f in ic.FINDINGS}
    assert {"dcf", "backtest", "paper_lot", "paper_pending"} <= names


def test_golden_dead_contradiction():
    ic.check_golden_dead("ゴールデンクロス 2業種 機械 / 鉱業 |\nデッドクロス 2業種 機械 / 銀行業 |")
    assert ic.FINDINGS and "機械" in ic.FINDINGS[0][2]


def test_clean_inputs_have_no_findings():
    ic.check_us_sector_scores({"sectors": {"XLE": {"composite_score": 61.0}, "XLK": {"composite_score": 40.0}}})
    ic.check_dcf({"results": {"X.T": {"inputs": {"current_price": 100}, "dcf": {"intrinsic_per_share": 150}}}})
    ic.check_backtest({"overall": {"max_drawdown": -23.0}})
    ic.check_golden_dead("ゴールデンクロス 1業種 機械 |\nデッドクロス 1業種 銀行業 |")
    assert ic.FINDINGS == []
