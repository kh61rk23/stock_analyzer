# ==============================================================
# edinetdb_web_backfill.py — EdinetDB の会社ページから財務を補完する（API枠を使わない）
# 2026-10-03 新設
# 【背景】edinet.financial_items（財務三表の細目）は 10/3 に新設したため、取得済みの
#   約480社は空。API の余り枠で1社1回ずつ埋めると数週間かかる。また
#   edinet.financials の fiscal_year_end は全行が空だった。
#   公開の会社ページ（https://edinetdb.jp/company/<EDINETコード>）には、API の
#   /financials と同じ約200項目が全年度ぶん JSON で埋め込まれている（キャメルケース）。
#   これを1秒に1社のペースで読み、financials / financial_items / doc_index を MERGE する。
# 【実行】GitHub Actions の edinetdb_web_backfill.yml（手動実行）。BQ認証は Secrets。
#   python edinetdb_web_backfill.py [--scope ingested|all] [--limit N] [--years N]
# ==============================================================
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import edinetdb_ingestor as ing

# ページのキー名 → API（snake_case）のキー名。機械変換で合わないものだけ
_KEY_OVERRIDES = {
    "doc_i_d": "doc_id",
    "other_ibd_jp": "other_interest_bearing_debt_jp",
    "financial_statement_basis": "basis",
}


def snake(key: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", key)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s).lower()
    s = s.replace("_non_listed", "_nonlisted")
    return _KEY_OVERRIDES.get(s, s)


def extract_financial_records(html: str) -> list[dict]:
    """会社ページに埋め込まれた年次財務レコードを、API と同じキー名で返す（純関数）。

    同じ年度が複数箇所に埋め込まれていることがあるので、値の多い方を残す。
    """
    by_fy: dict[int, dict] = {}
    for m in re.finditer(r'\{[^{}]*"fiscalYear":\s*\d{4}[^{}]*"rndExpenses"[^{}]*\}'
                         r'|\{[^{}]*"rndExpenses"[^{}]*"fiscalYear":\s*\d{4}[^{}]*\}', html or ""):
        try:
            raw = json.loads(m.group(0))
        except ValueError:
            continue
        rec = {snake(k): v for k, v in raw.items()}
        fy = rec.get("fiscal_year")
        if not isinstance(fy, int):
            continue
        filled = sum(v is not None for v in rec.values())
        if fy not in by_fy or filled > sum(v is not None for v in by_fy[fy].values()):
            by_fy[fy] = rec
    return [by_fy[k] for k in sorted(by_fy, reverse=True)]


def build_rows(code: str, company_code: str, records: list[dict], fy_month,
               years: int, now: str) -> dict:
    fin, items, docs = [], [], []
    for rec in records[:max(1, years)]:
        fye = ing.fiscal_year_end_date(rec.get("fiscal_year"), fy_month)
        f = ing.map_edinetdb_financials(rec, edinet_code=code, company_code=company_code, now=now)
        if not f.get("fiscal_year_end"):
            f["fiscal_year_end"] = fye
        fin.append(f)
        items += ing.map_edinetdb_financial_items(
            rec, edinet_code=code, company_code=company_code, fiscal_year_end=fye, now=now)
        d = ing.map_edinetdb_doc_index(
            rec, {"name": rec.get("filer_name")}, edinet_code=code,
            company_code=company_code, now=now)
        if d:
            d["fiscal_year_end"] = d.get("fiscal_year_end") or fye
            docs.append(d)
    return {"financials": fin, "financial_items": items, "doc_index": docs}


def _targets(bq, scope: str, companies: list[dict]) -> list[str]:
    if scope == "all":
        return [c["edinet_code"] for c in companies
                if c.get("edinet_code") and not c.get("is_delisted")]
    rows = bq.client.query(
        f"SELECT DISTINCT edinet_code FROM `{bq.project}.edinet.financials` "
        "WHERE source = 'edinetdb' AND edinet_code IS NOT NULL").result()
    return sorted(r.edinet_code for r in rows)


def run(cfg, scope: str = "ingested", limit: int | None = None, years: int = 10,
        sleep_sec: float = 1.0, flush_every: int = 25) -> dict:
    from edinet_bq_client import EDINETBQClient
    import requests
    bq = EDINETBQClient()
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    companies = (ing._load_json(data_dir / "edinetdb_company_cache.json", {}) or {}).get("companies", [])
    by_code = {c.get("edinet_code"): c for c in companies}
    meta_path = data_dir / getattr(cfg, "EDINETDB_ROTATION_META", "edinetdb_rotation_meta.json")
    meta = ing._load_json(meta_path, {})
    codes = _targets(bq, scope, companies)
    if limit:
        codes = codes[:limit]
    print(f"🌐 EdinetDB Web補完: 対象{len(codes)}社（scope={scope} / 直近{years}年度）")
    now = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()
    buf = {"financials": [], "financial_items": [], "doc_index": []}
    stats = {"ok": 0, "no_data": 0, "error": 0, "rows": {k: 0 for k in buf}}
    done: list[str] = []
    ua = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

    def flush():
        keys = {"financials": ["edinet_code", "fiscal_year"],
                "financial_items": ["item_id"], "doc_index": ["doc_id"]}
        for table, rows in buf.items():
            for i in range(0, len(rows), 4000):
                part = rows[i:i + 4000]
                try:
                    bq.merge_rows(table, part, keys[table])
                    stats["rows"][table] += len(part)
                except Exception as exc:
                    stats["error"] += 1
                    print(f"  ⚠️ {table} MERGE失敗: {type(exc).__name__}: {ing._brief_error(exc)}")
            rows.clear()
        for c in done:
            info = meta.setdefault(c, {})
            info["items_v1"] = today
        done.clear()
        ing._save_json(meta_path, meta)

    for n, code in enumerate(codes, 1):
        if n > 1:
            time.sleep(sleep_sec)          # 相手サイトへの配慮（1秒に1社）
        try:
            r = requests.get(getattr(cfg, "EDINETDB_WEB_BASE", "https://edinetdb.jp/company/") + code,
                             headers=ua, timeout=20)
        except Exception as exc:
            stats["error"] += 1
            print(f"  ⚠️ {code}: 通信失敗 {type(exc).__name__}")
            continue
        page = ing.parse_company_page(r.text, r.status_code)
        records = extract_financial_records(r.text) if r.status_code == 200 else []
        if not records:
            stats["no_data"] += 1
            continue
        company_code = ing._sec4((by_code.get(code) or {}).get("sec_code"))
        fy_month = page.get("fy_month") or ing.infer_fy_month(
            [r.get("submit_date_time") for r in records])
        rows = build_rows(code, company_code, records, fy_month, years, now)
        for k, v in rows.items():
            buf[k] += v
        info = meta.setdefault(code, {})
        if fy_month:
            info["fy_month"] = fy_month
        info["web_check"] = {"date": today, **page}
        info["held_fy"] = max(info.get("held_fy") or 0, records[0].get("fiscal_year") or 0) or None
        done.append(code)
        stats["ok"] += 1
        if n % flush_every == 0:
            flush()
            print(f"  … {n}/{len(codes)}社 / 細目{stats['rows']['financial_items']:,}行")
    flush()
    print(f"✅ 完了: 成功{stats['ok']}社 / データ無し{stats['no_data']}社 / 失敗{stats['error']}件 / "
          f"financials {stats['rows']['financials']:,}行・細目 {stats['rows']['financial_items']:,}行・"
          f"書類 {stats['rows']['doc_index']:,}行")
    return stats


if __name__ == "__main__":
    import config as cfg
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="ingested", choices=["ingested", "all"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--years", type=int, default=10)
    a = ap.parse_args()
    s = run(cfg, a.scope, a.limit, a.years)
    raise SystemExit(0 if s["ok"] else 1)
