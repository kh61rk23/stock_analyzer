"""obsidian_sync の STATE_STORE_MODE=bq の経路（偽の BQ）。git / dual では今のまま同期用 clone を使う。"""
import json
import types

import obsidian_sync as osync
import state_store as ss
from tests.test_state_store import FakeBackend


def _cfg(tmp_path, mode):
    return types.SimpleNamespace(
        STATE_STORE_MODE=mode, DATA_DIR=str(tmp_path / "mac" / "data"), DRIVE_BASE=str(tmp_path / "mac"),
        INSIGHT_DIR=str(tmp_path / "mac" / "insights"), LOG_DIR=str(tmp_path / "mac" / "logs"),
        CACHE_DIR=str(tmp_path / "mac" / "cache"),
        STATE_STORE_PATHS=("stock_data/", "data/portfolio_holdings.csv"),
        STATE_STORE_EXCLUDE=("stock_data/models/",), STATE_STORE_CHUNK_BYTES=1024)


def _patch(tmp_path, monkeypatch, be):
    monkeypatch.delenv("STATE_STORE_MODE", raising=False)
    monkeypatch.setattr(osync, "STATE_ROOT", tmp_path / "state_root")
    monkeypatch.setattr(osync, "SYNC_CACHE", tmp_path / "no_clone")
    monkeypatch.setattr(ss, "BQBackend", lambda cfg: be)


def test_state_dir_by_mode(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch, FakeBackend())
    assert osync._state_dir(_cfg(tmp_path, "bq")) == tmp_path / "state_root"
    assert osync._state_dir(_cfg(tmp_path, "dual")) == tmp_path / "no_clone"
    assert osync._state_dir(_cfg(tmp_path, "git")) == tmp_path / "no_clone"


def test_mirror_saves_to_bq_without_clone(tmp_path, monkeypatch):
    be = FakeBackend()
    _patch(tmp_path, monkeypatch, be)
    import deliberation_scheduler as ds
    monkeypatch.setattr(ds, "_load_ledger_tickers", lambda cfg: {"7203.T"})
    cfg = _cfg(tmp_path, "bq")
    (tmp_path / "mac" / "data").mkdir(parents=True)
    assert osync.cmd_mirror(cfg) is True
    latest = ss.latest_versions(r for _, r in be.iter_meta())
    assert list(latest) == ["stock_data/data/deliberated_tickers.json"]
    # Mac 用の stock_data にも同じ中身を置く
    mac = json.loads((tmp_path / "mac" / "data" / "deliberated_tickers.json").read_text(encoding="utf-8"))
    assert mac["tickers"] == ["7203.T"]


def test_restore_state_then_mirror_copy(tmp_path, monkeypatch):
    be = FakeBackend()
    src = tmp_path / "actions"
    (src / "stock_data" / "data").mkdir(parents=True)
    (src / "stock_data" / "data" / "x.json").write_text('{"x": 1}', encoding="utf-8")
    ss.save(be, src, ("stock_data/",), (), source="daily_run", run_id="1")
    _patch(tmp_path, monkeypatch, be)
    got = osync.restore_state(_cfg(tmp_path, "bq"))
    assert got == tmp_path / "state_root" / "stock_data"
    assert (got / "data" / "x.json").read_text(encoding="utf-8") == '{"x": 1}'


def test_restore_actions_logs_needs_credentials(tmp_path, monkeypatch):
    be = FakeBackend()
    src = tmp_path / "actions"
    (src / "stock_data" / "logs").mkdir(parents=True)
    (src / "stock_data" / "logs" / "actions_daily_run_2026-10-20.log").write_text("full", encoding="utf-8")
    ss.save(be, src, ("stock_data/",), (), source="daily_run", run_id="1")
    _patch(tmp_path, monkeypatch, be)
    for k in ("BQ_CREDENTIALS", "BQ_CREDENTIALS_PATH", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(k, raising=False)
    cfg = _cfg(tmp_path, "dual")
    osync.restore_actions_logs(cfg)
    assert not (tmp_path / "mac" / "logs").exists()
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/dev/null")
    osync.restore_actions_logs(cfg)
    assert (tmp_path / "mac" / "logs" / "actions_daily_run_2026-10-20.log").read_text(encoding="utf-8") == "full"
