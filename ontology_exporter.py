# ==============================================================
# ontology_exporter.py  v1.0  (Wave ONT-1)
# 既存キャッシュ → オントロジー層 ETL
# ==============================================================
# 【役割】
#   stock_analyzer の既存成果物（screener_history / drift_history /
#   portfolio_holdings / 投資部判断台帳）からエンティティ・イベント行を
#   組み立て、ontology データセットへ流し込む。
#
# 【実行】
#   python3 ontology_exporter.py --dry-run   # BQ接続なし。
#       stock_data/ontology/*.jsonl にローカル出力（内容検証用）
#   python3 ontology_exporter.py             # BQ へ INSERT
#       （事前に ontology_bq_schema.py を1回実行しておくこと）
#
# 【冪等性】
#   event_id / (case_id, decided_at) を一意キーとして生成するため、
#   重複 INSERT は BQ 側クエリの ROW_NUMBER()/QUALIFY で排除する
#   （edinet_bq_client.insert_rows と同じ追記式運用）。
# ==============================================================

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent


def _load_config():
    spec = importlib.util.spec_from_file_location(
        "config", str(REPO / "config.py"))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    base_env = os.environ.get("STOCK_ANALYZER_BASE", "")
    base = Path(base_env) if base_env else (
        Path(cfg.DRIVE_BASE) if Path(cfg.DRIVE_BASE).exists()
        else REPO / "stock_data")
    cfg.DRIVE_BASE, cfg.DATA_DIR = base, base / "data"
    cfg.INSIGHT_DIR, cfg.CACHE_DIR = base / "insights", base / "cache"
    return cfg


def _load_json(p: Path):
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _eid(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


_NOW = datetime.now().isoformat()


# ==============================================================
# BLOCK 1: 行ビルダー（全て読み取り専用・best-effort）
# ==============================================================

LISTED_JP_SQL = """
SELECT CONCAT(SUBSTR(sec_code, 1, 4), '.T') AS ticker, name AS company_name, sector_code
FROM `{project}.edinet.company_master`
WHERE sec_code IS NOT NULL AND NOT IFNULL(is_delisted, FALSE)
"""
LISTED_US_SQL = """
SELECT ticker, name AS company_name, sic AS sector_code
FROM `{project}.us.company_profile`
WHERE ticker IS NOT NULL
"""


def listed_universe(client) -> list[dict]:
    """全上場（日本株は銘柄マスタ・米国株は SEC の企業情報）を企業エンティティの土台にする。2クエリ。"""
    out = []
    for sql, market in ((LISTED_JP_SQL, "JP"), (LISTED_US_SQL, "US")):
        try:
            for r in client.query(sql.format(project=client.project)).result():
                out.append({"ticker": r.ticker, "company_name": r.company_name, "market": market,
                            "sector_code": r.sector_code or ""})
        except Exception as e:
            print(f"  ⚠️ [ontology] 全上場の読み込み失敗（{market}）: {str(e)[:100]}")
    return out


def build_company_entities(cfg, listed: list | None = None) -> list[dict]:
    """全上場（listed）を土台に、保有銘柄とスクリーナー通過銘柄の属性を重ねて企業エンティティを作る。"""
    rows: dict[str, dict] = {}
    for x in listed or []:
        tk = str(x.get("ticker") or "")
        if tk:
            rows[tk] = {"ticker": tk, "company_name": x.get("company_name") or tk, "market": x.get("market"),
                        "sector_code": str(x.get("sector_code") or ""), "size_tier": None, "business_model": None,
                        "is_portfolio": False, "layer": None, "updated_at": _NOW}
    # 保有銘柄
    p = REPO / "data" / "portfolio_holdings.csv"
    if p.exists():
        for enc in ("utf-8", "cp932"):
            try:
                with open(p, encoding=enc) as f:
                    for r in csv.DictReader(f):
                        tk = r.get("ticker", "")
                        if tk:
                            base = rows.get(tk, {})
                            rows[tk] = {
                                "ticker": tk,
                                "company_name": r.get("name") or base.get("company_name", ""),
                                "market": r.get("market") or base.get("market", ""),
                                "sector_code": r.get("sector_code") or base.get("sector_code", ""),
                                "size_tier": None,
                                "business_model": None,
                                "is_portfolio": True,
                                "layer": None,
                                "updated_at": _NOW,
                            }
                break
            except UnicodeDecodeError:
                continue
    # screener 通過銘柄（最新日）
    sh = _load_json(Path(cfg.DATA_DIR) / "screener_history.json")
    if sh:
        try:
            latest = sorted(sh.keys())[-1]
            for c in (sh[latest] or {}).get("candidates", []):
                tk = str(c.get("ticker", ""))
                if not tk:
                    continue
                e = rows.setdefault(tk, {
                    "ticker": tk, "company_name": c.get("name", tk),
                    "market": "JP" if tk.endswith(".T") else "US",
                    "sector_code": str(c.get("sector_code", "")),
                    "is_portfolio": False, "layer": None,
                    "updated_at": _NOW,
                })
                e["size_tier"] = c.get("size_tier")
                e["business_model"] = c.get("business_model")
        except Exception:
            pass
    return list(rows.values())


def build_signal_events(cfg) -> list[dict]:
    """screener通過・ドリフトをシグナルイベント化。"""
    events = []
    sh = _load_json(Path(cfg.DATA_DIR) / "screener_history.json")
    if sh:
        try:
            for date in sorted(sh.keys())[-30:]:   # 直近30営業日
                # screener_history の実体は {日付: [通過銘柄, ...]} のリスト。
                # list に .get() を呼ぶと AttributeError が except で握り潰され、
                # 常に0件になる（BQ ontology.signal_event が空になる）。
                _day = sh.get(date)
                if isinstance(_day, dict):
                    _rows = _day.get("candidates") or []
                elif isinstance(_day, list):
                    _rows = _day
                else:
                    continue
                for c in _rows:
                    if not isinstance(c, dict):
                        continue
                    # 履歴に載っている時点で通過済み。pass_filters は
                    # 履歴側に保存されないため、あるときだけ尊重する。
                    if "pass_filters" in c and not c.get("pass_filters"):
                        continue
                    tk = str(c.get("ticker", ""))
                    if not tk:
                        continue
                    events.append({
                        "event_id": _eid("screener_pass", date, tk),
                        "fired_at": date,
                        "ticker": tk,
                        "sector_code": str(c.get("sector_code", "")),
                        "signal_category": "screener_pass",
                        "signal_value": float(c.get("composite_score", 0) or 0),
                        "direction": "bullish",
                        "source_module": "stock_screener",
                        "detail": f"strategy={c.get('strategy_label', '')}",
                        "ingested_at": _NOW,
                    })
        except Exception:
            pass
    dh = _load_json(Path(cfg.INSIGHT_DIR) / "drift" / "drift_history.json")
    if isinstance(dh, list):
        for d in dh:
            if not isinstance(d, dict):
                continue
            fired = str(d.get("detected_at", ""))[:10] or None
            if not fired:
                continue
            delta = float(d.get("delta", 0) or 0)
            events.append({
                "event_id": _eid("drift", fired, d.get("code", "")),
                "fired_at": fired,
                "ticker": None,
                "sector_code": str(d.get("code", "")),
                "signal_category": f"drift_{d.get('drift_type', '')}",
                "signal_value": delta,
                "direction": "bearish" if delta < 0 else "bullish",
                "source_module": "drift_detector",
                "detail": f"severity={d.get('severity', '')}",
                "ingested_at": _NOW,
            })
    return events


ALERT_ITEMS_SQL = """
SELECT report_date, section_key, severity, ticker, market, line
FROM `{project}.analytics.alert_items_v`
WHERE ticker IS NOT NULL
"""
_BEARISH_KEYS = {"earnings_surprise_down", "financial_risk", "dead_cross"}


def alert_signal_events(client) -> list[dict]:
    """日次メールの注意項目（自己株式取得・増配・決算サプライズ・異常検知 等）に出た銘柄をシグナルイベントにする。"""
    events = []
    try:
        rows = client.query(ALERT_ITEMS_SQL.format(project=client.project)).result()
    except Exception as e:
        print(f"  ⚠️ [ontology] 注意項目の読み込み失敗: {str(e)[:100]}")
        return events
    for r in rows:
        day = r.report_date.isoformat() if hasattr(r.report_date, "isoformat") else str(r.report_date)
        key = str(r.section_key or "")
        events.append({
            "event_id": _eid("alert", day, key, r.ticker, (r.line or "")[:80]),
            "fired_at": day,
            "ticker": r.ticker,
            "sector_code": None,
            "signal_category": f"alert_{key}",
            "signal_value": None,
            "direction": "bearish" if key in _BEARISH_KEYS else "neutral",
            "source_module": "alert_notifier",
            "detail": (r.line or "")[:300],
            "ingested_at": _NOW,
        })
    return events


def build_theme_entities(cfg) -> list[dict]:
    """サブテーマJSON＋クラスターキャッシュをテーマエンティティ化。"""
    themes = []
    for fname, market in (("jp_sector_subtheme.json", "JP"),
                          ("us_sector_subtheme.json", "US")):
        data = _load_json(REPO / "data" / fname)
        if not isinstance(data, dict):
            continue
        for key, body in data.items():
            if not isinstance(body, dict):
                continue
            themes.append({
                "theme_id": _eid("subtheme", market, key),
                "theme_name": str(key),
                "theme_type": "subtheme",
                "market": market,
                "member_tickers": [],
                "heat_score": None,
                "updated_at": _NOW,
            })
    # technology_cluster_engine が書き出す cluster_phase_cache["clusters"] の実体は
    # **list**（build_cluster_phase_cache の docstring 参照）。dict とみなして
    # .items() を呼ぶと ontology_export 全体が落ち、ontology.signal_event /
    # decision_event が0行になる。
    # 参照キーも実体の列（cluster_id / sectors / avg_score）に合わせる
    # （name / tickers / phase_score は存在しない）。
    cl = _load_json(Path(cfg.CACHE_DIR) / "cluster_phase_cache.json")
    clusters = cl.get("clusters") if isinstance(cl, dict) else None
    if isinstance(clusters, dict):        # 将来 dict 形式に戻っても壊さない
        clusters = list(clusters.values())
    for body in (clusters or []):
        if not isinstance(body, dict):
            continue
        cid = str(body.get("cluster_id") or "")
        if not cid:
            continue
        themes.append({
            "theme_id": _eid("cluster", cid),
            # クラスターに人間可読名は無いので、代表技術ノードを名前にする。
            "theme_name": (", ".join(str(x) for x in
                                     (body.get("centroid_nodes") or [])[:3])
                           or cid),
            "theme_type": "tech_cluster",
            "market": str(body.get("market", "")),
            # member は銘柄ではなく業種コード（クラスタリングの単位が業種）。
            "member_tickers": [str(x) for x in (body.get("sectors") or [])][:50],
            "heat_score": body.get("avg_score"),
            "updated_at": _NOW,
        })
    return themes


def _decision_events_from_db(cfg) -> list[dict]:
    """invest.deliberations の書き出し（invest_deliberations.json）から意思決定イベントを作る。
    Actions 上でも vault 無しで読める。ファイルが無い・壊れていれば空。"""
    raw = _load_json(Path(getattr(cfg, "DATA_DIR", REPO / "stock_data" / "data"))
                     / "invest_deliberations.json")
    recs = raw.get("deliberations") if isinstance(raw, dict) else None
    events = []
    for r in recs or []:
        cid = str(r.get("case_id") or "")
        decided = cid[:10] if len(cid) >= 10 else str(r.get("deliberated_at") or "")[:10]
        if not cid or not r.get("ticker") or not decided:
            continue
        try:
            conviction = int(r["conviction"]) if r.get("conviction") is not None else None
        except (TypeError, ValueError):
            conviction = None
        events.append({
            "case_id": cid, "ticker": r["ticker"],
            "verdict": r.get("verdict"), "conviction": conviction,
            "layer": r.get("layer"), "open_issues": int(r.get("devil_unresolved") or 0),
            "status": r.get("approval_status") or "承認待ち", "result": "",
            "decided_at": decided, "ingested_at": _NOW,
        })
    return events


def build_decision_events(cfg) -> list[dict]:
    """意思決定イベント。DB（invest.deliberations の書き出し）を優先し、
    DB に無い case だけ vault の 40_判断台帳.md から補う（Mac 実行時のみ）。"""
    events = _decision_events_from_db(cfg)
    seen = {e["case_id"] for e in events}
    vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", ""))
    lp = vault / "40_判断台帳.md"
    if not lp.exists():
        return events
    spec = importlib.util.spec_from_file_location(
        "obsidian_sync", str(REPO / "obsidian_sync.py"))
    osync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(osync)
    for r in osync._parse_ledger_rows(lp.read_text(encoding="utf-8")):
        if r["case_id"] in seen:
            continue
        decided = r["case_id"][:10] if len(r["case_id"]) >= 10 else None
        if not decided:
            continue
        try:
            conviction = int(r["conviction"]) if r["conviction"] else None
        except ValueError:
            conviction = None
        try:
            open_issues = int(r["open_issues"]) if r["open_issues"] else 0
        except ValueError:
            open_issues = 0
        events.append({
            "case_id": r["case_id"], "ticker": r["ticker"],
            "verdict": r["verdict"], "conviction": conviction,
            "layer": r["layer"], "open_issues": open_issues,
            "status": r["status"], "result": r["result"],
            "decided_at": decided, "ingested_at": _NOW,
        })
    return events


# ==============================================================
# BLOCK 2: 出力
# ==============================================================
# 書き込みは load job（無料枠。ストリーミング挿入は課金対象で、週ごとに同じ行が積み増しになる）。
#   company_entity / theme_entity / decision_event : 毎回その時点の全体で置き換える（WRITE_TRUNCATE）
#   signal_event : 一時表へ load → event_id で MERGE（過去分は残し、同じイベントは上書き）。週1本のクエリ
# ontology データセットは location=US。

_STAGE = "_signal_event_stage"


def _load(client, table: str, rows: list, disposition: str) -> int:
    from google.cloud import bigquery
    ref = f"{client.project}.ontology.{table}"
    schema = client.get_table(f"{client.project}.ontology.signal_event" if table == _STAGE else ref).schema
    client.load_table_from_json(rows, ref, location="US", job_config=bigquery.LoadJobConfig(
        schema=schema, write_disposition=disposition)).result()
    return len(rows)


def dedupe_signal_events(client) -> int:
    """同じ event_id が複数あれば1行に整理する（以前のストリーミング挿入で積み増しになった分）。"""
    p = client.project
    r = list(client.query(f"SELECT COUNT(*) n, COUNT(DISTINCT event_id) d FROM `{p}.ontology.signal_event`").result())[0]
    if r.n == r.d:
        return 0
    client.query(f"""
CREATE OR REPLACE TABLE `{p}.ontology.signal_event` PARTITION BY fired_at AS
SELECT * EXCEPT (rn) FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY ingested_at DESC) AS rn
  FROM `{p}.ontology.signal_event`) WHERE rn = 1""").result()
    return r.n - r.d


def write_bq(client, tables: dict) -> dict:
    from google.cloud import bigquery
    W = bigquery.WriteDisposition
    out = {}
    for name in ("company_entity", "theme_entity", "decision_event"):
        rows = tables.get(name) or []
        out[name] = _load(client, name, rows, W.WRITE_TRUNCATE) if rows else 0
    out["signal_event_dedup_removed"] = dedupe_signal_events(client)
    sig = list({r["event_id"]: r for r in tables.get("signal_event") or []}.values())
    if sig:
        _load(client, _STAGE, sig, W.WRITE_TRUNCATE)
        p = client.project
        cols = ["event_id", "fired_at", "ticker", "sector_code", "signal_category", "signal_value",
                "direction", "source_module", "detail", "ingested_at"]
        client.query(f"""
MERGE `{p}.ontology.signal_event` T
USING `{p}.ontology.{_STAGE}` S ON T.event_id = S.event_id
WHEN MATCHED THEN UPDATE SET signal_value = S.signal_value, detail = S.detail, ingested_at = S.ingested_at
WHEN NOT MATCHED THEN INSERT ({", ".join(cols)}) VALUES ({", ".join("S." + c for c in cols)})""").result()
    out["signal_event"] = len(sig)
    return out


def build_tables(cfg, client=None) -> dict:
    listed = listed_universe(client) if client is not None else []
    signals = build_signal_events(cfg) + (alert_signal_events(client) if client is not None else [])
    return {
        "company_entity": build_company_entities(cfg, listed),
        "signal_event":   signals,
        "theme_entity":   build_theme_entities(cfg),
        "decision_event": build_decision_events(cfg),
    }


def _dry_run(cfg, tables: dict) -> None:
    out_dir = Path(getattr(cfg, "DRIVE_BASE", REPO / "stock_data")) / "ontology"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        (out_dir / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        print(f"  📝 {name}: {len(rows)}行")


def export(dry_run: bool) -> None:
    cfg = _load_config()
    if dry_run:
        _dry_run(cfg, build_tables(cfg))
        print("✅ dry-run 完了（BQ未接続）")
        return
    print(run_ontology_exporter(cfg))


def run_ontology_exporter(cfg, results: dict = None) -> dict:
    """
    stock_analyzer_main.py から呼ばれる週次エントリ。
    BQ の認証がある環境（Actions）では load job / MERGE で書き、無い環境では JSONL に出す。
    """
    has_bq = bool(os.environ.get("BQ_CREDENTIALS", "") or os.environ.get("BQ_CREDENTIALS_PATH", ""))
    if not has_bq:
        tables = build_tables(cfg)
        _dry_run(cfg, tables)
        return {"mode": "dry_run", "stats": {k: len(v) for k, v in tables.items()}, "success": True}
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    tables = build_tables(cfg, client)
    stats = {k: len(v) for k, v in tables.items()}
    try:
        written = write_bq(client, tables)
        print(f"  🕸 [ontology] {written}")
        return {"mode": "bq", "stats": stats, "written": written, "success": True}
    except Exception as e:
        print(f"  ❌ [ontology] BQ 書き込み失敗: {str(e)[:200]}")
        return {"mode": "bq", "stats": stats, "success": False, "error": str(e)[:200]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="オントロジー層 ETL (ONT-1)")
    ap.add_argument("--dry-run", action="store_true",
                    help="BQ接続なしでJSONLをローカル出力")
    args = ap.parse_args()
    export(args.dry_run)
