# ==============================================================
# jp_us_sector_bridge.py  v1.0
# JP業種 × US セクター 横断相関分析 + ローテーション検出
# ==============================================================
# 【役割】
#   JP 33業種と US 11セクターの composite_score の週次相関を計算し、
#   「XLK(IT)が上昇した翌週に 1016(電気機器)が上昇する」という
#   パターンをデータから自動発見して業種スコアに先行シグナルを注入する。
#
# 【追加実装】
#   sector_rotation_detector: 業種スコアの時系列から
#   GARTHサイクル（上昇加速/天井/下落加速/底値）の4象限を判定
#
# 【composite_score への影響】
#   JP業種 X の1週先行シグナルがある場合:
#     相関 > 0.5 かつ US 先行業種がスコア上昇中 → JP業種に +3pt
#     相関 > 0.5 かつ US 先行業種がスコア下落中 → JP業種に -3pt
#
# 【依存】score_history.json / config.py
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
CORR_LOOKBACK_WEEKS  = 52   # 相関計算期間（1年）
CORR_SIGNIFICANT     = 0.45  # 有意な相関閾値
LAG_ADJUSTMENT_PTS   = 3.0   # 先行シグナルによる composite_score 補正
ROTATION_LOOKBACK    = 8     # ローテーション判定に使う週数

# JP業種コード → US ETF の対応マップ（相関が高い組み合わせ）
JP_US_AFFINITY = {
    "1016": "XLK",   # 電気機器 ↔ IT
    "1017": "XLY",   # 輸送用機器 ↔ 一般消費財
    "1028": "XLF",   # 銀行業 ↔ 金融
    "1030": "XLF",   # 保険業 ↔ 金融
    "1008": "XLV",   # 医薬品 ↔ ヘルスケア
    "1009": "XLE",   # 石油 ↔ エネルギー
    "1012": "XLB",   # 鉄鋼 ↔ 素材
    "1013": "XLB",   # 非鉄 ↔ 素材
    "1015": "XLI",   # 機械 ↔ 資本財
    "1020": "XLU",   # 電力ガス ↔ 公益
    "1032": "XLRE",  # 不動産 ↔ REIT
    "1025": "XLC",   # 情報通信 ↔ 通信
    "1027": "XLY",   # 小売 ↔ 一般消費財
}

CACHE_FILENAME = "jp_us_bridge_cache.json"
CACHE_HOURS    = 24 * 7

# ── Wave 7 追加定数（規模帯横断・独立性スコアブリッジ）──────────
# independence_score_calculator.py のスコアを bridge 経由で渡す際の
# 最低閾値（これ未満は cross_market_latent の対象外）
INDEPENDENCE_BRIDGE_THRESHOLD: float = 40.0

# size_decay_manager.py の有効シグナルを bridge 経由で参照する際のフラグキー
SIZE_DECAY_BRIDGE_KEY: str = "size_decay"

# JP/US 規模帯横断シグナルの business に加算する際の最大ブーストpt
CROSS_SIZE_BRIDGE_MAX_BOOST: float = 5.0

# ==============================================================
# BLOCK 1: score_history からJP/USの週次スコアを読み込む
# ==============================================================

def _load_score_history(cfg) -> dict:
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
        / "insights" / "score_history.json",
        Path("stock_data/insights/score_history.json"),
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _load_us_score_history(cfg) -> dict:
    """
    US セクタースコアの時系列を読み込む。
    us_sector_pipeline.py が stock_data/insights/us_sectors/ に
    us_sector_scores_YYYY-MM-DD.json を生成している想定。

    Returns:
        {"YYYY-MM-DD": {"XLK": float, "XLF": float, ...}}
    """
    us_dir_candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
        / "insights" / "us_sectors",
        Path("stock_data/insights/us_sectors"),
    ]
    result = {}
    for d in us_dir_candidates:
        if not d.exists():
            continue
        for f in sorted(d.glob("us_sector_scores_*.json"))[-52:]:   # 最新52ファイル
            try:
                date_str = f.stem.replace("us_sector_scores_", "")
                data = json.loads(f.read_text(encoding="utf-8"))
                # sectors は {ETF: {composite_score: float}} 形式
                day_scores = {}
                for etf, info in data.get("sectors", {}).items():
                    s = info.get("composite_score") or info.get("score")
                    if s is not None:
                        day_scores[etf] = float(s)
                if day_scores:
                    result[date_str] = day_scores
            except Exception:
                continue
        break

    return result


def _to_weekly(daily_history: dict, score_key: Optional[str] = None) -> dict:
    """
    日次スコア履歴を週次（月曜週）に変換する。

    Returns:
        {"YYYY-W01": {"CODE/ETF": score_float, ...}}
    """
    weekly: dict[str, dict[str, list]] = {}
    for date_str, day_data in sorted(daily_history.items()):
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            week_key = dt.strftime("%Y-W%V")
        except ValueError:
            continue

        weekly.setdefault(week_key, {})
        for code, val in day_data.items():
            if isinstance(val, dict):
                score = val.get("score") or val.get("composite_score")
            else:
                score = val
            if score is not None:
                weekly[week_key].setdefault(code, []).append(float(score))

    # 週末終値（週の最後の日）を代表値とする
    return {
        week: {code: round(vals[-1], 2) for code, vals in codes.items()}
        for week, codes in weekly.items()
        if codes
    }


# ==============================================================
# BLOCK 2: ラグ付き相関計算（JP業種は1週遅れで反応するか）
# ==============================================================

def _pearson(xs: list, ys: list) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    xs = xs[:n]; ys = ys[:n]
    mx = sum(xs) / n; my = sum(ys) / n
    num = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    sx  = math.sqrt(sum((x-mx)**2 for x in xs))
    sy  = math.sqrt(sum((y-my)**2 for y in ys))
    if sx < 1e-9 or sy < 1e-9:
        return None
    return round(num / (sx*sy), 4)


def calc_lagged_correlations(
    jp_weekly: dict, us_weekly: dict,
    lookback: int = CORR_LOOKBACK_WEEKS,
) -> dict:
    """
    JP業種スコアの変化 と US ETFスコアの 1週前の変化 の相関を計算する。
    （US が先行指標として機能するか検証）

    Returns:
        {
          "1016": {
            "us_etf":   "XLK",
            "lag0_corr": 0.62,   # 同時相関
            "lag1_corr": 0.55,   # US 1週先行
            "best_lag":  1,      # より高い相関が出る時差
            "best_corr": 0.55,
            "significant": True,
          },
          ...
        }
    """
    weeks = sorted(set(jp_weekly.keys()) & set(us_weekly.keys()))[-lookback:]
    if len(weeks) < 10:
        return {}

    results = {}
    for jp_code, us_etf in JP_US_AFFINITY.items():
        jp_scores = [jp_weekly.get(w, {}).get(jp_code) for w in weeks]
        us_scores = [us_weekly.get(w, {}).get(us_etf) for w in weeks]

        # None を除外してインデックスを合わせる
        valid = [(jp, us) for jp, us in zip(jp_scores, us_scores)
                 if jp is not None and us is not None]
        if len(valid) < 10:
            continue

        jp_v = [v[0] for v in valid]
        us_v = [v[1] for v in valid]

        # 変化率に変換
        jp_ret = [jp_v[i]-jp_v[i-1] for i in range(1, len(jp_v))]
        us_ret = [us_v[i]-us_v[i-1] for i in range(1, len(us_v))]

        # 同時相関
        lag0 = _pearson(jp_ret, us_ret)

        # 1週先行（US が JP の1週前）
        if len(jp_ret) > 2 and len(us_ret) > 2:
            lag1 = _pearson(jp_ret[1:], us_ret[:-1])
        else:
            lag1 = None

        best_corr = max(
            [(lag0 or 0, 0), (lag1 or 0, 1)],
            key=lambda x: abs(x[0])
        )

        results[jp_code] = {
            "us_etf":    us_etf,
            "lag0_corr": lag0,
            "lag1_corr": lag1,
            "best_lag":  best_corr[1],
            "best_corr": best_corr[0],
            "significant": abs(best_corr[0]) >= CORR_SIGNIFICANT,
        }

    return results


# ==============================================================
# BLOCK 3: 先行シグナル生成
# ==============================================================

def calc_lead_signal_adjustments(
    lagged_corrs: dict,
    us_weekly:    dict,
    sector_results: dict,
) -> dict:
    """
    US の直近スコア変化 × 相関係数から JP 業種補正量を算出する。

    Returns:
        {jp_sector_code: delta_float}
    """
    if not us_weekly or not lagged_corrs:
        return {}

    # 直近2週のUSスコア変化
    us_weeks = sorted(us_weekly.keys())
    if len(us_weeks) < 2:
        return {}

    us_latest = us_weekly[us_weeks[-1]]
    us_prev   = us_weekly[us_weeks[-2]]

    adjustments = {}
    for jp_code, corr_data in lagged_corrs.items():
        if not corr_data.get("significant"):
            continue
        us_etf  = corr_data["us_etf"]
        best_c  = corr_data["best_corr"]
        best_lag = corr_data["best_lag"]

        if best_lag == 0:
            continue   # 同時相関のみは先行シグナルとして使えない

        us_now  = us_latest.get(us_etf)
        us_prv  = us_prev.get(us_etf)
        if us_now is None or us_prv is None:
            continue

        us_change = us_now - us_prv
        # 相関が正で US が上昇 → JP にプラス補正
        direction = 1 if (best_c > 0 and us_change > 0) or (best_c < 0 and us_change < 0) else -1
        magnitude = min(LAG_ADJUSTMENT_PTS, abs(best_c) * abs(us_change) * 2)
        delta     = round(direction * magnitude, 1)

        if abs(delta) >= 0.5 and jp_code in sector_results:
            adjustments[jp_code] = delta

    return adjustments


# ==============================================================
# BLOCK 4: セクターローテーション検出（4象限）
# ==============================================================

def detect_rotation_phase(
    jp_weekly: dict,
    lookback: int = ROTATION_LOOKBACK,
) -> dict:
    """
    業種スコアの時系列から現在のローテーション局面を判定する。

    4象限（Relative Rotation Graph 簡易版）:
      LEADING:   スコア高 + 上昇加速中 → 保有継続
      WEAKENING: スコア高 + 上昇鈍化中 → 利益確定検討
      LAGGING:   スコア低 + 下落継続中 → 回避
      IMPROVING: スコア低 + 下落鈍化中 → 仕込み検討

    Returns:
        {sector_code: {"phase": str, "score": float, "momentum": float}}
    """
    weeks = sorted(jp_weekly.keys())[-lookback:]
    if len(weeks) < 4:
        return {}

    results = {}
    all_codes = set()
    for w in weeks:
        all_codes.update(jp_weekly[w].keys())

    for code in all_codes:
        scores = [jp_weekly.get(w, {}).get(code) for w in weeks]
        scores = [s for s in scores if s is not None]
        if len(scores) < 4:
            continue

        current_score = scores[-1]
        avg_score     = sum(scores) / len(scores)

        # モメンタム: 直近の傾き（線形回帰の傾き）
        n = len(scores)
        x_mean = (n - 1) / 2
        numerator   = sum((i - x_mean) * (scores[i] - avg_score) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        momentum    = numerator / denominator if denominator > 0 else 0

        # 象限判定（スコア50が基準）
        above_avg = current_score > 50
        rising    = momentum > 0.1

        if above_avg and rising:
            phase = "LEADING"
        elif above_avg and not rising:
            phase = "WEAKENING"
        elif not above_avg and not rising:
            phase = "LAGGING"
        else:
            phase = "IMPROVING"

        results[code] = {
            "phase":    phase,
            "score":    round(current_score, 1),
            "momentum": round(momentum, 3),
        }

    return results

# ==============================================================
# BLOCK 4.5: 議員取引シグナルのJP業種波及（Wave 0 - Policy Ph1）
# ==============================================================

def apply_politician_signal_to_jp(
    politician_result: dict,
    max_boost_pt:      float = 5.0,
    results:           Optional[dict] = None,
    cfg=None,
) -> dict:
    """
    議員取引シグナルのJP業種波及（Wave 3 - Policy Ph2 Level 4 Override対応版）。

    Level 4 Override 発動セクターは通常ブーストの 1.5倍 を適用する。
    boost_L4 = min(SCORE_MAX_POLICY_L4_ADJ, delta x 1.5)

    Args:
        politician_result : politician_monitor.run_politician_monitor() の返り値
        max_boost_pt      : 通常シグナルの最大波及量（デフォルト: 5.0pt）
        results           : stock_analyzer_main.results 辞書（L4判定用）
        cfg               : config モジュール（None 可）

    Returns:
        {jp_sector_code: delta_pt}
    politician_monitor.run_politician_monitor() の結果から
    JP業種コードへの波及シグナル（±max_boost_pt）を返す。

    politician_monitor 側で GICS→JP 変換・スコア按分済みの
    jp_sector_signals を受け取り、値の検証・クランプのみ行う。
    signal_aggregator が results["jp_us_bridge"]["politician_adjustments"]
    経由で参照する想定。

    Args:
        politician_result : politician_monitor.run_politician_monitor() の返り値
        max_boost_pt      : 1業種あたりの最大波及量（デフォルト: 5.0pt）

    Returns:
        {jp_sector_code: delta_pt}
        ・delta_pt > 0 → 買い取引による追い風
        ・delta_pt < 0 → 売り取引による逆風
        ・エラー時は空 dict（例外を外に投げない）
    """
    try:
        if not politician_result or politician_result.get("skipped"):
            return {}

        raw_signals = politician_result.get("jp_sector_signals", {})
        if not raw_signals:
            return {}

        # Level 4 Override 対象セクターを取得
        l4_sectors: set[str] = set()
        try:
            if results:
                scorer_result = results.get("politician_scorer", {})
                l4_boosts     = scorer_result.get("sector_boosts", {})
                l4_sectors    = set(l4_boosts.keys())
        except Exception:
            l4_sectors = set()

        l4_max_adj   = float(getattr(cfg, "SCORE_MAX_POLICY_L4_ADJ", 25.0)) \
                       if cfg else 25.0
        l4_multiplier = 1.5   # Level 4 Override ブースト倍率

        result: dict = {}
        for code, delta in raw_signals.items():
            if not isinstance(code, str):
                continue
            try:
                delta_f = float(delta)
            except (TypeError, ValueError):
                continue

            # Level 4 Override セクターは 1.5倍ブースト
            if code in l4_sectors:
                effective_max = min(l4_max_adj, max_boost_pt * l4_multiplier)
                clamped = round(
                    max(-effective_max, min(effective_max, delta_f * l4_multiplier)), 2
                )
            else:
                clamped = round(
                    max(-max_boost_pt, min(max_boost_pt, delta_f)), 2
                )

            if abs(clamped) >= 0.1:
                result[code] = clamped

        return result

    except Exception:
        return {}

# ==============================================================
# BLOCK 4.6: クロスマーケット潜伏銘柄検出（Wave 3 - Independence Ph1/Ph2）
# ==============================================================

def detect_cross_market_latent(
    results:  dict,
    cfg=None,
) -> list[dict]:
    """
    JP 潜伏銘柄（Latent Pearl）と US 市場の先行シグナルを突合し、
    両市場で独立性が高い「クロスマーケット潜伏銘柄」を検出する。

    検出条件:
      ① JP independence_score が LATENT_PEARL_THRESHOLD(70) 以上
      ② 対応する JP 業種が US 先行シグナル（lead_adjustments）でプラス
      ③ cross_size_signal がそのセクターで "small_leading" または確認済

    Args:
        results: stock_analyzer_main.results 辞書
        cfg:     config モジュール（None 可）

    Returns:
        [
          {
            "ticker":            str,
            "sector_code":       str,
            "independence_score": float,
            "us_lead_adj":       float,
            "cross_size_type":   str,
            "confidence":        str,    # "high" | "medium"
          }, ...
        ]
        independence_score 降順にソート済み
    """
    try:
        latent_candidates: list[dict] = []

        # ── Latent Pearl キャッシュ取得 ───────────────────────
        try:
            from latent_pearl_manager import (
                get_latent_pearls_list  as _get_lpl,
            )
            # {ticker: entry_dict} 形式に変換
            all_latents = {
                e["ticker"]: e
                for e in _get_lpl(cfg, min_score=0.0)
                if e.get("ticker")
            }
        except Exception:
            return []

        if not all_latents:
            return []

        # ── US 先行補正マップ取得 ─────────────────────────────
        bridge    = results.get("jp_us_bridge", {})
        lead_adj  = bridge.get("lead_adjustments", {})

        # ── Cross Size シグナルマップ取得 ─────────────────────
        # ★ 2026-06-10 バグ修正: 旧実装は results["cross_size"]["sector_signals"]
        #   （業種別）を参照していたが、プロデューサ cross_size_signal.py の
        #   実出力は results["cross_size_signal"] = {"signals":[...],
        #   "ticker_signals":{ticker:[signal_id,...]}} で業種別キーは存在せず、
        #   さらに判定文字列 "small_leading"/"confirmed" も実際の signal_type
        #   4種（upward_spillover 等）と不一致のため、本チェックは常時素通り
        #   だった。銘柄単位×direction ベースの判定に修正。
        cross_size   = results.get("cross_size_signal",
                                   results.get("cross_size", {})) or {}
        _cs_by_id    = {
            s.get("signal_id"): s
            for s in cross_size.get("signals", []) if isinstance(s, dict)
        }
        _cs_tk_index = cross_size.get("ticker_signals", {}) or {}

        # ── Wave 7: independence_scores.json からスコアを補強 ──
        _independence_lookup: dict = {}
        try:
            import json as _jb
            from pathlib import Path as _pb
            _ind_p = (
                _pb(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
                / "independence_scores.json"
            )
            if _ind_p.exists():
                _ind_data = _jb.loads(_ind_p.read_text(encoding="utf-8"))
                for _tk, _sc in _ind_data.get("scores", {}).items():
                    _independence_lookup[_tk] = float(
                        _sc.get("independence_score", 0.0)
                    )
        except Exception:
            pass

        # ── Wave 7: size_decay_state.json からTier別有効シグナルを取得 ──
        _decay_lookup: dict = {}
        try:
            import json as _jd
            from pathlib import Path as _pd
            _dec_p = (
                _pd(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
                / "size_decay_state.json"
            )
            if _dec_p.exists():
                _dec_data = _jd.loads(_dec_p.read_text(encoding="utf-8"))
                for _tk, _de in _dec_data.get("tickers", {}).items():
                    _decay_lookup[_tk] = {
                        "effective_signal":         float(_de.get("effective_signal", 0.0)),
                        "size_tier":                _de.get("size_tier", "Mid"),
                        "taxonomy_size_correction": float(_de.get("taxonomy_size_correction", 1.0)),
                    }
        except Exception:
            pass

        # ── メインループ ──────────────────────────────────────
        for ticker, entry in all_latents.items():
            try:
                # Wave 7: independence_scores.json 優先、なければ latent_pearl キャッシュ
                ind_score = float(
                    _independence_lookup.get(ticker)
                    or entry.get("independence_score")
                    or entry.get("latest_score")
                    or 0.0
                )

                # size_decay から Tier・有効シグナルを取得
                _decay_entry = _decay_lookup.get(ticker, {})
                _size_tier   = _decay_entry.get("size_tier", entry.get("size_tier", "Mid"))
                _eff_signal  = _decay_entry.get("effective_signal", 0.0)
                _tax_corr    = _decay_entry.get("taxonomy_size_correction", 1.0)

                sc = str(entry.get("sector_code", ""))

                # ① Independence Score フィルタ
                threshold = float(getattr(cfg, "LATENT_PEARL_THRESHOLD", 70.0))
                if ind_score < threshold:
                    continue

                # ② US 先行補正チェック
                us_adj = float(lead_adj.get(sc, 0.0))

                # ③ Cross Size シグナルチェック（銘柄単位 / 2026-06-10 修正）
                _cs_sigs = [
                    _cs_by_id[_sid]
                    for _sid in _cs_tk_index.get(ticker, [])
                    if _sid in _cs_by_id
                ]
                cs_types = [str(s.get("signal_type", "")) for s in _cs_sigs]
                cs_type  = "+".join(cs_types) if cs_types else "neutral"

                # 少なくとも ① を満たし ②③ のどちらかが正の場合
                has_us_signal = us_adj > 0.5
                has_cs_signal = any(
                    str(s.get("direction", "")) == "bullish" for s in _cs_sigs
                )

                if not (has_us_signal or has_cs_signal):
                    continue

                confidence = "high" if (has_us_signal and has_cs_signal) else "medium"

                latent_candidates.append({
                    "ticker":             ticker,
                    "sector_code":        sc,
                    "independence_score": ind_score,
                    "us_lead_adj":        us_adj,
                    "cross_size_type":    cs_type,
                    "confidence":         confidence,
                    "days_in_cache":      entry.get("days_in_cache", 0),
                    "size_tier":          _size_tier,
                    "effective_signal":   _eff_signal,
                    "taxonomy_correction": _tax_corr,
                    "latent_pearl_score": float(entry.get("latent_pearl_score", 0.0)),
                    "catalyst_flags":     entry.get("catalyst_flags", []),
                })
            except Exception:
                continue

        return sorted(
            latent_candidates,
            key=lambda x: x["independence_score"],
            reverse=True,
        )

    except Exception:
        return []
        
# ==============================================================
# BLOCK 5: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME

def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if datetime.now() - datetime.fromisoformat(data.get("cached_at", "2000-01-01")) \
                < timedelta(hours=CACHE_HOURS):
            return data
    except Exception:
        pass
    return None

def _save_cache(cfg, data: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        data["cached_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_jp_us_bridge(cfg, sector_results: dict) -> dict:
    """
    JP×US横断相関分析とローテーション検出を実行する。

    stock_analyzer_main.py から週次で呼び出す:
        _bridge = _load_local_module("jp_us_sector_bridge", ...)
        bridge_result = _bridge.run_jp_us_bridge(cfg, sector_results)
        results["jp_us_bridge"] = bridge_result

    Returns:
        {
          "lagged_correlations": dict,
          "lead_adjustments":    dict,  # {sector_code: delta}
          "rotation_phases":     dict,  # {sector_code: phase}
          "summary_text":        str,
          "timestamp":           str,
        }
    """
    print("\n  🌐 JP×US横断分析開始...")

    cached = _load_cache(cfg)
    if cached:
        adj = cached.get("lead_adjustments", {})
        print(f"    ✅ キャッシュ使用（先行補正: {len(adj)}業種）")
        return cached

    score_history = _load_score_history(cfg)
    us_history    = _load_us_score_history(cfg)

    if not score_history:
        print("    ⚠️  score_history.json なし → スキップ")
        return {"skipped": True, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # 週次変換
    jp_weekly = _to_weekly(score_history)
    us_weekly = _to_weekly(us_history)

    print(f"    JP: {len(jp_weekly)}週分 / US: {len(us_weekly)}週分")

    # ラグ相関計算
    lagged_corrs = calc_lagged_correlations(jp_weekly, us_weekly)
    sig_count    = sum(1 for d in lagged_corrs.values() if d.get("significant"))
    print(f"    有意な先行相関: {sig_count}/{len(lagged_corrs)}業種ペア")

    for jp_code, d in sorted(lagged_corrs.items(),
                               key=lambda x: abs(x[1].get("best_corr", 0)), reverse=True)[:5]:
        print(f"      {jp_code} ↔ {d['us_etf']}: r={d['best_corr']:.3f} "
              f"(lag={d['best_lag']}週)")

    # 先行シグナル補正
    lead_adj = calc_lead_signal_adjustments(lagged_corrs, us_weekly, sector_results)
    if lead_adj:
        print(f"    先行シグナル補正: {len(lead_adj)}業種")
        for code, delta in sorted(lead_adj.items(), key=lambda x: abs(x[1]), reverse=True)[:3]:
            print(f"      [{code}]: {delta:+.1f}pt")

    # ── Cross-Size シグナル生成（Wave 3 - Size Ph3）───────────
    cross_size_result: dict = {}
    try:
        from cross_size_signal import run_cross_size_signal as _run_css
        # Wave 7: force_refresh=False でキャッシュ優先（日次実行済みのキャッシュを利用）
        # results に既存データを渡して cross_size を生成
        cross_size_result = _run_css(cfg=cfg, force_refresh=False)
        print(f"    📐 Cross-Size: シグナル={cross_size_result.get('stats', {}).get('total_signals', 0)}件")
    except Exception as _cse:
        print(f"    ⚠️  Cross-Size計算エラー（スキップ）: {str(_cse)[:60]}")

    # ローテーション局面
    rotation = detect_rotation_phase(jp_weekly)

    # サマリーテキスト生成
    leading   = [c for c, d in rotation.items() if d["phase"] == "LEADING"]
    weakening = [c for c, d in rotation.items() if d["phase"] == "WEAKENING"]
    improving = [c for c, d in rotation.items() if d["phase"] == "IMPROVING"]

    lines = [
        "## 🌐 JP×US横断分析",
        f"  有意な先行相関: {sig_count}業種ペア（閾値 r>{CORR_SIGNIFICANT}）",
        f"  LEADING: {len(leading)}業種 / WEAKENING: {len(weakening)}業種 / IMPROVING: {len(improving)}業種",
    ]
    if improving:
        names = " / ".join(f"[{c}]" for c in improving[:5])
        lines.append(f"  💡 仕込み候補（IMPROVING）: {names}")

    # ── Wave 7: bridge 用の独立性・減衰スコアを再取得 ────────────
    # detect_cross_market_latent 内のローカル変数を result に公開するため
    # 同一パスを参照して辞書を構築する。
    _bridge_independence: dict = {}
    _bridge_size_decay:   dict = {}
    try:
        import json as _jbr
        from pathlib import Path as _pbr
        _cache_dir_br = _pbr(getattr(cfg, "CACHE_DIR", "stock_data/cache"))

        _ind_path_br = _cache_dir_br / "independence_scores.json"
        if _ind_path_br.exists():
            _ind_br = _jbr.loads(_ind_path_br.read_text(encoding="utf-8"))
            _bridge_independence = {
                tk: float(sc.get("independence_score", 0.0))
                for tk, sc in _ind_br.get("scores", {}).items()
            }

        _dec_path_br = _cache_dir_br / "size_decay_state.json"
        if _dec_path_br.exists():
            _dec_br = _jbr.loads(_dec_path_br.read_text(encoding="utf-8"))
            _bridge_size_decay = {
                tk: {
                    "effective_signal":         float(de.get("effective_signal", 0.0)),
                    "size_tier":                de.get("size_tier", "Mid"),
                    "taxonomy_size_correction": float(de.get("taxonomy_size_correction", 1.0)),
                }
                for tk, de in _dec_br.get("tickers", {}).items()
            }
        if _bridge_independence:
            print(f"    🔗 Wave 7 Bridge: independence={len(_bridge_independence)}銘柄 / size_decay={len(_bridge_size_decay)}銘柄")
    except Exception as _br_e:
        print(f"    ⚠️  Wave 7 Bridge データ取得失敗（スキップ）: {str(_br_e)[:60]}")

    # ── クロスマーケット潜伏銘柄検出（Wave 3 - Independence Ph1/Ph2）──
    cross_latent: list[dict] = []
    try:
        _tmp_for_latent = {
            **sector_results,
            "jp_us_bridge":  {"lead_adjustments": lead_adj},
            "cross_size":    cross_size_result,
        }
        cross_latent = detect_cross_market_latent(_tmp_for_latent, cfg)
        if cross_latent:
            print(f"    💎 クロスマーケット潜伏銘柄: {len(cross_latent)}件")
            for cl in cross_latent[:3]:
                print(f"      [{cl['ticker']}] "
                      f"ind={cl['independence_score']:.0f}pt "
                      f"conf={cl['confidence']}")
    except Exception as _cle:
        print(f"    ⚠️  潜伏銘柄検出エラー（スキップ）: {str(_cle)[:60]}")

    result = {
        "lagged_correlations":    lagged_corrs,
        "lead_adjustments":       lead_adj,
        "rotation_phases":        rotation,
        "summary_text":           "\n".join(lines),
        "timestamp":              datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "politician_adjustments": {},
        "cross_size":             cross_size_result,       # Wave 3 - Size Ph3
        "size_decay_bridge":      _bridge_size_decay,      # Wave 7: Tier別有効シグナルマップ
        "independence_bridge":    _bridge_independence,    # Wave 7: 独立性スコアマップ
        "cross_market_latent":    cross_latent,            # Wave 3 - Independence Ph1/Ph2
    }

    _save_cache(cfg, result)
    print("    ✅ JP×US横断分析完了")
    return result
