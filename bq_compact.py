# ==============================================================
# bq_compact.py — BigQuery の保存量を減らす（重複・読み手のいない行を消す）
# ==============================================================
# 保存量の課金は論理サイズ（bq_storage.py）。load job の追記や遡りの取り込みで溜まった次のものを消す。
#   price_dedupe  analytics.price_daily … (ticker, trade_date) の重複を最新の1行にして作り直す。
#                 読み手の analytics.price_daily_latest は同じ「ingested_at が新しい1行」を採るので、見える値は変わらない。
#                 パーティション（trade_date）・クラスタ（ticker）・NOT NULL は元のまま。
#   price_monthly analytics.price_daily … 日足を残す期間（PRICE_DAILY_KEEP_MONTHS・既定24か月）より古い月を月足
#                 （analytics.price_monthly）にまとめ、まとめ終えた月の日足を消す。月ごとに「月足を作る→件数と取り込み時刻を
#                 確かめる→日足を消す」の順で、確かめで食い違えば日足は消さず止まる。続けて、日足と月足の境目で価格の基準が
#                 ずれた銘柄（分割・併合の取り直しが済んだあとの古い月足）を直す。古い期間は表関数 analytics.price_history_mixed
#                 （日足と月足を1本にした読み口）で読む。
#   sec_forms     us.sec_filings … SEC_FILINGS_KEEP_FORMS 以外の書類（Form 4 など）の行を消す。
#   gov_facts     edinet.gov_doc_facts … 大量保有（350/360/370）の、large_holdings view が引かない要素の行を消す。
# 【費用】どれも dryRun で読むバイト数を見積もり、BQ_COMPACT_MAX_BYTES（1GiB）を超えたら実行しない。
#   実行時も maximum_bytes_billed で止める。ここに書く処理は BQ の表を書き換える。--dry-run は見積もりだけ。
# 【実行】python3 bq_compact.py {price_dedupe|price_monthly|sec_forms|gov_facts|cot_legacy} [--dry-run]
# ==============================================================
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

PRICE_COLUMNS = ("trade_date DATE NOT NULL, ticker STRING NOT NULL, open FLOAT64, high FLOAT64, low FLOAT64, "
                 "close FLOAT64, volume FLOAT64, source STRING, ingested_at TIMESTAMP")
PRICE_LOCATION, SEC_LOCATION, GOV_LOCATION = "asia-northeast1", "US", "asia-northeast1"


def price_dedupe_sql(project: str) -> str:
    t = f"`{project}.analytics.price_daily`"
    return (f"CREATE OR REPLACE TABLE {t} ({PRICE_COLUMNS})\n"
            "PARTITION BY trade_date CLUSTER BY ticker AS\n"
            "SELECT trade_date, ticker, open, high, low, close, volume, source, ingested_at\n"
            f"FROM {t}\n"
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, trade_date ORDER BY ingested_at DESC) = 1")


def sec_forms_sql(project: str, keep_forms) -> str:
    if not keep_forms:
        raise ValueError("SEC_FILINGS_KEEP_FORMS が空（全種類を入れる設定）なので消すものがない")
    conds = " OR ".join("STARTS_WITH(UPPER(form), '%s')" % str(k).upper().replace("'", "") for k in keep_forms)
    return f"DELETE FROM `{project}.us.sec_filings` WHERE form IS NULL OR NOT ({conds})"


def gov_facts_sql(project: str) -> str:
    import edinet_gov_docs as g
    ids = ", ".join(str(i) for i in sorted(g.lvh_element_ids()))
    types = ", ".join(f"'{t}'" for t in g.LVH_TYPES)
    return (f"DELETE FROM `{project}.edinet.gov_doc_facts`\n"
            f"WHERE doc_type_code IN ({types}) AND element_id NOT IN ({ids})")


def cot_legacy_sql(project: str, cfg) -> str:
    import market_positioning as mp
    codes = sorted(mp.cot_keep_codes(cfg))
    if not codes:
        raise ValueError("残す legacy の市場が無い（COT_SIGNAL_MARKETS を確かめる）")
    keep = ", ".join("'%s'" % c.replace("'", "") for c in codes)
    return (f"DELETE FROM `{project}.analytics.cot_positions`\n"
            f"WHERE report = 'legacy' AND (market_code IS NULL OR market_code NOT IN ({keep}))")


# ==============================================================
# price_monthly: 古い日足を月足にまとめる
# ==============================================================
# 日足を残す期間の決め方（config PRICE_DAILY_KEEP_MONTHS・既定24か月）
#   ・読み手が日足で要る最長は約440日（米国スクリーナーの price_days）で、約14.5か月。
#   ・分割の取り直し（price_universe の SPLIT_REFETCH_DAYS=400営業日）は暦で約607日＝約20か月ぶんの日足を書き直す。
#     これより古い日足は月足に畳んでよいが、畳んだ月に取り直しが届くと月足との基準がずれるので、残す期間は
#     取り直しの届く範囲より長くする（下限 PRICE_MONTHLY_MIN_KEEP_MONTHS=22）。
#   ・境目は暦月で切る：今日の月の月初から keep か月前の月初より前の月だけを畳む（日足は keep〜keep+1 か月ぶん残る）。
PRICE_MONTHLY_TABLE = "analytics.price_monthly"
PRICE_MIXED_FUNCTION = "analytics.price_history_mixed"
PRICE_MONTHLY_COLUMNS = ("month_start DATE NOT NULL, ticker STRING NOT NULL, open FLOAT64, high FLOAT64, low FLOAT64, "
                         "close FLOAT64, volume FLOAT64, first_date DATE, last_date DATE, n_days INT64, "
                         "adj_basis_date DATE, source STRING, ingested_at TIMESTAMP")
_TICKER_OK = re.compile(r"^[A-Za-z0-9.\-=^]{1,20}$")
_JST = ZoneInfo("Asia/Tokyo")


class PriceMonthlyError(RuntimeError):
    """月足への畳み込みを続けられない（見積もり超過・確かめの食い違い・下限割れ）。日足は消していない。"""


def add_months(d: date, n: int) -> date:
    """d の月初を n か月ずらした日（n は負でもよい）。"""
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def fold_boundary(today: date, keep_months: int) -> date:
    """この日より前の月が月足に畳む対象。今日の月初から keep か月前の月初。"""
    return add_months(today.replace(day=1), -int(keep_months))


def price_monthly_ddl(project: str) -> list[str]:
    """月足の表と読み口（表関数）。月パーティション・ticker クラスタ。何度流しても同じ。"""
    t = f"`{project}.{PRICE_MONTHLY_TABLE}`"
    d = f"`{project}.analytics.price_daily`"
    fn = f"`{project}.{PRICE_MIXED_FUNCTION}`"
    return [
        f"CREATE TABLE IF NOT EXISTS {t} ({PRICE_MONTHLY_COLUMNS})\n"
        "PARTITION BY DATE_TRUNC(month_start, MONTH) CLUSTER BY ticker",
        # 月足の行は「その月の全体」。日足は重複を最新の1行にして読み、月足のある月の日足は出さない（畳み込みの途中で
        # 両方が在る間に二重に数えないため）。日付の絞り込みが表のパーティションに効くよう、ビューでなく表関数にする。
        f"CREATE OR REPLACE TABLE FUNCTION {fn}(start_date DATE, end_date DATE) AS (\n"
        "WITH mo AS (\n"
        "  SELECT month_start, ticker, open, high, low, close, volume, first_date, last_date, n_days\n"
        f"  FROM {t}\n"
        "  WHERE month_start >= DATE_TRUNC(start_date, MONTH) AND month_start <= end_date),\n"
        "dy AS (\n"
        "  SELECT trade_date, ticker, open, high, low, close, volume\n"
        f"  FROM {d}\n"
        "  WHERE trade_date BETWEEN start_date AND end_date\n"
        "  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, trade_date ORDER BY ingested_at DESC) = 1)\n"
        "SELECT 'M' AS granularity, month_start AS period_start, last_date AS period_end, ticker,\n"
        "       open, high, low, close, volume, n_days FROM mo\n"
        "UNION ALL\n"
        "SELECT 'D', trade_date, trade_date, ticker, open, high, low, close, volume, 1 FROM dy\n"
        "WHERE NOT EXISTS (SELECT 1 FROM mo WHERE mo.ticker = dy.ticker AND mo.month_start = DATE_TRUNC(dy.trade_date, MONTH)))",
    ]


def _lit(d: date) -> str:
    return f"DATE '{d.isoformat()}'"


def pending_months_sql(project: str, boundary: date) -> str:
    """日足の表にあって boundary より前の月（月ごとの行数つき）。メタデータだけを読む。"""
    return ("SELECT DATE_TRUNC(PARSE_DATE('%Y%m%d', partition_id), MONTH) AS m, SUM(total_rows) AS n\n"
            f"FROM `{project}.analytics.INFORMATION_SCHEMA.PARTITIONS`\n"
            "WHERE table_name = 'price_daily' AND partition_id NOT IN ('__NULL__', '__UNPARTITIONED__')\n"
            f"GROUP BY m HAVING m < {_lit(boundary)} AND SUM(total_rows) > 0 ORDER BY m")


def _daily_dedup(project: str, month: date) -> str:
    """その月の日足（重複を最新の1行にしたもの・終値なしは除く）。"""
    return (f"SELECT * FROM `{project}.analytics.price_daily`\n"
            f"  WHERE trade_date >= {_lit(month)} AND trade_date < {_lit(add_months(month, 1))} AND close > 0\n"
            "  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, trade_date ORDER BY ingested_at DESC) = 1")


def monthly_delete_sql(project: str, month: date) -> str:
    return f"DELETE FROM `{project}.{PRICE_MONTHLY_TABLE}` WHERE month_start = {_lit(month)}"


def monthly_insert_sql(project: str, month: date) -> str:
    """1か月ぶんの日足から月足を作る。始値は月初の日の、終値は月末の日の値（欠けた日は飛ばす）、出来高は合計。"""
    return (f"INSERT INTO `{project}.{PRICE_MONTHLY_TABLE}`\n"
            "(month_start, ticker, open, high, low, close, volume, first_date, last_date, n_days, adj_basis_date, source, ingested_at)\n"
            "SELECT DATE_TRUNC(trade_date, MONTH), ticker,\n"
            "  ARRAY_AGG(open IGNORE NULLS ORDER BY trade_date ASC LIMIT 1)[SAFE_OFFSET(0)],\n"
            "  MAX(high), MIN(low),\n"
            "  ARRAY_AGG(close IGNORE NULLS ORDER BY trade_date DESC LIMIT 1)[SAFE_OFFSET(0)],\n"
            "  SUM(volume), MIN(trade_date), MAX(trade_date), COUNT(*),\n"
            "  CURRENT_DATE('Asia/Tokyo'), 'price_monthly', CURRENT_TIMESTAMP()\n"
            f"FROM ({_daily_dedup(project, month)})\n"
            "GROUP BY 1, 2")


def monthly_verify_sql(project: str, month: date) -> str:
    """日足を消してよいかの確かめ。月足の銘柄数＝日足の銘柄数、かつ月足を作ったあとに日足へ入った行が無いこと。"""
    nxt = add_months(month, 1)
    rng = f"trade_date >= {_lit(month)} AND trade_date < {_lit(nxt)}"
    return ("SELECT\n"
            f"  (SELECT COUNT(*) FROM `{project}.{PRICE_MONTHLY_TABLE}` WHERE month_start = {_lit(month)}) AS monthly_n,\n"
            f"  (SELECT COUNT(DISTINCT ticker) FROM `{project}.analytics.price_daily` WHERE {rng} AND close > 0) AS daily_n,\n"
            f"  (SELECT COUNT(*) FROM `{project}.analytics.price_daily` WHERE {rng}\n"
            f"     AND ingested_at > (SELECT MAX(ingested_at) FROM `{project}.{PRICE_MONTHLY_TABLE}` WHERE month_start = {_lit(month)})) AS late_n")


def daily_delete_sql(project: str, month: date) -> str:
    """畳んだ月の日足を消す。月まるごと（日パーティションまるごと）なのでメタデータ操作で済み、読む量はほぼ 0。"""
    return (f"DELETE FROM `{project}.analytics.price_daily`\n"
            f"WHERE trade_date >= {_lit(month)} AND trade_date < {_lit(add_months(month, 1))}")


def latest_monthly_sql(project: str) -> str:
    return f"SELECT MAX(month_start) AS l FROM `{project}.{PRICE_MONTHLY_TABLE}`"


def seam_sql(project: str, last_month: date) -> str:
    """月足の最後の月末の終値と、日足の最初の月の最初の終値を銘柄ごとに並べ、分割比に近い跳びのある銘柄だけ返す。

    分割・併合の取り直しで日足は新しい基準に揃うが、畳んだあとの月足は古い基準のまま残る。その食い違いは境目の跳びとして見える。"""
    import price_universe as pu
    lo = 2 * (1 - pu.SPLIT_TOL_SMALL)
    first, second = add_months(last_month, 1), add_months(last_month, 2)
    return (
        "WITH m AS (\n"
        "  SELECT ticker, close AS m_close, SAFE_DIVIDE(volume, n_days) AS m_vol\n"
        f"  FROM `{project}.{PRICE_MONTHLY_TABLE}` WHERE month_start >= {_lit(add_months(last_month, -2))}\n"
        "  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY month_start DESC) = 1),\n"
        "d AS (\n"
        "  SELECT ticker, trade_date, close, volume FROM `" + f"{project}.analytics.price_daily`\n"
        f"  WHERE trade_date >= {_lit(first)} AND trade_date < {_lit(second)} AND close > 0\n"
        "  QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, trade_date ORDER BY ingested_at DESC) = 1),\n"
        "d1 AS (SELECT ticker, ARRAY_AGG(close ORDER BY trade_date LIMIT 1)[OFFSET(0)] AS d_close, AVG(volume) AS d_vol\n"
        "       FROM d GROUP BY ticker)\n"
        "SELECT m.ticker, m.m_close, d1.d_close, m.m_vol, d1.d_vol FROM m JOIN d1 USING (ticker)\n"
        f"WHERE m.m_close > 0 AND d1.d_close > 0 AND (m.m_close / d1.d_close >= {lo} OR m.m_close / d1.d_close <= {1 / lo})")


def plan_seam_rescale(rows, max_tickers: int = 50, vol_tol: float = 1.6) -> dict:
    """境目の跳びの行から、月足を直す銘柄と比を決める（純粋関数）。

    比 = 月足の終値 / 日足の終値 を分割比（price_universe の SPLIT_RATIOS）に丸める。実際の暴落・急騰を分割と取り違えないよう、
    出来高も確かめる：分割なら1日あたりの出来高は逆の比（月足側 / 日足側 ≒ 1/比）で動く。範囲外・出来高なしは直さない。
    1回に直す銘柄が max_tickers を超えるときは判定が荒れているとみて何も直さない。
    戻り値 {"ratios": {ticker: 比}, "skipped": [(ticker, 理由)], "refused": bool}。"""
    import price_universe as pu
    ratios: dict = {}
    skipped: list = []
    for r in rows or []:
        t = r["ticker"]
        try:
            raw = float(r["m_close"]) / float(r["d_close"])
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        s = pu._near_split(raw)
        if s is None:
            continue
        if not _TICKER_OK.match(str(t)):
            skipped.append((t, "ticker"))
            continue
        clean = s if raw > 1 else 1.0 / s
        mv, dv = r.get("m_vol"), r.get("d_vol")
        if not mv or not dv or float(mv) <= 0 or float(dv) <= 0:
            skipped.append((t, "no_volume"))
            continue
        vr, exp = float(mv) / float(dv), 1.0 / clean
        if not (exp / vol_tol <= vr <= exp * vol_tol):
            skipped.append((t, "volume"))
            continue
        ratios[t] = clean
    if len(ratios) > max_tickers:
        return {"ratios": {}, "skipped": skipped + [(t, "too_many") for t in ratios], "refused": True}
    return {"ratios": ratios, "skipped": skipped, "refused": False}


def monthly_rescale_sql(project: str, ratios: dict) -> str:
    """月足の価格を ratio（古い基準 / 新しい基準）で割り、出来高を掛けて新しい基準に揃える。"""
    if not ratios:
        raise ValueError("直す銘柄が無い")
    items = []
    for t, r in sorted(ratios.items()):
        if not _TICKER_OK.match(str(t)) or not float(r) > 0:
            raise ValueError(f"不正な銘柄または比: {t!r} {r!r}")
        items.append(f"STRUCT('{t}' AS ticker, {float(r)!r} AS ratio)")
    return (f"UPDATE `{project}.{PRICE_MONTHLY_TABLE}` AS m\n"
            "SET open = m.open / r.ratio, high = m.high / r.ratio, low = m.low / r.ratio, close = m.close / r.ratio,\n"
            "    volume = m.volume * r.ratio, adj_basis_date = CURRENT_DATE('Asia/Tokyo')\n"
            f"FROM (SELECT * FROM UNNEST([{', '.join(items)}])) AS r\n"
            "WHERE m.ticker = r.ticker")


def _estimate(client, sql: str, location: str, mx: int) -> int:
    from google.cloud import bigquery
    est = int(client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
                           location=location).total_bytes_processed or 0)
    if est > mx:
        raise PriceMonthlyError(f"見積もり {est / 1024 ** 2:.0f}MB が上限 {mx / 1024 ** 2:.0f}MB を超える: {sql[:80]!r}")
    return est


def _exec(client, sql: str, location: str, mx: int):
    """dryRun で見積もってから流す（上限超えは PriceMonthlyError）。結果の行と課金バイトを返す。"""
    from google.cloud import bigquery
    _estimate(client, sql, location, mx)
    job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=mx, use_query_cache=False),
                       location=location)
    rows = list(job.result())
    return rows, int(getattr(job, "total_bytes_billed", 0) or 0)


def run_price_monthly(client, cfg, dry_run: bool = False, today: date | None = None, keep_months: int | None = None) -> dict:
    """境目より古い日足の月を、古い月から最大 PRICE_MONTHLY_MAX_MONTHS_PER_RUN か月ぶん月足にして日足を消す。

    順序：表と関数を用意 → 境目のずれた銘柄を直す → 月ごとに（月足を作り直す → 確かめる → 日足を消す）。
    どこで失敗しても、確かめを通っていない月の日足は消さない。月足の作り直しは先に同じ月の月足を消してから入れるので、
    やり直しても二重にならない。dry_run は見積もりと対象の月を返すだけで何も書かない。"""
    project = client.project
    loc = PRICE_LOCATION
    mx = int(getattr(cfg, "BQ_COMPACT_MAX_BYTES", 1024 ** 3))
    floor = int(getattr(cfg, "PRICE_MONTHLY_MIN_KEEP_MONTHS", 22))
    keep = int(keep_months if keep_months is not None else getattr(cfg, "PRICE_DAILY_KEEP_MONTHS", 24))
    if keep < floor:
        raise PriceMonthlyError(f"日足を残す期間 {keep}か月は下限 {floor}か月（分割の取り直しが届く範囲）より短い")
    max_months = int(getattr(cfg, "PRICE_MONTHLY_MAX_MONTHS_PER_RUN", 3))
    today = today or datetime.now(_JST).date()
    boundary = fold_boundary(today, keep)
    out: dict = {"boundary": boundary.isoformat(), "keep_months": keep, "dry_run": dry_run, "folded": [], "pending": [],
                 "seam": {"fixed": [], "skipped": []}, "billed_bytes": 0}

    pending_rows, b = _exec(client, pending_months_sql(project, boundary), loc, mx)
    out["billed_bytes"] += b
    pending = [(r["m"], int(r["n"] or 0)) for r in pending_rows]
    out["pending"] = [m.isoformat() for m, _ in pending]
    if dry_run:
        out["estimated_bytes"] = sum(
            _estimate(client, monthly_insert_sql(project, m), loc, mx) for m, _ in pending[:max_months]) if _table_exists(client, PRICE_MONTHLY_TABLE) else None
        return out

    for stmt in price_monthly_ddl(project):
        _exec(client, stmt, loc, mx)

    # 境目のずれ（古い基準の月足）を、新しい月を畳む前に直す
    lm_rows, b = _exec(client, latest_monthly_sql(project), loc, mx)
    out["billed_bytes"] += b
    last = lm_rows[0]["l"] if lm_rows else None
    if last is not None:
        seam_rows, b = _exec(client, seam_sql(project, last), loc, mx)
        out["billed_bytes"] += b
        plan = plan_seam_rescale([dict(r) for r in seam_rows], int(getattr(cfg, "PRICE_MONTHLY_SEAM_MAX_TICKERS", 50)))
        out["seam"]["skipped"] = plan["skipped"]
        out["seam"]["refused"] = plan["refused"]
        if plan["ratios"]:
            _, b = _exec(client, monthly_rescale_sql(project, plan["ratios"]), loc, mx)
            out["billed_bytes"] += b
            out["seam"]["fixed"] = sorted(plan["ratios"].items())

    for month, _n in pending[:max_months]:
        for sql in (monthly_delete_sql(project, month), monthly_insert_sql(project, month)):
            _, b = _exec(client, sql, loc, mx)
            out["billed_bytes"] += b
        rows, b = _exec(client, monthly_verify_sql(project, month), loc, mx)
        out["billed_bytes"] += b
        v = rows[0]
        if not (int(v["monthly_n"]) > 0 and int(v["monthly_n"]) == int(v["daily_n"]) and int(v["late_n"]) == 0):
            raise PriceMonthlyError(
                f"{month:%Y-%m} の確かめが合わない（月足 {v['monthly_n']}銘柄・日足 {v['daily_n']}銘柄・後から入った行 {v['late_n']}）。"
                "日足は消していない")
        _, b = _exec(client, daily_delete_sql(project, month), loc, mx)
        out["billed_bytes"] += b
        out["folded"].append(month.isoformat())
    out["ran"] = True
    return out


def _table_exists(client, table: str) -> bool:
    try:
        client.get_table(f"{client.project}.{table}")
        return True
    except Exception:
        return False


TASKS = {   # 名前 → (SQL を作る関数, location, 対象の表)
    "price_dedupe": (lambda p, cfg: price_dedupe_sql(p), PRICE_LOCATION, "analytics.price_daily"),
    "sec_forms": (lambda p, cfg: sec_forms_sql(p, tuple(getattr(cfg, "SEC_FILINGS_KEEP_FORMS", ()) or ())),
                  SEC_LOCATION, "us.sec_filings"),
    "gov_facts": (lambda p, cfg: gov_facts_sql(p), GOV_LOCATION, "edinet.gov_doc_facts"),
    "cot_legacy": (lambda p, cfg: cot_legacy_sql(p, cfg), PRICE_LOCATION, "analytics.cot_positions"),
}


def run(client, cfg, task: str, dry_run: bool = False) -> dict:
    from google.cloud import bigquery
    build, location, table = TASKS[task]
    sql = build(client.project, cfg)
    mx = int(getattr(cfg, "BQ_COMPACT_MAX_BYTES", 1024 ** 3))
    est = int(client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
                           location=location).total_bytes_processed or 0)
    out = {"task": task, "estimated_bytes": est, "ran": False}
    if est > mx:
        out["refused"] = True
        return out
    ref = f"{client.project}.{table}"
    before = client.get_table(ref)
    out.update(rows_before=before.num_rows, bytes_before=before.num_bytes)
    if dry_run:
        return out
    job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=mx, use_query_cache=False),
                       location=location)
    job.result()
    after = client.get_table(ref)
    out.update(ran=True, billed_bytes=int(job.total_bytes_billed or 0), rows_after=after.num_rows,
               bytes_after=after.num_bytes)
    return out


def _gb(n) -> str:
    return f"{(n or 0) / 1e9:.3f}GB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BigQuery の重複・読み手のいない行を消す")
    ap.add_argument("task", choices=sorted(TASKS) + ["price_monthly"])
    ap.add_argument("--dry-run", action="store_true", help="見積もりだけ（表は書き換えない）")
    ap.add_argument("--keep-months", type=int, default=None, help="price_monthly：日足を残す月数（下限あり）")
    a = ap.parse_args(argv)
    import config as cfg
    import state_store
    t0 = time.time()
    if a.task == "price_monthly":
        try:
            r = run_price_monthly(state_store.make_client(cfg), cfg, a.dry_run, keep_months=a.keep_months)
        except PriceMonthlyError as e:
            print(f"❌ bq_compact price_monthly: {e}")
            return 1
        seam = r["seam"]
        tag = "（dry-run）" if a.dry_run else ""
        print(f"✅ bq_compact price_monthly{tag}: 境目 {r['boundary']}・畳む対象の月 {len(r['pending'])}（{', '.join(r['pending'][:6])}）"
              f"・今回畳んだ月 {', '.join(r['folded']) or 'なし'}・境目の基準を直した銘柄 {len(seam['fixed'])}"
              f"・見送り {len(seam['skipped'])}・課金 {r['billed_bytes'] / 1024 ** 2:.1f}MB・{time.time() - t0:.0f}秒")
        if seam.get("refused"):
            print("⚠️ 境目のずれた銘柄が多すぎるため月足の基準は直していない（判定を確かめる）")
            return 1
        return 0
    r = run(state_store.make_client(cfg), cfg, a.task, a.dry_run)
    mb = f"{r['estimated_bytes'] / 1024 ** 2:.1f}MB"
    if r.get("refused"):
        print(f"❌ bq_compact {a.task}: 見積もり {mb} が上限を超えるため実行しない")
        return 1
    if not r["ran"]:
        print(f"✅ bq_compact {a.task}（dry-run）: 見積もり {mb}・今 {r['rows_before']:,}行・{_gb(r['bytes_before'])}")
        return 0
    print(f"✅ bq_compact {a.task}: {r['rows_before']:,}行→{r['rows_after']:,}行・{_gb(r['bytes_before'])}→{_gb(r['bytes_after'])}"
          f"（課金 {r['billed_bytes'] / 1024 ** 2:.1f}MB・{time.time() - t0:.0f}秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
