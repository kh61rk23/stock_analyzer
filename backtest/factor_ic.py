# ==============================================================
# backtest/factor_ic.py — 含み益ファクター（修正PBR・資産バリュー倍率・含み益÷時価総額）の断面検証
# ==============================================================
# 【やること】毎月末、その時点で提出済みの有報だけを使って因子を作り、翌12か月（と翌1か月）の
#   配当込みリターン（1306.T 超過）との順位相関（IC）を出す。対照に通常PBRを並べる。
#   補足として、毎年7月末に因子上位20%を等ウェイトで1年持った場合の超過リターンも出す。
# 【時点整合】
#   - 会社×年度の行（backtest/re_factor_panel.py）は、有報の提出日（無ければ決算期末＋3か月）以降だけ使う。
#     提出から15か月を過ぎた行は古すぎるとして使わない。
#   - 時価総額＝評価日の終値（分割調整済み）× 期末の流通株式数 × 期末より後の株式分割の倍率。
#     yfinance の終値は後の分割で遡って調整されるため、倍率を掛けないと分割前の時価総額が小さく出る。
#   - 参照地価は保有不動産の行と同じ年度の値（パネル作成時に反映済み）。
# 【母集団】評価日の時価総額100億円以上、銀行・証券・保険・その他金融（1028〜1031）を除く、
#   含み益が時価総額の3倍超の会社を除く。
# 【既知のバイアス】保有不動産のデータは今上場している会社だけで、上場廃止の会社が入らない（生存者バイアス）。
#   株価が取れない銘柄の数は報告に出す。
# 【実行】python3 -m backtest.factor_ic   （株価は stock_data/cache/backtest/re_factor_prices.json にキャッシュ）
# ==============================================================
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path

TAX_RATE = 0.306
MIN_MCAP = 1e10
EXCLUDED_SECTORS = {"1028", "1029", "1030", "1031"}
STALE_DAYS = 456          # 提出から約15か月
BENCH = "1306.T"
FACTORS = {               # 名前: (向き。大きいほど良いなら +1)
    "adj_pbr": -1,
    "asset_value_ratio": +1,
    "gain_to_mcap": +1,
    "pbr": -1,
}


# ── 純関数 ────────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _d(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def available_from(row: dict) -> tuple[date | None, bool]:
    """その行を使ってよい最初の日と、提出日を「決算期末＋3か月」とみなしたか。"""
    d = _d(row.get("disclosure_date"))
    if d:
        return d, False
    fye = _d(row.get("fiscal_year_end"))
    return (fye + timedelta(days=92), True) if fye else (None, False)


def latest_rows(panel: list[dict], at: date) -> dict:
    """{code4: 評価日 at に使える最新の行}。提出から STALE_DAYS を過ぎた行は使わない。"""
    out: dict = {}
    for r in panel:
        avail, _ = available_from(r)
        if avail is None or avail > at or (at - avail).days > STALE_DAYS:
            continue
        cur = out.get(r["code4"])
        if cur is None or int(r["fiscal_year"]) > int(cur["fiscal_year"]):
            out[r["code4"]] = r
    return out


def split_factor_after(splits: list, after: date) -> float:
    """after より後の株式分割の倍率の積（splits=[(日付文字列, 倍率)]）。"""
    f = 1.0
    for d, ratio in splits or []:
        dd = _d(d)
        if dd and dd > after and ratio and ratio > 0:
            f *= ratio
    return f


def factors_for(row: dict, close: float, splits: list) -> dict | None:
    """1社の因子。時価総額が出せない・含み益が時価総額の3倍超なら None。"""
    shares, equity = _f(row.get("float_shares")), _f(row.get("equity"))
    gain_m = _f(row.get("gain_m_yen"))
    fye = _d(row.get("fiscal_year_end"))
    if not (shares and shares > 0 and close and close > 0 and fye and gain_m is not None):
        return None
    mcap = close * shares * split_factor_after(splits, fye)
    gain = gain_m * 1e6
    if gain > 3 * mcap:
        return None
    after = gain * (1 - TAX_RATE)
    cash, debt = _f(row.get("cash_and_equiv")), _f(row.get("interest_bearing_debt"))
    out = {"mcap": mcap, "gain_to_mcap": gain / mcap,
           "adj_pbr": None, "pbr": None, "asset_value_ratio": None}
    if equity and equity > 0:
        out["pbr"] = mcap / equity
    if equity is not None and equity + after > 0:
        out["adj_pbr"] = mcap / (equity + after)
    if cash is not None:
        out["asset_value_ratio"] = (cash - (debt or 0.0) + after) / mcap
    return out


def _ranks(xs: list) -> list:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1
        i = j + 1
    return r


def spearman(a: list, b: list) -> float | None:
    """順位相関（同順位は平均順位）。3組未満や分散0は None。"""
    if len(a) != len(b) or len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    if va == 0 or vb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / math.sqrt(va * vb)


def nw_tstat(xs: list, lags: int) -> float | None:
    """平均の t 値（Newey-West。翌12か月リターンは月ごとに重なるため lags=11 で自己相関を割り引く）。"""
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    s = sum(v * v for v in e) / n
    for L in range(1, min(lags, n - 1) + 1):
        w = 1 - L / (lags + 1)
        s += 2 * w * sum(e[i] * e[i - L] for i in range(L, n)) / n
    if s <= 0:
        return None
    return m / math.sqrt(s / n)


def month_ends(start: date, end: date) -> list[date]:
    out, y, m = [], start.year, start.month
    while True:
        nxt = date(y + (m // 12), m % 12 + 1, 1)
        d = nxt - timedelta(days=1)
        if d > end:
            return out
        if d >= start:
            out.append(d)
        y, m = nxt.year, nxt.month


def price_at(series: list, at: date, tolerance_days: int = 10):
    """series=[(日付文字列, 終値, 配当込み終値)] 昇順から、at 以前で最も近い値（tolerance 日以内）。"""
    keys = [s[0] for s in series]
    i = bisect_right(keys, at.isoformat()) - 1
    if i < 0:
        return None
    d = _d(series[i][0])
    if d is None or (at - d).days > tolerance_days:
        return None
    return series[i]


def add_months(d: date, n: int) -> date:
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return month_ends(date(y, m, 1), date(y, m, 28) + timedelta(days=4))[0]


# ── 株価（yfinance・キャッシュ）────────────────────────────────

def load_prices(tickers: list, start: str, cache: Path, chunk: int = 100, pause: float = 5.0) -> dict:
    """{ticker: {"px": [(日付, 終値, 配当込み終値)], "splits": [(日付, 倍率)]}}。月末値だけを持つ。"""
    data = {}
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
    todo = [t for t in tickers if t not in data]
    if todo:
        import yfinance as yf
        for i in range(0, len(todo), chunk):
            part = todo[i:i + chunk]
            df = None
            for attempt in (1, 2):
                try:
                    df = yf.download(part, start=start, interval="1d", auto_adjust=False, actions=True,
                                     group_by="ticker", threads=8, progress=False)
                    break
                except Exception as e:
                    print(f"  例外 {type(e).__name__}: {str(e)[:80]}（60秒待って再試行）")
                    time.sleep(60)
            got = 0
            for t in part:
                try:
                    sub = df[t] if df is not None and t in set(df.columns.get_level_values(0)) else None
                except Exception:
                    sub = None
                if sub is None or "Close" not in sub.columns:
                    data[t] = {"px": [], "splits": []}
                    continue
                sub = sub.dropna(subset=["Close"])
                spl = [(ix.date().isoformat(), float(v)) for ix, v in sub.get("Stock Splits", []).items()
                       if v and float(v) > 0] if "Stock Splits" in sub.columns else []
                me = sub.groupby([sub.index.year, sub.index.month]).tail(1)
                data[t] = {"px": [(ix.date().isoformat(), float(r["Close"]), float(r["Adj Close"]))
                                  for ix, r in me.iterrows()], "splits": spl}
                got += 1 if data[t]["px"] else 0
            print(f"  株価 {min(i + chunk, len(todo))}/{len(todo)}（このチャンクで取得 {got}/{len(part)}）")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data), encoding="utf-8")
            time.sleep(pause)
    return data


# ── 本体 ─────────────────────────────────────────────────────

def build_observations(panel: list[dict], prices: dict, dates: list[date], horizons=(12, 1)) -> tuple[list, dict]:
    """[(評価日, code4, 因子dict, {h: 超過リターン})] と集計の内訳。"""
    bench = (prices.get(BENCH) or {}).get("px") or []
    stats = {"no_price": set(), "assumed_disclosure": 0, "rows_used": 0}
    for r in panel:
        if available_from(r)[1]:
            stats["assumed_disclosure"] += 1
    obs = []
    for t in dates:
        rows = latest_rows(panel, t)
        b0 = price_at(bench, t)
        for code, r in rows.items():
            if r.get("sector_code") in EXCLUDED_SECTORS:
                continue
            p = prices.get(f"{code}.T") or {}
            s = p.get("px") or []
            if not s:
                stats["no_price"].add(code)
                continue
            p0 = price_at(s, t)
            if not p0:
                continue
            fac = factors_for(r, p0[1], p.get("splits"))
            if not fac or fac["mcap"] < MIN_MCAP:
                continue
            fwd = {}
            for h in horizons:
                t1 = add_months(t, h)
                p1, b1 = price_at(s, t1), price_at(bench, t1)
                if p1 and b0 and b1 and p0[2] > 0 and b0[2] > 0:
                    fwd[h] = (p1[2] / p0[2] - 1) - (b1[2] / b0[2] - 1)
            obs.append((t, code, fac, fwd))
    stats["no_price"] = len(stats["no_price"])
    return obs, stats


def ic_table(obs: list, horizon: int, common: bool = False) -> dict:
    """因子ごとの月次 IC 系列と要約。common=True は全因子がそろう銘柄だけで比べる。"""
    by_date: dict = {}
    for t, code, fac, fwd in obs:
        if horizon in fwd:
            by_date.setdefault(t, []).append((fac, fwd[horizon]))
    out = {}
    for name, sign in FACTORS.items():
        series = []
        for t in sorted(by_date):
            pts = by_date[t]
            if common:
                pts = [p for p in pts if all(p[0].get(k) is not None for k in FACTORS)]
            xs = [sign * p[0][name] for p in pts if p[0].get(name) is not None]
            ys = [p[1] for p in pts if p[0].get(name) is not None]
            ic = spearman(xs, ys)
            if ic is not None:
                series.append((t, ic, len(xs)))
        ics = [s[1] for s in series]
        yearly: dict = {}
        for t, ic, _ in series:
            yearly.setdefault(t.year, []).append(ic)
        out[name] = {
            "months": len(ics),
            "mean": sum(ics) / len(ics) if ics else None,
            "t_nw": nw_tstat(ics, 11 if horizon == 12 else 0),
            "pos_share": sum(1 for x in ics if x > 0) / len(ics) if ics else None,
            "avg_n": sum(s[2] for s in series) / len(series) if series else None,
            "yearly": {y: sum(v) / len(v) for y, v in sorted(yearly.items())},
        }
    return out


def quintile_portfolio(obs: list, month: int = 7) -> dict:
    """毎年 month 月末に因子上位20%を等ウェイトで12か月。母集団（等ウェイト）との差と1306.T 超過。"""
    out = {}
    for name, sign in FACTORS.items():
        years = []
        for t in sorted({o[0] for o in obs if o[0].month == month}):
            pts = [(sign * o[2][name], o[3][12]) for o in obs
                   if o[0] == t and o[2].get(name) is not None and 12 in o[3]]
            if len(pts) < 25:
                continue
            pts.sort(key=lambda p: p[0], reverse=True)
            top = pts[:max(1, len(pts) // 5)]
            r_top = sum(p[1] for p in top) / len(top)
            r_all = sum(p[1] for p in pts) / len(pts)
            years.append({"year": t.year, "n": len(pts), "top_excess": r_top, "spread_vs_universe": r_top - r_all})
        sp = [y["spread_vs_universe"] for y in years]
        out[name] = {"years": years,
                     "mean_top_excess": sum(y["top_excess"] for y in years) / len(years) if years else None,
                     "mean_spread": sum(sp) / len(sp) if sp else None,
                     "spread_pos_share": sum(1 for x in sp if x > 0) / len(sp) if sp else None}
    return out


def judge(ic12: dict, ic12_common: dict) -> dict:
    """設計書の採用基準（効果・固有性）。頑健性（walk-forward・bootstrap）はここでは判定しない。"""
    res = {}
    for name in ("adj_pbr", "asset_value_ratio"):
        a = ic12.get(name) or {}
        c, cp = ic12_common.get(name) or {}, ic12_common.get("pbr") or {}
        effect = (a.get("mean") or 0) > 0 and (a.get("t_nw") or 0) >= 2
        unique = (c.get("mean") is not None and cp.get("mean") is not None and c["mean"] > cp["mean"])
        res[name] = {"effect": effect, "unique": unique}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="stock_data/data/backtest/re_factor_panel.csv")
    ap.add_argument("--cache", default="stock_data/cache/backtest/re_factor_prices.json")
    ap.add_argument("--start", default="2017-01-31")
    ap.add_argument("--out", default="stock_data/data/backtest/re_factor_ic.json")
    args = ap.parse_args(argv)
    panel = list(csv.DictReader(open(args.panel, encoding="utf-8")))
    tickers = sorted({f"{r['code4']}.T" for r in panel}) + [BENCH]
    prices = load_prices(tickers, "2016-06-01", Path(args.cache))
    today = date.today()
    dates = month_ends(_d(args.start), add_months(today, -1))
    obs, stats = build_observations(panel, prices, dates)
    res = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "panel_rows": len(panel), "companies": len({r["code4"] for r in panel}),
        "observations": len(obs), "eval_months": len(dates), **stats,
        "ic_12m": ic_table(obs, 12), "ic_12m_common": ic_table(obs, 12, common=True),
        "ic_1m": ic_table(obs, 1), "quintile_july": quintile_portfolio(obs),
    }
    res["judge"] = judge(res["ic_12m"], res["ic_12m_common"])
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for k in ("ic_12m", "ic_12m_common", "ic_1m"):
        print(k)
        for name, v in res[k].items():
            print(f"  {name:18s} months={v['months']:3d} mean={v['mean'] if v['mean'] is None else round(v['mean'], 4)} "
                  f"t={v['t_nw'] if v['t_nw'] is None else round(v['t_nw'], 2)} pos={v['pos_share'] and round(v['pos_share'], 2)} n≈{v['avg_n'] and round(v['avg_n'])}")
    print("judge", res["judge"])
    print(f"✅ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
