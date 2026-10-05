# 2026-10-05: ティッカー/証券コード/業種コード → 名前
import name_resolver as n


def test_enrich_text_adds_names_but_keeps_machine_lines():
    out = n.enrich_text("📌 8058.T 90点\n[1028] → [1029]\nDELIBERATION_QUEUE_V1: 8058.T,8306.T\n三菱商事（8058.T）")
    lines = out.split("\n")
    assert lines[0].startswith("📌 8058.T 三菱商事")
    assert "[1028 銀行業]" in lines[1]
    assert lines[2] == "DELIBERATION_QUEUE_V1: 8058.T,8306.T"
    assert lines[3] == "三菱商事（8058.T）"          # 既に名前があれば重ねない


def test_sector_names():
    assert n.sector_name("1009") == "石油・石炭製品"
    assert n.sector_name("XLK") == "情報技術"
    assert n.sector_label("1028") == "銀行業（1028）"


def test_real_estate_alert():
    import alert_notifier as an
    a = an.build_real_estate_alert(
        {"skipped": False, "success": 45, "holdings": 3601, "area_profiles": 6, "targets": ["E02086"]},
        {"1032": {"real_estate_bonus": 2.6}, "1021": {"real_estate_bonus": -1.2}})
    assert "保有不動産 3,601件" in a["body"] and "不動産業（1032）: +2.60点" in a["body"]
    assert a["body"].count("陸運業") == 1


def test_scenario_portfolio_value_does_not_apply_fx_to_yen_fallback():
    import scenario_analyzer as sa
    h = [{"ticker": "IONQ", "market": "US", "quantity": 216, "avg_cost_jpy": 5319, "current_price": 0},
         {"ticker": "NVDA", "market": "US", "quantity": 1, "avg_cost_jpy": 19809, "current_price": 200.0},
         {"ticker": "8058.T", "market": "JP", "quantity": 200, "avg_cost_jpy": 1983, "current_price": 4979}]
    r = sa.calc_portfolio_impact(h, {}, usdjpy=150.0)
    # IONQ は株価なし→取得額（円）のまま、NVDA は 200ドル×150円
    assert r["total_value_jpy"] == round(216 * 5319 + 200 * 150 + 200 * 4979)
