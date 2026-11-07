"""
cot_commodity_ic.py — 商品の建玉の偏り（COT）の業種加点が、翌週の業種の成績を当てたかを毎週測る

【何を測るか】
  cot_signal.sector_adjustments が業種へ足す点（cot_commodity_adjustment）と、その業種（日本33業種）の
  CFTC 公表後の 5・20 取引日の成績（業種の全銘柄の単純平均リターン）の順位相関 IC と、点が付いた業種の的中率。
  CFTC の建玉は火曜時点で金曜に公表される。基準の終値は「火曜＋4日（土曜）以降の最初の終値」、つまり公表の翌営業日。
  過去の週の点は、BQ の建玉（analytics.cot_positions）をその週までに切って cot_signal で作り直す
  （点は建玉だけで決まるので、履歴ファイルを持たずに過去へさかのぼれる）。
【なぜ要るか】
  商品の加点は入れたばかりで効きの実績が無い。重みは据え置き、逆向きなら自動で止めずにメールで知らせる。
【出力】
  BQ analytics.cot_commodity_ic（1行＝報告日 × 先の日数。毎回 WRITE_TRUNCATE の load job・小さい）。
  results["cot_commodity_ic"]（summary と lines）→ メールの節「🛢 商品の加点の効き」。逆向きのときは severity=warning。
【クエリ】建玉1本（cot_signal.build_query・上限 COT_SIGNAL_MAX_BYTES）＋業種別リターン1本（日本の全上場の日足）。週1回。
【読み方の注意】
  加点が付く業種は商品の極端が出た週だけ（多くの週は0）。付いた業種が COT_IC_MIN_ADJ_SECTORS 未満の週は IC を出さない。
  20日先は隣の週と重なるので、t 値は 実効の週数＝週数÷(先の日数÷5) で割り引く。実効の週数が COT_IC_MIN_WEEKS 未満なら判定しない。
"""
from __future__ import annotations

import io
import json
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DATASET = "analytics"
TABLE = "cot_commodity_ic"
LOCATION = "asia-northeast1"
HORIZONS = (5, 20)
RELEASE_LAG_DAYS = 4      # 報告日（火）から基準の終値までの日数（金曜公表の翌営業日＝土曜以降の最初の終値）
MIN_SECTOR_MEMBERS = 5    # 業種の単純平均に使う最小の銘柄数
MAX_ABS_RETURN = 2.0      # 分割などの崩れを除く（リターンの絶対値の上限）
SCHEMA = [("report_date", "DATE"), ("horizon", "INT64"), ("n_sectors", "INT64"), ("n_adj", "INT64"),
          ("ic", "FLOAT64"), ("hit", "FLOAT64"), ("computed_at", "TIMESTAMP")]


def _param(cfg, name: str, default):
    return getattr(cfg, name, default) if cfg is not None else default


# ── 純関数 ───────────────────────────────────────────────

def _avg_ranks(xs: list) -> list:
    """同値は平均順位（1始まり）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return out


def spearman(xs: list, ys: list) -> float | None:
    """順位相関。片方が全部同値（分散0）・2点未満は None。"""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy)


def weekly_deltas(rows: list, cfg, weeks: int, report_dates: list | None = None) -> dict:
    """{報告日(date): {業種コード: 加点}}（日本33業種）。各報告日までの建玉だけで cot_signal の点を作り直す。

    加点が1つも付かなかった週は空 dict で残す（「極端が無かった週」と「測っていない週」を区別するため）。
    """
    from cot_signal import build_signals, sector_adjustments
    dates = sorted({(r["report_date"] if isinstance(r["report_date"], date)
                     else date.fromisoformat(str(r["report_date"])[:10])) for r in rows or []})
    if report_dates is not None:
        dates = [d for d in dates if d in set(report_dates)]
    dates = dates[-int(weeks):]
    norm = [{**r, "report_date": (r["report_date"] if isinstance(r["report_date"], date)
                                  else date.fromisoformat(str(r["report_date"])[:10]))} for r in rows or []]
    out = {}
    for d in dates:
        part = [r for r in norm if r["report_date"] <= d]
        sig = build_signals(part, cfg, today=d + timedelta(days=3))
        adj = sector_adjustments(sig, cfg, today=d + timedelta(days=3))
        out[d] = {c: float(v["delta"]) for c, v in adj["jp"].items() if v.get("delta")}
    return out


def weekly_ic(deltas: dict, returns: dict, horizon: int, min_adj: int = 3, min_sectors: int = 15) -> list:
    """報告日ごとの IC・的中率。

    deltas: {報告日: {業種: 加点}}、returns: {(報告日, 業種): {horizon: リターン}}。
    加点の無い業種は 0 として全業種の断面で順位相関を取る。加点が付いた業種が min_adj 未満・
    成績のある業種が min_sectors 未満の週は出さない。
    hit は加点が付いた業種のうち「加点の符号 = 業種のリターン − 断面の中央値 の符号」の割合。
    """
    out = []
    for d in sorted(deltas):
        rets = {c: v.get(horizon) for (dd, c), v in returns.items() if dd == d and v.get(horizon) is not None}
        if len(rets) < min_sectors:
            continue
        adj = {c: x for c, x in deltas[d].items() if c in rets and x}
        if len(adj) < min_adj:
            continue
        codes = sorted(rets)
        ic = spearman([deltas[d].get(c, 0.0) for c in codes], [rets[c] for c in codes])
        if ic is None:
            continue
        srt = sorted(rets.values())
        med = (srt[len(srt) // 2] + srt[(len(srt) - 1) // 2]) / 2.0
        hits = [1.0 if (x > 0) == (rets[c] - med > 0) else 0.0 for c, x in adj.items() if rets[c] != med]
        out.append({"report_date": d, "horizon": horizon, "n_sectors": len(rets), "n_adj": len(adj),
                    "ic": round(ic, 4), "hit": round(sum(hits) / len(hits), 3) if hits else None})
    return out


def summarize(rows: list, min_weeks: float = 8.0, ic_bar: float = 0.05, t_bar: float = 2.0) -> list:
    """先の日数ごとに 平均IC・重なりを割り引いた t・当たりの割合・的中率・判定 を返す。"""
    out = []
    for h in sorted({int(r["horizon"]) for r in rows if r.get("ic") is not None}):
        ics = [float(r["ic"]) for r in rows if int(r["horizon"]) == h and r.get("ic") is not None]
        hits = [float(r["hit"]) for r in rows if int(r["horizon"]) == h and r.get("hit") is not None]
        k = len(ics)
        mean = sum(ics) / k
        sd = math.sqrt(sum((v - mean) ** 2 for v in ics) / (k - 1)) if k > 1 else 0.0
        k_eff = k / (h / 5.0)
        t = mean / (sd / math.sqrt(k_eff)) if sd > 0 and k_eff > 0 else None
        if k_eff < min_weeks or t is None:
            verdict = "週数不足"
        elif mean >= ic_bar and t >= t_bar:
            verdict = "効いている"
        elif mean <= -ic_bar and t <= -t_bar:
            verdict = "逆向き"
        else:
            verdict = "はっきりしない"
        dates = [str(r["report_date"]) for r in rows if int(r["horizon"]) == h and r.get("ic") is not None]
        out.append({"horizon": h, "weeks": k, "eff_weeks": round(k_eff, 1), "mean_ic": round(mean, 4),
                    "t": None if t is None else round(t, 2), "pos_weeks": round(sum(v > 0 for v in ics) / k, 2),
                    "hit": round(sum(hits) / len(hits), 3) if hits else None, "verdict": verdict,
                    "first": min(dates), "last": max(dates)})
    return out


def report_lines(summary: list) -> list:
    if not summary:
        return []
    lines = ["商品の建玉の偏りで業種に足した点（cot_commodity_adjustment）が、公表後の業種の成績を当てたか。",
             "IC は日本33業種の断面の順位相関（加点が付いた業種が3つ以上の週だけ）。判定は 平均IC が ±0.05 以上かつ |t|≥2。", ""]
    for s in summary:
        mark = {"効いている": "✅", "逆向き": "🔻", "はっきりしない": "・", "週数不足": "…"}[s["verdict"]]
        t = "—" if s["t"] is None else f"{s['t']:+.1f}"
        hit = "—" if s["hit"] is None else f"{int(round(s['hit'] * 100))}%"
        lines.append(f"  {mark} {s['horizon']}取引日後: IC {s['mean_ic']:+.3f}（t {t}・IC>0の週 {int(s['pos_weeks'] * 100)}%・"
                     f"的中 {hit}・{s['weeks']}週 {s['first']}〜{s['last']}）{s['verdict']}")
    if any(s["verdict"] == "逆向き" for s in summary):
        lines += ["", "🔻 商品の加点は逆向きでした。重みは自動では変えていません。COT_COMMODITY_PT を下げる・0 にして止める・"
                      "影響先（COT_COMMODITY_MARKETS の jp）の向きを見直すかを判断してください。"]
    return lines


# ── BigQuery ───────────────────────────────────────────────

_RET_SQL = """
WITH sec AS (
  SELECT ticker, sector_code FROM `{p}.analytics.screener_universe_daily`
  WHERE score_date = (SELECT MAX(score_date) FROM `{p}.analytics.screener_universe_daily` WHERE score_date >= @recent)
    AND sector_code IS NOT NULL
),
px AS (
  SELECT ticker, trade_date, close, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS i
  FROM `{p}.analytics.price_daily_latest`
  WHERE trade_date >= @since AND close > 0 AND ticker IN (SELECT ticker FROM sec)
),
d AS (SELECT report_date FROM UNNEST(@dates) AS report_date),
base AS (
  SELECT d.report_date, x.ticker, x.i AS i0, x.close AS c0
  FROM d JOIN px x ON x.trade_date >= DATE_ADD(d.report_date, INTERVAL {lag} DAY)
   AND x.trade_date <= DATE_ADD(d.report_date, INTERVAL {lag} + 7 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY d.report_date, x.ticker ORDER BY x.trade_date) = 1
),
r AS (
  SELECT b.report_date, s.sector_code, SAFE_DIVIDE(p5.close, b.c0) - 1 AS r5, SAFE_DIVIDE(p20.close, b.c0) - 1 AS r20
  FROM base b JOIN sec s USING (ticker)
  LEFT JOIN px p5 ON p5.ticker = b.ticker AND p5.i = b.i0 + 5
  LEFT JOIN px p20 ON p20.ticker = b.ticker AND p20.i = b.i0 + 20
)
SELECT report_date, sector_code,
  AVG(IF(ABS(r5) <= {cap}, r5, NULL)) AS r5, COUNTIF(r5 IS NOT NULL AND ABS(r5) <= {cap}) AS n5,
  AVG(IF(ABS(r20) <= {cap}, r20, NULL)) AS r20, COUNTIF(r20 IS NOT NULL AND ABS(r20) <= {cap}) AS n20
FROM r GROUP BY report_date, sector_code
"""


def build_returns_sql(p: str) -> str:
    return _RET_SQL.format(p=p, lag=RELEASE_LAG_DAYS, cap=MAX_ABS_RETURN)


def _client():
    from edinet_bq_client import EDINETBQClient
    return EDINETBQClient().client


def fetch_positions(client, cfg) -> tuple:
    """建玉の行を1本で読む（cot_signal.refresh と同じ SQL・同じ上限）。見積もりが上限超なら (None, 理由)。"""
    from google.cloud import bigquery
    import cot_signal
    sql = cot_signal.build_query(cfg).replace("{project}", client.project)
    limit = int(_param(cfg, "COT_SIGNAL_MAX_BYTES", 100 * 1024 ** 2))
    est = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
                       location=LOCATION).total_bytes_processed
    if est > limit:
        return None, f"建玉の見積もり {est / 1024 ** 2:.1f}MB が上限超"
    job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=limit), location=LOCATION)
    return [dict(r) for r in job.result()], round(est / 1024 ** 3, 4)


def fetch_returns(client, dates: list, cfg) -> tuple:
    """{(報告日, 業種): {5: r5, 20: r20}} と請求GB。業種の銘柄数が MIN_SECTOR_MEMBERS 未満の組は除く。"""
    from google.cloud import bigquery
    if not dates:
        return {}, 0.0
    since = min(dates)
    jc = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("since", "DATE", since.isoformat()),
                          bigquery.ScalarQueryParameter("recent", "DATE", (date.today() - timedelta(days=30)).isoformat()),
                          bigquery.ArrayQueryParameter("dates", "DATE", [d.isoformat() for d in dates])],
        maximum_bytes_billed=int(_param(cfg, "COT_IC_MAX_BYTES", 150 * 1024 ** 2)))
    job = client.query(build_returns_sql(client.project), job_config=jc, location=LOCATION)
    out: dict = {}
    for r in job.result():
        v = {}
        if r.r5 is not None and (r.n5 or 0) >= MIN_SECTOR_MEMBERS:
            v[5] = float(r.r5)
        if r.r20 is not None and (r.n20 or 0) >= MIN_SECTOR_MEMBERS:
            v[20] = float(r.r20)
        if v:
            out[(r.report_date, str(r.sector_code))] = v
    return out, round((job.total_bytes_billed or 0) / 1024 ** 3, 3)


def write(client, rows: list) -> None:
    """結果で表を置き換える（load job なのでクエリの量に数えない）。"""
    from google.cloud import bigquery
    now = datetime.utcnow().isoformat()
    buf = io.BytesIO("\n".join(json.dumps({**{k: r.get(k) for k, _ in SCHEMA if k != "computed_at"},
                                           "report_date": str(r["report_date"]), "computed_at": now},
                                          ensure_ascii=False) for r in rows).encode("utf-8"))
    cfg = bigquery.LoadJobConfig(schema=[bigquery.SchemaField(n, t) for n, t in SCHEMA],
                                 write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                                 source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON)
    client.load_table_from_file(buf, f"{client.project}.{DATASET}.{TABLE}", location=LOCATION,
                                job_config=cfg).result()


def compute(positions: list, returns: dict, cfg, deltas: dict | None = None) -> list:
    """IC の行（先の日数ごと）。deltas を渡さなければ positions から作り直す。"""
    deltas = deltas if deltas is not None else weekly_deltas(positions, cfg, int(_param(cfg, "COT_IC_WEEKS", 52)))
    rows: list = []
    for h in HORIZONS:
        rows += weekly_ic(deltas, returns, h, int(_param(cfg, "COT_IC_MIN_ADJ_SECTORS", 3)))
    return rows


def run(cfg, client=None) -> dict:
    """毎週1回。失敗は例外にせず {"error": ...} で返す。逆向きでも点は止めず、メールの節で知らせる。"""
    if not _param(cfg, "ENABLE_COT_COMMODITY_IC", True):
        return {"skipped": True, "reason": "ENABLE_COT_COMMODITY_IC=False"}
    if not _param(cfg, "ENABLE_COT_COMMODITY", True):
        return {"skipped": True, "reason": "ENABLE_COT_COMMODITY=False"}
    try:
        client = client or _client()
        positions, gb_pos = fetch_positions(client, cfg)
        if positions is None:
            return {"skipped": True, "reason": gb_pos}
        deltas = weekly_deltas(positions, cfg, int(_param(cfg, "COT_IC_WEEKS", 52)))
        today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
        # 先の20取引日（約30暦日）が出そろっていない週は、5日先だけが測れる。クエリ側で欠けは NULL になる
        dates = [d for d in deltas if (today - d).days >= RELEASE_LAG_DAYS + 8]
        returns, gb = fetch_returns(client, dates, cfg)
        rows = compute(positions, returns, cfg, {d: deltas[d] for d in dates})
        if not rows:
            return {"skipped": True, "reason": "加点が付いた週で、先のリターンが出そろった週がまだ無い",
                    "weeks_with_adjustment": sum(1 for d in deltas.values() if d)}
        write(client, rows)
        summary = summarize(rows, min_weeks=float(_param(cfg, "COT_IC_MIN_WEEKS", 8.0)),
                            ic_bar=float(_param(cfg, "COT_IC_BAR", 0.05)), t_bar=float(_param(cfg, "COT_IC_T_BAR", 2.0)))
        res = {"rows": len(rows), "gb_billed": round(gb + (gb_pos or 0), 3), "summary": summary,
               "lines": report_lines(summary),
               "reversed": [f"{s['horizon']}d" for s in summary if s["verdict"] == "逆向き"],
               "effective": [f"{s['horizon']}d" for s in summary if s["verdict"] == "効いている"]}
        print(f"  🛢 商品の加点の効き: {len(rows)}行 / 逆向き {res['reversed'] or 'なし'}")
        return res
    except Exception as e:
        print(f"  ⚠️ 商品の加点の効きを測れない（{type(e).__name__}: {str(e)[:120]}）")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def build_alert(results: dict) -> dict | None:
    """日次メールの節（その回に計算した要約があるときだけ）。逆向きなら severity=warning で知らせる。"""
    res = (results or {}).get("cot_commodity_ic") or {}
    lines = res.get("lines") if isinstance(res, dict) else None
    if not lines:
        return None
    rev = bool(res.get("reversed"))
    return {"trigger": "cot_commodity_ic",
            "title": "🛢 商品の加点の効き（週次）" + ("：逆向き" if rev else ""),
            "body": "\n".join(lines), "severity": "warning" if rev else "info"}


if __name__ == "__main__":
    import config as _cfg
    r = run(_cfg)
    print("\n".join(r.get("lines") or [json.dumps({k: v for k, v in r.items() if k != "summary"}, ensure_ascii=False)]))
