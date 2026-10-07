# ==============================================================
# exit_rule_engine.py  v1.0  (Wave R0-6 / E1)
# 出口戦略エンジン — 投資哲学の売却ルールをコード化し日次監視
# ==============================================================
# 【役割】
#   保有銘柄（data/portfolio_holdings.csv）× ドメイン分類
#   （data/holdings_domain.csv）に対し、投資哲学.md の売却ルールを
#   毎日評価し、接近(near)/発火(fire) を results["exit_alerts"] に格納。
#   発火時は注文票（order_ticket）を生成する（発注は健太が手動 = D-01）。
#
# 【ルール一覧】要件定義書_統合改修_20260705.md §R0-6 の表と同一
#   R-A  恩株化   : +200%で元本分売却（180%接近）
#   R-S  短期     : 保有<6ヶ月 +20%（15%接近）→ 50%利確案
#   R-M  中期     : 保有1〜2年 +100%（90%接近）→ 元本回収
#   R-40 SaaS     : Rule of 40 割れ（データ欠損時は「判定不能」）
#   R-PBR バリュー: PBR>=1.2 段階利確（1.1接近・1.5全売却推奨）
#   R-STORY      : composite_score がベースライン比40%低下（30%接近）
#
# 【純粋ロジック】evaluate_rules() は I/O を持たない（pytest対象）。
# ==============================================================

from __future__ import annotations

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 1: 純粋ロジック（テスト対象・I/Oなし）
# ==============================================================

def _months_between(d1: str, d2: str) -> Optional[float]:
    """'YYYY-MM-DD'間の月数。パース不能は None。"""
    try:
        a = datetime.strptime(d1[:10], "%Y-%m-%d")
        b = datetime.strptime(d2[:10], "%Y-%m-%d")
        return abs((b - a).days) / 30.44
    except Exception:
        return None


def evaluate_rules(holding: dict, rule_cfg: dict) -> list:
    """
    1銘柄分のルール評価（純粋関数）。
    holding: {ticker, qty, avg_cost, price, domain, purchase_date, today,
              rule_of_40(optional), pbr(optional),
              composite_now(optional), composite_baseline(optional),
              unit(optional=100)}
    rule_cfg: config BLOCK 44 の定数dict（EXIT_* 群）
    Returns: [{rule_id, level: "fire"|"near", detail, suggested_qty}, ...]
    """
    alerts = []
    price    = holding.get("price")
    avg_cost = holding.get("avg_cost")
    qty      = int(holding.get("qty", 0) or 0)
    unit     = int(holding.get("unit", 100) or 100)
    if not price or not avg_cost or avg_cost <= 0 or qty <= 0:
        return alerts

    gain_pct = (price - avg_cost) / avg_cost * 100.0
    domain   = (holding.get("domain") or "").lower()
    months   = _months_between(holding.get("purchase_date", ""),
                               holding.get("today", ""))

    # ── R-A 恩株化（全銘柄・+200%で元本分売却） ──────────────
    fire_a = float(rule_cfg.get("EXIT_ONKABU_FIRE_PCT", 200))
    near_a = float(rule_cfg.get("EXIT_ONKABU_NEAR_PCT", 180))
    if gain_pct >= fire_a:
        principal = avg_cost * qty
        raw = principal / price
        sell_qty = min(qty, int(math.ceil(raw / unit)) * unit) if unit > 1 else int(math.ceil(raw))
        alerts.append({"rule_id": "R-A", "level": "fire",
                       "detail": f"+{gain_pct:.0f}% 恩株化条件。元本分{sell_qty}株の売却で以後ノーリスク",
                       "suggested_qty": sell_qty})
    elif gain_pct >= near_a:
        alerts.append({"rule_id": "R-A", "level": "near",
                       "detail": f"+{gain_pct:.0f}%（恩株化+{fire_a:.0f}%まであと{fire_a - gain_pct:.0f}pt）",
                       "suggested_qty": 0})

    # ── R-S 短期（<6ヶ月・+20%） ─────────────────────────────
    if months is not None and months < 6:
        fire_s = float(rule_cfg.get("EXIT_SHORT_FIRE_PCT", 20))
        near_s = float(rule_cfg.get("EXIT_SHORT_NEAR_PCT", 15))
        if gain_pct >= fire_s:
            half = max(unit, int(qty / 2 / unit) * unit) if unit > 1 else int(qty / 2)
            alerts.append({"rule_id": "R-S", "level": "fire",
                           "detail": f"短期+{gain_pct:.0f}%（保有{months:.1f}ヶ月）。統計的調整水準 → 全売却 or 50%利確（{half}株案）",
                           "suggested_qty": half})
        elif gain_pct >= near_s:
            alerts.append({"rule_id": "R-S", "level": "near",
                           "detail": f"短期+{gain_pct:.0f}%（発火+{fire_s:.0f}%接近）",
                           "suggested_qty": 0})

    # ── R-M 中期（1〜2年・+100%） ────────────────────────────
    if months is not None and 12 <= months <= 24:
        fire_m = float(rule_cfg.get("EXIT_MID_FIRE_PCT", 100))
        near_m = float(rule_cfg.get("EXIT_MID_NEAR_PCT", 90))
        if gain_pct >= fire_m:
            principal = avg_cost * qty
            raw = principal / price
            sell_qty = min(qty, int(math.ceil(raw / unit)) * unit) if unit > 1 else int(math.ceil(raw))
            alerts.append({"rule_id": "R-M", "level": "fire",
                           "detail": f"中期+{gain_pct:.0f}%（2倍）。元本回収{sell_qty}株 → 残りを恩株化",
                           "suggested_qty": sell_qty})
        elif gain_pct >= near_m:
            alerts.append({"rule_id": "R-M", "level": "near",
                           "detail": f"中期+{gain_pct:.0f}%（2倍接近）", "suggested_qty": 0})

    # ── R-40 SaaS（Rule of 40） ──────────────────────────────
    if domain == "saas":
        r40 = holding.get("rule_of_40")
        th  = float(rule_cfg.get("EXIT_RULE40_TH", 40))
        near_th = float(rule_cfg.get("EXIT_RULE40_NEAR_TH", 45))
        if r40 is None:
            alerts.append({"rule_id": "R-40", "level": "near",
                           "detail": "Rule of 40 判定不能（財務データ欠損）— 手動確認推奨",
                           "suggested_qty": 0})
        elif r40 < th:
            streak = int(holding.get("rule_of_40_below_streak", 1))
            if streak >= 2:
                alerts.append({"rule_id": "R-40", "level": "fire",
                               "detail": f"Rule of 40 = {r40:.0f} が2四半期連続で{th:.0f}割れ → 全売却ルール",
                               "suggested_qty": qty})
            else:
                alerts.append({"rule_id": "R-40", "level": "near",
                               "detail": f"Rule of 40 = {r40:.0f}（{th:.0f}割れ1回目・次回決算で確定）",
                               "suggested_qty": 0})
        elif r40 < near_th:
            alerts.append({"rule_id": "R-40", "level": "near",
                           "detail": f"Rule of 40 = {r40:.0f}（{th:.0f}接近）", "suggested_qty": 0})

    # ── R-PBR バリュー ────────────────────────────────────────
    if domain == "value":
        pbr = holding.get("pbr")
        if pbr is not None:
            fire_p = float(rule_cfg.get("EXIT_PBR_FIRE", 1.2))
            full_p = float(rule_cfg.get("EXIT_PBR_FULL", 1.5))
            near_p = float(rule_cfg.get("EXIT_PBR_NEAR", 1.1))
            if pbr >= full_p:
                alerts.append({"rule_id": "R-PBR", "level": "fire",
                               "detail": f"PBR {pbr:.2f} >= {full_p} → 全売却推奨水準",
                               "suggested_qty": qty})
            elif pbr >= fire_p:
                half = max(unit, int(qty / 2 / unit) * unit) if unit > 1 else int(qty / 2)
                alerts.append({"rule_id": "R-PBR", "level": "fire",
                               "detail": f"PBR {pbr:.2f} >= {fire_p} → 段階利確（{half}株案）",
                               "suggested_qty": half})
            elif pbr >= near_p:
                alerts.append({"rule_id": "R-PBR", "level": "near",
                               "detail": f"PBR {pbr:.2f}（{fire_p}接近）", "suggested_qty": 0})

    # ── R-STORY（composite_score 低下） ──────────────────────
    base = holding.get("composite_baseline")
    now  = holding.get("composite_now")
    if base and now and base > 0:
        drop_pct = (base - now) / base * 100.0
        fire_d = float(rule_cfg.get("EXIT_STORY_DROP_FIRE_PCT", 40))
        near_d = float(rule_cfg.get("EXIT_STORY_DROP_NEAR_PCT", 30))
        if drop_pct >= fire_d:
            alerts.append({"rule_id": "R-STORY", "level": "fire",
                           "detail": f"composite_score {base:.0f}→{now:.0f}（-{drop_pct:.0f}%）。ストーリー再点検 → B部門審議を推奨（自動売却はしない）",
                           "suggested_qty": 0})
        elif drop_pct >= near_d:
            alerts.append({"rule_id": "R-STORY", "level": "near",
                           "detail": f"composite_score -{drop_pct:.0f}%低下", "suggested_qty": 0})

    return alerts


def build_order_ticket(ticker: str, alert: dict, seq: int, today: str) -> dict:
    """fire かつ suggested_qty>0 のアラートから注文票を生成（共通スキーマ①）。"""
    return {
        "ticket_id":  f"OT-{today.replace('-', '')}-{seq:03d}",
        "created_at": today,
        "strategy":   "exit_engine",
        "ticker":     ticker,
        "side":       "SELL",
        "qty":        int(alert.get("suggested_qty", 0)),
        "order_hint": "成行寄付",
        "reason":     f"{alert['rule_id']}: {alert['detail']}",
        "valid_until": today,   # 当日限り（翌日は再評価で再発行）
        "guardrail_check": {"casino_budget_ok": True,
                            "max_single_pos_ok": True,
                            "margin_flag": False},
        "status": "pending", "executed_price": None, "executed_at": None,
    }


# ==============================================================
# BLOCK 2: I/O層
# ==============================================================

def _read_csv(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _load_baseline(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def run_exit_rule_engine(cfg, results: dict) -> dict:
    """
    メインエントリ（stock_analyzer_main から呼ばれる）。
    Returns: {"alerts": [...], "tickets": [...], "checked": int, "skipped": bool}
    """
    base_dir = Path(getattr(cfg, "PROJECT_DIR", "."))
    holdings_path = Path(getattr(cfg, "PORTFOLIO_HOLDINGS_PATH",
                                 base_dir / "data" / "portfolio_holdings.csv"))
    domain_path   = Path(getattr(cfg, "HOLDINGS_DOMAIN_PATH",
                                 base_dir / "data" / "holdings_domain.csv"))
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = data_dir / "exit_engine_baseline.json"
    orders_dir = Path(getattr(cfg, "ORDERS_DIR", "stock_data/orders"))
    orders_dir.mkdir(parents=True, exist_ok=True)

    holdings = _read_csv(holdings_path)
    if not holdings:
        print("    ⏩ [exit_engine] portfolio_holdings.csv なし/空 → スキップ")
        return {"alerts": [], "tickets": [], "checked": 0, "skipped": True}

    domains = {r.get("ticker", "").strip(): (r.get("domain") or "").strip()
               for r in _read_csv(domain_path)}
    baseline = _load_baseline(baseline_path)
    today = datetime.now().strftime("%Y-%m-%d")

    # composite_score の現在値（screener結果から拾う・無ければ None）
    comp_now = {}
    try:
        for c in (results.get("screener", {}) or {}).get("candidates", []):
            if c.get("ticker"):
                comp_now[c["ticker"]] = c.get("composite_score")
    except Exception:
        pass

    # ルール定数（config BLOCK 44 を dict 化）
    rule_cfg = {k: getattr(cfg, k) for k in dir(cfg) if k.startswith("EXIT_")}

    all_alerts, tickets, seq = [], [], 1
    from data_reliability import fetch_price_safe   # 同リポジトリ内
    from sync_holdings import usd_jpy, price_in_jpy
    fx = usd_jpy(cfg)   # 2026-10-06: 米国株はドル株価×為替で円の取得単価と比べる（以前はドル÷円で-99%）

    for h in holdings:
        ticker = (h.get("ticker") or "").strip()
        if not ticker:
            continue
        pr = fetch_price_safe(cfg, ticker)
        price = pr.get("close")
        if price is None:
            all_alerts.append({"ticker": ticker, "rule_id": "DATA",
                               "level": "near",
                               "detail": f"価格取得不能（{pr.get('error','')[:60]}）"})
            continue

        # ベースライン記録（初回のみ）
        if ticker not in baseline and comp_now.get(ticker) is not None:
            baseline[ticker] = {"composite": comp_now[ticker], "recorded": today}

        holding = {
            "ticker": ticker,
            "qty": float(h.get("quantity", 0) or 0),
            "avg_cost": float(h.get("avg_cost_jpy", 0) or 0),
            "price": price_in_jpy(h, price, fx),
            "domain": domains.get(ticker, ""),
            "purchase_date": h.get("purchase_date", ""),
            "today": today,
            "composite_now": comp_now.get(ticker),
            "composite_baseline": (baseline.get(ticker) or {}).get("composite"),
            "unit": 100 if ticker.endswith(".T") else 1,
        }
        for a in evaluate_rules(holding, rule_cfg):
            a["ticker"] = ticker
            a["price"]  = price
            a["stale"]  = pr.get("is_stale", False)
            all_alerts.append(a)
            if a["level"] == "fire" and a.get("suggested_qty", 0) > 0:
                # halt_flag（S9）が立っていたら注文票は発行しない
                halt = orders_dir / "halt_flag.json"
                halted = False
                if halt.exists():
                    try:
                        halted = bool(json.loads(halt.read_text()).get("halted"))
                    except Exception:
                        halted = False
                if not halted:
                    tickets.append(build_order_ticket(ticker, a, seq, today))
                    seq += 1

    # 永続化
    baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    if tickets:
        tickets_path = orders_dir / "order_tickets.jsonl"
        with open(tickets_path, "a", encoding="utf-8") as f:
            for t in tickets:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        print(f"    🎫 [exit_engine] 注文票 {len(tickets)}件 発行（発注は手動 = D-01）")

    fires = [a for a in all_alerts if a.get("level") == "fire"]
    print(f"    ✅ [exit_engine] 監視{len(holdings)}銘柄 / 発火{len(fires)}件 / "
          f"接近{len(all_alerts) - len(fires)}件")
    return {"alerts": all_alerts, "tickets": tickets,
            "checked": len(holdings), "skipped": False}
