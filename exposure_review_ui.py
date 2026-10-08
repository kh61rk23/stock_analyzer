# ==============================================================
# exposure_review_ui.py  v1.0  (G-C3)
# ==============================================================
# 【役割】
#   jogmec_scraper / sec_geographic_extractor /
#   edinet_exposure_extractor が自動抽出した
#   geopolitics.company_country_exposure レコードを
#   人間が目視確認・補完・承認するための Streamlit UI。
#
# 【主な機能】
#   Tab1: 未確認レコード一覧（review_flag=True / verified_flag=False）
#         - ティッカー・国・権益種別・強度を表示
#         - 個別に「承認」「修正」「削除」操作
#   Tab2: 手動追加フォーム
#         - 新規レコードを直接入力して BQ に INSERT
#   Tab3: 確認済みレコード一覧
#         - verified_flag=True の全レコードを閲覧
#   Tab4: 統計ダッシュボード
#         - 国別・ティッカー別のエクスポージャー分布
#
# 【実行方法】
#   Colab または ローカル環境で:
#     streamlit run exposure_review_ui.py
#   または stock_analyzer_main.py の Streamlit UI から
#   サイドバーリンクとして提供
#
# 【BQ 操作】
#   読み取り: geopolitics.company_country_exposure
#   書き込み: UPDATE verified_flag / review_flag / exposure_strength
#             INSERT 新規レコード
#             DELETE（論理削除: valid_to = CURRENT_DATE()）
#
# 【依存】
#   streamlit / edinet_bq_client.py / config.py
# ==============================================================

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any

try:
    import streamlit as st
    _STREAMLIT_AVAILABLE = True
except ImportError:
    _STREAMLIT_AVAILABLE = False
    print("⚠️  streamlit 未インストール: pip install streamlit")

# ==============================================================
# BLOCK 1: BQ アクセスユーティリティ
# ==============================================================

_BQ_CLIENT_CACHE: Any = None   # セッション内キャッシュ


def _get_raw_bq(project_id: str = "stock-analyzer-edinet"):
    """google.cloud.bigquery.Client を返す（キャッシュあり）。"""
    global _BQ_CLIENT_CACHE
    if _BQ_CLIENT_CACHE is not None:
        return _BQ_CLIENT_CACHE
    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _bq_mod = importlib.import_module("edinet_bq_client")
        _BQ_CLIENT_CACHE = _bq_mod.EDINETBQClient().client
        return _BQ_CLIENT_CACHE
    except Exception:
        try:
            from google.cloud import bigquery as _bq_lib
            try:
                from google.colab import auth as _auth
                _auth.authenticate_user()
            except Exception:
                pass
            _BQ_CLIENT_CACHE = _bq_lib.Client(
                project=project_id, location="asia-northeast1"
            )
            return _BQ_CLIENT_CACHE
        except Exception as e:
            if _STREAMLIT_AVAILABLE:
                st.error(f"BQ 接続失敗: {e}")
            return None


def _run_query(sql: str, project_id: str = "stock-analyzer-edinet") -> list[dict]:
    """SELECT クエリを実行して list[dict] を返す。失敗時は空リスト。"""
    bq = _get_raw_bq(project_id)
    if not bq:
        return []
    try:
        rows = list(bq.query(sql).result())
        return [dict(row) for row in rows]
    except Exception as e:
        if _STREAMLIT_AVAILABLE:
            st.error(f"クエリ失敗: {str(e)[:200]}")
        return []


def _run_dml(sql: str, project_id: str = "stock-analyzer-edinet") -> bool:
    """DML（UPDATE / INSERT / DELETE）を実行して成否を返す。"""
    bq = _get_raw_bq(project_id)
    if not bq:
        return False
    try:
        bq.query(sql).result()
        return True
    except Exception as e:
        if _STREAMLIT_AVAILABLE:
            st.error(f"DML 失敗: {str(e)[:200]}")
        return False


# ==============================================================
# BLOCK 2: データ取得関数
# ==============================================================

def _load_pending_records(
    project_id: str,
    limit: int = 200,
) -> list[dict]:
    """
    未確認レコード（review_flag=TRUE または verified_flag=FALSE）を取得する。
    valid_to が NULL（有効中）のレコードのみ対象。
    """
    sql = f"""
    SELECT
      exposure_id,
      ticker,
      market,
      country_code,
      exposure_type,
      exposure_strength,
      commodity_code,
      revenue_share_est,
      source,
      rationale,
      review_flag,
      verified_flag,
      FORMAT_DATE('%Y-%m-%d', valid_from) AS valid_from,
      updated_at
    FROM `{project_id}.geopolitics.company_country_exposure`
    WHERE (review_flag = TRUE OR verified_flag = FALSE)
      AND (valid_to IS NULL OR valid_to > CURRENT_DATE())
    ORDER BY updated_at DESC
    LIMIT {limit}
    """
    return _run_query(sql, project_id)


def _load_verified_records(
    project_id: str,
    ticker_filter: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """確認済みレコード（verified_flag=TRUE）を取得する。"""
    where_ticker = (
        f"AND ticker = '{ticker_filter}'"
        if ticker_filter else ""
    )
    sql = f"""
    SELECT
      exposure_id,
      ticker,
      market,
      country_code,
      exposure_type,
      exposure_strength,
      commodity_code,
      revenue_share_est,
      source,
      rationale,
      FORMAT_DATE('%Y-%m-%d', valid_from) AS valid_from,
      FORMAT_DATE('%Y-%m-%d', valid_to)   AS valid_to,
      updated_at
    FROM `{project_id}.geopolitics.company_country_exposure`
    WHERE verified_flag = TRUE
      {where_ticker}
    ORDER BY ticker, country_code
    LIMIT {limit}
    """
    return _run_query(sql, project_id)


def _load_stats(project_id: str) -> dict:
    """統計ダッシュボード用の集計データを取得する。"""
    sql_country = f"""
    SELECT
      country_code,
      COUNT(*) AS record_count,
      ROUND(AVG(exposure_strength), 3) AS avg_strength,
      COUNTIF(verified_flag = TRUE) AS verified_count
    FROM `{project_id}.geopolitics.company_country_exposure`
    WHERE valid_to IS NULL OR valid_to > CURRENT_DATE()
    GROUP BY country_code
    ORDER BY record_count DESC
    LIMIT 30
    """
    sql_ticker = f"""
    SELECT
      ticker,
      market,
      COUNT(*) AS country_count,
      ROUND(SUM(exposure_strength), 3) AS total_exposure,
      COUNTIF(verified_flag = TRUE) AS verified_count
    FROM `{project_id}.geopolitics.company_country_exposure`
    WHERE valid_to IS NULL OR valid_to > CURRENT_DATE()
    GROUP BY ticker, market
    ORDER BY total_exposure DESC
    LIMIT 30
    """
    sql_source = f"""
    SELECT
      source,
      COUNT(*) AS cnt,
      COUNTIF(verified_flag = TRUE) AS verified
    FROM `{project_id}.geopolitics.company_country_exposure`
    WHERE valid_to IS NULL OR valid_to > CURRENT_DATE()
    GROUP BY source
    ORDER BY cnt DESC
    """
    return {
        "by_country": _run_query(sql_country, project_id),
        "by_ticker":  _run_query(sql_ticker,  project_id),
        "by_source":  _run_query(sql_source,  project_id),
    }


# ==============================================================
# BLOCK 3: DML 操作関数
# ==============================================================

def _approve_record(
    exposure_id: str,
    new_strength: float,
    project_id: str,
) -> bool:
    """レコードを承認する（verified_flag=TRUE, review_flag=FALSE）。"""
    sql = f"""
    UPDATE `{project_id}.geopolitics.company_country_exposure`
    SET
      verified_flag    = TRUE,
      review_flag      = FALSE,
      exposure_strength = {new_strength},
      updated_at       = CURRENT_TIMESTAMP()
    WHERE exposure_id = '{exposure_id}'
    """
    return _run_dml(sql, project_id)


def _reject_record(exposure_id: str, project_id: str) -> bool:
    """レコードを論理削除する（valid_to = CURRENT_DATE()）。"""
    sql = f"""
    UPDATE `{project_id}.geopolitics.company_country_exposure`
    SET
      valid_to    = CURRENT_DATE(),
      updated_at  = CURRENT_TIMESTAMP()
    WHERE exposure_id = '{exposure_id}'
    """
    return _run_dml(sql, project_id)


def _insert_manual_record(record: dict, project_id: str) -> bool:
    """
    手動追加フォームのレコードを INSERT する。
    exposure_id は新規 UUID を生成。
    """
    def _s(v):
        if v is None:
            return "NULL"
        return "'" + str(v).replace("'", "\\'") + "'"

    def _f(v):
        if v is None:
            return "NULL"
        return str(float(v))

    exp_id   = str(uuid.uuid4())
    ticker   = record.get("ticker", "")
    market   = record.get("market", "JP")
    country  = record.get("country_code", "")
    exp_type = record.get("exposure_type", "SALES_NETWORK")
    strength = float(record.get("exposure_strength", 0.10))
    commodity = record.get("commodity_code")
    rev_share = record.get("revenue_share_est")
    source   = "manual"
    rationale = record.get("rationale", "手動入力")
    valid_from = record.get("valid_from", date.today().isoformat())

    sql = f"""
    INSERT INTO `{project_id}.geopolitics.company_country_exposure` (
      exposure_id, ticker, company_id, market, country_code,
      exposure_type, exposure_strength, commodity_code,
      revenue_share_est, employee_share_est, asset_share_est,
      source, evidence_url, rationale,
      auto_derived, verified_flag, review_flag,
      valid_from, valid_to, updated_at
    )
    VALUES (
      '{exp_id}', {_s(ticker)}, {_s(ticker)}, {_s(market)}, {_s(country)},
      {_s(exp_type)}, {strength}, {_s(commodity)},
      {_f(rev_share)}, NULL, NULL,
      'manual', NULL, {_s(rationale)},
      FALSE, TRUE, FALSE,
      DATE('{valid_from}'), NULL, CURRENT_TIMESTAMP()
    )
    """
    return _run_dml(sql, project_id)


# ==============================================================
# BLOCK 4: UI 定数・ヘルパー
# ==============================================================

_EXPOSURE_TYPE_OPTIONS = [
    "RESOURCE_CONCESSION",
    "PRODUCTION_FACILITY",
    "SALES_NETWORK",
    "JOINT_VENTURE",
    "EQUITY_STAKE",
    "PROCUREMENT",
    "CUSTOMER_CONC",
]

_EXPOSURE_TYPE_LABELS = {
    "RESOURCE_CONCESSION": "⛏️ 採掘権・鉱業権",
    "PRODUCTION_FACILITY": "🏭 現地生産設備・工場",
    "SALES_NETWORK":       "🛒 販売網（売上依存）",
    "JOINT_VENTURE":       "🤝 合弁会社持分",
    "EQUITY_STAKE":        "📊 現地企業への出資",
    "PROCUREMENT":         "📦 主要調達先",
    "CUSTOMER_CONC":       "👥 主要顧客集中",
}

_MARKET_OPTIONS    = ["JP", "US"]
_COMMODITY_OPTIONS = [
    None, "crude_oil", "lng", "natural_gas", "coal",
    "iron_ore", "copper", "lithium", "cobalt", "nickel",
    "rare_earth", "gold", "uranium", "aluminum", "platinum",
    "wheat", "soybeans", "palm_oil",
]


def _strength_color(v: float) -> str:
    """exposure_strength の値に応じた色を返す。"""
    if v >= 0.5:
        return "🔴"
    if v >= 0.3:
        return "🟠"
    if v >= 0.1:
        return "🟡"
    return "🟢"


# ==============================================================
# BLOCK 5: Tab1 – 未確認レコード一覧
# ==============================================================

def _render_tab_pending(project_id: str) -> None:
    """Tab1: 未確認レコードの一覧表示・承認・削除。"""
    st.subheader("🔍 未確認レコード一覧")
    st.caption(
        "自動抽出された権益情報のうち、未確認（review_flag=TRUE または "
        "verified_flag=FALSE）のレコードを表示します。"
    )

    # フィルタ
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        ticker_filter = st.text_input(
            "ティッカーで絞り込み", value="", key="pending_ticker_filter",
            placeholder="例: 8058.T",
        )
    with col_f2:
        source_filter = st.selectbox(
            "ソースで絞り込み",
            ["すべて", "jogmec_manual", "jogmec_rss", "edinet_auto",
             "sec_auto", "manual"],
            key="pending_source_filter",
        )

    with st.spinner("BQ からデータ取得中..."):
        records = _load_pending_records(project_id, limit=200)

    if not records:
        st.info("未確認レコードはありません。")
        return

    # フィルタ適用
    if ticker_filter.strip():
        records = [r for r in records
                   if ticker_filter.strip() in str(r.get("ticker", ""))]
    if source_filter != "すべて":
        records = [r for r in records
                   if r.get("source") == source_filter]

    st.write(f"**{len(records)} 件** 表示中")

    # 一括承認ボタン
    if st.button("✅ フィルタ表示中を全件承認（exposure_strength 変更なし）",
                 key="bulk_approve"):
        success_count = 0
        for r in records:
            if _approve_record(
                r["exposure_id"],
                float(r.get("exposure_strength") or 0.10),
                project_id,
            ):
                success_count += 1
        st.success(f"{success_count} 件を承認しました。")
        st.rerun()

    st.divider()

    # 個別レコード表示
    for idx, r in enumerate(records):
        exp_id   = r.get("exposure_id", "")
        ticker   = r.get("ticker", "")
        country  = r.get("country_code", "")
        exp_type = r.get("exposure_type", "")
        strength = float(r.get("exposure_strength") or 0.10)
        source   = r.get("source", "")
        rationale = r.get("rationale", "")
        commodity = r.get("commodity_code", "-")
        rev_share = r.get("revenue_share_est")

        label = (
            f"{_strength_color(strength)} "
            f"**{ticker}** → `{country}` "
            f"{_EXPOSURE_TYPE_LABELS.get(exp_type, exp_type)} "
            f"({strength*100:.0f}%)"
        )
        with st.expander(label, expanded=False):
            col1, col2, col3 = st.columns([2, 2, 1])
            with col1:
                st.write(f"**ソース**: {source}")
                st.write(f"**資源**: {commodity}")
                st.write(f"**売上比率推定**: "
                         f"{f'{rev_share*100:.1f}%' if rev_share else 'N/A'}")
                st.write(f"**根拠**: {rationale[:150]}")
            with col2:
                new_strength = st.slider(
                    "exposure_strength",
                    min_value=0.01,
                    max_value=1.00,
                    value=round(strength, 2),
                    step=0.01,
                    key=f"strength_{idx}_{exp_id[:8]}",
                )
            with col3:
                if st.button("✅ 承認", key=f"approve_{idx}_{exp_id[:8]}"):
                    if _approve_record(exp_id, new_strength, project_id):
                        st.success("承認しました")
                        st.rerun()
                if st.button("🗑️ 削除", key=f"reject_{idx}_{exp_id[:8]}"):
                    if _reject_record(exp_id, project_id):
                        st.warning("削除しました")
                        st.rerun()


# ==============================================================
# BLOCK 6: Tab2 – 手動追加フォーム
# ==============================================================

def _render_tab_manual(project_id: str) -> None:
    """Tab2: 手動追加フォーム。"""
    st.subheader("➕ 手動追加")
    st.caption("既知の権益情報を手動で登録します。verified_flag=TRUE で直接確認済みとして登録されます。")

    with st.form("manual_add_form"):
        col1, col2 = st.columns(2)
        with col1:
            ticker   = st.text_input("ティッカー *", placeholder="例: 8058.T / NVDA")
            market   = st.selectbox("市場 *", _MARKET_OPTIONS)
            country  = st.text_input(
                "国コード * (ISO 3166-1 alpha-3)",
                placeholder="例: AUS / CHN / SAU",
            )
            exp_type = st.selectbox(
                "権益種別 *",
                _EXPOSURE_TYPE_OPTIONS,
                format_func=lambda x: _EXPOSURE_TYPE_LABELS.get(x, x),
            )
        with col2:
            strength  = st.slider(
                "exposure_strength *",
                min_value=0.01, max_value=1.00,
                value=0.10, step=0.01,
            )
            rev_share = st.number_input(
                "revenue_share_est (任意)",
                min_value=0.0, max_value=1.0,
                value=0.0, step=0.01,
            )
            commodity = st.selectbox(
                "資源コード (任意)",
                _COMMODITY_OPTIONS,
                format_func=lambda x: x if x else "（なし）",
            )
            rationale = st.text_area(
                "根拠・メモ *",
                placeholder="例: 三菱商事イクシスLNG持分保有（INPEX共同）",
                height=80,
            )

        submitted = st.form_submit_button("登録する")

    if submitted:
        errors = []
        if not ticker.strip():
            errors.append("ティッカーは必須です")
        if not country.strip() or len(country.strip()) != 3:
            errors.append("国コードは ISO 3166-1 alpha-3（3文字大文字）を入力してください")
        if not rationale.strip():
            errors.append("根拠・メモは必須です")

        if errors:
            for e in errors:
                st.error(e)
        else:
            record = {
                "ticker":            ticker.strip().upper()
                                     if market == "US"
                                     else ticker.strip(),
                "market":            market,
                "country_code":      country.strip().upper(),
                "exposure_type":     exp_type,
                "exposure_strength": strength,
                "revenue_share_est": rev_share if rev_share > 0 else None,
                "commodity_code":    commodity,
                "rationale":         rationale.strip(),
                "valid_from":        date.today().isoformat(),
            }
            if _insert_manual_record(record, project_id):
                st.success(
                    f"✅ 登録完了: {ticker} → {country} "
                    f"{_EXPOSURE_TYPE_LABELS.get(exp_type, exp_type)}"
                )
            else:
                st.error("登録に失敗しました。BQ 接続を確認してください。")


# ==============================================================
# BLOCK 7: Tab3 – 確認済みレコード一覧
# ==============================================================

def _render_tab_verified(project_id: str) -> None:
    """Tab3: 確認済みレコード一覧・検索。"""
    st.subheader("✅ 確認済みレコード")

    col1, col2 = st.columns(2)
    with col1:
        ticker_filter = st.text_input(
            "ティッカーで絞り込み",
            value="",
            key="verified_ticker_filter",
            placeholder="例: 1605.T",
        )
    with col2:
        st.write("")  # スペーサー

    with st.spinner("BQ からデータ取得中..."):
        records = _load_verified_records(
            project_id,
            ticker_filter=ticker_filter.strip() or None,
            limit=500,
        )

    if not records:
        st.info("確認済みレコードがありません。")
        return

    st.write(f"**{len(records)} 件**")

    # テーブル表示
    import pandas as pd
    df = pd.DataFrame(records)
    display_cols = [
        "ticker", "market", "country_code", "exposure_type",
        "exposure_strength", "commodity_code",
        "revenue_share_est", "source", "valid_from",
    ]
    display_cols = [c for c in display_cols if c in df.columns]
    st.dataframe(
        df[display_cols],
        use_container_width=True,
        height=400,
    )

    # CSV ダウンロード
    csv_data = df[display_cols].to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        label="📥 CSV ダウンロード",
        data=csv_data,
        file_name=f"geo_exposure_verified_{date.today().isoformat()}.csv",
        mime="text/csv",
    )


# ==============================================================
# BLOCK 8: Tab4 – 統計ダッシュボード
# ==============================================================

def _render_tab_stats(project_id: str) -> None:
    """Tab4: 統計ダッシュボード。"""
    st.subheader("📊 統計ダッシュボード")

    with st.spinner("集計データ取得中..."):
        stats = _load_stats(project_id)

    if not any(stats.values()):
        st.info("統計データがありません。")
        return

    col1, col2 = st.columns(2)

    with col1:
        st.write("#### 🌍 国別レコード数（上位30）")
        if stats["by_country"]:
            import pandas as pd
            df_c = pd.DataFrame(stats["by_country"])
            st.dataframe(df_c, use_container_width=True, height=300)

    with col2:
        st.write("#### 📈 ティッカー別総エクスポージャー（上位30）")
        if stats["by_ticker"]:
            import pandas as pd
            df_t = pd.DataFrame(stats["by_ticker"])
            st.dataframe(df_t, use_container_width=True, height=300)

    st.write("#### 📂 ソース別件数")
    if stats["by_source"]:
        import pandas as pd
        df_s = pd.DataFrame(stats["by_source"])
        total = sum(r.get("cnt", 0) for r in stats["by_source"])
        verified = sum(r.get("verified", 0) for r in stats["by_source"])
        st.write(
            f"総レコード数: **{total}** 件 / "
            f"確認済み: **{verified}** 件 "
            f"({verified/total*100:.1f}%)" if total > 0 else ""
        )
        st.dataframe(df_s, use_container_width=True, height=200)


# ==============================================================
# BLOCK 9: メインアプリケーション
# ==============================================================

def run_exposure_review_ui(
    project_id: str = "stock-analyzer-edinet",
    page_title: str = "権益マッピング レビューUI",
) -> None:
    """
    Streamlit アプリケーションのメインエントリポイント。

    stock_analyzer_main.py の Streamlit UI から呼び出すか、
    または直接 `streamlit run exposure_review_ui.py` で起動する。
    """
    if not _STREAMLIT_AVAILABLE:
        print("ERROR: streamlit が未インストールです。")
        print("  pip install streamlit")
        return

    st.set_page_config(
        page_title=page_title,
        page_icon="🌍",
        layout="wide",
    )

    # ── ヘッダー ───────────────────────────────────────────────
    st.title("🌍 企業×国 権益マッピング レビューUI")
    st.caption(
        f"Wave 6 - G-C | プロジェクト: `{project_id}` | "
        f"テーブル: `geopolitics.company_country_exposure`"
    )

    # ── サイドバー ─────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ 設定")
        project_input = st.text_input(
            "GCPプロジェクトID",
            value=project_id,
            key="project_id_input",
        )
        if project_input != project_id:
            project_id = project_input
            global _BQ_CLIENT_CACHE
            _BQ_CLIENT_CACHE = None   # プロジェクト変更時はキャッシュリセット

        st.divider()
        st.write("**クイックアクション**")

        if st.button("🔄 scraper 再実行（JOGMEC）"):
            with st.spinner("jogmec_scraper 実行中..."):
                try:
                    import importlib, sys as _sys
                    from pathlib import Path as _Path
                    for p in ["/content", str(_Path(__file__).parent)]:
                        if p not in _sys.path:
                            _sys.path.insert(0, p)
                    _js = importlib.import_module("jogmec_scraper")
                    result = _js.run_jogmec_scraper(dry_run=False)
                    if result["success"]:
                        st.success(f"✅ {result['merged']} 件 MERGE 完了")
                    else:
                        st.error(f"❌ {result['error'][:100]}")
                except Exception as e:
                    st.error(f"実行失敗: {str(e)[:100]}")

        if st.button("🔄 extractor 再実行（EDINET）"):
            with st.spinner("edinet_exposure_extractor 実行中..."):
                try:
                    import importlib, sys as _sys
                    from pathlib import Path as _Path
                    for p in ["/content", str(_Path(__file__).parent)]:
                        if p not in _sys.path:
                            _sys.path.insert(0, p)
                    _ee = importlib.import_module("edinet_exposure_extractor")
                    result = _ee.run_edinet_exposure_extractor(dry_run=False)
                    if result["success"]:
                        st.success(f"✅ {result['merged']} 件 MERGE 完了")
                    else:
                        st.error(f"❌ {result['error'][:100]}")
                except Exception as e:
                    st.error(f"実行失敗: {str(e)[:100]}")

        if st.button("🔄 extractor 再実行（SEC）"):
            with st.spinner("sec_geographic_extractor 実行中..."):
                try:
                    import importlib, sys as _sys
                    from pathlib import Path as _Path
                    for p in ["/content", str(_Path(__file__).parent)]:
                        if p not in _sys.path:
                            _sys.path.insert(0, p)
                    _se = importlib.import_module("sec_geographic_extractor")
                    result = _se.run_sec_geographic_extractor(dry_run=False)
                    if result["success"]:
                        st.success(f"✅ {result['merged']} 件 MERGE 完了")
                    else:
                        st.error(f"❌ {result['error'][:100]}")
                except Exception as e:
                    st.error(f"実行失敗: {str(e)[:100]}")

        st.divider()
        st.caption(
            f"最終更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    # ── タブ ───────────────────────────────────────────────────
    tab1, tab2, tab3, tab4 = st.tabs([
        "🔍 未確認レコード",
        "➕ 手動追加",
        "✅ 確認済み一覧",
        "📊 統計",
    ])

    with tab1:
        _render_tab_pending(project_id)

    with tab2:
        _render_tab_manual(project_id)

    with tab3:
        _render_tab_verified(project_id)

    with tab4:
        _render_tab_stats(project_id)


# ==============================================================
# 単体実行エントリ（streamlit run exposure_review_ui.py）
# ==============================================================

if __name__ == "__main__":
    run_exposure_review_ui(
        project_id="stock-analyzer-edinet",
        page_title="権益マッピング レビューUI",
    )