# ==============================================================
# quant/types.py — 検証ラボの共通の型
# ==============================================================
# 【役割】quant/ のモジュール同士が受け渡す形をここだけで決める（engine・strategies・research・runner が共有）。
# 【時刻の約束】DataFrame・Series の index は日付（DatetimeIndex・昇順・時刻なし）。
#   「日 t の重み」は日 t の終値までの情報で決めた目標で、損益に効くのは engine が1日ずらした t+1 から。
#   戦略の側では絶対にずらさない（二重にずれる）・先の日の値を見ない。
# ==============================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class Instrument:
    symbol: str                 # yfinance のティッカー（価格の列名）
    asset_class: str            # fx / commodity / index / jp_margin / us_margin / options
    name: str                   # 日本語の名前
    funding: str                # cash / margin_jp / margin_us / fx / futures
    cost_bps: float             # 片道の売買コスト（スプレッドの半分＋滑り）
    currency: str = "USD"       # 値段の通貨（fx は quote と同じ）
    exec_hint: str = ""         # 実際に建てるときの商品の目安
    base: Optional[str] = None  # fx の基軸通貨
    quote: Optional[str] = None
    iv_symbol: Optional[str] = None   # options：予想変動率の指数


@dataclass
class MarketData:
    close: pd.DataFrame                    # 調整後終値（列＝symbol）
    returns: pd.DataFrame                  # 日次の単純収益率（close.pct_change()。欠損は NaN のまま）
    rates: pd.DataFrame                    # 通貨別の短期金利（年率・小数・列＝通貨コード・日次）
    instruments: dict                      # {symbol: Instrument}
    quality: dict = field(default_factory=dict)   # {"rates_source": "bis"|"partial"|"fallback", "missing": [...], "stale": [...], "as_of": "YYYY-MM-DD"}
    aux: dict = field(default_factory=dict)       # {"iv": DataFrame（列＝原資産の symbol・年率小数）ほか}

    def symbols(self, asset_class: str) -> list:
        return [s for s, i in self.instruments.items() if i.asset_class == asset_class and s in self.close.columns]


@dataclass
class SimResult:
    net: pd.Series            # コスト・金利を引いた後の日次収益率（資金に対する率）
    gross: pd.Series          # 引く前
    cost: pd.Series           # 売買コスト（正の数＝払った額）
    financing: pd.Series      # 金利・スワップ・貸株料（正＝払った・負＝受け取った）
    turnover: pd.Series       # 日次の売買の量（重みの変化の絶対値の合計）
    leverage: pd.Series       # 建玉の合計（絶対値）÷資金
    weights: pd.DataFrame     # 実際に効いた重み（ずらした後・列＝symbol）。オプションは空でよい


@runtime_checkable
class Family(Protocol):
    """戦略の系統。1つの系統が複数の設定（param_grid）を持ち、設定1つ＝試行1回として数える。"""
    family_id: str            # 例 "fx_tsmom"
    asset_class: str
    label_ja: str             # メール・レポートに出す名前
    model_priced: bool        # True＝理論価格での検証（オプション）
    needs_rate_history: bool  # True＝金利の履歴が無いと検証できない（金利差の戦略）

    def param_grid(self, cfg) -> list: ...
    def run(self, data: MarketData, params: dict, cfg, cost_mult: float = 1.0) -> SimResult: ...
    def targets(self, data: MarketData, params: dict, cfg) -> list:
        """最新の日の目標の建玉。[{symbol, name, weight（資金に対する率・売りは負）, exec_hint, note}]"""
        ...
