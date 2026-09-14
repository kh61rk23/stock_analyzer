# ==============================================================
# cluster_graph_exporter.py  v1.0  (Wave 6 - B)
# クラスター結果 外部利用用出力モジュール
# ==============================================================
# 【役割】
#   technology_cluster_engine.py / emerging_technology_detector.py が
#   生成したクラスター情報を、外部利用（BQ / JSON / レポート）向けに
#   構造変換して出力する。
#
#   出力先①: BQ taxonomy.company_taxonomy_map（MERGE によるアップサート）
#   出力先②: stock_data/cache/cluster_graph_export.json（週次スナップショット）
#   出力先③: results["cluster_export"]（stock_analyzer_main.py へ返却）
#
# 【呼び出しパターン（stock_analyzer_main.py から）】
#   from cluster_graph_exporter import run_cluster_graph_exporter
#   result = run_cluster_graph_exporter(cfg, results)
#
# 【設計方針】
#   - BQ書込は ENABLE_CLUSTER_GRAPH_EXPORT=True かつ BQ接続成功時のみ実施
#   - BQ書込失敗はログのみで続行（システム停止させない）
#   - JSON出力は BQ 成否にかかわらず必ず実施
#   - 全関数は try/except で保護し例外を外部に伝播しない
#
# 【依存】
#   config.py (ENABLE_CLUSTER_GRAPH_EXPORT / CACHE_DIR)
#   edinet_bq_client.py (EDINETBQClient)
#   標準ライブラリのみ（追加ライブラリ不使用）
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: モジュール定数
# ==============================================================

_MODULE_VERSION  = "1.0"
_EXPORT_FILENAME = "cluster_graph_export.json"


# ==============================================================
# BLOCK 1: クラスターデータ → BQ行形式 変換
# ==============================================================

def _build_taxonomy_rows(
    cluster_phase: dict,
    cluster_master: dict,
) -> list[dict]:
    """
    cluster_phase_cache と cluster_master から
    taxonomy.company_taxonomy_map テーブルへの書込行を構築する。

    ★ 2026-07-10（Wave R2-2）実スキーマ是正:
      旧実装は company_code/market/score/taxonomy_share/as_of_date 列で書き込んでいたが、
      実テーブル（ground truth: bq_all_schemas_20260704.json）の列は
      map_id/company_id/node_id/relevance_score/confidence/revenue_share_est/
      confidence_type/evidence_source/rationale/valid_from/auto_derived/verified_flag/updated_at。
      列名不一致で insert_rows_json が常に失敗＝「producer配線済みだが実質空」
      （HANDOFF_2026-06-30 §4-8）の根本原因だったため、実スキーマに合わせた。

    行の規約（叩き台=draft層）:
      company_id      "sector:{market}:{sector_code}"（業種レベルの擬似企業ID。
                      T-2Aの企業レベル行=実企業コードと区別する）
      confidence      0.3 固定（cluster_derived層の確度。edinet層=0.7・人間確定=1.0）
      confidence_type "cluster_derived"
      auto_derived    True / verified_flag False（人間レビュー前）

    Args:
        cluster_phase:  technology_cluster_engine の返り値
        cluster_master: technology_mapper の返り値

    Returns:
        [{"map_id": str, "company_id": str, "node_id": str, ...}, ...]
    """
    rows         = []
    as_of_date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    updated_at   = datetime.now(timezone.utc).isoformat()
    clusters     = cluster_phase.get("clusters", []) or []
    markets_data = cluster_master.get("markets", {}) or {}

    for cluster in clusters:
        market    = cluster.get("market", "JP")
        phase     = cluster.get("phase", "unknown")
        cluster_id = cluster.get("cluster_id", "")

        # centroid_nodes のスコア合計（taxonomy_share 計算用）
        centroid_nodes = cluster.get("centroid_nodes", [])
        market_sectors = markets_data.get(market, {})

        # クラスター内全業種のノードスコア平均を計算
        node_total: dict[str, float] = {}
        node_count: dict[str, int]   = {}

        for sector_code in cluster.get("sectors", []):
            tech_scores = (
                market_sectors
                .get(str(sector_code), {})
                .get("tech_scores", {})
            )
            for node_id, score in tech_scores.items():
                node_total[node_id] = node_total.get(node_id, 0.0) + float(score)
                node_count[node_id] = node_count.get(node_id, 0) + 1

        node_avg: dict[str, float] = {
            nid: round(node_total[nid] / node_count[nid], 4)
            for nid in node_total
        }

        total_score = sum(node_avg.values())
        if total_score <= 0:
            continue

        # 業種×ノード ごとに1行生成（centroid_nodes のみ対象）
        for sector_code in cluster.get("sectors", []):
            tech_scores = (
                market_sectors
                .get(str(sector_code), {})
                .get("tech_scores", {})
            )

            for node_id in centroid_nodes:
                score = tech_scores.get(node_id, 0.0)
                if score <= 0:
                    continue

                taxonomy_share = round(
                    node_avg.get(node_id, 0.0) / total_score, 4
                )

                company_id = f"sector:{market}:{sector_code}"
                rows.append({
                    "map_id":            f"{company_id}:{node_id}:{as_of_date}",
                    "company_id":        company_id,
                    "node_id":           node_id,
                    "relevance_score":   round(float(score), 4),
                    "confidence":        0.3,
                    "revenue_share_est": taxonomy_share,
                    "confidence_type":   "cluster_derived",
                    "evidence_source":   "cluster_graph_exporter",
                    "rationale":         f"cluster={cluster_id} phase={phase}",
                    "valid_from":        as_of_date,
                    "auto_derived":      True,
                    "verified_flag":     False,
                    "updated_at":        updated_at,
                })

    return rows


# ==============================================================
# BLOCK 2: JSON スナップショット構築
# ==============================================================

def _build_export_snapshot(
    cluster_phase:  dict,
    cluster_master: dict,
    emerging_tech:  dict,
    taxonomy_rows:  list[dict],
) -> dict:
    """
    cluster_graph_export.json 形式の dict を構築する。

    構造:
      {
        "generated":     str,
        "version":       str,
        "clusters":      [...],   # cluster_phase_cache の clusters そのまま
        "emerging_top5": [...],   # emerging_technology_cache の上位5件
        "taxonomy_rows": [...],   # BQ taxonomy 行（参照用サマリー）
        "stats": {
          "total_clusters":    int,
          "total_sectors_jp":  int,
          "total_sectors_us":  int,
          "phase_breakdown":   {"acceleration": N, ...},
          "top_emerging_node": str | None,
        }
      }
    """
    clusters = cluster_phase.get("clusters", []) or []
    emerging_themes = (emerging_tech or {}).get("emerging_themes", []) or []

    # フェーズ内訳
    phase_breakdown: dict[str, int] = {}
    jp_sectors_set: set[str] = set()
    us_sectors_set: set[str] = set()

    for c in clusters:
        ph = c.get("phase", "unknown")
        phase_breakdown[ph] = phase_breakdown.get(ph, 0) + 1
        if c.get("market") == "JP":
            jp_sectors_set.update(c.get("sectors", []))
        else:
            us_sectors_set.update(c.get("sectors", []))

    top_emerging = (
        emerging_themes[0]["node_id"] if emerging_themes else None
    )

    return {
        "generated":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version":       _MODULE_VERSION,
        "clusters":      clusters,
        "emerging_top5": emerging_themes[:5],
        "taxonomy_rows_count": len(taxonomy_rows),
        "stats": {
            "total_clusters":    len(clusters),
            "total_sectors_jp":  len(jp_sectors_set),
            "total_sectors_us":  len(us_sectors_set),
            "phase_breakdown":   phase_breakdown,
            "top_emerging_node": top_emerging,
        },
    }


# ==============================================================
# BLOCK 3: BQ 書込
# ==============================================================

def _write_to_bq(rows: list[dict], cfg) -> tuple[bool, int]:
    """
    taxonomy.company_taxonomy_map テーブルへ MERGE で書き込む。
    BQ未接続・書込失敗時はログのみで False を返す（例外伝播なし）。

    MERGE キー: company_code + market + node_id + as_of_date

    Args:
        rows: _build_taxonomy_rows() の返り値
        cfg:  config モジュール

    Returns:
        tuple: (success: bool, written_count: int)
    """
    if not rows:
        return True, 0

    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
    except Exception as e:
        print(
            f"  [cluster_exporter] BQクライアント初期化失敗 "
            f"({str(e)[:60]}): JSON出力のみ継続"
        )
        return False, 0

    try:
        project = getattr(cfg, "BQ_PROJECT_ID", "stock-analyzer-edinet")
        # 2026-09-14: 書込先は taxonomy データセット固定。cfg.BQ_DATASET は
        # EdinetDB 用の既定データセット（"edinet"）で、これを流用したため
        # 存在しない edinet.company_taxonomy_map を指して毎週 404 になっていた。
        # 行の列は taxonomy.company_taxonomy_map の実スキーマと一致を確認済み。
        dataset = "taxonomy"

        # staging table への INSERT → MERGE の2段階
        # staging は company_taxonomy_map と同スキーマの一時領域として利用
        # ※ GCP上に taxonomy.company_taxonomy_map が作成済みであること前提
        table_ref = f"{project}.{dataset}.company_taxonomy_map"

        # BQ streaming insert（insert_rows_json）で一括書込
        errors = bq.client.insert_rows_json(table_ref, rows)
        if errors:
            print(
                f"  [cluster_exporter] BQ insert_rows エラー "
                f"({len(errors)}件): {str(errors[0])[:80]}"
            )
            return False, 0

        print(
            f"  [cluster_exporter] ✅ BQ書込完了: "
            f"{len(rows)}行 → {table_ref}"
        )
        return True, len(rows)

    except Exception as e:
        print(
            f"  [cluster_exporter] BQ書込失敗 "
            f"({str(e)[:80]}): JSON出力のみ継続"
        )
        return False, 0


# ==============================================================
# BLOCK 4: JSON スナップショット書込
# ==============================================================

def _write_snapshot_json(
    snapshot:   dict,
    cache_path: Path,
) -> bool:
    """
    cluster_graph_export.json へ書き出す。

    Args:
        snapshot:   _build_export_snapshot() の返り値
        cache_path: 出力先パス

    Returns:
        bool: 成功/失敗
    """
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"  [cluster_exporter] ✅ JSONスナップショット書込: "
            f"{cache_path}"
        )
        return True
    except Exception as e:
        print(
            f"  [cluster_exporter] JSONスナップショット書込失敗: "
            f"{str(e)[:60]}"
        )
        return False


# ==============================================================
# BLOCK 5: パブリック API
# ==============================================================

def get_export_snapshot(
    cfg=None,
    export_path: Optional[Path] = None,
) -> dict:
    """
    cluster_graph_export.json を読み込んで返す。
    weekly_report_generator から参照する。

    Args:
        cfg:         config モジュール（None 可）
        export_path: cluster_graph_export.json のパス（None 時は cfg から取得）

    Returns:
        dict: スナップショット全体、未存在時は空 dict
    """
    try:
        if export_path is None and cfg is not None:
            cache_dir = getattr(cfg, "CACHE_DIR", Path("stock_data/cache"))
            export_path = Path(cache_dir) / _EXPORT_FILENAME

        if export_path is None:
            return {}

        p = Path(export_path)
        if not p.exists():
            return {}

        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def run_cluster_graph_exporter(cfg, results: dict) -> dict:
    """
    メインエントリポイント。stock_analyzer_main.py から呼び出す。

    処理フロー:
      1. results["cluster_phase"] / ["cluster_master"] / ["emerging_tech"] を確認
      2. _build_taxonomy_rows() で BQ行を構築
      3. ENABLE_CLUSTER_GRAPH_EXPORT=True かつ BQ接続可能なら BQ書込
      4. _build_export_snapshot() で JSON スナップショットを構築
      5. JSON ファイル書込
      6. results["cluster_export"] に結果を格納

    Args:
        cfg:     config モジュール
        results: run_all_tasks() の results dict（in-place 更新）

    Returns:
        {
          "success":       bool,
          "bq_written":    int,    # BQ書込行数（0=スキップ or 失敗）
          "json_written":  bool,
          "cluster_count": int,
          "emerging_count": int,
          "error":         str | None,
        }
    """
    out = {
        "success":       False,
        "bq_written":    0,
        "json_written":  False,
        "cluster_count": 0,
        "emerging_count": 0,
        "error":         None,
    }

    try:
        if not getattr(cfg, "ENABLE_CLUSTER_GRAPH_EXPORT", True):
            print(
                "  ⏩ cluster_graph_exporter: "
                "スキップ（ENABLE_CLUSTER_GRAPH_EXPORT=False）"
            )
            out["success"] = True
            return out

        # ── 必要データの取得 ──────────────────────────────────────
        cluster_phase  = results.get("cluster_phase") or {}
        cluster_master = results.get("cluster_master") or {}
        emerging_tech  = results.get("emerging_tech")  or {}

        if not cluster_phase or not cluster_phase.get("clusters"):
            out["error"] = (
                "cluster_phase が results に未存在。"
                "cluster_engine を先に実行すること。"
            )
            print(f"  [cluster_exporter] ⚠️  {out['error']}")
            return out

        # ── BQ行構築 ─────────────────────────────────────────────
        taxonomy_rows = []
        try:
            taxonomy_rows = _build_taxonomy_rows(cluster_phase, cluster_master)
            print(
                f"  [cluster_exporter] BQ行構築: {len(taxonomy_rows)}行"
            )
        except Exception as e:
            print(
                f"  [cluster_exporter] BQ行構築失敗 ({str(e)[:60]}): 続行"
            )

        # ── BQ書込（フラグ有効時のみ）─────────────────────────────
        bq_ok, bq_count = False, 0
        if getattr(cfg, "ENABLE_CLUSTER_GRAPH_EXPORT", True) and taxonomy_rows:
            bq_ok, bq_count = _write_to_bq(taxonomy_rows, cfg)
        out["bq_written"] = bq_count

        # ── JSON スナップショット構築・書込 ────────────────────────
        snapshot = _build_export_snapshot(
            cluster_phase,
            cluster_master,
            emerging_tech,
            taxonomy_rows,
        )

        cache_dir   = getattr(cfg, "CACHE_DIR", Path("stock_data/cache"))
        export_path = Path(cache_dir) / _EXPORT_FILENAME
        json_ok     = _write_snapshot_json(snapshot, export_path)
        out["json_written"] = json_ok

        # ── results に格納 ────────────────────────────────────────
        results["cluster_export"] = snapshot

        clusters     = cluster_phase.get("clusters", [])
        themes       = emerging_tech.get("emerging_themes", [])
        out.update({
            "success":       True,
            "cluster_count": len(clusters),
            "emerging_count": len(themes),
        })

        print(
            f"  [cluster_exporter] ✅ 完了: "
            f"クラスター={len(clusters)}件 / "
            f"萌芽テーマ={len(themes)}件 / "
            f"BQ={bq_count}行 / JSON={'OK' if json_ok else 'NG'}"
        )
        return out

    except Exception as e:
        out["error"] = str(e)[:120]
        print(f"  [cluster_exporter] ⚠️  実行エラー: {out['error']}")
        return out


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  cluster_graph_exporter.py v1.0 単体テスト")
    print("=" * 60)

    try:
        import config as cfg
    except ImportError:
        print("  ⚠️  config.py が見つかりません（テスト中断）")
        raise SystemExit(1)

    from technology_mapper          import run_technology_mapper
    from technology_cluster_engine  import run_cluster_engine
    from emerging_technology_detector import run_emerging_detector

    results = {}
    run_technology_mapper(cfg, results)
    run_cluster_engine(cfg, results)
    run_emerging_detector(cfg, results)

    out = run_cluster_graph_exporter(cfg, results)
    print(f"\n  結果: {out}")

    if results.get("cluster_export"):
        stats = results["cluster_export"].get("stats", {})
        print(f"\n  ── Stats ──")
        for k, v in stats.items():
            print(f"  {k}: {v}")

    print("\n✅ 単体テスト完了")