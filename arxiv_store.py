# ==============================================================
# arxiv_store.py
# arxiv_monitor が取得・採点した論文を BigQuery analytics.arxiv_papers に1論文1行で追記する。
# ==============================================================
# 【なぜ要るか】
#   論文は週1回の md レポート（insights/arxiv/arxiv_report_*.md）にしか残らず、
#   過去の論文をキーワードや点数で横断して探せなかった。表に溜めて BI（m_arxiv_papers）から引く。
# 【書き方】
#   NDJSON を load job（WRITE_APPEND・無料）で追記する。ストリーミング挿入は課金対象なので使わない。
#   job_id は「取得日＋論文IDの並び」から決めるので、同じ回を再実行しても二重に入らない。
#   同じ論文が別の回にもう一度入った場合は、読む側（m_arxiv_papers）で fetched_at が新しい行を採る。
# 【日本語タイトル】
#   DEEPL_API_KEY が無料キー（末尾 :fx）のときだけ、タイトルをまとめて訳す（1回 約70件×約100字）。
#   無料キーは月50万字で止まるだけで課金されない。有料キーや失敗時は訳さず title_ja を空にする。
# ==============================================================
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

TABLE = "analytics.arxiv_papers"
LOCATION = "asia-northeast1"
_KW_RE = re.compile(r"^\[[TS]\+\d+\](.+)$")


def keywords_of(matched: list | None) -> list:
    """matched_keywords（"[T+3]tail risk" や "[×カテゴリ1.30(q-fin.TR)]"）からキーワードだけを取る（純関数）。"""
    out = []
    for m in matched or []:
        k = _KW_RE.match(str(m).strip())
        if k and k.group(1).strip() not in out:
            out.append(k.group(1).strip())
    return out


def _published_date(s: str | None) -> str | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(s or ""))
        return m.group(1) if m else None


def build_rows(papers: list, threshold: int, fetched_date: str, fetched_at: str | None = None,
               titles_ja: dict | None = None, source: str = "monitor") -> list:
    """論文 dict のリストを1論文1行にする（同じ論文が複数カテゴリに出たら1行にまとめる・純関数）。"""
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    titles_ja = titles_ja or {}
    by_id: dict = {}
    for p in papers or []:
        pid = str(p.get("id") or "").strip()
        if not pid:
            continue
        cat = str(p.get("category") or "")
        cur = by_id.get(pid)
        if cur:
            if cat and cat not in cur["categories"].split(","):
                cur["categories"] += "," + cat
            cur["investment_score"] = max(cur["investment_score"] or 0, int(p.get("investment_score") or 0))
            continue
        kws = keywords_of(p.get("matched_keywords"))
        score = int(p.get("investment_score") or 0)
        by_id[pid] = {
            "paper_id": pid,
            "fetched_date": fetched_date,
            "fetched_at": fetched_at,
            "published_date": _published_date(p.get("published")),
            "primary_category": cat or None,
            "categories": cat,
            "title": (p.get("title") or "").strip()[:500],
            "title_ja": titles_ja.get(pid),
            "summary": (p.get("summary") or "").strip()[:2000] or None,
            "authors": (p.get("authors") or "").strip()[:300] or None,
            "url": p.get("url") or (f"https://arxiv.org/abs/{pid}"),
            "investment_score": score,
            "is_idea": score >= int(threshold),
            "keywords": "|".join(kws) or None,
            "score_detail": ", ".join(str(x) for x in (p.get("matched_keywords") or []))[:1000] or None,
            "source": source,
        }
    for r in by_id.values():
        r["is_idea"] = r["investment_score"] >= int(threshold)
    return list(by_id.values())


def translate_titles(papers: list, api_key: str, post=None) -> dict:
    """DeepL 無料キーでタイトルを日本語に訳す。{paper_id: 訳}。無料キー以外・失敗時は {}。"""
    if not api_key or not api_key.endswith(":fx"):
        return {}
    items = []
    seen = set()
    for p in papers or []:
        pid, t = str(p.get("id") or ""), (p.get("title") or "").strip()
        if pid and t and pid not in seen:
            seen.add(pid)
            items.append((pid, t[:300]))
    if not items:
        return {}
    if post is None:
        import requests
        post = requests.post
    out = {}
    for i in range(0, len(items), 50):
        chunk = items[i:i + 50]
        try:
            r = post("https://api-free.deepl.com/v2/translate",
                     headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
                     data=[("text", t) for _, t in chunk] + [("source_lang", "EN"), ("target_lang", "JA")],
                     timeout=30)
            if r.status_code != 200:
                print(f"    ⚠️ arXiv タイトル翻訳: HTTP {r.status_code}（訳さずに保存）")
                return out
            for (pid, _), tr in zip(chunk, r.json().get("translations", [])):
                out[pid] = (tr.get("text") or "").strip() or None
        except Exception as exc:
            print(f"    ⚠️ arXiv タイトル翻訳に失敗: {type(exc).__name__}（訳さずに保存）")
            return out
    return out


def schema():
    from google.cloud import bigquery as bq
    F = bq.SchemaField
    return [
        F("paper_id", "STRING", "REQUIRED"), F("fetched_date", "DATE", "REQUIRED"), F("fetched_at", "TIMESTAMP"),
        F("published_date", "DATE"), F("primary_category", "STRING"), F("categories", "STRING"),
        F("title", "STRING"), F("title_ja", "STRING"), F("summary", "STRING"), F("authors", "STRING"),
        F("url", "STRING"), F("investment_score", "INT64"), F("is_idea", "BOOL"), F("keywords", "STRING"),
        F("score_detail", "STRING"), F("source", "STRING"),
    ]


def job_id_for(rows: list, salt: str = "") -> str:
    raw = salt + "|".join(sorted(f"{r['fetched_date']}:{r['paper_id']}" for r in rows))
    return "arxiv_" + hashlib.sha1(raw.encode()).hexdigest()[:24]


def write_rows(client, rows: list, tmp_dir: Path, salt: str = "") -> int:
    """analytics.arxiv_papers へ load job で追記する。行数を返す。失敗は例外。"""
    if not rows:
        return 0
    from google.api_core.exceptions import Conflict, NotFound
    from google.cloud import bigquery
    ref = f"{client.project}.{TABLE}"
    try:
        client.get_table(ref)
    except NotFound:
        t = bigquery.Table(ref, schema=schema())
        t.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.MONTH, field="fetched_date")
        t.clustering_fields = ["primary_category"]
        client.create_table(t)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / "arxiv_rows.ndjson"
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    cfg = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON, schema=schema(),
                                 write_disposition=bigquery.WriteDisposition.WRITE_APPEND)
    jid = job_id_for(rows, salt)
    try:
        with open(tmp, "rb") as fh:
            client.load_table_from_file(fh, ref, job_id=jid, job_config=cfg, location=LOCATION).result()
    except Conflict:
        old = client.get_job(jid, location=LOCATION)
        if not (old.state == "DONE" and not old.error_result):
            raise RuntimeError(f"load job {jid} は既にあるが失敗している")
        print(f"    ↩️ arXiv: 同じ回は保存済み（{jid}）")
    finally:
        tmp.unlink(missing_ok=True)
    return len(rows)


def store(papers: list, threshold: int, cfg) -> dict:
    """arxiv_monitor から呼ぶ入口。{"rows": n} か {"error": 理由}。"""
    try:
        today = date.today().isoformat()
        titles = translate_titles(papers, str(getattr(cfg, "DEEPL_API_KEY", "") or "")) \
            if getattr(cfg, "ARXIV_TRANSLATE_TITLES", True) else {}
        rows = build_rows(papers, threshold, today, titles_ja=titles)
        from edinet_bq_client import EDINETBQClient
        n = write_rows(EDINETBQClient().client, rows, Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "cache")
        print(f"    ✅ BQ `{TABLE}` 追記: {n}行（日本語タイトル {sum(1 for r in rows if r['title_ja'])}件）")
        return {"rows": n, "translated": sum(1 for r in rows if r["title_ja"])}
    except Exception as exc:
        print(f"    ❌ arXiv の BQ 保存に失敗: {type(exc).__name__}: {str(exc)[:120]}")
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}


# ── 過去の md レポートからの取り込み（初回だけ） ──────────────────────────
_H_RE = re.compile(r"^### \[(?P<title>.+)\]\((?P<url>https?://arxiv\.org/abs/(?P<id>[^)]+))\)\s*$")
_ROW_RE = re.compile(r"^\| (?P<cat>[\w.\-]+) \| \[(?P<title>.+)\]\((?P<url>https?://arxiv\.org/abs/(?P<id>[^)]+))\) \| (?P<score>-?\d+) \|$")


def parse_report(text: str) -> list:
    """arxiv_report_*.md から論文 dict を取り出す（純関数）。候補節は詳細つき、表は題名・点数だけ（題名は55字で切れている）。"""
    papers, cur = [], None
    for line in text.splitlines():
        m = _H_RE.match(line)
        if m:
            cur = {"id": m["id"], "title": m["title"], "url": m["url"], "category": "", "matched_keywords": []}
            papers.append(cur)
            continue
        m = _ROW_RE.match(line)
        if m:
            papers.append({"id": m["id"], "title": m["title"], "url": m["url"], "category": m["cat"],
                           "investment_score": int(m["score"])})
            cur = None
            continue
        if cur is None:
            continue
        for key, field in (("- **カテゴリ**: ", "category"), ("- **著者**: ", "authors"), ("- **概要**: ", "summary")):
            if line.startswith(key):
                cur[field] = line[len(key):].rstrip(".").strip()
        if line.startswith("- **スコア**: "):
            cur["investment_score"] = int(re.sub(r"\D", "", line) or 0)
        if line.startswith("- **マッチ**: "):
            cur["matched_keywords"] = [x.strip() for x in line[len("- **マッチ**: "):].split(", ")]
    return papers


def backfill(cfg) -> dict:
    """insights/arxiv/arxiv_report_YYYY-MM-DD.md を取得日ごとに取り込む。"""
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    total = 0
    threshold = int(getattr(cfg, "ARXIV_INVESTMENT_THRESHOLD", 3))
    for path in sorted(Path(cfg.ARXIV_DIR).glob("arxiv_report_*.md")):
        d = re.search(r"(\d{4}-\d{2}-\d{2})", path.name).group(1)
        rows = build_rows(parse_report(path.read_text(encoding="utf-8")), threshold, d,
                          fetched_at=f"{d}T00:00:00+00:00", source="report_md")
        total += write_rows(client, rows, Path(cfg.DATA_DIR) / "cache")
        print(f"  {path.name}: {len(rows)}行")
    return {"rows": total}


def entries_to_papers(entries: list) -> dict:
    """arXiv API（Atom）の entry から {arXiv ID: 論文 dict}（純関数。ID の版番号 vN は外す）。"""
    out = {}
    for e in entries or []:
        m = re.search(r"abs/(.+?)(?:v\d+)?$", str(e.get("id", "")))
        if not m:
            continue
        pid = m.group(1).strip()
        authors = ", ".join(a.get("name", "") for a in (e.get("authors") or [])[:3])
        tags = [t.get("term") for t in (e.get("tags") or []) if t.get("term")]
        out[pid] = {"id": pid, "title": re.sub(r"\s+", " ", e.get("title", "")).strip(),
                    "summary": re.sub(r"\s+", " ", e.get("summary", "")).strip(), "authors": authors,
                    "published": e.get("published", ""), "url": f"https://arxiv.org/abs/{pid}",
                    "category": tags[0] if tags else ""}
    return out


def enrich(cfg, sleep_sec: float = 3.0) -> dict:
    """md から取り込んだ論文を arXiv API（無料）で取り直し（要旨・全題名・著者）、日本語タイトルをつけて追記する。
    取得日（fetched_date）と点数は md のまま、fetched_at を今にするので m_arxiv_papers ではこちらが採られる。"""
    import time
    import feedparser
    import requests
    threshold = int(getattr(cfg, "ARXIV_INVESTMENT_THRESHOLD", 3))
    todo = {}
    for path in sorted(Path(cfg.ARXIV_DIR).glob("arxiv_report_*.md")):
        d = re.search(r"(\d{4}-\d{2}-\d{2})", path.name).group(1)
        for p in parse_report(path.read_text(encoding="utf-8")):
            todo.setdefault(p["id"], (d, p))
    ids = list(todo)
    meta = {}
    for i in range(0, len(ids), 50):
        r = requests.get("https://export.arxiv.org/api/query",
                         params={"id_list": ",".join(ids[i:i + 50]), "max_results": 50}, timeout=60)
        r.raise_for_status()
        meta.update(entries_to_papers(feedparser.parse(r.text).entries))
        time.sleep(sleep_sec)   # arXiv API の利用規約（3秒に1回まで）
    papers_by_date: dict = {}
    for pid, (d, p) in todo.items():
        m = meta.get(pid)
        if not m:
            continue
        merged = dict(m, category=p.get("category") or m["category"], investment_score=p.get("investment_score", 0),
                      matched_keywords=p.get("matched_keywords") or [])
        papers_by_date.setdefault(d, []).append(merged)
    titles = translate_titles([x for v in papers_by_date.values() for x in v], str(getattr(cfg, "DEEPL_API_KEY", "") or ""))
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    total, now = 0, datetime.now(timezone.utc).isoformat()
    for d, ps in sorted(papers_by_date.items()):
        rows = build_rows(ps, threshold, d, fetched_at=now, titles_ja=titles, source="arxiv_api_backfill")
        total += write_rows(client, rows, Path(cfg.DATA_DIR) / "cache", salt="enrich")
    print(f"  md の論文 {len(ids)}本 → arXiv から {len(meta)}本取得・{total}行追記・日本語タイトル {len(titles)}件")
    return {"todo": len(ids), "fetched": len(meta), "rows": total, "translated": len(titles)}


if __name__ == "__main__":
    import argparse
    import config as _cfg
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="過去の md レポートを取り込む")
    ap.add_argument("--enrich", action="store_true", help="md から取り込んだ論文を arXiv API で取り直し、日本語タイトルをつける")
    a = ap.parse_args()
    if a.backfill:
        print(backfill(_cfg))
    if a.enrich:
        print(enrich(_cfg))
