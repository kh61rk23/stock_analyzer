# ==============================================================
# quant/research.py — 歩いて進む最適化（WFO）と候補の成績表
# ==============================================================
# 【役割】各系統（Family）の全設定を全期間で1回ずつ計算し（通常のコストとコスト2倍の2回だけ）、
#   日付を検証区間に分けて「学習期間で最良の設定の、検証区間の損益」をつないだ系列（oos）を作る。
#   その oos から門（gate.py）に渡す指標（DSR・PBO・利益の確率・p 値など）を出す。
# 【先の日を使わない】区間ごとの設定の選択は、その区間の開始の embargo 日前までの損益だけで決める。
#   学習のシャープが全設定で 0 以下でも最良の設定をそのまま使う（「悪いときは建てない」は後知恵になるため）。
# 【試行の数】DSR の試行数は全系統の設定の数の合計＋外部の実績の本数。落とした系統の設定も数える
#   （試したことに変わりはなく、数えないと偶然の最大を小さく見積もるため）。
# 【例外】family.run が例外を出した系統は、理由つきで落として次の系統へ進み、errors に残す（全体は止めない）。
# ==============================================================
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant import settings as S
from quant import stats
from quant.types import MarketData

MIN_FOLD_DAYS = 40   # 検証区間 1 つの最低の日数。これより短い区間はシャープの比較が雑音だけになる


# ── 区間割り（純関数） ──

def wfo_splits(index, n_folds: int, min_train: int, embargo: int) -> list:
    """日付の並び index を検証区間に分ける。位置はどれも両端を含む。

    最初の検証区間の開始は min_train + embargo。そこから最後までを区間の数で等分する。
    学習は先頭から「検証区間の開始の embargo 日前」まで（先頭から伸びていく）なので、学習の終わりと
    検証の始まりの間に embargo 日の空きがある。1区間が MIN_FOLD_DAYS 日未満になるなら区間の数を減らし、
    2区間も作れなければ空のリストを返す（呼び出し側が「日数不足」で落とす）。
    """
    n = len(index)
    first = int(min_train) + int(embargo)
    remain = n - first
    k = int(n_folds)
    while k >= 2 and remain // k < MIN_FOLD_DAYS:
        k -= 1
    if k < 2 or remain < 2 * MIN_FOLD_DAYS:
        return []
    bounds = [first + (i * remain) // k for i in range(k + 1)]
    out = []
    for i in range(k):
        start = bounds[i]
        out.append({
            "train_end_pos": start - int(embargo) - 1,
            "oos_start_pos": start,
            "oos_end_pos": bounds[i + 1] - 1,
        })
    return out


# ── 小さな道具 ──

def _active_start(res):
    """重みが初めて 0 でなくなった日。一度も建てていなければ None。

    leverage（建玉の合計）で見る。オプションのように weights が空でも leverage は入っているため。
    """
    lev = res.leverage
    if lev is not None and len(lev) and bool((lev.fillna(0.0).abs() > 0).any()):
        return lev.index[(lev.fillna(0.0).abs() > 0).to_numpy()][0]
    w = res.weights
    if w is not None and not w.empty:
        act = w.fillna(0.0).abs().sum(axis=1) > 0
        if bool(act.any()):
            return act.index[act.to_numpy()][0]
    return None


def _trim(net: pd.Series, start) -> pd.Series:
    """先頭の「建てていない期間」を落とした日次損益。"""
    return net.loc[net.index >= start].astype(float)


def _date(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _window(s: pd.Series, index, lo: int, hi: int) -> np.ndarray:
    """index の位置 lo〜hi（両端を含む）の値（NaN を落とす）。"""
    if hi < lo:
        return np.array([], dtype=float)
    v = s.reindex(index[lo:hi + 1]).to_numpy(dtype=float)
    return v[np.isfinite(v)]


def _pick(nets: list, index, hi: int, min_days: int):
    """位置 0〜hi の損益のシャープが最良の設定の番号とそのシャープ。

    学習の日数が min_days に満たない設定（始まりが遅い設定）は選ばない。同点は設定の並びで先のもの。
    """
    best_i, best_sr = None, -math.inf
    for i, s in enumerate(nets):
        a = _window(s, index, 0, hi)
        if a.size < min_days:
            continue
        sr = stats.sharpe(a)
        if sr > best_sr:
            best_i, best_sr = i, sr
    return best_i, best_sr


def _compound(a: np.ndarray) -> float:
    return float(np.prod(1.0 + a) - 1.0) if a.size else 0.0


def _error_text(e: Exception) -> str:
    msg = str(e).strip().replace("\n", " ")
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def _base_candidate(cid, family_id, asset_class, label, model_priced, external, n_params) -> dict:
    return {
        "id": cid, "family_id": family_id, "asset_class": asset_class, "label": label,
        "model_priced": bool(model_priced), "external": bool(external),
        "current_params": None, "n_params": int(n_params),
        "folds": [], "metrics": {}, "stress": {}, "oos_returns": [],
    }


def _rejected(base: dict, reason: str) -> dict:
    out = dict(base)
    out["reject_reason"] = reason
    return out


# ── 系統ごとの計算 ──

def _run_family(fam, grid, data: MarketData, cfg):
    """全設定を通常のコストとコスト2倍で1回ずつ計算する。

    戻り (nets, stress_nets, error)。error は例外が出たときの (params, 文)。どれかの設定で例外が出たら
    その系統は検証できないので、その時点で打ち切る。
    """
    mult = float(S.get(cfg, "QUANT_COST_STRESS_MULT"))
    nets, stress_nets = [], []
    for params in grid:
        try:
            res = fam.run(data, params, cfg)
            res_s = fam.run(data, params, cfg, cost_mult=mult)
        except Exception as e:  # 系統の不具合で全体を止めないため。理由は errors と reject_reason に残す
            return None, None, (params, _error_text(e))
        start = _active_start(res)
        if start is None:
            nets.append(pd.Series(dtype=float))
            stress_nets.append(pd.Series(dtype=float))
            continue
        nets.append(_trim(res.net, start))
        stress_nets.append(_trim(res_s.net, start))
    return nets, stress_nets, None


def _walk_forward(nets, stress_nets, grid, cfg):
    """区間ごとに学習で最良の設定を選び、検証区間の損益をつなぐ。日数が足りなければ None。"""
    nonempty = [s for s in nets if len(s)]
    if not nonempty:
        return None
    index = pd.DatetimeIndex(sorted(set().union(*[set(s.index) for s in nonempty])))
    min_train = int(S.get(cfg, "QUANT_WFO_MIN_TRAIN_DAYS"))
    embargo = int(S.get(cfg, "QUANT_WFO_EMBARGO_DAYS"))
    splits = wfo_splits(index, int(S.get(cfg, "QUANT_WFO_FOLDS")), min_train, embargo)
    if not splits:
        return None

    folds, oos_parts, stress_parts = [], [], []
    for k, sp in enumerate(splits):
        i, tr_sr = _pick(nets, index, sp["train_end_pos"], min_train)
        if i is None:
            return None
        lo, hi = sp["oos_start_pos"], sp["oos_end_pos"]
        days = index[lo:hi + 1]
        part = nets[i].reindex(days).dropna()
        spart = stress_nets[i].reindex(days).dropna()
        oos_parts.append(part)
        stress_parts.append(spart)
        a = part.to_numpy(dtype=float)
        folds.append({
            "fold": k + 1,
            "train_end": _date(index[sp["train_end_pos"]]),
            "oos_start": _date(index[lo]),
            "oos_end": _date(index[hi]),
            "params": grid[i],
            "train_sharpe": float(tr_sr),
            "oos_sharpe": stats.sharpe(a),
            "oos_return": _compound(a),
        })
    oos = pd.concat(oos_parts) if oos_parts else pd.Series(dtype=float)
    stress = pd.concat(stress_parts) if stress_parts else pd.Series(dtype=float)

    # 提案に使う今の設定：全期間（最後の embargo 日を除く）で最良のもの
    cur_i, _ = _pick(nets, index, len(index) - embargo - 1, min_train)
    current = grid[cur_i] if cur_i is not None else None
    return {"folds": folds, "oos": oos, "stress": stress, "current_params": current}


def _pbo(nets, cfg):
    """その系統の「設定×日」の行列（全設定に共通の有効な期間）で PBO。設定が1個なら None。"""
    live = [s for s in nets if len(s)]   # 一度も建てなかった設定は比べる相手にならないので除く
    if len(live) < 2:
        return None
    mat = pd.concat([s.rename(i) for i, s in enumerate(live)], axis=1, join="inner").dropna()
    res = stats.pbo_cscv(mat.to_numpy(dtype=float), n_blocks=int(S.get(cfg, "QUANT_PBO_BLOCKS")))
    return res["pbo"]


# ── 指標 ──

def _haircut_series(a: np.ndarray, haircut: float) -> np.ndarray:
    """理論価格の検証の割引：平均の利益を haircut 倍に縮めた系列（変動はそのまま＝シャープも haircut 倍）。損の系列は縮めない。"""
    mu = float(a.mean()) if a.size else 0.0
    if haircut >= 1.0 or mu <= 0.0:
        return a
    return a - (1.0 - haircut) * mu


def _metrics(oos: pd.Series, n_candidates: int, cfg, haircut: float) -> dict:
    """oos の指標。

    DSR の基準＝「本当のシャープが 0 の候補を n_candidates 本並べたとき、偶然いちばん良く見える1本のシャープ」。
    1本のシャープの推定のぶれは、検証の日数 T から 1/(T−1)（1日あたり）で決まる。候補どうしの実際の成績の差を
    ぶれとして数えない（性質の違う戦略を並べているので、差の大半は雑音ではなく戦略の違い）。
    並べる数は候補の本数。系統の中の設定の選び直しは検証区間の外で済んでいる（oos は選んだ後の損益）ので、
    設定の数では割り引かず、設定への過剰適合は PBO で見る。
    """
    a = oos.to_numpy(dtype=float)
    a = a[np.isfinite(a)]
    m = stats.summary(a)
    g = _haircut_series(a, haircut)
    m["gate_sharpe"] = stats.sharpe(g)

    var = 1.0 / (len(g) - 1) if len(g) > 1 else 0.0
    bench = stats.expected_max_sharpe(int(n_candidates), var) * math.sqrt(252)
    m["dsr"] = stats.psr(g, sr_benchmark=bench)
    m["dsr_benchmark"] = bench

    n_boot = int(S.get(cfg, "QUANT_BOOT_N"))
    block = float(S.get(cfg, "QUANT_BOOT_MEAN_BLOCK"))
    seed = int(S.get(cfg, "QUANT_BOOT_SEED"))
    pp = {}
    q05 = None
    # 利益の確率・p 値・最大の下げも、割引後の系列で測る（門の全項目を同じ保守的な系列で判定する）
    for h in S.get(cfg, "QUANT_PORT_HORIZONS"):
        r = stats.prob_profit(g, int(h), n_boot, block, seed)
        pp[str(int(h))] = r["p_profit"]
        if int(h) == 252:
            q05 = r["q05"]
    m["p_profit"] = pp
    m["p_profit_q05_12m"] = q05
    m["raw_p"] = stats.bootstrap_pvalue_mean(g, n_boot, block, seed)
    m["max_drawdown_raw"] = m["max_drawdown"]
    m["max_drawdown"] = stats.max_drawdown(g)
    m["adj_p"] = None
    m["pbo"] = None
    m["fold_positive"] = None
    return m


def _stress(s: pd.Series, haircut: float) -> dict:
    a = s.to_numpy(dtype=float)
    a = a[np.isfinite(a)]
    return {"sharpe": stats.sharpe(_haircut_series(a, haircut)), "sharpe_raw": stats.sharpe(a),
            "ann_return": stats.ann_return(a)}


# ── あらかじめ決めた組み合わせ ──

def ensemble_weights(streams: dict, lookback: int, min_members: int) -> pd.DataFrame:
    """各戦略の配分（行＝日・列＝戦略・行の合計 1）。日 t の配分は t−1 までの変動の逆数で決める。

    その日に損益のある戦略だけで配り、本数が min_members に満たない日は全部 NaN（組み合わせとして建てない）。
    """
    df = pd.DataFrame(streams).sort_index()
    vol = df.rolling(int(lookback), min_periods=max(20, int(lookback) // 2)).std().shift(1)
    inv = (1.0 / vol.where(vol > 0)).where(df.notna())
    w = inv.div(inv.sum(axis=1), axis=0)
    return w.where(w.notna().sum(axis=1) >= int(min_members))


def _ensemble_stream(streams: dict, weights: pd.DataFrame) -> pd.Series:
    df = pd.DataFrame(streams).sort_index().reindex(weights.index)
    live = weights.notna().any(axis=1)
    return (weights * df).sum(axis=1)[live]


def _next_weights(streams: dict, lookback: int, min_members: int) -> dict:
    """最新の日までの変動で決めた「次の日」の配分（提案に使う）。直近に損益のある戦略だけ。"""
    df = pd.DataFrame(streams).sort_index()
    if df.empty:
        return {}
    vol = df.rolling(int(lookback), min_periods=max(20, int(lookback) // 2)).std().iloc[-1]
    inv = (1.0 / vol.where(vol > 0)).where(df.iloc[-1].notna()).dropna()
    if len(inv) < int(min_members):
        return {}
    return {k: float(v) for k, v in (inv / inv.sum()).items()}


def _ensembles(done: dict, cfg) -> list:
    """QUANT_ENSEMBLES の組ごとに (候補の土台, 中身 or 落とす理由) を作る。done＝{family_id: {"oos","stress","haircut","label"}}。

    割引（理論価格）は戦略ごとに掛けてから足す。門に渡す系列と素の系列を別に持つ。
    """
    from fnmatch import fnmatch
    lookback = int(S.get(cfg, "QUANT_ENSEMBLE_VOL_LOOKBACK"))
    min_members = int(S.get(cfg, "QUANT_ENSEMBLE_MIN_MEMBERS"))
    out = []
    for eid, spec in S.get(cfg, "QUANT_ENSEMBLES").items():
        members = [fid for fid in done if any(fnmatch(fid, pat) for pat in spec["members"])]
        base = _base_candidate(eid, eid, "ensemble", spec["label"], False, False, 1)
        base["ensemble"] = True
        if len(members) < min_members:
            out.append((base, f"組み合わせる戦略が {min_members} 本に満たない"))
            continue
        raw = {m: done[m]["oos"] for m in members}
        # 中身の半数がそろう日から測る（少数の戦略だけの期間で測ると、運用で建てる中身と合わない）
        need = max(min_members, math.ceil(len(members) / 2))
        w = ensemble_weights(raw, lookback, need)
        oos = _ensemble_stream(raw, w)
        if len(oos) < 2:
            out.append((base, "組み合わせの損益なし"))
            continue
        gate = {m: pd.Series(_haircut_series(done[m]["oos"].to_numpy(dtype=float), done[m]["haircut"]),
                             index=done[m]["oos"].index) for m in members}
        stress = {m: pd.Series(_haircut_series(done[m]["stress"].to_numpy(dtype=float), done[m]["haircut"]),
                               index=done[m]["stress"].index) for m in members}
        stress_raw = {m: done[m]["stress"] for m in members}
        out.append((base, {
            "ensemble": True, "oos": oos, "gate": _ensemble_stream(gate, w),
            "stress": _ensemble_stream(stress, w), "stress_raw": _ensemble_stream(stress_raw, w),
            "members": _next_weights(raw, lookback, min_members),
            "model_priced": any(done[m]["haircut"] < 1.0 for m in members),
        }))
    return out


def _chunk_positive(oos: pd.Series, n: int) -> float:
    """検証の期間を n 等分したとき、利益の出た区間の割合（組み合わせには WFO の区間が無いので、同じ数に切って数える）。"""
    parts = [p for p in np.array_split(oos.to_numpy(dtype=float), max(1, int(n))) if len(p)]
    return float(np.mean([_compound(p) > 0 for p in parts])) if parts else 0.0


def _oos_list(s: pd.Series) -> list:
    return [(_date(d), float(v)) for d, v in s.items() if np.isfinite(v)]


# ── 入口 ──

def run(data: MarketData, cfg, families=None, external=None) -> dict:
    """全系統の WFO と候補の成績表。

    戻り {"as_of", "n_trials", "candidates": [候補], "trial_sharpes": {id: [年率シャープ…]}, "errors": [...]}。
    external は {id: {"label", "asset_class", "returns": Series}}。WFO なしでそのまま oos とみなす。
    """
    if families is None:
        from quant import strategies   # 戦略の一覧は重いので、使うときだけ読み込む
        families = strategies.all_families(cfg)
    external = external or {}
    ids = [f.family_id for f in families] + list(external)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"research.run: id が重複している（成績が上書きされる）: {dup}")
    haircut_opt = float(S.get(cfg, "QUANT_OPT_MODEL_HAIRCUT"))
    rates_ok = (data.quality or {}).get("rates_source") == "bis"

    errors: list = []
    trial_sharpes: dict = {}
    staged: list = []          # (候補の土台, 計算の中身 or 落とす理由)
    done: dict = {}            # 検証区間の損益まで出せた系統（組み合わせの材料）
    n_trials = 0

    for fam in families:
        grid = list(fam.param_grid(cfg))
        n_trials += len(grid)
        base = _base_candidate(fam.family_id, fam.family_id, fam.asset_class, fam.label_ja,
                               fam.model_priced, False, len(grid))
        if not grid:
            staged.append((base, "設定が無い"))
            continue
        if fam.needs_rate_history and not rates_ok:
            staged.append((base, "金利の履歴なし（金利差が損益の大きな部分を占めるため、定数の金利では検証しない）"))
            continue
        nets, stress_nets, err = _run_family(fam, grid, data, cfg)
        if err is not None:
            params, text = err
            errors.append({"family_id": fam.family_id, "params": params, "error": text})
            staged.append((base, f"計算に失敗（{text}）"))
            continue
        # 一度も建てなかった設定のシャープ 0 は「試した結果」ではないので、ばらつきの計算から除く（試行の数には数える）
        trial_sharpes[fam.family_id] = [stats.sharpe(s.to_numpy(dtype=float)) for s in nets if len(s)]
        wf = _walk_forward(nets, stress_nets, grid, cfg)
        if wf is None:
            staged.append((base, "日数不足（学習期間のあとに検証区間を2つ以上作れない）"))
            continue
        if len(wf["oos"]) < 2:
            staged.append((base, "検証区間の損益なし"))
            continue
        hc_f = haircut_opt if fam.model_priced else 1.0
        staged.append((base, {"wf": wf, "pbo": _pbo(nets, cfg), "haircut": hc_f}))
        done[fam.family_id] = {"oos": wf["oos"], "stress": wf["stress"], "haircut": hc_f}

    for eid, ext in external.items():
        n_trials += 1
        base = _base_candidate(eid, eid, ext.get("asset_class", "equity"), ext.get("label", eid),
                               False, True, 1)
        ret = pd.Series(ext["returns"], dtype=float).dropna().sort_index()
        if len(ret) < 2:
            staged.append((base, "実績の日数不足"))
            continue
        trial_sharpes[eid] = [stats.sharpe(ret.to_numpy(dtype=float))]
        staged.append((base, {"external": ret}))

    staged += _ensembles(done, cfg)
    # 並べた数は、定義した系統・外部の実績・組み合わせの全部（データの都合で落ちたものも数える。減らすと基準が甘くなる）
    n_live = len(staged)

    candidates = []
    for base, body in staged:
        if isinstance(body, str):
            candidates.append(_rejected(base, body))
            continue
        c = dict(base)
        if "external" in body:
            ret = body["external"]
            c["metrics"] = _metrics(ret, n_live, cfg, 1.0)
            # 外部の実績はコストを変えて計算し直せないので、実績そのものをコスト2倍の欄にも入れる
            c["stress"] = _stress(ret, 1.0)
            c["oos_returns"] = _oos_list(ret)
        elif "ensemble" in body:
            c["model_priced"] = body["model_priced"]
            c["members"] = body["members"]
            c["folds"] = []
            # 門に渡す値は、理論価格の戦略を割り引いてから足した系列で測る。素の系列の成績は *_raw に残す
            raw_m = stats.summary(body["oos"].to_numpy(dtype=float))
            c["metrics"] = _metrics(body["gate"], n_live, cfg, 1.0)
            c["metrics"].update({"sharpe": raw_m["sharpe"], "ann_return": raw_m["ann_return"],
                                 "max_drawdown_raw": raw_m["max_drawdown"],
                                 "fold_positive": _chunk_positive(body["gate"], int(S.get(cfg, "QUANT_WFO_FOLDS")))})
            sr_raw = body["stress_raw"].to_numpy(dtype=float)
            c["stress"] = {"sharpe": stats.sharpe(body["stress"].to_numpy(dtype=float)),
                           "sharpe_raw": stats.sharpe(sr_raw), "ann_return": stats.ann_return(sr_raw)}
            c["oos_returns"] = _oos_list(body["oos"])
        else:
            wf, hc = body["wf"], body["haircut"]
            c["current_params"] = wf["current_params"]
            c["folds"] = wf["folds"]
            c["metrics"] = _metrics(wf["oos"], n_live, cfg, hc)
            c["metrics"]["pbo"] = body["pbo"]
            c["metrics"]["fold_positive"] = float(np.mean([f["oos_return"] > 0 for f in wf["folds"]]))
            c["stress"] = _stress(wf["stress"], hc)
            c["oos_returns"] = _oos_list(wf["oos"])
        candidates.append(c)

    live = [c for c in candidates if "reject_reason" not in c]
    adj = stats.holm([c["metrics"]["raw_p"] for c in live])
    for c, p in zip(live, adj):
        c["metrics"]["adj_p"] = float(p)

    as_of = (data.quality or {}).get("as_of")
    if not as_of and len(data.returns.index):
        as_of = _date(data.returns.index[-1])
    return {"as_of": as_of, "n_trials": int(n_trials), "n_candidates": int(n_live), "candidates": candidates,
            "trial_sharpes": trial_sharpes, "errors": errors}
