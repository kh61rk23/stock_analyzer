# ==============================================================
# backtest/hidden_feature_ic.py — BQ にあるが分析に乗っていない特徴量の効きの測定（測るだけ。スコアは変えない）
# ==============================================================
# 【測るもの】
#   1) 公的支出（edinet.public_funding・economic_security_certs）：銘柄ごとの受給額・経済安保認定を特徴量にし、
#      翌20営業日・翌60営業日リターンとの順位相関（IC）と t 値（Newey-West）を出す。
#   2) 日次メールの注意項目（analytics.alert_items_v）：section_key（通知元）× severity、および source × severity ごとに、
#      通知の翌営業日の終値から20営業日後までの超過リターン（市場平均・業種平均との差）の平均と的中率を出す。
# 【時点整合】
#   - 公的支出は event_date に公表遅れ（FUNDING_LAG_DAYS）を足した日以降だけ使う。public_funding_rank は最新の
#     スナップショットしか無く過去を再現できないので使わない。
#   - 注意項目の入口は「通知日の翌営業日の終値」。通知日当日の値動きは含めない。
# 【母集団・価格】analytics.price_daily（2025-10 以降の日足）の東証銘柄（edinet.sector_jpx にある4桁コード）。
#   終値が前後5倍・0.2倍を超えて動く区間は分割などの欠陥とみなして捨てる。TOPIX ETF の履歴は BQ に無いので、
#   「市場平均」は同じ母集団の等ウェイト平均で代用する。
# 【実行】BQ の資格情報が無い環境では、--print-sql の SQL を BQ MCP で流し、結果JSONを --*-json で渡す。
#   python3 -m backtest.hidden_feature_ic --print-sql funding|alerts
#   python3 -m backtest.hidden_feature_ic --funding-json f.json --alerts-json a.json --out backtest/results/hidden_feature_ic.json
# ==============================================================
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

from backtest.factor_ic import nw_tstat
from backtest.re_factor_panel import rows_from_mcp_json

PROJECT = "stock-analyzer-edinet"
FUNDING_LAG_DAYS = 30      # 受給の公表遅れの仮定（event_date + 30日以降に知り得たとする）
ASOF_STEP = 5              # 評価日は5営業日おき
HORIZONS = (20, 60)
ALERT_HORIZON = 20
SPLIT_GUARD = (0.2, 5.0)   # IC用：終値比がこの範囲外の区間は分割などの欠陥として捨てる（順位相関なので緩め）
ALERT_RET_GUARD = (0.5, 2.0)  # 注意項目用：平均が外れ値に引きずられるため厳しめ（価格表に未調整の分割が混じる実例あり）
# 採用の目安：既存の検証（設計書 土地含み益ファクター §採用基準・効果）に合わせ、IC 平均 > 0 かつ t 値 ≥ 2
IC_T_MIN = 2.0
HIT_MIN = 0.5
MIN_EVENTS = 30            # これ未満の群は判定しない

FEATURES = ("cum_amount", "recent_1y", "subsidy_cum", "procurement_cum", "econ_sec_flag")


# ── SQL ──────────────────────────────────────────────────────

_PX_CTE = """
px AS (
  SELECT p.ticker, p.trade_date, p.close
  FROM `{project}.analytics.price_daily` AS p
  JOIN `{project}.edinet.sector_jpx` AS s ON s.code4 = SUBSTR(p.ticker, 1, 4)
  WHERE REGEXP_CONTAINS(p.ticker, r'^[0-9][0-9A-Z]{{3}}\\.T$') AND p.close > 0
  QUALIFY ROW_NUMBER() OVER (PARTITION BY p.ticker, p.trade_date ORDER BY p.ingested_at DESC) = 1
),
cal AS (SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date) AS rn FROM (SELECT DISTINCT trade_date FROM px)),
pxr AS (SELECT p.ticker, c.rn, p.trade_date, p.close FROM px AS p JOIN cal AS c USING (trade_date))
"""


def funding_sql(project: str = PROJECT) -> str:
    """評価日ごと・特徴量ごと・母集団ごと・期間ごとの IC を「日付:IC:銘柄数;…」の文字列にして返す1本のクエリ。"""
    lo, hi = SPLIT_GUARD
    return f"""
WITH {_PX_CTE.format(project=project)},
asof AS (SELECT trade_date AS t, rn FROM cal WHERE MOD(rn, {ASOF_STEP}) = 0),
ev AS (
  SELECT company_code AS code4, event_date, amount_yen, kind, 0 AS es
  FROM `{project}.edinet.public_funding` WHERE endpoint = 'company_public_funding' AND amount_yen IS NOT NULL
  UNION ALL
  SELECT company_code, event_date, 0.0, kind, 1 FROM `{project}.edinet.economic_security_certs`
),
feat AS (
  SELECT a.t, e.code4,
    SUM(IF(e.es = 0, e.amount_yen, 0)) AS cum_amount,
    SUM(IF(e.es = 0 AND e.event_date > DATE_SUB(a.t, INTERVAL {365 + FUNDING_LAG_DAYS} DAY), e.amount_yen, 0)) AS recent_1y,
    SUM(IF(e.es = 0 AND LOWER(IFNULL(e.kind, '')) LIKE '%subsid%', e.amount_yen, 0)) AS subsidy_cum,
    SUM(IF(e.es = 0 AND LOWER(IFNULL(e.kind, '')) LIKE '%procure%', e.amount_yen, 0)) AS procurement_cum,
    MAX(IF(e.es = 1, 1.0, 0.0)) AS econ_sec_flag
  FROM asof AS a JOIN ev AS e ON DATE_ADD(e.event_date, INTERVAL {FUNDING_LAG_DAYS} DAY) <= a.t
  GROUP BY a.t, e.code4
),
base AS (
  SELECT a.t, p0.ticker, p0.close AS c0,
    f20.close AS c20, f60.close AS c60,
    COALESCE(ft.cum_amount, 0) AS cum_amount, COALESCE(ft.recent_1y, 0) AS recent_1y,
    COALESCE(ft.subsidy_cum, 0) AS subsidy_cum, COALESCE(ft.procurement_cum, 0) AS procurement_cum,
    COALESCE(ft.econ_sec_flag, 0) AS econ_sec_flag
  FROM asof AS a
  JOIN pxr AS p0 ON p0.rn = a.rn
  LEFT JOIN pxr AS f20 ON f20.ticker = p0.ticker AND f20.rn = a.rn + 20
  LEFT JOIN pxr AS f60 ON f60.ticker = p0.ticker AND f60.rn = a.rn + 60
  LEFT JOIN feat AS ft ON ft.t = a.t AND ft.code4 = SUBSTR(p0.ticker, 1, 4)
),
longf AS (
  SELECT t, ticker, 'cum_amount' AS feature, cum_amount AS x, c0, c20, c60 FROM base UNION ALL
  SELECT t, ticker, 'recent_1y', recent_1y, c0, c20, c60 FROM base UNION ALL
  SELECT t, ticker, 'subsidy_cum', subsidy_cum, c0, c20, c60 FROM base UNION ALL
  SELECT t, ticker, 'procurement_cum', procurement_cum, c0, c20, c60 FROM base UNION ALL
  SELECT t, ticker, 'econ_sec_flag', econ_sec_flag, c0, c20, c60 FROM base
),
pts AS (
  SELECT t, feature, u AS universe, x, h, c_h / c0 - 1 AS y
  FROM longf, UNNEST(['all', 'funded']) AS u, UNNEST([STRUCT(20 AS h, c20 AS c_h), STRUCT(60, c60)])
  WHERE c_h IS NOT NULL AND c_h / c0 BETWEEN {lo} AND {hi} AND (u = 'all' OR x > 0)
),
rk AS (
  SELECT t, feature, universe, h, x, y,
    RANK() OVER (PARTITION BY t, feature, universe, h ORDER BY x) AS kx,
    RANK() OVER (PARTITION BY t, feature, universe, h ORDER BY y) AS ky
  FROM pts
),
ranked AS (
  SELECT t, feature, universe, h,
    kx + (COUNT(*) OVER (PARTITION BY t, feature, universe, h, kx) - 1) / 2.0 AS rx,
    ky + (COUNT(*) OVER (PARTITION BY t, feature, universe, h, ky) - 1) / 2.0 AS ry
  FROM rk
),
ic AS (
  SELECT t, feature, universe, h, CORR(rx, ry) AS ic_v, COUNT(*) AS n
  FROM ranked GROUP BY t, feature, universe, h HAVING COUNT(*) >= 20
)
SELECT feature, universe, h, COUNT(*) AS dates,
  STRING_AGG(CONCAT(CAST(t AS STRING), ':', CAST(ROUND(ic_v, 5) AS STRING), ':', CAST(n AS STRING)), ';' ORDER BY t) AS series
FROM ic WHERE ic_v IS NOT NULL GROUP BY feature, universe, h ORDER BY feature, universe, h
"""


def alerts_sql(project: str = PROJECT) -> str:
    """通知元×重要度（section_severity）と source×重要度（source_severity）ごとに、超過リターンの平均・的中率・通知日ごとの平均の標準偏差を返すクエリ（日本株のみ）。"""
    lo, hi = ALERT_RET_GUARD
    h = ALERT_HORIZON
    return f"""
WITH {_PX_CTE.format(project=project)},
fwd AS (
  SELECT a.ticker, a.rn, a.close / 1 AS c0, b.close AS c1, SUBSTR(a.ticker, 1, 4) AS code4
  FROM pxr AS a JOIN pxr AS b ON b.ticker = a.ticker AND b.rn = a.rn + {h}
  WHERE b.close / a.close BETWEEN {lo} AND {hi}
),
r AS (SELECT f.ticker, f.rn, f.c1 / f.c0 - 1 AS ret, s.sector_name FROM fwd AS f JOIN `{project}.edinet.sector_jpx` AS s ON s.code4 = f.code4),
mkt AS (SELECT rn, AVG(ret) AS m_ret FROM r GROUP BY rn),
sec AS (SELECT rn, sector_name, AVG(ret) AS s_ret, COUNT(*) AS s_n FROM r GROUP BY rn, sector_name),
ent AS (
  SELECT d AS report_date, MIN(c.rn) AS rn_e
  FROM (SELECT DISTINCT report_date AS d FROM `{project}.analytics.alert_items_v`) AS x JOIN cal AS c ON c.trade_date > x.d
  GROUP BY d
),
ai AS (
  SELECT DISTINCT report_date, section_key, severity, source, ticker
  FROM `{project}.analytics.alert_items_v` WHERE REGEXP_CONTAINS(ticker, r'^[0-9][0-9A-Z]{{3}}\\.T$')
),
ev AS (
  SELECT ai.report_date, ai.section_key, ai.severity, ai.source, ai.ticker,
    r.ret - m.m_ret AS ex_mkt,
    IF(sc.s_n >= 5, r.ret - sc.s_ret, NULL) AS ex_sec
  FROM ai JOIN ent AS e ON e.report_date = ai.report_date
  JOIN r ON r.ticker = ai.ticker AND r.rn = e.rn_e
  JOIN mkt AS m ON m.rn = e.rn_e
  JOIN sec AS sc ON sc.rn = e.rn_e AND sc.sector_name = r.sector_name
),
grp AS (
  SELECT 'section_severity' AS kind, CONCAT(section_key, '|', severity) AS k, report_date, ex_mkt, ex_sec FROM ev
  UNION ALL
  SELECT 'source_severity', CONCAT(source, '|', severity), report_date, ex_mkt, ex_sec FROM ev
),
med AS (
  SELECT kind, k, APPROX_QUANTILES(ex_mkt, 2 IGNORE NULLS)[OFFSET(1)] AS med_mkt, APPROX_QUANTILES(ex_sec, 2 IGNORE NULLS)[OFFSET(1)] AS med_sec
  FROM grp GROUP BY kind, k
),
dy AS (
  SELECT kind, k, report_date, COUNT(*) AS n, AVG(ex_mkt) AS m_mkt, COUNTIF(ex_mkt > 0) AS h_mkt,
    COUNT(ex_sec) AS n_sec, AVG(ex_sec) AS m_sec, COUNTIF(ex_sec > 0) AS h_sec
  FROM grp GROUP BY kind, k, report_date
)
SELECT dy.kind, dy.k, SUM(n) AS events, COUNT(*) AS days, ANY_VALUE(med.med_mkt) AS med_mkt, ANY_VALUE(med.med_sec) AS med_sec,
  SAFE_DIVIDE(SUM(m_mkt * n), SUM(n)) AS mean_mkt, SUM(h_mkt) / SUM(n) AS hit_mkt,
  AVG(m_mkt) AS day_mean_mkt, STDDEV(m_mkt) AS day_sd_mkt,
  SUM(n_sec) AS events_sec, SAFE_DIVIDE(SUM(m_sec * n_sec), SUM(n_sec)) AS mean_sec, SAFE_DIVIDE(SUM(h_sec), SUM(n_sec)) AS hit_sec,
  AVG(m_sec) AS day_mean_sec, STDDEV(m_sec) AS day_sd_sec
FROM dy JOIN med ON med.kind = dy.kind AND med.k = dy.k GROUP BY dy.kind, dy.k ORDER BY dy.kind, dy.k
"""


# ── 純関数 ────────────────────────────────────────────────────

def parse_series(s: str) -> list[tuple[str, float, int]]:
    """「日付:IC:銘柄数;…」→ [(日付, IC, n)]。壊れた項目は捨てる。"""
    out = []
    for item in (s or "").split(";"):
        parts = item.split(":")
        if len(parts) != 3:
            continue
        try:
            ic = float(parts[1])
            if math.isfinite(ic):
                out.append((parts[0], ic, int(parts[2])))
        except ValueError:
            continue
    return out


def summarize_ic(series: list, h: int) -> dict:
    """IC 系列の要約。評価日は ASOF_STEP 営業日おきなので、h 営業日先の重なりは h/ASOF_STEP - 1 ラグで割り引く。"""
    ics = [x[1] for x in series]
    if not ics:
        return {"dates": 0, "mean": None, "t_nw": None, "pos_share": None, "avg_n": None}
    lags = max(0, h // ASOF_STEP - 1)
    return {"dates": len(ics), "mean": sum(ics) / len(ics), "t_nw": nw_tstat(ics, lags),
            "pos_share": sum(1 for x in ics if x > 0) / len(ics), "avg_n": sum(x[2] for x in series) / len(series)}


def judge_ic(summary: dict) -> str:
    """IC 平均 > 0 かつ t ≥ 2 なら「採用候補（効果のみ。固有性・頑健性は未確認）」、そうでなければ「見送り」。"""
    m, t = summary.get("mean"), summary.get("t_nw")
    if m is not None and t is not None and m > 0 and t >= IC_T_MIN:
        return "採用候補"
    return "見送り"


def funding_report(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        h = int(r["h"])
        s = summarize_ic(parse_series(r.get("series")), h)
        out.append({"feature": r["feature"], "universe": r["universe"], "horizon": h, **s, "judge": judge_ic(s)})
    return out


def _fnum(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def alert_stat(row: dict, which: str) -> dict:
    """SQL が返した1群の行 → 事象単位の平均超過・的中率と、通知日ごとの平均に対する t 値。

    同じ日の事象は同じ相場環境に晒されるので、t 値は事象ではなく通知日を単位にする（日クラスタ）。
    which: 'mkt'（市場平均との差）か 'sec'（業種平均との差）。
    """
    n = _fnum(row.get("events" if which == "mkt" else "events_sec")) or 0
    days = int(_fnum(row.get("days")) or 0)
    dm, sd = _fnum(row.get(f"day_mean_{which}")), _fnum(row.get(f"day_sd_{which}"))
    t = dm / (sd / math.sqrt(days)) if dm is not None and sd and days >= 3 else None
    return {"events": int(n), "days": days, "mean_excess": _fnum(row.get(f"mean_{which}")),
            "median_excess": _fnum(row.get(f"med_{which}")), "hit_rate": _fnum(row.get(f"hit_{which}")), "t_day": t}


def judge_alert(st: dict) -> str:
    """事象数が足りない群は判定しない。平均超過 > 0・的中率 > 50%・日クラスタ t ≥ 2 なら採用候補、t ≤ -2 なら逆向き。"""
    if st["events"] < MIN_EVENTS or st["t_day"] is None or st["mean_excess"] is None or st["hit_rate"] is None:
        return "判定不能（事象数不足）"
    if st["mean_excess"] > 0 and st["hit_rate"] > HIT_MIN and st["t_day"] >= IC_T_MIN:
        return "採用候補"
    if st["mean_excess"] < 0 and st["t_day"] <= -IC_T_MIN:
        return "逆向き（見送り）"
    return "見送り"


def alerts_report(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        block = out.setdefault(r["kind"], {}).setdefault(r["k"], {})
        for which in ("mkt", "sec"):
            st = alert_stat(r, which)
            block[which] = {**st, "judge": judge_alert(st)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print-sql", choices=["funding", "alerts"])
    ap.add_argument("--funding-json", nargs="+", help="funding_sql の BQ MCP 結果JSON")
    ap.add_argument("--alerts-json", nargs="+", help="alerts_sql の BQ MCP 結果JSON")
    ap.add_argument("--bytes", type=int, default=None, help="BQ で使った課金バイト数の合計（記録用）")
    ap.add_argument("--out", default="backtest/results/hidden_feature_ic.json")
    args = ap.parse_args(argv)
    if args.print_sql:
        print(funding_sql() if args.print_sql == "funding" else alerts_sql())
        return 0
    res: dict = {"generated": datetime.now().isoformat(timespec="seconds"),
                 "assumptions": {"funding_lag_days": FUNDING_LAG_DAYS, "asof_step": ASOF_STEP,
                                 "split_guard": SPLIT_GUARD, "alert_ret_guard": ALERT_RET_GUARD,
                                 "alert_horizon": ALERT_HORIZON},
                 "bq_bytes_billed": args.bytes,
                 "caveats": [
                     "価格表は2025-10以降の約1年分。評価期間が短く、1つの相場局面に偏る（頑健性は未検証）",
                     "判定は効果基準（IC>0 かつ t>=2）だけ。固有性（規模・業種の効果を除いても残るか）と頑健性は未確認",
                     "母集団 all の IC には「受給の有無」が効く。受給のある銘柄だけ（funded）の IC は0近辺なら、額の大小ではなく有無の効果",
                     "public_funding_rank は最新分しか無く使っていない。受給は event_date + 30日以降に知り得たと仮定",
                     "注意項目は日本株のみ。通知が最近の項目は20営業日先の株価がまだ無く対象外",
                 ]}
    if args.funding_json:
        res["public_funding_ic"] = funding_report([r for p in args.funding_json for r in rows_from_mcp_json(Path(p))])
    if args.alerts_json:
        res["alerts"] = alerts_report([r for p in args.alerts_json for r in rows_from_mcp_json(Path(p))])
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"✅ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
