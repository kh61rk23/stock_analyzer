"""paper/options_book：理論価格・建てる・評価・満期の清算。"""
from paper import options_book as ob


def test_bs_put_call_parity():
    c = ob.bs_price(100, 100, 0.5, 0.3, 0.04, "C")
    p = ob.bs_price(100, 100, 0.5, 0.3, 0.04, "P")
    import math
    assert abs((c - p) - (100 - 100 * math.exp(-0.04 * 0.5))) < 1e-6
    assert ob.bs_price(110, 100, 0, 0.3, 0.04, "C") == 10 and ob.strike_for(123.4) == 123.0 and ob.strike_for(612) == 610.0


def test_open_mark_and_settle():
    book = ob.new_book(20000)
    sug = [{"ticker": "AAA", "strategy": "long_call", "expiry": "2026-12-18"},
           {"ticker": "BBB", "strategy": "cash_secured_put", "expiry": "2026-12-18"},
           {"ticker": "CCC", "strategy": "covered_call", "expiry": "2026-12-18"}]
    met = {"AAA": {"spot": 20.0, "hv": 0.4}, "BBB": {"spot": 50.0, "hv": 0.3}, "CCC": {"spot": 10.0, "hv": 0.3}}
    book, ev = ob.step(book, met, sug, "2026-11-02")
    assert [e["ticker"] for e in ev if e["type"] == "opt_open"] == ["AAA", "BBB"]      # カバードコールは建てない
    put = next(p for p in book["positions"] if p["ticker"] == "BBB")
    assert put["collateral"] == 5000.0 and put["premium_in"] > 0
    assert abs(book["equity"] - 20000) < 200                                             # 建てた日は手数料とスリッページだけ減る
    book, _ = ob.step(book, met, sug, "2026-11-03")                                       # 同じ提案は二重に建てない
    assert len(book["positions"]) == 2
    met2 = {"AAA": {"spot": 25.0, "hv": 0.4}, "BBB": {"spot": 45.0, "hv": 0.3}}
    book, ev = ob.step(book, met2, [], "2026-12-18")
    closes = {e["ticker"]: e for e in ev if e["type"] == "opt_close"}
    assert closes["AAA"]["value"] == 500.0 and closes["BBB"]["value"] == -500.0          # コールは +5×100・プットは −5×100
    assert book["positions"] == [] and abs(book["equity"] - (20000 + book["realized"])) < 1e-6
