"""
setup_subtheme_bq.py
====================
【役割】 BigQuery の edinet.ticker_subtheme_mapping / edinet.sub_theme_master を
         作成し、初期データを投入する（Phase 4 / Wave 6 - R-2 整備）。

【背景】 この2テーブルが未作成で get_tickers_for_sector / get_sub_theme_master が
         404 になり、セクター銘柄解決が常時失敗→全業種スコア0.0/リスクhigh だった。

【実行】 Colab 非依存。GitHub Actions / ローカル / Cowork から実行可能。
         認証は環境変数 BQ_CREDENTIALS（サービスアカウントJSON文字列）を使用。
         ※ サービスアカウントには bigquery.tables.create / bigquery.jobs.create が必要。

    BQ_CREDENTIALS='<service account json>' \
    BQ_PROJECT_ID='stock-analyzer-edinet' \
    python3 setup_subtheme_bq.py

【冪等性】 CREATE TABLE IF NOT EXISTS + MERGE/streaming upsert のため再実行安全。

【投入内容】
  - sub_theme_master      : jp/us_sector_subtheme.json（subtheme_loader 同期）
  - ticker_subtheme_mapping: config.SECTOR_REPRESENTATIVE_TICKERS（実銘柄）×
                             各業種の subthemes。SECTOR_ プレースホルダーは入れない。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, str(REPO / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# テーブル DDL（edinet_bq_schema.py TABLE_DEFINITIONS["edinet"] と一致させること）
_DDL = {
    "sub_theme_master": """(
        sector_code STRING NOT NULL,
        market STRING,
        name_ja STRING,
        supply_chain_position STRING,
        business_cycle_sensitivity STRING,
        export_dependency STRING,
        digital_transformation_stage STRING,
        esg_priority STRING,
        bm_type STRING,
        subthemes JSON,
        version STRING,
        updated_at TIMESTAMP
    )""",
    "ticker_subtheme_mapping": """(
        ticker STRING NOT NULL,
        sector_code STRING,
        market STRING,
        subtheme STRING,
        relevance FLOAT64,
        source STRING,
        updated_at TIMESTAMP
    )""",
}


def _create_tables(bq) -> bool:
    """edinet データセットに2テーブルを CREATE TABLE IF NOT EXISTS で作成する。"""
    ok = True
    for table, ddl in _DDL.items():
        full = f"{bq.dataset}.{table}"
        sql = f"CREATE TABLE IF NOT EXISTS `{full}` {ddl}"
        try:
            bq.client.query(sql).result()
            print(f"  ✅ テーブル確認/作成: {full}")
        except Exception as e:
            ok = False
            print(f"  ❌ テーブル作成失敗 ({full}): {str(e)[:160]}")
    return ok


def _build_real_ticker_rows(cfg, market: str = "JP") -> list[dict]:
    """
    config.SECTOR_REPRESENTATIVE_TICKERS の実銘柄 × 各業種の subthemes を
    ticker_subtheme_mapping の行に展開する。
    """
    rep = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {}) or {}
    if not rep:
        print("  ⚠️  SECTOR_REPRESENTATIVE_TICKERS が空 → ticker 投入なし")
        return []

    # 各業種の subthemes を JSON から取得（subtheme_loader 経由）
    try:
        sl = _load("subtheme_loader")
        sectors = sl.load_all_subthemes(market=market, cfg=cfg) or {}
    except Exception as e:
        print(f"  ⚠️  subtheme JSON ロード失敗（subtheme は既定値で代用）: {str(e)[:80]}")
        sectors = {}

    rows: list[dict] = []
    for sector_code, tickers in rep.items():
        info = sectors.get(str(sector_code), {}) if isinstance(sectors, dict) else {}
        subthemes = [s for s in (info.get("subthemes", []) or []) if s] \
            if isinstance(info, dict) else []
        if not subthemes:
            subthemes = ["業種代表"]   # subtheme は upsert 必須キーのため最低1件
        for ticker in tickers:
            t = str(ticker).strip()
            if not t:
                continue
            for subtheme in subthemes:
                rows.append({
                    "ticker":      t,
                    "sector_code": str(sector_code),
                    "market":      market,
                    "subtheme":    str(subtheme),
                    "relevance":   1.0,
                    "source":      "config_representative",
                })
    return rows


def main() -> int:
    if not os.environ.get("BQ_CREDENTIALS") and not os.environ.get("BQ_CREDENTIALS_PATH"):
        print("❌ BQ_CREDENTIALS（またはBQ_CREDENTIALS_PATH）が未設定です。")
        print("   サービスアカウントJSONを環境変数で渡してください。")
        return 2

    print("=" * 60)
    print("  Phase 4: subtheme BQ テーブル整備")
    print("=" * 60)

    cfg = _load("config")
    bqc = _load("edinet_bq_client")
    bq = bqc.EDINETBQClient()
    print(f"  プロジェクト: {bq.project} / データセット: {bq.dataset}")

    # ① テーブル作成
    print("\n[1/3] テーブル作成（CREATE TABLE IF NOT EXISTS）")
    if not _create_tables(bq):
        print("  ❌ テーブル作成に失敗したため中断します。")
        return 1
    # policy_support は専用の ensure メソッド（DDL内蔵）を持つが、これまで
    # どこからも呼ばれず未作成で get_policy_support_adoptions が 404 だった。
    # ここで冪等に作成する。
    try:
        ok_ps = bq.ensure_policy_support_table()
        print(f"  {'✅' if ok_ps else '❌'} テーブル確認/作成: {bq.dataset}.policy_support")
    except Exception as e:
        print(f"  ❌ policy_support 作成失敗: {str(e)[:160]}")

    # ② sub_theme_master 同期（JSON → BQ）
    print("\n[2/3] sub_theme_master 同期（JP/US JSON → BQ）")
    try:
        sl = _load("subtheme_loader")
        sync = sl.sync_all_markets_to_bq(cfg=cfg, force=True)
        print(f"  同期結果: {sync}")
    except Exception as e:
        print(f"  ⚠️  sub_theme_master 同期エラー: {str(e)[:160]}")

    # ③ ticker_subtheme_mapping 投入（実銘柄）
    print("\n[3/3] ticker_subtheme_mapping 投入（SECTOR_REPRESENTATIVE_TICKERS）")
    rows = _build_real_ticker_rows(cfg, market="JP")
    if rows:
        ok = bq.upsert_ticker_subtheme_mapping(rows)
        n_sectors = len({r["sector_code"] for r in rows})
        n_tickers = len({r["ticker"] for r in rows})
        print(f"  {'✅' if ok else '❌'} 投入: {len(rows)}行 "
              f"/ {n_sectors}業種 / {n_tickers}銘柄")
    else:
        print("  ⏩ 投入対象なし")

    # ④ 検証
    print("\n[検証] get_tickers_for_sector で実銘柄が返るか")
    sample = next(iter(getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})), None)
    if sample:
        got = bq.get_tickers_for_sector(sample, limit=5)
        print(f"  業種{sample}: {got}")

    print("\n✅ 完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
