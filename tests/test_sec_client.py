# ==============================================================
# tests/test_sec_client.py  (Wave J1 / U-1a)
# sec_client.py 単体テスト
# ==============================================================
# 【対象】sec_client.py（SEC EDGAR 共通アクセス層の単体窓口）
# 【方針】ネットワーク接続はすべてモック（requests.get / time.sleep）で
#   代替する。実ネットワークへ出る経路が無いことを目視でも確認できるよう、
#   すべてのテストで `sec_client.requests.get` を monkeypatch する。
# ==============================================================
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import sec_client  # noqa: E402


# ==============================================================
# 共通フィクスチャ
# ==============================================================

class _FakeResponse:
    """requests.Response の最小モック。"""

    def __init__(self, status_code: int, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


@pytest.fixture(autouse=True)
def _reset_throttle_state(monkeypatch):
    """レート制限の最終リクエスト時刻をテストごとにリセットする。

    time.monotonic() は常に増加し続けるため、直前のテストの値を
    引きずると意図しない sleep 分岐に入る。既定では 0.0（遠い過去）に
    リセットし、throttle が発火しない状態から各テストを始める。
    """
    monkeypatch.setattr(sec_client, "_last_request_ts", 0.0)
    yield


@pytest.fixture
def no_sleep(monkeypatch):
    """time.sleep をモックして待ち時間を記録する。"""
    calls = []

    def _fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(sec_client.time, "sleep", _fake_sleep)
    return calls


@pytest.fixture
def tmp_cache_cfg(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    return SimpleNamespace(CACHE_DIR=cache_dir)


# ==============================================================
# 1. user_agent() の優先順位3分岐
# ==============================================================

def test_user_agent_env_var_takes_priority(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "env-ua")
    cfg = SimpleNamespace(SEC_USER_AGENT="cfg-ua-should-be-ignored")
    assert sec_client.user_agent(cfg) == "env-ua"
    assert sec_client.user_agent(None) == "env-ua"


def test_user_agent_uses_cfg_when_no_env_var(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    cfg = SimpleNamespace(SEC_USER_AGENT="cfg-ua")
    assert sec_client.user_agent(cfg) == "cfg-ua"


def test_user_agent_fallback_when_cfg_none_and_no_env_var(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    import config
    monkeypatch.setattr(config, "SEC_USER_AGENT", "", raising=False)
    assert sec_client.user_agent(None) == "StockAnalyzer stockanalyzer@example.com"


def test_user_agent_fallback_when_cfg_has_no_attr(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    cfg = SimpleNamespace()  # SEC_USER_AGENT属性なし
    assert sec_client.user_agent(cfg) == "StockAnalyzer stockanalyzer@example.com"


# ==============================================================
# 2. get() のステータスコード別挙動
# ==============================================================

def test_get_403_returns_none_without_retry(monkeypatch, no_sleep, capsys):
    calls = []

    def _fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(403)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    result = sec_client.get("https://data.sec.gov/x.json", max_retries=3)

    assert result is None
    assert len(calls) == 1   # リトライしない
    out = capsys.readouterr().out
    assert "403" in out


def test_get_429_retries_then_succeeds(monkeypatch, no_sleep):
    responses = [_FakeResponse(429), _FakeResponse(200, {"ok": True})]

    def _fake_get(url, headers=None, timeout=None):
        return responses.pop(0)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    result = sec_client.get("https://data.sec.gov/x.json", max_retries=3)

    assert result is not None
    assert result.status_code == 200
    # 429 リトライの指数バックオフ待ち(3s)が呼ばれている
    assert 3 in no_sleep


def test_get_exhausts_max_retries_returns_none(monkeypatch, no_sleep):
    call_count = {"n": 0}

    def _fake_get(url, headers=None, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(429)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    result = sec_client.get("https://data.sec.gov/x.json", max_retries=3)

    assert result is None
    assert call_count["n"] == 3


def test_get_5xx_retries_like_429(monkeypatch, no_sleep):
    responses = [_FakeResponse(503), _FakeResponse(200, {"ok": True})]

    def _fake_get(url, headers=None, timeout=None):
        return responses.pop(0)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    result = sec_client.get("https://data.sec.gov/x.json", max_retries=3)

    assert result is not None
    assert result.status_code == 200


def test_get_exception_retries_then_succeeds(monkeypatch, no_sleep):
    responses = [RuntimeError("boom"), _FakeResponse(200, {"ok": True})]

    def _fake_get(url, headers=None, timeout=None):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    result = sec_client.get("https://data.sec.gov/x.json", max_retries=3)

    assert result is not None
    assert result.status_code == 200
    assert 2 in no_sleep   # その他例外は固定2秒待ちでリトライ


def test_get_success_no_retry_needed(monkeypatch, no_sleep):
    def _fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    result = sec_client.get("https://data.sec.gov/x.json")

    assert result is not None
    assert result.status_code == 200


def test_get_sends_user_agent_header(monkeypatch, no_sleep):
    captured = {}

    def _fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    monkeypatch.setenv("SEC_USER_AGENT", "test-ua-xyz")
    sec_client.get("https://data.sec.gov/x.json", timeout=7)

    assert captured["headers"]["User-Agent"] == "test-ua-xyz"
    assert captured["timeout"] == 7


# ==============================================================
# 3. get_json()
# ==============================================================

def test_get_json_returns_decoded_body(monkeypatch, no_sleep):
    def _fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, {"hello": "world"})

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    result = sec_client.get_json("https://data.sec.gov/x.json")

    assert result == {"hello": "world"}


def test_get_json_returns_none_when_get_fails(monkeypatch, no_sleep):
    def _fake_get(url, headers=None, timeout=None):
        return _FakeResponse(403)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    result = sec_client.get_json("https://data.sec.gov/x.json")

    assert result is None


# ==============================================================
# 4. cik_map() / get_cik()
# ==============================================================

_SAMPLE_TICKERS_JSON = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}


def test_cik_map_returns_zero_padded_10_digit_strings(monkeypatch, no_sleep, tmp_cache_cfg):
    def _fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, _SAMPLE_TICKERS_JSON)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    mapping = sec_client.cik_map(cfg=tmp_cache_cfg)

    assert mapping["AAPL"] == "0000320193"
    assert mapping["MSFT"] == "0000789019"
    assert len(mapping["AAPL"]) == 10


def test_get_cik_case_insensitive(monkeypatch, no_sleep, tmp_cache_cfg):
    def _fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, _SAMPLE_TICKERS_JSON)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)

    assert sec_client.get_cik("aapl", cfg=tmp_cache_cfg) == "0000320193"
    assert sec_client.get_cik("AAPL", cfg=tmp_cache_cfg) == "0000320193"
    assert sec_client.get_cik("AaPl", cfg=tmp_cache_cfg) == "0000320193"


def test_get_cik_unknown_ticker_returns_none(monkeypatch, no_sleep, tmp_cache_cfg):
    def _fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, _SAMPLE_TICKERS_JSON)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    assert sec_client.get_cik("NOSUCHTICKER", cfg=tmp_cache_cfg) is None


def test_cik_map_within_ttl_reads_cache_without_refetch(monkeypatch, no_sleep, tmp_cache_cfg):
    from datetime import datetime

    cache_path = tmp_cache_cfg.CACHE_DIR / "sec" / sec_client.CIK_MAP_CACHE_FILE
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            "cached_at": datetime.now().isoformat(),
            "map": {"AAPL": "0000320193"},
        }),
        encoding="utf-8",
    )

    called = {"n": 0}

    def _fake_get(url, headers=None, timeout=None):
        called["n"] += 1
        return _FakeResponse(200, _SAMPLE_TICKERS_JSON)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    mapping = sec_client.cik_map(cfg=tmp_cache_cfg, ttl_days=7)

    assert mapping == {"AAPL": "0000320193"}
    assert called["n"] == 0   # 再取得していない


def test_cik_map_expired_ttl_refetches(monkeypatch, no_sleep, tmp_cache_cfg):
    from datetime import datetime, timedelta

    cache_path = tmp_cache_cfg.CACHE_DIR / "sec" / sec_client.CIK_MAP_CACHE_FILE
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    stale_time = (datetime.now() - timedelta(days=30)).isoformat()
    cache_path.write_text(
        json.dumps({"cached_at": stale_time, "map": {"OLD": "0000000001"}}),
        encoding="utf-8",
    )

    called = {"n": 0}

    def _fake_get(url, headers=None, timeout=None):
        called["n"] += 1
        return _FakeResponse(200, _SAMPLE_TICKERS_JSON)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    mapping = sec_client.cik_map(cfg=tmp_cache_cfg, ttl_days=7)

    assert called["n"] == 1   # 期限切れなので再取得した
    assert mapping["AAPL"] == "0000320193"
    assert "OLD" not in mapping


def test_cik_map_falls_back_to_default_cache_dir_without_cfg(monkeypatch, no_sleep):
    """cfg=None のときフォールバックパスを使うこと（実書き込みはしない）。"""
    def _fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, _SAMPLE_TICKERS_JSON)

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    # 実ディスクへ触れないよう書き込みだけモックする
    monkeypatch.setattr(sec_client, "_write_cik_cache_file", lambda *a, **kw: None)
    monkeypatch.setattr(sec_client, "_read_cik_cache_file", lambda *a, **kw: None)

    mapping = sec_client.cik_map(cfg=None)
    assert mapping["AAPL"] == "0000320193"


# ==============================================================
# 5. submissions()
# ==============================================================

def test_submissions_builds_correct_zero_padded_url(monkeypatch, no_sleep):
    captured = {}

    def _fake_get_json(url, *, cfg=None, **kw):
        captured["url"] = url
        return {"filings": []}

    monkeypatch.setattr(sec_client, "get_json", _fake_get_json)
    sec_client.submissions("320193")

    assert captured["url"] == "https://data.sec.gov/submissions/CIK0000320193.json"


def test_submissions_accepts_int_cik(monkeypatch, no_sleep):
    captured = {}

    def _fake_get_json(url, *, cfg=None, **kw):
        captured["url"] = url
        return {"filings": []}

    monkeypatch.setattr(sec_client, "get_json", _fake_get_json)
    sec_client.submissions(320193)

    assert captured["url"] == "https://data.sec.gov/submissions/CIK0000320193.json"


def test_submissions_accepts_already_padded_cik(monkeypatch, no_sleep):
    captured = {}

    def _fake_get_json(url, *, cfg=None, **kw):
        captured["url"] = url
        return {"filings": []}

    monkeypatch.setattr(sec_client, "get_json", _fake_get_json)
    sec_client.submissions("0000320193")

    assert captured["url"] == "https://data.sec.gov/submissions/CIK0000320193.json"


def test_submissions_invalid_cik_returns_none(monkeypatch, no_sleep):
    def _fake_get_json(url, *, cfg=None, **kw):
        raise AssertionError("get_json should not be called for invalid cik")

    monkeypatch.setattr(sec_client, "get_json", _fake_get_json)
    assert sec_client.submissions("not-a-number") is None


# ==============================================================
# 6. レート制限（最低間隔を空けること）
# ==============================================================

def test_rate_limit_sleeps_between_consecutive_calls(monkeypatch, no_sleep):
    def _fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    # 直前のリクエストが「たった今」あったことにして、間隔ゼロで連続呼び出す
    monkeypatch.setattr(sec_client, "_last_request_ts", sec_client.time.monotonic())

    sec_client.get("https://data.sec.gov/x.json")

    # RATE_LIMIT_RPS=2.0 の既定なら最小間隔は0.5秒 → sleepが呼ばれる
    assert len(no_sleep) >= 1
    assert any(s > 0 for s in no_sleep)


def test_rate_limit_no_sleep_when_interval_already_elapsed(monkeypatch, no_sleep):
    def _fake_get(url, headers=None, timeout=None):
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(sec_client.requests, "get", _fake_get)
    # _reset_throttle_state フィクスチャにより _last_request_ts=0.0（遠い過去）
    sec_client.get("https://data.sec.gov/x.json")

    assert no_sleep == []   # 間隔は十分空いているのでsleepしない


# ==============================================================
# 7. ネットワーク到達防止の最終確認
# ==============================================================

def test_no_real_network_module_used_directly():
    """requests モジュールそのものは import されているが、
    各テストで monkeypatch 済みの sec_client.requests.get 経由のみを
    使うことを確認する（実ネットワークを叩く urlopen 等が無い）。"""
    import inspect
    source = inspect.getsource(sec_client)
    assert "urlopen" not in source
    assert "http.client" not in source
