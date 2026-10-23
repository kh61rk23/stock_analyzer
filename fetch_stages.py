# ==============================================================
# fetch_stages.py — 分析の前に済ませておく取得処理（BigQuery に書くだけのもの）
# ==============================================================
# 【使い方】
#   日次本体（stock_analyzer_main.run_all_tasks）と取得専用の workflow（fetch_runner.py）が同じ関数を呼ぶ。
#   取得 workflow は朝早く動いて、結果の要約と実行台帳の記録を
#   stock_data/data/fetch_external/<group>_<日付>.json に残す（状態として BigQuery に保存される）。
#   日次本体は環境変数 STOCK_ANALYZER_FETCH_EXTERNAL にグループ名があり、その日のファイルがあれば
#   取得をせずに中身を results と台帳へ写す（apply_external）。ファイルが無ければ今までどおり自分で取る。
# 【グループ】
#   jp_filings … 賃貸等不動産（EdinetDB）・政府 EDINET の公正価値・EdinetDB の財務と有報・書類索引・不動産DB・
#                EdinetDB の追加の窓口（edinetdb_extras・本体の後で残しておいた枠を使う）
# 【なぜ分けるか】
#   どれも BigQuery に書くだけで、後ろの分析はメモリ上の結果ではなく BQ を読む。
#   results に残すのはメールの要約と実行台帳に使う小さな dict だけなので、ファイルで受け渡せる。
# ==============================================================
from __future__ import annotations

import importlib
import json
import os
from datetime import date
from pathlib import Path

GROUPS = {"jp_filings": ("rental_property_fv", "edinet_gov_fv", "edinetdb_ingest", "edinet_doc_index", "fudosan_ingest",
                        "edinetdb_extras")}
EXTERNAL_ENV = "STOCK_ANALYZER_FETCH_EXTERNAL"


class LedgerRecorder:
    """logger.add を受けて記録しつつ、元の logger（あれば）にも渡す。"""

    def __init__(self, inner=None):
        self.inner = inner
        self.entries: list[dict] = []

    def add(self, module: str, status: str, note: str = ""):
        self.entries.append({"module": module, "status": status, "note": note})
        if self.inner is not None:
            self.inner.add(module, status, note)


def _import(name: str):
    try:
        return importlib.import_module(name)
    except Exception as e:
        print(f"  ⚠️  {name} を読み込めない: {type(e).__name__}: {str(e)[:80]}")
        return None


def run_jp_filings(cfg, results: dict, logger, in_mode=lambda key: True) -> None:
    """EdinetDB・政府 EDINET・不動産DB の取り込み。results と logger に結果を入れる。"""
    if getattr(cfg, "ENABLE_EDINETDB_INGEST", True) and in_mode("edinetdb_ingest"):
        # 賃貸等不動産（EdinetDB real-estate）を、日次取り込みの前に RENTAL_FV_DAILY_BUDGET 社まで取る
        # （無効の間は RENTAL_FV_TRIAL_LIMIT 社を1回だけ試す）。残りの枠を日次取り込みが使う
        try:
            import rental_property_ingestor as _rpi
            results["rental_property_fv"] = _rpi.run(cfg)
        except Exception as _rpe:
            results["rental_property_fv"] = {"error": str(_rpe)[:150]}
            print(f"  ⚠️  賃貸等不動産の取得エラー（スキップ）: {str(_rpe)[:80]}")
        # IFRS 会社の投資不動産の公正価値（政府 EDINET API）。キーが無ければ何もしない。失敗しても本処理は続ける
        if os.environ.get("EDINET_GOV_API_KEY") or getattr(cfg, "EDINET_GOV_API_KEY", ""):
            try:
                import edinet_gov_xbrl as _egx
                results["edinet_gov_fv"] = _egx.ingest(cfg)
            except Exception as _ege:
                results["edinet_gov_fv"] = {"error": type(_ege).__name__}
                print(f"  ⚠️  政府 EDINET の公正価値取得エラー（スキップ）: {type(_ege).__name__}")
        try:
            _edi = _import("edinetdb_ingestor")
            if _edi:
                value = results["edinetdb_ingest"] = _edi.run_edinetdb_ingest(cfg)
                # value["failure"] には「EdinetDBにデータ無し」（404）が混ざるので、実問題だけで判定する。
                # データ無しのみの場合は success（件数は note に残る）。対象があるのに取り込み0社は warn。
                if value.get("skipped"):
                    _st = "skipped"
                elif value.get("failure"):
                    _st = "alert"
                elif value.get("targets") and not value.get("success"):
                    _st = "warn"
                else:
                    _st = "success"
                _names = "、".join(
                    f"{c.get('sec_code') or c.get('edinet_code')} {c.get('name', '')}".strip()
                    for c in value.get("ingested", []) or []) or "なし"
                logger.add(
                    "edinetdb_ingest", _st,
                    f"取込{value.get('success', 0)}社 / データ無し{value.get('missing', 0)}社 / "
                    f"失敗{value.get('failure', 0)}社 / 書類{value.get('doc_index_rows', 0)}件 / "
                    f"API{value.get('api_calls', 0)}回 | 取込: {_names}",
                )
        except Exception as exc:
            logger.add("edinetdb_ingest", "error", str(exc)[:100])

    # 政府 EDINET 検索を使わず有報 doc_index を更新
    if in_mode("edinet_doc_index"):
        try:
            _indexer = _import("edinet_doc_indexer")
            if _indexer:
                value = results["edinet_doc_index"] = _indexer.run_doc_indexer(cfg=cfg)
                logger.add("edinet_doc_index", "skipped" if value.get("skipped") else "success", str(value))
        except Exception as exc:
            logger.add("edinet_doc_index", "error", str(exc)[:100])

    # 不動産DB P1 を BQ へ格納
    if getattr(cfg, "ENABLE_FUDOSAN_INGEST", True) and in_mode("fudosan_ingest"):
        try:
            _fudo = _import("fudosan_ingestor")
            if _fudo:
                value = results["fudosan_ingest"] = _fudo.run_fudosan_ingest(cfg)
                logger.add("fudosan_ingest",
                           "skipped" if value.get("skipped") else ("success" if not value.get("failure") else "alert"),
                           str(value))
        except Exception as exc:
            logger.add("fudosan_ingest", "error", str(exc)[:100])


    # EdinetDB の追加の窓口（決算短信・政策保有株・関係会社・主要販売先）。本体が残した EDINETDB_EXTRAS_DAILY_CALLS 回で回す
    if getattr(cfg, "ENABLE_EDINETDB_INGEST", True) and in_mode("edinetdb_extras"):
        try:
            import edinetdb_extras as _ex
            value = results["edinetdb_extras"] = _ex.run(cfg)
            logger.add("edinetdb_extras", "skipped" if value.get("skipped") else "success",
                       f"取得{value.get('ok', 0)}件 / データ無し{value.get('no_data', 0)}件"
                       f"{' / 止まった理由 ' + str(value['stopped']) if value.get('stopped') else ''}"
                       f" | 進み具合 {value.get('progress', {})}" if not value.get("skipped") else value.get("reason", ""))
        except Exception as exc:
            logger.add("edinetdb_extras", "error", str(exc)[:100])

RUNNERS = {"jp_filings": run_jp_filings}


def external_path(cfg, group: str, day: date | None = None) -> Path:
    day = day or date.today()
    base = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    return base / "fetch_external" / f"{group}_{day.isoformat()}.json"


def _jsonable(v):
    try:
        json.dumps(v, ensure_ascii=False)
        return v
    except (TypeError, ValueError):
        return json.loads(json.dumps(v, ensure_ascii=False, default=str))


def save_external(cfg, group: str, results: dict, entries: list, day: date | None = None) -> Path:
    keys = GROUPS[group]
    payload = {"group": group, "date": (day or date.today()).isoformat(),
               "results": {k: _jsonable(results[k]) for k in keys if k in results},
               "ledger": entries}
    p = external_path(cfg, group, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def external_groups() -> set[str]:
    return {g.strip() for g in os.environ.get(EXTERNAL_ENV, "").split(",") if g.strip() in GROUPS}


def apply_external(cfg, group: str, results: dict, logger, day: date | None = None) -> bool:
    """取得 workflow がその日に残した結果があれば results と台帳へ写して True。無ければ False（呼び出し側が自分で取る）。"""
    if group not in external_groups():
        return False
    p = external_path(cfg, group, day)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"  ↩️  取得 workflow の結果（{p.name}）が無いので、この回で取る")
        return False
    results.update(payload.get("results") or {})
    for e in payload.get("ledger") or []:
        logger.add(e["module"], e["status"], e.get("note", ""))
    print(f"  📥 取得 workflow の結果を使う（{p.name}・{len(payload.get('ledger') or [])}件）")
    return True
