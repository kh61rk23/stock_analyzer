# ==============================================================
# paper/monthly_report.py — 戦略別月次成績レポート（Wave R1-2 / S2）
# ==============================================================
# 使い方（月初にローカルMacで・launchd週次ループと同居可）:
#   python3 paper/monthly_report.py [YYYYMM]
# 出力: B-investment/30_レポート/paper_YYYYMM.md（vault不可視時は insights/）
# LLM不使用・テンプレ整形のみ（費用ゼロ原則）。
# ==============================================================

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from paper.paper_broker import load_accounts   # noqa: E402


def judge_promotion(acc: dict, cfg=None) -> dict:
    """実弾昇格M2基準の判定（2026-07-25 バッチ6で実装）。

    基準（config.py BLOCK 45）:
      取引数 >= PROMOTION_MIN_TRADES（既定30）
      PF     >= PROMOTION_MIN_PF（既定1.3）
      bootstrap p < PROMOTION_MAX_P（既定0.10）
    3つを全て満たしたときのみ「昇格候補」。最終判断は健太（D-01）。

    従来この3定数は誰にも読まれず、レポートに基準を文言で書くだけで
    判定が実装されていなかった。

    Returns:
        {"eligible": bool, "trades": int, "pf": float|None,
         "p": float|None, "reasons": [未達理由,...]}
    """
    import sys as _s
    from pathlib import Path as _P
    _repo = str(_P(__file__).resolve().parent.parent)
    if _repo not in _s.path:
        _s.path.insert(0, _repo)
    from allocation_meta import bootstrap_p_value, calc_profit_factor

    min_trades = int(getattr(cfg, "PROMOTION_MIN_TRADES", 30))
    min_pf     = float(getattr(cfg, "PROMOTION_MIN_PF", 1.3))
    max_p      = float(getattr(cfg, "PROMOTION_MAX_P", 0.10))

    # 取引数は約定イベント数（fills）。無ければ equity_history から推定しない
    # （水増しになるため）。
    trades = int(acc.get("total_fills", 0) or 0)

    # 月次リターン系列 → PF / bootstrap p
    by_month = {}
    for h in acc.get("equity_history", []):
        by_month[str(h.get("date", ""))[:7]] = h.get("equity")
    months = sorted(by_month)
    eqs = [by_month[m] for m in months]
    rets = [eqs[i] / eqs[i - 1] - 1 for i in range(1, len(eqs)) if eqs[i - 1]]
    # 昇格判定は全期間の実績で見る（淘汰の6ヶ月窓とは別基準）
    pf = calc_profit_factor(rets, months=0)
    p  = bootstrap_p_value(rets, months=0)

    # calc_profit_factor は「損失月なし」を None で返す（＝負けていないので
    # 淘汰判定の対象外という意味）。昇格判定では逆に最良ケースなので、
    # リターン実績があって損失月ゼロなら PF 基準は満たしたものとして扱う。
    _no_loss = bool(rets) and all(r >= 0 for r in rets)
    pf_ok = (pf is not None and pf >= min_pf) or (pf is None and _no_loss)

    reasons = []
    if trades < min_trades:
        reasons.append(f"取引数{trades}<{min_trades}")
    if not pf_ok:
        reasons.append(f"PF{'—' if pf is None else f'{pf:.2f}'}<{min_pf}")
    if p is None or p >= max_p:
        reasons.append(f"p{'—' if p is None else f'{p:.3f}'}>={max_p}")
    return {"eligible": not reasons, "trades": trades,
            "pf": pf, "p": p, "reasons": reasons}


def build_report(accounts: dict, yyyymm: str, cfg=None) -> str:
    """accounts.json から月次レポートMDを生成（純粋関数・pytest対象）。"""
    ym = f"{yyyymm[:4]}-{yyyymm[4:]}"
    lines = [f"# ペーパートレード月次成績 {ym}",
             "",
             "> S2仮想口座（実発注なし=D-01）。実弾昇格判定はM2基準"
             "（取引数>=30・PF>=1.3・bootstrap p<0.10・健太承認）。",
             "",
             "| 戦略 | 月初equity | 月末equity | 月次損益 | 月次リターン | 記録日数 |",
             "|---|---|---|---|---|---|"]
    for sid, acc in sorted(accounts.items()):
        if sid.startswith("_") or not isinstance(acc, dict):
            continue
        hist = [h for h in acc.get("equity_history", [])
                if h["date"][:7] == ym]
        if not hist:
            lines.append(f"| {sid} | - | - | - | - | 0 |")
            continue
        start, end = hist[0]["equity"], hist[-1]["equity"]
        pnl = end - start
        ret = pnl / start if start > 0 else 0.0
        lines.append(f"| {sid} | {start:,.0f} | {end:,.0f} | {pnl:+,.0f} | "
                     f"{ret:+.2%} | {len(hist)} |")
    # ── 実弾昇格M2判定（2026-07-25 バッチ6で実装）─────────────
    lines += ["", "## 実弾昇格M2判定",
              "", "> 3基準を全て満たすと「昇格候補」。**最終判断は健太**（D-01）。",
              "", "| 戦略 | 取引数 | PF | bootstrap p | 判定 | 未達項目 |",
              "|---|---|---|---|---|---|"]
    for sid, acc in sorted(accounts.items()):
        if sid.startswith("_") or not isinstance(acc, dict):
            continue
        j = judge_promotion(acc, cfg)
        pf_s = "—" if j["pf"] is None else f"{j['pf']:.2f}"
        p_s  = "—" if j["p"] is None else f"{j['p']:.3f}"
        mark = "🎯 昇格候補" if j["eligible"] else "—"
        lines.append(f"| {sid} | {j['trades']} | {pf_s} | {p_s} | {mark} | "
                     f"{'／'.join(j['reasons']) or '-'} |")

    lines += ["", "## 保有ポジション（月末時点）",
              "| 戦略 | ticker | qty | avg_cost |", "|---|---|---|---|"]
    for sid, acc in sorted(accounts.items()):
        if sid.startswith("_") or not isinstance(acc, dict):
            continue
        for t, p in sorted(acc.get("positions", {}).items()):
            lines.append(f"| {sid} | {t} | {p['qty']:.0f} | {p['avg_cost']:,.1f} |")
    return "\n".join(lines) + "\n"


def main():
    yyyymm = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m")
    try:
        import config as cfg
    except Exception:
        cfg = None
    accounts = load_accounts(cfg)
    md = build_report(accounts, yyyymm, cfg)

    vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", "")) if cfg else Path("")
    if vault and vault.exists():
        out = vault / "30_レポート" / f"paper_{yyyymm}.md"
    else:
        insight_dir = Path(
            getattr(cfg, "INSIGHT_DIR", _REPO / "stock_data" / "insights")
        )
        out = insight_dir / f"paper_{yyyymm}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"✅ 月次レポート出力: {out}")


if __name__ == "__main__":
    main()
