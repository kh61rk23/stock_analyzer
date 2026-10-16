"""実行台帳：別ワークフローの alert も連続失敗に数える・記録なしが続けば通知・別ワークフローの記録が来なければ通知。"""
import json
from types import SimpleNamespace

import module_ledger as ml


def _cfg(tmp_path, **kw):
    return SimpleNamespace(LOG_DIR=tmp_path, LEDGER_HISTORY_DAYS=14, LEDGER_CONSEC_FAIL_DAYS=3,
                           LEDGER_UNRECORDED_DAYS=2, LEDGER_EXTERNAL_EXPECT={"price_universe_jp": 4}, **kw)


def _day(tmp_path, d, modules=None, unrecorded=None):
    (tmp_path / f"module_ledger_{d}.json").write_text(json.dumps(
        {"date": d, "modules": modules or {}, "unrecorded": unrecorded or []}), encoding="utf-8")


def test_alert_status_counts_as_failure(tmp_path):
    cfg = _cfg(tmp_path)
    _day(tmp_path, "2026-10-14", {"price_universe_jp": {"status": "alert"}})
    _day(tmp_path, "2026-10-15", {"price_universe_jp": {"status": "error"}})
    today = {"date": "2026-10-16", "modules": {"price_universe_jp": {"status": "alert"}}}
    assert ml._calc_consecutive_failures(cfg, today) == {"price_universe_jp": 3}


def test_unrecorded_streak_and_external_stale(tmp_path):
    cfg = _cfg(tmp_path)
    _day(tmp_path, "2026-10-15", unrecorded=["tdnet"])
    _day(tmp_path, "2026-10-14", unrecorded=[])
    today = {"date": "2026-10-16", "modules": {}, "unrecorded": ["tdnet", "gov"]}
    assert ml._calc_unrecorded_streak(cfg, today) == {"tdnet": 2, "gov": 1}
    # price_universe_jp の記録が 10/11 以来無い → 5日 > 許容4日
    (tmp_path / "module_ledger_external_2026-10-11.json").write_text(
        json.dumps({"price_universe_jp": {"status": "success"}}), encoding="utf-8")
    assert ml._calc_external_stale(cfg, today) == {"price_universe_jp": 5}
    (tmp_path / "module_ledger_external_2026-10-14.json").write_text(
        json.dumps({"price_universe_jp": {"status": "success"}}), encoding="utf-8")
    assert ml._calc_external_stale(cfg, today) == {}
