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
【終了コード】どれか1つでも失敗したら 1。成功したマートの書き出しは残す。

内容が前回と同じマートは parquet を書き換えない。parquet は同じ内容でも書き出しのたびにバイト列が
変わりうるため、行を主キーで並べ替えた Arrow 表のハッシュ（content_sha）で同一性を判定する。
これを怠ると毎日 parquet がコミットされて git が肥大する。

実体化の手順（1マートあたり重いクエリは1本だけ）:
  1. `CREATE OR REPLACE TABLE bi.<mart>__next AS <マートSQL>` … 元表を読むのはここだけ
  2. 行数を検査（0行・前回の半分未満は失敗。ダッシュボードが読む本番表はまだ触らない）
  3. `list_rows` で表の中身を読む … tabledata.list は課金されない（クエリではない）
  4. コピージョブ（無料）で `bi.<mart>` に差し替え、ステージ表を消す
権限不足などで実体化できないときは握り潰さず失敗（終了コード1）にする。
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
}
# 前回の行数のこの割合を下回ったら、壊れた結果とみなして上書きしない
MIN_ROW_RATIO = 0.5

PROJECT = "stock-analyzer-edinet"
DATASET = "bi"
LOCATION = "asia-northeast1"  # マートの元表（edinet・geopolitics・real_estate）と同じ。違うと JOIN できない
STAGE_SUFFIX = "__next"


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


def export_mart(client, mart: str, prev: dict | None) -> tuple[dict | None, bool]:
    """1マートを実体化して書き出す。(manifestエントリ, parquetを書いたか) を返す。失敗は例外。"""
    sql = (MART_DIR / f"{mart}.sql").read_text(encoding="utf-8")
    sql_sha = sha256_text(sql)
    staged = stage_mart(client, mart, sql)
    try:
        check_rows(mart, staged["rows"], prev)
        table = canonical_table(read_stage(client, staged["stage"]), mart)
        check_rows(mart, table.num_rows, prev)
        info = promote_mart(client, mart, staged["stage"])
    finally:
        drop_stage(client, staged["stage"])
    bq = {"table": info["table"], "table_bytes": info["table_bytes"]}
    sha = content_sha(table)
    path = OUT_DIR / f"{mart}.parquet"
    columns = [{"name": f.name, "type": friendly_type(f.type)} for f in table.schema]
    if prev and prev.get("content_sha") == sha and path.exists():
        # 内容が同じなら parquet もマニフェスト項目も触らない（generated_at＝内容が変わった日時）
        entry = dict(prev)
        entry["sql_sha"] = sql_sha
        entry.update(bq)
        return entry, False
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf, compression="snappy")
    write_atomic(path, buf.getvalue().to_pybytes())
    entry = {
        "mart": mart,
        "rows": table.num_rows,
        "columns": columns,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sql_sha": sql_sha,
        "content_sha": sha,
        **bq,
    }
    return entry, True


def copy_templates() -> list[str]:
    """検査に通ったテンプレートだけを stock_data/bi/templates/ へ同期する。不合格があれば例外。"""
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--only", action="append", help="このマートだけ（複数指定可）")
    ap.add_argument("--dry-run", action="store_true", help="構文とスキャン量だけ確認し、何も書かない")
    ap.add_argument("--templates-only", action="store_true", help="テンプレートのコピーだけ")
    args = ap.parse_args(argv)

    failures: list[str] = []
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not args.dry_run:
        try:
            print(f"✅ テンプレートを同期: {', '.join(copy_templates())}")
        except Exception as e:  # noqa: BLE001 失敗は握り潰さず終了コードで返す
            print(f"❌ {e}")
            failures.append("templates")
    if args.templates_only:
        return 1 if failures else 0

    names = mart_names()
    if args.only:
        unknown = set(args.only) - set(names)
        if unknown:
            print(f"❌ 未知のマート: {sorted(unknown)}")
            return 1
        names = [n for n in names if n in args.only]

    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client

    if not args.dry_run:
        try:
            ensure_dataset(client)
        except Exception as e:  # noqa: BLE001 実体化できないなら parquet も更新しない（表と食い違うため）
            print(f"❌ {e}")
            return 1

    manifest = load_manifest()
    by_name = {e["mart"]: e for e in manifest["marts"]}
    changed = False
    for mart in names:
        try:
            if args.dry_run:
                sql = (MART_DIR / f"{mart}.sql").read_text(encoding="utf-8")
                print(f"🧪 {mart}: dry-run OK  スキャン {dry_run_bytes(client, sql):,} bytes")
                continue
            entry, wrote = export_mart(client, mart, by_name.get(mart))
            if entry != by_name.get(mart):
                by_name[mart] = entry
                changed = True
            state = "更新" if wrote else "変更なし（parquet据え置き）"
            print(f"✅ {mart}: {entry['rows']:,}行 {len(entry['columns'])}列 表 {entry['table']} "
                  f"{entry['table_bytes']:,}bytes {state}")
        except Exception as e:  # noqa: BLE001 マート単位で続行し、最後に非0で返す
            print(f"❌ {mart}: {type(e).__name__}: {e}")
            failures.append(mart)

    if changed:
        manifest = {"version": 1, "marts": [by_name[k] for k in sorted(by_name)]}
        write_atomic(MANIFEST, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        total = sum(e.get("table_bytes", 0) for e in manifest["marts"])
        print(f"✅ manifest.json を更新（{len(manifest['marts'])}マート・実体化した表の合計 {total:,}bytes）")
    if failures:
        print(f"❌ bi_export 失敗: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
