# ==============================================================
# edinet_gov_text.py — 政府 EDINET API v2 から有報の本文（テキストブロック）を全上場ぶん取り、BigQuery に溜める
# ==============================================================
# 【なぜ】有報の本文は edinet.text_analysis（EdinetDB API・1日100回の枠）で約580社しか無く、技術マップの語の照合
#   （taxonomy_company_mapper の text_match）が広がらない。政府 EDINET は回数の上限が無く、有報の CSV（type=5）に
#   節ごとの本文がそのまま入っている（1社 約180KB・1秒前後）。
# 【表】edinet.gov_text_blocks（load job で追記）：doc_id・edinet_code・sec_code・fiscal_year_end・submit_date・
#   section_type（text_analysis と同じ名前）・element・raw_text（SECTIONS の節だけ・EDINET_GOV_TEXT_SECTION_MAX_CHARS で切る）・fetched_at。
#   同じ doc_id を2回入れないよう、取った doc_id は stock_data/data/edinet_gov_text_state.json に残す。
# 【対象】会社ごとの最新の有報（edinet.doc_index の有価証券報告書と、edinet.gov_documents の docTypeCode=120 の新しい方）。
#   取っていない doc_id を新しい順に、時間の予算（--budget-min）まで取る。
# 【容量】全体が BQ_STORAGE_STOP_GB、またはこの表が EDINET_GOV_TEXT_MAX_GB を超えたら取らない。
# 【キー】EDINET_GOV_API_KEY。例外は型名だけを出す（要求 URL にキーが入るため）。
# 【実行】python3 edinet_gov_text.py [--budget-min 60] [--limit N]
# ==============================================================
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.edinet-fsa.go.jp/api/v2"
DATASET, TABLE, LOCATION = "edinet", "gov_text_blocks", "asia-northeast1"
SECTIONS = {
    "jpcrp_cor:DescriptionOfBusinessTextBlock": "事業の内容",
    "jpcrp_cor:ResearchAndDevelopmentActivitiesTextBlock": "研究開発活動",
    "jpcrp_cor:BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock": "事業方針・経営環境",
    "jpcrp_cor:BusinessRisksTextBlock": "事業等のリスク",
    "jpcrp_cor:ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock": "経営者による分析",
    "jpcrp_cor:OverviewOfCapitalExpendituresEtcTextBlock": "設備投資等の概要",
    "jpcrp_cor:MajorShareholdersTextBlock": "大株主の状況",
    "jpcrp_cor:OverviewOfAffiliatedEntitiesTextBlock": "関係会社の状況",
    "jpcrp_cor:CriticalContractsTextBlock": "重要な契約等",
    "jpcrp_cor:CompanyHistoryTextBlock": "沿革",
    "jpcrp_cor:DisclosureOfSustainabilityRelatedFinancialInformationTextBlock": "サステナビリティ",
    "jpcrp_cor:InformationAboutEmployeesTextBlock": "従業員の状況",
}
SCHEMA = [("doc_id", "STRING"), ("edinet_code", "STRING"), ("sec_code", "STRING"), ("fiscal_year_end", "DATE"),
          ("submit_date", "DATE"), ("section_type", "STRING"), ("element", "STRING"), ("raw_text", "STRING"),
          ("fetched_at", "TIMESTAMP")]

TARGET_SQL = """
WITH idx AS (
  SELECT edinet_code, doc_id, CAST(fiscal_year_end AS DATE) fye, CAST(disclosure_date AS DATE) d, CAST(NULL AS STRING) sec
  FROM `{p}.edinet.doc_index` WHERE doc_type = '有価証券報告書'
  UNION ALL
  SELECT edinet_code, doc_id, SAFE_CAST(period_end AS DATE), submit_date, sec_code
  FROM `{p}.edinet.gov_documents`
  WHERE doc_type_code = '120' AND csv_flag = '1' AND withdrawal_status = '0' AND edinet_code IS NOT NULL
    AND ordinance_code = '010' AND fund_code IS NULL   -- 会社の有報だけ（投資信託の有報は除く）
)
SELECT edinet_code, doc_id, fye, d, sec FROM idx
QUALIFY ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY d DESC, doc_id DESC) = 1
ORDER BY d DESC
"""


def blocks_from_zip(blob: bytes, max_chars: int) -> list[tuple[str, str, str]]:
    """有報の type=5 ZIP → [(element, section_type, 本文)]（純関数）。同じ節が複数あれば最初の1つ（提出日時点）。"""
    out, seen = [], set()
    zf = zipfile.ZipFile(io.BytesIO(blob))
    for name in zf.namelist():
        if not (name.endswith(".csv") and "jpcrp" in name):
            continue
        for line in zf.read(name).decode("utf-16", errors="replace").splitlines()[1:]:
            c = [x.strip().strip('"') for x in line.split("\t")]
            if len(c) < 9 or c[0] not in SECTIONS or c[0] in seen or not c[8] or c[8] in ("－", "-"):
                continue
            seen.add(c[0])
            out.append((c[0], SECTIONS[c[0]], c[8][:max_chars]))
    return out


def _client(cfg):
    import state_store
    return state_store.make_client(cfg)


def ensure_table(client) -> None:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    ref = f"{client.project}.{DATASET}.{TABLE}"
    try:
        client.get_table(ref)
    except NotFound:
        t = bigquery.Table(ref, schema=[bigquery.SchemaField(n, ty) for n, ty in SCHEMA])
        t.clustering_fields = ["edinet_code", "section_type"]
        client.create_table(t)


def _append(client, rows: list[dict]) -> None:
    from google.cloud import bigquery
    if not rows:
        return
    cfg = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                                 write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                                 schema=[bigquery.SchemaField(n, t) for n, t in SCHEMA])
    client.load_table_from_json(rows, f"{client.project}.{DATASET}.{TABLE}", job_config=cfg, location=LOCATION).result()


def run(cfg, budget_min: float | None = None, limit: int | None = None) -> dict:
    key = (os.environ.get("EDINET_GOV_API_KEY") or str(getattr(cfg, "EDINET_GOV_API_KEY", "") or "")).strip()
    if not key:
        return {"skipped": True, "reason": "EDINET_GOV_API_KEY missing"}
    import requests
    import bq_storage
    budget = 60 * float(budget_min if budget_min is not None else getattr(cfg, "EDINET_GOV_TEXT_TIME_BUDGET_MIN", 60))
    gap = float(getattr(cfg, "EDINET_GOV_MIN_INTERVAL_SEC", 1.0))
    max_chars = int(getattr(cfg, "EDINET_GOV_TEXT_SECTION_MAX_CHARS", 20000))
    client = _client(cfg)
    ensure_table(client)
    m = bq_storage.measure(client)
    bq_storage.record(cfg, m)
    mine = m["tables"].get(f"{DATASET}.{TABLE}", 0.0)
    if not bq_storage.allowed(cfg, m) or mine >= float(getattr(cfg, "EDINET_GOV_TEXT_MAX_GB", 0.4)):
        return {"skipped": True, "reason": f"保存量の上限（全体 {m['total_gb']}GB・この表 {mine:.3f}GB）"}
    path = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "edinet_gov_text_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    done = set(state.get("doc_ids") or [])
    targets = [r for r in client.query(TARGET_SQL.format(p=client.project), location=LOCATION).result()
               if r.doc_id not in done]
    if limit:
        targets = targets[:limit]
    t0, last = time.monotonic(), 0.0
    stat = {"targets": len(targets), "docs": 0, "rows": 0, "no_text": 0, "errors": 0, "budget_out": False,
            "table_gb": round(mine, 3), "storage_gb": m["total_gb"]}
    buf: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    import edinet_gov_api as ega
    streak = ega.Streak()
    for r in targets:
        if time.monotonic() - t0 > budget:
            stat["budget_out"] = True
            break
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        try:
            z = requests.get(f"{API}/documents/{r.doc_id}", params={"type": 5, "Subscription-Key": key}, timeout=120)
        except Exception as e:
            stat["errors"] += 1
            print(f"    ❌ {r.edinet_code} {r.doc_id}: {type(e).__name__}")
            last = time.monotonic()
            continue
        last = time.monotonic()
        ok, code, why = ega.check_zip(z)
        if not ok:
            stat["errors"] += 1
            if streak.fail(code, why):
                print(f"    ⏹️ {streak.stopped}")
                break
            continue
        streak.ok()
        try:
            blocks = blocks_from_zip(z.content, max_chars)
        except Exception as e:
            print(f"    ⚠️ {r.doc_id}: {type(e).__name__}")
            stat["errors"] += 1
            continue
        if not blocks:
            stat["no_text"] += 1
        for element, section, text in blocks:
            buf.append({"doc_id": r.doc_id, "edinet_code": r.edinet_code, "sec_code": r.sec,
                        "fiscal_year_end": r.fye.isoformat() if r.fye else None,
                        "submit_date": r.d.isoformat() if r.d else None, "section_type": section,
                        "element": element, "raw_text": text, "fetched_at": now})
        done.add(r.doc_id)
        stat["docs"] += 1
        if len(buf) >= 2000:
            _append(client, buf)
            stat["rows"] += len(buf)
            buf = []
            state["doc_ids"] = sorted(done)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    _append(client, buf)
    stat["rows"] += len(buf)
    state["doc_ids"] = sorted(done)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    stat["remaining"] = max(0, stat["targets"] - stat["docs"])
    streak.put(stat)
    return stat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-min", type=float)
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    import config as cfg
    try:
        from run_headless import apply_env_overrides
        apply_env_overrides(cfg)
    except Exception:
        pass
    st = run(cfg, a.budget_min, a.limit)
    import edinet_gov_api as ega
    mark, rc = ega.outcome(st, "docs")
    print(f"{mark} edinet_gov_text: {json.dumps(st, ensure_ascii=False)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
