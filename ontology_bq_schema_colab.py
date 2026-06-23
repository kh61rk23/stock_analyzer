# ==============================================================
# ontology_bq_schema_colab.py  v2.0（自己完結版）
# Colab 専用ランナー（auth.authenticate_user 方式・Drive非依存）
# ==============================================================
# 【役割】
#   dataset `ontology` の4テーブルを冪等に作成する。動作実績のある
#   edinet_bq_schema.py と「全く同じ認証方式」（google.colab の
#   auth.authenticate_user）を使う。
#
# 【v2.0 での変更（重要）】
#   v1.0 は本体 ontology_bq_schema.py を Drive から importlib で読み込んで
#   いたため、Drive 上の実パスが MyDrive/stock_analyzer/ でないと
#   「File not found」になった。v2.0 は DDL を本ファイル内に内蔵し、
#   Drive を一切参照しない。よって以下の2通りどちらでも動く:
#     ① Colab セルに本ファイルの中身をそのまま貼り付けて実行
#     ② Drive に置いて %run /content/drive/.../ontology_bq_schema_colab.py
#   ※ DDL は本体 ontology_bq_schema.py と同一内容（手動同期）。本体を
#     変更した場合は本ファイルの DDL も合わせること。
#
# 【実行】Colab のセルで（初回1回のみ・冪等なので再実行も安全）
# ==============================================================

# --- 0. ユーザー認証（edinet_bq_schema.py と同方式・Drive不要）---
from google.colab import auth
auth.authenticate_user()

from google.cloud import bigquery

PROJECT_ID = 'stock-analyzer-edinet'   # edinet_bq_schema.py と同じ固定プロジェクト
LOCATION   = 'US'                       # ontology は US ロケーション（DDLの OPTIONS と一致）
client = bigquery.Client(project=PROJECT_ID, location=LOCATION)

# --- 1. DDL（ontology_bq_schema.py の DDL リストと同一内容）---
DDL = [
    """
    CREATE SCHEMA IF NOT EXISTS `{project}.ontology`
    OPTIONS (location = 'US', description = 'エンティティグラフ層 (Wave ONT-1)')
    """,
    """
    CREATE TABLE IF NOT EXISTS `{project}.ontology.company_entity` (
      ticker        STRING NOT NULL OPTIONS(description='例: 7203.T / AAPL'),
      company_name  STRING,
      market        STRING OPTIONS(description='JP / US'),
      sector_code   STRING OPTIONS(description='東証33業種コード or GICS'),
      size_tier     STRING OPTIONS(description='Large/Mid/Small/Micro/Nano'),
      business_model STRING OPTIONS(description='BM-01〜BM-12'),
      is_portfolio  BOOL   OPTIONS(description='保有中フラグ'),
      layer         STRING OPTIONS(description='NISA/カジノ/対象外（投資部憲法）'),
      updated_at    TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS `{project}.ontology.signal_event` (
      event_id        STRING NOT NULL,
      fired_at        DATE   NOT NULL,
      ticker          STRING OPTIONS(description='銘柄。業種シグナルはNULL'),
      sector_code     STRING,
      signal_category STRING NOT NULL OPTIONS(description='signal_aggregator A-U / drift / screener_pass 等'),
      signal_value    FLOAT64,
      direction       STRING OPTIONS(description='bullish/bearish/neutral'),
      source_module   STRING NOT NULL,
      detail          STRING,
      ingested_at     TIMESTAMP NOT NULL
    )
    PARTITION BY fired_at
    """,
    """
    CREATE TABLE IF NOT EXISTS `{project}.ontology.theme_entity` (
      theme_id    STRING NOT NULL,
      theme_name  STRING NOT NULL,
      theme_type  STRING OPTIONS(description='subtheme/tech_cluster/etf_theme'),
      market      STRING,
      member_tickers ARRAY<STRING>,
      heat_score  FLOAT64,
      updated_at  TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS `{project}.ontology.decision_event` (
      case_id     STRING NOT NULL OPTIONS(description='投資部 case_id'),
      ticker      STRING NOT NULL,
      verdict     STRING OPTIONS(description='Buy/Watch/Avoid'),
      conviction  INT64,
      layer       STRING,
      open_issues INT64  OPTIONS(description='デビル未解消指摘数'),
      status      STRING OPTIONS(description='承認待ち/承認/見送り'),
      result      STRING OPTIONS(description='後日成績'),
      decided_at  DATE   NOT NULL,
      ingested_at TIMESTAMP NOT NULL
    )
    PARTITION BY decided_at
    """,
]

# --- 2. DDL を順に実行 ---
print(f"--- ontology スキーマ作成開始 (project={PROJECT_ID}, location={LOCATION}) ---")
for ddl in DDL:
    sql = ddl.format(project=PROJECT_ID)
    client.query(sql).result()
    first = sql.strip().splitlines()[0].strip()
    print(f"  ✅ {first[:70]}")
print("✅ ontology データセット作成完了（冪等）")
