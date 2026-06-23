# signal_dedup.py — 重複排除・上限適用 [③]
from conftest import load_module

sd = load_module("signal_dedup")


def _sig(code, direction, pts, priority=1, source="test"):
    return {"sector_code": code, "direction": direction,
            "pts": pts, "priority": priority, "source": source}


class TestDeduplicate:
    def test_same_direction_cap(self, tmp_cfg):
        """同方向シグナルは上限件数（3）まで採用、超過分は理由つき棄却"""
        sigs = [_sig("1016", "tailwind", 2.0, priority=p) for p in (5, 4, 3, 2, 1)]
        approved, rejected, net = sd.deduplicate_signals(sigs, tmp_cfg)
        assert len(approved["1016"]) == 3
        assert len(rejected["1016"]) == 2
        assert all("上限" in r["reject_reason"] for r in rejected["1016"])

    def test_priority_order_kept(self, tmp_cfg):
        """優先度の高いシグナルが採用される"""
        sigs = [_sig("1016", "tailwind", 1.0, priority=1),
                _sig("1016", "tailwind", 1.0, priority=9),
                _sig("1016", "tailwind", 1.0, priority=5),
                _sig("1016", "tailwind", 1.0, priority=2)]
        approved, rejected, _ = sd.deduplicate_signals(sigs, tmp_cfg)
        assert {s["priority"] for s in approved["1016"]} == {9, 5, 2}

    def test_positive_total_cap(self, tmp_cfg):
        """合計が上限（+15pt）を超えたらトリムされる"""
        sigs = [_sig("1016", "tailwind", 10.0, priority=3),
                _sig("1016", "tailwind", 10.0, priority=2)]
        _, _, net = sd.deduplicate_signals(sigs, tmp_cfg)
        assert net[0]["net_pts"] == 15.0

    def test_negative_total_cap(self, tmp_cfg):
        """合計が下限（-15pt）を下回ったらトリムされる"""
        sigs = [_sig("1016", "headwind", -10.0, priority=3),
                _sig("1016", "headwind", -12.0, priority=2)]
        _, _, net = sd.deduplicate_signals(sigs, tmp_cfg)
        assert net[0]["net_pts"] == -15.0

    def test_sectors_isolated(self, tmp_cfg):
        """業種をまたいだ合算はされない"""
        sigs = [_sig("1016", "tailwind", 10.0),
                _sig("1017", "tailwind", 10.0)]
        _, _, net = sd.deduplicate_signals(sigs, tmp_cfg)
        assert {n["sector_code"]: n["net_pts"] for n in net} == \
               {"1016": 10.0, "1017": 10.0}
