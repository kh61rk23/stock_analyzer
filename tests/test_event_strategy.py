# ==============================================================
# tests/test_event_strategy.py — R4-1 イベントドリブン戦略の受入テスト
# ==============================================================
# 受入条件（要件定義書 §R4-1）:
#   YAMLバリデータ（必須キー・型）＋entry/exit判定のpytest。
#   （S1でのpead_long_v1バックテストは backtest.cli で手動実行）
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

    def test_playbooks_have_one_canonical_directory(self):
        root_files = sorted((REPO / "playbooks").glob("*.yaml"))
        duplicate_files = sorted((REPO / "paper" / "playbooks").glob("*.yaml"))
        assert len(root_files) == 4
        assert duplicate_files == []


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


class TestOrderReason:
    """プレイブック戦略の注文が、発表の中身を含む理由を持つ。"""

    def test_buyback_reason_has_announcement(self):
        pb = {**PEAD_PB, "id": "tdnet_buyback", "event_source": "tdnet_monitor",
              "entry": {"side": "BUY", "max_concurrent": 3, "size_pct_of_casino": 5,
                        "conditions": [{"field": "category", "op": "==", "value": "BUYBACK"}]}}
        res = {"tdnet": {"buyback": [{"code": "6758", "name": "ソニーG", "date": "2026-10-30",
                                      "time": "15:00", "title": "自己株式取得に係る事項の決定に関するお知らせ"}]}}
        od = evaluate_entries(pb, res, set())[0]
        r = od["reason"]
        assert "自社株買い" in r["text"] and "2026-10-30 15:00" in r["text"]
        assert "自己株式取得に係る事項" in r["trigger"]

    def test_earnings_reason_has_surprise_rate(self):
        pb = {**PEAD_PB, "id": "earnings_beat_momentum", "event_source": "earnings_surprise",
              "entry": {"side": "BUY", "max_concurrent": 3, "size_pct_of_casino": 5,
                        "conditions": [{"field": "surprise_pct", "op": ">=", "value": 5}]}}
        res = {"earnings_surprise": {"candidates": [{"ticker": "AAA", "surprise_pct": 12.34,
                                                     "guidance_raised": True}]}}
        r = evaluate_entries(pb, res, set())[0]["reason"]
        assert "+12.3%" in r["text"] and "ガイダンス上方修正" in r["text"]

    def test_pead_reason_has_score_and_missing_values_omitted(self):
        res = {"pead": {"alert_candidates": [{"ticker": "AAA", "surprise_magnitude": 6,
                                              "pead_score": 3.5, "direction": "positive"}]}}
        r = evaluate_entries(PEAD_PB, res, set())[0]["reason"]
        assert r["score"] == 3.5 and "出来高" not in r["text"]

    def test_exit_reason_has_days_and_return(self):
        pb = {**PEAD_PB, "status": "paper"}
        acct = {"positions": {"AAA": {"qty": 10, "avg_cost": 100.0, "entry_date": "2026-09-01"}}}
        res = {"screener": {"candidates": [{"ticker": "AAA", "price": 120.0}]}}
        od = make_playbook_adapter(pb)(None, res, acct, "2026-09-10")[0]
        assert "利確" in od["reason"]["text"] and "+20.0%" in od["reason"]["text"]
