"""deliberation_queue.build_queue（純粋関数）の単体テスト。"""
from datetime import date
from types import SimpleNamespace

import deliberation_queue as dq

TODAY = date(2026, 10, 15)
CFG = SimpleNamespace(DELIB_QUEUE_SIZE=10, DELIB_RECHECK_DAYS=90, DELIB_RESCORE_JUMP=15, DELIB_HOLDING_MAX=3)


def cand(t, score, **kw):
    return {"ticker": t, "name": t, "composite_score": score, "sector_code": "1015", **kw}


def tickers(rows):
    return [r["ticker"] for r in rows]


def test_undeliberated_goes_in_by_score_desc():
    rows = dq.build_queue([cand("A.T", 80), cand("B.T", 95), cand("C.T", 90)], {}, [], CFG, TODAY)
    assert tickers(rows) == ["B.T", "C.T", "A.T"]
    assert [r["priority_rank"] for r in rows] == [1, 2, 3]
    assert all(r["status"] == "pending" and r["source"] == "screener" for r in rows)
    assert rows[0]["reason"].startswith("未審議")


def test_recently_deliberated_is_excluded_but_old_one_returns():
    latest = {"A.T": {"deliberated_at": date(2026, 9, 27), "verdict": "Watch", "final_score": 80},
              "B.T": {"deliberated_at": date(2026, 6, 1), "verdict": "Watch", "final_score": 80}}
    rows = dq.build_queue([cand("A.T", 85), cand("B.T", 85)], latest, [], CFG, TODAY)
    assert tickers(rows) == ["B.T"]
    assert "前回審議から136日" in rows[0]["reason"]
    assert rows[0]["last_verdict"] == "Watch" and rows[0]["last_deliberated_at"] == "2026-06-01"


def test_recheck_boundary_is_strictly_over_90_days():
    latest = {"A.T": {"deliberated_at": date(2026, 7, 17), "verdict": "Watch"}}   # ちょうど90日
    assert dq.build_queue([cand("A.T", 99)], latest, [], CFG, TODAY) == []
    latest = {"A.T": {"deliberated_at": date(2026, 7, 16), "verdict": "Watch"}}   # 91日
    assert tickers(dq.build_queue([cand("A.T", 99)], latest, [], CFG, TODAY)) == ["A.T"]


def test_score_jump_requeues_recent_deliberation():
    latest = {"A.T": {"deliberated_at": date(2026, 10, 1), "verdict": "Watch", "final_score": 70},
              "B.T": {"deliberated_at": date(2026, 10, 1), "verdict": "Watch", "final_score": 80}}
    rows = dq.build_queue([cand("A.T", 85), cand("B.T", 94)], latest, [], CFG, TODAY)
    assert tickers(rows) == ["A.T"]            # +15 は入る、+14 は入らない
    assert "点数急上昇" in rows[0]["reason"]


def test_no_previous_score_means_no_jump_trigger():
    latest = {"A.T": {"deliberated_at": date(2026, 10, 1), "verdict": "Watch", "final_score": None}}
    assert dq.build_queue([cand("A.T", 100)], latest, [], CFG, TODAY) == []


def test_core_holdings_are_excluded_everywhere():
    holdings = [{"ticker": "8058.T", "account_type": "NISA", "memo": ""},
                {"ticker": "9433.T", "account_type": "特定", "memo": "恩株。放置"}]
    rows = dq.build_queue([cand("8058.T", 99), cand("9433.T", 98), cand("A.T", 50)], {}, holdings, CFG, TODAY)
    assert tickers(rows) == ["A.T"]


def test_noncore_holding_becomes_holding_review_first():
    holdings = [{"ticker": "1605.T", "account_type": "特定", "memo": "", "name": "INPEX"}]
    latest = {"1605.T": {"deliberated_at": date(2026, 7, 7), "verdict": "Watch"}}
    rows = dq.build_queue([cand("A.T", 99)], latest, holdings, CFG, TODAY)
    assert tickers(rows) == ["1605.T", "A.T"]
    assert rows[0]["source"] == "holding_review" and "保有の定期見直し" in rows[0]["reason"]
    # 90日以内なら保有見直しは入らない
    latest = {"1605.T": {"deliberated_at": date(2026, 9, 1), "verdict": "Watch"}}
    assert tickers(dq.build_queue([cand("A.T", 99)], latest, holdings, CFG, TODAY)) == ["A.T"]


def test_holding_in_screener_is_not_duplicated():
    holdings = [{"ticker": "1605.T", "account_type": "特定", "memo": ""}]
    rows = dq.build_queue([cand("1605.T", 90), cand("A.T", 80)], {}, holdings, CFG, TODAY)
    assert tickers(rows) == ["1605.T", "A.T"]
    assert rows[0]["source"] == "holding_review" and rows[0]["final_score"] == 90


def test_size_limit_and_holding_cap():
    cfg = SimpleNamespace(DELIB_QUEUE_SIZE=3, DELIB_HOLDING_MAX=1)
    holdings = [{"ticker": f"H{i}.T", "account_type": "特定", "memo": ""} for i in range(3)]
    rows = dq.build_queue([cand(f"S{i}.T", 90 - i) for i in range(10)], {}, holdings, cfg, TODAY)
    assert len(rows) == 3
    assert sum(r["source"] == "holding_review" for r in rows) == 1


def test_default_config_values_when_missing():
    rows = dq.build_queue([cand(f"S{i}.T", 90 - i) for i in range(30)], {}, [], SimpleNamespace(), TODAY)
    assert len(rows) == 10


def test_final_score_preferred_and_candidates_without_score_skipped():
    rows = dq.build_queue([{"ticker": "A.T", "final_score": 70, "composite_score": 99},
                           {"ticker": "B.T", "final_score": 80, "composite_score": 10},
                           {"ticker": "C.T"}], {}, [], CFG, TODAY)
    assert tickers(rows) == ["B.T", "A.T"]
    assert rows[0]["composite_score"] == 10.0 and rows[0]["final_score"] == 80.0


def test_attach_packets_top_n_only_and_failure_reported():
    rows = dq.build_queue([cand(f"S{i}.T", 90 - i) for i in range(5)], {}, [], CFG, TODAY)

    def fn(t):
        if t == "S1.T":
            raise RuntimeError("boom")
        return f"packet {t}"

    res = dq.attach_packets(rows, 3, fn)
    assert rows[0]["packet_md"] == "packet S0.T"
    assert rows[1]["packet_md"] is None and "S1.T" in res["failed"]
    assert rows[2]["packet_md"] == "packet S2.T"
    assert rows[3]["packet_md"] is None and res["ok"] == 2


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _Row(dict):
    def items(self):
        return super().items()


class _FakeClient:
    project = "p"

    def __init__(self, fail=False):
        self.fail = fail

    def query(self, sql, **kw):
        if self.fail:
            raise RuntimeError("no bq")
        if "COUNT(*)" in sql:
            return _FakeJob([_Row(n=7)])
        if "deliberation_runs" in sql:
            return _FakeJob([_Row(run_id="r1", status="failed", error="boom")])
        return _FakeJob([_Row(ticker="A.T", name="A", sector_code="1", priority_rank=1, source="screener",
                              reason="未審議", final_score=91.0, composite_score=88.0, universe_score=None)])


def test_get_queue_for_report_reads_bq_and_keeps_interface():
    out = dq.get_queue_for_report(SimpleNamespace(), None, top_n=5, client=_FakeClient())
    assert out["queue"][0]["ticker"] == "A.T" and out["queue"][0]["composite_score"] == 91.0
    assert out["already_count"] == 7 and out["partial"] is False
    assert out["interval_days"] == 14 and out["last_run"]["status"] == "failed"
    for k in ("queue", "source", "already_count", "partial", "mirror_age_days", "interval_days"):
        assert k in out


def test_get_queue_for_report_falls_back_when_bq_fails(monkeypatch):
    import deliberation_scheduler as ds
    monkeypatch.setattr(ds, "get_queue_for_report", lambda cfg, results=None, top_n=5: {
        "queue": [], "source": "台帳(正本)", "already_count": 0, "partial": False,
        "mirror_age_days": None, "interval_days": 14})
    out = dq.get_queue_for_report(SimpleNamespace(), None, client=_FakeClient(fail=True))
    assert "BQ読込失敗" in out["source"] and out["last_run"] is None
