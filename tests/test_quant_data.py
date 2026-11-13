# ==============================================================
# tests/test_quant_data.py — quant/instruments.py・quant/data.py の検査
# ==============================================================
# 合成データだけで動かす。ネットワークに出る _download・_fetch_bis_zip は差し替える。
# ==============================================================
from __future__ import annotations

import io
import os
import time
import zipfile
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quant import data as D
from quant import instruments as I


def _cfg(tmp_path, **kw):
    return SimpleNamespace(DRIVE_BASE=str(tmp_path), **kw)


SMALL_UNIVERSE = {
    "fx": [{"symbol": "AAA", "name": "a", "base": "USD", "quote": "JPY", "cost_bps": 1.0}],
    "index": [{"symbol": "BBB", "name": "b", "exec": "ETF", "cost_bps": 2.0}],
    "options": [{"symbol": "BBB", "name": "b", "iv_symbol": "VIXX", "exec": "opt", "cost_bps": 0.0}],
}


def _small_cfg(tmp_path):
    return _cfg(tmp_path, QUANT_UNIVERSE=SMALL_UNIVERSE,
                QUANT_RATE_BIS_AREAS={"USD": "US", "JPY": "JP"},
                QUANT_RATE_FALLBACK={"USD": 0.04, "JPY": 0.007})


def _multi(frames: dict) -> pd.DataFrame:
    """group_by="ticker" の形（level0＝ticker・level1＝Open/Close）。"""
    parts = {}
    for t, s in frames.items():
        parts[(t, "Open")] = s
        parts[(t, "Close")] = s * 1.01
    return pd.DataFrame(parts)


# ── instruments ──────────────────────────────────────────────────────────

def test_instruments_load_fields():
    insts = I.load(None)
    fx = insts["USDJPY=X"]
    assert fx.funding == "fx" and fx.currency == "JPY" and (fx.base, fx.quote) == ("USD", "JPY")
    gld = insts["GLD"]
    assert gld.funding == "futures" and gld.currency == "USD" and "金先物" in gld.exec_hint
    assert insts["1321.T"].currency == "JPY"
    assert insts["1617.T"].funding == "margin_jp"
    assert insts["XLB"].funding == "margin_us"


def test_instruments_duplicate_symbol_keeps_first_and_by_class_has_both():
    insts = I.load(None)
    assert insts["SPY"].asset_class == "index"
    assert insts["SPY"].iv_symbol is None
    bc = I.by_class(None)
    assert any(i.symbol == "SPY" for i in bc["index"])
    opt_spy = [i for i in bc["options"] if i.symbol == "SPY"][0]
    assert opt_spy.asset_class == "options" and opt_spy.iv_symbol == "^VIX" and opt_spy.funding == "cash"


def test_all_symbols_includes_iv_and_is_unique():
    syms = I.all_symbols(None)
    assert "^VIX" in syms and "^VXN" in syms
    assert None not in syms
    assert len(syms) == len(set(syms))
    assert syms.count("SPY") == 1


def test_unknown_asset_class_funding_raises():
    cfg = SimpleNamespace(QUANT_UNIVERSE={"zzz": [{"symbol": "X"}]})
    with pytest.raises(KeyError):
        I.load(cfg)


# ── 形の正規化 ────────────────────────────────────────────────────────────

IDX = pd.bdate_range("2020-01-01", periods=10)


def test_extract_close_ticker_level0():
    raw = _multi({"A": pd.Series(range(10), index=IDX, dtype=float),
                  "B": pd.Series(range(10, 20), index=IDX, dtype=float)})
    out = D._extract_close(raw, ["A", "B"])
    assert list(out.columns) == ["A", "B"]
    assert out["B"].iloc[0] == pytest.approx(10 * 1.01)


def test_extract_close_skips_absent_symbol():
    raw = _multi({"A": pd.Series(1.0, index=IDX)})
    out = D._extract_close(raw, ["A", "Z"])
    assert list(out.columns) == ["A"]


def test_extract_close_single_layer_single_symbol():
    raw = pd.DataFrame({"Open": 1.0, "Close": np.arange(10.0)}, index=IDX)
    out = D._extract_close(raw, ["A"])
    assert list(out.columns) == ["A"] and out["A"].iloc[3] == 3.0


def test_extract_close_field_first_layout():
    raw = pd.DataFrame({("Close", "A"): np.arange(10.0), ("Open", "A"): 1.0}, index=IDX)
    out = D._extract_close(raw, ["A"])
    assert out["A"].iloc[5] == 5.0


def test_extract_close_empty_and_tz_normalized():
    assert D._extract_close(pd.DataFrame(), ["A"]).empty
    tz_idx = pd.date_range("2020-01-01 09:00", periods=3, freq="D", tz="Asia/Tokyo")
    raw = _multi({"A": pd.Series([1.0, 2.0, 3.0], index=tz_idx)})
    out = D._extract_close(raw, ["A"])
    assert out.index.tz is None and out.index[0] == pd.Timestamp("2020-01-01")


# ── load_prices ──────────────────────────────────────────────────────────

def test_load_prices_chunks_of_40_and_missing(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    syms = [f"S{i}" for i in range(85)]
    calls = []

    def fake(chunk, start, end):
        calls.append(list(chunk))
        return _multi({s: pd.Series(1.0, index=IDX) for s in chunk if s != "S50"})

    monkeypatch.setattr(D, "_download", fake)
    out = D.load_prices(syms, cfg, use_cache=False)
    assert [len(c) for c in calls] == [40, 40, 5]
    assert out.attrs["missing"] == ["S50"]
    assert "S50" not in out.columns and out.shape[1] == 84


def test_load_prices_all_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "_download", lambda c, s, e: pd.DataFrame())
    with pytest.raises(RuntimeError):
        D.load_prices(["A", "B"], _cfg(tmp_path), use_cache=False)


def test_load_prices_all_nan_column_is_missing(tmp_path, monkeypatch):
    def fake(chunk, s, e):
        return _multi({"A": pd.Series(1.0, index=IDX), "B": pd.Series(np.nan, index=IDX)})
    monkeypatch.setattr(D, "_download", fake)
    out = D.load_prices(["A", "B"], _cfg(tmp_path), use_cache=False)
    assert out.attrs["missing"] == ["B"] and list(out.columns) == ["A"]


def test_prices_cache_roundtrip_and_expiry(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    calls = []

    def fake(chunk, s, e):
        calls.append(1)
        return _multi({t: pd.Series(2.0, index=IDX) for t in chunk if t != "Z"})

    monkeypatch.setattr(D, "_download", fake)
    first = D.load_prices(["A", "Z"], cfg)
    second = D.load_prices(["A", "Z"], cfg)
    assert len(calls) == 1
    assert second.attrs["missing"] == ["Z"] and list(second.columns) == ["A"]
    assert second["A"].iloc[0] == pytest.approx(first["A"].iloc[0])
    assert isinstance(second.index, pd.DatetimeIndex)

    # 別の symbol 集合はキャッシュを使わない
    D.load_prices(["A", "C"], cfg)
    assert len(calls) == 2

    # 期限切れは取り直す
    csv_path = D._cache_files(cfg)[0]
    old = time.time() - 48 * 3600
    os.utime(csv_path, (old, old))
    D.load_prices(["A", "C"], cfg)
    assert len(calls) == 3

    # use_cache=False は常に取る
    D.load_prices(["A", "C"], cfg, use_cache=False)
    assert len(calls) == 4


# ── 金利 ──────────────────────────────────────────────────────────────────

BIS_HEADER = ["FREQ:Frequency", "REF_AREA:Reference area", "TIME_PERIOD:Time period or range",
              "OBS_VALUE:Observation Value"]


def _bis_zip(rows) -> bytes:
    """rows＝(頻度, 地域コード, 日付, 値) の並びから、BIS と同じ列名の CSV を入れた zip を作る。"""
    names = {"US": "United States", "JP": "Japan", "XM": "Euro area", "GB": "United Kingdom"}
    lines = [",".join(BIS_HEADER)]
    for freq, area, date, val in rows:
        f = {"D": "D: Daily", "M": "M: Monthly"}[freq]
        lines.append(f'{f},{area}: {names.get(area, area)},{date},{val}')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("WS_CBPOL_csv_flat.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


def _daily_rows(area, start, periods, value):
    return [("D", area, d.strftime("%Y-%m-%d"), value) for d in pd.date_range(start, periods=periods, freq="D")]


def _serve(monkeypatch, rows, calls=None):
    raw = _bis_zip(rows)

    def fake(url):
        if calls is not None:
            calls.append(url)
        return raw
    monkeypatch.setattr(D, "_fetch_bis_zip", fake)


def test_parse_bis_keeps_only_daily_target_areas():
    rows = [
        ("D", "US", "2020-01-01", "1.5"),
        ("D", "US", "2020-01-02", "1.75"),
        ("M", "US", "2020-01-31", "9.9"),          # 月次は捨てる
        ("D", "XM", "2020-01-01", "0.0"),          # 対象外の地域は捨てる
        ("D", "JP", "2020-01-01", ""),             # 空は捨てる
        ("D", "JP", "2020-01-02", "NaN"),          # NaN は捨てる
        ("D", "JP", "2020-01-03", "-0.1"),
    ]
    out = D._parse_bis(_bis_zip(rows), {"USD": "US", "JPY": "JP"})
    assert set(out) == {"USD", "JPY"}
    assert list(out["USD"].values) == [1.5, 1.75]
    assert out["USD"].index[0] == pd.Timestamp("2020-01-01")
    assert list(out["JPY"].values) == [-0.1] and out["JPY"].index[0] == pd.Timestamp("2020-01-03")


def test_parse_bis_currency_without_daily_rows_is_absent():
    out = D._parse_bis(_bis_zip([("D", "US", "2020-01-01", "1.0"), ("M", "JP", "2020-01-31", "0.1")]),
                       {"USD": "US", "JPY": "JP"})
    assert set(out) == {"USD"}


def test_parse_bis_missing_column_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.csv", "A,B\n1,2\n")
    with pytest.raises(ValueError):
        D._parse_bis(buf.getvalue(), {"USD": "US"})


def test_fetch_bis_zip_sends_user_agent_and_timeout(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"zipbytes"

    def fake(req, timeout):
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(D.urllib.request, "urlopen", fake)
    assert D._fetch_bis_zip("http://example.test/x.zip") == b"zipbytes"
    assert seen == {"ua": "quant-lab", "timeout": 60, "url": "http://example.test/x.zip"}


def test_rates_shift_one_business_day(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)
    rows = (_daily_rows("US", "2020-01-01", 31, "1.0") + _daily_rows("US", "2020-02-01", 60, "2.0")
            + _daily_rows("JP", "2020-01-01", 91, "0.5"))
    _serve(monkeypatch, rows)
    index = pd.bdate_range("2020-01-02", "2020-03-31")
    rates, source = D.load_rates(cfg, index, use_cache=False)
    assert source == "bis"
    # 日 t の値は t には使われず、次の営業日から効く（2/1 以降が 2%・2/3 の列は 2/2 までの値）
    assert rates.loc["2020-01-31", "USD"] == pytest.approx(0.01)       # 1/30 の値
    assert rates.loc["2020-02-03", "USD"] == pytest.approx(0.01)       # 2/3 の列は、2/3 の値でなく 1/31（1%）を引き継ぐ前営業日 1/31 の値
    assert rates.loc["2020-02-04", "USD"] == pytest.approx(0.02)
    assert rates.loc["2020-01-03", "JPY"] == pytest.approx(0.005)


def test_rates_value_on_day_t_is_used_from_next_business_day(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)
    rows = ([("D", "US", "2020-01-01", "1.0"), ("D", "US", "2020-01-07", "3.0")]
            + _daily_rows("JP", "2020-01-01", 20, "0.5"))
    _serve(monkeypatch, rows)
    index = pd.bdate_range("2020-01-02", "2020-01-17")
    rates, _ = D.load_rates(cfg, index, use_cache=False)
    assert rates.loc["2020-01-07", "USD"] == pytest.approx(0.01)       # 当日の 3% はまだ使わない
    assert rates.loc["2020-01-08", "USD"] == pytest.approx(0.03)
    assert rates.loc["2020-01-02", "USD"] == pytest.approx(0.01)       # index の初日は、その前の最後の観測


def test_rates_before_series_start_use_constant_per_currency(tmp_path, monkeypatch):
    rows = _daily_rows("US", "2020-02-03", 60, "5.0") + _daily_rows("JP", "2020-01-01", 120, "0.1")
    _serve(monkeypatch, rows)
    index = pd.bdate_range("2020-01-02", "2020-03-31")
    rates, source = D.load_rates(_small_cfg(tmp_path), index, use_cache=False)
    assert source == "bis"
    assert rates.loc["2020-01-31", "USD"] == pytest.approx(0.04)       # 始まりより前は定数
    assert rates.loc["2020-02-03", "USD"] == pytest.approx(0.04)       # 始まりの日の値も当日は使わない
    assert rates.loc["2020-02-04", "USD"] == pytest.approx(0.05)
    assert rates.loc["2020-01-02", "JPY"] == pytest.approx(0.001)      # index の前日 1/1 の値は 1/2 から使える


def test_rates_fallback_when_all_fail(tmp_path, monkeypatch):
    def boom(url):
        raise OSError("network down")
    monkeypatch.setattr(D, "_fetch_bis_zip", boom)
    index = pd.bdate_range("2020-01-02", periods=5)
    rates, source = D.load_rates(_small_cfg(tmp_path), index, use_cache=False)
    assert source == "fallback"
    assert (rates["USD"] == 0.04).all() and (rates["JPY"] == 0.007).all()


def test_rates_fallback_when_zip_is_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "_fetch_bis_zip", lambda url: b"not a zip")
    _, source = D.load_rates(_small_cfg(tmp_path), pd.bdate_range("2020-01-02", periods=3), use_cache=False)
    assert source == "fallback"


def test_rates_partial_when_one_currency_missing(tmp_path, monkeypatch):
    _serve(monkeypatch, _daily_rows("US", "2019-12-01", 60, "3.0"))        # JP の行が無い
    index = pd.bdate_range("2020-01-02", periods=5)
    rates, source = D.load_rates(_small_cfg(tmp_path), index, use_cache=False)
    assert source == "partial"
    assert np.allclose(rates["USD"].values, 0.03)
    assert (rates["JPY"] == 0.007).all()


def test_rates_unexpected_error_is_not_swallowed(tmp_path, monkeypatch):
    def boom(url):
        raise ZeroDivisionError("bug")
    monkeypatch.setattr(D, "_fetch_bis_zip", boom)
    with pytest.raises(ZeroDivisionError):
        D.load_rates(_small_cfg(tmp_path), pd.bdate_range("2020-02-03", periods=3), use_cache=False)


def test_rates_stale_series_warns_and_is_partial(tmp_path, monkeypatch, caplog):
    rows = _daily_rows("US", "2020-01-01", 10, "1.0") + _daily_rows("JP", "2020-01-01", 60, "0.5")
    _serve(monkeypatch, rows)
    index = pd.bdate_range("2020-02-03", "2020-03-02")          # US の最終 1/10 から 45 日超
    with caplog.at_level("WARNING"):
        rates, source = D.load_rates(_small_cfg(tmp_path), index, use_cache=False)
    assert source == "partial"
    assert any("古い" in r.message and "USD" in r.message for r in caplog.records)
    assert rates.loc["2020-03-02", "USD"] == pytest.approx(0.01)       # 最後の値は延ばして使う


def test_rates_cache_saved_only_for_bis(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)
    calls = []
    _serve(monkeypatch, _daily_rows("US", "2020-01-01", 60, "1.0") + _daily_rows("JP", "2020-01-01", 60, "0.5"), calls)
    index = pd.bdate_range("2020-02-03", periods=5)
    D.load_rates(cfg, index)
    assert len(calls) == 1 and D._rate_cache_path(cfg).exists()
    rates, source = D.load_rates(cfg, index)                    # キャッシュから
    assert len(calls) == 1 and source == "bis"
    assert rates.loc["2020-02-04", "JPY"] == pytest.approx(0.005)

    # 期限切れは取り直す
    old = time.time() - 48 * 3600
    os.utime(D._rate_cache_path(cfg), (old, old))
    D.load_rates(cfg, index)
    assert len(calls) == 2


def test_rates_cache_read_does_not_refresh_mtime(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)
    _serve(monkeypatch, _daily_rows("US", "2020-01-01", 60, "1.0") + _daily_rows("JP", "2020-01-01", 60, "0.5"))
    index = pd.bdate_range("2020-02-03", periods=5)
    D.load_rates(cfg, index)
    old = time.time() - 10 * 3600
    os.utime(D._rate_cache_path(cfg), (old, old))
    D.load_rates(cfg, index)                                    # キャッシュから読む
    assert abs(D._rate_cache_path(cfg).stat().st_mtime - old) < 1     # 読み出しでは書き直さない


def test_rates_empty_parse_warns_and_falls_back(tmp_path, monkeypatch, caplog):
    _serve(monkeypatch, [("M", "US", "2020-01-31", "1.0")])     # 日次の対象が1行も無い
    with caplog.at_level("WARNING"):
        _, source = D.load_rates(_small_cfg(tmp_path), pd.bdate_range("2020-02-03", periods=3), use_cache=False)
    assert source == "fallback"
    assert any("1通貨も無い" in r.message for r in caplog.records)


def test_rates_cache_not_saved_for_partial_or_fallback(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)
    _serve(monkeypatch, _daily_rows("US", "2020-01-01", 60, "1.0"))
    D.load_rates(cfg, pd.bdate_range("2020-02-03", periods=5))
    assert not D._rate_cache_path(cfg).exists()

    def boom(url):
        raise OSError("down")
    monkeypatch.setattr(D, "_fetch_bis_zip", boom)
    D.load_rates(cfg, pd.bdate_range("2020-02-03", periods=5))
    assert not D._rate_cache_path(cfg).exists()


def test_rates_stale_cache_is_not_bis(tmp_path, monkeypatch, caplog):
    cfg = _small_cfg(tmp_path)
    _serve(monkeypatch, _daily_rows("US", "2020-01-01", 60, "1.0") + _daily_rows("JP", "2020-01-01", 60, "0.5"))
    D.load_rates(cfg, pd.bdate_range("2020-02-03", periods=5))          # キャッシュを作る
    monkeypatch.setattr(D, "_fetch_bis_zip", lambda url: (_ for _ in ()).throw(AssertionError("net")))
    with caplog.at_level("WARNING"):
        _, source = D.load_rates(cfg, pd.bdate_range("2020-06-01", periods=3))   # 最終観測から45日超
    assert source == "partial"
    assert any("古い" in r.message for r in caplog.records)


def _serve_ok(monkeypatch):
    """build の検査用：全通貨が新しく取れる BIS の zip。"""
    _serve(monkeypatch, _daily_rows("US", "2019-12-01", 130, "1.0") + _daily_rows("JP", "2019-12-01", 130, "0.5"))


# ── build ────────────────────────────────────────────────────────────────

def _build_prices(chunk, start, end):
    idx = pd.bdate_range("2020-01-01", "2020-03-31")
    a = pd.Series(100.0, index=idx)
    a.loc["2020-02-10":] = 150.0                      # +50% の跳び 1 回
    b = pd.Series(50.0, index=idx)
    b = b.loc[:"2020-03-10"]                          # 3/10 で止まる
    v = pd.Series(20.0, index=idx)
    v.loc["2020-02-10":] = 80.0                       # 予想変動率の指数は跳びの対象外
    full = {"AAA": a, "BBB": b, "VIXX": v}
    return _multi({k: s for k, s in full.items() if k in chunk})


def test_build_quality_ffill_stale_jumps_iv(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)
    monkeypatch.setattr(D, "_download", _build_prices)
    _serve_ok(monkeypatch)
    md = D.build(cfg, use_cache=False, today="2020-03-31")

    assert set(md.quality) == {"rates_source", "missing", "stale", "jumps", "repairs", "as_of"}
    assert md.quality["rates_source"] == "bis"
    assert md.quality["missing"] == []
    assert md.quality["as_of"] == "2020-03-31"
    assert md.quality["stale"] == ["BBB"]
    assert [(j["symbol"], j["date"]) for j in md.quality["jumps"]] == [("AAA", "2020-02-10")]
    assert md.quality["jumps"][0]["ret"] == pytest.approx(0.5)

    # ffill は上限 3 営業日まで（3/10 の後 3/11・3/12・3/13 は埋まり、3/16 は欠損）
    assert md.close.loc["2020-03-13", "BBB"] == pytest.approx(50.0 * 1.01)
    assert np.isnan(md.close.loc["2020-03-16", "BBB"])
    # 欠損の日の収益率は NaN のまま、前方補完した日は 0（休場）
    assert md.returns.loc["2020-03-11", "BBB"] == 0.0
    assert np.isnan(md.returns.loc["2020-03-16", "BBB"])

    # 土日は索引に無い
    assert (md.close.index.dayofweek < 5).all()
    # 予想変動率：列名は原資産の symbol・100 で割る
    assert list(md.aux["iv"].columns) == ["BBB"]
    assert md.aux["iv"].loc["2020-01-02", "BBB"] == pytest.approx(0.2 * 1.01)
    # 資産クラスの引き
    assert md.symbols("fx") == ["AAA"]
    assert md.rates.index.equals(md.close.index)


def test_build_missing_symbol_reported(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)
    monkeypatch.setattr(D, "_download",
                        lambda chunk, s, e: _build_prices([c for c in chunk if c != "VIXX"], s, e))
    monkeypatch.setattr(D, "_fetch_bis_zip", lambda url: (_ for _ in ()).throw(OSError("x")))
    md = D.build(cfg, use_cache=False, today="2020-03-31")
    assert md.quality["missing"] == ["VIXX"]
    assert md.quality["rates_source"] == "fallback"
    assert md.aux["iv"].shape[1] == 0


# ── 追加の検査 ────────────────────────────────────────────────────────────

def test_download_passes_ticker_grouping_args(monkeypatch):
    import sys
    import types
    seen = {}

    def fake_download(tickers, **kw):
        seen["tickers"] = tickers
        seen["kw"] = kw
        return pd.DataFrame()

    mod = types.ModuleType("yf_cache")
    mod.download = fake_download
    monkeypatch.setitem(sys.modules, "yf_cache", mod)
    D._download(("A", "B"), "2020-01-01", "2020-02-01")
    assert seen["tickers"] == ["A", "B"]
    assert seen["kw"]["group_by"] == "ticker" and seen["kw"]["auto_adjust"] is True
    assert seen["kw"]["start"] == "2020-01-01" and seen["kw"]["end"] == "2020-02-01"


def test_load_prices_cache_with_only_missing_symbols_raises(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(D, "_download",
                        lambda c, s, e: _multi({t: pd.Series(2.0, index=IDX) for t in c if t != "Z"}))
    D.load_prices(["A", "Z"], cfg)
    with pytest.raises(RuntimeError):
        D.load_prices(["Z"], cfg)


def test_build_passes_end_as_today_plus_one(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)
    ends = []

    def fake(chunk, s, e):
        ends.append(e)
        return _build_prices(chunk, s, e)

    monkeypatch.setattr(D, "_download", fake)
    _serve_ok(monkeypatch)
    D.build(cfg, use_cache=False, today="2020-03-31")
    assert ends == ["2020-04-01"]


def test_build_returns_hand_values_and_negative_jump(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)

    def fake(chunk, s, e):
        idx = pd.bdate_range("2020-01-01", periods=10)
        a = pd.Series([100.0, 110.0, 99.0, 50.0, 50.0, 55.0, 55.0, 55.0, 55.0, 55.0], index=idx)
        return _multi({"AAA": a, "BBB": a * 0 + 10.0, "VIXX": a * 0 + 20.0})

    monkeypatch.setattr(D, "_download", fake)
    _serve_ok(monkeypatch)
    md = D.build(cfg, use_cache=False, today="2020-01-15")
    r = md.returns["AAA"]
    assert np.isnan(r.iloc[0])
    assert r.iloc[1] == pytest.approx(0.10) and r.iloc[2] == pytest.approx(-0.10)
    # 99→50 は戻らない水準の変化＝分割の調整漏れとして直す（その日の収益は 0・それより前の収益は元のまま）
    assert r.iloc[3] == pytest.approx(0.0) and r.iloc[4] == 0.0
    assert [(j["date"], round(j["ret"], 4)) for j in md.quality["jumps"]] == [("2020-01-06", round(50 / 99 - 1, 4))]
    assert [(x["date"], x["kind"]) for x in md.quality["repairs"]] == [("2020-01-06", "shift")]
    assert md.quality["stale"] == []


def test_build_ffill_limit_follows_config(tmp_path, monkeypatch):
    cfg = _small_cfg(tmp_path)
    cfg.QUANT_FFILL_LIMIT_DAYS = 1
    monkeypatch.setattr(D, "_download", _build_prices)
    _serve_ok(monkeypatch)
    md = D.build(cfg, use_cache=False, today="2020-03-31")
    assert md.close.loc["2020-03-11", "BBB"] == pytest.approx(50.5)
    assert np.isnan(md.close.loc["2020-03-12", "BBB"])


# ── 価格の修復 ────────────────────────────────────────────────────────────

def _px(values, start="2021-01-04"):
    return pd.DataFrame({"X": values}, index=pd.bdate_range(start, periods=len(values)), dtype=float)


def test_repair_spike_is_dropped_and_neighbours_keep_true_return():
    raw = _px([100, 101, 5, 102, 103])               # 1日だけの飛び値
    out, rep = D.repair_prices(raw)
    assert np.isnan(out["X"].iloc[2])
    assert out["X"].iloc[[0, 1, 3, 4]].tolist() == [100, 101, 102, 103]
    assert [(r["kind"], r["date"]) for r in rep] == [("spike", "2021-01-06")]


def test_repair_two_day_spike():
    raw = _px([100, 100, 900, 950, 101, 102])
    out, rep = D.repair_prices(raw)
    assert out["X"].isna().tolist() == [False, False, True, True, False, False]
    assert len(rep) == 1 and rep[0]["kind"] == "spike"


def test_repair_level_shift_rescales_history_and_keeps_past_returns():
    raw = _px([1000, 1010, 1020, 102, 103, 104, 105, 106, 107])     # 10分の1の調整漏れ
    out, rep = D.repair_prices(raw)
    r = out["X"].pct_change()
    assert r.iloc[1] == pytest.approx(0.01) and r.iloc[2] == pytest.approx(1020 / 1010 - 1)
    assert r.iloc[3] == pytest.approx(0.0)
    assert r.iloc[4] == pytest.approx(103 / 102 - 1)
    assert out["X"].iloc[3:].tolist() == [102, 103, 104, 105, 106, 107]      # 新しい側の値は動かさない
    assert rep[0]["kind"] == "shift" and rep[0]["ratio"] == pytest.approx(0.1)


def test_repair_nonpositive_drops_history_up_to_last_bad_day():
    raw = _px([-5, 3, -1, 50, 51, 52])
    out, rep = D.repair_prices(raw)
    assert out["X"].isna().tolist() == [True, True, True, False, False, False]
    assert rep[0]["kind"] == "nonpositive" and rep[0]["date"] == "2021-01-06"


def test_repair_leaves_normal_moves_and_skipped_columns_alone():
    raw = _px([100, 130, 95, 120, 80])               # ±40% 以内の大きな動きは実際の値動きとして残す
    out, rep = D.repair_prices(raw)
    assert out["X"].tolist() == raw["X"].tolist() and rep == []
    vix = _px([10, 40, 12, 80])
    out, rep = D.repair_prices(vix, skip={"X"})
    assert out["X"].tolist() == vix["X"].tolist() and rep == []


def test_repair_result_never_has_returns_beyond_threshold():
    rng = np.random.default_rng(3)
    x = 100 * np.cumprod(1 + rng.normal(0, 0.01, 400))
    x[10] = -1; x[50] *= 30; x[120:] *= 0.1; x[300:302] *= 0.01
    out, rep = D.repair_prices(_px(x))
    r = out["X"].dropna().pct_change().dropna()
    assert r.abs().max() <= D.JUMP_THRESHOLD
    assert {q["kind"] for q in rep} == {"spike", "shift", "nonpositive"}


def test_repair_keeps_real_crash_that_is_not_a_split_ratio():
    """戻らない −45%（分割の比ではない）は本物の値動きとして残す。直すと暴落が損益から消える。"""
    raw = _px([100, 102, 56, 57, 58, 57, 58, 59])
    out, rep = D.repair_prices(raw)
    assert out["X"].tolist() == raw["X"].tolist()
    assert [(r["kind"], r["date"]) for r in rep] == [("unexplained", "2021-01-06")]
    assert out["X"].pct_change().iloc[2] == pytest.approx(56 / 102 - 1)


def test_repair_trailing_jump_is_pending_not_fixed():
    """末尾の大きな動きは、戻るかどうかをまだ判定できない。過去を縮めて収益 0 を確定させず、その日以降を欠損にして待つ。"""
    raw = _px([100, 101, 102, 103, 50, 51])
    out, rep = D.repair_prices(raw)
    assert out["X"].iloc[:4].tolist() == [100, 101, 102, 103]            # 過去は触らない
    assert out["X"].iloc[4:].isna().all()
    assert [(r["kind"], r["date"]) for r in rep] == [("pending", "2021-01-08")]


def test_repair_drops_medium_spike_that_returns():
    """±40% に届かない飛び値（+30% で翌日に戻る）も欠損にする。残すと逆張りの戦略に偽の利益が入る。"""
    raw = _px([100, 101, 131, 101.5, 102, 103])
    out, rep = D.repair_prices(raw)
    assert np.isnan(out["X"].iloc[2]) and rep[0]["kind"] == "spike"
    # 戻らない +30% は本物として残す
    keep = _px([100, 101, 131, 132, 133, 134, 135])
    out2, rep2 = D.repair_prices(keep)
    assert out2["X"].tolist() == keep["X"].tolist() and rep2 == []
