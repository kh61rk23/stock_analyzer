# ==============================================================
# size_tier_classifier.py  v1.1  (Wave 7 - Step 4)
# 銘柄規模分類マスター生成モジュール
# ==============================================================
# 【役割】
#   全銘柄を時価総額で5層（Large/Mid/Small/Micro/Nano）に分類し、
#   流動性ランクと業態（BM-01〜BM-12）を合わせて
#   size_tier_master.json を生成・更新する。
#   週次実行（WEEKLY_MODULES に追加）。
#
# 【分類基準（JP / 億円）】
#   Large  : 1兆円以上（10,000億以上）
#   Mid    : 1,000億以上
#   Small  : 100億以上
#   Micro  : 50億以上
#   Nano   : 50億未満
#
# 【分類基準（US / 億USD）】
#   Large  : 1,000億USD以上（Mega/Large Cap）
#   Mid    : 200億USD以上
#   Small  : 20億USD以上
#   Micro  : 5億USD以上
#   Nano   : 5億USD未満
#
# 【流動性ランク算出】
#   $daily\_turnover\_b = avg\_volume \times last\_price \div 10^8$ (億円/日)
#   Rank:  100億+/日→100, 50億+/日→90, 10億+/日→80,
#          1億+/日→70, 0.1億+/日→50, 未満→20
#
# 【Nano評価の負荷制限】
#   NANO_EVALUATION_MAX_PER_DAY 件/日（デフォルト5）まで評価し、
#   超過分は次回実行に繰り越す。
#
# 【生成ファイル】
#   CACHE_DIR/size_tier_master.json  TTL: 7日, 週次更新
#
# 【依存】
#   business_model_detector.py（業態判定）
#   config.py（BLOCK 25 定数）
#   stock_data: results["screener"] / results["us_screener"]（JP/US候補）
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: デフォルト定数（config.py が読めない場合のフォールバック）
# ==============================================================

_DEFAULT_THRESHOLDS_JP: dict = {
    "Large": 10_000,
    "Mid":    1_000,
    "Small":    100,
    "Micro":     50,
    "Nano":       0,
}

_DEFAULT_THRESHOLDS_US: dict = {
    "Large": 1_000,
    "Mid":     200,
    "Small":    20,
    "Micro":     5,
    "Nano":      0,
}

_TIER_ORDER = ["Large", "Mid", "Small", "Micro", "Nano"]

CACHE_FILENAME = "size_tier_master.json"

# ==============================================================
# BLOCK 0-B: taxonomy×size 交差評価定数（Wave 7 追加）
# ==============================================================

# セクターコード → taxonomy_node_id マッピング
# EDINET/TSE 33業種コード → 内部タクソノミノードID
_SECTOR_TO_TAXONOMY: dict = {
    # 電気機器（1017）: 半導体・電子部品
    "1017": ["semiconductor_logic", "semiconductor_rf", "electronic_component"],
    # 精密機器（1018）: センサー・光学
    "1018": ["sensor_image", "sensor_radar", "photonics"],
    # 情報・通信業（1025）: ソフトウェア・SaaS
    "1025": ["ai_ml_framework", "telecom_software", "ai_software"],
    # 機械（1015）: パワー半導体・産業機械
    "1015": ["power_semiconductor", "semiconductor_package"],
    # 輸送用機器（1017_auto）: EV・車載
    "1017_auto": ["ev_battery", "ev_charger", "sensor_radar"],
    # 化学（1006）: 素材・高機能材料
    "1006": ["advanced_material", "semiconductor_package"],
    # 卸売業（1028）: 商社（資源・技術）
    "1028": ["resource_trading", "technology_trading"],
    # 銀行業（1030）: フィンテック含む
    "1030": ["fintech_platform"],
    # サービス業（1033）: SaaS・プラットフォーム
    "1033": ["saas_platform", "ai_software"],
}

# taxonomy × size の交差評価補正テーブル
# 形式: {taxonomy_node_id: {size_tier: correction_factor}}
# correction_factor > 1.0 → スコアをブーストする
# correction_factor < 1.0 → スコアをペナルティする
_TAXONOMY_SIZE_CORRECTION: dict = {
    # 半導体ロジック: Micro-capは専門ファブレス企業が多い → ブースト
    "semiconductor_logic": {
        "Large": 1.0,
        "Mid":   1.05,
        "Small": 1.10,
        "Micro": 1.20,  # 専門ファブレス補正
        "Nano":  0.80,
    },
    # 半導体RF: Small/Microに有望な専門メーカーが集積 → ブースト
    "semiconductor_rf": {
        "Large": 1.0,
        "Mid":   1.05,
        "Small": 1.15,
        "Micro": 1.20,
        "Nano":  0.75,
    },
    # AI/MLフレームワーク: Small-cap純粋AIスタートアップ → 大幅ブースト
    "ai_ml_framework": {
        "Large": 0.95,  # 大企業はAI事業の比率が低いため軽微ペナルティ
        "Mid":   1.05,
        "Small": 1.25,
        "Micro": 1.30,
        "Nano":  0.70,
    },
    # SaaSプラットフォーム: Mid以下の専業企業 → ブースト
    "saas_platform": {
        "Large": 0.90,
        "Mid":   1.10,
        "Small": 1.20,
        "Micro": 1.15,
        "Nano":  0.65,
    },
    # EV電池: 大企業が優位（規模の経済） → Large/Mid優遇
    "ev_battery": {
        "Large": 1.15,
        "Mid":   1.10,
        "Small": 0.95,
        "Micro": 0.80,
        "Nano":  0.60,
    },
    # パワー半導体（SiC/GaN）: Small/Microに専業メーカー集積
    "power_semiconductor": {
        "Large": 1.0,
        "Mid":   1.05,
        "Small": 1.15,
        "Micro": 1.25,  # SiC専業補正
        "Nano":  0.70,
    },
    # 半導体パッケージ: Mid以上が主流
    "semiconductor_package": {
        "Large": 1.10,
        "Mid":   1.10,
        "Small": 1.0,
        "Micro": 0.85,
        "Nano":  0.60,
    },
    # フォトニクス: Micro-cap専業企業が存在
    "photonics": {
        "Large": 1.0,
        "Mid":   1.05,
        "Small": 1.10,
        "Micro": 1.20,
        "Nano":  0.75,
    },
    # 衛星通信/NTN: 大型プロジェクト → Large/Mid優位
    "satellite_comm": {
        "Large": 1.20,
        "Mid":   1.10,
        "Small": 0.90,
        "Micro": 0.70,
        "Nano":  0.50,
    },
    # デフォルト（未定義taxonomy）: 補正なし
    "_default": {
        "Large": 1.0,
        "Mid":   1.0,
        "Small": 1.0,
        "Micro": 1.0,
        "Nano":  1.0,
    },
}

# Micro-cap専用の閾値補正（規模×セクターのダブル条件）
# 半導体カテゴリの Micro は Pearl スコアのベンチマークROEを引き下げる
_MICRO_CAP_ROE_BENCHMARK_OVERRIDE: dict = {
    # {taxonomy_node_id: 引き下げ後ROEベンチマーク}
    # 通常ベンチマークより低く設定（成長期のCAPEX負担を考慮）
    "semiconductor_logic":   0.06,
    "semiconductor_rf":      0.06,
    "power_semiconductor":   0.05,
    "ai_ml_framework":       0.04,  # 先行投資フェーズ
    "saas_platform":         0.05,
    "photonics":             0.06,
}

# ==============================================================
# BLOCK 1: 規模ティア分類
# ==============================================================

def classify_size_tier(
    market_cap_b: Optional[float],
    thresholds:   dict,
) -> str:
    """
    時価総額（億円 or 億USD）からサイズティアを返す。

    Args:
        market_cap_b: 時価総額（単位はJPなら億円、USなら億USD）
        thresholds:   SIZE_TIER_THRESHOLDS_JP or SIZE_TIER_THRESHOLDS_US

    Returns:
        "Large" / "Mid" / "Small" / "Micro" / "Nano"
    """
    try:
        if market_cap_b is None:
            return "Nano"   # 不明 → 最小層に分類
        mcap = float(market_cap_b)
        for tier in _TIER_ORDER[:-1]:   # Nano は最後のデフォルト
            if mcap >= float(thresholds.get(tier, 0)):
                return tier
        return "Nano"
    except Exception:
        return "Nano"

def get_taxonomy_nodes_for_sector(sector_code: str) -> list:
    """
    セクターコードから対応する taxonomy_node_id リストを返す。

    Args:
        sector_code: TSE 33業種コード（例: "1017"）

    Returns:
        taxonomy_node_id のリスト（例: ["semiconductor_logic", ...]）
    """
    try:
        return list(_SECTOR_TO_TAXONOMY.get(str(sector_code), []))
    except Exception:
        return []


def calc_taxonomy_size_correction(
    taxonomy_node_ids: list,
    size_tier: str,
) -> float:
    """
    taxonomy × size の交差評価補正係数を計算する。
    複数 taxonomy_node_id が該当する場合は最大値を採用する。

    $correction = \\max_{n \\in taxonomy\\_nodes}(table[n][size\\_tier])$

    Args:
        taxonomy_node_ids: 該当するタクソノミノードIDのリスト
        size_tier:         "Large"/"Mid"/"Small"/"Micro"/"Nano"

    Returns:
        float: 補正係数（デフォルト 1.0）
    """
    try:
        if not taxonomy_node_ids:
            return 1.0
        max_correction = 1.0
        for node_id in taxonomy_node_ids:
            node_table = _TAXONOMY_SIZE_CORRECTION.get(
                node_id,
                _TAXONOMY_SIZE_CORRECTION["_default"],
            )
            correction = float(node_table.get(size_tier, 1.0))
            if correction > max_correction:
                max_correction = correction
        return round(max_correction, 4)
    except Exception:
        return 1.0


def get_micro_cap_roe_benchmark(
    taxonomy_node_ids: list,
    default_benchmark: float = 0.08,
) -> float:
    """
    Micro-cap 銘柄の taxonomy 別 ROEベンチマーク引き下げ値を返す。
    Micro でない銘柄に呼んでも安全（default_benchmark をそのまま返す）。

    Args:
        taxonomy_node_ids: タクソノミノードIDリスト
        default_benchmark: デフォルトROEベンチマーク

    Returns:
        float: ROEベンチマーク（引き下げ後）
    """
    try:
        overrides = []
        for node_id in taxonomy_node_ids:
            v = _MICRO_CAP_ROE_BENCHMARK_OVERRIDE.get(node_id)
            if v is not None:
                overrides.append(v)
        if overrides:
            return round(min(overrides), 4)  # 最も低いベンチマークを採用
        return default_benchmark
    except Exception:
        return default_benchmark

def is_penny_stock_jp(
    last_price: Optional[float],
    threshold:  float = 100.0,
) -> bool:
    """JP株ペニー株フラグ（株価 < threshold 円）。"""
    try:
        if last_price is None:
            return False
        return float(last_price) < threshold
    except Exception:
        return False


# ==============================================================
# BLOCK 2: 流動性ランク算出
# ==============================================================

def calc_liquidity_rank(
    avg_volume:   Optional[float],
    last_price:   Optional[float],
    market_cap_b: Optional[float] = None,
) -> int:
    """
    $daily\\_turnover\\_b = avg\\_volume \times last\\_price \\div 10^8$（億円/日）
    から流動性ランク（0〜100）を算出する。

    Returns:
        int: 0〜100（高いほど流動性が高い）
    """
    try:
        if avg_volume is None or last_price is None:
            # 時価総額からの代替計算
            if market_cap_b is not None:
                mcap = float(market_cap_b)
                if mcap >= 10_000:   return 85
                if mcap >= 1_000:    return 70
                if mcap >= 100:      return 50
                if mcap >= 50:       return 30
                return 15
            return 0

        daily_b = float(avg_volume) * float(last_price) / 1e8

        if daily_b >= 100:  return 100
        if daily_b >= 50:   return 90
        if daily_b >= 10:   return 80
        if daily_b >= 1:    return 70
        if daily_b >= 0.1:  return 50
        if daily_b >= 0.01: return 30
        return 15

    except Exception:
        return 0


# ==============================================================
# BLOCK 3: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / CACHE_FILENAME


def _load_master(cfg) -> Optional[dict]:
    """
    TTL 内の size_tier_master.json を返す。
    失効または未存在の場合は None。
    """
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        last_updated = data.get("last_updated", "")
        ttl_days     = int(getattr(cfg, "SIZE_TIER_CACHE_TTL_DAYS", 7))
        if last_updated:
            age = (datetime.now() -
                   datetime.strptime(last_updated, "%Y-%m-%d")).days
            if age < ttl_days:
                return data
    except Exception:
        pass
    return None


def _save_master(cfg, data: dict) -> None:
    try:
        data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        p = _cache_path(cfg)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [size_tier] master保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 4: データ収集
# ==============================================================

def _collect_jp_candidates(results: dict) -> list[dict]:
    """
    results["screener"] から JP 銘柄のデータを収集する。
    """
    try:
        screener = results.get("screener", {})
        if not screener or screener.get("skipped"):
            return []
        candidates = screener.get("candidates",
                     screener.get("passed", []))
        out = []
        for c in candidates:
            try:
                out.append({
                    "ticker":       str(c.get("ticker", c.get("code", ""))),
                    "name":         str(c.get("name", "")),
                    "market_cap_b": _safe_float(c.get("market_cap_b")),
                    "last_price":   _safe_float(c.get("last_price",
                                    c.get("price"))),
                    "avg_volume":   _safe_float(c.get("avg_volume")),
                    "sector_code":  str(c.get("sector_code", "")),
                    "roe":          _safe_float(c.get("roe")),
                    "gross_margin": _safe_float(c.get("gross_margin")),
                    "debt_ratio":   _safe_float(c.get("debt_ratio")),
                    "asset_turnover": _safe_float(c.get("asset_turnover")),
                    "recurring_revenue_ratio": _safe_float(
                        c.get("recurring_revenue_ratio")),
                    "revenue_per_employee": _safe_float(
                        c.get("revenue_per_employee")),
                    "market": "JP",
                })
            except Exception:
                pass
        return out
    except Exception:
        return []


def _collect_us_candidates(results: dict) -> list[dict]:
    """
    results["us_screener"] から US 銘柄のデータを収集する。
    """
    try:
        us = results.get("us_screener", {})
        if not us or us.get("skipped"):
            return []
        passed = us.get("passed", us.get("candidates", []))
        out = []
        for c in passed:
            try:
                # US: market_cap は raw USD（billions に変換）
                mcap_raw = _safe_float(c.get("market_cap"))
                mcap_b   = round(mcap_raw / 1e9, 2) if mcap_raw else None

                out.append({
                    "ticker":       str(c.get("ticker", "")),
                    "name":         str(c.get("name", "")),
                    "market_cap_b": mcap_b,     # 億USD換算
                    "last_price":   _safe_float(c.get("last_price",
                                    c.get("price"))),
                    "avg_volume":   _safe_float(c.get("avg_volume")),
                    "sector_code":  str(c.get("sector_code",
                                    c.get("sector", ""))),
                    "roe":          _safe_float(c.get("roe")),
                    "gross_margin": _safe_float(c.get("gross_margin")),
                    "debt_ratio":   _safe_float(c.get("debt_ratio")),
                    "asset_turnover": _safe_float(c.get("asset_turnover")),
                    "recurring_revenue_ratio": _safe_float(
                        c.get("recurring_revenue_ratio")),
                    "revenue_per_employee": _safe_float(
                        c.get("revenue_per_employee")),
                    "market": "US",
                })
            except Exception:
                pass
        return out
    except Exception:
        return []


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ==============================================================
# BLOCK 5: マスター構築
# ==============================================================

def build_size_tier_master(
    candidates_jp: list[dict],
    candidates_us: list[dict],
    cfg=None,
    existing_master: Optional[dict] = None,
) -> dict:
    """
    JP/US 候補リストから size_tier_master を構築する。
    業態判定には business_model_detector を使用する。

    Args:
        candidates_jp:   JP 銘柄リスト（_collect_jp_candidates の返り値）
        candidates_us:   US 銘柄リスト（_collect_us_candidates の返り値）
        cfg:             config モジュール
        existing_master: 既存マスター（Nano 制限のため保持）

    Returns:
        {
          "last_updated": str,
          "version":      str,
          "jp":           dict,
          "us":           dict,
          "stats":        dict,
        }
    """
    # ── 設定値取得 ────────────────────────────────────────────
    th_jp  = getattr(cfg, "SIZE_TIER_THRESHOLDS_JP",  _DEFAULT_THRESHOLDS_JP)
    th_us  = getattr(cfg, "SIZE_TIER_THRESHOLDS_US",  _DEFAULT_THRESHOLDS_US)
    penny  = float(getattr(cfg, "PENNY_FLAG_THRESHOLD_JP", 100.0))
    nano_max = int(getattr(cfg, "NANO_EVALUATION_MAX_PER_DAY", 5))
    today  = datetime.now().strftime("%Y-%m-%d")

    # ── business_model_detector のロード ─────────────────────
    _bmd = None
    try:
        from business_model_detector import detect_business_model as _bmd
    except ImportError:
        pass

    # ── 既存マスターを引き継ぎ ────────────────────────────────
    prev_jp: dict = {}
    prev_us: dict = {}
    if existing_master:
        prev_jp = existing_master.get("jp", {})
        prev_us = existing_master.get("us", {})

    master_jp: dict = {}
    master_us: dict = {}

    # ── Nano カウンター（日次上限制御）────────────────────────
    nano_count = 0

    # ===========================================================
    # JP 銘柄の処理
    # ===========================================================
    for c in candidates_jp:
        ticker   = c.get("ticker", "")
        if not ticker:
            continue

        try:
            mcap_b = c.get("market_cap_b")
            tier   = classify_size_tier(mcap_b, th_jp)

            # Nano 上限チェック
            if tier == "Nano":
                if nano_count >= nano_max:
                    # 既存マスターを引き継いで処理をスキップ
                    if ticker in prev_jp:
                        master_jp[ticker] = {
                            **prev_jp[ticker],
                            "nano_skipped": True,
                        }
                    continue
                nano_count += 1

            last_price = c.get("last_price")
            avg_vol    = c.get("avg_volume")
            liq_rank   = calc_liquidity_rank(avg_vol, last_price, mcap_b)
            penny_flag = is_penny_stock_jp(last_price, penny)

            # 業態判定
            bm_type   = prev_jp.get(ticker, {}).get("business_model", "BM-10")
            bm_label  = prev_jp.get(ticker, {}).get("bm_label", "")
            bm_conf   = 0.0
            roe_bench = 0.08
            roe_exc   = 0.0

            if _bmd is not None:
                try:
                    bm_res  = _bmd(c, cfg, c.get("sector_code", ""))
                    bm_type  = bm_res.get("bm_type",  bm_type)
                    bm_label = bm_res.get("bm_label", bm_label)
                    bm_conf  = bm_res.get("confidence",    0.0)
                    roe_bench = bm_res.get("roe_benchmark", 0.08)
                    roe_exc   = bm_res.get("roe_excess",    0.0)
                except Exception:
                    pass

            # ── taxonomy × size 交差評価（Wave 7 追加）──────────
            taxonomy_nodes = get_taxonomy_nodes_for_sector(
                c.get("sector_code", "")
            )
            taxonomy_size_correction = calc_taxonomy_size_correction(
                taxonomy_nodes, tier
            )
            # Micro-cap の場合は taxonomy 別 ROEベンチマーク補正を適用
            if tier == "Micro":
                roe_bench = get_micro_cap_roe_benchmark(
                    taxonomy_nodes, roe_bench
                )
                # roe_excess も補正済みベンチマークで再計算
                roe_val = _safe_float(c.get("roe"))
                if roe_val is not None:
                    roe_exc = round(roe_val - roe_bench, 4)

            master_jp[ticker] = {
                "ticker":          ticker,
                "name":            c.get("name", ""),
                "market_cap_b":    round(mcap_b, 1) if mcap_b else None,
                "size_tier":       tier,
                "liquidity_rank":  liq_rank,
                "penny_flag":      penny_flag,
                "business_model":  bm_type,
                "bm_label":        bm_label,
                "bm_confidence":   round(bm_conf, 2),
                "sector_code":     c.get("sector_code", ""),
                "roe":             c.get("roe"),
                "roe_benchmark":   round(roe_bench, 4),
                "roe_excess":      round(roe_exc, 4),
                "avg_volume":      avg_vol,
                "last_price":      last_price,
                "updated_at":      today,
                "taxonomy_nodes":          taxonomy_nodes,
                "taxonomy_size_correction": taxonomy_size_correction,
                "nano_skipped":    False,
            }

        except Exception:
            continue

    # ===========================================================
    # US 銘柄の処理
    # ===========================================================
    for c in candidates_us:
        ticker = c.get("ticker", "")
        if not ticker:
            continue

        try:
            mcap_b = c.get("market_cap_b")
            tier   = classify_size_tier(mcap_b, th_us)

            liq_rank = calc_liquidity_rank(
                c.get("avg_volume"),
                c.get("last_price"),
                mcap_b,
            )

            # 業態判定
            bm_type   = prev_us.get(ticker, {}).get("business_model", "BM-10")
            bm_label  = prev_us.get(ticker, {}).get("bm_label", "")
            bm_conf   = 0.0
            roe_bench = 0.08
            roe_exc   = 0.0

            if _bmd is not None:
                try:
                    bm_res   = _bmd(c, cfg, c.get("sector_code", ""))
                    bm_type  = bm_res.get("bm_type",      bm_type)
                    bm_label = bm_res.get("bm_label",     bm_label)
                    bm_conf  = bm_res.get("confidence",   0.0)
                    roe_bench = bm_res.get("roe_benchmark", 0.08)
                    roe_exc   = bm_res.get("roe_excess",    0.0)
                except Exception:
                    pass

            # ── taxonomy × size 交差評価（Wave 7 追加）──────────
            taxonomy_nodes_us = get_taxonomy_nodes_for_sector(
                c.get("sector_code", "")
            )
            taxonomy_size_correction_us = calc_taxonomy_size_correction(
                taxonomy_nodes_us, tier
            )
            if tier == "Micro":
                roe_bench = get_micro_cap_roe_benchmark(
                    taxonomy_nodes_us, roe_bench
                )
                roe_val_us = _safe_float(c.get("roe"))
                if roe_val_us is not None:
                    roe_exc = round(roe_val_us - roe_bench, 4)

            master_us[ticker] = {
                "ticker":          ticker,
                "name":            c.get("name", ""),
                "market_cap_usd_b": round(mcap_b, 2) if mcap_b else None,
                "size_tier":       tier,
                "liquidity_rank":  liq_rank,
                "business_model":  bm_type,
                "bm_label":        bm_label,
                "bm_confidence":   round(bm_conf, 2),
                "sector_code":     c.get("sector_code", ""),
                "roe":             c.get("roe"),
                "taxonomy_nodes":           taxonomy_nodes_us,
                "taxonomy_size_correction": taxonomy_size_correction_us,
                "roe_benchmark":   round(roe_bench, 4),
                "roe_excess":      round(roe_exc, 4),
                "updated_at":      today,
            }

        except Exception:
            continue

    # ── Nano をスキップした JP 銘柄は既存マスターから継承 ─────
    for ticker, prev_data in prev_jp.items():
        if ticker not in master_jp and prev_data.get("size_tier") == "Nano":
            master_jp[ticker] = {**prev_data, "nano_skipped": True}

    # ── 統計集計 ─────────────────────────────────────────────
    tier_dist: dict = {t: 0 for t in _TIER_ORDER}
    for entry in master_jp.values():
        t = entry.get("size_tier", "Nano")
        tier_dist[t] = tier_dist.get(t, 0) + 1
    for entry in master_us.values():
        t = entry.get("size_tier", "Nano")
        tier_dist[t] = tier_dist.get(t, 0) + 1

    bm_dist: dict = {}
    for entry in {**master_jp, **master_us}.values():
        bm = entry.get("business_model", "BM-10")
        bm_dist[bm] = bm_dist.get(bm, 0) + 1

    return {
        "last_updated": today,
        "version":      "1.0",
        "jp":           master_jp,
        "us":           master_us,
        "stats": {
            "jp_total":          len(master_jp),
            "us_total":          len(master_us),
            "nano_evaluated":    nano_count,
            "nano_limit":        nano_max,
            "tier_distribution": tier_dist,
            "bm_distribution":   dict(sorted(bm_dist.items())),
        },
    }


# ==============================================================
# BLOCK 6: メイン実行フロー
# ==============================================================

def run_size_tier_classifier(cfg, results: dict = None) -> dict:
    """
    stock_analyzer_main.py の WEEKLY_MODULES から呼ばれるエントリポイント。
    sector 実行の直後に実行する。

    処理フロー:
      1. キャッシュ有効（TTL内） → キャッシュ返却
      2. results から JP/US 候補を収集
      3. build_size_tier_master() でマスター構築
      4. size_tier_master.json に保存
      5. results["size_tier_master"] にセットして返す

    Returns:
        {
          "master":    dict,    # size_tier_master の中身
          "stats":     dict,    # 統計サマリー
          "cached":    bool,
          "timestamp": str,
        }
    """
    print("\n  📊 規模分類マスター生成（Wave 0 Size Ph1）実行中...")

    # ── キャッシュ確認 ───────────────────────────────────────────
    cached = _load_master(cfg)
    if cached:
        stats = cached.get("stats", {})
        print(
            f"    ✅ キャッシュ使用: "
            f"JP={stats.get('jp_total',0)} / US={stats.get('us_total',0)} "
            f"/ Tier分布={stats.get('tier_distribution', {})}"
        )
        cached["cached"] = True
        return {
            "master":    cached,
            "stats":     stats,
            "cached":    True,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── データ収集 ───────────────────────────────────────────────
    if results is None:
        results = {}

    candidates_jp = _collect_jp_candidates(results)
    candidates_us = _collect_us_candidates(results)
    print(
        f"    📋 収集: JP={len(candidates_jp)}銘柄 / "
        f"US={len(candidates_us)}銘柄"
    )

    if not candidates_jp and not candidates_us:
        print("    ⚠️  候補銘柄がありません → スキップ")
        return {
            "master":    {},
            "stats":     {},
            "cached":    False,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 既存マスター読み込み（Nano 制限の継承のため）────────────
    try:
        p = _cache_path(cfg)
        existing = None
        if p.exists():
            existing = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        existing = None

    # ── マスター構築 ─────────────────────────────────────────────
    master = build_size_tier_master(
        candidates_jp, candidates_us, cfg, existing
    )

    # ── 保存 ─────────────────────────────────────────────────────
    _save_master(cfg, master)

    stats = master.get("stats", {})
    td    = stats.get("tier_distribution", {})
    print(
        f"    ✅ マスター生成完了: "
        f"JP={stats.get('jp_total',0)} / US={stats.get('us_total',0)}"
    )
    print(
        f"    📊 Tier分布: "
        f"Large={td.get('Large',0)} "
        f"Mid={td.get('Mid',0)} "
        f"Small={td.get('Small',0)} "
        f"Micro={td.get('Micro',0)} "
        f"Nano={td.get('Nano',0)}"
    )

    bm_dist = stats.get("bm_distribution", {})
    top3_bm = sorted(bm_dist.items(), key=lambda x: x[1], reverse=True)[:3]
    if top3_bm:
        bm_str = " / ".join(f"{bm}:{cnt}" for bm, cnt in top3_bm)
        print(f"    🏭 BM上位3: {bm_str}")

    return {
        "master":    master,
        "stats":     stats,
        "cached":    False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 7: 外部参照用アクセサ
# ==============================================================

def get_size_tier(ticker: str, cfg=None) -> Optional[str]:
    """
    銘柄コードのサイズティアを size_tier_master.json から取得する。

    Returns:
        "Large"/"Mid"/"Small"/"Micro"/"Nano" or None
    """
    try:
        master = _load_master(cfg)
        if not master:
            return None
        jp_entry = master.get("jp", {}).get(ticker)
        if jp_entry:
            return jp_entry.get("size_tier")
        us_entry = master.get("us", {}).get(ticker)
        if us_entry:
            return us_entry.get("size_tier")
        return None
    except Exception:
        return None


def get_bm_type(ticker: str, cfg=None) -> Optional[str]:
    """
    銘柄コードの業態タイプを size_tier_master.json から取得する。

    Returns:
        "BM-04" 等 or None
    """
    try:
        master = _load_master(cfg)
        if not master:
            return None
        for market_key in ("jp", "us"):
            entry = master.get(market_key, {}).get(ticker)
            if entry:
                return entry.get("business_model")
        return None
    except Exception:
        return None

def get_taxonomy_size_correction(ticker: str, cfg=None) -> float:
    """
    銘柄の taxonomy×size 補正係数を size_tier_master.json から取得する。

    Returns:
        float 補正係数（デフォルト 1.0）
    """
    try:
        master = _load_master(cfg)
        if not master:
            return 1.0
        for market_key in ("jp", "us"):
            entry = master.get(market_key, {}).get(ticker)
            if entry:
                return float(entry.get("taxonomy_size_correction", 1.0))
        return 1.0
    except Exception:
        return 1.0


def get_taxonomy_nodes(ticker: str, cfg=None) -> list:
    """
    銘柄の taxonomy_node_ids を size_tier_master.json から取得する。

    Returns:
        list[str]（例: ["semiconductor_logic", "semiconductor_rf"]）
    """
    try:
        master = _load_master(cfg)
        if not master:
            return []
        for market_key in ("jp", "us"):
            entry = master.get(market_key, {}).get(ticker)
            if entry:
                return list(entry.get("taxonomy_nodes", []))
        return []
    except Exception:
        return []

# ==============================================================
# BLOCK 8: 単体テスト
# ==============================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("  📊 size_tier_classifier.py v1.0 単体テスト")
    print("=" * 60)

    # ── classify_size_tier のテスト ─────────────────────────────
    print("\n  【JP 規模分類テスト】")
    test_mcaps_jp = [50_000, 5_000, 500, 150, 80, 20]
    for m in test_mcaps_jp:
        t = classify_size_tier(m, _DEFAULT_THRESHOLDS_JP)
        print(f"    {m:7,.0f}億円 → {t}")

    print("\n  【US 規模分類テスト】")
    test_mcaps_us = [2_000, 500, 50, 10, 3, 0.5]
    for m in test_mcaps_us:
        t = classify_size_tier(m, _DEFAULT_THRESHOLDS_US)
        print(f"    {m:7,.1f}億USD → {t}")

    # ── 流動性ランクのテスト ─────────────────────────────────────
    print("\n  【流動性ランクテスト】")
    test_liq = [
        (10_000_000, 3_000, "大型株 (300億円/日)"),
        (500_000,    1_000, "中型株 (5億円/日)"),
        (50_000,       500, "小型株 (0.25億円/日)"),
        (5_000,        200, "Nano株 (0.01億円/日)"),
    ]
    for vol, price, label in test_liq:
        rank = calc_liquidity_rank(vol, price)
        print(f"    {label}: rank={rank}")

    # ── ダミー results でのフロー確認 ────────────────────────────
    print("\n  【ダミー results での build テスト】")
    dummy_results = {
        "screener": {
            "candidates": [
                {"ticker": "7203.T", "name": "トヨタ自動車",
                 "market_cap_b": 35_000, "last_price": 2800, "avg_volume": 5_000_000,
                 "sector_code": "1017", "roe": 0.10, "gross_margin": 0.20},
                {"ticker": "4689.T", "name": "Zホールディングス",
                 "market_cap_b": 3_500, "last_price": 380, "avg_volume": 8_000_000,
                 "sector_code": "1025", "roe": 0.08, "gross_margin": 0.30},
                {"ticker": "9999.T", "name": "テストNano株",
                 "market_cap_b": 25, "last_price": 85, "avg_volume": 3_000,
                 "sector_code": "1033", "roe": 0.03},
            ]
        }
    }

    master = build_size_tier_master(
        _collect_jp_candidates(dummy_results), [], cfg=None
    )
    print(f"    JP 銘柄数: {master['stats']['jp_total']}")
    print(f"    Tier分布:  {master['stats']['tier_distribution']}")
    print(f"    BM分布:    {master['stats']['bm_distribution']}")
    for ticker, entry in master["jp"].items():
        print(
            f"    {ticker:12s}: "
            f"{entry['size_tier']:6s} "
            f"流動性={entry['liquidity_rank']:3d} "
            f"BM={entry['business_model']} "
            f"({entry['bm_label'][:14]})"
        )
