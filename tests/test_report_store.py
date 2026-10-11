"""report_store: md レポートの種類判定と、業種・マクロ・米国債・米国セクターの読み取り。"""
from pathlib import Path

import report_store as rs

SECTOR = """## サービス業 分析レポート

**更新**: 2026-10-09 | **総合スコア**: 22.4点 | **リスク**: 🟡 中

### マクロシグナル

- 💚 金利上昇: 追い風
- ⚪ 現在特筆すべきマクロシグナルなし

### 主要企業 財務サマリー
"""
MACRO = """# マクロ経済 分析レポート

## 現在のマクロ指標

| 指標 | 値 |
|------|-----|
| USD/JPY | 158.16 |
| 日経平均 | 69,100.00 |

## アクティブなマクロシグナル

### 金利上昇（米10年債: 5.23%）

| 業種 | 判定 | 理由 |
|------|------|------|
| 銀行業 | 💚 追い風 | 預貸金利ザヤ拡大 |
| 不動産業 | 🔴 逆風 |  |
"""
BOND = """## 🏛️ 1. 米国国債イールドカーブ

| 年限 | 利回り | 1Mチェンジ | ソース |
|------|--------|-----------|-------|
| 米2年 | 4.770% | ↑ +0.380% | FRED |
| 米10年 | 5.280% | ↓ -0.120% | FRED |

### スプレッド
"""
CSV = "﻿date,etf,sector_en,sector_ja,score,risk_level,ret_1m,ret_3m,ret_12m,current,high_52w,signals\n2026-10-09,XLK,Information Technology,情報技術,50.0,low,5.40,6.58,39.17,197.78,202.00,52週高値圏|出来高増加\n"


def test_report_kind():
    assert rs.report_kind(Path("x/sectors/1033_2026-10-09.md")) == ("sector", "1033", "2026-10-09")
    assert rs.report_kind(Path("x/macro/macro_report_2026-10-09.md")) == ("macro_report", "", "2026-10-09")
    assert rs.report_kind(Path("x/macro/policy_changes_2026-10.md")) == ("policy_changes", "", "2026-10-01")
    assert rs.report_kind(Path("x/us_sectors/us_sector_scores_2026-10-09.json")) is None


def test_parse_jp_sector():
    r = rs.parse_jp_sector(SECTOR)
    assert r == {"sector_name": "サービス業", "score": 22.4, "risk_level": "medium", "signals": "💚 金利上昇: 追い風"}


def test_parse_macro_report_indicators_and_signals():
    ind, sigs = rs.parse_macro_report(MACRO)
    assert ind == [("USD/JPY", 158.16), ("日経平均", 69100.0)]
    assert sigs[0] == ("金利上昇", "米10年債: 5.23%", "銀行業", "tailwind", "預貸金利ザヤ拡大")
    assert sigs[1][3] == "headwind"


def test_parse_bond_and_us_csv():
    assert rs.parse_bond_yields(BOND) == [("米2年", 4.77, 0.38), ("米10年", 5.28, -0.12)]
    r = rs.parse_us_scores_csv(CSV)[0]
    assert r["sector_code"] == "XLK" and r["score"] == 50.0 and r["ret_12m"] == 39.17 and r["signals"] == "52週高値圏 | 出来高増加"


def test_build_all_routes_files(tmp_path):
    for sub, name, body in (("sectors", "1033_2026-10-09.md", SECTOR), ("macro", "macro_report_2026-10-09.md", MACRO),
                            ("macro", "bond_commodity_2026-10-09.md", BOND), ("us_sectors", "us_sector_scores_2026-10-09.csv", CSV),
                            ("us_sectors", "us_sector_scores_2026-10-09.json", "{}")):
        (tmp_path / sub).mkdir(exist_ok=True)
        (tmp_path / sub / name).write_text(body, encoding="utf-8")
    b = rs.build_all(rs.collect(tmp_path, "2026-10-09"), "2026-10-11T00:00:00+00:00")
    assert len(b["report_texts"]) == 3
    assert {(r["market"], r["sector_code"]) for r in b["sector_scores_daily"]} == {("JP", "1033"), ("US", "XLK")}
    assert len(b["macro_indicators_daily"]) == 4 and len(b["macro_signals_daily"]) == 2


def test_job_id_ignores_fetched_at():
    a = [{"report_date": "2026-10-09", "x": 1, "fetched_at": "t1"}]
    b = [{"report_date": "2026-10-09", "x": 1, "fetched_at": "t2"}]
    assert rs.job_id_for("t", a) == rs.job_id_for("t", b) != rs.job_id_for("u", a)


ETF_MD = """# テーマETFトラッキングレポート（2026-10-09）

## 🌡️ テーマ熱量ランキング

| テーマ | ETF | 熱量 | ETFモメンタム | ArXivトレンド | シグナル |
|--------|-----|------|--------------|--------------|---------|
| サイバーセキュリティ | CIBR | **66** | 75 | +29% | 📈 warming |
| 水素・クリーンエネルギー | ICLN | **34** | 37 | +-25% | ❄️ cooling |

## 📊 テーマ別詳細

### 📈 サイバーセキュリティ（熱量スコア: 66）

- **CIBR**: $106.2  1M:+12.4%  3M:+15.9%  MA50乖離:+7.5%  出来高前月比:+17.5%
"""


def test_etf_md_and_json():
    th, et = rs.etf_rows_from_md(ETF_MD, "2026-10-09", "t")
    assert [(t["theme_name"], t["heat_score"], t["arxiv_growth_rate"], t["signal"]) for t in th] == [
        ("サイバーセキュリティ", 66.0, 29.0, "warming"), ("水素・クリーンエネルギー", 34.0, -25.0, "cooling")]
    assert et == [{"report_date": "2026-10-09", "theme_name": "サイバーセキュリティ", "ticker": "CIBR", "price": 106.2, "ret_1m": 12.4,
                   "ret_3m": 15.9, "ma50_ratio": 7.5, "vol_ratio": 17.5, "fetched_at": "t"}]
    th, et = rs.etf_rows_from_json({"themes": [{"theme_name": "量子", "etfs": ["QTUM"], "heat_score": 58, "signal": "warming",
                                                "arxiv_detail": {"growth_rate": 55.1, "recent_4w": 929, "prev_4w": 597},
                                                "etf_details": [{"ticker": "QTUM", "price": 150.89, "ret_1m": 2.4}]}]}, "2026-10-09", "t")
    assert th[0]["arxiv_recent_4w"] == 929 and et[0]["ticker"] == "QTUM"


def test_news_rows_dedupe_and_zero_score():
    rows = rs.news_rows([{"url": "u1", "title": "A", "importance_score": 0, "matched_sectors": ["1016", "macro"], "investment_relevant": False},
                         {"url": "u1", "title": "A again"}, {"url": "", "title": "no url"}], "2026-10-09", "t")
    assert len(rows) == 1 and rows[0]["importance_score"] == 0.0 and rows[0]["matched_sectors"] == "1016,macro"
    assert rows[0]["investment_relevant"] is False
