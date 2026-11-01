"""
universe_factor_ic.py — 全上場の一次採点の観点別の効き（順位相関 IC）を毎週測る

【何を測るか】
  一次採点（screener_universe.py・us_screener_universe.py）の各観点の点（s_move・s_momentum・s_value …）と
  総合点（universe_score・rank_score）が、その後 5・20 取引日の指数超過リターンをどれだけ当てたか。
  評価日ごとに全銘柄の断面で順位相関（Spearman。同点は平均順位）と、上位5分の1−下位5分の1の超過の差を出す。
  指数は日本 1306.T・米国 SPY。買った値は評価日以降の最初の終値（pick_outcomes と同じ考え方）。
【なぜ要るか】
  一次採点の重み（SCREEN_UNIVERSE_WEIGHTS）は手で決めていて、観点ごとに効いているかを継続して測る仕組みが無かった。
  総合点しか見ないと、効いている観点と逆向きの観点が打ち消し合っていても気付けない。
【出力】
  BQ analytics.universe_factor_ic（1行＝市場 × 評価日 × 先の日数 × 観点）。毎回、直近 UFI_LOOKBACK_DAYS 日を
  計算し直して置き換える（load job・WRITE_TRUNCATE。表は小さい）。
  results["universe_factor_ic"]（summary と lines）→ 週次の日次メールに「📐 一次採点の観点別の効き」の節。
【クエリ】市場ごとに1本（日本で約0.05GB）。週1回だけ動かす（WEEKLY_MODULES）。
【読み方の注意】
  評価日は毎営業日で、先の期間（5・20日）が隣の評価日と重なる。t 値は重なりを考えて
  実効の日数＝評価日の数÷先の日数 で割り引いた値を出す。実効の日数が UFI_MIN_EFFECTIVE_DAYS 未満なら判定しない。
"""
from __future__ import annotations

import io
import json
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DATASET = "analytics"
TABLE = "universe_factor_ic"
LOCATION = "asia-northeast1"
HORIZONS = (5, 20)
FACTORS = ("universe_score", "rank_score", "s_move", "s_momentum", "s_technical", "s_asset", "s_quality",
           "s_growth", "s_value", "s_flow", "s_theme", "s_sector", "s_idio")
FACTOR_LABEL = {
    "universe_score": "総合点", "rank_score": "順位用の点（5回平均）", "s_move": "値動き", "s_momentum": "モメンタム",
    "s_technical": "テクニカル", "s_asset": "資産", "s_quality": "質", "s_growth": "成長", "s_value": "割安",
    "s_flow": "需給", "s_theme": "テーマ", "s_sector": "業種", "s_idio": "銘柄独自",
}
# (市場, 一次採点の表, 指数)
MARKETS = (("JP", "screener_universe_daily", "1306.T"), ("US", "screener_universe_us_daily", "SPY"))
SCHEMA = [("market", "STRING"), ("score_date", "DATE"), ("horizon", "INT64"), ("factor", "STRING"),
          ("n", "INT64"), ("ic", "FLOAT64"), ("q5_q1", "FLOAT64"), ("n_distinct", "INT64"),
          ("computed_at", "TIMESTAMP")]


def build_sql(p: str, table: str, bench: str, horizons=HORIZONS) -> str:
    """評価日 @since 以降の (評価日, 先の日数, 観点) ごとの IC を返す SELECT。

    同点の順位は平均順位（昇順の RANK と降順の RANK から出す。FLOAT の列では PARTITION BY 値 が使えないため）。
    5分位は値→ティッカーのハッシュの順で切る。先のリターンで同点を割ると、上位5分の1に勝った銘柄が寄って差が大きく出る。
    """
    hj = "\n".join(f"  LEFT JOIN px s{h} ON s{h}.ticker = u.ticker AND s{h}.i = se.ei + {h}\n"
                   f"  LEFT JOIN px b{h} ON b{h}.ticker = '{bench}' AND b{h}.i = be.bi + {h}" for h in horizons)
    hc = ",\n".join(f"    SAFE_DIVIDE(s{h}.close, se.c0) - SAFE_DIVIDE(b{h}.close, be.b0) AS ex_{h}d" for h in horizons)
    hu = ", ".join(f"STRUCT({h} AS h, ex_{h}d AS ex)" for h in horizons)
    cols = ", ".join(FACTORS)
    return f"""
WITH u AS (
  SELECT score_date, ticker, {cols}
  FROM `{p}.{DATASET}.{table}` WHERE score_date >= @since
),
px AS (
  SELECT ticker, trade_date, close, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS i
  FROM `{p}.analytics.price_daily_latest`
  WHERE trade_date >= DATE_SUB(@since, INTERVAL 10 DAY) AND close > 0
    AND ticker IN (SELECT DISTINCT ticker FROM u UNION DISTINCT SELECT '{bench}')
),
se AS (
  SELECT u.score_date, u.ticker, s.i AS ei, s.close AS c0
  FROM u JOIN px s ON s.ticker = u.ticker
   AND s.trade_date BETWEEN u.score_date AND DATE_ADD(u.score_date, INTERVAL 10 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY u.score_date, u.ticker ORDER BY s.trade_date) = 1
),
be AS (
  SELECT d.score_date, b.i AS bi, b.close AS b0
  FROM (SELECT DISTINCT score_date FROM u) d
  JOIN px b ON b.ticker = '{bench}'
   AND b.trade_date BETWEEN d.score_date AND DATE_ADD(d.score_date, INTERVAL 10 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY d.score_date ORDER BY b.trade_date) = 1
),
fwd AS (
  SELECT u.*,
{hc}
  FROM u JOIN se USING (score_date, ticker) JOIN be USING (score_date)
{hj}
),
lng AS (
  SELECT score_date, ticker, factor, val, x.h, x.ex
  FROM fwd UNPIVOT (val FOR factor IN ({cols}))
  CROSS JOIN UNNEST([{hu}]) x
  WHERE x.ex IS NOT NULL
),
ranked AS (
  SELECT score_date, factor, h, val, ex,
         (RANK() OVER (PARTITION BY score_date, factor, h ORDER BY val)
          + COUNT(*) OVER (PARTITION BY score_date, factor, h) + 1
          - RANK() OVER (PARTITION BY score_date, factor, h ORDER BY val DESC)) / 2 AS rv,
         RANK() OVER (PARTITION BY score_date, factor, h ORDER BY ex) AS re,
         NTILE(5) OVER (PARTITION BY score_date, factor, h ORDER BY val, FARM_FINGERPRINT(ticker)) AS q
  FROM lng
)
SELECT score_date, h AS horizon, factor, COUNT(*) AS n, CORR(rv, re) AS ic,
       AVG(IF(q = 5, ex, NULL)) - AVG(IF(q = 1, ex, NULL)) AS q5_q1,
       COUNT(DISTINCT val) AS n_distinct
FROM ranked GROUP BY score_date, horizon, factor
HAVING COUNT(*) >= 30
ORDER BY score_date, horizon, factor
"""


# ── 集計（純関数） ──────────────────────────────────────────

def summarize(rows: list[dict], min_eff_days: float = 4.0, ic_bar: float = 0.02, t_bar: float = 2.0) -> list[dict]:
    """(市場, 先の日数, 観点) ごとに 平均IC・重なりを割り引いた t 値・IC>0 の割合・5分位の差の平均・判定 を返す。"""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if r.get("ic") is None:
            continue
        groups.setdefault((r["market"], int(r["horizon"]), r["factor"]), []).append(r)
    out = []
    for (mkt, h, f), lst in groups.items():
        ics = [float(x["ic"]) for x in lst]
        k = len(ics)
        mean = sum(ics) / k
        sd = math.sqrt(sum((v - mean) ** 2 for v in ics) / (k - 1)) if k > 1 else 0.0
        k_eff = k / h
        t = mean / (sd / math.sqrt(k_eff)) if sd > 0 and k_eff > 0 else None
        spreads = [float(x["q5_q1"]) for x in lst if x.get("q5_q1") is not None]
        if k_eff < min_eff_days or t is None:
            verdict = "日数不足"
        elif mean >= ic_bar and t >= t_bar:
            verdict = "効いている"
        elif mean <= -ic_bar and t <= -t_bar:
            verdict = "逆向き"
        else:
            verdict = "はっきりしない"
        out.append({"market": mkt, "horizon": h, "factor": f, "days": k, "eff_days": round(k_eff, 1),
                    "mean_ic": round(mean, 4), "t": None if t is None else round(t, 2),
                    "hit": round(sum(v > 0 for v in ics) / k, 2),
                    "q5_q1": round(sum(spreads) / len(spreads), 4) if spreads else None,
                    "n": int(lst[-1].get("n") or 0), "verdict": verdict,
                    "first": str(min(x["score_date"] for x in lst)), "last": str(max(x["score_date"] for x in lst))})
    out.sort(key=lambda s: (s["market"], s["horizon"], -s["mean_ic"]))
    return out


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:+.2f}%"


def report_lines(summary: list[dict]) -> list[str]:
    """メールの節の本文。市場 × 先の日数 ごとに観点を平均IC の順に並べる。"""
    if not summary:
        return []
    lines = ["一次採点の各観点が、その後の指数超過リターンをどれだけ当てたか（全銘柄の順位相関 IC の平均）。",
             "IC は +0.02〜0.05 でも実用上は効いている水準。t は評価日の重なりを割り引いた値。"
             "判定は 平均IC が ±0.02 以上かつ |t|≥2 のとき。",
             ""]
    keys = sorted({(s["market"], s["horizon"]) for s in summary}, key=lambda x: (x[0] != "JP", x[1]))
    for mkt, h in keys:
        part = [s for s in summary if s["market"] == mkt and s["horizon"] == h]
        if not part:
            continue
        flag = {"JP": "🇯🇵 日本", "US": "🇺🇸 米国"}.get(mkt, mkt)
        lines.append(f"■ {flag}・{h}取引日後（評価日 {part[0]['first']}〜{part[0]['last']}・{part[0]['days']}日・"
                     f"約{part[0]['n']}社）")
        for s in part:
            mark = {"効いている": "✅", "逆向き": "🔻", "はっきりしない": "・", "日数不足": "…"}[s["verdict"]]
            t = "—" if s["t"] is None else f"{s['t']:+.1f}"
            lines.append(f"  {mark} {FACTOR_LABEL.get(s['factor'], s['factor'])}: IC {s['mean_ic']:+.3f}（t {t}・"
                         f"当たり {int(s['hit'] * 100)}%）上位−下位 {_pct(s['q5_q1'])}・{s['verdict']}")
        lines.append("")
    rev = [s for s in summary if s["verdict"] == "逆向き"]
    if rev:
        lines.append("🔻 逆向きの観点は、重み（SCREEN_UNIVERSE_WEIGHTS）を下げるか向きを見直す候補。"
                     "相場の局面で入れ替わることがあるので、数週続くかを見てから変える。")
    return lines


# ── BigQuery ───────────────────────────────────────────────

def _client():
    from edinet_bq_client import EDINETBQClient
    return EDINETBQClient().client


def fetch(client, since: date) -> tuple[list[dict], float, list[str]]:
    """市場ごとに1本ずつ IC を計算して返す。一次採点の表がまだ無い市場は飛ばす。"""
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    rows: list[dict] = []
    gb = 0.0
    skipped: list[str] = []
    for mkt, table, bench in MARKETS:
        try:
            client.get_table(f"{client.project}.{DATASET}.{table}")
        except NotFound:
            skipped.append(f"{mkt}: {table} が無い")
            continue
        jc = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("since", "DATE", since.isoformat())])
        job = client.query(build_sql(client.project, table, bench), job_config=jc, location=LOCATION)
        for r in job.result():
            d = dict(r.items())
            d["market"] = mkt
            rows.append(d)
        gb += (job.total_bytes_billed or 0) / 1024 ** 3
    return rows, round(gb, 3), skipped


def write(client, rows: list[dict]) -> None:
    """直近の計算結果で表を置き換える（load job なのでクエリの量に数えない）。"""
    from google.cloud import bigquery
    now = datetime.utcnow().isoformat()
    buf = io.BytesIO("\n".join(json.dumps({**{k: r.get(k) for k, _ in SCHEMA if k != "computed_at"},
                                           "score_date": str(r["score_date"]), "computed_at": now},
                                          ensure_ascii=False) for r in rows).encode("utf-8"))
    cfg = bigquery.LoadJobConfig(schema=[bigquery.SchemaField(n, t) for n, t in SCHEMA],
                                 write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                                 source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON)
    client.load_table_from_file(buf, f"{client.project}.{DATASET}.{TABLE}", location=LOCATION,
                                job_config=cfg).result()


def run(cfg, today: date | None = None, client=None) -> dict:
    """IC を計算し直して BQ に置き、メール用の要約を返す。失敗は例外にせず {"error": ...} で返す。"""
    if not getattr(cfg, "ENABLE_UNIVERSE_FACTOR_IC", True):
        return {"skipped": True, "reason": "ENABLE_UNIVERSE_FACTOR_IC=False"}
    today = today or datetime.now(ZoneInfo("Asia/Tokyo")).date()
    since = today - timedelta(days=int(getattr(cfg, "UFI_LOOKBACK_DAYS", 200)))
    try:
        client = client or _client()
        rows, gb, skipped = fetch(client, since)
        if not rows:
            return {"skipped": True, "reason": "先のリターンが付いた評価日がまだ無い", "markets_skipped": skipped}
        write(client, rows)
        summary = summarize(rows, min_eff_days=float(getattr(cfg, "UFI_MIN_EFFECTIVE_DAYS", 4.0)))
        res = {"rows": len(rows), "gb_billed": gb, "markets_skipped": skipped, "summary": summary,
               "lines": report_lines(summary),
               "effective": [f"{s['market']}{s['horizon']}d:{s['factor']}" for s in summary if s["verdict"] == "効いている"],
               "reversed": [f"{s['market']}{s['horizon']}d:{s['factor']}" for s in summary if s["verdict"] == "逆向き"]}
        print(f"  📐 一次採点の観点別の効き: {len(rows)}行 / {gb}GB / 逆向き {res['reversed'] or 'なし'}")
        return res
    except Exception as e:
        print(f"  ⚠️ 一次採点の観点別の効きを測れない（{type(e).__name__}: {str(e)[:120]}）")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def build_alert(results: dict) -> dict | None:
    """日次メールの節（その回に計算した要約があるときだけ）。"""
    res = (results or {}).get("universe_factor_ic") or {}
    lines = res.get("lines") if isinstance(res, dict) else None
    if not lines:
        return None
    return {"trigger": "universe_factor_ic", "title": "📐 一次採点の観点別の効き（週次）",
            "body": "\n".join(lines), "severity": "info"}


if __name__ == "__main__":
    import argparse
    import config as _cfg
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--print", action="store_true", help="BQ に書かず要約を表示するだけ")
    a = ap.parse_args()
    if a.print:
        c = _client()
        rs, g, sk = fetch(c, date.today() - timedelta(days=int(getattr(_cfg, "UFI_LOOKBACK_DAYS", 200))))
        print("\n".join(report_lines(summarize(rs))), f"\n({len(rs)}行 / {g}GB / {sk})")
    else:
        r = run(_cfg)
        print("\n".join(r.get("lines") or [json.dumps({k: v for k, v in r.items() if k != "summary"}, ensure_ascii=False)]))
