"""paper：約定の時点（日本株は寄付・米国株はその日の米国の寄付）・約定待ちと失効・日次メールの節。"""
import types
from datetime import datetime

from paper import mail_section as ms
from paper import paper_broker as pb


def _at(s):
    return datetime.fromisoformat(s).replace(tzinfo=pb.JST)


def test_eligible_date_jp_before_and_after_open():
    assert pb.eligible_date("7203.T", _at("2026-10-30T08:40")) == "2026-10-30"
    assert pb.eligible_date("7203.T", _at("2026-10-30T09:00")) == "2026-10-31"
    assert pb.eligible_date("7203.T", _at("2026-10-30T10:30")) == "2026-10-31"


def test_eligible_date_us_same_day_session():
    assert pb.eligible_date("AAPL", _at("2026-10-30T10:30")) == "2026-10-30"
    assert pb.eligible_date("AAPL", _at("2026-10-30T23:00")) == "2026-10-31"


def test_fill_bar_skips_weekend_and_today_rows():
    bars = [("2026-10-30", 100, 101), ("2026-11-02", 105, 106), ("2026-11-03", 107, 108)]
    # 土曜（10/31）以降の最初の取引日は 11/2
    assert pb.fill_bar(bars, "2026-10-31", "2026-11-04") == ("2026-11-02", 105.0, 106.0)
    # 実行日当日の行は使わない（場中の途中の値のことがある）
    assert pb.fill_bar(bars, "2026-10-31", "2026-11-02") is None
    assert pb.last_close(bars, "2026-11-03") == ("2026-11-02", 106.0)
    assert pb.last_close([], "2026-11-03") is None


def _cfg(tmp_path):
    base = tmp_path / "stock_data"
    base.mkdir()
    return types.SimpleNamespace(DRIVE_BASE=base, PAPER_LEDGER_PATH="stock_data/paper",
                                 PAPER_STRATEGIES=["s1"], PAPER_INITIAL_CAPITAL=1_000_000,
                                 PAPER_INCLUDE_PLAYBOOK_STRATEGIES=False, PAPER_ORDER_EXPIRE_DAYS=3,
                                 BT_COMMISSION_BPS=0, BT_SLIPPAGE_BPS_JP=0, BT_SLIPPAGE_BPS_US=0)


def test_us_order_fills_at_same_day_us_open(tmp_path):
    cfg = _cfg(tmp_path)
    ad = {"s1": lambda c, r, a, t: [{"ticker": "AAPL", "side": "BUY", "qty": 10}] if t == "2026-10-29" else []}
    pb.run_paper_trading(cfg, {}, bars={}, adapters_override=ad, today="2026-10-29")
    s = pb.run_paper_trading(cfg, {}, bars={"AAPL": [("2026-10-29", 340.0, 345.0)]},
                             adapters_override=ad, today="2026-10-30")
    f = s["s1"]["fill_list"][0]
    assert (f["date"], f["price"], f["session"]) == ("2026-10-29", 340.0, "米国の寄付")
    assert s["s1"]["equity"] == 1_000_000 - 3400 + 3450
    assert s["price"]["asof"] == "2026-10-29"


def test_pending_waits_then_expires_and_no_duplicate(tmp_path):
    cfg = _cfg(tmp_path)
    ad = {"s1": lambda c, r, a, t: [{"ticker": "9999.T", "side": "BUY", "weight": 0.1}]}
    s = pb.run_paper_trading(cfg, {}, bars={}, adapters_override=ad, today="2026-10-01")
    assert s["s1"]["new_orders"] == 1
    s = pb.run_paper_trading(cfg, {}, bars={}, adapters_override=ad, today="2026-10-02")
    assert s["s1"]["new_orders"] == 0 and s["s1"]["pending"] == 1     # 同じ銘柄・同じ売買は足さない
    s = pb.run_paper_trading(cfg, {}, bars={}, adapters_override=ad, today="2026-10-06")
    assert s["s1"]["expired"] == 1                                    # 約定日 10/2 から3日を過ぎた
    assert any(e["type"] == "expire" for e in pb.load_events(cfg))


def test_legacy_pending_without_eligible_is_filled(tmp_path):
    cfg = _cfg(tmp_path)
    # 10/29 の記帳で積まれた注文（約定の時点なし）→ 10/30 の寄付で約定
    hist = [{"date": "2026-10-29", "equity": 1_000_000, "day_pnl": 0}]
    pb.save_accounts(cfg, {"s1": {"cash": 1_000_000, "positions": {}, "equity_history": hist, "meta": {}},
                           "_pending": {"s1": [{"ticker": "7203.T", "side": "BUY", "qty": 100}]}})
    s = pb.run_paper_trading(cfg, {}, bars={"7203.T": [("2026-10-30", 3000.0, 3050.0)]},
                             adapters_override={"s1": lambda *a: []}, today="2026-10-31")
    assert s["s1"]["fills"] == 1


def test_mail_section_lists_orders_fills_and_options():
    summary = {"s1": {"equity": 1_010_000, "day_pnl": 5000, "total_pnl": 10_000, "initial": 1_000_000,
                      "positions": 2, "pending": 3, "expired": 0,
                      "new_list": [{"ticker": "7203.T", "side": "BUY", "weight": 0.05, "eligible": "2026-10-31",
                                    "session": "東証の寄付"}] * 7,
                      "fill_list": [{"ticker": "AAPL", "side": "BUY", "qty": 10, "price": 340.34, "open": 340.0,
                                     "date": "2026-10-29", "session": "米国の寄付", "created": "2026-10-29"}]},
               "skipped": False, "price": {"asof": "2026-10-29", "error": None, "expire_days": 10}}
    opt = [{"type": "opt_open", "strategy": "options_s4", "date": "2026-10-29", "ticker": "NVDA", "kind": "long_call",
            "strike": 200, "expiry": "2026-11-20", "spot": 198.5, "cash_flow": -820},
           {"type": "opt_mark", "strategy": "options_s4", "date": "2026-10-29", "equity": 20150, "day_pnl": 50, "open": 3}]
    a = ms.build_alert(summary, opt, {"start": 20000}, top_n=5)
    body = a["body"]
    assert "10/29 時点" in body and "今日の注文 7件" in body and "…ほか2件" in body
    assert "AAPL 10株 @340.34（10/29 米国の寄付 始値 340.00" in body
    assert "s1: 1,010,000円　前日比 +5,000　開始来 +10,000（+1.0%）" in body
    assert "オプション S4（10/29 記帳" in body and "建てた NVDA コール買い 行使200" in body
    assert a["title"] == "🧪 仮想トレード（新規注文 7件）"


def test_mail_section_none_when_nothing():
    assert ms.build_alert({}, [], None) is None
    assert ms.build_alert({"skipped": False, "price": {}}, [], None) is None


def test_reason_and_timing_recorded_on_order_and_fill(tmp_path):
    cfg = _cfg(tmp_path)
    reason = {"text": "PEAD 点 5.0：上振れ決算", "score": 5.0}
    ad = {"s1": lambda c, r, a, t: [{"ticker": "AAPL", "side": "BUY", "qty": 10, "tag": "t", "reason": reason}]
          if t == "2026-10-29" else []}
    pb.run_paper_trading(cfg, {}, bars={}, adapters_override=ad, today="2026-10-29")
    s = pb.run_paper_trading(cfg, {}, bars={"AAPL": [("2026-10-29", 340.0, 345.0)]},
                             adapters_override=ad, today="2026-10-30")
    ev = pb.load_events(cfg)
    order = next(e for e in ev if e["type"] == "order")
    assert order["reason"] == reason and "米国の寄付" in order["timing"] and order["eligible"] == "2026-10-29"
    f = s["s1"]["fill_list"][0]
    assert f["reason"] == reason and f["created_at"].startswith("2026-10-29T10:00")
    assert "2026-10-29 10:00 のシグナル" in f["timing"] and "始値" in f["timing"]
    body = ms.build_alert(s, [], None)["body"]
    assert "理由: PEAD 点 5.0：上振れ決算" in body


def test_reason_fallback_from_tag():
    assert "利確" in pb.reason_of("tdnet_buyback", {"side": "SELL", "tag": "tdnet_buyback_take_profit(+11.0%)"})["text"]
    assert "買い条件" in pb.reason_of("tdnet_buyback", {"side": "BUY", "tag": "tdnet_buyback_entry"})["text"]


def test_options_events_carry_reason():
    from paper import options_book as ob
    sug = [{"ticker": "AAA", "strategy": "long_call", "expiry": "2026-12-18", "rationale": "HVが低く割安", "hv_percentile": 12}]
    book, ev = ob.step(ob.new_book(20000), {"AAA": {"spot": 20.0, "hv": 0.4}}, sug, "2026-11-02")
    o = next(e for e in ev if e["type"] == "opt_open")
    assert "HVが低く割安" in o["reason_text"] and "HV12%タイル" in o["reason_text"] and o["timing"]
    book, ev = ob.step(book, {"AAA": {"spot": 25.0, "hv": 0.4}}, [], "2026-12-18")
    assert "満期" in next(e for e in ev if e["type"] == "opt_close")["reason_text"]
    body = ms.build_alert({}, [dict(o, strategy="options_s4"), {"type": "opt_mark", "date": "2026-11-02", "equity": 20000, "open": 1}],
                          {"start": 20000})["body"]
    assert "理由: HVが低く割安" in body
