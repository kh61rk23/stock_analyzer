# 設計監査（2026-10-18）の高・中の対応: 外側ステップの台帳化・Actions 使用量・運用の監視・
# Mac 同期状況・保有銘柄の generated・キー判定・サブテーマ表の置き換え・米国株価の週3回
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

from conftest import load_module

REPO = Path(__file__).parent.parent


# ── 米国株価の取得は週3回（UTC 月・水・金 = JST 火・木・土）─────────────

def test_price_universe_us_runs_three_times_a_week():
    wf = yaml.safe_load((REPO / ".github/workflows/price_universe.yml").read_text(encoding="utf-8"))
    crons = [c["cron"] for c in wf[True]["schedule"]]
    assert "47 7 * * 1-5" in crons          # 日本株は従来どおり平日毎日
    assert "47 22 * * 1,3,5" in crons       # 米国株は週3回
    assert len(crons) == 2


def test_price_universe_daily_period_covers_the_gap():
    pu = load_module("price_universe")
    # 最も空く 土→火 でも2営業日。1回失敗しても次の回で取り戻せる幅（5営業日）を取る
    assert pu.DAILY_PERIOD == "5d"


# ── 外側ステップの outcome を台帳へ ──────────────────────────────

def test_parse_step_outcomes():
    ml = load_module("module_ledger")
    got = ml.parse_step_outcomes(["bi_export=failure", "a=success", "b=skipped", "c=cancelled",
                                  "d=", "e", "=success"])
    assert got == {"bi_export": "error", "a": "success", "b": "skipped", "c": "error"}


def test_steps_cli_records_external(tmp_path, monkeypatch):
    ml = load_module("module_ledger")
    monkeypatch.setenv("STOCK_ANALYZER_BASE", str(tmp_path))
    assert ml.main(["steps", "bi_export=failure", "monthly_runner="]) == 0
    files = list((tmp_path / "logs").glob("module_ledger_external_*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["bi_export"]["status"] == "error"
    assert "monthly_runner" not in rec      # 走らなかったステップは記録しない


def test_daily_run_records_outer_steps_and_push():
    text = (REPO / ".github/workflows/daily_run.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    steps = wf["jobs"]["run-analyzer"]["steps"]
    ids = {s.get("id") for s in steps}
    for need in ("db-schema", "taxonomy", "web-refresh", "analyzer", "db-catalog", "monthly",
                 "report-store", "bi-export"):
        assert need in ids, need
    rec = next(s for s in steps if s.get("name", "").startswith("📒 Record outer"))
    assert rec["if"] == "always()"
    for name in ("bi_export=${{ steps.bi-export.outcome }}",
                 "edinetdb_web_refresh=${{ steps.web-refresh.outcome }}"):
        assert name in rec["run"]
    # 保存の印は commit（git）と save（BigQuery）の両方に載るよう、その前のステップで書く
    names = [s.get("name", "") for s in steps]
    mark = next(s for s in steps if s.get("name", "").startswith("🏷️ Mark state"))
    assert "daily_state_push=success" in mark["run"] and mark["if"] == "always()"
    i_mark = names.index(mark["name"])
    i_save = next(i for i, n in enumerate(names) if n.startswith("☁️ Save state"))
    i_commit = next(i for i, n in enumerate(names) if n.startswith("📊 Commit"))
    assert i_mark < i_save < i_commit
    assert "git add stock_data/data/" in steps[i_commit]["run"]
    assert wf["permissions"]["actions"] == "read"


def test_ledger_expects_outer_steps():
    cfg = load_module("config")
    for name in ("bi_export", "edinetdb_web_refresh", "daily_state_push"):
        assert cfg.LEDGER_EXTERNAL_EXPECT[name] >= 3   # 日曜休み（土→月）で鳴らない


# ── 週次の追いつき ──────────────────────────────────────────────

def test_weekly_catch_up_marker_written_only_on_weekly_success():
    text = (REPO / ".github/workflows/daily_run.yml").read_text(encoding="utf-8")
    assert "last_weekly_run.json" in text
    assert '"$AGE" -ge 8' in text
    assert 'steps.analyzer.outcome }}" = "success"' in text


# ── Actions 使用量 ──────────────────────────────────────────────

def test_actions_run_minutes_and_summary():
    au = load_module("actions_usage")
    runs = [
        {"name": "daily", "status": "completed", "run_started_at": "2026-10-17T04:20:00Z",
         "updated_at": "2026-10-17T05:11:10Z"},                     # 51分10秒 → 52分
        {"name": "daily", "status": "in_progress", "run_started_at": "2026-10-18T04:20:00Z",
         "updated_at": "2026-10-18T04:30:00Z"},                     # 実行中は数えない
        {"name": "price", "status": "completed", "run_started_at": "2026-10-16T13:00:00Z",
         "updated_at": "2026-10-16T13:10:00Z"},                     # 10分
        {"name": "price", "status": "completed", "run_started_at": "2026-09-30T13:00:00Z",
         "updated_at": "2026-09-30T13:10:00Z"},                     # 前月は数えない
    ]
    s = au.summarize(runs, "2026-10")
    assert s["minutes"] == 62 and s["runs"] == 2
    assert s["by_workflow"] == {"daily": 52, "price": 10}


def test_actions_usage_issue_threshold():
    oh = load_module("ops_health")
    today = date(2026, 10, 17)
    low = {"month": "2026-10", "minutes": 900, "by_workflow": {}}
    assert oh.actions_usage_issues(low, 2000, 0.8, today) == []
    high = {"month": "2026-10", "minutes": 1911, "by_workflow": {"daily": 781}}
    got = oh.actions_usage_issues(high, 2000, 0.8, today)
    assert got and got[0]["severity"] == "high" and "1,911/2,000分" in got[0]["text"]
    # 今は8割未満でも、日割りの月末見込みが枠を超えるなら知らせる
    pace = {"month": "2026-10", "minutes": 1300, "by_workflow": {}}
    assert oh.actions_usage_issues(pace, 2000, 0.8, date(2026, 10, 15))
    # 前月のファイルは見ない
    assert oh.actions_usage_issues({"month": "2026-09", "minutes": 1999}, 2000, 0.8, today) == []


# ── Mac 同期・週次ループ ────────────────────────────────────────

def test_mac_sync_issues():
    oh = load_module("ops_health")
    now = datetime(2026, 10, 18, 14, 0)
    assert oh.mac_sync_issues(None, now, 48, 9)[0]["severity"] == "medium"
    fresh = {"last_success_at": "2026-10-18 10:30:00", "steps": {"pull": True},
             "weekly_loop": {"last_success": "2026-10-12 21:00"}}
    assert oh.mac_sync_issues(fresh, now, 48, 9) == []
    stale = {"last_success_at": "2026-10-15 10:30:00", "steps": {"pull": False, "mirror": True},
             "weekly_loop": {"last_success": "2026-08-01 21:00"}}
    got = oh.mac_sync_issues(stale, now, 48, 9)
    assert got[0]["severity"] == "high" and "pull" in got[0]["text"]
    assert "週次ループ" in got[1]["text"]


def test_obsidian_status_build_and_push_rule():
    os_ = load_module("obsidian_sync")
    now = datetime(2026, 10, 18, 10, 0)
    st = os_.build_mac_status(None, {"pull": True, "rules": True}, now, {"last_success": "x"})
    assert st["last_ok"] and st["last_success_at"] == "2026-10-18 10:00:00"
    bad = os_.build_mac_status(st, {"pull": False}, datetime(2026, 10, 18, 10, 30), None)
    assert not bad["last_ok"] and bad["last_success_at"] == "2026-10-18 10:00:00"
    assert bad["weekly_loop"] == {"last_success": "x"}      # 読めなかった回は前回の値を残す
    # 成否が変わったら送る／同じなら6時間ごと
    assert os_.should_push_status(st, bad, datetime(2026, 10, 18, 10, 30))
    same = os_.build_mac_status(st, {"pull": True}, datetime(2026, 10, 18, 11, 0), {"last_success": "x"})
    assert not os_.should_push_status(st, same, datetime(2026, 10, 18, 11, 0))
    assert os_.should_push_status(st, same, datetime(2026, 10, 18, 16, 0))
    assert os_.should_push_status(None, same, now)


def test_rules_sync_stages_outside_git_and_saves_to_bq():
    text = (REPO / "obsidian_sync.py").read_text(encoding="utf-8")
    start = text.index("def cmd_rules")
    body = text[start:text.index("# ======", start)]
    # 写し先は作業コピー・同期用 clone の外。部則は git に入れない（commit・push しない）
    assert '"--root", str(RULES_STAGE)' in body
    code = body.split('"""')[2]
    assert "SYNC_CACHE" not in code and "push" not in code and "git" not in code
    tool = (REPO / "tools/sync_invest_rules.py").read_text(encoding="utf-8")
    assert "save_to_bq" in tool and '"push"' not in tool and '"commit"' not in tool


# ── 保有銘柄 ────────────────────────────────────────────────────

def test_holdings_issue_and_keep_generated():
    oh = load_module("ops_health")
    assert oh.holdings_issues({"as_of": "2026-09-17"}, date(2026, 10, 18), 14)
    assert oh.holdings_issues({"as_of": "2026-10-10"}, date(2026, 10, 18), 14) == []
    assert oh.holdings_issues(None, date(2026, 10, 18), 14) == []
    sh = load_module("sync_holdings")
    old = {"as_of": "2026-09-17", "generated": "2026-10-01", "total_assets_jpy": 1}
    new = {"as_of": "2026-09-17", "generated": "2026-10-18", "total_assets_jpy": 1}
    assert sh.keep_generated(old, new)["generated"] == "2026-10-01"
    changed = {**new, "total_assets_jpy": 2}
    assert sh.keep_generated(old, changed)["generated"] == "2026-10-18"


# ── クラウド審議 routine ────────────────────────────────────────

def test_routine_issues():
    oh = load_module("ops_health")
    now = datetime(2026, 11, 20, 12, 0)
    ok = {"last_activity": "2026-11-10 11:00:00", "stale_claimed": 0, "stale_running": 0}
    assert oh.routine_issues(ok, now, 14, 2) == []
    gone = {"last_activity": "2026-10-18 11:00:00", "stale_claimed": 2, "stale_running": 1}
    got = oh.routine_issues(gone, now, 14, 2)
    assert [g["severity"] for g in got] == ["high", "high", "medium"]
    # 導入前の空白は数えない（初回予定日から数える）
    never = {"last_activity": None, "stale_claimed": 0, "stale_running": 0}
    assert oh.routine_issues(never, datetime(2026, 10, 25), 14, 2, date(2026, 10, 18)) == []
    assert oh.routine_issues(never, datetime(2026, 11, 10), 14, 2, date(2026, 10, 18))


def test_build_alert_and_ci_flag(tmp_path, monkeypatch):
    oh = load_module("ops_health")
    assert oh.build_alert([]) is None
    a = oh.build_alert([{"severity": "medium", "text": "x"}, {"severity": "high", "text": "y"}])
    assert a["severity"] == "high" and a["trigger"] == "ops_health" and "・y" in a["body"]
    env, summ = tmp_path / "env", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_ENV", str(env))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    oh.flag_ci_failure("Gmail 失敗\n改行")
    assert env.read_text(encoding="utf-8").startswith("NOTIFY_FAILED=Gmail 失敗 改行")
    assert "通知が届いていない" in summ.read_text(encoding="utf-8")


def test_collect_file_issues_mac_only_on_actions(tmp_path, monkeypatch):
    oh = load_module("ops_health")
    cfg = SimpleNamespace(LOG_DIR=tmp_path / "logs", DATA_DIR=tmp_path / "data",
                          HOLDINGS_SUMMARY_PATH=tmp_path / "data" / "portfolio_summary.json")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert oh.collect_file_issues(cfg, datetime(2026, 10, 18)) == []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    got = oh.collect_file_issues(cfg, datetime(2026, 10, 18))
    assert len(got) == 1 and "mac_sync_status" in got[0]["text"]


# ── キー判定 ────────────────────────────────────────────────────

def test_classify_google_oauth():
    kh = load_module("api_key_health")
    assert kh.classify_google_oauth(200) == kh.STATE_ALIVE
    assert kh.classify_google_oauth(400, '{"error": "invalid_grant"}') == kh.STATE_DEAD
    assert kh.classify_google_oauth(401, '{"error": "invalid_client"}') == kh.STATE_DEAD
    assert kh.classify_google_oauth(500, "") == kh.STATE_LIMITED
    assert kh.classify_google_oauth(None) == kh.STATE_LIMITED


def test_gmail_dead_flags_ci(monkeypatch, tmp_path):
    kh = load_module("api_key_health")
    env = tmp_path / "env"
    monkeypatch.setenv("GITHUB_ENV", str(env))
    monkeypatch.setattr(kh, "_probe_gmail_smtp", lambda a, b: kh._entry(kh.STATE_DEAD, "x"))

    class _Cfg:
        ALERT_GMAIL_FROM = "a@example.com"
        ALERT_GMAIL_PASSWORD = "pw"

    kh.run_key_health(_Cfg())
    assert "NOTIFY_FAILED=" in env.read_text(encoding="utf-8")


# ── サブテーマ表の置き換え ──────────────────────────────────────

def test_subtheme_mapping_dedupe_and_fingerprint():
    ss = load_module("setup_subtheme_bq")
    sectors = {"1016": {"subthemes": ["AI半導体", "AI半導体", ""]}, "x": "broken"}
    ph = ss.build_placeholder_rows(sectors, "JP")
    assert [r["ticker"] for r in ph] == ["SECTOR_1016", "SECTOR_1016"]
    rows = ss.dedupe_mapping(ph + [{"ticker": "6758.T", "sector_code": "1016", "market": "JP", "subtheme": "AI半導体"}])
    assert len(rows) == 2
    master = ss.build_master_rows(sectors, "JP", "1.0")
    assert len(master) == 1 and master[0]["subthemes"] == ["AI半導体", "AI半導体", ""]
    assert ss.fingerprint(rows, master) == ss.fingerprint(list(reversed(rows)), master)
    assert ss.fingerprint(rows[:1], master) != ss.fingerprint(rows, master)


def test_monthly_runner_has_subtheme_refresh():
    mr = load_module("monthly_runner")
    steps = mr.build_steps(None, "202610", date(2026, 11, 1))
    names = [s[0] for s in steps]
    assert "subtheme_refresh" in names and names[-1] == "monthly_mail"
    cmd = next(s[2] for s in steps if s[0] == "subtheme_refresh")
    assert cmd[-2:] == ["--refresh", "--if-changed"]


# ── BI マートの鮮度（高-3）─────────────────────────────────────

def test_bi_mart_issues():
    oh = load_module("ops_health")
    man = {"marts": [{"mart": "m_screener", "generated_at": "2026-10-17T05:26:17Z"},
                     {"mart": "m_arxiv_keywords", "generated_at": "2026-10-01T00:00:00Z"}]}
    # 前日分（日次の中で読む通常の古さ）と 土→月 の2日は正常
    assert oh.bi_mart_issues(man, datetime(2026, 10, 18, 5, 0), 3) == []
    assert oh.bi_mart_issues(man, datetime(2026, 10, 19, 6, 0), 3) == []
    got = oh.bi_mart_issues(man, datetime(2026, 10, 21, 6, 0), 3)
    assert len(got) == 1 and "m_screener" in got[0]["text"] and got[0]["severity"] == "high"
    assert oh.bi_mart_issues(None, datetime(2026, 10, 21), 3) == []


def test_collect_reads_bi_manifest(tmp_path, monkeypatch):
    oh = load_module("ops_health")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    (tmp_path / "bi").mkdir()
    (tmp_path / "bi" / "manifest.json").write_text(json.dumps(
        {"marts": [{"mart": "m_screener", "generated_at": "2026-10-10T05:00:00Z"}]}), encoding="utf-8")
    cfg = SimpleNamespace(LOG_DIR=tmp_path / "logs", DATA_DIR=tmp_path / "data",
                          HOLDINGS_SUMMARY_PATH=tmp_path / "data" / "portfolio_summary.json")
    got = oh.collect_file_issues(cfg, datetime(2026, 10, 18, 14, 0))
    assert len(got) == 1 and "m_screener" in got[0]["text"]


# ── 日本株の台帳日付（高-4）────────────────────────────────────

def test_price_universe_ledger_date():
    pu = load_module("price_universe")
    assert pu.ledger_date(datetime(2026, 10, 14, 1, 30)) == "2026-10-13"   # 遅れて0時をまたいだ JP の回
    assert pu.ledger_date(datetime(2026, 10, 13, 21, 45)) == "2026-10-13"
    assert pu.ledger_date(datetime(2026, 10, 14, 9, 45)) == "2026-10-14"   # US の朝の回


# ── テストは本物の BQ を読まない ──────────────────────────────

def test_no_real_bq_credentials_in_tests():
    import os
    assert not os.environ.get("BQ_CREDENTIALS")
    assert not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
