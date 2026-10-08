"""EdinetDB / FudosanDB 用の小さな共通 REST クライアント。

API の呼出数はローカル台帳と、提供される場合は ``/usage`` の実残量で抑制する。
取得データのキャッシュは各 ingestor の責務とし、このクラスは API 呼出台帳だけを
永続化する。
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests


MAX_RETRIES = 2
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B5_DB_REST_CLIENT_RETRYABLE_STATUS_CODES': 'RETRYABLE_STATUS_CODES',
}

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def budget_remaining(
    calls_used: int,
    daily_budget: int,
    remote_daily_remaining: int | None = None,
) -> int:
    """ローカル上限と API 実残量の小さい方を、残り呼出数として返す。"""
    local_remaining = max(0, int(daily_budget) - max(0, int(calls_used)))
    if remote_daily_remaining is None:
        return local_remaining
    return max(0, min(local_remaining, int(remote_daily_remaining)))


class DBRestClient:
    """同一ベンダーの ``{data, meta}`` REST API を扱う薄いクライアント。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        cache_dir: str | Path = "stock_data/data/db_rest_cache",
        daily_budget: int = 80,
        source_name: str = "database",
        *,
        timeout: int = 20,
        session: requests.Session | None = None,
        usage_endpoint: str | None = "/usage",
        min_interval: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.cache_dir = Path(cache_dir)
        self.daily_budget = max(0, int(daily_budget))
        self.source_name = source_name
        self.timeout = max(1, int(timeout))
        self.session = session
        self.usage_endpoint = usage_endpoint
        self._auth_failed = False
        # 連続GETが速すぎると 429 を誘発し、その再試行が日次予算を食い潰す。
        # 送信間隔を最低 min_interval 秒あけて 429 自体を防ぐ。
        self.min_interval = max(0.0, float(min_interval))
        self._last_request_at = 0.0
        self._rate_limited = False   # 429が続いたら当日は打ち切る
        # ログ抑制カウンタと直近の失敗種別。
        # last_error は呼び出し側が「404=恒久的な不在」と
        # 「429/予算枯渇=一時障害」を区別するために読む。
        self._log_counts: dict[str, int] = {}
        self.last_error: str | None = None

    @classmethod
    def edinetdb(cls, cfg):
        sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
        data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
        return cls(
            getattr(cfg, "EDINETDB_BASE_URL", "https://edinetdb.jp/v1"),
            getattr(cfg, "EDINETDB_API_KEY", ""),
            data_dir / "edinetdb_cache",
            getattr(cfg, "EDINETDB_DAILY_BUDGET", getattr(cfg, "EDINET_DAILY_BUDGET", 80)),
            "edinetdb",
            min_interval=float(getattr(cfg, "EDINETDB_MIN_INTERVAL_SEC", 0.5)),
        )

    @classmethod
    def fudosandb(cls, cfg):
        data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
        return cls(
            getattr(cfg, "FUDOSAN_BASE_URL", getattr(cfg, "FUDOSANDB_BASE_URL", "https://fudosandb.jp/v1")),
            getattr(cfg, "FUDOSAN_API_KEY", ""),
            data_dir / "fudosandb_cache",
            getattr(cfg, "FUDOSAN_DAILY_BUDGET", getattr(cfg, "FUDOSANDB_DAILY_BUDGET", 90)),
            "fudosandb",
            usage_endpoint=None,
            min_interval=float(getattr(cfg, "FUDOSAN_MIN_INTERVAL_SEC", 0.5)),
        )

    @property
    def calls(self) -> int:
        """当日の成功API呼出数。旧クライアントとの互換プロパティ。"""
        return int(self._load_ledger().get("calls_used", 0))

    @property
    def _today(self) -> str:
        return date.today().isoformat()

    @property
    def _ledger_path(self) -> Path:
        return self.cache_dir / f"daily_{self._today}.json"

    def _load_ledger(self) -> dict[str, Any]:
        try:
            ledger = json.loads(self._ledger_path.read_text(encoding="utf-8"))
            if ledger.get("date") == self._today:
                return ledger
        except (OSError, ValueError, TypeError):
            pass
        return {
            "date": self._today,
            "calls_used": 0,
            "remote_daily_remaining": None,
        }

    def _save_ledger(self, ledger: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._ledger_path.write_text(
                json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(f"⚠️ {self.source_name} 日次台帳の保存失敗: {exc}")

    def _headers(self) -> dict[str, str]:
        # 両ベンダーの公式案内は Authorization: Bearer だが X-API-Key でも通る。
        # どちらか一方しか見ないサーバーでも認証が成立するよう両方送る。
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-API-Key": self.api_key,
            "User-Agent": "StockAnalyzer/1.0",
            "Accept": "application/json",
        }

    def _wait_for_slot(self) -> None:
        """直前の送信から min_interval 秒あけて 429 を予防する。"""
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if 0 <= elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _record_call(self) -> None:
        """成功可否にかかわらず、実際のHTTP試行を保守的に1回記録する。"""
        ledger = self._load_ledger()
        ledger["calls_used"] = int(ledger.get("calls_used", 0)) + 1
        remaining = ledger.get("remote_daily_remaining")
        if remaining is not None:
            ledger["remote_daily_remaining"] = max(0, int(remaining) - 1)
        self._save_ledger(ledger)

    def _log_once(self, key: str, message: str) -> None:
        """同一種別のログを1実行につき1回だけ出す。

        「日次予算到達」「GET再試行上限」を呼び出しのたびに出すと
        本番ログが同じ行で埋まり他の異常が見えなくなるため。
        件数は最後に集計して出す（_log_summary）。
        """
        self._log_counts[key] = self._log_counts.get(key, 0) + 1
        if self._log_counts[key] == 1:
            print(message)

    def log_summary(self) -> dict:
        """抑制したログの件数を1行で報告し、カウンタを返す。"""
        counts = dict(self._log_counts)
        extra = {k: v for k, v in counts.items() if v > 1}
        if extra:
            detail = " / ".join(f"{k}×{v}" for k, v in sorted(extra.items()))
            print(f"  ℹ️ {self.source_name} 抑制したログ: {detail}")
        return counts

    @property
    def budget_exhausted(self) -> bool:
        """日次予算を使い切っているか（呼び出し側の早期打ち切り用）。"""
        ledger = self._load_ledger()
        return budget_remaining(
            ledger.get("calls_used", 0), self.daily_budget,
            ledger.get("remote_daily_remaining"),
        ) <= 0

    def _request(self, path: str, params: dict | None = None) -> dict | None:
        """429/5xx だけを最大2回再試行して JSON 封筒を返す。

        失敗の種別は self.last_error に残す（呼び出し側が
        「恒久的な不在(404)」と「一時障害・予算枯渇」を区別するため）。
        """
        self.last_error = None
        if not self.api_key:
            self._log_once("no_api_key", f"⏭️ {self.source_name}: APIキー未設定")
            self.last_error = "no_api_key"
            return None
        if self._rate_limited:
            self.last_error = "rate_limited"
            return None

        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(MAX_RETRIES + 1):
            ledger = self._load_ledger()
            if budget_remaining(
                ledger.get("calls_used", 0), self.daily_budget,
                ledger.get("remote_daily_remaining"),
            ) <= 0:
                self._log_once(
                    "budget",
                    f"⏹️ {self.source_name} 日次予算{self.daily_budget}到達")
                self.last_error = "budget"
                return None
            # 通信例外でもサーバー到達の有無を確定できないため、送信前に
            # 1試行として記録し、日次上限を越える再試行を防ぐ。
            self._record_call()
            self._wait_for_slot()
            try:
                getter = self.session.get if self.session is not None else requests.get
                response = getter(
                    url, headers=self._headers(), params=params, timeout=self.timeout
                )
            except requests.RequestException as exc:
                self._last_request_at = time.monotonic()
                self._log_once(
                    "net",
                    f"⚠️ {self.source_name} GET失敗 ({path}): {type(exc).__name__}")
                self.last_error = "network"
                return None
            self._last_request_at = time.monotonic()

            if response.status_code == 429:
                # 429は「速すぎる」の合図。間隔を広げて数回だけ待ち、
                # それでも続くなら当日は打ち切る。再試行を重ねるほど
                # 日次予算を食い潰し、他モジュールの取得まで巻き添えになる。
                self.min_interval = min(8.0, max(1.0, self.min_interval * 2))
                if attempt == MAX_RETRIES:
                    self._rate_limited = True
                    self._log_once(
                        "rate_limited",
                        f"⏹️ {self.source_name}: HTTP 429が続くため"
                        f"当日の取得を打ち切り ({path})")
                    self.last_error = "rate_limited"
                    return None
                time.sleep(self.min_interval)
                continue

            if response.status_code in (401, 403):
                self._auth_failed = True
                self._log_once(
                    "auth",
                    f"⚠️ {self.source_name}: APIキー認証失敗 "
                    f"(HTTP {response.status_code})")
                self.last_error = "auth"
                return None
            if response.status_code == 404:
                # 恒久的な不在。再試行しても永遠に取れないので、
                # 呼び出し側が「取得済み（データなし）」として扱えるようにする。
                self._log_once(
                    "not_found",
                    f"ℹ️ {self.source_name} データなし ({path}): HTTP 404")
                self.last_error = "not_found"
                return None
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    self._log_once(
                        "json",
                        f"⚠️ {self.source_name} GET失敗 ({path}): JSONでない応答")
                    self.last_error = "bad_json"
                    return None
                return payload if isinstance(payload, dict) else None
            if response.status_code not in RETRYABLE_STATUS_CODES:
                self._log_once(
                    f"http_{response.status_code}",
                    f"⚠️ {self.source_name} GET失敗 ({path}): "
                    f"HTTP {response.status_code}")
                self.last_error = f"http_{response.status_code}"
                return None
            if attempt == MAX_RETRIES:
                self._log_once(
                    "retry_exhausted",
                    f"⚠️ {self.source_name} GET再試行上限 ({path}): "
                    f"HTTP {response.status_code}")
                self.last_error = "retry_exhausted"
                return None
            time.sleep(2 ** attempt)
        return None

    @staticmethod
    def _usage_data(payload: dict | None) -> dict | None:
        if not payload:
            return None
        data = payload.get("data", payload)
        return data if isinstance(data, dict) else None

    @staticmethod
    def _daily_remaining(usage: dict | None) -> int | None:
        if not usage:
            return None
        value = usage.get("daily_remaining")
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    def usage(self) -> dict | None:
        """``/usage`` の data を返し、成功時は実残量を日次台帳に記録する。"""
        if not self.usage_endpoint:
            return None
        ledger = self._load_ledger()
        if budget_remaining(ledger.get("calls_used", 0), self.daily_budget) <= 0:
            return None
        usage = self._usage_data(self._request(self.usage_endpoint))
        if usage is None:
            return None
        remaining = self._daily_remaining(usage)
        ledger = self._load_ledger()
        if remaining is not None:
            ledger["remote_daily_remaining"] = remaining
        self._save_ledger(ledger)
        return usage

    def get(self, path: str, params: dict | None = None) -> dict | list | None:
        """予算を確認して GET し、成功時に封筒内の ``data`` を返す。"""
        ledger = self._load_ledger()
        remote_remaining = ledger.get("remote_daily_remaining")
        if remote_remaining is None and self.usage_endpoint:
            usage = self.usage()
            if self._auth_failed:
                return None
            remote_remaining = self._daily_remaining(usage)
            ledger = self._load_ledger()

        if budget_remaining(
            ledger.get("calls_used", 0), self.daily_budget, remote_remaining
        ) <= 0:
            # 生printだと予算切れ後の呼び出しごとに同じ行が出て
            # 他の異常が埋もれるため、_log_once で1回だけ出す。
            self._log_once(
                "budget",
                f"⏹️ {self.source_name} 日次予算{self.daily_budget}到達")
            self.last_error = "budget"
            return None

        payload = self._request(path, params)
        if payload is None:
            return None
        if "data" not in payload:
            print(f"⚠️ {self.source_name} GET失敗 ({path}): data封筒なし")
            return None

        return payload["data"]
