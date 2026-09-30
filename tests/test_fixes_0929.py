# 2026-09-29: EDINET DB 取り込みの見える化
#   ・その日取り込んだ銘柄をメール（Gmail）に載せる
#   ・対象があるのに取り込み0社の日を success と記録していた
import alert_notifier as an


def test_ingest_alert_lists_companies():
    al = an.build_edinetdb_ingest_alert({
        "success": 2, "targets": ["E1", "E2", "E3"], "failure": 0,
        "ingested": [{"sec_code": "3983", "name": "オロ", "fiscal_year": 2025, "financial_rows": 5},
                     {"sec_code": "7203", "name": "トヨタ自動車"}],
        "missing_companies": [{"sec_code": "5866", "name": "新規上場A"}],
        "doc_index_rows": 14, "api_calls": 20})
    assert al["channel"] == "gmail" and al["severity"] == "info"
    assert "3983 オロ" in al["body"] and "7203 トヨタ自動車" in al["body"]
    assert "5866 新規上場A" in al["body"]
    assert al["title"] == "🗂 EDINET DB 取り込み 2社"


def test_zero_ingest_is_flagged():
    al = an.build_edinetdb_ingest_alert({"success": 0, "targets": ["E1"], "ingested": [],
                                         "missing_companies": [], "failure": 0})
    assert "要確認" in al["title"] and al["severity"] == "medium"


def test_extract_alerts_includes_ingest(monkeypatch):
    import types
    cfg = types.SimpleNamespace(ALERT_ON_EDINETDB_INGEST=True)
    res = {"edinetdb_ingest": {"skipped": False, "success": 1, "targets": ["E1"],
                               "ingested": [{"sec_code": "1332", "name": "ニッスイ"}]}}
    alerts = an.extract_alerts(cfg, res)
    assert any(a["trigger"] == "edinetdb_ingest" for a in alerts)


# ── ④⑤: 点数の経路・銘柄単位の政策加点上限・審議キューの並びと入れ替わり ──
import types as _types

import candidate_kpi as ck
import deliberation_scheduler as ds
import score_trail as st
import screener_trace as trc
from policy_signal_applier import apply_policy_signals_to_screener


def _cand(t, score, sector="1016", base=None):
    return {"ticker": t, "name": t, "sector_code": sector, "composite_score": score,
            "sector_score": 49.0, "value_score": 60.0, "quality_score": 80.0,
            "pass_filters": True}


def test_policy_adj_is_capped_per_stock_and_trail_records_it():
    cfg = _types.SimpleNamespace(SCORE_MAX_POLICY_STOCK_ADJ=10.0, SCORE_MAX_POLICY_L4_ADJ=25.0)
    c = _cand("6504.T", 66.9)
    res = {"screener": {"candidates": [c]}}
    st.checkpoint("stock_screener", res)
    apply_policy_signals_to_screener([c], {"1016": 25.0}, False, {}, cfg)
    st.checkpoint("policy_signal_applier", res)
    assert c["composite_score"] == 76.9, "業種の+25点がそのまま乗っている"
    assert c["base_score"] == 66.9
    assert [t["stage"] for t in c["score_trail"]] == ["stock_screener", "policy_signal_applier"]
    assert c["score_trail"][1]["delta"] == 10.0 and "政策+10.0" in c["score_trail"][1]["note"]
    assert "基本点 業種49×40%" in c["score_trail"][0]["note"]


def test_trail_notes_points_lost_at_ceiling():
    c = _cand("X.T", 95.0)
    res = {"screener": {"candidates": [c]}}
    st.checkpoint("stock_screener", res)
    c["policy_signal_adj"] = 10.0
    c["composite_score"] = 100.0
    st.checkpoint("policy_signal_applier", res)
    assert "上限100で5.0点切り捨て" in c["score_trail"][-1]["note"]
    assert "（上限）" in st.summary(c)


def test_queue_breaks_ties_by_base_score():
    cands = [dict(_cand("A.T", 100.0), base_score=70.0),
             dict(_cand("B.T", 100.0), base_score=90.0),
             dict(_cand("C.T", 95.0), base_score=95.0)]
    assert [c["ticker"] for c in ds.build_deliberation_queue(cands, set(), 3)] == ["B.T", "A.T", "C.T"]


def test_queue_changes_streak_and_dropped():
    hist = {"2026-09-27": [_cand("A.T", 90), _cand("B.T", 80)],
            "2026-09-28": [_cand("A.T", 90), _cand("B.T", 80)]}
    today = [_cand("A.T", 90), _cand("C.T", 85), _cand("B.T", 70)]
    q = ds.build_deliberation_queue(today, set(), 2)
    ch = ds.queue_changes(hist, q, set(), top_n=2, today="2026-09-29", today_cands=today)
    assert ch["streak"] == {"A.T": 3, "C.T": 1}
    assert ch["dropped"][0]["ticker"] == "B.T" and "順位が下がった（今日3位" in ch["dropped"][0]["reason"]


# ── ①: 9指標（富士電機 6504 の実データ・EV/EBITDA は yfinance 9.98 とほぼ一致）──
_R6504 = [(2026, 1227595e6, 136620e6, 98030e6, 842936e6, None, 69873e6, 123562e6, 56711e6, 61778e6),
          (2025, 1123407e6, 117646e6, 92239e6, 730658e6, None, 62675e6, 144920e6, 85185e6, 57341e6),
          (2024, 1103214e6, 106066e6, 75353e6, 661472e6, None, 65543e6, 84858e6, 68311e6, 51875e6),
          (2023, 1009447e6, 88882e6, 61348e6, 572068e6, None, 84165e6, 116163e6, 84147e6, 45938e6),
          (2022, 910226e6, 74835e6, 58660e6, 523729e6, None, 91350e6, 76809e6, 59320e6, 39969e6)]


def test_kpi_from_real_financials():
    rows = [dict(zip(ck._FIELDS, r)) for r in _R6504]
    cand = {"ticker": "6504.T", "market_cap_raw": 1.995e12, "total_debt": 67.0e9,
            "total_cash": 60.0e9, "pbr": 2.52, "per": 20.6}
    k = ck.compute_kpi(cand, rows)
    assert 9.5 < k["ev_ebitda"] < 10.5
    assert 0.09 < k["roic"] < 0.12 and k["irr"] is not None
    assert k["down_sales_years"] == 0 and k["years"] == 5
    assert abs(k["fcf_yield"] - (123562e6 - 56711e6) / 1.995e12) < 1e-4
    assert "負債は直近値で近似" in k["notes"]


def test_irr_simple():
    assert abs(ck.irr([-100, 10, 110]) - 0.10) < 1e-4
    assert ck.irr([100, 10]) is None


# ── ⑤: 落ちた理由 ──
def test_rejection_reasons_are_readable():
    cfg = _types.SimpleNamespace(SCREEN_PER_MAX=30.0, SCREEN_ROE_MIN=0.08)
    r = trc.rejection_reasons({"per": "NG(35.2)", "pbr": "OK(1.1)", "roe": "NG(4.1%)"}, cfg)
    assert r == ["PER 35.2（条件 0超〜30倍）", "ROE 4.1%（条件 8%以上）"]


def test_trace_md_has_three_sections(tmp_path):
    c = _cand("6504.T", 76.9)
    res = {"screener": {"candidates": [c, dict(_cand("9999.T", 40), pass_filters=False,
                                               filter_detail={"per": "NG(45.0)"})]}}
    st.checkpoint("stock_screener", res)
    cfg = _types.SimpleNamespace(DATA_DIR=str(tmp_path / "d"), INSIGHT_DIR=str(tmp_path / "i"))
    out = trc.run(cfg, res)
    md = open(out["path"], encoding="utf-8").read()
    assert "## ✅ 通過 1銘柄" in md and "## 📐 9指標" in md and "PER 45.0" in md
    assert (tmp_path / "d" / "screener_rejections.json").exists()


# ── EdinetDB Web事前確認（財務が無い会社は API で取りに行かず次の候補へ）──
import edinetdb_ingestor as edi
from datetime import date as _date


def test_page_has_financials_markers():
    assert edi.page_has_financials("…開示財務データ…FY2025に売上高83億円…") is True
    assert edi.page_has_financials("…分析に必要なデータが不足しています…") is False
    assert edi.page_has_financials("", 404) is False
    assert edi.page_has_financials(None, 503) is None


def test_prefilter_skips_no_data_and_fills_quota():
    web = {"E1": False, "E2": True, "E3": False, "E4": True, "E5": True}
    meta = {}
    sel, skip = edi.prefilter_targets(["E1", "E2", "E3", "E4", "E5"], 2, meta,
                                      _types.SimpleNamespace(), checker=web.get,
                                      today=_date(2026, 9, 29), sleep=lambda s: None)
    assert sel == ["E2", "E4"] and skip == ["E1", "E3"]
    assert meta["E1"]["web_check"] == {"date": "2026-09-29", "has": False}
    # 翌日は記録を使い、Web を見に行かない
    calls = []
    sel2, _ = edi.prefilter_targets(["E1", "E2"], 2, meta, _types.SimpleNamespace(),
                                    checker=lambda c: calls.append(c) or True,
                                    today=_date(2026, 9, 30), sleep=lambda s: None)
    assert sel2 == ["E2"] and calls == []


def test_prefilter_unknown_falls_back_to_api():
    sel, skip = edi.prefilter_targets(["E1"], 1, {}, _types.SimpleNamespace(),
                                      checker=lambda c: None, sleep=lambda s: None)
    assert sel == ["E1"] and skip == []


def test_ingest_alert_lists_web_skipped():
    al = an.build_edinetdb_ingest_alert({"success": 1, "targets": ["E2"], "ingested": [
        {"sec_code": "3983", "name": "オロ"}], "web_skipped": [{"sec_code": "154A", "name": "GAIA"}]})
    assert "Webで財務なしのため取り込み対象外 1社: 154A GAIA" in al["body"]


def test_alphanumeric_securities_codes():
    """2024年以降の英字入り証券コード（624A 等）が空文字・3桁になっていた"""
    import edinet_bq_client as ebc
    assert [edi._sec4(x) for x in ["624A0", "39830", "154A.T", "ABCD"]] == ["624A", "3983", "154A", ""]
    assert [ebc._fin_code(x) for x in ["624A.T", "7203.T", "285A0"]] == ["624A", "7203", "285A"]
