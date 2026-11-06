"""ペーパートレード：分割・併合（価格表が後から調整済みに置き換わる場合を含む）と、過去の売りの補正。"""
from types import SimpleNamespace

from paper import paper_broker as pb
from paper.monthly_report import round_trips


def _cfg(tmp_path, strategies=("s",)):
    return SimpleNamespace(PAPER_LEDGER_PATH=str(tmp_path), PAPER_STRATEGIES=list(strategies),
                           PAPER_INITIAL_CAPITAL=2_000_000)


def _run(cfg, accounts, bars, today, adapters=None):
    pb.save_accounts(cfg, accounts)
    return pb.run_paper_trading(cfg, {}, today=today, bars=bars, adapters_override=adapters or {"s": lambda *a: []})


def _pos(**kw):
    p = {"qty": 123.0, "avg_cost": 121.0, "entry_date": "2026-10-19",
         "last_close": 116.0, "last_close_date": "2026-10-30", "split_checked": "2026-10-30"}
    p.update(kw)
    return {"s": {"cash": 0.0, "positions": {"6574.T": p}, "equity_history": [], "meta": {}}, "_pending": {}}


def test_adjusted_table_without_jump_is_detected(tmp_path):
    # 表の過去分が調整済みに取り直され、跳ねが消えている。保有に残した 10/30 の終値 116 と、表の同じ日の 1160 を比べる
    bars = {"6574.T": [("2026-10-29", 1200, 1190), ("2026-10-30", 1250, 1160), ("2026-11-02", 1170, 1180)]}
    cfg = _cfg(tmp_path)
    _run(cfg, _pos(), bars, "2026-11-04")
    a = pb.load_accounts(cfg)["s"]["positions"]["6574.T"]
    assert abs(a["qty"] - 12.3) < 1e-9 and abs(a["avg_cost"] - 1210.0) < 1e-6
    ev = [e for e in pb.load_events(cfg) if e["type"] == "split_adjust"]
    assert len(ev) == 1 and ev[0]["factor"] == 10.0
    _run(cfg, pb.load_accounts(cfg), bars, "2026-11-05")      # 次の日は基準がそろっているので直さない
    assert len([e for e in pb.load_events(cfg) if e["type"] == "split_adjust"]) == 1


def test_split_two_for_one_with_adjusted_table(tmp_path):
    acc = _pos(qty=10.0, avg_cost=3000.0, last_close=3100.0)
    bars = {"6574.T": [("2026-10-30", 1500, 1550), ("2026-11-02", 1560, 1580)]}
    cfg = _cfg(tmp_path)
    _run(cfg, acc, bars, "2026-11-04")
    a = pb.load_accounts(cfg)["s"]["positions"]["6574.T"]
    assert abs(a["qty"] - 20.0) < 1e-9 and abs(a["avg_cost"] - 1500.0) < 1e-6


def test_real_price_move_is_not_a_split(tmp_path):
    bars = {"6574.T": [("2026-10-30", 118, 117), ("2026-11-02", 150, 180)]}    # 同じ日の比は 1.0・その後の +54% は跳ねとみなさない
    cfg = _cfg(tmp_path)
    _run(cfg, _pos(), bars, "2026-11-04")
    assert pb.load_accounts(cfg)["s"]["positions"]["6574.T"]["qty"] == 123.0


def test_pending_sell_after_adjust_uses_adjusted_basis(tmp_path):
    acc = _pos()
    acc["_pending"] = {"s": [{"ticker": "6574.T", "side": "SELL", "qty": 123.0, "eligible": "2026-11-02", "created": "2026-11-01"}]}
    bars = {"6574.T": [("2026-10-30", 1250, 1160), ("2026-11-02", 1135, 1180)]}
    cfg = _cfg(tmp_path)
    _run(cfg, acc, bars, "2026-11-04")
    a = pb.load_accounts(cfg)["s"]
    fill = next(e for e in pb.load_events(cfg) if e["type"] == "fill")
    assert abs(fill["qty"] - 12.3) < 1e-9 and "6574.T" not in a["positions"]     # 株数は併合後の 12.3、売値は表（調整後）の始値
    assert abs(a["cash"] - 12.3 * fill["price"]) < 1e-6


# ── 過去の売りの補正 ─────────────────────────────────────────

def _history(cfg):
    ev = [{"type": "init", "strategy": "s", "cash": 100000.0, "date": "2026-10-18"},
          {"type": "fill", "strategy": "s", "date": "2026-10-19", "ticker": "6574.T", "side": "BUY", "qty": 123.0,
           "price": 121.2, "fee": 0.0, "open": 121.0},
          {"type": "mark", "strategy": "s", "date": "2026-10-20", "equity": 85092.4 + 123 * 116, "day_pnl": 0.0},
          {"type": "mark", "strategy": "s", "date": "2026-10-29", "equity": 85092.4 + 123 * 1200, "day_pnl": 123 * (1200 - 116)},
          {"type": "mark", "strategy": "s", "date": "2026-11-04", "equity": 85092.4 + 123 * 1210, "day_pnl": 600.0},
          {"type": "fill", "strategy": "s", "date": "2026-11-05", "ticker": "6574.T", "side": "SELL", "qty": 123.0,
           "price": 1132.7, "fee": 0.0},
          {"type": "mark", "strategy": "s", "date": "2026-11-06", "equity": 85092.4 + 123 * 1132.7, "day_pnl": -9000.0}]
    for e in ev:
        pb.append_event(cfg, e)
    acc = pb.rebuild_accounts_from_events(ev)
    acc["_pending"] = {}
    return acc


ADJ_BARS = {"6574.T": [("2026-10-19", 1212, 1160), ("2026-10-28", 1195, 1200), ("2026-11-03", 1205, 1210),
                       ("2026-11-05", 1135, 1140)]}


def test_split_correction_once(tmp_path):
    cfg = _cfg(tmp_path)
    acc = _history(cfg)
    _run(cfg, acc, ADJ_BARS, "2026-11-09")
    ev = [e for e in pb.load_events(cfg) if e["type"] == "split_correction"]
    assert len(ev) == 1 and ev[0]["factor"] == 10.0
    assert abs(ev[0]["pnl_recorded"] - (1132.7 - 121.2) * 123) < 1
    assert abs(ev[0]["pnl_true"] - (1132.7 - 1212.0) * 12.3) < 1
    a = pb.load_accounts(cfg)["s"]
    assert abs(a["cash"] - (85092.4 + 12.3 * 1132.7)) < 1e-6
    hist = {h["date"]: h["equity"] for h in a["equity_history"]}
    assert abs(hist["2026-10-20"] - (85092.4 + 123 * 116)) < 1e-6                  # 基準がそろっていた日は触らない
    assert abs(hist["2026-10-29"] - (85092.4 + 12.3 * 1200)) < 0.5                 # ずれていた日は 10分の1 の評価に
    assert abs(hist["2026-11-06"] - (85092.4 + 12.3 * 1132.7)) < 1e-6
    # 2回目以降は効かない
    _run(cfg, pb.load_accounts(cfg), ADJ_BARS, "2026-11-10")
    assert len([e for e in pb.load_events(cfg) if e["type"] == "split_correction"]) == 1
    assert abs(pb.load_accounts(cfg)["s"]["cash"] - a["cash"]) < 1e-6
    # イベントから口座を作り直しても同じ
    rb = pb.rebuild_accounts_from_events(pb.load_events(cfg))["s"]
    assert abs(rb["cash"] - a["cash"]) < 1e-6
    rb_hist = {h["date"]: h["equity"] for h in rb["equity_history"]}
    assert all(abs(rb_hist[d] - v) < 0.01 for d, v in hist.items() if d <= "2026-11-06")


def test_round_trips_use_corrected_basis(tmp_path):
    cfg = _cfg(tmp_path)
    _run(cfg, _history(cfg), ADJ_BARS, "2026-11-09")
    trips = round_trips(pb.load_events(cfg), "s")
    assert len(trips) == 1 and abs(trips[0]["pnl"] - (1132.7 - 1212.0) * 12.3) < 1 and trips[0]["ret"] < 0


def test_genuine_gain_is_reviewed_not_corrected(tmp_path):
    cfg = _cfg(tmp_path)
    ev = [{"type": "init", "strategy": "s", "cash": 100000.0, "date": "2026-10-18"},
          {"type": "fill", "strategy": "s", "date": "2026-10-19", "ticker": "1111.T", "side": "BUY", "qty": 100.0, "price": 100.0, "fee": 0.0},
          {"type": "fill", "strategy": "s", "date": "2026-10-25", "ticker": "1111.T", "side": "SELL", "qty": 100.0, "price": 200.0, "fee": 0.0}]
    for e in ev:
        pb.append_event(cfg, e)
    acc = pb.rebuild_accounts_from_events(ev)
    acc["_pending"] = {}
    bars = {"1111.T": [("2026-10-19", 100, 101), ("2026-10-21", 140, 150), ("2026-10-23", 180, 190),
                       ("2026-10-25", 200, 205)]}   # 表でも買い・売りの基準は同じ（数日かけて本当に2倍）
    _run(cfg, acc, bars, "2026-11-09")
    types = [e["type"] for e in pb.load_events(cfg)]
    assert "split_correction" not in types and types.count("split_review") == 1
    assert abs(pb.load_accounts(cfg)["s"]["cash"] - 110000.0) < 1e-6
    _run(cfg, pb.load_accounts(cfg), bars, "2026-11-10")
    assert [e["type"] for e in pb.load_events(cfg)].count("split_review") == 1


def test_correction_waits_when_table_unavailable(tmp_path):
    cfg = _cfg(tmp_path)
    acc = _history(cfg)
    _run(cfg, acc, {"9999.T": [("2026-11-02", 1, 1)]}, "2026-11-09")     # 6574.T の表が読めない
    assert not [e for e in pb.load_events(cfg) if e["type"] in ("split_correction", "split_review")]
    _run(cfg, pb.load_accounts(cfg), ADJ_BARS, "2026-11-10")              # 読めた次回に補正
    assert len([e for e in pb.load_events(cfg) if e["type"] == "split_correction"]) == 1
