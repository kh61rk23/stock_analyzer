# run_headless._upload_to_gdrive_oauth — Drive差分同期と並列送信
#
# 2026-09-08: 全2,089ファイルを毎回無条件送信していたのを差分同期化。
# 2026-09-09: 逐次送信で初回31分かかっていたのを並列化。
import importlib.util
import json
import sys
import threading
import types

import pytest

REPO_FILE = "run_headless.py"


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Files:
    def __init__(self, counter, lock, existing=None):
        self.counter = counter
        self.lock = lock
        self.seq = [0]
        self.existing = existing or {}

    def list(self, **_kw):
        with self.lock:
            self.counter["list"] += 1
        return _Exec({"files": [{"id": v, "name": k}
                                for k, v in self.existing.items()]})

    def create(self, **_kw):
        with self.lock:
            self.counter["create"] += 1
            self.seq[0] += 1
            n = self.seq[0]
        return _Exec({"id": f"id{n}"})

    def update(self, **_kw):
        with self.lock:
            self.counter["update"] += 1
        return _Exec({"id": "updated"})


@pytest.fixture
def gdrive(monkeypatch):
    """Google API をモックした run_headless を返す"""
    counter = {"list": 0, "create": 0, "update": 0, "build": 0}
    lock = threading.Lock()
    files = _Files(counter, lock)

    class _Service:
        def files(self):
            return files

    def _build(*_a, **_k):
        with lock:
            counter["build"] += 1
        return _Service()

    mods = {
        "google": types.ModuleType("google"),
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.credentials": types.ModuleType(
            "google.oauth2.credentials"),
        "google.auth": types.ModuleType("google.auth"),
        "google.auth.transport": types.ModuleType("google.auth.transport"),
        "google.auth.transport.requests": types.ModuleType(
            "google.auth.transport.requests"),
        "googleapiclient": types.ModuleType("googleapiclient"),
        "googleapiclient.discovery": types.ModuleType(
            "googleapiclient.discovery"),
        "googleapiclient.http": types.ModuleType("googleapiclient.http"),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    mods["google.oauth2.credentials"].Credentials = (
        lambda **_k: types.SimpleNamespace(refresh=lambda _r: None))
    mods["google.auth.transport.requests"].Request = lambda: None
    mods["googleapiclient.discovery"].build = _build
    mods["googleapiclient.http"].MediaFileUpload = lambda _p: object()

    spec = importlib.util.spec_from_file_location("rh_test", REPO_FILE)
    rh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rh)
    rh._counter = counter
    return rh


def _tree(tmp_path, n_files=5):
    ins = tmp_path / "insights"
    (ins / "sub").mkdir(parents=True)
    for i in range(n_files):
        (ins / f"f{i}.md").write_text(f"body{i}", encoding="utf-8")
    (ins / "sub" / "deep.md").write_text("deep", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.log").write_text("log", encoding="utf-8")
    return ins, logs


def _sync(rh, ins, logs, state):
    rh._upload_to_gdrive_oauth("ci", "cs", "rt", "root",
                               str(ins), str(logs), state_path=state)


class TestIncrementalSync:
    def test_first_run_uploads_everything(self, gdrive, tmp_path):
        ins, logs = _tree(tmp_path, 5)
        state = tmp_path / "state.json"
        _sync(gdrive, ins, logs, state)
        # 5 + sub/deep + logs/run = 7ファイル
        assert gdrive._counter["create"] >= 7
        assert len(json.loads(state.read_text(encoding="utf-8"))) == 7

    def test_second_run_makes_no_api_calls(self, gdrive, tmp_path):
        ins, logs = _tree(tmp_path, 5)
        state = tmp_path / "state.json"
        _sync(gdrive, ins, logs, state)
        before = dict(gdrive._counter)
        _sync(gdrive, ins, logs, state)
        for k in ("list", "create", "update"):
            assert gdrive._counter[k] == before[k], (
                f"変更が無いのに {k} が呼ばれている")

    def test_only_changed_file_is_sent(self, gdrive, tmp_path):
        ins, logs = _tree(tmp_path, 5)
        state = tmp_path / "state.json"
        _sync(gdrive, ins, logs, state)
        before = dict(gdrive._counter)

        (ins / "f0.md").write_text("CHANGED CONTENT", encoding="utf-8")
        _sync(gdrive, ins, logs, state)

        sent = (gdrive._counter["create"] - before["create"]
                + gdrive._counter["update"] - before["update"])
        # フォルダ作成分を含むため厳密一致は取らないが、
        # 全件(7)を送り直していないことを担保する
        assert sent < 7, "変更1件なのに全件送り直している"

    def test_missing_state_file_falls_back_to_full_sync(self, gdrive,
                                                        tmp_path):
        """署名台帳が消えても壊れず、全件送り直すだけ"""
        ins, logs = _tree(tmp_path, 3)
        state = tmp_path / "state.json"
        _sync(gdrive, ins, logs, state)
        state.unlink()
        before = dict(gdrive._counter)
        _sync(gdrive, ins, logs, state)
        assert gdrive._counter["create"] > before["create"]

    def test_corrupt_state_file_is_tolerated(self, gdrive, tmp_path):
        ins, logs = _tree(tmp_path, 2)
        state = tmp_path / "state.json"
        state.write_text("{ not json", encoding="utf-8")
        _sync(gdrive, ins, logs, state)
        assert json.loads(state.read_text(encoding="utf-8"))


class TestParallelUpload:
    def test_each_thread_gets_its_own_service(self, gdrive, tmp_path):
        """httplib2 はスレッドセーフでないため service を共有しない"""
        ins, logs = _tree(tmp_path, 30)
        state = tmp_path / "state.json"
        _sync(gdrive, ins, logs, state)
        # 直列用の1つ + 送信スレッド分（<= _UPLOAD_WORKERS + 1）
        assert gdrive._counter["build"] > 1, "スレッド毎のserviceが作られていない"
        assert gdrive._counter["build"] <= gdrive._UPLOAD_WORKERS + 1

    def test_all_files_are_uploaded_exactly_once(self, gdrive, tmp_path):
        ins, logs = _tree(tmp_path, 20)
        state = tmp_path / "state.json"
        _sync(gdrive, ins, logs, state)
        recorded = json.loads(state.read_text(encoding="utf-8"))
        assert len(recorded) == 22, "並列送信で取りこぼし/重複がある"

    def test_failed_upload_is_not_recorded_so_it_retries(self, gdrive,
                                                         tmp_path,
                                                         monkeypatch):
        """失敗したファイルは署名を残さず、次回再送されること"""
        ins, logs = _tree(tmp_path, 4)
        state = tmp_path / "state.json"

        real_media = sys.modules["googleapiclient.http"].MediaFileUpload
        calls = {"n": 0}

        def _flaky(path):
            calls["n"] += 1
            if "f1.md" in str(path):
                raise RuntimeError("upload boom")
            return real_media(path)

        monkeypatch.setitem(sys.modules["googleapiclient.http"].__dict__,
                            "MediaFileUpload", _flaky)
        _sync(gdrive, ins, logs, state)

        recorded = json.loads(state.read_text(encoding="utf-8"))
        assert not any(k.endswith("f1.md") for k in recorded), (
            "失敗したファイルが同期済みとして記録されている（次回再送されない）")
        assert any(k.endswith("f0.md") for k in recorded), (
            "1件の失敗で他のファイルまで巻き添えになっている")
