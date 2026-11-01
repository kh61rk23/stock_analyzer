#!/usr/bin/env python3
"""S8 LLMニュース・アルファの週次結果を invest.llm_news_* に入れるための SQL を作る。

  check [--file F]   data/llm_news_signal.json を検査する（問題があれば理由を出して exit 1）
  sql   [--file F]   検査して、BigQuery に流す1本のスクリプト（同じ as_of を消して入れ直す）を標準出力へ出す

BigQuery へは触らない。スクリプトを流すのは、BigQuery コネクタを持つ週次の定時実行（または手動）。
文字列の引用符・改行の扱いをここで一括して行い、実行側が SQL を手で組まなくて済むようにしている。
signal_aggregator の点数・重みには入れない（参考情報。配線は D-19 で決める）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from invest_bq_schema import NEWS_RUNS_TABLE, NEWS_SIGNALS_TABLE  # noqa: E402

DEFAULT_FILE = REPO / "data" / "llm_news_signal.json"
PROJECT = "stock-analyzer-edinet"
DATASET = "invest"
MAX_EXPIRE_DAYS = 28
REQUIRED_KEYS = ("ticker", "direction", "strength", "thesis", "source_url", "expire_date")
DIRECTIONS = ("bull", "bear")


def _date(v, label: str) -> date:
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise ValueError(f"{label} は YYYY-MM-DD で書く: {v!r}") from None


def market_of(ticker: str) -> str:
    return "JP" if ticker.upper().endswith(".T") else "US"


def validate(data: dict) -> tuple:
    """(as_of, rows) を返す。rows は列名→値の dict のリスト。不正なら ValueError（理由は1件目だけ）。"""
    if not isinstance(data, dict):
        raise ValueError("JSON のトップがオブジェクトではない")
    as_of = _date(data.get("as_of"), "as_of")
    signals = data.get("signals")
    if not isinstance(signals, list):
        raise ValueError("signals が配列ではない（0件は [] と書く）")
    rows, seen = [], set()
    for i, s in enumerate(signals, 1):
        if not isinstance(s, dict):
            raise ValueError(f"signals[{i}] がオブジェクトではない")
        missing = [k for k in REQUIRED_KEYS if s.get(k) in (None, "")]
        if missing:
            raise ValueError(f"signals[{i}] に必須キーが無い: {', '.join(missing)}")
        ticker = str(s["ticker"]).strip()
        if ticker in seen:
            raise ValueError(f"signals[{i}] ticker が重複: {ticker}")
        seen.add(ticker)
        if s["direction"] not in DIRECTIONS:
            raise ValueError(f"signals[{i}] direction は bull か bear: {s['direction']!r}")
        strength = s["strength"]
        if isinstance(strength, bool) or not isinstance(strength, int) or strength not in (1, 2, 3):
            raise ValueError(f"signals[{i}] strength は 1〜3 の整数: {strength!r}")
        url = str(s["source_url"]).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"signals[{i}] source_url が http(s) ではない")
        expire = _date(s["expire_date"], f"signals[{i}] expire_date")
        if not as_of <= expire <= as_of + timedelta(days=MAX_EXPIRE_DAYS):
            raise ValueError(f"signals[{i}] expire_date は as_of から{MAX_EXPIRE_DAYS}日以内: {expire}")
        market = str(s.get("market") or market_of(ticker)).strip().upper()
        if market not in ("JP", "US"):
            raise ValueError(f"signals[{i}] market は JP か US: {market!r}")
        rows.append({"as_of": as_of, "ticker": ticker, "market": market, "direction": s["direction"],
                     "strength": strength, "thesis": str(s["thesis"]).strip(), "source_url": url,
                     "expire_date": expire})
    return as_of, rows


def lit(v) -> str:
    """BigQuery（GoogleSQL）のリテラル。文字列は二重引用符＋バックスラッシュのエスケープ。"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, date):
        return f'DATE "{v.isoformat()}"'
    out = []
    for ch in str(v):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def build_script(data: dict, recorded_at: datetime | None = None, run_env: str = "cloud_routine") -> str:
    """同じ as_of の実行記録とシグナルを消して入れ直す1本のスクリプト。0件でも実行記録は1行入る。"""
    as_of, rows = validate(data)
    now = (recorded_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ts = f'TIMESTAMP "{now.strftime("%Y-%m-%d %H:%M:%S")} UTC"'
    runs = f"`{PROJECT}.{DATASET}.{NEWS_RUNS_TABLE}`"
    sigs = f"`{PROJECT}.{DATASET}.{NEWS_SIGNALS_TABLE}`"
    d = lit(as_of)
    parts = [
        "BEGIN TRANSACTION;",
        f"DELETE FROM {runs} WHERE as_of = {d};",
        f"DELETE FROM {sigs} WHERE as_of = {d};",
        f"INSERT INTO {runs} (as_of, signal_count, recorded_at, run_env, note) "
        f"VALUES ({d}, {len(rows)}, {ts}, {lit(run_env)}, {lit('' if rows else '該当なし')});",
    ]
    if rows:
        cols = ("as_of", "ticker", "market", "direction", "strength", "thesis", "source_url", "expire_date")
        values = ",\n  ".join(
            "(" + ", ".join(lit(r[c]) for c in cols) + f", {ts})" for r in rows)
        parts.append(f"INSERT INTO {sigs} ({', '.join(cols)}, recorded_at) VALUES\n  {values};")
    parts.append("COMMIT TRANSACTION;")
    return "\n".join(parts)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("check", "sql"))
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--run-env", default="cloud_routine")
    args = ap.parse_args(argv)
    try:
        data = _load(Path(args.file))
        if args.cmd == "check":
            as_of, rows = validate(data)
            print(f"OK as_of={as_of} signals={len(rows)}")
        else:
            print(build_script(data, run_env=args.run_env))
    except (OSError, ValueError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
