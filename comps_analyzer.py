# ==============================================================
# comps_analyzer.py  v1.0  (5-2 / 財務5)
# 同業種内 PER/PBR/EV-EBITDA 相対評価 + 割安スコア統合
# ==============================================================
# 【役割】
#   screener 通過銘柄を「同業種内で相対的に割安か」という視点で
#   評価する。単純なPERだけでなく EV/EBITDA も使うことで、
#   利益をあまり出していない成長企業も適切に評価できる。
#
# 【計算ロジック】
#   1. JP_SECTOR_TICKERS から業種内の全銘柄の財務指標を取得
#   2. 各指標の業種中央値（メジアン）を算出
#   3. 対象銘柄の指標が中央値より有利かどうかをスコア化
#
# 【comps_score（0〜100）の計算】
#   PER 相対スコア   (40%) : 業種内ランク百分率
#   PBR 相対スコア   (30%) : 業種内ランク百分率
#   EV/EBITDA スコア(30%) : 業種内ランク百分率（データある場合のみ）
#   ROE 相対スコア          : 診断表示のみ（quality_scoreとの二重計上を防止）
#
# 【screener 統合】
#   comps_score と DCF を一元的に value_score へ統合する。
#   同じ候補へ再適用しても base_value_score から再計算するため二重計上しない。
#
# 【依存】yfinance / stock_screener.py の JP_SECTOR_TICKERS / config.py
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

FETCH_DELAY        = 0.2
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_COMPS_ANALYZER_CACHE_HOURS': 'CACHE_HOURS',
    'B5_COMPS_ANALYZER_FETCH_DELAY': 'FETCH_DELAY',
}

CACHE_HOURS        = 24
CACHE_FILENAME     = "comps_analyzer_cache.json"
_COMPS_SCORE_VERSION = 2


# ==============================================================
# BLOCK 1: 業種内銘柄の財務指標一括取得
# ==============================================================

def _fetch_sector_multiples(tickers: list) -> list:
    """
    業種内銘柄の財務倍率を取得する。

    Returns:
        [
          {"ticker", "per", "pbr", "roe",
           "ev_ebitda", "enterprise_value", "ebitda"},
          ...
        ]
    """
    try:
        import yf_cache as yf  # type: ignore
    except ImportError:
        return []

    records = []
    for t in tickers[:20]:   # 取得上限20銘柄（処理時間制限）
        try:
            info = yf.Ticker(t).info or {}
            per       = info.get("trailingPE")
            pbr       = info.get("priceToBook")
            roe       = info.get("returnOnEquity")
            ev        = info.get("enterpriseValue")
            ebitda    = info.get("ebitda")
            ev_ebitda = None
            if ev and ebitda and float(ebitda) > 0:
                ev_ebitda = round(float(ev) / float(ebitda), 2)

            records.append({
                "ticker":    t,
                "per":       round(float(per), 2) if per and float(per) > 0 else None,
                "pbr":       round(float(pbr), 2) if pbr and float(pbr) > 0 else None,
                "roe":       round(float(roe) * 100, 2) if roe else None,
                "ev_ebitda": ev_ebitda,
            })
            time.sleep(FETCH_DELAY)
        except Exception:
            records.append({"ticker": t})

    return [r for r in records if any(r.get(k) for k in ("per", "pbr", "roe"))]


def _median(vals: list) -> Optional[float]:
    """None を除いた中央値を返す。"""
    clean = sorted(v for v in vals if v is not None and v > 0)
    if not clean:
        return None
    n = len(clean)
    return clean[n // 2] if n % 2 else (clean[n//2 - 1] + clean[n//2]) / 2


def _percentile_rank(val: float, vals: list, higher_is_better: bool = True) -> float:
    """
    val が vals の中で何パーセンタイルか（0〜100）を返す。
    higher_is_better=True の場合、上位ほど高スコア。
    """
    clean = [v for v in vals if v is not None]
    if not clean:
        return 50.0
    rank = sum(1 for v in clean if v < val) / len(clean)
    return round((rank if higher_is_better else 1 - rank) * 100, 1)


# ==============================================================
# BLOCK 2: 相対スコア算出
# ==============================================================

def calc_comps_score(
    target_ticker:   str,
    sector_records:  list,
) -> dict:
    """
    対象銘柄の相対的割安度（comps_score）を算出する。

    Returns:
        {
          "comps_score":       float,   # 0〜100
          "per_rank":          float,   # 業種内 PER ランク（低いほど割安）
          "pbr_rank":          float,
          "roe_rank":          float,
          "ev_ebitda_rank":    float,
          "sector_median_per": float,
          "sector_median_pbr": float,
          "sector_median_roe": float,
          "comps_bonus":       float,   # value_score への加算値
          "label":             str,
          "n_peers":           int,
        }
    """
    target = next((r for r in sector_records if r["ticker"] == target_ticker), None)
    if not target:
        return {"comps_score": 50.0, "comps_bonus": 0.0, "label": "データなし", "n_peers": 0}

    all_per   = [r["per"]       for r in sector_records]
    all_pbr   = [r["pbr"]       for r in sector_records]
    all_roe   = [r["roe"]       for r in sector_records]
    all_ev    = [r.get("ev_ebitda") for r in sector_records]

    med_per = _median(all_per)
    med_pbr = _median(all_pbr)
    med_roe = _median(all_roe)
    med_ev  = _median([v for v in all_ev if v is not None])

    # PER ランク（低いほど割安 → higher_is_better=False）
    per_score = None
    if target.get("per") and med_per:
        per_score = _percentile_rank(target["per"], all_per, higher_is_better=False)

    # PBR ランク（低いほど割安）
    pbr_score = None
    if target.get("pbr") and med_pbr:
        pbr_score = _percentile_rank(target["pbr"], all_pbr, higher_is_better=False)

    # ROE ランク（高いほど優位）
    roe_score = None
    if target.get("roe") is not None:
        roe_score = _percentile_rank(target["roe"], all_roe, higher_is_better=True)

    # EV/EBITDA ランク（低いほど割安）
    ev_score = None
    if target.get("ev_ebitda") and med_ev:
        ev_score = _percentile_rank(target["ev_ebitda"], all_ev, higher_is_better=False)

    # 加重平均（利用可能な価値指標だけで計算）。ROE は quality_score の
    # 主軸として既に総合点へ入るため、診断順位には残すがここでは寄与させない。
    parts = []
    if per_score is not None: parts.append((per_score, 0.40))
    if pbr_score is not None: parts.append((pbr_score, 0.30))
    if ev_score  is not None: parts.append((ev_score,  0.30))

    if not parts:
        return {"comps_score": 50.0, "comps_bonus": 0.0, "label": "指標不足", "n_peers": len(sector_records)}

    total_weight = sum(w for _, w in parts)
    comps_score  = sum(s * w for s, w in parts) / total_weight
    comps_score  = round(comps_score, 1)

    # comps_bonus: value_score に加算する量（±10pt）
    comps_bonus = round((comps_score - 50) / 50 * 10, 1)
    comps_bonus = max(-10, min(10, comps_bonus))

    if comps_score >= 70: label = "🟢 同業種内 割安"
    elif comps_score >= 55: label = "🟡 やや割安"
    elif comps_score >= 45: label = "⚪ 業種内 適正"
    elif comps_score >= 30: label = "🟠 やや割高"
    else:                   label = "🔴 同業種内 割高"

    return {
        "comps_score":       comps_score,
        "per_rank":          per_score,
        "pbr_rank":          pbr_score,
        "roe_rank":          roe_score,
        "ev_ebitda_rank":    ev_score,
        "sector_median_per": round(med_per, 1) if med_per else None,
        "sector_median_pbr": round(med_pbr, 2) if med_pbr else None,
        "sector_median_roe": round(med_roe, 1) if med_roe else None,
        "sector_median_ev":  round(med_ev, 1) if med_ev else None,
        "target_per":        target.get("per"),
        "target_pbr":        target.get("pbr"),
        "target_roe":        target.get("roe"),
        "target_ev_ebitda":  target.get("ev_ebitda"),
        "comps_bonus":       comps_bonus,
        "label":             label,
        "n_peers":           len(sector_records),
    }


# ==============================================================
# BLOCK 3: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME

def _load_cache(cfg) -> dict:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("score_version") != _COMPS_SCORE_VERSION:
            return {}
        if datetime.now() - datetime.fromisoformat(data.get("cached_at", "2000-01-01")) \
                < timedelta(hours=CACHE_HOURS):
            return data.get("results", {})
    except Exception:
        pass
    return {}

def _save_cache(cfg, results: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"cached_at": datetime.now().isoformat(),
                                  "score_version": _COMPS_SCORE_VERSION,
                                  "results": results}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass

def get_comps_bonus(ticker: str, comps_cache: dict) -> float:
    """stock_screener.py から呼び出すインターフェース。"""
    return float(comps_cache.get(ticker, {}).get("comps_bonus", 0.0))


def integrate_valuation_score(
    base_value_score: float,
    comps_data: Optional[dict] = None,
    dcf_bonus: Optional[float] = None,
    cfg=None,
) -> dict:
    """既存財務・comps・DCFを一度だけ統合した value_score を返す。

    comps は加算ではなく base_value_score との差分ブレンド、DCF は既存の
    段階ボーナスとする。利用可能なソースだけを使い、合計寄与を制限する。
    """
    base = max(0.0, min(100.0, float(base_value_score)))
    weight = float(getattr(cfg, "COMPS_VALUE_BLEND_WEIGHT", 0.30))
    comps_cap = abs(float(getattr(cfg, "COMPS_VALUE_DELTA_CAP", 10.0)))
    total_cap = abs(float(getattr(cfg, "VALUATION_TOTAL_ADJUSTMENT_CAP", 10.0)))
    if not 0.0 <= weight <= 1.0:
        raise ValueError("COMPS_VALUE_BLEND_WEIGHT must be between 0 and 1")

    comps_delta = 0.0
    comps_score = None
    if isinstance(comps_data, dict) and comps_data.get("comps_score") is not None:
        try:
            comps_score = max(0.0, min(100.0, float(comps_data["comps_score"])))
            comps_delta = max(-comps_cap, min(comps_cap, weight * (comps_score - base)))
        except (TypeError, ValueError):
            comps_score = None
            comps_delta = 0.0

    try:
        dcf_delta = float(dcf_bonus) if dcf_bonus is not None else 0.0
    except (TypeError, ValueError):
        dcf_delta = 0.0

    total_delta = max(-total_cap, min(total_cap, comps_delta + dcf_delta))
    value_score = max(0.0, min(100.0, base + total_delta))
    sources = []
    if comps_score is not None:
        sources.append("comps")
    if dcf_bonus is not None:
        sources.append("dcf")
    return {
        "base_value_score": round(base, 1),
        "value_score": round(value_score, 1),
        "comps_score": round(comps_score, 1) if comps_score is not None else None,
        "comps_delta": round(comps_delta, 1),
        "dcf_delta": round(dcf_delta, 1),
        "valuation_adjustment": round(total_delta, 1),
        "valuation_sources": sources,
    }


# ==============================================================
# BLOCK 4: メインエントリポイント
# ==============================================================

def run_comps_analyzer(cfg, screener_result: dict) -> dict:
    """
    screener通過銘柄の同業種内相対評価を実行する。

    stock_analyzer_main.py から呼び出す:
        _ca = _load_local_module("comps_analyzer", ...)
        ca_result = _ca.run_comps_analyzer(cfg, screen_result)
        results["comps_analyzer"] = ca_result

    Returns:
        {"results": {ticker: comps_result}, "top_cheap": [...], "timestamp": str}
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📊 同業種内比較分析（comps）開始...")

    cached   = _load_cache(cfg)
    passed   = [c for c in screener_result.get("candidates", []) if c.get("pass_filters")]
    to_fetch = [c for c in passed if c.get("ticker", "") not in cached]
    results  = dict(cached)

    # 業種別にグループ化（同業種を1回のバッチで取得）
    sector_groups: dict[str, list] = {}
    for c in to_fetch:
        code = c.get("sector_code", "")
        sector_groups.setdefault(code, []).append(c)

    # JP_SECTOR_TICKERS を取得
    jp_sector_tickers = {}
    try:
        import importlib, sys
        sys.path.insert(0, str(Path(__file__).parent))
        ss = importlib.import_module("stock_screener")
        jp_sector_tickers = ss.JP_SECTOR_TICKERS
    except Exception:
        pass

    for sector_code, sector_candidates in sector_groups.items():
        tickers_in_sector = jp_sector_tickers.get(sector_code, [])[:20]
        if not tickers_in_sector:
            continue

        print(f"    [{sector_code}] 業種内比較取得中（{len(tickers_in_sector)}銘柄）...")
        sector_records = _fetch_sector_multiples(tickers_in_sector)
        if len(sector_records) < 3:
            continue

        for c in sector_candidates:
            ticker = c.get("ticker", "")
            if not ticker:
                continue
            comps = calc_comps_score(ticker, sector_records)
            results[ticker] = comps
            if abs(comps.get("comps_bonus", 0)) >= 3:
                print(f"      {ticker}: {comps['label']} bonus={comps.get('comps_bonus',0):+.1f}pt "
                      f"(PER {comps.get('target_per','N/A')} vs 中央値{comps.get('sector_median_per','N/A')})")

    _save_cache(cfg, results)

    top_cheap = sorted(
        [{"ticker": t, **d} for t, d in results.items() if d.get("comps_bonus", 0) >= 3],
        key=lambda x: -x.get("comps_score", 0))[:10]

    print(f"    ✅ comps分析完了: {len(results)}銘柄 / 割安候補{len(top_cheap)}件")
    return {"results": results, "top_cheap": top_cheap,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
