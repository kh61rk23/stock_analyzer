# ==============================================================
# tests/test_jp_analyst_revision.py — U-4 JP rec_mean 取得（F-1）
# ==============================================================
# 目的（要件定義書_JP-US統合_20260726.md §6 U-4 / J-01=a 承認済み）:
#   stock_screener._fetch_ticker_data() が yfinance .info から
#   rec_mean / rec_key / target_price / analyst_upside を取得することを
#   検証する（R4-1〜R4-3）。
#   また、_calc_revision_score() の rec_mean 指定時の境界値を凍結する
#   （R4-4 / R4-5）。
#
#   期待値はすべて本ファイルの assert を実際に実行して得た実測値であり、
#   （tests/test_screener_characterization.py の
#    TestJp...test_jp_calc_revision_score と同じ実装・同じ cfg デフォルトを
#    通るため、そちらの既存実測値 rec_mean=1.0→3.0 / 3.0→0.0 / 5.0→-3.0 とも
#    一致する）。
#
#   yfinance はネットワーク接続を避けるため、関数内 import 先である
#   sys.modules["yfinance"] を monkeypatch でフェイクモジュールに差し替える
#   （stock_screener.py 自体は変更しない）。
# ==============================================================
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import stock_screener as ss  # noqa: E402


class _FakeTicker:
    def __init__(self, info):
        self.info = info


def _install_fake_yfinance(monkeypatch, info):
    """sys.modules["yfinance"] を、渡した info を返すフェイクに差し替える。"""
    fake_yf = SimpleNamespace(Ticker=lambda ticker: _FakeTicker(info))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)


# ==============================================================
# R4-1 / R4-2 / R4-3: _fetch_ticker_data の返り値に4キーが追加されること
# ==============================================================
class TestFetchTickerDataAnalystFields:
    def test_all_fields_present_and_cleaned(self, monkeypatch):
        info = {
            "regularMarketPrice": 100.0,
            "currentPrice": 100.0,
            "trailingPE": 12.0,
            "priceToBook": 1.2,
            "returnOnEquity": 0.10,
            "marketCap": 1_000_000_000,
            "longName": "Test Corp",
            "averageVolume": 500_000,
            "recommendationMean": 2.0,
            "recommendationKey": "buy",
            "targetMeanPrice": 120.0,
            "numberOfAnalystOpinions": 10,
        }
        _install_fake_yfinance(monkeypatch, info)

        data = ss._fetch_ticker_data("TEST.T")

        assert data is not None
        # R4-1: 4キーが存在する
        assert "rec_mean" in data
        assert "rec_key" in data
        assert "target_price" in data
        assert "analyst_upside" in data
        # 値そのもの
        assert data["rec_mean"] == 2.0
        assert data["rec_key"] == "buy"
        assert data["target_price"] == 120.0
        assert data["analyst_upside"] == pytest.approx(0.2)
        # R4-2: analyst_count は numberOfAnalystOpinions 由来・重複取得なし
        assert data["analyst_count"] == 10

    def test_rec_mean_nan_cleaned_to_none(self, monkeypatch):
        # _clean() は NaN を None に変換する（R4-3: rec_mean は _clean を通す）
        info = {
            "regularMarketPrice": 50.0,
            "recommendationMean": float("nan"),
            "recommendationKey": "hold",
            "targetMeanPrice": None,
        }
        _install_fake_yfinance(monkeypatch, info)

        data = ss._fetch_ticker_data("TEST2.T")

        assert data is not None
        assert data["rec_mean"] is None
        assert data["rec_key"] == "hold"

    def test_rec_key_missing_defaults_to_empty_string(self, monkeypatch):
        # R4-1: rec_key 未取得時は ""
        info = {
            "regularMarketPrice": 50.0,
        }
        _install_fake_yfinance(monkeypatch, info)

        data = ss._fetch_ticker_data("TEST3.T")

        assert data is not None
        assert data["rec_key"] == ""
        assert isinstance(data["rec_key"], str)
        # rec_key は _clean を通らない（str のまま保持される・R4-3）
        assert data["rec_mean"] is None
        assert data["target_price"] is None
        assert data["analyst_upside"] is None


# ==============================================================
# R4-1: analyst_upside の導出
# ==============================================================
class TestAnalystUpsideDerivation:
    def test_upside_positive_case(self, monkeypatch):
        # target_price=120, current_price=100 → (120-100)/100 = 0.2
        info = {
            "regularMarketPrice": 100.0,
            "targetMeanPrice": 120.0,
        }
        _install_fake_yfinance(monkeypatch, info)

        data = ss._fetch_ticker_data("TEST4.T")

        assert data["current_price"] == 100.0
        assert data["target_price"] == 120.0
        assert data["analyst_upside"] == pytest.approx(0.2)

    def test_upside_none_when_current_price_zero(self, monkeypatch):
        # regularMarketPrice は None ではないが 0（早期returnはNoneのみで判定）
        # currentPrice / previousClose も未設定のため current_price は 0 のまま
        info = {
            "regularMarketPrice": 0.0,
            "targetMeanPrice": 120.0,
        }
        _install_fake_yfinance(monkeypatch, info)

        data = ss._fetch_ticker_data("TEST5.T")

        assert data is not None
        assert data["current_price"] == 0.0
        assert data["analyst_upside"] is None

    def test_upside_none_when_target_price_missing(self, monkeypatch):
        info = {
            "regularMarketPrice": 100.0,
            # targetMeanPrice なし → None
        }
        _install_fake_yfinance(monkeypatch, info)

        data = ss._fetch_ticker_data("TEST6.T")

        assert data is not None
        assert data["target_price"] is None
        assert data["analyst_upside"] is None


# ==============================================================
# R4-4 / R4-5: _calc_revision_score 本体は不変。rec_mean 指定時の実測境界値。
# ==============================================================
# 実測手順: 本ファイルの pytest 実行によりハードコードされた expected と
# ss._calc_revision_score() の実行結果を突き合わせている（このテスト自体が
# 実測手段）。cfg=None のためデフォルト PEAD_TRIGGER_MIN_REVISION=0.3 が使われ、
# jp_max = 0.3*10.0=3.0 → min(3.0,3.0)=3.0 でクリップされる。
#   rec_mean=1.0 → r<=1.5 → raw=0.50 → 0.50*6.0=3.0 → cap 3.0 で一致 → +3.0
#   rec_mean=3.0 → r<=3.0 → raw=0.00 → 0.0
#   rec_mean=5.0 → r>3.5  → raw=-0.50 → -0.50*6.0=-3.0 → cap -3.0 で一致 → -3.0
# tests/test_screener_characterization.py の
# TestJp...test_jp_calc_revision_score と同一の実測値であることを確認済み。
@pytest.mark.parametrize("rec_mean,expected", [
    (1.0, 3.0),
    (3.0, 0.0),
    (5.0, -3.0),
])
def test_calc_revision_score_nonzero_within_cap(rec_mean, expected):
    data = {"rec_mean": rec_mean}
    result = ss._calc_revision_score("TEST.T", data, None)
    assert result == expected
    assert -3.0 <= result <= 3.0


def test_calc_revision_score_zero_when_rec_mean_absent():
    # rec_mean が data に無い場合は 0.0
    assert ss._calc_revision_score("TEST.T", {}, None) == 0.0
