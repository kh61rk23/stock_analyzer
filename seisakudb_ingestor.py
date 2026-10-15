# ==============================================================
# seisakudb_ingestor.py — 政策DB（seisakudb.jp, Cabocia株式会社）の取り込み
#
# 仕様: https://seisakudb.jp/v1/openapi.json（REST /v1/*・Authorization: Bearer <SEISAKUDB_API_KEY>）
#   課金単位は「認証付きHTTPリクエスト1回＝1 unit」（4xx/5xx は0）。現在の上限は1日1000回。
#   データは月次更新（公的支出は FY2021〜の5年分）。
#   利用規約: Webページ（/company/* 等）の自動取得は禁止。API だけを使う。出典「政策DB」を表示する。
#
# 取り込むもの（1日の上限 SEISAKUDB_DAILY_BUDGET 回から SEISAKUDB_RESERVE を引いた分を上から順に使い切る）
#   ① 直近の上場企業向け公的支出イベント   GET /v1/funding/recent           毎日
#   ② 公的支出ランキング                    GET /v1/funding/top              毎日
#   ③ 経済安保 認定供給確保計画             GET /v1/economic-security/certifications  毎日
#   ④ 募集中の補助金                        GET /v1/subsidies/open           毎日（BQ には入れず当日まとめに残す）
#   ⑤ 個社の公的支出履歴                    GET /v1/companies/{5桁コード}/public-funding
#        保有銘柄 → スクリーナー候補 → 上場全社（最終取得日が古い順）。予算が尽きるまで続ける
#   個社APIの証券コードは5桁（4桁＋"0"）。4桁で呼ぶと0件になる。日付は RFC 1123 形式で来る。
# 保存先
#   BQ edinet.public_funding（①⑤）/ edinet.public_funding_rank（②）/ edinet.economic_security_certs（③）
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
_DATE_KEYS = ("award_date", "certification_date", "date", "event_date", "decided_at", "contract_date",
              "published_at", "updated_at")
_TITLE_KEYS = ("title", "project_name", "program_name", "name", "subject", "description")
_KIND_KEYS = ("funding_type", "type", "kind", "category", "source_type", "role", "funding_role",
              "critical_material")
_MINISTRY_KEYS = ("ministry", "agency", "ministry_name", "organization", "provider")
_CODE_KEYS = ("sec_code", "securities_code", "company_code", "ticker", "code")
_JCN_KEYS = ("jcn", "corporate_number", "houjin_bangou", "houjin_number")
_COMPANY_KEYS = ("company_name", "listed_name", "company", "recipient", "recipient_name", "corporate_name", "name")
_ID_KEYS = ("certification_id",)
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_RFC_DATE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})")


class SeisakuClient:
    """政策DB REST の薄いラッパー。1日の使用回数を記録し、上限・401・429 で止まる。"""

    def __init__(self, api_key: str, ledger: Path, daily_budget: int = 1000, interval: float = 1.0):
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

    def _note(self, path: str, status) -> None:
        """呼び出しを api_call_log に残す（メールの「API の使い道」用。失敗しても止めない）。"""
        try:
            import api_call_log
            api_call_log.note("seisakudb", path, status, data_dir=self.ledger.parent)
        except Exception:
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
            self._note(path, "error")
            return None
        self._note(path, r.status_code)
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


def parse_date(s) -> str | None:
    """ISO（先頭10文字）と RFC 1123（"Thu, 14 May 2026 00:00:00 GMT"）を "YYYY-MM-DD" にする。不正なら None（純関数）。"""
    if s in (None, ""):
        return None
    t = str(s).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", t):
        try:
            return date.fromisoformat(t[:10]).isoformat()
        except ValueError:
            return None
    m = _RFC_DATE.search(t)
    if m and m.group(2).title() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).title()], int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def code4_of(code) -> str | None:
    """証券コード（4桁または5桁）→ 先頭4桁（純関数）。"""
    return str(code).strip()[:4] if code not in (None, "") and str(code).strip() else None


def normalize(rec: dict, endpoint: str, company_code: str | None, fetched_at: str) -> dict:
    """1件 → BQ edinet.public_funding の行（純関数）。"""
    raw = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    code = company_code or _pick(rec, _CODE_KEYS)
    code = code4_of(code)
    fy = _pick(rec, _FY_KEYS)
    try:
        fy = int(str(fy)[:4]) if fy not in (None, "") else None
    except ValueError:
        fy = None
    rid = _pick(rec, _ID_KEYS)
    return {
        "record_id": hashlib.sha1(f"{endpoint}|{code}|{rid if rid is not None else raw}".encode()).hexdigest(),
        "endpoint": endpoint,
        "company_code": code,
        "jcn": (str(_pick(rec, _JCN_KEYS)) if _pick(rec, _JCN_KEYS) is not None else None),
        "company_name": (str(_pick(rec, _COMPANY_KEYS))[:200] if _pick(rec, _COMPANY_KEYS) else None),
        "fiscal_year": fy,
        "event_date": parse_date(_pick(rec, _DATE_KEYS)),
        "amount_yen": _num(_pick(rec, _AMOUNT_KEYS)),
        "kind": (str(_pick(rec, _KIND_KEYS))[:100] if _pick(rec, _KIND_KEYS) else None),
        "ministry": (str(_pick(rec, _MINISTRY_KEYS))[:100] if _pick(rec, _MINISTRY_KEYS) else None),
        "title": (str(_pick(rec, _TITLE_KEYS))[:500] if _pick(rec, _TITLE_KEYS) else None),
        "record_json": raw,
        "fetched_at": fetched_at,
    }


def due_companies(priority_codes: list[str], all_codes: list[str], meta: dict, today: date) -> list[str]:
    """今日取る個社の順序（純関数）。同日に取得済みは除く。
    先頭は priority_codes（保有・候補）を入力順のまま、続いて all_codes の残りを最終取得日が古い順（未取得が先）。"""
    t = today.isoformat()

    def last(c):
        return (meta.get(c) or {}).get("fetched") or ""

    out, seen = [], set()
    for c in priority_codes:
        if c in seen:
            continue
        seen.add(c)
        if last(c) != t:
            out.append(c)
    rest = []
    for c in all_codes:
        if c in seen:
            continue
        seen.add(c)
        if last(c) != t:
            rest.append(c)
    rest_sorted = sorted(enumerate(rest), key=lambda x: (last(x[1]), x[0]))
    return out + [c for _, c in rest_sorted]


def _norm_name(s: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKC", str(s or ""))
    t = re.sub(r"株式会社|\(株\)|（株）|\s+", "", t)
    return unicodedata.normalize("NFKC", t).lower()


def map_company_codes(names: list[str], companies: list[dict]) -> dict[str, str]:
    """社名 → 証券コード4桁（純関数）。株式会社・(株)・空白・全角半角の差を除いて一致したものだけ返す。"""
    index: dict[str, str] = {}
    dup: set[str] = set()
    for c in companies or []:
        code = code4_of(c.get("sec_code"))
        if not code or c.get("is_delisted"):
            continue
        for k in ("name", "name_ja"):
            n = _norm_name(c.get(k))
            if not n:
                continue
            if n in index and index[n] != code:
                dup.add(n)
            index.setdefault(n, code)
    out = {}
    for nm in names:
        n = _norm_name(nm)
        if n and n in index and n not in dup:
            out[nm] = index[n]
    return out


def rank_row(rec: dict, rank: int, today: date, now: str) -> dict | None:
    """funding/top の1行 → BQ edinet.public_funding_rank の行（純関数）。コードが無ければ None。"""
    code = code4_of(_pick(rec, _CODE_KEYS))
    if not code:
        return None

    def _i(v):
        n = _num(v)
        return int(n) if n is not None else None

    return {
        "company_code": code,
        "listed_name": (str(rec.get("listed_name"))[:200] if rec.get("listed_name") else None),
        "industry": (str(rec.get("industry"))[:100] if rec.get("industry") else None),
        "total_oku": _num(rec.get("total_oku")),
        "subsidy_oku": _num(rec.get("subsidy_oku")),
        "procurement_oku": _num(rec.get("procurement_oku")),
        "via_subsidiary_oku": _num(rec.get("via_subsidiary_oku")),
        "funding_events": _i(rec.get("funding_events")),
        "funding_types": _i(rec.get("funding_types")),
        "ministries": _i(rec.get("ministries")),
        "first_award": parse_date(rec.get("first_award")),
        "last_award": parse_date(rec.get("last_award")),
        "rank_no": rank,
        "fetched_date": today.isoformat(),
        "updated_at": now,
    }


def open_subsidy_rows(recs: list[dict], today: date, limit: int = 30) -> list[dict]:
    """募集中の補助金 → 締切が近い順（締切不明は最後）。過ぎたものは除く（純関数）。"""
    out = []
    for r in recs:
        end = parse_date(r.get("acceptance_end_datetime"))
        if end and end < today.isoformat():
            continue
        out.append({"title": r.get("title"), "institution_name": r.get("institution_name"),
                    "acceptance_end": end, "target_area_search": r.get("target_area_search"),
                    "subsidy_max_limit": r.get("subsidy_max_limit"),
                    "url": r.get("front_subsidy_detail_page_url")})
    out.sort(key=lambda x: (x["acceptance_end"] is None, x["acceptance_end"] or ""))
    return out[:limit]


def company_api_path(code4: str) -> str:
    """個社APIのパス。証券コードは5桁（4桁＋"0"）で呼ぶ（純関数）。"""
    return f"/v1/companies/{code4}0/public-funding"


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _listed_companies(data_dir: Path) -> list[dict]:
    d = _load(data_dir / "edinetdb_company_cache.json", {})
    cs = d.get("companies") if isinstance(d, dict) else None
    return [c for c in (cs or []) if isinstance(c, dict)]


def run(cfg, tickers: list[str], today: date | None = None) -> dict:
    """日次の取り込み。tickers は保有銘柄 → スクリーナー候補の順（優先）で渡す。"""
    today = today or date.today()
    key = os.environ.get("SEISAKUDB_API_KEY") or getattr(cfg, "SEISAKUDB_API_KEY", "")
    if not key:
        print("    ⏩ [seisakudb] SEISAKUDB_API_KEY 未設定 → スキップ")
        return {"skipped": True, "reason": "no_key"}
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    budget = max(0, int(getattr(cfg, "SEISAKUDB_DAILY_BUDGET", 1000)) - int(getattr(cfg, "SEISAKUDB_RESERVE", 0)))
    client = SeisakuClient(key, data_dir / "seisakudb_usage.json", budget,
                           float(getattr(cfg, "SEISAKUDB_MIN_INTERVAL_SEC", 0.5)))
    meta_path = data_dir / "seisakudb_meta.json"
    meta = _load(meta_path, {})
    now = datetime.now(timezone.utc).isoformat()
    rows, certs, ranks = [], [], []
    stats = {"recent": 0, "top_count": 0, "companies_ok": 0, "companies_empty": 0, "certs": 0,
             "certs_mapped": 0, "errors": 0}

    # a. 毎日の固定取得
    recent = results_of(client.get("/v1/funding/recent", {"limit": int(getattr(cfg, "SEISAKUDB_RECENT_LIMIT", 500))}))
    rows += [normalize(r, "funding_recent", None, now) for r in recent]
    stats["recent"] = len(recent)

    top = results_of(client.get("/v1/funding/top", {"limit": int(getattr(cfg, "SEISAKUDB_TOP_LIMIT", 2000))}))
    ranks = [x for x in (rank_row(r, i, today, now) for i, r in enumerate(top, 1)) if x]
    stats["top_count"] = len(ranks)

    certs_raw = results_of(client.get("/v1/economic-security/certifications", {"limit": 500}))
    certs = [normalize(r, "economic_security_cert", None, now) for r in certs_raw]
    companies = _listed_companies(data_dir)
    mapping = map_company_codes([c["company_name"] for c in certs if c["company_name"]], companies)
    for c in certs:
        if not c["company_code"] and c["company_name"] in mapping:
            c["company_code"] = mapping[c["company_name"]]
    stats["certs"] = len(certs)
    stats["certs_mapped"] = sum(1 for c in certs if c["company_code"])

    open_subs = open_subsidy_rows(results_of(client.get("/v1/subsidies/open", {"limit": 200})), today)

    # b. 個社ローテーション（保有 → 候補 → 上場全社の古い順・予算が尽きるまで）
    priority = [code4_of(t) for t in tickers if str(t).endswith(".T")]
    all_codes = [code4_of(c.get("sec_code")) for c in companies if not c.get("is_delisted") and c.get("sec_code")]
    todo = due_companies([c for c in priority if c], [c for c in all_codes if c], meta, today)
    hits = []
    for c in todo:
        if client.stopped:
            break
        payload = client.get(company_api_path(c))
        if payload is None:
            continue
        recs = results_of(payload)
        meta[c] = {"fetched": today.isoformat(), "n": len(recs)}
        if recs:
            stats["companies_ok"] += 1
            rows += [normalize(r, "company_public_funding", c, now) for r in recs]
            total = sum(x for x in (_num(_pick(r, _AMOUNT_KEYS)) for r in recs) if x)
            hits.append({"code": c, "n": len(recs), "amount_yen": total})
        else:
            stats["companies_empty"] += 1

    # 保存（BQ は MERGE で冪等）
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
        for table, part, keys in (("public_funding", rows, ["record_id"]),
                                  ("economic_security_certs", certs, ["record_id"]),
                                  ("public_funding_rank", ranks, ["company_code", "fetched_date"])):
            for i in range(0, len(part), 2000):
                bq.merge_rows(table, part[i:i + 2000], keys)
    except Exception as exc:
        stats["errors"] += 1
        print(f"    ⚠️ [seisakudb] BQ 保存失敗: {type(exc).__name__}: {str(exc)[:80]}")
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=0), encoding="utf-8")
    except OSError:
        stats["errors"] += 1
    summary = {
        "date": today.isoformat(), "calls": client.used, "budget": client.daily_budget,
        "stopped": client.stopped, **stats, "rows": len(rows),
        "company_hits": sorted(hits, key=lambda h: -(h["amount_yen"] or 0))[:20],
        "recent_sample": [{k: r[k] for k in ("company_code", "company_name", "event_date", "amount_yen",
                                              "kind", "ministry", "title")} for r in rows
                          if r["endpoint"] == "funding_recent"][:30],
        "open_subsidies": open_subs,
        "rank_top": [{"code": r["company_code"], "listed_name": r["listed_name"], "total_oku": r["total_oku"],
                      "subsidy_oku": r["subsidy_oku"], "procurement_oku": r["procurement_oku"],
                      "via_subsidiary_oku": r.get("via_subsidiary_oku")}
                     for r in sorted(ranks, key=lambda r: -(r["total_oku"] or 0))[:20]],
    }
    try:
        (data_dir / "seisakudb_last.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                      encoding="utf-8")
    except OSError:
        pass
    print(f"    🏛️ [seisakudb] 直近イベント{stats['recent']}件 / ランキング{stats['top_count']}社 / "
          f"個社{stats['companies_ok']}社（受給なし{stats['companies_empty']}）/ "
          f"経済安保{stats['certs']}件（コード対応{stats['certs_mapped']}）/ API {client.used}/{client.daily_budget}回"
          + (f" / 停止: {client.stopped}" if client.stopped and client.stopped != "budget" else ""))
    return summary


def probe(api_key: str, sec_code: str = "7011") -> None:
    """各エンドポイントを1件ずつ取り、項目名と値の例を表示する（キー登録後の形の確認用・約8回）。"""
    client = SeisakuClient(api_key, Path("stock_data/data/seisakudb_usage.json"), daily_budget=1000)
    targets = [("/v1/funding/recent", {"limit": 2}), (company_api_path(code4_of(sec_code) or sec_code), {}),
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
