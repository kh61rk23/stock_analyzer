# ==============================================================
# yf_cache.py  v1.0
# yfinance の実行内キャッシュ（重複取得の一掃）
# ==============================================================
# 【役割】
#   yfinance の Ticker / download を薄くラップし、同一実行内で
#   同じ銘柄・同じ条件の取得を1回に畳む。
#
# 【なぜ必要か】
#   2026-09-09 時点で 50 モジュールが個別に yfinance を叩いており
#   （yf.Ticker 55箇所 / yf.download 13箇所）、同じ銘柄の .info や
#   価格履歴を各モジュールが重複して取得していた。1銘柄の .info 取得は
#   ネットワーク往復を伴うため、重複分がそのまま実行時間になる。
#
# 【使い方】
#   既存の `import yfinance as yf` を `import yf_cache as yf` に置き換える
#   だけ。yf.Ticker(...) / yf.download(...) の呼び出し形式は変えない。
#   ラップしていない属性は本物の Ticker へ委譲するため、
#   .financials や .option_chain などもそのまま使える。
#
# 【キャッシュの範囲】
#   プロセス内のみ（実行が終われば消える）。日をまたぐ永続化はしない
#   ——価格は日中に変わるため、古い値を返す方が危険なため。
#
# 【依存】yfinance
# ==============================================================

from __future__ import annotations

import threading


def _yf():
    """yfinance を呼び出しのたびに解決する。

    モジュール読込時に束縛すると、テストが sys.modules["yfinance"] を
    差し替えても本物を掴んだままになり、モックが効かず実ネットワークへ
    出てしまう（差し替え前に yf_cache が読まれるため）。
    import 自体は sys.modules のキャッシュを引くだけなので安い。
    """
    import yfinance
    return yfinance

# (symbol) -> info dict
_INFO: dict = {}
# (symbol, sorted(kwargs)) -> DataFrame
_HISTORY: dict = {}
# (tickers, sorted(kwargs)) -> DataFrame
_DOWNLOAD: dict = {}
# 本物の Ticker インスタンス（生成自体にもコストがある）
_TICKERS: dict = {}

_LOCK = threading.Lock()

_STATS = {"info_hit": 0, "info_miss": 0,
          "history_hit": 0, "history_miss": 0,
          "download_hit": 0, "download_miss": 0}


def _key(kwargs: dict) -> tuple:
    """dict をハッシュ可能なキーへ。値が非ハッシュ可能なら str に落とす。"""
    out = []
    for k in sorted(kwargs):
        v = kwargs[k]
        try:
            hash(v)
        except TypeError:
            v = str(v)
        out.append((k, v))
    return tuple(out)


def _norm_tickers(tickers) -> tuple:
    """download の第1引数を正規化する（順序違いを同一視する）。"""
    if isinstance(tickers, str):
        parts = tickers.replace(",", " ").split()
    else:
        try:
            parts = [str(t) for t in tickers]
        except TypeError:
            parts = [str(tickers)]
    return tuple(sorted(parts))


def stats() -> dict:
    """キャッシュ統計。saved は節約できたネットワーク往復の回数。"""
    s = dict(_STATS)
    s["saved"] = s["info_hit"] + s["history_hit"] + s["download_hit"]
    return s


def clear() -> None:
    """キャッシュを破棄する（テスト用・長時間プロセス用）。"""
    with _LOCK:
        _INFO.clear()
        _HISTORY.clear()
        _DOWNLOAD.clear()
        _TICKERS.clear()
        for k in _STATS:
            _STATS[k] = 0


def download(tickers, **kwargs):
    """yfinance.download のキャッシュ版。

    同一実行内で同じ (銘柄集合, 条件) の取得は1回に畳む。
    呼び出し側が返り値を加工することがあるため、コピーを返す。
    """
    key = (_norm_tickers(tickers), _key(kwargs))
    with _LOCK:
        hit = key in _DOWNLOAD
        if hit:
            _STATS["download_hit"] += 1
            cached = _DOWNLOAD[key]
    if hit:
        return cached.copy() if hasattr(cached, "copy") else cached

    df = _yf().download(tickers, **kwargs)
    with _LOCK:
        _STATS["download_miss"] += 1
        _DOWNLOAD[key] = df
    return df.copy() if hasattr(df, "copy") else df


class Ticker:
    """yfinance.Ticker のキャッシュ版。

    .info と .history() をキャッシュする。それ以外の属性
    （.financials / .option_chain など）は本物へ委譲する。
    """

    def __init__(self, symbol, *args, **kwargs):
        self.ticker = str(symbol)
        self._args = args
        self._kwargs = kwargs

    def _real(self):
        with _LOCK:
            real = _TICKERS.get(self.ticker)
        if real is None:
            real = _yf().Ticker(self.ticker, *self._args, **self._kwargs)
            with _LOCK:
                _TICKERS[self.ticker] = real
        return real

    @property
    def info(self) -> dict:
        with _LOCK:
            if self.ticker in _INFO:
                _STATS["info_hit"] += 1
                return _INFO[self.ticker]
        data = self._real().info
        with _LOCK:
            _STATS["info_miss"] += 1
            _INFO[self.ticker] = data
        return data

    def history(self, **kwargs):
        key = (self.ticker, _key(kwargs))
        with _LOCK:
            hit = key in _HISTORY
            if hit:
                _STATS["history_hit"] += 1
                cached = _HISTORY[key]
        if hit:
            return cached.copy() if hasattr(cached, "copy") else cached

        df = self._real().history(**kwargs)
        with _LOCK:
            _STATS["history_miss"] += 1
            _HISTORY[key] = df
        return df.copy() if hasattr(df, "copy") else df

    def __getattr__(self, name):
        # __init__ で設定した属性より後に呼ばれるため、
        # ここに来るのはラップしていない yfinance 側の属性だけ。
        return getattr(self._real(), name)


def collected_prices() -> dict:
    """この実行で取得済みの日次価格を {ticker: DataFrame} で返す。

    BQへ永続化する側（price_history_writer）が使う。
    日足以外・極端に短い系列は対象外にする。
    """
    out: dict = {}
    with _LOCK:
        history_items = list(_HISTORY.items())
        download_items = list(_DOWNLOAD.items())

    for (ticker, kw), df in history_items:
        if df is None or getattr(df, "empty", True):
            continue
        if dict(kw).get("interval", "1d") != "1d":
            continue
        out.setdefault(ticker, df)

    for (tickers, kw), df in download_items:
        if df is None or getattr(df, "empty", True):
            continue
        if dict(kw).get("interval", "1d") != "1d":
            continue
        # group_by="ticker" の MultiIndex は level0=ticker
        cols = getattr(df, "columns", None)
        if cols is None or getattr(cols, "nlevels", 1) < 2:
            if len(tickers) == 1:
                out.setdefault(tickers[0], df)
            continue
        level0 = set(cols.get_level_values(0))
        for t in tickers:
            if t in level0 and t not in out:
                sub = df[t]
                if not getattr(sub, "empty", True):
                    out[t] = sub
    return out
