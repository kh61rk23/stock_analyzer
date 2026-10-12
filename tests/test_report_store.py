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
    assert rs.report_kind(Path("x/ideas/weekly_summary_2026-W37.md")) == ("weekly_summary", "", "2026-09-07")
    assert rs.report_kind(Path("x/commodity_alloc_202610.md")) == ("commodity_alloc", "", "2026-10-01")
    assert rs.report_kind(Path("x/tax_report_2026.md")) == ("tax_report", "", "2026-01-01")


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
    b = rs.build_all(rs.collect(tmp_path), "2026-10-11T00:00:00+00:00")
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


IMF = """## 実質GDP成長率(%) (NGDP_RPCH)

| 国 | 2024 | 2025 | 2026 |
|---|---|---|---|
| 日本(JPN) | -0.2 | 1.2 | 0.7 |
| 世界(W) | — | — | — |
"""
GOV = """| 統計名 | 最新値 | 前月比 | 前年比 | シグナル |
|--------|--------|--------|--------|---------|
| 消費者物価指数（CPI） | 102.5（2020年=100） (2026-09) | +0.2% | +2.7% | 📉 逆風 |
| 建設工事着工統計 | 17,043.0（戸） (2026-07) | +61.5% | +10.8% | 📈 追い風 |
"""


def test_parse_imf_and_gov():
    assert rs.parse_imf(IMF) == [("NGDP_RPCH", "実質GDP成長率(%)", "日本(JPN)", 2024, -0.2), ("NGDP_RPCH", "実質GDP成長率(%)", "日本(JPN)", 2025, 1.2),
                                 ("NGDP_RPCH", "実質GDP成長率(%)", "日本(JPN)", 2026, 0.7)]
    g = rs.parse_gov(GOV)
    assert g[0] == ("消費者物価指数（CPI）", 102.5, "（2020年=100） (2026-09)", "2026-09", 0.2, 2.7, "headwind")
    assert g[1][1] == 17043.0 and g[1][6] == "tailwind"


def test_pending_only_new_or_changed(tmp_path):
    (tmp_path / "macro").mkdir()
    a = tmp_path / "macro" / "macro_report_2026-10-09.md"
    b = tmp_path / "macro" / "macro_report_2026-10-10.md"
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    state = {"macro/macro_report_2026-10-09.md": rs.file_sha(a)}
    assert rs.pending(rs.collect(tmp_path), tmp_path, state) == [b]
    a.write_text("A2", encoding="utf-8")
    assert set(rs.pending(rs.collect(tmp_path), tmp_path, state)) == {a, b}


TRACE_MD = """# 選別の経路（2026-10-09）

## ✅ 通過 1銘柄（総合点順）

| 銘柄 | 総合点 | 加点前 | 点数の経路 |
|---|---|---|---|
| 5021.T Cosmo Energy Holdi | 97.0 | 92.5 | 92.5 →政策+4.4 +4.4→ 97.0 |

## ❌ 落ちた 1銘柄

| 銘柄 | 業種 | 落ちた理由 |
|---|---|---|
| 6501.T Hitachi, Ltd. | 電気機器 | PER 31.1（条件 0超〜30倍） |
"""


def test_trace_rows_from_md():
    rows = rs.trace_rows_from_md(TRACE_MD)
    assert rows[0]["ticker"] == "5021.T" and rows[0]["passed"] and rows[0]["composite_score"] == 97.0 and rows[0]["base_score"] == 92.5
    assert rows[1] == {"ticker": "6501.T", "name": "Hitachi, Ltd.", "sector_name": "電気機器", "passed": False, "composite_score": None,
                       "base_score": None, "score_path": None, "reasons": "PER 31.1（条件 0超〜30倍）"}


def test_screener_trace_rows_pure():
    import screener_trace as stc
    rows = stc.trace_rows([{"ticker": "A.T", "composite_score": 80, "base_score": 70, "kpi": {"pbr": 0.9, "roic": 0.12},
                            "score_trail": [{"stage": "base", "score": 70, "delta": 0}, {"stage": "policy", "score": 80, "delta": 10, "note": "政策"}]}],
                          [{"ticker": "B.T", "reasons": [], "composite_score": 40}])
    assert rows[0]["passed"] and rows[0]["kpi_pbr"] == 0.9 and rows[0]["score_path"].startswith("70.0")
    assert rows[1]["passed"] is False and rows[1]["reasons"] == "（フィルター以外）"


def test_us_trace_rows_and_ingest(tmp_path):
    import json
    import screener_trace as stc
    res = {"us_screener": {"candidates": [
        {"ticker": "AAPL", "name": "Apple", "sector": "Technology", "business_model": "platform", "fetch_ok": True,
         "pass_filters": True, "composite_score": 81, "per": 29.1},
        {"ticker": "XOM", "name": "Exxon", "fetch_ok": True, "pass_filters": False, "fail_reasons": ["PER=35.0 > 25", "ROE=5.0% < 10%"]},
        {"ticker": "ZZZ", "fetch_ok": False}]}}
    rows = stc.us_trace_rows(res)
    assert [(r["ticker"], r["passed"], r["reasons"]) for r in rows] == [
        ("AAPL", True, None), ("XOM", False, "PER=35.0 > 25 / ROE=5.0% < 10%"), ("ZZZ", False, "データ取得失敗")]
    (tmp_path / "ins" / "us_screener").mkdir(parents=True)
    (tmp_path / "ins" / "us_screener" / "us_screener_2026-10-13.md").write_text("# 米国", encoding="utf-8")
    (tmp_path / "data" / "screener_trace").mkdir(parents=True)
    (tmp_path / "data" / "screener_trace" / "trace_us_2026-10-13.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    b = rs.build_all(rs.collect(tmp_path / "ins"), "t", tmp_path / "data")
    tr = b["screener_trace_daily"]
    assert len(tr) == 3 and {r["market"] for r in tr} == {"US"} and tr[0]["kpi_per"] == 29.1


def test_num_drops_nan_and_inf():
    assert rs._num(float("inf")) is None and rs._num(float("nan")) is None and rs._num("Infinity") is None and rs._num(1) == 1.0
