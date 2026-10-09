"""price_universe の純粋ロジック（ユニバース除外・チャンク・MultiIndex・取得率・dead管理）"""
from datetime import date, datetime, timezone

import pandas as pd

import price_universe as pu


def test_jp_ticker_conversion():
    assert pu.jp_ticker_from_sec_code("78370") == "7837.T"
    assert pu.jp_ticker_from_sec_code("271A0") == "271A.T"
    assert pu.jp_ticker_from_sec_code("271A") == "271A.T"
    assert pu.jp_ticker_from_sec_code("") is None
    assert pu.jp_ticker_from_sec_code(None) is None
    assert pu.jp_ticker_from_sec_code("12") is None
    assert pu.jp_ticker_from_sec_code("ab!d0") is None


def test_jp_universe_dedup_sorted():
    assert pu.build_jp_universe(["78370", "78370", "13010", None, ""]) == ["1301.T", "7837.T"]


def _rec(ticker, exchange="Nasdaq", name="Acme Corp"):
    return {"ticker": ticker, "exchange": exchange, "name": name, "cik": 1}


def test_us_universe_exclusions():
    recs = [
        _rec("AAPL", "Nasdaq", "Apple Inc."),
        _rec("BRK-B", "NYSE", "Berkshire Hathaway"),
        _rec("PFBC", "Nasdaq", "Preferred Bank"),           # 名前に Preferred を含む普通株は残す
        _rec("JPM-PC", "NYSE", "JPMorgan Chase"),           # 優先株
        _rec("ABCDW", "Nasdaq", "Foo Warrants"),            # 5文字目 W = ワラント
        _rec("XYZ-WS", "NYSE", "Xyz"),
        _rec("KCAC-UN", "NYSE", "K Acq"),
        _rec("WPAC-RI", "Nasdaq", "White Pearl"),
        _rec("SPY", "NYSE", "SPDR S&P 500 ETF Trust"),
        _rec("NUV", "NYSE", "NUVEEN MUNICIPAL VALUE FUND INC"),
        _rec("TAVI", "Nasdaq", "Tavia Acquisition Corp."),
        _rec("OTCX", "OTC", "Otc Co"),
        _rec("CBOX", "CBOE", "Cboe Etf"),
        _rec("", "NYSE", "empty"),
    ]
    keep, counts = pu.filter_us_universe(recs)
    assert keep == ["AAPL", "BRK-B", "PFBC"]
    assert counts["kept"] == 3 and counts["total"] == len(recs)
    assert counts["other_exchange"] == 3 and counts["derivative"] == 5


def test_parse_sec_tickers():
    payload = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "A", "AA", "NYSE"]]}
    assert pu.parse_sec_tickers(payload) == [{"cik": 1, "name": "A", "ticker": "AA", "exchange": "NYSE"}]


def test_chunked():
    assert list(pu.chunked(list(range(7)), 3)) == [[0, 1, 2], [3, 4, 5], [6]]
    assert list(pu.chunked([], 3)) == []
    assert list(pu.chunked([1, 2], 0)) == [[1], [2]]


def _multi(tickers, dates):
    idx = pd.to_datetime(dates)
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(1.0, index=idx, columns=cols)
    for i, t in enumerate(tickers):
        df[(t, "Close")] = 100.0 + i
    return df


def test_extract_frames_reads_ticker_first():
    """group_by="ticker" は level0=ticker。data[ticker]["Close"] の順で読む。"""
    data = _multi(["AAA", "BBB"], ["2026-10-08", "2026-10-09"])
    frames = pu.extract_frames(data, ["AAA", "BBB", "CCC"])
    assert set(frames) == {"AAA", "BBB"}           # 返らなかった CCC は含めない
    assert list(frames["BBB"]["Close"]) == [101.0, 101.0]


def test_extract_frames_drops_all_nan_ticker():
    data = _multi(["AAA", "BBB"], ["2026-10-09"])
    data[("BBB", "Close")] = float("nan")
    assert set(pu.extract_frames(data, ["AAA", "BBB"])) == {"AAA"}


def test_extract_frames_flat_and_empty():
    flat = pd.DataFrame({"Close": [1.0]}, index=pd.to_datetime(["2026-10-09"]))
    assert set(pu.extract_frames(flat, ["AAA"])) == {"AAA"}
    assert pu.extract_frames(flat, ["AAA", "BBB"]) == {}
    assert pu.extract_frames(pd.DataFrame(), ["AAA"]) == {}
    assert pu.extract_frames(None, ["AAA"]) == {}


def test_rows_skip_existing_future_and_nan_close():
    data = _multi(["AAA"], ["2026-10-07", "2026-10-08", "2026-10-09"])
    data.loc["2026-10-07", ("AAA", "Close")] = float("nan")
    frames = pu.extract_frames(data, ["AAA"])
    rows = pu.rows_from_frames(
        frames, existing={("AAA", "2026-10-08")}, cutoff=date(2026, 10, 10), now_iso="t")
    assert [r["trade_date"] for r in rows] == ["2026-10-09"]
    assert set(rows[0]) == {"trade_date", "ticker", "open", "high", "low", "close", "volume",
                            "source", "ingested_at"}
    cut = pu.rows_from_frames(frames, set(), cutoff=date(2026, 10, 9), now_iso="t")
    assert [r["trade_date"] for r in cut] == ["2026-10-08"]       # cutoff 以降は入れない


def test_market_cutoff_excludes_session_in_progress():
    # JST 2026-10-09 14:00（立会い中）→ 当日の足は入れない
    assert pu.market_cutoff_date("jp", datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)) == date(2026, 10, 9)
    # JST 2026-10-09 16:47（閉場後）→ 当日の足を入れてよい
    assert pu.market_cutoff_date("jp", datetime(2026, 10, 9, 7, 47, tzinfo=timezone.utc)) == date(2026, 10, 10)
    # NY 2026-10-09 09:00（寄り前）→ 当日は含めない
    assert pu.market_cutoff_date("us", datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)) == date(2026, 10, 9)
    # JST 07:47 = NY 前日 18:47（閉場後）→ 前日の足を入れてよい
    assert pu.market_cutoff_date("us", datetime(2026, 10, 8, 22, 47, tzinfo=timezone.utc)) == date(2026, 10, 9)


def test_coverage_alert_below_threshold():
    assert pu.coverage(100, 90, 0.90)["status"] == "success"
    assert pu.coverage(100, 89, 0.90)["status"] == "alert"
    assert pu.coverage(0, 0, 0.90)["status"] == "alert"           # 対象0を success にしない
    assert pu.coverage(100, 89, 0.90)["missed"] == 11


def test_dead_after_consecutive_misses_and_recovery():
    dead: dict = {}
    for d in ("2026-10-01", "2026-10-02"):
        pu.update_dead_state(dead, [], ["XXX"], d, after=3, retry_days=30)
    assert not dead["XXX"]["dead"]
    pu.update_dead_state(dead, [], ["XXX"], "2026-10-02", after=3)   # 同日の再実行は数えない
    assert dead["XXX"]["misses"] == 2
    pu.update_dead_state(dead, [], ["XXX"], "2026-10-03", after=3, retry_days=30)
    assert dead["XXX"]["dead"] and dead["XXX"]["next_retry"] == "2026-11-02"
    targets, skipped = pu.select_targets(["XXX", "YYY"], dead, "2026-10-10")
    assert targets == ["YYY"] and skipped == 1
    targets, skipped = pu.select_targets(["XXX", "YYY"], dead, "2026-11-02")
    assert targets == ["XXX", "YYY"] and skipped == 0               # 再確認日が来たら対象に戻す
    pu.update_dead_state(dead, ["XXX"], [], "2026-11-02")
    assert "XXX" not in dead                                         # 取れたら記録から消す


class _FakeYF:
    """全チャンクが空を返す yfinance（429 全滅の再現）。呼び出し回数を数える。"""
    def __init__(self):
        self.calls = 0

    def download(self, chunk, **kw):
        self.calls += 1
        return pd.DataFrame()


def test_run_market_aborts_after_consecutive_failed_chunks(monkeypatch, tmp_path):
    """チャンクが連続で失敗したら打ち切り、alert にする。未取得を dead と記録しない。"""
    class Cfg:
        DRIVE_BASE = tmp_path
    monkeypatch.setattr(pu, "load_us_universe", lambda cfg, today: ([f"T{i}" for i in range(50)], {}))
    monkeypatch.setattr(pu, "CHUNK_SIZE", 5)
    monkeypatch.setattr(pu, "CHUNK_PAUSE_SEC", 0)
    monkeypatch.setattr(pu, "CHUNK_RETRY_WAIT_SEC", 0)
    yf = _FakeYF()
    info = pu.run_market("us", Cfg, None, dry_run=True, yf=yf)
    assert info["status"] == "alert" and info["aborted"]
    assert info["hit"] == 0 and info["failed_chunks"] == pu.MAX_FAILED_CHUNKS_IN_ROW
    assert yf.calls == pu.MAX_FAILED_CHUNKS_IN_ROW * 2        # 各チャンク1回だけ再試行


def test_record_external_flows_into_daily_ledger(tmp_path):
    """別ワークフローの記録は、本体が日次台帳を作り直しても残る。"""
    import module_ledger as ml

    class Cfg:
        LOG_DIR = tmp_path
        DAILY_MODULES: list = []
    ml.record_external(Cfg, "price_universe_jp", "alert", "取得率低下", date_str="2026-10-10")
    led = ml.build_daily_ledger(Cfg, [], date_str="2026-10-10")
    assert led["modules"]["price_universe_jp"]["status"] == "alert"
    assert led["stats"]["warn"] == 1
