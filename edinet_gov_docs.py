# ==============================================================
# edinet_gov_docs.py — 政府 EDINET API v2 の書類一覧と書類の中身（CSV）を BigQuery に溜める
# ==============================================================
# 【表】（すべて load job＝無料。日ごとのパーティションを丸ごと入れ替えるので、何度流しても重ならない）
#   edinet.gov_documents      … 全提出書類の一覧（documents.json?type=2 の全項目。投信も含む）。submit_date で分割
#   edinet.gov_doc_facts      … EDINET_GOV_FACT_DOC_TYPES の書類の XBRL の値（CSV＝type=5）。空欄（－）は入れない。
#                               1行＝1つの値。保存量を抑えるため、要素名は element_id（要素名の SHA1 先頭8バイト）で持ち、
#                               period は「提出日時点（FilingDateInstant）」以外の時だけ、member は提出者コードを外して持つ。
#                               提出者・取得時刻は gov_documents にある（doc_id でつなぐ）
#   edinet.gov_element_labels … element_id → 要素名・日本語の項目名
#   edinet.gov_doc_facts_named … view。値に要素名と項目名を付けたもの（人が読む・調べる用）
#   edinet.large_holdings     … view。大量保有（350/360/370）を「書類×保有者」1行に並べ直したもの
# 【進め方】
#   直近 EDINET_GOV_RECENT_DAYS 日は毎回一覧を取り直す（取り下げ・訂正・遅れて出る書類）。中身は、その日の
#   対象書類の顔ぶれが前回と違う時だけ取り直す。過去分は、済んだ最も古い日の前日から EDINET_GOV_BACKFILL_FLOOR
#   まで遡る。1回の実行は時間の予算（--budget-min）で区切り、途中の日は記録しない（次の回にその日をやり直す）。
#   進み具合は stock_data/data/edinet_gov_docs_state.json（日付ごとの件数と、中身を取った書類の顔ぶれのハッシュ）。
# 【容量】BigQuery の全表が BQ_STORAGE_STOP_GB（bq_storage.py）、または gov_doc_facts が EDINET_GOV_FACTS_MAX_GB を
#   超えたら中身は取らない（一覧だけは続ける。一覧は1日数百行で小さい）。測った値は bq_storage.record で残す。
# 【キー】EDINET_GOV_API_KEY。要求 URL にキーが入るので、例外は型名だけを出す。連続アクセスは
#   EDINET_GOV_MIN_INTERVAL_SEC 以上空ける。
# 【実行】python3 edinet_gov_docs.py [--budget-min 30] [--dates 2026-10-21 ...]
# ==============================================================
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://api.edinet-fsa.go.jp/api/v2"
DATASET = "edinet"
T_DOCS, T_FACTS, T_LABELS, V_LVH, V_NAMED = ("gov_documents", "gov_doc_facts", "gov_element_labels",
                                            "large_holdings", "gov_doc_facts_named")
LOCATION = "asia-northeast1"
EMPTY_VALUES = {"", "－", "-", "―"}

DOC_FIELDS = ("seqNumber", "docID", "edinetCode", "secCode", "JCN", "filerName", "fundCode", "ordinanceCode",
              "formCode", "docTypeCode", "periodStart", "periodEnd", "submitDateTime", "docDescription",
              "issuerEdinetCode", "subjectEdinetCode", "subsidiaryEdinetCode", "currentReportReason",
              "parentDocID", "opeDateTime", "withdrawalStatus", "docInfoEditStatus", "disclosureStatus",
              "xbrlFlag", "pdfFlag", "attachDocFlag", "englishDocFlag", "csvFlag", "legalStatus")


def _snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out).replace("doc_i_d", "doc_id").replace("j_c_n", "jcn")


DOC_COLUMNS = {f: _snake(f) for f in DOC_FIELDS}
DOC_SCHEMA = [(DOC_COLUMNS[f], "INTEGER" if f == "seqNumber" else "STRING") for f in DOC_FIELDS] + [
    ("submit_date", "DATE"), ("fetched_at", "TIMESTAMP")]
FACT_SCHEMA = [("doc_id", "STRING"), ("doc_type_code", "STRING"), ("submit_date", "DATE"),
               ("issuer_edinet_code", "STRING"), ("element_id", "INTEGER"), ("period", "STRING"),
               ("member", "STRING"), ("unit", "STRING"), ("value_num", "FLOAT"), ("value_text", "STRING")]
LABEL_SCHEMA = [("element_id", "INTEGER"), ("element", "STRING"), ("label", "STRING"), ("fetched_at", "TIMESTAMP")]
DEFAULT_PERIOD = "FilingDateInstant"


def element_id(element: str) -> int:
    """要素名 → 64bit の整数（SHA1 の先頭8バイト・符号付き）。BigQuery の INT64 に収まる。"""
    return int.from_bytes(hashlib.sha1(element.encode()).digest()[:8], "big", signed=True)


# ── 純関数 ───────────────────────────────────────────────────

def doc_rows(results: list, day: date, now: str) -> list[dict]:
    """documents.json の results → gov_documents の行。"""
    rows = []
    for r in results or []:
        row = {DOC_COLUMNS[f]: (None if r.get(f) in (None, "") else str(r.get(f))) for f in DOC_FIELDS}
        try:
            row["seq_number"] = int(r.get("seqNumber"))
        except (TypeError, ValueError):
            row["seq_number"] = None
        row["submit_date"] = day.isoformat()
        row["fetched_at"] = now
        rows.append(row)
    return rows


def fact_targets(results: list, doc_types) -> list[dict]:
    """中身を取る書類（対象の種類・CSV あり・取り下げでない）。"""
    return [r for r in results or [] if r.get("docTypeCode") in doc_types and r.get("csvFlag") == "1"
            and r.get("withdrawalStatus") in (None, "0")]


def targets_hash(targets: list) -> str:
    return hashlib.sha1(",".join(sorted(t["docID"] for t in targets)).encode()).hexdigest()[:16]


def split_context(ctx: str) -> tuple[str, str]:
    """FilingDateInstant_jplvh010000-lvh_E35580-000FilerLargeVolumeHolder1Member → (FilingDateInstant, FilerLargeVolumeHolder1Member)。
    member の先頭の提出者コード（E00000-000）は書類ごとに同じなので外す。"""
    parts = (ctx or "").split("_")
    member = parts[-1] if len(parts) > 1 else ""
    if len(member) > 10 and member[0] == "E" and member[6] == "-":
        member = member[10:]
    return parts[0], member


def _number(v: str):
    try:
        return float(v.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def facts_from_csv_zip(blob: bytes, meta: dict, day: date, text_max: int) -> tuple[list[dict], dict]:
    """type=5 の ZIP（XBRL_TO_CSV/*.csv・UTF-16・タブ区切り）→ (gov_doc_facts の行, {要素名: 項目名})。"""
    rows, labels = [], {}
    zf = zipfile.ZipFile(io.BytesIO(blob))
    for name in zf.namelist():
        if not name.endswith(".csv"):
            continue
        lines = zf.read(name).decode("utf-16", errors="replace").splitlines()
        for line in lines[1:]:
            c = [x.strip().strip('"') for x in line.split("\t")]
            if len(c) < 9 or c[8] in EMPTY_VALUES:
                continue
            element, label, ctx, unit, val = c[0], c[1], c[2], c[6], c[8]
            labels.setdefault(element, label)
            period, member = split_context(ctx)
            num = _number(val) if unit and unit not in ("", "-") else None
            rows.append({"doc_id": meta["docID"], "doc_type_code": meta.get("docTypeCode"),
                         "submit_date": day.isoformat(),
                         "issuer_edinet_code": meta.get("issuerEdinetCode") or meta.get("subjectEdinetCode"),
                         "element_id": element_id(element), "period": None if period == DEFAULT_PERIOD else period,
                         "member": member or None, "unit": unit or None,
                         "value_num": num, "value_text": None if num is not None else val[:text_max]})
    return rows, labels


def dates_to_run(state: dict, today: date, recent_days: int, floor: date) -> tuple[list[date], list[date]]:
    """(毎回取り直す直近の日, 遡る過去の日〔新しい順〕)。"""
    recent = [today - timedelta(days=i) for i in range(recent_days)]
    done = [date.fromisoformat(d) for d in (state.get("dates") or {})]
    start = min([d for d in done if d < recent[-1]] or [recent[-1]]) - timedelta(days=1)
    back, d = [], start
    while d >= floor:
        back.append(d)
        d -= timedelta(days=1)
    return recent, back


_LVH_FIELDS_DOC = {"issuer_name": ("NameOfIssuer", "text"), "issuer_sec_code": ("SecurityCodeOfIssuer", "code"),
                   "change_reason": ("ReasonForFilingChangeReportCoverPage", "text"),
                   "obligation_date": ("DateWhenFilingRequirementAroseCoverPage", "text")}
_LVH_FIELDS_HOLDER = {"holder_name": ("Name", "text"), "holder_kind": ("IndividualOrCorporation", "text"),
                      "holder_edinet_code": ("EDINETCodeDEI", "text"),
                      "holding_ratio": ("HoldingRatioOfShareCertificatesEtc", "num"),
                      "prior_ratio": ("HoldingRatioOfShareCertificatesEtcPerLastReport", "num"),
                      "shares_held": ("TotalNumberOfStocksEtcHeld", "num"),
                      "shares_outstanding": ("TotalNumberOfOutstandingStocksEtc", "num"),
                      "purpose": ("PurposeOfHolding", "text"),
                      "funding_total": ("TotalAmountOfFundingForAcquisition", "num"),
                      "funding_borrowings": ("TotalAmountOfBorrowings", "num"),
                      "base_date": ("BaseDate", "text")}


def _pick(col: str, name: str, kind: str) -> str:
    eid = element_id("jplvh_cor:" + name)
    val = {"text": "value_text", "num": "value_num",
           "code": "COALESCE(value_text, CAST(CAST(value_num AS INT64) AS STRING))"}[kind]
    return f"MAX(IF(element_id={eid}, {val}, NULL)) {col}"


def large_holdings_view_sql(project: str) -> str:
    """大量保有（350/360/370）を「書類×保有者」1行に並べ直す view（要素は element_id で引く）。"""
    f = f"`{project}.{DATASET}.{T_FACTS}`"
    d = f"`{project}.{DATASET}.{T_DOCS}`"
    doc_cols = ",\n         ".join(_pick(c, n, k) for c, (n, k) in _LVH_FIELDS_DOC.items())
    hol_cols = ",\n         ".join(_pick(c, n, k) for c, (n, k) in _LVH_FIELDS_HOLDER.items())
    return f"""
CREATE OR REPLACE VIEW `{project}.{DATASET}.{V_LVH}` AS
WITH f AS (SELECT * FROM {f} WHERE doc_type_code IN ('350','360','370')),
doc AS (
  SELECT doc_id, ANY_VALUE(doc_type_code) doc_type_code, ANY_VALUE(submit_date) submit_date,
         ANY_VALUE(issuer_edinet_code) issuer_edinet_code,
         {doc_cols}
  FROM f GROUP BY doc_id
), holder AS (
  SELECT doc_id, member,
         {hol_cols}
  FROM f WHERE member IS NOT NULL GROUP BY doc_id, member
), filer AS (
  SELECT doc_id, ANY_VALUE(edinet_code) filer_edinet_code, ANY_VALUE(filer_name) filer_name
  FROM {d} WHERE doc_type_code IN ('350','360','370') GROUP BY doc_id
)
SELECT doc.*, filer.filer_edinet_code, filer.filer_name, holder.* EXCEPT(doc_id)
FROM doc JOIN holder USING (doc_id) LEFT JOIN filer USING (doc_id)
"""


def named_view_sql(project: str) -> str:
    return f"""
CREATE OR REPLACE VIEW `{project}.{DATASET}.{V_NAMED}` AS
SELECT f.*, l.element, l.label
FROM `{project}.{DATASET}.{T_FACTS}` f
LEFT JOIN (SELECT element_id, ANY_VALUE(element) element, ANY_VALUE(label) label
           FROM `{project}.{DATASET}.{T_LABELS}` GROUP BY element_id) l USING (element_id)
"""


# ── BigQuery ─────────────────────────────────────────────────

def _client(cfg):
    import state_store
    return state_store.make_client(cfg)


def ensure_tables(client) -> None:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    for name, schema, part in ((T_DOCS, DOC_SCHEMA, "submit_date"), (T_FACTS, FACT_SCHEMA, "submit_date"),
                               (T_LABELS, LABEL_SCHEMA, None)):
        ref = f"{client.project}.{DATASET}.{name}"
        try:
            client.get_table(ref)
        except NotFound:
            t = bigquery.Table(ref, schema=[bigquery.SchemaField(n, ty) for n, ty in schema])
            if part:
                t.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.DAY, field=part)
            if name == T_FACTS:
                t.clustering_fields = ["doc_type_code", "issuer_edinet_code", "element_id"]
            client.create_table(t)
    for sql in (large_holdings_view_sql(client.project), named_view_sql(client.project)):
        client.query(sql, location=LOCATION).result()


def load_partition(client, table: str, schema, rows: list[dict], day: date) -> None:
    """その日のパーティションを rows で丸ごと入れ替える（rows が空なら空にする）。"""
    from google.cloud import bigquery
    cfg = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                                 write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                                 schema=[bigquery.SchemaField(n, t) for n, t in schema])
    ref = f"{client.project}.{DATASET}.{table}${day.strftime('%Y%m%d')}"
    client.load_table_from_json(rows, ref, job_config=cfg, location=LOCATION).result()


def append_rows(client, table: str, schema, rows: list[dict]) -> None:
    from google.cloud import bigquery
    if not rows:
        return
    cfg = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                                 write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                                 schema=[bigquery.SchemaField(n, t) for n, t in schema])
    client.load_table_from_json(rows, f"{client.project}.{DATASET}.{table}", job_config=cfg,
                                location=LOCATION).result()


# ── 本体 ─────────────────────────────────────────────────────

def _state_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "edinet_gov_docs_state.json"


def run(cfg, budget_min: float | None = None, only_dates: list[date] | None = None) -> dict:
    key = (os.environ.get("EDINET_GOV_API_KEY") or str(getattr(cfg, "EDINET_GOV_API_KEY", "") or "")).strip()
    if not key:
        return {"skipped": True, "reason": "EDINET_GOV_API_KEY missing"}
    import requests
    budget = 60 * float(budget_min if budget_min is not None else getattr(cfg, "EDINET_GOV_DOCS_TIME_BUDGET_MIN", 30))
    gap = float(getattr(cfg, "EDINET_GOV_MIN_INTERVAL_SEC", 1.0))
    types = tuple(getattr(cfg, "EDINET_GOV_FACT_DOC_TYPES", ("350", "360", "370", "180", "190")))
    text_max = int(getattr(cfg, "EDINET_GOV_TEXT_MAX_CHARS", 2000))
    floor = date.fromisoformat(str(getattr(cfg, "EDINET_GOV_BACKFILL_FLOOR", "2016-01-01")))
    t0, last = time.monotonic(), 0.0

    def _get(url, **params):
        nonlocal last
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(url, params={**params, "Subscription-Key": key}, timeout=120)
        except Exception as e:
            return None, type(e).__name__
        finally:
            last = time.monotonic()
        return r, ""

    client = _client(cfg)
    ensure_tables(client)
    import bq_storage
    m = bq_storage.measure(client)
    bq_storage.record(cfg, m)
    total_gb, facts_gb = m["total_gb"], m["tables"].get(f"{DATASET}.{T_FACTS}", 0.0)
    facts_ok = bq_storage.allowed(cfg, m) and facts_gb < float(getattr(cfg, "EDINET_GOV_FACTS_MAX_GB", 2.5))
    path = _state_path(cfg)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    state.setdefault("dates", {})
    known_labels = set(state.get("labels") or [])
    today = date.today()
    if only_dates:
        recent, back = list(only_dates), []
    else:
        recent, back = dates_to_run(state, today, int(getattr(cfg, "EDINET_GOV_RECENT_DAYS", 7)), floor)
    stat = {"days": 0, "docs": 0, "fact_docs": 0, "facts": 0, "errors": 0, "storage_gb": round(total_gb, 2),
            "facts_gb": round(facts_gb, 3), "facts_paused": not facts_ok, "oldest": None, "budget_out": False,
            "range": None}
    done_now: list[str] = []
    for day in recent + back:
        if time.monotonic() - t0 > budget:
            stat["budget_out"] = True
            break
        r, err = _get(f"{API}/documents.json", date=day.isoformat(), type=2)
        if r is None or r.status_code != 200:
            stat["errors"] += 1
            print(f"    ❌ 一覧 {day}: {err or r.status_code}")
            if r is not None and r.status_code == 429:
                break
            continue
        try:
            results = r.json().get("results") or []
        except ValueError:
            stat["errors"] += 1
            continue
        now = datetime.now(timezone.utc).isoformat()
        load_partition(client, T_DOCS, DOC_SCHEMA, doc_rows(results, day, now), day)
        entry = dict(state["dates"].get(day.isoformat()) or {})
        entry.update({"docs": len(results), "at": now[:19]})
        targets = fact_targets(results, types)
        h = targets_hash(targets)
        if facts_ok and entry.get("facts_hash") != h:
            rows, failed = [], 0
            for t in targets:
                if time.monotonic() - t0 > budget:
                    break
                z, zerr = _get(f"{API}/documents/{t['docID']}", type=5)
                if z is None or z.status_code != 200 or not z.content.startswith(b"PK"):
                    failed += 1
                    continue
                try:
                    frows, labels = facts_from_csv_zip(z.content, t, day, text_max)
                except Exception as e:
                    print(f"    ⚠️ {t['docID']}: {type(e).__name__}")
                    failed += 1
                    continue
                rows += frows
                new = [{"element_id": element_id(k), "element": k, "label": v, "fetched_at": now}
                       for k, v in labels.items() if k not in known_labels]
                if new:
                    append_rows(client, T_LABELS, LABEL_SCHEMA, new)
                    known_labels.update(x["element"] for x in new)
                stat["fact_docs"] += 1
            if time.monotonic() - t0 > budget:
                stat["budget_out"] = True
                break   # その日は途中なので記録しない（次の回にやり直す）
            load_partition(client, T_FACTS, FACT_SCHEMA, rows, day)
            stat["facts"] += len(rows)
            stat["errors"] += failed
            if not failed:
                entry["facts_hash"] = h
            entry["fact_docs"] = len(targets) - failed
        state["dates"][day.isoformat()] = entry
        done_now.append(day.isoformat())
        stat["days"] += 1
        stat["docs"] += len(results)
    done = sorted(state["dates"])
    stat["oldest"] = done[0] if done else None
    stat["range"] = [min(done_now), max(done_now)] if done_now else None   # この回に入れた日（通知が中身を引く範囲）
    state["labels"] = sorted(known_labels)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    return stat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-min", type=float, help="この回に使う時間（分）。既定は EDINET_GOV_DOCS_TIME_BUDGET_MIN")
    ap.add_argument("--dates", nargs="*", help="この日だけ取る（YYYY-MM-DD）")
    a = ap.parse_args(argv)
    import config as cfg
    try:
        from run_headless import apply_env_overrides
        apply_env_overrides(cfg)
    except Exception:
        pass
    st = run(cfg, a.budget_min, [date.fromisoformat(d) for d in a.dates] if a.dates else None)
    print(f"{'⏩' if st.get('skipped') else '✅'} edinet_gov_docs: {json.dumps(st, ensure_ascii=False)}")
    return 1 if st.get("errors") and not st.get("days") else 0


if __name__ == "__main__":
    sys.exit(main())
