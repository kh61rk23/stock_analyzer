"""企業保有不動産から含み益・修正PBR・資産バリュー倍率・エリアリスク補正を算出する純関数。

含み益の定義は BI マート（bi/marts/m_re_holdings.sql・m_screener.sql）と同じにする。
スコアとスクリーナー表示で同じ会社の含み益が食い違わないようにするため。
  - 時価の目安＝土地面積 × 参照地価（工業地→商業地→住宅地の順で最初にある値。円/m2）。
  - 含み益＝土地簿価がある施設だけの（時価の目安 − 土地簿価）の合計。簿価が無い施設は比べられないので入れない。
  - 土地簿価がある施設で時価の目安が簿価の EXCLUDE_MULTIPLE 倍を超えるものは、本社欄に会社全体の面積が
    入っているなどの紐づけ違いとみなして時価・含み益から除く（出光興産の本社欄で 22兆円になる例がある）。
  - 含み益が時価総額の GAIN_MCAP_CAP 倍を超える会社は単位違い・再取得の疑いとして、含み益を使う値を出さない。
  - 税効果は TAX_RATE（実効税率の目安）で差し引く。
金額の単位は、保有不動産（BQ契約）が百万円、時価総額・現金・負債（yfinance）が円。
"""

from __future__ import annotations

import math

EXCLUDE_MULTIPLE = 20.0
GAIN_MCAP_CAP = 3.0
TAX_RATE = 0.306
_PRICE_KEYS = ("industrial_land_price_per_m2", "commercial_land_price_per_m2",
               "residential_land_price_per_m2")


def _number(value) -> float | None:
    try:
        v = float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None
    return v if v is not None and math.isfinite(v) else None


def _fraction(value) -> float:
    """APIの百分率(例: 12.5)と比率(例: 0.125)を同じ比率へ揃える。"""
    number = _number(value)
    if number is None:
        return 0.0
    return number / 100.0 if abs(number) > 1.0 else number


def _ref_price(row: dict) -> float | None:
    for key in _PRICE_KEYS:
        v = _number(row.get(key))
        if v is not None and v > 0:
            return v
    return None


def summarize_holdings(holdings: list[dict]) -> dict:
    """施設行を会社単位にまとめる（金額は百万円）。

    Returns: land_value_est_m_yen / unrealized_gain_m_yen / matched_book_land_m_yen /
             excluded_count / area_risk（0..2・大きいほど悪い）/ has_data
    """
    land_value = 0.0
    gain = 0.0
    matched_book = 0.0
    has_value = has_gain = False
    excluded = 0
    weighted_risk = 0.0
    total_book = 0.0
    for row in holdings or []:
        if not isinstance(row, dict):
            continue
        area = _number(row.get("land_area_m2"))
        price = _ref_price(row)
        book_land = max(0.0, _number(row.get("book_value_land_m_yen")) or 0.0)
        book_bldg = max(0.0, _number(row.get("book_value_buildings_m_yen")) or 0.0)

        est = area * price / 1_000_000.0 if area and area > 0 and price else None
        if est is not None and book_land > 0 and est > EXCLUDE_MULTIPLE * book_land:
            excluded += 1
            est = None
        if est is not None:
            land_value += est
            has_value = True
            if book_land > 0:
                gain += est - book_land
                matched_book += book_land
                has_gain = True

        book_total = book_land + book_bldg
        if book_total > 0:
            flood = max(0.0, _fraction(row.get("flood_risk_area_pct")))
            pop = _number(row.get("population_change_pct"))
            decline = max(0.0, -pop) / 100.0 if pop is not None else 0.0
            weighted_risk += book_total * (flood + decline)
            total_book += book_total

    area_risk = max(0.0, min(2.0, weighted_risk / total_book)) if total_book > 0 else 0.0
    return {
        "land_value_est_m_yen": round(land_value, 3) if has_value else None,
        "unrealized_gain_m_yen": round(gain, 3) if has_gain else None,
        "matched_book_land_m_yen": round(matched_book, 3) if has_gain else None,
        "excluded_count": excluded,
        "area_risk": round(area_risk, 4),
        "has_data": has_value or total_book > 0,
    }


def _clip(cfg, value: float) -> float:
    minimum = float(getattr(cfg, "RE_BONUS_MIN", -2.0))
    maximum = float(getattr(cfg, "RE_BONUS_MAX", 3.0))
    return round(max(minimum, min(maximum, value)), 4)


def calc_real_estate_bonus(holdings: list[dict], cfg) -> float:
    """時価総額を使わない版（含み益率＋エリアリスク）。``RE_BONUS_MIN..MAX`` に収める。

    含み益率＝含み益 ÷ 比べた施設の土地簿価。銘柄の時価総額が無いときの代わりに使う。
    算出できる保有不動産が無ければ0.0。
    """
    s = summarize_holdings(holdings)
    if not s["has_data"]:
        return 0.0
    hidden = 0.0
    if s["unrealized_gain_m_yen"] is not None and s["matched_book_land_m_yen"]:
        rate = s["unrealized_gain_m_yen"] / max(1.0, s["matched_book_land_m_yen"])
        hidden = max(0.0, min(3.0, math.tanh(rate) * 3.0))
    return _clip(cfg, hidden - s["area_risk"])


def stock_valuation(summary: dict, market_cap, pbr, total_cash=None, total_debt=None) -> dict:
    """含み益を時価総額・PBR と合わせて銘柄の割安さに直す（m_screener と同じ式）。

    adj_pbr           = 時価総額 ÷（純資産 ＋ 税引後含み益）。純資産＝時価総額 ÷ PBR。
    asset_value_ratio = （現金 − 有利子負債 ＋ 税引後含み益）÷ 時価総額。
    """
    out = {"gain_to_mcap": None, "adj_pbr": None, "asset_value_ratio": None, "flag": None}
    mcap = _number(market_cap)
    gain_m = summary.get("unrealized_gain_m_yen")
    if not mcap or mcap <= 0 or gain_m is None:
        return out
    gain = gain_m * 1_000_000.0
    if gain > GAIN_MCAP_CAP * mcap:
        out["flag"] = "gt_3x_mcap"
        return out
    after_tax = gain * (1 - TAX_RATE)
    out["gain_to_mcap"] = round(gain / mcap, 4)
    p = _number(pbr)
    if p and p > 0:
        adj_equity = mcap / p + after_tax
        if adj_equity > 0:
            out["adj_pbr"] = round(mcap / adj_equity, 4)
    cash, debt = _number(total_cash), _number(total_debt)
    if cash is not None:
        out["asset_value_ratio"] = round((cash - (debt or 0.0) + after_tax) / mcap, 4)
    return out


def calc_stock_real_estate_bonus(holdings: list[dict], company: dict, cfg) -> dict:
    """銘柄単位の不動産ボーナス（``RE_BONUS_MIN..MAX``）と根拠の値を返す。

    割安の加点（0..3）：
      - 含み益があり修正PBR が1未満なら最大 1.5（0.5以下で満点）。含み損の会社は PBR が低くても加点しない
        （不動産の効果ではないため）。
      - 資産バリュー倍率（ネットキャッシュ＋税引後含み益が時価総額の何倍か）が最大 1.5（1倍以上で満点）。
    エリアリスクの減点（−2..0）：簿価で重みづけした洪水リスク割合＋2050年までの人口減少率。
    時価総額が無い銘柄は calc_real_estate_bonus（含み益率）で代える。
    """
    s = summarize_holdings(holdings)
    company = company or {}
    v = stock_valuation(s, company.get("market_cap"), company.get("pbr"),
                        company.get("total_cash"), company.get("total_debt"))
    out = {"bonus": 0.0, **v, "area_risk": s["area_risk"],
           "unrealized_gain_m_yen": s["unrealized_gain_m_yen"],
           "excluded_count": s["excluded_count"], "basis": "none"}
    if not s["has_data"]:
        return out
    if v["flag"] is None and (v["adj_pbr"] is not None or v["asset_value_ratio"] is not None):
        value = 0.0
        has_gain = (s["unrealized_gain_m_yen"] or 0.0) > 0
        if has_gain and v["adj_pbr"] is not None and v["adj_pbr"] < 1.0:
            value += 1.5 * min(1.0, (1.0 - v["adj_pbr"]) / 0.5)
        if v["asset_value_ratio"] is not None:
            value += 1.5 * min(1.0, max(0.0, v["asset_value_ratio"]))
        out["bonus"] = _clip(cfg, value - s["area_risk"])
        out["basis"] = "valuation"
    elif v["flag"] is not None:
        out["bonus"] = _clip(cfg, -s["area_risk"])
        out["basis"] = "area_only"
    else:
        out["bonus"] = calc_real_estate_bonus(holdings, cfg)
        out["basis"] = "gain_rate"
    return out
