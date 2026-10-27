"""
daily_changes.py — 日次メールの先頭の「今日の変化」：前の営業日から通過銘柄がどう変わったか

  ・新しく通過した銘柄（点の高い順）と、その点を押し上げている観点
  ・外れた銘柄（前回の点の高い順）と、今日の足切りで落ちた理由（候補に残っていれば）
  ・点が大きく動いた銘柄（|差| が DAILY_CHANGES_MOVE_PT 以上・差の大きい順）と、いちばん動いた観点
日本株（二次評価）と米国株（スクリーナー）を別々に出す。

今日の分は分析の結果（results["screener"] / ["us_screener"] の候補）から stock_snapshot_writer.build_snapshot_rows で
スナップショットと同じ形にして作る（メールはスナップショットの保存より前に送るため）。前の営業日の分は
analytics.stock_snapshot の今日より前の最新日を1本のクエリで読む。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ASPECTS = {"sector_score": "業種", "value_score": "割安", "quality_score": "質", "technical_score": "テクニカル",
           "discovery_score": "発掘"}
MARKETS = (("JP", "screener", "日本株"), ("US", "us_screener", "米国株"))

PREV_SQL = """
WITH d AS (
  SELECT market, MAX(snapshot_date) AS d FROM `{p}.analytics.stock_snapshot`
  WHERE snapshot_date < @today AND snapshot_date >= DATE_SUB(@today, INTERVAL 10 DAY) GROUP BY market
)
SELECT s.market, s.snapshot_date, s.ticker, ANY_VALUE(s.name) AS name, MAX(s.composite_score) AS composite_score,
       {aspects}
FROM `{p}.analytics.stock_snapshot` s JOIN d ON d.market = s.market AND d.d = s.snapshot_date
GROUP BY s.market, s.snapshot_date, s.ticker
"""


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def top_aspect(row: dict) -> str:
    """点の高い観点（名前）。無ければ ""。"""
    vals = [(ASPECTS[k], _num(row.get(k))) for k in ASPECTS]
    vals = [(n, v) for n, v in vals if v is not None]
    return max(vals, key=lambda x: x[1])[0] if vals else ""


def moved_aspect(now: dict, before: dict) -> str:
    """いちばん動いた観点と差（例「割安 +6」）。比べられる観点が無ければ ""。"""
    best = None
    for k, name in ASPECTS.items():
        a, b = _num(now.get(k)), _num(before.get(k))
        if a is None or b is None:
            continue
        d = a - b
        if best is None or abs(d) > abs(best[1]):
            best = (name, d)
    return f"{best[0]} {best[1]:+.0f}" if best and abs(best[1]) >= 0.5 else ""


def diff(today: dict, prev: dict, fail_reasons: dict, move_pt: float = 5.0, top: int = 5) -> dict:
    """today・prev は {ticker: 行}。新規・脱落・大きく動いた銘柄（各 top 件まで）と件数。"""
    new = sorted((t for t in today if t not in prev), key=lambda t: -(_num(today[t].get("composite_score")) or 0))
    gone = sorted((t for t in prev if t not in today), key=lambda t: -(_num(prev[t].get("composite_score")) or 0))
    moves = []
    for t in today:
        if t in prev:
            a, b = _num(today[t].get("composite_score")), _num(prev[t].get("composite_score"))
            if a is not None and b is not None and abs(a - b) >= move_pt:
                moves.append((t, a - b))
    moves.sort(key=lambda x: (-abs(x[1]), x[0]))
    return {
        "n_new": len(new), "n_gone": len(gone), "n_move": len(moves),
        "new": [(t, today[t], top_aspect(today[t])) for t in new[:top]],
        "gone": [(t, prev[t], (fail_reasons.get(t) or [""])[0]) for t in gone[:top]],
        "moves": [(t, today[t], d, moved_aspect(today[t], prev[t])) for t, d in moves[:top]],
    }


def _label(t: str, row: dict) -> str:
    name = str(row.get("name") or "").strip()
    return f"{name[:14]}（{t}）" if name else t


def lines_for(label: str, prev_date: str, d: dict) -> list[str]:
    out = [f"  【{label}】前回 {prev_date} から：新しく通過 {d['n_new']}社・外れた {d['n_gone']}社・点が大きく動いた {d['n_move']}社"]
    for t, row, why in d["new"]:
        out.append(f"    🆕 {_label(t, row)} {(_num(row.get('composite_score')) or 0):.0f}点" + (f"（強い観点：{why}）" if why else ""))
    for t, row, why in d["gone"]:
        out.append(f"    ⤵️ {_label(t, row)} 前回 {(_num(row.get('composite_score')) or 0):.0f}点" + (f"（{why}）" if why else ""))
    for t, row, delta, why in d["moves"]:
        out.append(f"    {'⬆️' if delta > 0 else '⬇️'} {_label(t, row)} {(_num(row.get('composite_score')) or 0):.0f}点"
                   f"（{delta:+.0f}" + (f"・{why}" if why else "") + "）")
    return out


def fail_reasons_of(c: dict) -> list[str]:
    """足切りで落ちた理由。米国は fail_reasons（文字列の列）、日本は filter_detail（{指標: "OK(..)"/それ以外}）。"""
    fr = c.get("fail_reasons")
    if fr:
        return [str(x) for x in (fr if isinstance(fr, (list, tuple)) else [fr])]
    det = c.get("filter_detail")
    if isinstance(det, dict):
        return [f"{k} {v}" for k, v in det.items() if not str(v).startswith("OK")]
    return []


def _today_rows(results: dict, key: str, market: str, today: str) -> tuple[dict, dict]:
    from stock_snapshot_writer import build_snapshot_rows
    cands = (results.get(key) or {}).get("candidates") or []
    rows = build_snapshot_rows(cands, market, today)
    fails = {}
    for c in cands:
        if isinstance(c, dict) and "pass_filters" in c and not c.get("pass_filters"):
            fails[str(c.get("ticker"))] = fail_reasons_of(c)[:1]
    return {r["ticker"]: r for r in rows}, fails


def build_alert(cfg, results: dict, prev_rows: list[dict] | None = None, today: str | None = None) -> dict | None:
    """「今日の変化」の alert（trigger=daily_changes）。前回の記録も今日の通過も無ければ None。
    prev_rows はテスト用（渡さなければ BigQuery から読む）。"""
    if not getattr(cfg, "ENABLE_DAILY_CHANGES", True):
        return None
    today = today or datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    if prev_rows is None:
        from google.cloud import bigquery
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
        sql = PREV_SQL.format(p=client.project, aspects=", ".join(f"MAX(s.{k}) AS {k}" for k in ASPECTS))
        jc = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("today", "DATE", today)])
        prev_rows = [dict(r.items()) for r in client.query(sql, job_config=jc).result()]
    move_pt = float(getattr(cfg, "DAILY_CHANGES_MOVE_PT", 5.0))
    top = int(getattr(cfg, "DAILY_CHANGES_TOP", 5))
    body = []
    for market, key, label in MARKETS:
        cur, fails = _today_rows(results, key, market, today)
        prev = {r["ticker"]: r for r in prev_rows if r.get("market") == market}
        if not cur or not prev:
            continue
        prev_date = str(next(iter(prev.values())).get("snapshot_date"))
        body += lines_for(label, prev_date, diff(cur, prev, fails, move_pt, top))
    if not body:
        return None
    return {"trigger": "daily_changes", "title": "🔄 今日の変化（通過銘柄の入れ替わり・点の動き）",
            "body": "\n".join(body), "severity": "info"}
