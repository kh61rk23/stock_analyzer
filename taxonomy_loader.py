# ==============================================================
# taxonomy_loader.py — T-1後半: taxonomy_node BQローダ
# ==============================================================
# 【役割】data/taxonomy_seed/batch*.json のうち **承認済み**
#   （_meta.status が APPROVED で始まる）バッチのノードを
#   BQ taxonomy.taxonomy_node へ冪等投入する（node_id で重複排除=MERGE）。
# 【原則】
#   - DRAFTバッチは投入しない（人間承認=verified_flag が正本。HANDOFF_2026-06-30 §4-7）
#   - --dry-run 必須運用: まず件数とMERGE対象を確認してから実行
#   - BQ未接続/失敗でも exit 0（ローカル正本・外部はベストエフォート）
# 使い方（リポジトリ直下・要ネットワーク+BQ_CREDENTIALS）:
#   python3 taxonomy_loader.py --dry-run     # 投入対象の確認のみ
#   python3 taxonomy_loader.py               # MERGE実行
# ==============================================================

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SEED_DIR = REPO / "data" / "taxonomy_seed"


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def load_seed_batches(seed_dir: Path = SEED_DIR) -> list[dict]:
    """batch*.json を番号順に読み込む（壊れたJSONはスキップ・警告）。"""
    batches = []
    for p in sorted(seed_dir.glob("batch*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d["_file"] = p.name
            batches.append(d)
        except Exception as e:
            print(f"  ⚠️ {p.name} 読込失敗: {str(e)[:60]}")
    return batches


def select_approved_nodes(batches: list[dict]) -> tuple[list[dict], list[str]]:
    """
    承認済みバッチ（_meta.status が 'APPROVED' 始まり）のノードのみ抽出。
    Returns: (nodes, skipped_batch_names)
    """
    nodes, skipped = [], []
    for b in batches:
        status = str((b.get("_meta") or {}).get("status", ""))
        if status.startswith("APPROVED"):
            for n in b.get("nodes", []):
                n = dict(n)
                n["_batch"] = b["_file"]
                n["_taxonomy_version"] = (b.get("_meta") or {}).get(
                    "taxonomy_version", "v1.0")
                nodes.append(n)
        else:
            skipped.append(f"{b.get('_file', '?')}（{status or '不明'}）")
    return nodes, skipped


def validate_nodes(nodes: list[dict]) -> list[str]:
    """投入前検証: node_id重複・孤立親・必須フィールド。エラー文のリストを返す。"""
    errors = []
    ids = {}
    for n in nodes:
        nid = n.get("node_id")
        if not nid:
            errors.append(f"node_id欠損: {str(n)[:60]}")
            continue
        if nid in ids:
            errors.append(f"node_id重複: {nid}（{ids[nid]} と {n.get('_batch')}）")
        ids[nid] = n.get("_batch")
        if n.get("node_level") is None or not n.get("node_name_ja"):
            errors.append(f"必須フィールド欠損: {nid}")
    idset = set(ids)
    for n in nodes:
        pid = n.get("parent_id")
        if pid and pid not in idset:
            errors.append(f"孤立親: {n.get('node_id')} → {pid}（承認済みバッチ内に親なし）")
    return errors


def build_bq_rows(nodes: list[dict], as_of: str = None) -> list[dict]:
    """taxonomy.taxonomy_node の実スキーマ（bq_all_schemas_20260704.json）に整形。"""
    as_of = as_of or date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for n in nodes:
        rows.append({
            "node_id":             n["node_id"],
            "parent_id":           n.get("parent_id"),
            "node_name_ja":        n.get("node_name_ja"),
            "node_name_en":        n.get("node_name_en"),
            "node_level":          int(n.get("node_level", 0)),
            "node_type":           n.get("node_type"),
            "description":         n.get("description"),
            "taxonomy_version":    n.get("_taxonomy_version", "v1.0"),
            "effective_from":      as_of,
            "effective_to":        None,
            "is_active":           bool(n.get("is_active", True)),
            "supply_chain_pos":    n.get("supply_chain_pos"),
            "related_commodities": json.dumps(n.get("related_commodities") or [],
                                              ensure_ascii=False),
            "related_countries":   json.dumps(n.get("related_countries") or [],
                                              ensure_ascii=False),
            "notes":               f"seed={n.get('_batch', '?')}",
            "updated_at":          now,
        })
    return rows


def build_merge_sql(project: str, dataset: str = "taxonomy") -> str:
    """staging → 本テーブルの MERGE 文（node_id キーで冪等）。"""
    t = f"`{project}.{dataset}.taxonomy_node`"
    s = f"`{project}.{dataset}._staging_taxonomy_node`"
    return f"""
MERGE {t} T
USING {s} S
ON T.node_id = S.node_id
WHEN MATCHED THEN UPDATE SET
  parent_id = S.parent_id, node_name_ja = S.node_name_ja,
  node_name_en = S.node_name_en, node_level = S.node_level,
  node_type = S.node_type, description = S.description,
  taxonomy_version = S.taxonomy_version, is_active = S.is_active,
  supply_chain_pos = S.supply_chain_pos,
  related_commodities = S.related_commodities,
  related_countries = S.related_countries,
  notes = S.notes, updated_at = S.updated_at
WHEN NOT MATCHED THEN INSERT ROW
"""


# ── I/O層（BQ） ──────────────────────────────────────────────

def load_to_bq(rows: list[dict], cfg) -> tuple[bool, int]:
    """staging表に投入→MERGE→staging削除。失敗しても例外を伝播しない。"""
    if not rows:
        return True, 0
    try:
        from google.cloud import bigquery
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
        # configに属性があっても空文字の場合がある。認証JSONから解決済みの
        # client.projectを正本にし、`.taxonomy...` という壊れたtable idを防ぐ。
        project = (
            str(getattr(cfg, "BQ_PROJECT_ID", "") or "").strip()
            or bq.project
        )
        staging = f"{project}.taxonomy._staging_taxonomy_node"

        # staging はJSON列を含むため本テーブルの LIKE で作る（冪等）
        bq.client.query(
            f"CREATE OR REPLACE TABLE `{staging}` "
            f"LIKE `{project}.taxonomy.taxonomy_node`").result()
        job_config = bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        )
        import io
        buf = io.BytesIO(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
            .encode("utf-8"))
        bq.client.load_table_from_file(buf, staging,
                                       job_config=job_config).result()
        bq.client.query(build_merge_sql(project)).result()
        bq.client.query(f"DROP TABLE IF EXISTS `{staging}`").result()
        return True, len(rows)
    except Exception as e:
        print(f"  ⚠️ BQ投入失敗（ローカル正本は維持）: {str(e)[:100]}")
        return False, 0


def main():
    dry = "--dry-run" in sys.argv
    try:
        import config as cfg
    except Exception:
        cfg = None

    # 定期実行（週次）では有効フラグで止められる。
    # --dry-run は検証用途なのでフラグに関わらず通す。
    if not dry and not getattr(cfg, "ENABLE_TAXONOMY_LOADER", True):
        print("⏩ taxonomy_loader: スキップ（ENABLE_TAXONOMY_LOADER=False）")
        return

    batches = load_seed_batches()
    nodes, skipped = select_approved_nodes(batches)
    print(f"📦 seedバッチ: {len(batches)}件 / 承認済みノード: {len(nodes)}件")
    for s in skipped:
        print(f"  ⏩ 未承認のためスキップ: {s}")

    errors = validate_nodes(nodes)
    if errors:
        print(f"❌ 投入前検証エラー {len(errors)}件（投入中止）:")
        for e in errors[:10]:
            print(f"   - {e}")
        sys.exit(1)

    rows = build_bq_rows(nodes)
    if dry:
        print(f"✅ --dry-run: MERGE対象 {len(rows)}行（taxonomy.taxonomy_node・node_idキー）")
        for r in rows[:5]:
            print(f"   {r['node_id']} (L{r['node_level']} {r['node_type']}) "
                  f"active={r['is_active']}")
        if len(rows) > 5:
            print(f"   ... 他{len(rows) - 5}行")
        return

    ok, n = load_to_bq(rows, cfg)
    print(f"{'✅ MERGE完了' if ok else '⚠️ 投入失敗（exit 0）'}: {n}行")


if __name__ == "__main__":
    main()
