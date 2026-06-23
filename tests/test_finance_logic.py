# finance_logic.py — 純粋計算・フォーマット関数（標準ライブラリのみ）
from conftest import load_module

fl = load_module("finance_logic")


class TestNormalizeTicker:
    def test_jp_digits_get_t_suffix(self):
        assert fl.normalize_ticker("7203") == "7203.T"

    def test_us_passthrough_upper(self):
        assert fl.normalize_ticker("aapl") == "AAPL"

    def test_strip_whitespace(self):
        assert fl.normalize_ticker(" 7203 ") == "7203.T"


class TestValidateTicker:
    def test_jp_4digit(self):
        ok, is_jp, norm, err = fl.validate_ticker("7203")
        assert ok and is_jp and norm == "7203.T" and err == ""

    def test_jp_with_t(self):
        ok, is_jp, norm, _ = fl.validate_ticker("7203.T")
        assert ok and is_jp and norm == "7203.T"

    def test_jp_wrong_digits(self):
        ok, _, _, err = fl.validate_ticker("720")
        assert not ok and "4桁" in err

    def test_us_symbol(self):
        ok, is_jp, norm, _ = fl.validate_ticker("BRK.B")
        assert ok and not is_jp and norm == "BRK.B"

    def test_empty(self):
        ok, _, _, err = fl.validate_ticker("")
        assert not ok and err

    def test_too_long(self):
        ok, _, _, err = fl.validate_ticker("A" * 17)
        assert not ok and "長すぎ" in err


class TestIsJapanStock:
    def test_t_suffix(self):
        assert fl.is_japan_stock("7203.T")

    def test_digits(self):
        assert fl.is_japan_stock("7203")

    def test_us(self):
        assert not fl.is_japan_stock("AAPL")


class TestFmtLarge:
    def test_trillion(self):
        assert fl.fmt_large(2.5e12) == "2.50 兆"

    def test_oku(self):
        assert fl.fmt_large(3.0e8) == "3.0 億"

    def test_none(self):
        assert fl.fmt_large(None) == "―"

    def test_garbage(self):
        assert fl.fmt_large("abc") == "―"
