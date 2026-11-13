# ==============================================================
# quant/stats.py — 検証ラボの統計の純関数
# ==============================================================
# 【役割】日次収益率から成績の指標と「偶然ではないか」の検定を出す。副作用なし・numpy だけ。
#   - 指標：シャープ・ソルティノ・最大の下げ・カルマー・年率の収益と変動・歪度・尖度（正規で 3）
#   - 検定：PSR / DSR（試した数で割り引くシャープ）・最小の実績日数・PBO（CSCV）
#   - 再標本：定常ブートストラップ（Politis–Romano）・利益が出る確率・平均>0 の片側 p
#   - 多重検定：Holm・Benjamini–Hochberg
# 【入力】1 次元の日次収益率（list・ndarray・Series）。NaN と ±inf は落としてから計算する。
# 【乱数】seed を引数で受けて np.random.default_rng(seed) を使う（同じ seed なら同じ結果）。
# 【正規分布】scipy に頼らないため、累積は math.erf、逆関数は Acklam の近似に Halley 法を1回かけて
#   誤差 1e-12 程度まで詰めている。
# ==============================================================
from __future__ import annotations

import itertools
import math

import numpy as np

EULER_GAMMA = 0.5772156649015329


# ── 正規分布 ──

def norm_cdf(x: float) -> float:
    """標準正規分布の累積 Φ(x)。"""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00)
_P_LOW = 0.02425


def norm_ppf(p: float) -> float:
    """標準正規分布の逆関数 Φ⁻¹(p)。p は (0, 1)。端の 0・1 は ∓inf。

    Acklam の有理近似（相対誤差 1e-9 程度）に Halley 法を1回かけて精度を上げる。
    """
    if not (0.0 <= p <= 1.0) or math.isnan(p):
        raise ValueError(f"norm_ppf: p は 0〜1 の範囲で渡す（p={p}）")
    if p == 0.0:
        return -math.inf
    if p == 1.0:
        return math.inf
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    elif p <= 1.0 - _P_LOW:
        q = p - 0.5
        rr = q * q
        x = (((((_A[0] * rr + _A[1]) * rr + _A[2]) * rr + _A[3]) * rr + _A[4]) * rr + _A[5]) * q / \
            (((((_B[0] * rr + _B[1]) * rr + _B[2]) * rr + _B[3]) * rr + _B[4]) * rr + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    # Halley 法（上側の裾は 1−p の桁落ちを避けるため対称に計算する）
    if p > 0.5:
        e = 0.5 * math.erfc(x / math.sqrt(2.0)) - (1.0 - p)
        u = -e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    else:
        e = 0.5 * math.erfc(-x / math.sqrt(2.0)) - p
        u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


# ── 入力の整理 ──

def _clean(r) -> np.ndarray:
    """1 次元の float 配列にし、NaN と ±inf を落とす。"""
    a = np.asarray(r, dtype=float).ravel()
    return a[np.isfinite(a)]


def _moments(a: np.ndarray):
    """(平均, 標準偏差 ddof=1, 歪度, 尖度) を返す。歪度・尖度は標本の中心モーメントから（正規で 0・3）。"""
    mu = float(a.mean())
    sd = float(a.std(ddof=1))
    d = a - mu
    m2 = float(np.mean(d ** 2))
    if m2 <= 0.0:
        return mu, sd, 0.0, 3.0
    g3 = float(np.mean(d ** 3)) / m2 ** 1.5
    g4 = float(np.mean(d ** 4)) / m2 ** 2
    return mu, sd, g3, g4


# ── 指標 ──

def sharpe(r, periods: int = 252) -> float:
    """年率のシャープ（無リスク金利 0）。標準偏差 0・n<2 は 0.0。"""
    a = _clean(r)
    if a.size < 2:
        return 0.0
    sd = float(a.std(ddof=1))
    if sd <= 0.0:
        return 0.0
    return float(a.mean()) / sd * math.sqrt(periods)


def sortino(r, periods: int = 252) -> float:
    """年率のソルティノ。下方偏差は sqrt(mean(min(r,0)²))（目標 0）。下げが無い・n<2 は 0.0。"""
    a = _clean(r)
    if a.size < 2:
        return 0.0
    dd = math.sqrt(float(np.mean(np.minimum(a, 0.0) ** 2)))
    if dd <= 0.0:
        return 0.0
    return float(a.mean()) / dd * math.sqrt(periods)


def max_drawdown(r) -> float:
    """複利の資産曲線（初期値 1）の高値からの最大の下落率。正の数（0.2＝20% の下げ）。"""
    a = _clean(r)
    if a.size == 0:
        return 0.0
    eq = np.concatenate([[1.0], np.cumprod(1.0 + a)])
    peak = np.maximum.accumulate(eq)
    return float(np.max(1.0 - eq / peak))


def ann_return(r, periods: int = 252) -> float:
    """複利の年率収益。資産が 0 以下になった系列は -1.0。"""
    a = _clean(r)
    if a.size == 0:
        return 0.0
    growth = float(np.prod(1.0 + a))
    if growth <= 0.0:
        return -1.0
    return growth ** (periods / a.size) - 1.0


def ann_vol(r, periods: int = 252) -> float:
    """年率の変動（標準偏差 ddof=1 × √periods）。n<2 は 0.0。"""
    a = _clean(r)
    if a.size < 2:
        return 0.0
    return float(a.std(ddof=1)) * math.sqrt(periods)


def calmar(r, periods: int = 252) -> float:
    """年率収益 ÷ 最大の下げ。下げが 0 は 0.0。"""
    mdd = max_drawdown(r)
    if mdd <= 0.0:
        return 0.0
    return ann_return(r, periods) / mdd


def skew(r) -> float:
    """歪度（標本の中心モーメント）。n<2・変動 0 は 0.0。"""
    a = _clean(r)
    if a.size < 2:
        return 0.0
    return _moments(a)[2]


def kurtosis(r) -> float:
    """尖度（超過ではない・正規で 3）。n<2・変動 0 は 3.0。"""
    a = _clean(r)
    if a.size < 2:
        return 3.0
    return _moments(a)[3]


# ── シャープの検定 ──

def _sr_stats(a: np.ndarray):
    """期間あたりのシャープと (歪度, 尖度)。n<2・変動 0 は None。"""
    if a.size < 2:
        return None
    mu, sd, g3, g4 = _moments(a)
    if sd <= 0.0:
        return None
    return mu / sd, g3, g4


def _sr_denominator(sr: float, g3: float, g4: float) -> float:
    """PSR の分母の中身 1 − γ3·SR + (γ4−1)/4·SR²。数値の都合で 0 以下になったら小さな正の数にする。"""
    v = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    return max(v, 1e-12)


def psr(r, sr_benchmark: float = 0.0, periods: int = 252) -> float:
    """Probabilistic Sharpe Ratio：真のシャープが sr_benchmark（年率）を超える確率。

    期間あたりのシャープで Φ((SR−SR*)·√(n−1) / √(1 − γ3·SR + (γ4−1)/4·SR²)) を計算する。
    n<2・変動 0 は判断できないので 0.0。
    """
    a = _clean(r)
    st = _sr_stats(a)
    if st is None:
        return 0.0
    sr, g3, g4 = st
    sr_star = sr_benchmark / math.sqrt(periods)
    z = (sr - sr_star) * math.sqrt(a.size - 1) / math.sqrt(_sr_denominator(sr, g3, g4))
    return norm_cdf(z)


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """真のシャープが 0 の試行を n 回したときの、シャープの最大の期待値（var_sr と同じ単位）。

    √var · ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))、γ＝オイラー定数。N≤1・var≤0 は 0.0。
    """
    n = int(n_trials)
    if n <= 1 or not (var_sr > 0.0):
        return 0.0
    z1 = norm_ppf(1.0 - 1.0 / n)
    z2 = norm_ppf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(var_sr) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def dsr(r, trial_sharpes_annual, periods: int = 252) -> dict:
    """Deflated Sharpe Ratio：試した全ての設定のシャープのばらつきから「偶然の最大」を出し、それを基準にした PSR。

    trial_sharpes_annual は試した全設定の年率シャープ（自分を含む）。分散は期間あたりに直してから ddof=1 で取る。
    """
    ts = _clean(trial_sharpes_annual) / math.sqrt(periods)
    n = int(ts.size)
    var = float(ts.var(ddof=1)) if n >= 2 else 0.0
    sr_star = expected_max_sharpe(n, var)
    sr_star_annual = sr_star * math.sqrt(periods)
    return {
        "dsr": psr(r, sr_benchmark=sr_star_annual, periods=periods),
        "sr_benchmark_annual": sr_star_annual,
        "n_trials": n,
    }


def min_track_record_length(r, sr_benchmark: float = 0.0, conf: float = 0.95,
                            periods: int = 252) -> float:
    """シャープが sr_benchmark（年率）を conf の確からしさで超えると言うのに要る日数。

    1 + (1 − γ3·SR + (γ4−1)/4·SR²)·(Φ⁻¹(conf)/(SR−SR*))²（期間あたり）。SR≤SR*・判断できない系列は inf。
    """
    a = _clean(r)
    st = _sr_stats(a)
    if st is None:
        return math.inf
    sr, g3, g4 = st
    sr_star = sr_benchmark / math.sqrt(periods)
    if sr <= sr_star:
        return math.inf
    z = norm_ppf(conf)
    return 1.0 + _sr_denominator(sr, g3, g4) * (z / (sr - sr_star)) ** 2


# ── PBO（CSCV） ──

def pbo_cscv(trial_returns, n_blocks: int = 10) -> dict:
    """Probability of Backtest Overfitting（Combinatorially Symmetric Cross-Validation）。

    trial_returns は行＝日・列＝試行の 2 次元。NaN を含む行は落とす（試行どうしを同じ日で比べるため）。
    行を n_blocks に等分（割り切れない先頭の行は捨てる）し、半分を学習・残りを検証にする全ての組で、
    学習のシャープが最良の試行の、検証のシャープでの順位 ω＝rank/(N+1)（同順位は平均の順位）を求める。
    pbo＝logit(ω)≤0（検証で中央以下）の割合。各組のシャープはブロックの和と二乗和からまとめて計算する。
    """
    x = np.asarray(trial_returns, dtype=float)
    if x.ndim != 2:
        raise ValueError(f"pbo_cscv: 2 次元（行＝日・列＝試行）で渡す（ndim={x.ndim}）")
    if n_blocks < 2 or n_blocks % 2 != 0:
        raise ValueError(f"pbo_cscv: n_blocks は 2 以上の偶数（n_blocks={n_blocks}）")
    x = x[np.all(np.isfinite(x), axis=1)]
    t, n = x.shape
    n_combos = math.comb(n_blocks, n_blocks // 2)
    if n < 2:
        return {"pbo": None, "n_combos": n_combos, "n_trials": int(n), "reason": "試行が2未満"}
    block_len = t // n_blocks
    if block_len < 2:
        return {"pbo": None, "n_combos": n_combos, "n_trials": int(n), "reason": "日数不足"}
    x = x[t - block_len * n_blocks:]
    blocks = x.reshape(n_blocks, block_len, n)
    bsum = blocks.sum(axis=1)                    # (S, N)
    bsq = (blocks ** 2).sum(axis=1)              # (S, N)

    combos = list(itertools.combinations(range(n_blocks), n_blocks // 2))
    mask = np.zeros((len(combos), n_blocks), dtype=float)
    for i, c in enumerate(combos):
        mask[i, list(c)] = 1.0
    cnt = block_len * (n_blocks // 2)

    def _sharpe_from(msk):
        s = msk @ bsum                           # (C, N)
        q = msk @ bsq
        mean = s / cnt
        var = (q - cnt * mean * mean) / (cnt - 1)
        sd = np.sqrt(np.maximum(var, 0.0))
        out = np.zeros_like(mean)
        # 定数列は浮動小数の丸めで分散が 1e-20 程度の正になり得る。巨大なシャープを作らないよう二乗平均との比で判定する
        ok = var > 1e-12 * (q / cnt)
        out[ok] = mean[ok] / sd[ok]
        return out

    sr_is = _sharpe_from(mask)
    sr_oos = _sharpe_from(1.0 - mask)
    best = np.argmax(sr_is, axis=1)              # (C,)
    rows = np.arange(len(combos))
    oos_best = sr_oos[rows, best][:, None]
    below = np.sum(sr_oos < oos_best, axis=1)
    ties = np.sum(sr_oos == oos_best, axis=1) - 1
    rank = below + 1.0 + 0.5 * ties
    omega = rank / (n + 1.0)
    logit = np.log(omega / (1.0 - omega))
    return {
        "pbo": float(np.mean(logit <= 0.0)),
        "n_combos": len(combos),
        "n_trials": int(n),
    }


# ── 再標本 ──

def stationary_bootstrap(r, n_boot: int, mean_block: float, seed: int, length=None) -> np.ndarray:
    """Politis–Romano の定常ブートストラップ。戻りは shape (n_boot, length)。

    各位置で確率 1/mean_block で新しい開始点（一様）に跳び、それ以外は前の位置の次（末尾の次は先頭）を取る。
    ブロックの長さが幾何分布になるので、自己相関をある程度残したまま定常な系列が作れる。
    """
    a = _clean(r)
    n = a.size
    if n == 0:
        raise ValueError("stationary_bootstrap: 有効な値が無い")
    n_boot = int(n_boot)
    length = n if length is None else int(length)
    if n_boot < 1 or length < 1:
        raise ValueError(f"stationary_bootstrap: n_boot・length は 1 以上（n_boot={n_boot}, length={length}）")
    p = 1.0 / max(float(mean_block), 1.0)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(n_boot, length))
    jump = rng.random((n_boot, length)) < p
    jump[:, 0] = True
    pos = np.arange(length)
    last = np.maximum.accumulate(np.where(jump, pos, 0), axis=1)   # 直近に跳んだ位置
    base = np.take_along_axis(starts, last, axis=1)
    idx = (base + (pos - last)) % n
    return a[idx]


def prob_profit(r, horizon: int, n_boot: int, mean_block: float, seed: int) -> dict:
    """horizon 日の複利の累積収益を再標本し、正になる割合と分位（5%・50%・95%）を返す。n<2 は None。"""
    a = _clean(r)
    if a.size < 2:
        return {"p_profit": None, "q05": None, "q50": None, "q95": None}
    paths = stationary_bootstrap(a, n_boot, mean_block, seed, length=horizon)
    cum = np.prod(1.0 + paths, axis=1) - 1.0
    q05, q50, q95 = np.quantile(cum, [0.05, 0.5, 0.95])
    return {"p_profit": float(np.mean(cum > 0.0)), "q05": float(q05), "q50": float(q50), "q95": float(q95)}


def bootstrap_pvalue_mean(r, n_boot: int, mean_block: float, seed: int) -> float:
    """「平均>0」の片側 p。平均を 0 に寄せた系列を再標本し、標本の平均が元の平均以上になる割合。n<2 は 1.0。"""
    a = _clean(r)
    if a.size < 2:
        return 1.0
    mu = float(a.mean())
    boots = stationary_bootstrap(a - mu, n_boot, mean_block, seed)
    return float(np.mean(boots.mean(axis=1) >= mu))


# ── 多重検定 ──

def _pvalues(pvalues) -> np.ndarray:
    p = np.asarray(pvalues, dtype=float).ravel()
    bad = np.isfinite(p) & ((p < 0.0) | (p > 1.0))
    if bad.any():
        raise ValueError("p 値は 0〜1 の範囲で渡す")
    return p


def holm(pvalues) -> list:
    """Holm の補正後 p（入力と同じ順）。NaN はそのまま NaN で、数 m に含めない。"""
    p = _pvalues(pvalues)
    out = np.full(p.shape, np.nan)
    ok = np.flatnonzero(np.isfinite(p))
    m = ok.size
    if m == 0:
        return out.tolist()
    order = ok[np.argsort(p[ok], kind="mergesort")]
    adj = np.minimum(1.0, (m - np.arange(m)) * p[order])
    out[order] = np.maximum.accumulate(adj)
    return out.tolist()


def benjamini_hochberg(pvalues) -> list:
    """Benjamini–Hochberg の補正後 p（q 値・入力と同じ順）。NaN はそのまま NaN で、数 m に含めない。"""
    p = _pvalues(pvalues)
    out = np.full(p.shape, np.nan)
    ok = np.flatnonzero(np.isfinite(p))
    m = ok.size
    if m == 0:
        return out.tolist()
    order = ok[np.argsort(p[ok], kind="mergesort")]
    adj = p[order] * m / np.arange(1, m + 1)
    adj = np.minimum.accumulate(adj[::-1])[::-1]
    out[order] = np.minimum(1.0, adj)
    return out.tolist()


# ── まとめ ──

def summary(r, periods: int = 252) -> dict:
    """主な指標を 1 つの dict に。hit_rate は収益が正の日の割合。"""
    a = _clean(r)
    return {
        "days": int(a.size),
        "ann_return": ann_return(a, periods),
        "ann_vol": ann_vol(a, periods),
        "sharpe": sharpe(a, periods),
        "sortino": sortino(a, periods),
        "max_drawdown": max_drawdown(a),
        "calmar": calmar(a, periods),
        "skew": skew(a),
        "kurtosis": kurtosis(a),
        "hit_rate": float(np.mean(a > 0.0)) if a.size else 0.0,
    }
