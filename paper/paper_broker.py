# ==============================================================
# paper/paper_broker.py — 仮想口座ブローカー（Wave R1-2 / S2）
# ==============================================================
# 日次処理（stock_analyzer_main の日次末尾ステージから呼ばれる）:
#   1) 前日注文を当日始値で仮想約定（共通スキーマ② = backtest/cost_model 共用）
#   2) equity更新 → results["paper_summary"]（戦略別 {equity, day_pnl, mtd_pnl}）
#   3) strategy_adapters で翌日分の注文を生成しキューへ
# 全イベントは events.jsonl に追記され、口座はイベントから再構築可能
# （rebuild_accounts_from_events / 受入条件）。
# ==============================================================

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backtest.cost_model import commission, fill_price   # noqa: E402


# ── 永続化 ───────────────────────────────────────────────────

def _paper_dir(cfg) -> Path:
    p = getattr(cfg, "PAPER_LEDGER_PATH", "stock_data/paper")
    d = Path(p)
    if not d.is_absolute():
        base = Path(getattr(cfg, "DRIVE_BASE", "."))
        # 2026-09-29: Actions では DRIVE_BASE 自体が <workspace>/stock_data のため、
        # base / "stock_data/paper" が stock_data/stock_data/paper になり、
        # コミット対象（git add stock_data/paper/）の外に書いていた。口座と注文が
        # 毎回消え、毎日「新規注文8件・約定0件」を繰り返していた。
        rel = d.parts[1:] if (d.parts and d.parts[0] == "stock_data"
                              and base.name == "stock_data") else d.parts
        cand = base.joinpath(*rel)
        d = cand if base.exists() else Path(p)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_accounts(cfg) -> dict:
    p = _paper_dir(cfg) / "accounts.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_accounts(cfg, accounts: dict) -> None:
    p = _paper_dir(cfg) / "accounts.json"
    p.write_text(json.dumps(accounts, ensure_ascii=False, indent=1),
                 encoding="utf-8")


def append_event(cfg, event: dict) -> None:
    p = _paper_dir(cfg) / "events.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_events(cfg) -> list:
    p = _paper_dir(cfg) / "events.jsonl"
    if not p.exists():
        return []
    events = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return events


# ── イベントソーシング（受入条件: 再構築テスト） ──────────────

def rebuild_accounts_from_events(events: list) -> dict:
    """
    events.jsonl の内容から口座状態（cash/positions/equity_history）を再構築する。
    イベント種別:
      init : {type, strategy, cash}
      fill : {type, strategy, ticker, side, qty, price, fee}
      mark : {type, strategy, date, equity, day_pnl}
    """
    accounts: dict = {}
    for ev in events:
        s = ev.get("strategy")
        if not s:
            continue
        t = ev.get("type")
        if t == "init":
            accounts[s] = {"cash": float(ev["cash"]), "positions": {},
                           "equity_history": []}
        elif t == "fill" and s in accounts:
            acc = accounts[s]
            qty, price = float(ev["qty"]), float(ev["price"])
            fee = float(ev.get("fee", 0))
            pos = acc["positions"].get(ev["ticker"],
                                       {"qty": 0.0, "avg_cost": 0.0})
            if ev["side"] == "BUY":
                acc["cash"] -= price * qty + fee
                total = pos["qty"] + qty
                pos["avg_cost"] = ((pos["avg_cost"] * pos["qty"] + price * qty)
                                   / total) if total > 0 else 0.0
                pos["qty"] = total
                acc["positions"][ev["ticker"]] = pos
            else:
                sell = min(qty, pos["qty"])
                acc["cash"] += price * sell - fee
                pos["qty"] -= sell
                if pos["qty"] <= 0:
                    acc["positions"].pop(ev["ticker"], None)
                else:
                    acc["positions"][ev["ticker"]] = pos
        elif t == "mark" and s in accounts:
            accounts[s]["equity_history"].append(
                {"date": ev["date"], "equity": float(ev["equity"]),
                 "day_pnl": float(ev.get("day_pnl", 0))})
    return accounts


# ── 日次処理 ─────────────────────────────────────────────────

def _default_price_getter(cfg):
    """本番用: yfinance→data_reliability フォールバックで {open, close} を返す。"""
    def get(ticker: str):
        try:
            # 2026-09-10: 実行内キャッシュ経由。日次パイプラインから
            # 保有銘柄ごとに呼ばれるため、素の yfinance だと他モジュールが
            # 既に取得済みの日足を取り直していた。
            try:
                import yf_cache as yf
            except ImportError:  # pragma: no cover - 単体実行時のみ
                import yfinance as yf
            h = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
            if h is not None and len(h) > 0:
                last = h.iloc[-1]
                return {"open": float(last["Open"]), "close": float(last["Close"])}
        except Exception:
            pass
        try:
            from data_reliability import fetch_price_safe
            r = fetch_price_safe(cfg, ticker)
            px = r.get("price") if isinstance(r, dict) else None
            if px:
                return {"open": float(px), "close": float(px)}
        except Exception:
            pass
        return None
    return get


def _mtd_pnl(history: list, equity: float, today: str) -> float:
    month = today[:7]
    base = None
    for h in history:
        if h["date"][:7] == month:
            base = h["equity"]
            break
    return equity - base if base is not None else 0.0


def run_paper_trading(cfg, results: dict, price_getter=None,
                      adapters_override: dict = None,
                      today: str = None) -> dict:
    """
    メインエントリ（stock_analyzer_main から呼ばれる）。
    Returns: {strategy_id: {"equity":.., "day_pnl":.., "mtd_pnl":..,
                            "fills": int, "new_orders": int}, "skipped": bool}
    """
    strategies = list(getattr(cfg, "PAPER_STRATEGIES",
                              ["screener_topN", "ignition", "pead"]))
    initial = float(getattr(cfg, "PAPER_INITIAL_CAPITAL", 2_000_000))
    today = today or datetime.now().strftime("%Y-%m-%d")
    price_of = price_getter or _default_price_getter(cfg)

    if adapters_override is None:
        from paper import strategy_adapters as _sa
        # R4-1: 組込adapter＋playbooks/*.yaml のイベントドリブン戦略を統合
        adapters = _sa.get_all_adapters(cfg)
    else:
        adapters = adapters_override

    # ── 2026-07-25: playbook 戦略の自動編入 ────────────────────
    # get_all_adapters() は playbooks/*.yaml（status=paper かつ side=BUY）を
    # adapter として登録するが、PAPER_STRATEGIES に id が無いと下のループに
    # 入らず、登録されただけで口座が一切作られない状態だった。
    # プレイブックは「YAMLを置けば増える」のが設計意図なので、config への
    # 追記を都度要求せず、登録済み adapter を自動で対象に加える。
    # 明示的に外したい場合は PAPER_INCLUDE_PLAYBOOK_STRATEGIES=False にする。
    if (adapters_override is None
            and getattr(cfg, "PAPER_INCLUDE_PLAYBOOK_STRATEGIES", True)):
        builtin = set(getattr(_sa, "ADAPTERS", {}))
        for sid in adapters:
            if sid not in builtin and sid not in strategies:
                strategies.append(sid)

    accounts = load_accounts(cfg)
    pending_all = accounts.get("_pending", {})
    summary: dict = {}

    for sid in strategies:
        acc = accounts.get(sid)
        if acc is None:
            acc = {"cash": initial, "positions": {}, "equity_history": [],
                   "meta": {}}
            accounts[sid] = acc
            append_event(cfg, {"type": "init", "strategy": sid, "cash": initial,
                               "date": today})

        # 同日再実行の冪等ガード（同じ日のmarkが既にあれば約定・発注をスキップ）
        already = any(h["date"] == today for h in acc["equity_history"])

        fills = 0
        if not already:
            # 1) 前日注文の仮想約定
            for od in pending_all.get(sid, []):
                px = price_of(od["ticker"])
                if not px:
                    continue                        # 価格取得不可→注文失効
                qty = od.get("qty")
                if qty is None:
                    eq_est = acc["cash"] + sum(
                        (price_of(t) or {}).get("close", 0) * p["qty"]
                        for t, p in acc["positions"].items())
                    ref = px["open"]
                    qty = int(max(0.0, od.get("weight", 0)) * eq_est / ref) if ref > 0 else 0
                qty = float(qty)
                if qty <= 0:
                    continue
                price = fill_price(px["open"], od["side"], od["ticker"], cfg)
                fee = commission(price * qty, cfg)
                pos = acc["positions"].get(od["ticker"],
                                           {"qty": 0.0, "avg_cost": 0.0,
                                            "entry_date": today})
                if od["side"] == "BUY":
                    cost = price * qty + fee
                    if cost > acc["cash"]:
                        qty = int((acc["cash"] - fee) / price) if price > 0 else 0
                        if qty <= 0:
                            continue
                        cost = price * qty + fee
                    acc["cash"] -= cost
                    total = pos["qty"] + qty
                    pos["avg_cost"] = (pos["avg_cost"] * pos["qty"]
                                       + price * qty) / total
                    pos["qty"] = total
                    pos.setdefault("entry_date", today)
                    acc["positions"][od["ticker"]] = pos
                else:
                    qty = min(qty, pos["qty"])      # ペーパーはショート不可
                    if qty <= 0:
                        continue
                    acc["cash"] += price * qty - fee
                    pos["qty"] -= qty
                    if pos["qty"] <= 0:
                        acc["positions"].pop(od["ticker"], None)
                    else:
                        acc["positions"][od["ticker"]] = pos
                fills += 1
                append_event(cfg, {"type": "fill", "strategy": sid,
                                   "date": today, "ticker": od["ticker"],
                                   "side": od["side"], "qty": qty,
                                   "price": round(price, 4),
                                   "fee": round(fee, 4),
                                   "tag": od.get("tag", "")})
            pending_all[sid] = []

        # 2) equity 更新
        invested = 0.0
        for t, p in acc["positions"].items():
            px = price_of(t)
            mark = px["close"] if px else p["avg_cost"]   # 取得不可→簿価
            invested += mark * p["qty"]
        equity = acc["cash"] + invested
        prev = acc["equity_history"][-1]["equity"] if acc["equity_history"] else initial
        day_pnl = equity - prev
        if not already:
            acc["equity_history"].append({"date": today,
                                          "equity": round(equity, 2),
                                          "day_pnl": round(day_pnl, 2)})
            acc["equity_history"] = acc["equity_history"][-750:]   # 約3年分
            append_event(cfg, {"type": "mark", "strategy": sid, "date": today,
                               "equity": round(equity, 2),
                               "day_pnl": round(day_pnl, 2)})

        # 3) 翌日分の注文生成（adapter）
        new_orders = []
        if not already:
            adapter = adapters.get(sid)
            if adapter:
                try:
                    new_orders = list(adapter(cfg, results, acc, today) or [])
                except Exception as e:
                    print(f"    ⚠️ [paper:{sid}] adapter例外: {str(e)[:60]}")
            pending_all[sid] = new_orders

        summary[sid] = {"equity": round(equity, 2),
                        "day_pnl": round(day_pnl, 2),
                        "mtd_pnl": round(_mtd_pnl(acc["equity_history"],
                                                  equity, today), 2),
                        "fills": fills,
                        "new_orders": len(new_orders)}

    accounts["_pending"] = pending_all
    save_accounts(cfg, accounts)
    summary["skipped"] = False
    return summary
