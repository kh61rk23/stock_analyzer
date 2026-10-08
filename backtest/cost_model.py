# ==============================================================
# backtest/cost_model.py — コストモデル確定仕様（R1-1 / S1）
# ==============================================================
# 要件定義書 §R1-1 cost_model の忠実なコード化:
#   - 手数料: BT_COMMISSION_BPS（既定0）
#   - スリッページ: JP 20bps / US 10bps（BUYは+、SELLは−）
#   - 税: 実現益に BT_TAX_RATE=20.315%（NISAは0）。年内損益通算
#     （同一年の実現損失と相殺後に課税）
#   - 配当: 権利落ち日にcash加算（税同様）
#   - 空売り: 貸株料 年1.1%日割り + 逆日歩 保守的に年4%固定
# 純粋関数のみ（I/Oなし）— pytest 対象。
# ==============================================================

from __future__ import annotations


def is_jp_ticker(ticker: str) -> bool:
    """東証ティッカー（.T / 4桁数字）判定。それ以外はUS扱い。"""
    t = ticker.upper()
    return t.endswith(".T") or (t.isdigit() and len(t) == 4)


def slippage_bps(ticker: str, cfg=None) -> int:
    jp = getattr(cfg, "BT_SLIPPAGE_BPS_JP", 20)
    us = getattr(cfg, "BT_SLIPPAGE_BPS_US", 10)
    return jp if is_jp_ticker(ticker) else us


def fill_price(open_price: float, side: str, ticker: str, cfg=None) -> float:
    """共通スキーマ②: 約定価格 = 翌営業日始値 × (1±slippage)。BUYは+、SELLは−。"""
    s = slippage_bps(ticker, cfg) / 10000.0
    if side == "BUY":
        return open_price * (1 + s)
    if side == "SELL":
        return open_price * (1 - s)
    raise ValueError(f"side は BUY/SELL のみ: {side}")


def commission(notional: float, cfg=None) -> float:
    bps = getattr(cfg, "BT_COMMISSION_BPS", 0)
    return abs(notional) * bps / 10000.0


def short_carry_cost(short_notional: float, days: int = 1, cfg=None) -> float:
    """空売り保有コスト = (貸株料1.1% + 逆日歩4%固定) の日割り。"""
    loan = getattr(cfg, "BT_SHORT_STOCK_LOAN_RATE", 0.011)
    neg = getattr(cfg, "BT_SHORT_NEGATIVE_RATE", 0.04)
    return abs(short_notional) * (loan + neg) / 365.0 * days


def dividend_net(gross: float, account_type: str, cfg=None) -> float:
    """配当の受取額（権利落ち日にcash加算）。NISAは非課税。"""
    rate = getattr(cfg, "BT_TAX_RATE", 0.20315)
    return gross if account_type == "NISA" else gross * (1 - rate)


class TaxLedger:
    """
    年内損益通算つき譲渡益課税の台帳（特定口座のみ課税・NISAは対象外）。

    使い方:
        ledger.add_realized(year, pnl, account_type)   # 実現損益を記帳
        tax = ledger.settle_year(year)                 # 年末に一括課税額を返す
    同一年の実現損失と相殺後、正の残額にのみ BT_TAX_RATE を課す。
    """

    def __init__(self, cfg=None):
        self._rate = getattr(cfg, "BT_TAX_RATE", 0.20315)
        self._by_year: dict[int, float] = {}     # 特定口座の年別実現損益合計
        self._settled: set[int] = set()

    def add_realized(self, year: int, pnl: float, account_type: str = "特定") -> None:
        if account_type == "NISA":
            return                              # NISAは非課税（通算にも入れない）
        self._by_year[year] = self._by_year.get(year, 0.0) + pnl

    def year_realized(self, year: int) -> float:
        return self._by_year.get(year, 0.0)

    def settle_year(self, year: int) -> float:
        """その年の課税額（= max(0, 年内実現損益合計) × 税率）。二重精算は0を返す。"""
        if year in self._settled:
            return 0.0
        self._settled.add(year)
        gain = max(0.0, self._by_year.get(year, 0.0))
        return gain * self._rate

    def settle_all(self) -> float:
        return sum(self.settle_year(y) for y in sorted(self._by_year))
