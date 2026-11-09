"""横断産業シグナル・先行指標の補正は全業種に当たる（経済サイクル補正のある業種の中だけに閉じない）。"""
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "stock_analyzer_main.py").read_text(encoding="utf-8").split("\n")


def _indent(marker: str) -> int:
    line = next(l for l in SRC if marker in l)
    return len(line) - len(line.lstrip())


def test_cross_and_leading_are_siblings_of_cycle_if():
    cyc = _indent("if ec_adj and isinstance(res, dict) and code in ec_adj:")
    assert _indent("# ── [R-1] 横断産業シグナル補正") == cyc
    assert _indent("# ── [R-2] 先行指標シグナル補正") == cyc
    assert _indent('logger.add("sector_agent", "success",') == cyc
