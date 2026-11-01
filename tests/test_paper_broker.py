# ==============================================================
# tests/test_paper_broker.py — S2ペーパートレードの受入テスト（R1-2）
# ==============================================================
# 受入条件（要件定義書 §R1-2）:
#   - 2営業日分のゴールデンテスト（既知価格で約定・損益が手計算と一致）
#   - events.jsonl からの口座再構築テスト
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from paper.paper_broker import (load_accounts, load_events,           # noqa: E402
                                rebuild_accounts_from_events,
                                run_paper_trading)
from paper.strategy_adapters import (ignition_adapter, pead_adapter,  # noqa: E402
                                     screener_topn_adapter)
from paper.monthly_report import build_report                         # noqa: E402


class PaperCfg:
    """paperテスト用の最小cfg（PAPER_LEDGER_PATHをtmpに向ける）。"""
    PAPER_STRATEGIES = ["test_strat"]
    PAPER_INITIAL_CAPITAL = 1_000_000
    BT_COMMISSION_BPS = 0
    BT_SLIPPAGE_BPS_JP = 20
    BT_SLIPPAGE_BPS_US = 10

    def __init__(self, tmp):
        self.PAPER_LEDGER_PATH = str(tmp / "paper")
        self.DRIVE_BASE = tmp


def bars(**rows):
    """bars(T7203=[("2026-07-07", 1010, 1015)]) → {"7203.T": [...]}（BQ の price_daily_latest の代わり）。"""
    return {k[1:] + ".T": v for k, v in rows.items()}


class TestGoldenTwoDays:
    def test_fill_and_pnl_match_hand_calc(self, tmp_path):
        cfg = PaperCfg(tmp_path)
        # 7/6（月）の朝の分析（10時）で 7203.T を100株 買い注文 → 東証は寄付のあとなので 7/7 の寄付で約定
        day1_orders = [{"ticker": "7203.T", "side": "BUY", "qty": 100,
                        "tag": "golden"}]
        adapters = {"test_strat": lambda c, r, a, t: day1_orders if t == "2026-07-06" else []}
        s1 = run_paper_trading(cfg, {}, {}, adapters, today="2026-07-06")
        assert s1["test_strat"]["equity"] == pytest.approx(1_000_000)
        assert s1["test_strat"]["new_orders"] == 1
        assert s1["test_strat"]["new_list"][0]["eligible"] == "2026-07-07"

        # 7/7 の朝はまだ 7/7 の始値が無い（当日の行は使わない）→ 約定待ち
        s2 = run_paper_trading(cfg, {}, bars(T7203=[("2026-07-06", 990, 1000), ("2026-07-07", 1010, 1015)]),
                               adapters, today="2026-07-07")
        assert s2["test_strat"]["fills"] == 0 and s2["test_strat"]["pending"] == 1

        # 7/8 の朝：7/7 の始値1010で約定（+20bps → 1012.02）、終値1015で評価
        s3 = run_paper_trading(cfg, {}, bars(T7203=[("2026-07-07", 1010, 1015)]),
                               adapters, today="2026-07-08")
        # cash = 1,000,000 - 100×1012.02 = 898,798
        # equity = 898,798 + 100×1015 = 1,000,298
        assert s3["test_strat"]["fills"] == 1
        f = s3["test_strat"]["fill_list"][0]
        assert f["date"] == "2026-07-07" and f["open"] == 1010 and f["created"] == "2026-07-06"
        assert s3["test_strat"]["equity"] == pytest.approx(1_000_298.0)
        assert s3["test_strat"]["day_pnl"] == pytest.approx(298.0)

        acc = load_accounts(cfg)["test_strat"]
        assert acc["cash"] == pytest.approx(898_798.0)
        assert acc["positions"]["7203.T"]["qty"] == 100
        assert acc["positions"]["7203.T"]["avg_cost"] == pytest.approx(1012.02)

    def test_idempotent_same_day(self, tmp_path):
        """同日再実行では二重約定しない。"""
        cfg = PaperCfg(tmp_path)
        adapters = {"test_strat": lambda c, r, a, t: [
            {"ticker": "7203.T", "side": "BUY", "qty": 100}]}
        px = bars(T7203=[("2026-07-07", 1000, 1000)])
        run_paper_trading(cfg, {}, px, adapters, today="2026-07-06")
        run_paper_trading(cfg, {}, px, adapters, today="2026-07-08")
        s3 = run_paper_trading(cfg, {}, px, adapters, today="2026-07-08")
        assert s3["test_strat"]["fills"] == 0          # 再実行は約定なし
        acc = load_accounts(cfg)["test_strat"]
        assert acc["positions"]["7203.T"]["qty"] == 100


class TestEventSourcing:
    def test_rebuild_from_events(self, tmp_path):
        """events.jsonl から口座を再構築 → accounts.json と一致（受入条件）。"""
        cfg = PaperCfg(tmp_path)
        orders = {"2026-07-06": [{"ticker": "7203.T", "side": "BUY", "qty": 100}],
                  "2026-07-08": [{"ticker": "7203.T", "side": "SELL", "qty": 40}]}
        adapters = {"test_strat": lambda c, r, a, t: orders.get(t, [])}
        px = bars(T7203=[("2026-07-07", 1000, 1000), ("2026-07-08", 1010, 1020),
                         ("2026-07-09", 1030, 1040)])
        for d in ("2026-07-06", "2026-07-08", "2026-07-10"):
            run_paper_trading(cfg, {}, px, adapters, today=d)

        current = load_accounts(cfg)["test_strat"]
        assert current["positions"]["7203.T"]["qty"] == 60
        rebuilt = rebuild_accounts_from_events(load_events(cfg))["test_strat"]
        assert rebuilt["cash"] == pytest.approx(current["cash"])
        assert {t: p["qty"] for t, p in rebuilt["positions"].items()} == \
               {t: p["qty"] for t, p in current["positions"].items()}
        assert [h["equity"] for h in rebuilt["equity_history"]] == \
               [h["equity"] for h in current["equity_history"]]


class TestPlaybookStrategyInclusion:
    """playbooks/*.yaml 由来の戦略が仮想口座を持つこと。

    adapter は登録されるのに PAPER_STRATEGIES に id が無く、口座が一切
    作られないまま空回りする回帰を防ぐ。
    """

    def test_playbook_adapters_get_accounts(self, tmp_path):
        cfg = PaperCfg(tmp_path)
        cfg.PAPER_STRATEGIES = ["screener_topN"]
        cfg.ENABLE_EVENT_STRATEGY = True
        run_paper_trading(cfg, {}, {}, today="2026-07-06")
        accounts = load_accounts(cfg)
        # 組込＋playbook（status=paper かつ side=BUY の3本）が口座を持つ
        assert "screener_topN" in accounts
        assert "pead_long_v1" in accounts, "playbook戦略の口座が作られていない"

    def test_short_playbook_excluded(self, tmp_path):
        """ショート系プレイブックは口座を持たない（B部門鉄則8: ショート禁止）。"""
        cfg = PaperCfg(tmp_path)
        cfg.PAPER_STRATEGIES = ["screener_topN"]
        cfg.ENABLE_EVENT_STRATEGY = True
        run_paper_trading(cfg, {}, {}, today="2026-07-06")
        assert "us8k_ceo_change_short_watch" not in load_accounts(cfg)

    def test_opt_out_flag(self, tmp_path):
        cfg = PaperCfg(tmp_path)
        cfg.PAPER_STRATEGIES = ["screener_topN"]
        cfg.ENABLE_EVENT_STRATEGY = True
        cfg.PAPER_INCLUDE_PLAYBOOK_STRATEGIES = False
        run_paper_trading(cfg, {}, {}, today="2026-07-06")
        assert "pead_long_v1" not in load_accounts(cfg)


class TestAdapters:
    def _account(self):
        return {"cash": 1_000_000, "positions": {}, "equity_history": [],
                "meta": {}}

    def test_screener_topn_monthly(self):
        results = {"screener": {"candidates": [
            {"ticker": f"T{i}.T", "composite_score": 90 - i} for i in range(8)]}}
        acc = self._account()
        orders = screener_topn_adapter(None, results, acc, "2026-07-06")
        assert len(orders) == 5
        assert all(o["side"] == "BUY" for o in orders)
        assert orders[0]["ticker"] == "T0.T"           # スコア降順
        # 同月2回目はリバランスしない
        assert screener_topn_adapter(None, results, acc, "2026-07-07") == []

    def test_screener_topn_empty_results(self):
        assert screener_topn_adapter(None, {}, self._account(),
                                     "2026-07-06") == []

    def test_ignition_adapter_sector_to_ticker(self):
        class C:
            SECTOR_REPRESENTATIVE_TICKERS = {"1017": ["6501.T", "6758.T"]}
        results = {"ignition": {"alerts": [{"sector_code": "1017"}]}}
        orders = ignition_adapter(C(), results, self._account(), "2026-07-06")
        assert [{k: o[k] for k in ("ticker", "side", "weight", "tag")} for o in orders] == [
            {"ticker": "6501.T", "side": "BUY", "weight": 0.05, "tag": "ignition_entry(1017)"}]
        assert "初動" in orders[0]["reason"]["text"]

    def test_pead_adapter_entry_and_time_exit(self):
        results = {"pead": {"alert_candidates": [
            {"ticker": "X.T", "direction": "positive", "pead_score": 5.0},
            {"ticker": "Y.T", "direction": "negative", "pead_score": 4.0}]}}
        acc = self._account()
        orders = pead_adapter(None, results, acc, "2026-07-06")
        assert [o["ticker"] for o in orders if o["side"] == "BUY"] == ["X.T"]
        # 56暦日超の保有は時間exit
        acc["positions"]["Z.T"] = {"qty": 100, "avg_cost": 500,
                                   "entry_date": "2026-05-01"}
        orders = pead_adapter(None, {}, acc, "2026-07-06")
        assert any(o["side"] == "SELL" and o["ticker"] == "Z.T"
                   for o in orders)


class TestMonthlyReport:
    def test_build_report(self):
        accounts = {"test_strat": {
            "cash": 900_000,
            "positions": {"7203.T": {"qty": 100, "avg_cost": 1000.0}},
            "equity_history": [
                {"date": "2026-07-01", "equity": 1_000_000, "day_pnl": 0},
                {"date": "2026-07-31", "equity": 1_050_000, "day_pnl": 100}]},
            "_pending": {}}
        md = build_report(accounts, "202607")
        assert "test_strat" in md and "+5.00%" in md and "7203.T" in md
