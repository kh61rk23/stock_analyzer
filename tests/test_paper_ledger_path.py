# paper_broker: 口座が stock_data/stock_data/paper のような二重パスに書かれないこと。
# 書かれると Actions でコミットされず、毎日「新規注文のみ・約定0件」になる。
import types

from paper import paper_broker as pb


def _cfg(tmp_path):
    base = tmp_path / "stock_data"
    base.mkdir()
    return types.SimpleNamespace(DRIVE_BASE=base, PAPER_LEDGER_PATH="stock_data/paper",
                                 PAPER_STRATEGIES=["s1"], PAPER_INITIAL_CAPITAL=1_000_000,
                                 PAPER_INCLUDE_PLAYBOOK_STRATEGIES=False)


def test_ledger_is_under_committed_path(tmp_path):
    cfg = _cfg(tmp_path)
    assert pb._paper_dir(cfg) == tmp_path / "stock_data" / "paper"


def test_order_fills_next_day(tmp_path):
    cfg = _cfg(tmp_path)
    adapters = {"s1": lambda cfg, results, acc, today:
                [{"ticker": "7203.T", "side": "BUY", "qty": 100}] if today == "2026-09-29" else []}
    price = lambda t: {"open": 2500.0, "close": 2510.0}
    d1 = pb.run_paper_trading(cfg, {}, price_getter=price, adapters_override=adapters, today="2026-09-29")
    assert d1["s1"]["new_orders"] == 1 and d1["s1"]["fills"] == 0
    d2 = pb.run_paper_trading(cfg, {}, price_getter=price, adapters_override=adapters, today="2026-09-30")
    assert d2["s1"]["fills"] == 1, "前日の注文が翌日に約定していない（口座が保存されていない）"
