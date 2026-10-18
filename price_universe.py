# ==============================================================
# price_universe.py
# 全上場企業（日本株・米国株）の日次終値を BigQuery analytics.price_daily へ蓄積する
# ==============================================================
# 【役割】
#   日本株は edinet.company（is_delisted=false で sec_code のある会社）、米国株は
#   SEC の company_tickers_exchange.json（NYSE / Nasdaq の普通株）を全銘柄の元とし、
#   yfinance から日足を取って price_daily へ追記する。企業スクリーナーの株価系の列
#   （時価総額・PER・PBR・騰落率）を全上場で埋めるための経路。
#   既存の price_history_writer（実行中に集めた銘柄だけを書く）とは別経路。
#
# 【実行】 python3 price_universe.py --market jp|us|all [--backfill] [--dry-run] [--limit N]
#   通常: 直近 DAILY_PERIOD（既定5日）を取り、BQに無い (ticker, trade_date) だけ追記
#   --backfill: BACKFILL_PERIOD（既定1年）を取る（初回の過去分。重複は同じ除外で防ぐ）
#
# 【費用・重複】
#   書き込みは load job（無料・クエリ枠を使わない）。既存キーの照会はパーティションを
#   絞った 1 クエリだけ。それでも混入した重複は analytics.price_daily_latest ビュー
#   （ticker, trade_date ごとに ingested_at 最新）で除く。
#
# 【取得の作法】
#   チャンク（既定300銘柄）に分け、チャンク間に間隔を空け、同時接続数（YF_THREADS）を絞る。
#   Yahoo は数千銘柄を続けて取ると数分間まとめて弾く（チャンクの取得率が数%に落ちる）。
#   取得率が CHUNK_FAIL_RATE 未満のチャンクは「制限中」とみなし、その場で1回だけ再試行したあと、
#   取れなかった銘柄を後回しの列に入れ、待ち時間を倍々に延ばしながら先へ進む。
#   本走のあと、後回しの銘柄を小さいチャンク（RETRY_CHUNK_SIZE）で RETRY_ROUNDS 回まで取り直す。
#   全体は TIME_BUDGET_SEC で打ち切る（ジョブの timeout-minutes より短く）。
#   制限中に取れなかった銘柄は「取れない」と記録しない（dead 判定に数えない）。
#
# 【取れない銘柄】
#   連続 DEAD_AFTER_MISSES 回データが無い銘柄は dead として記録し、以後は取得対象から外す
#   （DEAD_RETRY_DAYS 日ごとに1度だけ再確認）。取得率は dead を除いた対象に対して出し、
#   JP/US それぞれの下限を割ったら台帳に alert を残し、メールを送り、終了コード1で落とす。
# ==============================================================

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── 定数（config.py の B5_PRICE_UNIVERSE_* が正本。起動時に上書きされる）──
CHUNK_SIZE = 300
CHUNK_PAUSE_SEC = 3.0
CHUNK_RETRY_WAIT_SEC = 60.0
CHUNK_FAIL_RATE = 0.6          # チャンクの取得率がこれ未満なら制限中とみなす（平常時は9割超）
MAX_FAILED_CHUNKS_IN_ROW = 6   # 待ちを延ばしても連続でこれだけ失敗したら本走をやめて後回し分の再取得へ進む
COOLDOWN_MAX_SEC = 600.0       # 制限中の待ち時間の上限（CHUNK_RETRY_WAIT_SEC から倍々に延ばす）
RETRY_CHUNK_SIZE = 100
RETRY_ROUNDS = 2
RETRY_ROUND_WAIT_SEC = 300.0   # 後回し分を取り直す前の待ち
TIME_BUDGET_SEC = 3000.0       # 全体の時間予算（price_universe.yml の timeout-minutes 90 より十分短く）
YF_THREADS = 8                 # yfinance の同時接続数（True＝無制限だと数千銘柄で制限に掛かる）
MIN_RATE_JP = 0.90
MIN_RATE_US = 0.85
DEAD_AFTER_MISSES = 3
DEAD_RETRY_DAYS = 30
UNIVERSE_REFRESH_DAYS = 7
DAILY_PERIOD = "5d"
BACKFILL_PERIOD = "1y"
LOAD_BATCH_ROWS = 100000
EXISTING_KEY_DAYS_DAILY = 10
EXISTING_KEY_DAYS_BACKFILL = 400
SEC_MIN_INTERVAL_SEC = 0.5

_CFG_CONST_MAP = {
    "B5_PRICE_UNIVERSE_CHUNK_SIZE": "CHUNK_SIZE",
    "B5_PRICE_UNIVERSE_CHUNK_PAUSE_SEC": "CHUNK_PAUSE_SEC",
    "B5_PRICE_UNIVERSE_CHUNK_RETRY_WAIT_SEC": "CHUNK_RETRY_WAIT_SEC",
    "B5_PRICE_UNIVERSE_CHUNK_FAIL_RATE": "CHUNK_FAIL_RATE",
    "B5_PRICE_UNIVERSE_MAX_FAILED_CHUNKS_IN_ROW": "MAX_FAILED_CHUNKS_IN_ROW",
    "B5_PRICE_UNIVERSE_COOLDOWN_MAX_SEC": "COOLDOWN_MAX_SEC",
    "B5_PRICE_UNIVERSE_RETRY_CHUNK_SIZE": "RETRY_CHUNK_SIZE",
    "B5_PRICE_UNIVERSE_RETRY_ROUNDS": "RETRY_ROUNDS",
    "B5_PRICE_UNIVERSE_RETRY_ROUND_WAIT_SEC": "RETRY_ROUND_WAIT_SEC",
    "B5_PRICE_UNIVERSE_TIME_BUDGET_SEC": "TIME_BUDGET_SEC",
    "B5_PRICE_UNIVERSE_YF_THREADS": "YF_THREADS",
    "B5_PRICE_UNIVERSE_MIN_RATE_JP": "MIN_RATE_JP",
    "B5_PRICE_UNIVERSE_MIN_RATE_US": "MIN_RATE_US",
    "B5_PRICE_UNIVERSE_DEAD_AFTER_MISSES": "DEAD_AFTER_MISSES",
    "B5_PRICE_UNIVERSE_DEAD_RETRY_DAYS": "DEAD_RETRY_DAYS",
    "B5_PRICE_UNIVERSE_REFRESH_DAYS": "UNIVERSE_REFRESH_DAYS",
    "B5_PRICE_UNIVERSE_DAILY_PERIOD": "DAILY_PERIOD",
    "B5_PRICE_UNIVERSE_BACKFILL_PERIOD": "BACKFILL_PERIOD",
    "B5_PRICE_UNIVERSE_LOAD_BATCH_ROWS": "LOAD_BATCH_ROWS",
    "B5_PRICE_UNIVERSE_SEC_MIN_INTERVAL_SEC": "SEC_MIN_INTERVAL_SEC",
}

_TABLE_DATASET = "analytics"
_TABLE = "price_daily"
_VIEW = "price_daily_latest"
_BQ_LOCATION = "asia-northeast1"
_SEC_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_US_EXCHANGES = ("NYSE", "Nasdaq")
_SCHEMA = [
    ("trade_date", "DATE", "REQUIRED"), ("ticker", "STRING", "REQUIRED"),
    ("open", "FLOAT", "NULLABLE"), ("high", "FLOAT", "NULLABLE"),
    ("low", "FLOAT", "NULLABLE"), ("close", "FLOAT", "NULLABLE"),
    ("volume", "FLOAT", "NULLABLE"), ("source", "STRING", "NULLABLE"),
    ("ingested_at", "TIMESTAMP", "NULLABLE"),
]
_COLS = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}

_MARKET_TZ = {"jp": ("Asia/Tokyo", 15, 45), "us": ("America/New_York", 16, 30)}


# ==============================================================
# 純粋関数（ユニバース・チャンク・DataFrame の読み方・取得率）
# ==============================================================

def jp_ticker_from_sec_code(sec_code) -> str | None:
    """edinet.company.sec_code（5桁 '78370'・英字入り4桁 '271A' など）→ yfinance '7837.T'。"""
    s = str(sec_code or "").strip().upper()
    if len(s) < 4:
        return None
    code = s[:4]
    if not re.fullmatch(r"[0-9]{3}[0-9A-Z]", code):
        return None
    return f"{code}.T"


def build_jp_universe(sec_codes) -> list[str]:
    out = {jp_ticker_from_sec_code(c) for c in sec_codes or []}
    out.discard(None)
    return sorted(out)


# 優先株（-P / -PA …）・ワラント（-W / -WS / -WT）・ユニット（-U / -UN）・ライツ（-R / -RT）
_US_DERIV_SUFFIX = re.compile(r"-(P[A-Z]?|W[A-Z]{0,2}|U[N]?|R[A-Z]?)$")
# 取引所上の 5 文字目の慣習: W=ワラント R=ライツ U=ユニット Q=破産手続き中
_US_FIFTH_LETTER = re.compile(r"^[A-Z]{4}[WRUQ]$")
_US_NAME_EXCLUDE = re.compile(
    r"\b(ETF|ETN|Fund|Funds|Index|Acquisition|Warrants?)\b"
    r"|\bPreferred (Stock|Shares)\b|\bDepositary (Shares|Units)\b"
    r"|\bTrust\b.*\b(Shares|Units)\b|\bProShares\b|\biShares\b|\bSPDR\b|\bInvesco\b.*\bTrust\b",
    re.IGNORECASE,
)


def filter_us_universe(records) -> tuple[list[str], dict]:
    """SEC company_tickers_exchange の {cik,name,ticker,exchange} 群から普通株の ticker を選ぶ。

    除外: NYSE/Nasdaq 以外（OTC・CBOE 等）、優先株・ワラント・ユニット・ライツ、
    名称が ETF/ファンド/SPAC 等のもの。戻り値は (ticker 昇順, 除外件数の内訳)。
    """
    counts = {"total": 0, "other_exchange": 0, "derivative": 0, "fund_or_spac": 0, "kept": 0}
    keep: set[str] = set()
    for r in records or []:
        counts["total"] += 1
        ticker = str(r.get("ticker") or "").strip().upper()
        if not ticker or str(r.get("exchange") or "") not in _US_EXCHANGES:
            counts["other_exchange"] += 1
            continue
        if _US_DERIV_SUFFIX.search(ticker) or _US_FIFTH_LETTER.match(ticker):
            counts["derivative"] += 1
            continue
        if _US_NAME_EXCLUDE.search(str(r.get("name") or "")):
            counts["fund_or_spac"] += 1
            continue
        keep.add(ticker)
    counts["kept"] = len(keep)
    return sorted(keep), counts


def parse_sec_tickers(payload: dict) -> list[dict]:
    fields = payload.get("fields") or []
    return [dict(zip(fields, row)) for row in payload.get("data") or []]


def chunked(seq, n: int):
    n = max(1, int(n))
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def extract_frames(data, tickers) -> dict:
    """yfinance.download(group_by="ticker") の結果を {ticker: DataFrame} にする。

    列は MultiIndex で level0=ticker, level1=Open/High/…。data[ticker]["Close"] の順で引く
    （data["Close"][ticker] は KeyError になり、全銘柄が黙って捨てられる）。
    1銘柄だけで平らな列が返ったときは、その銘柄の列としてそのまま使う。
    """
    out: dict = {}
    if data is None or getattr(data, "empty", True):
        return out
    cols = data.columns
    if getattr(cols, "nlevels", 1) < 2:
        if len(tickers) == 1:
            df = data.dropna(subset=["Close"]) if "Close" in cols else data.iloc[0:0]
            if not df.empty:
                out[tickers[0]] = df
        return out
    present = set(cols.get_level_values(0))
    for t in tickers:
        if t not in present:
            continue
        df = data[t]
        if "Close" not in df.columns:
            continue
        df = df.dropna(subset=["Close"])
        if not df.empty:
            out[t] = df
    return out


def market_cutoff_date(market: str, now_utc: datetime) -> date:
    """この日付以降の足は取り込まない（立会い中の途中経過を終値として保存しないため）。

    現地の閉場後（JP 15:45・US 16:30）なら翌日、それ以前なら当日。
    """
    tzname, hh, mm = _MARKET_TZ[market]
    local = now_utc.astimezone(ZoneInfo(tzname))
    closed = (local.hour, local.minute) >= (hh, mm)
    return local.date() + timedelta(days=1 if closed else 0)


LEDGER_DAY_ROLLOVER_HOUR = 6   # JST のこの時刻より前に終わった回は前日の回として台帳に記録する


def ledger_date(now_jst: datetime) -> str:
    """台帳（record_external）に記録する日付（純関数）。

    日本株の回は 16:47 JST 予定だが、GitHub 側の遅れで開始が翌 02:00 になる日がある。
    終わった時刻の日付で記録すると、その日の台帳に JP の記録が無く、翌日に2回分が重なる。
    0〜6時に終わった回は前日の回とみなす（米国株の 07:47 予定の回は遅れても昼までに終わる）。"""
    d = now_jst.date()
    if now_jst.hour < LEDGER_DAY_ROLLOVER_HOUR:
        d -= timedelta(days=1)
    return d.isoformat()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else f


def rows_from_frames(frames: dict, existing: set, cutoff: date, now_iso: str,
                     source: str = "yfinance") -> list[dict]:
    """{ticker: DataFrame} → BQ 行。既存キー・cutoff 以降・終値なしは除く。"""
    rows: list[dict] = []
    for ticker, df in frames.items():
        if getattr(df.index, "has_duplicates", False):
            df = df[~df.index.duplicated(keep="last")]
        for idx, rec in df.to_dict("index").items():
            d = idx.date() if hasattr(idx, "date") else idx
            if not isinstance(d, date) or d >= cutoff:
                continue
            key = (ticker, d.isoformat())
            if key in existing:
                continue
            vals = {bq: _num(rec.get(src)) for src, bq in _COLS.items()}
            if vals["close"] is None:
                continue
            rows.append({"trade_date": d.isoformat(), "ticker": ticker,
                         "source": source, "ingested_at": now_iso, **vals})
    return rows


def select_targets(universe, dead: dict, today: str) -> tuple[list[str], int]:
    """dead でない銘柄と、再確認日が来た dead 銘柄を返す。(対象, 除外した dead 数)"""
    targets, skipped = [], 0
    for t in universe:
        e = dead.get(t)
        if e and e.get("dead") and today < e.get("next_retry", ""):
            skipped += 1
            continue
        targets.append(t)
    return targets, skipped


def update_dead_state(dead: dict, hit, missed, today: str,
                      after: int | None = None, retry_days: int | None = None) -> dict:
    """取れた銘柄は記録から外し、取れなかった銘柄の連続未取得を数える（1日1回まで）。"""
    after = DEAD_AFTER_MISSES if after is None else after
    retry_days = DEAD_RETRY_DAYS if retry_days is None else retry_days
    for t in hit:
        dead.pop(t, None)
    for t in missed:
        e = dead.setdefault(t, {"misses": 0, "first_miss": today, "dead": False})
        if e.get("last_miss") == today:
            continue
        e["misses"] = int(e.get("misses", 0)) + 1
        e["last_miss"] = today
        if e["misses"] >= after:
            e["dead"] = True
            e["next_retry"] = (date.fromisoformat(today) + timedelta(days=retry_days)).isoformat()
    return dead


def coverage(attempted: int, hit: int, min_rate: float) -> dict:
    rate = (hit / attempted) if attempted else 0.0
    ok = attempted > 0 and rate >= min_rate
    return {"attempted": attempted, "hit": hit, "missed": attempted - hit,
            "rate": round(rate, 4), "min_rate": min_rate,
            "status": "success" if ok else "alert"}


# ==============================================================
# 入出力（SEC・BigQuery・状態ファイル）
# ==============================================================

def _load_cfg():
    try:
        import config as cfg
        return cfg
    except Exception as e:
        print(f"⚠️ config 読込失敗（既定値で継続）: {str(e)[:100]}")
        return None


def _state_dir(cfg) -> Path:
    base = Path(getattr(cfg, "DRIVE_BASE", "stock_data")) if cfg else Path("stock_data")
    d = base / "data" / "price_universe"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def _fresh(saved: dict, today: str) -> bool:
    try:
        age = (date.fromisoformat(today) - date.fromisoformat(saved["fetched"])).days
    except Exception:
        return False
    return 0 <= age < UNIVERSE_REFRESH_DAYS and bool(saved.get("tickers"))


def bq_client(cfg=None):
    from google.cloud import bigquery
    from google.oauth2 import service_account
    raw = os.environ.get("BQ_CREDENTIALS", "")
    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = os.environ.get("BQ_PROJECT_ID") or info.get("project_id")
        return bigquery.Client(project=project, credentials=creds, location=_BQ_LOCATION)
    path = str(getattr(cfg, "BQ_CREDENTIALS_PATH", "") or "")
    if path and Path(path).exists():
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = os.environ.get("BQ_PROJECT_ID") or getattr(cfg, "BQ_PROJECT_ID", "") or creds.project_id
        return bigquery.Client(project=project, credentials=creds, location=_BQ_LOCATION)
    raise RuntimeError("BQ認証情報がありません（BQ_CREDENTIALS か BQ_CREDENTIALS_PATH）")


def load_jp_universe(cfg, client, today: str) -> list[str]:
    path = _state_dir(cfg) / "jp_universe.json"
    saved = _read_json(path, {})
    if _fresh(saved, today):
        return saved["tickers"]
    sql = (f"SELECT DISTINCT sec_code FROM `{client.project}.edinet.company` "
           "WHERE is_delisted = FALSE AND sec_code IS NOT NULL AND sec_code != ''")
    codes = [r["sec_code"] for r in client.query(sql).result()]
    tickers = build_jp_universe(codes)
    if not tickers:
        if saved.get("tickers"):
            print("⚠️ JPユニバースの取得が0件。前回のキャッシュを使う")
            return saved["tickers"]
        raise RuntimeError("JPユニバースが0件（edinet.company を確認）")
    _write_json(path, {"fetched": today, "source": "edinet.company is_delisted=false",
                       "count": len(tickers), "tickers": tickers})
    return tickers


def load_us_universe(cfg, today: str) -> tuple[list[str], dict]:
    path = _state_dir(cfg) / "us_universe.json"
    saved = _read_json(path, {})
    if _fresh(saved, today):
        return saved["tickers"], saved.get("counts", {})
    import requests
    ua = os.environ.get("SEC_USER_AGENT") or str(getattr(cfg, "SEC_USER_AGENT", "") or "StockAnalyzer")
    try:
        time.sleep(SEC_MIN_INTERVAL_SEC)
        resp = requests.get(_SEC_URL, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=60)
        resp.raise_for_status()
        tickers, counts = filter_us_universe(parse_sec_tickers(resp.json()))
        if len(tickers) < 1000:
            raise RuntimeError(f"US普通株が{len(tickers)}件しかない（SECの形式変更を疑う）")
    except Exception as e:
        if saved.get("tickers"):
            print(f"⚠️ SECユニバース取得失敗（前回のキャッシュを使う）: {str(e)[:100]}")
            return saved["tickers"], saved.get("counts", {})
        raise
    _write_json(path, {"fetched": today, "source": _SEC_URL, "counts": counts,
                       "count": len(tickers), "tickers": tickers})
    return tickers, counts


def existing_keys(client, days: int) -> set:
    """直近 days 日の (ticker, trade_date) を 1 クエリで取る（パーティションを絞る）。"""
    from google.cloud import bigquery
    sql = (f"SELECT DISTINCT ticker, FORMAT_DATE('%F', trade_date) AS d "
           f"FROM `{client.project}.{_TABLE_DATASET}.{_TABLE}` "
           "WHERE trade_date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL @days DAY)")
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("days", "INT64", int(days))],
        maximum_bytes_billed=1024 ** 3)
    return {(r["ticker"], r["d"]) for r in client.query(sql, job_config=cfg).result()}


def ensure_view(client) -> bool:
    """重複除去ビュー analytics.price_daily_latest が無ければ作る（あれば何もしない）。"""
    from google.api_core.exceptions import NotFound
    ref = f"{client.project}.{_TABLE_DATASET}.{_VIEW}"
    try:
        client.get_table(ref)
        return False
    except NotFound:
        pass
    src = f"{client.project}.{_TABLE_DATASET}.{_TABLE}"
    client.query(
        f"CREATE OR REPLACE VIEW `{ref}` AS SELECT * EXCEPT(rn) FROM ("
        f"SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker, trade_date ORDER BY ingested_at DESC) AS rn "
        f"FROM `{src}`) WHERE rn = 1").result()
    return True


def load_rows(client, rows: list[dict]) -> int:
    """load job（無料）で price_daily へ追記する。"""
    from google.cloud import bigquery
    if not rows:
        return 0
    job_cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        schema=[bigquery.SchemaField(n, t, mode=m) for n, t, m in _SCHEMA])
    job = client.load_table_from_json(
        rows, f"{client.project}.{_TABLE_DATASET}.{_TABLE}", job_config=job_cfg, location=_BQ_LOCATION)
    job.result()
    return int(job.output_rows or len(rows))


# ==============================================================
# 本体
# ==============================================================

def download_chunk(yf, chunk: list[str], period: str):
    return yf.download(chunk, period=period, interval="1d", group_by="ticker",
                       auto_adjust=False, threads=YF_THREADS, progress=False)


def run_market(market: str, cfg, client, backfill: bool = False, limit: int = 0,
               dry_run: bool = False, yf=None) -> dict:
    t0 = time.time()
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    period = BACKFILL_PERIOD if backfill else DAILY_PERIOD
    info: dict = {"market": market, "period": period, "backfill": backfill}

    if market == "jp":
        universe = load_jp_universe(cfg, client, today)
    else:
        universe, info["us_universe_counts"] = load_us_universe(cfg, today)
    info["universe"] = len(universe)

    dead_path = _state_dir(cfg) / f"dead_tickers_{market}.json"
    dead = _read_json(dead_path, {})
    targets, skipped_dead = select_targets(universe, dead, today)
    if limit:
        targets = targets[:limit]
    info["dead_skipped"] = skipped_dead
    info["targets"] = len(targets)
    print(f"[{market}] ユニバース{len(universe)} / dead除外{skipped_dead} / 取得対象{len(targets)} / period={period}")

    if yf is None:
        import yfinance as yf
    existing = set() if dry_run else existing_keys(
        client, EXISTING_KEY_DAYS_BACKFILL if backfill else EXISTING_KEY_DAYS_DAILY)
    if not dry_run:
        ensure_view(client)
    cutoff = market_cutoff_date(market, now)
    now_iso = now.isoformat()

    hit: set = set()
    missed: set = set()
    deferred: list[str] = []
    failed_chunks = 0
    aborted = False
    buf: list[dict] = []
    loaded = 0

    def left() -> float:
        return TIME_BUDGET_SEC - (time.time() - t0)

    def fetch(chunk: list[str], label: str) -> dict:
        """1チャンクを取る。取得率が低ければ1回だけ待って再試行する。"""
        frames: dict = {}
        for attempt in (1, 2):
            try:
                frames = extract_frames(download_chunk(yf, chunk, period), chunk)
            except Exception as e:
                print(f"  {label} 例外 {type(e).__name__}: {str(e)[:80]}")
                frames = {}
            if len(frames) / len(chunk) >= CHUNK_FAIL_RATE:
                break
            if attempt == 1 and left() > CHUNK_RETRY_WAIT_SEC:
                print(f"  {label} 取得率{len(frames)}/{len(chunk)}。{CHUNK_RETRY_WAIT_SEC:.0f}秒待って再試行")
                time.sleep(CHUNK_RETRY_WAIT_SEC)
            else:
                break
        return frames

    def run_pass(queue: list[str], size: int, tag: str) -> list[str]:
        """queue を size ごとに取る。制限中で取れなかった銘柄を返す（後回し）。"""
        nonlocal failed_chunks, aborted, buf, loaded
        later: list[str] = []
        chunks = list(chunked(queue, size))
        in_row = 0
        for i, chunk in enumerate(chunks, 1):
            label = f"{tag} {i}/{len(chunks)}"
            if left() <= 0:
                aborted = True
                rest = [t for c in chunks[i - 1:] for t in c]
                later.extend(rest)
                print(f"  ⏱ 時間予算 {TIME_BUDGET_SEC:.0f}秒を使い切った。残り{len(rest)}銘柄は未取得のまま終える")
                break
            frames = fetch(chunk, label)
            got = set(frames)
            hit.update(got)
            buf.extend(rows_from_frames(frames, existing, cutoff, now_iso))
            if len(frames) / len(chunk) < CHUNK_FAIL_RATE:
                failed_chunks += 1
                in_row += 1
                later.extend(t for t in chunk if t not in got)
                if in_row >= MAX_FAILED_CHUNKS_IN_ROW:
                    rest = [t for c in chunks[i:] for t in c]
                    later.extend(rest)
                    print(f"  ❌ {in_row}回連続で制限中。本走をやめ、残り{len(rest)}銘柄も後回しにする")
                    break
                wait = min(COOLDOWN_MAX_SEC, CHUNK_RETRY_WAIT_SEC * (2 ** in_row))
            else:
                in_row = 0
                missed.update(t for t in chunk if t not in got)
                wait = CHUNK_PAUSE_SEC
            if len(buf) >= LOAD_BATCH_ROWS and not dry_run:
                loaded += load_rows(client, buf)
                buf = []
            print(f"  {label}: 累計 取得{len(hit)} 未取得{len(missed)} 後回し{len(later)} 行{loaded + len(buf)}")
            if i < len(chunks):
                if in_row:
                    wait = min(wait, max(0.0, left()))
                    print(f"    制限中とみなし {wait:.0f}秒待つ")
                time.sleep(wait)
        return later

    deferred = run_pass(targets, CHUNK_SIZE, "chunk")
    for rnd in range(1, RETRY_ROUNDS + 1):
        if not deferred or aborted:
            break
        if left() < RETRY_ROUND_WAIT_SEC + 60:
            aborted = True
            print(f"  ⏱ 時間予算が足りず、後回し{len(deferred)}銘柄の取り直しをしない")
            break
        print(f"  後回し{len(deferred)}銘柄を {RETRY_ROUND_WAIT_SEC:.0f}秒待ってから取り直す（{rnd}/{RETRY_ROUNDS}回目）")
        time.sleep(RETRY_ROUND_WAIT_SEC)
        deferred = run_pass(deferred, RETRY_CHUNK_SIZE, f"retry{rnd}")
    if buf and not dry_run:
        loaded += load_rows(client, buf)
    rows_total = loaded + (len(buf) if dry_run else 0)

    if not dry_run:
        _write_json(dead_path, update_dead_state(dead, hit, missed, today))
    cov = coverage(len(targets), len(hit), MIN_RATE_JP if market == "jp" else MIN_RATE_US)
    info.update(cov)
    info.update({"rows": rows_total, "failed_chunks": failed_chunks, "aborted": aborted,
                 "deferred_left": len(deferred), "no_data": len(missed),
                 "dead_total": sum(1 for e in dead.values() if e.get("dead")),
                 "elapsed_sec": round(time.time() - t0, 1)})
    if aborted:
        info["status"] = "alert"
    return info


def _note(info: dict) -> str:
    return (f"{info['market']} 取得{info['hit']}/{info['attempted']}（{info['rate']:.1%}・下限{info['min_rate']:.0%}）"
            f" 未取得{info['missed']} dead{info['dead_total']} 行{info['rows']} {info['elapsed_sec']}秒"
            + (f" 制限で取れず{info['deferred_left']}" if info.get("deferred_left") else "")
            + (" 打ち切り" if info.get("aborted") else ""))


def _notify(cfg, subject: str, body: str) -> None:
    try:
        frm = os.environ.get("ALERT_GMAIL_FROM", "")
        pwd = os.environ.get("ALERT_GMAIL_PASSWORD", "")
        to = os.environ.get("ALERT_GMAIL_TO", "")
        if not (frm and pwd and to):
            return
        from alert_notifier import send_gmail
        ok, err = send_gmail(frm, pwd, to, subject, f"<pre>{body}</pre>")
        print("📧 アラート送信" if ok else f"⚠️ アラート送信失敗: {err}")
    except Exception as e:
        print(f"⚠️ アラート送信失敗: {str(e)[:80]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["jp", "us", "all"], default="all")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="BQに書かない・状態も更新しない")
    ap.add_argument("--limit", type=int, default=0, help="対象銘柄数の上限（試験用）")
    args = ap.parse_args(argv)

    cfg = _load_cfg()
    from config_sync import sync_module_constants
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    try:
        client = bq_client(cfg)
    except Exception:
        if not args.dry_run:
            raise
        client = None  # dry-run は BQ 不要（JPユニバースはキャッシュがある時だけ使える）
    markets = ["jp", "us"] if args.market == "all" else [args.market]
    bad: list[str] = []
    for m in markets:
        name = f"price_universe_{'backfill_' if args.backfill else ''}{m}"
        try:
            info = run_market(m, cfg, client, backfill=args.backfill, limit=args.limit, dry_run=args.dry_run)
            status, note = info["status"], _note(info)
        except Exception as e:
            info, status, note = {}, "error", f"{type(e).__name__}: {str(e)[:150]}"
        print(f"📒 {name}: {status} {note}")
        if info:
            print(json.dumps(info, ensure_ascii=False, default=str))
        if not args.dry_run and not args.limit:
            try:
                from module_ledger import record_external
                record_external(cfg, name, status, note,
                                date_str=ledger_date(datetime.now(ZoneInfo("Asia/Tokyo"))))
            except Exception as e:
                print(f"⚠️ 台帳への記録に失敗: {str(e)[:80]}")
        if status != "success":
            bad.append(f"{name}: {note}")
    if bad:
        _notify(cfg, "🚨 全上場株価の取得に問題", "\n".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
