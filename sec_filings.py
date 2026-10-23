# ==============================================================
# sec_filings.py — SEC EDGAR の全提出一覧・8-K の項目・13D/13G を BigQuery（us データセット）に溜める
# ==============================================================
# 【表】（load job＝無料。日ごとのパーティションを丸ごと入れ替えるので、何度流しても重ならない）
#   us.sec_filings … 日次の提出一覧（daily-index/master.YYYYMMDD.idx）の全書類。filed_date で日分割。
#                    列は cik・form・accession・items（8-K と 8-K/A だけ。"2.02,9.01" の形）。
#                    会社名は us.sec_filers（cik → name。新しい cik だけ追記）に分ける（行を細くして保存量を抑える）
#   us.sec_13dg    … SCHEDULE 13D / 13G（XML 形式・2024-12 以降）を報告者ごとに1行。発行会社（CIK・名前・CUSIP）・
#                    報告者名・種別（IN 個人・IA 投資顧問 等）・保有株数・保有割合・議決権・処分権・事象日。
#                    それより古い SC 13D/13G（文書形式）は一覧（sec_filings）にだけ載る
# 【進め方】edinet_gov_docs.py と同じ。直近 SEC_FILINGS_RECENT_DAYS 日は毎回取り直し、過去は SEC_FILINGS_BACKFILL_FLOOR
#   まで遡る。土日・祝日は一覧が無い（404）ので0件として記録する。1回の実行は時間の予算で区切り、途中の日は記録しない。
#   進み具合は stock_data/data/sec_filings_state.json。
# 【容量】全体が BQ_STORAGE_STOP_GB、またはこの3表の合計が SEC_FILINGS_MAX_GB を超えたら書かない。
# 【名乗り】SEC_USER_AGENT（無ければ何もしない）。連続アクセスは SEC_FILINGS_MIN_INTERVAL_SEC 空ける（SEC は1秒10回まで）。
# 【実行】python3 sec_filings.py [--budget-min 20] [--dates 2026-10-21 ...]
# ==============================================================
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATASET, LOCATION = "us", "US"
T_FILINGS, T_FILERS, T_13DG = "sec_filings", "sec_filers", "sec_13dg"
ARCHIVES = "https://www.sec.gov/Archives/edgar"

FILING_SCHEMA = [("filed_date", "DATE"), ("cik", "INTEGER"), ("form", "STRING"), ("accession", "STRING"),
                 ("items", "STRING")]
FILER_SCHEMA = [("cik", "INTEGER"), ("name", "STRING"), ("first_seen", "DATE")]
DG_SCHEMA = [("filed_date", "DATE"), ("accession", "STRING"), ("form", "STRING"), ("filer_cik", "INTEGER"),
             ("issuer_cik", "INTEGER"), ("issuer_name", "STRING"), ("issuer_cusip", "STRING"),
             ("security_class", "STRING"), ("event_date", "DATE"), ("rule", "STRING"),
             ("person_name", "STRING"), ("person_type", "STRING"), ("citizenship", "STRING"),
             ("shares", "FLOAT"), ("percent", "FLOAT"), ("sole_voting", "FLOAT"), ("shared_voting", "FLOAT"),
             ("sole_dispositive", "FLOAT"), ("shared_dispositive", "FLOAT")]
ITEM_FORMS = ("8-K", "8-K/A")


# ── 純関数 ───────────────────────────────────────────────────

def index_url(day: date) -> str:
    return f"{ARCHIVES}/daily-index/{day.year}/QTR{(day.month - 1) // 3 + 1}/master.{day:%Y%m%d}.idx"


def parse_master(text: str, day: date) -> tuple[list[dict], dict]:
    """master.idx（CIK|Company Name|Form Type|Date Filed|Filename）→ (sec_filings の行, {cik: 会社名})。"""
    rows, names = [], {}
    started = False
    for line in (text or "").splitlines():
        if line.startswith("-----"):
            started = True
            continue
        if not started or line.count("|") != 4:
            continue
        cik, name, form, _filed, fn = line.split("|")
        try:
            c = int(cik)
        except ValueError:
            continue
        acc = fn.rsplit("/", 1)[-1].removesuffix(".txt")
        rows.append({"filed_date": day.isoformat(), "cik": c, "form": form.strip(), "accession": acc, "items": None})
        names.setdefault(c, name.strip())
    return rows, names


def filing_index_url(cik: int, accession: str) -> str:
    return f"{ARCHIVES}/data/{cik}/{accession.replace('-', '')}/{accession}-index.htm"


_ITEM = re.compile(r"Item\s+(\d+\.\d+)")


def items_from_index(html: str) -> str | None:
    """8-K の目次ページ → 項目番号（"2.02,9.01"）。"""
    found = sorted(set(_ITEM.findall(html or "")), key=lambda s: tuple(int(x) for x in s.split(".")))
    return ",".join(found) or None


def xml_doc_from_index(html: str) -> str | None:
    """13D/13G の目次ページ → 生の primary_doc.xml の パス（xsl で整形したものは除く）。"""
    for href in re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', html or ""):
        if "/xsl" not in href:
            return href
    return None


def _tag(x: str, name: str) -> str | None:
    m = re.search(rf"<(?:\w+:)?{name}>([^<]*)</(?:\w+:)?{name}>", x)
    return m.group(1).strip() if m else None


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _mdy(v) -> str | None:
    try:
        return datetime.strptime(str(v), "%m/%d/%Y").date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_13dg(xml: str, meta: dict) -> list[dict]:
    """SCHEDULE 13D/13G の XML → 報告者ごとの行（純関数）。"""
    base = {"filed_date": meta["filed_date"], "accession": meta["accession"], "form": meta["form"],
            "filer_cik": meta["cik"], "issuer_cik": _int(_tag(xml, "issuerCik")),
            "issuer_name": _tag(xml, "issuerName"), "issuer_cusip": _tag(xml, "issuerCusipNumber") or _tag(xml, "issuerCUSIP"),
            "security_class": _tag(xml, "securitiesClassTitle"),
            "event_date": _mdy(_tag(xml, "eventDateRequiresFilingThisStatement") or _tag(xml, "dateOfEvent")),
            "rule": _tag(xml, "designateRulePursuantThisScheduleFiled")}
    out = []
    blocks = re.findall(r"<(?:\w+:)?coverPageHeaderReportingPersonDetails>(.*?)</(?:\w+:)?coverPageHeaderReportingPersonDetails>",
                        xml, re.S) or re.findall(r"<(?:\w+:)?reportingPersonInfo>(.*?)</(?:\w+:)?reportingPersonInfo>", xml, re.S)
    for b in blocks:
        name = _tag(b, "reportingPersonName")
        if not name:
            continue
        out.append({**base, "person_name": name, "person_type": _tag(b, "typeOfReportingPerson"),
                    "citizenship": _tag(b, "citizenshipOrOrganization"),
                    "shares": _num(_tag(b, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")
                                   or _tag(b, "aggregateAmountOwned")),
                    "percent": _num(_tag(b, "classPercent") or _tag(b, "percentOfClass")),
                    "sole_voting": _num(_tag(b, "soleVotingPower")), "shared_voting": _num(_tag(b, "sharedVotingPower")),
                    "sole_dispositive": _num(_tag(b, "soleDispositivePower")),
                    "shared_dispositive": _num(_tag(b, "sharedDispositivePower"))})
    return out


def dates_to_run(state: dict, today: date, recent_days: int, floor: date) -> tuple[list[date], list[date]]:
    import edinet_gov_docs
    return edinet_gov_docs.dates_to_run(state, today, recent_days, floor)


# ── BigQuery ─────────────────────────────────────────────────

def ensure_tables(client) -> None:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    for name, schema, part, cluster in ((T_FILINGS, FILING_SCHEMA, "filed_date", ["form", "cik"]),
                                        (T_13DG, DG_SCHEMA, "filed_date", ["issuer_cik"]),
                                        (T_FILERS, FILER_SCHEMA, None, None)):
        ref = f"{client.project}.{DATASET}.{name}"
        try:
            client.get_table(ref)
        except NotFound:
            t = bigquery.Table(ref, schema=[bigquery.SchemaField(n, ty) for n, ty in schema])
            if part:
                t.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.DAY, field=part)
            if cluster:
                t.clustering_fields = cluster
            client.create_table(t)


def _load(client, table: str, schema, rows: list[dict], day: date | None) -> None:
    from google.cloud import bigquery
    if day is None and not rows:
        return
    cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=(bigquery.WriteDisposition.WRITE_TRUNCATE if day else bigquery.WriteDisposition.WRITE_APPEND),
        schema=[bigquery.SchemaField(n, t) for n, t in schema])
    ref = f"{client.project}.{DATASET}.{table}" + (f"${day:%Y%m%d}" if day else "")
    client.load_table_from_json(rows, ref, job_config=cfg, location=LOCATION).result()


# ── 本体 ─────────────────────────────────────────────────────

def run(cfg, budget_min: float | None = None, only_dates: list[date] | None = None) -> dict:
    ua = (os.environ.get("SEC_USER_AGENT") or str(getattr(cfg, "SEC_USER_AGENT", "") or "")).strip()
    if not ua:
        return {"skipped": True, "reason": "SEC_USER_AGENT missing"}
    import requests
    import bq_storage
    import state_store
    budget = 60 * float(budget_min if budget_min is not None else getattr(cfg, "SEC_FILINGS_TIME_BUDGET_MIN", 20))
    gap = float(getattr(cfg, "SEC_FILINGS_MIN_INTERVAL_SEC", 0.15))
    floor = date.fromisoformat(str(getattr(cfg, "SEC_FILINGS_BACKFILL_FLOOR", "2016-01-01")))
    t0, last = time.monotonic(), 0.0
    session = requests.Session()
    session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})

    def _get(url):
        nonlocal last
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        try:
            return session.get(url, timeout=60)
        except Exception:
            return None
        finally:
            last = time.monotonic()

    client = state_store.make_client(cfg)
    ensure_tables(client)
    m = bq_storage.measure(client)
    bq_storage.record(cfg, m)
    mine = sum(m["tables"].get(f"{DATASET}.{t}", 0.0) for t in (T_FILINGS, T_13DG, T_FILERS))
    if not bq_storage.allowed(cfg, m) or mine >= float(getattr(cfg, "SEC_FILINGS_MAX_GB", 0.6)):
        return {"skipped": True, "reason": f"保存量の上限（全体 {m['total_gb']}GB・この取り込み {mine:.2f}GB）"}
    path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "sec_filings_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    state.setdefault("dates", {})
    known = set(state.get("ciks") or [])
    if only_dates:
        recent, back = list(only_dates), []
    else:
        recent, back = dates_to_run(state, date.today(), int(getattr(cfg, "SEC_FILINGS_RECENT_DAYS", 5)), floor)
    stat = {"days": 0, "filings": 0, "items": 0, "dg_rows": 0, "errors": 0, "budget_out": False, "oldest": None,
            "storage_gb": m["total_gb"], "this_gb": round(mine, 3)}
    for day in recent + back:
        if time.monotonic() - t0 > budget:
            stat["budget_out"] = True
            break
        r = _get(index_url(day))
        if r is None or r.status_code not in (200, 403, 404):
            stat["errors"] += 1
            continue
        if r.status_code != 200:   # 土日・祝日は一覧が無い（403/404）
            _load(client, T_FILINGS, FILING_SCHEMA, [], day)
            state["dates"][day.isoformat()] = {"filings": 0}
            stat["days"] += 1
            continue
        rows, names = parse_master(r.text, day)
        dg_rows, cut = [], False
        for row in rows:
            if time.monotonic() - t0 > budget:
                cut = True
                break
            if row["form"] in ITEM_FORMS:
                ix = _get(filing_index_url(row["cik"], row["accession"]))
                if ix is not None and ix.status_code == 200:
                    row["items"] = items_from_index(ix.text)
                    stat["items"] += 1
            elif row["form"].startswith("SCHEDULE 13"):
                ix = _get(filing_index_url(row["cik"], row["accession"]))
                doc = xml_doc_from_index(ix.text) if ix is not None and ix.status_code == 200 else None
                x = _get("https://www.sec.gov" + doc) if doc else None
                if x is not None and x.status_code == 200:
                    dg_rows += parse_13dg(x.text, row)
        if cut:
            stat["budget_out"] = True
            break   # その日は途中なので記録しない（次の回にやり直す）
        _load(client, T_FILINGS, FILING_SCHEMA, rows, day)
        _load(client, T_13DG, DG_SCHEMA, dg_rows, day)
        new = [{"cik": c, "name": n, "first_seen": day.isoformat()} for c, n in names.items() if c not in known]
        _load(client, T_FILERS, FILER_SCHEMA, new, None)
        known.update(x["cik"] for x in new)
        state["dates"][day.isoformat()] = {"filings": len(rows), "dg": len(dg_rows),
                                           "at": datetime.now(timezone.utc).isoformat()[:19]}
        stat["days"] += 1
        stat["filings"] += len(rows)
        stat["dg_rows"] += len(dg_rows)
    done = sorted(state["dates"])
    stat["oldest"] = done[0] if done else None
    state["ciks"] = sorted(known)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    return stat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-min", type=float)
    ap.add_argument("--dates", nargs="*")
    a = ap.parse_args(argv)
    import config as cfg
    try:
        from run_headless import apply_env_overrides
        apply_env_overrides(cfg)
    except Exception:
        pass
    st = run(cfg, a.budget_min, [date.fromisoformat(d) for d in a.dates] if a.dates else None)
    print(f"{'⏩' if st.get('skipped') else '✅'} sec_filings: {json.dumps(st, ensure_ascii=False)}")
    return 1 if st.get("errors") and not st.get("days") else 0


if __name__ == "__main__":
    sys.exit(main())
