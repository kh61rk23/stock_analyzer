# ==============================================================
# tests/test_ml_meta.py — R5-1 MLメタモデルの受入テスト
# ==============================================================
# 受入条件（要件定義書 §R5-1）:
#   ・リークテスト: 特徴量に未来情報が混入していないことを日付シフトテストで保証
#   ・embargo有無でICが変わること（Purged CVの分割が実際に変化すること）の確認
# 加えて: 補正テーブルの昇格基準検証・±10pt上限・decile割当・snapshot冪等性。
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from ml.corrector_applier import (apply_corrections, assign_deciles,   # noqa: E402
                                  predict_score, validate_table)
from ml.dataset_builder import (build_feature_rows,                    # noqa: E402
                                forward_excess_returns, snapshot)
from ml.purged_cv import (ic_t_stat, purged_walk_forward_splits,       # noqa: E402
                          rank_ic)


# ── ヘルパ ────────────────────────────────────────────────────

def _dates(n):
    """営業日の代用として単調増加する日付文字列をn個生成。"""
    return [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)]


class _Cfg:
    """テスト用の最小config。"""
    SECTOR_REPRESENTATIVE_TICKERS = {"1028": ["8306.T", "8316.T"],
                                     "1017": ["7203.T"]}
    ML_META_DATASET_MAX_DAYS = 500

    def __init__(self, tmp):
        self.ML_META_DATASET_PATH = str(tmp / "features.jsonl")
        self.CACHE_DIR = tmp


def _results():
    return {
        "economic_cycle": {"phase": "A"},
        "signal_aggregator": {
            "sector_signals": {
                "1028": {"components": {"sector_score": 62.0,
                                        "macro_signals": 55.0,
                                        "taxonomy_signal": None}},
                "1017": {"components": {"sector_score": 48.0}},
            },
        },
    }


# ── Purged Walk-Forward CV ───────────────────────────────────

class TestPurgedCV:
    def test_test_always_after_train(self):
        dates = _dates(300)
        for tr, te in purged_walk_forward_splits(dates, 5, 20, 20):
            assert max(tr) < min(te)   # walk-forward: trainは常にtestより過去

    def test_embargo_changes_splits(self):
        # 受入条件: embargo有無で分割（→IC）が変わること
        dates = _dates(300)
        with_e = purged_walk_forward_splits(dates, 5, 20, 20)
        without_e = purged_walk_forward_splits(dates, 5, 0, 0)
        assert len(with_e) == len(without_e) == 5
        # embargoありは train が gap 分小さい
        for (tr_e, _), (tr_n, _) in zip(with_e, without_e):
            assert len(tr_e) < len(tr_n)
        # 削られた行数 = horizon+embargo = 40
        assert len(without_e[0][0]) - len(with_e[0][0]) == 40

    def test_purged_gap_excludes_boundary_dates(self):
        # test直前の (horizon+embargo) 日は train に含まれない
        dates = _dates(300)
        for tr, te in purged_walk_forward_splits(dates, 5, 20, 20):
            test_start_idx = dates.index(min(te))
            banned = set(dates[max(0, test_start_idx - 40):test_start_idx])
            assert not (tr & banned)

    def test_insufficient_data_returns_empty(self):
        assert purged_walk_forward_splits(_dates(4), 5, 20, 20) == []


class TestRankIC:
    def test_perfect_correlation(self):
        assert rank_ic([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_perfect_anticorrelation(self):
        assert rank_ic([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_no_variance_returns_zero(self):
        assert rank_ic([1, 1, 1], [1, 2, 3]) == 0.0

    def test_t_stat(self):
        # 一定の正のICが続けば t は大きい
        assert ic_t_stat([0.05, 0.04, 0.06, 0.05]) > 2.0
        assert ic_t_stat([0.05]) == 0.0


# ── リークテスト（受入条件・最重要） ──────────────────────────

class TestLeakPrevention:
    def test_target_uses_only_future_prices(self):
        # 目的変数は date i と i+H のみに依存 → 過去価格を全部変えても不変
        dates = _dates(60)
        px = {d: 100.0 + i for i, d in enumerate(dates)}
        bench = {d: 200.0 + 0.5 * i for i, d in enumerate(dates)}
        base = forward_excess_returns(dates, px, bench, 20)

        # 日付シフトテスト: date 30 より前の価格を滅茶苦茶にする
        px_shifted = dict(px)
        for d in dates[:30]:
            px_shifted[d] = 9999.0
        shifted = forward_excess_returns(dates, px_shifted, bench, 20)
        # date 30 以降の目的変数は完全一致（過去の情報が混入していない）
        for d in dates[30:40]:   # 30..39 は i+20 <= 59 なので算出可能
            assert shifted[d] == pytest.approx(base[d])

    def test_last_horizon_days_have_no_target(self):
        # 末尾 H 日は未来が未確定 → 目的変数が存在しない（先読みの否定）
        dates = _dates(60)
        px = {d: 100.0 + i for i, d in enumerate(dates)}
        bench = {d: 200.0 for d in dates}
        out = forward_excess_returns(dates, px, bench, 20)
        for d in dates[-20:]:
            assert d not in out

    def test_feature_rows_have_no_target(self):
        # 蓄積時点の行は target=None（後付けラベリング前提＝当日に未来値を持たない）
        rows = build_feature_rows(_Cfg(Path("/nonexistent")), _results(),
                                  "2026-07-12")
        assert rows
        assert all(r["target"] is None for r in rows)


# ── dataset_builder ──────────────────────────────────────────

class TestDatasetBuilder:
    def test_build_rows_expands_universe(self):
        rows = build_feature_rows(_Cfg(Path("/nonexistent")), _results(),
                                  "2026-07-12")
        tickers = {r["ticker"] for r in rows}
        assert tickers == {"8306.T", "8316.T", "7203.T"}
        r = next(r for r in rows if r["ticker"] == "8306.T")
        assert r["sector"] == "1028"
        assert r["regime"] == "A"
        assert r["f_sector_score"] == 62.0
        assert "f_taxonomy_signal" not in r   # None のカテゴリは含めない

    def test_snapshot_idempotent(self, tmp_path):
        cfg = _Cfg(tmp_path)
        out1 = snapshot(cfg, _results())
        out2 = snapshot(cfg, _results())   # 同日再実行 → 置き換え（重複しない）
        assert out1["rows"] == out2["rows"] == 3
        assert out2["total_rows"] == 3
        lines = Path(cfg.ML_META_DATASET_PATH).read_text().strip().splitlines()
        assert len(lines) == 3

    def test_snapshot_skips_when_no_signals(self, tmp_path):
        cfg = _Cfg(tmp_path)
        out = snapshot(cfg, {"signal_aggregator": {}})
        assert out["skipped"] is True


# ── corrector_applier ────────────────────────────────────────

def _good_table():
    return {"trained_at": "2026-07-12", "oos_rank_ic": 0.03, "t_stat": 2.5,
            "decile_adjustments": {"0": -6.0, "9": 6.0},
            "model_coefs": {"_intercept": 0.0, "f_sector_score": 0.01}}


class TestValidateTable:
    def test_good_table_passes(self):
        ok, _ = validate_table(_good_table())
        assert ok

    def test_low_ic_rejected(self):
        t = _good_table(); t["oos_rank_ic"] = 0.01
        ok, reason = validate_table(t)
        assert not ok and "IC" in reason

    def test_low_t_rejected(self):
        t = _good_table(); t["t_stat"] = 1.5
        ok, _ = validate_table(t)
        assert not ok

    def test_adjustment_over_10pt_rejected(self):
        # 補正上限 ±10pt（変更禁止）を超えるテーブルは適用拒否
        t = _good_table(); t["decile_adjustments"]["9"] = 12.0
        ok, reason = validate_table(t)
        assert not ok and "上限" in reason


class TestApplyCorrections:
    def test_deciles_ordered_by_prediction(self):
        preds = {f"t{i}": float(i) for i in range(20)}
        d = assign_deciles(preds, 10)
        assert d["t0"] == 0 and d["t19"] == 9

    def test_correction_clamped_and_recorded(self):
        cands = [{"ticker": "A", "composite_score": 96.0}]
        n = apply_corrections(cands, {"A": 9}, {"9": 8.0}, max_pt=10.0)
        assert n == 1
        assert cands[0]["composite_score"] == 100.0   # 0..100クランプ
        assert cands[0]["ml_meta_correction"] == 8.0

    def test_predict_score_linear(self):
        y = predict_score({"f_a": 2.0}, {"_intercept": 1.0, "f_a": 3.0})
        assert y == pytest.approx(7.0)

    def test_missing_ticker_untouched(self):
        cands = [{"ticker": "B", "composite_score": 50.0}]
        n = apply_corrections(cands, {"A": 9}, {"9": 8.0})
        assert n == 0
        assert cands[0]["composite_score"] == 50.0
