"""公開しない資料（構成管理資料・投資部の部則）を state_store の同じ表に置くときの約束。

- Actions の save（STATE_STORE_PATHS）は資料・部則に削除の印を付けない
- 範囲を渡した save は、その範囲の中にしか削除の印を付けない
- restore は `.claude/skills/invest-` のような途中までの前方一致で取り出せる（クラウド routine の使い方）
"""
from pathlib import Path

import state_store as ss
from tests.test_state_store import FakeBackend

EXCL = ("stock_data/models/", "__pycache__/", ".DS_Store")
PRIVATE_SCOPE = ("構成管理資料/", "investment_dept/", ".claude/skills/invest-dept/", ".claude/skills/invest-b1-bull/")
PRIVATE_FILES = {
    "構成管理資料/01_設計書/SPEC_core.md": b"spec",
    "構成管理資料/00_README.md": b"readme",
    "investment_dept/B1-bull.md": b"bull",
    "investment_dept/tools/fundamental_packet.py": b"print(1)",
    ".claude/skills/invest-dept/SKILL.md": b"dept",
    ".claude/skills/invest-b1-bull/SKILL.md": b"b1",
}


def _write(root: Path, rel: str, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _private_saved(tmp_path):
    root = tmp_path / "mac"
    for rel, data in PRIVATE_FILES.items():
        _write(root, rel, data)
    be = FakeBackend()
    r = ss.save(be, root, PRIVATE_SCOPE, EXCL, source="docs-sync")
    assert r.ok and r.uploaded == len(PRIVATE_FILES)
    return be


def _latest(be):
    return ss.latest_versions(x for _, x in be.iter_meta())


def test_config_state_store_paths_do_not_cover_private_docs():
    cfg = ss.load_config()
    for rel in list(PRIVATE_FILES) + [".claude/skills/other/SKILL.md", "説明書_構成管理資料同期_2026-10-15.md"]:
        assert not ss.in_scope(rel, cfg.STATE_STORE_PATHS), rel


def test_actions_save_does_not_tombstone_private_docs(tmp_path):
    be = _private_saved(tmp_path)
    actions = tmp_path / "actions"           # Actions の checkout には資料も部則も無い
    _write(actions, "stock_data/data/a.json", b"{}")
    cfg = ss.load_config()
    r = ss.save(be, actions, tuple(cfg.STATE_STORE_PATHS), tuple(cfg.STATE_STORE_EXCLUDE), source="daily_run",
                run_id="1")
    assert r.deleted == 0 and r.uploaded == 1
    latest = _latest(be)
    assert all(not latest[p]["deleted"] for p in PRIVATE_FILES)


def test_scoped_save_tombstones_only_inside_its_scope(tmp_path):
    be = _private_saved(tmp_path)
    root = tmp_path / "docs_only"
    _write(root, "構成管理資料/00_README.md", b"readme")   # SPEC_core.md を消した
    r = ss.save(be, root, ("構成管理資料/",), EXCL, source="docs-sync")
    assert r.deleted == 1 and r.uploaded == 0
    latest = _latest(be)
    assert latest["構成管理資料/01_設計書/SPEC_core.md"]["deleted"]
    assert not latest["investment_dept/B1-bull.md"]["deleted"]
    assert not latest[".claude/skills/invest-dept/SKILL.md"]["deleted"]


def test_save_delete_missing_false_only_adds_and_updates(tmp_path):
    be = _private_saved(tmp_path)
    root = tmp_path / "partial"
    _write(root, "構成管理資料/00_README.md", b"readme2")
    r = ss.save(be, root, ("構成管理資料/",), EXCL, source="docs-sync", delete_missing=False)
    assert r.uploaded == 1 and r.deleted == 0
    assert not _latest(be)["構成管理資料/01_設計書/SPEC_core.md"]["deleted"]


def test_save_scoped_uses_config_excludes(tmp_path):
    root = tmp_path / "r"
    _write(root, "構成管理資料/a.md", b"a")
    _write(root, "構成管理資料/.DS_Store", b"x")
    be = FakeBackend()

    class C:
        STATE_STORE_EXCLUDE = (".DS_Store",)
    r = ss.save_scoped(root, ("構成管理資料/",), source="docs-sync", cfg=C, backend=be)
    assert r.uploaded == 1 and {x["path"] for x in be.rows} == {"構成管理資料/a.md"}
    assert {x["source"] for x in be.rows} == {"docs-sync"}


def test_restore_partial_prefix_gets_invest_skills_and_rules_only(tmp_path):
    be = _private_saved(tmp_path)
    other = tmp_path / "o"
    _write(other, ".claude/skills/other/SKILL.md", b"o")
    ss.save(be, other, (".claude/skills/other/",), EXCL, source="t")
    cloud = tmp_path / "cloud"
    rr = ss.restore(be, cloud, ["investment_dept/", ".claude/skills/invest-"])
    assert rr.bad == 0 and rr.written == 4
    assert (cloud / ".claude/skills/invest-b1-bull/SKILL.md").read_bytes() == b"b1"
    assert (cloud / "investment_dept/tools/fundamental_packet.py").read_bytes() == b"print(1)"
    assert not (cloud / ".claude/skills/other/SKILL.md").exists()
    assert not (cloud / "構成管理資料").exists()


def test_cli_restore_accepts_routine_arguments(monkeypatch, tmp_path):
    be = _private_saved(tmp_path)
    monkeypatch.setattr(ss, "BQBackend", lambda cfg: be)
    monkeypatch.setenv("STATE_STORE_MODE", "git")      # restore はモードに関係なく動く
    cloud = tmp_path / "cloud"
    assert ss.main(["restore", "--root", str(cloud), "--prefix", "investment_dept/", ".claude/skills/invest-",
                    "--force"]) == 0
    assert (cloud / ".claude/skills/invest-dept/SKILL.md").read_bytes() == b"dept"
    assert not (cloud / "構成管理資料").exists()
    assert ss.main(["restore", "--root", str(cloud), "--prefix", "構成管理資料/", "--force"]) == 0
    assert (cloud / "構成管理資料/01_設計書/SPEC_core.md").read_bytes() == b"spec"


def test_cli_save_refuses_explicit_paths_missing_locally(monkeypatch, tmp_path, capsys):
    be = _private_saved(tmp_path)
    monkeypatch.setattr(ss, "BQBackend", lambda cfg: be)
    n = len(be.rows)
    clone = tmp_path / "fresh_clone"
    clone.mkdir()
    args = ["save", "--root", str(clone), "--paths", "構成管理資料/", "--source", "cloud-docs",
            "--no-baseline", "--force"]
    assert ss.main(args) == 1
    assert len(be.rows) == n and "restore" in capsys.readouterr().out
    # restore してから直して戻すのは通り、直していない資料は消えない
    assert ss.main(["restore", "--root", str(clone), "--prefix", "構成管理資料/", "--force"]) == 0
    _write(clone, "構成管理資料/00_README.md", b"edited in cloud")
    assert ss.main(args) == 0
    latest = _latest(be)
    assert latest["構成管理資料/00_README.md"]["sha256"] == ss.sha256_bytes(b"edited in cloud")
    assert not latest["構成管理資料/01_設計書/SPEC_core.md"]["deleted"]
    assert latest["investment_dept/B1-bull.md"]["sha256"] == ss.sha256_bytes(b"bull")
