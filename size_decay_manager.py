# ==============================================================
# size_decay_manager.py  v2.0
# 規模別シグナル減衰・コンポジットウェイト管理モジュール
# ==============================================================
# 【役割】
#   size_tier_classifier.py が生成した size_tier_master.json を参照し、
#   規模（Size Tier）別のシグナル減衰状態（Decay State）を管理する。
#   時価総額規模（Size Tier）に応じた分類、コンポジットスコアのウェイト管理、
#   およびシグナルの時間的減衰（Decay State）とNano銘柄評価キューを管理する。
#   具体的には：
#     (1) 各銘柄のシグナルに対して Tier別の時間的減衰係数を計算・更新
#     (2) Nano銘柄の評価キューを管理（日次上限制御）
#     (3) 減衰状態を size_decay_state.json に保存
#     (4) Nano評価キューを nano_evaluation_queue.json に保存
#
# 【設計思想】
#   ・Large銘柄: シグナルが市場に早期に織り込まれる → 減衰が速い
#   ・Small/Micro銘柄: 情報伝播が遅い → 減衰が遅い（シグナル寿命が長い）
#   ・Nano銘柄: 流動性リスクが高く、評価コストが大きい
#               → 日次上限（NANO_EVALUATION_MAX_PER_DAY）で制御
#   ・cross_size_signal.py（Step 6）はこのキャッシュを参照して
#     横断シグナルを生成する
#
# 【減衰モデル】
#   $decay(t) = e^{-\\lambda \\times t}$
#   $\\lambda$ = Tier別減衰定数（Large=0.15, Mid=0.10, Small=0.07,
#                                  Micro=0.05, Nano=0.03）
#   $t$ = 最終シグナル更新からの経過日数
#   $effective\\_signal = raw\\_signal \\times decay(t) \\times taxonomy\\_correction$
#
# 【主要出力キャッシュ】
#   1. CACHE_DIR/size_decay_state.json  TTL: 7日, 日次更新
#      {ticker: {size_tier, decay_lambda, last_signal_date,
#                days_elapsed, decay_factor, effective_signal, ...}}
#
#   2. CACHE_DIR/nano_evaluation_queue.json  TTL: 7日, 日次更新
#      {queue: [{ticker, priority_score, queued_at, ...}],
#       evaluated_today: [ticker, ...]}
#
# 【依存】
#   size_tier_classifier.py（size_tier_master.json 参照）
#   config.py（減衰・キュー管理の定数参照）
#   try/except で全ブロックを保護
# 【主な機能】
#   1. Size Tier 判定 (Large/Mid/Small/Micro/Nano)
#   2. Tier別 composite ウェイトの提供
#   3. 日次バッチによる Tier別シグナル減衰状態の更新 (decay_factor)
#   4. Nano銘柄の優先度ベース日次評価キュー管理
#
# 【実行方式】
#   状態更新は stock_analyzer_main.py 等から1日1回 `run_size_decay_manager()` を
#   バッチ実行することで完結する。他モジュールは Read-Only で状態を参照する。
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: デフォルト定数
# ==============================================================

_DECAY_STATE_FILENAME      = "size_decay_state.json"
_NANO_QUEUE_FILENAME       = "nano_evaluation_queue.json"
_COMPOSITE_SCORES_FILENAME = "size_composite_scores.json"

# ── Size Tier 閾値 ──

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'SDM_DEFAULT_THRESHOLDS_JP': '_DEFAULT_THRESHOLDS_JP',
    'SDM_DEFAULT_THRESHOLDS_US': '_DEFAULT_THRESHOLDS_US',
    'SDM_TIER_COMPOSITE_WEIGHTS': '_TIER_COMPOSITE_WEIGHTS',
    'SDM_SECTOR_WEIGHT_MULTIPLIER': '_SECTOR_WEIGHT_MULTIPLIER',
    'SDM_EFFECTIVE_SIGNAL_MIN_THRESHOLD': '_EFFECTIVE_SIGNAL_MIN_THRESHOLD',
    'SDM_NANO_PRIORITY_WEIGHTS': '_NANO_PRIORITY_WEIGHTS',
    'B6_SIZE_DECAY_MANAGER_DEFAULT_NANO_MAX_PER_DAY': '_DEFAULT_NANO_MAX_PER_DAY',
}

_DEFAULT_THRESHOLDS_JP: dict[str, float] = {
    "Large":  10_000.0,
    "Mid":     1_000.0,
    "Small":     100.0,
    "Micro":      50.0,
    "Nano":        0.0,
}

_DEFAULT_THRESHOLDS_US: dict[str, float] = {
    "Large":  1_000.0,
    "Mid":      200.0,
    "Small":     20.0,
    "Micro":      5.0,
    "Nano":       0.0,
}

# ── Tier別 composite ウェイト ──
_TIER_COMPOSITE_WEIGHTS: dict[str, dict[str, float]] = {
    "Large": {"sector_score": 0.45, "value_score": 0.25, "quality_score": 0.30},
    "Mid":   {"sector_score": 0.40, "value_score": 0.30, "quality_score": 0.30},
    "Small": {"sector_score": 0.33, "value_score": 0.30, "quality_score": 0.37},
    "Micro": {"sector_score": 0.25, "value_score": 0.30, "quality_score": 0.45},
    "Nano":  {"sector_score": 0.20, "value_score": 0.25, "quality_score": 0.55},
}

_SECTOR_WEIGHT_MULTIPLIER: dict[str, float] = {
    "Large": 1.00, "Mid": 1.00, "Small": 0.90, "Micro": 0.65, "Nano": 0.40,
}

# ── 減衰・キュー管理設定 ──
_DEFAULT_DECAY_LAMBDAS: dict = {
    "Large": 0.15, "Mid": 0.10, "Small": 0.07, "Micro": 0.05, "Nano": 0.03,
}

_DECAY_HALF_LIFE: dict = {
    "Large": 4.6, "Mid": 6.9, "Small": 9.9, "Micro": 13.9, "Nano": 23.1,
}

_EFFECTIVE_SIGNAL_MIN_THRESHOLD = 0.10

_NANO_PRIORITY_WEIGHTS = {
    "liquidity_rank_norm": 0.30,
    "roe_excess_norm":     0.25,
    "days_in_queue":       0.20,
    "taxonomy_correction": 0.25,
}

_DEFAULT_NANO_MAX_PER_DAY = 5


# ==============================================================
# BLOCK 1: キャッシュ管理 (共通)
# ==============================================================

def _cache_dir(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d

def _load_json(filename: str, cfg) -> Optional[dict]:
    try:
        p = _cache_dir(cfg) / filename
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def _save_json(filename: str, data: dict, cfg) -> None:
    try:
        p = _cache_dir(cfg) / filename
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  [{filename}] 保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: Size Tier 判定 & コンポジットウェイト計算
# ==============================================================

def classify_size_tier(market_cap_raw: Optional[float], market: str = "JP", cfg=None) -> str:
    try:
        if market_cap_raw is None or float(market_cap_raw) <= 0:
            return "Nano"

        raw = float(market_cap_raw)
        val_oku = raw / 1e8
        thresholds = (
            getattr(cfg, "SIZE_TIER_THRESHOLDS_JP", _DEFAULT_THRESHOLDS_JP) if market == "JP"
            else getattr(cfg, "SIZE_TIER_THRESHOLDS_US", _DEFAULT_THRESHOLDS_US)
        )

        for tier in ("Large", "Mid", "Small", "Micro"):
            if val_oku >= float(thresholds.get(tier, 0)):
                return tier
        return "Nano"
    except Exception:
        return "Nano"

def classify_size_tier_from_b(market_cap_b: Optional[float], market: str = "JP", cfg=None) -> str:
    try:
        if market_cap_b is None or float(market_cap_b) <= 0:
            return "Nano"

        val = float(market_cap_b)
        thresholds = (
            getattr(cfg, "SIZE_TIER_THRESHOLDS_JP", _DEFAULT_THRESHOLDS_JP) if market == "JP"
            else getattr(cfg, "SIZE_TIER_THRESHOLDS_US", _DEFAULT_THRESHOLDS_US)
        )

        for tier in ("Large", "Mid", "Small", "Micro"):
            if val >= float(thresholds.get(tier, 0)):
                return tier
        return "Nano"
    except Exception:
        return "Nano"

def get_composite_weights(size_tier: str, cfg=None) -> dict[str, float]:
    try:
        tier = size_tier if size_tier in _TIER_COMPOSITE_WEIGHTS else "Mid"
        return dict(_TIER_COMPOSITE_WEIGHTS[tier])
    except Exception:
        return {"sector_score": 0.40, "value_score": 0.30, "quality_score": 0.30}

def get_sector_score_weight_multiplier(size_tier: str) -> float:
    return _SECTOR_WEIGHT_MULTIPLIER.get(size_tier, 1.00)

def calc_tier_composite_score(
    sector_score: float, value_score: float, quality_score: float, size_tier: str, cfg=None
) -> tuple[float, dict]:
    try:
        w = get_composite_weights(size_tier, cfg)
        comp = round(
            sector_score * w["sector_score"] +
            value_score * w["value_score"] +
            quality_score * w["quality_score"], 1
        )
        return min(100.0, max(0.0, comp)), w
    except Exception:
        return 0.0, get_composite_weights("Mid", cfg)

def save_size_composite_score(ticker: str, score: float, size_tier: str, weights: dict, cfg=None) -> None:
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        data = _load_json(_COMPOSITE_SCORES_FILENAME, cfg) or {"date": today, "scores": {}}
        if data.get("date") != today:
            data = {"date": today, "scores": {}}
        
        data["scores"][ticker] = {
            "composite_score": round(float(score), 1),
            "size_tier": size_tier,
            "weights": weights,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        _save_json(_COMPOSITE_SCORES_FILENAME, data, cfg)
    except Exception:
        pass


# ==============================================================
# BLOCK 3: 減衰計算
# ==============================================================

def calc_decay_factor(size_tier: str, last_signal_date: str, decay_lambdas: dict = None) -> tuple:
    try:
        lambdas = decay_lambdas or _DEFAULT_DECAY_LAMBDAS
        lam = float(lambdas.get(size_tier, 0.10))

        if last_signal_date:
            last_dt = datetime.strptime(last_signal_date, "%Y-%m-%d")
            days_elapsed = max(0, (datetime.now() - last_dt).days)
        else:
            days_elapsed = 0

        decay = math.exp(-lam * days_elapsed)
        return days_elapsed, round(decay, 6)
    except Exception:
        return 0, 1.0

def calc_effective_signal(raw_signal: float, decay_factor: float, taxonomy_correction: float = 1.0) -> float:
    try:
        eff = float(raw_signal) * float(decay_factor) * float(taxonomy_correction)
        return round(min(10.0, max(0.0, eff)), 4)
    except Exception:
        return 0.0


# ==============================================================
# BLOCK 4: 減衰状態・Nanoキュー バッチ構築処理
# ==============================================================

def _build_decay_state(entries: list, prev_state: dict, decay_lambdas: dict, cfg) -> dict:
    today_str = datetime.now().strftime("%Y-%m-%d")
    decay_entries = {}
    prev_tickers = prev_state.get("tickers", {})

    for entry in entries:
        ticker = entry["ticker"]
        tier = entry["size_tier"]
        tax_corr = float(entry.get("taxonomy_size_correction", 1.0))

        prev = prev_tickers.get(ticker, {})
        last_date = prev.get("last_signal_date", today_str)
        raw_sig = float(prev.get("raw_signal", 5.0))

        days_elapsed, decay_factor = calc_decay_factor(tier, last_date, decay_lambdas)
        eff_signal = calc_effective_signal(raw_sig, decay_factor, tax_corr)
        is_active = eff_signal >= _EFFECTIVE_SIGNAL_MIN_THRESHOLD

        decay_entries[ticker] = {
            "ticker": ticker,
            "market": entry["market"],
            "size_tier": tier,
            "decay_lambda": decay_lambdas.get(tier, 0.10),
            "last_signal_date": last_date,
            "days_elapsed": days_elapsed,
            "decay_factor": decay_factor,
            "raw_signal": raw_sig,
            "taxonomy_size_correction": tax_corr,
            "effective_signal": eff_signal,
            "is_active": is_active,
            "half_life_days": _DECAY_HALF_LIFE.get(tier, 6.9),
            "business_model": entry.get("business_model", "BM-10"),
            "updated_at": today_str,
        }
    return decay_entries

def _calc_nano_priority(entry: dict) -> float:
    try:
        liq_rank = float(entry.get("liquidity_rank", 0))
        roe_exc = float(entry.get("roe_excess", 0.0) or 0.0)
        tax_corr = float(entry.get("taxonomy_size_correction", 1.0))

        liq_norm = min(1.0, liq_rank / 100.0)
        roe_norm = min(1.0, max(0.0, (roe_exc + 0.10) / 0.20))
        tax_norm = min(1.0, max(0.0, (tax_corr - 0.5) / 1.0))

        w = _NANO_PRIORITY_WEIGHTS
        score = (w["liquidity_rank_norm"] * liq_norm +
                 w["roe_excess_norm"] * roe_norm +
                 w["taxonomy_correction"] * tax_norm)
        return round(min(1.0, max(0.0, score)), 4)
    except Exception:
        return 0.0

def _build_nano_queue(entries: list, prev_queue: dict, nano_max: int, cfg) -> dict:
    today_str = datetime.now().strftime("%Y-%m-%d")
    prev_last = prev_queue.get("last_updated", "")
    
    evaluated_today = [] if prev_last != today_str else list(prev_queue.get("evaluated_today", []))
    prev_queue_tickers = {item["ticker"] for item in prev_queue.get("queue", [])}

    new_items = []
    for entry in entries:
        if entry.get("size_tier") != "Nano":
            continue
        ticker = entry["ticker"]
        if ticker in prev_queue_tickers:
            continue
        new_items.append({
            "ticker": ticker,
            "market": entry.get("market", "JP"),
            "priority_score": _calc_nano_priority(entry),
            "queued_at": today_str,
            "evaluated": False,
        })

    existing_items = []
    for item in prev_queue.get("queue", []):
        ticker = item["ticker"]
        matched = next((e for e in entries if e["ticker"] == ticker), None)
        if matched:
            item["priority_score"] = _calc_nano_priority(matched)
            try:
                queued_dt = datetime.strptime(item.get("queued_at", today_str), "%Y-%m-%d")
                days_queued = max(0, (datetime.now() - queued_dt).days)
                days_boost = min(1.0, days_queued / 10.0) * _NANO_PRIORITY_WEIGHTS["days_in_queue"]
                item["priority_score"] = round(min(1.0, item["priority_score"] + days_boost), 4)
                item["days_in_queue"] = days_queued
            except Exception:
                pass
        existing_items.append(item)

    all_items = existing_items + new_items
    all_items.sort(key=lambda x: x.get("priority_score", 0.0), reverse=True)

    to_evaluate = []
    for item in all_items:
        if item["ticker"] in evaluated_today:
            item["evaluated"] = True
            continue
        if len(to_evaluate) < nano_max:
            to_evaluate.append(item["ticker"])
            item["evaluated"] = False

    return {
        "last_updated": today_str,
        "queue": all_items,
        "evaluated_today": list(set(evaluated_today)),
        "to_evaluate_next": to_evaluate,
        "stats": {
            "total_nano_in_queue": len(all_items),
            "evaluated_today": len(evaluated_today),
            "pending": len(all_items) - len(set(evaluated_today)),
            "nano_max_per_day": nano_max,
        },
    }


# ==============================================================
# BLOCK 5: 外部参照用アクセサ (読取専用)
# ==============================================================

def get_decay_factor(ticker: str, cfg=None) -> Optional[float]:
    state = _load_json(_DECAY_STATE_FILENAME, cfg)
    return state.get("tickers", {}).get(ticker, {}).get("decay_factor") if state else None

def get_effective_signal(ticker: str, cfg=None) -> Optional[float]:
    state = _load_json(_DECAY_STATE_FILENAME, cfg)
    return state.get("tickers", {}).get(ticker, {}).get("effective_signal") if state else None

def get_nano_queue(cfg=None) -> list:
    q = _load_json(_NANO_QUEUE_FILENAME, cfg)
    return q.get("queue", []) if q else []

def get_tickers_to_evaluate_today(cfg=None) -> list:
    q = _load_json(_NANO_QUEUE_FILENAME, cfg)
    return q.get("to_evaluate_next", []) if q else []

def get_active_tickers_by_tier(size_tier: str, cfg=None, min_effective_signal: float = None) -> list:
    state = _load_json(_DECAY_STATE_FILENAME, cfg)
    if not state:
        return []
    threshold = min_effective_signal if min_effective_signal is not None else _EFFECTIVE_SIGNAL_MIN_THRESHOLD
    result = [e for e in state.get("tickers", {}).values() if e.get("size_tier") == size_tier and float(e.get("effective_signal", 0.0)) >= threshold]
    result.sort(key=lambda x: x.get("effective_signal", 0.0), reverse=True)
    return result


# ==============================================================
# BLOCK 6: メインバッチ実行フロー (日次)
# ==============================================================

def resolve_size_tier(
    ticker:          str = None,
    market_cap_raw:  float = None,
    market_cap_b:    float = None,
    market:          str = "JP",
    cfg=None,
) -> str:
    """
    stock_screener.py / us_stock_screener.py から呼ばれる Size Tier 解決API。

    両スクリーナーが本関数名で import する（無いと ImportError がフォールバックに
    握り潰され、Size Ph2 の tier 別 composite ウェイトが適用されなくなる）。
    classify_size_tier / classify_size_tier_from_b へ委譲する。

    優先順位: market_cap_raw（円/ドル生値）→ market_cap_b（十億単位）→ "Mid"
    ※ 時価総額不明時に "Nano" ではなく "Mid" を返すのは、未知銘柄へ
      極端なウェイトを適用しない安全側の選択。
    ticker は将来の size_tier_master.json 参照用に受けるが現状未使用。
    """
    try:
        if market_cap_raw is not None and float(market_cap_raw) > 0:
            return classify_size_tier(market_cap_raw, market, cfg)
        if market_cap_b is not None and float(market_cap_b) > 0:
            return classify_size_tier_from_b(market_cap_b, market, cfg)
    except Exception:
        pass
    return "Mid"


def run_size_decay_manager(cfg=None, force_refresh: bool = False) -> dict:
    """日次で実行される減衰・キュー更新のエントリポイント"""
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📉 規模別シグナル減衰管理（Wave 2 & 7 統合版）実行中...")

    if not force_refresh:
        cached = _load_json(_DECAY_STATE_FILENAME, cfg)
        if cached and cached.get("last_updated") == datetime.now().strftime("%Y-%m-%d"):
            print("    ✅ 本日分の状態キャッシュを使用します。")
            return {
                "decay_state": cached,
                "nano_queue": _load_json(_NANO_QUEUE_FILENAME, cfg) or {},
                "stats": cached.get("stats", {}),
                "cached": True,
            }

    # このマスタは size_tier_classifier（週次）が生成するが、
    # 同モジュールは results["screener"] / ["us_screener"] を入力とするため
    # **本モジュールより後**に実行される（順序は変更できない）。
    # したがって参照するのは常に「前回の週次実行が残したファイル」であり、
    # workflow の git add で永続化されていることが前提になる
    # （永続化されないとここが空になり「マスタデータなし → スキップ」となる。
    # independence_score も同じ前提）。
    master = _load_json("size_tier_master.json", cfg) or {"jp": {}, "us": {}}
    entries = []
    for m in ("jp", "us"):
        for t, d in master.get(m, {}).items():
            d["ticker"] = t
            d["market"] = m.upper()
            entries.append(d)

    if not entries:
        # 「無い」のか「前回の週次がまだ回っていない」のかを区別できる形で出す
        print("    ⏩ 規模マスタ未取得 → スキップ"
              "（size_tier_classifier の週次実行後に生成／要永続化）")
        return {"decay_state": {}, "nano_queue": {}, "stats": {}, "cached": False}

    decay_lambdas = getattr(cfg, "SIZE_DECAY_LAMBDAS", _DEFAULT_DECAY_LAMBDAS)
    nano_max = int(getattr(cfg, "NANO_EVALUATION_MAX_PER_DAY", _DEFAULT_NANO_MAX_PER_DAY))

    prev_state = _load_json(_DECAY_STATE_FILENAME, cfg) or {"tickers": {}}
    prev_queue = _load_json(_NANO_QUEUE_FILENAME, cfg) or {"queue": [], "evaluated_today": []}

    decay_tickers = _build_decay_state(entries, prev_state, decay_lambdas, cfg)
    nano_queue = _build_nano_queue(entries, prev_queue, nano_max, cfg)

    active_count = sum(1 for e in decay_tickers.values() if e.get("is_active"))
    stats = {
        "total_tickers": len(decay_tickers),
        "active_tickers": active_count,
        "nano_pending": nano_queue.get("stats", {}).get("pending", 0),
    }

    decay_state_data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "tickers": decay_tickers,
        "stats": stats,
    }

    _save_json(_DECAY_STATE_FILENAME, decay_state_data, cfg)
    _save_json(_NANO_QUEUE_FILENAME, nano_queue, cfg)

    print(f"    ✅ 完了: 銘柄={len(decay_tickers)} / アクティブ={active_count} / Nano待機={stats['nano_pending']}")
    return {"decay_state": decay_state_data, "nano_queue": nano_queue, "stats": stats, "cached": False}
