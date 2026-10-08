# ==============================================================
# emerging_technology_detector.py  v1.0  (B)
# 技術テーマ 萌芽検知モジュール
# ==============================================================
# 【役割】
#   cluster_phase_cache.json のクラスタースコア推移 +
#   results["arxiv"] (ArXivトレンドスコア) +
#   results["news"]  (ニュースキーワード出現数) を横断的に分析し、
#   急浮上している技術テーマ（萌芽テーマ）を検知して
#   emerging_technology_cache.json へ保存する。
#
# 【呼び出しパターン（stock_analyzer_main.py から）】
#   from emerging_technology_detector import run_emerging_detector
#   result = run_emerging_detector(cfg, results)
#
# 【萌芽スコア計算式】
#   $emerging\_score = w_1 \\cdot cluster\_delta + w_2 \\cdot arxiv\_score
#                    + w_3 \\cdot news\_surge$
#   w1=0.50, w2=0.30, w3=0.20
#
# 【検知閾値】
#   emerging_score >= EMERGING_SCORE_MIN かつ
#   いずれか1信号がサージ（EMERGING_KEYWORD_SURGE_THRESHOLD超え）
#
# 【依存】
#   config.py (EMERGING_KEYWORD_SURGE_THRESHOLD / EMERGING_SCORE_MIN 等)
#   標準ライブラリのみ
# ==============================================================

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: モジュール定数
# ==============================================================

_MODULE_VERSION  = "1.0"
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_EMERGING_TECHNOLOGY_DETECTOR_CACHE_TTL_DAYS': '_CACHE_TTL_DAYS',
    'B6_EMERGING_TECHNOLOGY_DETECTOR_W_CLUSTER': '_W_CLUSTER',
    'B6_EMERGING_TECHNOLOGY_DETECTOR_W_ARXIV': '_W_ARXIV',
    'B6_EMERGING_TECHNOLOGY_DETECTOR_W_NEWS': '_W_NEWS',
}

_CACHE_TTL_DAYS  = 14   # emerging_technology_cache.json の有効期間（日数）

# 萌芽スコアの信号別ウェイト
_W_CLUSTER = 0.50   # クラスタースコア変化シグナル
_W_ARXIV   = 0.30   # ArXiv論文トレンドシグナル
_W_NEWS    = 0.20   # ニュースキーワード急増シグナル


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _load_cache(cache_path: Path) -> Optional[dict]:
    """emerging_technology_cache.json を読み込む。TTL切れ・未存在時は None。"""
    try:
        if not cache_path.exists():
            return None
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        generated_str = raw.get("generated", "")
        if not generated_str:
            return None
        generated_dt = datetime.fromisoformat(generated_str)
        if datetime.now() - generated_dt > timedelta(days=_CACHE_TTL_DAYS):
            return None
        return raw
    except Exception:
        return None


def _save_cache(cache_path: Path, data: dict) -> bool:
    """emerging_technology_cache.json へ書き出す。"""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        print(
            f"  [emerging_detector] キャッシュ書込失敗: {str(e)[:60]}"
        )
        return False


# ==============================================================
# BLOCK 2: ArXiv シグナル抽出
# ==============================================================

def _extract_arxiv_signals(
    results:          dict,
    tech_keyword_map: dict,
    surge_threshold:  float,
) -> dict[str, float]:
    """
    results["arxiv"] からキーワードマッチングで技術ノード別のArXivシグナルを抽出する。

    ArXiv データ構造（arxiv_monitor.py の返り値）:
      results["arxiv"] = {
        "papers": [{"title": str, "summary": str, "score": float}, ...],
        "top_keywords": [{"keyword": str, "count": int}, ...],
        ...
      }

    Args:
        results:          run_all_tasks() の results dict
        tech_keyword_map: config.TECH_KEYWORD_MAP
        surge_threshold:  EMERGING_KEYWORD_SURGE_THRESHOLD

    Returns:
        {node_id: normalized_score (0.0〜1.0), ...}
        ※ スコアが閾値未満のノードは除外
    """
    arxiv_data = results.get("arxiv") or {}
    if not arxiv_data:
        return {}

    papers     = arxiv_data.get("papers", []) or []
    top_kw     = arxiv_data.get("top_keywords", []) or []

    # top_keywords から keyword→count マップを構築
    kw_count_map: dict[str, int] = {}
    for kw_entry in top_kw:
        if isinstance(kw_entry, dict):
            kw  = str(kw_entry.get("keyword", "")).lower()
            cnt = int(kw_entry.get("count", 0) or 0)
            if kw:
                kw_count_map[kw] = cnt

    max_count = max(kw_count_map.values(), default=1)
    if max_count == 0:
        max_count = 1

    node_signals: dict[str, float] = {}

    for node_id, keywords in tech_keyword_map.items():
        node_score = 0.0
        matched    = 0

        for kw in keywords:
            kw_lower = kw.lower()
            # top_keywords マッチ
            if kw_lower in kw_count_map:
                normalized = kw_count_map[kw_lower] / max_count
                node_score += normalized
                matched    += 1
                continue

            # papers タイトル・サマリーへの部分一致（軽量チェック）
            for paper in papers[:50]:  # 上位50件のみチェック（計算量制限）
                title   = str(paper.get("title",   "") or "").lower()
                summary = str(paper.get("summary", "") or "").lower()
                paper_score = float(paper.get("score", 0) or 0)
                if kw_lower in title:
                    node_score += min(1.0, paper_score / 5.0) * 0.8
                    matched    += 1
                    break
                elif kw_lower in summary:
                    node_score += min(1.0, paper_score / 5.0) * 0.4
                    matched    += 1
                    break

        if matched > 0:
            normalized_node = round(min(1.0, node_score / max(matched, 1)), 4)
            if normalized_node >= surge_threshold:
                node_signals[node_id] = normalized_node

    return node_signals


# ==============================================================
# BLOCK 3: ニュースシグナル抽出
# ==============================================================

def _extract_news_signals(
    results:          dict,
    tech_keyword_map: dict,
    surge_threshold:  float,
) -> dict[str, float]:
    """
    results["news"] からキーワードマッチングで技術ノード別のニュースシグナルを抽出する。

    ニュースデータ構造（news_pipeline.py の返り値）:
      results["news"] = {
        "articles": [{"title": str, "sector": str, "score": float}, ...],
        "keyword_counts": {"キーワード": N, ...},
        ...
      }

    Args:
        results:          run_all_tasks() の results dict
        tech_keyword_map: config.TECH_KEYWORD_MAP
        surge_threshold:  EMERGING_KEYWORD_SURGE_THRESHOLD

    Returns:
        {node_id: normalized_score (0.0〜1.0), ...}
    """
    news_data = results.get("news") or {}
    if not news_data:
        return {}

    articles      = news_data.get("articles", []) or []
    kw_counts_raw = news_data.get("keyword_counts", {}) or {}

    # keyword_counts の正規化
    kw_counts: dict[str, int] = {}
    for k, v in kw_counts_raw.items():
        try:
            kw_counts[str(k).lower()] = int(v or 0)
        except Exception:
            pass

    max_count = max(kw_counts.values(), default=1)
    if max_count == 0:
        max_count = 1

    node_signals: dict[str, float] = {}

    for node_id, keywords in tech_keyword_map.items():
        node_score = 0.0
        matched    = 0

        for kw in keywords:
            kw_lower = kw.lower()

            # keyword_counts マッチ
            if kw_lower in kw_counts:
                normalized = kw_counts[kw_lower] / max_count
                node_score += normalized
                matched    += 1
                continue

            # articles タイトルへの部分一致（上位30件）
            for article in articles[:30]:
                title = str(article.get("title", "") or "").lower()
                if kw_lower in title:
                    art_score = float(article.get("score", 1) or 1)
                    node_score += min(1.0, art_score / 5.0) * 0.6
                    matched    += 1
                    break

        if matched > 0:
            normalized_node = round(min(1.0, node_score / max(matched, 1)), 4)
            if normalized_node >= surge_threshold:
                node_signals[node_id] = normalized_node

    return node_signals


# ==============================================================
# BLOCK 4: クラスタースコア変化シグナル抽出
# ==============================================================

def _extract_cluster_delta_signals(
    results: dict,
    surge_threshold: float,
) -> dict[str, float]:
    """
    results["cluster_phase"] のクラスターフェーズから技術ノード別の
    スコア変化シグナルを抽出する。

    acceleration / expansion フェーズのクラスターの centroid_nodes を
    萌芽シグナルとして扱う。

    Args:
        results:         run_all_tasks() の results dict
        surge_threshold: 萌芽シグナル閾値

    Returns:
        {node_id: signal_score (0.0〜1.0), ...}
    """
    cluster_phase = results.get("cluster_phase") or {}
    clusters      = cluster_phase.get("clusters", []) or []
    if not clusters:
        return {}

    node_signals: dict[str, float] = defaultdict(float)
    node_count:   dict[str, int]   = defaultdict(int)

    for cluster in clusters:
        phase = cluster.get("phase", "unknown")
        if phase not in ("acceleration", "expansion"):
            continue

        # フェーズに応じた信号強度
        phase_signal = 1.0 if phase == "acceleration" else 0.6

        avg_score = float(cluster.get("avg_score", 0.0))
        # クラスター自体のスコアで重み付け
        weighted_signal = phase_signal * min(1.0, avg_score * 1.5)

        for node_id in cluster.get("centroid_nodes", []):
            node_signals[node_id] += weighted_signal
            node_count[node_id]   += 1

    result: dict[str, float] = {}
    for node_id, total in node_signals.items():
        avg_signal = round(total / max(node_count[node_id], 1), 4)
        if avg_signal >= surge_threshold:
            result[node_id] = min(1.0, avg_signal)

    return result


# ==============================================================
# BLOCK 5: 萌芽スコア統合
# ==============================================================

def _calc_emerging_score(
    cluster_delta: float,
    arxiv_score:   float,
    news_score:    float,
) -> float:
    """
    3信号を統合して萌芽スコアを計算する。

    $emerging\\_score = w_1 \\cdot cluster\\_delta + w_2 \\cdot arxiv\\_score
                     + w_3 \\cdot news\\_surge$

    w1=0.50, w2=0.30, w3=0.20

    Returns:
        float: 0.0 〜 1.0 の萌芽スコア（上限クリップ済み）
    """
    raw = (
        _W_CLUSTER * cluster_delta
        + _W_ARXIV  * arxiv_score
        + _W_NEWS   * news_score
    )
    return round(min(1.0, max(0.0, raw)), 4)


def build_emerging_cache(
    results:          dict,
    cfg,
) -> dict:
    """
    3信号を統合して emerging_technology_cache.json 形式の dict を構築する。

    Args:
        results: run_all_tasks() の results dict
        cfg:     config モジュール

    Returns:
        {
          "generated":       "2026-06-03T08:00:00",
          "version":         "1.0",
          "ttl_days":        14,
          "emerging_themes": [
            {
              "node_id":        "generative_ai",
              "emerging_score": 0.82,
              "signals": {
                "cluster_delta": 0.90,
                "arxiv":         0.75,
                "news":          0.60,
              },
              "affected_sectors": ["1025", "1016"],
              "rank":            1,
            },
            ...
          ]
        }
    """
    tech_keyword_map  = getattr(cfg, "TECH_KEYWORD_MAP", {})
    surge_threshold   = getattr(cfg, "EMERGING_KEYWORD_SURGE_THRESHOLD", 0.50)
    emerging_min      = getattr(cfg, "EMERGING_SCORE_MIN",              0.30)

    if not tech_keyword_map:
        return {
            "generated":       datetime.now().isoformat(timespec="seconds"),
            "version":         _MODULE_VERSION,
            "ttl_days":        _CACHE_TTL_DAYS,
            "emerging_themes": [],
        }

    # ── 3信号を並行抽出 ──────────────────────────────────────────
    try:
        cluster_delta_map = _extract_cluster_delta_signals(results, surge_threshold)
    except Exception as e:
        print(f"  [emerging_detector] クラスターシグナル抽出失敗: {str(e)[:60]}")
        cluster_delta_map = {}

    try:
        arxiv_map = _extract_arxiv_signals(results, tech_keyword_map, surge_threshold)
    except Exception as e:
        print(f"  [emerging_detector] ArXivシグナル抽出失敗: {str(e)[:60]}")
        arxiv_map = {}

    try:
        news_map = _extract_news_signals(results, tech_keyword_map, surge_threshold)
    except Exception as e:
        print(f"  [emerging_detector] ニュースシグナル抽出失敗: {str(e)[:60]}")
        news_map = {}

    # ── 全技術ノードを統合 ────────────────────────────────────────
    all_nodes = (
        set(cluster_delta_map.keys())
        | set(arxiv_map.keys())
        | set(news_map.keys())
    )

    # クラスターフェーズから affected_sectors マッピングを構築
    cluster_phase  = results.get("cluster_phase") or {}
    clusters_list  = cluster_phase.get("clusters", []) or []
    node_to_sectors: dict[str, list[str]] = defaultdict(list)
    for cluster in clusters_list:
        for node_id in cluster.get("centroid_nodes", []):
            for sector_code in cluster.get("sectors", []):
                if sector_code not in node_to_sectors[node_id]:
                    node_to_sectors[node_id].append(sector_code)

    emerging_themes = []

    for node_id in all_nodes:
        c_delta = cluster_delta_map.get(node_id, 0.0)
        a_score = arxiv_map.get(node_id, 0.0)
        n_score = news_map.get(node_id, 0.0)

        emerging_score = _calc_emerging_score(c_delta, a_score, n_score)

        if emerging_score < emerging_min:
            continue

        # いずれか1シグナルがサージ閾値を超えていること
        any_surge = (
            c_delta >= surge_threshold
            or a_score >= surge_threshold
            or n_score >= surge_threshold
        )
        if not any_surge:
            continue

        affected_sectors = node_to_sectors.get(node_id, [])

        emerging_themes.append({
            "node_id":         node_id,
            "emerging_score":  emerging_score,
            "signals": {
                "cluster_delta": c_delta,
                "arxiv":         a_score,
                "news":          n_score,
            },
            "affected_sectors": affected_sectors,
        })

    # emerging_score 降順でソート + ランク付け
    emerging_themes.sort(key=lambda x: -x["emerging_score"])
    for rank, theme in enumerate(emerging_themes, start=1):
        theme["rank"] = rank

    return {
        "generated":       datetime.now().isoformat(timespec="seconds"),
        "version":         _MODULE_VERSION,
        "ttl_days":        _CACHE_TTL_DAYS,
        "emerging_themes": emerging_themes,
    }


# ==============================================================
# BLOCK 6: パブリック API
# ==============================================================

def get_emerging_themes(
    cfg=None,
    cache_path: Optional[Path] = None,
    top_n:      int = 10,
) -> list[dict]:
    """
    萌芽テーマランキング上位 N 件を返す。
    weekly_report_generator._section_technology_cluster() から呼び出す。

    Args:
        cfg:        config モジュール（None 可）
        cache_path: emerging_technology_cache.json のパス（None 時は cfg から取得）
        top_n:      返す件数

    Returns:
        [
          {
            "node_id":         "generative_ai",
            "emerging_score":  0.82,
            "signals":         {...},
            "affected_sectors": ["1025", "1016"],
            "rank":            1,
          },
          ...
        ]
        ※ キャッシュ未存在・エラー時は空リスト
    """
    try:
        if cache_path is None and cfg is not None:
            cache_path = getattr(cfg, "EMERGING_TECH_CACHE", None)
        if cache_path is None:
            return []

        cached = _load_cache(Path(cache_path))
        if not cached:
            return []

        themes = cached.get("emerging_themes", [])
        return themes[:top_n]
    except Exception:
        return []


def run_emerging_detector(cfg, results: dict) -> dict:
    """
    メインエントリポイント。stock_analyzer_main.py から呼び出す。

    処理フロー:
      1. emerging_technology_cache.json キャッシュを確認
      2. TTL切れ or 未存在なら build_emerging_cache() で再計算
      3. キャッシュ保存
      4. results["emerging_tech"] に結果を格納

    Args:
        cfg:     config モジュール
        results: run_all_tasks() の results dict（in-place 更新）

    Returns:
        {
          "success":       bool,
          "theme_count":   int,
          "top_theme":     str | None,  # 1位の node_id
          "cache_hit":     bool,
          "error":         str | None,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    out = {
        "success":    False,
        "theme_count": 0,
        "top_theme":  None,
        "cache_hit":  False,
        "error":      None,
    }

    try:
        if not getattr(cfg, "ENABLE_EMERGING_DETECTOR", True):
            print("  ⏩ emerging_detector: スキップ（ENABLE_EMERGING_DETECTOR=False）")
            out["success"] = True
            return out

        cache_path = Path(
            getattr(cfg, "EMERGING_TECH_CACHE",
                    "stock_data/cache/emerging_technology_cache.json")
        )

        # ① キャッシュ確認
        cached = _load_cache(cache_path)
        if cached:
            print(
                f"  [emerging_detector] ✅ キャッシュ利用 "
                f"({cached.get('generated', 'unknown')})"
            )
            results["emerging_tech"] = cached
            themes = cached.get("emerging_themes", [])
            out.update({
                "success":     True,
                "theme_count": len(themes),
                "top_theme":   themes[0]["node_id"] if themes else None,
                "cache_hit":   True,
            })
            return out

        # ② cluster_phase が results に存在するか確認（警告のみ・続行可）
        if not results.get("cluster_phase"):
            print(
                "  [emerging_detector] ⚠️  cluster_phase が results に未存在。"
                "クラスターシグナルなしで続行。"
            )

        # ③ 萌芽テーマ検知
        print("  [emerging_detector] 萌芽テーマ検知中...")
        emerging_cache = build_emerging_cache(results, cfg)

        # ④ キャッシュ保存
        _save_cache(cache_path, emerging_cache)

        # ⑤ results に格納
        results["emerging_tech"] = emerging_cache

        themes = emerging_cache.get("emerging_themes", [])
        top_theme = themes[0]["node_id"] if themes else None

        out.update({
            "success":     True,
            "theme_count": len(themes),
            "top_theme":   top_theme,
            "cache_hit":   False,
        })

        print(
            f"  [emerging_detector] ✅ 完了: "
            f"{len(themes)}萌芽テーマ検知 "
            f"/ Top: {top_theme or '（なし）'}"
        )
        return out

    except Exception as e:
        out["error"] = str(e)[:120]
        print(f"  [emerging_detector] ⚠️  実行エラー: {out['error']}")
        return out


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  emerging_technology_detector.py v1.0 単体テスト")
    print("=" * 60)

    try:
        import config as cfg
    except ImportError:
        print("  ⚠️  config.py が見つかりません（テスト中断）")
        raise SystemExit(1)

    from technology_mapper import run_technology_mapper
    from technology_cluster_engine import run_cluster_engine

    results = {}
    run_technology_mapper(cfg, results)
    run_cluster_engine(cfg, results)

    out = run_emerging_detector(cfg, results)
    print(f"\n  結果: {out}")

    if results.get("emerging_tech"):
        themes = results["emerging_tech"].get("emerging_themes", [])
        print(f"\n  検知テーマ数: {len(themes)}")
        for t in themes[:3]:
            print(
                f"  [{t['rank']}] {t['node_id']}: "
                f"score={t['emerging_score']} "
                f"sectors={t['affected_sectors'][:3]}"
            )

    print("\n✅ 単体テスト完了")
