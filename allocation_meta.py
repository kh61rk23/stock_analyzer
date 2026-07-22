# ==============================================================
# allocation_meta.py — マルチ戦略資金配分メタレイヤ（Wave R4-4 / S11）
# ==============================================================
# 【役割】S2ペーパー/実弾の戦略別月次リターン系列から、分数ケリー×
#   リスクパリティで資金配分を提案する（月次）。制約=カジノ枠。
#   PF低迷戦略は淘汰提案（retired）。
# 【設計正本】要件定義書_統合改修_20260705.md §R4-4（確定式・変更禁止）:
#   f_s(ケリー)=μ_s/σ_s²  →  f_s'=0.25×f_s（負なら0）
#   リスクパリティ補正: w_s = f_s'/σ_s を正規化
#   制約: Σ(カジノ系w)×総資産 ≤ カジノ枠(min(15%,200万))・コア長期枠は不可侵
#   淘汰: 直近6ヶ月 PF<1.0 かつ bootstrap p>0.5 → 配分0
# 【原則】出力は提案のみ。**健太承認後に** config（PAPER_STRATEGIES枠値）を変更。
# 使い方: python3 allocation_meta.py [YYYYMM]
# ==============================================================

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def _mean(xs: list) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _variance(xs: list) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def kelly_fraction(returns: list, kelly_frac: float = 0.25) -> dict:
    """
    月次リターン系列 → 分数ケリー f_s' と σ_s。
    f_s = μ/σ² → f_s' = kelly_frac×f_s（負は0）。
    Returns: {"mu","var","sigma","kelly_raw","kelly_frac"}
    """
    mu = _mean(returns)
    var = _variance(returns)
    sigma = var ** 0.5
    if var <= 0:
        return {"mu": mu, "var": var, "sigma": sigma,
                "kelly_raw": 0.0, "kelly_frac": 0.0}
    f_raw = mu / var
    f_frac = max(0.0, kelly_frac * f_raw)
    return {"mu": round(mu, 5), "var": round(var, 7), "sigma": round(sigma, 5),
            "kelly_raw": round(f_raw, 4), "kelly_frac": round(f_frac, 4)}


def risk_parity_weights(strategy_stats: dict) -> dict:
    """
    w_s = f_s'/σ_s を正規化（Σw=1）。σ=0 or f'=0 の戦略は w=0。
    strategy_stats: {sid: {"kelly_frac":.., "sigma":..}}
    Returns: {sid: weight}
    """
    raw = {}
    for sid, st in strategy_stats.items():
        sigma = st.get("sigma", 0)
        f = st.get("kelly_frac", 0)
        raw[sid] = (f / sigma) if (sigma > 0 and f > 0) else 0.0
    total = sum(raw.values())
    if total <= 0:
        return {sid: 0.0 for sid in strategy_stats}
    return {sid: round(v / total, 4) for sid, v in raw.items()}


def apply_casino_constraint(weights: dict, total_assets: float,
                            casino_budget: float) -> dict:
    """
    カジノ系戦略の配分合計額がカジノ枠を超えないよう比例縮小。
    Returns: {sid: 配分額(円)}
    """
    alloc = {sid: w * casino_budget for sid, w in weights.items()}
    total = sum(alloc.values())
    if total > casino_budget and total > 0:
        scale = casino_budget / total
        alloc = {sid: round(v * scale, 0) for sid, v in alloc.items()}
    else:
        alloc = {sid: round(v, 0) for sid, v in alloc.items()}
    return alloc


def cull_check(pf: float | None, bootstrap_p: float | None,
               pf_th: float = 1.0, p_th: float = 0.5) -> bool:
    """淘汰判定: PF<1.0 かつ bootstrap p>0.5 → True（retired提案）。"""
    if pf is None or bootstrap_p is None:
        return False
    return pf < pf_th and bootstrap_p > p_th


def build_allocation(strategy_returns: dict, strategy_pf: dict,
                     strategy_p: dict, total_assets: float,
                     casino_budget: float, cfg=None) -> dict:
    """
    全戦略の配分提案を組み立てる（純粋関数）。
    strategy_returns: {sid: [月次リターン,...]}
    strategy_pf/strategy_p: {sid: PF} / {sid: bootstrap p}
    Returns: {"stats","weights","allocations","culled","insufficient","reason"}
    """
    kelly_frac = getattr(cfg, "KELLY_FRACTION", 0.25)
    min_months = getattr(cfg, "ALLOC_MIN_HISTORY_M", 3)
    pf_th = getattr(cfg, "CULL_PF_TH", 1.0)
    p_th = getattr(cfg, "CULL_P_TH", 0.5)

    stats, insufficient, culled = {}, [], []
    for sid, rets in strategy_returns.items():
        if len(rets) < min_months:
            insufficient.append(sid)
            continue
        if cull_check(strategy_pf.get(sid), strategy_p.get(sid), pf_th, p_th):
            culled.append(sid)
            continue
        stats[sid] = kelly_fraction(rets, kelly_frac)

    weights = risk_parity_weights(stats)
    allocations = apply_casino_constraint(weights, total_assets, casino_budget)
    return {
        "stats": stats, "weights": weights, "allocations": allocations,
        "culled": culled, "insufficient": insufficient,
        "casino_budget": casino_budget, "min_months": min_months,
        "reason": (f"{len(stats)}戦略に配分 / 淘汰{len(culled)} / "
                   f"データ不足{len(insufficient)}"),
    }


# ── I/O層 ────────────────────────────────────────────────────

def _load_paper_returns(cfg) -> tuple[dict, dict, dict]:
    """paper/accounts.json の equity_history から月次リターン系列を復元。"""
    paper_dir = Path(getattr(cfg, "PAPER_LEDGER_PATH", "stock_data/paper"))
    p = paper_dir / "accounts.json"
    returns, pf, pval = {}, {}, {}
    if not p.exists():
        return returns, pf, pval
    try:
        accounts = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return returns, pf, pval
    for sid, acc in accounts.items():
        if sid.startswith("_") or not isinstance(acc, dict):
            continue
        # 月末equityの系列 → 月次リターン
        by_month = {}
        for h in acc.get("equity_history", []):
            ym = str(h.get("date", ""))[:7]
            by_month[ym] = h.get("equity")
        months = sorted(by_month)
        eqs = [by_month[m] for m in months]
        rets = [eqs[i] / eqs[i-1] - 1 for i in range(1, len(eqs))
                if eqs[i-1]]
        if rets:
            returns[sid] = rets
        # PF/p は簡易（win/loss比・未算出は None）
        pf[sid] = None
        pval[sid] = None
    return returns, pf, pval


def _casino_budget(cfg, total_assets: float) -> float:
    pct = getattr(cfg, "CASINO_BUDGET_PCT", 15.0) / 100.0
    cap = getattr(cfg, "CASINO_BUDGET_CAP_YEN", 2_000_000)
    return min(total_assets * pct, cap)


def _total_assets(cfg) -> float:
    import csv
    base = Path(getattr(cfg, "PROJECT_DIR", "."))
    path = Path(getattr(cfg, "PORTFOLIO_HOLDINGS_PATH",
                        base / "data" / "portfolio_holdings.csv"))
    if not path.exists():
        return 0.0
    total = 0.0
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                total += float(r.get("quantity", 0) or 0) * \
                    float(r.get("avg_cost_jpy", 0) or 0)
    except Exception:
        return 0.0
    return total


def build_report(plan: dict, yyyymm: str) -> str:
    lines = [f"# マルチ戦略資金配分 提案書 {yyyymm[:4]}-{yyyymm[4:]}（S11）",
             "",
             "> 分数ケリー(0.25)×リスクパリティ。**健太承認後に** config "
             "PAPER_STRATEGIES の枠値を変更。コア長期枠は本レイヤ対象外（不可侵）。",
             f"> カジノ枠: ¥{plan.get('casino_budget',0):,.0f} / {plan.get('reason','')}",
             "",
             "| 戦略 | μ(月次) | σ | 分数ケリー | ウェイト | 配分額 |",
             "|---|---|---|---|---|---|"]
    stats = plan.get("stats", {})
    weights = plan.get("weights", {})
    allocs = plan.get("allocations", {})
    for sid in sorted(stats):
        st = stats[sid]
        lines.append(f"| {sid} | {st['mu']:+.2%} | {st['sigma']:.2%} | "
                     f"{st['kelly_frac']:.3f} | {weights.get(sid,0):.1%} | "
                     f"¥{allocs.get(sid,0):,.0f} |")
    if not stats:
        lines.append("| （配分対象なし） | | | | | |")
    if plan.get("culled"):
        lines += ["", f"## 淘汰提案（retired）: {', '.join(plan['culled'])}",
                  "- 直近6ヶ月 PF<1.0 かつ bootstrap p>0.5 → 配分0を提案"]
    if plan.get("insufficient"):
        lines += ["", f"## データ不足（最低{plan.get('min_months', 3)}ヶ月未満・保留）: "
                  f"{', '.join(plan['insufficient'])}"]
    return "\n".join(lines) + "\n"


def _yyyymm(value: str) -> str:
    """YYYYMMを検証し、月次成果物の不正なファイル名生成を防ぐ。"""
    if not re.fullmatch(r"\d{6}", value):
        raise argparse.ArgumentTypeError("対象月はYYYYMM（例: 202607）で指定してください")
    month = int(value[4:])
    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError("対象月のMMは01〜12で指定してください")
    return value


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="S2ペーパー成績から月次の資金配分提案を作成"
    )
    parser.add_argument(
        "yyyymm",
        nargs="?",
        type=_yyyymm,
        default=date.today().strftime("%Y%m"),
        help="対象月（YYYYMM、省略時は当月）",
    )
    args = parser.parse_args(argv)
    yyyymm = args.yyyymm
    try:
        import config as cfg
    except Exception:
        cfg = None
    returns, pf, pval = _load_paper_returns(cfg)
    if not returns:
        print(
            "⏩ S2月次リターンなし → 資金配分提案をスキップ"
            "（accounts.jsonに3ヶ月以上蓄積後に実行）"
        )
        return
    total = _total_assets(cfg)
    budget = _casino_budget(cfg, total)
    plan = build_allocation(returns, pf, pval, total, budget, cfg)
    md = build_report(plan, yyyymm)
    insight_dir = Path(getattr(cfg, "INSIGHT_DIR", REPO / "stock_data" / "insights"))
    out = insight_dir / f"allocation_proposal_{yyyymm}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"✅ 資金配分提案出力: {out}（{plan['reason']}）")


if __name__ == "__main__":
    main()
