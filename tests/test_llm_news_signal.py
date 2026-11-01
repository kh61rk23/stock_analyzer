"""S8 週次結果 → invest.llm_news_* の SQL 生成（純粋関数）の単体テスト。"""
from datetime import datetime, timezone

import pytest

import invest_bq_schema as sch
from tools import llm_news_signal as ln


def sig(**kw):
    base = {"ticker": "ORN", "direction": "bull", "strength": 2, "thesis": "受注残の伸び",
            "source_url": "https://example.com/a", "expire_date": "2026-11-27"}
    base.update(kw)
    return base


def data(*signals, as_of="2026-10-30"):
    return {"as_of": as_of, "signals": list(signals)}


def test_market_is_derived_from_ticker_when_missing():
    _, rows = ln.validate(data(sig(ticker="7011.T"), sig(ticker="GH")))
    assert [r["market"] for r in rows] == ["JP", "US"]


@pytest.mark.parametrize("bad", [
    sig(direction="long"), sig(strength=4), sig(strength="2"), sig(strength=True), sig(source_url="ftp://x"),
    sig(expire_date="2026-12-01"), sig(expire_date="2026-10-29"), sig(thesis=""), sig(market="EU"),
])
def test_invalid_signal_is_rejected(bad):
    with pytest.raises(ValueError):
        ln.validate(data(bad))


def test_duplicate_ticker_and_bad_shapes_are_rejected():
    with pytest.raises(ValueError):
        ln.validate(data(sig(), sig()))
    with pytest.raises(ValueError):
        ln.validate({"as_of": "2026-10-30", "signals": None})
    with pytest.raises(ValueError):
        ln.validate({"as_of": "10/30", "signals": []})


def test_script_replaces_same_as_of_in_one_transaction():
    now = datetime(2026, 11, 1, 1, 2, 3, tzinfo=timezone.utc)
    s = ln.build_script(data(sig(), sig(ticker="AES", strength=1)), now)
    lines = s.splitlines()
    assert lines[0] == "BEGIN TRANSACTION;" and lines[-1] == "COMMIT TRANSACTION;"
    assert s.count('DELETE FROM `stock-analyzer-edinet.invest.llm_news_runs` WHERE as_of = DATE "2026-10-30";') == 1
    assert s.count('DELETE FROM `stock-analyzer-edinet.invest.llm_news_signals` WHERE as_of = DATE "2026-10-30";') == 1
    assert s.index("DELETE FROM `stock-analyzer-edinet.invest.llm_news_signals`") < s.index("INSERT INTO `stock-analyzer-edinet.invest.llm_news_signals`")
    assert 'VALUES (DATE "2026-10-30", 2, TIMESTAMP "2026-11-01 01:02:03 UTC"' in s
    assert s.count('TIMESTAMP "2026-11-01 01:02:03 UTC")') >= 2


def test_empty_week_still_records_a_run_and_inserts_no_signals():
    s = ln.build_script(data(), datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert 'VALUES (DATE "2026-10-30", 0,' in s and "該当なし" in s
    assert "INSERT INTO `stock-analyzer-edinet.invest.llm_news_signals`" not in s


def test_string_literals_escape_quotes_backslashes_and_newlines():
    assert ln.lit('a"b\\c\nd') == '"a\\"b\\\\c\\nd"'
    assert ln.lit(None) == "NULL" and ln.lit(3) == "3"
    s = ln.build_script(data(sig(thesis='社長が"退任"\n後任は未定')), datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert '社長が\\"退任\\"\\n後任は未定' in s


def test_schema_declares_news_tables_and_view():
    ddl = "\n".join(sch.all_ddl("p"))
    for name in (sch.NEWS_RUNS_TABLE, sch.NEWS_SIGNALS_TABLE, sch.NEWS_LATEST_VIEW):
        assert name in sch.ORDER and f"`p.invest.{name}`" in ddl
