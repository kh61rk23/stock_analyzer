# ==============================================================
# screener_sector_heatmap.py  v1.0
# screener通過銘柄のセクター集中度ヒートマップ
# ==============================================================
# 【役割】
#   「今週のscreener上位銘柄はどの業種に集中しているか」を
#   screener_history.json から自動集計し、業種ローテーションの
#   パターンを可視化する。
#
#   現在できていなかった3項目:
#   (c) screener通過銘柄のセクター集計
#       → 過去データと比較して業種ローテーションを検出
#
# 【分析内容】
#   A. 週次セクター集中度
#      「今週は電気機器と自動車からの通過が多い」を数値化
#
#   B. セクターモメンタム（3ヶ月間の通過頻度推移）
#      「直近3週: 1016が増加中 → 市場の注目が集まっている」
#
#   C. ローテーション検出
#      先週まで多かった業種 vs 今週新たに増えた業種
#      → 「金融 → 製造業へのローテーション」を自動検出
#
#   D. 長期スコアトレンド（score_history活用）
#      同業種のcomposite_scoreが3ヶ月で何点上昇/下降したか
#      → これが(b)の「3ヶ月前との比較による長期ローテーション検出」
#
# 【出力スキーマ】
#   {
#     "current_week": {sector_code: count},   # 今週の通過数
#     "trend_3w":     {sector_code: trend},   # 3週トレンド（増加/減少）
#     "rotation_in":  [sector_code, ...],     # 新規増加業種
#     "rotation_out": [sector_code, ...],     # 減少業種
#     "score_trend_3m": {sector_code: delta}, # 3ヶ月スコア変化
#     "heatmap_md":   str,                    # Markdown表
#     "timestamp":    str,
#   }
#
# 【依存】screener_history.json / score_history.json / config.py
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

HEATMAP_WEEKS     = 8    # 過去N週分のヒートマップを生成
SCORE_TREND_WEEKS = 13   # 3ヶ月 = 約13週
ROTATION_DELTA    = 2    # N件以上増加 = ローテーションin
CACHE_HOURS       = 24
CACHE_FILENAME    = "screener_sector_heatmap_cache.json"


# ==============================================================
# BLOCK 1: データ読み込み
# ==============================================================

def _load_screener_history(cfg) -> dict:
    for p in [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "screener_history.json",
        Path("stock_data/data/screener_history.json"),
    ]:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _load_score_history(cfg) -> dict:
    for p in [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
        / "insights" / "score_history.json",
        Path("stock_data/insights/score_history.json"),
    ]:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


# ==============================================================
# BLOCK 2: 週次セクター集計
# ==============================================================

def _date_to_week(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%Y-W%V")
    except ValueError:
        return ""


def calc_weekly_sector_counts(
    screener_history: dict,
    lookback_weeks:   int = HEATMAP_WEEKS,
) -> dict:
    """
    週ごとのセクター通過数を集計する。

    Returns:
        {"YYYY-Wxx": {sector_code: count, ...}, ...}
    """
    today   = datetime.now().date()
    cutoff  = today - timedelta(weeks=lookback_weeks)
    weekly  = {}

    for date_str, candidates in screener_history.items():
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dt < cutoff:
            continue
        week_key = _date_to_week(date_str)
        if not week_key:
            continue

        weekly.setdefault(week_key, {})
        for c in (candidates if isinstance(candidates, list) else []):
            if not isinstance(c, dict) or not c.get("pass_filters"):
                continue
            code = c.get("sector_code", "")
            if code:
                weekly[week_key][code] = weekly[week_key].get(code, 0) + 1

    return dict(sorted(weekly.items()))


# ==============================================================
# BLOCK 3: ローテーション検出
# ==============================================================

def detect_rotation(weekly_counts: dict) -> dict:
    """
    直近2週の変化からローテーションを検出する。

    Returns:
        {
          "rotation_in":  [sector_code, ...],  # 新規増加業種
          "rotation_out": [sector_code, ...],  # 減少業種
          "stable":       [sector_code, ...],  # 安定業種
          "trend_3w":     {sector_code: "up"/"down"/"stable"},
        }
    """
    weeks = sorted(weekly_counts.keys())
    if len(weeks) < 2:
        return {"rotation_in": [], "rotation_out": [], "stable": [], "trend_3w": {}}

    latest  = weekly_counts[weeks[-1]]
    prev    = weekly_counts[weeks[-2]]

    all_codes = set(list(latest.keys()) + list(prev.keys()))

    rotation_in  = []
    rotation_out = []
    stable       = []

    for code in all_codes:
        curr_count = latest.get(code, 0)
        prev_count = prev.get(code, 0)
        delta      = curr_count - prev_count

        if delta >= ROTATION_DELTA:
            rotation_in.append((code, delta))
        elif delta <= -ROTATION_DELTA:
            rotation_out.append((code, delta))
        elif curr_count > 0:
            stable.append(code)

    # 3週トレンド（直近3週の傾き）
    trend_3w = {}
    if len(weeks) >= 3:
        recent_3 = weeks[-3:]
        for code in all_codes:
            counts = [weekly_counts.get(w, {}).get(code, 0) for w in recent_3]
            slope  = (counts[-1] - counts[0]) / 2   # 単純な傾き
            if slope > 0.5:
                trend_3w[code] = "up"
            elif slope < -0.5:
                trend_3w[code] = "down"
            else:
                trend_3w[code] = "stable"

    return {
        "rotation_in":  [c for c, _ in sorted(rotation_in,  key=lambda x: -x[1])],
        "rotation_out": [c for c, _ in sorted(rotation_out, key=lambda x:  x[1])],
        "stable":       stable,
        "trend_3w":     trend_3w,
    }


# ==============================================================
# BLOCK 4: 3ヶ月スコアトレンド（score_history活用）
# ==============================================================

def calc_score_trend_3m(
    score_history:  dict,
    lookback_weeks: int = SCORE_TREND_WEEKS,
) -> dict:
    """
    score_history から業種ごとの3ヶ月スコア変化を計算する。
    ← これが「(b) 3ヶ月前との比較による長期ローテーション検出」の実装

    Returns:
        {sector_code: {"delta_3m": float, "trend": str, "current": float, "3m_ago": float}}
    """
    if not score_history:
        return {}

    dates = sorted(score_history.keys())
    today = datetime.now()

    # 現在のスコア（最新日）
    current_date = dates[-1]
    current_data = score_history.get(current_date, {})

    # 3ヶ月前のスコア
    target_3m   = (today - timedelta(weeks=lookback_weeks)).strftime("%Y-%m-%d")
    past_date   = next(
        (d for d in reversed(dates) if d <= target_3m),
        dates[0]
    )
    past_data   = score_history.get(past_date, {})

    results = {}
    for code in set(list(current_data.keys()) + list(past_data.keys())):
        curr = current_data.get(code)
        past = past_data.get(code)

        # score_history は {code: {"score": float}} または {code: float} の両形式に対応
        def _extract(v):
            if isinstance(v, dict):
                return v.get("score") or v.get("composite_score")
            return v

        curr_score = _extract(curr)
        past_score = _extract(past)

        if curr_score is None or past_score is None:
            continue

        delta = round(float(curr_score) - float(past_score), 2)

        if delta > 5:
            trend = "🔺 上昇加速中"
        elif delta > 2:
            trend = "↗ 緩やかに上昇"
        elif delta > -2:
            trend = "→ 安定"
        elif delta > -5:
            trend = "↘ 緩やかに下落"
        else:
            trend = "🔻 下落加速中"

        results[code] = {
            "delta_3m":    delta,
            "trend":       trend,
            "current":     round(float(curr_score), 1),
            "3m_ago":      round(float(past_score), 1),
            "period":      f"{past_date} → {current_date}",
        }

    return results


# ==============================================================
# BLOCK 5: Markdown ヒートマップ生成
# ==============================================================

def format_heatmap_md(
    weekly_counts:    dict,
    rotation:         dict,
    score_trend_3m:   dict,
    sector_names:     dict,
    top_n_weeks:      int = 6,
) -> str:
    """週次ヒートマップとローテーション分析を Markdown で出力する。"""
    weeks = sorted(weekly_counts.keys())[-top_n_weeks:]
    if not weeks:
        return "データ蓄積中..."

    # 全登場業種
    all_codes = sorted(set(c for w in weeks for c in weekly_counts.get(w, {})))
    if not all_codes:
        return "screener通過銘柄なし"

    lines = [
        "## 📊 screenerセクターヒートマップ",
        "",
        "### 週次通過数（業種別）",
        "",
    ]

    # ヘッダー行
    header = "| 業種 | " + " | ".join(w[-4:] for w in weeks) + " | 3ヶ月Δ |"
    sep    = "|" + "---|" * (len(weeks) + 2)
    lines += [header, sep]

    # データ行
    for code in all_codes:
        name   = sector_names.get(code, code)[:8]
        counts = [str(weekly_counts.get(w, {}).get(code, 0)) for w in weeks]
        trend  = score_trend_3m.get(code, {})
        delta  = trend.get("delta_3m")
        delta_s = f"{delta:+.1f}" if delta is not None else "—"
        t3w    = rotation.get("trend_3w", {}).get(code, "stable")
        t3w_icon = {"up": "↑", "down": "↓", "stable": "→"}.get(t3w, "")
        lines.append(f"| {t3w_icon}{name} | " + " | ".join(counts) + f" | {delta_s} |")

    lines.append("")

    # ローテーション
    rot_in  = rotation.get("rotation_in",  [])
    rot_out = rotation.get("rotation_out", [])

    if rot_in:
        names_in = " / ".join(f"**{sector_names.get(c,c)}**" for c in rot_in[:4])
        lines.append(f"📈 **ローテーション流入**: {names_in}")
    if rot_out:
        names_out = " / ".join(sector_names.get(c, c) for c in rot_out[:4])
        lines.append(f"📉 **ローテーション流出**: {names_out}")

    lines.append("")

    # 3ヶ月スコアトレンド
    if score_trend_3m:
        rising  = [(c, d) for c, d in score_trend_3m.items() if d["delta_3m"] > 5]
        falling = [(c, d) for c, d in score_trend_3m.items() if d["delta_3m"] < -5]
        rising  = sorted(rising,  key=lambda x: -x[1]["delta_3m"])[:4]
        falling = sorted(falling, key=lambda x:  x[1]["delta_3m"])[:3]

        if rising or falling:
            lines += ["### 📈 3ヶ月スコアトレンド（長期ローテーション）", ""]
            for code, d in rising:
                lines.append(f"  🔺 {sector_names.get(code,code)}: "
                              f"{d['3m_ago']:.0f}→{d['current']:.0f}点 "
                              f"({d['delta_3m']:+.1f}) {d['trend']}")
            for code, d in falling:
                lines.append(f"  🔻 {sector_names.get(code,code)}: "
                              f"{d['3m_ago']:.0f}→{d['current']:.0f}点 "
                              f"({d['delta_3m']:+.1f}) {d['trend']}")

    return "\n".join(lines)


# ==============================================================
# BLOCK 6: キャッシュ管理
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
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_screener_sector_heatmap(cfg) -> dict:
    """
    screenerセクターヒートマップを生成する。

    stock_analyzer_main.py から週次で呼び出す:
        _ssh = _load_local_module("screener_sector_heatmap", ...)
        ssh_result = _ssh.run_screener_sector_heatmap(cfg)
        results["screener_heatmap"] = ssh_result

    Returns:
        {
          "current_week":   {sector_code: count},
          "weekly_counts":  dict,
          "rotation":       dict,
          "score_trend_3m": dict,
          "heatmap_md":     str,
          "rotation_in":    list,
          "rotation_out":   list,
          "timestamp":      str,
        }
    """
    print("\n  📊 screenerセクターヒートマップ生成...")

    cached = _load_cache(cfg)
    if cached:
        print(f"    ✅ キャッシュ使用")
        return cached

    history       = _load_screener_history(cfg)
    score_history = _load_score_history(cfg)

    if not history:
        print("    ⚠️  screener_history.json なし → スキップ")
        return {"skipped": True, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # 週次集計
    weekly_counts = calc_weekly_sector_counts(history)
    rotation      = detect_rotation(weekly_counts)

    weeks = sorted(weekly_counts.keys())
    current_week  = weekly_counts.get(weeks[-1], {}) if weeks else {}

    # ローテーション表示
    if rotation.get("rotation_in"):
        print(f"    📈 流入: {rotation['rotation_in'][:3]}")
    if rotation.get("rotation_out"):
        print(f"    📉 流出: {rotation['rotation_out'][:3]}")

    # 3ヶ月スコアトレンド（score_historyが蓄積済みの場合のみ）
    score_trend_3m = {}
    if score_history and len(score_history) >= 4:
        score_trend_3m = calc_score_trend_3m(score_history)
        rising_count  = sum(1 for d in score_trend_3m.values() if d.get("delta_3m", 0) > 5)
        falling_count = sum(1 for d in score_trend_3m.values() if d.get("delta_3m", 0) < -5)
        print(f"    📈 3ヶ月上昇業種: {rising_count} / 📉 下落業種: {falling_count}")

    # Markdown生成
    sector_names = {
        code: info.get("name_ja", code)
        for code, info in getattr(cfg, "JAPAN_SECTORS", {}).items()
    }
    heatmap_md = format_heatmap_md(
        weekly_counts, rotation, score_trend_3m, sector_names)

    result = {
        "current_week":   current_week,
        "weekly_counts":  weekly_counts,
        "rotation":       rotation,
        "rotation_in":    rotation.get("rotation_in", []),
        "rotation_out":   rotation.get("rotation_out", []),
        "score_trend_3m": score_trend_3m,
        "heatmap_md":     heatmap_md,
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_cache(cfg, result)
    print(f"    ✅ ヒートマップ生成完了: {len(weekly_counts)}週分")
    return result
