# ==============================================================
# tests/test_data_reliability.py  (Wave R0-1 / E7')
# ==============================================================
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data_reliability as dr


# ── retry_call ──────────────────────────────────────────────
def test_retry_success_first_try():
    result, err = dr.retry_call(lambda: 42, schedule=[])
    assert result == 42 and err == ""


def test_retry_recovers_after_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    result, err = dr.retry_call(flaky, schedule=[0, 0])
    assert result == "ok" and calls["n"] == 3


def test_retry_exhausted():
    result, err = dr.retry_call(lambda: None, schedule=[0])
    assert result is None and err == "returned None"


# ── Stooqシンボル変換 ────────────────────────────────────────
def test_stooq_symbol_jp():
    assert dr._to_stooq_symbol("7203.T") == "7203.jp"
    assert dr._to_stooq_symbol("1306.t") == "1306.jp"


def test_stooq_symbol_us():
    assert dr._to_stooq_symbol("AAPL") == "aapl.us"


def test_stooq_symbol_passthrough():
    assert dr._to_stooq_symbol("BRK-B.US") == "brk-b.us"


# ── EDINET繰越キュー ─────────────────────────────────────────
class _Cfg:
    def __init__(self, tmp):
        self.CACHE_DIR = str(tmp)
        self.EDINET_RETRY_DAILY_LIMIT = 2


def test_edinet_queue_push_pop(tmp_path):
    cfg = _Cfg(tmp_path)
    dr.push_edinet_retry(cfg, "DOC1", "7203.T", "dns error")
    dr.push_edinet_retry(cfg, "DOC2", "6758.T", "timeout")
    dr.push_edinet_retry(cfg, "DOC3", "9432.T", "500")
    dr.push_edinet_retry(cfg, "DOC1", "7203.T", "dup")  # 重複は無視

    todo = dr.pop_edinet_retries(cfg)          # limit=2
    assert [q["doc_id"] for q in todo] == ["DOC1", "DOC2"]
    rest = dr.pop_edinet_retries(cfg, limit=10)
    assert [q["doc_id"] for q in rest] == ["DOC3"]
    assert dr.pop_edinet_retries(cfg, limit=10) == []


# ── 鮮度SLA ─────────────────────────────────────────────────
def test_sla_detects_stale_and_missing(tmp_path):
    fresh = tmp_path / "fresh.json"
    fresh.write_text("{}")
    stale = tmp_path / "stale.json"
    stale.write_text("{}")
    import os
    old = time.time() - 10 * 86400
    os.utime(stale, (old, old))

    class Cfg:
        CACHE_DIR = str(tmp_path)
        DATA_SLA_DAYS = {"price": 2, "news": 3, "financials": 100}
        DATA_SLA_TARGETS = {
            "price": str(stale),                 # 10日前 → 違反
            "news": str(fresh),                  # 今 → OK
            "financials": str(tmp_path / "nofile.json"),  # 欠落 → 違反
        }

    report = dr.run_data_sla_check(Cfg(), {})
    kinds = {v["kind"]: v["status"] for v in report["violations"]}
    assert kinds.get("price") == "stale"
    assert kinds.get("financials") == "missing"
    assert "news" not in kinds
    assert report["violation_count"] == 2
    assert report["checked"] == 3
