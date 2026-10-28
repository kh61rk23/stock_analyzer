"""業種エージェントのレポートの「技術ノード」節（先読みした技術マップから会社ごとに上位ノード）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base_agent as ba


def test_tech_node_lines(monkeypatch):
    monkeypatch.setattr(ba, "_TECH_PROFILES", {
        "6723": [{"node_id": "semiconductor_logic", "node_name_ja": "半導体ロジック", "relevance_score": 0.6,
                  "hotness": 72.4, "leading_only": True, "confidence_type": "text_match"},
                 {"node_id": "auto", "node_name_ja": "車載", "relevance_score": 0.4, "hotness": None,
                  "confidence_type": "fallback"}]})
    lines = ba.tech_node_lines([{"ticker": "6723.T", "name": "ルネサス"}, {"ticker": "9999.T", "name": "なし"}])
    assert "### 技術ノード（技術マップ）" in lines
    row = [l for l in lines if l.startswith("- ")]
    assert row == ["- **ルネサス**（6723.T）: 半導体ロジック（関与 0.60・熱度 72・先行） / 車載（関与 0.40・補い）"]


def test_no_section_without_profiles(monkeypatch):
    monkeypatch.setattr(ba, "_TECH_PROFILES", {})
    assert ba.tech_node_lines([{"ticker": "6723.T"}]) == []
