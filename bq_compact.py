# ==============================================================
# bq_compact.py — BigQuery の保存量を減らす（重複・読み手のいない行を消す）
# ==============================================================
# 保存量の課金は論理サイズ（bq_storage.py）。load job の追記や遡りの取り込みで溜まった次のものを消す。
#   price_dedupe  analytics.price_daily … (ticker, trade_date) の重複を最新の1行にして作り直す。
#                 読み手の analytics.price_daily_latest は同じ「ingested_at が新しい1行」を採るので、見える値は変わらない。
#                 パーティション（trade_date）・クラスタ（ticker）・NOT NULL は元のまま。
#   sec_forms     us.sec_filings … SEC_FILINGS_KEEP_FORMS 以外の書類（Form 4 など）の行を消す。
#   gov_facts     edinet.gov_doc_facts … 大量保有（350/360/370）の、large_holdings view が引かない要素の行を消す。
# 【費用】どれも dryRun で読むバイト数を見積もり、BQ_COMPACT_MAX_BYTES（1GiB）を超えたら実行しない。
#   実行時も maximum_bytes_billed で止める。ここに書く処理は BQ の表を書き換える。--dry-run は見積もりだけ。
# 【実行】python3 bq_compact.py {price_dedupe|sec_forms|gov_facts} [--dry-run]
# ==============================================================
from __future__ import annotations

import argparse
import sys
import time

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
    ap.add_argument("task", choices=sorted(TASKS))
    ap.add_argument("--dry-run", action="store_true", help="見積もりだけ（表は書き換えない）")
    a = ap.parse_args(argv)
    import config as cfg
    import state_store
    t0 = time.time()
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
