# ==============================================================
# dcf_valuation.py  v1.0  (6-3 / 財務1)
# FCF/WACC/ターミナル成長率から理論株価を算出 + 感応度分析
# ==============================================================
# 【役割】
#   screener 通過銘柄の「理論株価」を DCF（割引キャッシュフロー）法で
#   算出し、現在株価との乖離率から「割安度スコア」を生成する。
#
# 【DCF 計算式】
#   企業価値 = Σ (FCF_t / (1+WACC)^t) + TV / (1+WACC)^n
#   TerminalValue (TV) = FCF_n × (1+g) / (WACC - g)
#   理論株価 = (企業価値 - 純有利子負債) / 発行済株式数
#
# 【主要パラメータ】
#   FCF:  yfinance の cashflow（営業CF - 投資CF）直近3年平均
#   WACC: 推定式 = リスクフリーレート + β × リスクプレミアム（下限 DCF_WACC_FLOOR）
#         デフォルト: rf=1.5%（JP10Y相当）, rp=6.0%, β=yfinance の値を平均回帰で調整（0.67β+0.33）
#         日本株は低β・低金利で式の値が 6% 前後になり永続価値が膨らむため、下限 9% が実質の資本コストになる
#   FCF 成長: 業種別（DCF_FCF_GROWTH_BY_SECTOR・既定3%・上限 DCF_FCF_GROWTH_CAP）。予測期間中は一定
#   g:    ターミナル成長率（日本 1.0%・米国 2.0%。長期の名目成長率を超えない）
#   n:    予測期間（デフォルト5年）
#
# 【感応度分析（Sensitivity Analysis）】
#   WACC と g の組み合わせ（3×3=9通り）で理論株価の範囲を示す
#   → 「最悪でXX円、最良でXX円」という投資判断の幅を明示
#
# 【screener 統合】
#   dcf_discount = (理論株価 - 現在株価) / 現在株価 × 100
#   加点は対象全体（有効な乖離率を持つ銘柄）の中での順位で付ける（apply_rank_bonus）。
#     中央値の順位が 0pt、最上位が +DCF_RANK_BONUS_MAX、最下位が -DCF_RANK_BONUS_MIN（線形）。
#     ただし乖離率が正の銘柄だけが加点、負の銘柄だけが減点（中央値が負・正にずれても逆符号にしない）。
#     前提（資本コスト・成長率）が甘くて全銘柄が割安に出ても、上限に張り付く銘柄は上位の一部だけになる。
#   対象が DCF_RANK_MIN_POP 未満のときは乖離率の絶対値で付ける旧方式（calc_dcf_bonus）に戻す。
#   discount > 300%: モデル破綻とみなし無効（ボーナス0・順位の母集団にも入れない）
#
# 【DCF を適用しない銘柄】
#   銀行・証券・保険・不動産賃貸業は営業CFに預金・預り金・契約負債の増減が混ざり、
#   営業CF+capex が事業のFCFにならない。業種で除外し、業種が取れなくても
#   FCF/時価総額 > DCF_MAX_FCF_YIELD の銘柄は同じ理由で除外する。
#
# 【依存】yfinance / config.py
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── デフォルトパラメータ ──────────────────────────────────────
DEFAULT_RF_JP     = 0.015   # リスクフリーレート（JP10年債 ~1.5%）
DEFAULT_RF_US     = 0.045   # リスクフリーレート（US10年債 ~4.5%）
MARKET_RISK_PREM  = 0.06    # 市場リスクプレミアム（日米共通6.0%）
DEFAULT_BETA      = 1.0     # β取得失敗時のデフォルト
DEFAULT_GROWTH    = 0.02    # ターミナル成長率（2%）
FORECAST_YEARS    = 5       # FCF 予測期間
FCF_GROWTH_RATE   = 0.03    # 予測期間中のFCF成長率の既定（業種別の値が無いとき）
FETCH_DELAY       = 0.3
CACHE_FILENAME    = "dcf_valuation_cache.json"
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_DCF_VALUATION_CACHE_HOURS': 'CACHE_HOURS',
    'B5_DCF_VALUATION_FETCH_DELAY': 'FETCH_DELAY',
    'B6_DCF_VALUATION_DEFAULT_RF_JP': 'DEFAULT_RF_JP',
    'B6_DCF_VALUATION_DEFAULT_RF_US': 'DEFAULT_RF_US',
    'B6_DCF_VALUATION_MARKET_RISK_PREM': 'MARKET_RISK_PREM',
    'B6_DCF_VALUATION_DEFAULT_BETA': 'DEFAULT_BETA',
    'B6_DCF_VALUATION_DEFAULT_GROWTH': 'DEFAULT_GROWTH',
    'B6_DCF_VALUATION_FORECAST_YEARS': 'FORECAST_YEARS',
    'B6_DCF_VALUATION_FCF_GROWTH_RATE': 'FCF_GROWTH_RATE',
    'B6_DCF_VALUATION_WACC_RANGE': 'WACC_RANGE',
    'B6_DCF_VALUATION_GROWTH_RANGE': 'GROWTH_RANGE',
    'B6_DCF_VALUATION_DCF_LARGE_DISCOUNT': 'DCF_LARGE_DISCOUNT',
    'B6_DCF_VALUATION_DCF_SMALL_DISCOUNT': 'DCF_SMALL_DISCOUNT',
}
# B4〜B6 の接頭辞の規約に入らない評価の前提（config の BLOCK 78）。run_dcf_valuation の冒頭で取り込む
_CFG_VALUATION_MAP = {
    'DCF_WACC_FLOOR': 'DCF_WACC_FLOOR',
    'DCF_BETA_BLEND': 'DCF_BETA_BLEND',
    'DCF_TERMINAL_GROWTH_JP': 'DCF_TERMINAL_GROWTH_JP',
    'DCF_FCF_GROWTH_BY_SECTOR': 'DCF_FCF_GROWTH_BY_SECTOR',
    'DCF_FCF_GROWTH_CAP': 'DCF_FCF_GROWTH_CAP',
    'DCF_RANK_BONUS_MAX': 'DCF_RANK_BONUS_MAX',
    'DCF_RANK_BONUS_MIN': 'DCF_RANK_BONUS_MIN',
    'DCF_RANK_MIN_POP': 'DCF_RANK_MIN_POP',
}

CACHE_HOURS       = 24 * 7  # 週次（財務データは変化が緩やか）
CACHE_SCHEMA      = 3       # 古い版のキャッシュ（前提・ボーナスの方式が違うもの）は再計算する

# 株主資本コストの下限。beta が小さい銘柄で WACC が g に近づくと TV が発散する
DCF_WACC_FLOOR     = 0.09
# beta は平均回帰させて使う（観測 beta が低い銘柄の資本コストを過小にしない）: 調整beta = BLEND×beta + (1−BLEND)
DCF_BETA_BLEND     = 0.67
# ターミナル成長率（日本は名目成長率が低いので米国より低く置く）
DCF_TERMINAL_GROWTH_JP = 0.01
# 予測期間中の FCF 成長率（yfinance の sector 別）。上限は DCF_FCF_GROWTH_CAP
DCF_FCF_GROWTH_BY_SECTOR = {
    "Technology": 0.04, "Healthcare": 0.04, "Communication Services": 0.03,
    "Industrials": 0.03, "Consumer Cyclical": 0.03, "Basic Materials": 0.02,
    "Consumer Defensive": 0.02, "Energy": 0.02, "Utilities": 0.015, "Real Estate": 0.02,
}
DCF_FCF_GROWTH_CAP = 0.05
# 順位ボーナスの最大加点・最大減点（pt）と、順位方式を使う最小の母集団
DCF_RANK_BONUS_MAX = 8.0
DCF_RANK_BONUS_MIN = 6.0
DCF_RANK_MIN_POP   = 20
# FCF平均/時価総額がこれを超えると預金・預り金などの混入とみなし DCF を適用しない
DCF_MAX_FCF_YIELD  = 0.15
# 乖離率がこれを超える結果はモデルの破綻として無効にする
DCF_MAX_DISCOUNT   = 300.0
# ボーナスを計算する乖離率の上限（これ以上は頭打ち）
DCF_BONUS_CAP_DISCOUNT = 100.0
DCF_BONUS_MAX      = 8.0

# DCF の前提が成り立たない業種（yfinance sector / industry の語）
DCF_EXCLUDED_SECTORS = ("Financial Services",)
DCF_EXCLUDED_INDUSTRY_WORDS = (
    "Bank", "Insurance", "Capital Markets", "Credit Services",
    "Mortgage Finance", "Asset Management", "REIT",
)
# 東証33業種コード（銀行・証券・保険・その他金融）。業種コードが取れる場合に使う
DCF_EXCLUDED_JP_SECTOR_CODES = ("1028", "1029", "1030", "1031")

# 感応度分析のWACC/g グリッド
WACC_RANGE        = [-0.02, 0.0, +0.02]   # ベース ± 2%
GROWTH_RANGE      = [-0.01, 0.0, +0.01]  # ベース ± 1%

# screener ボーナス閾値
DCF_LARGE_DISCOUNT = 20.0    # 20%以上割安 → +8pt
DCF_SMALL_DISCOUNT = 10.0    # 10%以上割安 → +4pt
DCF_OVERVALUED     = -20.0   # 20%以上割高 → -6pt


# ==============================================================
# BLOCK 1: 財務データ取得
# ==============================================================

def _fetch_fcf_series(ticker: str) -> Optional[list]:
    """
    yfinance から直近3年分の FCF（フリーキャッシュフロー）を取得する。
    FCF = 営業CF - 設備投資（capex）

    Returns:
        [fcf_most_recent, fcf_2y_ago, fcf_3y_ago] または None
    """
    try:
        import yf_cache as yf
        t  = yf.Ticker(ticker)
        cf = t.cashflow   # 列=年、行=項目

        if cf is None or cf.empty:
            return None

        op_cf  = cf.loc["Operating Cash Flow"]  if "Operating Cash Flow"  in cf.index else None
        cap_ex = cf.loc["Capital Expenditure"]   if "Capital Expenditure"   in cf.index else None

        if op_cf is None or cap_ex is None:
            return None

        fcf_list = []
        for i in range(min(3, len(op_cf))):
            try:
                fcf = float(op_cf.iloc[i]) + float(cap_ex.iloc[i])  # capex is negative
                fcf_list.append(fcf)
            except (ValueError, TypeError):
                continue

        return fcf_list if fcf_list else None

    except Exception:
        return None


def _fetch_dcf_inputs(ticker: str) -> dict:
    """
    DCF 計算に必要な全パラメータを yfinance から取得する。

    Returns:
        {
          "fcf_list":         [float, ...],    # 直近3年FCF（新→旧）
          "fcf_avg":          float,           # 平均FCF
          "beta":             float,
          "shares_outstanding": float,
          "net_debt":         float,           # 純有利子負債（有利子負債 - 現金）
          "current_price":    float,
          "market_cap":       float,
          "currency":         str,             # "JPY" or "USD"
          "name":             str,
        }
    """
    try:
        import yf_cache as yf
        t    = yf.Ticker(ticker)
        info = t.info or {}

        name   = info.get("longName") or info.get("shortName") or ticker
        price  = info.get("regularMarketPrice") or info.get("currentPrice") or 0
        beta   = info.get("beta") or DEFAULT_BETA
        shares = info.get("sharesOutstanding") or 0
        mc     = info.get("marketCap") or 0
        sector   = info.get("sector") or ""
        industry = info.get("industry") or ""

        # 純有利子負債
        total_debt = info.get("totalDebt") or 0
        cash_equiv = info.get("totalCash") or 0
        net_debt   = float(total_debt) - float(cash_equiv)

        # 通貨判定
        currency   = "JPY" if ticker.endswith(".T") else "USD"

        fcf_list = _fetch_fcf_series(ticker)
        fcf_avg  = sum(fcf_list) / len(fcf_list) if fcf_list else None

        return {
            "fcf_list":           fcf_list or [],
            "fcf_avg":            fcf_avg,
            "beta":               float(beta) if beta else DEFAULT_BETA,
            "shares_outstanding": float(shares),
            "net_debt":           net_debt,
            "current_price":      float(price),
            "market_cap":         float(mc),
            "currency":           currency,
            "name":               name[:30],
            "sector":             sector,
            "industry":           industry,
        }
    except Exception:
        return {}


# ==============================================================
# BLOCK 2: WACC 計算
# ==============================================================

def calc_wacc(
    beta:        float,
    rf:          float,
    market_rp:   float = MARKET_RISK_PREM,
    floor:       Optional[float] = None,
) -> float:
    """
    CAPM ベースの WACC（株主資本コストのみ簡易版）を計算する。
    WACC = Rf + β × (Rm - Rf)

    注: 本実装では純粋に株主資本コスト（Ke）を WACC として使用。
    加重平均（負債コスト含む）はデータ要件が高いため簡略化。
    beta は DCF_BETA_BLEND で 1 側へ寄せてから使う。
    """
    b = DCF_BETA_BLEND * float(beta) + (1.0 - DCF_BETA_BLEND)
    ke = rf + b * market_rp
    # 下限を置くのは、低betaで WACC が g に迫ると TV が発散し理論株価が桁違いになるため
    lo = DCF_WACC_FLOOR if floor is None else float(floor)
    return round(max(lo, min(0.25, ke)), 4)


# ==============================================================
# BLOCK 3: DCF 計算
# ==============================================================

def calc_dcf(
    fcf_base:       float,
    wacc:           float,
    terminal_growth: float = DEFAULT_GROWTH,
    fcf_growth:     float = FCF_GROWTH_RATE,
    years:          int   = FORECAST_YEARS,
    shares:         float = 1.0,
    net_debt:       float = 0.0,
) -> dict:
    """
    DCF モデルで企業価値・理論株価を計算する。

    Args:
        fcf_base:        ベース FCF（直近平均）
        wacc:            加重平均資本コスト
        terminal_growth: ターミナル成長率
        fcf_growth:      予測期間中の FCF 成長率
        years:           予測期間（年）
        shares:          発行済株式数
        net_debt:        純有利子負債（企業価値から引く）

    Returns:
        {
          "pv_fcf":          float,   # 予測期間FCFの現在価値合計
          "terminal_value":  float,   # ターミナルバリュー
          "pv_terminal":     float,   # TVの現在価値
          "enterprise_value": float,
          "equity_value":    float,
          "intrinsic_per_share": float,
          "fcf_projections": [float, ...],
        }
    """
    if wacc <= terminal_growth:
        # WACC > g の制約（Gordon Growth Model の前提）
        terminal_growth = wacc - 0.01

    # 予測期間の FCF を計算
    fcf_projections = []
    pv_fcf = 0.0
    for t in range(1, years + 1):
        fcf_t  = fcf_base * (1 + fcf_growth) ** t
        pv_t   = fcf_t / (1 + wacc) ** t
        fcf_projections.append(round(fcf_t, 0))
        pv_fcf += pv_t

    # ターミナルバリュー
    fcf_final      = fcf_base * (1 + fcf_growth) ** years
    tv             = fcf_final * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal    = tv / (1 + wacc) ** years
    enterprise_val = pv_fcf + pv_terminal
    equity_val     = enterprise_val - net_debt

    if shares > 0 and equity_val > 0:
        intrinsic = round(equity_val / shares, 2)
    else:
        intrinsic = None

    return {
        "pv_fcf":              round(pv_fcf, 0),
        "terminal_value":      round(tv, 0),
        "pv_terminal":         round(pv_terminal, 0),
        "enterprise_value":    round(enterprise_val, 0),
        "equity_value":        round(equity_val, 0),
        "intrinsic_per_share": intrinsic,
        "fcf_projections":     fcf_projections,
        "tv_ratio":            round(pv_terminal / enterprise_val * 100, 1) if enterprise_val > 0 else None,
    }


# ==============================================================
# BLOCK 4: 感応度分析
# ==============================================================

def calc_sensitivity(
    fcf_base:        float,
    base_wacc:       float,
    base_growth:     float,
    shares:          float,
    net_debt:        float,
    wacc_deltas:     list = WACC_RANGE,
    growth_deltas:   list = GROWTH_RANGE,
    fcf_growth:      float = None,
) -> dict:
    """
    WACC と ターミナル成長率の組み合わせ（3×3=9通り）で
    理論株価の感応度分析を行う。

    Returns:
        {
          "matrix": {
            "wacc_labels":   ["5.0%", "7.0%", "9.0%"],
            "growth_labels": ["1.0%", "2.0%", "3.0%"],
            "prices":        [[price_wh_gl, ...], ...],  # [wacc][growth]
          },
          "min_price": float,
          "max_price": float,
          "base_price": float,
        }
    """
    wacc_vals   = [round(base_wacc + d, 4) for d in wacc_deltas]
    growth_vals = [round(base_growth + d, 4) for d in growth_deltas]

    prices = []
    for w in wacc_vals:
        row = []
        for g in growth_vals:
            r = calc_dcf(fcf_base, w, g, FCF_GROWTH_RATE if fcf_growth is None else fcf_growth,
                         shares=shares, net_debt=net_debt)
            row.append(r.get("intrinsic_per_share"))
        prices.append(row)

    all_prices = [p for row in prices for p in row if p is not None]

    return {
        "matrix": {
            "wacc_labels":   [f"{w*100:.1f}%" for w in wacc_vals],
            "growth_labels": [f"{g*100:.1f}%" for g in growth_vals],
            "prices":        prices,
        },
        "min_price":  min(all_prices) if all_prices else None,
        "max_price":  max(all_prices) if all_prices else None,
        "base_price": prices[1][1] if len(prices) > 1 and len(prices[1]) > 1 else None,
    }


# ==============================================================
# BLOCK 5: screener ボーナス計算
# ==============================================================

def calc_dcf_discount(intrinsic: float, current: float) -> Optional[float]:
    """理論株価と現在株価の乖離率（%）を計算する。正=割安、負=割高。"""
    if not intrinsic or not current or current <= 0:
        return None
    return round((intrinsic - current) / current * 100, 2)


def is_valid_discount(discount_pct: Optional[float]) -> bool:
    """乖離率が算出でき、かつモデル破綻でない（<= DCF_MAX_DISCOUNT）か。"""
    return discount_pct is not None and discount_pct <= DCF_MAX_DISCOUNT


def calc_dcf_bonus(discount_pct: Optional[float]) -> float:
    """value_score への加算ボーナスを計算する。

    乖離率が大きいほど段階的に加点する（一律加点だと FCF の誤差で全銘柄が
    満点になり順位がつかない）。無効な乖離率（None / 300%超）は 0。
      10%→+1 … 20%→+2 … 100%以上→+8（20〜100%は線形）
    """
    if not is_valid_discount(discount_pct):
        return 0.0
    if discount_pct <= DCF_OVERVALUED:
        return -6.0
    if discount_pct >= DCF_LARGE_DISCOUNT:
        d = min(discount_pct, DCF_BONUS_CAP_DISCOUNT)
        span = max(DCF_BONUS_CAP_DISCOUNT - DCF_LARGE_DISCOUNT, 1e-9)
        return round(2.0 + (d - DCF_LARGE_DISCOUNT) * (DCF_BONUS_MAX - 2.0) / span, 2)
    if discount_pct >= DCF_SMALL_DISCOUNT:
        span = max(DCF_LARGE_DISCOUNT - DCF_SMALL_DISCOUNT, 1e-9)
        return round(1.0 + (discount_pct - DCF_SMALL_DISCOUNT) / span, 2)
    return 0.0


def fcf_growth_for(sector: str) -> float:
    """業種（yfinance sector）ごとの予測期間の FCF 成長率。未登録は既定値。上限 DCF_FCF_GROWTH_CAP。"""
    g = DCF_FCF_GROWTH_BY_SECTOR.get(str(sector or ""), FCF_GROWTH_RATE)
    return min(float(g), DCF_FCF_GROWTH_CAP)


def terminal_growth_for(ticker: str) -> float:
    """ターミナル成長率。日本株は低く、米国は DEFAULT_GROWTH。"""
    return DCF_TERMINAL_GROWTH_JP if str(ticker).endswith(".T") else DEFAULT_GROWTH


def apply_rank_bonus(results: dict) -> dict:
    """有効な乖離率を持つ銘柄の中での順位から dcf_bonus を付け直す（results を書き換えて返す）。

    順位 pct（0=最下位〜1=最上位・同値は平均順位）から c=(pct−0.5)×2 を作り、
    c>0 かつ乖離率>0 なら c×DCF_RANK_BONUS_MAX、c<0 かつ乖離率<0 なら c×DCF_RANK_BONUS_MIN（負）、それ以外は 0。
    各銘柄に dcf_rank_pct（0〜100）と dcf_bonus_basis（"rank"|"absolute"|"none"）を残す。
    母集団が DCF_RANK_MIN_POP 未満なら絶対値方式（calc_dcf_bonus）。
    """
    pop = [(t, d["discount_pct"]) for t, d in results.items()
           if isinstance(d, dict) and not d.get("dcf_skip") and is_valid_discount(d.get("discount_pct"))]
    n = len(pop)
    for d in results.values():
        if isinstance(d, dict):
            d.pop("dcf_rank_pct", None)
            d["dcf_bonus"] = 0.0
            d["dcf_bonus_basis"] = "none"
    if n == 0:
        return results
    if n < DCF_RANK_MIN_POP:
        for t, disc in pop:
            results[t]["dcf_bonus"] = calc_dcf_bonus(disc)
            results[t]["dcf_bonus_basis"] = "absolute"
        return results
    srt = sorted(pop, key=lambda kv: kv[1])
    i = 0
    while i < n:
        j = i
        while j + 1 < n and srt[j + 1][1] == srt[i][1]:
            j += 1
        pct = ((i + j) / 2.0) / (n - 1)
        for k in range(i, j + 1):
            t, disc = srt[k]
            c = (pct - 0.5) * 2.0
            if c > 0 and disc > 0:
                bonus = c * DCF_RANK_BONUS_MAX
            elif c < 0 and disc < 0:
                bonus = c * DCF_RANK_BONUS_MIN
            else:
                bonus = 0.0
            results[t]["dcf_bonus"] = round(bonus, 2)
            results[t]["dcf_rank_pct"] = round(pct * 100.0, 1)
            results[t]["dcf_bonus_basis"] = "rank"
        i = j + 1
    return results


def dcf_skip_reason(inputs: dict) -> Optional[str]:
    """DCF を適用しない理由を返す（適用してよければ None）。"""
    sector   = str(inputs.get("sector") or "")
    industry = str(inputs.get("industry") or "")
    code     = str(inputs.get("sector_code") or "")
    if code and code in DCF_EXCLUDED_JP_SECTOR_CODES:
        return f"金融業種コード{code}"
    if sector in DCF_EXCLUDED_SECTORS:
        return f"金融業({sector})"
    if any(w in industry for w in DCF_EXCLUDED_INDUSTRY_WORDS):
        return f"金融・賃貸業({industry})"
    fcf = inputs.get("fcf_avg")
    mc  = inputs.get("market_cap")
    if fcf and mc and mc > 0 and fcf / mc > DCF_MAX_FCF_YIELD:
        return f"FCF/時価総額{fcf / mc * 100:.0f}%が異常に高い"
    return None


def get_dcf_bonus(ticker: str, dcf_cache: dict) -> float:
    """stock_screener.py から呼び出すインターフェース。"""
    return float(dcf_cache.get(ticker, {}).get("dcf_bonus", 0.0))


# ==============================================================
# BLOCK 6: レポート生成
# ==============================================================

def format_dcf_md(ticker: str, result: dict) -> str:
    """1銘柄のDCFレポートをMarkdown形式で生成する。"""
    name    = result.get("name", ticker)
    inputs  = result.get("inputs", {})
    dcf     = result.get("dcf", {})
    sens    = result.get("sensitivity", {})
    price   = inputs.get("current_price", 0)
    intrinsic = dcf.get("intrinsic_per_share")
    discount  = result.get("discount_pct")
    wacc    = result.get("wacc", 0)
    g       = result.get("terminal_growth", DEFAULT_GROWTH)
    fcf_avg = inputs.get("fcf_avg")

    disc_icon = ("🟢 割安" if discount and discount >= DCF_SMALL_DISCOUNT
                 else "🔴 割高" if discount and discount <= DCF_OVERVALUED
                 else "🟡 適正")

    lines = [
        f"## 📐 {name} ({ticker}) DCF分析",
        "",
        f"| 項目 | 値 |",
        f"|------|---|",
        f"| 現在株価 | {price:,.0f}円 |",
        f"| 理論株価（DCF） | {intrinsic:,.0f}円 |" if intrinsic else "| 理論株価 | 計算不可 |",
        f"| 割引率（discount） | {discount:+.1f}% {disc_icon} |" if discount else "| 割引率 | N/A |",
        f"| WACC | {wacc*100:.1f}% |",
        f"| ターミナル成長率 | {g*100:.1f}% |",
        f"| 平均FCF | {fcf_avg/1e8:,.1f}億円 |" if fcf_avg else "| 平均FCF | N/A |",
        f"| β（ベータ） | {inputs.get('beta', DEFAULT_BETA):.2f} |",
        "",
    ]

    # 感応度分析マトリクス
    matrix = sens.get("matrix", {})
    if matrix.get("prices"):
        lines += [
            "### 📊 感応度分析（理論株価）",
            "",
            f"| WACC＼成長率 | " + " | ".join(matrix.get("growth_labels", [])) + " |",
            "|" + "---|" * (len(matrix.get("growth_labels", [])) + 1),
        ]
        for i, w_label in enumerate(matrix.get("wacc_labels", [])):
            row_prices = matrix["prices"][i]
            bold_start = "**" if i == 1 else ""
            bold_end   = "**" if i == 1 else ""
            row = f"| {bold_start}{w_label}{bold_end} | "
            row += " | ".join(
                f"{bold_start}{p:,.0f}円{bold_end}" if p else "—"
                for p in row_prices
            ) + " |"
            lines.append(row)
        min_p = sens.get("min_price")
        max_p = sens.get("max_price")
        if min_p and max_p:
            lines.append(f"\n> 理論株価レンジ: **{min_p:,.0f}〜{max_p:,.0f}円**")

    lines += [
        "",
        "> ⚠️ DCF は将来の不確実性を含むモデルです。感応度分析の範囲を参考に総合判断してください。",
    ]
    return "\n".join(lines)


# ==============================================================
# BLOCK 7: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME

def _load_cache(cfg) -> dict:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if datetime.now() - datetime.fromisoformat(data.get("cached_at", "2000-01-01")) \
                < timedelta(hours=CACHE_HOURS):
            return {t: r for t, r in data.get("results", {}).items()
                    if isinstance(r, dict) and r.get("schema") == CACHE_SCHEMA}
    except Exception:
        pass
    return {}

def _save_cache(cfg, results: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"cached_at": datetime.now().isoformat(),
                                  "results": results}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 8: メインエントリポイント
# ==============================================================

def run_dcf_valuation(cfg, tickers: list) -> dict:
    """
    screener 通過銘柄の DCF 理論株価を一括計算する。

    stock_analyzer_main.py の screener ブロック後に呼び出す:
        _dcf = _load_local_module("dcf_valuation", ...)
        dcf_result = _dcf.run_dcf_valuation(cfg, screener_tickers)
        results["dcf_valuation"] = dcf_result

    Returns:
        {"results": {ticker: dcf_result}, "top_undervalued": [...], "timestamp": str}
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    sync_module_constants(globals(), cfg, _CFG_VALUATION_MAP)
    print(f"\n  📐 DCF理論株価計算開始（{len(tickers)}銘柄）...")

    wacc_floor = float(getattr(cfg, "DCF_WACC_FLOOR", DCF_WACC_FLOOR))
    cached   = _load_cache(cfg)
    to_fetch = [t for t in tickers if t not in cached]
    results  = dict(cached)

    for ticker in to_fetch:
        inputs = _fetch_dcf_inputs(ticker)
        if not inputs or not inputs.get("fcf_avg") or inputs["fcf_avg"] <= 0:
            # FCFがマイナス or 取得不能は除外（DCF不適用）
            continue

        skip = dcf_skip_reason(inputs)
        if skip:
            # 理由つきで保存し、毎回取り直さない。bonus 0 で割安リストには載らない
            results[ticker] = {
                "ticker": ticker, "name": inputs.get("name", ticker),
                "inputs": inputs, "dcf": {}, "sensitivity": {},
                "discount_pct": None, "dcf_bonus": 0.0,
                "dcf_skip": skip, "schema": CACHE_SCHEMA,
            }
            time.sleep(FETCH_DELAY)
            continue

        # WACC 計算
        rf   = DEFAULT_RF_JP if ticker.endswith(".T") else DEFAULT_RF_US
        wacc = calc_wacc(inputs.get("beta", DEFAULT_BETA), rf, floor=wacc_floor)
        g_fcf  = fcf_growth_for(inputs.get("sector"))
        g_term = terminal_growth_for(ticker)

        # DCF 計算
        dcf = calc_dcf(
            fcf_base        = inputs["fcf_avg"],
            wacc            = wacc,
            terminal_growth = g_term,
            fcf_growth      = g_fcf,
            shares          = inputs.get("shares_outstanding", 1),
            net_debt        = inputs.get("net_debt", 0),
        )

        # 感応度分析
        sens = calc_sensitivity(
            fcf_base     = inputs["fcf_avg"],
            base_wacc    = wacc,
            base_growth  = g_term,
            shares       = inputs.get("shares_outstanding", 1),
            net_debt     = inputs.get("net_debt", 0),
            fcf_growth   = g_fcf,
        )

        # 割引率とボーナス
        intrinsic = dcf.get("intrinsic_per_share")
        discount  = calc_dcf_discount(intrinsic, inputs.get("current_price"))
        invalid   = discount is not None and not is_valid_discount(discount)

        entry = {
            "ticker":          ticker,
            "name":            inputs.get("name", ticker),
            "inputs":          inputs,
            "dcf":             dcf,
            "sensitivity":     sens,
            "wacc":            wacc,
            "terminal_growth": g_term,
            "fcf_growth":      g_fcf,
            "discount_pct":    discount,
            "dcf_bonus":       0.0,
            "dcf_invalid":     invalid,
            "schema":          CACHE_SCHEMA,
        }
        results[ticker] = entry

        if discount is not None and not invalid and abs(discount) >= 10:
            icon = "🟢" if discount > 0 else "🔴"
            print(f"      {ticker}: 理論株価{intrinsic:,.0f} 現在{inputs['current_price']:,.0f} "
                  f"{icon}{discount:+.1f}%")
        time.sleep(FETCH_DELAY)

    apply_rank_bonus(results)
    _save_cache(cfg, results)

    top_undervalued = sorted(
        [{"ticker": t, **d} for t, d in results.items()
         if is_valid_discount(d.get("discount_pct"))
         and d["discount_pct"] >= DCF_SMALL_DISCOUNT],
        key=lambda x: -x["discount_pct"])[:5]

    n_under = len(top_undervalued)
    n_over  = sum(1 for d in results.values()
                  if is_valid_discount(d.get("discount_pct"))
                  and d["discount_pct"] <= DCF_OVERVALUED)

    print(f"    ✅ DCF完了: {len(results)}銘柄 / "
          f"割安{n_under}件 / 割高{n_over}件")

    return {
        "results":        results,
        "top_undervalued": top_undervalued,
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
