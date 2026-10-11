# ==============================================================
# paper/position_var.py
# 紙トレード口座ごとの日次VaR95%を「今の保有銘柄×過去1年の日次騰落」で出す（ヒストリカル・シミュレーション）。
# ==============================================================
# 【なぜ要るか】
#   リスク管制ボードの VaR は口座の資産推移（equity_history）から計算しており、口座開始（2026-09-30）から
#   21日分たまるまで「データ不足」だった。保有銘柄の過去の値動きを今の持ち高に当てはめれば初日から出せる。
#   口座の資産推移が十分たまったあとも、両方を並べて見られるように残す。
# 【計算】
#   各銘柄の評価額＝数量×最新終値。過去の各営業日について Σ(評価額×その日の騰落率) を口座の評価額（現金込み）で割り、
#   その分布の下位5%の損失を VaR95% とする。現金は動かない扱い。値が無い日はその銘柄の騰落を0とする。
#   終値は analytics.price_daily_latest（全上場の日次・分割調整済み）。前日比 0.6倍未満／1.7倍超の日は分割の調整漏れとみて除く。
# 【出力】
#   stock_data/paper/var_positions.json（board の risk_data.py が読む）。BigQuery は1日1本（保有銘柄だけを読む）。
# ==============================================================
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

MIN_DAYS = 60          # これより少ない共通日数では VaR を出さない
LOOKBACK_DAYS = 400    # 約1年（営業日で約250日）


def daily_returns(closes: list) -> dict:
    """[(日付, 終値)] → {日付: 前日比騰落率}。分割の調整漏れらしい日（0.6倍未満・1.7倍超）は除く（純関数）。"""
    out = {}
    s = sorted((d, c) for d, c in closes if c and c > 0)
    for (d0, c0), (d1, c1) in zip(s, s[1:]):
        r = c1 / c0
        if 0.6 <= r <= 1.7:
            out[d1] = r - 1
    return out


def portfolio_var(positions: dict, cash: float, closes: dict, conf: float = 0.95, min_days: int = MIN_DAYS) -> dict:
    """positions={ticker: 数量}、closes={ticker: [(日付, 終値)]}。純関数。"""
    values, rets = {}, {}
    for t, qty in positions.items():
        c = sorted(closes.get(t) or [])
        if not c or not qty:
            continue
        values[t] = float(qty) * c[-1][1]
        rets[t] = daily_returns(c)
    gross = sum(values.values())
    equity = float(cash or 0) + gross
    if not positions:
        return {"var95": 0.0, "method": "cash_only", "n_days": 0, "gross": 0.0, "equity": equity, "positions": 0, "priced": 0}
    if equity <= 0 or not values:
        return {"var95": None, "method": "position_hist", "n_days": 0, "gross": gross, "equity": equity,
                "positions": len(positions), "priced": len(values), "reason": "保有銘柄の終値が取れない"}
    dates = sorted(set().union(*[set(r) for r in rets.values()]))
    pnl = [sum(values[t] * rets[t].get(d, 0.0) for t in values) / equity for d in dates]
    if len(pnl) < min_days:
        return {"var95": None, "method": "position_hist", "n_days": len(pnl), "gross": gross, "equity": equity,
                "positions": len(positions), "priced": len(values), "reason": f"終値の履歴が{len(pnl)}日（{min_days}日未満）"}
    pnl.sort()
    idx = max(0, int(len(pnl) * (1 - conf)) - 1)
    return {"var95": max(0.0, -pnl[idx]), "method": "position_hist", "n_days": len(pnl), "gross": gross, "equity": equity,
            "positions": len(positions), "priced": len(values),
            "exposure": gross / equity, "worst_day": -pnl[0]}


def fetch_closes(tickers: list, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """BigQuery から保有銘柄の日次終値（1クエリ）。"""
    if not tickers:
        return {}
    from google.cloud import bigquery
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    sql = f"""
      SELECT ticker, trade_date, close
      FROM `{client.project}.analytics.price_daily_latest`
      WHERE trade_date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL {int(lookback_days)} DAY)
        AND ticker IN UNNEST(@tickers) AND close > 0
    """
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("tickers", "STRING", sorted(set(tickers)))]))
    out: dict = {}
    for r in job.result():
        out.setdefault(r.ticker, []).append((r.trade_date.isoformat(), float(r.close)))
    return out


def run(cfg, accounts: dict, paper_dir: Path, closes: dict | None = None) -> dict:
    """全口座の VaR を計算して var_positions.json に書く。"""
    strategies = {sid: a for sid, a in (accounts or {}).items() if isinstance(a, dict) and not sid.startswith("_")}
    tickers = [t for a in strategies.values() for t, p in (a.get("positions") or {}).items() if (p or {}).get("qty")]
    if closes is None:
        closes = fetch_closes(tickers)
    out = {}
    for sid, a in strategies.items():
        pos = {t: (p or {}).get("qty", 0) for t, p in (a.get("positions") or {}).items() if (p or {}).get("qty")}
        out[sid] = portfolio_var(pos, a.get("cash", 0.0), closes)
    doc = {"date": date.today().isoformat(), "method_note": "保有銘柄×過去約1年の日次騰落（ヒストリカル・シミュレーション）",
           "strategies": out}
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "var_positions.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for v in out.values() if v.get("var95") is not None)
    print(f"  📉 [position_var] {ok}/{len(out)}口座で VaR95% を算出 → var_positions.json")
    return doc
