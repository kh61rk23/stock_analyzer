# ==============================================================
# paper/strategy_adapters.py — 戦略アダプタ（R1-2 / S2）
# ==============================================================
# 各adapterは日次 results から翌日分の注文リストを生成する。
# 注文形式（dict）: {"ticker", "side": BUY/SELL, "qty" or "weight", "tag"}
# 対象（要件定義書 §R1-2）:
#   screener_topN : composite上位5・月次リバランス
#   ignition      : 発火セクターの代表銘柄を小口買い・20営業日で手仕舞い
#   pead          : PEADアラート候補を買い・TP15%/SL-7%/40日
# S5プレイブック全部はR4-1稼働後に、commodity(S6)はR4-2後に追加。
# options(S4)はIBKRペーパーを使うため対象外。
# 全adapterはデータ欠損時に安全に「注文なし」を返す。
# ==============================================================

from __future__ import annotations

from datetime import datetime


def _days_between(d1: str, d2: str) -> int:
    try:
        a = datetime.strptime(d1, "%Y-%m-%d")
        b = datetime.strptime(d2, "%Y-%m-%d")
        return abs((b - a).days)
    except Exception:
        return 0


# ── screener_topN ────────────────────────────────────────────

def screener_topn_adapter(cfg, results: dict, account: dict,
                          today: str) -> list:
    """composite_score 上位5銘柄・月次リバランス（equal weight）。"""
    meta = account.setdefault("meta", {})
    if meta.get("last_rebalance_month") == today[:7]:
        return []
    cands = ((results.get("screener") or {}).get("candidates") or [])
    ranked = sorted((c for c in cands if c.get("ticker")
                     and c.get("composite_score") is not None),
                    key=lambda c: -float(c["composite_score"]))
    targets = [c["ticker"] for c in ranked[:5]]
    if not targets:
        return []
    meta["last_rebalance_month"] = today[:7]
    orders = [{"ticker": t, "side": "SELL", "qty": p["qty"],
               "tag": "rebalance_exit"}
              for t, p in account["positions"].items()
              if t not in targets and p["qty"] > 0]
    w = 0.95 / len(targets)
    orders += [{"ticker": t, "side": "BUY", "weight": w,
                "tag": "rebalance_entry"}
               for t in targets if t not in account["positions"]]
    return orders


# ── ignition ─────────────────────────────────────────────────

def ignition_adapter(cfg, results: dict, account: dict, today: str) -> list:
    """
    ignition_detector の発火セクター（results["ignition"]["alerts"]）の
    代表銘柄（SECTOR_REPRESENTATIVE_TICKERS 先頭）を weight 5% で買い。
    保有20営業日（暦28日）で手仕舞い。同時保有上限4。
    """
    orders = []
    # exit: 保有期限
    for t, p in account["positions"].items():
        if p["qty"] > 0 and _days_between(p.get("entry_date", today), today) >= 28:
            orders.append({"ticker": t, "side": "SELL", "qty": p["qty"],
                           "tag": "ignition_time_exit"})
    exiting = {o["ticker"] for o in orders}
    held = sum(1 for t, p in account["positions"].items()
               if p["qty"] > 0 and t not in exiting)
    slots = 4 - held
    sector_map = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {}) or {}
    for alert in ((results.get("ignition") or {}).get("alerts") or []):
        if slots <= 0:
            break
        code = str(alert.get("sector_code", alert.get("code", "")) or "")
        reps = sector_map.get(code) or []
        if not reps:
            continue
        t = reps[0]
        if t in account["positions"] or t in exiting:
            continue
        orders.append({"ticker": t, "side": "BUY", "weight": 0.05,
                       "tag": f"ignition_entry({code})"})
        slots -= 1
    return orders


# ── pead ─────────────────────────────────────────────────────

def pead_adapter(cfg, results: dict, account: dict, today: str) -> list:
    """
    results["pead"]["alert_candidates"]（positive方向）を weight 5% で買い。
    TP+15% / SL-7% / 40暦日で手仕舞い。同時保有上限3。
    現値は screener candidates の price か avg_cost 比の判定に使えないため、
    exit判定は日次の adapter 呼び出し時に price_hint（results経由）が
    無い場合スキップし、時間exitのみ確実に効かせる。
    """
    orders = []
    # 現値ヒント（screener candidates から）
    price_hint = {}
    for c in ((results.get("screener") or {}).get("candidates") or []):
        t, px = c.get("ticker"), c.get("price") or c.get("close")
        if t and px:
            price_hint[t] = float(px)

    for t, p in account["positions"].items():
        if p["qty"] <= 0:
            continue
        days = _days_between(p.get("entry_date", today), today)
        px = price_hint.get(t)
        ret = (px / p["avg_cost"] - 1) if (px and p["avg_cost"] > 0) else None
        if days >= 56:                                   # 40営業日≒56暦日
            orders.append({"ticker": t, "side": "SELL", "qty": p["qty"],
                           "tag": f"pead_time_exit({days}d)"})
        elif ret is not None and (ret >= 0.15 or ret <= -0.07):
            orders.append({"ticker": t, "side": "SELL", "qty": p["qty"],
                           "tag": f"pead_pnl_exit({ret:+.1%})"})
    exiting = {o["ticker"] for o in orders}
    held = sum(1 for t, p in account["positions"].items()
               if p["qty"] > 0 and t not in exiting)
    slots = 3 - held
    for c in ((results.get("pead") or {}).get("alert_candidates") or []):
        if slots <= 0:
            break
        t = c.get("ticker")
        if (not t or t in account["positions"] or t in exiting
                or c.get("direction") == "negative"):
            continue
        orders.append({"ticker": t, "side": "BUY", "weight": 0.05,
                       "tag": f"pead_entry(score={c.get('pead_score', '?')})"})
        slots -= 1
    return orders


ADAPTERS = {
    "screener_topN": screener_topn_adapter,
    "ignition": ignition_adapter,
    "pead": pead_adapter,
}


def get_all_adapters(cfg=None) -> dict:
    """
    組込adapter＋R4-1のイベントドリブン・プレイブック（playbooks/*.yaml）を
    統合した {strategy_id: adapter} を返す。paper_broker が PAPER_STRATEGIES に
    含まれる id をここから解決する。プレイブックは status=paper のみ登録。
    """
    adapters = dict(ADAPTERS)
    if getattr(cfg, "ENABLE_EVENT_STRATEGY", False):
        try:
            from event_strategy_runner import register_playbook_adapters
            adapters.update(register_playbook_adapters(cfg))
        except Exception as e:
            print(f"    ⚠️ [paper] プレイブックadapter登録失敗: {str(e)[:60]}")
    return adapters
