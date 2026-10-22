"""fetch_stages: 取得 workflow の結果ファイルの受け渡し（保存 → 日次本体での写し）。"""
from datetime import date
from types import SimpleNamespace

import fetch_stages as fs


class _Log:
    def __init__(self):
        self.rows = []

    def add(self, m, s, n=""):
        self.rows.append((m, s, n))


def _cfg(tmp_path):
    return SimpleNamespace(DATA_DIR=tmp_path / "data")


def test_roundtrip_copies_results_and_ledger(tmp_path, monkeypatch):
    cfg, d = _cfg(tmp_path), date(2026, 10, 23)
    fs.save_external(cfg, "jp_filings", {"edinetdb_ingest": {"success": 24}, "other": 1},
                     [{"module": "edinetdb_ingest", "status": "success", "note": "取込24社"}], d)
    monkeypatch.setenv(fs.EXTERNAL_ENV, "jp_filings")
    results, log = {}, _Log()
    assert fs.apply_external(cfg, "jp_filings", results, log, d)
    assert results == {"edinetdb_ingest": {"success": 24}}          # グループ外のキーは持ち込まない
    assert log.rows == [("edinetdb_ingest", "success", "取込24社")]


def test_without_env_or_file_the_caller_fetches_itself(tmp_path, monkeypatch):
    cfg, d = _cfg(tmp_path), date(2026, 10, 23)
    monkeypatch.delenv(fs.EXTERNAL_ENV, raising=False)
    fs.save_external(cfg, "jp_filings", {}, [{"module": "x", "status": "success"}], d)
    assert not fs.apply_external(cfg, "jp_filings", {}, _Log(), d)   # 環境変数が無い（Mac 等）
    monkeypatch.setenv(fs.EXTERNAL_ENV, "jp_filings")
    assert not fs.apply_external(cfg, "jp_filings", {}, _Log(), date(2026, 10, 24))   # その日のファイルが無い


def test_recorder_passes_through():
    inner = _Log()
    rec = fs.LedgerRecorder(inner)
    rec.add("a", "success", "n")
    assert rec.entries == [{"module": "a", "status": "success", "note": "n"}] and inner.rows == [("a", "success", "n")]


def test_main_uses_fetch_stages_for_jp_filings():
    from pathlib import Path
    src = (Path(fs.__file__).parent / "stock_analyzer_main.py").read_text(encoding="utf-8")
    assert '_fs.apply_external(cfg, "jp_filings", results, logger)' in src
    assert "run_edinetdb_ingest" not in src            # 取得の本体は fetch_stages の1か所だけ
