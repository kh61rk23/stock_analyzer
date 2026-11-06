# ==============================================================
# bq_storage.py — BigQuery の保存量（論理サイズ）を測り、上限を超えていないかを見る
# ==============================================================
# 無料枠は保存 10GB/月で、その月に保存している合計にかかる（溜めた分は毎月数え直される）。
# 全データセットは論理サイズの課金（既定）なので、入れ替えた古い版（タイムトラベル）は数に入らない。
# データを溜める取り込みは、書く前に allowed() を見て、BQ_STORAGE_STOP_GB を超えていたら書かない。
# 測った値は stock_data/logs/bq_storage.json に残し、日次メールの「運用の監視」（ops_health）が読む。
# メタデータを読むだけなので課金されない（表ごとに get_table を1回）。
# ==============================================================
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

STATUS_FILE = "bq_storage.json"


def measure(client) -> dict:
    """{'total_gb', 'tables': {dataset.table: GB}}（大きい順）。"""
    tables = {}
    for ds in client.list_datasets():
        for t in client.list_tables(ds.reference):
            tables[f"{ds.dataset_id}.{t.table_id}"] = (client.get_table(t.reference).num_bytes or 0) / 1e9
    tables = dict(sorted(tables.items(), key=lambda kv: -kv[1]))
    return {"total_gb": round(sum(tables.values()), 3), "tables": {k: round(v, 4) for k, v in tables.items()}}


def record(cfg, m: dict) -> None:
    p = Path(getattr(cfg, "LOG_DIR", "stock_data/logs")) / STATUS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    top = dict(list(m["tables"].items())[:10])
    p.write_text(json.dumps({"measured_at": datetime.now().isoformat(timespec="seconds"),
                             "total_gb": m["total_gb"], "top_tables": top}, ensure_ascii=False, indent=1),
                 encoding="utf-8")


def allowed(cfg, m: dict) -> bool:
    return m["total_gb"] < float(getattr(cfg, "BQ_STORAGE_STOP_GB", 8.0))


# ── 月のクエリ使用量（月次メール用） ──────────────────────────────────────
# INFORMATION_SCHEMA.JOBS_BY_PROJECT は1か月ぶんでリージョンごとに約 0.17GB を読む（dryRun の実測）。月次メールの1回だけ引く。
# ontology と us は location=US なので、2つのリージョンを1本ずつ引く
USAGE_REGIONS = (("asia-northeast1", "region-asia-northeast1"), ("US", "region-us"))
_USAGE_MAX_BYTES = 300 * 1024 ** 2


def month_bounds(ym: str) -> tuple[str, str]:
    """YYYYMM → (月初, 翌月初) の ISO 日付。"""
    y, m = int(ym[:4]), int(ym[4:])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(y, m, 1).isoformat(), date(ny, nm, 1).isoformat()


def measure_usage(client, ym: str) -> dict:
    """対象月のクエリ使用量。{'jobs','billed_gib','max_day','max_day_gib','regions_ok'}。
    リージョンごとの失敗は regions_ok から外すだけで、読めた分を返す。"""
    from google.cloud import bigquery
    a, b = month_bounds(ym)
    per_day: dict = {}
    jobs, ok = 0, []
    for loc, view in USAGE_REGIONS:
        sql = (f"SELECT DATE(creation_time, 'Asia/Tokyo') AS d, COUNT(*) AS jobs, SUM(total_bytes_billed) AS billed "
               f"FROM `{client.project}`.`{view}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT "
               f"WHERE creation_time >= TIMESTAMP(@a, 'Asia/Tokyo') AND creation_time < TIMESTAMP(@b, 'Asia/Tokyo') "
               f"AND job_type = 'QUERY' AND (statement_type IS NULL OR statement_type != 'SCRIPT') GROUP BY d")
        jc = bigquery.QueryJobConfig(maximum_bytes_billed=_USAGE_MAX_BYTES, query_parameters=[
            bigquery.ScalarQueryParameter("a", "STRING", a), bigquery.ScalarQueryParameter("b", "STRING", b)])
        try:
            for r in client.query(sql, job_config=jc, location=loc).result():
                jobs += int(r["jobs"] or 0)
                per_day[str(r["d"])] = per_day.get(str(r["d"]), 0) + int(r["billed"] or 0)
            ok.append(loc)
        except Exception:
            continue
    top = max(per_day.items(), key=lambda kv: kv[1]) if per_day else ("", 0)
    return {"jobs": jobs, "billed_gib": sum(per_day.values()) / 1024 ** 3,
            "max_day": top[0], "max_day_gib": top[1] / 1024 ** 3, "regions_ok": ok}


def usage_line(ym: str, usage: dict | None, storage: dict | None, cfg=None) -> str:
    """月次メールの運用の1行（純関数）。枠の種類（クエリ／保存）・期間・使用量を毎回明示する。"""
    free_q = float(getattr(cfg, "BQ_QUERY_FREE_GIB_PER_MONTH", 1024.0))
    cap_d = float(getattr(cfg, "BQ_QUERY_DAILY_CAP_GIB", 30.7))
    free_s = float(getattr(cfg, "BQ_STORAGE_FREE_GB", 10.0))
    parts = []
    if usage and usage.get("regions_ok"):
        g = usage["billed_gib"]
        part = (f"クエリ（{int(ym[4:])}月・月1TiBの無料枠）{usage['jobs']:,}本・課金 {g:,.1f}GiB（枠の {g / free_q:.1%}）"
                f"・1日の最大 {usage['max_day_gib']:,.1f}GiB（{usage['max_day'][5:]}・上限 {cap_d:g}GiB/日の {usage['max_day_gib'] / cap_d:.0%}）")
        if len(usage["regions_ok"]) < len(USAGE_REGIONS):
            part += "（一部のリージョンは取れず）"
        parts.append(part)
    else:
        parts.append(f"クエリの使用量は取得できず（{int(ym[4:])}月）")
    if storage and storage.get("total_gb") is not None:
        t = float(storage["total_gb"])
        parts.append(f"保存（無料枠 {free_s:g}GB・{str(storage.get('measured_at', ''))[:10]}時点）{t:,.2f}GB（枠の {t / free_s:.0%}）")
    else:
        parts.append("保存量は未測定")
    return "BigQuery: " + " ／ ".join(parts)
