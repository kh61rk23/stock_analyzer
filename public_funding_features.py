# ==============================================================
# public_funding_features.py — 公的支出・経済安保の銘柄特徴量
#
# seisakudb_ingestor が BQ に取り込んだ edinet.public_funding / public_funding_rank /
# economic_security_certs と edinet.financials を、銘柄ごとの特徴量にする。
#   build_features        BQ を1本のクエリで一括取得して特徴量化（ループ内で BQ を引かない）
#   public_funding_bonus  特徴量 → スコア加点（純関数。反映するかは呼び出し側が決める）
#   sector_public_funding セクター別の直近1年の公的支出合計（表示用の純関数）
# 結果は stock_data/data/public_funding_features.json に保存し、BQ 失敗時は前回分を返す。
# ==============================================================
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

_FEATURES_FILE = "public_funding_features.json"
_CHUNK = 2000


def empty_feat() -> dict:
    return {"total_yen": 0.0, "subsidy_yen": 0.0, "procurement_yen": 0.0, "recent_1y_yen": 0.0,
            "n_events": 0, "last_award": None, "rank_total_oku": None, "ratio_to_sales": None,
            "econ_security_materials": []}


def ticker_to_code(ticker: str) -> str | None:
    """'6758.T' / '67580' / '624A' → 4桁の証券コード。日本株の形でなければ None。"""
    head = str(ticker or "").split(".")[0].strip().upper()
    if re.fullmatch(r"\d{3}[0-9A-Z]0?", head):
        return head[:4]
    return None


def _data_dir(cfg) -> Path:
    base = getattr(cfg, "DATA_DIR", None)
    return Path(base) if base else Path("stock_data/data")


def _num(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f


def _date_str(v) -> str | None:
    if v is None:
        return None
    s = str(v)[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def _row_to_feat(row: dict) -> dict:
    """BQ の1行 → feat。売上が無い・0以下なら ratio_to_sales は None。"""
    feat = empty_feat()
    recent = _num(row.get("recent_1y_yen"))
    feat.update({
        "total_yen": _num(row.get("total_yen")),
        "subsidy_yen": _num(row.get("subsidy_yen")),
        "procurement_yen": _num(row.get("procurement_yen")),
        "recent_1y_yen": recent,
        "n_events": int(_num(row.get("n_events"))),
        "last_award": _date_str(row.get("last_award")),
        "econ_security_materials": sorted({str(m) for m in (row.get("materials") or []) if m}),
    })
    if row.get("rank_total_oku") is not None:
        feat["rank_total_oku"] = _num(row.get("rank_total_oku"), None)
    sales = _num(row.get("net_sales"), None)
    if sales and sales > 0:
        feat["ratio_to_sales"] = recent / sales
    return feat


def _sql(dataset: str) -> str:
    return f"""
    WITH req AS (SELECT code FROM UNNEST(@codes) AS code),
    pf AS (
      SELECT company_code,
        SUM(amount_yen) AS total_yen,
        SUM(IF(LOWER(IFNULL(kind, '')) LIKE '%subsid%', amount_yen, 0)) AS subsidy_yen,
        SUM(IF(LOWER(IFNULL(kind, '')) LIKE '%procure%', amount_yen, 0)) AS procurement_yen,
        SUM(IF(event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 365 DAY), amount_yen, 0)) AS recent_1y_yen,
        COUNT(*) AS n_events,
        MAX(event_date) AS last_award
      FROM `{dataset}.public_funding`
      WHERE endpoint = 'company_public_funding' AND company_code IN (SELECT code FROM req)
      GROUP BY company_code
    ),
    rk AS (
      SELECT company_code, ANY_VALUE(total_oku) AS rank_total_oku
      FROM `{dataset}.public_funding_rank`
      WHERE fetched_date = (SELECT MAX(fetched_date) FROM `{dataset}.public_funding_rank`)
        AND company_code IN (SELECT code FROM req)
      GROUP BY company_code
    ),
    es AS (
      SELECT company_code, ARRAY_AGG(DISTINCT kind IGNORE NULLS) AS materials
      FROM `{dataset}.economic_security_certs`
      WHERE company_code IN (SELECT code FROM req)
      GROUP BY company_code
    ),
    fin AS (
      SELECT company_code, net_sales FROM (
        SELECT company_code, net_sales,
          ROW_NUMBER() OVER (
            PARTITION BY company_code
            ORDER BY COALESCE(fiscal_year, EXTRACT(YEAR FROM fiscal_year_end)) DESC, updated_at DESC
          ) AS rn
        FROM `{dataset}.financials`
        WHERE company_code IN (SELECT code FROM req) AND net_sales IS NOT NULL
      ) WHERE rn = 1
    )
    SELECT r.code AS company_code, pf.total_yen, pf.subsidy_yen, pf.procurement_yen, pf.recent_1y_yen,
           pf.n_events, CAST(pf.last_award AS STRING) AS last_award, rk.rank_total_oku,
           es.materials, fin.net_sales
    FROM req r
    LEFT JOIN pf ON pf.company_code = r.code
    LEFT JOIN rk ON rk.company_code = r.code
    LEFT JOIN es ON es.company_code = r.code
    LEFT JOIN fin ON fin.company_code = r.code
    """


def fetch_rows_from_bq(codes: list[str]) -> list[dict]:
    """4桁コードの一覧 → 会社ごとの集計行（BQ 1本）。失敗は例外のまま上へ。"""
    from google.cloud import bigquery
    from edinet_bq_client import EDINETBQClient
    bq = EDINETBQClient()
    cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("codes", "STRING", codes)])
    df = bq.client.query(_sql(bq.dataset), job_config=cfg).to_dataframe(create_bqstorage_client=False)
    rows = df.to_dict("records")
    for r in rows:
        m = r.get("materials")
        r["materials"] = list(m) if m is not None and not isinstance(m, float) else []
    return rows


def _load_previous(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        f = d.get("features")
        return f if isinstance(f, dict) else {}
    except (OSError, ValueError):
        return {}


def build_features(cfg, tickers: list[str], fetcher=None, today: date | None = None) -> dict:
    """ticker → feat。該当なしの銘柄も空の feat で埋める。

    fetcher(codes) -> list[dict] を差し替えるとBQ無しで動く（テスト用）。
    BQ が失敗したら前回の JSON を返す（無い銘柄は空の feat）。
    """
    today = today or date.today()
    path = _data_dir(cfg) / _FEATURES_FILE
    code_of = {t: ticker_to_code(t) for t in tickers}
    codes = sorted({c for c in code_of.values() if c})
    by_code: dict[str, dict] = {}
    ok = True
    try:
        fetch = fetcher or fetch_rows_from_bq
        for i in range(0, len(codes), _CHUNK):
            for r in fetch(codes[i:i + _CHUNK]):
                code = str(r.get("company_code") or "")
                if code:
                    by_code[code] = _row_to_feat(r)
    except Exception as exc:
        ok = False
        print(f"    ⚠️ [public_funding] BQ 取得失敗、前回分を使用: {type(exc).__name__}: {str(exc)[:80]}")
    if not ok:
        prev = _load_previous(path)
        return {t: prev.get(t) or empty_feat() for t in tickers}
    feats = {t: (by_code.get(c) if c else None) or empty_feat() for t, c in code_of.items()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"date": today.isoformat(), "features": feats}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass
    return feats


def public_funding_bonus(feat: dict | None, cfg=None, today: date | None = None) -> tuple[float, list[str]]:
    """特徴量 → (加点, 理由)。純関数。cfg が None でも既定値で動く。"""
    feat = feat or {}
    today = today or date.today()
    g = lambda name, default: getattr(cfg, name, default)
    pt, reasons = 0.0, []
    mats = feat.get("econ_security_materials") or []
    if mats:
        pt += g("SEISAKU_BONUS_ECON_SECURITY", 1.5)
        reasons.append("経済安保の認定あり（" + "・".join(mats[:3]) + "）")
    ratio = feat.get("ratio_to_sales")
    lo, hi = g("SEISAKU_RATIO_MIN", 0.01), g("SEISAKU_RATIO_MAX", 0.30)
    if ratio is not None and lo <= ratio <= hi:
        pt += g("SEISAKU_BONUS_GOV_DEMAND", 1.0)
        reasons.append(f"政府需要が売上の{ratio * 100:.1f}%")
    last = _date_str(feat.get("last_award"))
    if last and _num(feat.get("recent_1y_yen")) > 0:
        try:
            age = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
        except ValueError:
            age = None
        if age is not None and age <= 180:
            pt += g("SEISAKU_BONUS_RECENT_AWARD", 0.5)
            reasons.append("直近半年に公的支出あり")
    return min(pt, g("SCORE_MAX_PUBLIC_FUNDING_ADJ", 3.0)), reasons


def sector_public_funding(features: dict, ticker_to_sector: dict) -> dict:
    """セクターコード → 直近1年の公的支出（円）の合計。純関数。"""
    out: dict = {}
    for t, feat in (features or {}).items():
        sec = (ticker_to_sector or {}).get(t)
        if sec is None:
            continue
        out[sec] = out.get(sec, 0.0) + _num((feat or {}).get("recent_1y_yen"))
    return out
