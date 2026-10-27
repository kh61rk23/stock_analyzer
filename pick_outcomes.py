"""
pick_outcomes.py — 推奨のその後：通過銘柄の 5・20・60 取引日後の騰落を溜め、月1回メールに成績表を出す

【記録するもの（analytics.pick_outcomes・1行＝1つの選び方 × 1日 × 1銘柄）】
  source   jp_screener … 日本株の二次評価の通過銘柄（analytics.stock_snapshot・market=JP）
           us_screener … 米国株スクリーナーの通過銘柄（stock_snapshot・market=US）
           jp_universe … 日本株の全上場の一次採点の上位 PICK_OUTCOMES_UNIVERSE_TOP 社（screener_universe_daily）
  rank は同じ日・同じ選び方の中の点数の順位。is_new は前の営業日（その選び方で記録のある前の日）に無かった銘柄。
  買った値（entry）は、選ばれた日以降の最初の終値。分析はその日の朝に出るので、日本株はその日の大引け、
  米国株は日本時間のその日の夜の取引の終値になる。そこから各銘柄の取引日で 5・20・60 日後の終値との騰落を出し、
  同じ日に同じように買った指数の ETF（日本 1306.T・米国 SPY）の騰落を引いたものを超過（ex_*）とする。
  株価は analytics.price_daily_latest（重複を除いた view）。まだその日数が経っていなければ NULL。

【更新】日次の分析の最後（stock_snapshot の後）に1回。直近 PICK_OUTCOMES_REFRESH_DAYS 日の選ばれた日を計算し直して
  MERGE する（60 取引日後が埋まるまで取り直すため）。クエリは1本（price_daily を約0.1GB 読む）。
【成績表】月が替わって最初の日次メールに、前の月に選ばれた銘柄の成績（選び方 × 順位の帯）を1節出す。
  送った月は stock_data/data/pick_outcomes_state.json に残す（状態として BigQuery に保存される）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DATASET = "analytics"
TABLE = "pick_outcomes"
LOCATION = "asia-northeast1"
HORIZONS = (5, 20, 60)
BENCH = {"jp_screener": "1306.T", "us_screener": "SPY", "jp_universe": "1306.T"}
SOURCE_LABEL = {"jp_screener": "日本株（二次評価の通過）", "us_screener": "米国株（スクリーナーの通過）",
                "jp_universe": "日本株（全上場の一次採点の上位）"}
RANK_BANDS = ((1, 10, "上位10"), (11, 25, "11〜25位"), (26, 10 ** 6, "26位〜"))

SCHEMA = [
    ("pick_date", "DATE", "REQUIRED"), ("source", "STRING", "REQUIRED"), ("ticker", "STRING", "REQUIRED"),
    ("name", "STRING", "NULLABLE"), ("score", "FLOAT64", "NULLABLE"), ("rank", "INT64", "NULLABLE"),
    ("n_picks", "INT64", "NULLABLE"), ("is_new", "BOOL", "NULLABLE"), ("bench", "STRING", "NULLABLE"),
    ("entry_date", "DATE", "NULLABLE"), ("entry_close", "FLOAT64", "NULLABLE"),
] + [(f"{k}_{h}d", "FLOAT64", "NULLABLE") for h in HORIZONS for k in ("ret", "bench", "ex")] + [
    ("updated_at", "TIMESTAMP", "NULLABLE"),
]


def build_source_sql(p: str) -> str:
    """選ばれた日 @since 以降の行を計算する SELECT（MERGE の USING に入る）。"""
    joins, cols = [], []
    for h in HORIZONS:
        joins.append(f"LEFT JOIN px s{h} ON s{h}.ticker = e.ticker AND s{h}.i = e.ei + {h}")
        joins.append(f"LEFT JOIN px b{h} ON b{h}.ticker = e.bench AND b{h}.i = e.bi + {h}")
        cols.append(f"SAFE_DIVIDE(s{h}.close, e.entry_close) - 1 AS ret_{h}d")
        cols.append(f"SAFE_DIVIDE(b{h}.close, e.bench_close) - 1 AS bench_{h}d")
        cols.append(f"(SAFE_DIVIDE(s{h}.close, e.entry_close) - 1) - (SAFE_DIVIDE(b{h}.close, e.bench_close) - 1) AS ex_{h}d")
    return f"""
WITH raw AS (
  SELECT 'jp_screener' AS source, snapshot_date AS pick_date, ticker, ANY_VALUE(name) AS name, MAX(composite_score) AS score
  FROM `{p}.analytics.stock_snapshot` WHERE market = 'JP' AND snapshot_date >= @since GROUP BY 1, 2, 3
  UNION ALL
  SELECT 'us_screener', snapshot_date, ticker, ANY_VALUE(name), MAX(composite_score)
  FROM `{p}.analytics.stock_snapshot` WHERE market = 'US' AND snapshot_date >= @since GROUP BY 1, 2, 3
  UNION ALL
  SELECT 'jp_universe', score_date, ticker, ANY_VALUE(company_name), MAX(universe_score)
  FROM `{p}.analytics.screener_universe_daily`
  WHERE score_date >= @since AND universe_rank <= @top GROUP BY 1, 2, 3
),
days AS (
  SELECT source, pick_date, DENSE_RANK() OVER (PARTITION BY source ORDER BY pick_date) AS di
  FROM (SELECT DISTINCT source, pick_date FROM raw)
),
picks AS (
  SELECT r.*, d.di,
         ROW_NUMBER() OVER (PARTITION BY r.source, r.pick_date ORDER BY r.score DESC, r.ticker) AS rank,
         COUNT(*) OVER (PARTITION BY r.source, r.pick_date) AS n_picks,
         CASE r.source WHEN 'us_screener' THEN 'SPY' ELSE '1306.T' END AS bench
  FROM raw r JOIN days d USING (source, pick_date)
),
flagged AS (
  SELECT *, IFNULL(LAG(di) OVER (PARTITION BY source, ticker ORDER BY pick_date) != di - 1, TRUE) AS is_new
  FROM picks
),
px AS (
  SELECT ticker, trade_date, close, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date) AS i
  FROM `{p}.analytics.price_daily_latest`
  WHERE trade_date >= DATE_SUB(@since, INTERVAL 10 DAY) AND close > 0
    AND ticker IN (SELECT ticker FROM raw UNION DISTINCT SELECT bench FROM picks)
),
-- 選ばれた日以降の最初の終値（10日以内に無ければ買えなかった扱いで NULL）
se AS (
  SELECT f.source, f.pick_date, f.ticker, s.trade_date AS entry_date, s.close AS entry_close, s.i AS ei
  FROM flagged f JOIN px s ON s.ticker = f.ticker
   AND s.trade_date BETWEEN f.pick_date AND DATE_ADD(f.pick_date, INTERVAL 10 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY f.source, f.pick_date, f.ticker ORDER BY s.trade_date) = 1
),
be AS (
  SELECT d.bench, d.pick_date, b.close AS bench_close, b.i AS bi
  FROM (SELECT DISTINCT bench, pick_date FROM picks) d JOIN px b ON b.ticker = d.bench
   AND b.trade_date BETWEEN d.pick_date AND DATE_ADD(d.pick_date, INTERVAL 10 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY d.bench, d.pick_date ORDER BY b.trade_date) = 1
),
e AS (
  SELECT f.*, se.entry_date, se.entry_close, se.ei, be.bench_close, be.bi
  FROM flagged f
  LEFT JOIN se USING (source, pick_date, ticker)
  LEFT JOIN be USING (bench, pick_date)
)
SELECT e.pick_date, e.source, e.ticker, e.name, e.score, e.rank, e.n_picks, e.is_new, e.bench,
       e.entry_date, e.entry_close,
       {", ".join(cols)},
       CURRENT_TIMESTAMP() AS updated_at
FROM e
{chr(10).join(joins)}
"""


def build_merge_sql(p: str) -> str:
    names = [n for n, _t, _m in SCHEMA]
    sets = ", ".join(f"{n} = S.{n}" for n in names if n not in ("pick_date", "source", "ticker"))
    return f"""
MERGE `{p}.{DATASET}.{TABLE}` T
USING ({build_source_sql(p)}) S
ON T.pick_date = S.pick_date AND T.source = S.source AND T.ticker = S.ticker
WHEN MATCHED THEN UPDATE SET {sets}
WHEN NOT MATCHED THEN INSERT ({", ".join(names)}) VALUES ({", ".join("S." + n for n in names)})
"""


# ── 成績表（純関数） ──────────────────────────────────────────

def band_of(rank: int) -> str:
    for lo, hi, label in RANK_BANDS:
        if lo <= rank <= hi:
            return label
    return RANK_BANDS[-1][2]


def summarize(rows: list[dict]) -> dict:
    """{(source, 帯, new_only): {h: {"n", "avg", "ex", "hit"}}}。new_only=True は新しく選ばれた日だけ。"""
    out: dict = {}
    for r in rows:
        for new_only in (False, True):
            if new_only and not r.get("is_new"):
                continue
            for band in (band_of(int(r["rank"] or 10 ** 6)), "全体"):
                cell = out.setdefault((r["source"], band, new_only), {h: [] for h in HORIZONS})
                for h in HORIZONS:
                    ret, ex = r.get(f"ret_{h}d"), r.get(f"ex_{h}d")
                    if ret is not None and ex is not None:
                        cell[h].append((float(ret), float(ex)))
    res: dict = {}
    for key, cell in out.items():
        res[key] = {}
        for h, xs in cell.items():
            n = len(xs)
            res[key][h] = {"n": n,
                           "avg": sum(a for a, _ in xs) / n if n else None,
                           "ex": sum(b for _, b in xs) / n if n else None,
                           "hit": sum(1 for _, b in xs if b > 0) / n if n else None}
    return res


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def report_lines(month: str, summary: dict, best: list[dict], worst: list[dict]) -> list[str]:
    """成績表のメールの本文の行。"""
    lines = [f"  {month} に選ばれた銘柄の、選ばれた日の次の終値で買ったときの騰落（超過＝同じ日に指数の ETF を買った場合との差・"
             "勝ち＝超過がプラスの割合）。60日後はまだ出ていない分を除く。"]
    for src in ("jp_screener", "jp_universe", "us_screener"):
        bands = [b for _lo, _hi, b in RANK_BANDS] + ["全体"]
        if not any((src, b, False) in summary for b in bands):
            continue
        lines.append(f"  【{SOURCE_LABEL[src]}】")
        for new_only in (True, False):
            lines.append("    " + ("新しく選ばれた日だけ" if new_only else "毎日の全部（同じ銘柄が続けて選ばれると重く数える）"))
            for b in bands:
                cell = summary.get((src, b, new_only))
                if not cell:
                    continue
                parts = []
                for h in HORIZONS:
                    c = cell[h]
                    if c["n"]:
                        parts.append(f"{h}日 {_pct(c['avg'])}（超過 {_pct(c['ex'])}・勝ち {c['hit'] * 100:.0f}%・{c['n']}件）")
                lines.append(f"      {b}: " + (" / ".join(parts) if parts else "まだ日数が経っていない"))
    if best:
        lines.append("  20日後の超過が大きかった新しい選定: " + "、".join(
            f"{r['name'] or r['ticker']}（{r['ticker']}・{_pct(r['ex_20d'])}）" for r in best))
    if worst:
        lines.append("  小さかった新しい選定: " + "、".join(
            f"{r['name'] or r['ticker']}（{r['ticker']}・{_pct(r['ex_20d'])}）" for r in worst))
    return lines


def previous_month(today: date) -> tuple[date, date, str]:
    first = today.replace(day=1)
    last_prev = first - timedelta(days=1)
    start = last_prev.replace(day=1)
    return start, first, start.strftime("%Y-%m")


# ── I/O ──────────────────────────────────────────────────────

def _client(cfg):
    from edinet_bq_client import EDINETBQClient
    return EDINETBQClient().client


def ensure_table(client) -> None:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    ref = f"{client.project}.{DATASET}.{TABLE}"
    try:
        client.get_table(ref)
        return
    except NotFound:
        pass
    t = bigquery.Table(ref, schema=[bigquery.SchemaField(n, ty, mode=m) for n, ty, m in SCHEMA])
    t.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.MONTH, field="pick_date")
    t.clustering_fields = ["source", "ticker"]
    client.create_table(t)


def update(cfg, since: date | None = None) -> dict:
    """直近の選ばれた日を計算し直して MERGE する。失敗は例外にせず {"error": ...} で返す。"""
    if not getattr(cfg, "ENABLE_PICK_OUTCOMES", True):
        return {"skipped": True, "reason": "ENABLE_PICK_OUTCOMES=False"}
    from google.cloud import bigquery
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    since = since or today - timedelta(days=int(getattr(cfg, "PICK_OUTCOMES_REFRESH_DAYS", 130)))
    try:
        client = _client(cfg)
        ensure_table(client)
        jc = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("since", "DATE", since.isoformat()),
            bigquery.ScalarQueryParameter("top", "INT64", int(getattr(cfg, "PICK_OUTCOMES_UNIVERSE_TOP", 100)))])
        job = client.query(build_merge_sql(client.project), job_config=jc, location=LOCATION)
        job.result()
        info = {"since": since.isoformat(), "rows": int(job.num_dml_affected_rows or 0),
                "gb_billed": round((job.total_bytes_billed or 0) / 1024 ** 3, 3)}
        print(f"  📈 推奨のその後: {info}")
        return info
    except Exception as e:
        print(f"  ⚠️ 推奨のその後を更新できない（{type(e).__name__}: {str(e)[:120]}）")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def _state_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "pick_outcomes_state.json"


def monthly_report(cfg, today: date | None = None, force: bool = False) -> dict | None:
    """月が替わって最初の回なら、前の月の成績表の alert（trigger=pick_outcomes_monthly）を返す。それ以外は None。"""
    if not getattr(cfg, "ENABLE_PICK_OUTCOMES", True):
        return None
    today = today or datetime.now(ZoneInfo("Asia/Tokyo")).date()
    start, end, month = previous_month(today)
    path = _state_path(cfg)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if not force and state.get("last_report_month") == month:
        return None
    from google.cloud import bigquery
    client = _client(cfg)
    jc = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("a", "DATE", start.isoformat()),
        bigquery.ScalarQueryParameter("b", "DATE", end.isoformat())])
    sql = ("SELECT source, ticker, name, rank, is_new, " +
           ", ".join(f"ret_{h}d, ex_{h}d" for h in HORIZONS) +
           f" FROM `{client.project}.{DATASET}.{TABLE}` WHERE pick_date >= @a AND pick_date < @b")
    rows = [dict(r.items()) for r in client.query(sql, job_config=jc, location=LOCATION).result()]
    if not rows:
        return None
    summary = summarize(rows)
    new20 = sorted((r for r in rows if r.get("is_new") and r.get("ex_20d") is not None and int(r["rank"] or 99) <= 25),
                   key=lambda r: r["ex_20d"], reverse=True)
    lines = report_lines(month, summary, new20[:5], new20[-5:][::-1] if len(new20) > 5 else [])
    state["last_report_month"] = month
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return {"trigger": "pick_outcomes_monthly", "title": f"📈 推奨のその後（{month} の成績表）",
            "body": "\n".join(lines), "severity": "info"}


DELIB_SQL = """
WITH dl AS (
  SELECT ticker, ANY_VALUE(company_name) AS name, DATE(deliberated_at, 'Asia/Tokyo') AS d,
         ANY_VALUE(verdict) AS verdict, MAX(conviction) AS conviction, ANY_VALUE(approval_status) AS approval,
         IF(ENDS_WITH(ticker, '.T'), '1306.T', 'SPY') AS bench
  FROM `{p}.invest.deliberations`
  WHERE DATE(deliberated_at, 'Asia/Tokyo') >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL @days DAY)
  GROUP BY ticker, d
),
px AS (
  SELECT ticker, trade_date, close FROM `{p}.analytics.price_daily_latest`
  WHERE trade_date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL @days + 10 DAY) AND close > 0
    AND ticker IN (SELECT ticker FROM dl UNION DISTINCT SELECT bench FROM dl)
),
e AS (   -- 審議した日以降の最初の終値
  SELECT dl.ticker, dl.d, px.close AS c0 FROM dl JOIN px ON px.ticker = dl.ticker AND px.trade_date >= dl.d
  QUALIFY ROW_NUMBER() OVER (PARTITION BY dl.ticker, dl.d ORDER BY px.trade_date) = 1
),
l AS (   -- 直近の終値
  SELECT ticker, trade_date AS d1, close AS c1 FROM px
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) = 1
),
be AS (
  SELECT k.bench, k.d, px.close AS b0 FROM (SELECT DISTINCT bench, d FROM dl) k
  JOIN px ON px.ticker = k.bench AND px.trade_date >= k.d
  QUALIFY ROW_NUMBER() OVER (PARTITION BY k.bench, k.d ORDER BY px.trade_date) = 1
),
bl AS (
  SELECT x.ticker, x.d1, px.close AS b1 FROM (SELECT DISTINCT dl.ticker, dl.bench, l.d1 FROM dl JOIN l USING (ticker)) x
  JOIN px ON px.ticker = x.bench AND px.trade_date <= x.d1
  QUALIFY ROW_NUMBER() OVER (PARTITION BY x.ticker ORDER BY px.trade_date DESC) = 1
)
SELECT dl.*, e.c0, l.c1, l.d1, be.b0, bl.b1
FROM dl LEFT JOIN e USING (ticker, d) LEFT JOIN l USING (ticker)
LEFT JOIN be USING (bench, d) LEFT JOIN bl USING (ticker)
ORDER BY dl.d DESC, dl.conviction DESC
"""


def deliberation_lines(rows: list[dict]) -> list[str]:
    """審議の結果とその後（審議した日以降の最初の終値から直近の終値まで・指数の ETF との差）。"""
    out = []
    for r in rows:
        ret = (r["c1"] / r["c0"] - 1) if r.get("c0") and r.get("c1") else None
        bret = (r["b1"] / r["b0"] - 1) if r.get("b0") and r.get("b1") else None
        tail = "株価なし" if ret is None else (f"その後 {_pct(ret)}" + (f"（超過 {_pct(ret - bret)}）" if bret is not None else ""))
        name = (r.get("name") or "").strip()
        out.append(f"    {r['d']} {name[:14]}（{r['ticker']}）: {r.get('verdict') or '-'}・確信度 {r.get('conviction') or '-'}"
                   f"・{r.get('approval') or '-'} → {tail}")
    return out


def deliberation_alert(cfg) -> dict | None:
    """日次メールの「審議の結果とその後」（直近 PICK_OUTCOMES_DELIB_DAYS 日の審議）。審議が無ければ None。"""
    if not getattr(cfg, "ENABLE_PICK_OUTCOMES", True):
        return None
    from google.cloud import bigquery
    client = _client(cfg)
    jc = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("days", "INT64", int(getattr(cfg, "PICK_OUTCOMES_DELIB_DAYS", 90)))])
    rows = [dict(r.items()) for r in client.query(DELIB_SQL.format(p=client.project), job_config=jc,
                                                   location=LOCATION).result()]
    if not rows:
        return None
    return {"trigger": "deliberation_followup", "title": f"🗳 審議の結果とその後（直近{getattr(cfg, 'PICK_OUTCOMES_DELIB_DAYS', 90)}日・{len(rows)}件）",
            "body": "\n".join(["  審議した日以降の最初の終値から直近の終値まで。超過は同じ期間の指数の ETF（1306.T・SPY）との差。"]
                              + deliberation_lines(rows)), "severity": "info"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="この日以降の選ばれた日を計算し直す（YYYY-MM-DD）。既定は直近 PICK_OUTCOMES_REFRESH_DAYS 日")
    ap.add_argument("--report", help="この日を今日として前の月の成績表を出す（YYYY-MM-DD・送った記録は付けない）")
    a = ap.parse_args(argv)
    import config as cfg
    if a.report:
        st = _state_path(cfg)
        keep = st.read_text(encoding="utf-8") if st.exists() else None
        alert = monthly_report(cfg, date.fromisoformat(a.report), force=True)
        if keep is None:
            st.unlink(missing_ok=True)
        else:
            st.write_text(keep, encoding="utf-8")
        print(alert["title"] + "\n" + alert["body"] if alert else "（その月の記録が無い）")
        return 0
    info = update(cfg, date.fromisoformat(a.since) if a.since else None)
    return 1 if info.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
