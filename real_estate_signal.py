"""企業保有不動産の含み益・エリアリスクと、銘柄単位の不動産ボーナスへの換算（純関数）。

銘柄単位のボーナスは stock_bonus が BI マート（bi.m_screener）の修正PBR・資産バリュー倍率から出す
（読み込みは stock_real_estate.py）。summarize_holdings は施設行から同じ定義で含み益を出すもので、
マートの定義（bi/marts/m_re_holdings.sql・m_screener.sql）と一致させておく。
  - 時価の目安＝土地面積 × 参照地価（工業地→商業地→住宅地の順で最初にある値。円/m2）。
  - 含み益＝土地簿価がある施設だけの（時価の目安 − 土地簿価）の合計。簿価が無い施設は比べられないので入れない。
  - 土地簿価がある施設で時価の目安が簿価の EXCLUDE_MULTIPLE 倍を超えるものは、本社欄に会社全体の面積が
    入っているなどの紐づけ違いとみなして時価・含み益から除く（出光興産の本社欄で 22兆円になる例がある）。
  - 時価の目安には、参照地価の種別×地域（三大都市圏かどうか）の補正係数（config.RE_EST_CALIBRATION）を掛ける。
    簿価20倍超の除外は補正前の値で判定する（紐づけ違いは数十倍の誤差なので、1未満の係数で境目の内側へ戻すと
    本社欄に全社面積が入った施設のような疑わしい行が兆円単位の含み益で混ざる）。マートの cal CTE と同じ値。
  - 含み益が時価総額の3倍を超える会社は、マート側で単位違い・再取得の疑いの印（gt_3x_mcap）が付く。
  - 開示された時価（賃貸等不動産・IFRS の投資不動産）がある会社は、推定の含み益のうち賃貸分を開示の含み益に
    置き換える（blend_disclosed_gain。マートでは m_screener の re CTE）。
金額の単位は、保有不動産（BQ契約）が百万円。
"""

from __future__ import annotations

import math

EXCLUDE_MULTIPLE = 20.0
_PRICE_KEYS = ("industrial_land_price_per_m2", "commercial_land_price_per_m2",
               "residential_land_price_per_m2")
_PRICE_KIND = {"industrial_land_price_per_m2": "industrial", "commercial_land_price_per_m2": "commercial",
               "residential_land_price_per_m2": "residential"}
# 三大都市圏（首都圏・中京圏・近畿圏）の都府県コード。市区町村コードの先頭2桁で判定する。マートと同じ並び
# 開示時価÷簿価がこの範囲外の行は抽出誤り・単位違いの疑いとして使わない（EdinetDB 側の除外と同じ範囲）。マートと同じ値
FV_BV_RANGE = (0.1, 30.0)
METRO_PREF_CODES = ("11", "12", "13", "14", "21", "23", "24", "26", "27", "28", "29")


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
    return _ref_price_kind(row)[0]


def _ref_price_kind(row: dict) -> tuple[float | None, str | None]:
    """参照地価と種別（industrial / commercial / residential）。マートの COALESCE と同じ順。"""
    for key in _PRICE_KEYS:
        v = _number(row.get(key))
        if v is not None and v > 0:
            return v, _PRICE_KIND[key]
    return None, None


def region_of(municipality_code) -> str:
    """metro（三大都市圏の都府県）／ other。"""
    return "metro" if str(municipality_code or "")[:2] in METRO_PREF_CODES else "other"


def calibration_factor(row: dict, calibration: dict | None) -> float:
    """施設行の推定時価に掛ける係数。表に無い層は1.0。"""
    _, kind = _ref_price_kind(row)
    if not calibration or kind is None:
        return 1.0
    k = _number((calibration or {}).get(f"{kind}|{region_of(row.get('municipality_code'))}"))
    return k if k is not None and k > 0 else 1.0


def summarize_holdings(holdings: list[dict], calibration: dict | None = None) -> dict:
    """施設行を会社単位にまとめる（金額は百万円）。calibration は config.RE_EST_CALIBRATION の形。

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
            est *= calibration_factor(row, calibration)
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

    含み益率＝含み益 ÷ 比べた施設の土地簿価。日次のスコアは stock_bonus を使い、これは施設行だけから
    目安を出したいとき（時価総額や財務が手元に無いとき）のためのもの。
    算出できる保有不動産が無ければ0.0。
    """
    s = summarize_holdings(holdings, getattr(cfg, "RE_EST_CALIBRATION", None))
    if not s["has_data"]:
        return 0.0
    hidden = 0.0
    if s["unrealized_gain_m_yen"] is not None and s["matched_book_land_m_yen"]:
        rate = s["unrealized_gain_m_yen"] / max(1.0, s["matched_book_land_m_yen"])
        hidden = max(0.0, min(3.0, math.tanh(rate) * 3.0))
    return _clip(cfg, hidden - s["area_risk"])


def blend_disclosed_gain(est_gain, book_total, rental_book, rental_fair) -> tuple[float | None, float | None, str | None]:
    """推定の含み益と開示の含み益（賃貸等不動産・IFRS 投資不動産）を合わせる（金額の単位はそろっていれば何でもよい）。

    推定は保有不動産（主要設備）の施設ごとの値で、どの施設が賃貸用かは分からない。そこで、賃貸分の簿価が
    保有不動産の簿価（土地＋建物）に占める割合 share だけ推定の含み益を減らし、開示の含み益（時価−簿価）を足す。
    賃貸分が主要設備に載っていない場合は share が大きめに出て推定を多めに外す（二重に数えない側に倒す）。
    保有不動産の簿価が無い・賃貸分の簿価のほうが大きい場合は share=1（推定を使わず開示だけ）。
    時価÷簿価が FV_BV_RANGE の外の開示は使わない。
    Returns: (含み益, share, basis)。basis は estimate / estimate+disclosed / disclosed、どちらも無ければ None。
    """
    est, bt = _number(est_gain), _number(book_total)
    rb, rf = _number(rental_book), _number(rental_fair)
    if rb is None or rf is None or rb <= 0 or rf <= 0 or not (FV_BV_RANGE[0] <= rf / rb <= FV_BV_RANGE[1]):
        return (est, None, "estimate") if est is not None else (None, None, None)
    share = min(1.0, rb / bt) if bt and bt > 0 else 1.0
    disclosed = rf - rb
    if est is None or share >= 1.0:
        return disclosed, share, "disclosed"
    return est * (1.0 - share) + disclosed, share, "estimate+disclosed"


def stock_bonus(adj_pbr, asset_value_ratio, unrealized_gain, flag, area_risk, cfg) -> tuple[float, str]:
    """銘柄単位の不動産ボーナス（``RE_BONUS_MIN..MAX``）と根拠の種類を返す。

    入力は BI マート bi.m_screener の値（adj_pbr・asset_value_ratio・含み益・gt_3x_mcap の印）と、
    保有不動産のエリアリスク（0..2）。マートが正本で、ここでは点数への換算だけをする。
    割安の加点（0..3）：
      - 含み益があり修正PBR が1未満なら最大 1.5（0.5以下で満点）。含み損の会社は PBR が低くても加点しない
        （不動産の効果ではないため）。
      - 資産バリュー倍率（ネットキャッシュ＋税引後含み益が時価総額の何倍か）で最大 1.5（1倍以上で満点）。
    エリアリスクの減点（−2..0）。
    basis: valuation（加点あり得る）／area_only（時価総額の3倍超の印か、修正PBR等が無い）／none（データなし）。
    """
    adj, avr, risk = _number(adj_pbr), _number(asset_value_ratio), _number(area_risk)
    risk = max(0.0, min(2.0, risk or 0.0))
    gain = _number(unrealized_gain)
    if flag is None and (adj is not None or avr is not None):
        value = 0.0
        if (gain or 0.0) > 0 and adj is not None and adj < 1.0:
            value += 1.5 * min(1.0, (1.0 - adj) / 0.5)
        if avr is not None:
            value += 1.5 * min(1.0, max(0.0, avr))
        return _clip(cfg, value - risk), "valuation"
    if flag is not None or risk > 0 or gain is not None:
        return _clip(cfg, -risk), "area_only"
    return 0.0, "none"
