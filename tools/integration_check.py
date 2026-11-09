"""結合テスト：本番の出力（状態ファイル・レポート）が満たすべき条件を機械的に検査する。

単体テストは偽の入力で関数を1つずつ見るので、モジュールをつないだときの「数字は出ているが明らかにおかしい」
（範囲外・全部同じ値・矛盾する分類・単元株の無視・止まった評価）を捕まえられない。ここでは本番の出力を入力にして
検査する。BigQuery もネットワークも使わない。

使い方:
  python3 state_store.py restore --root <作業フォルダ> --force     # 本番の状態を取り出す
  python3 tools/integration_check.py --root <作業フォルダ> [--date YYYY-MM-DD]
終了コード: 問題なし 0 ／ 問題あり 1。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

FINDINGS: list[tuple[str, str, str]] = []   # (検査名, 重大度, 内容)


def find(check: str, severity: str, msg: str) -> None:
    FINDINGS.append((check, severity, msg))


def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── 個々の検査（純関数に近い形。入力はファイルの中身） ─────────────────

def check_us_sector_scores(d: dict) -> None:
    sec = (d or {}).get("sectors") or {}
    vals = {k: (v or {}).get("composite_score") for k, v in sec.items()}
    bad = {k: v for k, v in vals.items() if v is not None and not 0 <= float(v) <= 100}
    if bad:
        find("us_sector_scores", "HIGH", f"点数が0〜100の外: {bad}")
    fifty = [k for k, v in vals.items() if v == 50.0]
    if len(fifty) >= 3:
        find("us_sector_scores", "MID", f"ちょうど50点（既定値の疑い）が {len(fifty)} セクター: {fifty}")


def check_dcf(d: dict, max_upside: float = 2.0) -> None:
    res = (d or {}).get("results") or {}
    # schema が無い行は修正前の版で作ったキャッシュ。週次の次回に全件作り直される（dcf_valuation._load_cache）
    stale = bool(res) and not any(isinstance(r, dict) and r.get("schema") for r in res.values())
    out = []
    for t, r in res.items():
        if r.get("dcf_invalid") or r.get("dcf_skip"):
            continue        # 無効・対象外と印が付いた行は加点されない
        try:
            px = float(r["inputs"]["current_price"])
            iv = float(r["dcf"]["intrinsic_per_share"])
        except (KeyError, TypeError, ValueError):
            continue
        if px > 0 and iv / px - 1 > max_upside:
            out.append(f"{t} {iv:,.0f}/{px:,.0f}（{(iv / px - 1) * 100:+.0f}%）")
    if out:
        find("dcf", "LOW" if stale else "HIGH",
             f"理論株価が現在値の{1 + max_upside:.0f}倍超 {len(out)}件（例 {', '.join(out[:5])}）"
             + ("（修正前の版のキャッシュ。次の週次で作り直される）" if stale else ""))


def check_backtest(d: dict, stale: bool = False) -> None:
    ov = (d or {}).get("overall") or {}
    mdd = ov.get("max_drawdown")
    if mdd is not None and float(mdd) <= -99:
        find("backtest", "LOW" if stale else "HIGH", f"最大ドローダウン {mdd}%（全損の表示。計算の誤り）"
             + ("（修正前の週次の出力。次の週次で確認）" if stale else ""))


# 日本株の買いを100株単位にする修正の反映日。これより前の注文日（signal 日）の買いは端株のままの約定が残る
LOT_RULE_FROM = "2026-11-03"


def _odd_lot_buys(events: list[dict]) -> list[dict]:
    """日本株の買いで100株の倍数でない約定。売りは数えない（端株の保有は全量を売るので端数になる）。"""
    out = []
    for ev in events:
        if ev.get("type") == "fill" and ev.get("side") == "BUY" and str(ev.get("ticker", "")).endswith(".T"):
            q = float(ev.get("qty") or 0)
            if q and q % 100:
                out.append(ev)
    return out


def check_paper(accounts: dict, events: list[dict], today: date) -> None:
    odd = _odd_lot_buys(events)
    new = [e for e in odd if str(e.get("created") or e.get("date") or "") >= LOT_RULE_FROM]
    old = [e for e in odd if e not in new]
    if new:
        e = new[0]
        find("paper_lot", "HIGH", "日本株を単元（100株）未満・端数で買い約定している（修正後の注文）"
             f"（例 {e.get('strategy')} {e.get('ticker')} {float(e['qty']):g}株 {e.get('date')}）")
    elif old:
        e = old[-1]
        find("paper_lot", "LOW", f"修正前の注文による端株の買い約定が履歴に {len(old)}件（最新 {e.get('ticker')} "
             f"{float(e['qty']):g}株 {e.get('date')}）。保有は全量を売る前提で、新しい約定には出ていない")
    pend_raw = (accounts or {}).get("_pending") or {}
    pend = [p for v in (pend_raw.values() if isinstance(pend_raw, dict) else [pend_raw]) for p in (v or [])
            if isinstance(p, dict)]
    old = [p for p in pend if str(p.get("created", "")) and
           (today - date.fromisoformat(str(p["created"])[:10])).days > 4]
    if old:
        find("paper_pending", "HIGH", f"約定待ちが5日以上たまっている {len(old)}件（例 {old[0].get('ticker')} {old[0].get('created')}）")
    marks = defaultdict(list)
    for ev in events:
        if ev.get("type") == "mark":
            marks[ev.get("strategy")].append(ev)
    for st, ms in marks.items():
        ms = sorted(ms, key=lambda e: e.get("date", ""))[-6:]
        held = [m for m in ms if (m.get("positions") or m.get("n_positions") or m.get("open"))]
        eqs = [round(float(m.get("equity") or 0), 0) for m in ms]
        if len(ms) >= 5 and len(set(eqs)) == 1 and held:
            find("paper_mark", "HIGH", f"{st}: 保有があるのに直近{len(ms)}回の評価額が同じ {eqs[0]:,.0f}円（評価が止まっている）")
    # 売値と取得単価の比が分割・併合の整数比に近い売りは、基準がずれた偽の損益の疑い。
    # 価格表で確かめて補正（split_correction）か見送り（split_review）の出来事が付いていれば問題なし
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from paper.paper_broker import candidate_split_sells
    for c in candidate_split_sells(events)[:3]:
        find("paper_split", "HIGH", f"{c['strategy']}: {c['ticker']} の売り（{c['sell_date']}）の売値が取得単価の "
             f"{c['price'] / c['avg_cost']:.2f}倍で分割・併合の整数比に近いのに、補正も見送りの記録も無い（偽の実現損益の疑い）")
    # 約定待ちの注文（order）が出ていれば入力は来ている。約定は注文の翌営業日になる
    n_act = defaultdict(int)
    for ev in events:
        if ev.get("type") in ("fill", "order"):
            n_act[ev.get("strategy")] += 1
    for st in (accounts or {}):
        if not st.startswith("_") and n_act.get(st, 0) == 0:
            find("paper_idle", "MID", f"{st}: 開始から注文・約定が0件（戦略の入力が来ていない疑い）")
    # オプションの仮想口座は opt_open で建てる。評価（opt_mark）が1週間分あるのに1本も建っていなければ、
    # 提案の入力（ユニバース・総合点の条件）が届いていない疑い
    opt_marks = sum(1 for ev in events if ev.get("type") == "opt_mark")
    opt_opens = sum(1 for ev in events if ev.get("type") == "opt_open")
    if opt_marks >= 7 and opt_opens == 0:
        find("paper_options_idle", "MID", f"オプションの仮想口座: 評価が{opt_marks}回あるのに建玉が開始から0本（提案の入力が来ていない疑い）")


def check_us_bridge_ledger(note: str) -> None:
    """米国の決算ブリッジ（us_sector_bridge）の台帳の文面。決算があるのに EPS の判定がほぼ全件できていなければ入力不足。"""
    m = re.search(r"決算(\d+).*?beat(\d+).*?miss(\d+)", note or "")
    if not m:
        return
    n, beat, miss = int(m.group(1)), int(m.group(2)), int(m.group(3))
    u = re.search(r"EPS不明(\d+)", note)
    if n >= 20 and beat + miss == 0 and u and int(u.group(1)) >= 0.8 * n:
        find("us_bridge_input", "MID", f"米国の決算ブリッジ: 決算{n}件のうち EPS が取れず判定できないものが {u.group(1)}件"
             "（beat/miss が0なのは入力不足。us.company_facts を引けているか確認）")


def check_score_history(hist: dict, swing: float = 10.0, min_sectors: int = 4, n_days: int = 10) -> None:
    """業種の総合点の履歴：範囲外・全業種同じ値・日替わりの大きな振れ。"""
    days = sorted(hist or {})
    if not days:
        return
    last = hist[days[-1]] or {}
    vals = {c: (v or {}).get("score") for c, v in last.items()}
    bad = {c: v for c, v in vals.items() if v is not None and not -30 <= float(v) <= 100}
    if bad:
        find("score_history_range", "HIGH", f"業種の総合点が範囲外（-30〜100）: {bad}")
    present = [round(float(v), 1) for v in vals.values() if v is not None]
    if len(present) >= 10 and len(set(present)) == 1:
        find("score_history_flat", "HIGH", f"{days[-1]}: 全業種の総合点が同じ値 {present[0]}")
    recent = days[-(n_days + 1):]
    flips = []
    for a, b in zip(recent, recent[1:]):
        big = [c for c, v in (hist[b] or {}).items()
               if c in (hist[a] or {}) and abs(float(v["score"]) - float(hist[a][c]["score"])) >= swing]
        if len(big) >= min_sectors:
            flips.append((b, len(big)))
    if len(flips) >= 3:
        find("score_swing", "MID", f"直近{n_days}日のうち {len(flips)}日で、{min_sectors}業種以上の総合点が前日比 {swing:g}pt 以上動いた"
             f"（例 {flips[-1][0]} {flips[-1][1]}業種。日替わりで点が入れ替わる原因（補正を足すモジュールの日による有無）を確認）")


WEEKLY_PATTERNS = [
    ("weekly_dd", "HIGH", r"最大ドローダウン\s*\|\s*-100\.0%", "バックテストの最大ドローダウンが -100%"),
    ("weekly_longterm", "HIGH", r"回避:\s*33業種", "10年シナリオで33業種すべてが「回避」"),
    ("weekly_dcf", "HIGH", r"\+[1-9]\d{3,}\.\d%\s*\|\s*\+\d+pt", "DCF の割安率が +1000% 超で加点されている"),
    ("weekly_names", "MID", r"\|\s*(Allied Teles|Nomura Corpo|Japan Cash M)", "表の銘柄名が英語の途中で切れている"),
    ("weekly_na", "LOW", r"(\|\s*N/A\s*\|\s*\n.*){5}", "常に N/A の列がある"),
]


# 週次の数値・表示の修正（0ec35c7・008e26e）の反映日。これより前の週次の出力は修正前のコードで作られている
WEEKLY_FIXED_FROM = "2026-11-03"


def check_weekly_md(text: str, stale: bool = False) -> None:
    """stale: 修正前のコードで作られた週次の出力。次の週次で消えるので LOW で知らせるだけにする。"""
    for name, sev, pat, msg in WEEKLY_PATTERNS:
        if re.search(pat, text):
            find(name, "LOW" if stale else sev, msg + ("（修正前の週次の出力。次の週次で確認）" if stale else ""))
    geo = re.findall(r"\|\s*`\d{4}`\s*\|[^|]+\|\s*(\d+)/100", text)
    if len(geo) >= 5 and len(set(geo)) == 1:
        find("weekly_geo", "LOW" if stale else "HIGH", f"業種別の地政学シグナルが全部 {geo[0]}/100（差が付いていない）"
             + ("（修正前の週次の出力。次の週次で確認）" if stale else ""))
    sc = re.findall(r"\|\s*[🔴🟡🟢][^|]*\|\s*([+-][\d,]+(?:万)?円)\s*\|\s*([+-][\d.]+)%", text)
    if sc and all(abs(float(p)) < 0.5 for _, p in sc):
        find("weekly_scenario", "LOW" if stale else "HIGH", "マクロシナリオのポートフォリオ影響が全シナリオで ±0.5% 未満（感応していない）"
             + ("（修正前の週次の出力。次の週次で確認）" if stale else ""))


def check_golden_dead(text: str) -> None:
    g = re.search(r"ゴールデンクロス\s*\d+業種\s*([^|\n]+)", text)
    d = re.search(r"デッドクロス\s*\d+業種\s*([^|\n]+)", text)
    if g and d:
        both = {s.strip() for s in g.group(1).split("/")} & {s.strip() for s in d.group(1).split("/")}
        if both:
            find("golden_dead", "HIGH", f"同じ日にゴールデンクロスとデッドクロスの両方に入る業種: {sorted(both)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="state_store restore で取り出したフォルダ（stock_data/ を含む）")
    ap.add_argument("--date", default=None)
    ap.add_argument("--daily-text", default=None, help="日次メールの本文（テキスト）のファイル。あれば矛盾も検査する")
    a = ap.parse_args(argv)
    sd = Path(a.root) / "stock_data"
    today = date.fromisoformat(a.date) if a.date else date.today()
    d = today.isoformat()

    usj = _json(sd / "insights" / "us_sectors" / f"us_sector_scores_{d}.json")
    if usj is not None:
        check_us_sector_scores(usj)
    check_dcf(_json(sd / "data" / "dcf_valuation_cache.json") or {})
    bt = sd / "data" / "backtest_results.json"
    bt_stale = bt.exists() and date.fromtimestamp(bt.stat().st_mtime).isoformat() < WEEKLY_FIXED_FROM
    check_backtest(_json(bt) or {}, stale=bt_stale)
    ev_path = sd / "paper" / "events.jsonl"
    events = [json.loads(x) for x in ev_path.read_text(encoding="utf-8").splitlines() if x.strip()] if ev_path.exists() else []
    check_paper(_json(sd / "paper" / "accounts.json") or {}, events, today)
    check_score_history(_json(sd / "data" / "score_history.json") or {})
    led = _json(sd / "logs" / f"module_ledger_{d}.json")
    if led is None:
        ls = sorted((sd / "logs").glob("module_ledger_20??-??-??.json"))
        led = _json(ls[-1]) if ls else None
    if led:
        check_us_bridge_ledger((((led.get("modules") or {}).get("us_sector_bridge")) or {}).get("note", ""))
    wk = sorted((sd / "insights" / "weekly").glob("weekly_*.md"))
    if wk:
        wdate = re.search(r"(\d{4}-\d{2}-\d{2})", wk[-1].name)
        check_weekly_md(wk[-1].read_text(encoding="utf-8"),
                        stale=bool(wdate and wdate.group(1) < WEEKLY_FIXED_FROM))
    if a.daily_text:
        check_golden_dead(Path(a.daily_text).read_text(encoding="utf-8"))

    order = {"HIGH": 0, "MID": 1, "LOW": 2}
    for name, sev, msg in sorted(FINDINGS, key=lambda f: order.get(f[1], 9)):
        print(f"[{sev}] {name}: {msg}")
    print(f"結合テスト: 問題 {len(FINDINGS)}件（HIGH {sum(f[1] == 'HIGH' for f in FINDINGS)}）")
    return 1 if FINDINGS else 0


if __name__ == "__main__":
    sys.exit(main())
