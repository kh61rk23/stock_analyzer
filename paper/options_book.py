# ==============================================================
# paper/options_book.py — オプションの仮想口座（S4・証券会社の口座を使わない）
# ==============================================================
# 【役割】options_strategy_advisor の提案（コール買い・現金担保プット）を仮想で建て、毎日評価し、満期で清算する。
#   実発注は一切しない（paper/ と同じ D-01）。
# 【値段】オプションの気配は日本時間の朝には取れない（米国の時間外で 0.0001 などが返る）ため、
#   ブラック・ショールズの理論価格（ボラティリティは原資産の HV・金利 PAPER_OPTIONS_RATE）で値を付ける。
#   建てるときは買いを理論価格×(1+スリッページ)、売りを×(1−スリッページ)、1枚あたり手数料を引く。
# 【口座】ドル建て・PAPER_OPTIONS_START_USD から。現金担保プットは行使価格×100 を担保として現金から外す。
#   建玉は stock_data/paper/options_book.json、出来事は events.jsonl（strategy='options_s4'）に追記する
#   （opt_open・opt_mark・opt_close）。board の「仮想トレード」の画面はこの2つを読む。
# 【上限】同時の建玉 PAPER_OPTIONS_MAX_OPEN 本・同じ銘柄と戦略は1本・コール買い1本の代金は評価額の
#   PAPER_OPTIONS_MAX_PREMIUM_PCT まで。
# ==============================================================
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

STRATEGY_ID = "options_s4"
OPENABLE = {"long_call": ("C", "long"), "cash_secured_put": ("P", "short")}


# ── 純関数 ───────────────────────────────────────────────────

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, vol: float, rate: float, right: str) -> float:
    """ブラック・ショールズの1株あたりの理論価格（満期なら本質的価値）。"""
    if t_years <= 0 or vol <= 0:
        return max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * t_years) / (vol * math.sqrt(t_years))
    d2 = d1 - vol * math.sqrt(t_years)
    if right == "C":
        return spot * _ncdf(d1) - strike * math.exp(-rate * t_years) * _ncdf(d2)
    return strike * math.exp(-rate * t_years) * _ncdf(-d2) - spot * _ncdf(-d1)


def strike_for(spot: float) -> float:
    """行使価格の刻み（25ドル未満 0.5・200ドル未満 1・それ以上 5）で、いちばん近いアット・ザ・マネー。"""
    step = 0.5 if spot < 25 else (1.0 if spot < 200 else 5.0)
    return round(round(spot / step) * step, 2)


def years_to(expiry: str, today: str) -> float:
    return max(0.0, (date.fromisoformat(expiry) - date.fromisoformat(today)).days / 365.0)


def position_value(p: dict, spot: float, vol: float, rate: float, today: str) -> float:
    """建玉の今の値（ドル・ロングは資産、ショートは負債としてマイナス）。"""
    px = bs_price(spot, p["strike"], years_to(p["expiry"], today), vol, rate, p["right"]) * 100 * p["qty"]
    return px if p["side"] == "long" else -px


def equity(book: dict, marks: dict) -> float:
    """現金＋担保＋建玉の値。marks は {建玉ID: 値}。"""
    return round(book["cash"] + sum(p.get("collateral", 0.0) for p in book["positions"])
                 + sum(marks.get(p["id"], 0.0) for p in book["positions"]), 2)


def step(book: dict, metrics: dict, suggestions: list, today: str, cfg=None) -> tuple[dict, list]:
    """1日分：満期の清算 → 評価 → 新しい提案を建てる。(新しい book, 出来事の列) を返す（純関数）。
    metrics は {ticker: {"spot": 値, "hv": 年率}}。値の無い銘柄の建玉は前回の値のまま置く。"""
    g = lambda k, d: getattr(cfg, k, d) if cfg else d
    rate = float(g("PAPER_OPTIONS_RATE", 0.04))
    slip = float(g("PAPER_OPTIONS_SLIPPAGE", 0.05))
    fee = float(g("PAPER_OPTIONS_FEE_PER_CONTRACT", 0.65))
    max_open = int(g("PAPER_OPTIONS_MAX_OPEN", 10))
    max_prem = float(g("PAPER_OPTIONS_MAX_PREMIUM_PCT", 0.05))
    book = json.loads(json.dumps(book))
    events, keep, marks = [], [], {}
    for p in book["positions"]:
        m = metrics.get(p["ticker"]) or {}
        spot, vol = m.get("spot"), m.get("hv") or p["vol_entry"]
        if spot and today >= p["expiry"]:
            intrinsic = bs_price(spot, p["strike"], 0, vol, rate, p["right"]) * 100 * p["qty"]
            cash_in = intrinsic if p["side"] == "long" else -intrinsic
            book["cash"] += p.get("collateral", 0.0) + cash_in
            pnl = cash_in - p["cost"] if p["side"] == "long" else cash_in + p["premium_in"]
            book["realized"] = round(book.get("realized", 0.0) + pnl, 2)
            events.append({"type": "opt_close", "strategy": STRATEGY_ID, "date": today, "id": p["id"], "ticker": p["ticker"],
                           "kind": p["kind"], "right": p["right"], "strike": p["strike"], "expiry": p["expiry"],
                           "spot": round(spot, 2), "value": round(cash_in, 2), "pnl": round(pnl, 2), "reason": "expiry"})
            continue
        if spot:
            p["last_value"] = round(position_value(p, spot, vol, rate, today), 2)
            p["last_spot"] = round(spot, 2)
        marks[p["id"]] = p.get("last_value", 0.0)
        keep.append(p)
    book["positions"] = keep
    eq = equity(book, marks)
    held = {(p["ticker"], p["kind"]) for p in keep}
    for s in suggestions or []:
        kind = s.get("strategy")
        if kind not in OPENABLE or (s.get("ticker"), kind) in held or len(book["positions"]) >= max_open:
            continue
        m = metrics.get(s["ticker"]) or {}
        spot, vol, expiry = m.get("spot"), m.get("hv"), s.get("expiry")
        if not spot or not vol or not expiry or expiry <= today:
            continue
        right, side = OPENABLE[kind]
        k = strike_for(spot)
        theo = bs_price(spot, k, years_to(expiry, today), vol, rate, right)
        pid = f"{today}:{s['ticker']}:{kind}"
        p = {"id": pid, "ticker": s["ticker"], "kind": kind, "right": right, "side": side, "strike": k,
             "expiry": expiry, "qty": 1, "entry_date": today, "spot_entry": round(spot, 2), "vol_entry": vol}
        if side == "long":
            cost = theo * (1 + slip) * 100 + fee
            if cost > eq * max_prem or cost > book["cash"]:
                continue
            book["cash"] -= cost
            p.update(cost=round(cost, 2), premium_in=0.0, collateral=0.0)
        else:
            collateral = k * 100
            premium = theo * (1 - slip) * 100 - fee
            if collateral > book["cash"]:
                continue
            book["cash"] += premium - collateral
            p.update(cost=0.0, premium_in=round(premium, 2), collateral=collateral)
        p["last_value"] = round(position_value(p, spot, vol, rate, today), 2)
        p["last_spot"] = p["spot_entry"]
        book["positions"].append(p)
        held.add((s["ticker"], kind))
        marks[pid] = p["last_value"]
        events.append({"type": "opt_open", "strategy": STRATEGY_ID, "date": today, "id": pid, "ticker": s["ticker"],
                       "kind": kind, "right": right, "side": side, "strike": k, "expiry": expiry, "spot": round(spot, 2),
                       "vol": round(vol, 4), "price": round(theo * (1 + slip if side == "long" else 1 - slip), 4),
                       "cash_flow": round(-p["cost"] if side == "long" else p["premium_in"], 2)})
    eq = equity(book, marks)
    day_pnl = round(eq - float(book.get("equity", book.get("start", eq))), 2)
    book["equity"] = eq
    book.setdefault("history", []).append({"date": today, "equity": eq})
    events.append({"type": "opt_mark", "strategy": STRATEGY_ID, "date": today, "equity": eq, "day_pnl": day_pnl,
                   "open": len(book["positions"]), "cash": round(book["cash"], 2)})
    return book, events


def new_book(start_usd: float) -> dict:
    return {"strategy": STRATEGY_ID, "currency": "USD", "start": float(start_usd), "cash": float(start_usd),
            "equity": float(start_usd), "realized": 0.0, "positions": [], "history": []}


# ── 入出力 ───────────────────────────────────────────────────

def run(cfg, results: dict, today: str | None = None) -> dict:
    from paper.paper_broker import _paper_dir
    if not getattr(cfg, "ENABLE_PAPER_OPTIONS", True):
        return {"skipped": True, "reason": "disabled"}
    oa = results.get("options_advisor") or {}
    metrics = {m["ticker"]: {"spot": m.get("spot"), "hv": m.get("hv")} for m in oa.get("metrics") or [] if m.get("ticker")}
    d = _paper_dir(cfg)
    path = d / "options_book.json"
    try:
        book = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        book = new_book(float(getattr(cfg, "PAPER_OPTIONS_START_USD", 20000)))
    today = today or datetime.now().date().isoformat()
    if (book.get("history") or [{}])[-1].get("date") == today:
        return {"skipped": True, "reason": "今日は済み", "equity": book.get("equity")}
    missing = sorted({p["ticker"] for p in book["positions"]} - set(metrics))
    if missing:
        metrics.update(_spot_hv(missing))
    book, events = step(book, metrics, oa.get("suggestions") or [], today, cfg)
    path.write_text(json.dumps(book, ensure_ascii=False, indent=1), encoding="utf-8")
    with (d / "events.jsonl").open("a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return {"equity": book["equity"], "open": len(book["positions"]), "realized": book.get("realized", 0.0),
            "opened": sum(1 for e in events if e["type"] == "opt_open"),
            "closed": sum(1 for e in events if e["type"] == "opt_close")}


def _spot_hv(tickers: list[str]) -> dict:
    """今日の提案に無い建玉の原資産の終値と HV（yfinance の一括1回）。"""
    try:
        import yfinance as yf
        from options_strategy_advisor import calc_hv
        data = yf.download(tickers, period="3mo", group_by="ticker", progress=False, auto_adjust=False, threads=False)
        out = {}
        for t in tickers:
            try:
                closes = [float(x) for x in (data[t]["Close"] if len(tickers) > 1 else data["Close"]).dropna()]
                if closes:
                    out[t] = {"spot": closes[-1], "hv": calc_hv(closes)}
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"    ⚠️ オプションの仮想口座: 原資産の終値を取れない（{type(e).__name__}）")
        return {}
