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

def build_company_entities(cfg) -> list[dict]:
    """portfolio_holdings + screener_history から企業エンティティを構築。"""
    rows: dict[str, dict] = {}
    # 保有銘柄
    p = REPO / "data" / "portfolio_holdings.csv"
    if p.exists():
        for enc in ("utf-8", "cp932"):
            try:
                with open(p, encoding=enc) as f:
                    for r in csv.DictReader(f):
                        tk = r.get("ticker", "")
                        if tk:
                            rows[tk] = {
                                "ticker": tk,
                                "company_name": r.get("name", ""),
                                "market": r.get("market", ""),
                                "sector_code": r.get("sector_code", ""),
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
                for c in (sh[date] or {}).get("candidates", []):
                    if not (isinstance(c, dict) and c.get("pass_filters")):
                        continue
                    tk = str(c.get("ticker", ""))
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
    cl = _load_json(Path(cfg.CACHE_DIR) / "cluster_phase_cache.json")
    if isinstance(cl, dict):
        for cid, body in (cl.get("clusters", cl) or {}).items():
            if not isinstance(body, dict):
                continue
            themes.append({
                "theme_id": _eid("cluster", cid),
                "theme_name": str(body.get("name", cid)),
                "theme_type": "tech_cluster",
                "market": str(body.get("market", "")),
                "member_tickers": list(body.get("tickers", []))[:50],
                "heat_score": body.get("phase_score"),
                "updated_at": _NOW,
            })
    return themes


def build_decision_events(cfg) -> list[dict]:
    """投資部 40_判断台帳.md を意思決定イベント化。"""
    vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", ""))
    lp = vault / "40_判断台帳.md"
    if not lp.exists():
        return []
    spec = importlib.util.spec_from_file_location(
        "obsidian_sync", str(REPO / "obsidian_sync.py"))
    osync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(osync)
    events = []
    for r in osync._parse_ledger_rows(lp.read_text(encoding="utf-8")):
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

def export(dry_run: bool) -> None:
    cfg = _load_config()
    tables = {
        "company_entity": build_company_entities(cfg),
        "signal_event":   build_signal_events(cfg),
        "theme_entity":   build_theme_entities(cfg),
        "decision_event": build_decision_events(cfg),
    }
    if dry_run:
        out_dir = Path(cfg.DRIVE_BASE) / "ontology"
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, rows in tables.items():
            p = out_dir / f"{name}.jsonl"
            p.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                encoding="utf-8")
            print(f"  📝 {name}: {len(rows)}行 → {p}")
        print("✅ dry-run 完了（BQ未接続）")
        return
    import edinet_bq_client as bqc
    for name, rows in tables.items():
        if not rows:
            print(f"  ⏩ {name}: 0行（スキップ）")
            continue
        ok = bqc.insert_rows(rows=rows, table_ref=f"ontology.{name}")
        print(f"  {'✅' if ok else '❌'} {name}: {len(rows)}行")
    print("✅ BQ 取込完了")


def run_ontology_exporter(cfg, results: dict = None) -> dict:
    """
    stock_analyzer_main.py から呼ばれる週次エントリ（Wave ONT-1 本番配線）。
    GitHub Actions 環境（BQ_CREDENTIALS あり）でのみ実書き込みし、
    認証が無い環境では dry-run（JSONLローカル出力）に自動フォールバックする。
    """
    import os
    tables = {
        "company_entity": build_company_entities(cfg),
        "signal_event":   build_signal_events(cfg),
        "theme_entity":   build_theme_entities(cfg),
        "decision_event": build_decision_events(cfg),
    }
    stats = {name: len(rows) for name, rows in tables.items()}
    has_bq = bool(os.environ.get("BQ_CREDENTIALS", "")
                  or os.environ.get("BQ_CREDENTIALS_PATH", ""))
    if not has_bq:
        out_dir = Path(getattr(cfg, "DRIVE_BASE", REPO / "stock_data")) / "ontology"
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, rows in tables.items():
            (out_dir / f"{name}.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                encoding="utf-8")
        return {"mode": "dry_run", "stats": stats, "success": True}
    import edinet_bq_client as bqc
    ok_all = True
    for name, rows in tables.items():
        if rows:
            ok_all &= bqc.insert_rows(rows=rows, table_ref=f"ontology.{name}")
    return {"mode": "bq", "stats": stats, "success": ok_all}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="オントロジー層 ETL (ONT-1)")
    ap.add_argument("--dry-run", action="store_true",
                    help="BQ接続なしでJSONLをローカル出力")
    args = ap.parse_args()
    export(args.dry_run)
