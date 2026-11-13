# ==============================================================
# quant/options_sim.py — オプション戦略の理論価格での日次損益
# ==============================================================
# 【役割】現金担保プット売り・プットのクレジットスプレッド・カバードコール・カラーを、
#   Black-Scholes の理論価格で毎日値洗いして日次損益（必要資金に対する率）にする。裸売りは無い。
# 【時刻の約束】日 t の予想変動率は t の終値までの値だけ（指数が無い日は当日までの20日の実績×比率）。
#   建てた日の終値の理論価格で約定とみなすので、建てた日の損益はコストだけで、値動きの損益は翌日から効く。
#   満期は建てた日から QUANT_OPT_DTE 暦日後以降の最初の営業日。満期日は本質的価値で清算し、同じ日に次を建てる。
# 【資金】cash_secured_put＝建てた日の行使価格・put_credit_spread＝行使価格の差・
#   covered_call と collar＝建てた日の現値。日次損益はこれで割った率。
# ==============================================================
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from backtest.options_model import bs_price
from quant import settings as S
from quant.types import MarketData, SimResult

KINDS = ("cash_secured_put", "put_credit_spread", "covered_call", "collar")
_DAYS_PER_YEAR = 365.0


_CALL_IV_FLOOR = 0.6          # 上の行使価格の予想変動率を下げるときの下限（現値の予想変動率に対する比）

def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"オプション戦略の kind が未知: {kind}（{', '.join(KINDS)}）")


def _legs(kind: str, spot: float, params: dict) -> tuple:
    """建てる日の現値から (脚の一覧, 現物を持つか, 必要資金) を返す。脚＝(数量, "put"|"call", 行使価格)。
    数量は売りが -1・買いが +1（1株あたり）。"""
    otm = float(params.get("otm", 0.05))
    if kind == "cash_secured_put":
        k = spot * (1.0 - otm)
        return [(-1, "put", k)], False, k
    if kind == "put_credit_spread":
        width = float(params.get("width", 0.05))
        k_short = spot * (1.0 - otm)
        k_long = k_short - spot * width
        if k_long <= 0:
            raise ValueError("width が大きすぎて買いのプットの行使価格が 0 以下になる")
        return [(-1, "put", k_short), (+1, "put", k_long)], False, k_short - k_long
    if kind == "covered_call":
        return [(-1, "call", spot * (1.0 + otm))], True, spot
    put_otm = float(params.get("put_otm", otm))
    call_otm = float(params.get("call_otm", otm))
    return [(+1, "put", spot * (1.0 - put_otm)), (-1, "call", spot * (1.0 + call_otm))], True, spot


def _leg_prices(legs: list, spot: float, t_years: float, rate: float, sigma: float, skew: tuple) -> list:
    """各脚の理論価格（1株あたり・数量を掛ける前）。skew＝（下の行使価格の上げ幅, 上の行使価格の下げ幅）。
    行使価格が現値より低いほど予想変動率を上げ（プット側が割高）、高いほど下げる。"""
    skew, call_skew = skew
    out = []
    for _, opt, k in legs:
        if k <= spot:
            iv_k = sigma * (1.0 + skew * ((spot - k) / spot) / 0.10)
        else:
            # 上の行使価格は実際の市場では予想変動率が低い。下げずに値を付けるとコール売りの受取額を多く見積もる
            iv_k = sigma * max(_CALL_IV_FLOOR, 1.0 - call_skew * ((k - spot) / spot) / 0.10)
        out.append(bs_price(spot, k, t_years, rate, iv_k, opt))
    return out


def _position_value(legs: list, has_stock: bool, spot: float, t_years: float, rate: float,
                    sigma: float, skew: tuple) -> float:
    prices = _leg_prices(legs, spot, t_years, rate, sigma, skew)
    v = sum(q * p for (q, _, _), p in zip(legs, prices))
    return v + (spot if has_stock else 0.0)


def _trade_cost(price: float, spread_pct: float, min_cost: float, cost_mult: float) -> float:
    """1株あたりの片道コスト＝代金×スプレッド率/2（最低 min_cost）に cost_mult を掛けたもの。"""
    return max(price * spread_pct / 2.0, min_cost) * cost_mult


def _sigma_series(data: MarketData, symbol: str, dates: pd.DatetimeIndex, close: pd.Series, cfg) -> np.ndarray:
    """日ごとの現値の予想変動率。指数が正の値なら指数×QUANT_OPT_IV_INDEX_RATIO、無い日は当日までの20日の実績×
    QUANT_OPT_IV_HV_RATIO、どちらも無ければ NaN。
    指数（VIX など）は裾の行使価格の分を含むので、現値の行使価格の予想変動率より高い。そのまま使うと売りの受取額を多く見積もる。"""
    log_ret = np.log(close).diff()
    hv = log_ret.rolling(20).std() * math.sqrt(252.0) * S.get(cfg, "QUANT_OPT_IV_HV_RATIO")
    sigma = hv.copy()
    iv_df = data.aux.get("iv") if data.aux else None
    if iv_df is not None and symbol in iv_df.columns:
        iv = iv_df[symbol].reindex(dates)
        sigma = (iv * float(S.get(cfg, "QUANT_OPT_IV_INDEX_RATIO"))).where(iv > 0, hv)
    return sigma.to_numpy(dtype=float)


def _rate_series(data: MarketData, symbol: str, dates: pd.DatetimeIndex) -> np.ndarray:
    inst = data.instruments.get(symbol) if data.instruments else None
    ccy = getattr(inst, "currency", "USD") if inst is not None else "USD"
    if data.rates is None or ccy not in data.rates.columns:
        return np.zeros(len(dates))
    return data.rates[ccy].reindex(dates, method="ffill").fillna(0.0).to_numpy(dtype=float)


def _prepare(kind: str, symbol: str, data: MarketData, cfg) -> tuple:
    _check_kind(kind)
    if symbol not in data.close.columns:
        raise ValueError(f"価格に原資産が無い: {symbol}")
    close = data.close[symbol].dropna()
    if close.empty:
        raise ValueError(f"原資産の価格が空: {symbol}")
    dates = close.index
    return close, dates, _sigma_series(data, symbol, dates, close, cfg), _rate_series(data, symbol, dates)


def simulate_options(kind: str, symbol: str, data: MarketData, params: dict, cfg,
                     cost_mult: float = 1.0) -> SimResult:
    """オプション戦略1つの日次損益。net＝gross−cost（金利は理論価格の割引にだけ使い、担保の利息は入れない）。"""
    close, dates, sigma, rate = _prepare(kind, symbol, data, cfg)
    px = close.to_numpy(dtype=float)
    n = len(dates)
    dte = int(S.get(cfg, "QUANT_OPT_DTE"))
    skew = (float(S.get(cfg, "QUANT_OPT_SKEW_PER_10PCT")), float(S.get(cfg, "QUANT_OPT_CALL_SKEW_PER_10PCT")))
    spread = S.get(cfg, "QUANT_OPT_SPREAD_PCT")
    min_cost = S.get(cfg, "QUANT_OPT_MIN_COST_PER_SHARE")

    gross = np.zeros(n)
    cost = np.zeros(n)
    turnover = np.zeros(n)
    leverage = np.zeros(n)
    pos = None
    last_sigma = float("nan")

    for i in range(n):
        spot = px[i]
        if not math.isnan(sigma[i]):
            last_sigma = sigma[i]
        sig = last_sigma
        if pos is not None:
            t_years = 0.0 if i >= pos["exp_i"] else (pos["exp_date"] - dates[i]).days / _DAYS_PER_YEAR
            value = _position_value(pos["legs"], pos["stock"], spot, t_years, rate[i], sig, skew)
            gross[i] += (value - pos["value"]) / pos["cap"]
            pos["value"] = value
            leverage[i] = 1.0
            if i >= pos["exp_i"]:
                # 満期の清算：価値が残る脚だけ閉じるコストがかかる（価値 0 は放置で消える）
                for (_, opt, k) in pos["legs"]:
                    intrinsic = max(0.0, k - spot) if opt == "put" else max(0.0, spot - k)
                    if intrinsic > 0:
                        cost[i] += _trade_cost(intrinsic, spread, min_cost, cost_mult) / pos["cap"]
                        turnover[i] += intrinsic / pos["cap"]
                pos = None
        if pos is None and not math.isnan(sig):
            legs, stock, cap = _legs(kind, spot, params)
            target = dates[i] + pd.Timedelta(days=dte)
            exp_i = int(dates.searchsorted(target, side="left"))
            exp_date = dates[exp_i] if exp_i < n else target
            t_years = (exp_date - dates[i]).days / _DAYS_PER_YEAR
            prices = _leg_prices(legs, spot, t_years, rate[i], sig, skew)
            for p in prices:
                cost[i] += _trade_cost(p, spread, min_cost, cost_mult) / cap
                turnover[i] += p / cap
            pos = {"legs": legs, "stock": stock, "cap": cap, "exp_i": exp_i, "exp_date": exp_date,
                   "value": _position_value(legs, stock, spot, t_years, rate[i], sig, skew)}
            leverage[i] = 1.0

    # 原資産の価格が無い日（上場前・長い欠け）も含めた全体の日付にそろえて 0 で埋める。
    # 他の戦略（engine）の結果と同じ index にしないと、research で並べたときに日がずれる。
    idx = data.close.index

    def _full(a: np.ndarray) -> pd.Series:
        return pd.Series(a, index=dates).reindex(idx).fillna(0.0)

    g = _full(gross)
    c = _full(cost)
    return SimResult(
        net=g - c, gross=g, cost=c,
        financing=pd.Series(0.0, index=idx),
        turnover=_full(turnover),
        leverage=_full(leverage),
        weights=pd.DataFrame(index=idx),
    )


def targets_options(kind: str, symbol: str, data: MarketData, params: dict, cfg) -> list:
    """最新の日に建てるなら、の脚ごとの行使価格・満期・理論価格。予想変動率が出せないときは空。
    premium は1株あたりで、受け取る側（売り）は正・払う側（買い）は負。net_credit は脚の合計（1株あたり）。"""
    close, dates, sigma, rate = _prepare(kind, symbol, data, cfg)
    if math.isnan(sigma[-1]):
        return []
    spot = float(close.iloc[-1])
    dte = int(S.get(cfg, "QUANT_OPT_DTE"))
    skew = (float(S.get(cfg, "QUANT_OPT_SKEW_PER_10PCT")), float(S.get(cfg, "QUANT_OPT_CALL_SKEW_PER_10PCT")))
    legs, _, cap = _legs(kind, spot, params)
    # 先の営業日は分からないので、DTE 暦日後が土日なら次の平日を満期とする（simulate_options の満期の決め方に合わせる）
    expiry = dates[-1] + pd.Timedelta(days=dte)
    if expiry.weekday() >= 5:
        expiry = expiry + pd.offsets.BDay(1)
    prices = _leg_prices(legs, spot, (expiry - dates[-1]).days / _DAYS_PER_YEAR, rate[-1], float(sigma[-1]), skew)
    net = float(sum(-q * p for (q, _, _), p in zip(legs, prices)))
    return [
        {"kind": kind, "symbol": symbol, "side": "sell" if q < 0 else "buy", "option_type": opt,
         "strike": float(k), "expiry": expiry.strftime("%Y-%m-%d"), "dte": int((expiry - dates[-1]).days),
         "premium": float(-q * p), "net_credit": net, "capital_per_share": float(cap)}
        for (q, opt, k), p in zip(legs, prices)
    ]
