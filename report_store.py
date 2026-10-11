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
#   news_articles        … ニュース記事（data/news_cache/news_{date}.json の全記事。md は上位10本と業種ごと3本しか出ない）
#   etf_theme_daily      … テーマETFの熱量（data/etf_cache/thematic_{date}.json、無い日は etf/thematic_{date}.md のランキング表）
#   etf_daily            … テーマETFの個別 ETF（同 JSON、無い日は md の詳細＝上位テーマ分のみ）
#   screener_trace_daily … 日本株スクリーナーの全候補（通過＝点数の経路・9指標、落選＝理由）。data/screener_trace/trace_{date}.json、
#                          無い日は screener/screener_trace_{date}.md（＋data/screener_rejections.json の落選）
#   imf_forecasts        … IMF の見通し（国×指標×年。imf/imf_{date}.md の表）
#   gov_stats            … 政府統計の最新値・前月比・前年比・シグナル（gov/gov_report_{date}.md の表）
#   上記以外の md（週次・アイデア・事業概要・米国スクリーナー・銘柄詳細・月次の配分提案や税務など）も report_texts に全文を入れる。
# 【どれを入れるか】
#   取り込み記録 data/report_store_state.json（insights からの相対パス→内容の sha1）と違うファイル＝新しいファイルと中身が変わったファイルだけ。
#   週次・月次・*_latest.md のように日付の付き方がまちまちでも、日次処理で取りこぼさない。
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
_D_PATTERNS = (
    (re.compile(r"(\d{4}-\d{2}-\d{2})"), lambda m: m.group(1)),
    (re.compile(r"(\d{4})-W(\d{2})"), lambda m: date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1).isoformat()),
    (re.compile(r"(\d{4}-\d{2})(?!-?\d)"), lambda m: m.group(1) + "-01"),
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(?!\d)"), lambda m: f"{m.group(1)}-{m.group(2)}-01"),
    (re.compile(r"(?<!\d)(20\d{2})(?!\d)"), lambda m: f"{m.group(1)}-01-01"),
)


def _date_token(name: str):
    """ファイル名の日付（日・ISO週・月・年の順に探す）。(日付, 一致) か (None, None)。"""
    for rx, f in _D_PATTERNS:
        m = rx.search(name)
        if m:
            try:
                return f(m), m
            except ValueError:
                continue
    return None, None


def report_kind(path: Path) -> tuple:
    """(種類, キー, 日付)。対象外は None。日付の無い *_latest.md などは更新日時の日付。"""
    if path.suffix != ".md":
        return None
    name, parent = path.name, path.parent.name
    if parent == "sectors":
        code = name.split("_")[0]
        d, _ = _date_token(name)
        return ("sector", code, d) if code.isdigit() and d else None
    if parent.startswith("detail_"):           # screener/detail_{date}/{ticker}.md（銘柄レポート）
        d, _ = _date_token(parent)
        d = d or datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
        return ("stock_detail", path.stem.replace("_", "."), d)
    d, m = _date_token(name)
    if m:
        kind = name[: m.start()].rstrip("_-") or path.stem
    else:
        kind = re.sub(r"_latest$", "", path.stem)
        d = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    return (kind, "", d)


def _num(s):
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return float(s)
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


def news_rows(articles: list, d: str, fetched_at: str) -> list:
    """news_cache の記事リストから行（純関数）。同じ URL は1行。"""
    out, seen = [], set()
    for a in articles or []:
        url = (a.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"report_date": d, "url": url[:1000], "title": (a.get("title") or "").strip()[:500],
                    "summary": (a.get("summary") or "").strip()[:1500] or None, "source": a.get("source"),
                    "priority": a.get("priority"), "lang": a.get("lang"), "pub_date": (a.get("pub_date") or None),
                    "matched_sectors": ",".join(str(x) for x in (a.get("matched_sectors") or [])) or None,
                    "importance_score": _num(a.get("importance_score")),
                    "investment_relevant": bool(a.get("investment_relevant")) if a.get("investment_relevant") is not None else None,
                    "sentiment_label": a.get("sentiment_label"), "sentiment_score": _num(a.get("sentiment_score")),
                    "fetched_at": fetched_at})
    return out


def etf_rows_from_json(data: dict, d: str, fetched_at: str) -> tuple:
    themes, etfs = [], []
    for t in (data or {}).get("themes", []):
        ad = t.get("arxiv_detail") or {}
        themes.append({"report_date": d, "theme_name": t.get("theme_name"), "etfs": "/".join(t.get("etfs") or []),
                       "heat_score": _num(t.get("heat_score")), "etf_momentum_score": _num(t.get("etf_momentum_score")),
                       "arxiv_trend_score": _num(t.get("arxiv_trend_score")), "arxiv_growth_rate": _num(ad.get("growth_rate")),
                       "arxiv_recent_4w": _num(ad.get("recent_4w")), "arxiv_prev_4w": _num(ad.get("prev_4w")),
                       "signal": t.get("signal"), "fetched_at": fetched_at})
        for e in t.get("etf_details") or []:
            etfs.append({"report_date": d, "theme_name": t.get("theme_name"), "ticker": e.get("ticker"),
                         "price": _num(e.get("price")), "ret_1m": _num(e.get("ret_1m")), "ret_3m": _num(e.get("ret_3m")),
                         "ma50_ratio": _num(e.get("ma50_ratio")), "vol_ratio": _num(e.get("vol_ratio")), "fetched_at": fetched_at})
    return themes, etfs


_ETF_LINE = re.compile(r"^- \*\*(?P<t>[\w.\-]+)\*\*: \$(?P<p>[\d.]+)\s+1M:(?P<m1>[+-]?[\d.]+)%\s+3M:(?P<m3>[+-]?[\d.]+)%\s+MA50乖離:(?P<ma>[+-]?[\d.]+)%\s+出来高前月比:(?P<v>[+-]?[\d.]+)%")
_SIG = {"🔥": "hot", "📈": "warming", "➡️": "neutral", "❄️": "cooling"}


def etf_rows_from_md(md: str, d: str, fetched_at: str) -> tuple:
    """JSON が無い日の md から（ランキング表は全テーマ、ETF 詳細は上位テーマだけ）。"""
    themes = []
    for c in _rows(md, "| テーマ | ETF | 熱量 |"):
        if len(c) >= 6:
            g = re.search(r"([+-]?\d+(?:\.\d+)?)%", c[4].replace("+-", "-"))
            sig = c[5].split()[-1] if c[5].split() else None
            themes.append({"report_date": d, "theme_name": c[0], "etfs": c[1], "heat_score": _num(c[2].strip("*")),
                           "etf_momentum_score": _num(c[3]), "arxiv_trend_score": None,
                           "arxiv_growth_rate": float(g.group(1)) if g else None, "arxiv_recent_4w": None,
                           "arxiv_prev_4w": None, "signal": sig, "fetched_at": fetched_at})
    etfs, theme = [], None
    for line in md.splitlines():
        h = re.match(r"^### \S+ (.+?)（熱量スコア", line)
        if h:
            theme = h.group(1).strip()
            continue
        m = _ETF_LINE.match(line)
        if m and theme:
            etfs.append({"report_date": d, "theme_name": theme, "ticker": m["t"], "price": float(m["p"]),
                         "ret_1m": float(m["m1"]), "ret_3m": float(m["m3"]), "ma50_ratio": float(m["ma"]),
                         "vol_ratio": float(m["v"]), "fetched_at": fetched_at})
    return themes, etfs


def parse_imf(md: str) -> list:
    """IMF レポートの指標表から [(指標コード, 指標名, 国, 年, 値)]（「—」は飛ばす）。"""
    out = []
    for m in re.finditer(r"^## ([^\n]+?) \(([A-Z_]+)\)\n\n(\|.*?)(?=\n\n|\Z)", md, re.M | re.S):
        lines = [l for l in m.group(3).splitlines() if l.startswith("|")]
        if len(lines) < 3:
            continue
        years = [c.strip() for c in lines[0].strip().strip("|").split("|")][1:]
        for line in lines[2:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            for y, v in zip(years, cells[1:]):
                val = _num(v)
                if val is not None and y.isdigit():
                    out.append((m.group(2), m.group(1).strip(), cells[0], int(y), val))
    return out


def parse_gov(md: str) -> list:
    """政府統計レポートの取得統計サマリー表から [(統計名, 最新値, 単位と期間, 前月比%, 前年比%, シグナル)]。"""
    out = []
    for c in _rows(md, "| 統計名 | 最新値 |"):
        if len(c) >= 5:
            v = re.match(r"(-?[\d,]+(?:\.\d+)?)\s*(.*)$", c[1])
            per = re.search(r"\((\d{4}-\d{2})\)", c[1])
            sig = "tailwind" if "追い風" in c[4] else "headwind" if "逆風" in c[4] else (c[4] or None)
            out.append((c[0], _num(v.group(1)) if v else None, (v.group(2).strip() if v else c[1])[:100],
                        per.group(1) if per else None, _num(c[2]), _num(c[3]), sig))
    return out


def trace_rows_from_md(md: str) -> list:
    """screener_trace の md から（通過＝総合点・加点前・経路、落選＝業種・理由）。"""
    out = []
    for c in _rows(md, "| 銘柄 | 総合点 | 加点前 | 点数の経路 |"):
        if len(c) >= 4:
            tk, _, nm = c[0].partition(" ")
            out.append({"ticker": tk, "name": nm or None, "sector_name": None, "passed": True, "composite_score": _num(c[1]),
                        "base_score": _num(c[2]), "score_path": c[3] if c[3] != "—" else None, "reasons": None})
    for c in _rows(md, "| 銘柄 | 業種 | 落ちた理由 |"):
        if len(c) >= 3:
            tk, _, nm = c[0].partition(" ")
            out.append({"ticker": tk, "name": nm or None, "sector_name": c[1] or None, "passed": False, "composite_score": None,
                        "base_score": None, "score_path": None, "reasons": c[2]})
    return out


TRACE_COLS = ("ticker", "name", "sector_code", "sector_name", "passed", "composite_score", "base_score", "score_path", "reasons",
              "kpi_pbr", "kpi_per", "kpi_fcf_yield", "kpi_ev_ebitda", "kpi_roic", "kpi_net_cash_ratio", "kpi_irr")


def build_all(paths: list, fetched_at: str, data_dir: Path | None = None) -> dict:
    """md/csv のパス一覧（と data_dir のニュース・ETF の JSON）から各表の行を作る。"""
    out = {"report_texts": [], "sector_scores_daily": [], "macro_indicators_daily": [], "macro_signals_daily": [],
           "news_articles": [], "etf_theme_daily": [], "etf_daily": [], "imf_forecasts": [], "gov_stats": [], "screener_trace_daily": []}
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
        elif kind == "thematic":
            js = (data_dir / "etf_cache" / f"thematic_{d}.json") if data_dir else None
            if js and js.exists():
                th, et = etf_rows_from_json(json.loads(js.read_text(encoding="utf-8")), d, fetched_at)
            else:
                th, et = etf_rows_from_md(text, d, fetched_at)
            out["etf_theme_daily"] += th
            out["etf_daily"] += et
        elif kind == "news":
            js = (data_dir / "news_cache" / f"news_{d}.json") if data_dir else None
            if js and js.exists():
                out["news_articles"] += news_rows(json.loads(js.read_text(encoding="utf-8")), d, fetched_at)
        elif kind == "screener_trace":
            js = (data_dir / "screener_trace" / f"trace_{d}.json") if data_dir else None
            if js and js.exists():
                rows = json.loads(js.read_text(encoding="utf-8")).get("rows", [])
            else:
                rows = trace_rows_from_md(text)
                rej = {}
                try:
                    rej = json.loads((data_dir / "screener_rejections.json").read_text(encoding="utf-8")).get(d) or [] if data_dir else []
                except (OSError, ValueError):
                    rej = []
                if rej:   # 落選は JSON の方が業種コード・点数まである
                    rows = [r for r in rows if r["passed"]] + [
                        {"ticker": r.get("ticker"), "name": r.get("name") or None, "sector_code": r.get("sector_code") or None,
                         "sector_name": r.get("sector_name") or None, "passed": False, "composite_score": _num(r.get("composite_score")),
                         "reasons": " / ".join(r.get("reasons") or []) or "（フィルター以外）"} for r in rej]
            for r in rows:
                row = {k: r.get(k) for k in TRACE_COLS}
                for k in ("composite_score", "base_score", "kpi_pbr", "kpi_per", "kpi_fcf_yield", "kpi_ev_ebitda", "kpi_roic",
                          "kpi_net_cash_ratio", "kpi_irr"):
                    row[k] = _num(row[k])
                out["screener_trace_daily"].append(dict(row, report_date=d, market="JP", fetched_at=fetched_at))
        elif kind == "imf":
            for code, name, country, yr, v in parse_imf(text):
                out["imf_forecasts"].append({"report_date": d, "indicator_code": code, "indicator_name": name, "country": country,
                                             "year": yr, "value": v, "fetched_at": fetched_at})
        elif kind == "gov_report":
            for name, v, unit, per, mom, yoy, sig in parse_gov(text):
                out["gov_stats"].append({"report_date": d, "stat_name": name, "value": v, "unit": unit, "period": per,
                                         "mom_pct": mom, "yoy_pct": yoy, "signal": sig, "fetched_at": fetched_at})
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
        "news_articles": [F("report_date", "DATE", "REQUIRED"), F("url", "STRING"), F("title", "STRING"), F("summary", "STRING"),
                          F("source", "STRING"), F("priority", "STRING"), F("lang", "STRING"), F("pub_date", "STRING"),
                          F("matched_sectors", "STRING"), F("importance_score", "FLOAT64"), F("investment_relevant", "BOOL"),
                          F("sentiment_label", "STRING"), F("sentiment_score", "FLOAT64")] + ts,
        "etf_theme_daily": [F("report_date", "DATE", "REQUIRED"), F("theme_name", "STRING"), F("etfs", "STRING"),
                            F("heat_score", "FLOAT64"), F("etf_momentum_score", "FLOAT64"), F("arxiv_trend_score", "FLOAT64"),
                            F("arxiv_growth_rate", "FLOAT64"), F("arxiv_recent_4w", "FLOAT64"), F("arxiv_prev_4w", "FLOAT64"),
                            F("signal", "STRING")] + ts,
        "etf_daily": [F("report_date", "DATE", "REQUIRED"), F("theme_name", "STRING"), F("ticker", "STRING"), F("price", "FLOAT64"),
                      F("ret_1m", "FLOAT64"), F("ret_3m", "FLOAT64"), F("ma50_ratio", "FLOAT64"), F("vol_ratio", "FLOAT64")] + ts,
        "imf_forecasts": [F("report_date", "DATE", "REQUIRED"), F("indicator_code", "STRING"), F("indicator_name", "STRING"),
                          F("country", "STRING"), F("year", "INT64"), F("value", "FLOAT64")] + ts,
        "screener_trace_daily": [F("report_date", "DATE", "REQUIRED"), F("market", "STRING"), F("ticker", "STRING"), F("name", "STRING"),
                                 F("sector_code", "STRING"), F("sector_name", "STRING"), F("passed", "BOOL"),
                                 F("composite_score", "FLOAT64"), F("base_score", "FLOAT64"), F("score_path", "STRING"),
                                 F("reasons", "STRING")] + [F(k, "FLOAT64") for k in ("kpi_pbr", "kpi_per", "kpi_fcf_yield",
                                 "kpi_ev_ebitda", "kpi_roic", "kpi_net_cash_ratio", "kpi_irr")] + ts,
        "gov_stats": [F("report_date", "DATE", "REQUIRED"), F("stat_name", "STRING"), F("value", "FLOAT64"), F("unit", "STRING"),
                      F("period", "STRING"), F("mom_pct", "FLOAT64"), F("yoy_pct", "FLOAT64"), F("signal", "STRING")] + ts,
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


STATE_NAME = "report_store_state.json"


def collect(insight_dir: Path, subdirs: tuple | None = None) -> list:
    """insights 以下の md（と米国セクターの csv）。subdirs で最上位フォルダを絞れる。"""
    out = []
    for p in sorted(insight_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(insight_dir)
        if subdirs and rel.parts[0] not in subdirs:
            continue
        if p.suffix == ".md" or (p.suffix == ".csv" and rel.parts[0] == "us_sectors" and p.name.startswith("us_sector_scores_")):
            out.append(p)
    return out


def file_sha(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def pending(paths: list, insight_dir: Path, state: dict) -> list:
    """取り込み記録（相対パス→内容の sha1）と違うファイルだけ（新しいファイル・中身が変わったファイル）。"""
    return [p for p in paths if state.get(p.relative_to(insight_dir).as_posix()) != file_sha(p)]


def run(cfg, subdirs: tuple | None = None, init_state: bool = False) -> dict:
    insight = Path(getattr(cfg, "INSIGHT_DIR", "stock_data/insights"))
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    state_p = data_dir / STATE_NAME
    try:
        state = json.loads(state_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    todo = pending(collect(insight, subdirs), insight, state)
    if init_state:
        # 既に BigQuery に入っているファイルを「取り込み済み」として記録だけする（初回の切り替え用）
        counts: dict = {}
    else:
        built = build_all(todo, datetime.now(timezone.utc).isoformat(), data_dir)
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
        counts = {}
        # 1回の load を小さく保つため月ごとに分ける
        months = sorted({r["report_date"][:7] for rows in built.values() for r in rows})
        for m in months:
            part = {t: [r for r in rows if r["report_date"].startswith(m)] for t, rows in built.items()}
            for t, n in write(client, part, data_dir / "cache").items():
                counts[t] = counts.get(t, 0) + n
    for p in todo:
        state[p.relative_to(insight).as_posix()] = file_sha(p)
    state_p.parent.mkdir(parents=True, exist_ok=True)
    state_p.write_text(json.dumps(state, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    print(f"📚 report_store: 新規・変更 {len(todo)}ファイル → " + (" / ".join(f"{t} {n}行" for t, n in counts.items() if n) or "（記録のみ）"))
    return {"files": len(todo), **counts}


if __name__ == "__main__":
    import argparse
    import sys
    import config as _cfg
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="insights 直下のフォルダを絞る（カンマ区切り。例 news,etf）")
    ap.add_argument("--init-state", action="store_true", help="BQ に入れずに取り込み記録だけ作る（既に入っている分の印付け）")
    a = ap.parse_args()
    try:
        res = run(_cfg, tuple(a.only.split(",")) if a.only else None, a.init_state)
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
