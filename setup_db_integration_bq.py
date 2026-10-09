"""DB統合W3/W5のBigQueryスキーマを冪等に作成・拡張する。"""

from __future__ import annotations

import time

from edinet_bq_client import EDINETBQClient

# 同一テーブルへの連続 ALTER はレート制限に当たるため、待って再試行する
_RATE_LIMIT_RETRIES  = 3
_RATE_LIMIT_WAIT_SEC = 15


def _statements(project: str) -> list[str]:
    return [
        f'CREATE SCHEMA IF NOT EXISTS `{project}.real_estate` OPTIONS(location="asia-northeast1")',
        f"""CREATE TABLE IF NOT EXISTS `{project}.edinet.company` (
          edinet_code STRING NOT NULL, sec_code STRING, name STRING, name_en STRING,
          industry STRING, listing_status STRING, is_delisted BOOL,
          accounting_standard STRING, credit_score INT64, corporate_number STRING,
          last_fetched_at TIMESTAMP, updated_at TIMESTAMP
        )""",
        f"ALTER TABLE `{project}.edinet.financials` ADD COLUMN IF NOT EXISTS edinet_code STRING",
        f"ALTER TABLE `{project}.edinet.financials` ADD COLUMN IF NOT EXISTS fiscal_year INT64",
        f"ALTER TABLE `{project}.edinet.text_analysis` ADD COLUMN IF NOT EXISTS raw_text STRING",
        f"ALTER TABLE `{project}.edinet.text_analysis` ADD COLUMN IF NOT EXISTS source STRING",
        f"""CREATE TABLE IF NOT EXISTS `{project}.real_estate.corporate_holdings_p1` (
          edinet_code STRING NOT NULL,
          company_name STRING,
          fiscal_year INT64 NOT NULL,
          facility_name STRING NOT NULL,
          prefecture STRING,
          municipality_code STRING,
          book_value_land_m_yen FLOAT64,
          book_value_buildings_m_yen FLOAT64,
          land_area_m2 FLOAT64,
          commercial_land_price_per_m2 FLOAT64,
          residential_land_price_per_m2 FLOAT64,
          industrial_land_price_per_m2 FLOAT64,
          population_current INT64,
          population_2050 INT64,
          population_change_pct FLOAT64,
          flood_risk_area_pct FLOAT64,
          landslide_risk_area_pct FLOAT64,
          source STRING,
          updated_at TIMESTAMP
        ) CLUSTER BY edinet_code, fiscal_year""",
        f"""CREATE TABLE IF NOT EXISTS `{project}.real_estate.area_profile_p1` (
          municipality_code STRING NOT NULL,
          prefecture_name STRING,
          municipality_name STRING,
          total_transactions INT64,
          median_price FLOAT64,
          latest_land_price FLOAT64,
          land_price_yoy FLOAT64,
          avg_land_price_commercial FLOAT64,
          avg_land_price_residential FLOAT64,
          land_price_yoy_change FLOAT64,
          flood_risk_area_pct FLOAT64,
          latest_avg_price_condo FLOAT64,
          hospital_count INT64,
          source STRING,
          updated_at TIMESTAMP
        )""",
        # ── geopolitics.company_country_exposure の列を DDL に揃える ──
        # CREATE TABLE IF NOT EXISTS は既存テーブルを変更しないため、
        # edinet_bq_schema.py の DDL を拡張しても実表には反映されない。
        # 列が欠けると geo_signal.py が「Name ticker not found」で落ちる。
        # 【注意】BigQuery の ALTER ADD COLUMN は REQUIRED 列を追加できないため、
        #   すべて NULLABLE で追加する。
        f"ALTER TABLE `{project}.geopolitics.company_country_exposure` "
        f"ADD COLUMN IF NOT EXISTS ticker STRING",
        f"ALTER TABLE `{project}.geopolitics.company_country_exposure` "
        f"ADD COLUMN IF NOT EXISTS revenue_share_est FLOAT64",
        f"ALTER TABLE `{project}.geopolitics.company_country_exposure` "
        f"ADD COLUMN IF NOT EXISTS employee_share_est FLOAT64",
        f"ALTER TABLE `{project}.geopolitics.company_country_exposure` "
        f"ADD COLUMN IF NOT EXISTS asset_share_est FLOAT64",
        f"ALTER TABLE `{project}.geopolitics.company_country_exposure` "
        f"ADD COLUMN IF NOT EXISTS evidence_url STRING",
        f"ALTER TABLE `{project}.geopolitics.company_country_exposure` "
        f"ADD COLUMN IF NOT EXISTS rationale STRING",
        f"ALTER TABLE `{project}.geopolitics.company_country_exposure` "
        f"ADD COLUMN IF NOT EXISTS auto_derived BOOL",
        f"ALTER TABLE `{project}.geopolitics.company_country_exposure` "
        f"ADD COLUMN IF NOT EXISTS review_flag BOOL",
        # スクリーナー結果の日次スナップショット（DB移管 Phase 1）。
        # 銘柄mdの指標の約半分（technical系・FCFマージン・D/E・自己資本比率・
        # 配当利回り等）は screener_history.json の14項目に含まれないため、
        # ここを正本にして時系列で追えるようにする。
        f"""CREATE TABLE IF NOT EXISTS `{project}.analytics.stock_snapshot` (
          snapshot_date DATE NOT NULL,
          ticker STRING NOT NULL,
          market STRING,
          name STRING,
          sector_code STRING,
          sector_name STRING,
          composite_score FLOAT64,
          sector_score FLOAT64,
          value_score FLOAT64,
          quality_score FLOAT64,
          technical_score FLOAT64,
          discovery_score FLOAT64,
          per FLOAT64,
          pbr FLOAT64,
          roe FLOAT64,
          roic FLOAT64,
          fcf_margin FLOAT64,
          debt_equity FLOAT64,
          equity_ratio FLOAT64,
          dividend_yield FLOAT64,
          gross_margin FLOAT64,
          op_margin FLOAT64,
          market_cap_b FLOAT64,
          avg_volume FLOAT64,
          current_price FLOAT64,
          rsi14 FLOAT64,
          macd_crossup BOOL,
          above_ma200 BOOL,
          above_vwap BOOL,
          vwap_20d FLOAT64,
          week52_position_pct FLOAT64,
          accrual_tier STRING,
          size_tier STRING,
          strategy_label STRING,
          analyst_reco STRING,
          signal_text STRING,
          pass_filters BOOL,
          ingested_at TIMESTAMP
        ) PARTITION BY snapshot_date CLUSTER BY ticker, market""",
        # 株価の永続化。yf_cache が実行内で集めた日足をここへ落とし、
        # 事後検証と yfinance 障害時の参照元にする。
        f"""CREATE TABLE IF NOT EXISTS `{project}.analytics.price_daily` (
          trade_date DATE NOT NULL,
          ticker STRING NOT NULL,
          open FLOAT64,
          high FLOAT64,
          low FLOAT64,
          close FLOAT64,
          volume FLOAT64,
          source STRING,
          ingested_at TIMESTAMP
        ) PARTITION BY trade_date CLUSTER BY ticker""",
        # price_daily は load job の追記で (ticker, trade_date) の重複がありうる。読む側はこのビューを使う。
        f"""CREATE OR REPLACE VIEW `{project}.analytics.price_daily_latest` AS
        SELECT * EXCEPT(rn) FROM (
          SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker, trade_date ORDER BY ingested_at DESC) AS rn
          FROM `{project}.analytics.price_daily`) WHERE rn = 1""",
        # 財務三表の細目（売上原価・販管費・研究開発費・支払利息・棚卸資産・
        # のれん・支払配当金など154項目）。edinet.financials は約25項目だけなので、
        # EdinetDB /financials（1年度178項目）の残りをここに縦持ちで入れる。
        # 項目定義は edinetdb_fields.py（区分 PL/BS/CF/株式/人的資本/環境/指標）。
        f"""CREATE TABLE IF NOT EXISTS `{project}.edinet.financial_items` (
          item_id STRING NOT NULL,
          edinet_code STRING NOT NULL,
          company_code STRING,
          fiscal_year INT64,
          fiscal_year_end DATE,
          statement STRING,
          item STRING NOT NULL,
          label_ja STRING,
          unit STRING,
          value FLOAT64,
          accounting_standard STRING,
          basis STRING,
          doc_id STRING,
          source STRING,
          updated_at TIMESTAMP
        ) CLUSTER BY edinet_code, statement, item""",
        # 単四半期の財務細目（EdinetDB period=quarterly_standalone）
        f"""CREATE TABLE IF NOT EXISTS `{project}.edinet.financial_items_q` (
          item_id STRING NOT NULL,
          edinet_code STRING NOT NULL,
          company_code STRING,
          fiscal_year INT64,
          quarter STRING,
          fiscal_year_end DATE,
          statement STRING,
          item STRING NOT NULL,
          label_ja STRING,
          unit STRING,
          value FLOAT64,
          accounting_standard STRING,
          basis STRING,
          derivation_method STRING,
          doc_id STRING,
          source STRING,
          updated_at TIMESTAMP
        ) CLUSTER BY edinet_code, item""",
    ]


def setup() -> None:
    """全ステートメントを実行する。

    最初の失敗で打ち切らず全件試し、最後にまとめて報告する
    （残りの成否がログから分かるように）。1件でも失敗したら非ゼロ終了する。
    """
    bq = EDINETBQClient()
    statements = _statements(bq.project)      # 毎回組み立て直さない
    total = len(statements)
    failures: list[tuple[int, str, str]] = []

    for index, sql in enumerate(statements, 1):
        head = " ".join(sql.split())[:90]
        # 同一テーブルへ ALTER を連続実行すると
        # 「Exceeded rate limits: too many table update operations for this table」
        # になる（テーブルのメタデータ更新は数秒あたり数回が上限）。
        # レート制限だけは待って再試行する。
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            try:
                bq.client.query(sql).result()
                print(f"✅ DB integration schema {index}/{total}")
                break
            except Exception as exc:                   # noqa: BLE001
                msg = str(exc)
                is_rate = ("rate limit" in msg.lower()
                           or "too many table update" in msg.lower())
                if is_rate and attempt < _RATE_LIMIT_RETRIES:
                    wait = _RATE_LIMIT_WAIT_SEC * (attempt + 1)
                    print(f"   ⏳ {index}/{total} レート制限 → {wait}秒待って再試行")
                    time.sleep(wait)
                    continue
                failures.append((index, head, msg))
                print(f"❌ DB integration schema {index}/{total} 失敗: {head}")
                print(f"   → {msg[:200]}")
                break

    if failures:
        print(f"\n❌ {len(failures)}/{total} 件が失敗しました。")
        if any("billingNotEnabled" in e or "Billing has not been enabled" in e
               for _, _, e in failures):
            print("   ⚠️  GCP プロジェクトの課金が無効です（サンドボックス）。"
                  "DDL/DML は課金を有効化するまで実行できません。")
            print("   → https://console.cloud.google.com/billing で課金を有効化してください。")
        raise SystemExit(1)

    print(f"\n✅ 全 {total} 件完了。")


if __name__ == "__main__":
    setup()
