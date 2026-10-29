# ==============================================================
# us_taxonomy_mapper.py — 米国上場会社に技術マップのノードを付ける（us.company_taxonomy_map）
# ==============================================================
# 【入力】us.company_profile の SIC 業種コード（SEC の会社情報。米国の取り込み対象の全社にある）。
#   対応は data/taxonomy_us_sic_map.json：4桁のコード → ノード、無ければ上2桁 → ノード。
# 【出力】us.company_taxonomy_map（location=US。us の表と JOIN できるよう日本側の taxonomy とは分ける）。
#   列は日本の taxonomy.company_taxonomy_map と同じ。company_id は米国のティッカー、confidence_type='us_sic'。
#   毎回作り直す（load job の WRITE_TRUNCATE・無料）。
# 【読む量】company_profile をまとめて1本（約 5MB・最低課金 10MB）。
# 【使い方】python us_taxonomy_mapper.py [--dry]
# ==============================================================
from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

MAP_PATH = Path(__file__).parent / "data" / "taxonomy_us_sic_map.json"
TABLE = "us.company_taxonomy_map"
CONFIDENCE_TYPE = "us_sic"
CONFIDENCE = 0.5          # 業種コードだけの当たり。事業の中身を読んだ当たりより低くする

SQL = """
SELECT ticker, cik, sic, sic_description
FROM `{p}.us.company_profile`
WHERE ticker IS NOT NULL AND sic IS NOT NULL
"""

COLUMNS = [
    ("map_id", "STRING"), ("company_id", "STRING"), ("node_id", "STRING"), ("relevance_score", "FLOAT"),
    ("confidence", "FLOAT"), ("revenue_share_est", "FLOAT"), ("confidence_type", "STRING"),
    ("evidence_source", "STRING"), ("evidence_url", "STRING"), ("rationale", "STRING"), ("valid_from", "DATE"),
    ("valid_to", "DATE"), ("last_verified_at", "TIMESTAMP"), ("auto_derived", "BOOLEAN"),
    ("verified_flag", "BOOLEAN"), ("updated_at", "TIMESTAMP"),
]


def load_map(path: Path = MAP_PATH) -> tuple[dict, dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return d.get("sic_to_node") or {}, d.get("major_to_node") or {}


def node_for_sic(sic, by_code: dict, by_major: dict) -> str | None:
    s = str(sic or "").strip().zfill(4)
    if not s.isdigit() or s == "0000":
        return None
    return by_code.get(s) or by_major.get(s[:2])


def build_rows(profiles: list[dict], by_code: dict, by_major: dict, known_nodes: set | None = None,
               as_of: str | None = None) -> list[dict]:
    """company_profile の行 → 対応表の行（純関数）。1社1ノード・関与度 1.0。"""
    as_of = as_of or date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    out, seen = [], set()
    for p in profiles:
        t = str(p.get("ticker") or "").strip()
        nid = node_for_sic(p.get("sic"), by_code, by_major)
        if not t or not nid or t in seen or (known_nodes is not None and nid not in known_nodes):
            continue
        seen.add(t)
        out.append({
            "map_id": f"{t}:{nid}:{CONFIDENCE_TYPE}", "company_id": t, "node_id": nid, "relevance_score": 1.0,
            "confidence": CONFIDENCE, "revenue_share_est": None, "confidence_type": CONFIDENCE_TYPE,
            "evidence_source": f"sec_sic:{str(p.get('sic')).zfill(4)}",
            "evidence_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={p.get('cik')}",
            "rationale": f"SIC {str(p.get('sic')).zfill(4)} {p.get('sic_description') or ''}"[:500],
            "valid_from": as_of, "valid_to": None, "last_verified_at": None, "auto_derived": True,
            "verified_flag": False, "updated_at": now,
        })
    return out


def write(client, rows: list[dict]) -> int:
    from google.cloud import bigquery
    schema = [bigquery.SchemaField(n, t) for n, t in COLUMNS]
    buf = io.BytesIO("\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows).encode("utf-8"))
    client.load_table_from_file(buf, f"{client.project}.{TABLE}", location="US", job_config=bigquery.LoadJobConfig(
        schema=schema, source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)).result()
    return len(rows)


def run(cfg=None, dry: bool = False, client=None) -> dict:
    from taxonomy_loader import load_seed_batches, select_approved_nodes
    if client is None:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
    known = {n["node_id"] for n in select_approved_nodes(load_seed_batches())[0]}
    profiles = [dict(r.items()) for r in client.query(SQL.format(p=client.project), location="US").result()]
    by_code, by_major = load_map()
    rows = build_rows(profiles, by_code, by_major, known)
    n = 0 if dry else write(client, rows)
    return {"companies": len(profiles), "tagged": len(rows), "nodes": len({r["node_id"] for r in rows}), "written": n}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    print(f"✅ us_taxonomy_mapper: {run(dry=a.dry)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
