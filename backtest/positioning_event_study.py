"""大株主・需給の材料が、その後の超過リターンを当てるか（イベントスタディと順位相関）。

  ① 日本の大量保有：法人・経営に関わる目的の新規・買い増し（positioning_signals と同じ条件）の提出日の翌営業日から
     5・20営業日の騰落 − TOPIX ETF（1306.T）
  ② 米国の 13D（新規・変更）：同じく − SPY
  ③ 米国の空売り残：決済日ごとに、返済日数（days_to_cover）・前回比と、その後20営業日の超過の順位相関
使い方: python3 backtest/positioning_event_study.py <作業フォルダ> --fetch   （--fetch で BQ からまとめて取り出す。クエリ4本）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import positioning_signals as ps  # noqa: E402

JP_EV = """
SELECT submit_date, CONCAT(LEFT(issuer_sec_code, 4), '.T') ticker, holder_kind, prior_ratio, holding_ratio, IFNULL(purpose, '') purpose
FROM `{p}.bi.m_large_holdings`
WHERE holding_ratio IS NOT NULL AND issuer_sec_code IS NOT NULL AND holding_ratio - IFNULL(prior_ratio, 0) >= 0.01
QUALIFY ROW_NUMBER() OVER (PARTITION BY issuer_sec_code, holder_name, submit_date ORDER BY doc_id) = 1
"""
US_EV = ps.US_SQL.split("), k AS (")[0].replace("WHERE s.filed_date >= @since AND", "WHERE") + ") SELECT filed_date, ticker, form, percent FROM d"
SI = """SELECT settlement_date, symbol ticker, days_to_cover, change_pct FROM `{p}.analytics.short_interest`
WHERE days_to_cover IS NOT NULL AND settlement_date >= '2025-10-01'"""
PX = """SELECT ticker, trade_date, close FROM `{p}.analytics.price_daily` WHERE trade_date >= '2025-09-01'"""


def fetch(out: Path) -> None:
    from edinet_bq_client import EDINETBQClient
    c = EDINETBQClient().client
    p = c.project
    c.query(JP_EV.format(p=p), location="asia-northeast1").to_dataframe().to_parquet(out / "jp_ev.parquet")
    c.query(US_EV.format(p=p), location="US").to_dataframe().to_parquet(out / "us_ev.parquet")
    c.query(SI.format(p=p), location="asia-northeast1").to_dataframe().to_parquet(out / "si.parquet")
    c.query(PX.format(p=p), location="asia-northeast1").to_dataframe().to_parquet(out / "px.parquet")


def _read(path):
    import pyarrow.parquet as pq
    return pq.read_table(path).to_pandas(ignore_metadata=True)


def forward(close: pd.DataFrame, bench: str, events: pd.DataFrame, horizons=(5, 20)) -> pd.DataFrame:
    """events(ticker, d) → 翌営業日の終値で買って h 日後までの騰落 − 指数（純関数）。"""
    idx = close.index
    rows = []
    for t, d in zip(events["ticker"], events["d"]):
        if t not in close.columns:
            continue
        i = idx.searchsorted(pd.Timestamp(d), side="right")      # 提出日の翌営業日
        r = {"ticker": t, "d": d}
        for h in horizons:
            if i + h >= len(idx):
                r[f"ex{h}"] = np.nan
                continue
            a, b = close[t].iloc[i], close[t].iloc[i + h]
            m0, m1 = close[bench].iloc[i], close[bench].iloc[i + h]
            r[f"ex{h}"] = (b / a - 1) - (m1 / m0 - 1) if a and b and m0 and m1 else np.nan
        rows.append(r)
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, label: str) -> dict:
    out = {"材料": label, "件数": int(df["ex20"].notna().sum())}
    for h in (5, 20):
        x = df[f"ex{h}"].dropna()
        out[f"{h}日の超過の平均"] = round(x.mean() * 100, 2) if len(x) else None
        out[f"{h}日の勝ち"] = round((x > 0).mean() * 100, 1) if len(x) else None
        out[f"{h}日 t値"] = round(x.mean() / x.std(ddof=1) * np.sqrt(len(x)), 2) if len(x) > 2 else None
    return out


def main(work: str, do_fetch: bool) -> None:
    w = Path(work)
    w.mkdir(parents=True, exist_ok=True)
    if do_fetch:
        fetch(w)
    px = _read(w / "px.parquet")
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    close = px.pivot_table(index="trade_date", columns="ticker", values="close", aggfunc="last").sort_index()
    res = []
    jp = _read(w / "jp_ev.parquet")
    jp["active"] = jp.apply(lambda r: bool(re.search(ps.ACTIVE_PURPOSE, r["purpose"])) or (
        str(r["holder_kind"] or "").startswith("法人") and not re.search(ps.STABLE_PURPOSE, r["purpose"])), axis=1)
    jp["d"] = pd.to_datetime(jp["submit_date"])
    jp_close = close.loc[close["1306.T"].notna()]
    for label, sub in [("日本の大量保有・全部の買い増し", jp), ("日本の大量保有・法人と経営に関わる目的", jp[jp["active"]]),
                       ("同・うち新規の報告", jp[jp["active"] & jp["prior_ratio"].isna()])]:
        res.append(summarize(forward(jp_close, "1306.T", sub.drop_duplicates(["ticker", "d"])), label))
    us = _read(w / "us_ev.parquet")
    us["d"] = pd.to_datetime(us["filed_date"])
    us_close = close.loc[close["SPY"].notna()]
    for label, sub in [("米国の 13D（新規と変更）", us), ("米国の 13D・新規だけ", us[us["form"] == "SCHEDULE 13D"])]:
        res.append(summarize(forward(us_close, "SPY", sub.drop_duplicates(["ticker", "d"])), label))
    print(pd.DataFrame(res).to_string(index=False))
    si = _read(w / "si.parquet")
    si["d"] = pd.to_datetime(si["settlement_date"])
    fw = forward(us_close, "SPY", si[["ticker", "d"]].drop_duplicates(), horizons=(20,))
    m = si.merge(fw, on=["ticker", "d"]).dropna(subset=["ex20"])
    ics = []
    for d, g in m.groupby("d"):
        if len(g) >= 200:
            ics.append({"d": d, "dtc": g["days_to_cover"].rank().corr(g["ex20"].rank()),
                        "chg": g["change_pct"].rank().corr(g["ex20"].rank())})
    ic = pd.DataFrame(ics)
    if len(ic):
        for k, name in [("dtc", "空売り残の返済日数"), ("chg", "空売り残の前回比")]:
            x = ic[k].dropna()
            print(f"{name}: 決済日 {len(x)}回・平均IC {x.mean():+.3f}・t {x.mean() / x.std(ddof=1) * np.sqrt(len(x)):+.2f}"
                  f"・1日あたり {int(m.groupby('d').size().mean())}銘柄")


if __name__ == "__main__":
    main(sys.argv[1], "--fetch" in sys.argv)
