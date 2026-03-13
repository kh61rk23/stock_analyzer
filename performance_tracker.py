# ==============================================================
# performance_tracker.py  v1.0  (2-3 / 拡張E / E-2)
# Self-Evolution Stage 1: 先週 screener 通過銘柄の実績追跡
# ==============================================================
# 【役割】
#   stock_screener.py が先週選んだ候補銘柄の「その後のリターン」を
#   yfinance で自動集計し、以下を計算する:
#     - 週間リターン（%）: 先週月曜始値 → 今週金曜終値
#     - 勝率: プラスリターン銘柄 / 全銘柄
#     - 平均リターン / メジアン / 標準偏差
#     - 上位3・下位3銘柄の詳細
#
# 【Self-Evolution との関係】
#   screener の予測精度を定量的にフィードバックし、
#   スコアリング閾値・フィルター条件の調整根拠を提供する（拡張E）。
#   週次レポートに「先週の予測精度」セクションとして統合（E-2）。
#
# 【データフロー】
#   ① stock_screener.py が実行されるたびに screener_history.json を更新
#      （stock_analyzer_main.py 側で追記を呼び出す）
#   ② 毎週月曜の weekly_report 実行時に run_performance_tracker() を呼び出す
#   ③ 先週（月〜金）の screener 通過銘柄を screener_history.json から取得
#   ④ yfinance でその週のリターンを集計
#   ⑤ 結果を weekly_report_generator.py の results に注入
#
# 【screener_history.json の形式】
#   {
#     "YYYY-MM-DD": [
#       {
#         "ticker":          "6758.T",
#         "name":            "ソニーグループ",
#         "sector_code":     "1016",
#         "sector_name":     "電気機器",
#         "composite_score": 74.2,
#         "value_score":     68.0,
#         "quality_score":   72.0,
#         "per":             18.4,
#         "pbr":             1.82,
#         "roe":             12.5,
#         "market_cap_b":    150000.0,
#         "avg_volume":      5230000,
#       },
#       ...
#     ],
#     ...
#   }
#   保持期間: 90日分。古いものから自動削除。
#
# 【出力形式（run_performance_tracker の戻り値）】
#   {
#     "tracked_week":     "2026-W10",       # 追跡した週
#     "candidates_n":     12,               # 追跡対象銘柄数
#     "returns": [
#       {"ticker": "6758.T", "name": "ソニーグループ",
#        "return_pct": 3.2, "entry_price": 12800, "exit_price": 13209,
#        "composite_score": 74.2, "sector_name": "電気機器"},
#       ...
#     ],
#     "win_rate":         0.583,   # 勝率（小数）
#     "avg_return":       1.8,     # 平均リターン(%)
#     "median_return":    1.2,     # メジアン(%)
#     "std_return":       3.4,     # 標準偏差(%)
#     "top3":             [...],   # リターン上位3銘柄
#     "bottom3":          [...],   # リターン下位3銘柄
#     "score_return_corr": 0.42,   # composite_score vs リターンの相関係数
#     "summary_text":     str,     # 週次レポート埋め込み用テキスト
#     "skipped":          False,
#     "skip_reason":      "",
#   }
#
# 【依存】yfinance / config.py
# 【呼出】from performance_tracker import run_performance_tracker,
#                                         append_screener_history
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
HISTORY_RETAIN_DAYS = 90    # screener_history.json 保持日数
RETURN_FETCH_DELAY  = 0.25  # yfinance レート制限対策（秒）
MIN_CANDIDATES      = 3     # 追跡対象の最低銘柄数（少なすぎる場合はスキップ）
SCORE_CORR_MIN_N    = 5     # 相関係数を計算する最低データ点数


# ==============================================================
# BLOCK 1: screener_history.json 管理
# ==============================================================

def _history_path(cfg) -> Path:
    """screener_history.json のパスを返す。"""
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "screener_history.json"


def load_screener_history(cfg) -> dict:
    """
    screener_history.json を読み込む。

    Returns:
        {"YYYY-MM-DD": [candidate_dict, ...], ...}
        ファイルが存在しない場合は空辞書 {}
    """
    p = _history_path(cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠️  screener_history 読み込み失敗: {e}")
        return {}


def append_screener_history(cfg, screener_result: dict) -> None:
    """
    今日のスクリーニング通過銘柄を screener_history.json に追記する。

    stock_analyzer_main.py から毎日の screener 実行後に呼び出す。
    既存の同日エントリは上書きする（冪等性を保証）。
    HISTORY_RETAIN_DAYS を超えた古いエントリを自動削除する。

    Args:
        cfg:             config モジュール
        screener_result: run_stock_screener() の戻り値
    """
    today = date.today().strftime("%Y-%m-%d")
    p     = _history_path(cfg)

    try:
        history = load_screener_history(cfg)

        # 通過銘柄のみ抽出してスリム化（ストレージ節約）
        candidates = screener_result.get("candidates", [])
        passed = [
            {
                "ticker":          c.get("ticker", ""),
                "name":            c.get("name", ""),
                "sector_code":     c.get("sector_code", ""),
                "sector_name":     c.get("sector_name", ""),
                "composite_score": c.get("composite_score"),
                "value_score":     c.get("value_score"),
                "quality_score":   c.get("quality_score"),
                "accrual_tier":    c.get("accrual_tier", "N/A"),
                "discovery_score": c.get("discovery_score", 0),
                "per":             c.get("per"),
                "pbr":             c.get("pbr"),
                "roe":             c.get("roe"),
                "market_cap_b":    c.get("market_cap_b"),
                "avg_volume":      c.get("avg_volume"),
            }
            for c in candidates
            if c.get("pass_filters")
        ]

        history[today] = passed

        # 古いエントリを削除（HISTORY_RETAIN_DAYS 超過分）
        cutoff = (date.today() - timedelta(days=HISTORY_RETAIN_DAYS)).strftime("%Y-%m-%d")
        history = {d: v for d, v in history.items() if d >= cutoff}

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  💾 screener_history 更新: {today} → {len(passed)}銘柄 "
              f"（累積{len(history)}日分）")

    except Exception as e:
        print(f"  ⚠️  screener_history 保存失敗: {e}")


# ==============================================================
# BLOCK 2: 先週の screener 通過銘柄を特定
# ==============================================================

def _get_last_week_range(reference: Optional[date] = None) -> tuple[str, str]:
    """
    「先週月曜〜先週金曜」の日付文字列を返す。

    Args:
        reference: 基準日（省略時: 今日）

    Returns:
        (last_monday: str, last_friday: str) 形式 "YYYY-MM-DD"
    """
    today = reference or date.today()
    # 今週月曜 = 今日 - weekday 日数（月=0, 火=1, ...）
    this_monday = today - timedelta(days=today.weekday())
    last_monday  = this_monday - timedelta(weeks=1)
    last_friday  = last_monday + timedelta(days=4)
    return last_monday.strftime("%Y-%m-%d"), last_friday.strftime("%Y-%m-%d")


def get_last_week_candidates(cfg, reference: Optional[date] = None) -> list:
    """
    screener_history.json から先週のスクリーニング通過銘柄を取得する。

    先週（月〜金）に保存されたエントリのうち、
    最もエントリ数が多い日のデータを代表として使用する。
    （月〜金に複数日実行されている場合は合算して重複除去）

    Returns:
        [candidate_dict, ...]  先週の通過銘柄リスト
    """
    history = load_screener_history(cfg)
    if not history:
        return []

    last_mon, last_fri = _get_last_week_range(reference)

    # 先週日付のエントリを収集
    weekly_entries: dict[str, dict] = {}  # ticker → candidate_dict（重複除去）
    for d_str, candidates in history.items():
        if last_mon <= d_str <= last_fri:
            for c in candidates:
                ticker = c.get("ticker", "")
                if ticker and ticker not in weekly_entries:
                    weekly_entries[ticker] = c

    return list(weekly_entries.values())


# ==============================================================
# BLOCK 3: yfinance でリターン取得
# ==============================================================

def _fetch_weekly_return(ticker: str, entry_date: str, exit_date: str) -> Optional[dict]:
    """
    1銘柄の週間リターンを yfinance で取得する。

    エントリ価格: entry_date 週の月曜始値（または最初の取引日始値）
    エグジット価格: exit_date 週の金曜終値（または最後の取引日終値）

    Args:
        ticker:     例 "6758.T"
        entry_date: 先週月曜 "YYYY-MM-DD"
        exit_date:  先週金曜 "YYYY-MM-DD"（バッファ含む）

    Returns:
        {"entry_price": float, "exit_price": float, "return_pct": float}
        取得失敗時は None
    """
    try:
        import yfinance as yf

        # 取得範囲: entry_date の1営業日前〜exit_date の1営業日後（バッファ）
        fetch_start = (datetime.strptime(entry_date, "%Y-%m-%d")
                       - timedelta(days=3)).strftime("%Y-%m-%d")
        fetch_end   = (datetime.strptime(exit_date, "%Y-%m-%d")
                       + timedelta(days=3)).strftime("%Y-%m-%d")

        hist = yf.Ticker(ticker).history(
            start=fetch_start,
            end=fetch_end,
            auto_adjust=True,
        )
        if hist is None or hist.empty:
            return None

        # entry_date 以降の最初の行を entry として使用
        entry_rows = hist[hist.index.strftime("%Y-%m-%d") >= entry_date]
        exit_rows  = hist[hist.index.strftime("%Y-%m-%d") <= exit_date]

        if entry_rows.empty or exit_rows.empty:
            return None

        entry_price = float(entry_rows.iloc[0]["Open"])
        exit_price  = float(exit_rows.iloc[-1]["Close"])

        if entry_price <= 0:
            return None

        return_pct = round((exit_price - entry_price) / entry_price * 100, 2)

        return {
            "entry_price": round(entry_price, 2),
            "exit_price":  round(exit_price, 2),
            "return_pct":  return_pct,
        }

    except Exception:
        return None


def fetch_returns_for_candidates(
    candidates: list,
    entry_date: str,
    exit_date:  str,
) -> list:
    """
    候補銘柄リストの週間リターンを一括取得する。

    Returns:
        リターン情報を付加した辞書のリスト。取得失敗銘柄は return_pct=None。
    """
    results = []
    n = len(candidates)
    print(f"    📈 リターン取得中: {n}銘柄 "
          f"（{entry_date} 〜 {exit_date}）...")

    for i, c in enumerate(candidates, 1):
        ticker = c.get("ticker", "")
        ret = _fetch_weekly_return(ticker, entry_date, exit_date)

        record = {
            "ticker":          ticker,
            "name":            c.get("name", ticker),
            "sector_code":     c.get("sector_code", ""),
            "sector_name":     c.get("sector_name", ""),
            "composite_score": c.get("composite_score"),
            "accrual_tier":    c.get("accrual_tier", "N/A"),
            "discovery_score": c.get("discovery_score", 0),
        }
        if ret:
            record["entry_price"] = ret["entry_price"]
            record["exit_price"]  = ret["exit_price"]
            record["return_pct"]  = ret["return_pct"]
        else:
            record["entry_price"] = None
            record["exit_price"]  = None
            record["return_pct"]  = None

        results.append(record)

        if i % 5 == 0:
            print(f"      ... {i}/{n}完了")
        time.sleep(RETURN_FETCH_DELAY)

    return results


# ==============================================================
# BLOCK 4: 統計集計
# ==============================================================

def _pearson_corr(xs: list, ys: list) -> Optional[float]:
    """
    2リストの Pearson 相関係数を返す。
    scipy/numpy 不要の純粋実装。
    """
    n = len(xs)
    if n < SCORE_CORR_MIN_N:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx  = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy  = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 3)


def calc_performance_stats(returns: list) -> dict:
    """
    リターンリストから統計指標を算出する。

    Args:
        returns: fetch_returns_for_candidates() の戻り値

    Returns:
        {
          "valid_n":          int,    # リターン取得成功銘柄数
          "win_rate":         float,  # 勝率（プラスリターン率）
          "avg_return":       float,  # 平均リターン(%)
          "median_return":    float,  # メジアン(%)
          "std_return":       float,  # 標準偏差(%)
          "max_return":       float,
          "min_return":       float,
          "top3":             list,   # リターン上位3
          "bottom3":          list,   # リターン下位3
          "score_return_corr": float|None,  # composite_score vs リターン相関
        }
    """
    valid = [r for r in returns if r.get("return_pct") is not None]
    if not valid:
        return {
            "valid_n": 0, "win_rate": None, "avg_return": None,
            "median_return": None, "std_return": None,
            "max_return": None, "min_return": None,
            "top3": [], "bottom3": [], "score_return_corr": None,
        }

    rets = [r["return_pct"] for r in valid]
    n    = len(rets)

    # 基本統計
    win_n     = sum(1 for r in rets if r > 0)
    win_rate  = round(win_n / n, 3)
    avg       = round(sum(rets) / n, 2)
    rets_s    = sorted(rets)
    mid       = n // 2
    median    = round(
        rets_s[mid] if n % 2 == 1 else (rets_s[mid - 1] + rets_s[mid]) / 2,
        2,
    )
    variance  = sum((r - avg) ** 2 for r in rets) / n
    std       = round(variance ** 0.5, 2)

    # ソートして上位/下位
    sorted_by_ret = sorted(valid, key=lambda x: x["return_pct"], reverse=True)
    top3    = sorted_by_ret[:3]
    bottom3 = sorted_by_ret[-3:]

    # composite_score vs リターンの相関係数
    score_ret_pairs = [
        (r["composite_score"], r["return_pct"])
        for r in valid
        if r.get("composite_score") is not None
    ]
    corr = None
    if len(score_ret_pairs) >= SCORE_CORR_MIN_N:
        xs = [p[0] for p in score_ret_pairs]
        ys = [p[1] for p in score_ret_pairs]
        corr = _pearson_corr(xs, ys)

    return {
        "valid_n":           n,
        "win_rate":          win_rate,
        "avg_return":        avg,
        "median_return":     median,
        "std_return":        std,
        "max_return":        round(max(rets), 2),
        "min_return":        round(min(rets), 2),
        "top3":              top3,
        "bottom3":           bottom3,
        "score_return_corr": corr,
    }


# ==============================================================
# BLOCK 5: テキスト整形（週次レポート埋め込み用）
# ==============================================================

def format_performance_section(result: dict) -> str:
    """
    run_performance_tracker() の戻り値を週次レポート用 Markdown に整形する。

    出力例:
      ## 📊 先週の screener 予測精度（Self-Evolution）

      **追跡対象**: 12銘柄 (2026-W10)
      **勝率**: 58.3% (7/12)  **平均リターン**: +1.8%  **メジアン**: +1.2%

      ### 📈 上位銘柄
      | Ticker | 銘柄名 | 業種 | スコア | リターン |
      ...

      ### 📉 下位銘柄
      ...

      **スコア・リターン相関**: r = 0.42（スコアの有効性: 中程度）
    """
    if result.get("skipped"):
        reason = result.get("skip_reason", "")
        return f"## 📊 先週の screener 予測精度\n\n*スキップ: {reason}*\n"

    lines = [
        "## 📊 先週の screener 予測精度（Self-Evolution）",
        "",
        f"**追跡対象**: {result['candidates_n']}銘柄  "
        f"（{result['tracked_week']}）",
    ]

    stats = result
    valid_n = stats.get("valid_n", 0)
    if valid_n == 0:
        lines.append("\n*リターンデータ取得失敗*\n")
        return "\n".join(lines)

    win_rate   = stats.get("win_rate", 0)
    avg_ret    = stats.get("avg_return", 0)
    median_ret = stats.get("median_return", 0)
    std_ret    = stats.get("std_return", 0)
    win_n      = round(win_rate * valid_n)

    # 勝率の評価アイコン
    if win_rate >= 0.60:
        wr_icon = "🟢"
    elif win_rate >= 0.45:
        wr_icon = "🟡"
    else:
        wr_icon = "🔴"

    avg_sign = "+" if avg_ret >= 0 else ""
    lines += [
        f"**勝率**: {wr_icon} {win_rate*100:.1f}% ({win_n}/{valid_n})",
        f"**平均リターン**: {avg_sign}{avg_ret:.1f}%  "
        f"**メジアン**: {median_ret:+.1f}%  "
        f"**標準偏差**: ±{std_ret:.1f}%",
        "",
    ]

    # 上位銘柄
    top3 = stats.get("top3", [])
    if top3:
        lines += [
            "### 📈 上位銘柄",
            "",
            "| Ticker | 銘柄名 | 業種 | スコア | リターン |",
            "|--------|--------|------|--------|--------|",
        ]
        for c in top3:
            ret_s  = f"{c['return_pct']:+.1f}%"
            score  = f"{c['composite_score']:.0f}" if c.get("composite_score") else "N/A"
            lines.append(
                f"| {c['ticker']} | {c['name'][:16]} | "
                f"{c.get('sector_name','')[:8]} | {score} | **{ret_s}** |"
            )
        lines.append("")

    # 下位銘柄
    bottom3 = stats.get("bottom3", [])
    if bottom3:
        lines += [
            "### 📉 下位銘柄",
            "",
            "| Ticker | 銘柄名 | 業種 | スコア | リターン |",
            "|--------|--------|------|--------|--------|",
        ]
        for c in bottom3:
            ret_s  = f"{c['return_pct']:+.1f}%"
            score  = f"{c['composite_score']:.0f}" if c.get("composite_score") else "N/A"
            lines.append(
                f"| {c['ticker']} | {c['name'][:16]} | "
                f"{c.get('sector_name','')[:8]} | {score} | {ret_s} |"
            )
        lines.append("")

    # スコア相関
    corr = stats.get("score_return_corr")
    if corr is not None:
        if abs(corr) >= 0.5:
            corr_label = "強い相関あり（スコアが有効）"
        elif abs(corr) >= 0.3:
            corr_label = "中程度の相関（スコアは概ね有効）"
        else:
            corr_label = "弱い相関（スコア改善の余地あり）"
        lines.append(f"**スコア・リターン相関**: r = {corr:.2f}（{corr_label}）")
        lines.append("")

    # 改善提案（相関が低い場合）
    if corr is not None and corr < 0.2 and valid_n >= SCORE_CORR_MIN_N:
        lines += [
            "> ⚠️ **改善提案**: composite_score と実績リターンの相関が低いです。",
            "> フィルター条件（PER/ROE閾値）またはスコア重み（sector/value/quality比率）",
            "> の見直しを検討してください。",
            "",
        ]

    return "\n".join(lines)


# ==============================================================
# BLOCK 6: 実績保存（performance_history.json）
# ==============================================================

def save_performance_record(cfg, result: dict) -> None:
    """
    週次パフォーマンス結果を performance_history.json に追記する。

    長期的なスコア精度トレンド分析のために保存する。
    """
    p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "performance_history.json"

    try:
        history = []
        if p.exists():
            history = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = []

        record = {
            "week":              result.get("tracked_week", ""),
            "candidates_n":      result.get("candidates_n", 0),
            "valid_n":           result.get("valid_n", 0),
            "win_rate":          result.get("win_rate"),
            "avg_return":        result.get("avg_return"),
            "median_return":     result.get("median_return"),
            "std_return":        result.get("std_return"),
            "score_return_corr": result.get("score_return_corr"),
            "recorded_at":       datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        # 同一週の重複除去
        history = [h for h in history if h.get("week") != record["week"]]
        history.append(record)

        # 最新52週分を保持（約1年）
        history = sorted(history, key=lambda x: x.get("week", ""))[-52:]

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  💾 performance_history 更新: {record['week']} "
              f"勝率{(record['win_rate'] or 0)*100:.0f}% "
              f"平均{record['avg_return']:+.1f}%")

    except Exception as e:
        print(f"  ⚠️  performance_history 保存失敗: {e}")


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_performance_tracker(cfg, reference_date: Optional[date] = None) -> dict:
    """
    先週の screener 通過銘柄の実績リターンを集計する。

    weekly_report_generator.py の run_weekly_report() 内から
    月曜日のみ呼び出す。

    Args:
        cfg:            config モジュール（DATA_DIR を参照）
        reference_date: テスト用の基準日（省略時: 今日）

    Returns:
        パフォーマンス集計結果辞書（上記スキーマ参照）
    """
    today = reference_date or date.today()
    print(f"\n  📊 パフォーマンストラッカー開始（基準日: {today}）...")

    # 月曜日以外は軽量スキップ（週次処理のため）
    if today.weekday() != 0:
        print(f"  ⏩ パフォーマンストラッカー: 月曜以外はスキップ（{today.strftime('%A')}）")
        return {"skipped": True, "skip_reason": f"月曜日以外（{today.strftime('%A')}）"}

    # 先週の日付範囲
    last_mon, last_fri = _get_last_week_range(today)
    # ISO週番号（例: "2026-W10"）
    last_mon_dt = datetime.strptime(last_mon, "%Y-%m-%d").date()
    tracked_week = f"{last_mon_dt.isocalendar()[0]}-W{last_mon_dt.isocalendar()[1]:02d}"

    print(f"    追跡期間: {last_mon} 〜 {last_fri}（{tracked_week}）")

    # 先週の候補銘柄を取得
    candidates = get_last_week_candidates(cfg, reference_date)

    if len(candidates) < MIN_CANDIDATES:
        reason = (f"screener_history に先週データなし または銘柄数不足 "
                  f"（{len(candidates)}銘柄 < 最小{MIN_CANDIDATES}銘柄）")
        print(f"  ⏩ {reason}")
        return {
            "skipped":      True,
            "skip_reason":  reason,
            "tracked_week": tracked_week,
            "candidates_n": len(candidates),
        }

    print(f"    先週の通過銘柄: {len(candidates)}銘柄")

    # yfinance でリターン取得
    returns = fetch_returns_for_candidates(candidates, last_mon, last_fri)

    # 統計集計
    stats = calc_performance_stats(returns)

    valid_n = stats["valid_n"]
    if valid_n > 0:
        print(
            f"    ✅ 集計完了: {valid_n}銘柄 "
            f"勝率{stats['win_rate']*100:.0f}% "
            f"平均{stats['avg_return']:+.1f}% "
            f"メジアン{stats['median_return']:+.1f}%"
        )
        if stats["score_return_corr"] is not None:
            print(f"    📐 スコア相関: r = {stats['score_return_corr']:.2f}")

    result = {
        "tracked_week":      tracked_week,
        "candidates_n":      len(candidates),
        "returns":           returns,
        "skipped":           False,
        "skip_reason":       "",
        **stats,
    }

    # フォーマット済みテキストを生成
    result["summary_text"] = format_performance_section(result)

    # 長期履歴に保存
    save_performance_record(cfg, result)

    return result


# ==============================================================
# BLOCK 8: 単体テスト
# ==============================================================
if __name__ == "__main__":
    import importlib.util

    for p in ["/content/config.py",
              str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break

    # ── テスト1: append_screener_history ──────────────────────
    print("=== テスト1: append_screener_history ===")
    mock_screener = {
        "candidates": [
            {"ticker": "6758.T", "name": "ソニー", "sector_code": "1016",
             "sector_name": "電気機器", "composite_score": 74.2, "value_score": 68.0,
             "quality_score": 72.0, "per": 18.4, "pbr": 1.82, "roe": 12.5,
             "market_cap_b": 15000.0, "avg_volume": 5230000,
             "pass_filters": True, "accrual_tier": "AA", "discovery_score": 0},
            {"ticker": "7203.T", "name": "トヨタ自動車", "sector_code": "1017",
             "sector_name": "輸送用機器", "composite_score": 71.0, "value_score": 75.0,
             "quality_score": 68.0, "per": 8.2, "pbr": 1.10, "roe": 14.1,
             "market_cap_b": 39000.0, "avg_volume": 8500000,
             "pass_filters": True, "accrual_tier": "A", "discovery_score": 0},
            {"ticker": "4502.T", "name": "武田薬品工業", "sector_code": "1008",
             "sector_name": "医薬品", "composite_score": 55.0, "value_score": 50.0,
             "quality_score": 60.0, "per": 22.1, "pbr": 1.45, "roe": 6.8,
             "market_cap_b": 6500.0, "avg_volume": 3200000,
             "pass_filters": False, "accrual_tier": "N/A", "discovery_score": 0},
        ],
        "pass_count": 2,
        "screened_count": 3,
    }
    append_screener_history(cfg, mock_screener)

    hist = load_screener_history(cfg)
    today_str = date.today().strftime("%Y-%m-%d")
    print(f"  保存件数: {len(hist.get(today_str, []))}銘柄（pass_filters=Trueのみ）")
    assert len(hist.get(today_str, [])) == 2, "pass_filters=Trueの2銘柄のみ保存されるべき"
    print("  ✅ テスト1 OK")

    # ── テスト2: 統計計算 ──────────────────────────────────────
    print("\n=== テスト2: calc_performance_stats ===")
    mock_returns = [
        {"ticker": "6758.T", "name": "ソニー", "sector_code": "1016",
         "sector_name": "電気機器", "composite_score": 74.2, "return_pct": 3.5},
        {"ticker": "7203.T", "name": "トヨタ", "sector_code": "1017",
         "sector_name": "輸送用機器", "composite_score": 71.0, "return_pct": -1.2},
        {"ticker": "6501.T", "name": "日立製作所", "sector_code": "1016",
         "sector_name": "電気機器", "composite_score": 68.0, "return_pct": 5.1},
        {"ticker": "8306.T", "name": "三菱UFJ", "sector_code": "1028",
         "sector_name": "銀行業", "composite_score": 60.0, "return_pct": 2.0},
        {"ticker": "4063.T", "name": "信越化学", "sector_code": "1007",
         "sector_name": "化学", "composite_score": 66.0, "return_pct": -0.5},
        {"ticker": "6857.T", "name": "アドバンテスト", "sector_code": "1016",
         "sector_name": "電気機器", "composite_score": 76.0, "return_pct": 7.8},
    ]
    stats = calc_performance_stats(mock_returns)
    print(f"  勝率: {stats['win_rate']*100:.0f}% ({round(stats['win_rate']*6)}/6)")
    print(f"  平均リターン: {stats['avg_return']:+.2f}%")
    print(f"  メジアン: {stats['median_return']:+.2f}%")
    print(f"  標準偏差: ±{stats['std_return']:.2f}%")
    print(f"  スコア相関: r = {stats['score_return_corr']}")
    print(f"  上位: {stats['top3'][0]['ticker']} {stats['top3'][0]['return_pct']:+.1f}%")
    print(f"  下位: {stats['bottom3'][0]['ticker']} {stats['bottom3'][0]['return_pct']:+.1f}%")
    assert stats["win_rate"] == round(4/6, 3), f"勝率エラー: {stats['win_rate']}"
    print("  ✅ テスト2 OK")

    # ── テスト3: format_performance_section ───────────────────
    print("\n=== テスト3: format_performance_section ===")
    mock_result = {
        "tracked_week": "2026-W10",
        "candidates_n": 6,
        "skipped": False,
        "skip_reason": "",
        **stats,
    }
    text = format_performance_section(mock_result)
    print(text)
    print("  ✅ テスト3 OK")

    # ── テスト4: 日付ユーティリティ ───────────────────────────
    print("=== テスト4: 日付範囲 ===")
    mon, fri = _get_last_week_range(date(2026, 3, 16))  # 3/16=月曜
    print(f"  基準日2026-03-16（月）→ 先週: {mon} 〜 {fri}")
    assert mon == "2026-03-09" and fri == "2026-03-13", f"日付エラー: {mon}, {fri}"
    print("  ✅ テスト4 OK")

    print("\n全テスト完了 ✅")
