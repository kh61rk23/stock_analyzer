# 月次（メール・monthly_runner・戦略レビュー・ペーパー成績・シグナル淘汰の入力・ルール重みの月次判定）の修正
import ast
import json
import types
from datetime import date
from pathlib import Path

from conftest import load_module

REPO = Path(__file__).parent.parent


# ── monthly_runner: 回数の上限・メールは他が終わってから ─────────────

def _fake_today(mr, d):
    return type("D", (), {"today": staticmethod(lambda: d)})


def _run_month(mr, monkeypatch, tmp_path, fail: set, day: date):
    monkeypatch.setattr(mr, "STATUS_PATH", tmp_path / "monthly_loop_status.json")
    monkeypatch.setattr(mr, "date", _fake_today(mr, day))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        name = Path(cmd[1]).stem
        return types.SimpleNamespace(returncode=1 if name in fail else 0, stdout="", stderr="boom")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    rc = mr.main([])
    st = json.loads((tmp_path / "monthly_loop_status.json").read_text(encoding="utf-8"))
    return rc, st, calls


def test_mail_waits_then_gives_up(monkeypatch, tmp_path):
    mr = load_module("monthly_runner")
    # 1日目: signal_culling が失敗 → メールは待つ
    rc, st, calls = _run_month(mr, monkeypatch, tmp_path, {"signal_culling_report"}, date(2026, 11, 1))
    assert rc == 1
    assert st["steps"]["monthly_mail"].startswith("waiting")
    assert not any("monthly_mail.py" in c[1] for c in calls)
    assert st["attempts"]["signal_culling"] == 1
    # 2日目・3日目も失敗 → 3回目で上限
    _run_month(mr, monkeypatch, tmp_path, {"signal_culling_report"}, date(2026, 11, 2))
    rc, st, calls = _run_month(mr, monkeypatch, tmp_path, {"signal_culling_report"}, date(2026, 11, 3))
    assert st["attempts"]["signal_culling"] == 3
    # 4日目: もう試さず諦め、メールは諦めた旨を付けて送る
    rc, st, calls = _run_month(mr, monkeypatch, tmp_path, {"signal_culling_report"}, date(2026, 11, 4))
    assert st["steps"]["signal_culling"].startswith("gave_up")
    assert not any("signal_culling_report.py" in c[1] for c in calls)
    mail = [c for c in calls if "monthly_mail.py" in c[1]]
    assert mail and mail[0][-2:] == ["--gave-up", "signal_culling"]
    assert st["steps"]["monthly_mail"] == "success" and rc == 0
    # 5日目: 全部済みなので何もしない
    rc, st, calls = _run_month(mr, monkeypatch, tmp_path, set(), date(2026, 11, 5))
    assert calls == [] and rc == 0


def test_mail_blockers():
    mr = load_module("monthly_runner")
    assert mr.mail_blockers({"a": "success", "b": "skipped / x", "c": "gave_up / 3回",
                             "monthly_mail": "waiting"}) == []
    assert mr.mail_blockers({"a": "exit 1 / boom", "b": "success"}) == ["a"]


# ── 月次メール ───────────────────────────────────────────────

def test_monthly_mail_tax_only_in_tax_months(tmp_path):
    mm = load_module("monthly_mail")
    for f in ("strategy_review_202609.md", "tax_report_2026.md", "strategy_review_202610.md"):
        (tmp_path / f).write_text("# x\n", encoding="utf-8")
    t9 = [t for t, _ in mm.report_files(tmp_path, "202609")]      # 10月に送る＝実施月
    t10 = [t for t, _ in mm.report_files(tmp_path, "202610")]     # 11月に送る＝実施月
    (tmp_path / "strategy_review_202611.md").write_text("# x\n", encoding="utf-8")
    t11 = [t for t, _ in mm.report_files(tmp_path, "202611")]     # 12月は実施月でない
    assert any("税" in t for t in t9) and any("税" in t for t in t10)
    assert not any("税" in t for t in t11)


def test_monthly_mail_inline_italic_and_bold():
    mm = load_module("monthly_mail")
    assert mm._inline("*自動では変更しない。* と **太字**") == "<i>自動では変更しない。</i> と <b>太字</b>"
    assert mm._inline("2*3*4") == "2*3*4"


def test_monthly_mail_empty_is_failure_and_mac_does_not_send(tmp_path, monkeypatch):
    mm = load_module("monthly_mail")
    monkeypatch.setattr(mm, "REPO", tmp_path)
    sent = []
    import alert_notifier
    monkeypatch.setattr(alert_notifier, "send_gmail", lambda *a, **k: (sent.append(a), (True, ""))[1])

    class Cfg:
        INSIGHT_DIR = tmp_path

    import sys
    monkeypatch.setitem(sys.modules, "config", Cfg)
    assert mm.main(["202609"]) == 1                       # レポート0本は失敗
    (tmp_path / "strategy_review_202609.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert mm.main(["202609"]) == 0 and sent == []        # Actions の外では送らない
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert mm.main(["202609", "--gave-up", "signal_culling"]) == 0 and len(sent) == 1
    assert "signal_culling" in sent[0][4]


def test_monthly_mail_reads_insight_dir_setting():
    text = (REPO / "monthly_mail.py").read_text(encoding="utf-8")
    assert '"INSIGHTS_DIR"' not in text and '"INSIGHT_DIR"' in text


# ── 月次戦略レビュー ─────────────────────────────────────────

def test_strategy_review_sources():
    sr = load_module("monthly_strategy_review")
    perf = {"log_entries": [
        {"week": "2026-W41", "evaluated": 173, "win_rate_overall": 0.474, "win_rate_score80+": 0.464,
         "win_rate_60_80": 0.511, "win_rate_40_60": 0, "win_rate_sub40": 0, "avg_alpha": -0.062},
        {"week": "2026-W36", "evaluated": 10, "win_rate_overall": 0.5},
    ]}
    tbl = sr._score_band_table(perf, "202610")
    assert len(tbl) == 3 and "2026-W41" in tbl[2] and "47%" in tbl[2]
    assert sr._iso_week_month("2026-W40") == "2026-10"    # 木曜 10/1 → 10月
    recs = [{"deliberated_at": "2026-09-26T03:00:00+00:00", "verdict": "Watch", "approval_status": "承認"},
            {"deliberated_at": "2026-09-01T03:00:00+00:00", "verdict": "Avoid"},
            {"deliberated_at": "2026-10-01T03:00:00+00:00", "verdict": "Buy"}]
    assert sr._count_deliberations_month(recs, "202609") == {
        "count": 2, "by_verdict": {"Watch": 1, "Avoid": 1}, "approved": 1}
    md = sr.build_review({"learning_audit": {"factors_updated": [
        {"factor": "yen_depreciation", "old_weight": 0.95, "new_weight": 1.12}]},
        "deliberations_month": {"count": 2, "by_verdict": {"Watch": 2}, "approved": 0}}, "202609")
    assert "yen_depreciation: 0.95 → 1.12" in md and "当月の審議: 2件" in md


# ── ペーパー成績 ─────────────────────────────────────────────

def test_paper_month_start_positions_and_fills():
    from paper import monthly_report as pm
    hist = [{"date": "2026-09-30", "equity": 2000000.0}, {"date": "2026-10-01", "equity": 2019248.0},
            {"date": "2026-10-17", "equity": 1946918.0}]
    assert pm.month_start_equity(hist, "2026-10") == 2000000.0
    assert pm.month_start_equity(hist[:1], "2026-09") == 2000000.0
    events = [{"type": "init", "strategy": "s", "cash": 100.0},
              {"type": "fill", "strategy": "s", "date": "2026-10-02", "ticker": "A", "side": "BUY", "qty": 1, "price": 10}]
    assert pm.positions_at_month_end(events, "2026-09") == {"s": {}}
    assert pm.positions_at_month_end(events, "2026-10")["s"]["A"]["qty"] == 1
    md = pm.build_report({"s": {"equity_history": hist, "positions": {"A": {"qty": 1, "avg_cost": 10}}}},
                         "202609", None, events=events)
    sect = md[md.index("## 保有"):]
    assert "月末時点" in sect and "| s | A |" not in sect          # 9月末にはまだ持っていない
    assert "| s | 0 |" in md                                     # 9月までの約定は0件


# ── シグナル淘汰の入力（カテゴリパネル）は signal_aggregator の後で書く ──

def test_category_panel_written_after_signal_aggregator():
    src = (REPO / "stock_analyzer_main.py").read_text(encoding="utf-8")
    agg = src.index('results["signal_aggregator"] = sa_result')
    panel = src.index("record_category_panel(cfg, results)")
    assert panel > agg


def test_learning_audit_not_inside_except():
    tree = ast.parse((REPO / "stock_analyzer_main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr == "generate_audit_log":
                    raise AssertionError("learning_audit の生成が except 節の中にある（成功した日に動かない）")


# ── ルール重み・IC の月次判定 ─────────────────────────────────

def test_rule_optimizer_month_end_window(tmp_path):
    ro = load_module("rule_optimizer")
    # 2026-10 の最後の7日間（25〜31日）に入る月曜は 26日だけ
    mondays = [d for d in range(1, 32) if date(2026, 10, d).weekday() == 0]
    assert [d for d in mondays if ro.in_month_end_window(date(2026, 10, d))] == [26]
    # 月末が日曜の月（2026-05-31）でも、その前の月曜（25日）で動く
    assert ro.in_month_end_window(date(2026, 5, 25)) and not ro.in_month_end_window(date(2026, 5, 18))
    cfg = types.SimpleNamespace(DATA_DIR=tmp_path)
    assert not ro.updated_this_month(cfg, date(2026, 10, 26))
    (tmp_path / "rule_optimizer_meta.json").write_text(json.dumps({"last_update_month": "2026-10"}), encoding="utf-8")
    assert ro.updated_this_month(cfg, date(2026, 10, 27))
    assert not ro.updated_this_month(cfg, date(2026, 11, 30))


# ── 実行台帳: 確定後に届いた外側ステップの記録で「記録が来ない」を取り消す ──

def test_late_record_clears_external_stale(tmp_path):
    ml = load_module("module_ledger")
    cfg = types.SimpleNamespace(LOG_DIR=tmp_path)
    day = "2026-10-19"
    daily = {"date": day, "modules": {"a": {"status": "success"}},
             "stats": {"success": 1, "error": 0, "skipped": 0, "warn": 0, "total": 1},
             "external_stale": {"bi_export": 14, "daily_state_push": 14},
             "alerts": [{"kind": "external_stale", "module": "bi_export", "days": 14, "note": ""},
                        {"kind": "external_stale", "module": "daily_state_push", "days": 14, "note": ""},
                        {"kind": "consecutive_failure", "module": "x", "days": 3, "note": ""}]}
    (tmp_path / f"module_ledger_{day}.json").write_text(json.dumps(daily), encoding="utf-8")
    (tmp_path / "module_ledger_latest.json").write_text(json.dumps(daily), encoding="utf-8")
    ml.record_external(cfg, "bi_export", "success", "step", date_str=day)
    for name in (f"module_ledger_{day}.json", "module_ledger_latest.json"):
        d = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        assert d["external_stale"] == {"daily_state_push": 14}
        assert [a["module"] for a in d["alerts"]] == ["daily_state_push", "x"]
        assert d["stats"]["success"] == 2 and d["stats"]["total"] == 2


# ── 運用の監視: 保有銘柄の要約はリポジトリ直下の data/ にある ─────────

def test_holdings_summary_path_is_repo_data():
    oh = load_module("ops_health")
    sh = load_module("sync_holdings")
    assert oh.HOLDINGS_SUMMARY == sh.OUT_SUMMARY


# ── ポジションサイジング: results["macro"] は True（辞書ではない）──────

def test_position_sizer_handles_macro_flag(tmp_path, monkeypatch):
    import position_sizer as ps
    monkeypatch.setattr(ps, "calc_atr", lambda t: {"atr14": 10.0, "current_price": 1000.0,
                                                   "price_52w_high": 1100.0, "price_52w_low": 900.0})
    monkeypatch.setattr(ps, "FETCH_DELAY", 0)
    cfg = types.SimpleNamespace(DATA_DIR=tmp_path, DRIVE_BASE=tmp_path)
    r = ps.run_position_sizer(cfg, 10_000_000, tickers_override=["NVDA"],
                              results={"macro": True, "macro_snapshot": {"usdjpy": 150.0}},
                              screener_result={"candidates": []})
    assert len(r["positions"]) == 1 and not r["positions"][0].get("error")


# ── 地政学: EDINET 本文は1クエリでまとめて取る ─────────────────────

def test_edinet_exposure_prefetches_in_one_query(monkeypatch):
    import sys
    queries = []

    class _Row:
        def __init__(self, code, sec, text):
            self.company_code, self.section_type, self.text_body = code, sec, text

    class _Client:
        def query(self, q, job_config=None):
            queries.append(q)
            rows = [_Row("1605", "事業の概況", "海外" * 80), _Row("8058", "海外事業", "")]
            return types.SimpleNamespace(result=lambda: rows)

    bq = types.ModuleType("edinet_bq_client")
    bq.EDINETBQClient = lambda: types.SimpleNamespace(client=_Client())
    html_calls = []
    te = types.ModuleType("text_extractor")
    te.extract_sections = lambda cfg, code: (html_calls.append(code), {"sections": {}})[1]
    monkeypatch.setitem(sys.modules, "edinet_bq_client", bq)
    monkeypatch.setitem(sys.modules, "text_extractor", te)
    m = load_module("edinet_exposure_extractor")
    cfg = types.SimpleNamespace(GEO_EDINET_HTML_FALLBACK_MAX=1)
    m.run_edinet_exposure_extractor(cfg, target_tickers=["1605.T", "8058.T", "9999.T"], dry_run=True)
    assert len(queries) == 1 and "UNNEST(@codes)" in queries[0]
    assert html_calls == ["8058"]          # 本文が無い会社だけ・上限1社まで


# ── 月次メールの要点 ─────────────────────────────────────────

def test_monthly_mail_summary_lines():
    mm = load_module("monthly_mail")
    assert mm.summarize_report("signal_culling_202611.md", "提案あり: 2件 / 判定不能: 21件") == (
        "重みの変更提案 2件・判定不能 21件", True)
    line, ask = mm.summarize_report("paper_202610.md",
                                    "| pead | 2,000,000 | 2,014,145 | +14,145 | +0.71% | 14 |\n"
                                    "| screener_topN | 2,000,000 | 1,946,918 | -53,082 | -2.65% | 14 |")
    assert line.startswith("月次損益 合計 -38,937円") and "最良 pead" in line and not ask
    assert mm.summarize_report("commodity_alloc_202611.md",
                               "- 現在の経済サイクル象限: **A** / x\n- コモディティ配分合計: 6.0% / 現金") == (
        "象限 A ／ 配分合計 6.0%", True)
    assert mm.summarize_report("unknown.md", "x") == ("", False)


# ── 日次メール: 米国株スクリーナーの銘柄を国別に分ける ───────────────

def test_country_of_and_split():
    import alert_notifier as an
    assert [an.country_of(t) for t in ["GOOGL", "BRK-B", "000660.KS", "SESG.PA", "HBM.TO", "6758.T", "X.ZZ"]] == [
        "米国", "米国", "韓国", "フランス", "カナダ", "日本", "その他（.ZZ）"]
    lines = an.build_intl_pick_lines([{"ticker": "000660.KS", "composite_score": 88},
                                      {"ticker": "005930.KS", "composite_score": 85},
                                      {"ticker": "HBM.TO", "composite_score": 90}], per_country=1)
    assert lines[0].startswith("  【カナダ】1社") and lines[2].startswith("  【韓国】2社通過（上位1社）")
    assert len(lines) == 4


def test_us_summary_excludes_non_us_and_sections_survive():
    import alert_notifier as an
    import config as cfg
    picks = [{"ticker": "GOOGL", "composite_score": 99}, {"ticker": "000660.KS", "composite_score": 88}]
    al = an.extract_alerts(cfg, {"us_screener": {"top_picks": picks, "pass_count": 2, "screened_count": 9,
                                                  "candidates": [dict(p, div_yield=0.05) for p in picks]}})
    by = {a["trigger"]: a for a in al}
    assert "000660.KS" not in by["us_summary"]["body"] and "GOOGL" in by["us_summary"]["body"]
    assert "【韓国】" in by["intl_summary"]["body"]
    assert "海外1社" in by["high_dividend"]["title"]
    # EdinetDB 節を通らない日でも、Path を使う節（不動産DB 等）が UnboundLocalError で消えない
    assert "real_estate" in by


def test_bi_export_skips_unchanged_copies():
    import bi_export as b
    from datetime import datetime, timezone
    src = b.SOURCES[-1]
    want = src["columns"] + src.get("optional_columns", [])

    class C:
        def __init__(self, s_mod, d_mod, cols=want):
            self.m = {src["source"]: types.SimpleNamespace(modified=s_mod, schema=[], num_rows=5),
                      b.table_ref(src["dest"]): types.SimpleNamespace(
                          modified=d_mod, num_rows=7, schema=[types.SimpleNamespace(name=c) for c in cols])}

        def get_table(self, ref):
            return self.m[ref]

    t1, t2 = datetime(2026, 10, 10, tzinfo=timezone.utc), datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert b.copy_is_current(C(t1, t2), src) == 7          # 元表は写し以降変わっていない
    assert b.copy_is_current(C(t2, t1), src) is None       # 元表が新しい
    assert b.copy_is_current(C(t1, t2, want[:-1]), src) is None   # 列の定義が変わった
    assert b.copy_is_current(object(), src) is None        # 判断できない
