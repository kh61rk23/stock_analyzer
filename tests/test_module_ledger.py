# module_ledger.py — 実行台帳 [OBS-1]
import json
from datetime import datetime, timedelta

from conftest import load_module

ml = load_module("module_ledger")
TODAY = datetime.now().strftime("%Y-%m-%d")


def _log(module, status, note="", date=TODAY):
    return {"datetime": f"{date} 08:00:00", "module": module,
            "status": status, "note": note}


class TestBuildDailyLedger:
    def test_status_separation(self, tmp_cfg):
        logs = [_log("gov", "success"), _log("inference", "error"),
                _log("screener", "skipped")]
        led = ml.build_daily_ledger(tmp_cfg, logs)
        assert led["stats"] == {"success": 1, "error": 1,
                                "skipped": 1, "warn": 0, "total": 3}

    def test_size_tier_alias_is_not_reported_as_unwired(self, tmp_cfg):
        """size_tier は size_decay_manager / cross_size_signal で記録される"""
        tmp_cfg.DAILY_MODULES = ["size_tier"]
        led = ml.build_daily_ledger(
            tmp_cfg, [_log("size_decay_manager", "success"),
                      _log("cross_size_signal", "success")])
        assert "size_tier" not in led["unrecorded"]

        # 本当に記録が無いときは従来どおり配線漏れとして挙げる
        led_none = ml.build_daily_ledger(tmp_cfg, [_log("unrelated", "success")])
        assert "size_tier" in led_none["unrecorded"]

    def test_warn_and_alert_counted(self, tmp_cfg):
        """warn / alert が success にも error にも入らず埋もれないこと"""
        logs = [_log("gov", "success"), _log("edinetdb_ingest", "alert"),
                _log("anomaly_detector", "warn")]
        led = ml.build_daily_ledger(tmp_cfg, logs)
        assert led["stats"]["warn"] == 2
        assert led["stats"]["success"] == 1
        assert led["stats"]["error"] == 0

    def test_old_logs_excluded(self, tmp_cfg):
        led = ml.build_daily_ledger(
            tmp_cfg, [_log("old", "error", date="2020-01-01")])
        assert led["stats"]["total"] == 0

    def test_unrecorded_detection(self, tmp_cfg):
        """DAILY_MODULES登録済みなのに記録なし＝配線漏れ検出"""
        led = ml.build_daily_ledger(tmp_cfg, [_log("gov", "success")])
        assert "news" in led["unrecorded"] and "macro" in led["unrecorded"]

    def test_last_entry_wins(self, tmp_cfg):
        logs = [_log("gov", "error"), _log("gov", "success")]
        led = ml.build_daily_ledger(tmp_cfg, logs)
        assert led["modules"]["gov"]["status"] == "success"

    def test_alias_resolves_reversed_prefix(self, tmp_cfg):
        """mode-key と記録キーがズレる場合はエイリアスで記録済みと判定する。

        screener（mode-key）は stock_screener として記録されるため、
        接頭辞ルール（mode_key + "_"）では救えず誤検出されていた（B-3）。
        """
        led = ml.build_daily_ledger(tmp_cfg, [_log("stock_screener", "success")])
        assert "screener" not in led["unrecorded"]

    def test_alias_does_not_mask_real_gap(self, tmp_cfg):
        """エイリアスが無関係なモジュールの記録で穴を埋めないこと。"""
        led = ml.build_daily_ledger(tmp_cfg, [_log("stock_screener", "success")])
        assert {"gov", "macro", "news"} <= set(led["unrecorded"])

    def test_cfg_can_override_aliases(self, tmp_cfg):
        """cfg.LEDGER_MODE_KEY_ALIASES で追加のズレを吸収できること。"""
        tmp_cfg.LEDGER_MODE_KEY_ALIASES = {"news": ["news_pipeline_v2"]}
        led = ml.build_daily_ledger(tmp_cfg, [_log("news_pipeline_v2", "success")])
        assert "news" not in led["unrecorded"]


class TestConsecutiveFailures:
    def test_three_day_streak_alerts(self, tmp_cfg):
        for i in (1, 2):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            (tmp_cfg.LOG_DIR / f"module_ledger_{d}.json").write_text(
                json.dumps({"date": d, "modules": {
                    "inference": {"status": "error", "note": "", "datetime": ""}}}),
                encoding="utf-8")
        led = ml.finalize_daily_ledger(
            tmp_cfg, [_log("inference", "error", "NameError")], send_alert=False)
        assert led["consecutive_failures"]["inference"] == 3
        assert led["alerts"][0]["module"] == "inference"

    def test_success_breaks_streak(self, tmp_cfg):
        d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        (tmp_cfg.LOG_DIR / f"module_ledger_{d1}.json").write_text(
            json.dumps({"date": d1, "modules": {
                "gov": {"status": "success", "note": "", "datetime": ""}}}),
            encoding="utf-8")
        led = ml.finalize_daily_ledger(
            tmp_cfg, [_log("gov", "error")], send_alert=False)
        assert led["consecutive_failures"]["gov"] == 1
        assert led["alerts"] == []


class TestFreshnessApi:
    def test_last_success_today(self, tmp_cfg):
        ml.finalize_daily_ledger(tmp_cfg, [_log("gov", "success")],
                                 send_alert=False)
        assert ml.get_module_freshness(tmp_cfg)["gov"] == TODAY
