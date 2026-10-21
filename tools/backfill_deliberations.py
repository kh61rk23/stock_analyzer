#!/usr/bin/env python3
"""vault の投資部（B-investment）の過去審議を invest.deliberations へ一度だけ取り込む。

入力: 40_判断台帳.md（正本の一覧）＋ 30_レポート/<case_id>_判断レポート.md ＋ 20_審議/<case_id>/{bull,bear,devil}.md
出力: case ごとの INSERT 文（--emit-dir）。ローカルに BQ 認証が無い環境では、この INSERT を
      BigQuery MCP の execute_sql で流す。直接投入する場合は BQ_CREDENTIALS を設定して --load。

冪等: INSERT は `WHERE NOT EXISTS (case_id)` 付きなので、何度流しても同じ case は二重に入らない。
取り込まない行: ticker が銘柄コード形式でない行（例: D-03 制度決議）。件数に「skipped」として出す。
承認列: 台帳に残っている人間の承認（状態列の「承認（YYYY-MM-DD）」）だけを転記する。
        エージェントが承認を作ることはない。

  python3 tools/backfill_deliberations.py                       # 解析結果の一覧（何も書かない）
  python3 tools/backfill_deliberations.py --emit-dir DIR        # case ごとの .sql と expected.json を書く
  python3 tools/backfill_deliberations.py --load                # BQ_CREDENTIALS で直接投入
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import invest_queue as iq  # noqa: E402

DEFAULT_VAULT = (Path.home() / "Documents" / "Obsidian_Local" / "Vault2_ClaudeCode_Local" / "CEO室"
                 / "departments" / "B-investment")
JST = timezone(timedelta(hours=9))
TICKER_RE = re.compile(r"^(?:\d{3}[0-9A-Z]\.T|[A-Z]{1,5})$")
APPROVED_RE = re.compile(r"承認[（(](\d{4}-\d{2}-\d{2})[）)]")
TEXT_COLS = ("bull_memo", "bear_memo", "devil_memo", "report_md")

COLUMNS = ["case_id", "ticker", "company_name", "sector_code", "deliberated_at", "verdict", "conviction",
           "layer", "bull_conviction", "bear_conviction", "devil_unresolved", "devil_reversal", "kpi",
           "bull_memo", "bear_memo", "devil_memo", "report_md", "packet_md", "models", "run_env",
           "approval_status", "approved_by", "approved_at", "review_on"]


# ── 台帳のパース（純粋関数・pytest 対象） ───────────────────────

def parse_ledger(text: str) -> tuple:
    """40_判断台帳.md の表を読む。Returns: (取り込む行のリスト, 取り込まない行のリスト)

    取り込む行: {"case_id","ticker","verdict","conviction","layer","unresolved","status",
                 "approval_status","approved_at"}
    """
    keep, skipped = [], []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 7 or cells[0] in ("case_id", "") or set(cells[0]) <= {"-", " "}:
            continue
        case_id, ticker, verdict, conv, layer, unres, status = cells[:7]
        ticker = ticker.replace("[[", "").replace("]]", "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}_", case_id) or not TICKER_RE.match(ticker):
            skipped.append({"case_id": case_id, "ticker": ticker, "reason": "ticker が銘柄コード形式でない"})
            continue
        m = APPROVED_RE.search(status)
        if m:
            approval, approved_at = "承認", m.group(1)
        elif "承認待ち" in status:
            approval, approved_at = "承認待ち", None
        else:
            approval, approved_at = None, None
        keep.append({"case_id": case_id, "ticker": ticker, "verdict": verdict, "conviction": iq._int(conv),
                     "layer": layer or None, "unresolved": iq._int(unres), "status": status,
                     "approval_status": approval, "approved_at": approved_at})
    return keep, skipped


def case_datetime(case_id: str) -> datetime:
    """case_id 先頭の日付を JST 正午の時刻にする（時刻は元データに無い）。"""
    d = date.fromisoformat(case_id[:10])
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=JST)


def build_row(entry: dict, report_md: str | None, memos: dict) -> dict:
    """台帳行＋レポート本文＋3メモ → deliberations の行（承認以外は invest_queue と同じ導出）。"""
    fm = iq.parse_frontmatter(report_md or "")
    bull, bear = memos.get("bull.md") or "", memos.get("bear.md") or ""
    devil = memos.get("devil.md") or ""
    bconv = iq._int(fm.get("bull_conviction")) or iq._int(iq.memo_conclusion(bull, "bull").get("conviction"))
    rconv = iq._int(fm.get("bear_conviction")) or iq._int(iq.memo_conclusion(bear, "bear").get("conviction"))
    dy = iq.memo_conclusion(devil, "devil")
    approved_at = entry.get("approved_at")
    review_on = (date.fromisoformat(approved_at) + timedelta(days=90)).isoformat() if approved_at else None
    return {
        "case_id": entry["case_id"],
        "ticker": entry["ticker"],
        "company_name": fm.get("company") or fm.get("company_name"),
        "sector_code": None,
        "deliberated_at": case_datetime(entry["case_id"]).isoformat(),
        "verdict": entry["verdict"],
        "conviction": entry["conviction"],
        "layer": entry["layer"],
        "bull_conviction": bconv,
        "bear_conviction": rconv,
        "devil_unresolved": entry["unresolved"] if entry["unresolved"] is not None
        else iq._first_int(fm.get("unresolved"), dy.get("unresolved_critical")),
        "devil_reversal": iq.count_reversal(devil) if devil else None,
        "kpi": iq.parse_kpi(report_md or "") or None,
        "bull_memo": bull or None,
        "bear_memo": bear or None,
        "devil_memo": devil or None,
        "report_md": report_md or None,
        "packet_md": None,
        "models": None,
        "run_env": "backfill",
        "approval_status": entry["approval_status"],
        "approved_by": "利用者" if entry["approval_status"] == "承認" else None,
        "approved_at": f"{approved_at}T00:00:00+09:00" if approved_at else None,
        "review_on": review_on,
    }


# ── SQL 生成 ──────────────────────────────────────────────────

def sql_string(s: str) -> str:
    """BigQuery の文字列リテラルにする。raw 三重引用符で書ければそのまま（本文を一切変えない）、
    区切りが本文に含まれるときだけ通常の引用符にエスケープして書く。"""
    for q in ('"""', "'''"):
        if q not in s and not s.endswith((q[0], "\\")) and "\r" not in s:
            return f"r{q}{s}{q}"
    esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{esc}"'


def _lit(col: str, v) -> str:
    if v is None:
        return "CAST(NULL AS " + _null_type(col) + ")"
    if col in ("kpi", "models"):
        return f"PARSE_JSON({sql_string(json.dumps(v, ensure_ascii=False))})"
    if col in ("deliberated_at", "approved_at"):
        return f"TIMESTAMP {sql_string(v)}"
    if col == "review_on":
        return f"DATE {sql_string(v)}"
    if isinstance(v, int):
        return str(v)
    return sql_string(str(v))


def _null_type(col: str) -> str:
    if col in ("kpi", "models"):
        return "JSON"
    if col in ("deliberated_at", "approved_at"):
        return "TIMESTAMP"
    if col == "review_on":
        return "DATE"
    if col in ("conviction", "bull_conviction", "bear_conviction", "devil_unresolved", "devil_reversal"):
        return "INT64"
    return "STRING"


def insert_sql(row: dict, project: str) -> str:
    """case 1件分の冪等 INSERT（同じ case_id が既にあれば何も入れない）。"""
    t = f"`{project}.invest.deliberations`"
    vals = ",\n  ".join(_lit(c, row.get(c)) for c in COLUMNS)
    cid = sql_string(row["case_id"])
    return (f"INSERT INTO {t} ({', '.join(COLUMNS)})\nSELECT\n  {vals}\n"
            f"FROM (SELECT 1)\nWHERE NOT EXISTS (SELECT 1 FROM {t} WHERE case_id = {cid})")


def fingerprint(row: dict) -> dict:
    """転記の取りこぼし検出用: 本文列ごとの文字数と MD5（BQ 側の LENGTH / TO_HEX(MD5) と照合する）。"""
    out = {}
    for c in TEXT_COLS:
        v = row.get(c)
        out[c] = None if v is None else {"len": len(v), "md5": hashlib.md5(v.encode("utf-8")).hexdigest()}
    return out


# ── vault 読み込み ────────────────────────────────────────────

def collect(vault: Path) -> tuple:
    ledger = vault / "40_判断台帳.md"
    entries, skipped = parse_ledger(ledger.read_text(encoding="utf-8"))
    rows = []
    for e in entries:
        rp = vault / "30_レポート" / f"{e['case_id']}_判断レポート.md"
        report = rp.read_text(encoding="utf-8") if rp.exists() else None
        memos = {}
        for n in ("bull.md", "bear.md", "devil.md"):
            mp = vault / "20_審議" / e["case_id"] / n
            if mp.exists():
                memos[n] = mp.read_text(encoding="utf-8")
        rows.append(build_row(e, report, memos))
    return rows, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", default=str(DEFAULT_VAULT))
    ap.add_argument("--project", default="stock-analyzer-edinet")
    ap.add_argument("--emit-dir", help="case ごとの .sql と expected.json を書き出す")
    ap.add_argument("--load", action="store_true", help="BQ_CREDENTIALS で直接投入する")
    ap.add_argument("--replace", action="store_true",
                    help="--load 時、既存 case を消して入れ直す（メタ情報だけ先に入れた case の本文補完用）")
    ap.add_argument("--meta-only", action="store_true",
                    help="本文（メモ・レポート）を除いたメタ情報だけの行にする（BQ MCP の書き込み量を抑える用）")
    ap.add_argument("--exclude", default="", help="カンマ区切りの case_id を対象から外す")
    args = ap.parse_args(argv)

    rows, skipped = collect(Path(args.vault))
    ex = {c.strip() for c in args.exclude.split(",") if c.strip()}
    rows = [r for r in rows if r["case_id"] not in ex]
    if args.meta_only:
        for r in rows:
            for c in TEXT_COLS:
                r[c] = None
    print(f"取り込み対象 {len(rows)} 件 / 対象外 {len(skipped)} 件")
    for r in rows:
        print(f"  {r['case_id']:<22} {r['verdict']:<6} conv={r['conviction']} 承認={r['approval_status']} "
              f"report={'○' if r['report_md'] else '×'} memos="
              f"{sum(1 for c in ('bull_memo', 'bear_memo', 'devil_memo') if r[c])}/3 kpi={'○' if r['kpi'] else '×'}")
    for s in skipped:
        print(f"  skip: {s['case_id']} ({s['reason']}: {s['ticker']})")

    if args.emit_dir:
        out = Path(args.emit_dir)
        out.mkdir(parents=True, exist_ok=True)
        expected = {}
        for r in rows:
            (out / f"{r['case_id']}.sql").write_text(insert_sql(r, args.project), encoding="utf-8")
            expected[r["case_id"]] = fingerprint(r)
        (out / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"SQL を書き出した: {out}")

    if args.load:
        from edinet_bq_client import EDINETBQClient
        client = EDINETBQClient().client
        for r in rows:
            if args.replace:
                client.query(f"DELETE FROM `{client.project}.invest.deliberations` WHERE case_id = "
                             f"{sql_string(r['case_id'])}", location="asia-northeast1").result()
            client.query(insert_sql(r, client.project), location="asia-northeast1").result()
        print(f"投入完了: {len(rows)} 件（既存 case は何もしない）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
