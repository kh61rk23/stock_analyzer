# ==============================================================
# event_strategy_runner.py — イベントドリブン短期戦略（Wave R4-1 / S5）
# ==============================================================
# 【役割】playbooks/*.yaml を読み、各イベントソース（PEAD/決算/TDnet/8-K）の
#   当日候補に entry 条件を当てて注文を生成する。S2ペーパー（paper/）の
#   adapter として動く（Strategy契約と同じ dict注文を返す）。
# 【設計正本】要件定義書_統合改修_20260705.md §R4-1。
#   - condition は eval不使用の構造化評価（field/op/value の連言）。
#   - status=paper のみ稼働（live昇格はM2判定＝取引数>=30・PF>=1.3・p<0.10・健太承認）。
#   - side=WATCH は監視のみ（注文なし）。発注は常に健太手動（D-01）。
# 使い方: paper/strategy_adapters に自動登録（register_playbook_adapters）。
#   単体確認: python3 event_strategy_runner.py --validate
# ==============================================================

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_REQUIRED_TOP = {"id", "event_source", "entry", "exit", "status"}
_VALID_SOURCES = {"pead_monitor", "earnings_surprise", "tdnet_monitor",
                  "us_8k_monitor"}
_OPS = {
    ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b, "<": lambda a, b: a < b,
    "==": lambda a, b: _eq(a, b), "!=": lambda a, b: not _eq(a, b),
}


def _eq(a, b) -> bool:
    """型ゆるめの等価（"positive"=="positive" / true==True / 5==5.0）。"""
    if isinstance(b, bool) or isinstance(a, bool):
        return bool(a) == bool(b)
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


# ── バリデーション（受入条件） ───────────────────────────────

def validate_playbook(pb: dict) -> list[str]:
    """必須キー・型・値域を検査。エラー文リスト（空＝OK）。"""
    errors = []
    if not isinstance(pb, dict):
        return ["playbookがdictでない"]
    for k in _REQUIRED_TOP - set(pb):
        errors.append(f"必須キー欠損: {k}")
    if pb.get("event_source") not in _VALID_SOURCES:
        errors.append(f"未知のevent_source: {pb.get('event_source')}")
    if pb.get("status") not in ("paper", "live", "retired"):
        errors.append(f"未知のstatus: {pb.get('status')}")
    entry = pb.get("entry") or {}
    if entry.get("side") not in ("BUY", "SELL", "WATCH"):
        errors.append(f"entry.side不正: {entry.get('side')}")
    for c in (entry.get("conditions") or []):
        if not {"field", "op", "value"} <= set(c or {}):
            errors.append(f"condition欠損: {c}")
        elif c["op"] not in _OPS:
            errors.append(f"未知のop: {c['op']}")
    ex = pb.get("exit") or {}
    for k in ("hold_days_max", "take_profit_pct", "stop_loss_pct"):
        if k not in ex:
            errors.append(f"exit.{k}欠損")
    return errors


# ── entry/exit判定（純粋ロジック） ───────────────────────────

def eval_conditions(conditions: list, candidate: dict) -> bool:
    """全条件を連言(and)で評価。フィールド欠損は False。"""
    for c in (conditions or []):
        field, op, value = c.get("field"), c.get("op"), c.get("value")
        if field not in candidate or candidate.get(field) is None:
            return False
        fn = _OPS.get(op)
        if fn is None:
            return False
        try:
            if not fn(candidate[field], value):
                return False
        except Exception:
            return False
    return True


def extract_candidates(event_source: str, results: dict) -> list[dict]:
    """event_source別に候補を統一形式 [{ticker, ...fields}] へ正規化。"""
    if event_source == "pead_monitor":
        return [c for c in ((results.get("pead") or {}).get("alert_candidates")
                            or []) if c.get("ticker")]
    if event_source == "earnings_surprise":
        es = results.get("earnings_surprise") or {}
        rows = es.get("candidates") or es.get("surprises") or []
        return [c for c in rows if isinstance(c, dict) and c.get("ticker")]
    if event_source == "tdnet_monitor":
        out = []
        for ev in ((results.get("tdnet") or {}).get("buyback") or []):
            if ev.get("ticker") or ev.get("code"):
                out.append({**ev, "ticker": ev.get("ticker") or ev.get("code"),
                            "category": ev.get("category", "BUYBACK")})
        return out
    if event_source == "us_8k_monitor":
        u8 = results.get("us_8k") or {}
        return [{"ticker": t, "event_type": "ceo_change"}
                for t in (u8.get("ceo_change_tickers") or [])]
    return []


def evaluate_entries(playbook: dict, results: dict, held: set,
                     casino_pct: float = 15.0) -> list[dict]:
    """当日のentry注文リストを生成（WATCH/retired/liveでない場合は空）。"""
    entry = playbook.get("entry") or {}
    if entry.get("side") != "BUY" or playbook.get("status") != "paper":
        return []                      # WATCHや非paperは注文を出さない
    cands = extract_candidates(playbook["event_source"], results)
    slots = int(entry.get("max_concurrent", 0))
    weight = (float(entry.get("size_pct_of_casino", 0)) / 100.0
              * casino_pct / 100.0)    # カジノ枠%→equity比の近似
    orders = []
    for c in cands:
        if slots <= 0:
            break
        t = c["ticker"]
        if t in held:
            continue
        if eval_conditions(entry.get("conditions"), c):
            orders.append({"ticker": t, "side": "BUY", "weight": weight,
                           "tag": f"{playbook['id']}_entry"})
            held.add(t)
            slots -= 1
    return orders


def evaluate_exit(playbook: dict, entry_days: int, ret_pct: float | None) -> str | None:
    """1保有に対するexit判定。返り値: 理由文 or None（保持）。"""
    ex = playbook.get("exit") or {}
    if entry_days >= int(ex.get("hold_days_max", 10**9)):
        return f"time_exit({entry_days}d)"
    if ret_pct is not None:
        if ret_pct >= float(ex.get("take_profit_pct", 10**9)) / 100.0:
            return f"take_profit({ret_pct:+.1%})"
        if ret_pct <= float(ex.get("stop_loss_pct", -10**9)) / 100.0:
            return f"stop_loss({ret_pct:+.1%})"
    return None


# ── I/O層・adapter化 ─────────────────────────────────────────

def load_playbooks(cfg=None) -> list[dict]:
    import yaml
    pdir = REPO / getattr(cfg, "PLAYBOOK_DIR", "playbooks")
    out = []
    for p in sorted(pdir.glob("*.yaml")):
        try:
            pb = yaml.safe_load(p.read_text(encoding="utf-8"))
            pb["_file"] = p.name
            out.append(pb)
        except Exception as e:
            print(f"  ⚠️ [event_strategy] {p.name} 読込失敗: {str(e)[:60]}")
    return out


def _days_between(d1: str, d2: str) -> int:
    from datetime import datetime
    try:
        a = datetime.strptime(d1, "%Y-%m-%d")
        b = datetime.strptime(d2, "%Y-%m-%d")
        return abs((b - a).days)
    except Exception:
        return 0


def make_playbook_adapter(playbook: dict):
    """paper/strategy_adapters 互換の adapter(cfg, results, account, today) を返す。"""
    def adapter(cfg, results, account, today):
        casino_pct = getattr(cfg, "CASINO_BUDGET_PCT", 15.0)
        positions = account.get("positions", {})
        held = {t for t, p in positions.items() if p.get("qty", 0) > 0}
        # 現値ヒント（screener candidates から）
        price_hint = {}
        for c in ((results.get("screener") or {}).get("candidates") or []):
            t, px = c.get("ticker"), c.get("price") or c.get("close")
            if t and px:
                price_hint[t] = float(px)
        orders = []
        # exit判定
        for t, p in positions.items():
            if p.get("qty", 0) <= 0:
                continue
            days = _days_between(p.get("entry_date", today), today)
            px = price_hint.get(t)
            ret = (px / p["avg_cost"] - 1) if (px and p.get("avg_cost")) else None
            reason = evaluate_exit(playbook, days, ret)
            if reason:
                orders.append({"ticker": t, "side": "SELL", "qty": p["qty"],
                               "tag": f"{playbook['id']}_{reason}"})
                held.discard(t)
        # entry判定
        orders += evaluate_entries(playbook, results, held, casino_pct)
        return orders
    adapter.__name__ = f"playbook_{playbook.get('id','x')}"
    return adapter


def register_playbook_adapters(cfg) -> dict:
    """有効な paper プレイブックを {id: adapter} で返す（paper側が ADAPTERS に統合）。"""
    out = {}
    for pb in load_playbooks(cfg):
        if validate_playbook(pb):
            continue                   # 不正な playbook はスキップ
        if pb.get("status") == "paper" and (pb.get("entry") or {}).get("side") == "BUY":
            out[pb["id"]] = make_playbook_adapter(pb)
    return out


def main():
    if "--validate" in sys.argv:
        pbs = load_playbooks(None)
        allok = True
        for pb in pbs:
            errs = validate_playbook(pb)
            status = "✅" if not errs else "❌ " + "; ".join(errs)
            print(f"  {pb.get('_file','?')} [{pb.get('id','?')}] {status}")
            allok = allok and not errs
        print(f"\n{'✅ 全プレイブック妥当' if allok else '❌ エラーあり'}"
              f"（{len(pbs)}本）")
        return
    print("使い方: python3 event_strategy_runner.py --validate")


if __name__ == "__main__":
    main()
