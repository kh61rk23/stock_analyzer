# 日次メールの節は重要度の段 → severity の順に並び、冒頭に段1の見出しが出る
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import alert_notifier as an  # noqa: E402
import config  # noqa: E402

TIERS = {"a1": 1, "b2": 2, "d4": 4, "e5": 5}


def al(trigger, sev="info", title=None):
    return {"trigger": trigger, "title": title or f"T-{trigger}", "body": "x", "severity": sev}


def test_tier_then_severity_order():
    out = an.sort_alerts_by_tier(
        [al("e5", "high"), al("b2", "info"), al("a1", "info"), al("b2", "critical"), al("b2", "medium")], TIERS)
    assert [(a["trigger"], a["severity"]) for a in out] == [
        ("a1", "info"), ("b2", "critical"), ("b2", "medium"), ("b2", "info"), ("e5", "high")]


def test_unregistered_goes_to_tier3_between_2_and_4():
    out = an.sort_alerts_by_tier([al("d4"), al("zzz"), al("b2")], TIERS)
    assert [a["trigger"] for a in out] == ["b2", "zzz", "d4"]


def test_ties_keep_original_order():
    src = [al("b2", "medium", "first"), al("b2", "warning", "second"), al("b2", "medium", "third")]
    assert [a["title"] for a in an.sort_alerts_by_tier(src, TIERS)] == ["first", "second", "third"]


def test_critical_and_warning_have_label_and_color():
    html = an.build_message([al("a1", "critical"), al("b2", "warning")], tiers=TIERS)
    assert "最重要" in html and "#b91c1c" in html
    assert html.count("🟡 注意") == 1


def test_first_look_block_lists_tier1_titles_only_and_caps():
    alerts = [al("b2", "high", "候補")] + [al("a1", "info", f"動く{i}") for i in range(7)]
    html = an.build_message(alerts, tiers=TIERS)
    head = html.split("<!-- アラート本文 -->")[0]
    assert "今日まず見るもの" in head and "動く4" in head and "動く5" not in head and "候補" not in head
    assert "ほか 2 件" in head
    # 本文は段1が先
    assert html.index("動く0") < html.index("候補")


def test_no_first_look_without_tier1():
    assert "今日まず見るもの" not in an.build_message([al("b2"), al("zzz")], tiers=TIERS)


def test_config_table_places_known_sections():
    t = config.ALERT_SECTION_TIER
    assert t["exit_rule_engine"] == 1 and t["paper_trading"] == 2 and t["high_dividend"] == 4
    assert t["ops_health"] == 5 and t["edinetdb_ingest"] == 5
    assert all(v in (1, 2, 3, 4, 5) for v in t.values())


def test_alert_own_tier_overrides_config():
    import alert_notifier as an
    alerts = [{"trigger": "risk_text_deterioration", "severity": "warning", "title": "r", "tier": 4},
              {"trigger": "top_sectors", "severity": "info", "title": "t"}]
    out = an.sort_alerts_by_tier(alerts, {"risk_text_deterioration": 1, "top_sectors": 2})
    assert [a["title"] for a in out] == ["t", "r"]
