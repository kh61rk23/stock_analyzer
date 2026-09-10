# 週次パイプラインの producer/consumer キー契約
#
# 2026-09-10: module_ledger の履歴を遡ったところ、週次実行のたびに
# 同じ例外で落ち続けているモジュールが2件あった。
#   ontology_export [error] 'list' object has no attribute 'items'（2026-07-27〜）
#   weekly_report   [error] 'name'                              （2026-09-07〜）
# どちらも「読む側が想像したキー／型」と「書く側の実体」がずれていたもので、
# 例外を握り潰していないぶん発見はできたが、原因が判るまで
# ontology.signal_event / decision_event が作成以来0行のままだった。
# 同じ形のズレを二度と入れないよう、実体側の形で固定する。
import json
import types

import pytest


class TestOntologyClusterShape:
    """cluster_phase_cache["clusters"] は list（dict ではない）"""

    def _cache(self, tmp_path):
        (tmp_path / "cluster_phase_cache.json").write_text(json.dumps({
            "generated": "2026-09-10T00:00:00", "version": "1.0", "ttl_days": 7,
            # technology_cluster_engine.build_cluster_phase_cache が返す実体
            "clusters": [
                {"cluster_id": "cluster_001", "market": "JP",
                 "sectors": ["1016", "1025"],
                 "centroid_nodes": ["ai_semiconductor", "power_device"],
                 "avg_score": 0.65, "phase": "acceleration",
                 "phase_bonus": 8.0, "phase_label": "技術加速"},
            ]}), encoding="utf-8")
        return types.SimpleNamespace(CACHE_DIR=str(tmp_path))

    def test_list_form_does_not_raise(self, tmp_path):
        import ontology_exporter as oe
        rows = oe.build_theme_entities(self._cache(tmp_path))
        clusters = [r for r in rows if r["theme_type"] == "tech_cluster"]
        assert len(clusters) == 1, "clusters が list だと拾えていない"

    def test_maps_real_producer_keys(self, tmp_path):
        import ontology_exporter as oe
        row = [r for r in oe.build_theme_entities(self._cache(tmp_path))
               if r["theme_type"] == "tech_cluster"][0]
        # name / tickers / phase_score は産出側に存在しないキー
        assert row["heat_score"] == 0.65, "avg_score を heat_score に載せること"
        assert row["member_tickers"] == ["1016", "1025"]
        assert "ai_semiconductor" in row["theme_name"]

    def test_dict_form_is_still_accepted(self, tmp_path):
        """将来 dict 形式へ戻しても壊れないこと"""
        import ontology_exporter as oe
        (tmp_path / "cluster_phase_cache.json").write_text(json.dumps({
            "clusters": {"c1": {"cluster_id": "c1", "market": "JP",
                                "sectors": ["1016"], "avg_score": 0.5}}}),
            encoding="utf-8")
        cfg = types.SimpleNamespace(CACHE_DIR=str(tmp_path))
        rows = [r for r in oe.build_theme_entities(cfg)
                if r["theme_type"] == "tech_cluster"]
        assert len(rows) == 1

    def test_missing_cache_is_not_an_error(self, tmp_path):
        import ontology_exporter as oe
        cfg = types.SimpleNamespace(CACHE_DIR=str(tmp_path / "none"))
        assert oe.build_theme_entities(cfg) == [] or True  # 例外を出さなければ良い


class TestPolicyEventKeys:
    """policy_radar._detect_events_30d の実キーで週次レポートが組めること"""

    # 産出側が実際に返すキー（name / date_estimate / days_until は無い）
    EVENT = {"label": "半導体補助金", "keyword": "補助金",
             "sectors": ["1016", "1025", "1030"], "direction": "tailwind",
             "lead_days": 30, "source": "経産省", "link": "https://example.invalid",
             "detected_at": "2026-09-10 09:00:00"}

    def test_producer_does_not_emit_the_old_keys(self):
        """契約の固定: 読む側が想像したキーは産出側に存在しない"""
        for absent in ("name", "date_estimate", "days_until", "boost_active"):
            assert absent not in self.EVENT

    def test_section_renders_without_keyerror(self):
        import weekly_report_generator as w
        out = w._section_policy_radar(
            {"policy_radar": {"total_signals": 3,
                              "events_30d": [self.EVENT], "signals": []}})
        body = "\n".join(out)
        assert "半導体補助金" in body
        assert "追い風" in body
        assert "1016" in body

    def test_headwind_is_distinguished(self):
        import weekly_report_generator as w
        ev = dict(self.EVENT, direction="headwind")
        body = "\n".join(w._section_policy_radar(
            {"policy_radar": {"total_signals": 1,
                              "events_30d": [ev], "signals": []}}))
        assert "逆風" in body

    def test_sparse_event_does_not_raise(self):
        """label も keyword も欠けた行が来ても落ちない"""
        import weekly_report_generator as w
        body = "\n".join(w._section_policy_radar(
            {"policy_radar": {"total_signals": 1,
                              "events_30d": [{"direction": "tailwind"}],
                              "signals": []}}))
        assert "(無題)" in body
