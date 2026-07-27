# size_tier_classifier.py — 規模分類マスター（2026-07-27 X-1 の回帰固定）
#
# X-1: module_ledger 2026-07-06 / 07-13 が2回連続で
#      "JP:0 / US:0 / Large:0 Mid:0 Small:0 Micro:0 Nano:0" を success 記録。
#      真因は実行順序（screener/us_screener より前に配線されていた）。
#      本ファイルは「空マスターを有効キャッシュとして扱わない」「候補ゼロを
#      成功として返さない」「US の単位換算」を固定する。
import json

from conftest import load_module

stc = load_module("size_tier_classifier")


class TestIsEmptyMaster:
    def test_none_is_empty(self):
        assert stc.is_empty_master(None) is True

    def test_no_entries_is_empty(self):
        assert stc.is_empty_master({"jp": {}, "us": {}, "stats": {}}) is True

    def test_jp_only_is_not_empty(self):
        assert stc.is_empty_master({"jp": {"7203.T": {}}, "us": {}}) is False

    def test_us_only_is_not_empty(self):
        assert stc.is_empty_master({"jp": {}, "us": {"AAPL": {}}}) is False


class TestLoadMasterEmptyGuard:
    """空マスターは TTL 内でも None（＝再構築）を返すこと"""

    def _write(self, cfg, payload):
        p = stc._cache_path(cfg)
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    def test_fresh_but_empty_returns_none(self, tmp_cfg):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        self._write(tmp_cfg, {"last_updated": today, "jp": {}, "us": {}})
        assert stc._load_master(tmp_cfg) is None

    def test_fresh_and_populated_returns_data(self, tmp_cfg):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        self._write(tmp_cfg, {
            "last_updated": today,
            "jp": {"7203.T": {"size_tier": "Large"}},
            "us": {},
        })
        data = stc._load_master(tmp_cfg)
        assert data is not None
        assert data["jp"]["7203.T"]["size_tier"] == "Large"

    def test_save_master_refuses_empty(self, tmp_cfg):
        stc._save_master(tmp_cfg, {"jp": {}, "us": {}})
        assert not stc._cache_path(tmp_cfg).exists()

    def test_get_size_tier_none_on_empty_master(self, tmp_cfg):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        self._write(tmp_cfg, {"last_updated": today, "jp": {}, "us": {}})
        assert stc.get_size_tier("7203.T", tmp_cfg) is None


class TestCollectUsCandidates:
    """US の時価総額は 億USD（SIZE_TIER_THRESHOLDS_US と同一単位）で持つこと"""

    def test_raw_usd_to_oku(self):
        out = stc._collect_us_candidates({
            "us_screener": {"passed": [
                {"ticker": "AAPL", "market_cap": 3.5e12},
            ]}
        })
        assert out[0]["market_cap_b"] == 35000.0

    def test_tier_matches_ssot_unit(self):
        # 500億USD（=$50B）は Mid（>=200億USD）。旧 1e9 換算では Small に誤落ちしていた。
        out = stc._collect_us_candidates({
            "us_screener": {"passed": [{"ticker": "X", "market_cap": 5.0e10}]}
        })
        tier = stc.classify_size_tier(
            out[0]["market_cap_b"], stc._DEFAULT_THRESHOLDS_US
        )
        assert tier == "Mid"

    def test_skipped_us_screener_returns_empty(self):
        assert stc._collect_us_candidates(
            {"us_screener": {"skipped": True}}
        ) == []


class TestPriceKeyContract:
    """両スクリーナーの実キーは current_price（last_price/price は存在しない）"""

    def test_jp_current_price_is_read(self):
        out = stc._collect_jp_candidates({
            "screener": {"candidates": [
                {"ticker": "1234.T", "current_price": 85.0,
                 "market_cap_b": 30},
            ]}
        })
        assert out[0]["last_price"] == 85.0

    def test_us_current_price_is_read(self):
        out = stc._collect_us_candidates({
            "us_screener": {"passed": [
                {"ticker": "X", "market_cap": 1e10, "current_price": 12.5},
            ]}
        })
        assert out[0]["last_price"] == 12.5

    def test_penny_flag_now_reachable(self, tmp_cfg):
        res = stc.run_size_tier_classifier(tmp_cfg, {
            "screener": {"candidates": [
                {"ticker": "1234.T", "current_price": 85.0,
                 "market_cap_b": 30, "sector_code": "1033"},
            ]}
        })
        assert res["master"]["jp"]["1234.T"]["penny_flag"] is True


class TestRunSkipSignal:
    """候補ゼロ／空マスターを success として返さないこと"""

    def test_no_candidates_returns_skipped(self, tmp_cfg):
        res = stc.run_size_tier_classifier(tmp_cfg, {})
        assert res["skipped"] is True
        assert res["master"] == {}
        assert "no_candidates" in res["reason"]

    def test_empty_results_key_returns_skipped(self, tmp_cfg):
        # main の results 初期値（screener={}）を再現
        res = stc.run_size_tier_classifier(
            tmp_cfg, {"screener": {}, "us_screener": {}}
        )
        assert res["skipped"] is True

    def test_builds_master_when_candidates_present(self, tmp_cfg):
        results = {
            "screener": {"candidates": [
                {"ticker": "7203.T", "name": "トヨタ", "market_cap_b": 35000,
                 "last_price": 2800, "avg_volume": 5_000_000,
                 "sector_code": "1017", "roe": 0.10},
            ]},
            "us_screener": {"passed": [
                {"ticker": "AAPL", "market_cap": 3.5e12,
                 "last_price": 220, "avg_volume": 50_000_000},
            ]},
        }
        res = stc.run_size_tier_classifier(tmp_cfg, results)
        assert res.get("skipped") is not True
        assert res["stats"]["jp_total"] == 1
        assert res["stats"]["us_total"] == 1
        assert res["master"]["jp"]["7203.T"]["size_tier"] == "Large"
        assert res["master"]["us"]["AAPL"]["size_tier"] == "Large"
        assert stc._cache_path(tmp_cfg).exists()

    def test_empty_cache_is_rebuilt_not_returned(self, tmp_cfg):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        stc._cache_path(tmp_cfg).write_text(
            json.dumps({"last_updated": today, "jp": {}, "us": {},
                        "stats": {"jp_total": 0, "us_total": 0}}),
            encoding="utf-8",
        )
        results = {"screener": {"candidates": [
            {"ticker": "6758.T", "market_cap_b": 200, "last_price": 3000,
             "avg_volume": 1_000_000, "sector_code": "1017"},
        ]}}
        res = stc.run_size_tier_classifier(tmp_cfg, results)
        assert res["cached"] is False
        assert res["stats"]["jp_total"] == 1
