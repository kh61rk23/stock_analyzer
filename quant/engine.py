# ==============================================================
# quant/engine.py — 重み → 日次損益（検証ラボの損益計算の中心）
# ==============================================================
# 【役割】戦略が出した「日ごとの目標の重み」を、売買コスト・金利（スワップ・貸株料など）・レバレッジの上限を
#   込みの日次損益 SimResult にする。重みを1日ずらすのは simulate だけ（日 t の重みは日 t+1 の損益に効く）。
#   戦略の側でずらすと二重にずれるので、strategies は絶対にずらさない。
# 【金利の日割り】/252。営業日の行にしか課さないため、/365 だと土日ぶんが漏れる。
# 【cost_mult の範囲】売買コスト・業者の取り分（FX のスワップの上乗せ）・逆日歩の見込み・米国信用の上乗せ金利だけ。
#   市場の金利そのもの・貸株料・買方金利には掛けない（コストを2倍にしても市場の金利は倍にならないため）。
# 【補助】vol_target（今日の重みの束を過去の収益に当てはめた変動で拡大縮小）・inverse_vol（1/σ）・apply_band（小さな差では建玉を動かさない）。
#   どれも当日までの値だけを使う。
# ==============================================================
from __future__ import annotations

import numpy as np
import pandas as pd

from quant import settings as S
from quant.types import MarketData, SimResult

PERIODS = 252


def _instrument(data: MarketData, symbol: str):
    inst = data.instruments.get(symbol)
    if inst is None:
        raise KeyError(f"engine: instruments に無い symbol: {symbol}")
    return inst


def _asof(frame: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    """frame を index の日付にそろえる。index に無い日付の値も捨てず、その後の最初の日へ前方補完で運ぶ
    （休場日や月末の暦日に置かれた値を落とさないため。後ろの日へ運ぶだけなので先の日の値は使わない）。"""
    full = frame.reindex(frame.index.union(index)).ffill()
    return full.reindex(index)


def _align_weights(weights: pd.DataFrame, data: MarketData) -> pd.DataFrame:
    """重みを価格の日付にそろえる。価格の無い銘柄の列は落とし、重みの無い日は直前の重みを持ち越す（先頭は 0）。
    終値の無い日（上場前・前方補完の上限を超えた欠け）の重みは 0 にする（値の付かない建玉は持てないため）。"""
    cols = [c for c in weights.columns if c in data.returns.columns]
    w = _asof(weights[cols].astype(float).sort_index(), data.returns.index).fillna(0.0)
    priced = data.close.reindex(index=data.returns.index, columns=cols).notna()
    return w.where(priced, 0.0).astype(float)


def _default_cap(data: MarketData, cfg, symbols) -> float:
    """資産クラスごとの QUANT_MAX_LEVERAGE のうち、対象の列が属するクラスの最小。列が無ければ上限なし。"""
    table = S.get(cfg, "QUANT_MAX_LEVERAGE")
    caps = []
    for s in symbols:
        ac = _instrument(data, s).asset_class
        if ac not in table:
            raise KeyError(f"engine: QUANT_MAX_LEVERAGE に無い資産クラス: {ac}")
        caps.append(float(table[ac]))
    return min(caps) if caps else float("inf")


def _cap_leverage(w: pd.DataFrame, cap: float) -> pd.DataFrame:
    """行ごとに Σ|w| が cap を超えたら、その行の全銘柄を比例で縮める。"""
    if w.shape[1] == 0 or not np.isfinite(cap):
        return w
    gross = w.abs().sum(axis=1)
    scale = pd.Series(1.0, index=w.index)
    over = gross > cap
    scale[over] = cap / gross[over]
    return w.mul(scale, axis=0)


def _rate_frame(data: MarketData, index: pd.DatetimeIndex) -> pd.DataFrame:
    """通貨別の年率金利を価格の日付にそろえる（前方補完。先頭の欠けは 0）。
    金利の日付が価格の日付と一致しない（月初の日付だけ等）ときも、その値を次の価格の日へ運ぶ。"""
    if data.rates.shape[1] == 0:
        return pd.DataFrame(index=index)
    return _asof(data.rates.sort_index(), index).fillna(0.0)


def _rate(rates: pd.DataFrame, ccy, symbol: str) -> pd.Series:
    if ccy is None or ccy not in rates.columns:
        raise KeyError(f"engine: {symbol} の金利（通貨 {ccy}）が data.rates に無い")
    return rates[ccy]


def simulate(weights: pd.DataFrame, data: MarketData, cfg, cost_mult: float = 1.0,
             max_leverage=None) -> SimResult:
    """重み（日 t の終値までの情報で決めた目標）から日次損益を作る。

    手順: 日付をそろえる → cash の売りを 0 に切る → レバレッジの上限で比例縮小 → 1日ずらす →
    gross・cost・financing を出して net = gross − cost − financing。
    """
    w = _align_weights(weights, data)
    symbols = list(w.columns)
    insts = {s: _instrument(data, s) for s in symbols}

    # 現物（cash）は売れない。
    for s in symbols:
        if insts[s].funding == "cash":
            w[s] = w[s].clip(lower=0.0)

    cap = float(max_leverage) if max_leverage is not None else _default_cap(data, cfg, symbols)
    w = _cap_leverage(w, cap)

    # 日 t の重みは日 t+1 の損益に効く。ずらすのはここだけ。
    w_eff = w.shift(1).fillna(0.0)
    ret = data.returns[symbols].fillna(0.0)

    gross = (w_eff * ret).sum(axis=1)

    bps = pd.Series({s: insts[s].cost_bps for s in symbols}, dtype=float)
    delta = (w_eff - w_eff.shift(1).fillna(0.0)).abs()
    turnover = delta.sum(axis=1)
    cost = (delta * bps / 1e4).sum(axis=1) * cost_mult

    rates = _rate_frame(data, w.index)
    fin = pd.Series(0.0, index=w.index)
    for s in symbols:
        inst = insts[s]
        x = w_eff[s]
        f = inst.funding
        if f == "fx":
            markup = S.get(cfg, "QUANT_FX_SWAP_MARKUP") * cost_mult
            diff = _rate(rates, inst.base, s) - _rate(rates, inst.quote, s)
            received = x * diff - x.abs() * markup
            fin += -received / PERIODS
        elif f == "futures":
            fin += x * _rate(rates, inst.currency, s) / PERIODS
        elif f == "margin_jp":
            long_rate = S.get(cfg, "QUANT_MARGIN_JP_LONG_RATE")
            short_rate = S.get(cfg, "QUANT_MARGIN_JP_SHORT_RATE")
            premium = S.get(cfg, "QUANT_MARGIN_JP_SHORT_PREMIUM") * cost_mult
            fin += (x.clip(lower=0.0) * long_rate + (-x).clip(lower=0.0) * (short_rate + premium)) / PERIODS
        elif f == "margin_us":
            spread = S.get(cfg, "QUANT_MARGIN_US_LONG_RATE_SPREAD") * cost_mult
            short_rate = S.get(cfg, "QUANT_MARGIN_US_SHORT_RATE")
            usd = _rate(rates, "USD", s)
            fin += (x.clip(lower=0.0) * (usd + spread) + (-x).clip(lower=0.0) * short_rate) / PERIODS
        elif f == "cash":
            pass
        else:
            raise ValueError(f"engine: 未知の funding: {f}（{s}）")

    net = gross - cost - fin
    leverage = w_eff.abs().sum(axis=1)
    return SimResult(net=net, gross=gross, cost=cost, financing=fin,
                     turnover=turnover, leverage=leverage, weights=w_eff)


def vol_target(weights: pd.DataFrame, data: MarketData, cfg, target=None, lookback=None,
               cap=None) -> pd.DataFrame:
    """重みの束の変動（年率）を target に合わせて全体を拡大縮小する。

    日 t の変動は「日 t の重みの束を、過去 lookback 日（t を含む）の収益に当てはめたときの日次損益」の
    標準偏差×√252。実際に持っていた損益ではなく今の束で測るので、現金から建て直した直後でも
    ずっと建てていた場合と同じ大きさに見積もれる。当日までの収益だけを使うので、先の日の値を変えても
    過去の倍率は変わらない。
    収益が NaN の日は、その銘柄の重みが 0 でない日に限り窓から外す（無い銘柄の変動を 0 と数えて
    小さく見積もらないため）。窓の有効な日が lookback の半分に満たない日・変動が 0 の日は倍率 0（建てない）。
    倍率 = min(target / 変動, cap / Σ|重み|)。拡大後の建玉の合計（Σ|w|）が cap を超えない。
    """
    target = S.get(cfg, "QUANT_STRATEGY_TARGET_VOL") if target is None else target
    lookback = int(S.get(cfg, "QUANT_VOL_LOOKBACK") if lookback is None else lookback)
    w = _align_weights(weights, data)
    if cap is None:
        cap = _default_cap(data, cfg, w.columns)
    if w.shape[1] == 0:
        return w
    wv = w.to_numpy(dtype=float)
    ret = data.returns[w.columns].to_numpy(dtype=float)
    nan = np.isnan(ret)
    ret0 = np.where(nan, 0.0, ret)
    min_valid = max(2, int(np.ceil(lookback / 2)))
    gross = np.abs(wv).sum(axis=1)
    scale = np.zeros(wv.shape[0], dtype=float)
    for t in range(wv.shape[0]):
        if gross[t] <= 0:
            continue
        lo = max(0, t - lookback + 1)
        row = wv[t]
        pr = ret0[lo:t + 1] @ row
        bad = nan[lo:t + 1] @ (row != 0).astype(float) > 0
        pr = pr[~bad]
        if pr.size < min_valid:
            continue
        vol = pr.std(ddof=1) * np.sqrt(PERIODS)
        if not vol > 0:
            continue
        scale[t] = min(target / vol, cap / gross[t])
    return w.mul(pd.Series(scale, index=w.index), axis=0)


def inverse_vol(returns: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """銘柄ごとの 1/σ（日次の標準偏差・当日まで）。履歴が lookback 日に満たない・σ が 0 の所は NaN。"""
    sd = returns.rolling(int(lookback), min_periods=int(lookback)).std()
    return 1.0 / sd.where(sd > 0)


def apply_band(weights: pd.DataFrame, band: float) -> pd.DataFrame:
    """目標との相対差が band 未満なら、前の日に実際に持っている重みのまま動かさない（売買の量を減らす）。

    相対差 = |目標 − 保有| / max(|目標|, |保有|)。保有との差で判定するので、小さな変更が積もっても
    いつかは動く。前の日の保有に依存するため行を順に処理する。NaN の目標は 0 として扱う。
    """
    tgt = weights.fillna(0.0).to_numpy(dtype=float)
    out = np.empty_like(tgt)
    held = np.zeros(tgt.shape[1], dtype=float)
    for i in range(tgt.shape[0]):
        row = tgt[i]
        denom = np.maximum(np.abs(row), np.abs(held))
        diff = np.abs(row - held)
        with np.errstate(divide="ignore", invalid="ignore"):
            rel = np.where(denom > 0, diff / denom, 0.0)
        move = rel >= band
        held = np.where(move, row, held)
        out[i] = held
    return pd.DataFrame(out, index=weights.index, columns=weights.columns)
