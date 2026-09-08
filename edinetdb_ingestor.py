"""EdinetDB REST を既存 BigQuery ``edinet.*`` へ格納する一次ingestor。"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from db_rest_client import DBRestClient


def _stop_ingest(client) -> bool:
    """予算枯渇・レート制限に入ったか（以降の企業は必ず失敗するため周回を止める）。"""
    return getattr(client, "last_error", None) in ("budget", "rate_limited")


def _ratio(numerator, denominator):
    try:
        if numerator is None or denominator is None or float(denominator) == 0:
            return None
        return round(float(numerator) / float(denominator), 6)
    except (TypeError, ValueError):
        return None


def _number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _explicit_date(row: dict) -> str | None:
    """APIが明示した決算期末だけを採用し、submit_dateからは推測しない。"""
    for key in ("fiscal_year_end", "period_end", "accounting_period_end"):
        value = str(row.get(key) or "")[:10]
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            continue
    return None


def map_edinetdb_financials(
    row: dict,
    *,
    edinet_code: str = "",
    company_code: str = "",
    now: str | None = None,
) -> dict:
    """OpenAPI ``Financial`` を既存 ``edinet.financials`` 行へ写像する。"""
    now = now or datetime.now(timezone.utc).isoformat()
    fiscal_year = _integer(row.get("fiscal_year") or row.get("fiscalYear"))
    doc_id = str(row.get("doc_id") or row.get("docId") or "")
    if not doc_id:
        doc_id = f"edinetdb_{edinet_code}_{fiscal_year or 'unknown'}"
    op_cf = _number(row.get("cf_operating"))
    inv_cf = _number(row.get("cf_investing"))
    net_sales = _number(row.get("revenue"))
    operating_income = _number(row.get("operating_income"))
    net_income = _number(row.get("net_income"))
    total_assets = _number(row.get("total_assets"))
    equity = _number(row.get("net_assets"))
    employee_count = _integer(row.get("num_employees"))
    debt_parts = [row.get("ibd_current"), row.get("ibd_noncurrent")]
    debt_values = [_number(value) for value in debt_parts if value is not None]
    interest_bearing_debt = sum(debt_values) if debt_values else None
    free_cf = op_cf + inv_cf if op_cf is not None and inv_cf is not None else None
    roe = _number(row.get("roe_official"))
    if roe is None:
        roe = _ratio(net_income, equity)
    return {
        "fin_id": f"{edinet_code}_{fiscal_year or _explicit_date(row) or doc_id}",
        "doc_id": doc_id,
        "company_code": company_code or None,
        "edinet_code": edinet_code or None,
        "fiscal_year": fiscal_year,
        "fiscal_year_end": _explicit_date(row),
        "report_period": "FY",
        "net_sales": net_sales,
        "operating_income": operating_income,
        "ordinary_income": _number(row.get("ordinary_income")),
        "net_income": net_income,
        "total_assets": total_assets,
        "equity": equity,
        "interest_bearing_debt": interest_bearing_debt,
        "cash_and_equiv": _number(row.get("cash")),
        "operating_cf": op_cf,
        "investing_cf": inv_cf,
        "free_cf": free_cf,
        "employee_count": employee_count,
        "avg_age_years": _number(row.get("avg_age")),
        "avg_annual_salary": _number(row.get("avg_annual_salary")),
        "total_director_comp": _number(row.get("director_remuneration_total")),
        "max_single_comp": None,
        "capex": _number(row.get("capex")),
        "depreciation": _number(row.get("depreciation")),
        "roe": roe,
        "roa": _ratio(net_income, total_assets),
        "operating_margin": _ratio(operating_income, net_sales),
        "accrual_ratio": None,
        "fcf_yield": None,
        "labor_productivity": _ratio(net_sales, employee_count),
        "currency": "JPY",
        "source": "edinetdb",
        "updated_at": now,
    }


def _field(row: dict, *names):
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def map_edinetdb_segments(
    code: str,
    seg_list: list[dict],
    period: str | int | None,
    *,
    company_code: str = "",
    now: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """公式契約のcamelCase/snake_case双方を既存2表へ写像する。"""
    now = now or datetime.now(timezone.utc).isoformat()
    period_str = str(period) if period is not None else None
    segments_rows, finance_rows = [], []
    for index, item in enumerate(seg_list or []):
        if not isinstance(item, dict):
            continue
        name = str(_field(item, "segmentName", "segment_name", "name") or "").strip()
        if not name:
            continue
        segment_type = str(
            _field(item, "segmentType", "segment_type") or "reportable"
        ).lower()
        fiscal_year = _field(item, "fiscalYear", "fiscal_year") or period_str or "unknown"
        digest = hashlib.sha1(
            f"{code}|{fiscal_year}|{segment_type}|{name}".encode("utf-8")
        ).hexdigest()[:16]
        segment_id = f"{code}_{fiscal_year}_{digest}"
        doc_id = str(_field(item, "docId", "doc_id") or f"edinetdb_{code}_{fiscal_year}")
        revenue = _number(_field(item, "revenue", "segmentRevenue", "segment_sales"))
        operating_profit = _number(
            _field(item, "operatingIncome", "operating_income", "segment_profit")
        )
        revenue_ratio = _number(_field(item, "revenueShare", "revenue_share"))
        if revenue_ratio is not None and revenue_ratio > 1:
            revenue_ratio = revenue_ratio / 100.0
        margin = _number(_field(item, "oiMargin", "oi_margin", "operating_margin"))
        if margin is not None and abs(margin) > 1:
            margin = margin / 100.0
        if margin is None:
            margin = _ratio(operating_profit, revenue)
        segments_rows.append({
            "segment_id": segment_id,
            "doc_id": doc_id,
            "company_code": company_code or None,
            "segment_name": name,
            "segment_name_norm": "".join(name.lower().split()),
            "segment_type": segment_type,
            "parent_segment_id": None,
            "report_period": period_str,
            "source_id": doc_id,
            "updated_at": now,
        })
        finance_rows.append({
            "sf_id": f"{segment_id}_fin",
            "segment_id": segment_id,
            "revenue": revenue,
            "revenue_ratio": revenue_ratio,
            "operating_profit": operating_profit,
            "operating_margin": margin,
            "assets": _number(_field(item, "assets", "segmentAssets", "segment_assets")),
            "geography": _field(item, "geography", "region"),
            "currency": "JPY",
            "report_period": period_str,
            "updated_at": now,
        })
    return segments_rows, finance_rows


def map_edinetdb_text_blocks(
    code: str,
    blocks: list[dict],
    fiscal_year: int | None,
    *,
    company_code: str = "",
    fiscal_year_end: str | None = None,
    now: str | None = None,
) -> list[dict]:
    now = now or datetime.now(timezone.utc).isoformat()
    rows = []
    doc_id = f"edinetdb_{code}_{fiscal_year or 'latest'}"
    for block in blocks or []:
        section = str(block.get("section") or "").strip()
        raw_text = str(block.get("text") or "").strip()
        if not section or not raw_text:
            continue
        digest = hashlib.sha1(section.encode("utf-8")).hexdigest()[:12]
        rows.append({
            "text_id": f"{code}_{fiscal_year or 'latest'}_{digest}",
            "doc_id": doc_id,
            "company_code": company_code or None,
            "fiscal_year_end": fiscal_year_end,
            "section_type": section,
            "finbert_positive": None,
            "finbert_negative": None,
            "finbert_neutral": None,
            "yoy_delta": None,
            "risk_deterioration_flag": None,
            "risk_category_scores": None,
            "raw_text": raw_text,
            "source": "edinetdb",
            "updated_at": now,
        })
    return rows


def text_block_params(fiscal_year: int | None) -> dict:
    """全セクションの全文を1 request unitで取得する契約パラメータ。"""
    params = {"full": "true"}
    if fiscal_year is not None:
        params["fiscal_year"] = int(fiscal_year)
    return params


def reconcile_meta_with_bq(meta: dict, companies: Iterable,
                           bq_latest: dict) -> tuple[dict, int]:
    """BQ にある取得済みデータをローテ台帳へ反映する（純関数・pytest対象）。

    【なぜ必要か / 2026-07-30】
    ローテ台帳（edinetdb_rotation_meta.json）はローカルJSONのキャッシュに
    過ぎず、Actions の状態コミットが飛ぶと BQ より遅れる。実測で
    **BQ に 114社ある一方で台帳は 69社**しか記録しておらず、既に BQ へ
    格納済みの約45社を毎日再取得して API 予算を空費していた。
    BQ を正とみなして台帳を補完し、「取れているもの／取れていないもの」を
    はっきりさせる。

    Args:
        meta:      現在のローテ台帳 {edinet_code: {"last_fetched": "YYYY-MM-DD"}}
        companies: company cache の全社（edinet_code と sec_code を持つ）
        bq_latest: BQ から得た {証券コード4桁: "YYYY-MM-DD"}

    Returns:
        (更新後meta, 補完した件数)
    """
    if not bq_latest:
        return meta, 0
    # sec_code は5桁（末尾0）で入っているため先頭4桁で突合する
    sec_to_edinet: dict[str, str] = {}
    for company in companies or []:
        if not isinstance(company, dict):
            continue
        edinet_code = str(company.get("edinet_code") or "").strip()
        sec_code = str(company.get("sec_code") or "").strip()
        if edinet_code and len(sec_code) >= 4:
            sec_to_edinet.setdefault(sec_code[:4], edinet_code)

    filled = 0
    for sec_code, fetched_at in bq_latest.items():
        edinet_code = sec_to_edinet.get(str(sec_code)[:4])
        if not edinet_code or not fetched_at:
            continue
        current = str((meta.get(edinet_code) or {}).get("last_fetched") or "")[:10]
        new_value = str(fetched_at)[:10]
        # BQ の方が新しい（または台帳に無い）ときだけ書き換える
        if new_value > current:
            entry = dict(meta.get(edinet_code) or {})
            entry["last_fetched"] = new_value
            entry["source"] = "bq"
            meta[edinet_code] = entry
            filled += 1
    return meta, filled


def _bq_ingested_latest(cfg) -> dict:
    """BQ の取得済み企業と最終更新日を {証券コード: "YYYY-MM-DD"} で返す。

    読み取り1クエリのみ（EdinetDB の API 予算は消費しない）。
    BQ が使えない環境では空dictを返し、従来どおり台帳のみで動く。
    """
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
    except Exception:
        return {}
    sql = f"""
        SELECT company_code, CAST(MAX(updated_at) AS STRING) AS latest
        FROM `{bq.project}.edinet.financials`
        WHERE company_code IS NOT NULL
        GROUP BY company_code
    """
    try:
        rows = list(bq.client.query(sql).result())
    except Exception as exc:
        print(f"  ⚠️ BQ 取得済み照会に失敗（台帳のみで継続）: {str(exc)[:70]}")
        return {}
    return {str(r.company_code): str(r.latest or "")[:10] for r in rows}


def select_ingest_targets(
    holdings: Iterable,
    screener: Iterable,
    representatives: Iterable,
    remaining: Iterable,
    meta: dict,
    budget: int,
    *,
    calls_per_company: int = 3,
    refresh_days: int = 180,
    today: date | None = None,
) -> list[str]:
    """P1→P4順、重複排除、鮮度除外、予算上限で対象を返す純関数。"""
    today = today or date.today()
    limit = max(0, int(budget)) // max(1, int(calls_per_company))
    if limit <= 0:
        return []
    selected, seen = [], set()

    def code_of(item) -> str:
        if isinstance(item, dict):
            return str(item.get("edinet_code") or item.get("code") or "").strip()
        return str(item or "").strip()

    def fresh(code: str) -> bool:
        value = str((meta.get(code) or {}).get("last_fetched") or "")[:10]
        try:
            return (today - date.fromisoformat(value)).days < refresh_days
        except ValueError:
            return False

    # P4だけは「未取得→最終取得が古い順」のFIFOにする。
    remaining_fifo = sorted(
        remaining or [],
        key=lambda item: str(
            (meta.get(code_of(item)) or {}).get("last_fetched") or "0000-00-00"
        ),
    )
    for group in (holdings, screener, representatives, remaining_fifo):
        for item in group or []:
            code = code_of(item)
            if not code or code in seen:
                continue
            seen.add(code)
            if fresh(code):
                continue
            selected.append(code)
            if len(selected) >= limit:
                return selected
    return selected


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _sec4(value) -> str:
    value = str(value or "").upper().replace(".T", "").strip()
    if len(value) == 5 and value.endswith("0"):
        value = value[:4]
    return value if len(value) == 4 and value.isdigit() else ""


def _priority_tickers(cfg) -> tuple[list[str], list[str], list[str]]:
    holdings = []
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "portfolio_holdings.csv",
        Path(__file__).parent / "data" / "portfolio_holdings.csv",
    ]
    for path in candidates:
        try:
            with path.open(encoding="utf-8-sig") as handle:
                holdings = [
                    row.get("ticker", "") for row in csv.DictReader(handle)
                    if str(row.get("market", "JP")).upper() == "JP"
                ]
            break
        except OSError:
            continue

    screener = []
    history = _load_json(
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "screener_history.json", {}
    )
    if isinstance(history, dict) and history:
        latest = history[sorted(history)[-1]]
        items = latest.get("candidates", []) if isinstance(latest, dict) else latest
        for item in items or []:
            if isinstance(item, dict):
                screener.append(item.get("ticker") or item.get("code") or "")

    # 2026-07-25 バッチ6: config.EDINET_SKIP_SECTORS を配線。
    # 「EdinetDB取得をスキップする業種コード（優先度低・APIコール節約）」と
    # 定義されていたのに実装が一度も参照しておらず、除外が効いていなかった。
    # 保有銘柄（P1）と screener 候補（P2）は投資判断に直結するため除外しない。
    # 対象は第3優先の代表銘柄のみ＝業種まるごとの取得を間引く、という設計意図に沿う。
    _skip = {str(s) for s in (getattr(cfg, "EDINET_SKIP_SECTORS", None) or [])}
    representatives = []
    for _sector_code, tickers in getattr(
            cfg, "SECTOR_REPRESENTATIVE_TICKERS", {}).items():
        if str(_sector_code) in _skip:
            continue
        representatives.extend(tickers)
    return holdings, screener[:50], representatives


def _company_row(company: dict, now: str) -> dict:
    return {
        "edinet_code": company.get("edinet_code"),
        "sec_code": company.get("sec_code"),
        "name": company.get("name") or company.get("name_ja"),
        "name_en": company.get("name_en"),
        "industry": company.get("industry"),
        "listing_status": company.get("listing_status"),
        "is_delisted": bool(company.get("is_delisted", False)),
        "accounting_standard": company.get("accounting_standard"),
        "credit_score": _integer(company.get("credit_score")),
        "corporate_number": company.get("corporate_number"),
        "last_fetched_at": now,
        "updated_at": now,
    }


def _plan_targets(companies: list[dict], cfg, meta: dict, budget: int) -> list[str]:
    by_sec = {
        _sec4(company.get("sec_code")): company
        for company in companies if _sec4(company.get("sec_code"))
    }
    holdings_t, screener_t, representatives_t = _priority_tickers(cfg)
    to_codes = lambda values: [
        by_sec[_sec4(value)]["edinet_code"]
        for value in values if _sec4(value) in by_sec
    ]
    return select_ingest_targets(
        to_codes(holdings_t), to_codes(screener_t), to_codes(representatives_t),
        [company.get("edinet_code") for company in companies if not company.get("is_delisted")],
        meta, budget, calls_per_company=3,
        refresh_days=int(getattr(cfg, "EDINETDB_REFRESH_DAYS", 180)),
    )


def _w3_company_budget(cfg, daily_budget: int) -> int:
    """usage/company-master/W4 doc-index分を除いた個社3 endpoint枠。"""
    doc_reserve = max(0, int(getattr(cfg, "EDINETDB_DOC_INDEX_RESERVE", 11)))
    return max(0, int(daily_budget) - 2 - doc_reserve)


def run_edinetdb_ingest(
    cfg,
    results: dict | int | None = None,
    budget: int | None = None,
    *,
    dry_run: bool = False,
    target_limit: int | None = None,
    doc_index_only: bool = False,
) -> dict:
    """優先対象を取得し、各BQ表へ冪等MERGEする。すべての障害はfail-soft。"""
    # 旧呼出し ``run_edinetdb_ingest(cfg, results)`` と、先行実装の
    # ``run_edinetdb_ingest(cfg, budget)`` の双方を壊さない。
    if budget is None and isinstance(results, int) and not isinstance(results, bool):
        budget = results
    if doc_index_only:
        from edinet_doc_indexer import run_doc_indexer
        return run_doc_indexer(
            days_back=int(getattr(cfg, "EDINETDB_LOOKBACK_DAYS", 7)), cfg=cfg
        )
    if not getattr(cfg, "ENABLE_EDINETDB_INGEST", True):
        return {"skipped": True, "reason": "disabled"}
    daily_budget = min(
        int(budget) if budget is not None else int(getattr(cfg, "EDINETDB_DAILY_BUDGET", 90)),
        int(getattr(cfg, "EDINETDB_DAILY_BUDGET", 90)),
    )
    # 通常運用はW4分も先取りする。明示budgetは受入試験・限定実行の総枠なので、
    # usage/company masterの2回だけを控除し、budget=5なら個社1件を計画できる。
    company_budget = (
        _w3_company_budget(cfg, daily_budget)
        if budget is None else max(0, daily_budget - 2)
    )
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    company_cache_path = data_dir / "edinetdb_company_cache.json"
    company_cache = _load_json(company_cache_path, {})
    cached_companies = company_cache.get("companies", []) if isinstance(company_cache, dict) else []
    meta_path = data_dir / getattr(cfg, "EDINETDB_ROTATION_META", "edinetdb_rotation_meta.json")
    meta = _load_json(meta_path, {})

    # 2026-07-30: 取得ローテを回す前に「BQに既にあるもの」を台帳へ反映する。
    # 台帳はローカルJSONのキャッシュに過ぎず BQ より遅れることがあり、
    # 実測で BQ 114社 vs 台帳 69社の乖離＝約45社の無駄な再取得があった。
    # 読み取り1クエリだけで、EdinetDB の API 予算は消費しない。
    if getattr(cfg, "EDINETDB_RECONCILE_WITH_BQ", True) and cached_companies:
        meta, _filled = reconcile_meta_with_bq(
            meta, cached_companies, _bq_ingested_latest(cfg))
        if _filled:
            print(f"  🔁 BQ照合: 取得済み{_filled}社を台帳へ補完（再取得を回避）")
            if not dry_run:
                _save_json(meta_path, meta)

    if dry_run:
        if not cached_companies:
            holdings, screener, representatives = _priority_tickers(cfg)
            targets = select_ingest_targets(
                holdings, screener, representatives, [], {}, company_budget,
                calls_per_company=3,
                refresh_days=int(getattr(cfg, "EDINETDB_REFRESH_DAYS", 180)),
            )
            identifier = "ticker (company cache未生成)"
        else:
            targets = _plan_targets(cached_companies, cfg, meta, company_budget)
            identifier = "edinet_code"
        if target_limit is not None:
            targets = targets[:max(0, int(target_limit))]
        return {
            "skipped": False, "dry_run": True, "targets": targets,
            "identifier": identifier, "budget": daily_budget,
        }

    api_key = str(getattr(cfg, "EDINETDB_API_KEY", "") or "").strip()
    if not api_key:
        return {"skipped": True, "reason": "EDINETDB_API_KEY missing"}
    client = DBRestClient(
        getattr(cfg, "EDINETDB_BASE_URL", "https://edinetdb.jp/v1"), api_key,
        data_dir / "edinetdb_cache", daily_budget, "edinetdb",
    )
    usage = client.usage() or {}
    remote_caps = []
    for key in ("daily_remaining", "monthly_remaining"):
        try:
            if usage.get(key) is not None:
                remote_caps.append(max(0, int(usage[key])))
        except (TypeError, ValueError):
            pass
    try:
        monthly_used = int(usage.get("monthly_used"))
        remote_caps.append(max(
            0, int(getattr(cfg, "EDINETDB_MONTHLY_GUARD", 1450)) - monthly_used
        ))
    except (TypeError, ValueError):
        pass
    if remote_caps:
        daily_budget = min(daily_budget, *remote_caps)
        client.daily_budget = daily_budget
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
    except Exception as exc:
        return {"skipped": True, "reason": f"BQ unavailable: {type(exc).__name__}"}

    companies = cached_companies
    cache_date = str(company_cache.get("fetched_at", ""))[:10] if isinstance(company_cache, dict) else ""
    try:
        cache_fresh = (date.today() - date.fromisoformat(cache_date)).days < 30
    except ValueError:
        cache_fresh = False
    if not companies or not cache_fresh:
        fetched = client.get("/companies", {"per_page": 5000, "page": 1})
        if not isinstance(fetched, list):
            return {"skipped": True, "reason": "company master unavailable"}
        companies = fetched
        _save_json(company_cache_path, {
            "fetched_at": date.today().isoformat(), "companies": companies
        })

    now = datetime.now(timezone.utc).isoformat()
    company_rows = [_company_row(c, now) for c in companies if c.get("edinet_code")]
    try:
        bq.merge_rows("company", company_rows, ["edinet_code"])
    except Exception as exc:
        return {"skipped": True, "reason": f"company MERGE failed: {type(exc).__name__}"}

    by_edinet = {str(company.get("edinet_code")): company for company in companies}
    targets = _plan_targets(companies, cfg, meta, company_budget)
    if target_limit is not None:
        targets = targets[:max(0, int(target_limit))]
    success = failure = 0
    for code in targets:
        company = by_edinet.get(code, {})
        company_code = _sec4(company.get("sec_code"))
        # 予算枯渇・レート制限に入ったら以降の企業は必ず失敗する。
        # 打ち切りが無かったため、2026-08-27 は予算切れ後も11社を回して
        # failure だけを積み上げていた（成功0/11）。
        if _stop_ingest(client):
            break
        financials = client.get(f"/companies/{code}/financials", {"years": 5, "period": "annual"})
        if not isinstance(financials, list):
            failure += 1
            if _stop_ingest(client):
                break
            continue
        segments = client.get(f"/companies/{code}/segments", {"empty_as_200": "true"})
        if not isinstance(segments, list):
            # 予算枯渇や一時障害を空データと誤認すると、refresh期間中ずっと
            # segmentsが欠落する。成功台帳を進めず、次回に再試行させる。
            failure += 1
            if _stop_ingest(client):
                break
            continue
        fin_rows = [
            map_edinetdb_financials(item, edinet_code=code, company_code=company_code, now=now)
            for item in financials if isinstance(item, dict)
        ]
        latest_year = max(
            [row.get("fiscal_year") for row in fin_rows if row.get("fiscal_year") is not None],
            default=None,
        )
        seg_rows, sf_rows = map_edinetdb_segments(
            code, segments if isinstance(segments, list) else [], latest_year,
            company_code=company_code, now=now,
        )
        text_rows = []
        fetch_count = int((meta.get(code) or {}).get("fetch_count", 0))
        every_n = max(1, int(getattr(cfg, "EDINETDB_TEXTBLOCK_EVERY_N", 2)))
        if fetch_count % every_n == 0:
            blocks = client.get(
                f"/companies/{code}/text-blocks",
                text_block_params(latest_year),
            )
            if not isinstance(blocks, list):
                # full text未取得を成功扱いせず、同じfetch_countで再試行する。
                failure += 1
                if _stop_ingest(client):
                    break
                continue
            text_rows = map_edinetdb_text_blocks(
                code, blocks, latest_year,
                company_code=company_code,
                fiscal_year_end=next(
                    (row.get("fiscal_year_end") for row in fin_rows if row.get("fiscal_year_end")), None
                ), now=now,
            )
        try:
            bq.merge_rows("financials", fin_rows, ["edinet_code", "fiscal_year"])
            bq.merge_rows("segments", seg_rows, ["segment_id"])
            bq.merge_rows("segment_finance", sf_rows, ["sf_id"])
            bq.merge_rows("text_analysis", text_rows, ["text_id"])
            meta[code] = {
                "last_fetched": date.today().isoformat(),
                "fetch_count": fetch_count + 1, "company_code": company_code,
            }
            success += 1
        except Exception:
            failure += 1
    _save_json(meta_path, meta)
    return {
        "skipped": False, "success": success, "failure": failure,
        "targets": targets, "budget": daily_budget, "api_calls": client.calls,
    }


if __name__ == "__main__":
    import argparse
    import config as cfg
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="対象企業数上限")
    args = parser.parse_args()
    print(json.dumps(
        run_edinetdb_ingest(cfg, dry_run=args.dry_run, target_limit=args.limit),
        ensure_ascii=False, indent=2,
    ))
