# ==============================================================
# sector_rotation_detector.py  v1.0  (Idea2-B)
# 業種ローテーション 4象限分類 & 象限変化シグナル検出
# ==============================================================
# 【役割】
#   score_history.json の時系列スコアを使い、
#   JP 33業種を「長期平均 vs 直近モメンタム」の2軸で
#   以下の4象限に分類する。
#
#              モメンタム正（上昇トレンド）
#                      |
#       rising          |         peak
#   （低値×上昇中）      |     （高値×上昇継続）
#   ─────────────────────────────────────────  長期平均
#       trough          |         falling
#   （低値×下落中）      |     （高値→下落中）
#                      |
#              モメンタム負（下落トレンド）
#
#   象限変化をシグナルとして検出し、
#   signal_aggregator._apply_rotation_boost() が以下のブーストを加算する：
#     rising  → +2.0 pt（底値からの反転 → 積極的買い）
#     peak    → -1.0 pt（高値圏 → 過熱注意）
#     trough  → +1.0 pt（底値圏 → 底打ち期待）
#     falling →  0.0 pt（下落中 → 待ち）
#
# 【算出ロジック】
#   1. score_history.json から業種ごとの日次スコア系列を抽出
#   2. 直近 AVG_WINDOW_DAYS（84日=12週）の平均スコアを「長期平均」とする
#   3. 直近 TREND_WINDOW_DAYS（28日=4週）の線形回帰傾きを「モメンタム」とする
#   4. 2軸で象限を決定し、前回キャッシュと比較して象限変化を検出する
#
# 【出力スキーマ（results["sector_rotation"]）】
#   {
#     "quadrants": {
#       "1001": "trough",   # 象限ラベル: rising/peak/falling/trough
#       "1016": "rising",
#       ...
#     },
#     "sector_stats": {
#       "1001": {
#         "current_score":   float,
#         "avg_score":       float,
#         "above_avg":       bool,
#         "momentum":        float,  # 傾き（pt/day）
#         "momentum_label":  str,    # "↑上昇" / "↓下落" / "→横ばい"
#         "quadrant":        str,
#         "boost_pt":        float,  # signal_aggregator への加算量
#       }, ...
#     },
#     "changes": [
#       {
#         "code":       str,    # 業種コード
#         "name":       str,    # 業種名
#         "from":       str,    # 前回象限
#         "to":         str,    # 今回象限
#         "signal":     str,    # 変化の意味（例: "trough_to_rising"）
#         "priority":   str,    # "high" / "medium" / "low"
#       }, ...
#     ],
#     "summary": {
#       "rising_count":  int,
#       "peak_count":    int,
#       "falling_count": int,
#       "trough_count":  int,
#       "change_count":  int,
#       "insufficient_data": bool,
#     },
#     "cached":    bool,
#     "timestamp": str,
#   }
#
# 【signal_aggregator との連携】
#   signal_aggregator.py に追加する _apply_rotation_boost() は
#   results["sector_rotation"]["quadrants"] を参照する。
#   ★ sector_rotation_detector は WEEKLY_MODULES の signal_aggregator より
#      必ず前に実行される必要がある（run_all_tasks() 側で保証）。
#
# 【生成ファイル】
#   CACHE_DIR/sector_rotation_cache.json  TTL: 7日, 週次更新
#
# 【入力（実コード確認済み）】
#   ・score_history.json（_load_score_history(cfg) が読む。候補パスは
#     DATA_DIR/score_history.json → DATA_DIR.parent/insights/score_history.json →
#     相対パス固定の stock_data/data/score_history.json →
#     stock_data/insights/score_history.json の順）
#   ・CACHE_DIR/sector_rotation_cache.json（前回キャッシュ。TTL内なら再計算せず返す。
#     TTL切れでも「前回象限」だけは変化検出のために読み直す）
#
# 【依存】
#   config.py（DATA_DIR / CACHE_DIR / JAPAN_SECTORS）
#   score_history.json（_append_score_history が日次蓄積）
#   ★ numpy 不使用・追加ライブラリなし
#
# 【消費者】
#   ・signal_aggregator.py:801 _apply_rotation_boost() — quadrants を読み、
#     rising/peak/trough/falling を ±ptのブーストに変換して signal_score に加算する。
#     これが本モジュールの正式なスコア反映経路（【役割】節に記載の設計どおり）。
#   ・ignition_detector.py:210 build_ignition_bundle() — results["sector_rotation"] を
#     bundle にそのままコピーする。ignition_detector.py:291 は
#     bundle["sector_rotation"]["sectors"][code]["rotation_score"] を参照しているが、
#     本モジュールの実際の出力キーは quadrants / sector_stats / changes / summary であり
#     "sectors" キーも "rotation_score" フィールドも存在しない（producer/consumerキー不一致の疑い。
#     ignition_detector.py 側は未修正）。
#
# 【他3モジュールとの違い】
#   ・vs jp_us_sector_bridge.py: bridge にも detect_rotation_phase() という
#     4象限判定があるが、本モジュールとは窓幅（8週 vs 84日/28日）・判定基準
#     （固定閾値50 vs 動的平均比較）・出力ラベル（LEADING/WEAKENING/LAGGING/IMPROVING vs
#     rising/peak/falling/trough）が異なる別実装。signal_aggregator が実際にスコアへ
#     反映するのは本モジュールの results["sector_rotation"] のみ（意図的な非統合）。
#   ・vs jp_us_sector_correlation.py: 本モジュールはJP単独の時系列トレンド判定であり、
#     US側データとの相関計算は行わない。
#   ・vs us_sector_bridge_enhanced.py: 本モジュールはscore_historyのみに基づく統計判定。
#     USの決算イベントは参照しない。
#
# 【既知の状態】
#   象限分類は出力されるが、象限「変化」の検出は0件が続いている。
#   score_history の蓄積不足により、前回キャッシュとの比較で有意な変化が
#   検出しにくいと疑われる。J-04 / U-10 の扱いに準じ、score_history の
#   蓄積待ちとする。
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import finance_logic

# ==============================================================
# BLOCK 0: 定数
# ==============================================================

# 象限分類パラメータ
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_SECTOR_ROTATION_DETECTOR_AVG_WINDOW_DAYS': 'AVG_WINDOW_DAYS',
    'B4_SECTOR_ROTATION_DETECTOR_TREND_WINDOW_DAYS': 'TREND_WINDOW_DAYS',
    'B4_SECTOR_ROTATION_DETECTOR_CACHE_TTL_DAYS': 'CACHE_TTL_DAYS',
    'B6_SECTOR_ROTATION_DETECTOR_MOMENTUM_DEADZONE': 'MOMENTUM_DEADZONE',
    'B6_SECTOR_ROTATION_DETECTOR_MIN_DATA_POINTS': 'MIN_DATA_POINTS',
}

AVG_WINDOW_DAYS   = 84    # 長期平均の計算窓（12週 = 3ヶ月）
TREND_WINDOW_DAYS = 28    # モメンタム計算窓（4週）
MOMENTUM_DEADZONE = 0.02  # ±この傾き以内は「横ばい」（横ばいは直前象限を保持）
MIN_DATA_POINTS   = 8     # 最低必要データ点数

# 象限 → signal_aggregator への加算ブースト値（参照用。実際の加算はsignal_aggregator側）
QUADRANT_BOOST: dict[str, float] = {
    "rising":  +2.0,
    "peak":    -1.0,
    "trough":  +1.0,
    "falling":  0.0,
}

# 象限変化のシグナル優先度定義
CHANGE_PRIORITY: dict[str, str] = {
    "trough_to_rising":  "high",    # 底値→上昇: 最も重要な買いシグナル
    "peak_to_falling":   "high",    # 高値→下落: 最も重要な売りシグナル
    "falling_to_trough": "medium",  # 下落→底値: 底打ち候補
    "rising_to_peak":    "medium",  # 上昇→高値: 高値圏到達
    "trough_to_falling": "low",     # 底値→さらに下落
    "peak_to_rising":    "low",     # 高値→再加速（まれ）
    "rising_to_trough":  "low",     # 上昇→下落転換
    "falling_to_rising": "low",     # 下落→反転（V字）
}

CACHE_FILENAME = "sector_rotation_cache.json"
CACHE_TTL_DAYS = 7


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / CACHE_FILENAME


def _load_cache(cfg) -> Optional[dict]:
    """TTL 以内のキャッシュがあれば返す。なければ None。"""
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = data.get("cached_at", "")
        if cached_at:
            age = (datetime.now() - datetime.fromisoformat(cached_at)).days
            if age < CACHE_TTL_DAYS:
                return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    try:
        data["cached_at"] = datetime.now().isoformat()
        p = _cache_path(cfg)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [rotation] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: データ読み込み
# ==============================================================

def _load_score_history(cfg) -> dict:
    """
    score_history.json を読み込む。
    形式: {"YYYY-MM-DD": {"1001": {"score": float, "risk": str}, ...}}
    保存場所: DATA_DIR/score_history.json（主）
              DRIVE_BASE/insights/score_history.json（副）
    """
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "score_history.json",
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")).parent
        / "insights" / "score_history.json",
        Path("stock_data/data/score_history.json"),
        Path("stock_data/insights/score_history.json"),
    ]
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data:
                    return data
            except Exception:
                pass
    return {}


# ==============================================================
# BLOCK 3: スコア系列構築
# ==============================================================

def _build_score_series(score_history: dict) -> dict[str, list[tuple[str, float]]]:
    """
    score_history から業種ごとの日付付きスコア系列を構築する。

    Returns:
        {sector_code: [("YYYY-MM-DD", score), ...]}  ← 日付昇順
    """
    series: dict[str, list[tuple[str, float]]] = {}

    for date_str in sorted(score_history.keys()):
        day_data = score_history[date_str]
        if not isinstance(day_data, dict):
            continue
        for code, entry in day_data.items():
            if not isinstance(code, str):
                continue
            # entry は {"score": float, "risk": str} または float
            if isinstance(entry, dict):
                score = entry.get("score") or entry.get("composite_score")
            else:
                try:
                    score = float(entry)
                except (TypeError, ValueError):
                    score = None
            if score is not None:
                series.setdefault(code, []).append((date_str, float(score)))

    return series


# ==============================================================
# BLOCK 4: 統計計算
# ==============================================================

def _calc_slope(values: list[float]) -> float:
    """共通実装は finance_logic.linear_slope（Batch 10）。"""
    return finance_logic.linear_slope(values, min_n=3, round_digits=6)


def _calc_stats(
    series: list[tuple[str, float]],
    as_of_days_ago: int = 0,
) -> Optional[dict]:
    """
    1業種のスコア系列から象限分類に必要な統計を計算する。

    Args:
        series:         [("YYYY-MM-DD", score)] 昇順リスト
        as_of_days_ago: 何日前時点で計算するか（変化検出時に使用）

    Returns:
        {
          "current_score": float,
          "avg_score":     float,
          "above_avg":     bool,
          "momentum":      float,   # 傾き pt/day
          "momentum_dir":  str,     # "up" / "down" / "flat"
          "data_points":   int,
        }
        データ不足の場合は None
    """
    if not series:
        return None

    # as_of_days_ago 分だけ末尾を削る
    effective = series if as_of_days_ago == 0 else series[:-as_of_days_ago] or series

    if len(effective) < MIN_DATA_POINTS:
        return None

    # --- 長期平均（直近 AVG_WINDOW_DAYS 日） ---
    # series は日付昇順。直近 AVG_WINDOW_DAYS 日分を抽出
    cutoff_date = (
        datetime.now() - timedelta(days=AVG_WINDOW_DAYS)
    ).strftime("%Y-%m-%d")
    avg_window_vals = [
        s for d, s in effective if d >= cutoff_date
    ]
    if len(avg_window_vals) < MIN_DATA_POINTS:
        # データが少ない場合は全期間を使う
        avg_window_vals = [s for _, s in effective]

    avg_score = sum(avg_window_vals) / len(avg_window_vals)

    # --- 現在スコア（最新値） ---
    current_score = effective[-1][1]

    # --- モメンタム（直近 TREND_WINDOW_DAYS 日の傾き） ---
    trend_cutoff = (
        datetime.now() - timedelta(days=TREND_WINDOW_DAYS)
    ).strftime("%Y-%m-%d")
    trend_vals = [s for d, s in effective if d >= trend_cutoff]
    if len(trend_vals) < 3:
        trend_vals = [s for _, s in effective[-TREND_WINDOW_DAYS:]]

    momentum = _calc_slope(trend_vals)

    # デッドゾーン判定
    if abs(momentum) <= MOMENTUM_DEADZONE:
        momentum_dir = "flat"
    elif momentum > 0:
        momentum_dir = "up"
    else:
        momentum_dir = "down"

    return {
        "current_score": round(current_score, 2),
        "avg_score":     round(avg_score, 2),
        "above_avg":     current_score >= avg_score,
        "momentum":      momentum,
        "momentum_dir":  momentum_dir,
        "data_points":   len(effective),
    }


# ==============================================================
# BLOCK 5: 象限分類
# ==============================================================

def _classify_quadrant(
    stats: dict,
    prev_quadrant: Optional[str] = None,
) -> str:
    """
    統計値から4象限を決定する。

    デッドゾーン（momentum_dir == "flat"）の場合は
    前回の象限を継続することで細かい振れを排除する。

    Quadrant Logic:
        above_avg=True,  trend=up   → peak
        above_avg=True,  trend=down → falling
        above_avg=False, trend=up   → rising
        above_avg=False, trend=down → trough
        trend=flat → 前回象限を維持（なければ above_avg で判定）
    """
    above = stats["above_avg"]
    mdir  = stats["momentum_dir"]

    if mdir == "flat":
        # 横ばい → 前回象限を維持
        if prev_quadrant and prev_quadrant in QUADRANT_BOOST:
            return prev_quadrant
        # 前回なし → level のみで判定
        return "peak" if above else "trough"

    if above and mdir == "up":
        return "peak"
    if above and mdir == "down":
        return "falling"
    if not above and mdir == "up":
        return "rising"
    # not above and mdir == "down"
    return "trough"


def _momentum_label(momentum: float, direction: str) -> str:
    """モメンタム値を日本語ラベルに変換する。"""
    if direction == "flat":
        return "→ 横ばい"
    abs_m = abs(momentum)
    if abs_m >= 1.0:
        strength = "急"
    elif abs_m >= 0.3:
        strength = "緩やか"
    else:
        strength = "微"
    arrow = "↑" if direction == "up" else "↓"
    return f"{arrow} {strength}{'上昇' if direction == 'up' else '下落'}"


def _quadrant_label_ja(quadrant: str) -> str:
    """象限ラベルの日本語表記。"""
    return {
        "rising":  "🟢 上昇中（底値→反転）",
        "peak":    "🟡 ピーク圏（高値継続）",
        "falling": "🔴 下落中（高値→反転）",
        "trough":  "⚪ 底値圏（低値継続）",
    }.get(quadrant, quadrant)


# ==============================================================
# BLOCK 6: 象限変化検出
# ==============================================================

def _detect_changes(
    current_quadrants:  dict[str, str],
    previous_quadrants: dict[str, str],
    sector_names:       dict[str, str],
) -> list[dict]:
    """
    前回→今回の象限変化を検出し、変化リストを返す。

    Returns:
        [
          {
            "code":     str,
            "name":     str,
            "from":     str,
            "to":       str,
            "signal":   str,
            "priority": str,
          }, ...
        ]
    """
    changes = []
    for code, new_q in current_quadrants.items():
        old_q = previous_quadrants.get(code)
        if old_q is None or old_q == new_q:
            continue
        signal = f"{old_q}_to_{new_q}"
        priority = CHANGE_PRIORITY.get(signal, "low")
        changes.append({
            "code":     code,
            "name":     sector_names.get(code, code),
            "from":     old_q,
            "to":       new_q,
            "signal":   signal,
            "priority": priority,
        })

    # 優先度降順（high > medium > low）でソート
    prio_order = {"high": 0, "medium": 1, "low": 2}
    changes.sort(key=lambda x: (prio_order.get(x["priority"], 9), x["code"]))
    return changes


# ==============================================================
# BLOCK 7: メイン実行フロー
# ==============================================================

def run_sector_rotation_detector(cfg) -> dict:
    """
    stock_analyzer_main.py の WEEKLY_MODULES から呼ばれるエントリポイント。
    ★ signal_aggregator より前に実行すること（依存関係）。

    処理フロー:
      1. キャッシュ有効 → キャッシュ返却
      2. score_history.json を読み込み業種別スコア系列を構築
      3. 各業種の長期平均・モメンタムを算出
      4. 4象限に分類
      5. 前回キャッシュと比較して象限変化を検出
      6. キャッシュ保存して結果を返す

    Returns:
        {
          "quadrants":    {code: quadrant_str},
          "sector_stats": {code: stats_dict},
          "changes":      [change_dict, ...],
          "summary":      stats_summary,
          "cached":       bool,
          "timestamp":    str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🔄 業種ローテーション検出（Idea2-B）実行中...")

    # ── キャッシュ確認 ───────────────────────────────────────────
    cached = _load_cache(cfg)
    if cached:
        summary = cached.get("summary", {})
        n_changes = summary.get("change_count", 0)
        rising  = summary.get("rising_count",  0)
        peak    = summary.get("peak_count",    0)
        falling = summary.get("falling_count", 0)
        trough  = summary.get("trough_count",  0)
        print(f"    ✅ キャッシュ使用: "
              f"上昇{rising} ピーク{peak} 下落{falling} 底値{trough} "
              f"/ 変化{n_changes}件")
        cached["cached"] = True
        return cached

    # ── 1. score_history 読み込み ────────────────────────────────
    score_history = _load_score_history(cfg)
    if not score_history:
        print("    ⚠️  score_history.json なし → スキップ")
        return _empty_result(insufficient_data=True)

    print(f"    📋 履歴日数: {len(score_history)}日分")

    # ── 2. 業種別スコア系列構築 ──────────────────────────────────
    series_map = _build_score_series(score_history)
    sector_codes = sorted(series_map.keys())

    if not sector_codes:
        print("    ⚠️  業種データなし → スキップ")
        return _empty_result(insufficient_data=True)

    # ── 3. 前回象限（変化検出用）の読み込み ────────────────────
    # キャッシュが期限切れでも前回象限だけは再利用
    prev_quadrants: dict[str, str] = {}
    try:
        p = _cache_path(cfg)
        if p.exists():
            old_data = json.loads(p.read_text(encoding="utf-8"))
            prev_quadrants = old_data.get("quadrants", {})
    except Exception:
        pass

    # ── 4. 各業種の統計計算 & 象限分類 ─────────────────────────
    sector_names = {
        code: info.get("name_ja", code)
        for code, info in getattr(cfg, "JAPAN_SECTORS", {}).items()
    }

    quadrants:    dict[str, str]  = {}
    sector_stats: dict[str, dict] = {}
    skipped = 0

    for code in sector_codes:
        series = series_map[code]
        stats  = _calc_stats(series)
        if stats is None:
            skipped += 1
            continue

        prev_q   = prev_quadrants.get(code)
        quadrant = _classify_quadrant(stats, prev_quadrant=prev_q)

        quadrants[code] = quadrant
        sector_stats[code] = {
            "current_score":  stats["current_score"],
            "avg_score":      stats["avg_score"],
            "above_avg":      stats["above_avg"],
            "momentum":       stats["momentum"],
            "momentum_label": _momentum_label(stats["momentum"], stats["momentum_dir"]),
            "quadrant":       quadrant,
            "quadrant_ja":    _quadrant_label_ja(quadrant),
            "boost_pt":       QUADRANT_BOOST.get(quadrant, 0.0),
            "name_ja":        sector_names.get(code, code),
            "data_points":    stats["data_points"],
        }

    if skipped:
        print(f"    ℹ️  データ不足でスキップ: {skipped}業種")

    # ── 5. 象限変化検出 ─────────────────────────────────────────
    changes = _detect_changes(quadrants, prev_quadrants, sector_names)

    # ── 6. サマリー集計 ─────────────────────────────────────────
    from collections import Counter
    q_counts = Counter(quadrants.values())
    summary = {
        "rising_count":      q_counts.get("rising",  0),
        "peak_count":        q_counts.get("peak",    0),
        "falling_count":     q_counts.get("falling", 0),
        "trough_count":      q_counts.get("trough",  0),
        "change_count":      len(changes),
        "high_priority_changes": sum(
            1 for c in changes if c["priority"] == "high"
        ),
        "total_classified":  len(quadrants),
        "insufficient_data": len(quadrants) == 0,
    }

    # ── コンソール出力 ───────────────────────────────────────────
    print(
        f"    📊 象限分類: "
        f"上昇中{summary['rising_count']} / "
        f"ピーク{summary['peak_count']} / "
        f"下落中{summary['falling_count']} / "
        f"底値圏{summary['trough_count']}"
    )
    if changes:
        print(f"    🔔 象限変化: {len(changes)}件"
              f"（高優先度: {summary['high_priority_changes']}件）")
        for ch in changes[:5]:
            print(f"       [{ch['priority'].upper():6s}] "
                  f"{ch['name']:12s}: "
                  f"{ch['from']:7s} → {ch['to']}")

    # 上昇中トップ3 / 下落中ワースト3 を表示
    rising_codes  = [c for c, q in quadrants.items() if q == "rising"]
    falling_codes = [c for c, q in quadrants.items() if q == "falling"]
    if rising_codes:
        names = [sector_stats[c]["name_ja"] for c in rising_codes[:3]]
        print(f"    🟢 上昇中TOP: {', '.join(names)}")
    if falling_codes:
        names = [sector_stats[c]["name_ja"] for c in falling_codes[:3]]
        print(f"    🔴 下落中:   {', '.join(names)}")

    # ── 7. キャッシュ保存 ────────────────────────────────────────
    result = {
        "quadrants":    quadrants,
        "sector_stats": sector_stats,
        "changes":      changes,
        "summary":      summary,
        "cached":       False,
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_cache(cfg, result)

    print(f"  ✅ ローテーション検出完了: "
          f"{len(quadrants)}業種分類 / 象限変化{len(changes)}件")
    return result


def _empty_result(insufficient_data: bool = False) -> dict:
    return {
        "quadrants":    {},
        "sector_stats": {},
        "changes":      [],
        "summary": {
            "rising_count": 0, "peak_count": 0,
            "falling_count": 0, "trough_count": 0,
            "change_count": 0, "high_priority_changes": 0,
            "total_classified": 0,
            "insufficient_data": insufficient_data,
        },
        "cached":    False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 8: signal_aggregator から呼ばれるアクセサ関数
# ==============================================================

def get_rotation_boost(sector_code: str, results: dict) -> float:
    """
    signal_aggregator._apply_rotation_boost() から呼ばれる補助関数。
    results["sector_rotation"]["quadrants"] を参照してブースト値を返す。

    Args:
        sector_code: JP 業種コード（例: "1016"）
        results:     run_all_tasks() の results dict

    Returns:
        float: ブースト値（rising: +2.0, peak: -1.0, trough: +1.0, falling: 0.0）
    """
    try:
        rotation = results.get("sector_rotation", {})
        if not rotation or rotation.get("summary", {}).get("insufficient_data", True):
            return 0.0
        quadrant = rotation.get("quadrants", {}).get(sector_code)
        return QUADRANT_BOOST.get(quadrant, 0.0)
    except Exception:
        return 0.0


# ==============================================================
# BLOCK 9: 単体テスト用エントリポイント
# ==============================================================

if __name__ == "__main__":
    import importlib.util
    import sys

    cfg = None
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            if spec and spec.loader:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    if cfg is None:
        print("ERROR: config.py が読み込めません")
        sys.exit(1)

    result = run_sector_rotation_detector(cfg)
    summary = result.get("summary", {})

    print("\n--- 象限サマリー ---")
    print(f"上昇中 (rising) : {summary.get('rising_count',  0)}業種  [+2pt ブースト]")
    print(f"ピーク  (peak)  : {summary.get('peak_count',    0)}業種  [-1pt ペナルティ]")
    print(f"下落中 (falling): {summary.get('falling_count', 0)}業種  [±0pt]")
    print(f"底値圏 (trough) : {summary.get('trough_count',  0)}業種  [+1pt ブースト]")
    print(f"象限変化        : {summary.get('change_count',  0)}件")

    changes = result.get("changes", [])
    if changes:
        print("\n--- 象限変化（高優先度） ---")
        for ch in [c for c in changes if c["priority"] == "high"]:
            print(f"  ⭐ {ch['name']:12s}: {ch['from']:7s} → {ch['to']}"
                  f"  [{ch['signal']}]")

    print("\n--- 業種別詳細（上位10） ---")
    stats = result.get("sector_stats", {})
    for code, st in list(stats.items())[:10]:
        print(
            f"  {st.get('name_ja', code):12s} | "
            f"現在:{st['current_score']:5.1f} "
            f"平均:{st['avg_score']:5.1f} "
            f"{st['momentum_label']:12s} | "
            f"{st['quadrant_ja']}"
        )
