# ==============================================================
# report_store.py
# 日次の md レポート（insights/macro・sectors・us_sectors）を読み、BigQuery analytics に溜める。
# ==============================================================
# 【なぜ要るか】
#   マクロ・業種のレポートは Drive の md にしか残らず、日をまたいで比べたり探したりできなかった。
#   生成側（base_agent・stock_analyzer_main・us_sector_pipeline・anomaly_detector ほか）は変えずに、
#   できあがった md を読んで構造化する。過去分も同じ処理で取り込める（--backfill）。
# 【表】（すべて analytics・load job の WRITE_APPEND＝無料。ストリーミング挿入は課金対象なので使わない）
#   report_texts         … 1レポート1行。種類・日付・キー（業種コード等）・題名・本文（全文検索用）
#   sector_scores_daily  … 日本33業種（sectors/{code}_{date}.md の見出し）と米国11セクター（us_sector_scores_{date}.csv）
#   macro_indicators_daily … マクロ指標（macro_report の表）と米国債の年限別利回り（bond_commodity の表）
#   macro_signals_daily  … マクロシグナル×業種の判定（macro_report の節）
# 【重複】
#   load job の job_id は「表・内容」から決めるので、同じ内容の再実行は二重に入らない。
#   同じ日のレポートが作り直されて内容が変わったときは行が増えるので、読む側（BI マート）で fetched_at が新しい行を採る。
# ==============================================================
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

DATASET = "analytics"
LOCATION = "asia-northeast1"
_DATE_RE = re.compile(r"(\d{4}-\d{2}(?:-\d{2})?)")
_RISK = {"💚": "low", "🟡": "medium", "🔴": "high", "低": "low", "中": "medium", "高": "high"}
_JUDGE = {"💚": "tailwind", "🔴": "headwind", "🟡": "watch"}


# ── 解析（純関数） ────────────────────────────────────────────
def report_kind(path: Path) -> tuple:
    """(種類, キー, 日付)。対象外は None。"""
    name = path.name
    parent = path.parent.name
    m = _DATE_RE.search(name)
    if not m or path.suffix not in (".md", ".csv"):
        return None
    d = m.group(1)
    if len(d) == 7:           # 月次（policy_changes_2026-10 など）は月初で持つ
        d += "-01"
    if parent == "sectors":
        code = name.split("_")[0]
        return ("sector", code, d) if code.isdigit() and path.suffix == ".md" else None
    stem = name[: m.start()].rstrip("_")
    if parent in ("macro", "us_sectors") and path.suffix == ".md":
        return (stem, "", d)
    return None


def _num(s: str):
    s = str(s or "").replace(",", "").replace("%", "").replace("$", "").replace("円", "").strip()
    s = s.replace("＋", "+").replace("−", "-")
    try:
        return float(s)
    except ValueError:
        return None


def _rows(md: str, header_startswith: str) -> list:
    """見出し行が header_startswith で始まる md 表の本体行（セルのリスト）。"""
    out, on = [], False
    for line in md.splitlines():
        if line.startswith(header_startswith):
            on = True
            continue
        if on:
            if re.match(r"^\|\s*:?-{3,}", line):
                continue
            if not line.startswith("|"):
                if out:
                    break
                continue
            out.append([c.strip() for c in line.strip().strip("|").split("|")])
    return out


def parse_jp_sector(md: str) -> dict:
    """日本の業種レポートの見出しから 業種名・総合スコア・リスク・マクロシグナル。"""
    name = re.search(r"^## (.+?) 分析レポート", md, re.M)
    sc = re.search(r"\*\*総合スコア\*\*: (-?[\d.]+)点", md)
    rk = re.search(r"\*\*リスク\*\*: (\S+)", md)
    sig = []
    m = re.search(r"### マクロシグナル\n(.*?)(?:\n### |\Z)", md, re.S)
    if m:
        for line in m.group(1).splitlines():
            t = line.strip().lstrip("-").strip()
            if t and "特筆すべき" not in t:
                sig.append(t)
    return {"sector_name": name.group(1).strip() if name else None,
            "score": float(sc.group(1)) if sc else None,
            "risk_level": _RISK.get(rk.group(1)[:1], _RISK.get(rk.group(1), None)) if rk else None,
            "signals": " | ".join(sig)[:1000] or None}


def parse_us_scores_csv(text: str) -> list:
    rows = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        rows.append({"sector_code": r.get("etf"), "sector_name": r.get("sector_ja") or r.get("sector_en"),
                     "score": _num(r.get("score")), "risk_level": (r.get("risk_level") or None),
                     "ret_1m": _num(r.get("ret_1m")), "ret_3m": _num(r.get("ret_3m")), "ret_12m": _num(r.get("ret_12m")),
                     "signals": (r.get("signals") or "").replace("|", " | ")[:1000] or None})
    return rows


def parse_macro_report(md: str) -> tuple:
    """(指標 [(名前, 値)], シグナル [(シグナル, きっかけ, 業種, 判定, 理由)])"""
    ind = [(c[0], _num(c[1])) for c in _rows(md, "| 指標 | 値 |") if len(c) >= 2]
    sigs = []
    for m in re.finditer(r"^### (.+?)（(.*?)）\n\n(\|.*?)(?=\n\n|\Z)", md, re.M | re.S):
        for c in _rows(m.group(3), "| 業種 | 判定 | 理由 |"):
            if len(c) >= 2:
                sigs.append((m.group(1).strip(), m.group(2).strip(), c[0], _JUDGE.get(c[1][:1], c[1]), c[2] if len(c) > 2 else ""))
    return ind, sigs


def parse_bond_yields(md: str) -> list:
    """債券・コモディティの米国債イールドの表から [(年限, 利回り%, 1か月差%)]"""
    out = []
    for c in _rows(md, "| 年限 | 利回り |"):
        if len(c) >= 2:
            chg = re.search(r"([+-]?\d+\.\d+)%", c[2]) if len(c) > 2 else None
            out.append((c[0], _num(c[1]), float(chg.group(1)) if chg else None))
    return out


def build_all(paths: list, fetched_at: str) -> dict:
    """md/csv のパス一覧から4表ぶんの行を作る。"""
    out = {"report_texts": [], "sector_scores_daily": [], "macro_indicators_daily": [], "macro_signals_daily": []}
    for p in sorted(paths):
        k = report_kind(p)
        csv_us = p.parent.name == "us_sectors" and p.name.startswith("us_sector_scores_") and p.suffix == ".csv"
        if not k and not csv_us:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if csv_us:
            d = _DATE_RE.search(p.name).group(1)
            for r in parse_us_scores_csv(text):
                out["sector_scores_daily"].append(dict(r, report_date=d, market="US", fetched_at=fetched_at))
            continue
        kind, key, d = k
        title = next((l.lstrip("#").strip() for l in text.splitlines() if l.startswith("#")), p.stem)
        out["report_texts"].append({"report_date": d, "report_type": kind, "report_key": key, "title": title[:300],
                                    "body_md": text[:200000], "chars": len(text), "source_path": f"{p.parent.name}/{p.name}",
                                    "fetched_at": fetched_at})
        if kind == "sector":
            r = parse_jp_sector(text)
            out["sector_scores_daily"].append(dict(r, report_date=d, market="JP", sector_code=key, ret_1m=None,
                                                   ret_3m=None, ret_12m=None, fetched_at=fetched_at))
        elif kind == "macro_report":
            ind, sigs = parse_macro_report(text)
            for name, v in ind:
                out["macro_indicators_daily"].append({"report_date": d, "source": "macro_report", "indicator": name,
                                                      "value": v, "change_1m": None, "fetched_at": fetched_at})
            for s, trig, sec, j, why in sigs:
                out["macro_signals_daily"].append({"report_date": d, "signal": s, "trigger_value": trig, "sector_name": sec,
                                                   "judgement": j, "reason": why or None, "fetched_at": fetched_at})
        elif kind == "bond_commodity":
            for name, v, chg in parse_bond_yields(text):
                out["macro_indicators_daily"].append({"report_date": d, "source": "bond_commodity", "indicator": name,
                                                      "value": v, "change_1m": chg, "fetched_at": fetched_at})
    return out


# ── BigQuery ─────────────────────────────────────────────────
def schemas():
    from google.cloud import bigquery as bq
    F = bq.SchemaField
    ts = [F("fetched_at", "TIMESTAMP")]
    return {
        "report_texts": [F("report_date", "DATE", "REQUIRED"), F("report_type", "STRING"), F("report_key", "STRING"),
                         F("title", "STRING"), F("body_md", "STRING"), F("chars", "INT64"), F("source_path", "STRING")] + ts,
        "sector_scores_daily": [F("report_date", "DATE", "REQUIRED"), F("market", "STRING"), F("sector_code", "STRING"),
                                F("sector_name", "STRING"), F("score", "FLOAT64"), F("risk_level", "STRING"),
                                F("ret_1m", "FLOAT64"), F("ret_3m", "FLOAT64"), F("ret_12m", "FLOAT64"),
                                F("signals", "STRING")] + ts,
        "macro_indicators_daily": [F("report_date", "DATE", "REQUIRED"), F("source", "STRING"), F("indicator", "STRING"),
                                   F("value", "FLOAT64"), F("change_1m", "FLOAT64")] + ts,
        "macro_signals_daily": [F("report_date", "DATE", "REQUIRED"), F("signal", "STRING"), F("trigger_value", "STRING"),
                                F("sector_name", "STRING"), F("judgement", "STRING"), F("reason", "STRING")] + ts,
    }


def job_id_for(table: str, rows: list) -> str:
    """表と内容（fetched_at を除く）から決める。同じ内容の再実行は同じ ID になり二重に入らない。"""
    body = json.dumps(sorted(json.dumps({k: v for k, v in r.items() if k != "fetched_at"}, ensure_ascii=False, sort_keys=True)
                             for r in rows), ensure_ascii=False)
    return f"rpt_{table}_" + hashlib.sha1(body.encode()).hexdigest()[:24]


def write(client, built: dict, tmp_dir: Path) -> dict:
    from google.api_core.exceptions import Conflict, NotFound
    from google.cloud import bigquery
    sch = schemas()
    counts = {}
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for table, rows in built.items():
        if not rows:
            counts[table] = 0
            continue
        ref = f"{client.project}.{DATASET}.{table}"
        try:
            client.get_table(ref)
        except NotFound:
            t = bigquery.Table(ref, schema=sch[table])
            t.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.MONTH, field="report_date")
            client.create_table(t)
        tmp = tmp_dir / f"report_store_{table}.ndjson"
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        cfg = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON, schema=sch[table],
                                     write_disposition=bigquery.WriteDisposition.WRITE_APPEND)
        jid = job_id_for(table, rows)
        try:
            with open(tmp, "rb") as fh:
                client.load_table_from_file(fh, ref, job_id=jid, job_config=cfg, location=LOCATION).result()
            counts[table] = len(rows)
        except Conflict:
            old = client.get_job(jid, location=LOCATION)
            if not (old.state == "DONE" and not old.error_result):
                raise RuntimeError(f"load job {jid} は既にあるが失敗している")
            counts[table] = 0
        finally:
            tmp.unlink(missing_ok=True)
    return counts


def collect(insight_dir: Path, day: str | None) -> list:
    """対象ファイル。day を渡すとその日付（月次は月）を名前に含むものだけ。"""
    out = []
    for sub in ("macro", "sectors", "us_sectors"):
        for p in (insight_dir / sub).glob("*"):
            if p.is_file() and (day is None or day in p.name or day[:7] + ".md" in p.name):
                out.append(p)
    return out


def run(cfg, day: str | None = None, backfill: bool = False) -> dict:
    insight = Path(getattr(cfg, "INSIGHT_DIR", "stock_data/insights"))
    day = None if backfill else (day or date.today().isoformat())
    paths = collect(insight, day)
    built = build_all(paths, datetime.now(timezone.utc).isoformat())
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    tmp = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "cache"
    if backfill:
        # 1回の load を小さく保つため月ごとに分ける
        counts: dict = {}
        months = sorted({r["report_date"][:7] for rows in built.values() for r in rows})
        for m in months:
            part = {t: [r for r in rows if r["report_date"].startswith(m)] for t, rows in built.items()}
            for t, n in write(client, part, tmp).items():
                counts[t] = counts.get(t, 0) + n
    else:
        counts = write(client, built, tmp)
    print(f"📚 report_store: ファイル {len(paths)} → " + " / ".join(f"{t} {n}行" for t, n in counts.items()))
    return {"files": len(paths), **counts}


if __name__ == "__main__":
    import argparse
    import sys
    import config as _cfg
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD（既定は今日）")
    ap.add_argument("--backfill", action="store_true", help="insights にある全日付を取り込む")
    a = ap.parse_args()
    try:
        res = run(_cfg, a.date, a.backfill)
        status, note = "success", json.dumps(res, ensure_ascii=False)
    except Exception as exc:
        status, note = "error", f"{type(exc).__name__}: {str(exc)[:150]}"
        print(f"❌ report_store: {note}")
    try:
        from module_ledger import record_external
        record_external(_cfg, "report_store", status, note[:200])
    except Exception:
        pass
    sys.exit(0 if status == "success" else 1)
