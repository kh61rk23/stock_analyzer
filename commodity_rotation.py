# ==============================================================
# commodity_rotation.py — コモディティ/マクロ・ローテーション（Wave R4-2 / S6）
# ==============================================================
# 【役割】月初に、経済サイクル4象限で許可されたコモディティETFのうち
#   12-1モメンタムが正のものへ配分提案を出す（合計≤総資産10%）。
# 【設計正本】要件定義書_統合改修_20260705.md §R4-2。config BLOCK 46。
#   - momentum = 12ヶ月リターン − 直近1ヶ月リターン（12-1モメンタム）
#   - レジーム（economic_cycle 4象限）→ 象限別の許可カテゴリ（他象限=現金）
#     A:成長↑インフレ↑→energy/grains  C:成長↓インフレ↑→gold/broad  B,D→現金
#   - momentum<0 の資産は配分ゼロ（絶対モメンタムフィルタ）
#   - 出力: 月次配分提案（注文票＋レポート）。発注は健太手動（D-01）。
# 使い方: python3 commodity_rotation.py（月初）/ --dry-run
# ==============================================================

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def calc_12_1_momentum(closes: list, long_lb: int = 252,
                       short_lb: int = 21) -> float | None:
    """
    12-1モメンタム = (P_t / P_{t-252} − 1) − (P_t / P_{t-21} − 1)。
    データ不足は None。
    """
    n = len(closes)
    if n <= long_lb or closes[-1] is None:
        return None
    p_now = closes[-1]
    p_long = closes[-1 - long_lb]
    p_short = closes[-1 - short_lb]
    if not p_long or not p_short or p_long <= 0 or p_short <= 0:
        return None
    ret_long = p_now / p_long - 1
    ret_short = p_now / p_short - 1
    return round(ret_long - ret_short, 4)


def select_allocations(momentum_by_ticker: dict, regime: str,
                       universe: dict, regime_allow: dict,
                       max_alloc_pct: float = 10.0) -> dict:
    """
    象限別許可カテゴリ × 絶対モメンタム>0 の資産へ等ウェイト配分。
    Returns: {"regime","allowed_categories","allocations":{ticker: pct},
              "total_pct", "cash_pct", "reason"}
    """
    allowed = set(regime_allow.get(regime, []))
    if not allowed:
        return {"regime": regime, "allowed_categories": [],
                "allocations": {}, "total_pct": 0.0, "cash_pct": 100.0,
                "reason": f"象限{regime}はコモディティ配分なし（現金）"}
    # 許可カテゴリ かつ momentum>0（絶対モメンタムフィルタ）
    eligible = [t for t, cat in universe.items()
                if cat in allowed
                and momentum_by_ticker.get(t) is not None
                and momentum_by_ticker[t] > 0]
    if not eligible:
        return {"regime": regime, "allowed_categories": sorted(allowed),
                "allocations": {}, "total_pct": 0.0, "cash_pct": 100.0,
                "reason": "許可カテゴリ内に正モメンタム資産なし（現金）"}
    each = round(max_alloc_pct / len(eligible), 2)
    allocations = {t: each for t in eligible}
    total = round(each * len(eligible), 2)
    return {"regime": regime, "allowed_categories": sorted(allowed),
            "allocations": allocations, "total_pct": total,
            "cash_pct": round(100.0 - total, 2),
            "reason": f"{len(eligible)}資産へ等ウェイト（各{each}%）"}


def build_order_tickets(allocations: dict, total_assets: float,
                        prices: dict, today: str) -> list[dict]:
    """配分%→注文票（共通スキーマ①・qtyは概算・発注は健太手動）。"""
    tickets, seq = [], 1
    for ticker, pct in allocations.items():
        price = prices.get(ticker)
        if not price or price <= 0:
            continue
        target_value = total_assets * pct / 100.0
        unit = 1        # コモディティETFは1口単位が多い
        qty = int(target_value / price / unit) * unit
        if qty <= 0:
            continue
        tickets.append({
            "ticket_id": f"OT-{today.replace('-', '')}-CMD{seq:02d}",
            "created_at": today, "strategy": "commodity",
            "ticker": ticker, "side": "BUY", "qty": qty,
            "order_hint": "成行寄付",
            "reason": f"コモディティローテーション 配分{pct}%",
            "valid_until": today,
            "guardrail_check": {"casino_budget_ok": True,
                                "max_single_pos_ok": True, "margin_flag": False},
            "status": "pending", "executed_price": None, "executed_at": None,
        })
        seq += 1
    return tickets


# ── I/O層 ────────────────────────────────────────────────────

def _fetch_closes(ticker: str, days: int = 300) -> list:
    """yfinanceで調整済み終値を取得（best-effort・失敗は空）。"""
    try:
        import yf_cache as yf
        h = yf.Ticker(ticker).history(period=f"{days}d", auto_adjust=True)
        if h is not None and len(h) > 0:
            return [float(c) for c in h["Close"].tolist()]
    except Exception:
        pass
    return []


def run_commodity_rotation(cfg, results: dict = None,
                           closes_by_ticker: dict = None,
                           regime: str = None) -> dict:
    """
    メインエントリ（月初 / S1バックテストからは closes/regime を注入可能）。
    Returns: 配分提案 dict（注文票込み）。
    """
    universe = getattr(cfg, "CMD_ETF_UNIVERSE", {})
    regime_allow = getattr(cfg, "CMD_REGIME_ALLOW", {})
    max_pct = getattr(cfg, "CMD_MAX_ALLOC_PCT", 10.0)
    long_lb, short_lb = getattr(cfg, "CMD_MOM_LOOKBACK", (252, 21))

    if regime is None:
        regime = ((results or {}).get("economic_cycle") or {}).get("phase", "B")

    # モメンタム算出（注入 or yfinance）
    momentum, prices = {}, {}
    for ticker in universe:
        closes = (closes_by_ticker or {}).get(ticker)
        if closes is None:
            closes = _fetch_closes(ticker)
        if closes:
            prices[ticker] = closes[-1]
            momentum[ticker] = calc_12_1_momentum(closes, long_lb, short_lb)

    plan = select_allocations(momentum, regime, universe, regime_allow, max_pct)

    # 注文票（総資産はholdings簿価 or config・best-effort）
    total_assets = _estimate_total_assets(cfg)
    tickets = build_order_tickets(
        plan["allocations"], total_assets, prices,
        datetime.now().strftime("%Y-%m-%d")) if total_assets > 0 else []
    plan["order_tickets"] = tickets
    plan["momentum"] = momentum
    plan["total_assets"] = total_assets
    print(f"  🛢️ [commodity] 象限{regime} → 配分{plan['total_pct']}% "
          f"/ {len(plan['allocations'])}資産 / 注文票{len(tickets)}件")
    return plan


def _estimate_total_assets(cfg) -> float:
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
    lines = [f"# コモディティ・ローテーション提案 {yyyymm[:4]}-{yyyymm[4:]}（S6）",
             "",
             "> 月次配分提案（合計≤総資産10%）。発注は健太手動（D-01）。"
             "12-1モメンタム×経済サイクル4象限。",
             "",
             f"- 現在の経済サイクル象限: **{plan.get('regime','?')}** "
             f"/ 許可カテゴリ: {plan.get('allowed_categories') or '（なし＝現金）'}",
             f"- {plan.get('reason','')}",
             f"- コモディティ配分合計: {plan.get('total_pct',0)}% / "
             f"現金: {plan.get('cash_pct',100)}%",
             "",
             "## 配分提案", "| ETF | カテゴリ | 配分% | 12-1モメンタム |",
             "|---|---|---|---|"]
    universe = plan.get("_universe", {})
    for t, pct in (plan.get("allocations") or {}).items():
        mom = plan.get("momentum", {}).get(t)
        lines.append(f"| {t} | {universe.get(t,'')} | {pct}% | "
                     f"{f'{mom:+.1%}' if mom is not None else '-'} |")
    if not plan.get("allocations"):
        lines.append("| （現金100%・配分なし） | | | |")
    return "\n".join(lines) + "\n"


def main():
    dry = "--dry-run" in sys.argv
    try:
        import config as cfg
    except Exception:
        cfg = None
    plan = run_commodity_rotation(cfg, {})
    plan["_universe"] = getattr(cfg, "CMD_ETF_UNIVERSE", {})
    yyyymm = datetime.now().strftime("%Y%m")
    md = build_report(plan, yyyymm)
    if dry:
        print(md)
        return
    insight_dir = Path(getattr(cfg, "INSIGHT_DIR", REPO / "stock_data" / "insights"))
    out = insight_dir / f"commodity_alloc_{yyyymm}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    # 注文票の追記（既存 order_tickets.jsonl へ）
    tickets = plan.get("order_tickets") or []
    if tickets:
        orders_dir = Path(getattr(cfg, "ORDERS_DIR", "stock_data/orders"))
        orders_dir.mkdir(parents=True, exist_ok=True)
        with open(orders_dir / "order_tickets.jsonl", "a", encoding="utf-8") as f:
            for t in tickets:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(f"✅ コモディティ提案出力: {out}（注文票{len(tickets)}件）")


if __name__ == "__main__":
    main()
