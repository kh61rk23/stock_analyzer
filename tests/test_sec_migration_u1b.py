"""
U-1b（sec_client.py への移行・前半4ファイル）の回帰テスト。

対象: sec_summarizer.py / us_sector_bridge_enhanced.py /
      institutional_13f_monitor.py / sec_geographic_extractor.py

検証内容:
  1. ソース機械検査: `requests.get(` / `urllib.request` で sec.gov を
     直接叩く箇所が残っていないこと。
  2. ソース機械検査: `"User-Agent": "..."` の形のハードコードされた
     辞書リテラルが残っていないこと。
  3. 4ファイルすべてが `sec_client` を import していること。
  4. 主要な取得関数が sec_client.get / get_json / submissions 経由で
     動作すること（該当関数をモックして呼ばれることを検証）。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import sec_client
import sec_summarizer
import us_sector_bridge_enhanced
import institutional_13f_monitor
import sec_geographic_extractor

REPO = Path(__file__).resolve().parent.parent

TARGET_FILES = [
    "sec_summarizer.py",
    "us_sector_bridge_enhanced.py",
    "institutional_13f_monitor.py",
    "sec_geographic_extractor.py",
]

TARGET_MODULES = [
    sec_summarizer,
    us_sector_bridge_enhanced,
    institutional_13f_monitor,
    sec_geographic_extractor,
]

SEC_GOV_MARKERS = ("sec.gov",)

_HTTP_CALL_PATTERN = re.compile(
    r"requests\.(?:get|post|request)\(|urllib\.request\.urlopen\(|urlopen\("
)
_USER_AGENT_DICT_PATTERN = re.compile(r'"User-Agent"\s*:\s*"')


def _source(filename: str) -> str:
    return (REPO / filename).read_text(encoding="utf-8")


# ==============================================================
# 1. sec.gov への直叩き requests.get / urllib が残っていないこと
# ==============================================================

def test_no_direct_requests_to_sec_gov():
    """
    各ファイルの `requests.get(` / `urllib` 呼び出し箇所の前後を見て、
    sec.gov ドメインを直接叩いていないことを機械検査する。

    sec_summarizer.py は EDINETDB（api.edinetdb.com）への呼び出しが
    残るが、これは sec.gov 向けではないため対象外（R1-5 のスコープ外）。
    """
    for filename in TARGET_FILES:
        source = _source(filename)
        for m in _HTTP_CALL_PATTERN.finditer(source):
            window = source[max(0, m.start() - 400):m.end() + 400]
            assert not any(marker in window for marker in SEC_GOV_MARKERS), (
                f"{filename}: {m.group(0)!r} 付近に sec.gov への直接アクセスが"
                f"残っています: ...{window[:200]}..."
            )


def test_no_hardcoded_user_agent_dict_literal_u1b():
    """4ファイルに '\"User-Agent\": \"<文字列リテラル>\"' が残っていないこと。"""
    for filename in TARGET_FILES:
        source = _source(filename)
        assert not _USER_AGENT_DICT_PATTERN.search(source), (
            f"{filename} にハードコードされた User-Agent 辞書リテラルが残っています"
        )


def test_all_4_files_import_sec_client():
    for filename in TARGET_FILES:
        source = _source(filename)
        assert re.search(r"^import sec_client\s*$", source, re.MULTILINE), (
            f"{filename} が `import sec_client` していません"
        )


def test_all_4_modules_have_sec_client_attr():
    for mod in TARGET_MODULES:
        assert getattr(mod, "sec_client", None) is sec_client, (
            f"{mod.__name__}.sec_client が sec_client モジュールと一致しません"
        )


# ==============================================================
# 2. 主要な取得関数が sec_client 経由で動くこと（モック検証）
# ==============================================================

def test_sec_summarizer_get_cik_uses_sec_client(monkeypatch):
    called = {}

    def fake_get_cik(ticker, cfg=None):
        called["ticker"] = ticker
        called["cfg"] = cfg
        return "0000320193"

    monkeypatch.setattr(sec_client, "get_cik", fake_get_cik)
    cfg = SimpleNamespace(SEC_USER_AGENT="test-ua")

    result = sec_summarizer._get_cik_from_ticker("AAPL", cfg)

    assert result == "0000320193"
    assert called["ticker"] == "AAPL"
    assert called["cfg"] is cfg


def test_sec_summarizer_10k_accession_uses_sec_client_submissions(monkeypatch):
    called = {}

    def fake_submissions(cik, cfg=None):
        called["cik"] = cik
        called["cfg"] = cfg
        return {
            "filings": {
                "recent": {
                    "form": ["10-K", "8-K"],
                    "accessionNumber": ["0000320193-24-000001", "0000320193-24-000002"],
                }
            }
        }

    monkeypatch.setattr(sec_client, "submissions", fake_submissions)
    cfg = SimpleNamespace(SEC_USER_AGENT="test-ua")

    result = sec_summarizer._get_latest_10k_accession("0000320193", cfg)

    assert result == "000032019324000001"
    assert called["cik"] == "0000320193"
    assert called["cfg"] is cfg


def test_us_sector_bridge_enhanced_uses_sec_client_submissions(monkeypatch):
    called = {}

    def fake_submissions(cik, cfg=None):
        called["cik"] = cik
        called["cfg"] = cfg
        return {"sic": 3674, "sicDescription": "Semiconductors", "name": "NVIDIA CORP"}

    monkeypatch.setattr(sec_client, "submissions", fake_submissions)
    cfg = SimpleNamespace(SEC_USER_AGENT="test-ua")

    result = us_sector_bridge_enhanced._fetch_sic_from_submissions(1045810, cfg)

    assert result == {"sic": 3674, "sic_desc": "Semiconductors", "name": "NVIDIA CORP"}
    assert called["cik"] == 1045810
    assert called["cfg"] is cfg


def test_institutional_13f_fetch_cik_filings_uses_sec_client_submissions(monkeypatch):
    called = {}

    def fake_submissions(cik, cfg=None):
        called["cik"] = cik
        called["cfg"] = cfg
        return {"filings": {"recent": {"form": [], "accessionNumber": []}}}

    monkeypatch.setattr(sec_client, "submissions", fake_submissions)
    cfg = SimpleNamespace(SEC_USER_AGENT="test-ua")

    result = institutional_13f_monitor._fetch_cik_filings("0001067983", cfg)

    assert result == {"filings": {"recent": {"form": [], "accessionNumber": []}}}
    assert called["cik"] == "0001067983"
    assert called["cfg"] is cfg


def test_institutional_13f_fetch_holdings_uses_sec_client_get_json(monkeypatch):
    """2026-10-05: 旧仕様は存在しない infotable.json を読んでいた（全機関404）。
    提出書類の索引（index.json）を sec_client.get_json で読み、保有明細XMLを sec_client.get で取る。"""
    called = {}

    def fake_get_json(url, cfg=None, **kw):
        called["url"] = url
        called["cfg"] = cfg
        return {"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "53405.xml"}]}}

    class _Resp:
        status_code = 200
        text = ('<informationTable><infoTable><nameOfIssuer>BERKSHIRE HATHAWAY</nameOfIssuer>'
                '<cusip>123456789</cusip><value>1000</value><shrsOrPrnAmt><sshPrnamt>500</sshPrnamt>'
                '</shrsOrPrnAmt></infoTable></informationTable>')

    def fake_get(url, cfg=None, **kw):
        called["xml_url"] = url
        called["kw"] = kw
        return _Resp()

    monkeypatch.setattr(sec_client, "get_json", fake_get_json)
    monkeypatch.setattr(sec_client, "get", fake_get)
    monkeypatch.setattr(institutional_13f_monitor, "_resolve_tickers", lambda h, cfg=None: None)
    cfg = SimpleNamespace(SEC_USER_AGENT="test-ua")

    result = institutional_13f_monitor._fetch_13f_holdings(
        "0001067983-24-000001", "0001067983", cfg
    )

    assert len(result) == 1
    assert result[0]["cusip"] == "123456789" and result[0]["value_usd"] == 1000
    assert called["url"].endswith("/index.json") and "sec.gov" in called["url"]
    assert called["xml_url"].endswith("/53405.xml")
    assert called["cfg"] is cfg
    assert called["kw"].get("timeout") == 20


def test_sec_geographic_extractor_fetch_sec_json_uses_sec_client_get_json(monkeypatch):
    called = {}

    def fake_get_json(url, cfg=None, **kw):
        called["url"] = url
        called["cfg"] = cfg
        called["kw"] = kw
        return {"ok": True}

    monkeypatch.setattr(sec_client, "get_json", fake_get_json)
    cfg = SimpleNamespace(SEC_USER_AGENT="test-ua")

    result = sec_geographic_extractor._fetch_sec(
        "https://data.sec.gov/submissions/CIK0001045810.json", cfg=cfg, as_json=True
    )

    assert result == {"ok": True}
    assert called["cfg"] is cfg
    assert called["kw"].get("timeout") == 30  # XBRL等の特大レスポンス対応で維持


def test_sec_geographic_extractor_fetch_sec_text_uses_sec_client_get(monkeypatch):
    called = {}
    fake_resp = MagicMock()
    fake_resp.content = "hello world".encode("utf-8")

    def fake_get(url, cfg=None, **kw):
        called["url"] = url
        called["cfg"] = cfg
        called["kw"] = kw
        return fake_resp

    monkeypatch.setattr(sec_client, "get", fake_get)
    cfg = SimpleNamespace(SEC_USER_AGENT="test-ua")

    result = sec_geographic_extractor._fetch_sec(
        "https://www.sec.gov/Archives/edgar/data/1045810/x.htm", cfg=cfg, as_json=False
    )

    assert result == "hello world"
    assert called["cfg"] is cfg
    assert called["kw"].get("timeout") == 30


def test_sec_geographic_extractor_fetch_sec_get_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(sec_client, "get", lambda url, cfg=None, **kw: None)

    result = sec_geographic_extractor._fetch_sec(
        "https://www.sec.gov/Archives/edgar/data/1045810/x.htm", as_json=False
    )

    assert result is None
