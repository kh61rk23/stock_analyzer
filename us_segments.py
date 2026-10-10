# ==============================================================
# us_segments.py
# 米国株のセグメント別（事業・製品/サービス・地域）の売上と営業利益を、SEC の
# Financial Statement Data Sets（四半期ごとの zip・無料・一括）から取り、BigQuery の
# us.segment_financials へ追記する。直近5年分だけ。
# ==============================================================
# 【なぜ companyfacts ではないか】
#   companyfacts（us.company_facts）は次元の無い全社合計だけを集約しており、セグメントの値は入らない。
#   次元つきの値（例: 売上 × ProductOrService=iPhone）は Financial Statement Data Sets の num.txt の
#   segments 列（"Axis=Member;"）にだけある。
#
# 【取り込む範囲】
#   軸が1つだけの行（BusinessSegments・ProductOrService・Geographical）、売上系タグと OperatingIncomeLoss、
#   USD、四半期(qtrs=1)と通期(qtrs=4)、上場ユニバース（us_company_facts/universe.json）の CIK、
#   期末が直近 KEEP_YEARS 年。軸が2つ以上の組合せ・連結消去・株主資本の内訳などは入れない。
#   1四半期ぶんで約3万行、5年で約70万行（約100MB）。
#
# 【更新】
#   取り込み済みの四半期は state.json の segments_done に持つ。SEC が新しい四半期の zip を公開すると
#   次回の実行で追加する（未公開は 404 なので何もしない）。load job は WRITE_APPEND、job_id は四半期ごとに固定で、
#   再実行しても二重に入らない。同じ値が別の四半期の提出に再掲される（通期の比較列）ため、読む側は
#   (cik, axis, member, tag, period_end, qtrs) で提出日が新しい行を採る。
# ==============================================================

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import sec_client
import us_company_facts as ucf

ZIP_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{y}q{q}.zip"
SEGMENT_TABLE = "segment_financials"
KEEP_YEARS = 5
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F"}
AXES = {"BusinessSegments", "ProductOrService", "Geographical"}
REVENUE_TAGS = {
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense",
}
INCOME_TAGS = {"OperatingIncomeLoss"}
KEEP_TAGS = REVENUE_TAGS | INCOME_TAGS
NDJSON_NAME = "us_segments_rows.ndjson"


def quarters_back(today: date, years: int = KEEP_YEARS) -> list:
    """取りに行く四半期 [(y, q)] を古い順に。直近の2四半期前（公開待ち）まで含む。"""
    qi = today.year * 4 + (today.month - 1) // 3
    first = (today.year - years) * 4 + (today.month - 1) // 3
    return [(i // 4, i % 4 + 1) for i in range(first, qi + 1)]


def parse_segments(text: str) -> tuple | None:
    """'Axis=Member;' の1軸だけの文字列から (axis, member)。軸が複数・対象外の軸は None。"""
    parts = [p for p in (text or "").split(";") if p]
    if len(parts) != 1 or "=" not in parts[0]:
        return None
    axis, member = parts[0].split("=", 1)
    return (axis, member) if axis in AXES and member else None


def _ymd(s: str) -> str | None:
    s = (s or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else None


def extract_rows(sub_text, num_lines, ciks: set, cutoff: str) -> list:
    """sub.txt と num.txt（どちらも TSV）から行を作る。同じキーは提出日が新しい方に畳む。"""
    csv.field_size_limit(10 ** 9)
    subs = {}
    for x in csv.DictReader(io.StringIO(sub_text) if isinstance(sub_text, str) else sub_text, delimiter="\t"):
        try:
            cik = int(x["cik"])
        except (KeyError, ValueError):
            continue
        if cik in ciks and x.get("form") in FORMS:
            subs[x["adsh"]] = x
    best: dict = {}
    for x in csv.DictReader(num_lines, delimiter="\t"):
        s = subs.get(x.get("adsh"))
        if not s or x.get("tag") not in KEEP_TAGS or x.get("uom") != "USD" or x.get("coreg") or x.get("qtrs") not in ("1", "4"):
            continue
        seg = parse_segments(x.get("segments"))
        end = _ymd(x.get("ddate"))
        if not seg or not end or end < cutoff:
            continue
        try:
            val = float(x["value"])
        except (KeyError, TypeError, ValueError):
            continue
        filed = _ymd(s.get("filed"))
        key = (int(s["cik"]), seg[0], seg[1], x["tag"], end, int(x["qtrs"]))
        cur = best.get(key)
        if cur is None or (filed or "") > (cur["filed"] or ""):
            best[key] = {
                "cik": int(s["cik"]), "axis": seg[0], "member": seg[1], "tag": x["tag"],
                "period_end": end, "qtrs": int(x["qtrs"]), "val": val, "uom": "USD",
                "accn": s["adsh"], "form": s.get("form"), "fy": int(s["fy"]) if (s.get("fy") or "").isdigit() else None,
                "fp": s.get("fp") or None, "filed": filed,
            }
    return list(best.values())


def schema():
    from google.cloud import bigquery as bq
    F = bq.SchemaField
    return [
        F("cik", "INT64", "REQUIRED"), F("axis", "STRING", "REQUIRED"), F("member", "STRING", "REQUIRED"),
        F("tag", "STRING", "REQUIRED"), F("period_end", "DATE", "REQUIRED"), F("qtrs", "INT64"),
        F("val", "FLOAT64"), F("uom", "STRING"), F("accn", "STRING"), F("form", "STRING"),
        F("fy", "INT64"), F("fp", "STRING"), F("filed", "DATE"),
    ]


def ensure_table(client):
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    ref = f"{client.project}.us.{SEGMENT_TABLE}"
    try:
        return client.get_table(ref)
    except NotFound:
        t = bigquery.Table(ref, schema=schema())
        t.clustering_fields = ["cik", "axis"]
        return client.create_table(t)


def append_rows(client, rows: list, job_id: str, tmp: Path) -> None:
    from google.api_core.exceptions import Conflict
    from google.cloud import bigquery
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    cfg = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON, schema=schema(),
                                 write_disposition=bigquery.WriteDisposition.WRITE_APPEND)
    try:
        with open(tmp, "rb") as fh:
            client.load_table_from_file(fh, f"{client.project}.us.{SEGMENT_TABLE}", job_id=job_id, job_config=cfg,
                                        location=ucf.BQ_LOCATION).result()
    except Conflict:
        old = client.get_job(job_id, location=ucf.BQ_LOCATION)
        if not (old.state == "DONE" and not old.error_result):
            raise RuntimeError(f"load job {job_id} は既にあるが失敗している（job_id を変えて再実行）")
        print(f"  ↩️ {job_id} は成功済み")


def fetch_quarter(y: int, q: int, cfg):
    """(sub_text, num_lines) または None（未公開）。zip はメモリ上で読み、num.txt は行ごとに流す。"""
    resp = sec_client.get(ZIP_URL.format(y=y, q=q), cfg=cfg, accept="application/zip", timeout=600, max_retries=2)
    if resp is None:
        raise RuntimeError(f"{y}q{q}: 取得に失敗（SEC 側の制限・通信断）")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"{y}q{q}: HTTP {resp.status_code}")
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    sub = zf.read("sub.txt").decode("utf-8", "replace")
    num = io.TextIOWrapper(zf.open("num.txt"), encoding="utf-8", errors="replace", newline="")
    return sub, num


def run(cfg, client, dry_run=False, only: str | None = None) -> dict:
    t0 = time.monotonic()
    today = datetime.now(timezone.utc).date()
    ucf.configure_sec_rate()
    state_dir = ucf._state_dir(cfg)
    state_path = state_dir / "state.json"
    state = ucf._read_json(state_path, {})
    done = set(state.get("segments_done") or [])
    universe = ucf._read_json(state_dir / "universe.json", {})
    ciks = {int(v["cik"]) for v in universe.values()}
    if len(ciks) < 1000:
        raise RuntimeError("universe.json が小さすぎる（us_company_facts を先に実行）")
    cutoff = date(today.year - KEEP_YEARS, today.month, 1).isoformat()
    tmp = state_dir.parent.parent / "cache" / NDJSON_NAME
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if client and not dry_run:
        ensure_table(client)
    out = {"quarters_done": [], "missing": [], "rows": 0}
    for y, q in quarters_back(today):
        label = f"{y}q{q}"
        if (only and label != only) or label in done:
            continue
        got = fetch_quarter(y, q, cfg)
        if got is None:
            out["missing"].append(label)
            continue
        rows = extract_rows(got[0], got[1], ciks, cutoff)
        print(f"📦 {label}: {len(rows)} 行")
        if not dry_run and client:
            if rows:
                append_rows(client, rows, f"ussegments_{label}", tmp)
            done.add(label)
            state["segments_done"] = sorted(done)
            ucf._write_json(state_path, state)
        out["quarters_done"].append(label)
        out["rows"] += len(rows)
    tmp.unlink(missing_ok=True)
    out["elapsed_sec"] = int(time.monotonic() - t0)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="例: 2025q3（試験用）")
    args = ap.parse_args(argv)
    cfg = ucf._load_cfg()
    client = None
    try:
        client = ucf.bq_client(cfg)
    except Exception:
        if not args.dry_run:
            raise
    try:
        info = run(cfg, client, args.dry_run, args.only)
        status, note = "success", f"追加 {info['quarters_done']} / {info['rows']} 行 / 未公開 {info['missing']}"
    except Exception as e:
        info, status, note = {}, "error", f"{type(e).__name__}: {str(e)[:150]}"
    print(f"📒 us_segments: {status} {note}")
    if not args.dry_run and not args.only:
        try:
            from module_ledger import record_external
            record_external(cfg, "us_segments", status, note)
        except Exception as e:
            print(f"⚠️ 台帳への記録に失敗: {str(e)[:80]}")
    if status == "error":
        ucf._notify(cfg, "🚨 米国株のセグメント取得に問題", f"us_segments: {note}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
