# ==============================================================
# seisakudb_ingestor.py — 政策DB（seisakudb.jp, Cabocia株式会社）の取り込み
#
# 仕様: https://seisakudb.jp/v1/openapi.json（REST /v1/*・Authorization: Bearer <SEISAKUDB_API_KEY>）
#   課金単位は「認証付きHTTPリクエスト1回＝1 unit」（4xx/5xx は0）。Free プランは1日100回。
#   データは月次更新（公的支出は FY2021〜の5年分）なので、個社は30日に1回取り直せば足りる。
#   利用規約: Webページ（/company/* 等）の自動取得は禁止。API だけを使う。出典「政策DB」を表示する。
#
# 取り込むもの（1日の上限 SEISAKUDB_DAILY_BUDGET 回）
#   ① 直近1年の上場企業向け公的支出イベント  GET /v1/funding/recent            毎日1回
#   ② 個社の公的支出履歴（補助金・調達・子会社経由）GET /v1/companies/{code}/public-funding
#        保有銘柄 → スクリーナー候補の順に、前回から SEISAKUDB_COMPANY_TTL_DAYS 日以上たった会社だけ
#   ③ 経済安保 認定供給確保計画            GET /v1/economic-security/certifications  週1回（月曜）
# 保存先
#   BQ edinet.public_funding（①②）/ edinet.economic_security_certs（③）
#   応答の項目名は仕様書に載っていないので、元の1件を record_json に丸ごと残し、
#   金額・年度・省庁などは候補の項目名から拾う（_pick）。拾えなくても record_json から後で取り出せる。
#   stock_data/data/seisakudb_last.json（日次メール用の当日まとめ）
#   stock_data/data/seisakudb_meta.json（個社ごとの最終取得日）
# 実行
#   python3 seisakudb_ingestor.py --probe      # 各エンドポイントを1件ずつ取り、応答の形を表示（キー登録後の確認用）
# ==============================================================
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

BASE = "https://seisakudb.jp"
_AMOUNT_KEYS = ("amount_yen", "amount", "total_amount", "net_amount", "total_yen", "yen", "value", "sum_amount")
_FY_KEYS = ("fiscal_year", "fy", "year", "nendo")
_DATE_KEYS = ("date", "event_date", "decided_at", "award_date", "contract_date", "published_at", "updated_at")
_TITLE_KEYS = ("title", "project_name", "program_name", "name", "subject", "description")
_KIND_KEYS = ("funding_type", "type", "kind", "category", "source_type", "role", "funding_role")
_MINISTRY_KEYS = ("ministry", "agency", "ministry_name", "organization", "provider")
_CODE_KEYS = ("sec_code", "securities_code", "company_code", "ticker", "code")
_JCN_KEYS = ("jcn", "corporate_number", "houjin_bangou", "houjin_number")
_COMPANY_KEYS = ("company_name", "company", "recipient", "recipient_name", "corporate_name", "name")


class SeisakuClient:
    """政策DB REST の薄いラッパー。1日の使用回数を記録し、上限・401・429 で止まる。"""

    def __init__(self, api_key: str, ledger: Path, daily_budget: int = 90, interval: float = 1.0):
        self.api_key, self.ledger, self.daily_budget, self.interval = api_key, ledger, daily_budget, interval
        self._last = 0.0
        self.stopped = ""
        try:
            d = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        self.used = int(d.get("calls", 0)) if d.get("date") == date.today().isoformat() else 0

    def _save(self) -> None:
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.ledger.write_text(json.dumps({"date": date.today().isoformat(), "calls": self.used}),
                                   encoding="utf-8")
        except OSError:
            pass

    def get(self, path: str, params: dict | None = None):
        if self.stopped or self.used >= self.daily_budget:
            self.stopped = self.stopped or "budget"
            return None
        import requests
        wait = self.interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)          # 連打すると429を誘発する
        self._last = time.time()
        try:
            r = requests.get(BASE + path, params=params or {}, timeout=30,
                             headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json",
                                      "User-Agent": "stock_analyzer/1.0 (seisakudb_ingestor)"})
        except Exception as exc:
            print(f"    ⚠️ [seisakudb] {path}: 通信失敗 {type(exc).__name__}")
            return None
        if r.status_code < 400:
            self.used += 1            # 成功だけが1 unit（4xx/5xx は課金されない）
            self._save()
        if r.status_code in (401, 403):
            self.stopped = f"HTTP {r.status_code}（APIキーを確認）"
            return None
        if r.status_code == 429:
            self.stopped = "HTTP 429（レート上限）"
            return None
        if r.status_code >= 400:
            print(f"    ⚠️ [seisakudb] {path}: HTTP {r.status_code}")
            return None
        try:
            return r.json()
        except ValueError:
            return None


def results_of(payload) -> list[dict]:
    """応答 → レコードの配列（{count, results: [...]} 形式。配列そのものでも受ける）（純関数）。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("results", "items", "data", "records"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _pick(rec: dict, keys: tuple):
    for k in keys:
        v = rec.get(k)
        if v not in (None, ""):
            return v
    return None


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("円", ""))
    except (TypeError, ValueError):
        return None


def normalize(rec: dict, endpoint: str, company_code: str | None, fetched_at: str) -> dict:
    """1件 → BQ edinet.public_funding の行（純関数）。"""
    raw = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    code = company_code or _pick(rec, _CODE_KEYS)
    code = str(code)[:4] if code not in (None, "") else None
    fy = _pick(rec, _FY_KEYS)
    try:
        fy = int(str(fy)[:4]) if fy not in (None, "") else None
    except ValueError:
        fy = None
    d = _pick(rec, _DATE_KEYS)
    return {
        "record_id": hashlib.sha1(f"{endpoint}|{code}|{raw}".encode()).hexdigest(),
        "endpoint": endpoint,
        "company_code": code,
        "jcn": (str(_pick(rec, _JCN_KEYS)) if _pick(rec, _JCN_KEYS) is not None else None),
        "company_name": (str(_pick(rec, _COMPANY_KEYS))[:200] if _pick(rec, _COMPANY_KEYS) else None),
        "fiscal_year": fy,
        "event_date": str(d)[:10] if d and re.match(r"^\d{4}-\d{2}-\d{2}", str(d)) else None,
        "amount_yen": _num(_pick(rec, _AMOUNT_KEYS)),
        "kind": (str(_pick(rec, _KIND_KEYS))[:100] if _pick(rec, _KIND_KEYS) else None),
        "ministry": (str(_pick(rec, _MINISTRY_KEYS))[:100] if _pick(rec, _MINISTRY_KEYS) else None),
        "title": (str(_pick(rec, _TITLE_KEYS))[:500] if _pick(rec, _TITLE_KEYS) else None),
        "record_json": raw,
        "fetched_at": fetched_at,
    }


def due_companies(codes: list[str], meta: dict, today: date, ttl_days: int) -> list[str]:
    """前回取得から ttl_days 日以上たった（または未取得の）会社。順番は入力のまま（純関数）。"""
    out, seen = [], set()
    for c in codes:
        if c in seen:
            continue
        seen.add(c)
        last = (meta.get(c) or {}).get("fetched")
        try:
            if last and (today - date.fromisoformat(last)).days < ttl_days:
                continue
        except ValueError:
            pass
        out.append(c)
    return out


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run(cfg, tickers: list[str], today: date | None = None) -> dict:
    """日次の取り込み。tickers は保有銘柄が先頭（優先）になるよう渡す。"""
    today = today or date.today()
    key = os.environ.get("SEISAKUDB_API_KEY") or getattr(cfg, "SEISAKUDB_API_KEY", "")
    if not key:
        print("    ⏩ [seisakudb] SEISAKUDB_API_KEY 未設定 → スキップ")
        return {"skipped": True, "reason": "no_key"}
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    client = SeisakuClient(key, data_dir / "seisakudb_usage.json",
                           int(getattr(cfg, "SEISAKUDB_DAILY_BUDGET", 90)))
    meta_path = data_dir / "seisakudb_meta.json"
    meta = _load(meta_path, {})
    now = datetime.now(timezone.utc).isoformat()
    rows, certs = [], []
    stats = {"recent": 0, "companies_ok": 0, "companies_empty": 0, "certs": 0, "errors": 0}

    # ① 直近の公的支出イベント（毎日1回）
    recent = results_of(client.get("/v1/funding/recent", {"limit": int(getattr(cfg, "SEISAKUDB_RECENT_LIMIT", 200))}))
    rows += [normalize(r, "funding_recent", None, now) for r in recent]
    stats["recent"] = len(recent)

    # ② 個社の公的支出履歴（保有 → 候補の順・30日に1回）
    codes = [t[:-2] for t in tickers if str(t).endswith(".T")]
    todo = due_companies(codes, meta, today, int(getattr(cfg, "SEISAKUDB_COMPANY_TTL_DAYS", 30)))
    per_day = int(getattr(cfg, "SEISAKUDB_COMPANY_DAILY", 60))
    hits = []
    for c in todo[:per_day]:
        payload = client.get(f"/v1/companies/{c}/public-funding")
        if client.stopped:
            break
        recs = results_of(payload)
        meta[c] = {"fetched": today.isoformat(), "n": len(recs)}
        if recs:
            stats["companies_ok"] += 1
            rows += [normalize(r, "company_public_funding", c, now) for r in recs]
            total = sum(x for x in (_num(_pick(r, _AMOUNT_KEYS)) for r in recs) if x)
            hits.append({"code": c, "n": len(recs), "amount_yen": total})
        else:
            stats["companies_empty"] += 1

    # ③ 経済安保 認定（週1回・月曜）
    if today.weekday() == 0 and not client.stopped:
        certs_raw = results_of(client.get("/v1/economic-security/certifications", {"limit": 500}))
        certs = [normalize(r, "economic_security_cert", None, now) for r in certs_raw]
        stats["certs"] = len(certs)

    # 保存（BQ は MERGE で冪等）
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
        for table, part in (("public_funding", rows), ("economic_security_certs", certs)):
            for i in range(0, len(part), 2000):
                bq.merge_rows(table, part[i:i + 2000], ["record_id"])
    except Exception as exc:
        stats["errors"] += 1
        print(f"    ⚠️ [seisakudb] BQ 保存失敗: {type(exc).__name__}: {str(exc)[:80]}")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=0), encoding="utf-8")
    summary = {
        "date": today.isoformat(), "calls": client.used, "budget": client.daily_budget,
        "stopped": client.stopped, **stats, "rows": len(rows),
        "company_hits": sorted(hits, key=lambda h: -(h["amount_yen"] or 0))[:20],
        "recent_sample": [{k: r[k] for k in ("company_code", "company_name", "event_date", "amount_yen",
                                              "kind", "ministry", "title")} for r in rows
                          if r["endpoint"] == "funding_recent"][:30],
    }
    (data_dir / "seisakudb_last.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
    print(f"    🏛️ [seisakudb] 直近イベント{stats['recent']}件 / 個社{stats['companies_ok']}社"
          f"（受給なし{stats['companies_empty']}）/ 経済安保{stats['certs']}件 / API {client.used}/{client.daily_budget}回"
          + (f" / 停止: {client.stopped}" if client.stopped and client.stopped != "budget" else ""))
    return summary


def probe(api_key: str, sec_code: str = "7011") -> None:
    """各エンドポイントを1件ずつ取り、項目名と値の例を表示する（キー登録後の形の確認用・約8回）。"""
    client = SeisakuClient(api_key, Path("stock_data/data/seisakudb_usage.json"), daily_budget=100)
    targets = [("/v1/funding/recent", {"limit": 2}), (f"/v1/companies/{sec_code}/public-funding", {}),
               ("/v1/funding/top", {"limit": 2}), ("/v1/economic-security/certifications", {"limit": 2}),
               ("/v1/subsidies/open", {"limit": 2}), ("/v1/gpif/holdings", {"limit": 2}),
               ("/v1/research/policy-themes", {"limit": 2}), ("/v1/diet/bills/search", {"limit": 2})]
    for path, params in targets:
        p = client.get(path, params)
        top = list(p.keys()) if isinstance(p, dict) else type(p).__name__
        recs = results_of(p)
        print(f"== {path}  top={top}  件数={len(recs)}  stopped={client.stopped or '-'}")
        if recs:
            print(json.dumps(recs[0], ensure_ascii=False, indent=1)[:1500])
    print(f"使用 {client.used} 回")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--code", default="7011")
    a = ap.parse_args(argv)
    key = os.environ.get("SEISAKUDB_API_KEY", "")
    if not key:
        print("SEISAKUDB_API_KEY が未設定")
        return 1
    if a.probe:
        probe(key, a.code)
        return 0
    import config as cfg
    run(cfg, [f"{a.code}.T"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
