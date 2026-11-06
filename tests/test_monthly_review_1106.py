# 月次メールの並び・冒頭・中身のロジック（部分月・常にB象限・実現益不明・PFの数え方）
import json
import types
from pathlib import Path

import pytest

import allocation_meta as am
import bq_storage
import commodity_rotation as cr
import config
import monthly_mail as mm
import tax_optimizer as tx
from paper import monthly_report as mr


# ---- allocation_meta: 月次リターンの部分月 -------------------------------------------------
def _hist(rows):
    return [{"date": d, "equity": e} for d, e in rows]


def test_monthly_returns_drop_partial_current_month():
    h = _hist([("2026-09-30", 100), ("2026-10-01", 101), ("2026-10-30", 110), ("2026-11-02", 111)])
    assert am.monthly_returns(h) == pytest.approx([0.10, 111 / 110 - 1])        # 上限なしなら部分月も入る
    assert am.monthly_returns(h, "2026-10") == pytest.approx([0.10])            # 対象月までに限る


def test_monthly_returns_first_partial_month_is_base_only():
    h = _hist([("2026-09-15", 100), ("2026-09-30", 120), ("2026-10-30", 132)])
    assert am.monthly_returns(h, "2026-10") == pytest.approx([0.10])            # 9月は途中からなので基準にだけ使う


def test_allocation_not_sufficient_on_two_full_months_plus_one_day(tmp_path):
    """12/1 の作成時、11月までの完全な月は2か月。12/1 の1日分を3か月目に数えない"""
    h = _hist([("2026-09-30", 100), ("2026-10-30", 101), ("2026-11-30", 103), ("2026-12-01", 104)])
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "accounts.json").write_text(json.dumps({"s": {"equity_history": h}}), encoding="utf-8")
    cfg = types.SimpleNamespace(PAPER_LEDGER_PATH=str(paper))
    returns, _, _ = am._load_paper_returns(cfg, "2026-11")
    assert len(returns["s"]) == 2
    plan = am.build_allocation(returns, {}, {}, 1e6, 1e5, cfg)
    assert plan["insufficient"] == ["s"]
    assert "（2/3か月）" in am.build_report(plan, "202611")


# ---- commodity_rotation ------------------------------------------------------------------
def test_history_days_cover_252_trading_days():
    assert _bars(cr._history_days(252)) > 252


def _bars(calendar_days):          # 暦日→営業日の概算（週5日・祝日の余裕で0.68）
    return int(calendar_days * 0.68)


def test_latest_cycle_phase_reads_history(tmp_path):
    (tmp_path / "cycle_phase_history.json").write_text(
        json.dumps([{"date": "2026-11-01", "phase": "B"}, {"date": "2026-11-02", "phase": "A"}]), encoding="utf-8")
    cfg = types.SimpleNamespace(DATA_DIR=tmp_path)
    assert cr.latest_cycle_phase(cfg) == "A"
    assert cr.latest_cycle_phase(types.SimpleNamespace(DATA_DIR=tmp_path / "none")) == "B"


def test_monthly_run_uses_actual_phase_not_always_b(tmp_path):
    """月次は日次の results を持たない。象限を履歴から読まないと常に B（現金）になる"""
    (tmp_path / "cycle_phase_history.json").write_text(json.dumps([{"date": "2026-11-02", "phase": "A"}]), encoding="utf-8")
    cfg = types.SimpleNamespace(DATA_DIR=tmp_path, CMD_ETF_UNIVERSE={"1671.T": "energy", "1326.T": "gold"},
                                CMD_REGIME_ALLOW=config.CMD_REGIME_ALLOW, CMD_MAX_ALLOC_PCT=10.0,
                                CMD_MOM_LOOKBACK=(252, 21), PROJECT_DIR=str(tmp_path))
    closes = {"1671.T": [100.0] * 100 + [150.0] * 180, "1326.T": [100.0] * 280}
    plan = cr.run_commodity_rotation(cfg, {}, closes_by_ticker=closes)
    assert plan["regime"] == "A" and "1671.T" in plan["allocations"]


def test_commodity_report_lists_momentum_and_flags_missing_prices():
    plan = {"regime": "A", "allowed_categories": ["energy"], "allocations": {}, "momentum": {"1671.T": None},
            "_universe": {"1671.T": "energy"}, "total_pct": 0.0, "cash_pct": 100.0}
    md = cr.build_report(plan, "202611")
    assert "価格不足" in md and "取得の失敗" in md


# ---- tax_optimizer -----------------------------------------------------------------------
def test_unknown_realized_gain_is_not_reported_as_zero():
    h = tx.calc_tax_harvest([{"ticker": "7011.T", "unrealized_pnl": -44600, "account_type": "特定"}], None)
    assert h["realized_gain"] is None and h["tax_saving"] is None
    md = tx.build_report(h, [], {"used": 0, "total_limit": 1, "total_remaining": 1, "annual_limit": 1}, 2026)
    assert "不明" in md and "相殺可能額: ¥0" not in md
    line, ask = mm.summarize_report("tax_report_2026.md", md)
    assert "不明" in line and not ask


def test_realized_gain_none_without_file(tmp_path):
    cfg = types.SimpleNamespace(INVESTMENT_DEPT_VAULT_DIR=str(tmp_path / "v"), DATA_DIR=tmp_path)
    assert tx._realized_gain_ytd(cfg, 2026) is None


# ---- paper の PF は金額で数える ------------------------------------------------------------
def test_pf_is_money_weighted_and_month_realized_column():
    ev = [{"type": "fill", "strategy": "s", "date": "2026-10-01", "ticker": "A", "side": "BUY", "qty": 1000, "price": 100},
          {"type": "fill", "strategy": "s", "date": "2026-10-02", "ticker": "A", "side": "SELL", "qty": 1000, "price": 99},
          {"type": "fill", "strategy": "s", "date": "2026-10-03", "ticker": "B", "side": "BUY", "qty": 1, "price": 100},
          {"type": "fill", "strategy": "s", "date": "2026-10-04", "ticker": "B", "side": "SELL", "qty": 1, "price": 150},
          {"type": "fill", "strategy": "s", "date": "2026-11-02", "ticker": "C", "side": "BUY", "qty": 1, "price": 100},
          {"type": "fill", "strategy": "s", "date": "2026-11-03", "ticker": "C", "side": "SELL", "qty": 1, "price": 100}]
    trips = mr.round_trips(ev, "s", "2026-10")
    j = mr.judge_promotion({}, None, trips=trips)
    assert j["pf"] == pytest.approx(50 / 1000)          # 金額なら 50/1000。騰落率の合計なら 0.5/0.01 で 50 倍の差
    acc = {"s": {"equity_history": [{"date": "2026-09-30", "equity": 100}, {"date": "2026-10-30", "equity": 99}]}}
    md = mr.build_report(acc, "202610", None, ev)
    assert "月の実現損益" in md and "| -950 | 1/1 |" in md        # 11月の約定は10月の成績に入らない


# ---- monthly_mail: 並び・冒頭・大きさ・BQ の行 ---------------------------------------------
def test_tier_dict_does_not_collide_with_daily_triggers():
    assert not set(config.MONTHLY_SECTION_TIER) & set(config.ALERT_SECTION_TIER)
    assert set(config.MONTHLY_TIER_LABELS) == {1, 2, 3, 4, 5}
    assert set(config.MONTHLY_SECTION_TIER.values()) <= set(config.MONTHLY_TIER_LABELS)
    for _pre, key in mm._KEY_PREFIX:
        assert key in config.MONTHLY_SECTION_TIER


def test_signal_culling_with_proposals_moves_up():
    t = config.MONTHLY_SECTION_TIER
    assert mm.tier_of("signal_culling", "提案あり: 0件 / 判定不能: 3件", t, 3) == 3
    assert mm.tier_of("signal_culling", "提案あり: 2件 / 判定不能: 1件", t, 3) == 2


def _write_reports(d: Path, positions: int = 5):
    (d / "macro").mkdir(exist_ok=True)
    (d / "strategy_review_202610.md").write_text("# r\n## ① x\n- 審議\n", encoding="utf-8")
    (d / "signal_culling_202610.md").write_text("# s\n提案あり: 0件 / 判定不能: 23件\n", encoding="utf-8")
    rows = "\n".join(f"| s | T{i}.T | 10 | 1,000.0 |" for i in range(positions))
    (d / "paper_202610.md").write_text(
        "| 戦略 | a | b | c | d | e | f | g |\n|---|---|---|---|---|---|---|---|\n"
        "| pead | 2,000,000 | 1,990,000 | -10,000 | -0.50% | 24 | -3,000 | 1/2 |\n"
        "## 保有\n| 戦略 | t | q | c |\n|---|---|---|---|\n" + rows + "\n", encoding="utf-8")
    (d / "allocation_proposal_202610.md").write_text("> カジノ枠: ¥700,000 / 0戦略に配分\n", encoding="utf-8")
    (d / "commodity_alloc_202611.md").write_text("象限: **A**\nコモディティ配分合計: 0.0%\n", encoding="utf-8")
    (d / "macro" / "monthly_trend_2026-10.md").write_text("# 長期\n- 銀行\n", encoding="utf-8")


def test_sections_ordered_by_tier_with_first_look_on_top(tmp_path):
    _write_reports(tmp_path)
    picks = {"title": "📈 推奨のその後（2026-10 の成績表）", "body": "  【日本】\n    全体: 20日 +1.0%"}
    _s, body, n = mm.build("202610", tmp_path, bq_line="BigQuery: テスト", picks=picks)
    assert body.index("今月まず見るもの") < body.index("■ 今月の成績")
    order = ["■ 今月の成績", "ペーパー取引の月次成績", "推奨のその後", "■ 来月の方針と承認が要るもの",
             "資産配分の提案", "コモディティ配分", "■ 検証", "月次戦略レビュー", "シグナル淘汰", "■ 市場・業種の月次の振り返り",
             "市場・業種の月次の振り返り", "■ 運用", "BigQuery の使用量と保存量"]
    at = body.index("■ 今月の成績")
    for x in order:                       # 冒頭の「今月まず見るもの」より後で、この順に現れる
        nxt = body.index(x, at)
        assert nxt >= at
        at = nxt
    assert "うち売って確定した分 -3,000円" in body


def test_first_look_has_3_to_5_lines_and_failed_steps_first(tmp_path):
    _write_reports(tmp_path)
    files = mm.report_files(tmp_path, "202610")
    h = mm.first_look_html(files, ["signal_culling"], [("BigQuery", "保存 90%")])
    items = h.count("<li>")
    assert 3 <= items <= 5 and h.index("作れなかったステップ") < h.index("ペーパー")
    assert "BigQuery" in h        # 運用の注意は5行に収まる位置に入る


def test_mail_under_gmail_limit_even_with_many_positions(tmp_path):
    _write_reports(tmp_path, positions=900)
    _s, body, _n = mm.build("202610", tmp_path, bq_line="BigQuery: テスト")
    assert len(body.encode("utf-8")) < 102_000


def test_bq_usage_line_names_quota_period_and_amount():
    usage = {"jobs": 1234, "billed_gib": 102.4, "max_day": "2026-10-12", "max_day_gib": 15.35, "regions_ok": ["a", "b"]}
    line = bq_storage.usage_line("202610", usage, {"total_gb": 5.06, "measured_at": "2026-11-06T12:53:09"})
    assert "10月" in line and "1TiB" in line and "10.0%" in line and "上限 30.7GiB/日の 50%" in line
    assert "無料枠 10GB" in line and "5.06GB（枠の 51%）" in line
    assert "取得できず" in bq_storage.usage_line("202610", None, None) and "未測定" in bq_storage.usage_line("202610", None, None)
    assert bq_storage.month_bounds("202612") == ("2026-12-01", "2027-01-01")


def test_bq_not_queried_unless_enabled():
    assert mm.fetch_bq_extras("202610", types.SimpleNamespace()) == ("", None)
