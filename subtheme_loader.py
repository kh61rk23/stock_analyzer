# ==============================================================
# subtheme_loader.py  v1.0  (Wave 2 - R-1)
# サブテーマ JSON ローダー 共通ユーティリティ
# ==============================================================
# 【役割】
#   jp_sector_subtheme.json / us_sector_subtheme.json から
#   サブテーマ情報を読み込み、各モジュールに提供する。
#
# 【呼び出しパターン（base_agent.py から）】
#   from subtheme_loader import get_supply_chain_position, load_subtheme
#   pos = get_supply_chain_position(sector_code="1016", market="JP", cfg=cfg)
#   # -> "downstream"
#
# 【設計方針】
#   - 初回ロード後はモジュールレベルキャッシュに保持（再読み込み不要）
#   - JSON 未存在・破損時は空dict を返す（例外は try/except で完全吸収）
#   - cfg が None の場合はファイルパスを相対パス候補でフォールバック探索
#   - 全関数は例外を外部に伝播しない（システム停止を防ぐため）
#
# 【依存】
#   config.py (SUBTHEME_JSON_PATH_JP / SUBTHEME_JSON_PATH_US)
#   追加ライブラリなし（標準ライブラリのみ）
# ==============================================================

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: モジュールレベルキャッシュ
# ==============================================================

_cache: dict[str, dict] = {
    "JP": {},  # jp_sector_subtheme.json のロード結果
    "US": {},  # us_sector_subtheme.json のロード結果
}

_loaded: dict[str, bool] = {
    "JP": False,
    "US": False,
}

# JSONファイルのフォールバック候補パス（cfg が None の場合に探索）
_FALLBACK_PATHS: dict[str, list[str]] = {
    "JP": [
        "data/jp_sector_subtheme.json",
        "jp_sector_subtheme.json",
        "/content/drive/MyDrive/stock_analyzer/data/jp_sector_subtheme.json",
        "/content/jp_sector_subtheme.json",
    ],
    "US": [
        "data/us_sector_subtheme.json",
        "us_sector_subtheme.json",
        "/content/drive/MyDrive/stock_analyzer/data/us_sector_subtheme.json",
        "/content/us_sector_subtheme.json",
    ],
}


# ==============================================================
# BLOCK 1: JSONロード（モジュールキャッシュ付き）
# ==============================================================

def _load_json(market: str, cfg=None) -> dict:
    """
    対象 JSON を読み込み、モジュールキャッシュに格納する。
    既にロード済みであれば即時返却。
    失敗時は空 dict を返す（例外は吸収）。

    Args:
        market: "JP" or "US"
        cfg:    config モジュール（None 可）

    Returns:
        {"version": str, "sectors": {code: subtheme_dict}, ...}
    """
    global _loaded, _cache

    if _loaded.get(market):
        return _cache.get(market, {})

    try:
        # ── cfg からパスを優先取得 ───────────────────────────────
        json_path: Optional[Path] = None

        if cfg is not None:
            attr = (
                "SUBTHEME_JSON_PATH_JP" if market == "JP"
                else "SUBTHEME_JSON_PATH_US"
            )
            p = getattr(cfg, attr, None)
            if p is not None:
                json_path = Path(p)

        # ── cfg なし、またはパスが存在しない場合のフォールバック ──
        if json_path is None or not json_path.exists():
            for candidate in _FALLBACK_PATHS.get(market, []):
                cp = Path(candidate)
                if cp.exists():
                    json_path = cp
                    break

        if json_path is None or not json_path.exists():
            # JSON が見つからない場合は空 dict（ログのみ）
            print(
                f"  [subtheme_loader] ⚠️  {market} サブテーマJSON が見つかりません "
                f"（スキップ）"
            )
            _loaded[market] = True   # 再探索しない
            _cache[market]  = {}
            return {}

        raw = json.loads(json_path.read_text(encoding="utf-8"))
        _cache[market]  = raw
        _loaded[market] = True
        print(
            f"  [subtheme_loader] ✅ {market} サブテーマJSON ロード完了: "
            f"{len(raw.get('sectors', {}))}件 ({json_path})"
        )
        return raw

    except Exception as e:
        print(
            f"  [subtheme_loader] ⚠️  {market} サブテーマJSON 読込エラー "
            f"({str(e)[:60]}), 空dictで続行"
        )
        _loaded[market] = True
        _cache[market]  = {}
        return {}


# ==============================================================
# BLOCK 2: パブリック API
# ==============================================================

def load_subtheme(
    sector_code: str,
    market:      str  = "JP",
    cfg=None,
) -> dict:
    """
    指定セクターのサブテーマ情報辞書を返す。

    Args:
        sector_code: "1016"（JP）/ "US_45"（US）形式
        market:      "JP" or "US"
        cfg:         config モジュール（None 可）

    Returns:
        {
          "name_ja":                    str,
          "supply_chain_position":      str,
          "business_cycle_sensitivity": str,
          "export_dependency":          str,
          "digital_transformation_stage": str,
          "esg_priority":               str,
          "bm_type":                    str | None,
          "subthemes":                  list[str],
          ...
        }
        ※ セクター未定義・JSON 未ロードの場合は空 dict
    """
    try:
        raw = _load_json(market, cfg)
        sectors = raw.get("sectors", {})
        return dict(sectors.get(str(sector_code), {}))
    except Exception:
        return {}


def get_supply_chain_position(
    sector_code: str,
    market:      str  = "JP",
    cfg=None,
    default:     str  = "unknown",
) -> str:
    """
    supply_chain_position を返す簡易ラッパー。
    base_agent.py で直接呼び出す。

    Args:
        sector_code: "1016" / "US_45" 形式
        market:      "JP" or "US"
        cfg:         config モジュール（None 可）
        default:     取得失敗時のデフォルト値

    Returns:
        "upstream" | "midstream" | "downstream" | "service" | "financial" | default
    """
    try:
        info = load_subtheme(sector_code, market, cfg)
        return str(info.get("supply_chain_position", default)) or default
    except Exception:
        return default


def get_subthemes(
    sector_code: str,
    market:      str  = "JP",
    cfg=None,
) -> list[str]:
    """
    subthemes リストを返す簡易ラッパー。

    Returns:
        ["AI半導体", "パワー半導体（EV）", ...]
        ※ 未定義の場合は空リスト
    """
    try:
        info = load_subtheme(sector_code, market, cfg)
        return list(info.get("subthemes", []))
    except Exception:
        return []


def get_bm_type(
    sector_code: str,
    market:      str  = "JP",
    cfg=None,
) -> Optional[str]:
    """
    bm_type を返す簡易ラッパー。
    business_model_normalizer.py が業態判定に使用する。

    Returns:
        "BM-01" | "BM-02" | "BM-03" | "BM-US-FIN" | "BM-US-UTIL" | None
    """
    try:
        info = load_subtheme(sector_code, market, cfg)
        val = info.get("bm_type")
        return str(val) if val is not None else None
    except Exception:
        return None


def get_rate_sensitivity(
    sector_code: str,
    market:      str  = "US",
    cfg=None,
    default:     str  = "neutral",
) -> str:
    """
    rate_sensitivity を返す（USセクター専用）。
    signal_aggregator.py の金利シナリオ補正で参照。

    Returns:
        "positive" | "negative" | "neutral" | default
    """
    try:
        info = load_subtheme(sector_code, market, cfg)
        return str(info.get("rate_sensitivity", default)) or default
    except Exception:
        return default


def get_business_cycle_sensitivity(
    sector_code: str,
    market:      str  = "JP",
    cfg=None,
    default:     str  = "cyclical",
) -> str:
    """
    business_cycle_sensitivity を返す簡易ラッパー。

    Returns:
        "cyclical" | "defensive" | "growth" | "financial" | default
    """
    try:
        info = load_subtheme(sector_code, market, cfg)
        return str(info.get("business_cycle_sensitivity", default)) or default
    except Exception:
        return default


# ==============================================================
# BLOCK 3: キャッシュリセット（テスト用）
# ==============================================================

def reset_cache(market: Optional[str] = None):
    """
    モジュールキャッシュをリセットする（単体テスト・再ロード用）。

    Args:
        market: "JP" / "US" を指定すると対象市場のみリセット。
                None を指定すると全キャッシュをリセット。
    """
    global _loaded, _cache
    targets = [market] if market in ("JP", "US") else ["JP", "US"]
    for m in targets:
        _loaded[m] = False
        _cache[m]  = {}


# ==============================================================
# BLOCK 4: 全セクター一括取得（週次レポート用）
# ==============================================================

def load_all_subthemes(market: str = "JP", cfg=None) -> dict:
    """
    全セクターのサブテーマ辞書を返す。
    weekly_report_generator.py からの一括参照で使用。

    Returns:
        {"1016": {...}, "1025": {...}, ...}
    """
    try:
        raw = _load_json(market, cfg)
        return dict(raw.get("sectors", {}))
    except Exception:
        return {}

# ==============================================================
# BLOCK 5: BQ照合・同期ロジック（Wave 6 - R-2）
# ==============================================================

def sync_json_to_bq(
    market:  str  = "JP",
    cfg=None,
    force:   bool = False,
) -> dict:
    """
    JSON サブテーマ（jp_sector_subtheme.json / us_sector_subtheme.json）を
    BQ の sub_theme_master / ticker_subtheme_mapping に同期する。

    処理フロー:
      1. load_all_subthemes() でローカル JSON を読み込む
      2. get_sub_theme_master() で BQ の現在レコードを取得
      3. JSON と BQ の sector_code セットを比較（追加・更新検出）
      4. 差分がある場合（または force=True）に upsert を実行

    Args:
        market: "JP" or "US"
        cfg:    config モジュール（None 可）
        force:  True なら差分なしでも全件 upsert を実行

    Returns:
        {
          "market":          "JP",
          "json_count":      33,
          "bq_count":        30,
          "diff_count":      3,
          "synced":          True,
          "master_success":  True,
          "mapping_success": True,
          "master_count":    33,
          "mapping_count":   165,
          "error":           None,
        }
    """
    out = {
        "market":          market,
        "json_count":      0,
        "bq_count":        0,
        "diff_count":      0,
        "synced":          False,
        "master_success":  False,
        "mapping_success": False,
        "master_count":    0,
        "mapping_count":   0,
        "error":           None,
    }

    try:
        # ── Step 1: JSON ロード ─────────────────────────────────
        sectors = load_all_subthemes(market=market, cfg=cfg)
        if not sectors:
            out["error"] = f"{market} サブテーマJSON が空（ロード失敗）"
            print(f"  [subtheme_loader] ⚠️  {out['error']}")
            return out

        out["json_count"] = len(sectors)

        # バージョン情報を raw キャッシュから取得
        raw = _cache.get(market, {})
        version = str(raw.get("version", "1.0"))

        # ── Step 2: BQ 現在レコードを取得 ──────────────────────
        try:
            from edinet_bq_client import EDINETBQClient
            bq = EDINETBQClient()
            bq_rows = bq.get_sub_theme_master(market=market)
            bq_sector_codes = {str(r.get("sector_code", "")) for r in bq_rows}
            out["bq_count"] = len(bq_rows)
        except Exception as _bq_e:
            print(
                f"  [subtheme_loader] BQ接続失敗: {str(_bq_e)[:80]} "
                f"→ 強制 upsert で続行"
            )
            bq_sector_codes = set()
            out["bq_count"] = 0

        # ── Step 3: 差分チェック ────────────────────────────────
        json_sector_codes = set(str(k) for k in sectors.keys())
        diff_codes = json_sector_codes - bq_sector_codes
        out["diff_count"] = len(diff_codes)

        if not force and not diff_codes:
            # 差分なし・force=False → 更新不要
            out["synced"] = True
            out["master_success"]  = True
            out["mapping_success"] = True
            print(
                f"  [subtheme_loader] ✅ {market} BQ同期スキップ "
                f"（差分なし: JSON={out['json_count']}件 / BQ={out['bq_count']}件）"
            )
            return out

        print(
            f"  [subtheme_loader] {market} BQ同期開始: "
            f"JSON={out['json_count']}件 / BQ差分={out['diff_count']}件 "
            f"/ force={force}"
        )

        # ── Step 4: upsert 実行 ─────────────────────────────────
        try:
            sync_result = bq.sync_subtheme_json_to_bq(
                sectors=sectors,
                market=market,
                version=version,
            )
            out.update({
                "synced":          True,
                "master_success":  sync_result.get("master_success",  False),
                "mapping_success": sync_result.get("mapping_success", False),
                "master_count":    sync_result.get("master_count",    0),
                "mapping_count":   sync_result.get("mapping_count",   0),
            })
            print(
                f"  [subtheme_loader] ✅ {market} BQ同期完了: "
                f"master={out['master_count']}件 / "
                f"mapping={out['mapping_count']}件"
            )
        except Exception as _sync_e:
            out["error"] = str(_sync_e)[:120]
            print(
                f"  [subtheme_loader] ⚠️  BQ upsert 失敗: {out['error']}"
            )

        return out

    except Exception as e:
        out["error"] = str(e)[:120]
        print(f"  [subtheme_loader] ⚠️  sync_json_to_bq 実行エラー: {out['error']}")
        return out


def sync_all_markets_to_bq(cfg=None, force: bool = False) -> dict:
    """
    JP + US 両市場を一括で BQ 同期する。
    stock_analyzer_main.py の週次バッチから呼び出す。

    Args:
        cfg:   config モジュール（None 可）
        force: True なら差分なしでも全件 upsert

    Returns:
        {
          "JP": {sync_json_to_bq の返り値},
          "US": {sync_json_to_bq の返り値},
          "success": bool,   # JP・US 両方とも synced=True なら True
        }
    """
    out: dict = {}
    try:
        jp_result = sync_json_to_bq(market="JP", cfg=cfg, force=force)
        out["JP"]  = jp_result
    except Exception as e:
        out["JP"]  = {"synced": False, "error": str(e)[:80]}

    try:
        us_result = sync_json_to_bq(market="US", cfg=cfg, force=force)
        out["US"]  = us_result
    except Exception as e:
        out["US"]  = {"synced": False, "error": str(e)[:80]}

    out["success"] = (
        out.get("JP", {}).get("synced", False)
        and out.get("US", {}).get("synced", False)
    )
    return out


def compare_json_bq(
    market: str = "JP",
    cfg=None,
) -> dict:
    """
    JSON と BQ の sector_code セットを比較して差分を返す。
    デバッグ・運用確認用。

    Returns:
        {
          "only_in_json": ["sector_code", ...],   # JSON にあって BQ にない
          "only_in_bq":   ["sector_code", ...],   # BQ にあって JSON にない
          "in_both":      ["sector_code", ...],   # 両方に存在
          "json_count":   int,
          "bq_count":     int,
        }
    """
    try:
        sectors        = load_all_subthemes(market=market, cfg=cfg)
        json_codes     = set(str(k) for k in sectors.keys())

        from edinet_bq_client import EDINETBQClient
        bq        = EDINETBQClient()
        bq_rows   = bq.get_sub_theme_master(market=market)
        bq_codes  = set(str(r.get("sector_code", "")) for r in bq_rows)

        return {
            "only_in_json": sorted(json_codes - bq_codes),
            "only_in_bq":   sorted(bq_codes - json_codes),
            "in_both":      sorted(json_codes & bq_codes),
            "json_count":   len(json_codes),
            "bq_count":     len(bq_codes),
        }
    except Exception as e:
        return {
            "only_in_json": [],
            "only_in_bq":   [],
            "in_both":      [],
            "json_count":   0,
            "bq_count":     0,
            "error":        str(e)[:80],
        }

# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  subtheme_loader.py v1.0 単体テスト")
    print("=" * 60)

    # cfg なし（フォールバックパスで探索）でのテスト
    print("\n── JP 電気機器（1016）──")
    info = load_subtheme("1016", "JP")
    if info:
        print(f"  supply_chain_position: {info.get('supply_chain_position')}")
        print(f"  business_cycle_sensitivity: {info.get('business_cycle_sensitivity')}")
        print(f"  subthemes: {info.get('subthemes', [])[:3]}")
    else:
        print("  ⚠️  JSONが見つかりませんでした（data/jp_sector_subtheme.json を確認）")

    print("\n── US 情報技術（US_45）──")
    info_us = load_subtheme("US_45", "US")
    if info_us:
        print(f"  supply_chain_position: {info_us.get('supply_chain_position')}")
        print(f"  rate_sensitivity: {info_us.get('rate_sensitivity')}")
        print(f"  subthemes: {info_us.get('subthemes', [])[:3]}")
    else:
        print("  ⚠️  JSONが見つかりませんでした（data/us_sector_subtheme.json を確認）")

    print("\n── 簡易ラッパーテスト ──")
    print(f"  get_supply_chain_position('1026', 'JP'): "
          f"{get_supply_chain_position('1026', 'JP')}")
    print(f"  get_bm_type('1028', 'JP'): {get_bm_type('1028', 'JP')}")
    print(f"  get_rate_sensitivity('US_40', 'US'): "
          f"{get_rate_sensitivity('US_40', 'US')}")
    print(f"  get_business_cycle_sensitivity('1020', 'JP'): "
          f"{get_business_cycle_sensitivity('1020', 'JP')}")

    print("\n✅ 単体テスト完了")
