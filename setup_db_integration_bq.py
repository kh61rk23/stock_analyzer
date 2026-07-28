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
        # ── 2026-07-28: geopolitics.company_country_exposure の列欠落を修復 ──
        # 【事象】geo_signal.py が毎銘柄
        #   「400 Name ticker not found inside e」で失敗していた。
        # 【原因】edinet_bq_schema.py の DDL には ticker 等 20 列が定義されて
        #   いるが、本番の実表は 12 列しか無かった（MCP で実測確認）。
        #   CREATE TABLE IF NOT EXISTS は既存テーブルを変更しないため、
        #   DDL を後から拡張しても実表に反映されていなかった。
        # 【注意】BigQuery の ALTER ADD COLUMN は REQUIRED 列を追加できないため、
        #   すべて NULLABLE で追加する（既存行が無いので実害なし・実表0行）。
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
    ]


def setup() -> None:
    """全ステートメントを実行する。

    2026-07-28 改善: 従来は最初の失敗で例外送出して打ち切っていたため、
    本番ログが「✅ 1/8」の直後にスタックトレースで終わり、
    残り7件が成功するのか失敗するのか分からなかった。
    全件試して最後にまとめて報告し、1件でも失敗したら非ゼロ終了する。
    """
    bq = EDINETBQClient()
    statements = _statements(bq.project)      # 毎回組み立て直さない
    total = len(statements)
    failures: list[tuple[int, str, str]] = []

    for index, sql in enumerate(statements, 1):
        head = " ".join(sql.split())[:90]
        # 2026-07-28: 同一テーブルへ ALTER を連続実行すると
        # 「Exceeded rate limits: too many table update operations for this table」
        # になる（テーブルのメタデータ更新は数秒あたり数回が上限）。
        # 実際に company_country_exposure への8件連続 ALTER で5件目までしか
        # 通らなかったため、レート制限だけは待って再試行する。
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
