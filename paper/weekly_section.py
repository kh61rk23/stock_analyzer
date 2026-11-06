# ==============================================================
# paper/weekly_section.py — 週次メールの「仮想トレードの週の成績」の節
# ==============================================================
# 【入力】accounts.json（戦略ごとの equity_history・positions）と events.jsonl（約定）。BQ は読まない。
# 【週の成績】最新の評価日から days 暦日さかのぼった日以前の最後の評価額を起点にした増減。履歴がそれより短い戦略は
#   最初の評価額（開始時）を起点にし、起点の日付を表に出す。
# ==============================================================
from __future__ import annotations

from datetime import date, timedelta

SIDE = {"BUY": "買", "SELL": "売"}


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _base_point(hist: list, latest: date, days: int):
    """起点の (日付, 評価額)。latest から days 日以上前の最後の点、無ければ履歴の最初の点。"""
    cut = latest - timedelta(days=days)
    best = None
    for h in hist:
        d = _day(h.get("date"))
        if d is not None and d <= cut:
            best = (d, float(h["equity"]))
    if best is None:
        best = (_day(hist[0].get("date")), float(hist[0]["equity"]))
    return best


def _rows(accounts: dict, days: int):
    """[(戦略, 起点日, 起点の評価額, 最新の評価額, 保有数)] と最新の評価日。"""
    rows, latest_all = [], None
    for sid, a in sorted((accounts or {}).items()):
        hist = [h for h in (a.get("equity_history") or []) if h.get("equity") is not None]
        if not hist:
            continue
        ld = _day(hist[-1].get("date"))
        if ld is None:
            continue
        latest_all = max(latest_all, ld) if latest_all else ld
        bd, be = _base_point(hist, ld, days)
        rows.append((sid, bd, be, float(hist[-1]["equity"]), len(a.get("positions") or {})))
    return rows, latest_all


def headline(accounts: dict, days: int = 7) -> str:
    """冒頭用の1行。全戦略の週の増減の合計と、最良・最悪の戦略。口座が無ければ空文字。"""
    rows, _ = _rows(accounts, days)
    if not rows:
        return ""
    chg = [(sid, (eq - be) / be * 100 if be else 0.0, eq - be) for sid, _, be, eq, _ in rows]
    tot = sum(d for _, _, d in chg)
    best, worst = max(chg, key=lambda x: x[1]), min(chg, key=lambda x: x[1])
    return (f"{len(rows)}戦略の週の増減 合計 {tot:+,.0f}円・最良 {best[0]} {best[1]:+.1f}%・"
            f"最悪 {worst[0]} {worst[1]:+.1f}%")


def weekly_lines(accounts: dict, events: list, days: int = 7, top_n: int = 6) -> list:
    """節の本文（md）。口座が無ければ []。"""
    rows, latest_all = _rows(accounts, days)
    if not rows:
        return []
    lines = ["## 🧪 仮想トレードの週の成績", "",
             f"評価日 {latest_all.isoformat()} 時点・直近{days}日の評価額の増減（戦略ごとの独立した口座）。", "",
             "| 戦略 | 起点 | 評価額 | 週の増減 | 増減率 | 保有 |", "|---|---|---|---|---|---|"]
    for sid, bd, be, eq, npos in rows:
        diff = eq - be
        pct = f"{diff / be * 100:+.1f}%" if be else "—"
        lines.append(f"| {sid} | {bd.month}/{bd.day} | {eq:,.0f}円 | {diff:+,.0f}円 | {pct} | {npos}銘柄 |")
    since = latest_all - timedelta(days=days)
    fills = [e for e in events or [] if e.get("type") == "fill" and (_day(e.get("date")) or date.min) > since]
    lines.append("")
    if fills:
        buys = sum(1 for e in fills if e.get("side") == "BUY")
        lines.append(f"- 約定 {len(fills)}件（買 {buys}・売 {len(fills) - buys}）")
        for e in fills[-top_n:]:
            lines.append(f"  - {str(e.get('date'))[5:]} {e.get('strategy')} {SIDE.get(e.get('side'), e.get('side'))} "
                         f"{e.get('ticker')} {float(e.get('qty') or 0):,.0f}株 @{float(e.get('price') or 0):,.1f}")
    else:
        lines.append("- この期間の約定なし")
    lines.append("")
    return lines


def build_from_files(cfg) -> list:
    from paper.paper_broker import load_accounts, load_events
    return weekly_lines(load_accounts(cfg), load_events(cfg), int(getattr(cfg, "WEEKLY_PAPER_DAYS", 7)))
