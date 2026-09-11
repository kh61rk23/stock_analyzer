"""
edinet_bq_client.py
====================
【役割】   BigQuery読み書きの共通ユーティリティ（シングルトン）
【使用元】 edinet_doc_indexer.py / edinet_direct_fetcher.py（E2）
           segment_mapper.py（E3）/ text_signal.py（E4）/ base_agent.py（E2統合時）
【更新日】 2026-04-26
"""

from google.cloud import bigquery
from google.oauth2 import service_account
import threading
import pandas as pd
import os
import json
import re
from datetime import datetime, timezone


# ============================================================
# 設定読み込み
# ============================================================
# config.py が存在する場合はそこから取得、なければ環境変数から取得
try:
    from config import BQ_PROJECT_ID, BQ_CREDENTIALS_PATH, BQ_DATASET
except ImportError:
    BQ_PROJECT_ID      = os.environ.get("BQ_PROJECT_ID", "")
    BQ_CREDENTIALS_PATH = os.environ.get("BQ_CREDENTIALS_PATH", "")
    BQ_DATASET         = os.environ.get("BQ_DATASET", "edinet")

# 1クエリあたりのスキャン上限（暴走クエリの費用ガード / 2026-07-28）
try:
    from config import BQ_MAX_BYTES_BILLED
except ImportError:
    BQ_MAX_BYTES_BILLED = int(
        os.environ.get("BQ_MAX_BYTES_BILLED", 10 * 1024 ** 3)
    )


# ============================================================
# クライアント本体
# ============================================================

# 証券コード→保有不動産のプロセス内キャッシュ。
# 業種分析が1銘柄ずつ引くN+1（最大165クエリ）で同じ銘柄を複数回
# 参照するため。業種分析は並列実行されるので lock で保護する。
_RE_HOLDINGS_CACHE: dict = {}
_RE_HOLDINGS_LOCK = threading.Lock()

# 最新期 financials の実行内キャッシュ（{company_code: dict}）。
# get_latest_financials / get_financials_single は同じSQLを銘柄ごとに
# 撃っており、1実行で 85本 × 2種 が走っていた（2026-09-10 課金実測）。
_FIN_LATEST_CACHE: dict = {}
_FIN_LATEST_LOCK = threading.Lock()

# 業種分析バンドルの実行内キャッシュ（{company_code: payload}）。
# get_company_analysis_bundle() は業種ごとに呼ばれるため33本走っていた。
_BUNDLE_CACHE: dict = {}
_BUNDLE_LOCK = threading.Lock()


def _fin_code(securities_code) -> str:
    """証券コードを financials.company_code の表記（数字4桁）へ揃える。"""
    return "".join(
        ch for ch in str(securities_code or "").split(".")[0] if ch.isdigit()
    )[:4]


class EDINETBQClient:
    """
    BigQueryクライアント（シングルトン設計）。
    GitHub Actions 環境では BQ_CREDENTIALS 環境変数（JSON文字列）を使用。
    Colab / ローカル環境では BQ_CREDENTIALS_PATH のファイルを使用。
    """

    _instance = None
    # 2026-09-09: 業種分析を並列化したため、初回生成が複数スレッドから
    # 同時に走りうる。lock が無いと _init_client()（認証・クライアント生成）が
    # 並列数だけ実行され、片方の初期化途中のインスタンスを掴む恐れがある。
    _init_lock = threading.Lock()

    def __new__(cls):
        with cls._init_lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                obj._initialized = False
                cls._instance = obj
            return cls._instance

    def __init__(self):
        with type(self)._init_lock:
            if self._initialized:
                return
            self._init_client()
            self._initialized = True

    def _init_client(self):
        """認証初期化"""
        bq_json = os.environ.get("BQ_CREDENTIALS", "")
        credential_project = ""
        if bq_json:
            # GitHub Actions: Secret から直接 JSON 文字列を読み込む
            info  = json.loads(bq_json)
            credential_project = str(info.get("project_id", "")).strip()
            creds = service_account.Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        else:
            # Colab / ローカル: ファイルから読み込む
            credential_path = str(BQ_CREDENTIALS_PATH or "").strip()
            if not credential_path:
                raise ValueError(
                    "BQ認証情報がありません。BQ_CREDENTIALS（JSON文字列）または"
                    "BQ_CREDENTIALS_PATH（JSONファイル）を設定してください"
                )
            try:
                with open(credential_path, encoding="utf-8") as f:
                    credential_project = str(
                        json.load(f).get("project_id", "")
                    ).strip()
            except (OSError, ValueError, TypeError):
                credential_project = ""
            creds = service_account.Credentials.from_service_account_file(
                credential_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

        project = str(BQ_PROJECT_ID or "").strip() or credential_project
        if not project:
            raise ValueError(
                "BQ_PROJECT_IDが未設定で、認証情報にもproject_idがありません"
            )
        self.project = project
        self.dataset = f"{project}.{BQ_DATASET}"

        # ── 2026-07-28: 暴走クエリの費用上限（maximum_bytes_billed）──────
        # 1クエリでスキャンできるバイト数に上限を設ける。超えるクエリは
        # 実行前に失敗し、課金は 0 になる（BigQuery の仕様）。
        # default_query_job_config に入れておくと、各所の
        # bigquery.QueryJobConfig(query_parameters=[...]) を書き換えなくても、
        # 未設定プロパティが既定値で補完されるため全クエリに効く。
        # 既定 10GB = 無料枠(月1TB)の約1%。本システムの最大テーブルが17MB
        # であることを踏まえた「事故だけを止める」水準。
        try:
            _max_bytes = int(BQ_MAX_BYTES_BILLED or 0)
        except (TypeError, ValueError):
            _max_bytes = 0
        if _max_bytes <= 0:
            _max_bytes = 10 * 1024 ** 3      # 10GB（0/未設定時のフォールバック）

        self.max_bytes_billed = _max_bytes
        _default_job_config = bigquery.QueryJobConfig(
            maximum_bytes_billed=_max_bytes
        )
        self.client = bigquery.Client(
            project=project,
            credentials=creds,
            default_query_job_config=_default_job_config,
        )

    # ----------------------------------------------------------
    # 書き込み系
    # ----------------------------------------------------------

    def insert_rows(self, table_name: str, rows: list[dict], dataset: str | None = None) -> bool:
        """
        行を挿入する。
        BigQuery はネイティブ upsert 非対応のため追記式。
        重複排除はクエリ側で ROW_NUMBER() / QUALIFY を使う。
        """
        if not rows:
            return True
        table_ref = f"{self.project}.{dataset or BQ_DATASET}.{table_name}"
        errors = self.client.insert_rows_json(table_ref, rows)
        if errors:
            print(f"[BQClient] insert_rows error ({table_name}): {errors}")
            return False
        return True

    def run_dml(self, sql: str) -> int:
        """
        UPDATE / DELETE 等の DML を実行し、影響行数を返す。
        """
        job    = self.client.query(sql)
        result = job.result()
        return result.num_dml_affected_rows or 0

    def merge_rows(
        self,
        table_name: str,
        rows: list[dict],
        key_columns: list[str],
        dataset: str = "edinet",
    ) -> bool:
        """実テーブルの列型に合わせ、JSON配列を主キーで冪等MERGEする。"""
        if not rows:
            return True
        identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        if not identifier.fullmatch(table_name) or not identifier.fullmatch(dataset):
            raise ValueError("dataset/table name must be a BigQuery identifier")
        table_ref = f"{self.project}.{dataset}.{table_name}"
        schema = list(self.client.get_table(table_ref).schema)
        schema_by_name = {field.name: field for field in schema}
        missing_keys = [key for key in key_columns if key not in schema_by_name]
        if missing_keys:
            raise ValueError(f"MERGE key columns missing from {table_ref}: {missing_keys}")
        if any(
            key not in row or row.get(key) is None or row.get(key) == ""
            for row in rows for key in key_columns
        ):
            raise ValueError(f"MERGE rows contain empty keys: {key_columns}")
        # BigQuery MERGEは同一source keyが複数あると既存行へのUPDATEで失敗し得る。
        # APIページ内の重複をここで吸収し、最後に観測した行を正本とする。
        deduplicated = {}
        for row in rows:
            deduplicated[tuple(row[key] for key in key_columns)] = row
        rows = list(deduplicated.values())
        columns = [field.name for field in schema if any(field.name in row for row in rows)]
        if not columns or any(key not in columns for key in key_columns):
            raise ValueError(f"MERGE rows do not contain all keys: {key_columns}")

        def source_expr(column: str) -> str:
            field_type = schema_by_name[column].field_type.upper()
            json_path = f"$.{column}"
            if field_type == "STRING":
                return f"JSON_VALUE(row, '{json_path}') AS `{column}`"
            if field_type == "JSON":
                return f"JSON_QUERY(row, '{json_path}') AS `{column}`"
            cast_type = {
                "INTEGER": "INT64", "INT64": "INT64",
                "FLOAT": "FLOAT64", "FLOAT64": "FLOAT64",
                "NUMERIC": "NUMERIC", "BIGNUMERIC": "BIGNUMERIC",
                "BOOLEAN": "BOOL", "BOOL": "BOOL", "DATE": "DATE",
                "DATETIME": "DATETIME", "TIMESTAMP": "TIMESTAMP",
            }.get(field_type)
            if not cast_type:
                raise ValueError(f"unsupported MERGE field type: {field_type} ({column})")
            return f"SAFE_CAST(JSON_VALUE(row, '{json_path}') AS {cast_type}) AS `{column}`"

        source_columns = ",\n            ".join(source_expr(column) for column in columns)
        on_clause = " AND ".join(f"T.`{key}` = S.`{key}`" for key in key_columns)
        update_columns = [column for column in columns if column not in key_columns]
        matched_clause = ""
        if update_columns:
            assignments = ",\n          ".join(
                f"T.`{column}` = S.`{column}`" for column in update_columns
            )
            matched_clause = f"WHEN MATCHED THEN UPDATE SET\n          {assignments}"
        insert_columns = ", ".join(f"`{column}`" for column in columns)
        insert_values = ", ".join(f"S.`{column}`" for column in columns)
        sql = f"""
        MERGE `{table_ref}` AS T
        USING (
          SELECT
            {source_columns}
          -- 2026-09-11: wide_number_mode => 'round' は必須。
          -- 既定の 'exact' は「FLOAT64/NUMERICへ正確に往復できない数値」が
          -- 1つでもあると **JSON文書ごと** 400 で弾く。実測エラー:
          --   Invalid input: Input number: 0.057161 cannot round-trip
          --   through string representation; error in PARSE_JSON expression
          -- ROEや利益率など小数5〜6桁の財務比率は日常的にこれに該当し、
          -- 2026-09-11 の実行では edinet.financials のMERGE 11社中6社が
          -- これで落ちていた（0.057161 / -0.137364 / 0.081948 /
          -- 60026484.18059 / 0.071943 / 0.07093）。
          -- 取り出しは SAFE_CAST(JSON_VALUE(...)) ＝文字列経由なので、
          -- 'round' にしても抽出値の精度は落ちない（実測で往復一致を確認）。
          FROM UNNEST(JSON_QUERY_ARRAY(
            PARSE_JSON(@rows_json, wide_number_mode => 'round')
          )) AS row
        ) AS S
        ON {on_clause}
        {matched_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_columns})
        VALUES ({insert_values})
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(
                "rows_json", "STRING", json.dumps(rows, ensure_ascii=False, default=str)
            )
        ])
        self.client.query(sql, job_config=job_config).result()
        return True

    def mark_doc_as_fetched(self, doc_id: str) -> int:
        """doc_index の fetched_flag を True に更新"""
        sql = f"""
        UPDATE `{self.dataset}.doc_index`
        SET fetched_flag = TRUE,
            updated_at   = CURRENT_TIMESTAMP()
        WHERE doc_id = '{doc_id}'
        """
        return self.run_dml(sql)

    # ----------------------------------------------------------
    # 読み込み系
    # ----------------------------------------------------------

    def get_company_taxonomy_scores(
        self,
        company_code: str,
        market: str = "JP",
        as_of_date: str = None
    ) -> list[dict]:
        """
        企業（または業種）の taxonomy ノード関与度を返す。
        [Wave R2-4 / T-3] 2026-07-10 空スタブを実装。

        優先順位（同一node_idに複数層がある場合）:
          verified_flag=TRUE > confidence_type='edinet'（T-2A・企業レベル）
          > 'cluster_derived'（T-2C・業種レベル叩き台）
        企業コードで見つからない場合は sector擬似企業ID
        （"sector:{market}:{code}"・T-2Cの規約）でフォールバック検索する。

        Returns: [
            {"node_id": "semiconductor_logic", "score": 0.6,
             "confidence_type": "edinet", "taxonomy_share": 0.3},
        ]（失敗・データなしは []）
        """
        try:
            code = str(company_code).replace("'", "")
            mkt = str(market).replace("'", "")
            date_cond = ""
            if as_of_date:
                date_cond = f"AND valid_from <= '{str(as_of_date)[:10]}'"
            sql = f"""
            SELECT node_id,
                   relevance_score       AS score,
                   confidence_type,
                   revenue_share_est     AS taxonomy_share
            FROM `{self.project}.taxonomy.company_taxonomy_map`
            WHERE company_id IN ('{code}', 'sector:{mkt}:{code}')
              {date_cond}
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY node_id
                ORDER BY
                  CASE WHEN verified_flag THEN 0
                       WHEN confidence_type = 'edinet' THEN 1
                       ELSE 2 END,
                  updated_at DESC
            ) = 1
            ORDER BY score DESC
            """
            df = self.query_to_df(sql)
            return [
                {"node_id": str(r["node_id"]),
                 "score": float(r["score"]) if r["score"] == r["score"] else 0.0,
                 "confidence_type": str(r["confidence_type"]),
                 "taxonomy_share": (float(r["taxonomy_share"])
                                    if r["taxonomy_share"] == r["taxonomy_share"]
                                    else 0.0)}
                for _, r in df.iterrows()
            ]
        except Exception as e:
            print(f"[BQClient] get_company_taxonomy_scores失敗 "
                  f"({company_code}): {str(e)[:80]}")
            return []

    def get_segment_finance(
        self,
        company_code: str,
        report_period: str = None
    ) -> list[dict]:
        """
        1セグメント1行のリストを返す（全期間・report_period降順）。

        2026-07-19実装: 従来は docstring のみのスタブ（幻のAPI）で、
        consumer（segment_signal）は常に空リストを受け取っていた。
        実テーブル edinet.segments（名称マスタ）× edinet.segment_finance（数値）
        を segment_id で結合して返す。

        Returns:
            [{"segment_name", "segment_name_norm", "report_period",
              "revenue", "revenue_ratio", "operating_margin"}, ...]
        """
        code = str(company_code).replace(".T", "").replace("'", "''").strip()
        if not code:
            return []
        pf = ""
        if report_period:
            pf = "AND sf.report_period = '{}'".format(
                str(report_period).replace("'", "''"))
        sql = f"""
        SELECT s.segment_name,
               s.segment_name_norm,
               sf.report_period,
               sf.revenue,
               sf.revenue_ratio,
               sf.operating_margin
        FROM `{self.dataset}.segments` s
        JOIN `{self.dataset}.segment_finance` sf
          ON sf.segment_id = s.segment_id
        WHERE s.company_code = '{code}' {pf}
        ORDER BY sf.report_period DESC
        """
        try:
            df = self.query_to_df(sql)
            return df.to_dict("records") if not df.empty else []
        except Exception as e:
            print(f"[BQClient] get_segment_finance失敗 "
                  f"({company_code}): {str(e)[:80]}")
            return []

    def get_geo_exposure(
        self,
        company_code: str,
        market: str = "JP"
    ) -> list[dict]:
        """企業の国別権益エクスポージャーを返す"""

    def get_country_risk(
        self,
        country_code: str,
        risk_date: str = None
    ) -> dict:
        """当日のcomposite_risk_scoreを返す"""

    def query_to_df(self, sql: str) -> pd.DataFrame:
        """SQL クエリ結果を DataFrame で返す"""
        # 2026-07-16是正: 2026-07-12にgoogle-cloud-bigquery-storageを導入した結果、
        # to_dataframe()がStorage Read API経由に切り替わり bigquery.readsessions.create
        # 権限（roles/bigquery.readSessionUser）を要求→SAに無く edinet_rotation が
        # 3日連続403で停止した。本システムの読取量ではREST経路で十分なため明示的に
        # Storage APIを使わない（権限付与に依存しない構成）。将来SAに
        # readSessionUser を付与すれば True に戻して高速化してよい。
        return self.client.query(sql).to_dataframe(create_bqstorage_client=False)

    def get_unfetched_docs(self, limit: int = 33) -> pd.DataFrame:
        """
        未取得（fetched_flag=False）の doc_index を返す。
        XBRL あり・有報/四半期のみ・提出日降順。
        """
        sql = f"""
        SELECT *
        FROM `{self.dataset}.doc_index`
        WHERE fetched_flag  = FALSE
          AND xbrl_available = TRUE
          AND ifrs_flag     = FALSE          -- E2 フェーズは日本基準のみ
          AND doc_type IN ('有価証券報告書', '四半期報告書')
        ORDER BY disclosure_date
        LIMIT {limit}
        """
        return self.query_to_df(sql)

    def get_fetched_submit_dates(self, days: int = 7) -> list[str]:
        """
        過去 N 日間で doc_index に登録済みの開示日(disclosure_date)を返す。
        doc_indexer の重複取得防止に使用。
        ※ 正本スキーマの列名は disclosure_date（旧コードの submit_date は存在せず
          "Unrecognized name: submit_date" で常時失敗していた）。
        """
        sql = f"""
        SELECT DISTINCT FORMAT_DATE('%Y-%m-%d', disclosure_date) AS d
        FROM `{self.dataset}.doc_index`
        WHERE disclosure_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)
        """
        df = self.query_to_df(sql)
        return df["d"].tolist() if not df.empty else []

    def get_latest_financials(self, securities_code: str) -> dict:
        """
        証券コードで最新期の財務データを取得する。
        base_agent.py（E2 統合時）から呼び出す想定。
        """
        # ★ financials 正本スキーマの列は company_code / fiscal_year_end。
        #   旧コードは securities_code / period_end を参照し
        #   "Unrecognized name: securities_code" で常時 400 だった。
        return dict(self._latest_financials_cached(securities_code))

    def get_company_analysis_bundle(
        self,
        tickers: list[str],
        *,
        max_companies: int = 2,
    ) -> dict[str, dict]:
        """BQ ``edinet.*`` の既存行だけで業種分析互換dictを作る。

        EdinetDB RESTは呼ばず、company / financials / text_analysis /
        doc_index を1本のパラメータ化クエリで参照する。
        """
        bounded = max(1, min(20, int(max_companies)))
        requested = self._normalize_bundle_targets(tickers, limit=bounded)
        if not requested:
            return {}

        # 2026-09-10: 業種ごとに呼ばれるため1実行で33本走り、課金実測では
        # この形だけで 1.77GB/実行を占めていた（BQは参照テーブル1つにつき
        # 最低10MB課金＝5テーブル参照で1本あたり50MB）。
        # 先読み済みならクエリを撃たず、未取得ぶんだけ引く。
        with _BUNDLE_LOCK:
            missing = [pair for pair in requested if pair[0] not in _BUNDLE_CACHE]
        if missing:
            self._query_bundles(missing)

        out = {}
        with _BUNDLE_LOCK:
            for code, ticker in requested:
                payload = _BUNDLE_CACHE.get(code)
                if payload:
                    out[ticker] = dict(payload, ticker=ticker)
        return out

    @staticmethod
    def _normalize_bundle_targets(tickers, limit: int | None = None) -> list:
        """[(4桁コード, ticker)] へ正規化する（重複排除・順序保持）。"""
        requested: list[tuple[str, str]] = []
        seen = set()
        for raw in (tickers or []):
            ticker = str(raw or "").strip()
            code = "".join(ch for ch in ticker.split(".")[0] if ch.isdigit())[:4]
            if len(code) != 4 or code in seen:
                continue
            seen.add(code)
            requested.append((code, ticker if "." in ticker else f"{code}.T"))
            if limit is not None and len(requested) >= limit:
                break
        return requested

    def prefetch_company_analysis_bundles(self, tickers) -> int:
        """全銘柄ぶんの業種分析バンドルを1クエリで先読みする。

        業種ごとの get_company_analysis_bundle() が33本のクエリを撃つのを
        1本へ畳むための入口。該当行が無いコードも空で埋めて、
        キャッシュミスによる個別クエリへの逆戻りを防ぐ。

        Returns: キャッシュへ載せた銘柄コード数
        """
        requested = self._normalize_bundle_targets(tickers)
        with _BUNDLE_LOCK:
            missing = [pair for pair in requested if pair[0] not in _BUNDLE_CACHE]
        if not missing:
            return 0
        try:
            self._query_bundles(missing)
        except Exception as exc:
            print(f"[BQ] prefetch_company_analysis_bundles skip: {str(exc)[:80]}")
            return 0
        return len(missing)

    def _query_bundles(self, requested: list) -> None:
        """[(code, ticker)] を1クエリで引き、_BUNDLE_CACHE を埋める。"""
        if not requested:
            return

        sql = f"""
        WITH requested AS (
          SELECT code FROM UNNEST(@company_codes) AS code
        ),
        company_latest AS (
          SELECT
            SUBSTR(c.sec_code, 1, 4) AS company_code,
            ARRAY_AGG(
              STRUCT(c.name AS company_name, c.credit_score AS credit_score)
              ORDER BY c.updated_at DESC LIMIT 1
            )[SAFE_OFFSET(0)] AS company
          FROM `{self.dataset}.company` AS c
          JOIN requested AS r ON SUBSTR(c.sec_code, 1, 4) = r.code
          GROUP BY company_code
        ),
        financial_latest AS (
          SELECT * EXCEPT(rn)
          FROM (
            SELECT f.*,
              ROW_NUMBER() OVER (
                PARTITION BY f.company_code, f.fiscal_year_end
                ORDER BY f.updated_at DESC
              ) AS rn
            FROM `{self.dataset}.financials` AS f
            JOIN requested AS r ON f.company_code = r.code
          )
          WHERE rn = 1
        ),
        financial_grouped AS (
          SELECT company_code,
            ARRAY_AGG(STRUCT(
              COALESCE(fiscal_year, EXTRACT(YEAR FROM fiscal_year_end)) AS fiscal_year,
              net_sales AS revenue,
              operating_income AS operating_income,
              net_income AS net_income,
              total_assets AS total_assets,
              equity AS equity,
              operating_cf AS cf_operating,
              investing_cf AS cf_investing,
              free_cf AS free_cf,
              capex AS capex
            ) ORDER BY fiscal_year_end DESC LIMIT 6) AS financials
          FROM financial_latest
          GROUP BY company_code
        ),
        text_latest AS (
          SELECT * EXCEPT(rn)
          FROM (
            SELECT t.*,
              ROW_NUMBER() OVER (
                PARTITION BY t.company_code, t.section_type
                ORDER BY t.fiscal_year_end DESC, t.updated_at DESC
              ) AS rn
            FROM `{self.dataset}.text_analysis` AS t
            JOIN requested AS r ON t.company_code = r.code
            WHERE t.raw_text IS NOT NULL
          )
          WHERE rn = 1
        ),
        text_grouped AS (
          SELECT company_code,
            ARRAY_AGG(STRUCT(section_type, raw_text) LIMIT 12) AS texts
          FROM text_latest
          GROUP BY company_code
        ),
        doc_latest AS (
          SELECT company_code,
            STRUCT(
              CAST(disclosure_date AS STRING) AS submit_date,
              CAST(fiscal_year_end AS STRING) AS period_end
            ) AS gov_meta
          FROM `{self.dataset}.doc_index` AS d
          JOIN requested AS r ON d.company_code = r.code
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY company_code ORDER BY disclosure_date DESC, updated_at DESC
          ) = 1
        )
        SELECT r.code AS company_code, c.company, f.financials, t.texts, d.gov_meta
        FROM requested AS r
        LEFT JOIN company_latest AS c ON c.company_code = r.code
        LEFT JOIN financial_grouped AS f ON f.company_code = r.code
        LEFT JOIN text_grouped AS t ON t.company_code = r.code
        LEFT JOIN doc_latest AS d ON d.company_code = r.code
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter(
                "company_codes", "STRING", [code for code, _ in requested]
            )
        ])

        def plain(value):
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            if isinstance(value, (list, tuple)):
                return [plain(item) for item in value]
            if isinstance(value, dict):
                return {key: plain(item) for key, item in value.items()}
            if hasattr(value, "items"):
                return {key: plain(item) for key, item in value.items()}
            return value

        def cagr(values: list) -> float | None:
            positive = [float(value) for value in values if value is not None and value > 0]
            if len(positive) < 4:
                return None
            start, end = positive[-4], positive[-1]
            return (end / start) ** (1 / 3) - 1

        ticker_by_code = dict(requested)
        # 該当行が無いコードも空で埋める（埋めないと毎回引き直しになる）
        bundles = {code: None for code, _ in requested}
        rows = self.client.query(sql, job_config=job_config).result()
        for row in rows:
            record = plain(row)
            code = str(record.get("company_code") or "")
            company = plain(record.get("company")) or {}
            financial_rows = list(reversed(plain(record.get("financials")) or []))
            texts = plain(record.get("texts")) or []
            gov_meta = plain(record.get("gov_meta")) or {}
            if not company and not financial_rows and not texts and not gov_meta:
                continue

            business_summary = ""
            risk_factors = []
            for section in texts:
                section_type = str(section.get("section_type") or "")
                raw_text = str(section.get("raw_text") or "").strip()
                lowered = section_type.casefold()
                if not business_summary and (
                    "事業の内容" in section_type or "business" in lowered
                ):
                    business_summary = raw_text[:1000]
                if "リスク" in section_type or "risk" in lowered:
                    points = [
                        part.strip(" 　・ー-")
                        for part in re.split(r"[\r\n]+|(?<=。)", raw_text)
                        if part.strip(" 　・ー-")
                    ]
                    risk_factors.extend(points[:5])

            years = [item.get("fiscal_year") for item in financial_rows]
            revenue = [item.get("revenue") for item in financial_rows]
            operating_income = [item.get("operating_income") for item in financial_rows]
            score = company.get("credit_score")
            rating = ""
            if score is not None:
                rating = "excellent" if score >= 80 else "good" if score >= 60 else (
                    "fair" if score >= 40 else "poor"
                )
            ticker = ticker_by_code.get(code, f"{code}.T")
            bundles[code] = {
                "ticker": ticker,
                "company_name": company.get("company_name") or ticker,
                "analysis": {
                    "credit_score": score,
                    "credit_rating": rating,
                    "business_summary": business_summary,
                    "risk_factors": risk_factors[:5],
                },
                "financials": {
                    "years": years,
                    "revenue": revenue,
                    "operating_income": operating_income,
                    "net_income": [item.get("net_income") for item in financial_rows],
                    "total_assets": [item.get("total_assets") for item in financial_rows],
                    "equity": [item.get("equity") for item in financial_rows],
                    "cf_operating": [item.get("cf_operating") for item in financial_rows],
                    "cf_investing": [item.get("cf_investing") for item in financial_rows],
                    "free_cf": [item.get("free_cf") for item in financial_rows],
                    "capex": [item.get("capex") for item in financial_rows],
                    "revenue_cagr": cagr(revenue),
                    "op_income_cagr": cagr(operating_income),
                },
                "gov_meta": gov_meta,
                "data_source": "bigquery_edinet",
            }
        with _BUNDLE_LOCK:
            for code, payload in bundles.items():
                _BUNDLE_CACHE.setdefault(code, payload)

    def get_real_estate_holdings(self, securities_code: str) -> list[dict]:
        """証券コードに対応する最新年度の企業保有不動産を返す。

        2026-09-09: 業種分析から1銘柄ずつ呼ばれるN+1（業種あたり最大5社×
        33業種＝最大165クエリ）で、そのたびに47,455行のテーブルを
        スキャンしていた。同一プロセス内では結果をキャッシュし、
        最新年度の抽出は相関サブクエリから QUALIFY へ置き換える
        （サブクエリ側のスキャンが1回分まるごと減る）。
        """
        code = "".join(
            char for char in str(securities_code).split(".")[0] if char.isdigit()
        )[:4]
        if not code:
            return []

        with _RE_HOLDINGS_LOCK:
            if code in _RE_HOLDINGS_CACHE:
                return list(_RE_HOLDINGS_CACHE[code])

        sql = f"""
        SELECT h.*
        FROM `{self.project}.real_estate.corporate_holdings_p1` AS h
        JOIN `{self.dataset}.company` AS c USING (edinet_code)
        WHERE SUBSTR(c.sec_code, 1, 4) = @company_code
        QUALIFY h.fiscal_year = MAX(h.fiscal_year) OVER (
            PARTITION BY h.edinet_code
        )
        ORDER BY h.facility_name
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("company_code", "STRING", code)
        ])
        try:
            rows = [dict(row.items()) for row in self.client.query(
                sql, job_config=job_config
            ).result()]
        except Exception as exc:
            print(f"[BQ] get_real_estate_holdings({code}) skip: {str(exc)[:80]}")
            return []

        with _RE_HOLDINGS_LOCK:
            _RE_HOLDINGS_CACHE[code] = rows
        return list(rows)

    def prefetch_real_estate_holdings(self, securities_codes) -> int:
        """複数銘柄の保有不動産を1クエリでまとめて取得しキャッシュへ載せる。

        2026-09-10: BigQuery の課金実績（INFORMATION_SCHEMA.JOBS）を見ると
        1実行あたり 482 クエリ / 6.9GB が課金されており、その内訳は
        「1銘柄1クエリ」の N+1 が4種で 340 本を占めていた。
        BQ は**参照テーブル1つにつき最低10MB**を課金するため、
        実データが0行でも 2テーブルJOIN なら1本あたり20MB取られる。
        つまりこの経路の費用は**データ量ではなくクエリ本数**で決まる。
        85銘柄ぶんを1本に畳むと 1.7GB/実行 → 0.02GB/実行 になる。

        取得できなかったコードにも空リストを入れる。入れないと
        get_real_estate_holdings() 側が毎回キャッシュミスして
        結局1銘柄1クエリに戻ってしまう。

        Returns: キャッシュへ載せた銘柄コード数
        """
        codes = []
        for raw in (securities_codes or []):
            code = "".join(
                ch for ch in str(raw).split(".")[0] if ch.isdigit()
            )[:4]
            if code:
                codes.append(code)
        codes = sorted(set(codes))
        if not codes:
            return 0

        with _RE_HOLDINGS_LOCK:
            todo = [c for c in codes if c not in _RE_HOLDINGS_CACHE]
        if not todo:
            return 0

        sql = f"""
        SELECT SUBSTR(c.sec_code, 1, 4) AS _company_code, h.*
        FROM `{self.project}.real_estate.corporate_holdings_p1` AS h
        JOIN `{self.dataset}.company` AS c USING (edinet_code)
        WHERE SUBSTR(c.sec_code, 1, 4) IN UNNEST(@codes)
        QUALIFY h.fiscal_year = MAX(h.fiscal_year) OVER (
            PARTITION BY h.edinet_code
        )
        ORDER BY _company_code, h.facility_name
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("codes", "STRING", todo)
        ])
        grouped: dict = {c: [] for c in todo}
        try:
            for row in self.client.query(sql, job_config=job_config).result():
                item = dict(row.items())
                code = item.pop("_company_code", None)
                if code in grouped:
                    grouped[code].append(item)
        except Exception as exc:
            # 失敗時はキャッシュを汚さない（個別取得のフォールバックへ委ねる）
            print(f"[BQ] prefetch_real_estate_holdings skip: {str(exc)[:80]}")
            return 0

        with _RE_HOLDINGS_LOCK:
            for code, rows in grouped.items():
                _RE_HOLDINGS_CACHE.setdefault(code, rows)
        return len(grouped)

    def _latest_financials_cached(self, securities_code) -> dict:
        """最新期 financials を実行内キャッシュ経由で返す（単体フォールバック）。"""
        code = _fin_code(securities_code)
        if not code:
            return {}
        with _FIN_LATEST_LOCK:
            if code in _FIN_LATEST_CACHE:
                return _FIN_LATEST_CACHE[code]

        sql = f"""
        SELECT * EXCEPT(rn)
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY company_code
                       ORDER BY fiscal_year_end DESC
                   ) AS rn
            FROM `{self.dataset}.financials`
            WHERE company_code = @company_code
        )
        WHERE rn = 1
        """
        try:
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("company_code", "STRING", code)
            ])
            rows = [dict(r.items())
                    for r in self.client.query(sql, job_config=job_config).result()]
        except Exception as exc:
            print(f"[BQ] get_latest_financials({code}) skip: {str(exc)[:80]}")
            return {}

        row = rows[0] if rows else {}
        with _FIN_LATEST_LOCK:
            _FIN_LATEST_CACHE.setdefault(code, row)
        return row

    def prefetch_latest_financials(self, securities_codes) -> int:
        """複数銘柄の最新期 financials を1クエリで先読みしてキャッシュへ載せる。

        2026-09-10: get_latest_financials / get_financials_single が
        銘柄ごとに同じSQLを撃っており、課金実測では
        「PARTITION BY company_code」の形が2日で170本（85本/実行）走っていた。
        BQ は参照テーブル1つにつき最低10MB課金のため、
        financials が 0.3MB でも1本あたり10MBが課金される。
        該当なしの銘柄も空dictで埋めて、キャッシュミスによる
        単体クエリへの逆戻りを防ぐ。

        Returns: キャッシュへ載せた銘柄コード数
        """
        codes = sorted({c for c in (_fin_code(x) for x in (securities_codes or []))
                        if c})
        if not codes:
            return 0
        with _FIN_LATEST_LOCK:
            todo = [c for c in codes if c not in _FIN_LATEST_CACHE]
        if not todo:
            return 0

        sql = f"""
        SELECT * EXCEPT(rn)
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY company_code
                       ORDER BY fiscal_year_end DESC
                   ) AS rn
            FROM `{self.dataset}.financials`
            WHERE company_code IN UNNEST(@codes)
        )
        WHERE rn = 1
        """
        grouped: dict = {c: {} for c in todo}
        try:
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("codes", "STRING", todo)
            ])
            for row in self.client.query(sql, job_config=job_config).result():
                item = dict(row.items())
                code = _fin_code(item.get("company_code"))
                if code in grouped:
                    grouped[code] = item
        except Exception as exc:
            print(f"[BQ] prefetch_latest_financials skip: {str(exc)[:80]}")
            return 0

        with _FIN_LATEST_LOCK:
            for code, row in grouped.items():
                _FIN_LATEST_CACHE.setdefault(code, row)
        return len(grouped)

    def get_segment_bonus(self, securities_code: str) -> float:
        """
        セグメント収益性ボーナスを返す（E3 統合後に実装が充実する）。
        現時点では 0.0 を返すスタブ。
        """
        # E3 完了後に segment_signal.py の計算結果を参照する実装に差し替える
        return 0.0

    def get_text_risk_bonus(self, securities_code: str) -> float:
        """
        テキストリスクボーナスを返す（E4 統合後に実装が充実する）。
        現時点では 0.0 を返すスタブ。
        """
        # E4 完了後に text_signal.py の計算結果を参照する実装に差し替える
        return 0.0

    def get_tickers_by_sector_bulk(self, limit: int = 10) -> dict:
        """
        全業種の ticker を1クエリでまとめて返す（{sector_code: [ticker, ...]}）。

        業種ごとに get_tickers_for_sector() を呼ぶと33本のクエリが走り、
        実測で約5分・BQ課金も33回分かかっていた（2026-08-27）。
        件数を絞る順序は ORDER BY ticker で固定する。元の LIMIT だけの
        SQL は順序が未定義で、日によって選ばれる銘柄が変わりうるため。
        """
        lim = max(1, min(int(limit), 20))
        sql = f"""
        SELECT sector_code, ticker
        FROM (
            SELECT sector_code, ticker,
                   ROW_NUMBER() OVER (
                       PARTITION BY sector_code ORDER BY ticker
                   ) AS rn
            FROM (
                SELECT DISTINCT
                       CAST(sector_code AS STRING) AS sector_code,
                       ticker
                FROM `{self.dataset}.ticker_subtheme_mapping`
                WHERE ticker IS NOT NULL
                  AND sector_code IS NOT NULL
                  AND NOT STARTS_WITH(ticker, 'SECTOR_')
            )
        )
        WHERE rn <= {lim}
        """
        out: dict = {}
        try:
            df = self.query_to_df(sql)
            if df is None or df.empty:
                return {}
            if "sector_code" not in df.columns or "ticker" not in df.columns:
                return {}
            for code_raw, ticker_raw in zip(df["sector_code"], df["ticker"]):
                code = str(code_raw or "").strip()
                t    = str(ticker_raw or "").strip()
                if not code or not t or t.upper().startswith("SECTOR_"):
                    continue
                out.setdefault(code, []).append(
                    t if t.endswith(".T") else f"{t}.T"
                )
        except Exception as e:
            print(f"[BQ] get_tickers_by_sector_bulk skip: {str(e)[:80]}")
            return {}
        return out

    def get_tickers_for_sector(self, sector_code: str, limit: int = 5) -> list[str]:
        """
        業種コードに属する ticker リストを返す。
        優先: BQ ticker_subtheme_mapping → 空なら呼び出し元でフォールバック。
        """
        code = str(sector_code).replace("'", "''")
        lim = max(1, min(int(limit), 20))

        # Wave 6 R-2: テーブルが存在する環境のみ有効。
        # ★ "SECTOR_<code>" は sub_theme_master 同期時に入る業種レベルの
        #   プレースホルダー（実銘柄ではない）。これを実ティッカーとして
        #   返すと yfinance 等が解決不能になり、かつ Tier2/3 フォールバックを
        #   塞いでしまうため SQL とループの両方で除外する。
        sql = f"""
        SELECT DISTINCT ticker
        FROM `{self.dataset}.ticker_subtheme_mapping`
        WHERE sector_code = '{code}'
          AND ticker IS NOT NULL
          AND NOT STARTS_WITH(ticker, 'SECTOR_')
        LIMIT {lim}
        """
        try:
            df = self.query_to_df(sql)
            if df is not None and not df.empty and "ticker" in df.columns:
                out = []
                for raw in df["ticker"].tolist():
                    t = str(raw).strip()
                    if not t or t.upper().startswith("SECTOR_"):
                        continue
                    out.append(t if t.endswith(".T") else f"{t}.T")
                if out:
                    return out[:lim]
        except Exception as e:
            print(f"[BQ] get_tickers_for_sector({sector_code}) skip: {str(e)[:80]}")

        return []
        
    def get_financials_for_sector(self, sector_code: str) -> pd.DataFrame:
        """
        33業種コードに属する全銘柄の最新期財務データを返す。
        base_agent.py の edinet_hr_bonus 計算で使用する。
        sector_code: 例 "1016"（電気機器）
        """
        tickers = self.get_tickers_for_sector(sector_code, limit=50)
        if not tickers:
            return pd.DataFrame()
        codes = []
        for t in tickers:
            c = str(t).replace(".T", "").replace("'", "''")
            if c:
                codes.append(f"'{c}'")
        if not codes:
            return pd.DataFrame()
        in_clause = ", ".join(codes)
        # ★ 正本スキーマの列名へ是正（旧: securities_code/period_end/num_employees/
        #   avg_salary/total_director_remuneration/max_director_remuneration は
        #   いずれも存在せず 400 になっていた）。出力キーは従来名のままにするため
        #   AS で別名を付け、消費側（base_agent 等）を変更不要にする。
        #   併せて、組み立て済みなのに未使用だった対象銘柄フィルタ(in_clause)を適用。
        sql = f"""
        WITH latest AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY company_code
                       ORDER BY fiscal_year_end DESC
                   ) AS rn
            FROM `{self.dataset}.financials`
            WHERE company_code IN ({in_clause})
              AND net_sales IS NOT NULL
        )
        SELECT
            company_code         AS securities_code,
            fiscal_year_end      AS period_end,
            net_sales,
            operating_income,
            employee_count       AS num_employees,
            avg_annual_salary    AS avg_salary,
            total_director_comp  AS total_director_remuneration,
            max_single_comp      AS max_director_remuneration,
            operating_cf,
            free_cf,
            capex
        FROM latest
        WHERE rn = 1
        """
        return self.query_to_df(sql)

    def get_financials_single(self, securities_code: str) -> dict:
        """
        1銘柄の最新期財務データを dict で返す。
        base_agent.py の個別銘柄スコアリングで使用する。
        """
        # ★ 正本列名 company_code / fiscal_year_end へ是正（旧 securities_code /
        #   period_end は存在せず 400 エラーだった）。
        return dict(self._latest_financials_cached(securities_code))

    # ----------------------------------------------------------
    # I-2: NEDO採択情報 / policy_support テーブル（Wave 1）
    # ----------------------------------------------------------

    def ensure_policy_support_table(self) -> bool:
        """
        BigQuery に `edinet.policy_support` テーブルが存在しなければ作成する。

        テーブルスキーマ:
          securities_code  STRING     証券コード（4桁）/ 名寄せ失敗時は ""
          company_name     STRING     名寄せ後の正式社名
          raw_company_name STRING     採択ページ記載の原文社名
          project_name     STRING     採択事業名（最大200文字）
          adoption_date    STRING     採択日（YYYY-MM-DD）
          project_type     STRING     事業種別
                                        green_innovation / energy / ai_iot /
                                        medical / robotics / moonshot /
                                        materials / general
          support_org      STRING     支援機関（NEDO / JST 等）
          support_amount_m FLOAT64    補助金額（百万円）/ 非開示時は NULL
          source_url       STRING     出典URL
          scraped_at       TIMESTAMP  スクレイプ日時（UTC）
          match_confidence FLOAT64    名寄せ確信度（0.0〜1.0）
          match_method     STRING     exact / partial / fuzzy / unmatched

        Returns:
            bool: True=成功 or テーブル既存, False=DDL失敗
        """
        ddl = f"""
        CREATE TABLE IF NOT EXISTS `{self.dataset}.policy_support` (
            securities_code  STRING
                OPTIONS(description='証券コード（4桁）名寄せ失敗時は空文字'),
            company_name     STRING
                OPTIONS(description='名寄せ後の正式社名'),
            raw_company_name STRING
                OPTIONS(description='採択ページ記載の原文社名'),
            project_name     STRING
                OPTIONS(description='採択事業名（最大200文字）'),
            adoption_date    STRING
                OPTIONS(description='採択日（YYYY-MM-DD形式）'),
            project_type     STRING
                OPTIONS(description='事業種別: green_innovation/energy/ai_iot等'),
            support_org      STRING
                OPTIONS(description='支援機関: NEDO / JST 等'),
            support_amount_m FLOAT64
                OPTIONS(description='補助金額（百万円）非開示時はNULL'),
            source_url       STRING
                OPTIONS(description='出典URL'),
            scraped_at       TIMESTAMP
                OPTIONS(description='スクレイプ日時 UTC'),
            match_confidence FLOAT64
                OPTIONS(description='名寄せ確信度 0.0〜1.0'),
            match_method     STRING
                OPTIONS(description='exact/partial/fuzzy/unmatched')
        )
        OPTIONS(
            description='NEDO等政策支援採択情報（Wave 1 - I-2）',
            labels=[("wave", "1"), ("source", "nedo")]
        )
        """
        try:
            job = self.client.query(ddl)
            job.result()
            print("[BQClient] policy_support テーブル確認/作成 OK")
            return True
        except Exception as e:
            print(f"[BQClient] policy_support テーブル作成失敗: {e}")
            return False

    def insert_policy_support_rows(self, rows: list[dict]) -> bool:
        """
        `edinet.policy_support` テーブルに採択情報を一括挿入する。
        nedo_selection_scraper.run_nedo_selection_scraper() の呼び出し元。

        重複排除はクエリ側（QUALIFY / ROW_NUMBER）で行うため、
        同一レコードを複数回 INSERT しても整合性は保たれる。

        Args:
            rows: nedo_selection_scraper._build_bq_rows() の返り値

        Returns:
            bool: True=全行挿入成功, False=一部または全部失敗
        """
        if not rows:
            return True
        try:
            return self.insert_rows("policy_support", rows)
        except Exception as e:
            print(f"[BQClient] policy_support INSERT 失敗: {e}")
            return False

    def get_policy_support_adoptions(
        self,
        securities_code: str,
        days_back:       int   = 1095,    # デフォルト3年
        min_confidence:  float = 0.75,
    ) -> list[dict]:
        """
        指定証券コードの政策支援採択情報を返す。
        base_agent.py の get_policy_support_bonus() から呼ばれる。

        Args:
            securities_code: 証券コード（4桁文字列）
            days_back:       遡及日数（デフォルト1095日=3年）
            min_confidence:  名寄せ確信度の最低閾値（デフォルト0.75）

        Returns:
            [
              {
                "securities_code":  str,
                "project_name":     str,
                "adoption_date":    str,
                "project_type":     str,
                "support_amount_m": float|None,
                "match_confidence": float,
                "scraped_at":       str,
              }, ...
            ]
            エラー時は空リストを返す（例外を外に投げない）
        """
        try:
            code_safe = str(securities_code).replace("'", "''")
            sql = f"""
            SELECT
                securities_code,
                company_name,
                project_name,
                adoption_date,
                project_type,
                support_amount_m,
                match_confidence,
                CAST(scraped_at AS STRING) AS scraped_at
            FROM `{self.dataset}.policy_support`
            WHERE securities_code   = '{code_safe}'
              AND match_confidence  >= {min_confidence}
              AND (
                    adoption_date IS NULL
                    OR adoption_date = ''
                    OR SAFE.PARSE_DATE('%Y-%m-%d', adoption_date)
                       >= DATE_SUB(CURRENT_DATE(), INTERVAL {days_back} DAY)
                  )
            ORDER BY adoption_date DESC
            LIMIT 50
            """
            df = self.query_to_df(sql)
            if df.empty:
                return []
            return df.to_dict("records")
        except Exception as e:
            print(
                f"[BQClient] get_policy_support_adoptions 失敗 "
                f"({securities_code}): {e}"
            )
            return []

    def get_policy_support_bonus(
        self,
        securities_code: str,
        max_bonus:       float = 15.0,
    ) -> float:
        """
        政策支援採択情報から base_agent.calc_composite_score() への
        加算ボーナスを計算して返す（Wave 1 - I-2）。

        スコアリング式（採択1件あたり）:
          $bonus\\_per\\_adoption = base\\_pt \times confidence \times recency\\_factor
                                  \times type\\_multiplier$
          最終ボーナス = min(max_bonus, 全採択の合計)

        事業種別 type_multiplier:
          green_innovation: 1.5  / ai_iot: 1.4 / energy: 1.3
          medical: 1.3          / robotics: 1.2 / moonshot: 1.2
          materials: 1.1        / general: 1.0

        recency_factor（採択日から現在までの経過日数）:
          <= 365日:  1.0（直近1年）
          <= 730日:  0.7（1〜2年）
          <= 1095日: 0.5（2〜3年）
          > 1095日:  0.3

        Args:
            securities_code: 証券コード（4桁文字列）
            max_bonus:       最大ボーナス上限（デフォルト15.0pt）

        Returns:
            float: 政策支援ボーナス（0.0〜max_bonus）
                   BQ接続失敗・採択情報なしの場合は 0.0
        """
        _TYPE_MULTIPLIER: dict = {
            "green_innovation": 1.5,
            "ai_iot":           1.4,
            "energy":           1.3,
            "medical":          1.3,
            "robotics":         1.2,
            "moonshot":         1.2,
            "materials":        1.1,
            "general":          1.0,
        }
        _BASE_PT_PER_ADOPTION = 5.0

        try:
            adoptions = self.get_policy_support_adoptions(securities_code)
            if not adoptions:
                return 0.0

            today      = datetime.now(timezone.utc).date()
            total_bonus = 0.0

            for adoption in adoptions:
                try:
                    confidence = float(adoption.get("match_confidence", 0.75))
                    project_type = str(adoption.get("project_type", "general") or "general")
                    type_mult  = _TYPE_MULTIPLIER.get(project_type, 1.0)

                    # 採択日から recency_factor を計算
                    adoption_date_str = str(adoption.get("adoption_date", "") or "")
                    recency_factor = 0.5   # デフォルト: 日付不明は中間値
                    if adoption_date_str and len(adoption_date_str) == 10:
                        from datetime import date as _date
                        try:
                            adop_dt   = _date.fromisoformat(adoption_date_str)
                            days_past = (today - adop_dt).days
                            if days_past <= 365:
                                recency_factor = 1.0
                            elif days_past <= 730:
                                recency_factor = 0.7
                            elif days_past <= 1095:
                                recency_factor = 0.5
                            else:
                                recency_factor = 0.3
                        except (ValueError, TypeError):
                            pass

                    # $bonus\_per = base\_pt \times confidence
                    #               \times recency\_factor \times type\_mult$
                    bonus_per = (
                        _BASE_PT_PER_ADOPTION
                        * confidence
                        * recency_factor
                        * type_mult
                    )
                    total_bonus += bonus_per

                except Exception:
                    continue

            result = round(min(max_bonus, total_bonus), 2)

            if result > 0:
                print(
                    f"    🏛️  政策支援ボーナス [{securities_code}]: "
                    f"+{result:.2f}pt "
                    f"({len(adoptions)}件採択)"
                )
            return result

        except Exception as e:
            print(
                f"    ⚠️  get_policy_support_bonus 失敗 "
                f"({securities_code}): {str(e)[:60]}"
            )
            return 0.0

    def get_xbrl_text_sections(self, securities_code: str) -> list[dict]:
        """EdinetDB由来の最新raw textをtext_extractor互換形で返す。"""
        code = "".join(
            char for char in str(securities_code).split(".")[0] if char.isdigit()
        )[:4]
        if not code:
            return []
        sql = f"""
        SELECT
          section_type AS element_name,
          raw_text AS text_value,
          CAST(fiscal_year_end AS STRING) AS period_end
        FROM `{self.dataset}.text_analysis`
        WHERE company_code = @company_code
          AND raw_text IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY section_type
          ORDER BY fiscal_year_end DESC, updated_at DESC
        ) = 1
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("company_code", "STRING", code)
        ])
        try:
            return [dict(row.items()) for row in self.client.query(
                sql, job_config=job_config
            ).result()]
        except Exception:
            return []

    def search_company_text(
        self,
        securities_code: str,
        query: str,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """BQへ保存した有報全文を、個社内キーワードで安全に検索する。"""
        code = "".join(
            char for char in str(securities_code).split(".")[0] if char.isdigit()
        )[:4]
        keyword = str(query or "").strip()
        if not code or not keyword:
            return []
        bounded_limit = max(1, min(200, int(limit)))
        sql = f"""
        SELECT section_type, fiscal_year_end, raw_text, updated_at
        FROM `{self.dataset}.text_analysis`
        WHERE company_code = @company_code
          AND raw_text IS NOT NULL
          AND STRPOS(
            NORMALIZE_AND_CASEFOLD(raw_text),
            NORMALIZE_AND_CASEFOLD(@query)
          ) > 0
        ORDER BY fiscal_year_end DESC, section_type
        LIMIT @limit
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("company_code", "STRING", code),
            bigquery.ScalarQueryParameter("query", "STRING", keyword),
            bigquery.ScalarQueryParameter("limit", "INT64", bounded_limit),
        ])
        try:
            return [dict(row.items()) for row in self.client.query(
                sql, job_config=job_config
            ).result()]
        except Exception as exc:
            print(f"[BQ] search_company_text({code}) skip: {str(exc)[:80]}")
            return []

# ----------------------------------------------------------
    # Wave 6 - R-2: sub_theme_master / ticker_subtheme_mapping
    # ----------------------------------------------------------

    def get_sub_theme_master(
        self,
        market: str = "JP",
        sector_code: str = None,
    ) -> list[dict]:
        """
        edinet.sub_theme_master から業種×サブテーママスターを取得する。

        テーブル構造:
          sector_code   STRING  業種コード（JP: "1016" / US: "US_45"）
          market        STRING  "JP" | "US"
          name_ja       STRING  業種名
          supply_chain_position      STRING
          business_cycle_sensitivity STRING
          export_dependency          STRING
          digital_transformation_stage STRING
          esg_priority               STRING
          bm_type                    STRING  nullable
          subthemes                  JSON    サブテーマリスト（JSON配列文字列）
          version                    STRING  JSONバージョン
          updated_at                 TIMESTAMP

        Args:
            market:      "JP" or "US"（必須フィルター）
            sector_code: 指定時は1業種のみ返す。None の場合は全件返す。

        Returns:
            [
              {
                "sector_code": "1016",
                "market":      "JP",
                "name_ja":     "電気機器",
                "supply_chain_position": "downstream",
                "subthemes":   ["AI半導体", "パワー半導体（EV）", ...],
                ...
              },
              ...
            ]
            ※ BQ未接続・テーブル未存在時は空リスト
        """
        try:
            mkt = str(market).replace("'", "''")
            where_clauses = [f"market = '{mkt}'"]
            if sector_code is not None:
                sc  = str(sector_code).replace("'", "''")
                where_clauses.append(f"sector_code = '{sc}'")

            where_sql = " AND ".join(where_clauses)

            sql = f"""
            SELECT
                sector_code,
                market,
                name_ja,
                supply_chain_position,
                business_cycle_sensitivity,
                export_dependency,
                digital_transformation_stage,
                esg_priority,
                bm_type,
                subthemes,
                version,
                updated_at
            FROM `{self.dataset}.sub_theme_master`
            WHERE {where_sql}
            ORDER BY sector_code
            """
            df = self.query_to_df(sql)
            if df is None or df.empty:
                return []

            rows = []
            for _, row in df.iterrows():
                r = row.to_dict()
                # subthemes カラムは JSON 配列文字列として保存されているため parse する
                try:
                    raw_sub = r.get("subthemes") or "[]"
                    if isinstance(raw_sub, str):
                        r["subthemes"] = json.loads(raw_sub)
                    elif not isinstance(raw_sub, list):
                        r["subthemes"] = []
                except Exception:
                    r["subthemes"] = []
                rows.append(r)

            return rows

        except Exception as e:
            print(
                f"  [BQClient] get_sub_theme_master 失敗 "
                f"(market={market}): {str(e)[:80]}"
            )
            return []

    def upsert_sub_theme_master(self, rows: list[dict]) -> bool:
        """
        edinet.sub_theme_master へ MERGE（アップサート）する。

        MERGE キー: sector_code + market
        一致した場合は全カラムを UPDATE。
        不一致の場合は INSERT。

        Args:
            rows: [
              {
                "sector_code":   "1016",
                "market":        "JP",
                "name_ja":       "電気機器",
                "supply_chain_position":       "downstream",
                "business_cycle_sensitivity":  "growth",
                "export_dependency":           "high",
                "digital_transformation_stage": "leader",
                "esg_priority":  "balanced",
                "bm_type":       None,
                "subthemes":     ["AI半導体", ...],   # list → JSON文字列に変換
                "version":       "1.0",
              },
              ...
            ]

        Returns:
            bool: True=成功（一部失敗でも True） / False=全件失敗
        """
        if not rows:
            return True

        try:
            now_ts = self.now_iso()
            success_count = 0

            for row in rows:
                try:
                    sc      = str(row.get("sector_code", "")).replace("'", "''")
                    mkt     = str(row.get("market", "JP")).replace("'", "''")
                    nm_ja   = str(row.get("name_ja", "")).replace("'", "''")
                    sc_pos  = str(row.get("supply_chain_position", "")).replace("'", "''")
                    bc_sens = str(row.get("business_cycle_sensitivity", "")).replace("'", "''")
                    exp_dep = str(row.get("export_dependency", "")).replace("'", "''")
                    dt_stg  = str(row.get("digital_transformation_stage", "")).replace("'", "''")
                    esg_pri = str(row.get("esg_priority", "")).replace("'", "''")
                    bm_type_val = row.get("bm_type")
                    # ★ bm_type が None のとき素の NULL を書くと BQ は USING 句で
                    #   INT64 NULL と推論し「INT64 cannot be assigned to bm_type
                    #   (STRING)」で全行失敗する。明示的に STRING 型の NULL を渡す。
                    #   値ありの場合は文字列化して引用（JSON 側が int でも STRING 化）。
                    bm_str  = (
                        f"'{str(bm_type_val).replace(chr(39), chr(39)*2)}'"
                        if bm_type_val is not None
                        else "CAST(NULL AS STRING)"
                    )

                    # subthemes は list → JSON 文字列に変換してエスケープ
                    subthemes_raw = row.get("subthemes", [])
                    if isinstance(subthemes_raw, list):
                        subthemes_json = json.dumps(
                            subthemes_raw, ensure_ascii=False
                        ).replace("'", "''")
                    else:
                        subthemes_json = str(subthemes_raw).replace("'", "''")

                    ver = str(row.get("version", "1.0")).replace("'", "''")

                    sql = f"""
                    MERGE `{self.dataset}.sub_theme_master` AS T
                    USING (
                        SELECT
                            '{sc}'       AS sector_code,
                            '{mkt}'      AS market,
                            '{nm_ja}'    AS name_ja,
                            '{sc_pos}'   AS supply_chain_position,
                            '{bc_sens}'  AS business_cycle_sensitivity,
                            '{exp_dep}'  AS export_dependency,
                            '{dt_stg}'   AS digital_transformation_stage,
                            '{esg_pri}'  AS esg_priority,
                            {bm_str}     AS bm_type,
                            PARSE_JSON('{subthemes_json}') AS subthemes,
                            '{ver}'      AS version,
                            TIMESTAMP('{now_ts}') AS updated_at
                    ) AS S
                    ON T.sector_code = S.sector_code
                   AND T.market      = S.market
                    WHEN MATCHED THEN UPDATE SET
                        name_ja                       = S.name_ja,
                        supply_chain_position         = S.supply_chain_position,
                        business_cycle_sensitivity    = S.business_cycle_sensitivity,
                        export_dependency             = S.export_dependency,
                        digital_transformation_stage  = S.digital_transformation_stage,
                        esg_priority                  = S.esg_priority,
                        bm_type                       = S.bm_type,
                        subthemes                     = S.subthemes,
                        version                       = S.version,
                        updated_at                    = S.updated_at
                    WHEN NOT MATCHED THEN INSERT (
                        sector_code,
                        market,
                        name_ja,
                        supply_chain_position,
                        business_cycle_sensitivity,
                        export_dependency,
                        digital_transformation_stage,
                        esg_priority,
                        bm_type,
                        subthemes,
                        version,
                        updated_at
                    ) VALUES (
                        S.sector_code,
                        S.market,
                        S.name_ja,
                        S.supply_chain_position,
                        S.business_cycle_sensitivity,
                        S.export_dependency,
                        S.digital_transformation_stage,
                        S.esg_priority,
                        S.bm_type,
                        S.subthemes,
                        S.version,
                        S.updated_at
                    )
                    """
                    affected = self.run_dml(sql)
                    success_count += 1

                except Exception as _row_e:
                    print(
                        f"  [BQClient] upsert_sub_theme_master 1行失敗 "
                        f"(sector_code={row.get('sector_code','')}): "
                        f"{str(_row_e)[:80]}"
                    )
                    continue

            print(
                f"  [BQClient] ✅ upsert_sub_theme_master: "
                f"{success_count}/{len(rows)}件 MERGE 完了"
            )
            return success_count > 0

        except Exception as e:
            print(
                f"  [BQClient] upsert_sub_theme_master 全件失敗: "
                f"{str(e)[:80]}"
            )
            return False

    def get_ticker_subtheme_mapping(
        self,
        sector_code: str = None,
        market:      str = "JP",
        limit:       int = 500,
    ) -> list[dict]:
        """
        edinet.ticker_subtheme_mapping から銘柄×サブテーママッピングを SELECT する。

        テーブル構造:
          ticker        STRING  ティッカー（JP: "7203.T" / US: "AAPL"）
          sector_code   STRING  業種コード
          market        STRING  "JP" | "US"
          subtheme      STRING  個別サブテーマ文字列
          relevance     FLOAT64 関連度スコア（0.0〜1.0）
          source        STRING  "json_sync" | "manual" | "auto_derived"
          updated_at    TIMESTAMP

        Args:
            sector_code: フィルター業種コード（None=全件）
            market:      "JP" or "US"
            limit:       最大取得件数

        Returns:
            [{"ticker": str, "sector_code": str, "subtheme": str, ...}, ...]
        """
        try:
            mkt = str(market).replace("'", "''")
            lim = max(1, min(int(limit), 5000))
            where_clauses = [f"market = '{mkt}'"]
            if sector_code is not None:
                sc  = str(sector_code).replace("'", "''")
                where_clauses.append(f"sector_code = '{sc}'")

            where_sql = " AND ".join(where_clauses)

            sql = f"""
            SELECT
                ticker,
                sector_code,
                market,
                subtheme,
                relevance,
                source,
                updated_at
            FROM `{self.dataset}.ticker_subtheme_mapping`
            WHERE {where_sql}
            ORDER BY sector_code, relevance DESC
            LIMIT {lim}
            """
            df = self.query_to_df(sql)
            if df is None or df.empty:
                return []

            return df.to_dict(orient="records")

        except Exception as e:
            print(
                f"  [BQClient] get_ticker_subtheme_mapping 失敗 "
                f"(sector_code={sector_code}): {str(e)[:80]}"
            )
            return []

    def upsert_ticker_subtheme_mapping(self, rows: list[dict]) -> bool:
        """
        edinet.ticker_subtheme_mapping へ MERGE（アップサート）する。

        MERGE キー: ticker + sector_code + market + subtheme

        Args:
            rows: [
              {
                "ticker":       "6758.T",
                "sector_code":  "1016",
                "market":       "JP",
                "subtheme":     "AI半導体",
                "relevance":    0.85,
                "source":       "json_sync",
              },
              ...
            ]

        Returns:
            bool: True=1件以上成功 / False=全件失敗
        """
        if not rows:
            return True

        try:
            now_ts = self.now_iso()
            # streaming insert（insert_rows_json）でバルク書込
            # ★ self.dataset は既に "project.dataset" 形式なので、さらに
            #   self.project を前置すると "proj.proj.dataset.table" の4階層になり
            #   404（テーブル不在）になっていた。dataset.table の3階層にする。
            table_ref = f"{self.dataset}.ticker_subtheme_mapping"

            # updated_at を全行に付加
            enriched = []
            for r in rows:
                rec = {
                    "ticker":      str(r.get("ticker",      "") or ""),
                    "sector_code": str(r.get("sector_code", "") or ""),
                    "market":      str(r.get("market",      "JP") or "JP"),
                    "subtheme":    str(r.get("subtheme",    "") or ""),
                    "relevance":   float(r.get("relevance", 0.0) or 0.0),
                    "source":      str(r.get("source",      "json_sync") or "json_sync"),
                    "updated_at":  now_ts,
                }
                # 必須キーの空チェック
                if not rec["ticker"] or not rec["sector_code"] or not rec["subtheme"]:
                    continue
                enriched.append(rec)

            if not enriched:
                print("  [BQClient] upsert_ticker_subtheme_mapping: 有効行なし")
                return True

            # バッチサイズ 500 件に分割して書込（BQ streaming 制限対策）
            batch_size = 500
            total_errors = 0
            for i in range(0, len(enriched), batch_size):
                batch  = enriched[i: i + batch_size]
                errors = self.client.insert_rows_json(table_ref, batch)
                if errors:
                    total_errors += len(errors)
                    print(
                        f"  [BQClient] ticker_subtheme_mapping insert エラー "
                        f"({len(errors)}件): {str(errors[0])[:80]}"
                    )

            written = len(enriched) - total_errors
            print(
                f"  [BQClient] ✅ upsert_ticker_subtheme_mapping: "
                f"{written}/{len(enriched)}行 書込完了"
            )
            return written > 0

        except Exception as e:
            print(
                f"  [BQClient] upsert_ticker_subtheme_mapping 全件失敗: "
                f"{str(e)[:80]}"
            )
            return False

    def sync_subtheme_json_to_bq(
        self,
        sectors:     dict,
        market:      str = "JP",
        version:     str = "1.0",
    ) -> dict:
        """
        load_all_subthemes() の返り値（sectors dict）を BQ の
        sub_theme_master / ticker_subtheme_mapping の両テーブルに同期する。

        subtheme_loader.sync_json_to_bq() から呼び出す。

        Args:
            sectors: {"1016": {"name_ja": ..., "subthemes": [...], ...}, ...}
            market:  "JP" or "US"
            version: JSONバージョン文字列

        Returns:
            {
              "master_success":  bool,
              "mapping_success": bool,
              "master_count":    int,
              "mapping_count":   int,
            }
        """
        out = {
            "master_success":  False,
            "mapping_success": False,
            "master_count":    0,
            "mapping_count":   0,
        }
        if not sectors:
            return out

        # ── sub_theme_master 行を構築 ───────────────────────────
        master_rows = []
        mapping_rows = []

        for sector_code, info in sectors.items():
            if not isinstance(info, dict):
                continue

            # master 行
            master_rows.append({
                "sector_code":                  str(sector_code),
                "market":                       market,
                "name_ja":                      str(info.get("name_ja") or info.get("name_en") or ""),
                "supply_chain_position":         str(info.get("supply_chain_position", "") or ""),
                "business_cycle_sensitivity":    str(info.get("business_cycle_sensitivity", "") or ""),
                "export_dependency":             str(info.get("export_dependency", "") or ""),
                "digital_transformation_stage":  str(info.get("digital_transformation_stage", "") or ""),
                "esg_priority":                  str(info.get("esg_priority", "") or ""),
                "bm_type":                       info.get("bm_type"),
                "subthemes":                     list(info.get("subthemes", []) or []),
                "version":                       version,
            })

            # ticker_subtheme_mapping 行
            # ※ ticker は JSON に存在しないため sector_code をキーとして登録する
            #   実際の ticker は get_tickers_for_sector() で補完することを想定
            subthemes = list(info.get("subthemes", []) or [])
            for subtheme in subthemes:
                if not subtheme:
                    continue
                mapping_rows.append({
                    "ticker":      f"SECTOR_{sector_code}",   # 業種レベルプレースホルダー
                    "sector_code": str(sector_code),
                    "market":      market,
                    "subtheme":    str(subtheme),
                    "relevance":   1.0,
                    "source":      "json_sync",
                })

        # ── BQ書込 ───────────────────────────────────────────────
        out["master_count"]   = len(master_rows)
        out["mapping_count"]  = len(mapping_rows)
        out["master_success"] = self.upsert_sub_theme_master(master_rows)
        out["mapping_success"] = self.upsert_ticker_subtheme_mapping(mapping_rows)

        return out

    # ----------------------------------------------------------
    # ユーティリティ
    # ----------------------------------------------------------

    @staticmethod
    def now_iso() -> str:
        """現在の UTC タイムスタンプを ISO 8601 文字列で返す"""
        return datetime.now(timezone.utc).isoformat()

    def check_connection(self) -> bool:
        """接続確認（起動時チェック用）"""
        try:
            df = self.query_to_df(f"SELECT 1 AS ok FROM `{self.dataset}.doc_index` LIMIT 1")
            print(f"[BQClient] 接続確認 OK (project={self.project})")
            return True
        except Exception as e:
            print(f"[BQClient] 接続確認 NG: {e}")
            return False


# ============================================================
# モジュールレベル委譲関数
# ============================================================

def insert_rows(rows: list = None, table_ref: str = "",
                project_id: str = "", table_name: str = "", **_ignored) -> bool:
    """
    BQ INSERT のモジュールレベルAPI（dataset 横断対応）。

    ★ 2026-06-11 バグ修正: institutional_13f_monitor.py が
      insert_rows(project_id=..., table_ref="us.institutional_positions",
      rows=...) とモジュール関数として呼んでいたが、実体は EDINETBQClient
      のメソッド（edinet dataset 固定・シグネチャも別物）のみで、
      AttributeError が握り潰されて 13F の BQ 格納が常時スキップされていた。
      "dataset.table" / "project.dataset.table" 形式の table_ref を受ける。
    """
    try:
        if not rows:
            return True
        cli = EDINETBQClient()
        ref = table_ref or f"{cli.dataset}.{table_name}"
        # "dataset.table" 形式なら project を前置（cli.dataset は project 込み）
        if ref.count(".") == 1:
            ref = f"{project_id or cli.project}.{ref}"
        errors = cli.client.insert_rows_json(ref, rows)
        if errors:
            print(f"[BQClient] insert_rows error ({ref}): {errors}")
            return False
        return True
    except Exception as e:
        print(f"[BQClient] insert_rows 失敗: {str(e)[:80]}")
        return False


def get_latest_financials(securities_code: str, market: str = "JP") -> dict:
    """
    最新財務実績を返すモジュールレベルAPI。

    ★ 2026-06-10 バグ修正: midterm_plan_tracker.py が
      `from edinet_bq_client import get_latest_financials` と関数として
      import していたが、実体は EDINETBQClient のメソッドのみで
      ImportError が握り潰され中計実績取得が常時欠落していた。
      シングルトンクライアントへの委譲として実装。

    Args:
        securities_code: 証券コード
        market:          現状 "JP" のみ対応（BQ edinet データセットはJP専用）。
                         互換のため引数は受けるが参照しない。
    Returns:
        {カラム名: 値, ...} 取得失敗時は空dict
    """
    try:
        return EDINETBQClient().get_latest_financials(securities_code) or {}
    except Exception:
        return {}


# ============================================================
# 動作確認用エントリポイント
# ============================================================
if __name__ == "__main__":
    client = EDINETBQClient()
    client.check_connection()
