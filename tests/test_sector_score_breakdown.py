import json
import re
from pathlib import Path

import sector_score_breakdown as sbd


def _res(score, name="食料品"):
    r = {"composite_score": score, "sector_name_ja": name}
    sbd.begin(r)
    return r


def test_steps_sum_to_final_and_other_catches_unmarked():
    r = _res(30.0)
    r["composite_score"] = 20.0
    sbd.mark_one(r, "currency_adjustment")
    r["composite_score"] = 22.4
    sbd.mark_one(r, "us_macro_adjustment")
    r["composite_score"] = 21.0  # mark の付け忘れ
    out = sbd.finalize({"1004": r})["1004"]
    assert out["steps"] == {"currency_adjustment": -10.0, "us_macro_adjustment": 2.4, "other": -1.4}
    assert out["raw"] + sum(out["steps"].values()) == out["final"] == 21.0
    assert "_sbd" not in r


def test_cuts_and_summary():
    r = _res(30.0)
    sbd.note_cut(r, "currency_adjustment", 5.0)
    r["composite_score"] = 20.0
    sbd.mark_one(r, "currency_adjustment")
    e = sbd.finalize({"1004": r})["1004"]
    assert e["cuts"] == {"currency_adjustment": 5.0}
    s = sbd.summary_line(e)
    assert "多通貨-10.0" in s and "削減" in s and s.endswith("20.0")


def test_unmoved_sector_has_no_steps():
    r = _res(50.0)
    sbd.mark_one(r, "season_adjustment")
    assert sbd.finalize({"1": r})["1"]["steps"] == {}


def test_rank_corr_and_rows():
    bd = {str(i): {"raw": float(i), "final": float(i), "steps": {}, "cuts": {}, "name": "x", "total_adj": 0.0} for i in range(5)}
    assert sbd.rank_corr(bd) == 1.0
    bd["4"]["final"] = -5.0
    assert sbd.rank_corr(bd) < 1.0
    rows = sbd.bq_rows(bd, "2026-11-10", "t")
    assert len(rows) == 5 and json.loads(rows[0]["steps_json"]) == {}


def test_capped_adjustment_records_cut():
    import stock_analyzer_main as m
    r = _res(50.0)
    sr = {"1": r}
    m._apply_capped_adjustment(sr, {"1": 25.0}, "x_adj", cap_config_key="SCORE_MAX_CROSS_ADJ")
    sbd.mark(sr, "x_adj")
    e = sbd.finalize(sr)["1"]
    assert e["steps"]["x_adj"] == 10.0 and e["cuts"]["x_adj"] == 15.0


def test_later_adjustments_share_results_sectors_dict():
    # ベイズ更新後に sector_results を results["sectors"] と同じ辞書へ付け替えていること
    src = (Path(__file__).resolve().parent.parent / "stock_analyzer_main.py").read_text(encoding="utf-8")
    assert len(re.findall(r"^\s+sector_results = _updated$", src, re.M)) == 2
