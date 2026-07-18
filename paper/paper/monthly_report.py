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


def build_report(accounts: dict, yyyymm: str) -> str:
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
    md = build_report(accounts, yyyymm)

    vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", "")) if cfg else Path("")
    if vault and vault.exists():
        out = vault / "30_レポート" / f"paper_{yyyymm}.md"
    else:
        out = _REPO / "insights" / f"paper_{yyyymm}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"✅ 月次レポート出力: {out}")


if __name__ == "__main__":
    main()
