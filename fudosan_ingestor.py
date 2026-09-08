"""不動産DB P1（企業保有不動産・エリア情報）をBQへ保存するingestor。"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from db_rest_client import DBRestClient
from edinetdb_ingestor import select_ingest_targets


def _number(value):
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _integer(value):
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _field(row: dict, *names):
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def _items(data) -> list[dict]:
    """共通封筒のdataが配列またはページングobjectのどちらでも受ける。"""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("holdings", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def map_corporate_holding(row: dict, *, now: str | None = None) -> dict | None:
    """要件書の実測フィールドだけをcorporate_holdingsへ写像する。"""
    code = str(_field(row, "edinet_code", "edinetCode") or "").strip()
    fiscal_year = _integer(_field(row, "fiscal_year", "fiscalYear"))
    facility = str(_field(row, "facility_name", "facilityName") or "").strip()
    if not code or fiscal_year is None or not facility:
        return None
    now = now or datetime.now(timezone.utc).isoformat()
    return {
        "edinet_code": code,
        "company_name": _field(row, "company_name", "companyName"),
        "fiscal_year": fiscal_year,
        "facility_name": facility,
        "prefecture": _field(row, "prefecture", "prefecture_name"),
        "municipality_code": _field(row, "municipality_code", "municipalityCode"),
        "book_value_land_m_yen": _number(row.get("book_value_land_m_yen")),
        "book_value_buildings_m_yen": _number(row.get("book_value_buildings_m_yen")),
        "land_area_m2": _number(row.get("land_area_m2")),
        "commercial_land_price_per_m2": _number(row.get("commercial_land_price_per_m2")),
        "residential_land_price_per_m2": _number(row.get("residential_land_price_per_m2")),
        "industrial_land_price_per_m2": _number(row.get("industrial_land_price_per_m2")),
        "population_current": _integer(row.get("population_current")),
        "population_2050": _integer(row.get("population_2050")),
        "population_change_pct": _number(row.get("population_change_pct")),
        "flood_risk_area_pct": _number(row.get("flood_risk_area_pct")),
        "landslide_risk_area_pct": _number(row.get("landslide_risk_area_pct")),
        "source": "fudosandb",
        "updated_at": now,
    }


def map_area_profile(row: dict, municipality_code: str, *, now: str | None = None) -> dict:
    """公式docsの現行フィールドと要件書の実測フィールドを欠損許容で写像する。"""
    now = now or datetime.now(timezone.utc).isoformat()
    return {
        "municipality_code": str(row.get("municipality_code") or municipality_code),
        "prefecture_name": row.get("prefecture_name"),
        "municipality_name": row.get("municipality_name"),
        "total_transactions": _integer(row.get("total_transactions")),
        "median_price": _number(row.get("median_price")),
        "latest_land_price": _number(row.get("latest_land_price")),
        "land_price_yoy": _number(row.get("land_price_yoy")),
        "avg_land_price_commercial": _number(row.get("avg_land_price_commercial")),
        "avg_land_price_residential": _number(row.get("avg_land_price_residential")),
        "land_price_yoy_change": _number(row.get("land_price_yoy_change")),
        "flood_risk_area_pct": _number(row.get("flood_risk_area_pct")),
        "latest_avg_price_condo": _number(row.get("latest_avg_price_condo")),
        "hospital_count": _integer(row.get("hospital_count")),
        "source": "fudosandb",
        "updated_at": now,
    }


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _save_json(path: Path, value) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"⚠️ fudosandb 状態保存失敗: {exc}")


def _priority_codes(cfg, companies: list[dict]) -> tuple[list[str], list[str], list[str], list[str]]:
    by_sec = {}
    for company in companies:
        sec = str(company.get("sec_code") or "").strip().split(".")[0][:4]
        if sec:
            by_sec[sec] = str(company.get("edinet_code") or "")

    from edinetdb_ingestor import _priority_tickers
    holdings, screener, representatives = _priority_tickers(cfg)
    convert = lambda values: [
        by_sec[str(value).strip().split(".")[0][:4]]
        for value in values
        if str(value).strip().split(".")[0][:4] in by_sec
    ]
    remaining = [
        str(company.get("edinet_code")) for company in companies
        if company.get("edinet_code") and not company.get("is_delisted")
    ]
    return convert(holdings), convert(screener), convert(representatives), remaining


def plan_fudosan_targets(
    companies: list[dict], cfg, meta: dict, budget: int, *, today: date | None = None
) -> list[str]:
    groups = _priority_codes(cfg, companies)
    return select_ingest_targets(
        *groups,
        meta,
        budget,
        calls_per_company=2,
        refresh_days=int(getattr(cfg, "FUDOSAN_REFRESH_DAYS", 365)),
        today=today,
    )


def _company_matches(companies: list[dict], query: str) -> list[str]:
    """社名・証券コード・EDINETコードの部分一致をEDINETコードへ解決する。"""
    needle = str(query or "").strip().casefold()
    if not needle:
        return []
    matches = []
    for company in companies:
        values = (
            company.get("edinet_code"), company.get("sec_code"),
            company.get("name"), company.get("name_ja"), company.get("name_en"),
        )
        if any(needle in str(value or "").casefold() for value in values):
            code = str(company.get("edinet_code") or "").strip()
            if code and code not in matches:
                matches.append(code)
    return matches


def _effective_daily_budget(cfg, requested: int | None = None) -> int:
    configured = int(getattr(cfg, "FUDOSAN_DAILY_BUDGET", 900))
    cutoff = date.fromisoformat(str(getattr(cfg, "FUDOSAN_BACKFILL_CUTOFF", "2026-07-30")))
    post_cutoff = int(getattr(cfg, "FUDOSAN_POST_CUTOFF_BUDGET", 90))
    effective = configured if date.today() <= cutoff else min(configured, post_cutoff)
    return min(effective, int(requested)) if requested is not None else effective


def run_fudosan_ingest(
    cfg, budget: int | None = None, *, dry_run: bool = False,
    target_limit: int | None = None, company: str | None = None,
) -> dict:
    """P1対象を優先ローテーションし、2表へ冪等MERGEする（fail-soft）。"""
    if not getattr(cfg, "ENABLE_FUDOSAN_INGEST", True):
        return {"skipped": True, "reason": "disabled"}
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    companies_doc = _load_json(data_dir / "edinetdb_company_cache.json", {})
    companies = companies_doc.get("companies", []) if isinstance(companies_doc, dict) else []
    if not companies:
        if dry_run:
            if company:
                return {
                    "skipped": False, "dry_run": True, "targets": [company],
                    "identifier": "company query (company cache未生成)",
                    "budget": _effective_daily_budget(cfg, budget),
                }
            from edinetdb_ingestor import _priority_tickers
            holdings, screener, representatives = _priority_tickers(cfg)
            targets = select_ingest_targets(
                holdings, screener, representatives, [], {}, _effective_daily_budget(cfg, budget),
                calls_per_company=2,
                refresh_days=int(getattr(cfg, "FUDOSAN_REFRESH_DAYS", 365)),
            )
            if target_limit is not None:
                targets = targets[:max(0, int(target_limit))]
            return {
                "skipped": False, "dry_run": True, "targets": targets,
                "identifier": "ticker (company cache未生成)",
                "budget": _effective_daily_budget(cfg, budget),
            }
        return {"skipped": True, "reason": "EdinetDB company cache missing"}

    daily_budget = _effective_daily_budget(cfg, budget)
    meta_path = data_dir / getattr(cfg, "FUDOSAN_ROTATION_META", "fudosan_rotation_meta.json")
    meta = _load_json(meta_path, {})
    if company:
        targets = _company_matches(companies, company)
        if not targets:
            return {
                "skipped": True, "reason": f"company not found: {company}",
                "targets": [], "budget": daily_budget,
            }
    else:
        targets = plan_fudosan_targets(companies, cfg, meta, daily_budget)
    if target_limit is not None:
        targets = targets[:max(0, int(target_limit))]
    if dry_run:
        return {"skipped": False, "dry_run": True, "targets": targets, "budget": daily_budget}

    api_key = str(getattr(cfg, "FUDOSAN_API_KEY", "") or "").strip()
    if not api_key:
        return {"skipped": True, "reason": "FUDOSAN_API_KEY missing", "targets": targets}
    client = DBRestClient(
        getattr(cfg, "FUDOSAN_BASE_URL", "https://fudosandb.jp/v1"),
        api_key,
        data_dir / "fudosandb_cache",
        daily_budget,
        "fudosandb",
        usage_endpoint=None,
    )
    try:
        from edinet_bq_client import EDINETBQClient
        bq = EDINETBQClient()
    except Exception as exc:
        return {"skipped": True, "reason": f"BQ unavailable: {type(exc).__name__}"}

    now = datetime.now(timezone.utc).isoformat()
    success = failure = holdings_count = area_count = 0
    area_meta = meta.get("_area_profiles", {})
    if not isinstance(area_meta, dict):
        area_meta = {}
    seen_municipalities: set[str] = set()
    budget_stop = False
    for code in targets:
        if budget_stop:
            # 予算を使い切った後は全企業が失敗するだけなので周回を止める。
            break
        raw = client.get(
            "/corporate/real-estate-holdings", {"edinet_code": code, "limit": 2000}
        )
        raw_items = _items(raw)
        if raw is None or (not raw_items and not isinstance(raw, (list, dict))):
            failure += 1
            # rate_limited も budget と同じく「以降すべて失敗する」状態。
            # 打ち切らないと残り全社を空振りで回して失敗数だけ積む。
            if getattr(client, "last_error", None) in ("budget", "rate_limited"):
                break
            continue
        holding_rows = [map_corporate_holding(item, now=now) for item in raw_items]
        holding_rows = [row for row in holding_rows if row]
        municipality_codes = list(dict.fromkeys(
            str(row.get("municipality_code")) for row in holding_rows
            if row.get("municipality_code")
        ))
        area_rows = []
        area_updates: dict[str, dict] = {}
        profile_failed = False
        for municipality_code in municipality_codes:
            if municipality_code in seen_municipalities:
                continue
            last_fetched = str(
                (area_meta.get(municipality_code) or {}).get("last_fetched") or ""
            )[:10]
            try:
                if (date.today() - date.fromisoformat(last_fetched)).days < int(
                    getattr(cfg, "FUDOSAN_REFRESH_DAYS", 365)
                ):
                    seen_municipalities.add(municipality_code)
                    continue
            except ValueError:
                pass
            profile = client.get(f"/area-profile/{municipality_code}")
            # 2026-07-28: 失敗種別で扱いを分ける。
            #   ・成功        → 取得済みとして記録
            #   ・404         → その自治体にデータが無い＝再試行しても永遠に
            #                   取れない。取得済み扱いにして企業の完了を妨げない
            #   ・429/予算枯渇 → 一時障害。metaには記録せず次回に再試行する
            # いずれの場合も seen_municipalities には入れる。
            # 【なぜ入れるか】以前は失敗時に seen へ入れず「同一実行内でも別企業
            # から再試行できる」設計だったが、失敗が続く自治体を参照する企業が
            # N社あると1回の実行で同じ自治体をN回叩き、日次予算900を
            # 使い切っていた（実測: 33社しか完了していないのに予算満額消費）。
            # 1実行につき1自治体1回に制限し、再試行は次回の実行に回す。
            seen_municipalities.add(municipality_code)
            if isinstance(profile, dict):
                area_rows.append(map_area_profile(profile, municipality_code, now=now))
                area_updates[municipality_code] = {
                    "last_fetched": date.today().isoformat()
                }
            elif getattr(client, "last_error", None) == "not_found":
                # データ不在を「取得済み・空」として記録する。
                # これをしないと該当自治体を持つ企業が永久に完了しない。
                area_updates[municipality_code] = {
                    "last_fetched": date.today().isoformat(),
                    "empty": True,
                }
            else:
                profile_failed = True
                if getattr(client, "last_error", None) in ("budget",
                                                           "rate_limited"):
                    # 予算枯渇・レート制限後は以降の企業も必ず失敗する。
                    # 無駄な周回を止める。
                    budget_stop = True
                    break
        try:
            bq.merge_rows(
                getattr(cfg, "FUDOSAN_HOLDINGS_TABLE", "corporate_holdings_p1"), holding_rows,
                ["edinet_code", "fiscal_year", "facility_name"], dataset="real_estate",
            )
            bq.merge_rows(
                getattr(cfg, "FUDOSAN_AREA_TABLE", "area_profile_p1"),
                area_rows, ["municipality_code"], dataset="real_estate"
            )
            holdings_count += len(holding_rows)
            area_count += len(area_rows)
            area_meta.update(area_updates)
            if profile_failed:
                # holdingsは保存済みでも、未取得profileがある限り企業ローテーションを
                # 完了させず、次回に欠損分を再試行する。
                failure += 1
            else:
                meta[code] = {"last_fetched": date.today().isoformat()}
                success += 1
        except Exception:
            failure += 1
    meta["_area_profiles"] = area_meta
    _save_json(meta_path, meta)
    log_counts = client.log_summary() if hasattr(client, "log_summary") else {}
    return {
        "skipped": False, "success": success, "failure": failure,
        "holdings": holdings_count, "area_profiles": area_count,
        "targets": targets, "budget": daily_budget,
        "budget_stop": budget_stop,
        "log_counts": log_counts,
    }


if __name__ == "__main__":
    import config as cfg
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--company", default=None, help="社名・証券コード・EDINETコード")
    args = parser.parse_args()
    result = run_fudosan_ingest(
        cfg, dry_run=args.dry_run, target_limit=args.limit, company=args.company
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
