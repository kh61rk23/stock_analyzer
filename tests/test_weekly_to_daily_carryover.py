# 日次モジュールが週次モジュールの成果を受け取れること（2026-09-12）
#
# テーマETFの ArXiv スコアが全8テーマで 50（中立）に固定されていた。
# 原因は etf_tracker が **日次**、arxiv_monitor が **週次** なのに、
# 論文を results["arxiv"]（その実行のメモリ上）からしか受け取って
# いなかったこと。実測では月曜(2026-09-07)だけ 100/50/100/50/50/100/100 と
# 動き、他の曜日は一律 50 だった＝熱量スコアの4割が週6日死んでいた。
#
# 同型の依存が signal_aggregator（日次・23カテゴリ）にもあり、
# market_breadth / anomaly_season は results からしか読んでいなかった。
# cluster_phase だけは以前からキャッシュを読んでおり扱いが不統一だった。
import json
import types
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "data"; c = tmp_path / "cache"
    d.mkdir(); c.mkdir()
    return types.SimpleNamespace(DATA_DIR=str(d), CACHE_DIR=str(c),
                                 _data=d, _cache=c)


def _paper(days_ago, title="quantum computing qubit"):
    return {"title": title, "summary": "qubit coherence",
            "published": (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")}


class TestArxivCarryOver:
    THEME = {"arxiv_keywords": ["quantum computing", "qubit"]}

    def test_without_carryover_score_is_stuck_at_neutral(self):
        """従来の挙動（週次が回っていない日）"""
        import thematic_etf_tracker as t
        score, _ = t._calc_arxiv_trend_score(self.THEME, None)
        assert score == 50.0

    def test_saved_papers_are_reloaded(self, cfg):
        import thematic_etf_tracker as t
        papers = [_paper(5), _paper(10), _paper(40)]
        t._save_arxiv_papers(cfg, papers)
        assert len(t._load_arxiv_papers(cfg)) == 3

    def test_carried_papers_produce_a_moving_score(self, cfg):
        import thematic_etf_tracker as t
        t._save_arxiv_papers(cfg, [_paper(5), _paper(10), _paper(40)])
        carried = t._load_arxiv_papers(cfg)
        score, detail = t._calc_arxiv_trend_score(self.THEME, {"papers": carried})
        assert score != 50.0, "持ち越しても中立のまま"
        assert detail["recent_4w"] == 2 and detail["prev_4w"] == 1

    def test_stale_cache_is_rejected(self, cfg):
        """古すぎる論文は『直近4週』が空になり比較が壊れるので使わない"""
        import thematic_etf_tracker as t
        old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
        t._arxiv_cache_path(cfg).write_text(
            json.dumps({"saved_at": old, "papers": [_paper(45)]}), encoding="utf-8")
        assert t._load_arxiv_papers(cfg) == []

    def test_missing_cache_is_not_an_error(self, cfg):
        import thematic_etf_tracker as t
        assert t._load_arxiv_papers(cfg) == []

    def test_extract_papers_accepts_key_variants(self):
        import thematic_etf_tracker as t
        for key in ("papers", "new_papers_list", "all_papers"):
            assert t._extract_papers({key: [_paper(1)]})
        assert t._extract_papers({"new_papers": 150}) == [], "件数(int)を論文リストと誤認"


class TestSignalAggregatorWeeklyFallback:
    def _results(self, cfg):
        return {"_cfg": cfg, "sectors": {"1016": {"composite_score": 45.0}}}

    def test_breadth_falls_back_to_cache(self, cfg):
        import signal_aggregator as sa
        (cfg._data / "market_breadth_cache.json").write_text(json.dumps({
            "breadth_score_jp": 70.0, "divergence": {},
            "macro_adjustment": {"1016": 5},
            "timestamp": datetime.now().isoformat(),
        }), encoding="utf-8")
        assert sa._extract_breadth_score(self._results(cfg), "1016") == 75.0

    def test_anomaly_falls_back_to_cache(self, cfg):
        import signal_aggregator as sa
        (cfg._data / "anomaly_season_cache.json").write_text(json.dumps({
            "adjustments": {"1016": 2},
            "timestamp": datetime.now().isoformat(),
        }), encoding="utf-8")
        assert sa._extract_anomaly_score(self._results(cfg), "1016") == 60.0

    def test_results_take_priority_over_cache(self, cfg):
        """その日に週次が回ったら、そちらを使う"""
        import signal_aggregator as sa
        (cfg._data / "market_breadth_cache.json").write_text(json.dumps({
            "breadth_score_jp": 10.0, "timestamp": datetime.now().isoformat(),
        }), encoding="utf-8")
        r = self._results(cfg)
        r["market_breadth"] = {"breadth_score_jp": 80.0}
        assert sa._extract_breadth_score(r, "1016") == 80.0

    def test_stale_weekly_cache_is_rejected(self, cfg):
        import signal_aggregator as sa
        old = (datetime.now() - timedelta(days=40)).isoformat()
        (cfg._data / "market_breadth_cache.json").write_text(json.dumps({
            "breadth_score_jp": 70.0, "timestamp": old,
        }), encoding="utf-8")
        assert sa._extract_breadth_score(self._results(cfg), "1016") is None

    def test_no_cache_still_returns_none(self, cfg):
        import signal_aggregator as sa
        assert sa._extract_breadth_score(self._results(cfg), "1016") is None

    def test_coverage_improves_on_a_daily_run(self, cfg):
        """週次キーが results に無くても、キャッシュがあれば拾える"""
        import signal_aggregator as sa
        for name, payload in (
            ("market_breadth_cache.json", {"breadth_score_jp": 60.0}),
            ("anomaly_season_cache.json", {"adjustments": {"1016": 1}}),
        ):
            payload["timestamp"] = datetime.now().isoformat()
            (cfg._data / name).write_text(json.dumps(payload), encoding="utf-8")
        out = sa.calc_signal_score("1016", self._results(cfg), sa.DEFAULT_WEIGHTS)
        comps = out["components"]
        assert comps["market_breadth"] is not None
        assert comps["anomaly_season"] is not None
