# ==============================================================
# tests/test_screener_history_shape.py
# screener_history.json の形式の読み違いを防ぐ
# ==============================================================
# 【何を守るテストか】
# 正本の形式は performance_tracker.append_screener_history が書く
#   {"YYYY-MM-DD": [候補dict, ...]}   ← 日付の値は「リスト」
# 読み手が {"candidates": [...]} という辞書形式を前提に entry.get("candidates")
# を呼ぶと、リストに .get は無いので AttributeError → 各所の except で握り潰され、
#   - agent_packet_exporter: パケット①が常に「データなし」
#   - obsidian_sync:         審議キューが常に0件
#   - deliberation_scheduler: 審議対象が常に「候補なし」
# と、投資部の入口が丸ごと空回りする。3箇所がリスト形式を読めることを固定する。
# ==============================================================
import json
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from performance_tracker import (latest_candidates,          # noqa: E402
                                 normalize_history_entry)

CAND = {"ticker": "6674.T", "name": "ジーエス・ユアサ",
        "composite_score": 89.3, "value_score": 74.4}


class TestNormalizeHistoryEntry:
    def test_list_shape_is_canonical(self):
        """正本形式（リスト）をそのまま読めること。"""
        assert normalize_history_entry([CAND]) == [CAND]

    def test_dict_shape_still_supported(self):
        """旧来の読み手が想定していた辞書形式も許容する。"""
        assert normalize_history_entry({"candidates": [CAND]}) == [CAND]

    def test_garbage_is_empty_not_exception(self):
        """壊れた値でも例外を投げず空リストを返すこと。"""
        for bad in (None, "", 0, {"candidates": None}, [1, "x", None]):
            assert normalize_history_entry(bad) == []


class TestLatestCandidates:
    def test_picks_newest_nonempty_day(self):
        hist = {"2026-07-20": [CAND], "2026-07-22": [{"ticker": "6504.T"}]}
        day, cands = latest_candidates(hist)
        assert day == "2026-07-22"
        assert cands[0]["ticker"] == "6504.T"

    def test_falls_back_past_empty_days(self):
        """直近日が空（休場・スクリーナー不発）でも遡って拾うこと。"""
        hist = {"2026-07-20": [CAND], "2026-07-22": []}
        day, cands = latest_candidates(hist)
        assert day == "2026-07-20"
        assert cands == [CAND]

    def test_empty_history(self):
        assert latest_candidates({}) == ("", [])
        assert latest_candidates(None) == ("", [])


class TestDeliberationSchedulerReadsRealShape:
    def test_selects_target_from_list_shape(self, tmp_cfg):
        """正本形式の履歴から審議対象を選べること（空にならないこと）。"""
        from deliberation_scheduler import _load_screener_candidates
        (Path(tmp_cfg.DATA_DIR) / "screener_history.json").write_text(
            json.dumps({"2026-07-22": [CAND]}, ensure_ascii=False),
            encoding="utf-8")
        cands = _load_screener_candidates(tmp_cfg)
        assert len(cands) == 1
        assert cands[0]["ticker"] == "6674.T"
