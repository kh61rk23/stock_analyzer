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


# ── 2026-09-26: e-Stat 10桁時間コード / 同月比較 / 陳腐化フェイルオーバー ──
class TestEstatTimeCode:
    def test_monthly_10digit_code(self):
        from gov_data_pipeline import _parse_time_code
        assert _parse_time_code("2026000707") == "2026-07"
        assert _parse_time_code("2026000000") == "2026"
        assert _parse_time_code("2026100000") == "2026"

    def test_yoy_compares_same_month_not_december(self):
        # 建設着工の実データ: 2026-07=17043 / 2025-12=133452 / 2025-07=15377
        records = [
            {"time": "2026-07", "value": 17043.0},
            {"time": "2025-12", "value": 133452.0},
            {"time": "2025-07", "value": 15377.0},
        ]
        assert calculate_changes(records)["yoy_change"] == 10.83


class TestStaleFailover:
    def _patch(self, monkeypatch, table):
        import gov_data_pipeline as g
        monkeypatch.setattr(g, "fetch_estat_data_by_id",
                            lambda sid, app, limit=24: (table.get(sid), False))
        monkeypatch.setattr(g, "_get_all_candidate_ids", lambda *a: ["OLD2", "NEW"])
        monkeypatch.setattr(g.time, "sleep", lambda s: None)
        return g

    def test_stale_primary_triggers_failover(self, monkeypatch, tmp_path):
        old = [{"time": "2005-12", "time_code": "2005001212", "value": 1.0}]
        new = [{"time": "2026-07", "time_code": "2026000707", "value": 2.0}]
        g = self._patch(monkeypatch, {"OLD": old, "OLD2": old, "NEW": new})
        recs, used = g.fetch_estat_with_failover("OLD", "k", "C", "w", None, tmp_path)
        assert used == "NEW" and recs == new
        assert (tmp_path / "failover_id_C.txt").read_text() == "NEW"

    def test_no_fresh_candidate_returns_stale_and_marks_month(self, monkeypatch, tmp_path):
        old = [{"time": "2005-12", "time_code": "2005001212", "value": 1.0}]
        g = self._patch(monkeypatch, {"OLD": old, "OLD2": old})
        recs, used = g.fetch_estat_with_failover("OLD", "k", "C", "w", None, tmp_path)
        assert used == "OLD" and recs == old
        assert list(tmp_path.glob("failover_exhausted_C_*.txt"))

    def test_empty_primary_triggers_failover(self, monkeypatch, tmp_path):
        new = [{"time": "2026-07", "time_code": "2026000707", "value": 2.0}]
        g = self._patch(monkeypatch, {"EMPTY": None, "NEW": new})
        recs, used = g.fetch_estat_with_failover("EMPTY", "k", "C", "w", None, tmp_path)
        assert used == "NEW" and recs == new


# ── 2026-09-30: e-Stat の統計表ID追跡をやめ、FRED（固定の系列ID）から取る ──
class TestFredSource:
    def test_csv_parsing_skips_missing_and_sorts_desc(self, monkeypatch):
        import types
        import requests
        import gov_data_pipeline as g
        csv = "observation_date,X\n2026-04-01,1.0\n2026-05-01,.\n2026-06-01,2.5\n"
        monkeypatch.setattr(requests, "get", lambda *a, **k: types.SimpleNamespace(
            status_code=200, text=csv))
        recs, err = g.fetch_fred_series("X")
        assert err == "" and [r["time"] for r in recs] == ["2026-06", "2026-04"]

    def test_yoy_series_is_used_as_is(self):
        import gov_data_pipeline as g
        ch = g.fred_changes([{"time": "2026-06", "value": 2.46}, {"time": "2026-05", "value": 0.7}], "yoy")
        assert ch["yoy_change"] == 2.46 and ch["latest_time"] == "2026-06"

    def test_estat_ids_no_longer_used_for_fred_stats(self):
        import gov_data_pipeline as g
        fred_ids = {d["id"] for d in g.STAT_DEFINITIONS if d.get("fred")}
        assert {"industrial_production", "wages", "household_spending",
                "unemployment", "trade_exports"} <= fred_ids

    def test_stale_series_detected(self):
        import gov_data_pipeline as g
        assert g._months_old("2024-03") > g.FRED_STALE_MONTHS
