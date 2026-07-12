# ==============================================================
# tests/test_event_strategy.py — R4-1 イベントドリブン戦略の受入テスト
# ==============================================================
# 受入条件（要件定義書 §R4-1）:
#   YAMLバリデータ（必須キー・型）＋entry/exit判定のpytest。
#   （S1でのpead_long_v1バックテストは backtest.cli で健太が実行）
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from event_strategy_runner import (eval_conditions, evaluate_entries,      # noqa: E402
                                   evaluate_exit, extract_candidates,
                                   load_playbooks, make_playbook_adapter,
                                   validate_playbook)


PEAD_PB = {
    "id": "pead_long_v1", "event_source": "pead_monitor", "status": "paper",
    "entry": {"side": "BUY", "max_concurrent": 3, "size_pct_of_casino": 5,
              "conditions": [
                  {"field": "surprise_magnitude", "op": ">=", "value": 5},
                  {"field": "pead_score", "op": ">=", "value": 3.0},
                  {"field": "direction", "op": "==", "value": "positive"}]},
    "exit": {"hold_days_max": 40, "take_profit_pct": 15, "stop_loss_pct": -7},
}


class TestValidation:
    def test_valid_playbook(self):
        assert validate_playbook(PEAD_PB) == []

    def test_missing_keys(self):
        errs = validate_playbook({"id": "x"})
        assert any("必須キー欠損" in e for e in errs)

    def test_bad_source(self):
        pb = {**PEAD_PB, "event_source": "unknown_src"}
        assert any("event_source" in e for e in validate_playbook(pb))

    def test_bad_op(self):
        pb = {**PEAD_PB, "entry": {**PEAD_PB["entry"],
              "conditions": [{"field": "x", "op": "~=", "value": 1}]}}
        assert any("未知のop" in e for e in validate_playbook(pb))

    def test_real_playbooks_all_valid(self):
        """実playbooks/*.yaml が全て妥当（4本）。"""
        pbs = load_playbooks(None)
        assert len(pbs) >= 4
        for pb in pbs:
            assert validate_playbook(pb) == [], f"{pb.get('id')}: {validate_playbook(pb)}"


class TestConditions:
    def test_all_true(self):
        cand = {"surprise_magnitude": 8, "pead_score": 4.0, "direction": "positive"}
        assert eval_conditions(PEAD_PB["entry"]["conditions"], cand) is True

    def test_one_false(self):
        cand = {"surprise_magnitude": 3, "pead_score": 4.0, "direction": "positive"}
        assert eval_conditions(PEAD_PB["entry"]["conditions"], cand) is False

    def test_missing_field_is_false(self):
        assert eval_conditions([{"field": "x", "op": ">=", "value": 1}], {}) is False

    def test_type_loose_eq(self):
        assert eval_conditions([{"field": "d", "op": "==", "value": "positive"}],
                               {"d": "positive"}) is True
        assert eval_conditions([{"field": "b", "op": "==", "value": True}],
                               {"b": True}) is True


class TestExtractCandidates:
    def test_pead(self):
        results = {"pead": {"alert_candidates": [
            {"ticker": "A.T", "pead_score": 4}, {"no_ticker": 1}]}}
        cands = extract_candidates("pead_monitor", results)
        assert len(cands) == 1 and cands[0]["ticker"] == "A.T"

    def test_tdnet_buyback(self):
        results = {"tdnet": {"buyback": [{"code": "7203", "category": "BUYBACK"}]}}
        cands = extract_candidates("tdnet_monitor", results)
        assert cands[0]["ticker"] == "7203"

    def test_us8k(self):
        results = {"us_8k": {"ceo_change_tickers": ["AAPL", "MSFT"]}}
        cands = extract_candidates("us_8k_monitor", results)
        assert {c["ticker"] for c in cands} == {"AAPL", "MSFT"}
        assert all(c["event_type"] == "ceo_change" for c in cands)


class TestEntries:
    def test_entry_generates_orders(self):
        results = {"pead": {"alert_candidates": [
            {"ticker": "A.T", "surprise_magnitude": 8, "pead_score": 4.0,
             "direction": "positive"},
            {"ticker": "B.T", "surprise_magnitude": 2, "pead_score": 4.0,
             "direction": "positive"}]}}       # B は surprise 不足
        orders = evaluate_entries(PEAD_PB, results, held=set(), casino_pct=15.0)
        assert [o["ticker"] for o in orders] == ["A.T"]
        # weight = 5% × 15% = 0.75%
        assert orders[0]["weight"] == pytest.approx(0.0075)

    def test_max_concurrent_respected(self):
        pb = {**PEAD_PB, "entry": {**PEAD_PB["entry"], "max_concurrent": 1}}
        results = {"pead": {"alert_candidates": [
            {"ticker": f"T{i}.T", "surprise_magnitude": 8, "pead_score": 4.0,
             "direction": "positive"} for i in range(3)]}}
        orders = evaluate_entries(pb, results, held=set())
        assert len(orders) == 1

    def test_held_excluded(self):
        results = {"pead": {"alert_candidates": [
            {"ticker": "A.T", "surprise_magnitude": 8, "pead_score": 4.0,
             "direction": "positive"}]}}
        assert evaluate_entries(PEAD_PB, results, held={"A.T"}) == []

    def test_watch_side_no_orders(self):
        pb = {**PEAD_PB, "entry": {**PEAD_PB["entry"], "side": "WATCH"}}
        results = {"pead": {"alert_candidates": [
            {"ticker": "A.T", "surprise_magnitude": 8, "pead_score": 4.0,
             "direction": "positive"}]}}
        assert evaluate_entries(pb, results, held=set()) == []

    def test_non_paper_no_orders(self):
        pb = {**PEAD_PB, "status": "retired"}
        results = {"pead": {"alert_candidates": [
            {"ticker": "A.T", "surprise_magnitude": 8, "pead_score": 4.0,
             "direction": "positive"}]}}
        assert evaluate_entries(pb, results, held=set()) == []


class TestExit:
    def test_take_profit(self):
        assert "take_profit" in evaluate_exit(PEAD_PB, 5, 0.16)

    def test_stop_loss(self):
        assert "stop_loss" in evaluate_exit(PEAD_PB, 5, -0.08)

    def test_time_exit(self):
        assert "time_exit" in evaluate_exit(PEAD_PB, 40, 0.05)

    def test_hold(self):
        assert evaluate_exit(PEAD_PB, 5, 0.05) is None


class TestAdapter:
    def test_adapter_entry_and_exit(self):
        adapter = make_playbook_adapter(PEAD_PB)

        class Cfg:
            CASINO_BUDGET_PCT = 15.0
        results = {"pead": {"alert_candidates": [
            {"ticker": "A.T", "surprise_magnitude": 8, "pead_score": 4.0,
             "direction": "positive"}]},
            "screener": {"candidates": [{"ticker": "B.T", "price": 130}]}}
        # 保有 B.T は avg_cost 100 → +30% で take_profit exit、A.T は新規entry
        account = {"positions": {"B.T": {"qty": 100, "avg_cost": 100,
                                         "entry_date": "2026-07-01"}}}
        orders = adapter(Cfg(), results, account, "2026-07-10")
        sides = {(o["ticker"], o["side"]) for o in orders}
        assert ("B.T", "SELL") in sides       # take_profit
        assert ("A.T", "BUY") in sides        # entry
