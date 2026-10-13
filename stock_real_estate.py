# ==============================================================
# stock_real_estate.py
# 全上場の日本株について、銘柄単位の不動産ボーナス（−2〜+3点）と業種コードを出す
# ==============================================================
# 【正本】
#   修正PBR・資産バリュー倍率・含み益は BI マート bi.m_screener（m_re_holdings_pref 経由）の値をそのまま使う。
#   スクリーナー表示とスコアで同じ会社の値が食い違わないようにするため、Python 側で計算し直さない。
#   エリアリスク（簿価で重みづけした浸水リスク割合＋2050年までの人口減少率）は、保有不動産の最新年度から
#   同じクエリの中で出す（定義は real_estate_signal.summarize_holdings と同じ）。
#   点数への換算は real_estate_signal.stock_bonus（純関数）。
#   bi.m_screener は日次処理の最後（bi_export）で作り直すため、日次処理の中で読む値は前日分になる。
# 【業種コード】
#   edinet.company.industry（東証33業種の名前）を config.JAPAN_SECTORS のコード（1001〜1033）へ直す。
#   EdinetDB の銘柄マスタは新規上場（英字入りコード等）の業種名が空のことがあるため、JPX の上場銘柄一覧
#   （data_j.xlsx・33業種区分つき・無料公開）で補う。一覧は UNIVERSE_JPX_REFRESH_DAYS ごとに取り直す。
#   ファイルの URL と形式（xls→xlsx）は JPX 側で変わるため、掲載ページ（JPX_PAGE）からリンクをたどる。
#   BQ にも edinet.sector_master（33行）・edinet.sector_jpx（JPX一覧の 証券コード→33業種名）を load job で置き換え、
#   銘柄マスタに業種コードを付けたビュー edinet.company_master を用意する（DB 側の読み手が全銘柄の業種コードを引けるように）。
# 【費用】
#   読み取りは1実行1クエリ（プロセス内でキャッシュ）。sector_master は load job（無料）、ビューは無い時だけ作る。
# 【出力】
#   results["stock_real_estate"]、stock_data/data/stock_real_estate.json（board・検証記録が読む）。
# ==============================================================
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path

_LOCK = threading.Lock()
_CACHE: dict | None = None

_SQL = """
WITH h AS (
  SELECT edinet_code,
    COALESCE(book_value_land_m_yen, 0) + COALESCE(book_value_buildings_m_yen, 0) AS bk,
    GREATEST(0, IF(ABS(IFNULL(flood_risk_area_pct, 0)) > 1, flood_risk_area_pct / 100, IFNULL(flood_risk_area_pct, 0))) AS flood,
    GREATEST(0, -IFNULL(population_change_pct, 0)) / 100 AS decline
  FROM `{project}.real_estate.corporate_holdings_p1`
  QUALIFY fiscal_year = MAX(fiscal_year) OVER (PARTITION BY edinet_code)
),
area AS (
  SELECT edinet_code, LEAST(2, SAFE_DIVIDE(SUM(bk * (flood + decline)), SUM(bk))) AS area_risk
  FROM h WHERE bk > 0 GROUP BY edinet_code
)
SELECT s.ticker, s.sec_code4, s.name, s.industry, s.market_cap, s.pbr, s.adj_pbr, s.asset_value_ratio,
       s.re_unrealized_gain, s.re_gain_to_mcap, s.re_gain_flag, s.re_fiscal_year, a.area_risk
FROM `{project}.bi.m_screener` AS s
LEFT JOIN area AS a USING (edinet_code)
WHERE s.re_unrealized_gain IS NOT NULL OR a.area_risk IS NOT NULL OR s.re_gain_flag IS NOT NULL
"""

_VIEW_SQL = """
CREATE OR REPLACE VIEW `{project}.edinet.company_master`
OPTIONS (description = '銘柄マスタ（edinet.company）に東証33業種コードを付けたビュー。業種名は EdinetDB → JPX上場銘柄一覧の順。「、」と「・」の表記ゆれを吸収する')
AS
SELECT c.*, m.sector_code, m.sector_name, m.sector_group,
       IF(c.industry IS NULL AND j.sector_name IS NOT NULL, 'jpx', IF(c.industry IS NULL, NULL, 'edinetdb')) AS sector_source
FROM `{project}.edinet.company` AS c
LEFT JOIN `{project}.edinet.sector_jpx` AS j
  ON j.code4 = SUBSTR(c.sec_code, 1, 4)
LEFT JOIN `{project}.edinet.sector_master` AS m
  ON REPLACE(COALESCE(c.industry, j.sector_name), '、', '・') = m.sector_name
"""
_VIEW_MARK = "sector_jpx"
JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_FALLBACK_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
UNIVERSE_JPX_REFRESH_DAYS = 7


def _norm_name(name) -> str:
    return str(name or "").strip().replace("、", "・")


def sector_code_map(cfg) -> dict:
    """{業種名: 業種コード}。業種名の「、」と「・」の表記ゆれは「・」に揃える（純関数）。"""
    return {_norm_name(v.get("name_ja")): code
            for code, v in (getattr(cfg, "JAPAN_SECTORS", {}) or {}).items()}


def sector_code_of(industry, cfg) -> str | None:
    return sector_code_map(cfg).get(_norm_name(industry))


def parse_jpx_listing(df) -> dict:
    """JPX 上場銘柄一覧（data_j.xls を読んだ DataFrame）→ {証券コード4文字: 33業種区分}（純関数）。

    ETF・REIT 等は 33業種区分が「-」なので入れない。
    """
    out = {}
    if df is None or "コード" not in getattr(df, "columns", []) or "33業種区分" not in df.columns:
        return out
    for code, name in zip(df["コード"], df["33業種区分"]):
        c = str(code).strip().upper()
        if c.endswith(".0"):
            c = c[:-2]
        n = _norm_name(name)
        if len(c) == 4 and n and n not in ("-", "nan"):
            out[c] = n
    return out


def load_jpx_sectors(cfg) -> dict:
    """{証券コード4文字: 33業種名}。UNIVERSE_JPX_REFRESH_DAYS 日以内のキャッシュがあればそれを使う。"""
    path = Path(getattr(cfg, "DRIVE_BASE", ".")) / "data" / "jpx_sectors.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        age = (date.today() - date.fromisoformat(saved.get("date", "1970-01-01"))).days
        if age < UNIVERSE_JPX_REFRESH_DAYS and saved.get("sectors"):
            return saved["sectors"]
    except Exception:
        saved = {}
    try:
        import io
        import pandas as pd
        import requests
        import re
        headers = {"User-Agent": "Mozilla/5.0"}
        url = JPX_FALLBACK_URL
        try:
            page = requests.get(JPX_PAGE, timeout=30, headers=headers).text
            m = re.search(r'href="([^"]*data_j\.xlsx?)"', page)
            if m:
                url = requests.compat.urljoin(JPX_PAGE, m.group(1))
        except Exception:
            pass
        resp = requests.get(url, timeout=60, headers=headers)
        resp.raise_for_status()
        engine = "xlrd" if url.endswith(".xls") else "openpyxl"
        sectors = parse_jpx_listing(pd.read_excel(io.BytesIO(resp.content), engine=engine))
        if len(sectors) > 3000:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"date": date.today().isoformat(), "sectors": sectors},
                                       ensure_ascii=False), encoding="utf-8")
            return sectors
        print(f"    ⚠️  JPX上場銘柄一覧が{len(sectors)}件しかない（前回分を使う）")
    except Exception as e:
        print(f"    ⚠️  JPX上場銘柄一覧の取得失敗（前回分を使う）: {str(e)[:80]}")
    return (saved or {}).get("sectors") or {}


def build_by_ticker(rows, cfg, jpx: dict | None = None) -> dict:
    """クエリの行 → {ticker: {...}}（純関数）。業種名が空なら JPX 一覧の業種で補う。"""
    from real_estate_signal import stock_bonus
    names = sector_code_map(cfg)
    jpx = jpx or {}
    out = {}
    for r in rows or []:
        r = dict(r)
        ticker = r.get("ticker") or (f"{r['sec_code4']}.T" if r.get("sec_code4") else None)
        if not ticker:
            continue
        bonus, basis = stock_bonus(r.get("adj_pbr"), r.get("asset_value_ratio"), r.get("re_unrealized_gain"),
                                   r.get("re_gain_flag"), r.get("area_risk"), cfg)
        out[ticker] = {
            "bonus": bonus, "basis": basis,
            "adj_pbr": r.get("adj_pbr"), "asset_value_ratio": r.get("asset_value_ratio"),
            "gain_to_mcap": r.get("re_gain_to_mcap"), "flag": r.get("re_gain_flag"),
            "area_risk": r.get("area_risk"), "re_fiscal_year": r.get("re_fiscal_year"),
            "name": r.get("name"), "industry": r.get("industry") or jpx.get(str(r.get("sec_code4") or "")),
            "sector_code": names.get(_norm_name(r.get("industry") or jpx.get(str(r.get("sec_code4") or "")))),
        }
    return out


def _client():
    from edinet_bq_client import EDINETBQClient
    return EDINETBQClient().client


def load(cfg, client=None) -> dict:
    """{ticker: {...}}。1プロセス1クエリ。失敗時は空（呼び出し側は加点なしで続ける）。"""
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            client = client or _client()
            rows = client.query(_SQL.format(project=client.project)).result()
            _CACHE = build_by_ticker(rows, cfg, load_jpx_sectors(cfg))
            print(f"    🏢 銘柄単位の不動産ボーナス: {len(_CACHE)}銘柄（bi.m_screener・1クエリ）")
        except Exception as e:
            print(f"    ⚠️  銘柄単位の不動産ボーナス取得失敗（加点なしで続行）: {str(e)[:100]}")
            _CACHE = {}
        return _CACHE


def ensure_sector_master(cfg, client=None) -> int:
    """edinet.sector_master（config.JAPAN_SECTORS）と edinet.sector_jpx（JPX一覧）を置き換え、
    company_master ビューが無いか古い定義なら作り直す。"""
    from google.cloud import bigquery
    client = client or _client()
    rows = [{"sector_code": code, "sector_name": _norm_name(v.get("name_ja")),
             "sector_name_en": v.get("name_en"), "sector_group": v.get("group")}
            for code, v in sorted((getattr(cfg, "JAPAN_SECTORS", {}) or {}).items())]
    schema = [bigquery.SchemaField(n, "STRING", mode="REQUIRED" if n == "sector_code" else "NULLABLE")
              for n in ("sector_code", "sector_name", "sector_name_en", "sector_group")]
    job = client.load_table_from_json(
        rows, f"{client.project}.edinet.sector_master",
        job_config=bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE"))
    job.result()
    jpx_rows = [{"code4": c, "sector_name": n} for c, n in sorted(load_jpx_sectors(cfg).items())]
    jpx_schema = [bigquery.SchemaField("code4", "STRING", mode="REQUIRED"),
                  bigquery.SchemaField("sector_name", "STRING")]
    client.create_table(bigquery.Table(f"{client.project}.edinet.sector_jpx", schema=jpx_schema), exists_ok=True)
    if jpx_rows:
        client.load_table_from_json(
            jpx_rows, f"{client.project}.edinet.sector_jpx",
            job_config=bigquery.LoadJobConfig(schema=jpx_schema, write_disposition="WRITE_TRUNCATE")).result()
    try:
        current = client.get_table(f"{client.project}.edinet.company_master").view_query or ""
    except Exception:
        current = ""
    if _VIEW_MARK not in current:
        client.query(_VIEW_SQL.format(project=client.project)).result()
        print("    🗂 edinet.company_master ビューを作成・更新")
    return len(rows)


def summarize(by_ticker: dict) -> dict:
    """件数・basis 別件数・bonus の最小／中央／最大（純関数。検証記録と通知に使う）。"""
    vals = sorted(v["bonus"] for v in by_ticker.values())
    basis: dict = {}
    for v in by_ticker.values():
        basis[v["basis"]] = basis.get(v["basis"], 0) + 1
    mid = vals[len(vals) // 2] if vals else None
    return {"count": len(vals), "basis_counts": basis,
            "bonus_min": vals[0] if vals else None, "bonus_median": mid, "bonus_max": vals[-1] if vals else None,
            "nonzero": sum(1 for x in vals if x)}


def run(cfg, results: dict, out_dir: Path | None = None) -> dict:
    """日次: 業種マスタを整え、銘柄単位の不動産ボーナスを results と JSON に置く。"""
    info: dict = {"date": date.today().isoformat()}
    try:
        info["sector_master_rows"] = ensure_sector_master(cfg)
    except Exception as e:
        info["sector_master_error"] = str(e)[:150]
        print(f"    ⚠️  業種マスタの更新失敗: {str(e)[:100]}")
    by_ticker = load(cfg)
    info.update(summarize(by_ticker))
    info["by_ticker"] = by_ticker
    out_dir = out_dir or Path(getattr(cfg, "DRIVE_BASE", ".")) / "data"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "stock_real_estate.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  stock_real_estate.json 書き込み失敗: {str(e)[:80]}")
    results["stock_real_estate"] = info
    return info


if __name__ == "__main__":
    import sys
    import config
    _info = run(config, {})
    print(json.dumps({k: v for k, v in _info.items() if k != "by_ticker"}, ensure_ascii=False, default=str))
    sys.exit(0 if _info.get("count") and "sector_master_error" not in _info else 1)
