"""Small, fail-closed REST client shared by paid database integrations."""
from __future__ import annotations

import os
import time
from typing import Any

import requests


class DBRestClient:
    """EdinetDB/FudosanDB client with bounded retry and a per-run call budget."""

    def __init__(self, base_url: str, api_key: str = "", daily_budget: int = 80,
                 timeout: int = 30, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.daily_budget = max(0, int(daily_budget))
        self.timeout = timeout
        self.session = session or requests.Session()
        self.calls = 0

    @classmethod
    def edinetdb(cls, cfg):
        return cls(getattr(cfg, "EDINETDB_BASE_URL", "https://edinetdb.jp/v1"),
                   getattr(cfg, "EDINETDB_API_KEY", ""),
                   getattr(cfg, "EDINET_DAILY_BUDGET", 80))

    @classmethod
    def fudosandb(cls, cfg):
        return cls(getattr(cfg, "FUDOSANDB_BASE_URL", "https://fudosandb.jp/v1"),
                   getattr(cfg, "FUDOSAN_API_KEY", ""),
                   getattr(cfg, "FUDOSANDB_DAILY_BUDGET", 90))

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("API key is not configured")
        if self.calls >= self.daily_budget:
            raise RuntimeError("daily API budget exhausted")
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        for attempt in range(3):
            self.calls += 1
            response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            if response.status_code in (401, 403):
                response.raise_for_status()
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
            response.raise_for_status()
            payload = response.json()
            return payload.get("data", payload)
        raise RuntimeError("unreachable retry state")

    def usage(self) -> dict[str, Any]:
        return self.get("/usage")
