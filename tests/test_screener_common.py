# ==============================================================
# tests/test_screener_common.py — U-3 screener_common.py 単体テスト
# ==============================================================
# 目的（要件定義書_JP-US統合_20260726.md §6-J2 U-3 / R3-4）:
#   screener_common.py の4公開API（calc_risk_adjusted_score /
#   calc_revision_score / check_pead_trigger / calc_catalyst_bonus）の
#   単体テストと、両スクリーナーの委譲先が同一値を返すことの確認。
#
#   期待値は tests/test_screener_characterization.py と同じ実測値を
#   踏襲している（境界値: beta=1.5/0.5、rec_mean=1.5/2.0/2.5/3.0/3.5）。
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import screener_common as sc  # noqa: E402
import stock_screener as ss   # noqa: E402
import us_stock_screener as us  # noqa: E402


class DummyCfg:
    """test_screener_characterization.py の DummyCfg と同一の最小ダミー cfg。"""
    RISK_ADJ_PENALTY_HIGH_VOL = 1.20
    RISK_ADJ_PENALTY_LOW_VOL = 0.85
    PEAD_TRIGGER_MIN_REVISION = 0.3
    ANALYST_REVISION_WINDOW_DAYS = 30
    CATALYST_BONUS_MAX = 5.0


@pytest.fixture
def cfg():
    return DummyCfg()


# ==============================================================
# calc_risk_adjusted_score
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
def test_calc_risk_adjusted_score(cfg, label, beta, expected):
    data = {"beta": beta}
    result = sc.calc_risk_adjusted_score("TEST", 70.0, data, cfg)
    assert result == expected


def test_calc_risk_adjusted_score_no_cfg_defaults():
    # cfg=None でも getattr のデフォルト値で動作すること
    data = {"beta": 2.0}
    result = sc.calc_risk_adjusted_score("TEST", 70.0, data, None)
    assert result == 58.3


def test_calc_risk_adjusted_score_exception_fallback():
    # data が dict でない等の例外時は composite_score をそのまま返す
    class Bad:
        def get(self, key):
            raise RuntimeError("boom")
    result = sc.calc_risk_adjusted_score("TEST", 70.0, Bad(), None)
    assert result == 70.0


# ==============================================================
# calc_revision_score — market="JP" / market="US"
# ==============================================================
@pytest.mark.parametrize("rec_mean,expected", [
    (None, 0.0),
    (1.0, 3.0),
    (1.5, 3.0),
    (2.0, 1.8),
    (2.5, 0.6),
    (3.0, 0.0),
    (3.5, -1.2),
    (5.0, -3.0),
])
def test_calc_revision_score_jp(cfg, rec_mean, expected):
    data = {"rec_mean": rec_mean}
    result = sc.calc_revision_score("TEST", data, "JP", cfg)
    assert result == expected


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
def test_calc_revision_score_us(cfg, rec_mean, expected):
    data = {"rec_mean": rec_mean}
    result = sc.calc_revision_score("TEST", data, "US", cfg)
    assert result == expected


def test_calc_revision_score_jp_us_caps_differ(cfg):
    # 同じ強いBuyシグナル（rec_mean=1.0）でも JP と US で cap が異なること
    data = {"rec_mean": 1.0}
    jp_result = sc.calc_revision_score("TEST", data, "JP", cfg)
    us_result = sc.calc_revision_score("TEST", data, "US", cfg)
    assert jp_result == 3.0
    assert us_result == 5.0
    assert jp_result != us_result


@pytest.mark.parametrize("market", [None, "", "EU", "jp", "us", "unknown"])
def test_calc_revision_score_invalid_market_falls_back_to_jp(cfg, market):
    data = {"rec_mean": 1.0}
    result = sc.calc_revision_score("TEST", data, market, cfg)
    expected_jp = sc.calc_revision_score("TEST", data, "JP", cfg)
    assert result == expected_jp == 3.0


def test_calc_revision_score_jp_cap_matches_pead_trigger_min_revision():
    # JP cap は min(PEAD_TRIGGER_MIN_REVISION * 10.0, 3.0) で決まる。
    # PEAD_TRIGGER_MIN_REVISION を小さくすると cap も連動して小さくなること
    # （現行 JP 実装 = stock_screener._calc_revision_score の再現確認）。
    class SmallCapCfg:
        PEAD_TRIGGER_MIN_REVISION = 0.2  # -> jp_max = 2.0（3.0未満なのでそのまま）

    data = {"rec_mean": 1.0}  # raw=0.50 -> raw*6.0=3.0 だが cap=2.0 でクランプ
    result = sc.calc_revision_score("TEST", data, "JP", SmallCapCfg())
    assert result == 2.0


# ==============================================================
# check_pead_trigger
# ==============================================================
@pytest.mark.parametrize("revision_score,expected", [
    (0.3, True),
    (0.29, False),
    (0.31, True),
    (-1.0, False),
    (0.0, False),
])
def test_check_pead_trigger(cfg, revision_score, expected):
    assert sc.check_pead_trigger("TEST", revision_score, cfg) is expected


# ==============================================================
# calc_catalyst_bonus
# ==============================================================
class TestCalcCatalystBonus:
    def test_empty_cat_data_returns_zero(self, cfg):
        assert sc.calc_catalyst_bonus("6758.T", "3700", {}, cfg) == 0.0

    def test_single_event(self, cfg):
        cat_data = {
            "ticker_events": {
                "6758.T": [
                    {"type": "earnings", "days_until": 5, "proximity_score": 0.8},
                ],
            },
            "global_events": [],
        }
        assert sc.calc_catalyst_bonus("6758.T", "3700", cat_data, cfg) == 4.0

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
        assert sc.calc_catalyst_bonus("6758.T", "3700", cat_data, cfg) == 5.0

    def test_us_style_gics_sector_code(self, cfg):
        cat_data = {
            "ticker_events": {
                "AAPL": [
                    {"type": "earnings", "days_until": 5, "proximity_score": 0.8},
                ],
            },
            "global_events": [],
        }
        assert sc.calc_catalyst_bonus("AAPL", "US_45", cat_data, cfg) == 4.0


# ==============================================================
# 委譲テスト: stock_screener / us_stock_screener の旧関数が
# screener_common の新関数と同一の値を返すこと
# ==============================================================
class TestDelegationJp:
    @pytest.mark.parametrize("beta", [None, 1.5, 0.5, 2.0, 0.3, 10.0, 1.0])
    def test_risk_adjusted_score_delegates(self, cfg, beta):
        data = {"beta": beta}
        old = ss._calc_risk_adjusted_score("TEST.T", 70.0, data, cfg)
        new = sc.calc_risk_adjusted_score("TEST.T", 70.0, data, cfg)
        assert old == new

    @pytest.mark.parametrize("rec_mean", [None, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 5.0])
    def test_revision_score_delegates(self, cfg, rec_mean):
        data = {"rec_mean": rec_mean}
        old = ss._calc_revision_score("TEST.T", data, cfg)
        new = sc.calc_revision_score("TEST.T", data, "JP", cfg)
        assert old == new

    @pytest.mark.parametrize("revision_score", [0.3, 0.29, 0.31, -1.0, 0.0])
    def test_pead_trigger_delegates(self, cfg, revision_score):
        old = ss._check_pead_trigger("TEST.T", revision_score, cfg)
        new = sc.check_pead_trigger("TEST.T", revision_score, cfg)
        assert old == new

    def test_catalyst_bonus_delegates(self, cfg):
        cat_data = {
            "ticker_events": {
                "6758.T": [
                    {"type": "earnings", "days_until": 5, "proximity_score": 0.8},
                ],
            },
            "global_events": [],
        }
        old = ss._calc_catalyst_bonus("6758.T", "3700", cat_data, cfg)
        new = sc.calc_catalyst_bonus("6758.T", "3700", cat_data, cfg)
        assert old == new


class TestDelegationUs:
    @pytest.mark.parametrize("beta", [None, 1.5, 0.5, 2.0, 0.3, 10.0, 1.0])
    def test_risk_adjusted_score_us_delegates(self, cfg, beta):
        c = {"beta": beta}
        old = us._calc_risk_adjusted_score_us("TEST", 70.0, c, cfg)
        new = sc.calc_risk_adjusted_score("TEST", 70.0, c, cfg)
        assert old == new

    @pytest.mark.parametrize("rec_mean", [None, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 5.0])
    def test_revision_score_us_delegates(self, cfg, rec_mean):
        c = {"rec_mean": rec_mean}
        old = us._calc_revision_score_us("TEST", c, cfg)
        new = sc.calc_revision_score("TEST", c, "US", cfg)
        assert old == new

    @pytest.mark.parametrize("revision_score", [0.3, 0.29, 0.31, -1.0, 0.0])
    def test_pead_trigger_us_delegates(self, cfg, revision_score):
        old = us._check_pead_trigger_us("TEST", revision_score, cfg)
        new = sc.check_pead_trigger("TEST", revision_score, cfg)
        assert old == new

    def test_catalyst_bonus_us_delegates(self, cfg):
        cat_data = {
            "ticker_events": {
                "AAPL": [
                    {"type": "earnings", "days_until": 5, "proximity_score": 0.8},
                ],
            },
            "global_events": [],
        }
        old = us._calc_catalyst_bonus_us("AAPL", "US_45", cat_data, cfg)
        new = sc.calc_catalyst_bonus("AAPL", "US_45", cat_data, cfg)
        assert old == new
