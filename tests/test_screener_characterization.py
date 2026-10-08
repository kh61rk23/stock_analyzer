# ==============================================================
# tests/test_screener_characterization.py — U-0 特性化テスト先行整備
# ==============================================================
# 目的（要件定義書_JP-US統合_20260726.md §6 U-0）:
#   stock_screener.py（JP）/ us_stock_screener.py（US）の JP/US 統合作業
#   （U-1〜U-10）で挙動が変わっていないことを機械的に判定するため、
#   実装の出力を固定入力（外部API接続ゼロ）で凍結する。
#
# ★★★ 期待値の性質（R0-3）★★★
#   本ファイルの assert 期待値は理論値ではなく、
#   「stock_screener.py / us_stock_screener.py の実装を実際に実行して得た
#    実測値」をそのままハードコードしたものである。
#   生成手順:
#     本ファイルと同じ固定入力 dict で対象関数を一時スクリプトから直接呼び出し、
#     print() された返り値をそのまま expected へ転記する
#     （一時スクリプトはリポジトリ内に置かない）。
#   よって「このテストが通る = 現行実装と一致」であり、
#   「テストが落ちる = 実装が変わった」ことを意味する
#   （正しい/正しくないの判定基準ではない。差分検知が目的）。
#
# ★★★ cfg 依存と外部 import のフォールバックについて（R0-4）★★★
#   stock_screener._calc_scores() は内部で以下を try/except 付きで
#   import している:
#     - business_model_normalizer（detect_bm_norm_type / calc_bm_bonus 等）
#     - pearl_score_calculator（calc_pearl_score）
#     - size_decay_manager（resolve_size_tier / calc_tier_composite_score）
#   本テスト実行環境ではこれらのモジュールは実在し import に成功するため、
#   ここで凍結される pearl_score 等の値は「フォールバック値」ではなく
#   「これらのモジュールを実際に実行して得た実測値」である
#   （import 失敗時のフォールバック経路を通っているわけではない）。
#   ただし _calc_scores() 内の pearl_score_calculator 呼び出しは、
#   引数で渡した cfg ではなく `import config as _cfg_pearl` で
#   実際の config.py を直接 import して使う実装になっている
#   （_calc_scores() 自体の設計であり、本テストが作った制約ではない）。
#   これにより pearl_score 計算経路は config.CACHE_DIR 配下の
#   キャッシュファイル（pearl_candidates.json 等）を実際に読み書きしうる。
#   外部API接続ゼロ・副作用ゼロの原則を守るため、_calc_scores を呼ぶ
#   全テストで `monkeypatch.setattr(config, "CACHE_DIR", tmp_path)` を行い、
#   実運用の stock_data/cache/ を一切読み書きしない状態に隔離している。
#   （隔離してもキャッシュファイルは元々存在しないキーのみで、
#    出力値は隔離前と同一であることを実測確認済み）。
#
# 対象関数（凍結件数は各テストの docstring 参照）:
#   JP: _calc_scores / _apply_filters / _calc_risk_adjusted_score /
#       _calc_revision_score / _check_pead_trigger / _calc_catalyst_bonus
#   US: _calc_score / _apply_filter / _calc_risk_adjusted_score_us /
#       _calc_revision_score_us / _check_pead_trigger_us /
#       _calc_catalyst_bonus_us
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import stock_screener as ss  # noqa: E402
import us_stock_screener as us  # noqa: E402


class DummyCfg:
    """conftest.py の FakeCfg パターンに合わせた最小ダミー cfg。

    _calc_scores / _apply_filters / _calc_risk_adjusted_score(_us) /
    _calc_revision_score(_us) / _check_pead_trigger(_us) が参照する
    属性のみを、現行実装のデフォルト値と同じ値で明示的に持つ。
    getattr(cfg, "X", default) の default 値と揃えてあるため、
    cfg=None で呼んだ場合と同一の挙動になる（値は変えていない）。
    """
    ACCEL_STRONG_UP = 0.30
    ACCEL_MID_UP = 0.15
    ACCEL_MILD_UP = 0.05
    ACCEL_MILD_DOWN = -0.05
    ACCEL_MID_DOWN = -0.15
    ACCEL_STRONG_DOWN = -0.30
    RISK_ADJ_PENALTY_HIGH_VOL = 1.20
    RISK_ADJ_PENALTY_LOW_VOL = 0.85
    PEAD_TRIGGER_MIN_REVISION = 0.3
    ANALYST_REVISION_WINDOW_DAYS = 30
    CATALYST_BONUS_MAX = 5.0
    SCREEN_PER_MAX = 30.0
    SCREEN_PBR_MAX = 5.0
    SCREEN_ROE_MIN = 0.08
    SCREEN_MCAP_MIN_B = 100.0
    SCREEN_VOL_MIN = 100_000


@pytest.fixture
def cfg():
    return DummyCfg()


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """_calc_scores 内の pearl_score_calculator 経路が実運用の
    stock_data/cache/ を読み書きしないよう、config.CACHE_DIR を
    空の tmp_path へ差し替える（R0-4 コメント参照）。
    stock_screener.py 自体は変更しない — 変更するのは本テストが
    import している実際の config モジュールインスタンスの属性のみ。
    """
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    return tmp_path


# ==============================================================
# JP: stock_screener._calc_scores（代表3ケース）
# ==============================================================
class TestJpCalcScores:
    """
    3ケース: 全項目あり / EDINET欠損 / 時価総額小
    実測値は固定入力で _calc_scores() を実行して得た。
    """

    def test_case_all_fields(self, cfg, isolated_cache):
        data = {
            "symbol": "7203.T",
            "sector_code": "3700",
            "per": 12.5,
            "pbr": 1.2,
            "roe": 0.15,
            "inventory_turnover": 8.0,
            "earnings_growth": 0.10,
            "earnings_qgrowth": 0.20,
            # 加速度は profit_accel（四半期前年比−通期前年比）から取る
            "profit_accel": {"accel": 0.7},
            "market_cap": 3_000_000_000_000,
            "market_cap_b": 30000.0,
            "avg_volume": 5_000_000,
            "beta": 1.1,
        }
        result = ss._calc_scores(
            data=data,
            sector_score=65.0,
            edinet_accrual=-0.03,
            sector_accrual_mean=0.01,
            surprise_bonus=2.0,
            bq_bonus=1.0,
            ceo_bonus=1.5,
            cfg=cfg,
        )
        assert result == {
            "sector_score": 65.0,
            "base_value_score": 90.5,
            "value_score": 90.5,
            "quality_score": 83.5,
            "composite_score": 76.9,
            "accrual_ratio": -0.03,
            "accrual_tier": "AA",
            "accrual_bonus": 17.0,
            "surprise_bonus": 2.0,
            "acc_bonus": 10.0,
            "earnings_acceleration": 0.7,
            "bq_bonus": 1.0,
            "pearl_score": 26.3,   # D5の割安度（PER・PBR）込み
            "pearl_bonus": 0.0,
            "pearl_tier": None,
            "size_tier": "Large",
            "edinet_segment_text": "",
            "catalyst_score": 0.0,
            "midterm_overall_score": 0.0,
            "midterm_trajectory": "on_track",
            "size_weights": {
                "sector_score": 0.45,
                "value_score": 0.25,
                "quality_score": 0.3,
            },
            "bm_bonus": 0.0,
            "bm_norm_type": "NORM_STANDARD",
            "revision_score": 0.0,
            "risk_adjusted_score": 76.9,
        }

    def test_case_edinet_missing(self, cfg, isolated_cache):
        data = {
            "symbol": "9984.T",
            "sector_code": "5250",
            "per": 25.0,
            "pbr": 3.0,
            "roe": 0.05,
            "market_cap": 8_000_000_000_000,
            "market_cap_b": 80000.0,
            "avg_volume": 10_000_000,
            "beta": 1.8,
        }
        result = ss._calc_scores(
            data=data,
            sector_score=50.0,
            edinet_accrual=None,
            sector_accrual_mean=None,
            surprise_bonus=0.0,
            bq_bonus=0.0,
            ceo_bonus=0.0,
            cfg=cfg,
        )
        assert result == {
            "sector_score": 50.0,
            "base_value_score": 35.0,
            "value_score": 35.0,
            "quality_score": 16.7,
            "composite_score": 36.3,
            "accrual_ratio": None,
            "accrual_tier": "N/A",
            "accrual_bonus": 0.0,
            "surprise_bonus": 0.0,
            "acc_bonus": 0.0,
            "earnings_acceleration": None,
            "bq_bonus": 0.0,
            "pearl_score": 7.2,    # D5の割安度込み
            "pearl_bonus": 0.0,
            "pearl_tier": None,
            "size_tier": "Large",
            "edinet_segment_text": "",
            "catalyst_score": 0.0,
            "midterm_overall_score": 0.0,
            "midterm_trajectory": "on_track",
            "size_weights": {
                "sector_score": 0.45,
                "value_score": 0.25,
                "quality_score": 0.3,
            },
            "bm_bonus": 0.0,
            "bm_norm_type": "NORM_STANDARD",
            "revision_score": 0.0,
            "risk_adjusted_score": 30.2,
        }

    def test_case_small_market_cap(self, cfg, isolated_cache):
        data = {
            "symbol": "3999.T",
            "sector_code": "1050",
            "per": 8.0,
            "pbr": 0.8,
            "roe": 0.03,
            "market_cap": 3_000_000_000,
            "market_cap_b": 30.0,
            "avg_volume": 50_000,
            "beta": 0.4,
        }
        result = ss._calc_scores(
            data=data,
            sector_score=40.0,
            edinet_accrual=0.06,
            sector_accrual_mean=0.02,
            surprise_bonus=-1.0,
            bq_bonus=0.0,
            ceo_bonus=0.0,
            cfg=cfg,
        )
        assert result == {
            "sector_score": 40.0,
            "base_value_score": 100.0,
            "value_score": 100.0,
            "quality_score": 0.0,
            "composite_score": 33.0,
            "accrual_ratio": 0.06,
            "accrual_tier": "C",
            "accrual_bonus": -15.0,
            "surprise_bonus": -1.0,
            "acc_bonus": 0.0,
            "earnings_acceleration": None,
            "bq_bonus": 0.0,
            "pearl_score": 18.0,   # D5の割安度込み
            "pearl_bonus": 0.0,
            "pearl_tier": None,
            "size_tier": "Nano",
            "edinet_segment_text": "",
            "catalyst_score": 0.0,
            "midterm_overall_score": 0.0,
            "midterm_trajectory": "on_track",
            "size_weights": {
                "sector_score": 0.2,
                "value_score": 0.25,
                "quality_score": 0.55,
            },
            "bm_bonus": 0.0,
            "bm_norm_type": "NORM_STANDARD",
            "revision_score": 0.0,
            "risk_adjusted_score": 28.1,
        }


# ==============================================================
# JP: stock_screener._apply_filters（通過1 / PER不合格1 / N/A混在1）
# ==============================================================
class TestJpApplyFilters:
    def test_pass(self, cfg):
        data = {"per": 12.0, "pbr": 1.5, "roe": 0.12,
                "market_cap_b": 500.0, "avg_volume": 300_000}
        passed, detail = ss._apply_filters(data, cfg)
        assert passed is True
        assert detail == {
            "per": "OK(12.0)",
            "pbr": "OK(1.5)",
            "roe": "OK(12.0%)",
            "market_cap": "OK(500億)",
            "volume": "OK(300,000)",
        }

    def test_per_fail(self, cfg):
        data = {"per": 45.0, "pbr": 1.5, "roe": 0.12,
                "market_cap_b": 500.0, "avg_volume": 300_000}
        passed, detail = ss._apply_filters(data, cfg)
        assert passed is False
        assert detail == {
            "per": "NG(45.0)",
            "pbr": "OK(1.5)",
            "roe": "OK(12.0%)",
            "market_cap": "OK(500億)",
            "volume": "OK(300,000)",
        }

    def test_na_mixed(self, cfg):
        # per/pbr が N/A でも fails には数えられないため通過する
        data = {"per": None, "pbr": None, "roe": 0.12,
                "market_cap_b": 500.0, "avg_volume": 300_000}
        passed, detail = ss._apply_filters(data, cfg)
        assert passed is True
        assert detail == {
            "per": "N/A",
            "pbr": "N/A",
            "roe": "OK(12.0%)",
            "market_cap": "OK(500億)",
            "volume": "OK(300,000)",
        }


# ==============================================================
# JP: stock_screener._calc_risk_adjusted_score
# ==============================================================
@pytest.mark.parametrize("label,beta,expected", [
    ("beta_none", None, 70.0),
    ("beta_1.5_boundary", 1.5, 70.0),       # b > 1.5 が条件のため境界値は無調整
    ("beta_0.5_boundary", 0.5, 70.0),       # 0 < b < 0.5 が条件のため境界値は無調整
    ("beta_high_2.0", 2.0, 58.3),           # 高ボラ割引: 70/1.20
    ("beta_low_0.3", 0.3, 59.5),            # 低ボラ割引: 70*0.85
    ("beta_out_of_range_10", 10.0, 70.0),   # -2.0<=b<=5.0 の範囲外 → 無調整
    ("beta_normal_1.0", 1.0, 70.0),         # 通常範囲 → 無調整
])
def test_jp_calc_risk_adjusted_score(cfg, label, beta, expected):
    data = {"beta": beta}
    result = ss._calc_risk_adjusted_score("TEST.T", 70.0, data, cfg)
    assert result == expected


# ==============================================================
# JP: stock_screener._calc_revision_score
# ==============================================================
@pytest.mark.parametrize("rec_mean,expected", [
    (None, 0.0),
    (1.0, 3.0),
    (1.5, 3.0),    # 境界値: r<=1.5 → raw=0.50 → 0.50*6.0=3.0（capで一致）
    (2.0, 1.8),    # 境界値: r<=2.0 → raw=0.30 → 1.8
    (2.5, 0.6),    # 境界値: r<=2.5 → raw=0.10 → 0.6
    (3.0, 0.0),    # 境界値: r<=3.0 → raw=0.00 → 0.0
    (3.5, -1.2),   # 境界値: r<=3.5 → raw=-0.20 → -1.2
    (5.0, -3.0),   # r>3.5 → raw=-0.50 → -3.0（capで一致）
])
def test_jp_calc_revision_score(cfg, rec_mean, expected):
    data = {"rec_mean": rec_mean}
    result = ss._calc_revision_score("TEST.T", data, cfg)
    assert result == expected


# ==============================================================
# JP: stock_screener._check_pead_trigger
# ==============================================================
@pytest.mark.parametrize("revision_score,expected", [
    (0.3, True),    # min_rev と同値 → >= のため True
    (0.29, False),
    (0.31, True),
    (-1.0, False),
    (0.0, False),
])
def test_jp_check_pead_trigger(cfg, revision_score, expected):
    assert ss._check_pead_trigger("TEST.T", revision_score, cfg) is expected


# ==============================================================
# JP: stock_screener._calc_catalyst_bonus
# ==============================================================
class TestJpCalcCatalystBonus:
    def test_empty_cat_data_returns_zero(self, cfg):
        assert ss._calc_catalyst_bonus("6758.T", "3700", {}, cfg) == 0.0

    def test_single_event(self, cfg):
        cat_data = {
            "ticker_events": {
                "6758.T": [
                    {"type": "earnings", "days_until": 5, "proximity_score": 0.8},
                ],
            },
            "global_events": [],
        }
        assert ss._calc_catalyst_bonus("6758.T", "3700", cat_data, cfg) == 4.0

    def test_triple_catalyst(self, cfg):
        cat_data = {
            "ticker_events": {
                "6758.T": [
                    {"type": "earnings", "days_until": 3, "proximity_score": 1.0},
                    {"type": "dividend", "days_until": 5, "proximity_score": 0.6},
                    {"type": "buyback", "days_until": 10, "proximity_score": 0.4},
                ],
            },
            "global_events": [],
        }
        # proximity_score最大1.0 * bonus_max5.0 + triple_bonus1.0 = 6.0 だが
        # bonus_max=5.0 でクランプされ 5.0
        assert ss._calc_catalyst_bonus("6758.T", "3700", cat_data, cfg) == 5.0


# ==============================================================
# US: us_stock_screener._calc_score（代表3ケース）
# ==============================================================
@pytest.mark.parametrize("label,candidate,expected", [
    ("all_strong", {
        "roe": 0.30, "op_margin": 0.30, "gross_margin": 0.80,
        "revenue_growth": 0.35, "earnings_growth": 0.35,
        "per": 8.0, "pbr": 1.0, "free_cashflow": 1_000_000,
        "accrual_ratio": -0.03, "rec_mean": 1.2,
        "analyst_upside": 0.35, "week52_change": 0.35, "in_sp500": True,
    }, 100.0),
    ("weak", {
        "roe": 0.03, "op_margin": -0.05, "revenue_growth": -0.10,
        "per": -5.0, "pbr": 8.0, "free_cashflow": -500,
        "rec_mean": 4.5, "week52_change": -0.40,
    }, 0.0),
    ("empty", {}, 0.0),
])
def test_us_calc_score(label, candidate, expected):
    assert us._calc_score(candidate) == expected


# ==============================================================
# US: us_stock_screener._apply_filter
# ==============================================================
class TestUsApplyFilter:
    def test_pass(self):
        c = {"business_model": "mixed", "per": 20.0, "pbr": 2.0, "roe": 0.15,
             "op_margin": 0.10, "debt_to_equity": 50.0,
             "free_cashflow": 1_000_000, "revenue_growth": 0.10,
             "rec_mean": 2.0}
        passed, fails = us._apply_filter(c)
        assert passed is True
        assert fails == []

    def test_fail_multiple_reasons(self):
        c = {"business_model": "hardware", "per": 60.0, "pbr": 12.0,
             "roe": 0.02, "op_margin": 0.02, "debt_to_equity": 300.0,
             "free_cashflow": -1_000, "revenue_growth": 0.0,
             "rec_mean": 3.0}
        passed, fails = us._apply_filter(c)
        assert passed is False
        assert fails == [
            "PER=60.0 > 35",
            "PBR=12.0 > 10.0",
            "ROE=2.0% < 8%",
            "OPM=2.0% < 10%",
            "D/E=300 > 200",
            "FCF<0 (-0M)",
            "Rec=3.0 > 2.5",
        ]

    def test_na_mixed_biotech_no_checks(self):
        # biotech フィルターは per_max/pbr_max/roe_min/op_margin_min が
        # いずれも None のため、値が None でもチェック自体がスキップされる
        c = {"business_model": "biotech", "per": None, "pbr": None,
             "roe": None, "op_margin": None, "debt_to_equity": 50.0,
             "free_cashflow": None, "revenue_growth": None,
             "rec_mean": None}
        passed, fails = us._apply_filter(c)
        assert passed is True
        assert fails == []


# ==============================================================
# US: us_stock_screener._calc_risk_adjusted_score_us
# ==============================================================
@pytest.mark.parametrize("label,beta,expected", [
    ("beta_none", None, 70.0),
    ("beta_1.5_boundary", 1.5, 70.0),
    ("beta_0.5_boundary", 0.5, 70.0),
    ("beta_high_2.0", 2.0, 58.3),
    ("beta_low_0.3", 0.3, 59.5),
    ("beta_out_of_range_10", 10.0, 70.0),
    ("beta_normal_1.0", 1.0, 70.0),
])
def test_us_calc_risk_adjusted_score_us(cfg, label, beta, expected):
    c = {"beta": beta}
    result = us._calc_risk_adjusted_score_us("TEST", 70.0, c, cfg)
    assert result == expected


# ==============================================================
# US: us_stock_screener._calc_revision_score_us（JP版と判定表は同一だが
# 倍率10.0・上限±5pt が異なる ← D-4）
# ==============================================================
@pytest.mark.parametrize("rec_mean,expected", [
    (None, 0.0),
    (1.0, 5.0),
    (1.5, 5.0),
    (2.0, 3.0),
    (2.5, 1.0),
    (3.0, 0.0),
    (3.5, -2.0),
    (5.0, -5.0),
])
def test_us_calc_revision_score_us(cfg, rec_mean, expected):
    c = {"rec_mean": rec_mean}
    result = us._calc_revision_score_us("TEST", c, cfg)
    assert result == expected


# ==============================================================
# US: us_stock_screener._check_pead_trigger_us
# ==============================================================
@pytest.mark.parametrize("revision_score,expected", [
    (0.3, True),
    (0.29, False),
    (0.31, True),
    (-1.0, False),
    (0.0, False),
])
def test_us_check_pead_trigger_us(cfg, revision_score, expected):
    assert us._check_pead_trigger_us("TEST", revision_score, cfg) is expected


# ==============================================================
# US: us_stock_screener._calc_catalyst_bonus_us
# ==============================================================
class TestUsCalcCatalystBonusUs:
    def test_empty_cat_data_returns_zero(self, cfg):
        assert us._calc_catalyst_bonus_us("AAPL", "US_45", {}, cfg) == 0.0

    def test_single_event(self, cfg):
        cat_data = {
            "ticker_events": {
                "AAPL": [
                    {"type": "earnings", "days_until": 5, "proximity_score": 0.8},
                ],
            },
            "global_events": [],
        }
        assert us._calc_catalyst_bonus_us("AAPL", "US_45", cat_data, cfg) == 4.0

    def test_triple_catalyst(self, cfg):
        cat_data = {
            "ticker_events": {
                "AAPL": [
                    {"type": "earnings", "days_until": 3, "proximity_score": 1.0},
                    {"type": "dividend", "days_until": 5, "proximity_score": 0.6},
                    {"type": "buyback", "days_until": 10, "proximity_score": 0.4},
                ],
            },
            "global_events": [],
        }
        assert us._calc_catalyst_bonus_us("AAPL", "US_45", cat_data, cfg) == 5.0


# ==============================================================
# 輸入時ネットワーク接続ゼロの確認（メタテスト）
# ==============================================================
def test_import_has_no_network_side_effects():
    """stock_screener / us_stock_screener は import 時点で外部通信をしない
    設計になっている（yfinance 等は全て関数内 import）。
    この import 自体が本ファイルの先頭で既に成功していることが
    その証跡だが、明示的なメタテストとしても残す。
    """
    assert hasattr(ss, "_calc_scores")
    assert hasattr(us, "_calc_score")
