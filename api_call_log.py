# ==============================================================
# api_call_log.py
# 有料・回数制限つきの外部API（EdinetDB など）を「どこから・どのパスを・どの結果で」
# 叩いたかを1回1行で残し、日次メール用に集計する。
# ==============================================================
# 【なぜ要るか】
#   EdinetDB の1日100回は DBRestClient 以外（edinet_client の直叩き・APIキー死活確認）からも
#   消費される。日次台帳（calls_used）だけでは「誰が何回使ったか」「残りがなぜ余ったか」が分からない。
# 【置き場所】
#   <DATA_DIR>/<source>_cache/calls_YYYY-MM-DD.jsonl（1行＝{"t","caller","path","status"}）。
#   書けなくても本処理は止めない（記録は補助）。
# ==============================================================
from __future__ import annotations

import inspect
import json
import os
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

_SKIP_FILES = {"api_call_log.py", "db_rest_client.py"}
_CODE_RE = re.compile(r"/(?:E\d{5}|\d[0-9A-Z]\d[0-9A-Z]0?)(?=/|$)")
_NUM_RE = re.compile(r"/\d{4,}(?=/|$)")


def _data_dir() -> Path:
    try:
        import config
        return Path(getattr(config, "DATA_DIR"))
    except Exception:
        base = os.environ.get("STOCK_ANALYZER_BASE", "stock_data")
        return Path(base) / "data"


def normalize_path(path: str) -> str:
    """URL やパスから会社コード・数値を伏せて種類だけにする（純関数）。
    例: https://edinetdb.jp/v1/companies/E03606/analysis → /companies/{code}/analysis"""
    p = str(path or "")
    p = re.sub(r"^https?://[^/]+", "", p)
    p = re.sub(r"^/v\d+", "", p)
    p = p.split("?", 1)[0]
    p = _CODE_RE.sub("/{code}", p)
    return _NUM_RE.sub("/{n}", p) or "/"


def caller_of(stack=None) -> str:
    """呼び出し元の「ファイル名:関数名」。このモジュールと REST クライアント自身は飛ばす。"""
    for fr in (stack or inspect.stack()[1:]):
        name = os.path.basename(fr.filename)
        if name in _SKIP_FILES or "site-packages" in fr.filename or name.startswith("<"):
            continue
        return f"{name[:-3] if name.endswith('.py') else name}:{fr.function}"
    return "unknown"


def log_path(source: str, day: str | None = None, data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / f"{source}_cache" / f"calls_{day or date.today().isoformat()}.jsonl"


def note(source: str, path: str, status, caller: str | None = None, data_dir: Path | None = None) -> None:
    """1回の HTTP 試行を記録する（失敗しても例外を出さない）。"""
    try:
        p = log_path(source, data_dir=data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"t": datetime.now().strftime("%H:%M:%S"), "caller": caller or caller_of(),
               "path": normalize_path(path), "status": status}
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def summarize(source: str, day: str | None = None, data_dir: Path | None = None) -> dict:
    """その日の記録を、呼び出し元別・パス別・結果別に数える。"""
    rows = []
    try:
        for line in log_path(source, day, data_dir).read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return {
        "total": len(rows),
        "by_caller": Counter(r.get("caller") for r in rows).most_common(),
        "by_path": Counter(r.get("path") for r in rows).most_common(),
        "by_status": Counter(str(r.get("status")) for r in rows).most_common(),
        "first": rows[0]["t"] if rows else None,
        "last": rows[-1]["t"] if rows else None,
    }
