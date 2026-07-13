# ==============================================================
# tests/test_gov_data_quality.py — gov_data_pipeline データ品質ガードの回帰テスト
# ==============================================================
# 2026-07-12 実行ログ照合で発見した実害の再発防止:
#   ・毎月勤労統計(2009年)・労働力調査(2005年)の前年比がシグナル化されていた → 鮮度ガード
#   ・建設着工「前年比-83.5%」（カテゴリ混在起因の異常値）がシグナル化 → 異常値クランプ
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from gov_data_pipeline import calculate_changes, judge_sector_impact  # noqa: E402

STAT = {"id": "test", "name": "テスト統計", "direction": "higher_is_good"}


class TestStaleGuard:
    def test_stale_data_neutralized(self):
        # 最新が2年以上前 → シグナル化しない（2009年勤労統計の実害の遮断）
        changes = {"latest_value": 100.0, "latest_time": "2009-06",
                   "yoy_change": -3.8, "mom_change": None}
        out = judge_sector_impact(STAT, changes)
        assert out["signal"] == "neutral"
        assert out["score"] == 0
        assert "陳腐化" in out["reason"]

    def test_fresh_data_passes(self):
        changes = {"latest_value": 100.0, "latest_time": "2026-05",
                   "yoy_change": 1.2, "mom_change": None}
        out = judge_sector_impact(STAT, changes)
        assert out["signal"] == "tailwind"


class TestAnomalyClamp:
    def test_extreme_change_neutralized(self):
        # |前年比|>50% はカテゴリ混在等のデータ品質問題 → 無効化（-83.5%の実害）
        changes = {"latest_value": 10.0, "latest_time": "2026-05",
                   "yoy_change": -83.5, "mom_change": None}
        out = judge_sector_impact(STAT, changes)
        assert out["signal"] == "neutral"
        assert "異常" in out["reason"]

    def test_normal_headwind_passes(self):
        changes = {"latest_value": 95.0, "latest_time": "2026-05",
                   "yoy_change": -2.0, "mom_change": None}
        out = judge_sector_impact(STAT, changes)
        assert out["signal"] == "headwind"


class TestCalculateChanges:
    def test_yoy_uses_same_series_prior_year(self):
        records = [
            {"time": "2026-05", "value": 105.0, "time_code": "202605"},
            {"time": "2026-04", "value": 104.0, "time_code": "202604"},
            {"time": "2025-05", "value": 100.0, "time_code": "202505"},
        ]
        ch = calculate_changes(records)
        assert ch["yoy_change"] == 5.0
        assert ch["latest_time"] == "2026-05"
