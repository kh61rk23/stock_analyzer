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
from datetime import datetime
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
