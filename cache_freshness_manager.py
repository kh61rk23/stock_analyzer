# ==============================================================
# cache_freshness_manager.py  v1.0  (② キャッシュ鮮度管理)
# ==============================================================
# 【役割】
#   各キャッシュに「鮮度スコア（0.0〜1.0）」を付与し、
#   「古くなるほど信頼度が下がる」鮮度減衰を実現する。
#
# 【鮮度計算式】
#   freshness = exp(-経過時間h / half_life_h)
#
#   half_life_h の設定:
#     yfinance price         :   4時間
#     macro_snapshot         :  24時間
#     gov_data_pipeline      : 168時間（7日）
#     EDINET財務（日本）     : 720時間（30日）
#     SEC EDGAR財務（米国）  : 720時間（30日）
#     rule_weights           : 720時間（30日）
#
# 【スコアへの適用】
#   composite_score_adjusted =
#     cached_score * (1 - freshness) +
#     today_score  * freshness
#
#   freshness < 0.3（= 約10日超過 / 30日半減期の場合）→ 強制再取得フラグ
#
# 【統合先】
#   edinet_rotation_manager.py : load_db() 後に freshness を付与
#   us_fundamental_rotator.py  : lookup_us_fundamentals() 後に freshness を付与
#   run_all_tasks() 内から     : get_freshness_report() で全キャッシュを一括確認
#
# 【依存】math / config.py（任意）
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 半減期定義
# ==============================================================

# キャッシュ種別 → 半減期（時間）
# config.py の CACHE_HALF_LIFE_HOURS で上書き可能
DEFAULT_HALF_LIFE_HOURS: dict[str, float] = {
    # 市場データ（価格・オプション）
    "yfinance_price":       4.0,
    "options_monitor":      4.0,
    "macro_snapshot":      24.0,
    "us_8k":               20.0,
    "policy_radar":        24.0,

    # 中期データ（週次）
    "gov_data":           168.0,   # 7日
    "google_trends":      168.0,
    "sentiment":          168.0,
    "us_macro":            12.0,   # 日次だが12時間キャッシュ

    # 長期データ（月次・四半期）
    "edinet":             720.0,   # 30日
    "sec_edgar":          720.0,   # 30日
    "rule_weights":       720.0,   # 30日
    "us_sector_mapping":  720.0,   # 30日
    "longterm_scenario":  168.0,   # 週次更新
}

# freshness がこれ未満で強制再取得フラグを立てる

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'CFM_STALE_THRESHOLD': 'STALE_THRESHOLD',
    'CFM_IGNORE_THRESHOLD': 'IGNORE_THRESHOLD',
    'B4_CACHE_FRESHNESS_MANAGER_DEFAULT_HALF_LIFE_HOURS': 'DEFAULT_HALF_LIFE_HOURS',
}

STALE_THRESHOLD  = 0.30   # ≈ 経過時間 > 半減期 × 1.74

# freshness がこれ未満でスコアをゼロ重みにする（信頼しない）
IGNORE_THRESHOLD = 0.05   # ≈ 経過時間 > 半減期 × 4.3


# ==============================================================
# BLOCK 1: 鮮度計算コア
# ==============================================================

def calc_freshness(
    saved_at_str:   Optional[str],
    cache_type:     str,
    cfg = None,
) -> float:
    """
    キャッシュの保存日時と種別から鮮度スコアを計算する。

    Args:
        saved_at_str : キャッシュ保存日時（ISO形式 or "YYYY-MM-DD HH:MM:SS"）
        cache_type   : キャッシュ種別（DEFAULT_HALF_LIFE_HOURS のキー）
        cfg          : config モジュール（CACHE_HALF_LIFE_HOURS で上書き可能）

    Returns:
        freshness: 0.0（古い）〜 1.0（新鮮）
    """
    if not saved_at_str:
        return 0.0

    # half_life 取得（config 優先）
    half_life_map = getattr(cfg, "CACHE_HALF_LIFE_HOURS", {}) if cfg else {}
    half_life = float(
        half_life_map.get(cache_type) or
        DEFAULT_HALF_LIFE_HOURS.get(cache_type, 168.0)
    )

    # 経過時間（時間単位）
    try:
        saved_at = datetime.fromisoformat(saved_at_str)
    except ValueError:
        try:
            saved_at = datetime.strptime(saved_at_str[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0.0

    elapsed_hours = (datetime.now() - saved_at).total_seconds() / 3600.0
    if elapsed_hours < 0:
        elapsed_hours = 0.0

    return round(math.exp(-elapsed_hours / half_life), 6)


def is_stale(
    saved_at_str: Optional[str],
    cache_type:   str,
    cfg = None,
    threshold:    float = STALE_THRESHOLD,
) -> bool:
    """
    キャッシュが閾値未満の鮮度（= 陳腐化）か判定する。

    Returns:
        True = 再取得推奨
    """
    return calc_freshness(saved_at_str, cache_type, cfg) < threshold


def blend_scores(
    cached_score:  float,
    today_score:   float,
    freshness:     float,
) -> float:
    """
    鮮度に基づいてキャッシュ値と当日値を線形ブレンドする。

    blend = cached_score * (1 - freshness) + today_score * freshness

    freshness = 1.0 → 当日値のみを採用（完全新鮮）
    freshness = 0.0 → キャッシュ値のみを採用（完全陳腐）

    Args:
        cached_score : キャッシュ済みスコア
        today_score  : 当日算出スコア
        freshness    : calc_freshness() の出力

    Returns:
        ブレンド後スコア（元のスケールを維持）
    """
    return round(
        cached_score * (1.0 - freshness) + today_score * freshness,
        3,
    )


# ==============================================================
# BLOCK 2: EDINET DB への鮮度付与
# ==============================================================

def enrich_edinet_db_with_freshness(
    edinet_db: dict,
    cfg = None,
) -> dict:
    """
    edinet_rotation_manager.load_db() の結果に鮮度情報を付与する。

    各銘柄エントリの "_saved_at" から freshness を計算し、
    "freshness" / "stale" / "force_refresh" フィールドを追加する。

    Args:
        edinet_db : {ticker: {credit_score, _saved_at, ...}} の dict
        cfg       : config モジュール

    Returns:
        freshness フィールドを追加した edinet_db（in-place 更新）
    """
    stale_tickers   = []
    refresh_tickers = []

    for ticker, data in edinet_db.items():
        if not isinstance(data, dict):
            continue

        saved_at  = data.get("_saved_at") or data.get("last_fetched")
        freshness = calc_freshness(saved_at, "edinet", cfg)
        stale     = freshness < STALE_THRESHOLD
        ignore    = freshness < IGNORE_THRESHOLD

        data["freshness"]      = freshness
        data["stale"]          = stale
        data["force_refresh"]  = ignore   # True = 次回ローテーションで最優先
        data["freshness_pct"]  = round(freshness * 100, 1)

        if ignore:
            refresh_tickers.append(ticker)
        elif stale:
            stale_tickers.append(ticker)

    if refresh_tickers:
        print(f"    ⚠️  [鮮度] 強制再取得対象: {len(refresh_tickers)}社 "
              f"（freshness < {IGNORE_THRESHOLD:.0%}）")
    if stale_tickers:
        print(f"    📅 [鮮度] 陳腐化: {len(stale_tickers)}社 "
              f"（freshness < {STALE_THRESHOLD:.0%}）")

    return edinet_db


def get_edinet_credit_score_fresh(
    ticker:    str,
    edinet_db: dict,
    fallback:  float = 50.0,
) -> tuple[float, float]:
    """
    鮮度を考慮した credit_score を返す。

    freshness < IGNORE_THRESHOLD → fallback を返す（信頼しない）

    Returns:
        (credit_score: float, freshness: float)
    """
    data = edinet_db.get(ticker, {})
    if not isinstance(data, dict):
        return fallback, 0.0

    freshness    = data.get("freshness", 0.0)
    credit_score = data.get("credit_score")

    if credit_score is None or freshness < IGNORE_THRESHOLD:
        return fallback, freshness

    return float(credit_score), freshness


# ==============================================================
# BLOCK 3: SEC EDGAR（US財務）への鮮度付与
# ==============================================================

def enrich_us_fundamental_with_freshness(
    fund_data: dict,
    cfg = None,
) -> dict:
    """
    us_fundamental_rotator.lookup_us_fundamentals() の結果に鮮度情報を付与する。

    Args:
        fund_data : {ticker, eps_history, _saved_at, ...} の dict
        cfg       : config モジュール

    Returns:
        freshness フィールドを追加した fund_data（in-place 更新）
    """
    saved_at  = fund_data.get("_saved_at") or fund_data.get("last_fetched")
    freshness = calc_freshness(saved_at, "sec_edgar", cfg)

    fund_data["freshness"]      = freshness
    fund_data["stale"]          = freshness < STALE_THRESHOLD
    fund_data["force_refresh"]  = freshness < IGNORE_THRESHOLD
    fund_data["freshness_pct"]  = round(freshness * 100, 1)

    # EPS加速度スコアも鮮度で減衰させる
    raw_eps_accel = fund_data.get("eps_accel_score_xbrl")
    if raw_eps_accel is not None and freshness < STALE_THRESHOLD:
        # 陳腐化したEPSスコアは中立方向（0.0）にブレンド
        fund_data["eps_accel_score_fresh"] = round(
            raw_eps_accel * freshness, 2
        )
    else:
        fund_data["eps_accel_score_fresh"] = raw_eps_accel

    return fund_data


# ==============================================================
# BLOCK 4: 全キャッシュ一括鮮度レポート
# ==============================================================

CACHE_FILE_MAP: dict[str, str] = {
    "macro_snapshot":       "macro_snapshot_prev.json",
    "gov_data":             "gov_cache/cpi.json",
    "google_trends":        "trends_cache.json",
    "sentiment":            "sentiment_cache.json",
    "us_macro":             "us_macro_cache.json",
    "policy_radar":         "policy_radar_cache.json",
    "options_monitor":      "options_monitor_cache.json",
    "us_8k":                "us_8k_cache.json",
    "rule_weights":         "rule_weights.json",
    "us_sector_mapping":    "us_sector_mapping.json",
    "longterm_scenario":    "longterm_scenario_latest.json",
}


def get_freshness_report(
    cfg,
    print_summary: bool = True,
) -> dict:
    """
    全主要キャッシュの鮮度を一括チェックしてレポートを返す。

    run_all_tasks() の冒頭で呼び出すことで、
    実行前に陳腐化したキャッシュを把握できる。

    Returns:
        {
          cache_type: {
            "freshness":     float,
            "freshness_pct": float,
            "stale":         bool,
            "force_refresh": bool,
            "saved_at":      str | None,
          }, ...
        }
    """
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    report   = {}

    for cache_type, filename in CACHE_FILE_MAP.items():
        file_path = data_dir / filename
        saved_at  = None

        if file_path.exists():
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                # 保存日時キーを順に探す
                saved_at = (
                    raw.get("cached_at") or
                    raw.get("_saved_at") or
                    raw.get("timestamp") or
                    raw.get("last_updated") or
                    raw.get("generated_at")
                )
            except Exception:
                pass

        freshness = calc_freshness(saved_at, cache_type, cfg)
        report[cache_type] = {
            "freshness":     freshness,
            "freshness_pct": round(freshness * 100, 1),
            "stale":         freshness < STALE_THRESHOLD,
            "force_refresh": freshness < IGNORE_THRESHOLD,
            "saved_at":      saved_at,
            "file":          filename,
        }

    if print_summary:
        stale_list   = [k for k, v in report.items() if v["stale"] and not v["force_refresh"]]
        refresh_list = [k for k, v in report.items() if v["force_refresh"]]
        fresh_list   = [k for k, v in report.items() if not v["stale"]]

        print(f"  📊 キャッシュ鮮度レポート:")
        print(f"     新鮮（{len(fresh_list)}件）: {', '.join(fresh_list[:5])}")
        if stale_list:
            print(f"     陳腐化（{len(stale_list)}件）: {', '.join(stale_list[:5])}")
        if refresh_list:
            print(f"     強制再取得推奨（{len(refresh_list)}件）: "
                  f"{', '.join(refresh_list[:5])}")

    return report


# ==============================================================
# BLOCK 5: composite_score への鮮度ブレンド適用
# ==============================================================

def apply_freshness_to_sector_scores(
    sector_results: dict,
    edinet_db:      dict,
    cfg = None,
) -> dict:
    """
    sector_results の credit_score 由来の補正を鮮度でスケールする。

    screener の composite_score に EDINET 由来のボーナス（edinet_bonus）が
    含まれている場合、そのボーナスを鮮度スコアで減衰させる。

    fresh EDINET（freshness=1.0） → ボーナスをそのまま適用
    stale EDINET（freshness=0.3） → ボーナスを 30% に減衰

    Args:
        sector_results : {"sector_code": {"composite_score", "edinet_bonus", ...}}
        edinet_db      : enrich_edinet_db_with_freshness() 適用済みの DB
        cfg            : config モジュール

    Returns:
        freshness 減衰を適用した sector_results（in-place 更新）
    """
    for code, data in sector_results.items():
        if not isinstance(data, dict):
            continue

        edinet_bonus = data.get("edinet_bonus")
        if edinet_bonus is None or edinet_bonus == 0:
            continue

        # セクターの代表銘柄から freshness を推定
        sector_tickers = []
        japan_sectors = getattr(cfg, "JAPAN_SECTORS", {}) if cfg else {}
        reps = japan_sectors.get(code, {}).get("representatives", [])
        if isinstance(reps, list):
            sector_tickers = reps[:3]

        freshness_vals = []
        for t in sector_tickers:
            db_entry  = edinet_db.get(t, {})
            if isinstance(db_entry, dict):
                f = db_entry.get("freshness")
                if f is not None:
                    freshness_vals.append(f)

        if not freshness_vals:
            continue

        avg_freshness = sum(freshness_vals) / len(freshness_vals)

        # freshness < IGNORE_THRESHOLD → ボーナスを無効化
        if avg_freshness < IGNORE_THRESHOLD:
            data["edinet_bonus_original"] = edinet_bonus
            data["edinet_bonus"]          = 0.0
            data["freshness_zeroed"]      = True
        else:
            # freshness で線形スケール
            decayed_bonus = round(edinet_bonus * avg_freshness, 2)
            if decayed_bonus != edinet_bonus:
                data["edinet_bonus_original"] = edinet_bonus
                data["edinet_bonus"]          = decayed_bonus
                data["freshness_decayed"]     = True
                old_score = data.get("composite_score", 50.0)
                # ボーナス差分だけ composite_score を調整
                delta = decayed_bonus - edinet_bonus
                data["composite_score"] = round(
                    max(0.0, min(100.0, old_score + delta)), 1
                )

        data["edinet_freshness"] = round(avg_freshness, 4)

    return sector_results


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_cache_freshness_manager(cfg, results: dict | None = None) -> dict:
    """
    キャッシュ鮮度管理を実行する。

    run_all_tasks() の冒頭（fetch_macro_snapshot の直前）に呼び出す:
        _cfm = _load_local_module("cache_freshness_manager", ...)
        freshness_report = _cfm.run_cache_freshness_manager(cfg, results)
        results["freshness"] = freshness_report

    処理:
      1. 全主要キャッシュの鮮度を一括チェック
      2. 強制再取得フラグを results に格納
      3. EDINET DB に鮮度を付与（edinet_rotation 実行前）
      4. composite_score の EDINET ボーナスを鮮度で減衰

    Returns:
        {
          "report":           {cache_type: freshness_dict},
          "stale_caches":     [cache_type, ...],
          "refresh_caches":   [cache_type, ...],
          "edinet_enriched":  bool,
          "timestamp":        str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_CACHE_FRESHNESS", True):
        return {"skipped": True}

    print("\n  📊 キャッシュ鮮度管理 開始...")

    results = results or {}

    # ── Step1: 全キャッシュ鮮度レポート ─────────────────────
    report = get_freshness_report(cfg, print_summary=True)

    stale_caches   = [k for k, v in report.items() if v["stale"] and not v["force_refresh"]]
    refresh_caches = [k for k, v in report.items() if v["force_refresh"]]

    # ── Step2: EDINET DB に鮮度付与 ──────────────────────────
    edinet_enriched = False
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, str(Path(__file__).parent))
        _erm = _imp.import_module("edinet_rotation_manager")
        edinet_db = _erm.load_db(cfg)
        if edinet_db:
            edinet_db = enrich_edinet_db_with_freshness(edinet_db, cfg)
            # results に鮮度付きDBを格納（screener が参照）
            results["edinet_db_fresh"] = edinet_db
            edinet_enriched = True
    except Exception as _e:
        print(f"    ⚠️  EDINET鮮度付与失敗（スキップ）: {str(_e)[:60]}")

    print(
        f"  ✅ 鮮度管理完了: "
        f"新鮮{len(report) - len(stale_caches) - len(refresh_caches)}件 / "
        f"陳腐{len(stale_caches)}件 / "
        f"再取得推奨{len(refresh_caches)}件"
    )

    return {
        "report":           report,
        "stale_caches":     stale_caches,
        "refresh_caches":   refresh_caches,
        "edinet_enriched":  edinet_enriched,
        "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
