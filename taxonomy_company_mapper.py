# ==============================================================
# taxonomy_company_mapper.py — T-2A: EDINETセグメント上書き（Wave R2-3）
# ==============================================================
# 【役割】edinet.segments（＋segment_finance の売上比率）を segment_mapper
#   （sentence-transformers→Gemini fallback）で33業種に写像し、
#   size_tier_classifier の 業種→taxonomy_node 橋渡しを経由して
#   taxonomy.company_taxonomy_map に **企業レベル** の行を書き込む。
#   confidence_type="edinet"（confidence=0.7）で、cluster_derived の
#   draft層（sector擬似企業・confidence=0.3）を企業単位で上書きする（決定2: C→A）。
# 【前提】EDINETセグメントの蓄積（R0-1のEDINET安定化）。データが空でも
#   安全にスキップし exit 0（次回蓄積後に再実行すればよい）。
# 使い方: python3 taxonomy_company_mapper.py --dry-run / （本実行）
# ==============================================================

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── 純粋ロジック（pytest対象・mapper/橋渡しは注入可能） ────────

def build_company_node_rows(
    segments_by_company: dict,
    map_segment_fn,
    sector_to_nodes_fn,
    as_of: str = None,
) -> list[dict]:
    """
    企業×セグメント → 企業×taxonomy_node の行を構築する。

    Args:
        segments_by_company: {company_code: [{"segment_name": str, "share": float}]}
                             share は売上比率（欠損時は等分）
        map_segment_fn:      segment_name -> {"sector_code": str, "similarity_score": float} | None
        sector_to_nodes_fn:  sector_code -> [node_id, ...]
    Returns:
        taxonomy.company_taxonomy_map 実スキーマの行リスト
    """
    as_of = as_of or date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for company, segments in segments_by_company.items():
        if not segments:
            continue
        # share欠損は等分
        n_missing = sum(1 for s in segments if not s.get("share"))
        known = sum(float(s.get("share") or 0) for s in segments)
        default_share = max(0.0, (1.0 - known)) / n_missing if n_missing else 0.0

        node_score: dict[str, float] = {}
        node_share: dict[str, float] = {}
        rationale: dict[str, list] = {}
        for seg in segments:
            name = (seg.get("segment_name") or "").strip()
            if not name:
                continue
            share = float(seg.get("share") or default_share)
            mapped = map_segment_fn(name)
            if not mapped or not mapped.get("sector_code"):
                continue
            sim = float(mapped.get("similarity_score", 0.5) or 0.5)
            for node_id in (sector_to_nodes_fn(mapped["sector_code"]) or []):
                node_score[node_id] = node_score.get(node_id, 0.0) + share * sim
                node_share[node_id] = node_share.get(node_id, 0.0) + share
                rationale.setdefault(node_id, []).append(
                    f"{name}({share:.0%},sim={sim:.2f})")
        total = sum(node_score.values())
        if total <= 0:
            continue
        for node_id, sc in node_score.items():
            rows.append({
                "map_id":            f"{company}:{node_id}:{as_of}",
                "company_id":        str(company),
                "node_id":           node_id,
                "relevance_score":   round(sc / total, 4),
                "confidence":        0.7,
                "revenue_share_est": round(min(1.0, node_share[node_id]), 4),
                "confidence_type":   "edinet",
                "evidence_source":   "edinet_segments+segment_mapper",
                "rationale":         " / ".join(rationale[node_id])[:500],
                "valid_from":        as_of,
                "auto_derived":      True,
                "verified_flag":     False,
                "updated_at":        now,
            })
    return rows


# ── I/O層（BQ・segment_mapper） ──────────────────────────────

def fetch_segments_from_bq(cfg) -> dict:
    """edinet.segments × segment_finance(revenue_ratio) を企業別に取得。空なら {}。"""
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
        project = (
            str(getattr(cfg, "BQ_PROJECT_ID", "") or "").strip()
            or bq.project
        )
        sql = f"""
        SELECT s.company_code, s.segment_name,
               MAX(f.revenue_ratio) AS revenue_ratio
        FROM `{project}.edinet.segments` s
        LEFT JOIN `{project}.edinet.segment_finance` f
          ON f.segment_id = s.segment_id
        WHERE s.segment_name IS NOT NULL AND s.segment_name != ''
        GROUP BY s.company_code, s.segment_name
        """
        df = bq.query_to_df(sql)
        out: dict = {}
        for _, r in df.iterrows():
            share = r.get("revenue_ratio")
            out.setdefault(str(r["company_code"]), []).append({
                "segment_name": str(r["segment_name"]),
                "share": float(share) if share is not None and share == share else None,
            })
        return out
    except Exception as e:
        print(f"  ⚠️ segments取得失敗（EDINET蓄積待ちの可能性）: {str(e)[:80]}")
        return {}


def upsert_to_bq(rows: list[dict], cfg) -> tuple[bool, int]:
    """staging→MERGE（キー: company_id+node_id）。edinet層の冪等上書き。"""
    if not rows:
        return True, 0
    try:
        from google.cloud import bigquery
        from edinet_bq_client import EDINETBQClient
        import io
        bq = EDINETBQClient()
        project = (
            str(getattr(cfg, "BQ_PROJECT_ID", "") or "").strip()
            or bq.project
        )
        staging = f"{project}.taxonomy._staging_company_map"
        bq.client.query(
            f"CREATE OR REPLACE TABLE `{staging}` "
            f"LIKE `{project}.taxonomy.company_taxonomy_map`").result()
        job_config = bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON)
        buf = io.BytesIO("\n".join(
            json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8"))
        bq.client.load_table_from_file(buf, staging, job_config=job_config).result()
        bq.client.query(f"""
        MERGE `{project}.taxonomy.company_taxonomy_map` T
        USING `{staging}` S
        ON T.company_id = S.company_id AND T.node_id = S.node_id
           AND T.confidence_type = 'edinet'
        WHEN MATCHED THEN UPDATE SET
          relevance_score = S.relevance_score, confidence = S.confidence,
          revenue_share_est = S.revenue_share_est, rationale = S.rationale,
          valid_from = S.valid_from, updated_at = S.updated_at
        WHEN NOT MATCHED THEN INSERT ROW
        """).result()
        bq.client.query(f"DROP TABLE IF EXISTS `{staging}`").result()
        return True, len(rows)
    except Exception as e:
        print(f"  ⚠️ BQ upsert失敗: {str(e)[:100]}")
        return False, 0


def main():
    dry = "--dry-run" in sys.argv
    try:
        import config as cfg
    except Exception:
        cfg = None

    # 2026-07-25: 定期実行（週次）に載せたのに合わせて有効フラグを配線。
    if not dry and not getattr(cfg, "ENABLE_TAXONOMY_COMPANY_MAPPER", True):
        print("⏩ taxonomy_company_mapper: スキップ"
              "（ENABLE_TAXONOMY_COMPANY_MAPPER=False）")
        return

    segments = fetch_segments_from_bq(cfg)
    print(f"🏢 セグメント保有企業: {len(segments)}社")
    if not segments:
        print("  ⏩ セグメントデータなし → スキップ（EDINET蓄積後に再実行）")
        return

    from segment_mapper import SegmentMapper
    from size_tier_classifier import get_taxonomy_nodes_for_sector
    mapper = SegmentMapper(cfg)
    rows = build_company_node_rows(
        segments,
        lambda name: mapper.map(name),
        get_taxonomy_nodes_for_sector,
    )
    print(f"📊 生成行: {len(rows)}行（confidence_type=edinet）")

    if dry:
        for r in rows[:5]:
            print(f"   {r['company_id']} → {r['node_id']} "
                  f"score={r['relevance_score']} share={r['revenue_share_est']}")
        if len(rows) > 5:
            print(f"   ... 他{len(rows) - 5}行")
        print("✅ --dry-run のためBQ書込なし")
        return

    ok, n = upsert_to_bq(rows, cfg)
    print(f"{'✅ MERGE完了' if ok else '⚠️ 書込失敗（exit 0）'}: {n}行")


if __name__ == "__main__":
    main()
