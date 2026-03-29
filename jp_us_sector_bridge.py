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

    result = {
        "lagged_correlations": lagged_corrs,
        "lead_adjustments":    lead_adj,
        "rotation_phases":     rotation,
        "summary_text":        "\n".join(lines),
        "timestamp":           datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_cache(cfg, result)
    print(f"    ✅ JP×US横断分析完了")
    return result
