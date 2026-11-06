# 週次メール・レポートの節の並び（段）・冒頭「今週まず見るもの」・週次で走るロジックの出力の検査
import re
import types
from pathlib import Path


import config
import weekly_report_generator as w
from paper import weekly_section as pw

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    ns = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    ns.INSIGHT_DIR = str(tmp_path / "insights")
    ns.PAPER_LEDGER_PATH = str(tmp_path / "paper")
    ns.DATA_DIR = str(tmp_path / "data")
    return ns


# ---------------------------------------------------------------- 段の表
def test_tier_table_is_complete_and_valid():
    src = (ROOT / "weekly_report_generator.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'\badd\("(\w+)"', src))
    assert keys, "節の識別子を読めない"
    tiers = config.WEEKLY_SECTION_TIER
    assert not (keys - set(tiers)), f"段の表に無い節: {sorted(keys - set(tiers))}"
    assert all(1 <= t <= 5 for t in tiers.values())
    assert set(config.WEEKLY_TIER_LABELS) == {1, 2, 3, 4, 5}
    # 日次の表とは別の dict（名前も中身も共有しない）
    assert config.WEEKLY_SECTION_TIER is not config.ALERT_SECTION_TIER


def test_conclusions_come_before_candidates_before_macro_before_validation():
    t = config.WEEKLY_SECTION_TIER
    assert t["inference"] == t["portfolio_concentration"] == t["paper_weekly"] == t["performance"] == 1
    assert t["screener"] == t["deliberation_queue"] == t["catalyst_upcoming"] == 2
    assert t["macro"] == t["geo_country_risk"] == t["scenario"] == 3
    assert t["universe_factor_ic"] == t["backtest"] == t["self_evaluator"] == 4
    assert t["module_ledger"] == 5


def test_order_sections_is_stable_and_cleans(tmp_path):
    cfg = _cfg(tmp_path)
    secs = [
        ("module_ledger", ["## 運用", "x"]),
        ("screener", ["## 候補A", "a"]),
        ("backtest", ["## 検証", "v"]),
        ("inference", ["## 結論", "c"]),
        ("screener", []),                        # 空は出さない
        ("macro", ["## 見出しだけ", ""]),         # 見出しだけも出さない
        ("dividend_calendar", ["📅 見出しなし", "  本文"]),   # 見出しが無ければ先頭行を見出しに
        ("deliberation_queue", ["## 候補B", "b"]),
    ]
    out, tiers = w.order_sections(secs, cfg)
    heads = [ln for _, lines in out for ln in lines if ln.startswith("## ")]
    assert heads == ["## 結論", "## 候補A", "## 候補B", "## 📅 見出しなし", "## 検証", "## 運用"]
    assert tiers["結論"] == 1 and tiers["候補B"] == 2 and tiers["検証"] == 4 and tiers["運用"] == 5
    assert "見出しだけ" not in "".join(heads)
    assert all(lines[-1] == "" for _, lines in out)


def test_unregistered_section_gets_default_tier(tmp_path):
    cfg = _cfg(tmp_path)
    out, tiers = w.order_sections([("zzz", ["## 新しい節", "x"])], cfg)
    assert tiers["新しい節"] == config.WEEKLY_SECTION_TIER_DEFAULT


# ---------------------------------------------------------------- 全体の組み立て
def test_generated_report_is_in_tier_order_with_first_look_on_top(tmp_path):
    cfg = _cfg(tmp_path)
    results = {
        "total_signal_score": {"total_signal_score": 57.9},
        "signal_aggregator": {"market_regime": "リスクオン", "summary_text": "## 🎯 総合投資シグナル\n\nabc"},
        "performance": {"candidates_n": 200, "win_rate": 0.315, "avg_return": -1.4, "score_return_corr": -0.14,
                        "summary_text": "## 📊 先週の screener 予測精度\n\nx"},
        "dividend_calendar": {"alert_text": "📅 配当権利確定カレンダー（30日以内: 0銘柄）\n  なし"},
        "universe_factor_ic": {"lines": ["■ 日本", "  ✅ x"]},
        "inference": {"summary_md": "## 🧠 今週の投資判断サマリー\n\nhi"},
    }
    r = w.generate_weekly_report(cfg, results)
    md = r["report_md"]
    heads = [ln[3:].strip() for ln in md.split("\n") if ln.startswith("## ")]
    tiers = r["summary"]["section_tiers"]
    assert heads[0] == "👀 今週まず見るもの"
    seq = [tiers[h] for h in heads]
    assert seq == sorted(seq), f"段の順に並んでいない: {seq}"
    assert md.index("今週まず見るもの") < md.index("今週の投資判断サマリー")
    assert md.rstrip().endswith("自動生成: stock_analyzer_main.py | " + r["summary"]["report_date"] + "*")
    first = md.split("## 🧠")[0]
    assert "- **市場**: 総合シグナル 57.9点／レジーム リスクオン" in first
    assert "- **推奨のその後**" in first and "勝率 31.5%" in first and "高スコアほど悪い向き" in first
    n_first = len([ln for ln in first.split("\n") if ln.startswith("- **")])
    assert 1 <= n_first <= config.WEEKLY_FIRST_LOOK_MAX


def test_first_look_lines_are_independent_and_capped(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.WEEKLY_FIRST_LOOK_MAX = 2
    out = w.build_first_look({"total_signal_score": {"total_signal_score": 50.0},
                              "performance": {"candidates_n": 3, "win_rate": 0.5, "avg_return": 1.0}}, cfg)
    assert len(out) <= 2 and out[0].startswith("- **市場**")
    # 何も取れなくても例外にしない
    assert all(x.startswith("- **") for x in w.build_first_look({}, _cfg(tmp_path)))


def test_first_look_reads_queue_head_from_rendered_section(tmp_path):
    cfg = _cfg(tmp_path)
    lines = ["| # | ティッカー | 銘柄名 | スコア |", "|---|---|---|---|", "| **▶ 1** | `6418.T` | 日本金銭機械 | 77.4 |"]
    out = w.build_first_look({}, cfg, summary_hint={"sections": {"deliberation_queue": lines}})
    assert any("審議の先頭 日本金銭機械（6418.T）" in x for x in out)


# ---------------------------------------------------------------- メール
def _big(n):
    return "\n".join(f"| 行{i} | {i * 1234} | +{i}.0% | 説明の文章がここに入る{i} |" for i in range(n))


def test_email_follows_md_order_and_compacts_lower_tiers_within_gmail_limit():
    sections = [("結論A", 1), ("候補B", 2), ("マクロC", 3), ("検証D", 4), ("運用E", 5)]
    md = "# t\n" + "".join(
        f"## {t}\n\n文の行1\n文の行2\n文の行3\n\n| a | b | c | d |\n|---|---|---|---|\n{_big(900)}\n\n" for t, _ in sections)
    tiers = {t: k for t, k in sections}
    h = w.build_weekly_email_html(md, {"report_date": "2026-11-02", "section_tiers": tiers})
    assert len(h.encode("utf-8")) <= w._EMAIL_MAX_BYTES
    pos = [h.index(t) for t, _ in sections]
    assert pos == sorted(pos)
    assert "今週の結論" in h and "来週の候補" in h and "検証" in h
    # 段3以下は全文にしない（表の最終行が載らない）
    assert "行899" not in h


def test_email_without_tier_info_falls_back_to_priority_words():
    md = "# t\n## 雑多な節\n本文\n## 🧠 今週の投資判断サマリー\n結論\n"
    h = w.build_weekly_email_html(md, {"report_date": "2026-11-02"})
    assert h.index("今週の投資判断サマリー") < h.index("雑多な節")


# ---------------------------------------------------------------- 週次で走るロジックの出力
def test_us_macro_section_reads_pipeline_snapshot():
    um = {"macro_snapshot_ext": {"unrate": 4.2, "cpi_yoy": 4.02, "hy_spread": 3.32, "icsa": 189000.0,
                                 "icsa_mom_pct": -4.55, "permit": 1398.0, "permit_mom_pct": -0.36,
                                 "t10y2y": 0.38, "indpro_mom_pct": 0.66},
          "leading_signals": [], "composite_us_macro_score": 66.0}
    body = "\n".join(w._section_us_macro({"us_macro": um}))
    assert "米国マクロ拡張指標" in body and "| 失業率 | 4.2% |" in body and "| CPI前年比 | 4.0% |" in body
    assert "189,000件" in body and "-4.5%" in body and "先行シグナルの発火なし" in body
    assert w._section_us_macro({"us_macro": {"composite_us_macro_score": 50}}) == []
    sig = dict(um, leading_signals=[{"label": "米逆イールド", "signal": "headwind", "detail": "10Y-2Y -0.1%"}])
    assert "📉 米逆イールド" in "\n".join(w._section_us_macro({"us_macro": sig}))


def test_macro_section_shows_cot_position_up_to_three_lines(monkeypatch, tmp_path):
    import cot_signal
    monkeypatch.setattr(cot_signal, "load_signals", lambda cfg=None, today=None: {"as_of": "2026-10-27"})
    monkeypatch.setattr(cot_signal, "mail_lines", lambda s, cfg=None: [f"  市場{i}: 買い越し" for i in range(6)] + ["  （CFTC 2026-10-27 時点・週次）"])
    out = w._section_macro({}, _cfg(tmp_path))
    body = "\n".join(out)
    assert "建玉の偏り" in body and body.count("市場") == 3 and "CFTC 2026-10-27" in body
    monkeypatch.setattr(cot_signal, "mail_lines", lambda s, cfg=None: [])
    assert "建玉の偏り" not in "\n".join(w._section_macro({}, _cfg(tmp_path)))


def test_dcf_section_is_labelled_and_shows_universe_median():
    top = [{"ticker": "7203.T", "name": "Toyota Motor", "dcf": {"intrinsic_per_share": 3000}, "inputs": {"current_price": 2000},
            "discount_pct": 50.0, "dcf_bonus": 4.0}]
    res = {f"{1000 + i}.T": {"discount_pct": 100.0 + i} for i in range(20)}
    body = "\n".join(w._section_dcf({"dcf_valuation": {"top_undervalued": top, "results": res}}))
    assert "割安率" in body and "割引率" not in body and "中央値は +110%" in body and "順位で読む" in body


def test_inference_internal_key_is_dropped():
    out = w._section_inference({"inference": {"summary_md": "a\n**根拠指標**: top_scenario: yield_norm_yen_weak\nb"}})
    assert not any("top_scenario" in x or "根拠指標" in x for x in out)


def test_longterm_summary_does_not_call_neutral_sectors_unfavorable():
    import longterm_scenario as ls
    scores = {f"10{i:02d}": 50.0 for i in range(1, 34)}
    scores.update({"1016": 59.0, "1015": 55.0, "1009": 46.0})
    txt = ls._format_summary({"fast_cooperation_revival": 0.07}, scores, {}, [], types.SimpleNamespace(JAPAN_SECTORS={}))
    down = txt.split("長期不利業種")[1].split("###")[0]
    assert "46pt" in down and "50pt" not in down.replace("50点のまま", "") and "30業種はシナリオの影響表に無く" in down
    up = txt.split("長期有望業種")[1].split("###")[0]
    assert "59pt" in up and "55pt" in up and "50pt" not in up


def test_scenario_oil_is_relative_to_current_level():
    import scenario_analyzer as sa
    snap = {"oil_wti": 101.0, "usdjpy": 157.9}
    bull = sa._apply_scenario_overrides(snap, sa.PRESET_SCENARIOS["bull_case"]["overrides"])
    bear = sa._apply_scenario_overrides(snap, sa.PRESET_SCENARIOS["bear_case"]["overrides"])
    assert bull["oil_wti"] > 101.0 > bear["oil_wti"]          # 原油高のシナリオは現在より高く、景気後退は低い
    assert "oil_wti_x" not in bull and snap["oil_wti"] == 101.0
    none = sa._apply_scenario_overrides({"usdjpy": 150.0}, {"oil_wti_x": 1.2})   # 現在値が無ければ倍率は無視
    assert "oil_wti" not in none


# ---------------------------------------------------------------- 仮想トレードの週の成績
def _acct(hist, npos=1):
    return {"equity_history": [{"date": d, "equity": e} for d, e in hist], "positions": {f"{i}.T": {} for i in range(npos)}}


def test_paper_weekly_uses_equity_a_week_before_latest():
    acc = {"a": _acct([("2026-10-30", 1000000), ("2026-11-04", 1050000), ("2026-11-06", 1100000)], 2),
           "b": _acct([("2026-11-04", 2000000), ("2026-11-06", 1900000)])}      # 履歴が短い＝最初の点が起点
    ev = [{"type": "fill", "strategy": "a", "date": "2026-11-05", "ticker": "7203.T", "side": "SELL", "qty": 100, "price": 3000},
          {"type": "fill", "strategy": "a", "date": "2026-10-01", "ticker": "6758.T", "side": "BUY", "qty": 10, "price": 1}]
    body = "\n".join(pw.weekly_lines(acc, ev, 7))
    assert "| a | 10/30 | 1,100,000円 | +100,000円 | +10.0% | 2銘柄 |" in body
    assert "| b | 11/4 | 1,900,000円 | -100,000円 | -5.0% | 1銘柄 |" in body
    assert "約定 1件（買 0・売 1）" in body and "6758.T" not in body
    assert "最良 a +10.0%" in pw.headline(acc, 7) and "最悪 b -5.0%" in pw.headline(acc, 7)
    assert pw.weekly_lines({}, []) == [] and pw.headline({}) == ""
