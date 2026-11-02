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
    out = []
    for t, r in res.items():
        try:
            px = float(r["inputs"]["current_price"])
            iv = float(r["dcf"]["intrinsic_per_share"])
        except (KeyError, TypeError, ValueError):
            continue
        if px > 0 and iv / px - 1 > max_upside:
            out.append(f"{t} {iv:,.0f}/{px:,.0f}（{(iv / px - 1) * 100:+.0f}%）")
    if out:
        find("dcf", "HIGH", f"理論株価が現在値の{1 + max_upside:.0f}倍超 {len(out)}件（例 {', '.join(out[:5])}）")


def check_backtest(d: dict) -> None:
    ov = (d or {}).get("overall") or {}
    mdd = ov.get("max_drawdown")
    if mdd is not None and float(mdd) <= -99:
        find("backtest", "HIGH", f"最大ドローダウン {mdd}%（全損の表示。計算の誤り）")


def check_paper(accounts: dict, events: list[dict], today: date) -> None:
    for ev in events:
        if ev.get("type") == "fill" and str(ev.get("ticker", "")).endswith(".T"):
            q = float(ev.get("qty") or 0)
            if q and q % 100:
                find("paper_lot", "HIGH", "日本株を単元（100株）未満・端数で約定している"
                     f"（例 {ev.get('strategy')} {ev.get('ticker')} {q:g}株 {ev.get('date')}）")
                break
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
    n_fill = defaultdict(int)
    for ev in events:
        if ev.get("type") == "fill":
            n_fill[ev.get("strategy")] += 1
    for st in (accounts or {}):
        if not st.startswith("_") and n_fill.get(st, 0) == 0:
            find("paper_idle", "MID", f"{st}: 開始から約定が0件（戦略の入力が来ていない疑い）")


WEEKLY_PATTERNS = [
    ("weekly_dd", "HIGH", r"最大ドローダウン\s*\|\s*-100\.0%", "バックテストの最大ドローダウンが -100%"),
    ("weekly_longterm", "HIGH", r"回避:\s*33業種", "10年シナリオで33業種すべてが「回避」"),
    ("weekly_dcf", "HIGH", r"\+[1-9]\d{3,}\.\d%\s*\|\s*\+\d+pt", "DCF の割安率が +1000% 超で加点されている"),
    ("weekly_names", "MID", r"\|\s*(Allied Teles|Nomura Corpo|Japan Cash M)", "表の銘柄名が英語の途中で切れている"),
    ("weekly_na", "LOW", r"(\|\s*N/A\s*\|\s*\n.*){5}", "常に N/A の列がある"),
]


def check_weekly_md(text: str) -> None:
    for name, sev, pat, msg in WEEKLY_PATTERNS:
        if re.search(pat, text):
            find(name, sev, msg)
    geo = re.findall(r"\|\s*`\d{4}`\s*\|[^|]+\|\s*(\d+)/100", text)
    if len(geo) >= 5 and len(set(geo)) == 1:
        find("weekly_geo", "HIGH", f"業種別の地政学シグナルが全部 {geo[0]}/100（差が付いていない）")
    sc = re.findall(r"\|\s*[🔴🟡🟢][^|]*\|\s*([+-][\d,]+(?:万)?円)\s*\|\s*([+-][\d.]+)%", text)
    if sc and all(abs(float(p)) < 0.5 for _, p in sc):
        find("weekly_scenario", "HIGH", "マクロシナリオのポートフォリオ影響が全シナリオで ±0.5% 未満（感応していない）")


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
    check_backtest(_json(sd / "data" / "backtest_results.json") or {})
    ev_path = sd / "paper" / "events.jsonl"
    events = [json.loads(x) for x in ev_path.read_text(encoding="utf-8").splitlines() if x.strip()] if ev_path.exists() else []
    check_paper(_json(sd / "paper" / "accounts.json") or {}, events, today)
    wk = sorted((sd / "insights" / "weekly").glob("weekly_*.md"))
    if wk:
        check_weekly_md(wk[-1].read_text(encoding="utf-8"))
    if a.daily_text:
        check_golden_dead(Path(a.daily_text).read_text(encoding="utf-8"))

    order = {"HIGH": 0, "MID": 1, "LOW": 2}
    for name, sev, msg in sorted(FINDINGS, key=lambda f: order.get(f[1], 9)):
        print(f"[{sev}] {name}: {msg}")
    print(f"結合テスト: 問題 {len(FINDINGS)}件（HIGH {sum(f[1] == 'HIGH' for f in FINDINGS)}）")
    return 1 if FINDINGS else 0


if __name__ == "__main__":
    sys.exit(main())
