# ==============================================================
# data_reliability.py  v1.0  (Wave R0-1 / E7')
# データ取得の冗長化・リトライ・鮮度SLA検査
# ==============================================================
# 【役割】
#   1) retry_call()        : 任意呼び出しの指数バックオフ・リトライ
#   2) fetch_price_safe()  : 価格取得 yfinance → Stooq → キャッシュ の3段フォールバック
#   3) EDINET繰越キュー    : 失敗doc_idを翌日実行に繰り越す（1日の追加処理上限あり）
#   4) run_data_sla_check(): 主要キャッシュの鮮度SLA検査 → results["data_sla_report"]
#
# 【設計根拠】要件定義書_統合改修_20260705.md §R0-1（D-05: J-Quants不使用・全て無料）
# 【禁止】有料API・APIキー必須サービスの追加（費用ゼロ原則）
# ==============================================================

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# ── リトライ規約（BLOCK 53 で上書き可） ─────────────────────────
_DEFAULT_RETRY_SCHEDULE = [2, 4, 8]   # 秒（指数バックオフ）


# ==============================================================
# BLOCK 1: 汎用リトライ
# ==============================================================

def retry_call(fn: Callable[[], Any],
               schedule: list = None,
               label: str = "") -> tuple:
    """
    fn() を最大 len(schedule)+1 回試行する。
    Returns: (result, error_str)  成功時 error_str は ""。
    ※ fn が例外を投げるか None を返したら失敗とみなす。
    """
    schedule = schedule if schedule is not None else list(_DEFAULT_RETRY_SCHEDULE)
    last_err = ""
    for attempt in range(len(schedule) + 1):
        try:
            result = fn()
            if result is not None:
                return result, ""
            last_err = "returned None"
        except Exception as e:
            last_err = str(e)[:120]
        if attempt < len(schedule):
            time.sleep(schedule[attempt])
    if label:
        print(f"    ⚠️  [data_reliability] retry尽き: {label} ({last_err})")
    return None, last_err


# ==============================================================
# BLOCK 2: 価格フォールバック（yfinance → Stooq → キャッシュ）
# ==============================================================

def _to_stooq_symbol(ticker: str) -> str:
    """
    yfinance形式 → Stooq形式。
    7203.T → 7203.jp / AAPL → aapl.us / 1306.T → 1306.jp
    """
    t = ticker.strip()
    if t.upper().endswith(".T"):
        return t[:-2].lower() + ".jp"
    if "." not in t:
        return t.lower() + ".us"
    return t.lower()


def _fetch_stooq_daily(ticker: str, timeout: int = 15) -> Optional[dict]:
    """
    Stooq（無料・認証不要）から日足CSVを取得し、直近行を返す。
    Returns: {"close": float, "date": "YYYY-MM-DD", "volume": float} or None
    """
    import requests  # 遅延import（テスト容易性）
    sym = _to_stooq_symbol(ticker)
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    r = requests.get(url, timeout=timeout)
    if r.status_code != 200 or not r.text or r.text.startswith("No data"):
        return None
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if not rows:
        return None
    last = rows[-1]
    try:
        return {
            "close":  float(last["Close"]),
            "date":   last["Date"],
            "volume": float(last.get("Volume") or 0),
        }
    except (KeyError, ValueError):
        return None


def fetch_price_safe(cfg, ticker: str,
                     cache_lookup: Callable[[str], Optional[dict]] = None) -> dict:
    """
    3段フォールバック価格取得。
    Returns（必ずdict）:
      {"ticker", "close", "date", "source": "yfinance"|"stooq"|"cache"|"none",
       "is_stale": bool, "error": str}
    """
    schedule = list(getattr(cfg, "DATA_RETRY_SCHEDULE", _DEFAULT_RETRY_SCHEDULE))

    # ① yfinance
    def _yf():
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
        if h is None or h.empty:
            return None
        return {"close": float(h["Close"].iloc[-1]),
                "date":  h.index[-1].strftime("%Y-%m-%d"),
                "volume": float(h["Volume"].iloc[-1])}
    result, err = retry_call(_yf, schedule, label=f"yfinance:{ticker}")
    if result:
        return {"ticker": ticker, "source": "yfinance", "is_stale": False,
                "error": "", **result}

    # ② Stooq
    if bool(getattr(cfg, "STOOQ_ENABLED", True)):
        result, err2 = retry_call(lambda: _fetch_stooq_daily(ticker),
                                  schedule[:1], label=f"stooq:{ticker}")
        if result:
            return {"ticker": ticker, "source": "stooq", "is_stale": False,
                    "error": "", **result}
        err = f"{err} / stooq:{err2}"

    # ③ キャッシュ（呼び出し側が lookup を渡した場合のみ）
    if cache_lookup is not None:
        try:
            cached = cache_lookup(ticker)
            if cached and cached.get("close"):
                return {"ticker": ticker, "source": "cache", "is_stale": True,
                        "error": err, "close": cached["close"],
                        "date": cached.get("date", ""), "volume": cached.get("volume", 0)}
        except Exception as e:
            err = f"{err} / cache:{str(e)[:50]}"

    return {"ticker": ticker, "source": "none", "is_stale": True,
            "error": err, "close": None, "date": "", "volume": 0}


# ==============================================================
# BLOCK 3: EDINET 繰越キュー
# ==============================================================

def _retry_queue_path(cfg) -> Path:
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "edinet_retry_queue.json"


def _load_queue(cfg) -> list:
    p = _retry_queue_path(cfg)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def push_edinet_retry(cfg, doc_id: str, ticker: str = "", reason: str = "") -> None:
    """EDINET取得失敗分を翌日繰越キューへ追記（doc_idで重複排除）。"""
    queue = _load_queue(cfg)
    if any(q.get("doc_id") == doc_id for q in queue):
        return
    queue.append({
        "doc_id":    doc_id,
        "ticker":    ticker,
        "reason":    reason[:100],
        "failed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "attempts":  1,
    })
    _retry_queue_path(cfg).write_text(
        json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")


def pop_edinet_retries(cfg, limit: int = None) -> list:
    """
    繰越キューの先頭から limit 件を取り出す（残りは保存し直す）。
    limit の既定は cfg.EDINET_RETRY_DAILY_LIMIT（=10）。
    ※ EDINET APIの予算80コール/日を侵食しないよう上限を厳守。
    """
    limit = limit if limit is not None else int(
        getattr(cfg, "EDINET_RETRY_DAILY_LIMIT", 10))
    queue = _load_queue(cfg)
    todo, rest = queue[:limit], queue[limit:]
    _retry_queue_path(cfg).write_text(
        json.dumps(rest, ensure_ascii=False, indent=2), encoding="utf-8")
    return todo


# ==============================================================
# BLOCK 4: 鮮度SLA検査
# ==============================================================

def _file_age_days(p: Path) -> Optional[float]:
    try:
        return (datetime.now()
                - datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() / 86400
    except Exception:
        return None


def run_data_sla_check(cfg, results: dict) -> dict:
    """
    主要キャッシュファイルの鮮度をSLA（BLOCK 53: DATA_SLA_DAYS）と突合。
    Returns: {"violations": [...], "checked": int, "violation_count": int}
    ※ 検査のみ（修復はしない）。E11ブリーフとMAINTENANCE_PACKETに表示。
    """
    sla_map: dict = dict(getattr(cfg, "DATA_SLA_DAYS", {
        "price": 2, "financials": 100, "news": 3}))
    targets: dict = dict(getattr(cfg, "DATA_SLA_TARGETS", {}))
    cache_dir = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    data_dir  = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))

    # 既定の検査対象（config側 DATA_SLA_TARGETS で上書き/追加可能）
    if not targets:
        targets = {
            "price":      str(data_dir / "score_history.json"),
            "news":       str(data_dir / "news_cache.json"),
            "financials": str(cache_dir / "edinet"),
        }

    violations, checked = [], 0
    for kind, path_str in targets.items():
        limit_days = sla_map.get(kind)
        if limit_days is None:
            continue
        p = Path(path_str)
        # ディレクトリなら最新ファイルの鮮度で代表
        if p.is_dir():
            files = sorted(p.glob("**/*.json"),
                           key=lambda f: f.stat().st_mtime, reverse=True)
            p = files[0] if files else p
        checked += 1
        if not p.exists():
            violations.append({"kind": kind, "path": str(p),
                               "age_days": None, "limit_days": limit_days,
                               "status": "missing"})
            continue
        age = _file_age_days(p)
        if age is not None and age > limit_days:
            violations.append({"kind": kind, "path": str(p),
                               "age_days": round(age, 1),
                               "limit_days": limit_days, "status": "stale"})

    report = {"violations": violations, "checked": checked,
              "violation_count": len(violations),
              "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    if violations:
        print(f"    ⚠️  [data_sla] SLA違反 {len(violations)}件: "
              + ", ".join(v["kind"] for v in violations))
    else:
        print(f"    ✅ [data_sla] SLA違反なし（{checked}対象）")
    return report
