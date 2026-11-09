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
                   [{"type": "fill", "strategy": "s", "side": "BUY", "ticker": "A.T", "qty": 13,
                     "created": "2026-11-04", "date": "2026-11-05"}],
                   date(2026, 11, 5))
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


def _fill(side, qty, created, date_="2026-11-05", t="A.T"):
    return {"type": "fill", "strategy": "s", "side": side, "ticker": t, "qty": qty, "created": created, "date": date_}


def test_old_odd_lot_buys_are_low_and_sells_are_ignored():
    ic.check_paper({"s": {}}, [_fill("BUY", 13, "2026-10-01", "2026-10-02"), _fill("SELL", 13, "2026-11-02")],
                   date(2026, 11, 5))
    lot = [f for f in ic.FINDINGS if f[0] == "paper_lot"]
    assert len(lot) == 1 and lot[0][1] == "LOW"


def test_odd_lot_sell_alone_is_not_a_finding():
    ic.check_paper({"s": {}}, [_fill("SELL", 13, "2026-11-02")], date(2026, 11, 5))
    assert not [f for f in ic.FINDINGS if f[0] == "paper_lot"]


def test_idle_counts_pending_orders():
    ev = [{"type": "order", "strategy": "s", "ticker": "A.T", "side": "BUY"}]
    ic.check_paper({"s": {}, "t": {}}, ev, date(2026, 11, 5))
    idle = [f[2] for f in ic.FINDINGS if f[0] == "paper_idle"]
    assert len(idle) == 1 and idle[0].startswith("t:")


def test_stale_dcf_cache_is_low_and_invalid_rows_are_skipped():
    bad = {"inputs": {"current_price": 100}, "dcf": {"intrinsic_per_share": 1000}}
    ic.check_dcf({"results": {"X.T": bad}})
    assert ic.FINDINGS[0][1] == "LOW"
    ic.FINDINGS.clear()
    ic.check_dcf({"results": {"X.T": {**bad, "schema": 2, "dcf_invalid": True},
                              "Y.T": {**bad, "schema": 2}}})
    assert ic.FINDINGS[0][1] == "HIGH" and "1件" in ic.FINDINGS[0][2]


def test_stale_weekly_output_is_low():
    ic.check_weekly_md("| 最大ドローダウン | -100.0% |", stale=True)
    assert ic.FINDINGS and all(f[1] == "LOW" for f in ic.FINDINGS)


def test_us_bridge_ledger_flags_missing_eps_input():
    ic.check_us_bridge_ledger("決算43 / JP影響業種0業種 / beat0 / miss0 / cross0 / EPS不明40")
    assert [f[0] for f in ic.FINDINGS] == ["us_bridge_input"]
    ic.FINDINGS.clear()
    ic.check_us_bridge_ledger("決算43 / JP影響業種2業種 / beat3 / miss1 / cross4 / EPS不明10")
    ic.check_us_bridge_ledger("決算43 / JP影響業種0業種 / beat0 / miss0 / cross0")      # 旧形式は判定しない
    assert not ic.FINDINGS


def _hist(days):
    return {d: {c: {"score": s} for c, s in sc.items()} for d, sc in days.items()}


def test_score_history_range_flat_and_swing():
    ic.check_score_history(_hist({"2026-11-09": {f"10{i:02d}": 50.0 for i in range(12)}}))
    assert [f[0] for f in ic.FINDINGS] == ["score_history_flat"]
    ic.FINDINGS.clear()
    ic.check_score_history(_hist({"2026-11-09": {"1001": 130.0, "1002": 40.0}}))
    assert [f[0] for f in ic.FINDINGS] == ["score_history_range"]
    ic.FINDINGS.clear()
    days = {}
    for i in range(8):                       # 4業種が毎日 ±12 入れ替わる
        hi = i % 2 == 0
        days[f"2026-11-{i + 1:02d}"] = {f"10{k:02d}": (80.0 if hi else 68.0) if k < 4 else 40.0 + k for k in range(8)}
    ic.check_score_history(_hist(days))
    assert [f[0] for f in ic.FINDINGS] == ["score_swing"]


def test_paper_options_idle():
    ev = [{"type": "opt_mark", "strategy": "options_s4", "date": f"2026-11-0{i}"} for i in range(1, 8)]
    ic.check_paper({}, ev, date(2026, 11, 9))
    assert "paper_options_idle" in {f[0] for f in ic.FINDINGS}
    ic.FINDINGS.clear()
    ic.check_paper({}, ev + [{"type": "opt_open", "strategy": "options_s4", "date": "2026-11-08"}], date(2026, 11, 9))
    assert "paper_options_idle" not in {f[0] for f in ic.FINDINGS}
