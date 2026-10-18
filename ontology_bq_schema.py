# ==============================================================
# ontology_bq_schema.py  v1.0  (Wave ONT-1)
# オントロジー層 BigQuery スキーマ定義（Palantir Foundry型・第1段）
# ==============================================================
# 【役割】   dataset `ontology` の4テーブルを冪等に作成する
# 【実行】   初回のみ手動実行（edinet_bq_schema_new と同じ運用）:
#            python3 ontology_bq_schema.py
# 【注意】   IF NOT EXISTS のため再実行しても既存テーブルに影響なし
#
# 【設計思想 — なぜエンティティグラフか】
#   現状の stock_analyzer は「シグナル→スコア→推奨」の変換過程が
#   実行時の results dict の中にしか存在せず、翌日には消える。
#   オントロジー層はこれを永続エンティティ＋イベントとして蓄積し、
#   「なぜこの銘柄か」の系譜（リネージ）を SQL 1本で遡れるようにする。
#
# 【テーブル構成】
#   company_entity : 企業エンティティ（ticker を主キーとする静的属性）
#   signal_event   : シグナル発火イベント（日付×カテゴリ×対象×値）
#   theme_entity   : テーマ／技術クラスターエンティティ
#   decision_event : 投資部の審議・承認・成績イベント（判断台帳と同期）
#
# 【リネージ取得SQL（例: 7203.T の意思決定系譜）】
#   SELECT d.case_id, d.verdict, d.conviction, d.decided_at,
#          s.signal_category, s.signal_value, s.fired_at, s.source_module
#   FROM `ontology.decision_event` d
#   LEFT JOIN `ontology.signal_event` s
#     ON s.ticker = d.ticker
#    AND s.fired_at BETWEEN DATE_SUB(d.decided_at, INTERVAL 30 DAY)
#                        AND d.decided_at
#   WHERE d.ticker = '7203.T'
#   ORDER BY d.decided_at DESC, s.fired_at DESC;
# ==============================================================

from __future__ import annotations

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
      status      STRING OPTIONS(description='承認待ち/承認/否認/保留'),
      result      STRING OPTIONS(description='後日成績'),
      decided_at  DATE   NOT NULL,
      ingested_at TIMESTAMP NOT NULL
    )
    PARTITION BY decided_at
    """,
]


def create_all() -> None:
    import importlib.util
    from pathlib import Path
    repo = Path(__file__).parent
    spec = importlib.util.spec_from_file_location(
        "edinet_bq_client", str(repo / "edinet_bq_client.py"))
    bqc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bqc)
    cli = bqc.EDINETBQClient()
    project = cli.project
    for ddl in DDL:
        sql = ddl.format(project=project)
        cli.client.query(sql).result()
        first = sql.strip().splitlines()[0].strip()
        print(f"  ✅ {first[:70]}")
    print("✅ ontology データセット作成完了（冪等）")


if __name__ == "__main__":
    create_all()
