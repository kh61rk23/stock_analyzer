# ==============================================================
# candidate_kpi.py  (2026-09-29)
# スクリーナー候補に「審議KPI 9項目」を毎日付ける（表示専用・点数には使わない）
# --------------------------------------------------------------
# 定義は投資部の _運用ルール.md §8 に揃える（①だけ健太の指定で PBR）。
#   ① PBR            時価総額 ÷ 株主資本（yfinance 値があればそれ）
#   ② PER            yfinance の実績PER。併記: 時価総額 ÷ 直近期純利益
#   ③ FCF Yield      (営業CF − 設備投資) ÷ 時価総額。直近期と5期平均
#   ④ EV/EBITDA      EV = 時価総額 + 有利子負債 − 現金等、EBITDA = 営業利益 + 減価償却費
#   ⑤ ROIC           営業利益 × (1 − 0.306) ÷ (株主資本 + 有利子負債)。直近期と5期平均
#   ⑥ ネットキャッシュ 現金等 − 有利子負債（時価総額比）
#   ⑦ 本業のストック性 5期の減収年数・減益年数・営業赤字年数・売上成長率の標準偏差（代理指標）
#   ⑧ 投資先のEarnings Yield  持株会社の会社開示を転記する項目＝自動では出さない
#   ⑨ 投資資本IRR     CF0 = −純投下資本(期首)、CFt = NOPAT − Δ純投下資本、
#                     最終期に純投下資本を簿価で回収。純投下資本 = 株主資本 + 有利子負債 − 現金等
#
# 多年度の値は BQ edinet.financials（EdinetDB 由来・最大5期）から、時価総額・
# 直近の現金/有利子負債は yfinance（候補が持っている値）から取る。
# BQ の有利子負債は約16%の行しか埋まっていない（2026-09-29 実測 328/2072）ため、
# 無い年は yfinance の直近値で近似し notes に「負債は直近値で近似」と残す。
# ==============================================================
from __future__ import annotations

import statistics

TAX = 0.306
_FIELDS = ("fiscal_year", "net_sales", "operating_income", "net_income", "equity",
           "interest_bearing_debt", "cash_and_equiv", "operating_cf", "capex",
           "depreciation")


def _n(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _div(a, b):
    a, b = _n(a), _n(b)
    return None if a is None or b in (None, 0) else a / b


def irr(cashflows: list[float]) -> float | None:
    """二分法で IRR を求める（符号変化が無ければ None）。"""
    if len(cashflows) < 2 or all(c >= 0 for c in cashflows) or all(c <= 0 for c in cashflows):
        return None

    def npv(r):
        return sum(c / (1 + r) ** t for t, c in enumerate(cashflows))
    lo, hi = -0.95, 1.5
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def compute_kpi(cand: dict, rows: list[dict]) -> dict:
    """1銘柄の9指標（純関数）。rows は BQ financials の年次行（順不同）。"""
    rows = sorted([r for r in rows if _n(r.get("fiscal_year")) is not None],
                  key=lambda r: r["fiscal_year"])
    notes: list[str] = []
    mcap = _n(cand.get("market_cap_raw")) or (
        _n(cand.get("market_cap_b")) * 1e8 if _n(cand.get("market_cap_b")) else None)
    L = rows[-1] if rows else {}
    yf_debt, yf_cash = _n(cand.get("total_debt")), _n(cand.get("total_cash"))

    def debt_of(r):
        d = _n(r.get("interest_bearing_debt"))
        if d is None and yf_debt is not None:
            if "負債は直近値で近似" not in notes:
                notes.append("負債は直近値で近似")
            return yf_debt
        return d

    def cash_of(r):
        c = _n(r.get("cash_and_equiv"))
        return c if c is not None else (yf_cash if r is L else None)

    k: dict = {"fiscal_year": L.get("fiscal_year"), "years": len(rows)}
    # ① PBR ② PER
    k["pbr"] = _n(cand.get("pbr")) or _div(mcap, L.get("equity"))
    k["per"] = _n(cand.get("per"))
    ni = _n(L.get("net_income"))
    k["per_actual"] = _div(mcap, ni) if ni and ni > 0 else None
    # ③ FCF Yield
    fcfs = [(_n(r.get("operating_cf")) or 0) - (_n(r.get("capex")) or 0)
            for r in rows if _n(r.get("operating_cf")) is not None and _n(r.get("capex")) is not None]
    k["fcf_yield"] = _div(fcfs[-1], mcap) if fcfs else None
    k["fcf_yield_avg"] = _div(sum(fcfs) / len(fcfs), mcap) if fcfs else None
    # ④ EV/EBITDA ⑥ ネットキャッシュ
    debt, cash = (debt_of(L), cash_of(L)) if L else (yf_debt, yf_cash)
    oi, dep = _n(L.get("operating_income")), _n(L.get("depreciation"))
    ebitda = oi + dep if oi is not None and dep is not None else None
    ev = mcap + debt - cash if None not in (mcap, debt, cash) else None
    k["ev_ebitda"] = _div(ev, ebitda) if ebitda and ebitda > 0 else None
    k["net_cash"] = cash - debt if None not in (cash, debt) else None
    k["net_cash_ratio"] = _div(k["net_cash"], mcap)
    # ⑤ ROIC
    roics = []
    for r in rows:
        o, e, d = _n(r.get("operating_income")), _n(r.get("equity")), debt_of(r)
        if o is not None and e is not None and d is not None and e + d > 0:
            roics.append(o * (1 - TAX) / (e + d))
    k["roic"] = roics[-1] if roics else None
    k["roic_avg"] = sum(roics) / len(roics) if roics else None
    # ⑦ ストック性（代理指標）
    sales = [_n(r.get("net_sales")) for r in rows]
    ois = [_n(r.get("operating_income")) for r in rows]
    k["down_sales_years"] = sum(1 for a, b in zip(sales, sales[1:]) if a and b is not None and b < a)
    k["down_oi_years"] = sum(1 for a, b in zip(ois, ois[1:]) if a is not None and b is not None and b < a)
    k["op_loss_years"] = sum(1 for o in ois if o is not None and o < 0)
    growth = [(b - a) / a for a, b in zip(sales, sales[1:]) if a and b is not None and a > 0]
    k["sales_growth_sd"] = statistics.pstdev(growth) if len(growth) >= 2 else None
    # ⑧ 投資先のEarnings Yield：会社開示の転記項目（自動では出さない）
    k["investee_ey"] = None
    # ⑨ 投資資本IRR
    ics, nopats = [], []
    for r in rows:
        e, d, c = _n(r.get("equity")), debt_of(r), _n(r.get("cash_and_equiv"))
        o = _n(r.get("operating_income"))
        if None in (e, d, c, o):
            ics, nopats = [], []   # 欠けた年で途切れたら以降だけで計算
            continue
        ics.append(e + d - c)
        nopats.append(o * (1 - TAX))
    k["irr"] = None
    if len(ics) >= 3 and ics[0] > 0:
        cf = [-ics[0]] + [nopats[t] - (ics[t] - ics[t - 1]) for t in range(1, len(ics))]
        cf[-1] += ics[-1]
        k["irr"] = irr(cf)
    if not rows:
        notes.append("BQに財務なし（yfinance値のみ）")
    k["notes"] = notes
    return {key: (round(v, 4) if isinstance(v, float) else v) for key, v in k.items()}


def _load_financials(codes: list[str]) -> dict:
    """{4桁コード: [行...]} を1クエリで取る。"""
    from edinet_bq_client import EDINETBQClient
    from google.cloud import bigquery
    bq = EDINETBQClient()
    sql = f"""
    SELECT company_code, {", ".join(_FIELDS)}
    FROM (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY company_code, fiscal_year
                                   ORDER BY updated_at DESC) AS rn
      FROM `{bq.dataset}.financials`
      WHERE company_code IN UNNEST(@codes) AND fiscal_year IS NOT NULL
    ) WHERE rn = 1
    """
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("codes", "STRING", codes)])
    out: dict = {}
    for row in bq.client.query(sql, job_config=cfg).result():
        out.setdefault(str(row["company_code"])[:4], []).append(dict(row.items()))
    return out


def attach_kpis(cfg, results: dict) -> dict:
    """results["screener"] の候補に c["kpi"] を付ける。戻り値は件数の要約。"""
    sc = (results or {}).get("screener") or {}
    cands = [c for c in (sc.get("candidates") or []) if isinstance(c, dict)
             and str(c.get("ticker", "")).endswith(".T")]
    if not cands or not getattr(cfg, "ENABLE_CANDIDATE_KPI", True):
        return {"skipped": True, "count": 0}
    codes = sorted({c["ticker"].split(".")[0][:4] for c in cands})
    try:
        fin = _load_financials(codes)
    except Exception as e:
        print(f"  ⚠️  [candidate_kpi] BQ財務の取得失敗（yfinance値のみで計算）: {str(e)[:80]}")
        fin = {}
    with_bq = 0
    for c in cands:
        rows = fin.get(c["ticker"].split(".")[0][:4], [])
        with_bq += bool(rows)
        c["kpi"] = compute_kpi(c, rows)
    print(f"  📐 [candidate_kpi] 9指標: {len(cands)}銘柄（BQ財務あり {with_bq}）")
    return {"skipped": False, "count": len(cands), "with_bq": with_bq}
