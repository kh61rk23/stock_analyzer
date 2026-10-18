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

【冪等性】 既定の実行（初回整備）は ticker_subtheme_mapping に load job で追記する。job_id は内容から
         決まるので同じ内容の再実行は重ならないが、内容が変わると古い行が残る（sub_theme_master は MERGE）。
         定期の更新は --refresh を使う: 投入したい全行を重複なしで作り、
         ticker_subtheme_mapping を load job（WRITE_TRUNCATE・無料）で丸ごと置き換える。
         --if-changed を付けると、config の代表銘柄とサブテーマJSONが前回の置き換えから
         変わっていない月は何もしない（monthly_runner の subtheme_refresh が月初に呼ぶ）。

【投入内容】
  - sub_theme_master      : jp/us_sector_subtheme.json（subtheme_loader 同期）
  - ticker_subtheme_mapping: config.SECTOR_REPRESENTATIVE_TICKERS（実銘柄）×
                             各業種の subthemes。SECTOR_ プレースホルダーは入れない。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
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


_MAP_KEY = ("ticker", "sector_code", "market", "subtheme")
STATE_FILE = "subtheme_refresh_state.json"


def build_placeholder_rows(sectors: dict, market: str) -> list[dict]:
    """サブテーマJSONの業種×サブテーマ → 業種レベルの行（ticker='SECTOR_<業種>'）。
    sync_subtheme_json_to_bq と同じ形。読み手は SECTOR_ を実銘柄から除いて使う。"""
    rows = []
    for code, info in (sectors or {}).items():
        if not isinstance(info, dict):
            continue
        for st in info.get("subthemes", []) or []:
            if st:
                rows.append({"ticker": f"SECTOR_{code}", "sector_code": str(code), "market": market,
                             "subtheme": str(st), "relevance": 1.0, "source": "json_sync"})
    return rows


def build_master_rows(sectors: dict, market: str, version: str) -> list[dict]:
    """サブテーマJSON → sub_theme_master の行（upsert_sub_theme_master に渡す形）。"""
    out = []
    for code, info in (sectors or {}).items():
        if not isinstance(info, dict):
            continue
        out.append({
            "sector_code": str(code), "market": market,
            "name_ja": str(info.get("name_ja") or info.get("name_en") or ""),
            "supply_chain_position": str(info.get("supply_chain_position", "") or ""),
            "business_cycle_sensitivity": str(info.get("business_cycle_sensitivity", "") or ""),
            "export_dependency": str(info.get("export_dependency", "") or ""),
            "digital_transformation_stage": str(info.get("digital_transformation_stage", "") or ""),
            "esg_priority": str(info.get("esg_priority", "") or ""),
            "bm_type": info.get("bm_type"),
            "subthemes": list(info.get("subthemes", []) or []),
            "version": version,
        })
    return out


def dedupe_mapping(rows: list[dict]) -> list[dict]:
    """(ticker, sector_code, market, subtheme) が同じ行は最初の1行だけ残す（純関数）。"""
    seen, out = set(), []
    for r in rows:
        k = tuple(str(r.get(c, "")) for c in _MAP_KEY)
        if not all(k) or k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def fingerprint(mapping_rows: list[dict], master_rows: list[dict]) -> str:
    """投入内容の指紋（行の並びに依らない）。前回の置き換えと同じなら --if-changed で飛ばす。"""
    norm = lambda rows: sorted(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) for r in rows)
    payload = json.dumps([norm(mapping_rows), norm(master_rows)], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def refresh(if_changed: bool = False) -> int:
    """sub_theme_master を MERGE し、ticker_subtheme_mapping を重複なしの全行で置き換える。"""
    cfg = _load("config")
    sl = _load("subtheme_loader")
    mapping, master = list(_build_real_ticker_rows(cfg, market="JP")), []
    for market in ("JP", "US"):
        sectors = sl.load_all_subthemes(market=market, cfg=cfg) or {}
        version = str((getattr(sl, "_cache", {}).get(market) or {}).get("version", "1.0"))
        mapping += build_placeholder_rows(sectors, market)
        master += build_master_rows(sectors, market, version)
    mapping = dedupe_mapping(mapping)
    fp = fingerprint(mapping, master)
    state_path = Path(getattr(cfg, "DATA_DIR", REPO / "stock_data" / "data")) / STATE_FILE
    try:
        prev = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    if if_changed and prev.get("fingerprint") == fp:
        print(f"⏩ subtheme refresh: 代表銘柄・サブテーマJSONとも前回（{prev.get('refreshed_at', '')[:10]}）から変化なし")
        return 0
    if not mapping:
        print("❌ subtheme refresh: 投入する行が0件（config・JSON を読めていない）。表は置き換えない")
        return 1

    bqc = _load("edinet_bq_client")
    bq = bqc.EDINETBQClient()
    if not _create_tables(bq):
        return 1
    ok_master = bq.upsert_sub_theme_master(master) if master else True
    from google.cloud import bigquery
    table_ref = f"{bq.dataset}.ticker_subtheme_mapping"
    now = datetime.now(timezone.utc).isoformat()
    payload = [{**{c: r.get(c) for c in ("ticker", "sector_code", "market", "subtheme", "relevance", "source")},
                "updated_at": now} for r in mapping]
    job = bq.client.load_table_from_json(payload, table_ref, job_config=bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=bq.client.get_table(table_ref).schema))
    job.result()
    print(f"✅ ticker_subtheme_mapping 置き換え: {len(payload)}行"
          f"（実銘柄 {sum(1 for r in payload if not r['ticker'].startswith('SECTOR_'))}・業種行 "
          f"{sum(1 for r in payload if r['ticker'].startswith('SECTOR_'))}）／ sub_theme_master MERGE {len(master)}行"
          f"{'' if ok_master else '（失敗あり）'}")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"fingerprint": fp, "refreshed_at": now, "mapping_rows": len(payload),
                                      "master_rows": len(master)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok_master else 1


def main() -> int:
    if "--refresh" in sys.argv:
        if not os.environ.get("BQ_CREDENTIALS") and not os.environ.get("BQ_CREDENTIALS_PATH"):
            # monthly_runner は Mac（司令塔シェル）からも起動される。BQ の鍵が無い側では飛ばし、Actions 側で更新する
            print("⏩ subtheme refresh: BQ_CREDENTIALS が無いのでスキップ（Actions の月次で更新する）")
            return 0
        return refresh(if_changed="--if-changed" in sys.argv)
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
