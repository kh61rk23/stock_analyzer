"""地政学の権益の抽出（edinet_exposure_extractor）が読む節の名前が、有報本文の取り込みの節の名前と一致すること。
名前がずれると本文を1つも読めず、権益が手書きの十数社のままになる。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import edinet_gov_text


def test_target_sections_exist_in_text_blocks():
    known = set(edinet_gov_text.SECTIONS.values())
    assert config.GEO_EDINET_TARGET_SECTIONS
    assert set(config.GEO_EDINET_TARGET_SECTIONS) <= known, set(config.GEO_EDINET_TARGET_SECTIONS) - known


def test_all_listed_by_default():
    assert config.GEO_EDINET_ALL_LISTED is True
