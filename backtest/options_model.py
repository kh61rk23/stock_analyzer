# ==============================================================
# backtest/options_model.py — オプション評価（Wave R1-1 / S1）
# ==============================================================
# 確定仕様（要件定義書 §R1-1 cost_model）:
#   - 欧州型近似の Black-Scholes。IV入力が無い場合 HV20×1.1 を代用
#   - グリークスはBS解析解（delta/gamma/theta/vega）
#   - 証拠金は「現金100%担保」のみサポート（裸売りは ValueError）
# S4（R4-3）の options/greeks.py はここを共通化して使う。
# ==============================================================

from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def hv20(closes: list) -> float:
    """直近20日の年率ヒストリカルボラティリティ（対数リターン）。"""
    if len(closes) < 21:
        raise ValueError("HV20 には最低21本の終値が必要")
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - 20, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def implied_vol_or_fallback(iv: float = None, closes: list = None) -> float:
    """IVが与えられなければ HV20×1.1 を代用（確定仕様）。"""
    if iv is not None and iv > 0:
        return iv
    if closes:
        return hv20(closes) * 1.1
    raise ValueError("IVも価格履歴も無い（どちらか必須）")


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             option_type: str = "call") -> float:
    """Black-Scholes 欧州型オプション価格。Tは年（例: 30日=30/365）。"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # 満期・退化ケースは本源的価値
        intrinsic = max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
        return intrinsic
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    if option_type == "put":
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    raise ValueError(f"option_type は call/put のみ: {option_type}")


def greeks(S: float, K: float, T: float, r: float, sigma: float,
           option_type: str = "call") -> dict:
    """BS解析解のグリークス（delta/gamma/theta/vega）。thetaは1日あたり。"""
    if T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sqT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqT)
    d2 = d1 - sigma * sqT
    gamma = _norm_pdf(d1) / (S * sigma * sqT)
    vega = S * _norm_pdf(d1) * sqT / 100.0          # 1%ボラ変化あたり
    if option_type == "call":
        delta = _norm_cdf(d1)
        theta = (-S * _norm_pdf(d1) * sigma / (2 * sqT)
                 - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-S * _norm_pdf(d1) * sigma / (2 * sqT)
                 + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0
    return {"delta": round(delta, 4), "gamma": round(gamma, 6),
            "theta": round(theta, 4), "vega": round(vega, 4)}


def check_margin(side: str, option_type: str, contracts: int, strike: float,
                 cash: float, underlying_qty: float = 0,
                 multiplier: int = 100) -> None:
    """
    証拠金検査（現金100%担保のみサポート）。
    - プット売り: cash >= strike×multiplier×枚数（キャッシュセキュアド）
    - コール売り: 原資産 >= multiplier×枚数（カバードコール）
    どちらも満たさない売りは裸売り → ValueError（確定仕様）。
    """
    if side != "SELL":
        return
    if option_type == "put":
        required = strike * multiplier * contracts
        if cash < required:
            raise ValueError(
                f"裸売り禁止: キャッシュセキュアドプットには現金{required:,.0f}が必要"
                f"（現金{cash:,.0f}）")
    elif option_type == "call":
        if underlying_qty < multiplier * contracts:
            raise ValueError(
                f"裸売り禁止: カバードコールには原資産{multiplier * contracts}株が必要"
                f"（保有{underlying_qty:.0f}株）")
