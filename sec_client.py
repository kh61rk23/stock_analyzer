# ==============================================================
# sec_client.py  v1.0  (U-1a)
# SEC EDGAR 共通アクセス層
# ==============================================================
# 【役割】
#   SEC EDGAR への全アクセスの単一窓口（L1 取得層）。
#   User-Agent 解決・CIK マップ（company_tickers.json）・レート制限・
#   429/5xx リトライ・共通ディスクキャッシュを1箇所に集約する。
#
# 【利用元】
#   institutional_13f_monitor / sec_geographic_extractor / sec_summarizer /
#   us_8k_monitor / us_fundamental_rotator / us_insider_form4_scraper /
#   us_sector_bridge_enhanced（insider_monitor は us_insider_form4_scraper 経由）。
#   User-Agent・CIK解決・レート制限・リトライを各モジュールで独自実装しないこと（D-6/D-7）。
#   詳細: 02_Configuration_Management/05_提案_2026-07-26/
#         要件定義書_JP-US統合_20260726.md §6 U-1
#
# 【公開 API】
#   user_agent(cfg=None) -> str
#   get(url, *, cfg=None, accept="application/json",
#       timeout=None, max_retries=3) -> requests.Response | None
#   get_json(url, *, cfg=None, **kw) -> dict | list | None
#   cik_map(cfg=None, ttl_days=None) -> dict[str, str]
#   get_cik(ticker, cfg=None) -> str | None
#   submissions(cik, cfg=None) -> dict | None
#
# 【意味解釈は責務外】
#   Form 4 / 8-K / 13F の中身のパース・解釈は行わない（各モジュールに残す）。
#   本ファイルが持つキャッシュは CIK マップのみ（既存の各モジュール個別
#   キャッシュ = insider_form4_cache.json 等は移動も参照もしない）。
#
# 【依存】requests（requirements.txt 既存）/ config_sync.py
# ==============================================================

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

import requests

from config_sync import sync_module_constants

# ==============================================================
# BLOCK 0: 定数・エンドポイント
# ==============================================================

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik}.json"

CIK_MAP_CACHE_FILE = "cik_map.json"

_SEC_UA_FALLBACK = "StockAnalyzer stockanalyzer@example.com"

# config.py BLOCK 60 との対応（config名: モジュール定数名）
_CFG_CONST_MAP = {
    "SEC_RATE_LIMIT_RPS":   "RATE_LIMIT_RPS",
    "SEC_HTTP_TIMEOUT":     "HTTP_TIMEOUT",
    "SEC_CIK_MAP_TTL_DAYS": "CIK_MAP_TTL_DAYS",
    "SEC_MAX_RETRIES":      "MAX_RETRIES",
}

# 既定値は既存8ファイルの実測から「最も保守的な値」を採用（config.py BLOCK 60 参照）
RATE_LIMIT_RPS:   float = 2.0   # 現行の FETCH_DELAY=0.5s 相当（最も保守的）
HTTP_TIMEOUT:     int   = 15
CIK_MAP_TTL_DAYS: int   = 7
MAX_RETRIES:      int   = 3

# レート制限（最終リクエスト時刻。トークンバケット容量1の簡易実装）
_last_request_ts: float = 0.0


# ==============================================================
# BLOCK 1: User-Agent 解決
# ==============================================================

def user_agent(cfg=None) -> str:
    """SEC EDGAR へ送出する User-Agent 文字列を解決する。

    優先順:
      1. 環境変数 SEC_USER_AGENT
      2. cfg.SEC_USER_AGENT（cfg 指定時）
      3. cfg が None のとき: import config して config.SEC_USER_AGENT
      4. 固定フォールバック文字列

    実装は us_8k_monitor.py:116-124 の _sec_user_agent() と同型
    （us_insider_form4_scraper.py / institutional_13f_monitor.py の
    cfg 引数版と同一パターン）。
    """
    ua = os.getenv("SEC_USER_AGENT", "")
    if ua:
        return ua
    if cfg is not None:
        return getattr(cfg, "SEC_USER_AGENT", "") or _SEC_UA_FALLBACK
    try:
        import config as _cfg
        return getattr(_cfg, "SEC_USER_AGENT", "") or _SEC_UA_FALLBACK
    except Exception:
        return _SEC_UA_FALLBACK


# ==============================================================
# BLOCK 2: レート制限
# ==============================================================

def _throttle() -> None:
    """10 req/s を超えないよう最低間隔を空ける（容量1のトークンバケット）。

    RATE_LIMIT_RPS から算出した最小間隔に満たない場合のみ time.sleep する。
    """
    global _last_request_ts
    min_interval = (1.0 / RATE_LIMIT_RPS) if RATE_LIMIT_RPS > 0 else 0.0
    now = time.monotonic()
    elapsed = now - _last_request_ts
    if min_interval > 0 and elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_request_ts = time.monotonic()


# ==============================================================
# BLOCK 3: HTTP 取得
# ==============================================================

def get(
    url: str,
    *,
    cfg=None,
    accept: str = "application/json",
    timeout: Optional[int] = None,
    max_retries: int = 3,
) -> Optional["requests.Response"]:
    """SEC EDGAR へ GET リクエストを送る単一窓口。

    - レート制限: RATE_LIMIT_RPS を超えない間隔を空ける。
    - 429 / 5xx: 指数バックオフでリトライ（3s, 6s, 12s。
      us_stock_screener.py の _fetch_one と同じ待ち時間の考え方）。
    - 403: リトライせず即 None を返し、警告を print する
      （SEC の 403 は User-Agent 拒否であり、再試行しても成功しない）。
    - その他の例外: max_retries まで再試行（us_stock_screener.py の
      _fetch_one と同じ固定2秒待ち）し、最終的に None。
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)

    to = timeout if timeout is not None else HTTP_TIMEOUT
    headers = {"User-Agent": user_agent(cfg), "Accept": accept}

    for attempt in range(max_retries):
        _throttle()
        try:
            resp = requests.get(url, headers=headers, timeout=to)
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None

        if resp.status_code == 403:
            print(f"  ⚠️  sec_client: 403 Forbidden ({url}) — "
                  f"SECがUser-Agentを拒否した可能性。リトライしません。")
            return None

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < max_retries - 1:
                wait = (2 ** attempt) * 3   # 3s, 6s, 12s
                time.sleep(wait)
                continue
            return None

        return resp

    return None


def get_json(url: str, *, cfg=None, **kw) -> Union[dict, list, None]:
    """get() でレスポンスを取得し JSON デコードして返す。失敗時は None。"""
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    resp = get(url, cfg=cfg, **kw)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return None


# ==============================================================
# BLOCK 4: CIK マップ（company_tickers.json の共通キャッシュ）
# ==============================================================

def _cache_dir(cfg=None) -> Path:
    """sec_client 専用キャッシュディレクトリ（cfg.CACHE_DIR / "sec"）。

    cfg が None、または CACHE_DIR を持たない場合は
    `01_System/stock_data/cache/sec` にフォールバックする。
    """
    if cfg is not None:
        base = getattr(cfg, "CACHE_DIR", None)
        if base:
            return Path(base) / "sec"
    return Path(__file__).resolve().parent / "stock_data" / "cache" / "sec"


def _cik_cache_path(cfg=None) -> Path:
    return _cache_dir(cfg) / CIK_MAP_CACHE_FILE


def _read_cik_cache_file(path: Path) -> Optional[dict]:
    """キャッシュファイルの生内容（{"cached_at": ..., "map": {...}}）を返す。

    存在しない・壊れている場合は None。
    """
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "map" not in raw:
            return None
        return raw
    except Exception:
        return None


def _write_cik_cache_file(path: Path, mapping: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cached_at": datetime.now().isoformat(), "map": mapping}
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def cik_map(cfg=None, ttl_days: Optional[int] = None) -> dict:
    """https://www.sec.gov/files/company_tickers.json を1箇所でキャッシュする。

    Returns:
        {ticker(大文字): "0000320193"}（ゼロ詰め10桁の文字列）
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    ttl = ttl_days if ttl_days is not None else CIK_MAP_TTL_DAYS
    path = _cik_cache_path(cfg)

    cached = _read_cik_cache_file(path)
    if cached is not None:
        try:
            cached_at = datetime.fromisoformat(cached.get("cached_at", ""))
            if datetime.now() - cached_at < timedelta(days=ttl):
                return cached.get("map", {})
        except Exception:
            pass  # cached_at が壊れている → 再取得へフォールスルー

    data = get_json(COMPANY_TICKERS_URL, cfg=cfg, accept="application/json")
    if not data or not isinstance(data, dict):
        # 取得失敗: 期限切れでも既存キャッシュがあれば無いよりましなので返す
        if cached is not None:
            return cached.get("map", {})
        return {}

    mapping: dict = {}
    for item in data.values():
        if isinstance(item, dict):
            ticker = str(item.get("ticker", "")).upper()
            cik = str(item.get("cik_str", "")).zfill(10)
            if ticker and cik:
                mapping[ticker] = cik

    _write_cik_cache_file(path, mapping)
    return mapping


def get_cik(ticker: str, cfg=None) -> Optional[str]:
    """ティッカーに対応する CIK（ゼロ詰め10桁の文字列）を返す。

    呼び出し側の大文字小文字の揺れを吸収する。
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not ticker:
        return None
    mapping = cik_map(cfg=cfg)
    return mapping.get(str(ticker).strip().upper())


# ==============================================================
# BLOCK 5: 提出書類一覧
# ==============================================================

def submissions(cik: "Union[str, int]", cfg=None) -> Optional[dict]:
    """data.sec.gov/submissions/CIK##########.json を取得する。

    cik は int / 未パディング文字列 / 10桁ゼロ詰め文字列のいずれでも良い。
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    try:
        cik_int = int(str(cik).strip())
    except (TypeError, ValueError):
        return None
    cik_str = f"{cik_int:010d}"
    url = SUBMISSIONS_URL_TMPL.format(cik=cik_str)
    return get_json(url, cfg=cfg)
