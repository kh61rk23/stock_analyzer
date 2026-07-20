# ==============================================================
# backtest/data_feed.py — 日足OHLCV・シグナル履歴リプレイ（Wave R1-1 / S1）
# ==============================================================
# 2系統のフィード:
#   InMemoryFeed : テスト/合成データ用（ネットワーク不要・pytestで使用）
#   YFinanceFeed : 実データ用（yfinance→ローカルCSVキャッシュ。
#                  取得失敗時は data_reliability.fetch_price_safe と同思想で
#                  キャッシュへフォールバック）
# シグナルリプレイ: score_history.json / signal_history.json の
# 「当日以前の最新スナップショット」を Context.signals として返す
# （未来情報のリークを構造的に防止）。
# ==============================================================

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional


class InMemoryFeed:
    """
    テスト用フィード。
    prices: {ticker: [{date, open, high, low, close, volume}, ...]}（date昇順）
    signals_by_date: {date: dict}（省略可）
    dividends: {ticker: {date: 1株配当額}}（省略可）
    """

    def __init__(self, prices: dict, signals_by_date: dict = None,
                 dividends: dict = None):
        self._prices = prices
        self._signals = signals_by_date or {}
        self._dividends = dividends or {}

    def calendar(self) -> list:
        """全ティッカーの和集合の営業日カレンダー（昇順）。"""
        dates = set()
        for rows in self._prices.values():
            dates.update(r["date"] for r in rows)
        return sorted(dates)

    def bar(self, ticker: str, date: str) -> Optional[dict]:
        for r in self._prices.get(ticker, []):
            if r["date"] == date:
                return r
        return None

    def history(self, ticker: str, until_date: str, n: int = 60) -> list:
        rows = [r for r in self._prices.get(ticker, []) if r["date"] <= until_date]
        return rows[-n:]

    def signals_at(self, date: str) -> dict:
        """当日以前の最新シグナルスナップショット（リーク防止）。"""
        keys = sorted(k for k in self._signals if k <= date)
        return self._signals[keys[-1]] if keys else {}

    def dividend(self, ticker: str, date: str) -> float:
        return float(self._dividends.get(ticker, {}).get(date, 0.0))


class YFinanceFeed(InMemoryFeed):
    """
    実データフィード。yfinance の調整済み日足を取得し
    stock_data/cache/backtest/{ticker}.csv にキャッシュする。
    ネットワーク不通時はキャッシュから復元（無ければそのtickerは空）。
    """

    def __init__(self, tickers: list, start: str, end: str, cfg=None,
                 signals_by_date: dict = None):
        cache_dir = Path(getattr(cfg, "DRIVE_BASE", Path("stock_data"))) \
            if cfg is not None else Path("stock_data")
        cache_dir = cache_dir / "cache" / "backtest"
        cache_dir.mkdir(parents=True, exist_ok=True)
        prices, dividends = {}, {}
        for t in tickers:
            rows, divs = self._load(t, start, end, cache_dir)
            if rows:
                prices[t] = rows
                dividends[t] = divs
        sig = signals_by_date
        if sig is None and cfg is not None:
            sig = self._load_score_history(cfg)
        super().__init__(prices, sig, dividends)

    @staticmethod
    def _load_score_history(cfg) -> dict:
        try:
            p = Path(cfg.DATA_DIR) / "score_history.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return {d: {"score_history": v} for d, v in data.items()}
        except Exception:
            pass
        return {}

    def _load(self, ticker: str, start: str, end: str, cache_dir: Path):
        cache = cache_dir / f"{ticker.replace('.', '_')}.csv"
        rows, divs = [], {}
        try:
            import yfinance as yf
            h = yf.Ticker(ticker).history(start=start, end=end,
                                          auto_adjust=True, actions=True)
            if h is not None and len(h) > 0:
                for idx, r in h.iterrows():
                    d = idx.strftime("%Y-%m-%d")
                    rows.append({"date": d,
                                 "open": float(r["Open"]), "high": float(r["High"]),
                                 "low": float(r["Low"]), "close": float(r["Close"]),
                                 "volume": float(r.get("Volume", 0) or 0)})
                    div = float(r.get("Dividends", 0) or 0)
                    if div > 0:
                        divs[d] = div
                self._save_cache(cache, rows, divs)
                return rows, divs
        except Exception:
            pass
        # フォールバック: ローカルキャッシュ
        if cache.exists():
            try:
                with open(cache, newline="", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        if start <= r["date"] <= end:
                            rows.append({"date": r["date"],
                                         "open": float(r["open"]), "high": float(r["high"]),
                                         "low": float(r["low"]), "close": float(r["close"]),
                                         "volume": float(r["volume"])})
                            if float(r.get("dividend", 0) or 0) > 0:
                                divs[r["date"]] = float(r["dividend"])
            except Exception:
                pass
        return rows, divs

    @staticmethod
    def _save_cache(cache: Path, rows: list, divs: dict) -> None:
        try:
            with open(cache, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["date", "open", "high", "low", "close", "volume", "dividend"])
                for r in rows:
                    w.writerow([r["date"], r["open"], r["high"], r["low"],
                                r["close"], r["volume"], divs.get(r["date"], 0)])
        except Exception:
            pass
