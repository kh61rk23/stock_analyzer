# ==============================================================
# invest_bq_schema.py — 投資審議用データセット invest の DDL
# ==============================================================
# 【役割】審議キュー・審議結果・実行記録を BigQuery（asia-northeast1）に持たせ、
#   Mac や Obsidian vault が無くても審議が回るようにする。
#   deliberation_queue : 日次の審議キュー（パーティション=queue_date）
#   deliberations      : 審議結果（パーティション=deliberated_at の日付）
#   deliberation_runs  : routine の実行記録（隔週判定・失敗理由の表示用）
#   deliberations_latest(ビュー) : 銘柄ごとの最新1件
# 【約束】全て CREATE ... IF NOT EXISTS。何度流しても壊れない。
#   承認列（approval_status / approved_by / approved_at）は人間だけが更新する。
#   エージェントは書かない（部則5）。
# 【実行】python3 invest_bq_schema.py          # DDL を表示するだけ
#         python3 invest_bq_schema.py --apply  # BQ_CREDENTIALS で実際に作成
# ==============================================================
from __future__ import annotations

import argparse
import re

DATASET = "invest"
LOCATION = "asia-northeast1"

_IDENT = re.compile(r"^[A-Za-z0-9_\-]+$")

QUEUE_TABLE = "deliberation_queue"
DELIB_TABLE = "deliberations"
RUNS_TABLE = "deliberation_runs"
LATEST_VIEW = "deliberations_latest"

_DDL_TEMPLATES = {
    "dataset": """
CREATE SCHEMA IF NOT EXISTS `{project}.{ds}`
OPTIONS (location = '{loc}')""",
    QUEUE_TABLE: """
CREATE TABLE IF NOT EXISTS `{project}.{ds}.deliberation_queue` (
  queue_date DATE NOT NULL,
  ticker STRING NOT NULL,
  edinet_code STRING,
  company_name STRING,
  sector_code STRING,
  priority_rank INT64,
  source STRING,
  reason STRING,
  universe_score FLOAT64,
  final_score FLOAT64,
  composite_score FLOAT64,
  factor_scores JSON,
  last_deliberated_at DATE,
  last_verdict STRING,
  packet_md STRING,
  status STRING,
  claimed_at TIMESTAMP,
  case_id STRING,
  updated_at TIMESTAMP
)
PARTITION BY queue_date
CLUSTER BY status, ticker""",
    DELIB_TABLE: """
CREATE TABLE IF NOT EXISTS `{project}.{ds}.deliberations` (
  case_id STRING NOT NULL,
  ticker STRING,
  company_name STRING,
  sector_code STRING,
  deliberated_at TIMESTAMP,
  verdict STRING,
  conviction INT64,
  layer STRING,
  bull_conviction INT64,
  bear_conviction INT64,
  devil_unresolved INT64,
  devil_reversal INT64,
  kpi JSON,
  bull_memo STRING,
  bear_memo STRING,
  devil_memo STRING,
  report_md STRING,
  packet_md STRING,
  models JSON,
  run_env STRING,
  approval_status STRING,
  approved_by STRING,
  approved_at TIMESTAMP,
  review_on DATE
)
PARTITION BY DATE(deliberated_at)
CLUSTER BY ticker""",
    RUNS_TABLE: """
CREATE TABLE IF NOT EXISTS `{project}.{ds}.deliberation_runs` (
  run_id STRING NOT NULL,
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  run_env STRING,
  status STRING,
  claimed_count INT64,
  completed_count INT64,
  case_ids STRING,
  error STRING
)
PARTITION BY DATE(started_at)""",
    LATEST_VIEW: """
CREATE VIEW IF NOT EXISTS `{project}.{ds}.deliberations_latest` AS
SELECT *
FROM `{project}.{ds}.deliberations`
WHERE ticker IS NOT NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY deliberated_at DESC, case_id DESC) = 1""",
}

ORDER = ["dataset", QUEUE_TABLE, DELIB_TABLE, RUNS_TABLE, LATEST_VIEW]


def ddl(project: str, key: str, ds: str = DATASET) -> str:
    """1つ分の DDL を返す。project/ds は識別子として検査する（SQL 連結の事故防止）。"""
    for v in (project, ds):
        if not _IDENT.match(v):
            raise ValueError(f"識別子として不正: {v!r}")
    return _DDL_TEMPLATES[key].format(project=project, ds=ds, loc=LOCATION).strip()


def all_ddl(project: str, ds: str = DATASET) -> list[str]:
    return [ddl(project, k, ds) for k in ORDER]


def ensure(client) -> None:
    """DDL を順に流す。失敗は例外のまま上げる（握り潰さない）。"""
    for sql in all_ddl(client.project):
        client.query(sql, location=LOCATION).result()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="BQ_CREDENTIALS で実際に作成する")
    ap.add_argument("--project", default="stock-analyzer-edinet")
    args = ap.parse_args(argv)
    if not args.apply:
        print(";\n\n".join(all_ddl(args.project)) + ";")
        return 0
    from edinet_bq_client import EDINETBQClient
    ensure(EDINETBQClient().client)
    print("invest データセットと表を作成（既存なら何もしない）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
