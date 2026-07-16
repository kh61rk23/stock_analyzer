# ==============================================================
# technology_mapper.py  v1.0  (Wave 6 - B)
# 企業×技術分類 基礎マッピングモジュール
# ==============================================================
# 【役割】
#   jp_sector_subtheme.json / us_sector_subtheme.json のサブテーマと
#   config.TECH_KEYWORD_MAP を照合し、企業（業種コード）× 技術ノード の
#   relevance_score を計算して cluster_master.json へ書き出す。
#
# 【呼び出しパターン（stock_analyzer_main.py から）】
#   from technology_mapper import run_technology_mapper
#   result = run_technology_mapper(cfg, results)
#
# 【設計方針】
#   - subtheme_loader.py 経由でサブテーマJSONを参照（直接読込禁止）
#   - BQ taxonomy.company_taxonomy_map は参照のみ（書込はcluster_graph_exporter）
#   - 全関数は try/except で保護し例外を外部に伝播しない
#   - キャッシュ（cluster_master.json）が TTL 30日以内なら再計算しない
#
# 【依存】
#   config.py (TECH_KEYWORD_MAP / CLUSTER_MASTER_CACHE / JAPAN_SECTORS)
#   subtheme_loader.py (load_all_subthemes)
#   標準ライブラリのみ（numpy/scikit-learn 不使用）
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: モジュール定数
# ==============================================================

_MODULE_VERSION = "1.0"
_CACHE_TTL_DAYS = 30          # cluster_master.json の有効期間

# 承認済み taxonomy seed の場所（taxonomy_loader.py / taxonomy_hotness.py と同一解決）
_REPO = Path(__file__).resolve().parent
_SEED_DIR = _REPO / "data" / "taxonomy_seed"


# ==============================================================
# BLOCK 0b: TECH_KEYWORD_MAP の動的構築（seed正本）
# ==============================================================
# 2026-07-13: TECH_KEYWORD_MAPはSSOT再建で消失（幻キー）→承認済みseedから動的構築へ

def build_keyword_map_from_seeds(seed_dir: Path = _SEED_DIR) -> dict:
    """
    承認済み taxonomy seed（data/taxonomy_seed/batch*.json）から
    {node_id: [キーワード...]} を動的構築する。

    - verified_flag=true のノードのみ対象
    - キーワード = node_name_ja / node_name_en / node_id をアンダースコア分割した
      英単語（2文字以下は除外）。description は誤マッチ源になるため使わない
    - seedすら読めない場合は警告して空 dict を返す（クラッシュさせない）

    Returns:
        {"semiconductor": ["半導体", "Semiconductor", "semiconductor"], ...}
    """
    keyword_map: dict = {}
    try:
        batch_files = sorted(Path(seed_dir).glob("batch*.json"))
        for p in batch_files:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                print(
                    f"  [technology_mapper] seed読込失敗 "
                    f"({p.name}: {str(e)[:60]}): スキップ"
                )
                continue

            for node in d.get("nodes", []):
                if not node.get("verified_flag"):
                    continue
                node_id = node.get("node_id")
                if not node_id:
                    continue

                kws: list[str] = []
                seen: set[str] = set()

                def _add(term):
                    if not term:
                        return
                    term = str(term).strip()
                    if not term or term in seen:
                        return
                    seen.add(term)
                    kws.append(term)

                _add(node.get("node_name_ja"))
                _add(node.get("node_name_en"))
                # node_id をアンダースコア分割した英単語（2文字以下は除外）
                for tok in str(node_id).split("_"):
                    if len(tok) > 2:
                        _add(tok)

                keyword_map[node_id] = kws
    except Exception as e:
        print(
            f"  [technology_mapper] ⚠️  seedからのキーワード構築失敗 "
            f"({str(e)[:80]}): 空を返す"
        )
        return {}

    if not keyword_map:
        print(
            "  [technology_mapper] ⚠️  seedから技術キーワードを構築できませんでした "
            f"（{seed_dir} 確認）"
        )
    return keyword_map


def _get_keyword_map(cfg) -> dict:
    """
    技術キーワード表を取得する。
    config.TECH_KEYWORD_MAP が定義されていればそちら優先（将来の手動上書き用）、
    無ければ承認済みseedから自動構築する。
    """
    cfg_map = getattr(cfg, "TECH_KEYWORD_MAP", {})
    if cfg_map:
        return cfg_map
    return build_keyword_map_from_seeds()


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _load_cache(cache_path: Path) -> Optional[dict]:
    """
    cluster_master.json を読み込む。
    TTL切れ・未存在・破損時は None を返す。
    """
    try:
        if not cache_path.exists():
            return None
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        generated_str = raw.get("generated", "")
        if not generated_str:
            return None
        generated_dt = datetime.fromisoformat(generated_str)
        if datetime.now() - generated_dt > timedelta(days=_CACHE_TTL_DAYS):
            print(
                f"  [technology_mapper] キャッシュ期限切れ "
                f"({_CACHE_TTL_DAYS}日超): 再計算"
            )
            return None
        return raw
    except Exception as e:
        print(
            f"  [technology_mapper] キャッシュ読込失敗 "
            f"({str(e)[:60]}): 再計算"
        )
        return None


def _save_cache(cache_path: Path, data: dict) -> bool:
    """
    cluster_master.json へ書き出す。
    親ディレクトリが存在しない場合は作成する。
    """
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        print(
            f"  [technology_mapper] キャッシュ書込失敗 "
            f"({str(e)[:60]})"
        )
        return False


# ==============================================================
# BLOCK 2: サブテーマ→技術ノード マッピング計算
# ==============================================================

def _calc_keyword_overlap_score(
    subthemes: list[str],
    keywords: list[str],
) -> float:
    """
    subthemes リストと keywords リストのキーワード重複スコアを計算する。

    スコア計算式:
      score = \\frac{\\text{match\\_count}}{\\sqrt{len(subthemes) + 1}}

    部分一致（キーワードが subtheme 文字列に含まれる）を1件0.5点、
    完全一致を1件1.0点として集計する。
    上限は 1.0 にクリップする。

    Args:
        subthemes: サブテーマ文字列リスト（jp_sector_subtheme.json の subthemes）
        keywords:  技術ノードのキーワードリスト（config.TECH_KEYWORD_MAP の value）

    Returns:
        float: 0.0 〜 1.0 の relevance_score
    """
    if not subthemes or not keywords:
        return 0.0

    # subthemes を小文字化して照合（英語キーワード対応）
    subtheme_lower = [str(s).lower() for s in subthemes]
    kw_lower = [str(k).lower() for k in keywords]

    match_score = 0.0
    for kw in kw_lower:
        for st in subtheme_lower:
            if kw == st:
                match_score += 1.0
                break
            elif kw in st or st in kw:
                match_score += 0.5
                break

    if match_score == 0.0:
        return 0.0

    # score = match\_score / \sqrt{len(subthemes) + 1}
    raw = match_score / math.sqrt(len(subthemes) + 1)
    return round(min(1.0, raw), 4)


def _build_sector_tech_matrix(
    sectors: dict,
    tech_keyword_map: dict,
    market: str,
) -> dict:
    """
    業種コード × 技術ノードの relevance_score 行列を構築する。

    Args:
        sectors:          load_all_subthemes() の返り値
                          {"1016": {"subthemes": [...], ...}, ...}
        tech_keyword_map: config.TECH_KEYWORD_MAP
        market:           "JP" or "US"

    Returns:
        {
          "1016": {
            "ai_semiconductor": 0.72,
            "power_semiconductor": 0.55,
            ...
          },
          ...
        }
        ※ relevance_score == 0.0 のノードは除外する
    """
    matrix = {}
    for sector_code, sector_info in sectors.items():
        subthemes = sector_info.get("subthemes", [])
        if not subthemes:
            continue

        node_scores = {}
        for node_id, keywords in tech_keyword_map.items():
            score = _calc_keyword_overlap_score(subthemes, keywords)
            if score > 0.0:
                node_scores[node_id] = score

        if node_scores:
            matrix[str(sector_code)] = node_scores

    return matrix


# ==============================================================
# BLOCK 3: 業種メタ情報の付加
# ==============================================================

def _enrich_sector_meta(
    sector_code: str,
    sector_info: dict,
    cfg,
    market: str,
) -> dict:
    """
    業種コードに対して name_ja / supply_chain_position 等のメタ情報を付加する。

    Args:
        sector_code:  "1016" (JP) / "US_45" (US)
        sector_info:  subtheme_loader.load_subtheme() の返り値
        cfg:          config モジュール
        market:       "JP" or "US"

    Returns:
        {
          "name_ja":               str,
          "supply_chain_position": str,
          "business_cycle_sensitivity": str,
          "export_dependency":     str,
        }
    """
    try:
        # JP の場合は config.JAPAN_SECTORS から正式名を取得
        if market == "JP":
            japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})
            jp_meta = japan_sectors.get(str(sector_code), {})
            name_ja = jp_meta.get("name", sector_info.get("name_ja", ""))
        else:
            name_ja = sector_info.get("name_ja", sector_code)

        return {
            "name_ja":                    str(name_ja),
            "supply_chain_position":      str(sector_info.get("supply_chain_position", "unknown")),
            "business_cycle_sensitivity": str(sector_info.get("business_cycle_sensitivity", "cyclical")),
            "export_dependency":          str(sector_info.get("export_dependency", "medium")),
        }
    except Exception:
        return {
            "name_ja":                    sector_code,
            "supply_chain_position":      "unknown",
            "business_cycle_sensitivity": "cyclical",
            "export_dependency":          "medium",
        }


# ==============================================================
# BLOCK 4: cluster_master.json 構築
# ==============================================================

def build_cluster_master(cfg) -> dict:
    """
    JP + US 両市場の業種×技術マッピング行列を構築し、
    cluster_master.json 形式の dict を返す。

    Returns:
        {
          "generated":  "2026-06-03T08:00:00",
          "version":    "1.0",
          "ttl_days":   30,
          "markets": {
            "JP": {
              "1016": {
                "meta": {
                  "name_ja": "電気機器",
                  "supply_chain_position": "downstream",
                  ...
                },
                "tech_scores": {
                  "ai_semiconductor": 0.72,
                  "power_semiconductor": 0.55,
                  ...
                }
              },
              ...
            },
            "US": { ... }
          }
        }
    """
    try:
        from subtheme_loader import load_all_subthemes
    except ImportError as e:
        print(f"  [technology_mapper] subtheme_loader import失敗: {e}")
        return {}

    tech_keyword_map = _get_keyword_map(cfg)
    if not tech_keyword_map:
        print("  [technology_mapper] ⚠️  技術キーワード表が空です（seed/config.py 確認）")
        return {}

    result = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "version":   _MODULE_VERSION,
        "ttl_days":  _CACHE_TTL_DAYS,
        "markets":   {},
    }

    for market in ("JP", "US"):
        try:
            sectors = load_all_subthemes(market=market, cfg=cfg)
            if not sectors:
                print(
                    f"  [technology_mapper] ⚠️  {market} サブテーマ未ロード "
                    f"（スキップ）"
                )
                continue

            matrix = _build_sector_tech_matrix(sectors, tech_keyword_map, market)

            market_data = {}
            for sector_code, node_scores in matrix.items():
                sector_info = sectors.get(sector_code, {})
                meta = _enrich_sector_meta(sector_code, sector_info, cfg, market)
                market_data[sector_code] = {
                    "meta":        meta,
                    "tech_scores": node_scores,
                }

            result["markets"][market] = market_data
            print(
                f"  [technology_mapper] ✅ {market} マッピング完了: "
                f"{len(market_data)}業種 × {len(tech_keyword_map)}技術ノード"
            )

        except Exception as e:
            print(
                f"  [technology_mapper] ⚠️  {market} マッピング失敗 "
                f"({str(e)[:80]}): スキップ"
            )

    return result


# ==============================================================
# BLOCK 5: パブリック API
# ==============================================================

def get_sector_tech_scores(
    sector_code: str,
    market:      str = "JP",
    cfg=None,
    cache_path:  Optional[Path] = None,
) -> dict:
    """
    指定業種の技術ノードスコア辞書を返す。
    technology_cluster_engine.py が呼び出す。

    Args:
        sector_code: "1016" (JP) / "US_45" (US)
        market:      "JP" or "US"
        cfg:         config モジュール（None 可: キャッシュのみ参照）
        cache_path:  cluster_master.json のパス（None 時は cfg から取得）

    Returns:
        {"ai_semiconductor": 0.72, "power_semiconductor": 0.55, ...}
        ※ 未定義・エラー時は空 dict
    """
    try:
        if cache_path is None and cfg is not None:
            cache_path = getattr(cfg, "CLUSTER_MASTER_CACHE", None)
        if cache_path is None:
            return {}

        cached = _load_cache(Path(cache_path))
        if cached:
            return dict(
                cached.get("markets", {})
                      .get(market, {})
                      .get(str(sector_code), {})
                      .get("tech_scores", {})
            )
        return {}
    except Exception:
        return {}


def run_technology_mapper(cfg, results: dict) -> dict:
    """
    メインエントリポイント。stock_analyzer_main.py から呼び出す。

    処理フロー:
      1. cluster_master.json キャッシュを確認
      2. TTL切れまたは未存在なら build_cluster_master() で再計算
      3. キャッシュ保存
      4. results["cluster_master"] に結果を格納

    Args:
        cfg:     config モジュール
        results: run_all_tasks() の results dict（in-place 更新）

    Returns:
        {
          "success":      bool,
          "sector_count": int,   # マッピングした業種数
          "node_count":   int,   # 技術ノード数
          "cache_hit":    bool,
          "error":        str | None,
        }
    """
    out = {
        "success":      False,
        "sector_count": 0,
        "node_count":   0,
        "cache_hit":    False,
        "error":        None,
    }

    try:
        if not getattr(cfg, "ENABLE_TECHNOLOGY_MAPPER", True):
            print("  ⏩ technology_mapper: スキップ（ENABLE_TECHNOLOGY_MAPPER=False）")
            out["success"] = True
            return out

        cache_path = Path(getattr(cfg, "CLUSTER_MASTER_CACHE",
                                  "stock_data/cache/cluster_master.json"))

        # ① キャッシュ確認
        cached = _load_cache(cache_path)
        if cached:
            print(
                f"  [technology_mapper] ✅ キャッシュ利用 "
                f"(generated={cached.get('generated', 'unknown')})"
            )
            results["cluster_master"] = cached
            jp_count = len(cached.get("markets", {}).get("JP", {}))
            us_count = len(cached.get("markets", {}).get("US", {}))
            tech_keyword_map = _get_keyword_map(cfg)
            out.update({
                "success":      True,
                "sector_count": jp_count + us_count,
                "node_count":   len(tech_keyword_map),
                "cache_hit":    True,
            })
            return out

        # ② 再計算
        print("  [technology_mapper] クラスターマスター再計算中...")
        cluster_master = build_cluster_master(cfg)

        if not cluster_master or not cluster_master.get("markets"):
            out["error"] = "build_cluster_master() が空を返した"
            print(f"  [technology_mapper] ⚠️  {out['error']}")
            return out

        # ③ キャッシュ保存
        _save_cache(cache_path, cluster_master)

        # ④ results に格納
        results["cluster_master"] = cluster_master

        jp_count = len(cluster_master.get("markets", {}).get("JP", {}))
        us_count = len(cluster_master.get("markets", {}).get("US", {}))
        tech_keyword_map = _get_keyword_map(cfg)

        out.update({
            "success":      True,
            "sector_count": jp_count + us_count,
            "node_count":   len(tech_keyword_map),
            "cache_hit":    False,
        })

        print(
            f"  [technology_mapper] ✅ 完了: "
            f"JP={jp_count}業種 US={us_count}業種 "
            f"/ {len(tech_keyword_map)}技術ノード"
        )
        return out

    except Exception as e:
        out["error"] = str(e)[:120]
        print(f"  [technology_mapper] ⚠️  実行エラー: {out['error']}")
        return out


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  technology_mapper.py v1.0 単体テスト")
    print("=" * 60)

    try:
        import config as cfg
    except ImportError:
        print("  ⚠️  config.py が見つかりません（テスト中断）")
        raise SystemExit(1)

    results = {}
    out = run_technology_mapper(cfg, results)
    print(f"\n  結果: {out}")

    if results.get("cluster_master"):
        jp_data = results["cluster_master"].get("markets", {}).get("JP", {})
        if jp_data:
            sample_code = next(iter(jp_data))
            sample = jp_data[sample_code]
            print(f"\n  ── サンプル業種 [{sample_code}] ──")
            print(f"  name_ja: {sample['meta']['name_ja']}")
            print(f"  tech_scores top3: "
                  f"{sorted(sample['tech_scores'].items(), key=lambda x: -x[1])[:3]}")

    print("\n✅ 単体テスト完了")