# obsidian_sync.cmd_mirror — 審議済みミラーの自動 push（2026-09-29）
#   Mac で審議したあと deliberated_tickers.json を手でコミットしないと、
#   Actions のメールの審議キューに反映されなかった。
import json
import subprocess
import types

import obsidian_sync as osync


def _git(*a, cwd=None):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def _setup(tmp_path, monkeypatch, tickers):
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git("clone", "-q", str(origin), str(seed))
    (seed / "README").write_text("x")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
         "-m", "init", cwd=seed)
    _git("add", "README", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "readme", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    cache = tmp_path / "cache"
    _git("clone", "-q", str(origin), str(cache))
    _git("config", "user.name", "t", cwd=cache)
    _git("config", "user.email", "t@t", cwd=cache)
    monkeypatch.setattr(osync, "SYNC_CACHE", cache)
    import deliberation_scheduler as ds
    monkeypatch.setattr(ds, "_load_ledger_tickers", lambda cfg: set(tickers))
    monkeypatch.setattr(ds, "export_deliberated_mirror", lambda cfg: {"skipped": True})
    return origin, cache


def test_mirror_pushes_only_when_changed(tmp_path, monkeypatch):
    origin, cache = _setup(tmp_path, monkeypatch, {"7203.T", "543A.T"})
    cfg = types.SimpleNamespace()
    assert osync.cmd_mirror(cfg) is True
    log = subprocess.run(["git", "--git-dir", str(origin), "log", "--oneline", "-1"],
                         capture_output=True, text=True).stdout
    assert "審議済みミラー更新（2銘柄" in log
    shown = subprocess.run(["git", "--git-dir", str(origin), "show",
                            "main:stock_data/data/deliberated_tickers.json"],
                           capture_output=True, text=True).stdout
    assert json.loads(shown)["tickers"] == ["543A.T", "7203.T"]
    # 2回目は同じ内容なので push しない
    assert osync.cmd_mirror(cfg) is True
    n = subprocess.run(["git", "--git-dir", str(origin), "rev-list", "--count", "main"],
                       capture_output=True, text=True).stdout.strip()
    assert n == "3"


def test_empty_ledger_does_not_push(tmp_path, monkeypatch):
    origin, _ = _setup(tmp_path, monkeypatch, set())
    assert osync.cmd_mirror(types.SimpleNamespace()) is True
    n = subprocess.run(["git", "--git-dir", str(origin), "rev-list", "--count", "main"],
                       capture_output=True, text=True).stdout.strip()
    assert n == "2", "台帳が読めないときに空のミラーで上書きしてはいけない"
