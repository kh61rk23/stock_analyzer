"""銘柄独自の値動き・技術マップのタグ（マッピング）に応じた値動きと財務の動きが、その後20営業日の TOPIX 超過を当てるか。

screener_universe の idio 観点（独自ボラ・テーマとの差）と theme 観点の theme_sales_g の効きを測り直す検証。
入力  : BigQuery（analytics.price_daily_latest の日本株・taxonomy.company_taxonomy_map・edinet.company・edinet.financials
        と doc_index の提出日）を <作業フォルダ> に parquet で取り出して使う（--fetch）。約0.1GB。
出力  : 指標ごとの平均 IC・t値・上位5分の1−下位の差を表で出す。<作業フォルダ>/features.parquet に評価日ごとの値。
実行  : python3 backtest/idio_theme_ic.py <作業フォルダ> --fetch    （2回目以降は --fetch を付けずに計算だけ）
注意  : 評価日の間隔（10営業日）より先の期間（20営業日）が長いので、隣り合う評価日は重なる（t値は大きめに出る）。
        タグは今の対応表を過去にも当てている（先読みが少し入る）。財務は提出日（無ければ期末＋90日）までに出たものだけ。
作業フォルダは git の外に置く（データを git に入れない）。
"""
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

S = sys.argv[1]
H = 20            # 先の期間（営業日）

FETCH = {
    "px": "SELECT ticker, trade_date, close, volume FROM `{p}.analytics.price_daily_latest` WHERE ENDS_WITH(ticker,'.T') AND close > 0",
    "tax": "SELECT company_id, node_id, relevance_score, confidence_type FROM `{p}.taxonomy.company_taxonomy_map` "
           "WHERE valid_to IS NULL OR valid_to >= CURRENT_DATE('Asia/Tokyo')",
    "co": "SELECT LEFT(sec_code,4) code, ANY_VALUE(industry) industry, ANY_VALUE(name) name FROM `{p}.edinet.company` "
          "WHERE sec_code IS NOT NULL AND NOT IFNULL(is_delisted,FALSE) GROUP BY code",
    "fin": "SELECT f.company_code code, f.fiscal_year_end fye, f.net_sales, f.operating_income, "
           "COALESCE(d.disclosure_date, DATE_ADD(f.fiscal_year_end, INTERVAL 90 DAY)) AS avail "
           "FROM `{p}.edinet.financials` f LEFT JOIN (SELECT doc_id, ANY_VALUE(disclosure_date) disclosure_date "
           "FROM `{p}.edinet.doc_index` GROUP BY doc_id) d USING (doc_id) WHERE f.fiscal_year_end >= '2022-01-01'",
}
if "--fetch" in sys.argv:
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    Path(S).mkdir(parents=True, exist_ok=True)
    for name, sql in FETCH.items():
        client.query(sql.format(p=client.project), location="asia-northeast1").to_dataframe().to_parquet(f"{S}/{name}.parquet")
        print(f"取り出し {name}")


def rp(path):
    """BigQuery から書いた parquet は日付が dbdate 型の印つき。印を無視して読む。"""
    return pq.read_table(path).to_pandas(ignore_metadata=True)


px = rp(f"{S}/px.parquet")
px["trade_date"] = pd.to_datetime(px["trade_date"])
close = px.pivot_table(index="trade_date", columns="ticker", values="close", aggfunc="last").sort_index()
vol = px.pivot_table(index="trade_date", columns="ticker", values="volume", aggfunc="last").sort_index()
mkt = close["1306.T"]
close = close.drop(columns=["1306.T"])
# 取引日は市場（1306.T）がある日にそろえる
close = close.loc[mkt.dropna().index]
vol = vol.reindex(close.index)
ret = close.pct_change(fill_method=None)
mret = mkt.loc[close.index].pct_change()
turn = (close * vol).rolling(20).mean()

tax = rp(f"{S}/tax.parquet")
tax["ticker"] = tax["company_id"] + ".T"
tax = tax[tax["ticker"].isin(close.columns)]
tax = tax.groupby(["ticker", "node_id"], as_index=False)["relevance_score"].max()
co = rp(f"{S}/co.parquet")
co["ticker"] = co["code"] + ".T"
ind = co.set_index("ticker")["industry"]

fin = rp(f"{S}/fin.parquet")
fin["ticker"] = fin["code"].astype(str).str[:4] + ".T"
fin["avail"] = pd.to_datetime(fin["avail"])
fin = fin.sort_values(["ticker", "fye"])
fin["sales_g"] = fin.groupby("ticker")["net_sales"].pct_change(fill_method=None)
fin["op_g"] = fin.groupby("ticker")["operating_income"].diff() / fin.groupby("ticker")["operating_income"].shift().abs()

dates = close.index
ev = [d for i, d in enumerate(dates) if i >= 130 and i + H < len(dates) and (i - 130) % 10 == 0]
print(f"取引日 {len(dates)}（{dates[0].date()}〜{dates[-1].date()}）・評価日 {len(ev)}（{ev[0].date()}〜{ev[-1].date()}）・銘柄 {close.shape[1]}")


def node_avg(values: pd.Series) -> pd.Series:
    """銘柄の値 → ノードごとの平均（関与度で重み）→ 会社ごとに関与度で重み付き平均。自分自身は抜かない（数が多いので影響は小さい）。"""
    t = tax.merge(values.rename("v"), left_on="ticker", right_index=True)
    t = t.dropna(subset=["v"])
    t["w"] = t["relevance_score"].clip(lower=0.01)
    node = t.groupby("node_id").apply(lambda g: np.average(g["v"], weights=g["w"]) if len(g) >= 5 else np.nan,
                                       include_groups=False)
    m = tax.merge(node.rename("nv"), left_on="node_id", right_index=True).dropna(subset=["nv"])
    m["w"] = m["relevance_score"].clip(lower=0.01)
    return m.groupby("ticker").apply(lambda g: np.average(g["nv"], weights=g["w"]), include_groups=False)


rows = []
for d in ev:
    i = dates.get_loc(d)
    win = slice(i - 119, i + 1)
    r, m = ret.iloc[win], mret.iloc[win]
    ok = r.notna().sum() >= 100
    r = r.loc[:, ok]
    mm = m.values
    mc = mm - np.nanmean(mm)
    rc = r.values - np.nanmean(r.values, axis=0)
    beta = np.nansum(rc * mc[:, None], axis=0) / np.nansum(mc ** 2)
    resid = pd.DataFrame(r.values - beta[None, :] * mm[:, None], index=r.index, columns=r.columns)
    f = pd.DataFrame(index=r.columns)
    f["beta"] = beta
    f["mom_60"] = close.iloc[i - 5][r.columns] / close.iloc[i - 60][r.columns] - 1
    f["rev_5"] = close.iloc[i][r.columns] / close.iloc[i - 5][r.columns] - 1
    f["idio_mom_60"] = resid.iloc[-60:-5].sum()
    f["idio_mom_20"] = resid.iloc[-20:].sum()
    f["idio_vol_60"] = resid.iloc[-60:].std()
    f["ret_20"] = close.iloc[i][r.columns] / close.iloc[i - 20][r.columns] - 1
    # 業種（東証33業種）の勢い
    f["industry"] = ind.reindex(f.index)
    f["ind_mom_60"] = f.groupby("industry")["mom_60"].transform("mean")
    # マッピング（技術マップのタグ）に応じた値動き
    f["theme_mom_60"] = node_avg(f["mom_60"])
    f["theme_mom_20"] = node_avg(f["ret_20"])
    f["vs_theme_20"] = f["ret_20"] - f["theme_mom_20"]
    f["vs_theme_idio"] = f["idio_mom_20"] - node_avg(f["idio_mom_20"])
    # マッピングに応じた財務の動き（その日までに公表された最新の通期）
    lf = fin[fin["avail"] <= d].groupby("ticker").tail(1).set_index("ticker")
    f["own_sales_g"] = lf["sales_g"].reindex(f.index)
    f["theme_sales_g"] = node_avg(lf["sales_g"].clip(-0.5, 1.0))
    f["theme_op_g"] = node_avg(lf["op_g"].clip(-1.0, 2.0))
    f["sales_g_vs_theme"] = f["own_sales_g"].clip(-0.5, 1.0) - f["theme_sales_g"]
    # その後 H 日の超過
    fwd = close.iloc[i + H][r.columns] / close.iloc[i][r.columns] - 1
    f["fwd_ex"] = fwd - (mkt.iloc[i + H] / mkt.iloc[i] - 1)
    f["liq"] = turn.iloc[i][r.columns]
    f["date"] = d
    rows.append(f.reset_index(names="ticker"))

df = pd.concat(rows, ignore_index=True)
# 流動性の下限（20日平均売買代金 5千万円）。値の極端な銘柄の影響を抑えるため順位相関で測る
df = df[df["liq"] >= 5e7]
feats = ["mom_60", "rev_5", "idio_mom_60", "idio_mom_20", "idio_vol_60", "beta", "ind_mom_60",
         "theme_mom_60", "theme_mom_20", "vs_theme_20", "vs_theme_idio", "own_sales_g", "theme_sales_g", "theme_op_g",
         "sales_g_vs_theme"]
out = []
for f in feats:
    ics, spreads = [], []
    for d, g in df.groupby("date"):
        g = g[[f, "fwd_ex"]].dropna()
        if len(g) < 200:
            continue
        ics.append(g[f].rank().corr(g["fwd_ex"].rank()))
        q = pd.qcut(g[f].rank(method="first"), 5, labels=False)
        spreads.append(g["fwd_ex"][q == 4].mean() - g["fwd_ex"][q == 0].mean())
    ics = np.array(ics)
    n = len(ics)
    out.append({"指標": f, "評価日": n, "平均IC": ics.mean() if n else np.nan,
                "IC>0の割合": (ics > 0).mean() if n else np.nan,
                "t値": ics.mean() / ics.std(ddof=1) * np.sqrt(n) if n > 2 else np.nan,
                "上位5分の1−下位(20日)": np.mean(spreads) if spreads else np.nan,
                "銘柄数/日": int(df.groupby("date")[f].count().mean())})
res = pd.DataFrame(out)
pd.set_option("display.width", 200)
print(res.round(4).to_string(index=False))
# 既存のモメンタムとの重なり
c = df[["mom_60", "idio_mom_60", "theme_mom_60", "vs_theme_20", "theme_sales_g", "ind_mom_60"]].corr(method="spearman")
print("\n順位相関（指標どうし）\n", c.round(2).to_string())
df.to_parquet(f"{S}/features.parquet")
