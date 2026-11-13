# ==============================================================
# tests/test_self_heal.py — tools/self_heal.py（失敗の分類・対処・watchdog・Issue）と3本の workflow
# ==============================================================
# GitHub API（_api）は差し替える。ネットワークは使わない。合成の jobs・注記・実行履歴だけで確かめる。
# ==============================================================
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import self_heal as sh  # noqa: E402

JST = timezone(timedelta(hours=9))
WF = REPO / ".github" / "workflows"


def jst(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=JST)


def job(name, conclusion="failure", steps=None, jid=1, status="completed"):
    return {"id": jid, "name": name, "conclusion": conclusion, "status": status, "steps": steps or []}


def step(name, conclusion="success"):
    return {"name": name, "conclusion": conclusion}


def ann(msg, level="failure"):
    return {"message": msg, "annotation_level": level}


# ── classify ──────────────────────────────────────────────────

def test_classify_notify_only():
    j = job("📈 アナライザー実行", steps=[step("Run"), step("🚨 Fail if notification could not be sent", "failure")])
    assert sh.classify([j], [])["kind"] == "notify_only"


def test_notify_with_other_failed_step_is_not_notify_only():
    j = job("x", steps=[step("Run", "failure"), step("🚨 Fail if notification could not be sent", "failure")])
    assert sh.classify([j], [])["kind"] != "notify_only"


def test_classify_timeout_by_conclusion_and_annotation():
    assert sh.classify([job("x", "timed_out")], [])["kind"] == "timeout"
    a = [ann("The job running on runner GitHub Actions 1 has exceeded the maximum execution time of 180 minutes.")]
    assert sh.classify([job("x", "failure", steps=[step("Run", "cancelled")])], a)["kind"] == "timeout"


def test_classify_runner_loss():
    a = [ann("The runner has received a shutdown signal. This can happen when the runner service is stopped.")]
    assert sh.classify([job("x", steps=[step("Run", "failure")])], a)["kind"] == "transient"
    a = [ann("The hosted runner lost communication with the server.")]
    assert sh.classify([job("x")], a)["kind"] == "transient"


def test_classify_cancelled_is_not_rerun_but_timeout_wins():
    assert sh.classify([job("x", "cancelled")], [])["kind"] == "cancelled"
    assert sh.decide("cancelled", 1, "w", {"by_workflow": {}, "total": 0}) == "none"
    a = [ann("exceeded the maximum execution time")]
    assert sh.classify([job("x", "cancelled")], a)["kind"] == "timeout"


@pytest.mark.parametrize("name", ["Set up job", "🐍 Setup Python", "📥 Checkout", "Run actions/checkout@v6",
                                  "📦 Install dependencies", "♻️ Restore installed packages"])
def test_classify_setup_steps_transient(name):
    j = job("x", steps=[step(name, "failure")])
    assert sh.classify([j], [])["kind"] == "transient"


def test_classify_network_annotation():
    j = job("x", steps=[step("Fetch", "failure")])
    a = [ann("requests.exceptions.ConnectionError: Max retries exceeded with url")]
    assert sh.classify([j], a)["kind"] == "transient"


def test_classify_code():
    j = job("test", steps=[step("pytest", "failure")])
    assert sh.classify([j], [])["kind"] == "code"
    j = job("run", steps=[step("Run analyzer", "failure")])
    assert sh.classify([j], [ann("AssertionError: boom")])["kind"] == "code"
    j = job("test", steps=[step("results_schema audit", "failure")])
    assert sh.classify([j], [])["kind"] == "code"


def test_classify_unknown():
    j = job("x", steps=[step("Run analyzer", "failure")])
    assert sh.classify([j], [ann("Process completed with exit code 1.")])["kind"] == "unknown"
    assert sh.classify([job("x", "success")], [])["kind"] == "unknown"


def test_classify_ignores_non_failure_annotations_and_returns_names_only():
    j = job("📥 取得", steps=[step("Fetch", "failure")])
    r = sh.classify([j], [ann("connection reset by peer", level="warning")])
    assert r["kind"] == "unknown"
    assert r["jobs"] == ["📥 取得"] and r["steps"] == ["Fetch"]


# ── decide ────────────────────────────────────────────────────

NO = {"by_workflow": {}, "total": 0}


def test_decide_transient_reruns():
    assert sh.decide("transient", 1, "w", NO) == "rerun_failed"


def test_decide_rerun_limits():
    assert sh.decide("transient", 2, "w", NO) == "issue"
    assert sh.decide("transient", 1, "w", {"by_workflow": {"w": 1}, "total": 1}) == "issue"
    assert sh.decide("transient", 1, "w", {"by_workflow": {"v": 1}, "total": 1}) == "rerun_failed"
    assert sh.decide("transient", 1, "w", {"by_workflow": {}, "total": 3}) == "issue"
    assert sh.decide("transient", 1, "w", {"by_workflow": {"w": 1}, "total": 1},
                     cfg={"max_reruns_per_workflow": 2}) == "rerun_failed"


def test_decide_after_paid_step_never_reruns():
    assert sh.decide("transient", 1, "w", NO, before_paid=False) == "issue"


def test_decide_other_kinds():
    assert sh.decide("timeout", 1, "w", NO) == "issue"
    assert sh.decide("unknown", 1, "w", NO) == "issue"
    assert sh.decide("code", 1, "w", NO) == "issue"
    assert sh.decide("notify_only", 1, "w", NO) == "none"
    assert sh.decide("notify_only", 1, "w", NO, before_paid=False) == "none"


def test_failed_before_paid():
    steps_run = [step("Checkout"), step("Fetch", "failure")]
    steps_not = [step("Checkout", "failure"), step("Fetch", "skipped")]
    assert sh.failed_before_paid([job("j", steps=steps_run)], "Fetch") is False
    assert sh.failed_before_paid([job("j", steps=steps_not)], "Fetch") is True
    assert sh.failed_before_paid([job("j", steps=[])], "Fetch") is True
    assert sh.failed_before_paid([job("j", steps=steps_run)], None) is True
    assert sh.failed_before_paid([job("j", steps=[{"name": "Fetch", "conclusion": None}])], "Fetch") is True


# ── count_reruns ──────────────────────────────────────────────

def test_count_reruns_counts_today_attempts_only():
    now = jst(2026, 11, 13, 12)
    runs = {"daily_run.yml": [
        {"run_attempt": 2, "run_started_at": "2026-11-13T00:00:00Z"},      # JST 09:00 今日
        {"run_attempt": 1, "run_started_at": "2026-11-13T00:30:00Z"},      # 再実行ではない
        {"run_attempt": 2, "run_started_at": "2026-11-12T10:00:00Z"},      # JST 19:00 昨日
        {"run_attempt": 3, "created_at": "2026-11-13T01:00:00Z"},
    ], "price_universe.yml": [{"run_attempt": 2, "run_started_at": "2026-11-12T15:30:00Z"}]}  # JST 00:30 今日
    r = sh.count_reruns(now, runs)
    assert r["by_workflow"]["📊 Stock Analyzer 自律実行"] == 2
    assert r["by_workflow"]["全上場株価の日次取得"] == 1
    assert r["total"] == 3


# ── watchdog_due ──────────────────────────────────────────────

def run(created, main=None, event="schedule", active=False):
    return {"event": event, "created_at": created.astimezone(timezone.utc).isoformat(),
            "main_state": main, "active": active}


def due_files(now, history):
    return [e["file"] for e in sh.watchdog_due(now, history)]


FRI = (2026, 11, 13)   # 金曜
SAT = (2026, 11, 14)
SUN = (2026, 11, 15)
MON = (2026, 11, 16)


def test_weekday_fixture():
    assert jst(*FRI, 0).isoweekday() == 5 and jst(*SUN, 0).isoweekday() == 7 and jst(*MON, 0).isoweekday() == 1


def test_watchdog_before_schedule_and_within_grace():
    assert "price_universe.yml" not in due_files(jst(*FRI, 6, 30), {})
    assert "price_universe.yml" not in due_files(jst(*FRI, 7, 0), {})          # 06:47＋90分 = 08:17 より前
    assert "price_universe.yml" not in due_files(jst(*FRI, 8, 16), {})


def test_watchdog_after_grace_wakes():
    assert "price_universe.yml" in due_files(jst(*FRI, 8, 17), {})
    assert "price_universe.yml" in due_files(jst(*FRI, 15, 0), {})


def test_watchdog_not_if_started_today():
    now = jst(*FRI, 9, 0)
    for state in ("success", "active", "failure", "cancelled"):
        assert "price_universe.yml" not in due_files(now, {"price_universe.yml": [run(jst(*FRI, 6, 50), state)]})


def test_watchdog_gate_skip_counts_as_not_started():
    now = jst(*FRI, 9, 0)
    h = {"price_universe.yml": [run(jst(*FRI, 6, 50), "skipped")]}
    assert "price_universe.yml" in due_files(now, h)
    h = {"price_universe.yml": [run(jst(*FRI, 6, 50), None)]}
    assert "price_universe.yml" in due_files(now, h)


def test_watchdog_yesterdays_run_does_not_count():
    now = jst(*FRI, 9, 0)
    h = {"price_universe.yml": [run(jst(2026, 11, 12, 6, 50), "success")]}
    assert "price_universe.yml" in due_files(now, h)


def test_watchdog_once_per_day():
    now = jst(*FRI, 12, 0)
    h = {"price_universe.yml": [run(jst(*FRI, 9, 0), "skipped", event="workflow_dispatch")]}
    assert "price_universe.yml" not in due_files(now, h)


def test_watchdog_sunday_only_sec_filings():
    assert due_files(jst(*SUN, 23, 0), {}) == ["sec_filings.yml"]


def test_watchdog_saturday_weekly_workflows():
    files = due_files(jst(*SAT, 23, 0), {})
    assert {"market_positioning.yml", "quant_research.yml", "daily_run.yml"} <= set(files)
    assert "us_company_facts.yml" in files
    assert due_files(jst(*FRI, 20, 0), {}).count("market_positioning.yml") == 0


def test_watchdog_monday_has_no_price_universe_and_no_us_facts():
    files = due_files(jst(*MON, 23, 0), {})
    assert "price_universe.yml" not in files and "us_company_facts.yml" not in files
    assert "daily_run.yml" in files


def test_watchdog_daily_waits_for_fetch():
    now = jst(*FRI, 12, 30)
    h = {"fetch_jp_filings.yml": [run(jst(*FRI, 11, 0), None, event="workflow_dispatch", active=True)]}
    assert "daily_run.yml" not in due_files(now, h)
    h = {"fetch_jp_filings.yml": [run(jst(*FRI, 11, 0), "success", active=False)]}
    assert "daily_run.yml" in due_files(now, h)


def test_watchdog_daily_not_if_main_job_success_or_running():
    now = jst(*FRI, 12, 30)
    for state in ("success", "active"):
        assert "daily_run.yml" not in due_files(now, {"daily_run.yml": [run(jst(*FRI, 9, 17), state)]})


def test_watchdog_accepts_naive_utc_iso_strings():
    now = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)     # JST 09:00
    h = {"price_universe.yml": [{"event": "schedule", "created_at": "2026-11-12T21:47:10Z",
                                 "main_state": "success", "active": False}]}
    assert "price_universe.yml" not in [e["file"] for e in sh.watchdog_due(now, h)]


# ── handle / Issue（_api を差し替え）──────────────────────────

SECRET = "SECRET_LOG_LINE_8301234 銘柄7203 売上1234567890"


class FakeApi:
    def __init__(self, run, jobs, annotations=None, runs_by_file=None, issues=None):
        self.run, self.jobs = run, jobs
        self.annotations = annotations or []
        self.runs_by_file = runs_by_file or {}
        self.issues = list(issues or [])
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and re.fullmatch(r"/repos/o/r/actions/runs/\d+", path):
            return self.run
        if method == "GET" and "/jobs" in path:
            return {"jobs": self.jobs}
        if method == "GET" and "/annotations" in path:
            return self.annotations
        if method == "GET" and "/workflows/" in path:
            f = path.split("/workflows/")[1].split("/")[0]
            return {"workflow_runs": self.runs_by_file.get(f, [])}
        if method == "GET" and "/issues?" in path:
            return self.issues
        if method == "POST" and path.endswith("/issues"):
            self.issues.append({"number": 7, "title": body["title"], "body": body["body"]})
            return {"number": 7}
        return {}

    def posted(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")


def fetch_run(conclusion="failure", attempt=1):
    return {"id": 11, "name": "日本企業の開示データ取得", "path": ".github/workflows/fetch_jp_filings.yml@refs/heads/main",
            "conclusion": conclusion, "run_attempt": attempt, "html_url": "https://github.com/o/r/actions/runs/11"}


NOW = jst(*FRI, 9, 0)


def test_handle_transient_before_paid_reruns(env, monkeypatch):
    jobs = [job("📥 取得", steps=[step("Checkout"), step("Install dependencies", "failure"), step("Fetch", "skipped")])]
    api = FakeApi(fetch_run(), jobs)
    monkeypatch.setattr(sh, "_api", api)
    assert sh.handle(11, now=NOW) == "rerun_failed"
    assert api.posted("/actions/runs/11/rerun-failed-jobs")
    assert not api.posted("/issues")


def test_handle_after_paid_step_opens_issue_without_rerun(env, monkeypatch):
    jobs = [job("📥 取得", steps=[step("Fetch", "failure")], jid=5)]
    api = FakeApi(fetch_run(), jobs, annotations=[ann("ConnectionError: " + SECRET)])
    monkeypatch.setattr(sh, "_api", api)
    assert sh.handle(11, now=NOW) == "issue"
    assert not api.posted("/rerun-failed-jobs")
    assert api.posted("/issues")


def test_handle_rerun_cap_from_history(env, monkeypatch):
    jobs = [job("📥 取得", steps=[step("Install dependencies", "failure")])]
    hist = {"daily_run.yml": [{"run_attempt": 2, "run_started_at": "2026-11-13T00:10:00Z"}]}
    api = FakeApi(fetch_run(), jobs, runs_by_file={"fetch_jp_filings.yml": [
        {"run_attempt": 2, "run_started_at": "2026-11-13T00:00:00Z"}], **hist})
    monkeypatch.setattr(sh, "_api", api)
    assert sh.handle(11, now=NOW) == "issue"          # fetch は今日すでに1回再実行済み


def test_handle_second_attempt_goes_to_issue(env, monkeypatch):
    jobs = [job("📥 取得", steps=[step("Install dependencies", "failure")])]
    api = FakeApi(fetch_run(attempt=2), jobs)
    monkeypatch.setattr(sh, "_api", api)
    assert sh.handle(11, now=NOW) == "issue"


def test_handle_notify_only_does_nothing(env, monkeypatch):
    run_ = fetch_run()
    run_["path"] = ".github/workflows/daily_run.yml@refs/heads/main"
    jobs = [job("📈 アナライザー実行", steps=[step("🚨 Fail if notification could not be sent", "failure")])]
    api = FakeApi(run_, jobs)
    monkeypatch.setattr(sh, "_api", api)
    assert sh.handle(11, now=NOW) == "none"
    assert not [c for c in api.calls if c[0] != "GET"]


def test_handle_unwatched_workflow_skipped(env, monkeypatch):
    run_ = fetch_run()
    run_["path"] = ".github/workflows/ci.yml@refs/heads/main"
    api = FakeApi(run_, [])
    monkeypatch.setattr(sh, "_api", api)
    assert sh.handle(11, now=NOW) == "skip"


def test_issue_body_has_no_annotation_text(env, monkeypatch):
    jobs = [job("📥 取得", steps=[step("Fetch", "failure")], jid=5)]
    api = FakeApi(fetch_run(), jobs, annotations=[ann(SECRET), ann("Traceback (most recent call last) " + SECRET)])
    monkeypatch.setattr(sh, "_api", api)
    sh.handle(11, now=NOW)
    sent = " ".join(str(c[2]) for c in api.calls if c[2])
    assert "SECRET_LOG_LINE" not in sent and "7203" not in sent and "1234567890" not in sent
    body = api.posted("/issues")[0][2]["body"]
    for want in ("日本企業の開示データ取得", "📥 取得", "Fetch", "コードの失敗", "actions/runs/11", "JST"):
        assert want in body
    assert api.posted("/issues")[0][2]["labels"] == ["自己修復"]


def test_existing_open_issue_gets_comment_instead_of_new(env, monkeypatch):
    jobs = [job("📥 取得", steps=[step("Fetch", "failure")])]
    api = FakeApi(fetch_run(), jobs, issues=[
        {"number": 3, "title": "[自己修復] 日本企業の開示データ取得"},
        {"number": 4, "title": "[自己修復] 日本企業の開示データ取得", "pull_request": {}}])
    monkeypatch.setattr(sh, "_api", api)
    sh.handle(11, now=NOW)
    assert api.posted("/issues/3/comments") and not api.posted("/issues")


def test_success_closes_open_issue(env, monkeypatch):
    jobs = [job("📥 取得", "success", steps=[step("Fetch", "success")])]
    api = FakeApi(fetch_run("success"), jobs, issues=[{"number": 3, "title": "[自己修復] 日本企業の開示データ取得"}])
    monkeypatch.setattr(sh, "_api", api)
    assert sh.handle(11, now=NOW) == "closed"
    patch = [c for c in api.calls if c[0] == "PATCH"]
    assert patch and patch[0][2]["state"] == "closed"


def test_success_with_skipped_main_job_keeps_issue_open(env, monkeypatch):
    jobs = [job("🚦 今日の取得が要るか", "success"), job("📥 取得", "skipped")]
    api = FakeApi(fetch_run("success"), jobs, issues=[{"number": 3, "title": "[自己修復] 日本企業の開示データ取得"}])
    monkeypatch.setattr(sh, "_api", api)
    assert sh.handle(11, now=NOW) == "skip"
    assert not [c for c in api.calls if c[0] in ("PATCH",)]


def test_ensure_label_ignores_only_422(env, monkeypatch):
    def api422(*a, **k):
        raise sh.ApiError(422, "POST", "/x")
    monkeypatch.setattr(sh, "_api", api422)
    sh._ensure_label()

    def api500(*a, **k):
        raise sh.ApiError(500, "POST", "/x")
    monkeypatch.setattr(sh, "_api", api500)
    with pytest.raises(sh.ApiError):
        sh._ensure_label()


def test_watchdog_dispatches_with_weekly_input_on_monday(env, monkeypatch):
    api = FakeApi({}, [])
    monkeypatch.setattr(sh, "_api", api)
    monkeypatch.setattr(sh, "WATCH", tuple(e for e in sh.WATCH if e["file"] == "daily_run.yml"))
    monkeypatch.setattr(sh, "watchdog_due", lambda now, h, table=None: list(sh.WATCH))
    assert sh.watchdog(now=jst(*MON, 23, 0)) == ["daily_run.yml"]
    body = api.posted("/workflows/daily_run.yml/dispatches")[0][2]
    assert body["inputs"] == {"mode": "weekly"}


# ── workflow ファイル ──────────────────────────────────────────

def load(name):
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def triggers(doc):
    return doc.get("on", doc.get(True))


def test_three_workflows_load():
    for n in ("ci.yml", "self_heal.yml", "quant_research.yml"):
        assert isinstance(load(n), dict) and load(n).get("jobs")


def test_self_heal_workflows_list_matches_real_names():
    names = {load(p.name).get("name") for p in WF.glob("*.yml")}
    listed = triggers(load("self_heal.yml"))["workflow_run"]["workflows"]
    assert set(listed) <= names
    assert {e["name"] for e in sh.WATCH} | {sh.CI_WORKFLOW_NAME} == set(listed)
    assert sh.SELF_WORKFLOW_NAME not in listed
    assert triggers(load("self_heal.yml"))["workflow_run"]["types"] == ["completed"]


def test_self_heal_permissions_and_triggers():
    d = load("self_heal.yml")
    assert d["permissions"] == {"actions": "write", "issues": "write", "contents": "read"}
    t = triggers(d)
    assert "schedule" in t and "workflow_dispatch" in t


def test_no_github_event_expanded_inside_run():
    for n in ("ci.yml", "self_heal.yml", "quant_research.yml"):
        for jn, j in load(n)["jobs"].items():
            for s in j["steps"]:
                assert "github.event" not in str(s.get("run", "")), (n, jn, s.get("name"))
    # claude-code-action の prompt にも workflow_run の値は展開しない（env 経由）
    for s in load("self_heal.yml")["jobs"]["autofix"]["steps"]:
        assert "github.event" not in str((s.get("with") or {}).get("prompt", ""))


def test_ci_has_no_secrets_and_readonly():
    text = (WF / "ci.yml").read_text(encoding="utf-8")
    assert "secrets." not in text
    assert load("ci.yml")["permissions"] == {"contents": "read"}
    t = triggers(load("ci.yml"))
    assert t["push"]["branches"] == ["main"] and "pull_request" in t and "workflow_dispatch" in t
    cmds = " ".join(s.get("run", "") for s in load("ci.yml")["jobs"]["test"]["steps"])
    for want in ("pytest tests/ -q", "config_audit.py", "pipeline_registry_gen.py --audit", "results_schema.py --audit"):
        assert want in cmds


def test_autofix_is_gated_and_does_not_push_main():
    j = load("self_heal.yml")["jobs"]["autofix"]
    assert "SELF_HEAL_AUTOFIX" in j["if"] and "head_branch == 'main'" in j["if"]
    uses = [s.get("uses", "") for s in j["steps"]]
    assert any(u.startswith("anthropics/claude-code-action") for u in uses)
    assert "ANTHROPIC_API_KEY" in (WF / "self_heal.yml").read_text(encoding="utf-8")
    assert "origin main" not in str(j)
    assert "workflow_run.name != 'CI'" in load("self_heal.yml")["jobs"]["handle"]["if"]


def test_quant_research_workflow():
    d = load("quant_research.yml")
    assert d["jobs"]["run"]["timeout-minutes"] == 60
    assert triggers(d)["schedule"] == [{"cron": "17 3 * * 6"}]
    text = (WF / "quant_research.yml").read_text(encoding="utf-8")
    assert "python -u -m quant.runner research" in text
    assert "actions_log.py --workflow quant_research" in text
    assert "git commit" not in text and "git push" not in text
    assert d["jobs"]["run"]["name"] == [e for e in sh.WATCH if e["file"] == "quant_research.yml"][0]["main_job"]


# ── WATCH の表が実際の yml と合っているか ───────────────────────

def cron_jst(expr):
    """cron（UTC）→ (JST の 時刻 HH:MM, JST の曜日の集合 1=月〜7=日)。"""
    mi, h, _, _, dow = expr.split()
    if dow == "*":
        days = set(range(7))
    else:
        days = set()
        for part in dow.split(","):
            a, _, b = part.partition("-")
            days.update(range(int(a), int(b or a) + 1))
    days = {d % 7 for d in days}                      # cron の 0 と 7 は日曜
    shift = (int(h) + 9) // 24
    # cron の日曜=0 → ISO の 7。JST で日付が進むぶん曜日も進める
    jst_days = set()
    for d in days:
        iso = 7 if d == 0 else d
        jst_days.add(((iso - 1 + shift) % 7) + 1)
    return f"{(int(h) + 9) % 24:02d}:{int(mi):02d}", jst_days


def test_cron_jst_helper():
    assert cron_jst("17 23 * * 0-5") == ("08:17", {1, 2, 3, 4, 5, 6})
    assert cron_jst("17 0 * * 1-6") == ("09:17", {1, 2, 3, 4, 5, 6})
    assert cron_jst("47 21 * * 1-5") == ("06:47", {2, 3, 4, 5, 6})


@pytest.mark.parametrize("entry", sh.WATCH, ids=lambda e: e["file"])
def test_watch_table_matches_workflow_file(entry):
    d = load(entry["file"])
    assert d["name"] == entry["name"]
    t = triggers(d)
    assert "workflow_dispatch" in t, "watchdog が起こせない"
    jobs = d["jobs"]
    main = [j for k, j in jobs.items() if j.get("name", k) == entry["main_job"]]
    assert main, "main_job が yml に無い"
    if entry["paid_step"]:
        assert entry["paid_step"] in [s.get("name") for s in main[0]["steps"]]
    time_, days = cron_jst(t["schedule"][0]["cron"])
    if entry["file"] == "daily_run.yml":
        fetch = load("fetch_jp_filings.yml")
        ftime, _ = cron_jst(triggers(fetch)["schedule"][0]["cron"])
        fmax = fetch["jobs"]["run"]["timeout-minutes"]
        h, m = (int(x) for x in ftime.split(":"))
        earliest = h * 60 + m + fmax
        eh, em = (int(x) for x in entry["time"].split(":"))
        assert eh * 60 + em > earliest, "取得の予定＋timeout より後に置く"
        assert set(entry["weekdays"]) == days
    else:
        assert entry["time"] == time_
        assert set(entry["weekdays"]) == days


def test_watch_wait_for_refers_to_watched_files():
    files = {e["file"] for e in sh.WATCH}
    for e in sh.WATCH:
        assert set(e["wait_for"]) <= files
