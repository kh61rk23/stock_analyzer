# ==============================================================
# tests/test_exit_rules.py  (Wave R0-6 / E1)
# exit_rule_engine.evaluate_rules() の純粋ロジック検証
# ==============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exit_rule_engine import evaluate_rules, build_order_ticket, _months_between

CFG = {
    "EXIT_ONKABU_FIRE_PCT": 200.0, "EXIT_ONKABU_NEAR_PCT": 180.0,
    "EXIT_SHORT_FIRE_PCT": 20.0,  "EXIT_SHORT_NEAR_PCT": 15.0,
    "EXIT_MID_FIRE_PCT": 100.0,   "EXIT_MID_NEAR_PCT": 90.0,
    "EXIT_RULE40_TH": 40.0,       "EXIT_RULE40_NEAR_TH": 45.0,
    "EXIT_PBR_FIRE": 1.2, "EXIT_PBR_FULL": 1.5, "EXIT_PBR_NEAR": 1.1,
    "EXIT_STORY_DROP_FIRE_PCT": 40.0, "EXIT_STORY_DROP_NEAR_PCT": 30.0,
}


def _h(**kw):
    base = {"ticker": "7203.T", "qty": 100, "avg_cost": 1000, "price": 1000,
            "domain": "", "purchase_date": "2023-01-10", "today": "2026-07-05",
            "unit": 100}
    base.update(kw)
    return base


def _ids(alerts, level=None):
    return [a["rule_id"] for a in alerts if level is None or a["level"] == level]


# ── R-A 恩株化 ───────────────────────────────────────────────
def test_onkabu_fire():
    alerts = evaluate_rules(_h(price=3100), CFG)   # +210%
    fires = [a for a in alerts if a["rule_id"] == "R-A" and a["level"] == "fire"]
    assert len(fires) == 1
    # 元本100,000円 / 3100円 = 32.3株 → 単元切上げ100株
    assert fires[0]["suggested_qty"] == 100


def test_onkabu_near():
    alerts = evaluate_rules(_h(price=2850), CFG)   # +185%
    assert "R-A" in _ids(alerts, "near")
    assert "R-A" not in _ids(alerts, "fire")


def test_onkabu_none():
    alerts = evaluate_rules(_h(price=1500), CFG)   # +50%
    assert "R-A" not in _ids(alerts)


# ── R-S 短期 ────────────────────────────────────────────────
def test_short_fire():
    alerts = evaluate_rules(
        _h(price=1250, purchase_date="2026-05-01"), CFG)  # +25%・2ヶ月
    assert "R-S" in _ids(alerts, "fire")


def test_short_near():
    alerts = evaluate_rules(
        _h(price=1170, purchase_date="2026-05-01"), CFG)  # +17%
    assert "R-S" in _ids(alerts, "near")


def test_short_not_applied_after_6m():
    alerts = evaluate_rules(
        _h(price=1250, purchase_date="2025-01-01"), CFG)  # 18ヶ月保有
    assert "R-S" not in _ids(alerts)


# ── R-M 中期 ────────────────────────────────────────────────
def test_mid_fire():
    alerts = evaluate_rules(
        _h(price=2100, purchase_date="2025-01-05"), CFG)  # +110%・18ヶ月
    fires = [a for a in alerts if a["rule_id"] == "R-M" and a["level"] == "fire"]
    assert len(fires) == 1
    assert fires[0]["suggested_qty"] == 100  # 元本100,000/2100=47.6→切上げ100


def test_mid_near():
    alerts = evaluate_rules(
        _h(price=1950, purchase_date="2025-01-05"), CFG)  # +95%
    assert "R-M" in _ids(alerts, "near")


# ── R-40 SaaS ───────────────────────────────────────────────
def test_rule40_fire_after_2q():
    alerts = evaluate_rules(
        _h(domain="saas", rule_of_40=30, rule_of_40_below_streak=2), CFG)
    assert "R-40" in _ids(alerts, "fire")


def test_rule40_first_breach_is_near():
    alerts = evaluate_rules(
        _h(domain="saas", rule_of_40=30, rule_of_40_below_streak=1), CFG)
    assert "R-40" in _ids(alerts, "near")
    assert "R-40" not in _ids(alerts, "fire")


def test_rule40_missing_data():
    alerts = evaluate_rules(_h(domain="saas"), CFG)
    r40 = [a for a in alerts if a["rule_id"] == "R-40"]
    assert len(r40) == 1 and "判定不能" in r40[0]["detail"]


# ── R-PBR バリュー ──────────────────────────────────────────
def test_pbr_staged_fire():
    alerts = evaluate_rules(_h(domain="value", pbr=1.3), CFG)
    assert "R-PBR" in _ids(alerts, "fire")


def test_pbr_full_sell():
    alerts = evaluate_rules(_h(domain="value", pbr=1.6), CFG)
    fires = [a for a in alerts if a["rule_id"] == "R-PBR" and a["level"] == "fire"]
    assert fires and fires[0]["suggested_qty"] == 100  # 全量


def test_pbr_near():
    alerts = evaluate_rules(_h(domain="value", pbr=1.15), CFG)
    assert "R-PBR" in _ids(alerts, "near")


def test_pbr_not_for_other_domain():
    alerts = evaluate_rules(_h(domain="cyclical", pbr=1.6), CFG)
    assert "R-PBR" not in _ids(alerts)


# ── R-STORY ─────────────────────────────────────────────────
def test_story_fire_no_qty():
    alerts = evaluate_rules(
        _h(composite_baseline=80, composite_now=45), CFG)  # -44%
    fires = [a for a in alerts if a["rule_id"] == "R-STORY" and a["level"] == "fire"]
    assert fires and fires[0]["suggested_qty"] == 0  # 売却票は出さない仕様


def test_story_near():
    alerts = evaluate_rules(
        _h(composite_baseline=80, composite_now=53), CFG)  # -34%
    assert "R-STORY" in _ids(alerts, "near")


# ── 異常系・共通 ────────────────────────────────────────────
def test_invalid_holding_returns_empty():
    assert evaluate_rules(_h(price=None), CFG) == []
    assert evaluate_rules(_h(avg_cost=0), CFG) == []
    assert evaluate_rules(_h(qty=0), CFG) == []


def test_months_between_invalid():
    assert _months_between("bad", "2026-07-05") is None


def test_order_ticket_schema():
    t = build_order_ticket("7203.T",
                           {"rule_id": "R-A", "detail": "x", "suggested_qty": 100},
                           1, "2026-07-05")
    assert t["ticket_id"] == "OT-20260705-001"
    assert t["side"] == "SELL" and t["qty"] == 100
    assert t["guardrail_check"]["margin_flag"] is False
    assert t["status"] == "pending"
