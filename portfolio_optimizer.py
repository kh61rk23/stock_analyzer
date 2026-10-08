# ==============================================================
# portfolio_optimizer.py  v1.0  (6-1 / C-1)
# 効率的フロンティア計算 + リバランシング提案 月次Gmail通知
# ==============================================================
# 【役割】
#   portfolio_holdings.csv の保有銘柄に対して
#   効率的フロンティア（Modern Portfolio Theory）を計算し、
#   「リターン最大化 or リスク最小化」のための最適ウェイトを提案する。
#
# 【scipy を使わない純粋実装】
#   GitHub Actions での scipy インストールコストを避けるため、
#   最小分散ポートフォリオと最大シャープレシオを
#   ランダムサンプリング法（モンテカルロ最適化）で近似する。
#   1,000〜3,000 ポートフォリオをシミュレーションして最適解を探索。
#
# 【計算内容】
#   A. 現在ポートフォリオの評価
#      - 各銘柄の現在時価・評価損益・ウェイト
#      - ポートフォリオ全体のリターン・リスク（標準偏差）・シャープレシオ
#
#   B. 最適化
#      - 最小分散ポートフォリオ（リスク最小）
#      - 最大シャープレシオポートフォリオ
#      - 等ウェイトポートフォリオ（ベンチマーク）
#
#   C. リバランシング提案
#      - 現在ウェイトと最適ウェイトの差分を計算
#      - ±5%以上の乖離がある銘柄を「要リバランシング」と判定
#      - 必要売買金額（概算）を算出
#
# 【出力スキーマ】
#   {
#     "current":      CurrentPortfolioResult,
#     "min_variance": OptimalPortfolio,
#     "max_sharpe":   OptimalPortfolio,
#     "rebalance":    [RebalanceItem, ...],
#     "alert_html":   str,   # Gmail HTML
#     "report_md":    str,
#     "timestamp":    str,
#   }
#
# 【月次通知の仕組み】
#   毎月1日（または force=True）のとき Gmail 送信。
#
# 【依存】yfinance / portfolio_holdings.csv / config.py
# ==============================================================

from __future__ import annotations

import csv
import json
import math
import random
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

N_SIMULATIONS    = 2000   # モンテカルロ法のシミュレーション数
RISK_FREE_RATE   = 0.005  # 無リスク金利（年率0.5%）

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'PO_REBALANCE_THRESH': 'REBALANCE_THRESH',
    'B4_PORTFOLIO_OPTIMIZER_CACHE_HOURS': 'CACHE_HOURS',
    'B5_PORTFOLIO_OPTIMIZER_FETCH_DELAY': 'FETCH_DELAY',
    'B6_PORTFOLIO_OPTIMIZER_N_SIMULATIONS': 'N_SIMULATIONS',
    'B6_PORTFOLIO_OPTIMIZER_RISK_FREE_RATE': 'RISK_FREE_RATE',
}

REBALANCE_THRESH = 0.05   # リバランシング閾値（±5%）
FETCH_DELAY      = 0.3
CACHE_FILENAME   = "portfolio_optimizer_cache.json"
CACHE_HOURS      = 24


# ==============================================================
# BLOCK 1: ポートフォリオ読み込み
# ==============================================================

def load_portfolio(cfg) -> list:
    """
    portfolio_holdings.csv を読み込む。

    Returns:
        [{"ticker", "name", "quantity": int, "avg_cost_jpy": float,
          "sector_code", "market"}, ...]
    """
    # 実ファイルはリポジトリ直下の data/portfolio_holdings.csv にある
    # （exit_rule_engine / scenario_analysis は cfg.PORTFOLIO_HOLDINGS_PATH 経由で読む）。候補パスに含めること。
    base_dir = Path(__file__).parent
    candidates = [
        Path(getattr(cfg, "PORTFOLIO_HOLDINGS_PATH", base_dir / "data" / "portfolio_holdings.csv")),
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "portfolio_holdings.csv",
        base_dir / "data" / "portfolio_holdings.csv",
        base_dir / "portfolio_holdings.csv",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            holdings = []
            with open(p, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    # 持株会の株数は小数（740.955株）のため float で読む（int() だと例外で読み込み全体が失敗する）
                    qty  = float(row.get("quantity", 0) or 0)
                    cost = float(row.get("avg_cost_jpy", 0) or 0)
                    if row.get("ticker") and qty > 0:
                        holdings.append({
                            "ticker":       row["ticker"].strip(),
                            "name":         row.get("name", row["ticker"])[:20],
                            "quantity":     qty,
                            "avg_cost_jpy": cost,
                            "sector_code":  row.get("sector_code", ""),
                            "market":       row.get("market", "JP"),
                            "market_value_jpy": float(row.get("market_value_jpy", 0) or 0),
                        })
            return holdings
        except Exception:
            pass
    return []


# ==============================================================
# BLOCK 2: 価格・リターンデータ取得
# ==============================================================

def _calendar_days_for_trading_days(trading_days: int) -> int:
    """取引日数を、週末と祝日の余裕を含む暦日取得幅へ変換する。"""
    return math.ceil(trading_days * 7 / 5) + 14


def _validate_return_window(lookback_days: int, min_observations: int) -> None:
    if lookback_days <= 0 or min_observations <= 0:
        raise ValueError("portfolio return window values must be positive")
    if lookback_days < min_observations:
        raise ValueError(
            "PORTFOLIO_LOOKBACK_DAYS must be >= PORTFOLIO_MIN_RETURN_OBSERVATIONS"
        )


def _fetch_daily_returns(
    ticker: str,
    lookback_days: int = 252,
    min_observations: int = 60,
    now: Optional[datetime] = None,
) -> Optional[list]:
    """
    指定取引日数分の日次リターンを取得する。

    API取得は週末・祝日を考慮した暦日幅で行い、取得後に直近
    ``lookback_days + 1`` 終値へ切り詰める。
    """
    _validate_return_window(lookback_days, min_observations)
    try:
        import yf_cache as yf
        current_dt = now or datetime.now()
        calendar_days = _calendar_days_for_trading_days(lookback_days)
        start = (current_dt - timedelta(days=calendar_days)).strftime("%Y-%m-%d")
        end = (current_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        hist = yf.Ticker(ticker).history(
            start=start, end=end, auto_adjust=True
        )
        if hist is None or len(hist) < min_observations + 1:
            return None
        closes = hist["Close"].dropna().tolist()[-(lookback_days + 1):]
        rets   = [(closes[i] - closes[i-1]) / closes[i-1]
                  for i in range(1, len(closes))]
        if len(rets) < min_observations:
            return None
        return rets
    except Exception:
        return None


def fetch_portfolio_data(
    holdings: list,
    lookback_days: int = 252,
    min_observations: int = 60,
) -> dict:
    """
    全保有銘柄の価格・リターン・現在時価を一括取得する。

    Returns:
        {
          ticker: {
            "current_price": float,
            "returns":       [daily_ret, ...],
            "mean_annual":   float,   # 年率平均リターン
            "std_annual":    float,   # 年率標準偏差（リスク）
          }
        }
    """
    try:
        import yf_cache as yf
    except ImportError:
        return {}

    data = {}
    for h in holdings:
        ticker = h["ticker"]
        try:
            t     = yf.Ticker(ticker)
            info  = t.info or {}
            price = info.get("regularMarketPrice") or info.get("currentPrice") or 0
            rets  = _fetch_daily_returns(
                ticker, lookback_days, min_observations
            )
            if not rets or not price:
                continue
            mean_daily = sum(rets) / len(rets)
            var_daily  = sum((r - mean_daily) ** 2 for r in rets) / max(len(rets)-1, 1)
            std_daily  = math.sqrt(var_daily) if var_daily > 0 else 0

            data[ticker] = {
                "current_price": float(price),
                "returns":       rets,
                "mean_annual":   round(mean_daily * 252, 4),
                "std_annual":    round(std_daily * math.sqrt(252), 4),
            }
            time.sleep(FETCH_DELAY)
        except Exception:
            continue
    return data


# ==============================================================
# BLOCK 3: 相関行列 & 共分散行列の計算
# ==============================================================

def calc_covariance_matrix(tickers: list, returns_map: dict) -> list:
    """
    銘柄間の共分散行列（日次→年率換算）を計算する。

    Returns:
        2次元リスト cov[i][j] = 年率共分散
    """
    n   = len(tickers)
    cov = [[0.0] * n for _ in range(n)]

    for i, ti in enumerate(tickers):
        for j, tj in enumerate(tickers):
            ri = returns_map.get(ti, {}).get("returns", [])
            rj = returns_map.get(tj, {}).get("returns", [])
            min_len = min(len(ri), len(rj))
            if min_len < 20:
                cov[i][j] = 0.0
                continue
            ri = ri[-min_len:]
            rj = rj[-min_len:]
            mi = sum(ri) / min_len
            mj = sum(rj) / min_len
            c  = sum((ri[k] - mi) * (rj[k] - mj)
                     for k in range(min_len)) / max(min_len - 1, 1)
            cov[i][j] = c * 252   # 年率換算

    return cov


# ==============================================================
# BLOCK 4: ポートフォリオ統計量計算
# ==============================================================

def portfolio_stats(
    weights:  list,
    means:    list,
    cov:      list,
    risk_free: float = RISK_FREE_RATE,
) -> dict:
    """
    ウェイトから期待リターン・リスク・シャープレシオを計算する。

    Returns:
        {"return": float, "risk": float, "sharpe": float}
    """
    n      = len(weights)
    exp_ret = sum(weights[i] * means[i] for i in range(n))

    variance = 0.0
    for i in range(n):
        for j in range(n):
            variance += weights[i] * weights[j] * cov[i][j]
    risk = math.sqrt(max(0, variance))

    sharpe = (exp_ret - risk_free) / risk if risk > 1e-9 else 0.0

    return {
        "return": round(exp_ret, 4),
        "risk":   round(risk, 4),
        "sharpe": round(sharpe, 4),
    }


# ==============================================================
# BLOCK 5: モンテカルロ最適化
# ==============================================================

def monte_carlo_optimize(
    tickers:   list,
    means:     list,
    cov:       list,
    n_sim:     int = N_SIMULATIONS,
    seed:      int = 42,
) -> dict:
    """
    ランダムサンプリングで効率的フロンティア上の2点を探索する。

    Returns:
        {
          "min_variance": {"weights": [...], "stats": {...}},
          "max_sharpe":   {"weights": [...], "stats": {...}},
          "equal_weight": {"weights": [...], "stats": {...}},
          "frontier":     [{"return", "risk"}, ...],   # フロンティア上の点
        }
    """
    random.seed(seed)
    n = len(tickers)
    if n == 0:
        return {}

    results = []
    for _ in range(n_sim):
        # ディリクレ分布的なランダムウェイト（合計1）
        raw    = [random.random() for _ in range(n)]
        total  = sum(raw)
        w      = [r / total for r in raw]
        stats  = portfolio_stats(w, means, cov)
        results.append((w, stats))

    # 最小分散
    min_var = min(results, key=lambda x: x[1]["risk"])
    # 最大シャープレシオ
    max_sh  = max(results, key=lambda x: x[1]["sharpe"])
    # 等ウェイト
    eq_w    = [1.0 / n] * n
    eq_stats = portfolio_stats(eq_w, means, cov)

    # フロンティア用: リスク帯別の最大リターン点を抽出
    results.sort(key=lambda x: x[1]["risk"])
    step = max(1, len(results) // 20)
    frontier = [{"return": r[1]["return"], "risk": r[1]["risk"]}
                for r in results[::step]]

    def _to_dict(item):
        w, s = item
        return {
            "weights": {tickers[i]: round(w[i], 4) for i in range(n)},
            "stats":   s,
        }

    return {
        "min_variance": _to_dict(min_var),
        "max_sharpe":   _to_dict(max_sh),
        "equal_weight": {"weights": {tickers[i]: round(eq_w[i], 4) for i in range(n)},
                         "stats": eq_stats},
        "frontier":     frontier,
    }


# ==============================================================
# BLOCK 6: 現在ポートフォリオ評価
# ==============================================================

def evaluate_current_portfolio(
    holdings:     list,
    price_data:   dict,
) -> dict:
    """
    現在の保有状況を評価する。

    Returns:
        {
          "total_value_jpy": float,
          "positions": [{"ticker", "name", "value_jpy", "weight",
                         "unrealized_pnl_jpy", "unrealized_pnl_pct"}],
          "concentration_top3": float,   # 上位3銘柄の集中度
        }
    """
    positions = []
    total_value = 0.0

    # 米国株のドル株価を円の取得単価と直接比べない（評価損益が約−99%になる）。
    # 株価は円に直してから比べる（為替は保有銘柄CSVの実勢値）
    try:
        import json as _json
        _usd = float(_json.loads((Path(__file__).parent / "data" / "portfolio_summary.json")
                                 .read_text(encoding="utf-8")).get("usd_jpy") or 150.0)
    except (OSError, ValueError):
        _usd = 150.0
    from sync_holdings import holding_value_jpy
    for h in holdings:
        t     = h["ticker"]
        price_local = price_data.get(t, {}).get("current_price", 0)
        qty   = h["quantity"]
        cost  = h["avg_cost_jpy"]
        value = holding_value_jpy(h, price_local if price_local and price_local > 0 else None, _usd)
        price = value / qty if qty else 0.0          # 円換算の1株あたり
        total_value += value
        pnl     = value - cost * qty
        pnl_pct = round((price - cost) / cost * 100, 2) if cost > 0 else 0

        positions.append({
            "ticker":             t,
            "name":               h["name"],
            "quantity":           qty,
            "avg_cost":           cost,
            "current_price":      round(price, 2),
            "value_jpy":          round(value, 0),
            "weight":             0.0,   # 後で計算
            "unrealized_pnl_jpy": round(pnl, 0),
            "unrealized_pnl_pct": pnl_pct,
        })

    # ウェイト計算
    for p in positions:
        p["weight"] = round(p["value_jpy"] / total_value, 4) if total_value > 0 else 0

    # 集中度（上位3銘柄のウェイト合計）
    weights_sorted = sorted(p["weight"] for p in positions)[::-1]
    conc_top3 = sum(weights_sorted[:3])

    # ポートフォリオ統計
    tickers_ok = [p["ticker"] for p in positions if p["ticker"] in price_data]
    if tickers_ok:
        means  = [price_data[t]["mean_annual"] for t in tickers_ok]
        w_curr = [next(p["weight"] for p in positions if p["ticker"] == t)
                  for t in tickers_ok]
        cov    = calc_covariance_matrix(tickers_ok, price_data)
        stats  = portfolio_stats(w_curr, means, cov)
    else:
        stats  = {"return": 0, "risk": 0, "sharpe": 0}

    return {
        "total_value_jpy":    round(total_value, 0),
        "positions":          sorted(positions, key=lambda x: -x["value_jpy"]),
        "concentration_top3": round(conc_top3, 3),
        "portfolio_stats":    stats,
    }


# ==============================================================
# BLOCK 7: リバランシング提案
# ==============================================================

def generate_rebalance_plan(
    current_positions:    list,
    optimal_weights:      dict,   # {ticker: weight_float}
    total_value_jpy:      float,
    threshold:            float = REBALANCE_THRESH,
) -> list:
    """
    現在ウェイトと最適ウェイトの差分からリバランシング計画を生成する。

    Returns:
        [
          {"ticker", "name", "current_weight", "target_weight", "delta",
           "action": "BUY"/"SELL"/"HOLD", "amount_jpy": float}
        ]
    """
    plan = []
    for pos in current_positions:
        t       = pos["ticker"]
        curr_w  = pos["weight"]
        tgt_w   = optimal_weights.get(t, 0)
        delta   = tgt_w - curr_w

        if abs(delta) < threshold:
            action = "HOLD"
        elif delta > 0:
            action = "BUY"
        else:
            action = "SELL"

        amount = abs(delta) * total_value_jpy

        plan.append({
            "ticker":         t,
            "name":           pos["name"],
            "current_weight": round(curr_w * 100, 1),
            "target_weight":  round(tgt_w * 100, 1),
            "delta_pct":      round(delta * 100, 1),
            "action":         action,
            "amount_jpy":     round(amount, 0),
        })

    return sorted(plan, key=lambda x: abs(x["delta_pct"]), reverse=True)


# ==============================================================
# BLOCK 8: レポート・Gmail HTML 生成
# ==============================================================

def format_optimizer_md(result: dict) -> str:
    """週次レポート向け Markdown を生成する。"""
    curr   = result.get("current", {})
    ms     = result.get("max_sharpe", {})
    mv     = result.get("min_variance", {})
    rebal  = result.get("rebalance", [])
    total  = curr.get("total_value_jpy", 0)
    stats  = curr.get("portfolio_stats", {})
    conc   = curr.get("concentration_top3", 0)

    lines = [
        "## 💼 ポートフォリオ最適化レポート",
        "",
        f"**総時価**: {total:,.0f}円  "
        f"**期待リターン**: {stats.get('return',0):.1%}  "
        f"**リスク**: {stats.get('risk',0):.1%}  "
        f"**シャープ**: {stats.get('sharpe',0):.2f}",
        f"**上位3銘柄集中度**: {conc:.1%}",
        "",
        "### 📊 現在保有",
        "",
        "| 銘柄 | 時価（円） | ウェイト | 評価損益 |",
        "|-----|----------|--------|--------|",
    ]
    for p in curr.get("positions", [])[:8]:
        pnl_icon = "📈" if p["unrealized_pnl_jpy"] >= 0 else "📉"
        lines.append(
            f"| {p['name'][:10]} | {p['value_jpy']:,.0f} "
            f"| {p['weight']*100:.1f}% "
            f"| {pnl_icon}{p['unrealized_pnl_jpy']:+,.0f}円 "
            f"({p['unrealized_pnl_pct']:+.1f}%) |"
        )
    lines.append("")

    # 最適化比較
    if ms and mv:
        ms_s = ms.get("stats", {}); mv_s = mv.get("stats", {})
        lines += [
            "### 🎯 最適化シミュレーション（モンテカルロ法）",
            "",
            "| 戦略 | 期待リターン | リスク | シャープ |",
            "|-----|-----------|------|-------|",
            f"| **現在** | {stats.get('return',0):.1%} | {stats.get('risk',0):.1%} | {stats.get('sharpe',0):.2f} |",
            f"| 最大シャープ | {ms_s.get('return',0):.1%} | {ms_s.get('risk',0):.1%} | {ms_s.get('sharpe',0):.2f} |",
            f"| 最小リスク | {mv_s.get('return',0):.1%} | {mv_s.get('risk',0):.1%} | {mv_s.get('sharpe',0):.2f} |",
            "",
        ]

    # リバランシング必要銘柄
    needs_rebal = [r for r in rebal if r["action"] != "HOLD"]
    if needs_rebal:
        lines += [
            "### ⚖️ リバランシング提案（±5%以上の乖離）",
            "",
            "| 銘柄 | 現在 | 目標 | 差分 | アクション | 概算金額 |",
            "|-----|-----|-----|-----|--------|--------|",
        ]
        for r in needs_rebal[:6]:
            act_icon = "🟢買い" if r["action"] == "BUY" else "🔴売り"
            lines.append(
                f"| {r['name'][:10]} | {r['current_weight']:.1f}% "
                f"| {r['target_weight']:.1f}% "
                f"| {r['delta_pct']:+.1f}% "
                f"| {act_icon} "
                f"| {r['amount_jpy']:,.0f}円 |"
            )
    else:
        lines.append("✅ **リバランシング不要** — 現在のウェイトは最適範囲内です")

    lines += [
        "",
        "> ⚠️ 本レポートは参考情報です。投資判断は必ずご自身でご確認ください。",
    ]
    return "\n".join(lines)


def format_optimizer_gmail_html(result: dict) -> str:
    """月次 Gmail 通知用 HTML を生成する。"""
    curr  = result.get("current", {})
    rebal = result.get("rebalance", [])
    stats = curr.get("portfolio_stats", {})
    total = curr.get("total_value_jpy", 0)
    now   = datetime.now().strftime("%Y年%m月")
    needs = [r for r in rebal if r["action"] != "HOLD"]

    pos_rows = "".join(
        f"<tr><td style='padding:5px'>{p['name'][:12]}</td>"
        f"<td>{p['weight']*100:.1f}%</td>"
        f"<td style='color:{'#27ae60' if p['unrealized_pnl_jpy']>=0 else '#c0392b'}'>"
        f"{p['unrealized_pnl_jpy']:+,.0f}円</td></tr>"
        for p in curr.get("positions", [])[:6]
    )

    rebal_rows = "".join(
        f"<tr style='background:{'#fef9f0' if r['action']=='BUY' else '#fef2f2'}'>"
        f"<td style='padding:5px'>{r['name'][:12]}</td>"
        f"<td>{r['current_weight']:.1f}%→{r['target_weight']:.1f}%</td>"
        f"<td style='font-weight:bold;color:{'#27ae60' if r['action']=='BUY' else '#c0392b'}'>"
        f"{'買い増し' if r['action']=='BUY' else '一部売却'}</td>"
        f"<td>{r['amount_jpy']:,.0f}円</td></tr>"
        for r in needs[:5]
    ) if needs else "<tr><td colspan='4' style='padding:10px;text-align:center;color:#7f8c8d'>リバランシング不要</td></tr>"

    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:auto">
<h2 style="color:#2c3e50">💼 {now} ポートフォリオ最適化レポート</h2>
<p>総時価: <strong>{total:,.0f}円</strong>
   | 期待リターン: <strong>{stats.get('return',0):.1%}</strong>
   | リスク: <strong>{stats.get('risk',0):.1%}</strong>
   | シャープ: <strong>{stats.get('sharpe',0):.2f}</strong></p>
<h3>📊 保有銘柄</h3>
<table style="width:100%;border-collapse:collapse;border:1px solid #bdc3c7">
<tr style="background:#2c3e50;color:white"><th>銘柄</th><th>ウェイト</th><th>評価損益</th></tr>
{pos_rows}
</table>
<h3>⚖️ リバランシング提案</h3>
<table style="width:100%;border-collapse:collapse;border:1px solid #bdc3c7">
<tr style="background:#2c3e50;color:white"><th>銘柄</th><th>ウェイト変更</th><th>アクション</th><th>概算金額</th></tr>
{rebal_rows}
</table>
<p style="color:#7f8c8d;font-size:12px;margin-top:16px">
⚠️ 本レポートは参考情報です。最終判断はご自身でご確認ください。<br>
stock_analyzer — 自動生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}
</p></body></html>"""


# ==============================================================
# BLOCK 9: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME

def _load_cache(
    cfg,
    lookback_days: Optional[int] = None,
    min_observations: Optional[int] = None,
) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if (lookback_days is not None
                and int(data.get("lookback_days", -1)) != lookback_days):
            return None
        if (min_observations is not None
                and int(data.get("min_return_observations", -1)) != min_observations):
            return None
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
# BLOCK 10: メインエントリポイント
# ==============================================================

def run_portfolio_optimizer(
    cfg,
    force_monthly_mail: bool = False,
) -> dict:
    """
    ポートフォリオ最適化を実行し、月次で Gmail 通知を送信する。

    stock_analyzer_main.py から週次で呼び出す:
        _po = _load_local_module("portfolio_optimizer", ...)
        po_result = _po.run_portfolio_optimizer(cfg)
        results["portfolio_optimizer"] = po_result

    Returns:
        portfolio optimizer result dict
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  💼 ポートフォリオ最適化開始...")

    lookback_days = int(getattr(cfg, "PORTFOLIO_LOOKBACK_DAYS", 252))
    min_observations = int(getattr(
        cfg, "PORTFOLIO_MIN_RETURN_OBSERVATIONS", 60
    ))
    _validate_return_window(lookback_days, min_observations)

    cached = _load_cache(cfg, lookback_days, min_observations)
    if cached and not force_monthly_mail:
        print(f"    ✅ キャッシュ使用（総時価: {cached.get('current',{}).get('total_value_jpy',0):,.0f}円）")
        return cached

    # ポートフォリオ読み込み
    holdings = load_portfolio(cfg)
    if not holdings:
        print("    ⚠️  portfolio_holdings.csv なし or 保有銘柄なし → スキップ")
        return {"skipped": True, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    print(f"    対象: {len(holdings)}銘柄 / 価格・リターン取得中...")
    price_data = fetch_portfolio_data(
        holdings, lookback_days, min_observations
    )

    valid = [h for h in holdings if h["ticker"] in price_data]
    if len(valid) < 2:
        print(f"    ⚠️  有効データ不足 ({len(valid)}銘柄) → スキップ")
        return {"skipped": True, "reason": "有効銘柄不足",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # 現在ポートフォリオ評価
    current = evaluate_current_portfolio(valid, price_data)
    total   = current["total_value_jpy"]
    print(f"    総時価: {total:,.0f}円 / "
          f"期待リターン:{current['portfolio_stats']['return']:.1%} / "
          f"リスク:{current['portfolio_stats']['risk']:.1%}")

    # 最適化
    tickers = [h["ticker"] for h in valid if h["ticker"] in price_data]
    means   = [price_data[t]["mean_annual"] for t in tickers]
    cov     = calc_covariance_matrix(tickers, price_data)

    print(f"    モンテカルロ最適化（{N_SIMULATIONS}シミュレーション）...")
    opt = monte_carlo_optimize(tickers, means, cov)

    # リバランシング計画（最大シャープ比ポートフォリオを目標）
    ms_weights = opt.get("max_sharpe", {}).get("weights", {})
    rebalance  = generate_rebalance_plan(
        current["positions"], ms_weights, total)

    n_rebal = sum(1 for r in rebalance if r["action"] != "HOLD")
    if n_rebal > 0:
        print(f"    リバランシング推奨: {n_rebal}銘柄")
        for r in rebalance[:3]:
            if r["action"] != "HOLD":
                print(f"      {r['name']}: {r['current_weight']:.1f}%→"
                      f"{r['target_weight']:.1f}% {r['action']} "
                      f"{r['amount_jpy']:,.0f}円")

    result = {
        "current":      current,
        "max_sharpe":   opt.get("max_sharpe"),
        "min_variance": opt.get("min_variance"),
        "equal_weight": opt.get("equal_weight"),
        "rebalance":    rebalance,
        "lookback_days": lookback_days,
        "min_return_observations": min_observations,
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["report_md"]   = format_optimizer_md(result)
    result["alert_html"]  = format_optimizer_gmail_html(result)

    # 月次 Gmail 送信
    is_month_start = (date.today().day == 1)
    if (is_month_start or force_monthly_mail) and n_rebal > 0:
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            frm = getattr(cfg, "ALERT_GMAIL_FROM", "")
            pwd = getattr(cfg, "ALERT_GMAIL_PASSWORD", "")
            to  = getattr(cfg, "ALERT_GMAIL_TO", "")
            if frm and pwd and to:
                subject = f"💼【月次】ポートフォリオ最適化レポート — {n_rebal}銘柄要リバランシング"
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = frm
                msg["To"]      = to
                msg.attach(MIMEText(result["alert_html"], "html", "utf-8"))
                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                    smtp.login(frm, pwd)
                    smtp.sendmail(frm, to, msg.as_string())
                print(f"    ✅ 月次 Gmail 送信: {subject[:60]}")
                result["mail_sent"] = True
        except Exception as e:
            print(f"    ⚠️  Gmail 送信失敗: {str(e)[:60]}")
            result["mail_sent"] = False

    _save_cache(cfg, result)
    print(f"    ✅ ポートフォリオ最適化完了")
    return result
