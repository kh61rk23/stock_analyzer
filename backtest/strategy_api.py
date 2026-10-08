# ==============================================================
# backtest/strategy_api.py — Strategy プラグイン契約（R1-1 / S1）
# ==============================================================
# 全戦略（S1バックテスト / S2ペーパー / 将来のS5プレイブック等）が
# 実装する共通契約。要件定義書 §R1-1「Strategy API」の忠実なコード化。
#
#   class Strategy(Protocol):
#       strategy_id: str
#       def generate_orders(self, ctx: Context) -> list[Order]: ...
#
# Context: date, prices(直近N日DataFrame相当のdict), signals, positions, cash, equity
# Order:   ticker, side, qty または weight（qty優先）, tag
# ==============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class Order:
    """戦略が出す注文（翌営業日始値で約定 = 共通スキーマ②）。"""
    ticker: str
    side: str                       # "BUY" | "SELL"
    qty: Optional[float] = None     # 株数（weightより優先）
    weight: Optional[float] = None  # equity比の目標配分（qty未指定時に使用）
    tag: str = ""                   # 戦略側の自由メモ（ルールID等）

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side は BUY/SELL のみ: {self.side}")
        if self.qty is None and self.weight is None:
            raise ValueError("qty か weight のどちらかは必須")


@dataclass
class Position:
    """保有ポジション（account_type は税計算で使用）。"""
    ticker: str
    qty: float
    avg_cost: float
    account_type: str = "特定"      # "NISA" | "特定"

    @property
    def is_short(self) -> bool:
        return self.qty < 0


@dataclass
class Context:
    """generate_orders に渡される当日スナップショット。"""
    date: str                                   # "YYYY-MM-DD"
    prices: dict = field(default_factory=dict)  # {ticker: 直近N日の[{date,open,high,low,close,volume}]}
    signals: dict = field(default_factory=dict) # score_history/signal_history等の当日スナップショット
    positions: dict = field(default_factory=dict)  # {ticker: Position}
    cash: float = 0.0
    equity: float = 0.0

    def last_close(self, ticker: str) -> Optional[float]:
        rows = self.prices.get(ticker) or []
        return rows[-1]["close"] if rows else None


@runtime_checkable
class Strategy(Protocol):
    strategy_id: str

    def generate_orders(self, ctx: Context) -> list:
        ...
