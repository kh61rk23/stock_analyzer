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
