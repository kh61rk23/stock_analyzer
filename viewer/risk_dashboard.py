# ==============================================================
# viewer/risk_dashboard.py — リスク管制ダッシュボード（Wave R1-3 / S9）
# ==============================================================
# 起動（ローカルMacのみ・要 `pip install streamlit`）:
#   streamlit run viewer/risk_dashboard.py
#
# 設計正本: 要件定義書_統合改修_20260705.md §R1-3
# パネル: ①資産二層分離（コア/カジノ枠使用率） ②VaR95%・現在DD
#         ③E1売却ルール接近度 ④S2戦略別equity curve
#         ⑤注文票pending一覧 ⑥module_ledger健全性サマリ
#
# 【読取専用の原則】既存JSON/CSVの読取のみ。書込みは一切しない。
# 唯一の例外: 「新規注文票発行停止」トグル → stock_data/orders/halt_flag.json
# （各戦略は注文票生成前にこのフラグを確認する契約。exit_rule_engine 配線済み）
# ==============================================================

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    import streamlit as st
except ImportError:
    print("streamlit が未導入です: pip install streamlit")
    sys.exit(1)


# ── データ読取（すべて読取専用） ─────────────────────────────

def _load_cfg():
    try:
        import config
        return config
    except Exception:
        return None


def _read_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default if default is not None else {}


def _read_jsonl(path: Path) -> list:
    rows = []
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return rows


def _read_csv_rows(path: Path) -> list:
    try:
        if path.exists():
            with open(path, newline="", encoding="utf-8-sig") as f:
                return list(csv.DictReader(f))
    except Exception:
        pass
    return []


def _paths(cfg):
    base = Path(getattr(cfg, "DRIVE_BASE", REPO)) if cfg else REPO
    # リポジトリ直下運用（GitHub Actions/ローカル共通）: stock_data/ 直下
    root = base / "stock_data" if (base / "stock_data").exists() else Path("stock_data")
    return {
        "orders": root / "orders",
        "paper": root / "paper",
        "dd_state": root / "dd_state.json",
        "ledger": root / "logs" / "module_ledger.json",
        "holdings": Path(getattr(cfg, "PORTFOLIO_HOLDINGS_PATH",
                                 REPO / "data" / "portfolio_holdings.csv"))
        if cfg else REPO / "data" / "portfolio_holdings.csv",
        "sd_root": root,
    }


def casino_budget(total_assets: float, cfg) -> float:
    pct = getattr(cfg, "CASINO_BUDGET_PCT", 15.0) / 100.0
    cap = getattr(cfg, "CASINO_BUDGET_CAP_YEN", 2_000_000)
    return min(total_assets * pct, cap)


def var95_from_curve(equity_history: list) -> float:
    """equity履歴から日次リターンのヒストリカルVaR95%を計算。"""
    eq = [h["equity"] for h in equity_history if h.get("equity")]
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    if len(rets) < 20:
        return float("nan")
    rets.sort()
    idx = max(0, int(len(rets) * 0.05) - 1)
    return -rets[idx]


# ── UI ───────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="リスク管制", page_icon="🛡️", layout="wide")
    st.title("🛡️ リスク管制ダッシュボード（S9）")
    st.caption("読取専用。唯一の書込みは「新規注文票発行停止」トグル（halt_flag.json）のみ。"
               "発注は常に健太が手動（D-01）。")

    cfg = _load_cfg()
    P = _paths(cfg)

    # ── 停止トグル（唯一の書込み） ──
    halt_path = P["orders"] / "halt_flag.json"
    halt = _read_json(halt_path, {})
    halted = bool(halt.get("halted", False))
    new_halted = st.sidebar.toggle("🚨 新規注文票の発行を停止", value=halted)
    if new_halted != halted:
        P["orders"].mkdir(parents=True, exist_ok=True)
        halt_path.write_text(json.dumps(
            {"halted": new_halted,
             "changed_at": datetime.now().isoformat(timespec="seconds"),
             "changed_by": "risk_dashboard"}, ensure_ascii=False),
            encoding="utf-8")
        st.sidebar.success(f"halt_flag = {new_halted} を保存しました")
    if new_halted:
        st.error("⛔ 新規注文票の発行は停止中です（halt_flag=true）")

    col1, col2 = st.columns(2)

    # ── ① 資産二層分離 ──
    with col1:
        st.subheader("① 資産二層分離（コア/カジノ枠）")
        holdings = _read_csv_rows(P["holdings"])
        total = 0.0
        for r in holdings:
            try:
                total += float(r.get("qty", 0)) * float(r.get("avg_cost", 0))
            except Exception:
                continue
        budget = casino_budget(total, cfg) if total > 0 else 0.0
        paper_accounts = _read_json(P["paper"] / "accounts.json", {})
        casino_used = sum(
            sum(p["qty"] * p["avg_cost"] for p in acc.get("positions", {}).values())
            for sid, acc in paper_accounts.items()
            if isinstance(acc, dict) and not sid.startswith("_"))
        st.metric("総資産（簿価・holdings基準）", f"¥{total:,.0f}")
        c1, c2 = st.columns(2)
        c1.metric("カジノ枠上限 = min(15%, 200万)", f"¥{budget:,.0f}")
        c2.metric("カジノ枠使用（ペーパー含む・参考値）",
                  f"¥{casino_used:,.0f}",
                  f"{casino_used / budget * 100:.0f}%" if budget > 0 else "n/a")
        if not holdings:
            st.info("portfolio_holdings.csv が見つかりません")

    # ── ② VaR・DD状態 ──
    with col2:
        st.subheader("② ポートフォリオVaR95% / 現在DD（E9）")
        dd = _read_json(P["dd_state"], {})
        state = dd.get("state", "NORMAL（dd_controller未稼働=R3-3で実装）")
        st.metric("DD制御状態", str(state))
        if dd.get("dd") is not None:
            st.metric("現在ドローダウン", f"{float(dd['dd']):.1%}")
        curves = {sid: acc.get("equity_history", [])
                  for sid, acc in _read_json(P["paper"] / "accounts.json", {}).items()
                  if isinstance(acc, dict) and not sid.startswith("_")}
        var_rows = [(sid, var95_from_curve(h)) for sid, h in curves.items() if h]
        if var_rows:
            for sid, v in var_rows:
                st.write(f"- **{sid}** 日次VaR95%: "
                         + (f"{v:.2%}" if not math.isnan(v) else "データ不足(<20日)"))
        else:
            st.info("ペーパー口座の履歴がまだありません（S2稼働後に表示）")

    col3, col4 = st.columns(2)

    # ── ③ E1接近度 ──
    with col3:
        st.subheader("③ 売却ルール接近度（E1）")
        # exit_alerts はresultsキー（実行時）のため、注文票と併せ台帳から復元
        tickets = _read_jsonl(P["orders"] / "order_tickets.jsonl")
        exit_tickets = [t for t in tickets if t.get("strategy") == "exit_engine"]
        if exit_tickets:
            st.dataframe([{k: t.get(k) for k in
                           ("ticket_id", "ticker", "qty", "reason", "status")}
                          for t in exit_tickets[-20:]])
        else:
            st.info("出口エンジンの注文票はまだありません")

    # ── ④ S2 equity curve ──
    with col4:
        st.subheader("④ 戦略別 equity curve（S2）")
        if curves and any(curves.values()):
            chart_data = {}
            for sid, h in curves.items():
                for rec in h:
                    chart_data.setdefault(rec["date"], {})[sid] = rec["equity"]
            rows = [{"date": d, **vals} for d, vals in sorted(chart_data.items())]
            st.line_chart(rows, x="date")
        else:
            st.info("equity履歴なし（S2の日次稼働後に表示）")

    col5, col6 = st.columns(2)

    # ── ⑤ 注文票 pending ──
    with col5:
        st.subheader("⑤ 注文票 pending一覧")
        pend = [t for t in _read_jsonl(P["orders"] / "order_tickets.jsonl")
                if t.get("status") == "pending"]
        if pend:
            st.dataframe([{k: t.get(k) for k in
                           ("ticket_id", "created_at", "strategy", "ticker",
                            "side", "qty", "order_hint", "valid_until")}
                          for t in pend])
        else:
            st.info("pending の注文票はありません")

    # ── ⑥ module_ledger 健全性 ──
    with col6:
        st.subheader("⑥ システム健全性（module_ledger）")
        ledger = _read_json(P["ledger"], {})
        days = sorted(ledger.keys()) if isinstance(ledger, dict) else []
        if days:
            latest = ledger[days[-1]]
            if isinstance(latest, dict):
                fails = [m for m, rec in latest.items()
                         if isinstance(rec, dict) and rec.get("status") == "error"]
                st.metric(f"最終実行日 {days[-1]} の失敗モジュール", len(fails))
                if fails:
                    st.write("、".join(fails[:15]))
        else:
            st.info("module_ledger.json が見つかりません（ローカルに同期後に表示）")

    st.divider()
    st.caption(f"最終描画: {datetime.now().isoformat(timespec='seconds')} / "
               "Wave R1-3。書込みは halt_flag.json のみ（コードレビュー済み契約）。")


if __name__ == "__main__":
    main()
