# ==============================================================
# market_positioning.py — 需給のデータを溜める（FINRA の空売り残・CFTC の建玉明細 COT）
# ==============================================================
# 【FINRA の空売り残】api.finra.org の consolidatedShortInterest（鍵なし・月2回の基準日：15日ごろと月末）。
#   取引所の上場株だけ（marketClassCode が OTC のものは除く）を analytics.short_interest へ。基準日で日分割し、
#   その日を丸ごと入れ替える（訂正 revisionFlag が出ても重ならない）。列：settlement_date・symbol・issue_name・market・
#   short_qty・prev_short_qty・change_pct・avg_daily_volume・days_to_cover・split_flag・revision_flag。
#   月ごとに引き（1ページ5,000行）、SHORT_INTEREST_BACKFILL_FLOOR まで遡る。
# 【CFTC の建玉明細】publicreporting.cftc.gov（Socrata・鍵なし・週次・火曜時点を金曜に公表）。
#   COT_REPORTS の2種類（legacy＝商品の投機筋・実需筋、tff＝金融先物の銀行・運用会社・ヘッジファンド）を
#   analytics.cot_positions へ。列：report_date・report・market_code・market_name・commodity・open_interest・
#   positions（JSON。その報告の数値の列をそのまま・SQL は JSON_VALUE で引く）。週で日分割し、その週を丸ごと入れ替える。
# 【進み具合】stock_data/data/market_positioning_state.json（取り込んだ月・週）。直近 POSITIONING_RECENT_PERIODS 回は毎回取り直す。
# 【容量】全体が BQ_STORAGE_STOP_GB、または2表の合計が POSITIONING_MAX_GB を超えたら書かない。
# 【商品の取り直し】legacy の市場（COT_SIGNAL_MARKETS・COT_COMMODITY_MARKETS）が state の cot.legacy_codes に無いものを含むとき、
#   過去 COT_COMMODITY_BACKFILL_YEARS 年の週の済み印（cot.both）を外して同じ経路で取り直し、全部済んだら legacy_codes を更新する。
#   途中で止まっても済み印のない週から続きを取る。強制は --redo-cot。
# 【実行】python3 market_positioning.py [--budget-min 30] [--redo-cot]
# ==============================================================
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATASET, LOCATION = "analytics", "asia-northeast1"
T_SHORT, T_COT = "short_interest", "cot_positions"
FINRA_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
CFTC_URL = "https://publicreporting.cftc.gov/resource/{rid}.json"
COT_REPORTS = {"legacy": "6dca-aqww", "tff": "gpe5-46if"}
SHORT_SCHEMA = [("settlement_date", "DATE"), ("symbol", "STRING"), ("issue_name", "STRING"), ("market", "STRING"),
                ("short_qty", "FLOAT"), ("prev_short_qty", "FLOAT"), ("change_pct", "FLOAT"),
                ("avg_daily_volume", "FLOAT"), ("days_to_cover", "FLOAT"), ("split_flag", "STRING"),
                ("revision_flag", "STRING"), ("fetched_at", "TIMESTAMP")]
COT_SCHEMA = [("report_date", "DATE"), ("report", "STRING"), ("market_code", "STRING"), ("market_name", "STRING"),
              ("commodity", "STRING"), ("open_interest", "FLOAT"), ("positions", "JSON"), ("fetched_at", "TIMESTAMP")]
_COT_TEXT = {"id", "market_and_exchange_names", "report_date_as_yyyy_mm_dd", "yyyy_report_week_ww", "contract_market_name",
             "cftc_contract_market_code", "cftc_market_code", "cftc_region_code", "cftc_commodity_code", "commodity_name",
             "commodity", "commodity_subgroup_name", "commodity_group_name", "futonly_or_combined", "contract_units"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def short_rows(records: list, now: str) -> list[dict]:
    """FINRA の行 → short_interest の行（OTC は除く）（純関数）。"""
    out = []
    for r in records or []:
        if str(r.get("marketClassCode") or "").upper() == "OTC" or not r.get("symbolCode") or not r.get("settlementDate"):
            continue
        out.append({"settlement_date": r["settlementDate"][:10], "symbol": r["symbolCode"], "issue_name": r.get("issueName"),
                    "market": r.get("marketClassCode"), "short_qty": _num(r.get("currentShortPositionQuantity")),
                    "prev_short_qty": _num(r.get("previousShortPositionQuantity")), "change_pct": _num(r.get("changePercent")),
                    "avg_daily_volume": _num(r.get("averageDailyVolumeQuantity")), "days_to_cover": _num(r.get("daysToCoverQuantity")),
                    "split_flag": r.get("stockSplitFlag"), "revision_flag": r.get("revisionFlag"), "fetched_at": now})
    return out


def cot_rows(records: list, report: str, now: str) -> list[dict]:
    """CFTC の行 → cot_positions の行（数値の列は positions の JSON に）（純関数）。"""
    out = []
    for r in records or []:
        d = str(r.get("report_date_as_yyyy_mm_dd") or "")[:10]
        if not d:
            continue
        pos = {k: _num(v) for k, v in r.items() if k not in _COT_TEXT and _num(v) is not None}
        out.append({"report_date": d, "report": report, "market_code": (r.get("cftc_contract_market_code") or "").strip(),
                    "market_name": r.get("market_and_exchange_names"), "commodity": r.get("commodity_name"),
                    "open_interest": _num(r.get("open_interest_all")), "positions": pos, "fetched_at": now})
    return out


def cot_keep_codes(cfg) -> set:
    """legacy で残す市場コード。legacy は商品の全市場（約300）なので、読み手（cot_signal）が使う市場
    （COT_SIGNAL_MARKETS の legacy と COT_COMMODITY_MARKETS）と COT_LEGACY_EXTRA_CODES だけ残す（保存量のため）。
    tff は金融先物だけなので全部残す。"""
    import cot_signal
    return cot_signal.legacy_codes(cfg) | {str(c) for c in (getattr(cfg, "COT_LEGACY_EXTRA_CODES", ()) or ())}


def keep_cot_markets(rows: list, cfg) -> list:
    codes = cot_keep_codes(cfg)
    if not codes:
        return rows
    return [r for r in rows if r["report"] != "legacy" or r["market_code"] in codes]


def months_to_run(state: dict, today: date, recent: int, floor: date) -> list[date]:
    """取り込む月（月初の日付・新しい順）。直近 recent か月は毎回、残りは未取得の月だけ。"""
    m = date(today.year, today.month, 1)
    out, i = [], 0
    done = set(state.get("months") or [])
    while m >= floor:
        if i < recent or m.isoformat() not in done:
            out.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
        i += 1
    return out


def new_settlement_dates(dates, prev_latest: str | None, prev_done_months, recent_from: date) -> list[str]:
    """取り込んだ基準日のうち、今回初めて入ったもの（純関数）。

    直近の月は毎回取り直す（訂正・遅れて出る分）ので、同じ基準日が毎日入り直す。それを「新しい基準日」と数えないよう、
    前回までの最新の基準日より後のもの、または遡りで初めて入る月（recent_from より前で済み印が無い月）だけを返す。
    prev_latest が None（記録が無い初回）は全部を新しいとする。"""
    done = set(prev_done_months or [])
    out = set()
    for d in dates:
        mon = d[:8] + "01"
        if prev_latest is None or d > prev_latest or (mon < recent_from.isoformat() and mon not in done):
            out.add(d)
    return sorted(out)


def weeks_to_run(state: dict, report: str, today: date, recent: int, floor: date) -> list[date]:
    """取り込む週（その週の月曜・新しい順）。"""
    w = today - timedelta(days=today.weekday())
    out, i = [], 0
    done = set((state.get("cot") or {}).get(report) or [])
    while w >= floor:
        if i < recent or w.isoformat() not in done:
            out.append(w)
        w -= timedelta(days=7)
        i += 1
    return out


def commodity_backfill(state: dict, cfg, today: date, force: bool = False) -> tuple[set, list[date]]:
    """未取得の legacy 市場が増えたとき、過去 COT_COMMODITY_BACKFILL_YEARS 年の週を「取り込み済み」から外す（純関数）。

    legacy の行は tff と同じ日のパーティションに入り、週ごとに両方を取り直して丸ごと入れ替えるので、
    市場を足した分は済みの印を外して同じ経路で取り直す。返り値＝(取得済みにする市場コード, 外した週)。
    外した週が空なら取り直し不要。force なら市場が増えていなくても外す。"""
    want = {str(c) for c in cot_keep_codes(cfg)}
    have = set((state.get("cot") or {}).get("legacy_codes") or [])
    if not (want - have) and not force:
        return want, []
    years = int(getattr(cfg, "COT_COMMODITY_BACKFILL_YEARS", 3))
    first = today - timedelta(days=365 * years + 30)
    w = today - timedelta(days=today.weekday())
    weeks = []
    while w >= first:
        weeks.append(w)
        w -= timedelta(days=7)
    return want, weeks


def _ensure(client) -> None:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    for name, schema, part, cluster in ((T_SHORT, SHORT_SCHEMA, "settlement_date", ["symbol"]),
                                        (T_COT, COT_SCHEMA, "report_date", ["report", "market_code"])):
        ref = f"{client.project}.{DATASET}.{name}"
        try:
            client.get_table(ref)
        except NotFound:
            t = bigquery.Table(ref, schema=[bigquery.SchemaField(n, ty) for n, ty in schema])
            t.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.DAY, field=part)
            t.clustering_fields = cluster
            client.create_table(t)


def _replace_days(client, table: str, schema, rows: list[dict], key: str) -> None:
    """rows の日ごとにパーティションを丸ごと入れ替える（load job・無料）。"""
    from google.cloud import bigquery
    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r[key], []).append(r)
    for d, part in by_day.items():
        cfg = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                                     write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                                     schema=[bigquery.SchemaField(n, t) for n, t in schema])
        client.load_table_from_json(part, f"{client.project}.{DATASET}.{table}${d.replace('-', '')}",
                                    job_config=cfg, location=LOCATION).result()


def run(cfg, budget_min: float | None = None, force_cot: bool = False) -> dict:
    import requests
    import bq_storage
    import state_store
    budget = 60 * float(budget_min if budget_min is not None else getattr(cfg, "POSITIONING_TIME_BUDGET_MIN", 30))
    gap = float(getattr(cfg, "POSITIONING_MIN_INTERVAL_SEC", 1.0))
    recent = int(getattr(cfg, "POSITIONING_RECENT_PERIODS", 2))
    client = state_store.make_client(cfg)
    _ensure(client)
    m = bq_storage.measure(client)
    bq_storage.record(cfg, m)
    mine = sum(m["tables"].get(f"{DATASET}.{t}", 0.0) for t in (T_SHORT, T_COT))
    if not bq_storage.allowed(cfg, m) or mine >= float(getattr(cfg, "POSITIONING_MAX_GB", 0.3)):
        return {"skipped": True, "reason": f"保存量の上限（全体 {m['total_gb']}GB・この2表 {mine:.3f}GB）"}
    path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "market_positioning_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    t0, last = time.monotonic(), 0.0
    stat = {"short_months": 0, "short_rows": 0, "short_range": None, "cot_weeks": 0, "cot_rows": 0, "cot_range": None,
            "errors": 0, "budget_out": False}
    now = datetime.now(timezone.utc).isoformat()

    def _wait():
        nonlocal last
        w = gap - (time.monotonic() - last)
        if w > 0:
            time.sleep(w)
        last = time.monotonic()

    today = date.today()
    # ── FINRA の空売り残（月ごと） ──
    sfloor = date.fromisoformat(str(getattr(cfg, "SHORT_INTEREST_BACKFILL_FLOOR", "2022-01-01")))
    prev_done_m, prev_latest = set(state.get("months") or []), state.get("short_latest")
    done_m, dates = set(prev_done_m), []
    for mon in months_to_run(state, today, recent, sfloor):
        if time.monotonic() - t0 > budget:
            stat["budget_out"] = True
            break
        end = ((mon + timedelta(days=32)).replace(day=1) - timedelta(days=1))
        recs, offset, ok = [], 0, True
        while True:
            _wait()
            r = None
            try:
                r = requests.post(FINRA_URL, timeout=120, headers={"Accept": "application/json"},
                                  json={"limit": 5000, "offset": offset, "dateRangeFilters": [
                                      {"fieldName": "settlementDate", "startDate": mon.isoformat(), "endDate": end.isoformat()}]})
                page = r.json() if r.status_code == 200 and r.content else []
            except Exception:
                page, ok = [], False
            if r is not None and getattr(r, "status_code", 200) not in (200, 204):
                ok = False
            recs += page or []
            if not page or len(page) < 5000:
                break
            offset += 5000
        if not ok:
            stat["errors"] += 1
            continue
        rows = short_rows(recs, now)
        if rows:
            _replace_days(client, T_SHORT, SHORT_SCHEMA, rows, "settlement_date")
            dates += [x["settlement_date"] for x in rows]
        if end < today:
            done_m.add(mon.isoformat())
        stat["short_months"] += 1
        stat["short_rows"] += len(rows)
    state["months"] = sorted(done_m)
    # short_range は「この回に新しく入った基準日」。直近の月の取り直しだけの日は None（通知が空売り残の節を出さない）
    recent_from = date(today.year, today.month, 1)
    for _ in range(max(0, recent - 1)):
        recent_from = (recent_from - timedelta(days=1)).replace(day=1)
    fresh = new_settlement_dates(dates, prev_latest, prev_done_m, recent_from)
    stat["short_range"] = [fresh[0], fresh[-1]] if fresh else None
    stat["short_new_dates"] = len(fresh)
    if dates:
        state["short_latest"] = max([d for d in (prev_latest, max(dates)) if d])
    # ── CFTC の建玉明細（週ごと・2種類） ──
    cfloor = date.fromisoformat(str(getattr(cfg, "COT_BACKFILL_FLOOR", "2015-01-01")))
    cot_state = state.setdefault("cot", {})
    cdates = []
    # 2種類は同じ日の同じパーティションに入るので、週ごとに両方を取ってから1回で入れ替える
    # （別々に入れ替えると後の報告が前の報告の行を消す）。済みの印は両方がそろった週だけ「both」に付ける
    done_w = set(cot_state.get("both") or [])
    want_codes, redo = commodity_backfill(state, cfg, today, force_cot)
    done_w -= {w.isoformat() for w in redo}
    cot_state["both"] = sorted(done_w)
    weeks = weeks_to_run(state, "both", today, recent, cfloor)
    # 1回の問い合わせで13週ぶん（最大 5,000 行）を報告ごとに引く
    for i in range(0, len(weeks), 13):
        if time.monotonic() - t0 > budget:
            stat["budget_out"] = True
            break
        chunk = weeks[i:i + 13]
        lo, hi = min(chunk), max(chunk) + timedelta(days=6)
        rows, failed = [], False
        for report, rid in COT_REPORTS.items():
            _wait()
            try:
                r = requests.get(CFTC_URL.format(rid=rid), timeout=120, params={
                    "$limit": 50000, "$where": f"report_date_as_yyyy_mm_dd between '{lo}T00:00:00' and '{hi}T23:59:59'"})
                recs = r.json() if r.status_code == 200 else None
            except Exception:
                recs = None
            if recs is None:
                failed = True
                break
            rows += keep_cot_markets(cot_rows(recs, report, now), cfg)
        if failed:
            stat["errors"] += 1
            continue
        if rows:
            _replace_days(client, T_COT, COT_SCHEMA, rows, "report_date")
            cdates += [x["report_date"] for x in rows]
        for w in chunk:
            if w + timedelta(days=6) < today:
                done_w.add(w.isoformat())
        stat["cot_weeks"] += len(chunk)
        stat["cot_rows"] += len(rows)
    cot_state["both"] = sorted(done_w)
    # 取り直す週がすべて済み（エラー・時間切れなし）になったときだけ、取得済みの市場を記録する
    if redo and not stat["errors"] and not stat["budget_out"] and all(
            w.isoformat() in done_w for w in redo if w + timedelta(days=6) < today):
        cot_state["legacy_codes"] = sorted(want_codes)
    elif not redo:
        cot_state["legacy_codes"] = sorted(want_codes)
    stat["cot_backfill_weeks"] = len(redo)
    stat["cot_range"] = [min(cdates), max(cdates)] if cdates else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    # 取り込み直後に、建玉の偏り（マクロ判断の入力）を週1回の読みで作り直す。失敗しても取り込みの結果は返す
    try:
        import cot_signal
        stat["cot_signal"] = cot_signal.refresh(cfg, client)
    except Exception as e:
        stat["cot_signal"] = {"error": str(e)[:120]}
    return stat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-min", type=float)
    ap.add_argument("--redo-cot", action="store_true",
                    help="COT_COMMODITY_BACKFILL_YEARS 年分の建玉を、市場が増えていなくても取り直す")
    a = ap.parse_args(argv)
    import config as cfg
    st = run(cfg, a.budget_min, a.redo_cot)
    print(f"{'⏩' if st.get('skipped') else '✅'} market_positioning: {json.dumps(st, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
