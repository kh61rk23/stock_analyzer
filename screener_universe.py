# ==============================================================
# screener_universe.py
# 日本株スクリーナーの一次採点（全上場を1クエリで採点し、上位だけ二次評価へ回す）
# ==============================================================
# 【なぜ】
#   二次評価（stock_screener._screen_sector）は銘柄ごとに yfinance を引くため、約3,800社には回せない。
#   かといって売上上位で絞ると小型の有望株が構造的に落ちる。そこで BQ の全上場を先に採点し、上位だけを回す。
# 【原則】
#   新しい評価軸を独立に作らない。既存の評価部品を全上場に当てる。
#     テクニカル … technical_scorer.calc_technical_score に BQ の日足系列を渡す
#     不動産     … stock_real_estate.load() の bonus（score_calculator.calc_re_adj と同じ値）
#     質         … stock_screener.accrual_adjustment（Sloan tier・業種平均比）＋ EDINET品質キャッシュ
#     加点類     … 各モジュールの get_*_bonus。キャッシュにある銘柄だけ（無い銘柄は不利にせず要素から外す）
#     テーマ     … taxonomy_hotness の熱度 × company_taxonomy_map の関与度
#   値の無い観点は中立50にして coverage に記録する。規模（時価総額）はどの観点にも入れない。
# 【入力】BQ 1クエリ（価格系列・bi.m_screener・taxonomy を結合）。ループ内で BQ を引かない。
#   価格は trade_date < 実行日（前営業日の終値まで）。bi.m_screener は前回 bi_export の実体化分（前日分）。
# 【出力】run_universe() の dict、BQ analytics.screener_universe_daily（日付パーティション・同日は置き換え）。
# ==============================================================
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

ASPECTS = ("move", "momentum", "technical", "asset", "quality", "growth", "value", "flow", "theme", "sector", "idio")

DATASET = "analytics"
TABLE = "screener_universe_daily"
LOCATION = "asia-northeast1"

# 一次採点の全順位化に使う bi.m_screener の列（向き: True=大きいほど良い）。観点ごと。
RANK_COLUMNS = {
    "move":     [("ret_1d", True), ("vol_ratio", True), ("dist_high20", True)],
    "momentum": [("r1m", True), ("r3m", True), ("mom_12_1", True), ("near_high52", True)],
    "technical": [],
    "asset":    [("asset_value_ratio", True), ("net_cash_to_mcap", True), ("adj_pbr", False)],
    "quality":  [("roic", True), ("ocf_margin", True), ("fcf_margin", True), ("f_score", True),
                 ("interest_coverage", True)],
    "growth":   [("sales_growth_1y", True), ("op_income_growth_1y", True), ("op_income_cagr_3y", True),
                 ("consec_up_years", True)],
    "value":    [("earnings_yield", True), ("fcf_yield", True), ("total_payout_yield", True), ("ev_ebitda", False)],
    "flow":     [],
    "theme":    [("theme_value", True), ("theme_sales_g", True)],
    "sector":   [("sector_score", True)],
    # 銘柄独自：TOPIX（1306.T）で説明できない残りの値動き。荒さが小さいほど・テーマ（同じタグの会社群）に出遅れたほど良い
    "idio":     [("idio_vol_60", False), ("vs_theme_idio", False)],
}
MARKET_TICKER = "1306.T"

_SQL = """
WITH px_raw AS (
  SELECT ticker, trade_date, close, volume,
         LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_close
  FROM `{project}.analytics.price_daily_latest`
  WHERE trade_date >= DATE_SUB(@asof, INTERVAL @price_days DAY) AND trade_date < @asof
    AND ticker LIKE '%.T' AND close > 0 AND close < 10000000
),
px_brk AS (
  SELECT *, MAX(IF(prev_close > 0 AND (close / prev_close < 0.6 OR close / prev_close > 1.7), trade_date, NULL))
              OVER (PARTITION BY ticker) AS break_date
  FROM px_raw
),
px_rows AS (
  SELECT ticker, trade_date, close, volume, break_date,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn,
         MAX(trade_date) OVER (PARTITION BY ticker) AS last_date,
         MAX(trade_date) OVER () AS max_d
  FROM px_brk
  WHERE break_date IS NULL OR trade_date >= break_date
),
px AS (
  SELECT ticker, ANY_VALUE(last_date) AS last_date, ANY_VALUE(max_d) AS max_d, COUNT(*) AS price_days,
         MAX(break_date) AS break_date,
         ARRAY_AGG(IF(rn <= 260, close, NULL) IGNORE NULLS ORDER BY trade_date) AS closes,
         ARRAY_AGG(IF(rn <= 260, trade_date, NULL) IGNORE NULLS ORDER BY trade_date) AS dates,
         MAX(IF(rn = 1, volume, NULL)) AS vol_1d,
         AVG(IF(rn <= 20, volume, NULL)) AS vol_avg20,
         IF(COUNTIF(rn <= 20) >= 15, AVG(IF(rn <= 20, close * volume, NULL)), NULL) AS turnover_20d
  FROM px_rows
  GROUP BY ticker
),
tax AS (
  SELECT CONCAT(company_id, '.T') AS ticker,
         ARRAY_AGG(STRUCT(node_id, relevance_score AS relevance)) AS themes
  FROM (
    SELECT company_id, node_id, relevance_score
    FROM `{project}.taxonomy.company_taxonomy_map`
    WHERE REGEXP_CONTAINS(company_id, r'^[0-9A-Z]{{4}}$') AND relevance_score IS NOT NULL
      AND (valid_to IS NULL OR valid_to >= @asof)
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY company_id, node_id
      ORDER BY CASE WHEN verified_flag THEN 0 WHEN confidence_type = 'edinet' THEN 1 ELSE 2 END, updated_at DESC) = 1
  )
  GROUP BY ticker
)
SELECT s.edinet_code, s.ticker, s.name, s.industry, s.market_cap_b, s.current_price,
       s.sales_growth_1y, s.op_income_growth_1y, s.op_income_cagr_3y, s.consec_up_years,
       s.roic, s.ocf_margin, s.fcf_margin, s.f_score, s.interest_coverage, s.accrual_ratio,
       s.earnings_yield, s.fcf_yield, s.ev_ebitda, s.total_payout_yield,
       s.asset_value_ratio, s.net_cash_to_mcap, COALESCE(s.adj_pbr, s.pbr) AS adj_pbr,
       x.last_date, x.max_d, x.price_days, x.break_date, x.closes, x.dates, x.vol_1d, x.vol_avg20, x.turnover_20d,
       t.themes
FROM `{project}.bi.m_screener` AS s
LEFT JOIN px AS x ON x.ticker = s.ticker
LEFT JOIN tax AS t ON t.ticker = s.ticker
WHERE s.ticker IS NOT NULL
"""


# ── 純粋ロジック ──────────────────────────────────────────

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def pct_rank(values: dict, higher_better: bool = True) -> dict:
    """{key: 値 or None} → {key: 0〜100 のパーセンタイル順位}。値が無いキーは出力に含めない。

    同値は平均順位。値が1件だけなら中立の50。向きが逆（低いほど良い）なら順位を反転する。
    """
    items = [(k, _num(v)) for k, v in values.items()]
    items = [(k, v) for k, v in items if v is not None]
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 50.0}
    items.sort(key=lambda kv: kv[1])
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        pct = ((i + j) / 2.0) / (n - 1) * 100.0
        for k in range(i, j + 1):
            out[items[k][0]] = round(pct if higher_better else 100.0 - pct, 2)
        i = j + 1
    return out


def bonus_to_score(pts, cap: float) -> float | None:
    """既存の加点（pt）を0〜100へ写す。0ptが中立の50。±cap で飽和する。"""
    p = _num(pts)
    cap = abs(float(cap or 0))
    if p is None or cap <= 0:
        return None
    return round(50.0 + 50.0 * max(-1.0, min(1.0, p / cap)), 2)


def normalize_weights(weights: dict) -> dict:
    """観点の重みを合計1へ。負・非数・未知の観点は捨て、全部無効なら均等にする。"""
    w = {a: max(0.0, _num((weights or {}).get(a)) or 0.0) for a in ASPECTS}
    total = sum(w.values())
    if total <= 0:
        return {a: 1.0 / len(ASPECTS) for a in ASPECTS}
    return {a: v / total for a, v in w.items()}


def price_features(closes: list) -> dict:
    """日足終値の系列（古い順）から値動き・モメンタム系の指標を出す（足りない指標は None）。"""
    c = [x for x in (_num(v) for v in closes or []) if x is not None and x > 0]
    n = len(c)
    out = {k: None for k in ("ret_1d", "dist_high20", "r1m", "r3m", "mom_12_1", "near_high52")}
    if n < 2:
        return out
    last = c[-1]
    out["ret_1d"] = last / c[-2] - 1
    out["dist_high20"] = last / max(c[-20:]) - 1 if n >= 15 else None
    if n >= 22:
        out["r1m"] = last / c[-22] - 1
    if n >= 64:
        out["r3m"] = last / c[-64] - 1
    if n >= 253:
        out["mom_12_1"] = c[-22] / c[-253] - 1
    if n >= 120:
        out["near_high52"] = last / max(c[-252:])
    return out


def classify(row: dict, min_turnover: float, stale_days: int = 14) -> str | None:
    """売買できない銘柄の除外理由（対象なら None）。時価総額では外さない。"""
    if not row.get("closes") or row.get("last_date") is None:
        return "no_price"
    last, newest = row["last_date"], row.get("max_d")
    if newest is not None and (newest - last).days > stale_days:
        return "stale"
    t = _num(row.get("turnover_20d"))
    if t is None or t < min_turnover:
        return "low_turnover"
    return None


def theme_value(themes, hotness: dict, leading_only, leading_bonus: float = 15.0):
    """銘柄の max(関与度 × ノード熱度)。熱度は combined（0〜100）。先行のみ点灯のノードは熱度に加点する。

    熱度の無いノードしか持たない銘柄は None（値なし＝中立扱い）。
    """
    lead = set(leading_only or [])
    best = None
    for t in themes or []:
        t = dict(t)
        h = (hotness or {}).get(t.get("node_id"))
        rel = _num(t.get("relevance"))
        if not h or rel is None:
            continue
        heat = _num(h.get("combined"))
        if heat is None:
            continue
        if t.get("node_id") in lead:
            heat = min(100.0, heat + leading_bonus)
        v = rel * heat
        if best is None or v > best:
            best = v
    return best


def idio_features(series: dict, market: dict, themes: dict, sales_growth: dict, beta_days: int = 120,
                  vol_days: int = 60, mom_days: int = 20, min_days: int = 100, min_members: int = 5) -> dict:
    """銘柄独自の値動きと、マッピング（技術マップのタグ）に応じた値動き・財務の動き（純粋関数）。

    series: {ticker: (日付の列, 終値の列)}（古い順）、market: {日付: TOPIX ETF の終値}、
    themes: {ticker: [(node_id, 関与度)]}、sales_growth: {ticker: 売上の前年比}
    返り値 {ticker: {"beta", "idio_vol_60", "idio_mom_20", "vs_theme_idio", "theme_sales_g"}}（求まらない値は無い）。
      ・日次の騰落を市場の騰落に回帰（直近 beta_days 日）し、残り（独自の値動き）の直近 vol_days 日の標準偏差が
        idio_vol_60、直近 mom_days 日の合計が idio_mom_20。前の日に値の無い日（売買停止など）は使わない
      ・vs_theme_idio＝自分の idio_mom_20 − 同じタグの会社群の idio_mom_20 の平均（関与度で重み・会社が min_members 社以上のノードだけ）
      ・theme_sales_g＝同じタグの会社群の売上の前年比の平均（-50%〜+100% で切る）
    """
    mdates = sorted(market)
    mret = {d: market[d] / market[p] - 1 for p, d in zip(mdates, mdates[1:]) if market[p] > 0}
    prev_of = dict(zip(mdates[1:], mdates))
    out: dict = {}
    for t, (ds, cs) in series.items():
        px = {d: _num(c) for d, c in zip(ds or [], cs or []) if _num(c)}
        pts = [(mret[d], px[d] / px[prev_of[d]] - 1) for d in sorted(px)
               if d in mret and prev_of.get(d) in px][-beta_days:]
        if len(pts) < min_days:
            continue
        mx = sum(m for m, _ in pts) / len(pts)
        sx = sum(r for _, r in pts) / len(pts)
        var = sum((m - mx) ** 2 for m, _ in pts)
        beta = sum((m - mx) * (r - sx) for m, r in pts) / var if var > 0 else 0.0
        resid = [r - beta * m for m, r in pts]
        last = resid[-vol_days:]
        mu = sum(last) / len(last)
        out[t] = {"beta": round(beta, 4),
                  "idio_vol_60": math.sqrt(sum((x - mu) ** 2 for x in last) / (len(last) - 1)),
                  "idio_mom_20": sum(resid[-mom_days:])}

    def _node_avg(values: dict) -> dict:
        acc: dict = {}
        for t, nodes in themes.items():
            v = values.get(t)
            if v is None:
                continue
            for nid, rel in nodes:
                w = max(0.01, _num(rel) or 0.0)
                a = acc.setdefault(nid, [0.0, 0.0, 0])
                a[0] += w * v
                a[1] += w
                a[2] += 1
        node = {n: a[0] / a[1] for n, a in acc.items() if a[2] >= min_members and a[1] > 0}
        res = {}
        for t, nodes in themes.items():
            ws = [(max(0.01, _num(rel) or 0.0), node[n]) for n, rel in nodes if n in node]
            if ws:
                res[t] = sum(w * v for w, v in ws) / sum(w for w, _ in ws)
        return res

    theme_idio = _node_avg({t: f["idio_mom_20"] for t, f in out.items()})
    for t, v in theme_idio.items():
        if t in out:
            out[t]["vs_theme_idio"] = out[t]["idio_mom_20"] - v
    sg = {t: min(1.0, max(-0.5, v)) for t, v in ((t, _num(v)) for t, v in sales_growth.items()) if v is not None}
    for t, v in _node_avg(sg).items():
        out.setdefault(t, {})["theme_sales_g"] = v
    return out


def aspect_scores(rank_pcts: dict, score_elems: list) -> tuple:
    """1銘柄の観点スコア。rank_pcts: その観点の順位要素（0〜100）のリスト、score_elems: 既存部品の点（0〜100）。

    値のある要素の平均。1つも無ければ (50.0, False)＝中立で、値は無かったことを返す。
    """
    vals = [v for v in list(rank_pcts) + list(score_elems) if v is not None]
    if not vals:
        return 50.0, False
    return round(sum(vals) / len(vals), 2), True


def score_universe(records: list, weights: dict) -> list:
    """records（対象銘柄だけ）を採点して universe_rank 順に返す（純粋関数）。

    各 record: {"ticker", "cols": {指標名: 値}, "scores": {観点: [既存部品の点 0〜100]}, ...任意の付帯情報}
    出力は record に universe_score・universe_rank・aspects（10観点）・coverage を足したもの。
    """
    w = normalize_weights(weights)
    ranks: dict = {a: {} for a in ASPECTS}  # 観点 -> 指標 -> {ticker: pct}
    for a in ASPECTS:
        for col, hb in RANK_COLUMNS.get(a, []):
            ranks[a][col] = pct_rank({r["ticker"]: (r.get("cols") or {}).get(col) for r in records}, hb)
    out = []
    for r in records:
        t = r["ticker"]
        aspects, have = {}, 0
        for a in ASPECTS:
            rp = [ranks[a][col][t] for col, _ in RANK_COLUMNS.get(a, []) if t in ranks[a][col]]
            v, ok = aspect_scores(rp, (r.get("scores") or {}).get(a, []))
            aspects[a] = v
            have += 1 if ok else 0
        score = round(sum(w[a] * aspects[a] for a in ASPECTS), 2)
        out.append({**r, "universe_score": score, "aspects": aspects, "coverage": round(have / len(ASPECTS), 3)})
    out.sort(key=lambda x: (-x["universe_score"], x["ticker"]))
    for i, r in enumerate(out, 1):
        r["universe_rank"] = i
    return out


def distribution(records: list) -> dict:
    """観点別スコアの分布（p10/p50/p90/mean）。"""
    res = {}
    for a in ASPECTS:
        v = sorted(r["aspects"][a] for r in records)
        if not v:
            continue
        q = lambda p: v[min(len(v) - 1, int(p * (len(v) - 1) + 0.5))]
        res[a] = {"p10": q(0.1), "p50": q(0.5), "p90": q(0.9), "mean": round(sum(v) / len(v), 2)}
    return res


def group_by_sector(records: list, top_n: int) -> dict:
    """一次上位 top_n 社を業種ごとに束ねる（{sector_code: [ticker, ...]}。業種不明は "DYNAMIC"）。"""
    plan: dict = {}
    for r in records[:top_n]:
        plan.setdefault(r.get("sector_code") or "DYNAMIC", []).append(r["ticker"])
    return plan


def blend_final(universe_score, composite_score, blend: float) -> float:
    """final_score = blend×一次 + (1−blend)×二次。一次が無い銘柄は二次のまま。"""
    comp = _num(composite_score) or 0.0
    u = _num(universe_score)
    if u is None:
        return round(comp, 1)
    b = max(0.0, min(1.0, float(blend)))
    return round(b * u + (1 - b) * comp, 1)


# ── 入出力 ────────────────────────────────────────────────

def load_hotness(cfg) -> tuple:
    """node_hotness.json の履歴から、直近日のノード熱度と先行のみ点灯ノードを再計算する。

    taxonomy_hotness はスクリーナーより後に走るため、読むのは前日までに書かれた履歴の最新日。
    """
    import taxonomy_hotness as th
    path = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "taxonomy" / "node_hotness.json"
    if not path.exists():
        return {}, []
    history = json.loads(path.read_text(encoding="utf-8"))
    if not history:
        return {}, []
    latest = max(history)
    hotness = th.compute_node_hotness(history[latest], history, latest, cfg)
    return hotness, th.leading_only_nodes(hotness, cfg=cfg)


MARKET_SQL = """
SELECT trade_date, close FROM `{project}.analytics.price_daily_latest`
WHERE ticker = @t AND trade_date >= DATE_SUB(@asof, INTERVAL @price_days DAY) AND trade_date < @asof AND close > 0
"""


def fetch_market(client, cfg, asof: date) -> dict:
    """TOPIX の ETF（MARKET_TICKER）の {日付: 終値}。銘柄独自の値動きを測る基準。price_daily は ticker でまとめて
    保存されているので読む量は小さい。"""
    from google.cloud import bigquery
    jc = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("t", "STRING", MARKET_TICKER),
        bigquery.ScalarQueryParameter("asof", "DATE", asof),
        bigquery.ScalarQueryParameter("price_days", "INT64", int(getattr(cfg, "SCREEN_UNIVERSE_PRICE_DAYS", 400))),
    ])
    return {r["trade_date"]: float(r["close"]) for r in client.query(MARKET_SQL.format(project=client.project),
                                                                         job_config=jc).result()}


def fetch_rows(client, cfg, asof: date) -> list:
    """一次採点の入力（全上場）を BQ から1クエリで取る。"""
    from google.cloud import bigquery
    jc = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("asof", "DATE", asof),
        bigquery.ScalarQueryParameter("price_days", "INT64", int(getattr(cfg, "SCREEN_UNIVERSE_PRICE_DAYS", 400))),
    ])
    cap = getattr(cfg, "BQ_MAX_BYTES_BILLED", None)
    if cap:
        jc.maximum_bytes_billed = int(cap)
    rows = client.query(_SQL.format(project=client.project), job_config=jc).result()
    return [dict(r.items()) for r in rows]


def _sector_lookup(cfg) -> dict:
    """業種名→業種コード。「、」と「・」の表記ゆれを吸収する。"""
    names = {}
    for code, v in (getattr(cfg, "JAPAN_SECTORS", {}) or {}).items():
        names[str(v.get("name_ja", "")).replace("、", "・")] = str(code)
    return names


def build_records(rows: list, cfg, ctx: dict) -> tuple:
    """BQ の行 → 採点対象の records と除外件数。既存部品の点は ctx から引く。

    ctx: re_by_ticker（stock_real_estate.load）, accruals, sector_scores, hotness, leading_only,
         caches（{"edinet","surprise","dcf","comps","credit","insider","dividend","sr","vp"}）,
         accrual_fn（stock_screener.accrual_adjustment）, tech_fn / weekly_fn（technical_scorer）,
         bonus_getters（{名前: callable(ticker, cache)->pt}）
    """
    caps = getattr(cfg, "SCREEN_UNIVERSE_BONUS_CAPS", {}) or {}
    min_turn = float(getattr(cfg, "SCREEN_UNIVERSE_MIN_TURNOVER", 10_000_000.0))
    stale = int(getattr(cfg, "SCREEN_UNIVERSE_STALE_DAYS", 14))
    lead_bonus = float(getattr(cfg, "SCREEN_UNIVERSE_LEADING_BONUS", 15.0))
    names = _sector_lookup(cfg)
    code_name = {c: v.get("name_ja", c) for c, v in (getattr(cfg, "JAPAN_SECTORS", {}) or {}).items()}
    re_by = ctx.get("re_by_ticker") or {}
    caches = ctx.get("caches") or {}
    getters = ctx.get("bonus_getters") or {}
    accruals = ctx.get("accruals") or {}
    sector_scores = ctx.get("sector_scores") or {}
    hotness, leading = ctx.get("hotness") or {}, ctx.get("leading_only") or []

    # 業種ごとのアクルーアル平均（二次評価と同じく、業種内の銘柄の平均）
    by_sector: dict = {}
    sector_of: dict = {}
    for r in rows:
        t = r["ticker"]
        code = names.get(str(r.get("industry") or "").replace("、", "・")) or (re_by.get(t) or {}).get("sector_code")
        sector_of[t] = code
        acr = accruals.get(t, _num(r.get("accrual_ratio")))
        if acr is not None:
            by_sector.setdefault(code, []).append(acr)
    sector_mean = {c: sum(v) / len(v) for c, v in by_sector.items() if v}

    records, excluded = [], {}
    for r in rows:
        why = classify(r, min_turn, stale)
        if why:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        t = r["ticker"]
        code = sector_of.get(t)
        cols = {k: r.get(k) for cols_ in RANK_COLUMNS.values() for k, _ in cols_}
        pf = price_features(r["closes"])
        cols.update(pf)
        v20 = _num(r.get("vol_avg20"))
        cols["vol_ratio"] = (_num(r.get("vol_1d")) or 0) / v20 if v20 else None
        cols["theme_value"] = theme_value(r.get("themes"), hotness, leading, lead_bonus)
        cols["sector_score"] = sector_scores.get(code)
        sc: dict = {a: [] for a in ASPECTS}

        tech = ctx["tech_fn"](r["closes"], ctx["weekly_fn"](r["closes"]), [], [])
        sc["technical"].append(_num(tech.get("score")))
        for name, asp in (("sr", "technical"), ("vp", "technical"), ("edinet", "quality"), ("surprise", "growth"),
                          ("dcf", "value"), ("comps", "value"), ("insider", "flow"), ("dividend", "flow")):
            cache, fn = caches.get(name), getters.get(name)
            if cache and fn and _in_cache(name, t, cache):
                sc[asp].append(bonus_to_score(fn(t, cache), caps.get(name, 10.0)))
        cr, fn = caches.get("credit"), getters.get("credit")
        if cr and fn and t in cr:
            sc["flow"].append(bonus_to_score(fn(t, cr)[0], caps.get("credit", 5.0)))
        e = re_by.get(t)
        if e and _num(e.get("bonus")) is not None:
            sc["asset"].append(bonus_to_score(e["bonus"], caps.get("re", 3.0)))
        acr = accruals.get(t, _num(r.get("accrual_ratio")))
        if acr is not None:
            sc["quality"].append(bonus_to_score(ctx["accrual_fn"](acr, sector_mean.get(code))[1], caps.get("accrual", 20.0)))
        records.append({
            "ticker": t, "edinet_code": r.get("edinet_code"), "name": r.get("name"), "market": "JP",
            "sector_code": code, "sector_name": code_name.get(code, r.get("industry")),
            "market_cap_b": _num(r.get("market_cap_b")), "current_price": _num(r.get("current_price")),
            "turnover_20d": _num(r.get("turnover_20d")), "cols": cols,
            "scores": {a: [v for v in vs if v is not None] for a, vs in sc.items()},
        })
    # 銘柄独自の値動き・マッピングに応じた値動きと財務（全銘柄を見て決まるので、記録をそろえてから足す）
    by_t = {r["ticker"]: r for r in rows}
    feats = idio_features(
        {rec["ticker"]: (by_t[rec["ticker"]].get("dates"), by_t[rec["ticker"]].get("closes")) for rec in records},
        ctx.get("market") or {},
        {rec["ticker"]: [(dict(x).get("node_id"), dict(x).get("relevance")) for x in (by_t[rec["ticker"]].get("themes") or [])]
         for rec in records},
        {rec["ticker"]: rec["cols"].get("sales_growth_1y") for rec in records})
    for rec in records:
        f = feats.get(rec["ticker"]) or {}
        for k in ("beta", "idio_vol_60", "idio_mom_20", "vs_theme_idio", "theme_sales_g"):
            rec["cols"][k] = f.get(k)
    return records, excluded


def _in_cache(name: str, ticker: str, cache: dict) -> bool:
    """キャッシュにその銘柄の記録があるか（無い銘柄は 0pt にせず要素から外す）。"""
    if name in ("insider", "dividend"):
        return ticker in (cache.get("bonuses") or {})
    return ticker in cache


def summarize(scored: list, excluded: dict, top_n: int, stage2_n: int, total_rows: int) -> dict:
    """results["screener_universe"] に載せる要約。"""
    top = [{"ticker": r["ticker"], "name": r.get("name"), "sector_code": r.get("sector_code"),
            "universe_score": r["universe_score"], "universe_rank": r["universe_rank"],
            "market_cap_b": r.get("market_cap_b"), "coverage": r["coverage"], "aspects": r["aspects"]}
           for r in scored[:top_n]]
    small = sum(1 for r in scored[:top_n] if (r.get("market_cap_b") is not None and r["market_cap_b"] < 100))
    return {
        "date": date.today().isoformat(), "total_rows": total_rows, "universe_count": len(scored),
        "excluded": dict(excluded), "excluded_count": sum(excluded.values()), "stage2_n": stage2_n,
        "top": top, f"small_cap_under_100b_in_top{top_n}": small,
        "aspect_distribution": distribution(scored),
        "coverage_mean": round(sum(r["coverage"] for r in scored) / len(scored), 3) if scored else None,
    }


IDIO_COLS = ("beta", "idio_vol_60", "idio_mom_20", "vs_theme_idio", "theme_sales_g")   # 観点の材料も毎日残す（効きを測り直すため）


def output_rows(scored: list, score_date: str, stage2: set) -> list:
    """BQ 書き込み用の行（analytics.screener_universe_daily）。"""
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in scored:
        row = {"score_date": score_date, "ticker": r["ticker"], "edinet_code": r.get("edinet_code"),
               "company_name": r.get("name"), "sector_code": r.get("sector_code"),
               "universe_score": r["universe_score"], "universe_rank": r["universe_rank"]}
        row.update({f"s_{a}": r["aspects"][a] for a in ASPECTS})
        row.update({k: _num((r.get("cols") or {}).get(k)) for k in IDIO_COLS})
        row.update({"coverage": r["coverage"], "market_cap_b": r.get("market_cap_b"),
                    "stage2_selected": r["ticker"] in stage2, "created_at": now})
        rows.append(row)
    return rows


def _schema():
    from google.cloud.bigquery import SchemaField as F
    return ([F("score_date", "DATE", "REQUIRED"), F("ticker", "STRING", "REQUIRED"), F("edinet_code", "STRING"),
             F("company_name", "STRING"), F("sector_code", "STRING"), F("universe_score", "FLOAT64"),
             F("universe_rank", "INT64")]
            + [F(f"s_{a}", "FLOAT64") for a in ASPECTS]
            + [F(k, "FLOAT64") for k in IDIO_COLS]
            + [F("coverage", "FLOAT64"), F("market_cap_b", "FLOAT64"), F("stage2_selected", "BOOL"),
               F("created_at", "TIMESTAMP")])


def write_daily(client, rows: list, score_date: str, tmp_dir: Path) -> int:
    """その日の行で当日パーティションを置き換える（load job・無料。再実行しても二重にならない）。"""
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    if not rows:
        return 0
    ref = f"{client.project}.{DATASET}.{TABLE}"
    try:
        client.get_table(ref)
    except NotFound:
        t = bigquery.Table(ref, schema=_schema())
        t.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.DAY, field="score_date")
        client.create_table(t)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{TABLE}.ndjson"
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    try:
        with open(tmp, "rb") as fh:
            client.load_table_from_file(
                fh, f"{ref}${score_date.replace('-', '')}", location=LOCATION,
                job_config=bigquery.LoadJobConfig(
                    source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON, schema=_schema(),
                    write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                    # 列を足したときに既存の表へ追加する（観点・材料を足しても過去の区画は残る）
                    schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION])).result()
    finally:
        tmp.unlink(missing_ok=True)
    return len(rows)


def save_seed(cfg, tickers: list) -> None:
    """一次上位を翌日以降の個別キャッシュ育成に渡す（screener_candidate_builder が読む）。"""
    p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "screener_universe_top.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": date.today().isoformat(), "tickers": tickers}, ensure_ascii=False),
                 encoding="utf-8")


def run_universe(cfg, sector_results: dict, caches: dict, accruals: dict | None = None,
                 client=None, asof: date | None = None) -> dict:
    """一次採点を実行する。失敗は例外で返す（呼び出し側が従来経路へ縮退し、台帳に warn を残す）。

    Returns: {"records": 採点済み全件, "plan": {業種: [ticker]}, "stage2": [ticker...], "summary": dict}
    """
    import stock_real_estate
    import technical_scorer
    from stock_screener import accrual_adjustment
    asof = asof or datetime.now().date()
    if client is None:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
    rows = fetch_rows(client, cfg, asof)
    if not rows:
        raise RuntimeError("一次採点の入力が0行（bi.m_screener が空）")
    hotness, leading = {}, []
    try:
        hotness, leading = load_hotness(cfg)
    except Exception as e:
        print(f"    ⚠️  一次採点: テーマ熱度を読めず中立で続行: {str(e)[:80]}")
    import importlib
    getters = {}
    for name, mod, fn in (("sr", "support_resistance", "get_sr_bonus"), ("vp", "volume_profile", "get_vp_bonus"),
                          ("edinet", "screener_feedback_loop", "get_edinet_bonus"),
                          ("surprise", "earnings_surprise", "get_screener_bonus"),
                          ("dcf", "dcf_valuation", "get_dcf_bonus"), ("comps", "comps_analyzer", "get_comps_bonus"),
                          ("credit", "credit_data", "get_credit_bonus"), ("insider", "insider_monitor", "get_insider_bonus"),
                          ("dividend", "dividend_calendar", "get_dividend_bonus")):
        if caches.get(name):
            getters[name] = getattr(importlib.import_module(mod), fn)
    market = {}
    try:
        market = fetch_market(client, cfg, asof)
    except Exception as e:
        print(f"    ⚠️  一次採点: TOPIX ETF の株価を読めず、銘柄独自の観点は中立で続行: {str(e)[:80]}")
    ctx = {
        "market": market,
        "re_by_ticker": stock_real_estate.load(cfg),
        "accruals": accruals or {},
        "sector_scores": {str(c): _num(v.get("composite_score")) for c, v in (sector_results or {}).items()
                          if isinstance(v, dict)},
        "hotness": hotness, "leading_only": leading, "caches": caches, "bonus_getters": getters,
        "accrual_fn": accrual_adjustment, "tech_fn": technical_scorer.calc_technical_score,
        "weekly_fn": technical_scorer.weekly_closes,
    }
    records, excluded = build_records(rows, cfg, ctx)
    if not records:
        raise RuntimeError("一次採点の対象が0件（全件が除外）")
    scored = score_universe(records, getattr(cfg, "SCREEN_UNIVERSE_WEIGHTS", {}))
    top_n = int(getattr(cfg, "SCREEN_STAGE2_TOP_N", 200))
    plan = group_by_sector(scored, top_n)
    stage2 = [t for ts in plan.values() for t in ts]
    summary = summarize(scored, excluded, int(getattr(cfg, "SCREEN_UNIVERSE_TOP_REPORT_N", 50)), len(stage2), len(rows))
    day = asof.isoformat()
    try:
        summary["bq_rows_written"] = write_daily(
            client, output_rows(scored, day, set(stage2)), day,
            Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "tmp")
    except Exception as e:
        summary["bq_write_error"] = str(e)[:150]
        print(f"    ⚠️  screener_universe_daily の書き込み失敗（採点は続行）: {str(e)[:100]}")
    return {"records": scored, "plan": plan, "stage2": stage2, "summary": summary}
