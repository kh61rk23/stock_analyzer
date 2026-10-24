"""
us_facts_quarterly.py — us.company_facts（SEC の XBRL 全社分）から米国スクリーナーの XBRL 補完を作る

米国スクリーナー（us_stock_screener._fetch_one）が銘柄ごとに引く EPS の加速（eps_accel_score_xbrl）と
直近の FCF（latest_fcf）を、us.fundamentals（ローテーションで取った約800社）に無い銘柄も含めて全社分作る。

  ・EPS：希薄化 EPS（無ければ基本 EPS）の3か月の値を四半期とする。10-K は通期しか載せないので、
    Q4 は「通期 − その期の Q1〜Q3」で作る（株数が期中で変わるぶん少しずれる）。
    10-Q の累計（6か月・9か月）は使わない（累計を四半期と混ぜると前年比が崩れる）。
  ・FCF：営業 CF − 設備投資の直近12か月。CF は期首からの累計でしか出ないので、
    「直近の通期 ＋ 今期の累計 − 前年同期の累計」で作る。設備投資の項目を一度も出していない会社は 0 とみなす。

1回の実行で1本だけ読み（1銘柄ずつ引くと1本ごとに最低10MBを数える）、cik ごとに手元で計算して覚えておく。
スクリーナーは並列で引くので、最初の1回だけが読むようロックする。
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Optional

EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
OCF_TAGS = ("NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")
CAPEX_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")
ALL_TAGS = EPS_TAGS + OCF_TAGS + CAPEX_TAGS

# 期間の長さ（日）で四半期・累計・通期を見分ける
QUARTER = (80, 100)
YEAR = (350, 380)

FACTS_SQL = """
WITH tags AS (
  SELECT tag_id, tag FROM `{p}.us.fact_tags` WHERE taxonomy = 'us-gaap' AND tag IN UNNEST(@tags)
)
SELECT f.cik, t.tag, f.period_start s, f.period_end e, f.val v, f.filed
FROM `{p}.us.company_facts_v2` f JOIN tags t USING (tag_id)
WHERE f.period_end >= DATE_SUB(CURRENT_DATE(), INTERVAL @years YEAR)
  AND f.period_start IS NOT NULL AND f.val IS NOT NULL
  AND STARTS_WITH(IFNULL(f.form, ''), '10-')
  AND DATE_DIFF(f.period_end, f.period_start, DAY) BETWEEN 80 AND 380
QUALIFY ROW_NUMBER() OVER (PARTITION BY f.cik, t.tag, f.period_start, f.period_end ORDER BY f.filed DESC) = 1
"""
TICKER_SQL = "SELECT ticker, cik FROM `{p}.us.company_profile` WHERE ticker IS NOT NULL AND cik IS NOT NULL"


def _days(s: date, e: date) -> int:
    return (e - s).days


def _within(n: int, rng: tuple[int, int]) -> bool:
    return rng[0] <= n <= rng[1]


def _near(a: date, b: date, tol: int) -> bool:
    return abs((a - b).days) <= tol


# ── 純関数 ───────────────────────────────────────────────────

def quarterly_eps(facts: list[tuple[date, date, float]], n: int = 12) -> list[dict]:
    """[(開始, 終了, 値)] から四半期の EPS を新しい順に返す（[{"period": "YYYY-MM-DD", "val": float}]）。
    3か月の値をそのまま使い、無い期末は通期 − 同じ期の3四半期で埋める。"""
    q = {e: (s, v) for s, e, v in facts if _within(_days(s, e), QUARTER)}
    for s, e, v in facts:
        if not _within(_days(s, e), YEAR) or any(_near(e, qe, 5) for qe in q):
            continue
        inside = [qv for qe, (qs, qv) in q.items() if qs >= s - timedelta(days=5) and qe <= e - timedelta(days=20)]
        if len(inside) == 3:
            q[e] = (e - timedelta(days=91), round(v - sum(inside), 4))
    return [{"period": e.isoformat(), "val": float(v)} for e, (_s, v) in sorted(q.items(), reverse=True)[:n]]


def ttm(facts: list[tuple[date, date, float]]) -> Optional[tuple[date, float]]:
    """期首からの累計の値 [(開始, 終了, 値)] から、直近の期末と直近12か月の値。作れなければ None。"""
    if not facts:
        return None
    last = max(e for _s, e, _v in facts)
    at_last = [(s, v) for s, e, v in facts if e == last]
    for s, v in at_last:
        if _within(_days(s, last), YEAR):
            return last, v
    # 今期の累計（期首 s）＋ 前の通期（s の前日に終わる）－ 前年同期の累計
    for s, v in sorted(at_last, key=lambda x: x[0]):
        fy = [fv for fs, fe, fv in facts if _within(_days(fs, fe), YEAR) and _near(fe, s - timedelta(days=1), 7)]
        prior = [pv for ps, pe, pv in facts
                 if _near(pe, last - timedelta(days=365), 10) and _near(ps, s - timedelta(days=365), 10)
                 and not _within(_days(ps, pe), YEAR)]
        if fy and prior:
            return last, fy[0] + v - prior[0]
    return None


def complement_from_facts(rows: list[dict]) -> Optional[dict]:
    """1社分の行（tag・s・e・v）から、スクリーナー互換の補完 dict を作る。EPS も FCF も作れなければ None。"""
    import us_fundamental_rotator as ufr
    by_tag: dict = {}
    for r in rows:
        by_tag.setdefault(r["tag"], []).append((r["s"], r["e"], float(r["v"])))
    eps_hist: list[dict] = []
    for tag in EPS_TAGS:
        eps_hist = quarterly_eps(by_tag.get(tag, []))
        if len(eps_hist) >= 8:
            break
    ocf = next((t for t in (ttm(by_tag.get(tag, [])) for tag in OCF_TAGS) if t), None)
    fcf_hist: list[dict] = []
    if ocf:
        capex_facts = [f for tag in CAPEX_TAGS for f in by_tag.get(tag, [])]
        if not capex_facts:
            cap = 0.0
        else:
            cap_t = next((t for t in (ttm(by_tag.get(tag, [])) for tag in CAPEX_TAGS) if t and t[0] == ocf[0]), None)
            cap = abs(cap_t[1]) if cap_t else None
        if cap is not None:
            fcf_hist = [{"period": ocf[0].isoformat(), "val": round(ocf[1] - cap, 0)}]
    if not eps_hist and not fcf_hist:
        return None
    filed = max((r.get("filed") for r in rows if r.get("filed")), default=None)
    return {
        "cik": rows[0].get("cik"),
        "eps_history": eps_hist,
        "fcf_history": fcf_hist,
        "fcf_basis": "ttm",
        "latest_eps": eps_hist[0]["val"] if eps_hist else None,
        "latest_fcf": fcf_hist[0]["val"] if fcf_hist else None,
        "eps_accel_score_xbrl": ufr.calc_eps_accel_score_xbrl(eps_hist),
        "data_source": "sec_company_facts_bq",
        "last_fetched": filed.isoformat() if filed else None,
    }


# ── 読み出し（1回の実行で1本） ────────────────────────────────

_LOCK = threading.Lock()
_CACHE: dict = {}   # {"ticker": {ticker: cik}, "facts": {cik: [行]}, "done": {cik: dict|None}}


def _load(cfg) -> dict:
    with _LOCK:
        if _CACHE:
            return _CACHE
        tick, facts = {}, {}
        try:
            from google.cloud import bigquery
            from edinet_bq_client import EDINETBQClient
            client = EDINETBQClient().client
            p = client.project
            for r in client.query(TICKER_SQL.format(p=p), location="US").result():
                tick[str(r["ticker"]).upper()] = int(r["cik"])
            jc = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("tags", "STRING", list(ALL_TAGS)),
                bigquery.ScalarQueryParameter("years", "INT64", int(getattr(cfg, "US_FACTS_COMPLEMENT_YEARS", 4)))])
            for r in client.query(FACTS_SQL.format(p=p), job_config=jc, location="US").result():
                facts.setdefault(int(r["cik"]), []).append(dict(r.items()))
            print(f"    🧾 us.company_facts の補完: {len(facts):,}社分を読んだ（銘柄 {len(tick):,}）")
        except Exception as e:
            print(f"    ⚠️ us.company_facts の補完を読めない（{type(e).__name__}）。us.fundamentals だけで補完する")
            tick, facts = {}, {}
        _CACHE.update(ticker=tick, facts=facts, done={})
        return _CACHE


def lookup(cfg, ticker: str) -> Optional[dict]:
    """1銘柄の補完（無ければ None）。cik ごとに1回だけ計算する。"""
    if not getattr(cfg, "US_FACTS_COMPLEMENT_ENABLED", True):
        return None
    t = str(ticker or "").upper().strip().replace(".", "-")
    if not t:
        return None
    c = _load(cfg)
    cik = c["ticker"].get(t)
    if cik is None:
        return None
    done = c["done"]
    if cik not in done:
        out = complement_from_facts(c["facts"].get(cik) or [])
        if out:
            out["ticker"] = t
        done[cik] = out
    hit = done[cik]
    return dict(hit, ticker=t) if hit else None
