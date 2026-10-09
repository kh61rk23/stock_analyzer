"""
bi_export.py — BI基盤のマートを BigQuery から書き出す
=====================================================
【役割】bi/marts/*.sql を BigQuery で1マート1クエリで実行し、
        stock_data/bi/<mart>.parquet（pyarrow・snappy）と stock_data/bi/manifest.json を書く。
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
}
# 前回の行数のこの割合を下回ったら、壊れた結果とみなして上書きしない
MIN_ROW_RATIO = 0.5


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


def run_query(client, sql: str, dry_run: bool):
    """1クエリ。dry_run はスキャン量(bytes)を返し、通常実行は Arrow 表を返す。"""
    from google.cloud import bigquery

    if dry_run:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        return job.total_bytes_processed
    # Storage Read API は権限(readSessionUser)が要るので使わない（edinet_bq_client.query_to_df と同じ理由）
    return client.query(sql).result().to_arrow(create_bqstorage_client=False)


def export_mart(client, mart: str, prev: dict | None) -> tuple[dict | None, bool]:
    """1マートを書き出す。(manifestエントリ, parquetを書いたか) を返す。失敗は例外。"""
    sql = (MART_DIR / f"{mart}.sql").read_text(encoding="utf-8")
    sql_sha = sha256_text(sql)
    table = canonical_table(run_query(client, sql, False), mart)
    if table.num_rows == 0:
        raise RuntimeError(f"{mart}: 0行（認証・権限・SQLの不整合を疑う。既存の parquet は残す）")
    if prev and prev.get("rows") and table.num_rows < prev["rows"] * MIN_ROW_RATIO:
        raise RuntimeError(
            f"{mart}: 行数が前回 {prev['rows']} から {table.num_rows} に急減（壊れた結果とみなし上書きしない）")
    sha = content_sha(table)
    path = OUT_DIR / f"{mart}.parquet"
    columns = [{"name": f.name, "type": friendly_type(f.type)} for f in table.schema]
    if prev and prev.get("content_sha") == sha and path.exists():
        # 内容が同じなら parquet もマニフェスト項目も触らない（generated_at＝内容が変わった日時）
        entry = dict(prev)
        entry["sql_sha"] = sql_sha
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

    manifest = load_manifest()
    by_name = {e["mart"]: e for e in manifest["marts"]}
    changed = False
    for mart in names:
        try:
            if args.dry_run:
                sql = (MART_DIR / f"{mart}.sql").read_text(encoding="utf-8")
                print(f"🧪 {mart}: dry-run OK  スキャン {run_query(client, sql, True):,} bytes")
                continue
            entry, wrote = export_mart(client, mart, by_name.get(mart))
            if entry != by_name.get(mart):
                by_name[mart] = entry
                changed = True
            state = "更新" if wrote else "変更なし（parquet据え置き）"
            print(f"✅ {mart}: {entry['rows']:,}行 {len(entry['columns'])}列 {state}")
        except Exception as e:  # noqa: BLE001 マート単位で続行し、最後に非0で返す
            print(f"❌ {mart}: {type(e).__name__}: {e}")
            failures.append(mart)

    if changed:
        manifest = {"version": 1, "marts": [by_name[k] for k in sorted(by_name)]}
        write_atomic(MANIFEST, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        print(f"✅ manifest.json を更新（{len(manifest['marts'])}マート）")
    if failures:
        print(f"❌ bi_export 失敗: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
