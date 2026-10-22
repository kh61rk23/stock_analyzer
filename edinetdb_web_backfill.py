# ==============================================================
# edinetdb_web_backfill.py — EdinetDB の会社ページから財務を補完する（API枠を使わない）
# 【停止中】Web サイトをプログラムで読むのは EdinetDB の利用規約（https://edinetdb.com/legal/terms）で禁止。
#   config EDINETDB_WEB_SCRAPE_ALLOWED=False の間は何もしない。年次財務は edinetdb_ingestor が API で取る。
# 【背景】API の余り枠で1社1回ずつ財務を埋めると数週間かかる。
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


def re_name(name: str) -> str:
    return re.sub(r"(株式会社|（株）)", "", str(name or "")).strip()


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


def due_targets(meta: dict, companies: list[dict], today: date | None = None) -> list[str]:
    """日次のWeb更新対象（純関数）。①新しい年度の有報が出ているはずの会社 ②未取得の会社。

    年次財務は会社ページから取る。有報の提出は6月下旬（3月決算）などに
    集中するので、新年度の会社を先に、未取得の会社を後に並べる。財務なしと確認済みの会社は除く。
    """
    today = today or date.today()
    listed = [c.get("edinet_code") for c in companies
              if c.get("edinet_code") and not c.get("is_delisted")]
    due, never = [], []
    for code in listed:
        info = meta.get(code) or {}
        if (info.get("web_check") or {}).get("has") is False:
            continue
        if not info.get("items_v1"):
            nr = str(info.get("web_no_records") or "")[:10]
            try:
                if nr and (today - date.fromisoformat(nr)).days < 30:
                    continue          # 30日以内に「財務なし」を確認済み
            except ValueError:
                pass
            never.append(code)
        elif ing.report_due({**info, "last_fetched": info.get("items_v1")}, today):
            due.append(code)
    return due + never


def _targets(bq, scope: str, companies: list[dict], meta: dict | None = None) -> list[str]:
    if scope == "due":
        return due_targets(meta or {}, companies)
    if scope == "all":
        return [c["edinet_code"] for c in companies
                if c.get("edinet_code") and not c.get("is_delisted")]
    rows = bq.client.query(
        f"SELECT DISTINCT edinet_code FROM `{bq.project}.edinet.financials` "
        "WHERE source = 'edinetdb' AND edinet_code IS NOT NULL").result()
    return sorted(r.edinet_code for r in rows)


def run(cfg, scope: str = "ingested", limit: int | None = None, years: int = 10,
        sleep_sec: float = 1.0, flush_every: int = 25, redo: bool = False,
        time_budget_min: float | None = None) -> dict:
    if not getattr(cfg, "EDINETDB_WEB_SCRAPE_ALLOWED", False):
        # 会社ページをプログラムで読むのは EdinetDB の利用規約で禁止。年次財務は edinetdb_ingestor が API で取る
        print("⏩ EdinetDB の Web サイトの読み取りは利用規約で禁止のため何もしない（EDINETDB_WEB_SCRAPE_ALLOWED=False）")
        return {"ok": 0, "no_data": 0, "error": 0, "skipped": True, "paused": False,
                "rows": {"financials": 0, "financial_items": 0, "doc_index": 0}}
    from edinet_bq_client import EDINETBQClient
    import requests
    bq = EDINETBQClient()
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    companies = (ing._load_json(data_dir / "edinetdb_company_cache.json", {}) or {}).get("companies", [])
    by_code = {c.get("edinet_code"): c for c in companies}
    meta_path = data_dir / getattr(cfg, "EDINETDB_ROTATION_META", "edinetdb_rotation_meta.json")
    meta = ing._load_json(meta_path, {})
    codes = _targets(bq, scope, companies, meta)
    if scope == "due":
        redo = True        # 新年度の会社は30日以内に補完済みでも取り直す
    # 全社（約4,000社）は6時間のジョブ上限を超えうるので、直近に補完済みの会社は
    # 飛ばして続きから再開できるようにする（--redo で全部やり直し）
    if not redo:
        cutoff = date.today().toordinal() - 30
        def _recent(c):
            try:
                return date.fromisoformat(str((meta.get(c) or {}).get("items_v1") or "")[:10]).toordinal() >= cutoff
            except ValueError:
                return False
        skipped = [c for c in codes if _recent(c)]
        codes = [c for c in codes if not _recent(c)]
        if skipped:
            print(f"  ⏩ 30日以内に補完済み {len(skipped)}社は飛ばす")
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

    t_start = time.time()
    for n, code in enumerate(codes, 1):
        if time_budget_min and (time.time() - t_start) / 60 > time_budget_min:
            print(f"  ⏸ 時間上限 {time_budget_min:.0f}分に達したので中断（次回は続きから）: {n - 1}/{len(codes)}社")
            stats["paused"] = True
            break
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
            # 財務の載っていない会社を記録する。記録しないと日次の --scope due が
            # 毎日同じ会社を先頭から取り直し、枠を「データ無し」で使い切る
            stats["no_data"] += 1
            meta.setdefault(code, {})["web_no_records"] = today
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
        stats.setdefault("companies", []).append({
            "edinet_code": code, "sec_code": company_code,
            "name": re_name((by_code.get(code) or {}).get("name") or records[0].get("filer_name") or ""),
            "fiscal_year": records[0].get("fiscal_year")})
        if n % flush_every == 0:
            flush()
            print(f"  … {n}/{len(codes)}社 / 細目{stats['rows']['financial_items']:,}行")
    flush()
    # 日次メール（EdinetDB節）が読む本日の結果
    try:
        (data_dir / "edinetdb_web_last.json").write_text(json.dumps({
            "date": today, "scope": scope, "ok": stats["ok"], "no_data": stats["no_data"],
            "error": stats["error"], "financials": stats["rows"]["financials"],
            "items": stats["rows"]["financial_items"], "companies": stats.get("companies", [])[:50],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    print(f"✅ 完了: 成功{stats['ok']}社 / データ無し{stats['no_data']}社 / 失敗{stats['error']}件 / "
          f"financials {stats['rows']['financials']:,}行・細目 {stats['rows']['financial_items']:,}行・"
          f"書類 {stats['rows']['doc_index']:,}行")
    return stats


if __name__ == "__main__":
    import config as cfg
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="ingested", choices=["ingested", "all", "due"])
    ap.add_argument("--limit", type=int, default=None)  # due の既定は config.EDINETDB_WEB_DAILY_LIMIT
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--redo", action="store_true", help="補完済みも含めてやり直す")
    ap.add_argument("--time-budget-min", type=float, default=None)
    a = ap.parse_args()
    lim = a.limit if a.limit is not None else (
        int(getattr(cfg, "EDINETDB_WEB_DAILY_LIMIT", 100)) if a.scope == "due" else None)
    s = run(cfg, a.scope, lim, a.years, redo=a.redo, time_budget_min=a.time_budget_min)
    # GitHub Actions へ「時間切れで中断したか」を渡す（続きを自動で起動するため）
    import os
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"paused={'true' if s.get('paused') else 'false'}\n")
    raise SystemExit(0 if s["ok"] or s["error"] == 0 else 1)
