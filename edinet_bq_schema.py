"""
edinet_bq_schema_new_20260601.py
====================
【役割】   BigQueryのデータセット・全テーブルを構造的に定義し作成する
【実行】   初回のみ Colab で手動実行
【注意】   IF NOT EXISTS のため、既存テーブルへの影響なし（再実行しても安全）
"""

import os
from google.colab import drive
import sys

# --- 0. Google Driveをマウントする ---
if not os.path.exists('/content/drive'):
    drive.mount('/content/drive')

from google.cloud import bigquery
from google.api_core.exceptions import AlreadyExists

from google.colab import auth
auth.authenticate_user()

PROJECT_ID = 'stock-analyzer-edinet'
LOCATION = "asia-northeast1"
client = bigquery.Client(project=PROJECT_ID, location=LOCATION)

# 1. まずデータセットを作成する
datasets = ['edinet', 'taxonomy', 'geopolitics', 'analytics']

print("--- Step 1: Dataset Creation ---")
for ds_name in datasets:
    ds_id = f"{PROJECT_ID}.{ds_name}"
    dataset = bigquery.Dataset(ds_id)
    dataset.location = LOCATION
    try:
        client.create_dataset(dataset)
        print(f"✅ Dataset {ds_name} created.")
    except AlreadyExists:
        print(f"  └ Dataset {ds_name} already exists.")
    except Exception as e:
        print(f"  └ ❌ Error creating dataset {ds_name}: {e}")

# --- テーブル定義の構造化（すべてのテーブルで括弧とパーティションの記述位置を統一） ---
TABLE_DEFINITIONS = {
    # --- edinet データセット ---
    "edinet": {
        # 有報「役員の状況」（officer_extractor.py が EDINET の有報PDFから抽出）
        "officers": """(
            officer_id STRING NOT NULL,        -- doc_id_連番
            doc_id STRING,                     -- 有報の書類管理番号
            company_code STRING,               -- 証券コード (4桁)
            edinet_code STRING,
            fiscal_year_end DATE,
            seq INT64,                         -- 役員一覧での順番
            title STRING,                      -- 役職名
            name STRING,                       -- 氏名
            birth_date DATE,
            is_top BOOL,                       -- 経営トップ（代表者のうち社長/CEO）
            appointed_top DATE,                -- 経営トップの就任年月（1日）
            career_json STRING,                -- 略歴 [[YYYY-MM, 文], ...]
            updated_at TIMESTAMP
        )""",
        "doc_index": """(
            doc_id STRING NOT NULL,            -- 書類管理番号 (Primary Key相当)
            company_code STRING,               -- 証券コード (4桁)
            edinet_code STRING,                -- EDINET提出者コード
            company_name STRING,               -- 提出会社名
            doc_type STRING,                   -- 有価証券報告書 / 四半期報告書 等
            fiscal_year_end DATE,              -- 本決算期末日
            disclosure_date DATE,              -- 書類開示日
            xbrl_available BOOL,               -- XBRLデータ有無
            html_available BOOL,               -- HTMLデータ有無
            fetched_flag BOOL DEFAULT FALSE,   -- 取得済みフラグ
            fetched_at TIMESTAMP,              -- 取得実行日時
            ifrs_flag BOOL DEFAULT FALSE,      -- IFRS採用フラグ
            updated_at TIMESTAMP               -- レコード最終更新日時
        ) PARTITION BY disclosure_date""",
        
        "financials": """(
            fin_id STRING NOT NULL,            -- 財務データID (doc_id + company_code)
            doc_id STRING NOT NULL,            -- 元書類ID
            company_code STRING,               -- 証券コード
            fiscal_year_end DATE,              -- 決算期末日
            report_period STRING,              -- 通期 / 第3四半期 等
            -- PL (損益計算書) --
            net_sales FLOAT64,                 -- 売上高
            operating_income FLOAT64,          -- 営業利益
            ordinary_income FLOAT64,           -- 経常利益
            net_income FLOAT64,                -- 当期純利益
            -- BS (貸借対照表) --
            total_assets FLOAT64,              -- 総資産
            equity FLOAT64,                    -- 自己資本
            interest_bearing_debt FLOAT64,     -- 有利子負債
            cash_and_equiv FLOAT64,            -- 現金及び現金同等物
            -- CF (キャッシュフロー) --
            operating_cf FLOAT64,              -- 営業CF
            investing_cf FLOAT64,              -- 投資CF
            free_cf FLOAT64,                   -- フリーCF
            -- Indicators (主要指標) --
            employee_count INT64,              -- 従業員数
            avg_age_years FLOAT64,             -- 平均年齢
            avg_annual_salary FLOAT64,         -- 平均年間給与
            total_director_comp FLOAT64,       -- 役員報酬総額
            max_single_comp FLOAT64,           -- 役員最高報酬額
            capex FLOAT64,                     -- 設備投資額
            depreciation FLOAT64,              -- 減価償却費
            roe FLOAT64,                       -- 自己資本利益率
            roa FLOAT64,                       -- 総資産利益率
            operating_margin FLOAT64,          -- 営業利益率
            accrual_ratio FLOAT64,             -- 発生額比率 (利益の質)
            fcf_yield FLOAT64,                 -- FCF利回り
            labor_productivity FLOAT64,        -- 労働生産性 (従業員一人あたり営業利益)
            -- Meta (付加情報) --
            currency STRING DEFAULT 'JPY',     -- 通貨単位
            source STRING,                     -- データソース (EDINET等)
            updated_at TIMESTAMP               -- レコード最終更新日時
        ) PARTITION BY fiscal_year_end""",

        "segments": """(
            segment_id STRING NOT NULL,        -- セグメント一意識別ID
            doc_id STRING NOT NULL,            -- 元書類ID
            company_code STRING,               -- 証券コード
            segment_name STRING,               -- 原文のセグメント名称
            segment_name_norm STRING,          -- 正規化されたセグメント名 (分析用)
            segment_type STRING,               -- BUSINESS(事業別) / REGION(地域別) 等
            parent_segment_id STRING,          -- 親セグメントID (階層構造用)
            report_period STRING,              -- 対象期間 (通期/第3四半期等)
            source_id STRING,                  -- データソース内の参照ID
            updated_at TIMESTAMP               -- レコード最終更新日時
        )""",

        "segment_finance": """(
            sf_id STRING NOT NULL,             -- セグメント財務データID
            segment_id STRING NOT NULL,        -- segmentsテーブルへの外部キー
            revenue FLOAT64,                   -- 外部顧客への売上高
            revenue_ratio FLOAT64,             -- 全社売上に占める割合 (0.0-1.0)
            operating_profit FLOAT64,          -- セグメント利益 (営業利益)
            operating_margin FLOAT64,          -- セグメント利益率
            assets FLOAT64,                    -- セグメント資産
            geography STRING,                  -- 関連する地域・国名
            currency STRING DEFAULT 'JPY',     -- 通貨単位
            report_period STRING,              -- 対象期間
            updated_at TIMESTAMP               -- レコード最終更新日時
        )""",

        "text_analysis": """(
            text_id STRING NOT NULL,           -- 解析結果一意識別ID
            doc_id STRING NOT NULL,            -- 元書類ID
            company_code STRING,               -- 証券コード
            fiscal_year_end DATE,              -- 決算期末日
            section_type STRING,               -- 経営方針 / 事業のリスク 等
            -- Sentiment Scores (FinBERT等) --
            finbert_positive FLOAT64,          -- ポジティブ度
            finbert_negative FLOAT64,          -- ネガティブ度
            finbert_neutral FLOAT64,           -- 中立度
            -- Analysis Indicators --
            yoy_delta FLOAT64,                 -- 前年比の変化量
            risk_deterioration_flag BOOL,      -- リスク悪化フラグ
            risk_category_scores JSON,         -- カテゴリ別リスク詳細
            updated_at TIMESTAMP              -- レコード最終更新日時
        ) PARTITION BY fiscal_year_end""",

        # ── R-2: 業種サブテーマ・銘柄マッピング ──
        #   この2テーブルが無いと get_tickers_for_sector / get_sub_theme_master が
        #   404 になり、セクター銘柄解決が失敗する。
        "sub_theme_master": """(
            sector_code STRING NOT NULL,              -- 業種コード (JP: "1016" / US: "US_45")
            market STRING,                            -- "JP" | "US"
            name_ja STRING,                           -- 業種名
            supply_chain_position STRING,             -- upstream / midstream / downstream
            business_cycle_sensitivity STRING,        -- 景気感応度
            export_dependency STRING,                 -- 輸出依存度
            digital_transformation_stage STRING,      -- DX進度
            esg_priority STRING,                      -- ESG優先度
            bm_type STRING,                           -- ビジネスモデル類型 (nullable)
            subthemes JSON,                           -- サブテーマ配列
            version STRING,                           -- JSONバージョン
            updated_at TIMESTAMP                      -- レコード最終更新日時
        )""",

        "ticker_subtheme_mapping": """(
            ticker STRING NOT NULL,                   -- ティッカー (JP: "6758.T" / US: "AAPL")
            sector_code STRING,                       -- 業種コード
            market STRING,                            -- "JP" | "US"
            subtheme STRING,                          -- 個別サブテーマ
            relevance FLOAT64,                        -- 関連度 (0.0〜1.0)
            source STRING,                            -- "json_sync" | "manual" | "auto_derived"
            updated_at TIMESTAMP                      -- レコード最終更新日時
        )"""
    },

    "taxonomy": {
        "taxonomy_node": """(
            node_id STRING NOT NULL,           -- 一意識別ID (例: semiconductor_logic)
            parent_id STRING,                  -- 親ノードID (NULLは最上位)
            node_name_ja STRING,               -- 技術・業種名 (日本語)
            node_name_en STRING,               -- 技術・業種名 (英語)
            node_level INT64,                  -- 階層レベル (参考値)
            node_type STRING,                  -- industry / domain / category / leaf
            description STRING,                -- 技術定義の詳細説明
            taxonomy_version STRING,           -- スキーマのバージョン
            effective_from DATE,               -- 有効開始日
            effective_to DATE,                 -- 有効終了日 (NULLは現行)
            is_active BOOL DEFAULT TRUE,       -- 論理削除フラグ
            supply_chain_pos STRING,           -- upstream / midstream / downstream
            related_commodities JSON,          -- 関連資源コード (Phase G連携)
            related_countries JSON,            -- 関連の強い国 (Layer C連携)
            notes STRING,                      -- 運用上の備考
            updated_at TIMESTAMP               -- レコード最終更新日時
        )""",



        "company_taxonomy_map": """(
            map_id STRING NOT NULL,            -- マッピング一意識別ID
            company_id STRING NOT NULL,        -- 企業ID
            node_id STRING NOT NULL,           -- 技術ノードID
            relevance_score FLOAT64,           -- 関連強度 (0.0-1.0)
            confidence FLOAT64,                -- 判定の確信度 (0.0-1.0)
            revenue_share_est FLOAT64,         -- 推定売上比率 (0.0-1.0)
            confidence_type STRING,            -- actual / estimated / inferred
            evidence_source STRING,            -- 根拠資料
            evidence_url STRING,               -- 根拠資料のURL
            rationale STRING,                  -- 判定ロジックの簡潔な説明
            valid_from DATE,                   -- マッピング有効開始日
            valid_to DATE,                     -- マッピング有効終了日
            last_verified_at TIMESTAMP,        
            auto_derived BOOL,                 -- AIによる自動推定フラグ
            verified_flag BOOL DEFAULT FALSE,  -- 人間による確認済みフラグ
            updated_at TIMESTAMP               -- レコード最終更新日時
        )""",

    },

    "geopolitics": {
        "country_profile": """(
            country_code STRING NOT NULL,      -- ISO 3166-1 alpha-3
            country_name_ja STRING,
            region STRING,
            democracy_index FLOAT64,           
            corruption_index FLOAT64,          
            rule_of_law FLOAT64,               
            gdp_usd_bn FLOAT64,
            export_concentration FLOAT64,      
            top_export_commodities JSON,       
            is_landlocked BOOL,                
            sealane_adjacency JSON,            
            military_alliances JSON,           
            sanction_flag BOOL,                
            trade_dependency_jp FLOAT64,       
            updated_at TIMESTAMP
        )""",

        "country_risk_daily": """(
            country_code STRING NOT NULL,
            risk_date DATE NOT NULL,
            gdelt_instability FLOAT64,         
            foreign_ministry_level INT64,      
            state_dept_level INT64,            
            composite_risk_score FLOAT64,      
            updated_at TIMESTAMP
        ) PARTITION BY risk_date""",

        "company_country_exposure": """(
            exposure_id STRING NOT NULL,
            company_id STRING NOT NULL,        -- 企業ID（証券コード等）
            ticker STRING,
            market STRING,                     -- JP / US
            country_code STRING NOT NULL,
            exposure_type STRING,              -- RESOURCE_CONCESSION / PRODUCTION_FACILITY / SALES_NETWORK / JOINT_VENTURE / EQUITY_STAKE / PROCUREMENT / CUSTOMER_CONC
            exposure_strength FLOAT64,         -- 依存度 (0.0-1.0)
            commodity_code STRING,             -- 関連資源コード
            revenue_share_est FLOAT64,
            employee_share_est FLOAT64,
            asset_share_est FLOAT64,
            source STRING,                     -- 取得元 (jogmec / edinet_auto / sec_auto / manual)
            evidence_url STRING,
            rationale STRING,
            auto_derived BOOL DEFAULT TRUE,
            verified_flag BOOL DEFAULT FALSE,
            review_flag BOOL DEFAULT FALSE,
            valid_from DATE,
            valid_to DATE,
            updated_at TIMESTAMP
        )"""  
    },

    "analytics": {
    },
}

# ============================================================
# 実行アクション
# ============================================================
print("\n--- Step 2: Table Creation (IF NOT EXISTS) ---")
for dataset_name, tables in TABLE_DEFINITIONS.items():
    ds_id = f"{PROJECT_ID}.{dataset_name}"
    
    for table_name, schema_sql in tables.items():
        full_table_id = f"{ds_id}.{table_name}"
        # {schema_sql} の前後に括益をつけず、そのまま結合する安全な設計
        query = f"CREATE TABLE IF NOT EXISTS `{full_table_id}` {schema_sql}"
        
        try:
            client.query(query).result()
            print(f"  └ ✅ Table {table_name} is ready (or already exists).")
        except Exception as e:
            print(f"  └ ❌ Error in {table_name}: {e}")

print("\n--- Process Complete. ---")