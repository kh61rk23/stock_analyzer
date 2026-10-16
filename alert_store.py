# ==============================================================
# alert_store.py
# 日次メールの全節（注意・重要・情報）と、節の中に出た銘柄を BigQuery に貯める
# ==============================================================
# 【なぜ】
#   自己株式取得・増配・決算サプライズ候補・異常検知・ブレイクアウト・財務リスクウォッチなどの注意項目は、
#   メールに出るだけでどこにも残っていなかった。日付を過ぎると追えないため、送る内容をそのまま表に残す。
# 【表】analytics（asia-northeast1・report_date で月パーティション）
#   alert_sections：1行＝1日×1節。section_key（見出しから件数等を除いた名前）・重要度・見出し・本文。
#   alert_items   ：1行＝1日×1節×1銘柄。本文の各行から証券コード（7203.T・130A.T）と米国ティッカー（（MU））を拾う。
#   書き込みは load job（無料）。1日ずつ job_id を内容から決めるので、同じ日の同じ内容を二度入れても重複しない。
#   読むときはビュー alert_sections_v / alert_items_v を使う（同じ日に送信時の保存（live）と過去メールからの
#   取り込み（mail_backfill）の両方があれば live を採り、同じ行の重複を除く）。
# 【過去分】
#   送信済みの日次メール（件名「【投資AIエージェント】」）を Gmail の IMAP で読み、同じ形に直して入れる
#   （python3 alert_store.py --backfill）。メール送信用のアプリパスワード（ALERT_GMAIL_*）を使う。
#   同じ日にメールが複数あるときは、最後に送ったものをその日の正とする。
# ==============================================================
from __future__ import annotations

import argparse
import hashlib
import html as _html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DATASET = "analytics"
LOCATION = "asia-northeast1"
SUBJECT_MARK = "【投資AIエージェント】"

_SEVERITY_FROM_LABEL = {"🔴": "high", "🟡": "medium", "ℹ": "info"}
_JP_TICKER = re.compile(r"(?<![0-9A-Z])(\d[0-9A-Z]\d[0-9A-Z])\.T\b")
_US_TICKER = re.compile(r"[（(]([A-Z]{1,5}(?:\.[A-Z]{1,3})?)[）)]")
# 13F の節など、括弧なしで「新規取得: MSFT Microsoft …」と書く行
_US_LABELED = re.compile(r"(?:新規取得|買い増し|売却|保有)\s*[:：]\s*([A-Z]{1,5}(?:\.[A-Z]{1,3})?)\b")
_NOT_TICKERS = {"FRB", "ECB", "BOJ", "IMF", "ETF", "PER", "PBR", "ROE", "ROA", "FCF", "EPS", "USD", "JPY", "EUR", "CPI",
                "GDP", "NHK", "CNBC", "TOPIX", "NASDAQ", "FF", "AI", "DX", "IR", "TOB", "MBO", "IPO", "ESG", "API", "HTTP",
                "IFRS", "JGAAP", "OECD", "FOMC", "PMI", "ISM", "CEO", "CFO", "LLM", "EV", "REIT", "TSE", "SEC", "ADR"}
_COUNT = re.compile(r"[0-9０-９][0-9０-９,，.]*\s*(件|社|銘柄|業種|日|回|%|％|pt)?")

# 見出しの言い回しが日によって違っても同じ節として数えるための名前（先に当たったもの）
SECTION_KEYS = [
    ("出口戦略", "exit_strategy"), ("自己株式取得", "buyback"), ("増配", "dividend_up"),
    ("決算サプライズ高確率", "earnings_surprise_up"), ("ネガティブサプライズ", "earnings_surprise_down"),
    ("異常検知", "anomaly"), ("ブレイクアウト", "breakout"), ("財務リスク", "financial_risk"),
    ("ゴールデンクロス", "golden_cross"), ("デッドクロス", "dead_cross"), ("政策金利変化", "policy_rate_change"),
    ("中央銀行イベント", "central_bank_event"), ("審議キュー", "deliberation_queue"), ("高配当", "high_dividend"),
    ("EDINET DB", "edinetdb"), ("不動産DB", "fudosandb"), ("政策DB", "seisakudb"), ("金利", "rates"),
    ("注目ニュース", "news"), ("テーマETF", "theme_etf"), ("注目業種", "top_sectors"), ("米国株サマリー", "us_summary"),
    ("スコア急変", "score_jump"), ("インサイダー", "insider"), ("大量保有", "large_holding"),
    ("議員取引", "congress_trade"), ("アクティビスト", "institutional_13f"), ("高スコアアイデア", "ideas"),
    ("マクロ急変", "macro_shock"), ("センチメント極端", "sentiment_extreme"), ("データ鮮度", "freshness_sla"),
    ("アノマリー", "month_anomaly"),
]


def section_key(title: str) -> str:
    """見出し → 節の名前。表に無い見出しは、絵文字・数・括弧を除いた文字列（純関数）。"""
    t = str(title or "")
    for word, key in SECTION_KEYS:
        if word in t:
            return key
    t = re.sub(r"[（(][^）)]*[）)]", "", t)
    t = _COUNT.sub("", t)
    t = re.sub(r"[^\w・ー]+", " ", t).strip()
    return t[:60] or "unknown"


def extract_items(body: str) -> list[dict]:
    """本文の各行から銘柄を拾う（1行に複数あれば全部）。行の文は先頭400字（純関数）。"""
    out, seen = [], set()
    for line in str(body or "").splitlines():
        s = line.strip()
        if not s:
            continue
        found = [(t + ".T", "JP") for t in _JP_TICKER.findall(s)]
        found += [(t, "US") for t in _US_LABELED.findall(s) if t not in _NOT_TICKERS]
        found += [(t, "US") for t in _US_TICKER.findall(s)
                  if t not in _NOT_TICKERS and (not re.fullmatch(r"[A-Z]{1,2}", t) or t in ("MU", "GE", "BA", "F", "T", "V"))]
        for tk, mkt in found:
            key = (tk, s[:80])
            if key in seen:
                continue
            seen.add(key)
            out.append({"ticker": tk, "market": mkt, "line": s[:400]})
    return out


def rows_from_alerts(alerts: list[dict], report_date: str, source: str, sent_at: str | None = None,
                     message_id: str | None = None) -> tuple[list, list]:
    """節の一覧（{title, body, severity, trigger}）→ (alert_sections の行, alert_items の行)（純関数）。"""
    now = datetime.now(timezone.utc).isoformat()
    secs, items = [], []
    for i, al in enumerate(alerts or []):
        title, body = str(al.get("title") or ""), str(al.get("body") or "")
        key = section_key(title)
        base = {"report_date": report_date, "section_key": key, "severity": al.get("severity"), "source": source}
        secs.append({**base, "position": i, "trigger": al.get("trigger"), "title": title[:500], "body": body[:20000],
                     "sent_at": sent_at, "message_id": message_id, "fetched_at": now})
        for it in extract_items(body):
            items.append({**base, **it, "title": title[:200], "fetched_at": now})
    return secs, items


def parse_mail_html(html: str) -> list[dict]:
    """日次メールの HTML → 節の一覧（{title, body, severity}）。

    各節は「重要度ラベルの span → 見出しの span（font-weight:bold）→ 本文の div（<br> 区切り）」の並び。
    """
    out = []
    pat = re.compile(
        r'<span[^>]*background:[^>]*>\s*(?P<label>[^<]*)</span>\s*'
        r'<span[^>]*font-weight:\s*bold[^>]*>(?P<title>.*?)</span>\s*</div>\s*'
        r'<div[^>]*>(?P<body>.*?)</div>\s*</td>', re.S)
    for m in pat.finditer(html or ""):
        label = _html.unescape(re.sub(r"<[^>]+>", "", m.group("label"))).strip()
        sev = next((v for k, v in _SEVERITY_FROM_LABEL.items() if label.startswith(k)), None)
        body = re.sub(r"<br\s*/?>", "\n", m.group("body"))
        body = _html.unescape(re.sub(r"<[^>]+>", "", body)).replace("\xa0", " ")
        body = "\n".join(line.rstrip() for line in body.splitlines()).strip()
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group("title"))).strip()
        out.append({"title": title, "body": body, "severity": sev})
    return out


def _schemas():
    from google.cloud.bigquery import SchemaField as F
    common = [F("report_date", "DATE", "REQUIRED"), F("section_key", "STRING"), F("severity", "STRING"), F("source", "STRING")]
    return {
        "alert_sections": common + [F("position", "INT64"), F("trigger", "STRING"), F("title", "STRING"), F("body", "STRING"),
                                    F("sent_at", "TIMESTAMP"), F("message_id", "STRING"), F("fetched_at", "TIMESTAMP")],
        "alert_items": common + [F("ticker", "STRING"), F("market", "STRING"), F("line", "STRING"), F("title", "STRING"),
                                 F("fetched_at", "TIMESTAMP")],
    }


def _job_id(table: str, rows: list) -> str:
    body = json.dumps(sorted(json.dumps({k: v for k, v in r.items() if k != "fetched_at"}, ensure_ascii=False, sort_keys=True)
                             for r in rows), ensure_ascii=False)
    return f"alert_{table}_" + hashlib.sha1(body.encode()).hexdigest()[:24]


VIEWS = {
    "alert_sections_v": """
CREATE OR REPLACE VIEW `{project}.{ds}.alert_sections_v` AS
SELECT * EXCEPT (rn) FROM (
  SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.report_date, s.position ORDER BY s.fetched_at DESC) AS rn
  FROM `{project}.{ds}.alert_sections` AS s
  JOIN (SELECT report_date, IF(LOGICAL_OR(source = 'live'), 'live', 'mail_backfill') AS src
        FROM `{project}.{ds}.alert_sections` GROUP BY report_date) AS d
    ON d.report_date = s.report_date AND d.src = s.source
) WHERE rn = 1""",
    "alert_items_v": """
CREATE OR REPLACE VIEW `{project}.{ds}.alert_items_v` AS
SELECT * EXCEPT (rn) FROM (
  SELECT i.*, ROW_NUMBER() OVER (PARTITION BY i.report_date, i.section_key, i.ticker, i.line ORDER BY i.fetched_at DESC) AS rn
  FROM `{project}.{ds}.alert_items` AS i
  JOIN (SELECT report_date, IF(LOGICAL_OR(source = 'live'), 'live', 'mail_backfill') AS src
        FROM `{project}.{ds}.alert_items` GROUP BY report_date) AS d
    ON d.report_date = i.report_date AND d.src = i.source
) WHERE rn = 1""",
}


def ensure_views(client) -> None:
    from google.api_core.exceptions import NotFound
    for name, sql in VIEWS.items():
        try:
            client.get_table(f"{client.project}.{DATASET}.{name}")
        except NotFound:
            client.query(sql.format(project=client.project, ds=DATASET)).result()


def write(client, tables: dict, tmp_dir: Path) -> dict:
    """1日ずつ load job で追記する。同じ日の同じ内容は同じ job_id になり二度入らない。"""
    counts: dict = {}
    for table, rows in tables.items():
        by_day: dict = {}
        for r in rows:
            by_day.setdefault(r["report_date"], []).append(r)
        counts[table] = sum(_write_one(client, table, day_rows, tmp_dir) for _, day_rows in sorted(by_day.items()))
    try:
        ensure_views(client)
    except Exception as e:
        print(f"    ⚠️ alert_*_v ビューの作成失敗: {str(e)[:100]}")
    return counts


def _write_one(client, table: str, rows: list, tmp_dir: Path) -> int:
    from google.api_core.exceptions import Conflict, NotFound
    from google.cloud import bigquery
    sch = _schemas()
    if not rows:
        return 0
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ref = f"{client.project}.{DATASET}.{table}"
    try:
        client.get_table(ref)
    except NotFound:
        t = bigquery.Table(ref, schema=sch[table])
        t.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.MONTH, field="report_date")
        client.create_table(t)
    tmp = tmp_dir / f"alert_store_{table}.ndjson"
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    jid = _job_id(table, rows)
    try:
        with open(tmp, "rb") as fh:
            client.load_table_from_file(fh, ref, job_id=jid, location=LOCATION, job_config=bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON, schema=sch[table],
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND)).result()
        return len(rows)
    except Conflict:
        old = client.get_job(jid, location=LOCATION)
        if not (old.state == "DONE" and not old.error_result):
            raise RuntimeError(f"load job {jid} は既にあるが失敗している")
        return 0
    finally:
        tmp.unlink(missing_ok=True)


def _client():
    from edinet_bq_client import EDINETBQClient
    return EDINETBQClient().client


def store_live(cfg, alerts: list[dict], report_date: str | None = None) -> dict:
    """日次: 送った節をそのまま貯める（alert_notifier から呼ぶ）。失敗しても送信は止めない。"""
    try:
        day = report_date or datetime.now().strftime("%Y-%m-%d")
        secs, items = rows_from_alerts(alerts, day, "live", sent_at=datetime.now(timezone.utc).isoformat())
        counts = write(_client(), {"alert_sections": secs, "alert_items": items},
                       Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "tmp")
        print(f"    🗄 注意項目をBQへ: 節{counts.get('alert_sections', 0)}・銘柄{counts.get('alert_items', 0)}行")
        return counts
    except Exception as e:
        print(f"    ⚠️ 注意項目のBQ保存失敗（送信は済み）: {str(e)[:100]}")
        return {"error": str(e)[:150]}


# ── 過去分（Gmail IMAP）──────────────────────────────────────

def _decode_subject(raw) -> str:
    from email.header import decode_header
    out = ""
    for part, enc in decode_header(raw or ""):
        out += part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
    return out


def _html_of(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def fetch_mails(user: str, password: str, since: str = "01-Jan-2026") -> list[dict]:
    """全メール（[Gmail]/All Mail 相当）から自分が送った日次メールを集める。"""
    import email
    import imaplib
    from email.utils import parsedate_to_datetime
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(user, password)
    box = None
    typ, boxes = m.list()
    for b in boxes or []:
        line = b.decode(errors="replace")
        if "\\All" in line:
            box = line.split(' "/" ')[-1].strip()
            break
    m.select(box or "INBOX", readonly=True)
    typ, data = m.uid("search", None, f'(FROM "{user}" SINCE {since})')
    uids = (data[0] or b"").split()
    out = []
    for i in range(0, len(uids), 50):
        chunk = b",".join(uids[i:i + 50])
        typ, hdrs = m.uid("fetch", chunk, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])")
        keep = []
        for part in hdrs:
            if not isinstance(part, tuple):
                continue
            uid = re.search(rb"UID (\d+)", part[0]).group(1)
            h = email.message_from_bytes(part[1])
            if SUBJECT_MARK in _decode_subject(h.get("Subject")):
                keep.append(uid)
        for uid in keep:
            typ, body = m.uid("fetch", uid, "(RFC822)")
            msg = email.message_from_bytes(body[0][1])
            sent = parsedate_to_datetime(msg.get("Date"))
            out.append({"message_id": (msg.get("Message-ID") or "").strip(), "sent_at": sent.astimezone(timezone.utc).isoformat(),
                        "report_date": sent.astimezone().strftime("%Y-%m-%d") if sent.tzinfo else sent.strftime("%Y-%m-%d"),
                        "subject": _decode_subject(msg.get("Subject")), "html": _html_of(msg)})
    m.logout()
    return out


def latest_per_day(mails: list[dict]) -> list[dict]:
    """同じ日に複数あるときは最後に送ったもの（純関数）。"""
    best: dict = {}
    for x in mails:
        d = x["report_date"]
        if d not in best or x["sent_at"] > best[d]["sent_at"]:
            best[d] = x
    return [best[d] for d in sorted(best)]


def backfill(cfg, since: str = "01-Jan-2026") -> dict:
    import os
    from zoneinfo import ZoneInfo
    user, pwd = os.environ.get("ALERT_GMAIL_FROM", ""), os.environ.get("ALERT_GMAIL_PASSWORD", "")
    if not (user and pwd):
        return {"error": "ALERT_GMAIL_FROM / ALERT_GMAIL_PASSWORD がない"}
    mails = fetch_mails(user, pwd, since)
    for x in mails:
        x["report_date"] = datetime.fromisoformat(x["sent_at"]).astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    days = latest_per_day(mails)
    secs, items, empty = [], [], []
    for x in days:
        alerts = parse_mail_html(x["html"])
        if not alerts:
            empty.append(x["report_date"])
            continue
        s, it = rows_from_alerts(alerts, x["report_date"], "mail_backfill", x["sent_at"], x["message_id"])
        secs += s
        items += it
    counts = write(_client(), {"alert_sections": secs, "alert_items": items},
                   Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "tmp")
    info = {"mails": len(mails), "days": len(days), "first": days[0]["report_date"] if days else None,
            "last": days[-1]["report_date"] if days else None, "unparsed_days": empty, **counts}
    print(f"✅ 過去分: {info}")
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="送信済みの日次メールから過去分を入れる")
    ap.add_argument("--since", default="01-Jan-2026", help="IMAP の SINCE（例 01-Jan-2026）")
    args = ap.parse_args(argv)
    import config
    if args.backfill:
        info = backfill(config, args.since)
        return 1 if info.get("error") or not info.get("days") else 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
