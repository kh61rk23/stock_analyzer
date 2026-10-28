"""
master_tables.py — マスタの表を BigQuery の master データセットにまとめる（普通の表＝日々たまる表と分ける）

【置き場】
  master（asia-northeast1）… 日本側のマスタ。tax_node（技術ノードの全段・書き込み先）と、段ごとのマスタ
    tax_node_l1（大分類）・tax_node_l2（中分類）・tax_node_l3（小分類）・tax_node_l4（細分類）、
    tax_level（段階の定義）・industry_tse33（東証33業種）
  master_us（US）… 米国側のマスタ（us データセットと同じリージョンでないと view で読めないため分ける）。順に移す
  元の場所の名前（taxonomy.taxonomy_node など）は、同じ列を返す view にして残す（読み手のコードは変えなくてよい）。

【技術ノードの段階】大分類（1）→ 中分類（2）→ 小分類（3）→ 細分類（4）。tax_node は各ノードに、上の段の
  ノード（l1〜l3 の ID と名前）と段階の名前を持つ。中分類までしかないノードは小分類が空。

【書き手】
  tax_node … taxonomy_loader（承認済み seed を MERGE。元の taxonomy.taxonomy_node は view）。続けて段ごとのマスタを作り直す
  tax_level・industry_tse33 … このモジュールの sync_static（config の値から作り直す・load job で無料）
"""
from __future__ import annotations

import io
import json
import sys

DATASET = "master"
DATASET_US = "master_us"
LOCATION = "asia-northeast1"
LOCATION_US = "US"

LEVELS = [
    {"level": 1, "name_ja": "大分類", "name_en": "Domain", "description": "技術・産業の大きなまとまり（半導体・金融など）"},
    {"level": 2, "name_ja": "中分類", "name_en": "Category", "description": "大分類の中の分野（半導体設計・銀行など）"},
    {"level": 3, "name_ja": "小分類", "name_en": "Subcategory", "description": "中分類の中の製品・事業（ロジック・地方銀行など）"},
    {"level": 4, "name_ja": "細分類", "name_en": "Detail", "description": "小分類をさらに分けたもの（データセンター向け AI チップなど）"},
]
LEVEL_NAME = {x["level"]: x["name_ja"] for x in LEVELS}

# taxonomy.taxonomy_node と同じ列（元の名前の view はこの列だけを返す）
NODE_COLUMNS = [
    ("node_id", "STRING", "REQUIRED"), ("parent_id", "STRING", "NULLABLE"), ("node_name_ja", "STRING", "NULLABLE"),
    ("node_name_en", "STRING", "NULLABLE"), ("node_level", "INT64", "NULLABLE"), ("node_type", "STRING", "NULLABLE"),
    ("description", "STRING", "NULLABLE"), ("taxonomy_version", "STRING", "NULLABLE"), ("effective_from", "DATE", "NULLABLE"),
    ("effective_to", "DATE", "NULLABLE"), ("is_active", "BOOL", "NULLABLE"), ("supply_chain_pos", "STRING", "NULLABLE"),
    ("related_commodities", "JSON", "NULLABLE"), ("related_countries", "JSON", "NULLABLE"), ("notes", "STRING", "NULLABLE"),
    ("updated_at", "TIMESTAMP", "NULLABLE"),
]
PATH_COLUMNS = [
    ("level_name_ja", "STRING", "NULLABLE"),
    ("l1_id", "STRING", "NULLABLE"), ("l1_name_ja", "STRING", "NULLABLE"),
    ("l2_id", "STRING", "NULLABLE"), ("l2_name_ja", "STRING", "NULLABLE"),
    ("l3_id", "STRING", "NULLABLE"), ("l3_name_ja", "STRING", "NULLABLE"),
    ("path_name_ja", "STRING", "NULLABLE"),
]
NODE_TABLE = "tax_node"


# ── 純関数 ───────────────────────────────────────────────────

def node_paths(nodes: list[dict]) -> dict:
    """{node_id: {level_name_ja, l1_id, l1_name_ja, …, path_name_ja}}。親をたどって上の段を埋める。
    親が見つからない・循環しているノードは、たどれたところまで。"""
    by = {n["node_id"]: n for n in nodes}
    out = {}
    for nid, n in by.items():
        chain, seen, cur = [], set(), n
        while cur is not None and cur["node_id"] not in seen:
            seen.add(cur["node_id"])
            chain.append(cur)
            cur = by.get(cur.get("parent_id"))
        chain.reverse()                       # 大分類から順に
        row = {"level_name_ja": LEVEL_NAME.get(int(n.get("node_level") or 0))}
        for k in (1, 2, 3):
            hit = next((c for c in chain if int(c.get("node_level") or 0) == k), None)
            row[f"l{k}_id"] = hit["node_id"] if hit else None
            row[f"l{k}_name_ja"] = hit.get("node_name_ja") if hit else None
        row["path_name_ja"] = " > ".join(str(c.get("node_name_ja") or c["node_id"]) for c in chain)
        out[nid] = row
    return out


def industry_rows(japan_sectors: dict) -> list[dict]:
    return [{"sector_code": c, "name_ja": v.get("name_ja"), "name_en": v.get("name_en"), "group_ja": v.get("group")}
            for c, v in sorted((japan_sectors or {}).items())]


LEVEL_TABLES = {1: ("tax_node_l1", "大分類マスタ"), 2: ("tax_node_l2", "中分類マスタ"),
                3: ("tax_node_l3", "小分類マスタ"), 4: ("tax_node_l4", "細分類マスタ")}


def level_table_sql(project: str, level: int) -> str:
    """段ごとのマスタ（大分類・中分類・小分類・細分類）を tax_node から作り直す SQL。各段は上の段の ID を持つ。
    子の数（下の段のノード数）も持つ。"""
    table, _label = LEVEL_TABLES[level]
    uppers = ", ".join(f"l{k}_id" for k in range(level - 1, 0, -1))
    child = (f"(SELECT COUNT(*) FROM `{project}.{DATASET}.{NODE_TABLE}` c WHERE c.parent_id = n.node_id AND c.node_level = {level + 1})"
             if level < 4 else "0")
    return f"""
CREATE OR REPLACE TABLE `{project}.{DATASET}.{table}`
OPTIONS (description = '{LEVEL_TABLES[level][1]}（技術ノードの第{level}段・master_tables.py が tax_node から作る）') AS
SELECT n.node_id AS l{level}_id{', ' + uppers if uppers else ''}, n.node_name_ja AS name_ja, n.node_name_en AS name_en,
       n.description, n.supply_chain_pos, n.related_commodities, n.related_countries, n.is_active, n.path_name_ja,
       {child} AS child_count, n.updated_at
FROM `{project}.{DATASET}.{NODE_TABLE}` n
WHERE n.node_level = {level}
"""


def rebuild_level_tables(client) -> dict:
    """段ごとのマスタを作り直す（4本のクエリ・それぞれ最低10MB の課金）。"""
    out = {}
    for level, (table, _label) in LEVEL_TABLES.items():
        client.query(level_table_sql(client.project, level), location=LOCATION).result()
        out[table] = client.get_table(f"{client.project}.{DATASET}.{table}").num_rows
    return out


def legacy_view_sql(project: str) -> str:
    cols = ", ".join(n for n, _t, _m in NODE_COLUMNS)
    return f"CREATE OR REPLACE VIEW `{project}.taxonomy.taxonomy_node` AS SELECT {cols} FROM `{project}.{DATASET}.{NODE_TABLE}`"


# ── I/O ──────────────────────────────────────────────────────

def _bq():
    from google.cloud import bigquery
    return bigquery


def ensure_datasets(client, project: str | None = None) -> None:
    bq = _bq()
    for name, loc in ((DATASET, LOCATION), (DATASET_US, LOCATION_US)):
        ds = bq.Dataset(f"{project or client.project}.{name}")
        ds.location = loc
        ds.description = "マスタの表（日々たまる表とは分ける）。master_tables.py"
        client.create_dataset(ds, exists_ok=True)


def node_schema():
    bq = _bq()
    return [bq.SchemaField(n, t, mode=m) for n, t, m in NODE_COLUMNS + PATH_COLUMNS]


def _load(client, table: str, rows: list[dict], schema) -> int:
    bq = _bq()
    buf = io.BytesIO("\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows).encode("utf-8"))
    job = client.load_table_from_file(buf, f"{client.project}.{DATASET}.{table}", location=LOCATION,
                                      job_config=bq.LoadJobConfig(
                                          schema=schema, source_format=bq.SourceFormat.NEWLINE_DELIMITED_JSON,
                                          write_disposition=bq.WriteDisposition.WRITE_TRUNCATE))
    job.result()
    return len(rows)


def sync_static(cfg, client=None) -> dict:
    """段階と東証33業種のマスタを config から作り直す（load job・無料）。"""
    bq = _bq()
    if client is None:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
    ensure_datasets(client)
    S = bq.SchemaField
    n1 = _load(client, "tax_level", LEVELS, [S("level", "INT64", mode="REQUIRED"), S("name_ja", "STRING"),
                                              S("name_en", "STRING"), S("description", "STRING")])
    n2 = _load(client, "industry_tse33", industry_rows(getattr(cfg, "JAPAN_SECTORS", {})),
               [S("sector_code", "STRING", mode="REQUIRED"), S("name_ja", "STRING"), S("name_en", "STRING"),
                S("group_ja", "STRING")])
    return {"tax_level": n1, "industry_tse33": n2}


def migrate_taxonomy_node(client) -> dict:
    """一度だけ：taxonomy.taxonomy_node（表）を master.tax_node に写し（段階の列を足す）、元の名前を view にする。
    すでに view なら何もしない。"""
    from google.api_core.exceptions import NotFound
    ensure_datasets(client)
    p = client.project
    try:
        old = client.get_table(f"{p}.taxonomy.taxonomy_node")
    except NotFound:
        old = None
    if old is not None and old.table_type == "VIEW":
        return {"skipped": "already view"}
    rows = [dict(r.items()) for r in client.list_rows(f"{p}.taxonomy.taxonomy_node")] if old is not None else []
    paths = node_paths(rows)
    out = []
    for r in rows:
        r = {k: r.get(k) for k, _t, _m in NODE_COLUMNS}
        for k in ("related_commodities", "related_countries"):
            if r.get(k) is not None and not isinstance(r[k], str):
                r[k] = json.dumps(r[k], ensure_ascii=False)
        out.append({**r, **paths.get(r["node_id"], {})})
    n = _load(client, NODE_TABLE, out, node_schema())
    if old is not None:
        client.delete_table(f"{p}.taxonomy.taxonomy_node")
    client.query(legacy_view_sql(p), location=LOCATION).result()
    return {"copied": n}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--migrate", action="store_true", help="taxonomy.taxonomy_node を master.tax_node へ移す（一度だけ）")
    a = ap.parse_args(argv)
    import config as cfg
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    if a.migrate:
        print(f"✅ migrate: {migrate_taxonomy_node(client)}")
    print(f"✅ master_tables: {sync_static(cfg, client)}")
    print(f"✅ 段ごとのノードマスタ: {rebuild_level_tables(client)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
