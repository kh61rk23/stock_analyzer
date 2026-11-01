# ==============================================================
# paper/mail_section.py — 日次メール（【投資AIエージェント】）の「仮想トレード」の節
# ==============================================================
# 【入力】results["paper_summary"]（paper_broker.run_paper_trading の返り値）と、
#   オプション（S4）は events.jsonl の options_s4 の出来事・options_book.json。
#   オプションの記帳（options_book.run）は通知より後の段で動くため、メールには
#   events.jsonl にある最新の記帳日（ふつうは前回の実行）の分を載せ、その日付を見出しに出す。
# 【長さ】注文・約定・建玉の行はそれぞれ上位 PAPER_MAIL_TOP_N 件＋残りの件数。
# 純関数（build_alert）とファイルを読むだけの入口（build_from_files）。BQ は読まない。
# ==============================================================
from __future__ import annotations

import json

SIDE = {"BUY": "買", "SELL": "売"}
OPT_KIND = {"long_call": "コール買い", "cash_secured_put": "現金担保プット"}


def _md(d: str | None) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}" if d and len(d) >= 10 else "-"


def _yen(v) -> str:
    return f"{float(v):,.0f}円"


def _signed(v, unit: str = "") -> str:
    return f"{float(v):+,.0f}{unit}"


def _qty(od: dict) -> str:
    if od.get("qty") is not None:
        return f"{float(od['qty']):,.0f}株"
    return f"資金の{float(od.get('weight') or 0) * 100:.2g}%"


def _reason_text(e: dict) -> str:
    r = e.get("reason")
    if isinstance(r, dict):
        r = r.get("text")
    return str(r or e.get("tag") or "記録なし")[:160]


def _more(n_all: int, n_shown: int) -> list:
    return [f"  …ほか{n_all - n_shown}件"] if n_all > n_shown else []


def strategy_lines(summary: dict) -> list:
    rows = []
    for sid, s in summary.items():
        if not isinstance(s, dict) or "equity" not in s:
            continue
        init = float(s.get("initial") or 0)
        tot = s.get("total_pnl", s["equity"] - init if init else 0.0)
        pct = f"（{tot / init * 100:+.1f}%）" if init else ""
        rows.append((sid, f"  {sid}: {_yen(s['equity'])}　前日比 {_signed(s['day_pnl'])}　開始来 {_signed(tot)}{pct}"
                          f"　保有{s.get('positions', 0)}銘柄"))
    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows]


def options_lines(opt_events: list, book: dict | None, top_n: int) -> list:
    marks = [e for e in opt_events if e.get("type") == "opt_mark"]
    if not marks:
        return []
    day = max(e.get("date", "") for e in marks)
    m = [e for e in marks if e.get("date") == day][-1]
    start = float((book or {}).get("start") or 0)
    tot = f"　開始来 {float(m['equity']) - start:+,.0f}ドル" if start else ""
    out = [f"■ オプション S4（{_md(day)} 記帳・理論価格）評価 ${float(m['equity']):,.0f}　前日比 {float(m.get('day_pnl') or 0):+,.0f}ドル{tot}"
           f"　建玉{m.get('open', 0)}本"]
    opened = [e for e in opt_events if e.get("type") == "opt_open" and e.get("date") == day]
    closed = [e for e in opt_events if e.get("type") == "opt_close" and e.get("date") == day]
    for e in opened[:top_n]:
        out.append(f"  建てた {e['ticker']} {OPT_KIND.get(e.get('kind'), e.get('kind'))} 行使{e.get('strike')}・満期{e.get('expiry')}"
                   f"（原資産 {e.get('spot')}）現金 {float(e.get('cash_flow') or 0):+,.0f}ドル")
        if e.get("reason_text"):
            out.append(f"    理由: {e['reason_text']}")
    out += _more(len(opened), min(len(opened), top_n))
    for e in closed[:top_n]:
        out.append(f"  清算 {e['ticker']} {OPT_KIND.get(e.get('kind'), e.get('kind'))} 行使{e.get('strike')}（満期・原資産 {e.get('spot')}）"
                   f"損益 {float(e.get('pnl') or 0):+,.0f}ドル")
        if e.get("reason_text"):
            out.append(f"    理由: {e['reason_text']}")
    out += _more(len(closed), min(len(closed), top_n))
    if not opened and not closed:
        out.append("  この日に建てた・清算した玉はなし")
    return out


def build_alert(summary: dict | None, opt_events: list | None = None, opt_book: dict | None = None,
                top_n: int = 5) -> dict | None:
    """日次メールの節（alert 形式）を作る。株の口座もオプションも無ければ None（純関数）。"""
    summary = summary or {}
    opt_lines = options_lines(opt_events or [], opt_book, top_n)
    strat = strategy_lines(summary)
    if not strat and not opt_lines:
        return None
    price = summary.get("price") or {}
    asof = _md(price.get("asof")) if price.get("asof") else "なし"
    head = f"約定＝約定日の始値（BQ）×(1±スリッページ)・評価＝BQ の終値（{asof} 時点）"
    lines = [head]
    if price.get("error"):
        lines.append(f"⚠️ BQ の価格を読めず、約定は次回へ・評価は前回の終値（{price['error'][:60]}）")
    if strat:
        if any(s.get("already") for s in summary.values() if isinstance(s, dict)):
            lines.append("（今日は記帳済み。数字は記帳済みの口座から）")
        lines.append("■ 戦略別（評価額・前日比・開始来）")
        lines += strat
        accts = {k: v for k, v in summary.items() if isinstance(v, dict) and "equity" in v}
        new = [(sid, od) for sid, s in accts.items() for od in s.get("new_list") or []]
        fills = [(sid, e) for sid, s in accts.items() for e in s.get("fill_list") or []]
        pend = sum(int(s.get("pending") or 0) for s in accts.values())
        exp = sum(int(s.get("expired") or 0) for s in accts.values())
        lines.append(f"■ 今日の注文 {len(new)}件（朝の分析で生まれた注文・寄付成行で出したことにする）")
        for sid, od in new[:top_n]:
            lines.append(f"  {sid} {SIDE.get(od.get('side'), od.get('side'))} {od['ticker']} {_qty(od)}"
                         f" → {_md(od.get('eligible'))}以降の{od.get('session', '')}の始値で約定予定")
            lines.append(f"    理由: {_reason_text(od)}")
        lines += _more(len(new), min(len(new), top_n))
        lines.append(f"■ 約定 {len(fills)}件")
        for sid, e in fills[:top_n]:
            lines.append(f"  {sid} {SIDE.get(e.get('side'), e.get('side'))} {e['ticker']} {float(e['qty']):,.0f}株"
                         f" @{float(e['price']):,.2f}（{_md(e.get('date'))} {e.get('session', '')} 始値 {float(e.get('open') or e['price']):,.2f}"
                         f"・{_md(e.get('created'))} の注文）")
            lines.append(f"    理由: {_reason_text(e)}")
        lines += _more(len(fills), min(len(fills), top_n))
        lines.append(f"■ 約定待ち {pend}件・失効 {exp}件（約定日の始値が BQ に入るまで待つ・{price.get('expire_days', 10)}日を過ぎたら失効）")
    if opt_lines:
        lines += opt_lines
    total_new = sum(len(s.get("new_list") or []) for s in summary.values() if isinstance(s, dict))
    return {"trigger": "paper_trading", "title": f"🧪 仮想トレード（新規注文 {total_new}件）",
            "body": "\n".join(lines), "severity": "info"}


def build_from_files(cfg, summary: dict | None) -> dict | None:
    """events.jsonl と options_book.json を読んで build_alert を呼ぶ。"""
    from paper.paper_broker import _paper_dir
    d = _paper_dir(cfg)
    opt_events = []
    p = d / "events.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if '"options_s4"' not in line:
                continue
            try:
                opt_events.append(json.loads(line))
            except ValueError:
                continue
    try:
        book = json.loads((d / "options_book.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        book = None
    return build_alert(summary, opt_events, book, int(getattr(cfg, "PAPER_MAIL_TOP_N", 5)))
