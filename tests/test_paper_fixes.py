"""ペーパートレードの修正：分割・併合・単元株・評価の止まり・保有数の上限・損切り・新しい自社株買いだけ・決算の候補。"""
from paper import paper_broker as pb
import event_strategy_runner as esr


def test_split_factor_consolidation_with_flipping_rows():
    # 10株→1株の併合。ビューが日によって未調整・調整済みを入れ替えても、正味の倍率は10
    rows = [("2026-10-19", 121, 116), ("2026-10-22", 102, 103), ("2026-10-23", 1020, 970),
            ("2026-10-26", 99, 94), ("2026-10-27", 920, 920), ("2026-10-30", 1250, 1281)]
    assert pb.split_factor(rows, "2026-10-19", "2026-11-02") == 10.0
    assert pb.split_factor(rows, "2026-10-27", "2026-11-02") == 1.0     # 確認済みの日より後には跳ねなし
    assert pb.split_factor([("a", 1, 100), ("b", 1, 160)], "", "z") == 1.0    # +60% は分割ではない
    assert pb.split_factor([("a", 1, 2000), ("b", 1, 1000)], "", "z") == 0.5  # 1→2 の分割


def test_lot_qty_rounds_to_unit_or_skips():
    assert pb.lot_qty("7203.T", 250.0, 3000, 2_000_000) == 200
    assert pb.lot_qty("7203.T", 40.0, 3000, 2_000_000) == 100       # 1単元30万 ≤ 目標12万×3・口座の20%以内
    assert pb.lot_qty("7203.T", 10.0, 9000, 2_000_000) == 0          # 1単元 90万は口座の20%超
    assert pb.lot_qty("AAPL", 3.7, 200, 2_000_000) == 3


def _run(tmp_path, accounts, bars, today, adapters=None):
    from types import SimpleNamespace
    cfg = SimpleNamespace(PAPER_LEDGER_PATH=str(tmp_path), PAPER_STRATEGIES=["s"], PAPER_INITIAL_CAPITAL=2_000_000)
    pb.save_accounts(cfg, accounts)
    return pb.run_paper_trading(cfg, {}, today=today, bars=bars, adapters_override=adapters or {"s": lambda *a: []}), cfg


def test_split_adjust_and_stale_day_pnl(tmp_path):
    acc = {"s": {"cash": 0.0, "positions": {"6574.T": {"qty": 123.0, "avg_cost": 121.0, "entry_date": "2026-10-19"}},
                 "equity_history": [{"date": "2026-10-31", "equity": 14883.0, "day_pnl": 0, "price_asof": "2026-10-30"}],
                 "meta": {}}, "_pending": {}}
    bars = {"6574.T": [("2026-10-19", 121, 116), ("2026-10-22", 102, 103), ("2026-10-23", 1020, 970),
                       ("2026-10-30", 1250, 1281)]}
    res, cfg = _run(tmp_path, acc, bars, "2026-11-02")
    s = res["s"]
    a = pb.load_accounts(cfg)["s"]["positions"]["6574.T"]
    assert abs(a["qty"] - 12.3) < 1e-9 and abs(a["avg_cost"] - 1210.0) < 1e-6
    assert abs(s["equity"] - 12.3 * 1281) < 1e-6
    assert s["price_stale"] is True                      # 前回と同じ 10/30 の終値で評価した


def test_entries_respect_total_holdings_and_new_buyback_only():
    pbk = {"id": "tdnet_buyback", "event_source": "tdnet_monitor", "status": "paper",
           "entry": {"side": "BUY", "max_concurrent": 3, "size_pct_of_casino": 5,
                     "conditions": [{"field": "category", "op": "==", "value": "BUYBACK"}]},
           "exit": {"hold_days_max": 20}}
    res = {"tdnet": {"buyback": [
        {"code": "1111", "title": "自己株式の取得状況に関するお知らせ"},
        {"code": "2222", "title": "自己株式取得に係る事項の決定に関するお知らせ"},
        {"code": "3333", "title": "自己株式取得に係る事項の決定に関するお知らせ"},
        {"code": "4444", "title": "（訂正）自己株式取得に係る事項の決定"}]}}
    orders = esr.evaluate_entries(pbk, res, held={"9999", "8888"})
    assert [o["ticker"] for o in orders] == ["2222"]     # 保有2 + 新規1 = 上限3。取得状況・訂正は買わない


def test_stop_loss_uses_last_close_for_non_candidates():
    pbk = {"id": "tdnet_buyback", "event_source": "tdnet_monitor", "status": "paper",
           "entry": {"side": "BUY", "max_concurrent": 3, "size_pct_of_casino": 5, "conditions": []},
           "exit": {"hold_days_max": 20, "take_profit_pct": 10, "stop_loss_pct": -6}}
    acct = {"positions": {"228A.T": {"qty": 100, "avg_cost": 2142.0, "entry_date": "2026-10-25",
                                     "last_close": 1620.0}}}
    od = esr.make_playbook_adapter(pbk)(None, {}, acct, "2026-11-02")[0]
    assert od["side"] == "SELL" and "損切り" in od["reason"]["text"]


def test_earnings_candidates_flattened_and_fresh_only():
    es = {"results": {
        "A.T": {"ticker": "A.T", "surprise_score": 90, "consecutive_pos": 2,
                "latest_q": {"quarter": "2026-09-30", "surprise_pct": 12.0}},
        "B.T": {"ticker": "B.T", "surprise_score": 95, "consecutive_pos": 3,
                "latest_q": {"quarter": "2026-06-30", "surprise_pct": 50.0}},     # 古い四半期
        "C.T": {"ticker": "C.T", "latest_q": {"quarter": "2026-09-30", "surprise_pct": float("nan")}}}}
    c = esr.earnings_candidates(es, today="2026-11-02")
    assert [x["ticker"] for x in c] == ["A.T"] and c[0]["surprise_pct"] == 12.0
