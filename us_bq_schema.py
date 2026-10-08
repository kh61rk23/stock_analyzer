# ==============================================================
# us_bq_schema.py  v1.0
# 米国株オルタナティブデータ層 BigQuery スキーマ定義（U-4 / I-1 / I-3 / I-4）
# ==============================================================
# 【なぜこのファイルが必要か】
#   institutional_13f_monitor.py 等は `us.institutional_positions` へ INSERT するが、
#   us データセットを作るのはこのファイルだけ（edinet_bq_schema.py の datasets
#   リストに us は含まれない）。本ファイルは ontology_bq_schema.py と同じパターンで us データセット + 4テーブルを
#   冪等に作成する。
#
# 【役割】   dataset `us` の5テーブルを冪等に作成する
# 【実行】   初回のみ手動実行（ontology_bq_schema.py / edinet_bq_schema.py と同じ運用）:
#            python3 us_bq_schema.py
# 【注意】   IF NOT EXISTS のため再実行しても既存テーブルに影響なし。
#            実行前提: BUG-06（BQ_CREDENTIALSのSAに bigquery.jobs.create 権限なし・
#            CHANGELOG既知バグ表参照）が解消されていること。未解消の場合はテーブル作成
#            クエリ自体が失敗するため、先に IAM（roles/bigquery.jobUser 付与）を解消すること。
#
# 【テーブル構成（SPEC_db_schema.md §6 の設計をそのままDDL化）】
#   institutional_positions : SEC EDGAR Form 13F 機関投資家ポジション（institutional_13f_monitor.py が書込）
#   job_signal_cache        : 求人票キーワード急増シグナル（現状ローカルJSONのみ・将来のBQ移行用）
#   executive_transitions   : 役員略歴YoY差分・外部人材登用シグナル（同上）
#   facility_signals        : 施設着工シグナル（同上）
#   fundamentals            : SEC EDGAR四半期財務（us_fundamental_rotator.py が主キーMERGE）
#
# 【作成後にやること】
#   1. institutional_13f_monitor.py の次回実行でBQ書込が成功するか実行ログを確認
#   2. job_signal_cache / executive_transitions / facility_signals は現状ローカルJSON
#      キャッシュのみで運用中のため、BQ移行する場合は各モジュール側の insert 実装を追加
#      すること（本ファイルはテーブルの箱を用意するのみ）
#   3. SPEC_db_schema.md §6 の「実在未検証」表記を「作成済み」に更新すること
# ==============================================================

from __future__ import annotations

DDL = [
    """
    CREATE SCHEMA IF NOT EXISTS `{project}.us`
    OPTIONS (location = 'US', description = '米国株オルタナティブデータ層 (Wave 6 - U-4/I-1/I-3/I-4)')
    """,
    """
    CREATE TABLE IF NOT EXISTS `{project}.us.institutional_positions` (
      quarter          STRING NOT NULL OPTIONS(description='"2025Q4" 形式'),
      cik              STRING NOT NULL OPTIONS(description='SEC EDGAR CIKコード'),
      institution_name STRING OPTIONS(description='機関投資家名'),
      tier             INT64  OPTIONS(description='1=Buffett/2=Ackman系/3=大手'),
      ticker           STRING OPTIONS(description='対象銘柄ティッカー'),
      company_name     STRING,
      change_type      STRING OPTIONS(description='"new" / "increase" / "decrease" / "exit"'),
      value_usd        INT64  OPTIONS(description='今期ポジション額（ドル）'),
      prev_value_usd   INT64  OPTIONS(description='前期ポジション額（ドル）'),
      value_change_usd INT64  OPTIONS(description='変化額（ドル）'),
      shares           INT64  OPTIONS(description='保有株数'),
      adj_pt           FLOAT64 OPTIONS(description='screener加算ポイント（±最大8pt）'),
      detected_at      TIMESTAMP OPTIONS(description='シグナル検知日時'),
      updated_at       TIMESTAMP
    )
    PARTITION BY DATE(detected_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS `{project}.us.job_signal_cache` (
      signal_date       DATE   NOT NULL,
      category          STRING NOT NULL OPTIONS(description='"ai_ml" / "semiconductor" 等'),
      keyword_count     INT64  OPTIONS(description='今月のキーワードヒット数'),
      prev_count        INT64  OPTIONS(description='前月のキーワードヒット数'),
      surge_detected    BOOL   OPTIONS(description='急増フラグ（ratio >= 1.5）'),
      surge_ratio       FLOAT64 OPTIONS(description='実際の倍率'),
      sector_boost_json JSON   OPTIONS(description='{sector_code: boost_pt}'),
      updated_at        TIMESTAMP
    )
    PARTITION BY signal_date
    """,
    """
    CREATE TABLE IF NOT EXISTS `{project}.us.executive_transitions` (
      detection_date      DATE   NOT NULL,
      ticker               STRING NOT NULL,
      edinet_code          STRING,
      company_name         STRING,
      new_hire_count       INT64  OPTIONS(description='外部人材登用数'),
      skill_categories     JSON   OPTIONS(description='{category: count}'),
      transition_detected  BOOL   OPTIONS(description='転換シグナルフラグ'),
      fiscal_year_end      STRING OPTIONS(description='対象決算期'),
      sector_code          STRING,
      sector_boost_pt      FLOAT64 OPTIONS(description='加算ポイント'),
      updated_at           TIMESTAMP
    )
    PARTITION BY detection_date
    """,
    """
    CREATE TABLE IF NOT EXISTS `{project}.us.facility_signals` (
      signal_date     DATE   NOT NULL,
      project_name    STRING,
      project_type    STRING OPTIONS(description='"工場" / "物流" / "データセンター" 等'),
      company_name    STRING,
      prefecture      STRING,
      area_sqm        FLOAT64,
      matched_sectors JSON   OPTIONS(description='[sector_code, ...]'),
      sector_boost_pt FLOAT64,
      source          STRING OPTIONS(description='"env_assessment" / "meti_factory"'),
      announce_date   DATE,
      expires_at      DATE   OPTIONS(description='signal_date + FACILITY_EXPANSION_TTL_DAYS'),
      updated_at      TIMESTAMP
    )
    PARTITION BY signal_date
    """,
    """
    CREATE TABLE IF NOT EXISTS `{project}.us.fundamentals` (
      f_id              STRING NOT NULL OPTIONS(description='ticker + "_" + period_end の冪等主キー'),
      ticker            STRING NOT NULL,
      cik               INT64,
      period_end        DATE   NOT NULL,
      fiscal_period     STRING OPTIONS(description='例: 2025Q3'),
      revenue           FLOAT64,
      operating_income  FLOAT64,
      net_income        FLOAT64,
      eps_diluted       FLOAT64,
      fcf               FLOAT64 OPTIONS(description='OperatingCF - Capex'),
      buyback           FLOAT64 OPTIONS(description='PaymentsForRepurchaseOfCommonStock'),
      source            STRING,
      updated_at        TIMESTAMP
    )
    PARTITION BY period_end
    CLUSTER BY ticker
    """,
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS f_id STRING",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS period_end DATE",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS fiscal_period STRING",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS revenue FLOAT64",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS operating_income FLOAT64",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS net_income FLOAT64",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS eps_diluted FLOAT64",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS fcf FLOAT64",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS buyback FLOAT64",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS source STRING",
    "ALTER TABLE `{project}.us.fundamentals` ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
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
        # NOTE: format() は使わない。job_signal_cache / executive_transitions の
        # OPTIONS(description=...) に記述例として素の中括弧（例: '{sector_code: boost_pt}'）
        # が含まれ、str.format() だと KeyError('sector_code') で落ちる。
        # DDL中の置換対象は "{project}" のみなので replace() で置換する。
        sql = ddl.replace("{project}", project)
        cli.client.query(sql).result()
        first = sql.strip().splitlines()[0].strip()
        print(f"  ✅ {first[:70]}")
    print("✅ us データセット作成完了（冪等）")
    print("→ 次にやること: SPEC_db_schema.md §6の「実在未検証」表記を「作成済み」に更新すること")


if __name__ == "__main__":
    create_all()
