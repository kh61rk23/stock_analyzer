# ==============================================================
# quant/strategies.py — 戦略の系統（Family）の実装と一覧
# ==============================================================
# 【役割】FX・コモディティ・株価指数・信用取引・オプションの戦略を「系統（Family）」として並べ、
#   all_families(cfg) で全部返す。1系統が複数の設定（param_grid）を持ち、設定1つ＝試行1回として数える。
# 【重みの系統】重みの DataFrame を作る純関数（_tsmom_weights など）→ engine.vol_target（変動の目標）→
#   engine.apply_band（小さな差では動かさない）→ engine.simulate（損益）の順。targets() は vol_target と
#   band まで通した重みの最後の行から作る（simulate の中の1日ずらしの前）。
# 【時刻の約束】日 t の重みは日 t の終値までの値だけで決める（過去 L 日の収益＝close[t]/close[t−L]−1、
#   移動平均は当日を含む過去 M 日、1/σ も当日まで）。ここでは重みをずらさない（ずらすのは engine.simulate だけ）。
# 【再判定】月末は「その月の最後の営業日」（index だけで決まる。最新の月は月末がまだ来ていないので最後の行は月末とみなさない）、
#   N 日ごとは行の位置で決める。判定した日の重みを次の判定まで持つ。
# 【対象から外す条件】その日までの有効な終値の数が QUANT_MIN_HISTORY_DAYS に満たない銘柄、または 1/σ が出せない銘柄は
#   その日の対象から外す。断面で順位を付ける系統は、有効な銘柄が 2k 未満の日は建てない。
# 【オプション】simulate_options / targets_options を呼ぶだけ（理論価格なので model_priced=True）。
# ==============================================================
from __future__ import annotations

from typing import Callable, Optional

import numpy as np
import pandas as pd

from quant import engine, instruments
from quant import settings as S
from quant.options_sim import simulate_options, targets_options
from quant.types import MarketData, SimResult

_EPS = 1e-12


# ---------- 共通の部品 ----------
def _symbols(data: MarketData, asset_class: str, family_id: str) -> list:
    """価格のある、その資産クラスの銘柄。1本も無ければ理由つきで落とす。"""
    syms = data.symbols(asset_class)
    if not syms:
        raise ValueError(f"{family_id}: 資産クラス {asset_class} の価格が data に無い")
    return syms


def _eligible_inv_vol(data: MarketData, symbols: list, cfg) -> pd.DataFrame:
    """銘柄ごとの 1/σ。履歴が QUANT_MIN_HISTORY_DAYS に満たない日・当日の終値が無い日・σ が測れない日は NaN
    （NaN の銘柄はその日の対象から外れる）。履歴は「その日までの有効な終値の数」で数える。"""
    close = data.close[symbols]
    min_hist = int(S.get(cfg, "QUANT_MIN_HISTORY_DAYS"))
    enough = close.notna().cumsum() >= min_hist
    inv = engine.inverse_vol(data.returns[symbols], int(S.get(cfg, "QUANT_VOL_LOOKBACK")))
    return inv.where(enough & close.notna())


def _past_return(close: pd.DataFrame, lookback: int, skip: int = 0) -> pd.DataFrame:
    """過去 lookback 日の収益。skip があれば直近 skip 日を除く（close[t−skip]/close[t−skip−L]−1）。当日までの値だけ。"""
    return close.shift(skip) / close.shift(skip + lookback) - 1.0


def _normalize_gross(raw: pd.DataFrame, total: float = 1.0) -> pd.DataFrame:
    """行ごとに Σ|w| を total に合わせる（全部 0 の行は 0 のまま）。"""
    den = raw.abs().sum(axis=1).replace(0.0, np.nan)
    return raw.div(den, axis=0).fillna(0.0) * total


def _month_end_flags(index: pd.DatetimeIndex) -> np.ndarray:
    """各行が「その月の最後の営業日」か。次の行が別の月なら True。最後の行は月末がまだ来ていないので False。"""
    ym = np.asarray(index.year) * 12 + np.asarray(index.month)
    flags = np.zeros(len(index), dtype=bool)
    flags[:-1] = ym[1:] != ym[:-1]
    return flags


def _hold(raw: pd.DataFrame, flags: np.ndarray) -> pd.DataFrame:
    """flags が True の行の重みだけを採り、次の判定まで持ち越す。最初の判定より前は 0。"""
    cond = np.repeat(flags[:, None], raw.shape[1], axis=1)
    return raw.where(cond).ffill().fillna(0.0)


def _hold_monthly(raw: pd.DataFrame) -> pd.DataFrame:
    return _hold(raw, _month_end_flags(raw.index))


def _hold_every(raw: pd.DataFrame, days: int) -> pd.DataFrame:
    return _hold(raw, (np.arange(len(raw)) % int(days)) == 0)


def _leg_weights(mask: pd.DataFrame, inv: pd.DataFrame, total: float) -> pd.DataFrame:
    """選ばれた銘柄に 1/σ で配分し、片側の合計を total にする（選ばれた銘柄が無い行は 0）。"""
    raw = inv.where(mask, 0.0).fillna(0.0)
    den = raw.sum(axis=1).replace(0.0, np.nan)
    return raw.div(den, axis=0).fillna(0.0) * total


def _select_legs(long_score: pd.DataFrame, short_score: Optional[pd.DataFrame], inv: pd.DataFrame,
                 n_valid: pd.Series, k: int, long_only: bool) -> pd.DataFrame:
    """断面の順位で買いと売りを選ぶ。有効な銘柄（n_valid）が 2k 未満の日は建てない。

    long_score は「高いほど買いの候補」で、候補でない銘柄は NaN。
    - long_only：long_score の上位 k だけを買い、合計 1.0。
    - short_score が None（対称）：long_score の上位 k を買い・下位 k を売る。下位は順位の位置で決めるので、
      同点が並んでも買いと売りに同じ銘柄が入らない。
    - short_score あり（金利差のように買いと売りの候補が別）：それぞれの上位から、両側で同じ本数
      （k と両側の候補の数の小さいほう）を選ぶ。片側に候補が無い日は建てない。
    売買の両方を持つときは、1/σ 配分で買い側の合計＝売り側の合計＝0.5（危険量をそろえる）。
    """
    gate = (n_valid >= 2 * k).to_numpy()[:, None]
    long_rank = long_score.rank(axis=1, ascending=False, method="first")
    if long_only:
        return _leg_weights((long_rank <= k) & gate, inv, 1.0)
    if short_score is None:
        long_mask = (long_rank <= k) & gate
        short_mask = long_rank.gt(n_valid - k, axis=0) & gate
    else:
        short_rank = short_score.rank(axis=1, ascending=False, method="first")
        n = np.minimum(np.minimum(long_score.notna().sum(axis=1), short_score.notna().sum(axis=1)), k)
        long_mask = long_rank.le(n, axis=0) & gate
        short_mask = short_rank.le(n, axis=0) & gate
    return _leg_weights(long_mask, inv, 0.5) - _leg_weights(short_mask, inv, 0.5)


# ---------- 重みを作る純関数 ----------
def _tsmom_weights(data: MarketData, symbols: list, L: int, skip: int, long_only: bool, cfg) -> pd.DataFrame:
    """時系列モメンタム：過去 L 日の収益の符号で買い・売り、1/σ で配分、Σ|w|=1。long_only なら売りは建てない。"""
    inv = _eligible_inv_vol(data, symbols, cfg)
    sig = np.sign(_past_return(data.close[symbols], int(L), int(skip)))
    if long_only:
        sig = sig.clip(lower=0.0)
    raw = (sig * inv).fillna(0.0)
    return _normalize_gross(raw)


def _carry_weights(data: MarketData, symbols: list, k: int, trend: bool, cfg) -> pd.DataFrame:
    """金利差（base−quote−取り分）で、買いは正のものの上位・売りは（quote−base−取り分）が正のものの上位。
    買いと売りは同じ本数（k と両側の候補の数の小さいほう）。trend=True は過去 63 日の向きが逆（買いは収益<0・
    売りは収益>0）か判定できない銘柄を外す。有効な銘柄が 2k 未満の日・片側に候補が無い日は建てない。"""
    inv = _eligible_inv_vol(data, symbols, cfg)
    idx = data.close.index
    rates = data.rates.reindex(data.rates.index.union(idx)).ffill().reindex(idx)
    diff = {}
    for s in symbols:
        inst = data.instruments[s]
        for ccy in (inst.base, inst.quote):
            if ccy is None or ccy not in rates.columns:
                raise KeyError(f"fx_carry: {s} の金利（通貨 {ccy}）が data.rates に無い")
        diff[s] = rates[inst.base] - rates[inst.quote]
    d = pd.DataFrame(diff, index=idx)
    markup = S.get(cfg, "QUANT_FX_SWAP_MARKUP")
    buy = d - markup
    sell = -d - markup
    eligible = inv.notna()
    buy = buy.where(eligible & (buy > 0))
    sell = sell.where(eligible & (sell > 0))
    if trend:
        ret = _past_return(data.close[symbols], 63)
        buy = buy.where(ret >= 0)
        sell = sell.where(ret <= 0)
    return _select_legs(buy, sell, inv, eligible.sum(axis=1), int(k), False)


def _trend_weights(data: MarketData, symbols: list, M: int, monthly: bool, cfg) -> pd.DataFrame:
    """終値が移動平均（当日を含む過去 M 日）を上回る銘柄だけ買い、1/σ 配分で Σ|w|=1。下回れば現金。"""
    close = data.close[symbols]
    inv = _eligible_inv_vol(data, symbols, cfg)
    sma = close.rolling(int(M), min_periods=int(M)).mean()
    up = (close > sma).astype(float)
    w = _normalize_gross((up * inv).fillna(0.0))
    return _hold_monthly(w) if monthly else w


def _sector_mom_weights(data: MarketData, symbols: list, L: int, skip: int, k: int, long_only: bool,
                        cfg) -> pd.DataFrame:
    """業種の強弱：過去 L 日（直近 skip 日を除く）の騰落の上位 k を買い・下位 k を売り。"""
    inv = _eligible_inv_vol(data, symbols, cfg)
    ret = _past_return(data.close[symbols], int(L), int(skip)).where(inv.notna())
    return _select_legs(ret, None, inv, ret.notna().sum(axis=1), int(k), long_only)


def _reversal_weights(data: MarketData, symbols: list, k: int, hold: int, cfg, window: int = 5) -> pd.DataFrame:
    """短期の反動：過去 window 日の騰落の下位 k を買い・上位 k を売り、hold 日ごとに入れ替える。"""
    inv = _eligible_inv_vol(data, symbols, cfg)
    ret = _past_return(data.close[symbols], int(window)).where(inv.notna())
    raw = _select_legs(-ret, None, inv, ret.notna().sum(axis=1), int(k), False)
    return _hold_every(raw, int(hold))


# ---------- 重みの系統 ----------
class _WeightFamily:
    """重みの DataFrame で表す系統。raw_weights（純関数）→ vol_target → apply_band を final_weights にまとめる。"""

    needs_rate_history = False
    model_priced = False

    def __init__(self, family_id: str, asset_class: str, label_ja: str, grid: list,
                 raw_weights: Callable, note: Callable, needs_rate_history: bool = False):
        self.family_id = family_id
        self.asset_class = asset_class
        self.label_ja = label_ja
        self.needs_rate_history = needs_rate_history
        self._grid = grid
        self._raw = raw_weights
        self._note = note

    def param_grid(self, cfg) -> list:
        return [dict(p) for p in self._grid]

    def final_weights(self, data: MarketData, params: dict, cfg) -> pd.DataFrame:
        raw = self._raw(data, params, cfg)
        scaled = engine.vol_target(raw, data, cfg)
        return engine.apply_band(scaled, S.get(cfg, "QUANT_REBALANCE_BAND"))

    def run(self, data: MarketData, params: dict, cfg, cost_mult: float = 1.0) -> SimResult:
        return engine.simulate(self.final_weights(data, params, cfg), data, cfg, cost_mult=cost_mult)

    def targets(self, data: MarketData, params: dict, cfg) -> list:
        final = self.final_weights(data, params, cfg)
        if len(final) == 0:
            return []
        last = final.iloc[-1]
        out = []
        for sym, w in last.items():
            if abs(w) <= _EPS:
                continue
            inst = data.instruments.get(sym)
            out.append({"symbol": sym, "name": inst.name if inst else sym, "weight": float(w),
                        "exec_hint": inst.exec_hint if inst else "", "note": self._note(params, float(w))})
        return out


class _MultiTsmomFamily(_WeightFamily):
    """fx・commodity・index を別々に時系列モメンタムにし、クラスごとの変動の目標を全体の 1/3 にして足す。
    資産クラスの数ぶんだけ配分を割るので、足した全体のレバレッジも上限を超えない（クラスごとの上限 ÷ 3）。"""

    CLASSES = ("fx", "commodity", "index")

    def final_weights(self, data: MarketData, params: dict, cfg) -> pd.DataFrame:
        target = S.get(cfg, "QUANT_STRATEGY_TARGET_VOL") / len(self.CLASSES)
        caps = S.get(cfg, "QUANT_MAX_LEVERAGE")
        cap = min(float(caps[c]) for c in self.CLASSES) / len(self.CLASSES)
        parts = []
        for ac in self.CLASSES:
            syms = data.symbols(ac)
            if not syms:
                continue
            w = _tsmom_weights(data, syms, params["L"], 0, False, cfg)
            if params["monthly"]:
                w = _hold_monthly(w)
            parts.append(engine.vol_target(w, data, cfg, target=target, cap=cap))
        if not parts:
            raise ValueError("multi_tsmom: fx・commodity・index のどの価格も data に無い")
        return engine.apply_band(pd.concat(parts, axis=1), S.get(cfg, "QUANT_REBALANCE_BAND"))


def _side(w: float) -> str:
    return "買い" if w > 0 else "売り"


def _build_weight_families() -> list:
    def fx_tsmom_raw(data, p, cfg):
        return _tsmom_weights(data, _symbols(data, "fx", "fx_tsmom"), p["L"], p["skip"], False, cfg)

    def fx_carry_raw(data, p, cfg):
        return _carry_weights(data, _symbols(data, "fx", "fx_carry"), p["k"], p["trend"], cfg)

    def cmd_raw(data, p, cfg):
        return _tsmom_weights(data, _symbols(data, "commodity", "cmd_tsmom"), p["L"], 0, p["long_only"], cfg)

    def idx_raw(data, p, cfg):
        return _trend_weights(data, _symbols(data, "index", "idx_trend"), p["M"], p["monthly"], cfg)

    def jpm_raw(data, p, cfg):
        return _sector_mom_weights(data, _symbols(data, "jp_margin", "jpm_sector_mom"), p["L"], p["skip"],
                                   p["k"], p["long_only"], cfg)

    def rev_raw(data, p, cfg):
        return _reversal_weights(data, _symbols(data, "jp_margin", "jpm_reversal"), p["k"], p["hold"], cfg)

    def usm_raw(data, p, cfg):
        return _sector_mom_weights(data, _symbols(data, "us_margin", "usm_sector_mom"), p["L"], p["skip"],
                                   p["k"], False, cfg)

    def mom_note(p, w):
        skip = f"（直近{p['skip']}日を除く）" if p.get("skip") else ""
        return f"過去{p['L']}日{skip}の値動きが{'上向き' if w > 0 else '下向き'}なので{_side(w)}"

    def sector_note(p, w):
        return f"過去{p['L']}日（直近{p['skip']}日を除く）の騰落が{'上位' if w > 0 else '下位'}なので{_side(w)}"

    grid_tsmom_fx = [{"L": L, "skip": s} for L in (63, 126, 252) for s in (0, 5)]
    grid_cmd = [{"L": L, "long_only": lo} for L in (63, 126, 252) for lo in (False, True)]
    grid_multi = [{"L": L, "monthly": m} for L in (126, 252) for m in (False, True)]
    grid_jpm = [{"L": L, "skip": 21, "k": k, "long_only": lo}
                for L in (126, 252) for k in (3, 5) for lo in (False, True)]
    grid_usm = [{"L": L, "skip": 21, "k": k} for L in (126, 252) for k in (2, 3)]

    multi = _MultiTsmomFamily(
        "multi_tsmom", "index", "複数資産の順張り", grid_multi,
        raw_weights=lambda data, p, cfg: pd.DataFrame(), note=mom_note)

    return [
        _WeightFamily("fx_tsmom", "fx", "FX の順張り", grid_tsmom_fx, fx_tsmom_raw, mom_note,
                      needs_rate_history=True),
        _WeightFamily("fx_carry", "fx", "FX の金利差", [{"k": k, "trend": t} for k in (2, 3) for t in (False, True)],
                      fx_carry_raw,
                      lambda p, w: f"金利差（取り分を引いた後）が{'大きい' if w > 0 else '小さい'}ほうから{p['k']}本に入るので{_side(w)}",
                      needs_rate_history=True),
        _WeightFamily("cmd_tsmom", "commodity", "コモディティの順張り", grid_cmd, cmd_raw, mom_note),
        _WeightFamily("idx_trend", "index", "株価指数・債券の移動平均",
                      [{"M": m, "monthly": mo} for m in (100, 150, 200) for mo in (False, True)], idx_raw,
                      lambda p, w: f"終値が{p['M']}日移動平均より上なので買い"),
        multi,
        _WeightFamily("jpm_sector_mom", "jp_margin", "信用取引：日本の業種の強弱", grid_jpm, jpm_raw, sector_note),
        _WeightFamily("jpm_reversal", "jp_margin", "信用取引：日本の業種の短期の反動",
                      [{"k": k, "hold": h} for k in (3, 5) for h in (5, 10)], rev_raw,
                      lambda p, w: f"直近5日の騰落が{'下位' if w > 0 else '上位'}なので反動を見込んで{_side(w)}"),
        _WeightFamily("usm_sector_mom", "us_margin", "信用取引：米国の業種の強弱", grid_usm, usm_raw, sector_note),
    ]


# ---------- オプションの系統 ----------
_OPTION_KINDS = (
    ("opt_csp", "cash_secured_put", "の現金担保プット売り", [{"otm": o} for o in (0.03, 0.05, 0.08)]),
    ("opt_pcs", "put_credit_spread", "のプットスプレッド売り（損失限定）",
     [{"otm": o, "width": w} for o in (0.03, 0.05) for w in (0.05, 0.10)]),
    ("opt_cc", "covered_call", "のカバードコール（現物＋コール売り）", [{"otm": o} for o in (0.03, 0.05, 0.08)]),
)


def _id_symbol(symbol: str) -> str:
    """family_id に使えるよう "." "=" "^" を "_" に置き換える。"""
    for ch in (".", "=", "^"):
        symbol = symbol.replace(ch, "_")
    return symbol


class _OptionFamily:
    """オプション1種 × 原資産1つの系統。損益・目標は options_sim に任せる（理論価格での検証）。"""

    asset_class = "options"
    model_priced = True
    needs_rate_history = False

    def __init__(self, prefix: str, kind: str, symbol: str, name: str, suffix: str, grid: list):
        self.family_id = f"{prefix}_{_id_symbol(symbol)}"
        self.label_ja = f"オプション：{name} {suffix}"
        self._kind = kind
        self._symbol = symbol
        self._name = name
        self._grid = grid

    def param_grid(self, cfg) -> list:
        return [dict(p) for p in self._grid]

    def run(self, data: MarketData, params: dict, cfg, cost_mult: float = 1.0) -> SimResult:
        return simulate_options(self._kind, self._symbol, data, params, cfg, cost_mult=cost_mult)

    def targets(self, data: MarketData, params: dict, cfg) -> list:
        """今日建てるなら、の1セット。資金の全額（担保・必要資金）で1セットを建てる前提なので weight は 1.0。"""
        legs = targets_options(self._kind, self._symbol, data, params, cfg)
        if not legs:
            return []
        inst = data.instruments.get(self._symbol)
        parts = []
        for leg in legs:
            kind_ja = "プット" if leg["option_type"] == "put" else "コール"
            side_ja = "売り" if leg["side"] == "sell" else "買い"
            parts.append(f"{kind_ja}{side_ja} 行使価格 {leg['strike']:.2f}・満期 {leg['expiry']}")
        note = "、".join(parts) + f"（1株あたりの受取の合計 {legs[0]['net_credit']:.2f}・理論価格）"
        return [{"symbol": self._symbol, "name": self._name, "weight": 1.0,
                 "exec_hint": inst.exec_hint if inst else "", "note": note, "legs": legs}]


def _build_option_families(cfg) -> list:
    items = instruments.by_class(cfg).get("options", [])
    return [_OptionFamily(prefix, kind, inst.symbol, inst.name, suffix, grid)
            for inst in items for (prefix, kind, suffix, grid) in _OPTION_KINDS]


def all_families(cfg) -> list:
    """全系統（重みの系統 → オプションの系統の順）。family_id に重複は無い。"""
    fams = _build_weight_families() + _build_option_families(cfg)
    ids = [f.family_id for f in fams]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"family_id が重複している: {dup}")
    return fams
