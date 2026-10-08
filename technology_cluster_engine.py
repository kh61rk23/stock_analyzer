# ==============================================================
# technology_cluster_engine.py  v1.0
# 技術クラスター生成エンジン
# ==============================================================
# 【役割】
#   technology_mapper.py が生成した業種×技術スコア行列をもとに、
#   類似した技術プロファイルを持つ業種を「技術クラスター」としてグループ化する。
#   各クラスターのフェーズ（acceleration/expansion/mature/deceleration）を判定し、
#   cluster_phase_cache.json へ保存する。
#
# 【呼び出しパターン（stock_analyzer_main.py から）】
#   from technology_cluster_engine import run_cluster_engine
#   result = run_cluster_engine(cfg, results)
#
# 【クラスタリング手法】
#   - numpy/scikit-learn 不使用（標準ライブラリのみ）
#   - コサイン類似度ベースの Greedy クラスタリング
#   - 類似度 >= CLUSTER_SIMILARITY_THRESHOLD の業種を同一クラスターに統合
#   - クラスター代表技術ノード（centroid_nodes）= スコア平均 Top 5
#
# 【フェーズ判定基準】
#   cluster_phase_cache.json の前回スナップショットとの差分で判定:
#     acceleration: 平均スコア変化 >= +0.15 かつ 参加業種数増加
#     expansion:    平均スコア変化 >= +0.08 または 参加業種数増加
#     deceleration: 平均スコア変化 <= -0.10
#     mature:       上記以外（変化 < ±0.08）
#     unknown:      前回データなし
#
# 【依存】
#   config.py (CLUSTER_PHASE_RULES / CLUSTER_SIMILARITY_THRESHOLD 等)
#   technology_mapper.py (get_sector_tech_scores)
#   標準ライブラリのみ
# ==============================================================

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: モジュール定数
# ==============================================================

_MODULE_VERSION   = "1.0"
_PHASE_CACHE_TTL  = 7    # cluster_phase_cache.json の有効期間（日数）


# ==============================================================
# BLOCK 1: コサイン類似度計算
# ==============================================================

def _cosine_similarity(vec_a: dict, vec_b: dict) -> float:
    """
    2つのスコア辞書（{node_id: score}）のコサイン類似度を計算する。

    $sim = \\frac{\\sum_{k} a_k \\cdot b_k}{\\sqrt{\\sum_k a_k^2} \\cdot \\sqrt{\\sum_k b_k^2}}$

    Args:
        vec_a: {node_id: float} 形式のスコア辞書
        vec_b: {node_id: float} 形式のスコア辞書

    Returns:
        float: 0.0 〜 1.0 のコサイン類似度（エラー時は 0.0）
    """
    try:
        if not vec_a or not vec_b:
            return 0.0

        # 共通キーのみで内積を計算
        all_keys = set(vec_a.keys()) | set(vec_b.keys())
        dot     = sum(vec_a.get(k, 0.0) * vec_b.get(k, 0.0) for k in all_keys)
        norm_a  = math.sqrt(sum(v ** 2 for v in vec_a.values()))
        norm_b  = math.sqrt(sum(v ** 2 for v in vec_b.values()))

        if norm_a < 1e-9 or norm_b < 1e-9:
            return 0.0

        return round(dot / (norm_a * norm_b), 4)
    except Exception:
        return 0.0


# ==============================================================
# BLOCK 2: Greedy クラスタリング
# ==============================================================

def _greedy_cluster(
    sector_scores: dict,
    threshold:     float,
    min_size:      int,
    max_size:      int,
) -> list[dict]:
    """
    Greedy アルゴリズムで技術クラスターを生成する。

    処理フロー:
      1. スコア合計が大きい業種から順に処理（シード選択）
      2. 未割当業種の中でシードとの類似度 >= threshold を同一クラスターに統合
      3. min_size 未満のクラスターは除外
      4. max_size を超えるクラスターは類似度上位でトリミング

    Args:
        sector_scores: {sector_code: {node_id: score}} 形式
        threshold:     コサイン類似度の最低閾値
        min_size:      クラスター成立の最低業種数
        max_size:      クラスターの最大業種数

    Returns:
        [
          {
            "cluster_id":     "cluster_001",
            "sectors":        ["1016", "1025", "1018"],
            "centroid_nodes": ["ai_semiconductor", "cloud_datacenter", ...],
            "avg_score":      0.65,
            "similarity_matrix": {"1016_1025": 0.82, ...},
          },
          ...
        ]
    """
    if not sector_scores:
        return []

    # スコア合計で降順ソート（シード選択の優先順位）
    sorted_sectors = sorted(
        sector_scores.items(),
        key=lambda x: sum(x[1].values()),
        reverse=True,
    )

    assigned   = set()
    clusters   = []
    cluster_id = 1

    for seed_code, seed_vec in sorted_sectors:
        if seed_code in assigned:
            continue

        # このシードを中心にクラスターを形成
        cluster_members = [seed_code]
        similarity_matrix = {}

        for candidate_code, candidate_vec in sorted_sectors:
            if candidate_code in assigned or candidate_code == seed_code:
                continue
            if len(cluster_members) >= max_size:
                break

            sim = _cosine_similarity(seed_vec, candidate_vec)
            if sim >= threshold:
                cluster_members.append(candidate_code)
                key = f"{seed_code}_{candidate_code}"
                similarity_matrix[key] = sim

        # min_size 未満は単独クラスターとして記録しない
        if len(cluster_members) < min_size:
            continue

        # 全メンバーのスコアを集計して centroid_nodes を決定
        node_agg: dict[str, float] = defaultdict(float)
        for member in cluster_members:
            for node_id, score in sector_scores.get(member, {}).items():
                node_agg[node_id] += score

        # 平均スコアで上位5ノードを centroid_nodes とする
        node_avg = {
            nid: round(total / len(cluster_members), 4)
            for nid, total in node_agg.items()
        }
        centroid_nodes = [
            nid for nid, _ in
            sorted(node_avg.items(), key=lambda x: -x[1])[:5]
        ]

        # クラスター全体の平均スコア
        avg_score = round(
            sum(node_avg.values()) / len(node_avg) if node_avg else 0.0,
            4,
        )

        clusters.append({
            "cluster_id":       f"cluster_{cluster_id:03d}",
            "sectors":          cluster_members,
            "centroid_nodes":   centroid_nodes,
            "node_avg_scores":  node_avg,
            "avg_score":        avg_score,
            "similarity_matrix": similarity_matrix,
        })

        # 割当済みにマーク
        for m in cluster_members:
            assigned.add(m)

        cluster_id += 1

    return clusters


# ==============================================================
# BLOCK 3: フェーズ判定
# ==============================================================

def _load_phase_cache(cache_path: Path) -> Optional[dict]:
    """cluster_phase_cache.json を読み込む。TTL切れ・未存在時は None。"""
    try:
        if not cache_path.exists():
            return None
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        generated_str = raw.get("generated", "")
        if not generated_str:
            return None
        generated_dt = datetime.fromisoformat(generated_str)
        if datetime.now() - generated_dt > timedelta(days=_PHASE_CACHE_TTL):
            return None
        return raw
    except Exception:
        return None


def _save_phase_cache(cache_path: Path, data: dict) -> bool:
    """cluster_phase_cache.json へ書き出す。"""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        print(f"  [cluster_engine] フェーズキャッシュ書込失敗: {str(e)[:60]}")
        return False


def calc_cluster_phase(
    cluster:         dict,
    prev_clusters:   Optional[dict],
) -> str:
    """
    クラスターのフェーズを判定する。

    判定基準（prev_clusters との比較）:
      - "acceleration": avg_score変化 >= +0.15 かつ 業種数増加
      - "expansion":    avg_score変化 >= +0.08 または 業種数増加
      - "deceleration": avg_score変化 <= -0.10
      - "mature":       上記以外
      - "unknown":      前回データなし

    Args:
        cluster:       今回のクラスター dict（_greedy_cluster の返り値の1要素）
        prev_clusters: 前回の cluster_phase_cache の clusters リスト（None 可）

    Returns:
        str: "acceleration" | "expansion" | "mature" | "deceleration" | "unknown"
    """
    try:
        if not prev_clusters:
            return "unknown"

        cluster_id = cluster.get("cluster_id", "")
        # 前回の同一 cluster_id を探索
        prev_match = None
        for pc in prev_clusters:
            if pc.get("cluster_id") == cluster_id:
                prev_match = pc
                break

        # 前回と一致するクラスターがない場合は新規 → expansion 扱い
        if prev_match is None:
            # centroid_nodes が 50% 以上一致するものを前回対応と見なす
            curr_nodes = set(cluster.get("centroid_nodes", []))
            best_sim   = 0.0
            for pc in prev_clusters:
                prev_nodes = set(pc.get("centroid_nodes", []))
                if not curr_nodes or not prev_nodes:
                    continue
                overlap = len(curr_nodes & prev_nodes) / max(len(curr_nodes), len(prev_nodes))
                if overlap > best_sim:
                    best_sim    = overlap
                    prev_match  = pc

        if prev_match is None:
            return "unknown"

        curr_avg   = float(cluster.get("avg_score", 0.0))
        prev_avg   = float(prev_match.get("avg_score", 0.0))
        score_delta = curr_avg - prev_avg

        curr_size = len(cluster.get("sectors", []))
        prev_size = len(prev_match.get("sectors", []))
        size_grew = curr_size > prev_size

        if score_delta >= 0.15 and size_grew:
            return "acceleration"
        elif score_delta >= 0.08 or size_grew:
            return "expansion"
        elif score_delta <= -0.10:
            return "deceleration"
        else:
            return "mature"

    except Exception:
        return "unknown"


# ==============================================================
# BLOCK 4: cluster_phase_cache.json 構築
# ==============================================================

def build_cluster_phase_cache(
    cluster_master: dict,
    prev_phase_cache: Optional[dict],
    cfg,
) -> dict:
    """
    cluster_master.json の markets データからクラスターを生成し、
    cluster_phase_cache.json 形式の dict を構築する。

    Args:
        cluster_master:   technology_mapper.build_cluster_master() の返り値
        prev_phase_cache: 前回の cluster_phase_cache（None 可）
        cfg:              config モジュール

    Returns:
        {
          "generated": "2026-06-03T08:00:00",
          "version":   "1.0",
          "ttl_days":  7,
          "clusters": [
            {
              "cluster_id":   "cluster_001",
              "market":       "JP",
              "sectors":      ["1016", "1025"],
              "centroid_nodes": ["ai_semiconductor", ...],
              "avg_score":    0.65,
              "phase":        "acceleration",
              "phase_bonus":  8.0,
              "phase_label":  "技術加速",
            },
            ...
          ]
        }
    """
    threshold  = getattr(cfg, "CLUSTER_SIMILARITY_THRESHOLD", 0.60)
    min_size   = getattr(cfg, "CLUSTER_MIN_COMPANIES",       3)
    max_size   = getattr(cfg, "CLUSTER_MAX_SIZE",            20)
    phase_rules = getattr(cfg, "CLUSTER_PHASE_RULES", {})
    bonus_max  = getattr(cfg, "CLUSTER_BONUS_MAX",           8.0)
    bonus_min  = getattr(cfg, "CLUSTER_BONUS_MIN",          -3.0)

    prev_clusters = (prev_phase_cache or {}).get("clusters", [])

    all_clusters = []
    markets_data = cluster_master.get("markets", {})

    for market, market_sectors in markets_data.items():
        if not market_sectors:
            continue

        # 業種×技術スコア行列を抽出
        sector_scores = {
            sector_code: sector_data.get("tech_scores", {})
            for sector_code, sector_data in market_sectors.items()
            if sector_data.get("tech_scores")
        }

        clusters = _greedy_cluster(
            sector_scores,
            threshold=threshold,
            min_size=min_size,
            max_size=max_size,
        )

        for cluster in clusters:
            phase = calc_cluster_phase(cluster, prev_clusters)

            rule     = phase_rules.get(phase, {"bonus": 0.0, "label": "不明"})
            raw_bonus = float(rule.get("bonus", 0.0))
            # $cluster\_phase\_bonus \in [CLUSTER\_BONUS\_MIN, CLUSTER\_BONUS\_MAX]$
            bonus    = round(max(bonus_min, min(bonus_max, raw_bonus)), 2)

            cluster_record = {
                "cluster_id":    cluster["cluster_id"],
                "market":        market,
                "sectors":       cluster["sectors"],
                "centroid_nodes": cluster["centroid_nodes"],
                "avg_score":     cluster["avg_score"],
                "phase":         phase,
                "phase_bonus":   bonus,
                "phase_label":   rule.get("label", "不明"),
                "sector_count":  len(cluster["sectors"]),
            }
            all_clusters.append(cluster_record)

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "version":   _MODULE_VERSION,
        "ttl_days":  _PHASE_CACHE_TTL,
        "clusters":  all_clusters,
    }


# ==============================================================
# BLOCK 5: cluster_phase_bonus 取得 API
# ==============================================================

def get_cluster_phase_bonus(
    sector_code:     str,
    market:          str = "JP",
    cfg=None,
    phase_cache_path: Optional[Path] = None,
) -> tuple[float, str, str]:
    """
    指定業種が属するクラスターの cluster_phase_bonus を返す。
    signal_aggregator._extract_cluster_score() から呼び出す。

    Args:
        sector_code:       "1016" (JP) / "US_45" (US)
        market:            "JP" or "US"
        cfg:               config モジュール（None 可）
        phase_cache_path:  cluster_phase_cache.json のパス（None 時は cfg から取得）

    Returns:
        tuple: (phase_bonus: float, phase: str, cluster_id: str)
               エラー・未所属時は (0.0, "unknown", "")
    """
    try:
        if phase_cache_path is None and cfg is not None:
            phase_cache_path = getattr(cfg, "CLUSTER_PHASE_CACHE", None)
        if phase_cache_path is None:
            return 0.0, "unknown", ""

        cached = _load_phase_cache(Path(phase_cache_path))
        if not cached:
            return 0.0, "unknown", ""

        sector_str = str(sector_code)
        for cluster in cached.get("clusters", []):
            if cluster.get("market") != market:
                continue
            if sector_str in cluster.get("sectors", []):
                return (
                    float(cluster.get("phase_bonus", 0.0)),
                    str(cluster.get("phase", "unknown")),
                    str(cluster.get("cluster_id", "")),
                )
        return 0.0, "unknown", ""

    except Exception:
        return 0.0, "unknown", ""


# ==============================================================
# BLOCK 6: パブリック API
# ==============================================================

def run_cluster_engine(cfg, results: dict) -> dict:
    """
    メインエントリポイント。stock_analyzer_main.py から呼び出す。

    処理フロー:
      1. results["cluster_master"] が存在するか確認
      2. cluster_phase_cache.json を読み込み（TTL確認）
      3. TTL切れ or 未存在なら build_cluster_phase_cache() で再計算
      4. キャッシュ保存
      5. results["cluster_phase"] に結果を格納

    Args:
        cfg:     config モジュール
        results: run_all_tasks() の results dict（in-place 更新）

    Returns:
        {
          "success":       bool,
          "cluster_count": int,
          "phase_summary": {"acceleration": N, "expansion": N, ...},
          "cache_hit":     bool,
          "error":         str | None,
        }
    """
    out = {
        "success":       False,
        "cluster_count": 0,
        "phase_summary": {},
        "cache_hit":     False,
        "error":         None,
    }

    try:
        if not getattr(cfg, "ENABLE_CLUSTER_ENGINE", True):
            print("  ⏩ cluster_engine: スキップ（ENABLE_CLUSTER_ENGINE=False）")
            out["success"] = True
            return out

        phase_cache_path = Path(
            getattr(cfg, "CLUSTER_PHASE_CACHE",
                    "stock_data/cache/cluster_phase_cache.json")
        )

        # ① フェーズキャッシュが有効なら流用
        cached_phase = _load_phase_cache(phase_cache_path)
        if cached_phase:
            print(
                f"  [cluster_engine] ✅ フェーズキャッシュ利用 "
                f"({cached_phase.get('generated', 'unknown')})"
            )
            results["cluster_phase"] = cached_phase
            clusters = cached_phase.get("clusters", [])
            phase_summary: dict[str, int] = defaultdict(int)
            for c in clusters:
                phase_summary[c.get("phase", "unknown")] += 1
            out.update({
                "success":       True,
                "cluster_count": len(clusters),
                "phase_summary": dict(phase_summary),
                "cache_hit":     True,
            })
            return out

        # ② cluster_master が results に存在するか確認
        cluster_master = results.get("cluster_master")
        if not cluster_master or not cluster_master.get("markets"):
            out["error"] = (
                "cluster_master が results に存在しない。"
                "technology_mapper を先に実行すること。"
            )
            print(f"  [cluster_engine] ⚠️  {out['error']}")
            return out

        # ③ 前回フェーズキャッシュを読込（比較用・TTL無視）
        prev_phase_cache = None
        try:
            if phase_cache_path.exists():
                prev_phase_cache = json.loads(
                    phase_cache_path.read_text(encoding="utf-8")
                )
        except Exception:
            pass

        # ④ クラスター生成 + フェーズ判定
        print("  [cluster_engine] クラスター生成中...")
        phase_cache = build_cluster_phase_cache(
            cluster_master, prev_phase_cache, cfg
        )

        # ⑤ キャッシュ保存
        _save_phase_cache(phase_cache_path, phase_cache)

        # ⑥ results に格納
        results["cluster_phase"] = phase_cache

        clusters = phase_cache.get("clusters", [])
        phase_summary_d: dict[str, int] = defaultdict(int)
        for c in clusters:
            phase_summary_d[c.get("phase", "unknown")] += 1

        out.update({
            "success":       True,
            "cluster_count": len(clusters),
            "phase_summary": dict(phase_summary_d),
            "cache_hit":     False,
        })

        print(
            f"  [cluster_engine] ✅ 完了: "
            f"{len(clusters)}クラスター生成 "
            f"/ フェーズ内訳: {dict(phase_summary_d)}"
        )
        return out

    except Exception as e:
        out["error"] = str(e)[:120]
        print(f"  [cluster_engine] ⚠️  実行エラー: {out['error']}")
        return out


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  technology_cluster_engine.py v1.0 単体テスト")
    print("=" * 60)

    try:
        import config as cfg
    except ImportError:
        print("  ⚠️  config.py が見つかりません（テスト中断）")
        raise SystemExit(1)

    from technology_mapper import run_technology_mapper

    results = {}
    mapper_out = run_technology_mapper(cfg, results)
    print(f"\n  mapper結果: {mapper_out}")

    engine_out = run_cluster_engine(cfg, results)
    print(f"\n  engine結果: {engine_out}")

    if results.get("cluster_phase"):
        clusters = results["cluster_phase"].get("clusters", [])
        if clusters:
            print(f"\n  ── サンプルクラスター [{clusters[0]['cluster_id']}] ──")
            print(f"  market:       {clusters[0]['market']}")
            print(f"  sectors:      {clusters[0]['sectors']}")
            print(f"  phase:        {clusters[0]['phase']}")
            print(f"  phase_bonus:  {clusters[0]['phase_bonus']}")
            print(f"  centroid:     {clusters[0]['centroid_nodes']}")

    print("\n✅ 単体テスト完了")