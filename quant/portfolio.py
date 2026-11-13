# ==============================================================
# quant/portfolio.py — 採用した戦略の配分と、利益が出る確率
# ==============================================================
# 【役割】門を通った戦略を1つの組み合わせにし、各戦略への配分・全体の倍率・損失ブレーキ・今後の利益の確率を出す。
#   純関数（pandas の Series と dict を受ける）で、stats だけに依存する。
# 【手順】
#   1. 各戦略の危険量を均等にする（重み ∝ 1/年率の変動）。
#   2. 他の戦略と相関の高いものを割り引く（重み ×= 1/(1+Σ max(0, 相関))）→ 合計 1 に正規化。
#      相関は全戦略がそろっている共通の期間で測る。
#   3. 1戦略の上限・資産クラスの上限を掛ける。超えたぶんは他へ配り直さず現金にする（cash）。
#   4. 合成の日次損益（現金の利息は 0）の変動を目標に合わせる倍率（上限あり）を決め、さらに分数ケリー
#      （倍率 ≤ 係数×μ/σ²）で頭打ちにする。μ≤0 なら倍率 0（建てない）。
#   5. 損失ブレーキ：倍率を掛けた合成の損益（ブレーキ前）の高値からの下げに応じて建玉を絞る。下げは日 t の終値で測り、
#      効くのは翌日から（当日の下げを当日に避けない）。ブレーキ前で測るので、戦略が戻れば建玉も戻る。
# 【日付】全戦略の和集合にそろえ、持っていない日の損益は 0（建てていない）。
# 【戻りの意味】weights は倍率・ブレーキを掛ける前の配分（残りが cash）。実際の建玉は weights × scale × ブレーキの倍率。
#   returns・metrics・p_profit は倍率とブレーキを掛けた後の損益から出す。
#   brake_now は最新の日の終値までの下げで決めた、次の日から建てる分に掛ける倍率。
# ==============================================================
from __future__ import annotations

import numpy as np
import pandas as pd

from quant import settings as S
from quant import stats

PERIODS = 252
_MIN_COMMON_DAYS = 60   # 相関を測るのに必要な、全戦略がそろった日数


def _empty() -> dict:
    return {"weights": {}, "cash": 1.0, "note": "採用した戦略なし", "scale": 0.0, "metrics": {},
            "p_profit": {}, "returns": [], "brake_now": 1.0}


def _brake_multiplier(dd, brakes) -> float:
    """下げ dd に応じた倍率。しきい値以上のうち最も大きいしきい値の倍率（無ければ 1.0）。"""
    mult = 1.0
    for threshold, m in sorted(brakes, key=lambda x: x[0]):
        if dd >= threshold - 1e-12:
            mult = float(m)
    return mult


def build(adopted: dict, cfg) -> dict:
    """採用した戦略 {id: {"asset_class", "returns": Series, …}} から配分を作る。

    戻りは {"weights", "cash", "scale", "metrics", "p_profit": {"63":…,"126":…,"252":…}, "returns": [(YYYY-MM-DD, 値)],
    "brake_now", "note"}。採用 0 件は {"weights": {}, "cash": 1.0, "note": "採用した戦略なし", …}。
    """
    if not adopted:
        return _empty()

    ids = list(adopted)
    series = {}
    for sid in ids:
        r = pd.Series(adopted[sid]["returns"], dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
        if r.size < 2:
            raise ValueError(f"portfolio.build: {sid} の損益が少なすぎる（{r.size}日）")
        series[sid] = r
    raw = pd.concat(series, axis=1).sort_index()          # 和集合の日付・無い日は NaN
    vols = {sid: stats.ann_vol(series[sid].to_numpy()) for sid in ids}
    for sid, v in vols.items():
        if v <= 1e-12:
            raise ValueError(f"portfolio.build: {sid} の損益の変動が 0 で配分できない")

    # 1. 危険量の均等
    w = np.array([1.0 / vols[sid] for sid in ids])
    w = w / w.sum()

    # 2. 相関の割引（共通の期間で）
    if len(ids) >= 2:
        common = raw.dropna()
        if len(common) < _MIN_COMMON_DAYS:
            raise ValueError(f"portfolio.build: 全戦略がそろった期間が{len(common)}日で、相関を測れない（{_MIN_COMMON_DAYS}日未満）")
        # 共通の期間に動かない戦略があると相関が NaN になり重みが全部 NaN になるので、相関なし（0）として扱う
        corr = np.nan_to_num(common.corr().to_numpy(), nan=0.0)
        pos = np.clip(corr, 0.0, None)
        np.fill_diagonal(pos, 0.0)
        w = w / (1.0 + pos.sum(axis=1))
        w = w / w.sum()

    # 3. 上限（超えたぶんは現金）
    # 組み合わせ（ensemble）は中身が既に複数の戦略に分かれているので、1戦略の上限は掛けない。
    # 資産クラスの上限は、中身の資産クラスの内訳（class_mix）で数える（オプションだけの組み合わせが上限を抜けないように）
    ens = np.array([bool(adopted[sid].get("ensemble")) for sid in ids])
    w = np.where(ens, w, np.minimum(w, float(S.get(cfg, "QUANT_PORT_MAX_STRATEGY_WEIGHT"))))
    class_cap = float(S.get(cfg, "QUANT_PORT_MAX_CLASS_WEIGHT"))
    mixes = [adopted[sid].get("class_mix") or {adopted[sid].get("asset_class"): 1.0} for sid in ids]
    for c in sorted({k for m in mixes for k in m}, key=str):
        share = np.array([float(m.get(c, 0.0)) for m in mixes])
        total = float((w * share).sum())
        if total > class_cap:
            # そのクラスの持ち分だけを上限まで縮める（他のクラスの持ち分は動かさない）
            w = w * (1.0 - share * (1.0 - class_cap / total))
    weights = {sid: float(w[i]) for i, sid in enumerate(ids)}
    cash = float(max(0.0, 1.0 - w.sum()))

    # 4. 倍率（変動の目標・分数ケリー）
    port = (raw.fillna(0.0) * pd.Series(weights)).sum(axis=1)
    vol = stats.ann_vol(port.to_numpy())
    mu = float(port.mean()) * PERIODS
    notes = []
    if vol <= 0.0 or mu <= 0.0:
        scale = 0.0
        notes.append("合成の期待収益が 0 以下（または損益が動かない）ので建てない")
    else:
        vol_scale = min(float(S.get(cfg, "QUANT_PORT_MAX_SCALE")), float(S.get(cfg, "QUANT_PORT_TARGET_VOL")) / vol)
        kelly = float(S.get(cfg, "QUANT_PORT_KELLY_FRACTION")) * mu / (vol ** 2)
        scale = float(min(vol_scale, kelly))
        if kelly < vol_scale:
            notes.append("分数ケリーで倍率を抑えた")
    scaled = port * scale

    # 5. ブレーキ（前日までの下げで当日の倍率を決める）
    brakes = S.get(cfg, "QUANT_PORT_DD_BRAKES")
    equity = (1.0 + scaled).cumprod()
    dd = 1.0 - equity / np.maximum.accumulate(np.maximum(equity.to_numpy(), 1.0))
    dd = pd.Series(dd, index=scaled.index)
    mult_after = dd.map(lambda x: _brake_multiplier(x, brakes))   # 日 t の終値で決めた、次の日からの倍率
    mult_today = mult_after.shift(1).fillna(1.0)
    braked = scaled * mult_today
    brake_now = float(mult_after.iloc[-1])

    n_boot = int(S.get(cfg, "QUANT_BOOT_N"))
    block = float(S.get(cfg, "QUANT_BOOT_MEAN_BLOCK"))
    seed = int(S.get(cfg, "QUANT_BOOT_SEED"))
    p_profit = {str(int(h)): stats.prob_profit(braked.to_numpy(), int(h), n_boot, block, seed)["p_profit"]
                for h in S.get(cfg, "QUANT_PORT_HORIZONS")}
    return {
        "weights": weights,
        "cash": cash,
        "scale": scale,
        "metrics": stats.summary(braked.to_numpy()),
        "p_profit": p_profit,
        "returns": [(pd.Timestamp(d).strftime("%Y-%m-%d"), float(v)) for d, v in braked.items()],
        "brake_now": brake_now,
        "note": "。".join(notes),
    }
