# ==============================================================
# cross_size_signal.py  v2.0  (Wave 3 & Wave 7 統合版)
# JP/US 規模帯横断・業種連動シグナル生成モジュール
# ==============================================================
# 【役割】
#   各Tier の実効シグナルを横断的に比較・統合し、規模帯を跨いだ
#   先行/遅行シグナルを生成する。さらに、そのシグナルが各業種（セクター）
#   に与える影響スコアを算出する。
#
# 【設計思想（クロスサイズ・リターン伝播理論）】
#   規模が大きい銘柄のシグナルは、同業種の小型株に数週間〜数ヶ月
#   遅れて伝播する（Return Spillover / Lead-Lag Effect）。
#   ・Large→Mid→Small への上昇シグナル伝播を検出し、先行シグナルを生成
#   ・逆方向（Small→Large への逆張りシグナル）も検出
#   ・JP/US 間のクロスマーケットシグナルも生成
#
# 【シグナル種別】
#   (1) downward_cascade:  Large下落 → Small/Micro への売り先行シグナル
#   (2) upward_spillover:  Large上昇 → Small/Micro への先行買いシグナル
#   (3) contrarian_nano:   Nano高スコア → 流動性リスクを考慮した逆張りシグナル
#   (4) jp_us_cross:       US Large上昇 → JP 同業種 Small への先行シグナル
#
# 【主要出力キャッシュ】
#   CACHE_DIR/cross_size_signal_cache.json
#   TTL: 3日（日次更新）
#   スキーマ:
#     {
#       "last_updated": "YYYY-MM-DD",
#       "version": "1.0",
#       "signals": [
#         {
#           "signal_id": str,
#           "signal_type": str,   # 上記4種別
#           "source_tier": str,   # シグナル発生元Tier
#           "target_tier": str,   # シグナル伝播先Tier
#           "source_tickers": list,
#           "target_tickers": list,
#           "taxonomy_nodes": list,
#           "strength": float,    # 0.0 ~ 10.0
#           "direction": str,     # "bullish" / "bearish"
#           "lag_days": int,      # 推定伝播遅延日数
#           "confidence": float,  # 0.0 ~ 1.0
#           "generated_at": str,
#         }
#       ],
#       "ticker_signals": {
#         ticker: [signal_id, ...]  # 各銘柄が関係するシグナルID
#       },
#       "stats": { ... }
#     }
#
# 【依存】
#   size_decay_manager.py（get_active_tickers_by_tier / get_nano_queue）
#   size_tier_classifier.py（get_taxonomy_nodes / get_taxonomy_size_correction）
#   config.py（Wave 7追加定数参照）
#
# 【シグナル種別 (Wave 7準拠)】
#   (1) downward_cascade:  Large下落 → Small/Micro への売り先行シグナル
#   (2) upward_spillover:  Large上昇 → Small/Micro への先行買いシグナル
#   (3) contrarian_nano:   Nano高スコア → 流動性リスク考慮の逆張りシグナル
#   (4) jp_us_cross:       US Large上昇 → JP Small への先行シグナル
#
# 【業種連動 (Wave 3統合)】
#   生成された横断シグナルを _SECTOR_SIZE_AFFINITY に基づき、
#   各業種（セクター）が恩恵・悪影響を受けるかスコア化する。
# ==============================================================

from __future__ import annotations

import json
import hashlib
import math
from datetime import datetime
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: デフォルト定数
# ==============================================================

_CACHE_FILENAME   = "cross_size_signal_cache.json"
_DEFAULT_TTL_DAYS = 3

# ── 規模帯遷移ルール（Wave 7） ──
_SPILLOVER_RULES: list = [
    ("Large", "Mid",   14, 0.90),
    ("Large", "Small", 30, 0.75),
    ("Large", "Micro", 45, 0.60),
    ("Mid",   "Small", 21, 0.85),
    ("Mid",   "Micro", 35, 0.70),
    ("Small", "Micro", 14, 0.80),
    ("Small", "Nano",  21, 0.50),
]


from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'CSS_SOURCE_SIGNAL_THRESHOLD': '_SOURCE_SIGNAL_THRESHOLD',
    'CSS_NANO_CONTRARIAN_CONFIDENCE_MAX': '_NANO_CONTRARIAN_CONFIDENCE_MAX',
    'B6_CROSS_SIZE_SIGNAL_JP_US_CROSS_DECAY': '_JP_US_CROSS_DECAY',
}

_SOURCE_SIGNAL_THRESHOLD = 5.0
_JP_US_CROSS_DECAY = 0.70
_NANO_CONTRARIAN_CONFIDENCE_MAX = 0.55
_MIN_SOURCE_TICKERS = 2

# ── 業種アフィニティマッピング（Wave 3） ──
_SECTOR_SIZE_AFFINITY: dict[str, str] = {
    "1028": "large", "1030": "large", "1017": "large", "1016": "large",
    "1026": "large", "1009": "large", "1012": "large", "1020": "large",
    "1025": "large", "1032": "large",
    "1033": "small", "1015": "small", "1018": "small", "1007": "small",
    "1019": "small", "1024": "small", "1031": "small",
    "1004": "mid",   "1008": "mid",   "1027": "mid",
}


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / _CACHE_FILENAME

def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        last_updated = data.get("last_updated", "")
        ttl = int(getattr(cfg, "CROSS_SIZE_SIGNAL_TTL_DAYS", _DEFAULT_TTL_DAYS))
        if last_updated:
            age = (datetime.now() - datetime.strptime(last_updated, "%Y-%m-%d")).days
            if age < ttl:
                return data
    except Exception:
        pass
    return None

def _save_cache(cfg, data: dict) -> None:
    try:
        data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        p = _cache_path(cfg)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  [cross_size] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: データ取得 & 基礎計算
# ==============================================================

def _get_tier_data(cfg) -> dict:
    tier_data = {}
    tiers = ["Large", "Mid", "Small", "Micro", "Nano"]
    try:
        from size_decay_manager import get_active_tickers_by_tier
        for tier in tiers:
            min_sig = 0.05 if tier == "Nano" else 0.10
            tier_data[tier] = get_active_tickers_by_tier(tier, cfg, min_sig)
    except ImportError:
        pass
    return tier_data

def _get_taxonomy_nodes_for_ticker(ticker: str, cfg) -> list:
    try:
        from size_tier_classifier import get_taxonomy_nodes
        return get_taxonomy_nodes(ticker, cfg)
    except ImportError:
        return []

def _calc_tier_avg_signal(tier_entries: list) -> float:
    try:
        if not tier_entries: return 0.0
        return round(sum(float(e.get("effective_signal", 0.0)) for e in tier_entries) / len(tier_entries), 4)
    except Exception: return 0.0

def _calc_spillover_strength(source_avg: float, target_avg: float, weight: float, lag_days: int) -> float:
    try:
        lag_decay = math.exp(-0.02 * lag_days)
        direction_gap = max(0.0, source_avg - target_avg)
        strength = source_avg * weight * lag_decay * (1.0 + direction_gap)
        return round(min(10.0, max(0.0, strength)), 4)
    except Exception: return 0.0

def _calc_confidence(source_entries: list, strength: float) -> float:
    try:
        n = len(source_entries)
        if n < _MIN_SOURCE_TICKERS: return 0.0
        return round(min(1.0, n / 10.0) * min(1.0, strength / 10.0), 4)
    except Exception: return 0.0

def _make_signal_id(signal_type: str, source_tier: str, target_tier: str) -> str:
    base = f"{signal_type}_{source_tier}_{target_tier}_{datetime.now().strftime('%Y%m%d')}"
    return hashlib.md5(base.encode()).hexdigest()[:12]


# ==============================================================
# BLOCK 3: シグナル生成ロジック
# ==============================================================

def _gen_upward_spillover_signals(tier_data: dict, cfg) -> list:
    signals = []
    today_str = datetime.now().strftime("%Y-%m-%d")

    for source_tier, target_tier, lag_days, weight in _SPILLOVER_RULES:
        try:
            src = tier_data.get(source_tier, [])
            tgt = tier_data.get(target_tier, [])
            if not src or not tgt: continue

            src_avg = _calc_tier_avg_signal(src)
            tgt_avg = _calc_tier_avg_signal(tgt)

            if src_avg < _SOURCE_SIGNAL_THRESHOLD: continue

            strength = _calc_spillover_strength(src_avg, tgt_avg, weight, lag_days)
            confidence = _calc_confidence(src, strength)
            if strength < 0.5 or confidence < 0.05: continue

            sig_id = _make_signal_id("upward_spillover", source_tier, target_tier)
            signals.append({
                "signal_id": sig_id,
                "signal_type": "upward_spillover",
                "source_tier": source_tier,
                "target_tier": target_tier,
                "source_tickers": [e["ticker"] for e in src[:10]],
                "target_tickers": [e["ticker"] for e in tgt[:15]],
                "strength": strength,
                "direction": "bullish",
                "lag_days": lag_days,
                "confidence": confidence,
                "generated_at": today_str,
            })
        except Exception: pass
    return signals

def _gen_downward_cascade_signals(tier_data: dict, cfg) -> list:
    signals = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    rules = [("Large", "Mid", 14, 0.85), ("Large", "Small", 28, 0.70), ("Mid", "Small", 14, 0.80)]

    for source_tier, target_tier, lag_days, weight in rules:
        try:
            src = tier_data.get(source_tier, [])
            if not src: continue

            avg_decay = sum(float(e.get("decay_factor", 1.0)) for e in src) / max(1, len(src))
            if avg_decay >= 0.60: continue

            strength = round((1.0 - avg_decay) * 10.0 * weight, 4)
            confidence = _calc_confidence(src, strength)
            if strength < 1.0: continue

            tgt = tier_data.get(target_tier, [])
            sig_id = _make_signal_id("downward_cascade", source_tier, target_tier)
            signals.append({
                "signal_id": sig_id,
                "signal_type": "downward_cascade",
                "source_tier": source_tier,
                "target_tier": target_tier,
                "source_tickers": [e["ticker"] for e in src[:10]],
                "target_tickers": [e["ticker"] for e in tgt[:15]],
                "strength": strength,
                "direction": "bearish",
                "lag_days": lag_days,
                "confidence": confidence,
                "generated_at": today_str,
            })
        except Exception: pass
    return signals

def _gen_contrarian_nano_signals(tier_data: dict, cfg) -> list:
    signals = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        nano_entries = sorted(tier_data.get("Nano", []), key=lambda x: float(x.get("effective_signal", 0.0)), reverse=True)
        for entry in nano_entries[:10]:
            ticker = entry.get("ticker", "")
            eff_sig = float(entry.get("effective_signal", 0.0))
            liq_rank = float(entry.get("liquidity_rank", 0))
            tax_corr = float(entry.get("taxonomy_size_correction", 1.0))
            liq_discount = min(1.0, liq_rank / 100.0)

            strength = round(eff_sig * tax_corr * liq_discount, 4)
            if strength < 0.3: continue
            confidence = round(min(_NANO_CONTRARIAN_CONFIDENCE_MAX, strength / 10.0), 4)

            sig_id = _make_signal_id("contrarian_nano", "Nano", ticker)
            signals.append({
                "signal_id": sig_id,
                "signal_type": "contrarian_nano",
                "source_tier": "Nano",
                "target_tier": "Nano",
                "source_tickers": [ticker],
                "target_tickers": [ticker],
                "strength": min(10.0, strength),
                "direction": "bullish",
                "lag_days": 0,
                "confidence": confidence,
                "generated_at": today_str,
            })
    except Exception: pass
    return signals

def _gen_jp_us_cross_signals(tier_data: dict, cfg) -> list:
    signals = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        us_large = [e for e in tier_data.get("Large", []) if e.get("market", "JP") == "US"]
        if not us_large: return signals

        us_large_avg = _calc_tier_avg_signal(us_large)
        if us_large_avg < _SOURCE_SIGNAL_THRESHOLD: return signals

        for target_tier in ("Small", "Micro"):
            jp_tgt = [e for e in tier_data.get(target_tier, []) if e.get("market", "JP") == "JP"]
            if not jp_tgt: continue

            lag_days, weight = (30, 0.65) if target_tier == "Small" else (45, 0.50)
            strength = round(us_large_avg * _JP_US_CROSS_DECAY * weight, 4)
            confidence = round(_calc_confidence(us_large, strength) * _JP_US_CROSS_DECAY, 4)

            if strength < 0.5 or confidence < 0.05: continue

            sig_id = _make_signal_id("jp_us_cross", "Large_US", f"{target_tier}_JP")
            signals.append({
                "signal_id": sig_id,
                "signal_type": "jp_us_cross",
                "source_tier": "Large_US",
                "target_tier": f"{target_tier}_JP",
                "source_tickers": [e["ticker"] for e in us_large[:10]],
                "target_tickers": [e["ticker"] for e in jp_tgt[:10]],
                "strength": min(10.0, strength),
                "direction": "bullish",
                "lag_days": lag_days,
                "confidence": confidence,
                "generated_at": today_str,
            })
    except Exception: pass
    return signals


# ==============================================================
# BLOCK 4: 外部参照用アクセサ & セクター集計 (Wave 3連携用)
# ==============================================================

def _build_ticker_signals_index(signals: list) -> dict:
    index = {}
    for sig in signals:
        for key in ("source_tickers", "target_tickers"):
            for ticker in sig.get(key, []):
                index.setdefault(ticker, []).append(sig.get("signal_id"))
    return index

def get_signals_for_ticker(ticker: str, cfg=None) -> list:
    """
    銘柄に関係するシグナルリストを返す。
    latent_pearl_manager.py / us_stock_screener.py から参照。

    Returns:
        [signal_dict, ...]
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return []
        signal_ids  = cache.get("ticker_signals", {}).get(ticker, [])
        all_signals = {s["signal_id"]: s for s in cache.get("signals", [])}
        return [all_signals[sid] for sid in signal_ids if sid in all_signals]
    except Exception:
        return []


def get_target_tickers_by_signal_type(
    signal_type: str,
    min_strength: float = 1.0,
    cfg=None,
) -> list:
    """
    指定シグナル種別のターゲット銘柄リストを返す（重複排除済み）。
    latent_pearl_manager.py の潜伏銘柄候補選定に使用。

    Returns:
        [ticker, ...]
    """
    try:
        sigs    = get_signals_by_type(signal_type, cfg)
        tickers = []
        for s in sigs:
            if float(s.get("strength", 0)) >= min_strength:
                tickers.extend(s.get("target_tickers", []))
        return list(dict.fromkeys(tickers))  # 順序保持の重複排除
    except Exception:
        return []

def get_signals_by_type(signal_type: str, cfg=None) -> list:
    cache = _load_cache(cfg)
    return [s for s in cache.get("signals", []) if s.get("signal_type") == signal_type] if cache else []

def aggregate_cross_size_score(sector_code: str, cfg=None) -> float:
    """
    (Wave 3 統合) セクターごとの影響スコアを算出して返す(0〜100)。
    signal_aggregator等から呼ばれることを想定。
    """
    try:
        affinity = _SECTOR_SIZE_AFFINITY.get(str(sector_code))
        if not affinity: return 50.0

        cache = _load_cache(cfg)
        if not cache: return 50.0

        signals = cache.get("signals", [])
        score_modifier = 0.0

        for sig in signals:
            stype = sig.get("signal_type")
            intensity = sig.get("strength", 0.0) / 10.0 # 0.0~1.0正規化

            # 小型優位シグナル (Smallアフィニティにプラス)
            if stype in ("upward_spillover", "jp_us_cross") and affinity == "small":
                score_modifier += (intensity * 50.0)
            
            # 大型下落シグナル (Largeアフィニティにマイナス)
            elif stype == "downward_cascade" and affinity == "large":
                score_modifier -= (intensity * 50.0)

        # 基準点50に足し引きして0〜100に収める
        final_score = 50.0 + score_modifier
        return round(min(100.0, max(0.0, final_score)), 1)

    except Exception:
        return 50.0


# ==============================================================
# BLOCK 5: メイン実行フロー
# ==============================================================

def run_cross_size_signal(cfg=None, force_refresh: bool = False) -> dict:
    """日次で実行される横断シグナル生成のエントリポイント"""
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🔀 規模帯横断シグナル生成（Wave 3 & 7 統合版）実行中...")

    if not force_refresh:
        cached = _load_cache(cfg)
        if cached:
            stats = cached.get("stats", {})
            print(f"    ✅ キャッシュ使用: シグナル={stats.get('total_signals', 0)} / 関連銘柄={stats.get('total_tickers', 0)}")
            return {"signals": cached.get("signals", []), "ticker_signals": cached.get("ticker_signals", {}), "stats": stats, "cached": True}

    tier_data = _get_tier_data(cfg)
    all_signals = []

    all_signals.extend(_gen_upward_spillover_signals(tier_data, cfg))
    all_signals.extend(_gen_downward_cascade_signals(tier_data, cfg))
    all_signals.extend(_gen_contrarian_nano_signals(tier_data, cfg))
    all_signals.extend(_gen_jp_us_cross_signals(tier_data, cfg))

    ticker_signals = _build_ticker_signals_index(all_signals)

    type_counts = {}
    for s in all_signals:
        t = s.get("signal_type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    stats = {
        "total_signals": len(all_signals),
        "total_tickers": len(ticker_signals),
        "signal_type_counts": type_counts,
        "avg_strength": round(sum(s.get("strength", 0) for s in all_signals) / max(1, len(all_signals)), 3) if all_signals else 0.0,
    }

    cache_data = {"signals": all_signals, "ticker_signals": ticker_signals, "stats": stats, "version": "2.0"}
    _save_cache(cfg, cache_data)

    print(f"    ✅ 横断シグナル完了: 合計={len(all_signals)} / 関連銘柄={len(ticker_signals)}")
    return {"signals": all_signals, "ticker_signals": ticker_signals, "stats": stats, "cached": False}
