"""tools/sync_invest_rules.py の除外判定・収集の単体テスト。"""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "sync_invest_rules_cli", Path(__file__).resolve().parent.parent / "tools" / "sync_invest_rules.py")
sir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sir)


@pytest.mark.parametrize("rel", [
    "B1-bull.md.bak_20260929", "B1-bull.md.bak_20260929b", ".DS_Store", "skills/dist/invest-dept.skill",
    "skills/invest-dept/x.skill", "foo_key.json", "gcp_key.json", "client_secret_1.json", "OAuth-pass.json",
    "my-credentials.txt", "skills/dist/anything.md", "notes.zip", ".env.local",
])
def test_excluded(rel):
    assert sir.is_excluded(rel)


@pytest.mark.parametrize("rel", ["B1-bull.md", "_運用ルール.md", "tools/fundamental_packet.py",
                                 "skills/invest-dept/SKILL.md", "25_デビル校正ノート.md"])
def test_not_excluded(rel):
    assert not sir.is_excluded(rel)


def test_collect_maps_rules_tools_and_invest_skills_only(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "skills" / "invest-dept").mkdir(parents=True)
    (tmp_path / "skills" / "invest-b1-bull").mkdir()
    (tmp_path / "skills" / "other-skill").mkdir()
    (tmp_path / "skills" / "dist").mkdir()
    for rel in ["B1-bull.md", "B1-bull.md.bak_1", "_運用ルール.md", "40_判断台帳.md", "tools/fundamental_packet.py",
                "tools/portfolio_risk.py", "skills/invest-dept/SKILL.md", "skills/invest-dept/key.pem",
                "skills/invest-b1-bull/SKILL.md", "skills/other-skill/SKILL.md", "skills/dist/invest-dept.skill"]:
        (tmp_path / rel).write_text("x", encoding="utf-8")
    pairs, excluded, skipped = sir.collect(tmp_path)
    dst = sorted(d for _, d in pairs)
    assert dst == sorted([
        "investment_dept/B1-bull.md", "investment_dept/_運用ルール.md",
        "investment_dept/tools/fundamental_packet.py",
        ".claude/skills/invest-dept/SKILL.md", ".claude/skills/invest-b1-bull/SKILL.md"])
    assert "skills/invest-dept/key.pem" in excluded
    assert skipped == []


def test_collect_skips_non_text_in_skill(tmp_path):
    (tmp_path / "skills" / "invest-dept").mkdir(parents=True)
    (tmp_path / "skills" / "invest-dept" / "SKILL.md").write_text("x", encoding="utf-8")
    (tmp_path / "skills" / "invest-dept" / "logo.png").write_bytes(b"\x89PNG")
    pairs, _, skipped = sir.collect(tmp_path)
    assert [d for _, d in pairs] == [".claude/skills/invest-dept/SKILL.md"]
    assert skipped == ["skills/invest-dept/logo.png"]


def test_scan_detects_secrets(tmp_path):
    p = tmp_path / "a.md"
    p.write_text('"private_key": "abc"', encoding="utf-8")
    assert sir.scan(p)
    p.write_text("ふつうの部則", encoding="utf-8")
    assert sir.scan(p) == []


# ── 保存先は BigQuery（git には入れない） ─────────────────────

def _vault(tmp_path):
    src = tmp_path / "vault"
    for rel in ["B1-bull.md", "_運用ルール.md", "tools/fundamental_packet.py", "skills/invest-dept/SKILL.md",
                "skills/invest-b1-bull/SKILL.md"]:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel, encoding="utf-8")
    return src


class _FakeDocs:
    """sync_config_docs の normalize_names / save_to_bq の代わり（BQ に触れない）。"""

    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def normalize_names(self, root):
        return 0

    def save_to_bq(self, root, paths, source, delete_missing):
        self.calls.append((Path(root), paths, source, delete_missing))
        return self.rc


def test_save_paths_lists_invest_skill_folders_only(tmp_path):
    (tmp_path / ".claude/skills/invest-new").mkdir(parents=True)
    (tmp_path / ".claude/skills/other").mkdir(parents=True)
    paths = sir.save_paths(tmp_path, [(None, ".claude/skills/invest-x/SKILL.md")])
    assert paths[0] == "investment_dept/"
    assert set(paths[1:]) == {f".claude/skills/{n}/" for n in
                              list(sir.KNOWN_SKILLS) + ["invest-new", "invest-x"]}
    assert all(p.endswith("/") for p in paths)


def test_main_apply_copies_and_saves_to_bq_without_git(tmp_path, monkeypatch):
    monkeypatch.setattr(sir, "SRC", _vault(tmp_path))
    fake = _FakeDocs()
    monkeypatch.setattr(sir, "_load_sync_config_docs", lambda: fake)
    root = tmp_path / "stage"
    assert sir.main(["--apply", "--root", str(root)]) == 0
    assert (root / "investment_dept/_運用ルール.md").read_text(encoding="utf-8") == "_運用ルール.md"
    assert (root / ".claude/skills/invest-b1-bull/SKILL.md").exists()
    assert not (root / ".git").exists()
    (r, paths, source, delete_missing) = fake.calls[0]
    assert r == root and source == "invest-rules" and delete_missing is False
    assert "investment_dept/" in paths and ".claude/skills/invest-dept/" in paths


def test_main_prune_marks_deletions(tmp_path, monkeypatch):
    monkeypatch.setattr(sir, "SRC", _vault(tmp_path))
    fake = _FakeDocs()
    monkeypatch.setattr(sir, "_load_sync_config_docs", lambda: fake)
    root = tmp_path / "stage"
    stale = root / "investment_dept/old.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("x", encoding="utf-8")
    assert sir.main(["--apply", "--prune", "--root", str(root)]) == 0
    assert not stale.exists() and fake.calls[0][3] is True


def test_main_dry_run_does_not_copy_or_save(tmp_path, monkeypatch):
    monkeypatch.setattr(sir, "SRC", _vault(tmp_path))
    fake = _FakeDocs()
    monkeypatch.setattr(sir, "_load_sync_config_docs", lambda: fake)
    root = tmp_path / "stage"
    assert sir.main(["--root", str(root)]) == 0
    assert not root.exists() and fake.calls == []


def test_main_returns_nonzero_when_bq_save_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sir, "SRC", _vault(tmp_path))
    monkeypatch.setattr(sir, "_load_sync_config_docs", lambda: _FakeDocs(rc=7))
    root = tmp_path / "stage"
    assert sir.main(["--apply", "--root", str(root)]) == 7
    assert (root / "investment_dept/B1-bull.md").exists()      # コピーまでは済んでいる


def test_main_aborts_on_secret_without_copying(tmp_path, monkeypatch):
    src = _vault(tmp_path)
    (src / "B1-bull.md").write_text('"private_key": "abc"', encoding="utf-8")
    monkeypatch.setattr(sir, "SRC", src)
    fake = _FakeDocs()
    monkeypatch.setattr(sir, "_load_sync_config_docs", lambda: fake)
    root = tmp_path / "stage"
    assert sir.main(["--apply", "--root", str(root)]) == 2
    assert not root.exists() and fake.calls == []
