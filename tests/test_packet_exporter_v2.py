# ==============================================================
# tests/test_packet_exporter_v2.py — R3-1 exporter v2 の受入テスト
# ==============================================================
# 受入条件（要件定義書 §R3-1）:
#   7203.Tでパケット生成し、新セクション（⑪ontology時系列・⑫バリュエーション）が
#   データ欠損時も「データなし」表示で壊れないこと。
# ネットワーク/BQなし環境で完結（BQ接続は自動でデータなしにフォールバック）。
# ==============================================================
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import agent_packet_exporter as ape                    # noqa: E402


class Cfg:
    def __init__(self, tmp):
        self.DATA_DIR = tmp / "data"
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.CACHE_DIR = tmp / "cache"
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.BQ_PROJECT_ID = "stock-analyzer-edinet"


class TestValuationSection:
    def test_no_data_is_graceful(self, tmp_path):
        """キャッシュ皆無でもデータなし表示で壊れない。"""
        cfg = Cfg(tmp_path)
        out = ape.collect_valuation(cfg, "7203.T")
        text = "\n".join(out)
        assert "バリュエーションレンジ" in text
        assert "データなし" in text                    # DCF/comps/scenario全て欠損

    def test_dcf_and_comps_render(self, tmp_path):
        cfg = Cfg(tmp_path)
        (cfg.DATA_DIR / "dcf_valuation_cache.json").write_text(json.dumps(
            {"7203.T": {"min_price": 2000, "base_price": 3000,
                        "max_price": 4200, "dcf_bonus": 3.0}}), encoding="utf-8")
        (cfg.DATA_DIR / "comps_analyzer_cache.json").write_text(json.dumps(
            {"7203.T": {"comps_score": 68.0, "per_rank": 30.0,
                        "pbr_rank": 25.0, "n_peers": 12}}), encoding="utf-8")
        out = "\n".join(ape.collect_valuation(cfg, "7203.T"))
        assert "弱気 ¥2,000" in out and "強気 ¥4,200" in out
        assert "割安スコア 68" in out and "peers=12" in out
        assert "last_updated" in out

    def test_code_fallback(self, tmp_path):
        """.T なしの証券コードキーでも引ける。"""
        cfg = Cfg(tmp_path)
        (cfg.DATA_DIR / "dcf_valuation_cache.json").write_text(json.dumps(
            {"7203": {"min_price": 100, "base_price": 150, "max_price": 200}}),
            encoding="utf-8")
        out = "\n".join(ape.collect_valuation(cfg, "7203.T"))
        assert "基本 ¥150" in out

    def test_scenario_pnl_render(self, tmp_path):
        cfg = Cfg(tmp_path)
        (cfg.DATA_DIR / "scenario_analysis_cache.json").write_text(json.dumps(
            {"7203.T": {"scenario_pnl": {
                "bear_case": {"pnl_pct": -12.5}, "bull_case": {"pnl_pct": 18.0}}}}),
            encoding="utf-8")
        out = "\n".join(ape.collect_valuation(cfg, "7203.T"))
        assert "bear_case: -12.5%" in out and "bull_case: +18.0%" in out


class TestOntologySection:
    def test_bq_unavailable_is_graceful(self, tmp_path):
        """BQ未接続でも見出し＋データなしで壊れない。"""
        cfg = Cfg(tmp_path)
        out = "\n".join(ape.collect_ontology_events(cfg, "7203.T"))
        assert "イベント時系列" in out
        assert "データなし" in out


class TestBuildPacket:
    def test_full_packet_7203(self, tmp_path):
        """受入条件: 7203.Tで全体パケットが例外なく生成され新セクションを含む。"""
        cfg = Cfg(tmp_path)
        md = ape.build_packet(cfg, "7203.T")
        assert "審議パケット: 7203.T" in md
        assert "⑪ イベント時系列" in md
        assert "⑫ バリュエーションレンジ" in md
        # データ欠損でも審議引き継ぎまで到達している
        assert "審議への引き継ぎ事項" in md
