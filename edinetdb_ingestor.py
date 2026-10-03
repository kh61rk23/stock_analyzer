"""EdinetDB REST を既存 BigQuery ``edinet.*`` へ格納する一次ingestor。"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from db_rest_client import DBRestClient


def _brief_error(exc: Exception, limit: int = 240) -> str:
    """例外メッセージからURLを畳んで、理由の部分だけを残す。

    google-cloud-bigquery の例外は
    `400 GET https://bigquery.googleapis.com/.../queries/<uuid>?...: <理由>`
    という形で、素朴に先頭を切るとURLだけが残って理由が消える。
    """
    text = " ".join(str(exc).split())
    text = re.sub(r"https?://\S+", "<url>", text)
    return text[:limit]


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
        # 2026-09-26: 常に None で書いており、BQ の accrual_ratio 列は全2,002行が空だった
        "accrual_ratio": _ratio(net_income - op_cf, total_assets)
        if net_income is not None and op_cf is not None else None,
        "fcf_yield": None,
        "labor_productivity": _ratio(net_sales, employee_count),
        "currency": "JPY",
        "source": "edinetdb",
        "updated_at": now,
    }


def map_edinetdb_financial_items(
    row: dict,
    *,
    edinet_code: str,
    company_code: str = "",
    fiscal_year_end: str | None = None,
    now: str | None = None,
) -> list[dict]:
    """1年度分の財務レコードを、数値項目ごとの縦持ち行（edinet.financial_items）にする。

    2026-10-03: /financials は1年度178項目（売上原価・販管費・研究開発費・支払利息・
    棚卸資産・のれん・支払配当金など）を返すが、edinet.financials へは約25項目しか
    写しておらず残りは捨てていた。追加のAPI呼び出しなしで全数値項目を残す。
    定義に無い数値項目（API側の追加分）も区分「その他」で残す。
    """
    from edinetdb_fields import ITEM_META
    now = now or datetime.now(timezone.utc).isoformat()
    fy = _integer(row.get("fiscal_year") or row.get("fiscalYear"))
    if not edinet_code or fy is None:
        return []
    doc_id = str(row.get("doc_id") or row.get("docId") or "") or None
    out = []
    for key, raw in row.items():
        if key in ("fiscal_year", "fiscalYear") or isinstance(raw, bool):
            continue
        if not isinstance(raw, (int, float)):
            continue
        value = _number(raw)
        if value is None or value != value:   # NaN
            continue
        statement, label, unit = ITEM_META.get(key, ("その他", key, ""))
        out.append({
            "item_id": f"{edinet_code}_{fy}_{key}",
            "edinet_code": edinet_code, "company_code": company_code or None,
            "fiscal_year": fy, "fiscal_year_end": fiscal_year_end,
            "statement": statement, "item": key, "label_ja": label, "unit": unit,
            "value": value,
            "accounting_standard": row.get("accounting_standard"),
            "basis": row.get("basis"),
            "doc_id": doc_id, "source": "edinetdb", "updated_at": now,
        })
    return out


def map_edinetdb_doc_index(
    row: dict,
    company: dict,
    *,
    edinet_code: str = "",
    company_code: str = "",
    now: str | None = None,
) -> dict | None:
    """財務の年次レコードから doc_index 行を作る（追加のAPI呼び出しなし）。

    2026-09-15: EdinetDB の年次財務レコードは EDINET 書類ID（doc_id / docID）と
    提出日時（submit_date_time / submitDateTime）を持つ。BQ financials にも
    1,647行・340社ぶん本物の書類IDが入っていたのに、doc_index は別の開示
    エンドポイント（doc_id が空で返る）に頼っており 2026-04-26 から1件も
    増えていなかった。同じ応答から作れば呼び出しは増えない。
    書類IDが EDINET 形式（S1xxxxxx）でない合成IDの年度は作らない。
    """
    now = now or datetime.now(timezone.utc).isoformat()
    doc_id = str(_field(row, "doc_id", "docId", "docID") or "").strip()
    if not re.match(r"^S1[0-9A-Z]{6}$", doc_id):
        return None
    submitted = str(_field(row, "submit_date_time", "submitDateTime",
                           "submit_date", "submitDate") or "")[:10]
    try:
        disclosure_date = date.fromisoformat(submitted).isoformat()
    except ValueError:
        disclosure_date = None
    standard = str(_field(row, "accounting_standard", "accountingStandard")
                   or company.get("accounting_standard") or "").upper()
    return {
        "doc_id": doc_id,
        "company_code": company_code or None,
        "edinet_code": edinet_code or None,
        "company_name": company.get("name") or company.get("name_ja"),
        "doc_type": "有価証券報告書",
        "fiscal_year_end": _explicit_date(row),
        "disclosure_date": disclosure_date,
        "xbrl_available": True,
        "html_available": False,
        "fetched_flag": True,
        "fetched_at": now,
        "ifrs_flag": standard == "IFRS",
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
    fail_backoff_max_days: int = 30,
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
        info = meta.get(code) or {}
        value = str(info.get("last_fetched") or "")[:10]
        try:
            recent = (today - date.fromisoformat(value)).days < refresh_days
        except ValueError:
            return False
        # 新年度の有報が出ているはずなら、180日以内でも取り直す（2026-10-02）
        return recent and not report_due(info, today)

    def backing_off(code: str) -> bool:
        """直近に失敗した企業を一定日数スキップする。

        2026-09-10: last_fetched は**成功時にしか書かれない**ため、
        毎回失敗する企業は永久に fresh にならず、優先度P1/P2の先頭に
        居座って枠を塞いでいた。実際 2026-09-07/09/10 の3実行とも
        先頭6社（E01719/E36174/E02324/E03693/E04275/E37577）が同一で、
        11枠のうち8枠が同じ失敗の再試行に消えていた。
        指数的に間隔を空け、EdinetDB側にデータが増えたら拾い直す。
        """
        info = meta.get(code) or {}
        value = str(info.get("last_attempt") or "")[:10]
        if not value:
            return False
        fails = int(info.get("fail_count") or 1)
        # 1日 → 2 → 4 → 8 …（上限 fail_backoff_max_days）
        wait = min(2 ** max(0, fails - 1), max(1, int(fail_backoff_max_days)))
        try:
            return (today - date.fromisoformat(value)).days < wait
        except ValueError:
            return False

    # P4だけは「未取得→最終取得が古い順」のFIFOにする。
    remaining_fifo = sorted(
        remaining or [],
        key=lambda item: str(
            (meta.get(code_of(item)) or {}).get("last_fetched") or "0000-00-00"
        ),
    )
    # 新年度の有報が出ているはずの会社は、代表銘柄・未取得より先に取り直す
    due_group = [item for item in remaining or []
                 if report_due(meta.get(code_of(item)) or {}, today)]
    for group in (holdings, screener, due_group, representatives, remaining_fifo):
        for item in group or []:
            code = code_of(item)
            if not code or code in seen:
                continue
            seen.add(code)
            if fresh(code) or backing_off(code):
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
    # 2026-09-29: 2024年以降の新規上場は英字入りの証券コード（例: 624A・154A）。
    # 数字4桁しか受け付けず空文字になり、BQ に company_code=None で格納されていた
    value = str(value or "").upper().replace(".T", "").strip()
    if len(value) == 5 and value.endswith("0"):
        value = value[:4]
    return value if re.fullmatch(r"\d{3}[0-9A-Z]", value) else ""


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


# ==============================================================
# Web事前確認（2026-09-29・健太の要望）
# EdinetDB に財務が無い会社（主に新規上場）を API で取りに行き、毎日の枠を
# 空振りで潰していた（9/22・9/24 は11社すべて「データ無し」）。取り込み前に
# EdinetDB の公開Webページ（APIではない・予算を消費しない）を見て、財務が
# 無ければその会社は飛ばし、次の候補へ進む。
# ==============================================================
_WEB_HAS = "開示財務データ"
_WEB_LACK = "分析に必要なデータが不足"


def page_has_financials(html: str | None, status: int = 200):
    """会社ページのHTMLから財務データの有無を判定する（純関数）。

    True=財務あり / False=財務なし・ページなし / None=判定不能（通信失敗など）
    2026-09-29 実測: 財務ありの会社は「開示財務データ」欄と「FY20xxに売上高…」の
    要約文があり、APIで not_found だった会社は全て「分析に必要なデータが不足」。
    """
    if status == 404:
        return False
    if status != 200 or not html:
        return None
    if _WEB_HAS in html:
        return True
    if _WEB_LACK in html:
        return False
    return None


def parse_company_page(html: str | None, status: int = 200) -> dict:
    """会社ページから財務の有無・掲載中の最新年度・決算月を読む（純関数）。

    2026-10-02: 最新年度と決算月も読む。例: ザインエレクトロニクスは「12月期」
    「FY2025 開示財務データ」（2025年12月期が最新・2026-03-26提出）。
    """
    import re as _re
    out = {"has": page_has_financials(html, status), "latest_fy": None, "fy_month": None}
    if not html or status != 200:
        return out
    fys = [int(y) for y in _re.findall(r"FY(20\d\d)\s*開示財務データ", html)]
    fys += [int(y) for y in _re.findall(r'"fiscalYear":\s*(20\d\d)', html)]
    if fys:
        out["latest_fy"] = max(fys)
    m = _re.search(r"(?<![0-9])(1[0-2]|[1-9])月期", html)
    if m:
        out["fy_month"] = int(m.group(1))
    return out


def _web_check(code: str, cfg) -> dict:
    base = getattr(cfg, "EDINETDB_WEB_BASE", "https://edinetdb.jp/company/")
    try:
        import requests
        r = requests.get(base + code, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        return parse_company_page(r.text, r.status_code)
    except Exception:
        return {"has": None, "latest_fy": None, "fy_month": None}


def fiscal_year_end_date(fiscal_year, fy_month) -> str | None:
    """FY ラベル（決算期末の年）と決算月から期末日を返す。EdinetDB の FY2025 は
    12月決算なら 2025-12-31、3月決算なら 2025-03-31（期末の年）。"""
    import calendar
    try:
        y, m = int(fiscal_year), int(fy_month)
        return date(y, m, calendar.monthrange(y, m)[1]).isoformat()
    except (TypeError, ValueError):
        return None


def report_due(info: dict, today: date | None = None, grace_days: int = 100,
               retry_days: int = 14) -> bool:
    """新しい年度の有報が出ているはずなのに、手元の最新年度が古いか（純関数）。

    2026-10-02: 鮮度は「最終取得から180日」だけで見ていたため、取得直後に
    新しい有報が出ると最大180日古いまま残る。決算月から「期末+100日」を過ぎた
    年度を期待年度とし、手元（held_fy）がそれより古ければ取り直す。
    Webページに新しい年度が載っていれば、それも根拠にする。
    直近 retry_days 以内に取り直して増えていなければ待つ（EdinetDB側の反映待ち）。
    """
    today = today or date.today()
    held = info.get("held_fy")
    if held is None:
        return False
    held = int(held)
    try:
        last = date.fromisoformat(str(info.get("last_fetched") or "")[:10])
        if (today - last).days < retry_days:
            return False
    except ValueError:
        pass
    web_fy = (info.get("web_check") or {}).get("latest_fy")
    if web_fy and int(web_fy) > held:
        return True
    m = info.get("fy_month")
    if not m:
        return held < today.year - 1
    expected = today.year
    end = fiscal_year_end_date(expected, m)
    if end is None or (today - date.fromisoformat(end)).days < grace_days:
        expected -= 1
    return held < expected


def prefilter_targets(candidates: list[str], want: int, meta: dict, cfg,
                      checker=None, today: date | None = None,
                      sleep=None) -> tuple[list[str], list[str]]:
    """Webで財務が確認できた会社を want 社まで選ぶ（財務なしは飛ばして次へ）。

    確認結果は meta[code]["web_check"]={"date","has"} に残す。
    財務なしは RECHECK 日後に再確認（後から載ることがある）、財務ありは30日有効。
    判定不能（通信失敗）は従来どおり API で取りに行く。
    Returns: (selected, skipped_no_data)
    """
    import time as _t
    today = today or date.today()
    checker = checker or (lambda c: _web_check(c, cfg))
    sleep = sleep or _t.sleep
    recheck = int(getattr(cfg, "EDINETDB_WEB_RECHECK_DAYS", 7))
    max_checks = max(want, int(getattr(cfg, "EDINETDB_WEB_MAX_CHECKS", want * 5)))
    selected, skipped, checks = [], [], 0
    for code in candidates:
        if len(selected) >= want:
            break
        info = dict(meta.get(code) or {})
        wc = info.get("web_check") or {}
        try:
            age = (today - date.fromisoformat(str(wc.get("date", ""))[:10])).days
        except ValueError:
            age = 10 ** 6
        if wc.get("has") is False and age < recheck:
            skipped.append(code)
            continue
        # 決算月が未記録の旧形式は、ついでに読み直す（2026-10-02）
        if wc.get("has") is True and age < 30 and "fy_month" in wc:
            selected.append(code)
            continue
        if checks >= max_checks:
            if wc.get("has") is True:
                selected.append(code)
                continue
            break
        if checks:
            sleep(1.0)          # 相手サイトへの配慮（1秒に1回まで）
        checks += 1
        res = checker(code)
        if not isinstance(res, dict):
            res = {"has": res, "latest_fy": None, "fy_month": None}
        has = res.get("has")
        if has is not None:
            info["web_check"] = {"date": today.isoformat(), **res}
            if res.get("fy_month"):
                info["fy_month"] = res["fy_month"]
            meta[code] = info
        if has is False:
            skipped.append(code)
        else:
            selected.append(code)
    return selected, skipped


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
        # 2026-10-02: 30→7日。社名変更・上場廃止・持株会社化を早く反映する（1回で全社取得）
        cache_fresh = (date.today() - date.fromisoformat(cache_date)).days < int(
            getattr(cfg, "EDINETDB_COMPANY_CACHE_DAYS", 7))
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
    web_skipped: list[str] = []
    # 2026-10-01: 計画は1社3回で見積もるが、text-blocks は2回に1回しか取らないため
    # 実際は約2.5回/社で、毎日10回前後の枠が使われずに残っていた。
    # 予備の候補を後ろに足しておき、ループ側で「残り枠が1社分(3回)を切るまで」回す。
    reserve_calls = (max(0, int(getattr(cfg, "EDINETDB_DOC_INDEX_RESERVE", 11)))
                     if budget is None else 0)
    if target_limit is None and targets:
        # 全社が2回/社で済んだ場合でも枠を埋められる数まで候補を広げる
        pool = _plan_targets(companies, cfg, meta, company_budget * 3 // 2 + 3)
        if getattr(cfg, "EDINETDB_WEB_PRECHECK", False):
            # 候補を多めに選び、Webで財務が確認できた会社だけで枠を埋める
            wide = _plan_targets(companies, cfg, meta, company_budget * 5)
            targets, web_skipped = prefilter_targets(wide, len(pool), meta, cfg)
            print(f"    🔎 [edinetdb] Web事前確認: 採用{len(targets)}社 / "
                  f"財務なしで対象外{len(web_skipped)}社")
        else:
            targets = pool
    success = failure = missing = 0
    # 2026-09-29: 取り込んだ会社を通知メールに載せるため、社名つきで返す
    ingested: list[dict] = []
    missing_list: list[dict] = []
    doc_index_rows = 0
    # 2026-09-10: failure は4つの別原因（financials欠落 / segments欠落 /
    # text-blocks欠落 / BQ MERGE例外）を1つの数字に潰しており、
    # 台帳に「failure: 8」と出るだけで何が起きているか分からなかった。
    # 内訳を数え、EdinetDB側にデータが無いだけの not_found は
    # 障害ではなく "missing" として区別する。
    reasons: dict = {"financials": 0, "segments": 0, "text_blocks": 0,
                     "bq_merge": 0, "missing": 0}
    today_str = date.today().isoformat()

    def _note_attempt(code: str, company_code: str, *, missing: bool) -> None:
        """失敗も台帳に残してローテーションを前へ進める。

        last_fetched は成功時にしか書かれないため、毎回失敗する企業は
        永久に「未取得」のまま優先度上位に居座り、枠を塞いでいた。
        """
        info = dict(meta.get(code) or {})
        info["last_attempt"] = today_str
        info["fail_count"] = int(info.get("fail_count") or 0) + 1
        info["company_code"] = company_code or info.get("company_code")
        if missing:
            info.setdefault("missing_since", today_str)
        meta[code] = info

    def _record_failure(code: str, company_code: str, kind: str) -> None:
        """失敗を記録する。

        2026-09-12: EdinetDB側にそもそも当該企業のデータが無い（404）のは
        こちらの障害ではなく正常な結果なので `failure` に数えない。
        従来は混ぜていたため、実問題が0でも台帳が alert になり
        （2026-09-12 実測: failure 2 = missing 2 / bq_merge 0）、
        本当に直すべき失敗が埋もれていた。
        `missing` は reasons と戻り値で別建てにする。
        """
        nonlocal failure, missing
        is_missing = getattr(client, "last_error", None) == "not_found"
        if is_missing:
            reasons["missing"] += 1
            missing += 1
            _c = by_edinet.get(code, {})
            missing_list.append({"edinet_code": code, "sec_code": company_code,
                                 "name": _c.get("name") or _c.get("name_ja") or ""})
        else:
            reasons[kind] += 1
            failure += 1
        _note_attempt(code, company_code, missing=is_missing)

    item_rows: list[dict] = []          # 2026-10-03: 財務三表の細目（edinet.financial_items）
    items_done: set[str] = set()

    def _items_from(financials, code, company_code, fin_rows):
        fye = {r.get("fiscal_year"): r.get("fiscal_year_end") for r in fin_rows}
        rows = []
        for item in financials:
            if isinstance(item, dict):
                fy = _integer(item.get("fiscal_year") or item.get("fiscalYear"))
                rows += map_edinetdb_financial_items(
                    item, edinet_code=code, company_code=company_code,
                    fiscal_year_end=fye.get(fy), now=now)
        items_done.add(code)
        return rows

    for code in targets:
        company = by_edinet.get(code, {})
        company_code = _sec4(company.get("sec_code"))
        # 予算枯渇・レート制限に入ったら以降の企業は必ず失敗する。
        # 打ち切りが無かったため、2026-08-27 は予算切れ後も11社を回して
        # failure だけを積み上げていた（成功0/11）。
        if _stop_ingest(client):
            break
        # 残り枠がこの会社の分（2回＋本文を取る回なら1回）を切ったら止める。
        # 3回固定で見ていたため、2回で済む会社の分の枠が毎日余っていた（2026-10-02）
        _fc = int((meta.get(code) or {}).get("fetch_count", 0))
        _need = 2 + (1 if _fc % max(1, int(getattr(cfg, "EDINETDB_TEXTBLOCK_EVERY_N", 2))) == 0 else 0)
        if getattr(client, "daily_budget", daily_budget) - client.calls - reserve_calls < _need:
            continue
        financials = client.get(f"/companies/{code}/financials", {"years": 5, "period": "annual"})
        if not isinstance(financials, list):
            # 予算枯渇・レート制限で落ちた分は「その企業の失敗」ではないので
            # 台帳を進めない（次回そのまま再試行させる）。
            if _stop_ingest(client):
                break
            _record_failure(code, company_code, "financials")
            continue
        segments = client.get(f"/companies/{code}/segments", {"empty_as_200": "true"})
        if not isinstance(segments, list):
            # 予算枯渇や一時障害を空データと誤認すると、refresh期間中ずっと
            # segmentsが欠落する。成功台帳を進めず、次回に再試行させる。
            if _stop_ingest(client):
                break
            _record_failure(code, company_code, "segments")
            continue
        fin_rows = [
            map_edinetdb_financials(item, edinet_code=code, company_code=company_code, now=now)
            for item in financials if isinstance(item, dict)
        ]
        # 2026-10-02: APIに期末日が無く fiscal_year_end が全行空だった。
        # 会社ページの決算月（例「12月期」）から埋める
        _fm = (meta.get(code) or {}).get("fy_month")
        for _r in fin_rows:
            if not _r.get("fiscal_year_end") and _fm:
                _r["fiscal_year_end"] = fiscal_year_end_date(_r.get("fiscal_year"), _fm)
        # 書類一覧は同じ財務応答から作る（追加のAPI呼び出しなし）
        doc_rows = [
            r for r in (
                map_edinetdb_doc_index(item, company, edinet_code=code,
                                       company_code=company_code, now=now)
                for item in financials if isinstance(item, dict)
            ) if r
        ]
        item_rows.extend(_items_from(financials, code, company_code, fin_rows))
        # 書類一覧の期末日も、同じ書類IDの財務行から埋める
        _fye_by_doc = {r.get("doc_id"): r.get("fiscal_year_end") for r in fin_rows}
        for _d in doc_rows:
            if not _d.get("fiscal_year_end"):
                _d["fiscal_year_end"] = _fye_by_doc.get(_d.get("doc_id"))
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
                if _stop_ingest(client):
                    break
                _record_failure(code, company_code, "text_blocks")
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
            # 成功したら失敗カウンタを消す（バックオフを解除する）
            _prev = meta.get(code) or {}
            meta[code] = {
                "last_fetched": today_str,
                "fetch_count": fetch_count + 1, "company_code": company_code,
                "held_fy": latest_year,
                # 決算月とWeb確認の結果は残す（次の取り直し時期の判定に使う）
                **{k: _prev[k] for k in ("fy_month", "web_check") if k in _prev},
            }
            success += 1
            ingested.append({
                "edinet_code": code, "sec_code": company_code,
                "name": company.get("name") or company.get("name_ja") or "",
                "fiscal_year": latest_year, "financial_rows": len(fin_rows),
                "text_rows": len(text_rows),
            })
            # 書類一覧の MERGE 失敗は財務取込の成否に影響させない
            if doc_rows:
                try:
                    bq.merge_rows("doc_index", doc_rows, ["doc_id"])
                    doc_index_rows += len(doc_rows)
                except Exception as _doc_exc:
                    print(f"    ⚠️  [edinetdb] doc_index MERGE失敗 [{code}]: "
                          f"{_brief_error(_doc_exc)}")
        except Exception as exc:
            # 2026-09-10: ここは例外を完全に握り潰しており、BQ側の理由
            # （スキーマ不一致・型不正など）が一切ログに出なかった。
            # 2026-09-11: 先頭120文字だと google-cloud-bigquery が差し込む
            # `400 GET https://bigquery.googleapis.com/.../queries/<uuid>?...`
            # だけで埋まり、肝心の理由が毎回切り落とされていた
            # （実際 PARSE_JSON の round-trip エラーを1日見落とした）。
            # URLを畳んでから出す。
            print(f"    ⚠️  [edinetdb] BQ MERGE失敗 [{code}]: "
                  f"{type(exc).__name__}: {_brief_error(exc)}")
            reasons["bq_merge"] += 1
            failure += 1
            _note_attempt(code, company_code, missing=False)
    # 2026-10-03: 取得済みの会社は細目（financial_items）が未格納。残り枠で
    # /financials だけ（1社1回）取り直して埋める。最近取った会社から順に。
    backfilled = 0
    if not _stop_ingest(client) and getattr(cfg, "EDINETDB_ITEMS_BACKFILL", True):
        pending = sorted(
            (c for c, info in meta.items()
             if isinstance(info, dict) and info.get("last_fetched")
             and not info.get("items_v1") and c not in items_done and c in by_edinet),
            key=lambda c: str(meta[c].get("last_fetched")), reverse=True)
        for code in pending:
            if _stop_ingest(client) or (
                    getattr(client, "daily_budget", daily_budget) - client.calls - reserve_calls < 1):
                break
            company = by_edinet.get(code, {})
            company_code = _sec4(company.get("sec_code"))
            financials = client.get(f"/companies/{code}/financials", {"years": 5, "period": "annual"})
            if not isinstance(financials, list):
                continue
            fin_rows = [map_edinetdb_financials(item, edinet_code=code, company_code=company_code, now=now)
                        for item in financials if isinstance(item, dict)]
            _fm = (meta.get(code) or {}).get("fy_month")
            for _r in fin_rows:
                if not _r.get("fiscal_year_end") and _fm:
                    _r["fiscal_year_end"] = fiscal_year_end_date(_r.get("fiscal_year"), _fm)
            try:
                bq.merge_rows("financials", fin_rows, ["edinet_code", "fiscal_year"])
            except Exception as exc:
                print(f"    ⚠️  [edinetdb] 細目補完の financials MERGE失敗 [{code}]: {_brief_error(exc)}")
                continue
            item_rows.extend(_items_from(financials, code, company_code, fin_rows))
            backfilled += 1

    items_merged = 0
    if item_rows:
        chunk = int(getattr(cfg, "EDINETDB_ITEMS_MERGE_CHUNK", 4000))
        for i in range(0, len(item_rows), chunk):
            part = item_rows[i:i + chunk]
            try:
                bq.merge_rows("financial_items", part, ["item_id"])
                items_merged += len(part)
            except Exception as exc:
                print(f"    ⚠️  [edinetdb] financial_items MERGE失敗: {type(exc).__name__}: "
                      f"{_brief_error(exc)}")
                items_done.difference_update({r["edinet_code"] for r in part})
        for code in items_done:
            if code in meta and isinstance(meta[code], dict):
                meta[code]["items_v1"] = today_str
        print(f"    🧾 [edinetdb] 財務細目: {items_merged}行（{len(items_done)}社・うち補完{backfilled}社）")
    _save_json(meta_path, meta)
    if failure or missing:
        print(f"    ℹ️  [edinetdb] 失敗{failure}件 / データ無し{missing}件の内訳: "
              f"EdinetDBにデータ無し={reasons['missing']} / "
              f"financials欠落={reasons['financials']} / "
              f"segments欠落={reasons['segments']} / "
              f"text-blocks欠落={reasons['text_blocks']} / "
              f"BQ MERGE={reasons['bq_merge']}")
    return {
        "skipped": False, "success": success, "failure": failure,
        # EdinetDBにデータが無いだけのものは障害ではないので別建てにする
        "missing": missing,
        "doc_index_rows": doc_index_rows,
        "ingested": ingested,
        "missing_companies": missing_list,
        "web_skipped": [
            {"edinet_code": c, "sec_code": _sec4(by_edinet.get(c, {}).get("sec_code")),
             "name": by_edinet.get(c, {}).get("name") or by_edinet.get(c, {}).get("name_ja") or ""}
            for c in web_skipped],
        "reasons": reasons,
        "targets": targets, "budget": daily_budget, "api_calls": client.calls,
        "items_rows": items_merged, "items_companies": len(items_done),
        "items_backfilled": backfilled,
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
