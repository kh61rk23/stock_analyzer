# ==============================================================
# monthly_strategy_review.py — 月次戦略レビュー（Wave R3-4 / E12）
# ==============================================================
# 【役割】月初に、各サブシステムの成績を1本のレビューMDに集約する。
#   集約元（要件定義書 §R3-4）:
#     ① self_evaluator（performance_log: スコア帯別的中率）
#     ② E4レポート（insights/signal_culling_YYYYMM.md の有無・提案件数）
#     ③ 判断台帳の当月成績（E2・40_判断台帳.md）
#     ④ learning_audit_log（重み変更履歴 learning_audit_YYYY-MM.json）
#     ⑤ S2戦略別成績（paper/accounts.json の当月equity）
#   出力: B-investment/30_レポート/strategy_review_YYYYMM.md（vault不可視時は insights/）
# 【原則】LLM不使用・テンプレ整形のみ（費用ゼロ）。欠損は「データなし」表示。
# 使い方: python3 monthly_strategy_review.py [YYYYMM]
# ==============================================================

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def _score_band_table(perf_log: dict) -> list[str]:
    """performance_log からスコア帯別的中率を表化（形式差異に頑健）。"""
    bands = (perf_log or {}).get("score_bands") or (perf_log or {}).get("bands")
    if not isinstance(bands, dict) or not bands:
        return ["- スコア帯別的中率: データなし"]
    out = ["| スコア帯 | 件数 | 的中率 | 平均α |", "|---|---|---|---|"]
    for band, v in sorted(bands.items()):
        if not isinstance(v, dict):
            continue
        n = v.get("n", "-")
        hr = v.get("hit_rate")
        a = v.get("avg_alpha")
        out.append(f"| {band} | {n} | "
                   f"{f'{hr*100:.0f}%' if isinstance(hr,(int,float)) else '-'} | "
                   f"{f'{a:+.1f}%' if isinstance(a,(int,float)) else '-'} |")
    return out if len(out) > 2 else ["- スコア帯別的中率: データなし"]


def _paper_month_table(accounts: dict, yyyymm: str) -> list[str]:
    """paper accounts の当月成績（戦略別）。"""
    ym = f"{yyyymm[:4]}-{yyyymm[4:]}"
    rows = ["| 戦略 | 月初equity | 月末equity | 月次 |", "|---|---|---|---|"]
    found = False
    for sid, acc in sorted((accounts or {}).items()):
        if sid.startswith("_") or not isinstance(acc, dict):
            continue
        hist = [h for h in acc.get("equity_history", [])
                if str(h.get("date", "")).startswith(ym)]
        if not hist:
            continue
        found = True
        s, e = hist[0]["equity"], hist[-1]["equity"]
        pct = (e / s - 1) if s else 0.0
        rows.append(f"| {sid} | {s:,.0f} | {e:,.0f} | {pct:+.2%} |")
    return rows if found else ["- S2戦略別成績: データなし（ペーパー未稼働）"]


def _count_ledger_month(ledger_text: str, yyyymm: str) -> int:
    """判断台帳の当月エントリ数（YYYY-MM 行のカウント）。"""
    if not ledger_text:
        return 0
    ym = f"{yyyymm[:4]}-{yyyymm[4:]}"
    return len(re.findall(re.escape(ym) + r"-\d{2}", ledger_text))


def build_review(sources: dict, yyyymm: str) -> str:
    """全ソース dict からレビューMDを生成（純粋関数）。"""
    ym = f"{yyyymm[:4]}-{yyyymm[4:]}"
    lines = [f"# 月次戦略レビュー {ym}（E12）",
             "",
             "> LLM不使用・各サブシステムのキャッシュをテンプレ整形。"
             "数値の一次情報は各元ファイルを正本とする。",
             "",
             "## ① スコア帯別的中率（self_evaluator）"]
    lines += _score_band_table(sources.get("performance_log", {}))

    lines += ["", "## ② シグナル淘汰（E4）"]
    e4 = sources.get("signal_culling")
    if e4:
        lines.append(f"- signal_culling_{yyyymm}.md: 提案 {e4.get('proposals','?')}件"
                     f" / 判定不能 {e4.get('undecidable','?')}件")
    else:
        lines.append("- E4レポート: データなし（当月未生成 or N<60で判定不能）")

    lines += ["", "## ③ 審議台帳の当月成績（E2）",
              f"- 当月の審議/記帳エントリ: {sources.get('ledger_count', 0)}件"]

    lines += ["", "## ④ 重み変更履歴（learning_audit_log）"]
    la = sources.get("learning_audit")
    changes = (la or {}).get("changes") if isinstance(la, dict) else None
    if changes:
        lines.append(f"- 当月の重み変更: {len(changes)}件")
        for c in changes[:8]:
            lines.append(f"  - {c.get('rule','?')}: "
                         f"{c.get('old_weight','?')} → {c.get('new_weight','?')}")
    else:
        lines.append("- 重み変更履歴: データなし")

    lines += ["", "## ⑤ S2戦略別成績（ペーパー）"]
    lines += _paper_month_table(sources.get("paper_accounts", {}), yyyymm)

    lines += ["", "---",
              "## 次月への申し送り（健太記入欄）",
              "- [ ] 承認が必要な提案（E4淘汰 / S11配分 / ML適用）の要否",
              "- [ ] 戦略の停止/再開判断（S2成績・M2/M3マイルストーン）",
              "- [ ] DD状態（E9）と縮退モードの整合確認"]
    return "\n".join(lines) + "\n"


# ── I/O層 ────────────────────────────────────────────────────

def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def gather_sources(cfg, yyyymm: str) -> dict:
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    paper_dir = Path(getattr(cfg, "PAPER_LEDGER_PATH", "stock_data/paper"))
    vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", "")) if cfg else Path("")

    sources = {
        "performance_log": _load_json(data_dir / "performance_log.json"),
        "learning_audit": _load_json(
            data_dir / f"learning_audit_{yyyymm[:4]}-{yyyymm[4:]}.json"),
        "paper_accounts": _load_json(paper_dir / "accounts.json"),
    }
    # E4レポートの存在＋簡易件数
    insight_dir = Path(getattr(cfg, "INSIGHT_DIR", REPO / "stock_data" / "insights"))
    e4 = insight_dir / f"signal_culling_{yyyymm}.md"
    if e4.exists():
        txt = e4.read_text(encoding="utf-8")
        m = re.search(r"提案あり[:：]\s*(\d+)件.*判定不能[:：]\s*(\d+)件", txt)
        sources["signal_culling"] = {
            "proposals": m.group(1) if m else "?",
            "undecidable": m.group(2) if m else "?"}
    # 判断台帳の当月件数
    ledger = vault / "40_判断台帳.md"
    try:
        sources["ledger_count"] = _count_ledger_month(
            ledger.read_text(encoding="utf-8"), yyyymm)
    except Exception:
        sources["ledger_count"] = 0
    return sources


def main():
    yyyymm = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m")
    try:
        import config as cfg
    except Exception:
        cfg = None
    sources = gather_sources(cfg, yyyymm)
    md = build_review(sources, yyyymm)

    vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", "")) if cfg else Path("")
    if vault and vault.exists():
        out = vault / "30_レポート" / f"strategy_review_{yyyymm}.md"
    else:
        insight_dir = Path(getattr(cfg, "INSIGHT_DIR", REPO / "stock_data" / "insights"))
        out = insight_dir / f"strategy_review_{yyyymm}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"✅ 月次戦略レビュー出力: {out}")


if __name__ == "__main__":
    main()
