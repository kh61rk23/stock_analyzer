import alert_notifier as an
import alert_store as ast

ALERTS = [
    {"trigger": "tdnet_buyback", "severity": "medium", "title": "💴 自己株式取得 12社（本日）",
     "body": "💴 ソディック（6143.T）: 自己株式取得に係る事項の決定\n💴 Ｇ－チームスピリット（4397.T）: 決定\n💴 新規（130A.T）: 取得"},
    {"trigger": "us", "severity": "info", "title": "🇺🇸 米国株サマリー（本日）",
     "body": "📌 Micron Technology, I（MU）: 91点 | PER:12.8x\n📌 Alphabet Inc.（GOOGL）: 89点\n[FRB] 米FF（ETF）"},
    {"trigger": "x", "severity": "high", "title": "🎯 決算サプライズ高確率候補 22社", "body": "🎯 シマノ（7309.T）| 決算 2026-10-27"},
]


def test_section_key_ignores_counts_and_wording():
    assert ast.section_key("💴 自己株式取得 12社（本日）") == "buyback"
    assert ast.section_key("💴 自己株式取得 3社") == "buyback"
    assert ast.section_key("🧪 新しい節 5件（試験）") == "新しい節"


def test_extract_items_jp_alphanumeric_and_us_without_acronyms():
    items = ast.extract_items(ALERTS[0]["body"] + "\n" + ALERTS[1]["body"])
    tks = [i["ticker"] for i in items]
    assert tks == ["6143.T", "4397.T", "130A.T", "MU", "GOOGL"]
    assert items[0]["line"].startswith("💴 ソディック")


def test_round_trip_from_sent_html():
    """実際に送る HTML から、同じ節（見出し・本文・重要度）を取り出せる。過去メールの取り込みもこの経路。"""
    html = an.build_message(ALERTS, mode="email")
    got = ast.parse_mail_html(html)
    assert [g["title"] for g in got] == [a["title"] for a in ALERTS]
    assert [g["severity"] for g in got] == ["medium", "info", "high"]
    assert "ソディック（6143.T）" in got[0]["body"] and got[0]["body"].count("\n") == 2
    secs, items = ast.rows_from_alerts(got, "2026-10-15", "mail_backfill", "2026-10-15T05:13:16+00:00", "<id>")
    assert [s["section_key"] for s in secs] == ["buyback", "us_summary", "earnings_surprise_up"]
    assert {i["ticker"] for i in items} == {"6143.T", "4397.T", "130A.T", "MU", "GOOGL", "7309.T"}


def test_latest_per_day_keeps_last_sent():
    m = [{"report_date": "2026-10-15", "sent_at": "2026-10-15T01:00:00+00:00", "n": 1},
         {"report_date": "2026-10-15", "sent_at": "2026-10-15T05:00:00+00:00", "n": 2},
         {"report_date": "2026-10-14", "sent_at": "2026-10-14T05:00:00+00:00", "n": 3}]
    assert [x["n"] for x in ast.latest_per_day(m)] == [3, 2]


def test_labeled_us_ticker_and_new_section_keys():
    body = "🏦 [アクティビスト] Pershing Square\n     新規取得: MSFT Microsoft Corporation (MICROSOFT CORP)\n     取得額: $2.1B"
    assert [i["ticker"] for i in ast.extract_items(body)] == ["MSFT"]
    assert ast.section_key("🏛️ 重要議員取引 5件") == "congress_trade"
    assert ast.section_key("🏦 大手機関・アクティビスト新規取得") == "institutional_13f"
