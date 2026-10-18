"""
bi_export.py — BI基盤のマートを BigQuery から書き出す
=====================================================
【役割】bi/marts/*.sql を BigQuery で1マート1クエリで実行して `stock-analyzer-edinet.bi.<mart>` の表に実体化し、
        その表から stock_data/bi/<mart>.parquet（pyarrow・snappy）と stock_data/bi/manifest.json を書く。
        Claudeダッシュボードは元表ではなく実体化した表を読むので、1回の読み取りが小さく済む（費用を抑える）。
        bi/templates/*.json は検査のうえ stock_data/bi/templates/ へコピーする（board がローカルで読む）。
【実行】python3 bi_export.py                 … 全マート＋テンプレートのコピー
        python3 bi_export.py --only m_company  … 1マートだけ
        python3 bi_export.py --dry-run         … 構文とスキャン量の確認だけ（ファイルは書かない）
        python3 bi_export.py --templates-only  … テンプレートのコピーだけ（BQ不要）
【認証】edinet_bq_client.EDINETBQClient（GitHub Actions は BQ_CREDENTIALS、ローカルは BQ_CREDENTIALS_PATH）
【終了コード】どれか1つでも失敗したら 1。書き出しは「全部成功か、全部なし」：失敗したマートが1つでもあれば、
        本番表 bi.<mart>・parquet・manifest.json・templates のどれも変えない（ログに失敗したマート名を出す）。

内容が前回と同じマートは parquet を書き換えない。parquet は同じ内容でも書き出しのたびにバイト列が
変わりうるため、行を主キーで並べ替えた Arrow 表のハッシュ（content_sha）で同一性を判定する。
これを怠ると毎日 parquet がコミットされて git が肥大する。

実行は4段階（1回の bi_export で全マートを一斉に進める）:
  0. テンプレートを検査（NGなら何もせず失敗）。location が違う元表（US の ontology.signal_event・us.company_profile・us.financials_annual）は
     `list_rows`（無料）→ load job（無料）で asia-northeast1 の `bi.src_*` へ写す
  1. 全マートについて `CREATE OR REPLACE TABLE bi.<mart>__next AS <マートSQL>` … 元表を読むのはここだけ
  2. 全マートの行数検査（0行・前回の半分未満は失敗）と `list_rows` での中身の読み出し（tabledata.list は課金されない）
  3. 1・2 が全マートで通った時だけ、コピージョブ（無料）で全マートの `bi.<mart>` を続けて差し替える
  4. parquet・manifest.json・templates を書き出す（一時ファイルにそろえてから置き換える）
ステージ表は成否にかかわらず最後に消す。権限不足などで実体化できないときは握り潰さず失敗（終了コード1）にする。
`bi.src_*` は元表の写しで、ダッシュボードも board も読まない（マートSQLの入力）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import bi_templates

REPO = Path(__file__).resolve().parent
MART_DIR = REPO / "bi" / "marts"
TEMPLATE_DIR = REPO / "bi" / "templates"
BASE = Path(os.getenv("STOCK_ANALYZER_BASE", "").strip() or (REPO / "stock_data"))
OUT_DIR = BASE / "bi"
MANIFEST = OUT_DIR / "manifest.json"

# 並べ替えの主キー（行順が毎回変わっても同一内容と判定するため）。未登録のマートは全列で並べる。
MART_KEYS = {
    "m_company": ["edinet_code"],
    "m_fin_annual": ["edinet_code", "fiscal_year"],
    "m_country_profile": ["country_code"],
    "m_country_risk": ["country_code", "risk_date"],
    "m_company_country": ["ticker", "country_code"],
    "m_re_area": ["municipality_code"],
    "m_re_holdings": ["edinet_code", "fiscal_year", "municipality_code"],
    "m_re_holdings_pref": ["edinet_code", "fiscal_year", "prefecture"],
    "m_tax_node": ["node_id"],
    "m_company_tax": ["company_id", "node_id"],
    "m_theme": ["theme_name", "ticker"],
    "m_theme_def": ["theme_name", "sector_code", "market"],
    "m_theme_signal": ["theme_name", "sector_code", "fired_at", "signal_category", "direction"],
    "m_screener": ["edinet_code"],
    "m_screener_us": ["ticker"],
    "m_us_fin_annual": ["ticker", "fiscal_year_end"],
    "m_us_segments": ["cik", "axis", "member", "fiscal_year_end"],
    "m_stock_snapshot": ["snapshot_date", "ticker"],
    "m_arxiv_papers": ["paper_id"],
    "m_arxiv_keywords": ["paper_id", "keyword"],
    "m_sector_scores": ["report_date", "market", "sector_code"],
    "m_macro_indicators": ["report_date", "source", "indicator"],
    "m_macro_signals": ["report_date", "signal", "sector_name"],
    "m_reports": ["report_date", "report_type", "report_key"],
    "m_news": ["url"],
    "m_etf_themes": ["report_date", "theme_name"],
    "m_etf_daily": ["report_date", "theme_name", "ticker"],
    "m_imf_forecasts": ["report_date", "indicator_code", "country", "year"],
    "m_gov_stats": ["report_date", "stat_name"],
    "m_screener_trace": ["report_date", "market", "ticker"],
    "m_price_monthly": ["ticker", "month"],
    "m_sector_perf": ["industry", "trade_date"],
    "m_market_breadth": ["market", "trade_date"],
}
# 前回の行数のこの割合を下回ったら、壊れた結果とみなして上書きしない
MIN_ROW_RATIO = 0.5

PROJECT = "stock-analyzer-edinet"
DATASET = "bi"
LOCATION = "asia-northeast1"  # マートの元表（edinet・geopolitics・real_estate）と同じ。違うと JOIN できない
STAGE_SUFFIX = "__next"
# location が asia-northeast1 と違う元表。クエリを使わず list_rows（無料）→ load job（無料）で bi.<dest> へ写す。
# 同じクエリで JOIN できない US の表を、費用を増やさずマートSQLから読めるようにするため。
SOURCES = [
    {
        "source": "stock-analyzer-edinet.ontology.signal_event",
        "dest": "src_signal_event",
        "columns": ["event_id", "fired_at", "ticker", "sector_code", "signal_category", "signal_value", "direction", "source_module"],
    },
    {
        "source": "stock-analyzer-edinet.us.company_profile",
        "dest": "src_us_company_profile",
        "columns": ["ticker", "cik", "name", "exchange", "sic", "sic_description", "shares_outstanding", "shares_as_of", "shares_source",
                    "bs_period_end", "bs_total_assets", "bs_total_liabilities", "bs_equity", "bs_cash", "bs_debt_short", "bs_debt_long",
                    "ttm_period_end", "ttm_revenue", "ttm_operating_income", "ttm_net_income", "ttm_eps_diluted", "ttm_operating_cf", "ttm_capex"],
    },
    {
        "source": "stock-analyzer-edinet.us.financials_annual",
        "dest": "src_us_financials_annual",
        "columns": ["ticker", "cik", "period_end", "revenue", "operating_income", "net_income", "eps_diluted",
                    "operating_cf", "capex", "buyback", "dividends_paid"],
        "optional_columns": ["total_assets", "total_liabilities", "equity", "cash", "debt_short", "debt_long",
                             "gross_profit", "depreciation", "income_tax", "pretax_income", "interest_expense",
                             "shares_diluted", "assets_current", "liabilities_current"],
    },
    {
        "source": "stock-analyzer-edinet.us.segment_financials",
        "dest": "src_us_segments",
        "columns": ["cik", "axis", "member", "tag", "period_end", "qtrs", "val", "filed"],
    },
]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def friendly_type(t: pa.DataType) -> str:
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "string"
    if pa.types.is_floating(t):
        return "double"
    if pa.types.is_integer(t):
        return "int64"
    if pa.types.is_boolean(t):
        return "bool"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_timestamp(t):
        return "timestamp"
    return str(t)


def canonical_table(table: pa.Table, mart: str) -> pa.Table:
    keys = MART_KEYS.get(mart) or table.column_names
    return table.sort_by([(k, "ascending") for k in keys])


def content_sha(table: pa.Table) -> str:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()


def load_manifest() -> dict:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("marts"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "marts": []}


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def mart_names() -> list[str]:
    return sorted(p.stem for p in MART_DIR.glob("*.sql"))


def table_ref(name: str) -> str:
    return f"{PROJECT}.{DATASET}.{name}"


def dry_run_bytes(client, sql: str) -> int:
    """構文とスキャン量(bytes)の確認だけ。表は作らない。"""
    from google.cloud import bigquery

    job = client.query(sql, location=LOCATION,
                       job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
    return job.total_bytes_processed


def ensure_dataset(client) -> None:
    """データセット bi を asia-northeast1 に用意する。無い・作れない・場所が違うときは例外（握り潰さない）。"""
    from google.cloud import bigquery

    ds = bigquery.Dataset(f"{PROJECT}.{DATASET}")
    ds.location = LOCATION
    try:
        got = client.create_dataset(ds, exists_ok=True)
    except Exception as e:  # noqa: BLE001 権限不足を明示して呼び出し側へ
        raise RuntimeError(
            f"データセット {PROJECT}.{DATASET} を作れない/参照できない（bigquery.datasets.create・"
            f"tables.create の権限を確認）: {type(e).__name__}: {e}") from e
    if got.location != LOCATION:
        raise RuntimeError(f"データセット {DATASET} の場所が {got.location}（{LOCATION} が必要）")


def stage_mart(client, mart: str, sql: str) -> dict:
    """マートSQLを bi.<mart>__next に実体化する（重いクエリはこの1本だけ）。行数を返す。"""
    stage = table_ref(mart + STAGE_SUFFIX)
    ddl = (f"CREATE OR REPLACE TABLE `{stage}`\n"
           f"OPTIONS (description = 'bi_export.py が日次で作るマートの実体。手で編集しない')\nAS\n{sql}")
    client.query(ddl, location=LOCATION).result()
    return {"stage": stage, "rows": client.get_table(stage).num_rows}


def read_stage(client, stage: str) -> pa.Table:
    """ステージ表の中身を Arrow で読む。list_rows は tabledata.list で、クエリ課金も Storage Read 権限も要らない。"""
    return client.list_rows(stage).to_arrow(create_bqstorage_client=False)


def promote_mart(client, mart: str, stage: str) -> dict:
    """ステージ表を本番表 bi.<mart> へ差し替える（コピージョブは無料）。本番表のサイズを返す。"""
    from google.cloud import bigquery

    final = table_ref(mart)
    cfg = bigquery.CopyJobConfig(write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    client.copy_table(stage, final, location=LOCATION, job_config=cfg).result()
    t = client.get_table(final)
    return {"table": final, "table_bytes": int(t.num_bytes or 0), "table_rows": int(t.num_rows or 0)}


def drop_stage(client, stage: str) -> None:
    try:
        client.delete_table(stage, not_found_ok=True)
    except Exception as e:  # noqa: BLE001 後始末の失敗は本処理の成否を変えない（次回の CREATE OR REPLACE で上書きされる）
        print(f"⚠️ ステージ表 {stage} を消せない: {e}")


def check_rows(mart: str, rows: int, prev: dict | None) -> None:
    if rows == 0:
        raise RuntimeError(f"{mart}: 0行（認証・権限・SQLの不整合を疑う。既存の表と parquet は残す）")
    if prev and prev.get("rows") and rows < prev["rows"] * MIN_ROW_RATIO:
        raise RuntimeError(
            f"{mart}: 行数が前回 {prev['rows']} から {rows} に急減（壊れた結果とみなし上書きしない）")


def prepare_mart(client, mart: str, prev: dict | None) -> dict:
    """1マートをステージ表に実体化して検査し、書き出す内容をメモリ上に用意する。本番表・ファイルは触らない。
    失敗は例外（ステージ表はここで消す）。成功時のステージ表は呼び出し側が最後に消す。"""
    sql = (MART_DIR / f"{mart}.sql").read_text(encoding="utf-8")
    sql_sha = sha256_text(sql)
    staged = stage_mart(client, mart, sql)
    try:
        check_rows(mart, staged["rows"], prev)
        table = canonical_table(read_stage(client, staged["stage"]), mart)
        check_rows(mart, table.num_rows, prev)
        sha = content_sha(table)
        path = OUT_DIR / f"{mart}.parquet"
        unchanged = bool(prev and prev.get("content_sha") == sha and path.exists())
        parquet = None
        if not unchanged:
            buf = pa.BufferOutputStream()
            pq.write_table(table, buf, compression="snappy")
            parquet = buf.getvalue().to_pybytes()
    except BaseException:
        drop_stage(client, staged["stage"])
        raise
    return {
        "mart": mart, "stage": staged["stage"], "table": table, "sha": sha, "sql_sha": sql_sha,
        "path": path, "parquet": parquet, "prev": prev,
        "columns": [{"name": f.name, "type": friendly_type(f.type)} for f in table.schema],
    }


def finalize_entry(plan: dict, info: dict) -> dict:
    """本番表の差し替え結果（info）を足して manifest の項目を作る。"""
    bq = {"table": info["table"], "table_bytes": info["table_bytes"]}
    if plan["parquet"] is None:
        # 内容が同じなら parquet もマニフェスト項目も触らない（generated_at＝内容が変わった日時）
        entry = dict(plan["prev"])
        entry["sql_sha"] = plan["sql_sha"]
        entry.update(bq)
        return entry
    return {
        "mart": plan["mart"],
        "rows": plan["table"].num_rows,
        "columns": plan["columns"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sql_sha": plan["sql_sha"],
        "content_sha": plan["sha"],
        **bq,
    }


def refresh_source(client, src: dict) -> int:
    """location が違う元表を bi.<dest> へ写す（list_rows と load job はどちらも無料）。0行は失敗。"""
    from google.cloud import bigquery

    table = client.list_rows(src["source"]).to_arrow(create_bqstorage_client=False)
    # optional_columns は元表に後から足す列。元表の書き換え前でも写しの列をそろえるため、無ければ空の FLOAT64 列にする
    for col in src.get("optional_columns", []):
        if col not in table.column_names:
            table = table.append_column(pa.field(col, pa.float64()), pa.nulls(table.num_rows, pa.float64()))
    table = table.select(src["columns"] + src.get("optional_columns", []))
    if table.num_rows == 0:
        raise RuntimeError(f"{src['source']}: 0行（元表が空。写しを更新しない）")
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf, compression="snappy")
    cfg = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.PARQUET,
                                 write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    import io
    client.load_table_from_file(io.BytesIO(buf.getvalue().to_pybytes()), table_ref(src["dest"]),
                                job_config=cfg, location=LOCATION).result()
    return table.num_rows


def sources_for(names: list[str]) -> list[dict]:
    """選んだマートのSQLが読む写し（bi.src_*）に対応する元表。"""
    sqls = [(MART_DIR / f"{n}.sql").read_text(encoding="utf-8") for n in names]
    return [s for s in SOURCES if any(f"{DATASET}.{s['dest']}" in q for q in sqls)]


def write_outputs(plans: list[dict], entries: dict[str, dict]) -> bool:
    """parquet と manifest を一時ファイルにそろえてから置き換える。manifest を書いたら True。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pending: list[tuple[Path, Path]] = []
    try:
        for plan in plans:
            if plan["parquet"] is not None:
                tmp = plan["path"].with_name(plan["path"].name + ".tmp")
                tmp.write_bytes(plan["parquet"])
                pending.append((tmp, plan["path"]))
        manifest = {"version": 1, "marts": [entries[k] for k in sorted(entries)]}
        mtmp = MANIFEST.with_name(MANIFEST.name + ".tmp")
        mtmp.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        pending.append((mtmp, MANIFEST))
    except BaseException:
        for tmp, _ in pending:
            tmp.unlink(missing_ok=True)
        raise
    for tmp, dst in pending:
        os.replace(tmp, dst)
    return True


def check_templates() -> list[str]:
    """bi/templates/*.json を検査する。不合格があれば例外。合格したファイル名を返す。"""
    marts = set(mart_names())
    templates = bi_templates.load_templates(TEMPLATE_DIR)
    problems = []
    seen = set()
    for t in templates:
        for p in bi_templates.validate_template(t, marts):
            problems.append(f"{t.get('id', '?')}: {p}")
        if t.get("id") in seen:
            problems.append(f"{t.get('id')}: id が重複")
        seen.add(t.get("id"))
    if problems:
        raise RuntimeError("テンプレート検査NG:\n  " + "\n  ".join(problems))
    return sorted(p.name for p in TEMPLATE_DIR.glob("*.json"))


def sync_templates() -> list[str]:
    """検査済みのテンプレートを stock_data/bi/templates/ へ同期する（消えたJSONは削除）。"""
    dest = OUT_DIR / "templates"
    dest.mkdir(parents=True, exist_ok=True)
    names = set()
    for src in sorted(TEMPLATE_DIR.glob("*.json")):
        names.add(src.name)
        target = dest / src.name
        if not target.exists() or target.read_bytes() != src.read_bytes():
            shutil.copyfile(src, target)
    for stale in dest.glob("*.json"):
        if stale.name not in names:
            stale.unlink()
    return sorted(names)


def copy_templates() -> list[str]:
    check_templates()
    return sync_templates()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--only", action="append", help="このマートだけ（複数指定可）")
    ap.add_argument("--dry-run", action="store_true", help="構文とスキャン量だけ確認し、何も書かない")
    ap.add_argument("--templates-only", action="store_true", help="テンプレートのコピーだけ")
    args = ap.parse_args(argv)

    failures: list[str] = []

    # 0. テンプレート検査（NGなら BigQuery に触れる前に止める。書き出しは最後まで行わない）
    templates_ok = True
    if not args.dry_run:
        try:
            print(f"✅ テンプレートを検査: {', '.join(check_templates())}")
        except Exception as e:  # noqa: BLE001 失敗は握り潰さず終了コードで返す
            print(f"❌ {e}")
            failures.append("templates")
            templates_ok = False
    if args.templates_only:
        if templates_ok:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            print(f"✅ テンプレートを同期: {', '.join(sync_templates())}")
        return 1 if failures else 0
    if not templates_ok:
        print(f"❌ bi_export 失敗: {failures}（本番表・parquet・manifest・templates は変更していない）")
        return 1

    names = mart_names()
    if args.only:
        unknown = set(args.only) - set(names)
        if unknown:
            print(f"❌ 未知のマート: {sorted(unknown)}")
            return 1
        names = [n for n in names if n in args.only]

    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client

    if args.dry_run:
        for mart in names:
            try:
                sql = (MART_DIR / f"{mart}.sql").read_text(encoding="utf-8")
                print(f"🧪 {mart}: dry-run OK  スキャン {dry_run_bytes(client, sql):,} bytes")
            except Exception as e:  # noqa: BLE001
                print(f"❌ {mart}: {type(e).__name__}: {e}")
                failures.append(mart)
        if failures:
            print(f"❌ bi_export 失敗: {failures}")
        return 1 if failures else 0

    try:
        ensure_dataset(client)  # 実体化できないなら parquet も更新しない（表と食い違うため）
        for src in sources_for(names):
            rows = refresh_source(client, src)
            print(f"✅ 元表の写し {src['source']} → {DATASET}.{src['dest']}: {rows:,}行")
    except Exception as e:  # noqa: BLE001
        print(f"❌ {type(e).__name__}: {e}")
        print("❌ bi_export 失敗: 本番表・parquet・manifest・templates は変更していない")
        return 1

    manifest = load_manifest()
    by_name = {e["mart"]: e for e in manifest["marts"]}
    plans: list[dict] = []
    try:
        # 1・2. 全マートのステージ表を作って検査する（本番表・ファイルはまだ触らない）
        for mart in names:
            try:
                plans.append(prepare_mart(client, mart, by_name.get(mart)))
                print(f"🧪 {mart}: ステージ表OK {plans[-1]['table'].num_rows:,}行")
            except Exception as e:  # noqa: BLE001 失敗したマート名をすべて出すため最後まで検査する
                print(f"❌ {mart}: {type(e).__name__}: {e}")
                failures.append(mart)
        if failures:
            print(f"❌ bi_export 失敗マート: {failures}（本番表・parquet・manifest・templates は変更していない）")
            return 1

        # 3. 全部通ったので本番表を続けて差し替える
        entries = dict(by_name)
        promoted: list[str] = []
        for plan in plans:
            try:
                info = promote_mart(client, plan["mart"], plan["stage"])
            except Exception as e:  # noqa: BLE001
                print(f"❌ {plan['mart']}: 本番表の差し替えに失敗 {type(e).__name__}: {e}")
                if promoted:
                    print(f"❌ 差し替え済み（次回の実行で parquet と揃う）: {promoted}")
                print("❌ bi_export 失敗: parquet・manifest・templates は変更していない")
                return 1
            promoted.append(plan["mart"])
            entries[plan["mart"]] = finalize_entry(plan, info)

        # 4. ファイルを書き出す
        write_outputs(plans, entries)
        for plan in plans:
            e = entries[plan["mart"]]
            state = "更新" if plan["parquet"] is not None else "変更なし（parquet据え置き）"
            print(f"✅ {plan['mart']}: {e['rows']:,}行 {len(e['columns'])}列 表 {e['table']} "
                  f"{e['table_bytes']:,}bytes {state}")
        total = sum(e.get("table_bytes", 0) for e in entries.values())
        print(f"✅ manifest.json を更新（{len(entries)}マート・実体化した表の合計 {total:,}bytes）")
        print(f"✅ テンプレートを同期: {', '.join(sync_templates())}")
        return 0
    finally:
        for plan in plans:
            drop_stage(client, plan["stage"])


if __name__ == "__main__":
    sys.exit(main())
