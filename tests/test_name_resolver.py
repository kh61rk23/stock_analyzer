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
