# ==============================================================
# backtester.py  v1.0  (5-3 / C-2)
# screener条件の過去パフォーマンス自動検証
# ==============================================================
# 【役割】
#   screener_history.json に蓄積された過去の通過銘柄データを使い、
#   「このスクリーニング条件は実際に機能しているか」を
#   勝率・シャープレシオ・最大ドローダウンで自動検証する。
#
# 【バックテスト設計（軽量版 / GitHub Actions対応）】
#   - データソース: screener_history.json（既存蓄積データ）
#   - 検証期間: 直近90日（screener_historyの保持期間）
#   - ホールド期間: 4週間（screener通過日 → 4週後の価格）
#   - ベンチマーク: 日経225（^N225）または TOPIXの同期間リターン
#
# 【計算指標】
#   A. 勝率（Win Rate）
#      screener通過銘柄のうち、ホールド期間でベンチマークを超えた割合
#
#   B. 平均超過リターン（Alpha）
#      通過銘柄の平均リターン - ベンチマーク同期間リターン
#
#   C. シャープレシオ
#      平均リターン / リターンの標準偏差（無リスク金利0%仮定）
#
#   D. 最大ドローダウン
#      全保有期間中の最大の「ピーク→谷」下落率
#
#   E. スコア別分析
#      composite_score 70以上 / 50〜70 / 50以下 で分けて勝率比較
#      → 「高スコア銘柄ほど勝率が高いか」を検証
#
# 【保存先】
#   stock_data/data/backtest_results.json（週次上書き）
#   stock_data/insights/screener/backtest_YYYY-MM-DD.md
#
# 【依存】yfinance / screener_history.json / config.py
# ==============================================================

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_BACKTESTER_HOLD_DAYS': 'HOLD_DAYS',
    'B4_BACKTESTER_LOOKBACK_DAYS': 'LOOKBACK_DAYS',
    'B5_BACKTESTER_FETCH_DELAY': 'FETCH_DELAY',
    'B6_BACKTESTER_MIN_RECORDS': 'MIN_RECORDS',
}

HOLD_DAYS       = 28    # ホールド期間（日）
LOOKBACK_DAYS   = 90    # バックテスト対象期間
MIN_RECORDS     = 10    # 統計的有意性のための最低サンプル数
BENCHMARK_TICKER = "^N225"   # ベンチマーク（日経225）
FETCH_DELAY     = 0.25
RESULT_FILENAME = "backtest_results.json"


# ==============================================================
# BLOCK 1: screener_history 読み込みと有効レコード抽出
# ==============================================================

def load_screener_history(cfg) -> dict:
    """
    screener_history.json を読み込む。

    Returns:
        {"YYYY-MM-DD": [candidate_dict, ...], ...}
    """
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "screener_history.json",
        Path("stock_data/data/screener_history.json"),
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def extract_backtest_records(
    screener_history: dict,
    hold_days:        int = HOLD_DAYS,
    lookback_days:    int = LOOKBACK_DAYS,
) -> list:
    """
    バックテスト対象レコードを抽出する。
    HOLD_DAYS 経過していないレコードは除外（将来データになるため）。

    Returns:
        [
          {
            "ticker":        str,
            "entry_date":    str,
            "entry_price":   float,   # screener時の current_price
            "exit_date":     str,     # entry_date + hold_days
            "composite_score": float,
            "sector_code":   str,
            "cluster_phase_bonus": float,
          },
          ...
        ]
    """
    today    = date.today()
    cutoff   = today - timedelta(days=lookback_days)
    exit_min = today - timedelta(days=hold_days)   # 少なくともhold_days経過

    records = []
    for date_str, candidates in screener_history.items():
        try:
            entry_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if entry_dt < cutoff:
            continue
        if entry_dt > exit_min:
            continue   # まだホールド期間が終わっていない

        if not isinstance(candidates, list):
            continue

        for c in candidates:
            if not isinstance(c, dict):
                continue
            # screener_history には通過銘柄しか保存されない（pass_filters は
            # 古い行には無い）。欄が無い行を落とすと過去分が全滅するため、
            # 明示的に False のときだけ除外する。
            if c.get("pass_filters") is False:
                continue
            ticker = c.get("ticker", "")
            price  = c.get("current_price")
            score  = c.get("composite_score", 0) or 0
            # current_price は古い行には無い。無い行を落とすと「レコード不足(0件)」に
            # なるため None のまま残し、run_backtester がその日の終値で補う。
            if not ticker:
                continue

            exit_dt = entry_dt + timedelta(days=hold_days)
            records.append({
                "ticker":          ticker,
                "entry_date":      date_str,
                "entry_price":     float(price) if price else None,
                "exit_date":       exit_dt.strftime("%Y-%m-%d"),
                "composite_score": float(score),
                "cluster_phase_bonus": float(c.get("cluster_phase_bonus", 0) or 0),
                "sector_code":     c.get("sector_code", ""),
            })

    return records


# ==============================================================
# BLOCK 2: 出口価格・ベンチマーク取得
# ==============================================================

def fetch_close_map(tickers, start: str, end: str) -> dict:
    """{ticker: {"YYYY-MM-DD": 終値}} を銘柄ごとに1回の取得で返す。

    評価レコード1件ごとに yfinance を呼ばず、同じ銘柄・同じ期間を
    何百回も取り直さないよう、期間全体を1回で取る。
    """
    try:
        import yf_cache as yf  # type: ignore
    except ImportError:
        return {}
    out: dict = {}
    for t in sorted(set(tickers)):
        try:
            hist = yf.Ticker(t).history(start=start, end=end, auto_adjust=True)
            if hist is None or hist.empty:
                continue
            idx = hist.index.tz_localize(None) if getattr(hist.index, "tz", None) else hist.index
            out[t] = {d.strftime("%Y-%m-%d"): float(v)
                      for d, v in zip(idx, hist["Close"]) if v == v}
            time.sleep(FETCH_DELAY)
        except Exception:
            continue
    return out


def price_on(closes: dict, date_str: str, forward_days: int = 5) -> Optional[float]:
    """指定日かそれ以降の最初の営業日の終値（純関数）。"""
    if not closes:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for off in range(forward_days):
        v = closes.get((dt + timedelta(days=off)).strftime("%Y-%m-%d"))
        if v:
            return v
    return None


def fill_prices(records: list, closes: dict, bench: dict) -> None:
    """entry_price の欠け・exit_price・同期間のベンチマークを埋める（純関数）。"""
    for r in records:
        c = closes.get(r["ticker"], {})
        if not r.get("entry_price"):
            r["entry_price"] = price_on(c, r["entry_date"])
            r["entry_price_source"] = "close"
        r["exit_price"] = price_on(c, r["exit_date"])
        b0, b1 = price_on(bench, r["entry_date"]), price_on(bench, r["exit_date"])
        r["benchmark_return"] = round((b1 - b0) / b0 * 100, 3) if b0 and b1 else None


def _price_window(records: list) -> tuple[str, str]:
    lo = min(r["entry_date"] for r in records)
    hi = max(r["exit_date"] for r in records)
    return ((datetime.strptime(lo, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d"),
            (datetime.strptime(hi, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d"))


def _fetch_exit_prices(records: list) -> dict:
    """
    各レコードの exit_date における終値を yfinance から取得する。

    Returns:
        {ticker: {date_str: price_float}}
    """
    try:
        import yf_cache as yf  # type: ignore
    except ImportError:
        return {}

    # ticker × 日付の組み合わせを最小リクエスト数で取得
    ticker_date_map: dict[str, set] = {}
    for r in records:
        ticker_date_map.setdefault(r["ticker"], set()).add(r["exit_date"])

    exit_prices = {}
    for ticker, dates in ticker_date_map.items():
        try:
            # 最古の日付から最新の日付までを一括取得
            sorted_dates = sorted(dates)
            start = (datetime.strptime(sorted_dates[0], "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
            end   = (datetime.strptime(sorted_dates[-1], "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")
            hist  = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
            if hist is None or hist.empty:
                continue

            hist.index = hist.index.tz_localize(None) if hasattr(hist.index, 'tz_localize') else hist.index

            for d in dates:
                # 指定日またはその後の最初の営業日の終値を使用
                target_dt = datetime.strptime(d, "%Y-%m-%d")
                price = None
                for offset in range(5):
                    dt = target_dt + timedelta(days=offset)
                    dt_str = dt.strftime("%Y-%m-%d")
                    row = hist[hist.index.strftime("%Y-%m-%d") == dt_str]
                    if not row.empty:
                        price = float(row["Close"].iloc[0])
                        break
                if price:
                    exit_prices.setdefault(ticker, {})[d] = price

            time.sleep(FETCH_DELAY)
        except Exception:
            continue

    return exit_prices


def _fetch_benchmark_return(start_date: str, end_date: str) -> Optional[float]:
    """
    ベンチマーク（日経225）の同期間リターン（%）を取得する。
    """
    try:
        import yf_cache as yf  # type: ignore
        hist = yf.Ticker(BENCHMARK_TICKER).history(
            start=start_date, end=end_date, auto_adjust=True)
        if hist is None or len(hist) < 2:
            return None
        closes = hist["Close"].dropna().tolist()
        return round((closes[-1] - closes[0]) / closes[0] * 100, 3)
    except Exception:
        return None


# ==============================================================
# BLOCK 3: パフォーマンス指標計算
# ==============================================================

def calc_performance_stats(returns: list, benchmark_ret: Optional[float] = None) -> dict:
    """
    リターンのリストから統計指標を計算する。

    Args:
        returns:       個別株リターン（%）のリスト
        benchmark_ret: ベンチマーク同期間リターン（%）

    Returns:
        {
          "n":            int,
          "win_rate":     float,   # ベンチマーク超過の比率
          "avg_return":   float,
          "avg_alpha":    float,   # 平均超過リターン
          "sharpe":       float,
          "max_drawdown": float,
          "hit_rate_naive": float, # +0%超えの単純勝率
        }
    """
    n = len(returns)
    if n == 0:
        return {"n": 0}

    avg    = sum(returns) / n
    var_s  = sum((r - avg) ** 2 for r in returns) / max(n - 1, 1)
    std    = math.sqrt(var_s) if var_s > 0 else 0

    sharpe = round(avg / std, 3) if std > 0 else None

    bm     = benchmark_ret or 0
    alpha  = round(avg - bm, 3)
    win_vs_benchmark = sum(1 for r in returns if r > bm) / n
    hit_naive        = sum(1 for r in returns if r > 0) / n

    # 最大ドローダウン（累積リターン系列から計算）
    cumulative = [100.0]
    for r in returns:
        cumulative.append(cumulative[-1] * (1 + r / 100))
    peak = cumulative[0]
    max_dd = 0.0
    for c in cumulative[1:]:
        if c > peak:
            peak = c
        dd = (c - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd

    return {
        "n":              n,
        "win_rate":       round(win_vs_benchmark, 3),
        "avg_return":     round(avg, 3),
        "avg_alpha":      alpha,
        "std_return":     round(std, 3),
        "sharpe":         sharpe,
        "max_drawdown":   round(max_dd, 2),
        "hit_rate_naive": round(hit_naive, 3),
    }


def run_backtest_analysis(records: list, exit_prices: dict) -> dict:
    """
    レコードとの出口価格からバックテスト指標を計算する。

    Returns:
        {
          "overall":        perf_dict,
          "by_score_tier":  {"high": perf, "mid": perf, "low": perf},
          "by_sector":      {sector_code: perf},
          "evaluated_count": int,
          "missing_count":   int,
        }
    """
    evaluated   = []
    missing     = 0
    bm_rets     = []

    for r in records:
        ticker   = r["ticker"]
        exit_p   = r.get("exit_price") or (exit_prices.get(ticker) or {}).get(r["exit_date"])
        entry_p  = r.get("entry_price") or 0

        if not exit_p or entry_p <= 0:
            missing += 1
            continue

        ret = round((exit_p - entry_p) / entry_p * 100, 3)

        # ベンチマーク（この期間のみ）。fill_prices 済みならそれを使う（再取得しない）
        bm = (r["benchmark_return"] if "benchmark_return" in r
              else _fetch_benchmark_return(r["entry_date"], r["exit_date"]))
        bm_rets.append(bm or 0)

        evaluated.append({
            **r,
            "exit_price":  exit_p,
            "return_pct":  ret,
            "alpha":       round(ret - (bm or 0), 3),
        })

    if not evaluated:
        return {"overall": {"n": 0}, "evaluated_count": 0, "missing_count": missing}

    avg_bm = sum(bm_rets) / len(bm_rets) if bm_rets else 0
    returns = [e["return_pct"] for e in evaluated]

    overall = calc_performance_stats(returns, avg_bm)

    # スコア帯別分析
    high = [e["return_pct"] for e in evaluated if e["composite_score"] >= 70]
    mid  = [e["return_pct"] for e in evaluated if 50 <= e["composite_score"] < 70]
    low  = [e["return_pct"] for e in evaluated if e["composite_score"] < 50]

    by_score = {
        "high_70plus": calc_performance_stats(high, avg_bm) if high else {"n": 0},
        "mid_50_70":   calc_performance_stats(mid,  avg_bm) if mid  else {"n": 0},
        "low_sub50":   calc_performance_stats(low,  avg_bm) if low  else {"n": 0},
    }

    # セクター別分析
    sector_groups: dict[str, list] = {}
    for e in evaluated:
        sector_groups.setdefault(e.get("sector_code", "?"), []).append(e["return_pct"])

    by_sector = {
        code: calc_performance_stats(rets, avg_bm)
        for code, rets in sector_groups.items()
        if len(rets) >= 3
    }

    # ── [B / T-4] クラスターシグナル有無による精度比較 ──────────
    # cluster_phase_bonus が記録されているレコードのみを対象に、
    # ボーナス有（>0）/ ボーナス無（==0）の2群に分けて勝率・アルファを比較する。
    # データ未存在時（cluster_phase_bonus キー欠落）はサイレントスキップ。
    cluster_comparison: dict = {}
    try:
        with_cluster    = [
            e["return_pct"] for e in evaluated
            if float(e.get("cluster_phase_bonus", 0) or 0) > 0
        ]
        without_cluster = [
            e["return_pct"] for e in evaluated
            if float(e.get("cluster_phase_bonus", 0) or 0) == 0
        ]
        if with_cluster and without_cluster:
            cluster_comparison = {
                "with_cluster_bonus": {
                    **calc_performance_stats(with_cluster, avg_bm),
                    "n": len(with_cluster),
                },
                "without_cluster_bonus": {
                    **calc_performance_stats(without_cluster, avg_bm),
                    "n": len(without_cluster),
                },
                "alpha_lift": round(
                    (sum(with_cluster)    / len(with_cluster))
                    - (sum(without_cluster) / len(without_cluster)),
                    3,
                ),
            }
    except Exception:
        cluster_comparison = {}

    return {
        "overall":        overall,
        "by_score_tier":  by_score,
        "by_sector":      by_sector,
        "evaluated_count": len(evaluated),
        "missing_count":  missing,
        "avg_benchmark":  round(avg_bm, 3),
        "top_performers": sorted(evaluated, key=lambda x: -x["return_pct"])[:5],
        "worst":          sorted(evaluated, key=lambda x:  x["return_pct"])[:3],
        "cluster_comparison": cluster_comparison,   # B / T-4
    }


# ==============================================================
# BLOCK 4: レポート生成
# ==============================================================

def format_backtest_report(bt_result: dict) -> str:
    """週次レポート向けのMarkdownレポートを生成する。"""
    ov   = bt_result.get("overall", {})
    n    = ov.get("n", 0)
    bm   = bt_result.get("avg_benchmark", 0)

    lines = [
        "## 📉 バックテスト結果",
        f"（直近{LOOKBACK_DAYS}日 / ホールド{HOLD_DAYS}日 / {n}銘柄評価）",
        "",
    ]

    if n == 0:
        lines.append("サンプル不足（screener_history の蓄積中）")
        return "\n".join(lines)

    lines += [
        "| 指標 | 値 |",
        "|------|---|",
        f"| 評価銘柄数 | {n}件 |",
        f"| ベンチマーク平均 | {bm:+.2f}% |",
        f"| 平均リターン | {ov.get('avg_return',0):+.2f}% |",
        f"| 平均アルファ | {ov.get('avg_alpha',0):+.2f}% |",
        f"| 勝率（対ベンチ） | {ov.get('win_rate',0):.1%} |",
        f"| シャープレシオ | {ov.get('sharpe','N/A')} |",
        f"| 最大ドローダウン | {ov.get('max_drawdown',0):.1f}% |",
        "",
    ]

    by_score = bt_result.get("by_score_tier", {})
    if any(v.get("n", 0) > 0 for v in by_score.values()):
        lines += [
            "### スコア帯別 勝率",
            "",
            "| スコア帯 | N | 平均α | 勝率 |",
            "|---------|---|------|-----|",
        ]
        for tier, label in [("high_70plus","score≥70"),("mid_50_70","50〜70"),("low_sub50","<50")]:
            d = by_score.get(tier, {})
            if d.get("n", 0) > 0:
                lines.append(
                    f"| {label} | {d['n']} "
                    f"| {d.get('avg_alpha',0):+.2f}% "
                    f"| {d.get('win_rate',0):.1%} |"
                )
        lines.append("")

    # クラスターシグナル有無の比較セクション
    cc = bt_result.get("cluster_comparison", {})
    if cc.get("with_cluster_bonus") and cc.get("without_cluster_bonus"):
        wc  = cc["with_cluster_bonus"]
        woc = cc["without_cluster_bonus"]
        alpha_lift = cc.get("alpha_lift", 0)
        lines += [
            "### 🔬 クラスターシグナル 精度比較（Wave 6 - B）",
            "",
            "| 区分 | N | 平均α | 勝率 |",
            "|------|---|------|-----|",
            f"| クラスターボーナス有 | {wc.get('n',0)} "
            f"| {wc.get('avg_alpha',0):+.2f}% "
            f"| {wc.get('win_rate',0):.1%} |",
            f"| クラスターボーナス無 | {woc.get('n',0)} "
            f"| {woc.get('avg_alpha',0):+.2f}% "
            f"| {woc.get('win_rate',0):.1%} |",
            "",
            f"**アルファ改善幅**: {alpha_lift:+.2f}% "
            f"({'改善' if alpha_lift > 0 else '悪化'})",
            "",
        ]

    # 改善提案
    alpha = ov.get("avg_alpha", 0)
    win   = ov.get("win_rate", 0.5)
    if alpha > 2 and win > 0.55:
        lines.append("✅ **スクリーニング精度良好** — 高アルファ・高勝率を維持")
    elif alpha > 0 and win > 0.5:
        lines.append("🟡 **精度普通** — 若干のアルファあり。MACRO_RULESの重みを確認推奨")
    else:
        lines.append("⚠️ **精度低下** — rule_optimizer.py の実行とMACRO_RULES見直しを推奨")

    return "\n".join(lines)


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_backtester(cfg) -> dict:
    """
    バックテストを実行する（週次・GitHub Actions対応）。

    stock_analyzer_main.py から週次で呼び出す:
        _bt = _load_local_module("backtester", ...)
        bt_result = _bt.run_backtester(cfg)
        results["backtest"] = bt_result

    Returns:
        {
          "overall":         dict,
          "by_score_tier":   dict,
          "by_sector":       dict,
          "report_md":       str,
          "evaluated_count": int,
          "timestamp":       str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📉 バックテスト実行開始...")

    history = load_screener_history(cfg)
    if not history:
        print("    ⚠️  screener_history.json なし → スキップ")
        return {"skipped": True, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    records = extract_backtest_records(history)
    if len(records) < MIN_RECORDS:
        print(f"    ⚠️  有効レコード不足: {len(records)}件 < {MIN_RECORDS}件（蓄積中）")
        return {
            "skipped": True, "reason": f"レコード不足({len(records)}件)",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    print(f"    対象: {len(records)}レコード（{HOLD_DAYS}日ホールド）")

    # 価格取得（銘柄ごとに1回。取得時点の価格が無い過去分はその日の終値で補う）
    start, end = _price_window(records)
    tickers = {r["ticker"] for r in records}
    print(f"    価格取得中... {len(tickers)}銘柄 × 1回（{start}〜{end}）")
    closes = fetch_close_map(tickers | {BENCHMARK_TICKER}, start, end)
    fill_prices(records, closes, closes.get(BENCHMARK_TICKER, {}))
    n_filled = sum(1 for r in records if r.get("entry_price_source") == "close")
    if n_filled:
        print(f"    取得時点の価格を終値で補完: {n_filled}件")
    exit_prices = {}

    # バックテスト分析
    bt_result = run_backtest_analysis(records, exit_prices)
    bt_result["report_md"] = format_backtest_report(bt_result)

    ov = bt_result.get("overall", {})
    print(f"    ✅ バックテスト完了: {bt_result.get('evaluated_count',0)}銘柄評価 "
          f"平均α={ov.get('avg_alpha',0):+.2f}% "
          f"勝率={ov.get('win_rate',0):.1%}")

    # 保存
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / RESULT_FILENAME).write_text(
            json.dumps({**bt_result, "timestamp": datetime.now().isoformat()},
                       ensure_ascii=False, indent=2), encoding="utf-8")

        insight_dir = Path(getattr(cfg, "INSIGHT_DIR", "stock_data/insights")) / "screener"
        insight_dir.mkdir(parents=True, exist_ok=True)
        (insight_dir / f"backtest_{today}.md").write_text(
            bt_result["report_md"], encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  保存失敗: {e}")

    bt_result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # cluster_comparison サマリーをログ出力
    try:
        cc = bt_result.get("cluster_comparison", {})
        if cc.get("with_cluster_bonus"):
            lift = cc.get("alpha_lift", 0)
            print(
                f"    🔬 クラスターシグナル精度比較: "
                f"ボーナス有={cc['with_cluster_bonus']['n']}件 "
                f"/ アルファ改善幅={lift:+.2f}%"
            )
    except Exception:
        pass
    return bt_result
