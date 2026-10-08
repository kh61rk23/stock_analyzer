# ==============================================================
# tests/test_taxonomy_wiring.py — taxonomy 配線の受入テスト
# ==============================================================
# 対象:
#   R2-1 taxonomy_loader（承認済み抽出・検証・BQ行整形・MERGE SQL）
#   R2-2/3 company_taxonomy_map 行構築（実スキーマ準拠）
#   R2-5 taxonomy_hotness（キーワード照合・先行/準先行分離・業種スコア）
#   R2-6 signal_aggregator カテゴリV（23カテゴリ・重み合計1.00）
# ==============================================================
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from taxonomy_loader import (build_bq_rows, build_merge_sql,           # noqa: E402
                             load_seed_batches, select_approved_nodes,
                             validate_nodes)
from taxonomy_company_mapper import build_company_node_rows            # noqa: E402
from taxonomy_hotness import (build_node_keywords, compute_node_hotness,  # noqa: E402
                              hotness_score, leading_only_nodes,
                              match_counts, sector_scores_from_hotness)


# ── R2-1 loader ──────────────────────────────────────────────

class TestLoader:
    def test_approved_only(self):
        batches = [
            {"_file": "b1", "_meta": {"status": "APPROVED_承認済み",
                                      "taxonomy_version": "v1.0"},
             "nodes": [{"node_id": "a", "node_level": 1, "node_name_ja": "A"}]},
            {"_file": "b2", "_meta": {"status": "DRAFT_承認待ち"},
             "nodes": [{"node_id": "b", "node_level": 1, "node_name_ja": "B"}]},
        ]
        nodes, skipped = select_approved_nodes(batches)
        assert [n["node_id"] for n in nodes] == ["a"]
        assert len(skipped) == 1 and "b2" in skipped[0]

    def test_validate_detects_orphan_and_dup(self):
        nodes = [
            {"node_id": "a", "node_level": 1, "node_name_ja": "A",
             "parent_id": None, "_batch": "b1"},
            {"node_id": "a", "node_level": 2, "node_name_ja": "A2",
             "parent_id": None, "_batch": "b2"},
            {"node_id": "c", "node_level": 2, "node_name_ja": "C",
             "parent_id": "missing", "_batch": "b1"},
        ]
        errors = validate_nodes(nodes)
        assert any("重複" in e for e in errors)
        assert any("孤立親" in e for e in errors)

    def test_real_seed_is_loadable(self):
        """実seedファイル: 承認済みバッチが検証エラーゼロで整形できる。"""
        batches = load_seed_batches()
        assert len(batches) >= 11                     # 全産業11バッチ
        nodes, _ = select_approved_nodes(batches)
        assert len(nodes) >= 30                        # 少なくともbatch1分
        assert validate_nodes(nodes) == []             # 重複/孤立親/必須列ゼロ
        rows = build_bq_rows(nodes, as_of="2026-07-10")
        assert len(rows) == len(nodes)
        r = rows[0]
        # 実スキーマ（bq_all_schemas_20260704.json）の必須列
        for col in ("node_id", "node_level", "is_active", "taxonomy_version",
                    "related_commodities", "updated_at"):
            assert col in r
        json.loads(r["related_commodities"])         # JSON列は文字列で格納

    def test_merge_sql_idempotent_key(self):
        sql = build_merge_sql("proj")
        assert "ON T.node_id = S.node_id" in sql
        assert "WHEN NOT MATCHED THEN INSERT ROW" in sql


# ── R2-2/3 company map ───────────────────────────────────────

class TestCompanyMap:
    def _mapper(self, name):
        table = {"半導体事業": {"sector_code": "1017", "similarity_score": 0.9},
                 "物流事業": {"sector_code": "1028", "similarity_score": 0.8}}
        return table.get(name)

    def _s2n(self, code):
        return {"1017": ["semiconductor_logic", "electronic_component"],
                "1028": ["resource_trading"]}.get(code, [])

    def test_build_rows_normalized(self):
        segs = {"7203": [{"segment_name": "半導体事業", "share": 0.6},
                         {"segment_name": "物流事業", "share": 0.4}]}
        rows = build_company_node_rows(segs, self._mapper, self._s2n,
                                       as_of="2026-07-10")
        assert {r["node_id"] for r in rows} == {
            "semiconductor_logic", "electronic_component", "resource_trading"}
        # relevance_scoreは正規化されて合計1
        assert sum(r["relevance_score"] for r in rows) == pytest.approx(1.0, abs=0.01)
        assert all(r["confidence_type"] == "edinet" for r in rows)
        assert all(r["confidence"] == 0.7 for r in rows)
        assert all(r["auto_derived"] and not r["verified_flag"] for r in rows)

    def test_missing_share_equal_split(self):
        segs = {"9999": [{"segment_name": "半導体事業", "share": None},
                         {"segment_name": "物流事業", "share": None}]}
        rows = build_company_node_rows(segs, self._mapper, self._s2n)
        trading = next(r for r in rows if r["node_id"] == "resource_trading")
        assert trading["revenue_share_est"] == pytest.approx(0.5)

    def test_unmapped_segment_skipped(self):
        segs = {"1111": [{"segment_name": "不明事業", "share": 1.0}]}
        assert build_company_node_rows(segs, self._mapper, self._s2n) == []


# ── R2-5 hotness ─────────────────────────────────────────────

class TestHotness:
    NODES = [
        {"node_id": "hbm", "node_name_ja": "HBM(広帯域メモリ)",
         "node_name_en": "High Bandwidth Memory", "verified_flag": True},
        {"node_id": "solid_state_battery", "node_name_ja": "全固体電池",
         "node_name_en": "Solid-State Battery", "verified_flag": True},
    ]

    def test_keyword_match(self):
        kws = build_node_keywords(self.NODES)
        counts = match_counts(
            ["SK Hynix expands HBM capacity", "全固体電池の量産計画を発表",
             "無関係な記事"], kws)
        assert counts["hbm"] == 1
        assert counts["solid_state_battery"] == 1

    def test_hotness_score_heats_up(self):
        # ベースライン1件/日 → 当日3件 = 加熱（>50）
        assert hotness_score(3, [1, 1, 1]) > 50
        assert hotness_score(0, [1, 1, 1]) < 50
        assert hotness_score(1, [1, 1, 1]) == pytest.approx(50.0)

    def test_leading_semi_separation(self):
        today = "2026-07-10"
        history = {f"2026-06-{d:02d}": {"hbm": {"leading": 1, "news": 1,
                                                "policy": 0}}
                   for d in range(1, 21)}
        # 先行(arxiv)だけ急増・ニュースは平常
        counts = {"hbm": {"leading": 5, "news": 1, "policy": 0}}
        h = compute_node_hotness(counts, history, today)
        assert h["hbm"]["leading"] > h["hbm"]["semi_leading"]
        assert leading_only_nodes(h) == ["hbm"]

    def test_sector_scores_mapping(self):
        hotness = {"hbm": {"combined": 80.0},
                   "solid_state_battery": {"combined": 60.0}}
        s = sector_scores_from_hotness(
            hotness, {"1017": ["hbm"], "9999": ["unknown_node"]})
        assert s == {"1017": 80.0}                   # データなし業種はキーなし

    def test_sector_scores_weighted(self):
        """T-3消費経路: relevance加重（dict形式）で業種スコアを算出。"""
        hotness = {"hbm": {"combined": 90.0},
                   "dram": {"combined": 50.0}}
        # 加重: (90×0.8 + 50×0.2) / 1.0 = 82.0
        s = sector_scores_from_hotness(
            hotness, {"1017": {"hbm": 0.8, "dram": 0.2}})
        assert s["1017"] == pytest.approx(82.0)
        # 均等平均だと (90+50)/2=70 になるはず → 加重で差がつくことを確認
        s_eq = sector_scores_from_hotness(hotness, {"1017": ["hbm", "dram"]})
        assert s_eq["1017"] == pytest.approx(70.0)


# ── R2-6 カテゴリV ────────────────────────────────────────────

class TestCategoryV:
    def test_23_categories_sum_to_one(self):
        from signal_aggregator import DEFAULT_WEIGHTS, _extract_taxonomy_signal
        assert len(DEFAULT_WEIGHTS) == 23
        assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)
        assert DEFAULT_WEIGHTS["taxonomy_signal"] == pytest.approx(0.02)
        # 抽出関数: sector_scores から読む・欠損はNone
        results = {"taxonomy_signal": {"sector_scores": {"1017": 72.5}}}
        assert _extract_taxonomy_signal(results, "1017") == 72.5
        assert _extract_taxonomy_signal(results, "1099") is None
        assert _extract_taxonomy_signal({}, "1017") is None
