# devil_calibration.py — デビル校正ノート [DEPT-3]
from pathlib import Path

from conftest import load_module

dc = load_module("devil_calibration")

LEDGER = """| case_id | ticker | 判断 | conviction | 層 | 未解消指摘 | 状態 | 成績（後日） |
|---|---|---|---|---|---|---|---|
| 2026-05-01_7203T | 7203.T | Buy | 4 | カジノ | 0 | 承認 | +12.0% |
| 2026-05-02_6501T | 6501.T | Buy | 4 | カジノ | 1 | 承認 | -4.0% |
| 2026-05-03_8001T | 8001.T | Buy | 5 | NISA | 0 | 承認 | +20.0% |
| 2026-05-04_9101T | 9101.T | Avoid | 3 | カジノ | 2 | 承認 | |
"""

DEVIL_MEMO = """# 検証メモ
### BP-1 への検証
- 判定: A（堅牢）
### RP-1 への検証
- 判定: C（要再確認）
"""


def _vault(tmp_cfg) -> Path:
    return Path(tmp_cfg.INVESTMENT_DEPT_VAULT_DIR)


class TestEmptyVault:
    def test_no_data_message(self, tmp_cfg):
        note = dc.build_note(_vault(tmp_cfg))
        assert "実績データなし" in note
        assert "断定しない" in note


class TestWithHistory:
    def test_conviction_stats(self, tmp_cfg):
        v = _vault(tmp_cfg)
        v.joinpath("40_判断台帳.md").write_text(LEDGER, encoding="utf-8")
        note = dc.build_note(v)
        # conviction 4 = (+12-4)/2 = +4.0% / 2件
        assert "| 4 | 2 | +4.0% | 1/2 |" in note
        assert "| 5 | 1 | +20.0% | 1/1 |" in note
        # 成績なし行（Avoid）は集計から除外される
        assert "サンプル3件" in note  # 少数サンプル警告

    def test_devil_judgement_scan(self, tmp_cfg):
        v = _vault(tmp_cfg)
        case = v / "20_審議" / "2026-05-01_7203T"
        case.mkdir(parents=True)
        (case / "devil.md").write_text(DEVIL_MEMO, encoding="utf-8")
        note = dc.build_note(v)
        assert "A 1 / B 0 / C 1" in note

    def test_layer_stats(self, tmp_cfg):
        v = _vault(tmp_cfg)
        v.joinpath("40_判断台帳.md").write_text(LEDGER, encoding="utf-8")
        note = dc.build_note(v)
        assert "| NISA | 1 | +20.0% |" in note
