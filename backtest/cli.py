# ==============================================================
# backtest/cli.py — バックテスト実行CLI（Wave R1-1 / S1）
# ==============================================================
# 使い方（リポジトリ直下から）:
#   python3 -m backtest.cli --strategy bh_benchmark --start 2024-07-01 --end 2026-07-01
#   python3 -m backtest.cli --strategy screener_topN --start 2025-01-01 --end 2026-07-01
#   python3 -m backtest.cli --strategy bh_benchmark --verify-benchmark   # 受入条件①
# レポート出力先: insights/backtest/{strategy}_{end}.md
# ネットワーク必須（yfinance）。GitHub Actions/ローカルMacでの実行を想定。
# ==============================================================

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from backtest import metrics as M                    # noqa: E402
from backtest import validation as V                 # noqa: E402
from backtest.data_feed import YFinanceFeed          # noqa: E402
from backtest.engine import Engine                   # noqa: E402
from backtest.strategies import (BuyAndHoldBenchmark, PeadPlaybook,  # noqa: E402
                                 ScreenerTopN)


def _load_cfg():
    try:
        import config
        return config
    except Exception:
        return None


def build_strategy(name: str, cfg):
    if name == "bh_benchmark":
        return BuyAndHoldBenchmark(getattr(cfg, "BT_BENCHMARK_TICKER", "1306.T"))
    if name == "screener_topN":
        sector_map = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {}) or {}
        return ScreenerTopN(sector_map, top_n=5)
    if name == "pead_playbook":
        return PeadPlaybook()
    raise SystemExit(f"未知の戦略: {name}（bh_benchmark/screener_topN/pead_playbook）")


def render_report(strategy_id: str, summary: dict, val: dict,
                  start: str, end: str) -> str:
    lines = [f"# バックテストレポート: {strategy_id}",
             f"- 期間: {start} 〜 {end}",
             f"- 生成: {date.today().isoformat()} / backtest v1（Wave R1-1）",
             "",
             "## 成績サマリ",
             "| 指標 | 値 |", "|---|---|"]
    label = {"cagr": "CAGR", "annual_vol": "年率ボラ", "sharpe": "Sharpe(rf=0)",
             "max_drawdown": "最大DD", "win_rate": "勝率",
             "profit_factor": "Profit Factor", "n_trades": "取引数",
             "n_closed": "決済数", "exposure": "期間エクスポージャ",
             "taxes_paid": "課税額（年内通算後）", "final_equity": "最終資産"}
    for k, v in summary.items():
        lines.append(f"| {label.get(k, k)} | {v} |")
    lines += ["", "## walk-forward（fold別OOS成績）",
              "| fold | OOS期間 | OOSリターン | OOS Sharpe | OOS最大DD | 日数 |",
              "|---|---|---|---|---|---|"]
    for f in val["folds"]:
        lines.append(f"| {f['fold']} | {f['oos_start']}〜{f['oos_end']} | "
                     f"{f['oos_return']:+.2%} | {f['oos_sharpe']} | "
                     f"{f['oos_max_dd']:.2%} | {f['n_days']} |")
    lines += ["", "## 有意性検定（circular block bootstrap）",
              f"- OOS日次超過リターン（対ベンチマーク）の片側p値: **{val['bootstrap_p']}** "
              f"(N={val['n_oos_days']}日)"]
    if not val["statistically_supported"]:
        lines.append("- <span style=\"color:red\">**⚠️ 統計的裏付けなし（p>=0.10）**</span>")
    else:
        lines.append("- ✅ 統計的に有意（p<0.10）")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="統合バックテスト基盤（S1）")
    ap.add_argument("--strategy", default="bh_benchmark")
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--capital", type=float, default=2_000_000)
    ap.add_argument("--account", default="特定", choices=["特定", "NISA"])
    ap.add_argument("--verify-benchmark", action="store_true",
                    help="受入条件①: bh_benchmark と yfinance 1306累積リターンの誤差±0.5%%検証")
    args = ap.parse_args()

    cfg = _load_cfg()
    strategy = build_strategy(args.strategy, cfg)
    bench_ticker = getattr(cfg, "BT_BENCHMARK_TICKER", "1306.T")
    tickers = sorted(set((strategy.universe or []) + [bench_ticker]))

    print(f"📥 価格取得: {len(tickers)}銘柄 {args.start}〜{args.end}")
    feed = YFinanceFeed(tickers, args.start, args.end, cfg)
    if not feed.calendar():
        raise SystemExit("❌ 価格データが取得できません（ネットワーク/キャッシュなし）")

    result = Engine(feed, strategy, args.capital, args.account, cfg=cfg).run()
    bench = Engine(feed, BuyAndHoldBenchmark(bench_ticker), args.capital,
                   "NISA", cfg=cfg).run()
    summary = M.summarize(result)
    val = V.validate(result, bench, feed.calendar(), cfg)

    if args.verify_benchmark:
        rows = feed.history(bench_ticker, args.end, 100000)
        raw = rows[-1]["close"] / rows[0]["close"] - 1
        bt = bench.final_equity / args.capital - 1
        diff = abs(bt - raw)
        print(f"受入条件①: エンジン{bt:+.4%} vs 現物{raw:+.4%} → 誤差{diff:.4%} "
              f"{'✅ ±0.5%以内' if diff <= 0.005 else '❌ 0.5%超'}")

    out_dir = REPO / "insights" / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{strategy.strategy_id}_{args.end}.md"
    out.write_text(render_report(strategy.strategy_id, summary, val,
                                 args.start, args.end), encoding="utf-8")
    print(f"✅ レポート出力: {out}")
    for k, v in summary.items():
        print(f"   {k}: {v}")
    print(f"   bootstrap p={val['bootstrap_p']} "
          f"{'（統計的裏付けなし）' if not val['statistically_supported'] else ''}")


if __name__ == "__main__":
    main()
