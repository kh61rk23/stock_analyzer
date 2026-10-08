"""
U-1c（sec_client.py への移行・後半3ファイル）の回帰テスト。

対象: us_8k_monitor.py / us_insider_form4_scraper.py / us_fundamental_rotator.py
参照: sec_client.py（U-1a 新設）/ tests/test_sec_user_agent.py（U-2 の同型テスト）

検証内容:
  1. 3ファイルのソースに sec.gov を直接叩く requests.get( / urllib.request が
     残っていないこと（機械検査）。
  2. 3ファイルのソースに "User-Agent": "<リテラル>" のハードコード辞書が
     残っていないこと（機械検査）。
  3. 3ファイルが sec_client を import していること。
  4. 主要な取得関数が sec_client.get / get_json / cik_map / submissions 経由で
     動作すること（モックして呼び出しを検証）。
  5. us_insider_form4_scraper._parse_form4_xml の Form 4 XML パース結果が
     移行前後で変わらないこと（固定 XML を食わせた特性化テスト。期待値は
     実際に実行して得た実測値）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import us_8k_monitor
import us_fundamental_rotator
import us_insider_form4_scraper

REPO = Path(__file__).resolve().parent.parent

TARGET_FILES = [
    "us_8k_monitor.py",
    "us_insider_form4_scraper.py",
    "us_fundamental_rotator.py",
]

_RAW_REQUESTS_GET = re.compile(r"requests\.get\(")
_URLLIB_REQUEST = re.compile(r"urllib\.request|urlopen\(")
_HARDCODED_UA_DICT = re.compile(r'"User-Agent"\s*:\s*"')


def _source(filename: str) -> str:
    return (REPO / filename).read_text(encoding="utf-8")


# ==============================================================
# 1. sec.gov への生の HTTP 呼び出しが残っていないこと（機械検査）
# ==============================================================

def test_no_raw_requests_get_remaining():
    for filename in TARGET_FILES:
        source = _source(filename)
        assert not _RAW_REQUESTS_GET.search(source), (
            f"{filename} に requests.get( の直接呼び出しが残っています"
        )


def test_no_urllib_request_remaining():
    for filename in TARGET_FILES:
        source = _source(filename)
        assert not _URLLIB_REQUEST.search(source), (
            f"{filename} に urllib.request / urlopen( の直接呼び出しが残っています"
        )


# ==============================================================
# 2. ハードコードされた User-Agent 辞書リテラルが残っていないこと
# ==============================================================

def test_no_hardcoded_user_agent_dict_literal():
    for filename in TARGET_FILES:
        source = _source(filename)
        assert not _HARDCODED_UA_DICT.search(source), (
            f"{filename} にハードコードされた User-Agent 辞書リテラルが残っています"
        )


# ==============================================================
# 3. sec_client を import していること
# ==============================================================

def test_all_three_files_import_sec_client():
    for filename in TARGET_FILES:
        tree = ast.parse(_source(filename))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "sec_client" in imported, f"{filename} が sec_client を import していません"


# ==============================================================
# 4. 主要な取得関数が sec_client 経由で動作すること
# ==============================================================

def test_us_8k_monitor_fetch_filings_uses_sec_client_get_json(monkeypatch):
    calls = []

    def fake_get_json(url, **kw):
        calls.append(url)
        return {"hits": {"hits": [
            {
                "_id": "0000320193-26-000001",
                "_source": {"entity_name": "Apple Inc.", "file_num": "001-36743"},
            }
        ]}}

    monkeypatch.setattr(us_8k_monitor.sec_client, "get_json", fake_get_json)
    result = us_8k_monitor._fetch_8k_filings("2026-07-24")

    assert calls, "sec_client.get_json が呼ばれていません"
    assert "efts.sec.gov" in calls[0]
    assert result[0]["cik"] == 320193
    assert result[0]["company_name"] == "Apple Inc."


def test_us_8k_monitor_fetch_items_uses_sec_client_get(monkeypatch):
    calls = []

    class _Resp:
        text = "Item 2.02 ... Item 9.01 ..."

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(us_8k_monitor.sec_client, "get", fake_get)
    items = us_8k_monitor._fetch_8k_items(320193, "0000320193-26-000001")

    assert calls, "sec_client.get が呼ばれていません"
    assert items == ["2.02", "9.01"]


def test_us_8k_monitor_load_cik_to_ticker_map_uses_sec_client_cik_map(monkeypatch):
    calls = []

    def fake_cik_map(cfg=None, ttl_days=None):
        calls.append(cfg)
        return {"AAPL": "0000320193", "MSFT": "0000789019"}

    monkeypatch.setattr(us_8k_monitor.sec_client, "cik_map", fake_cik_map)
    result = us_8k_monitor._load_cik_to_ticker_map(SimpleNamespace())

    assert calls, "sec_client.cik_map が呼ばれていません"
    assert result == {320193: "AAPL", 789019: "MSFT"}


def test_insider_scraper_load_cik_map_uses_sec_client(monkeypatch):
    calls = []

    def fake_cik_map(cfg=None, ttl_days=None):
        calls.append(cfg)
        return {"AAPL": "0000320193"}

    monkeypatch.setattr(us_insider_form4_scraper.sec_client, "cik_map", fake_cik_map)
    result = us_insider_form4_scraper._load_cik_map()

    assert calls, "sec_client.cik_map が呼ばれていません"
    assert result == {"AAPL": "0000320193"}


def test_insider_scraper_get_cik_uses_sec_client(monkeypatch):
    calls = []

    def fake_get_cik(ticker, cfg=None):
        calls.append((ticker, cfg))
        return "0000320193"

    monkeypatch.setattr(us_insider_form4_scraper.sec_client, "get_cik", fake_get_cik)
    result = us_insider_form4_scraper._get_cik("aapl")

    assert calls == [("aapl", None)]
    assert result == "0000320193"


def test_insider_scraper_fetch_recent_filings_uses_sec_client_submissions(monkeypatch):
    calls = []

    # 固定日付だと実行日が進むと days=90 の窓から外れるため、実行日からの相対日付にする。
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=10)).isoformat()
    older = (date.today() - timedelta(days=40)).isoformat()

    def fake_submissions(cik, cfg=None):
        calls.append(cik)
        return {
            "filings": {"recent": {
                "form": ["4", "10-Q"],
                "filingDate": [recent, older],
                "accessionNumber": ["0001234567-26-000001", "0001234567-26-000002"],
                "primaryDocument": ["form4.xml", "10q.htm"],
            }}
        }

    monkeypatch.setattr(us_insider_form4_scraper.sec_client, "submissions", fake_submissions)
    result = us_insider_form4_scraper._fetch_recent_form4_filings("0000320193", days=90)

    assert calls == [320193]
    assert len(result) == 1
    assert result[0]["accession_number"] == "0001234567-26-000001"


def test_insider_scraper_fetch_form4_xml_uses_sec_client_get(monkeypatch):
    calls = []

    class _Resp:
        text = "<ownershipDocument></ownershipDocument>"

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(us_insider_form4_scraper.sec_client, "get", fake_get)
    monkeypatch.setattr(us_insider_form4_scraper.time, "sleep", lambda _s: None)
    result = us_insider_form4_scraper._fetch_form4_xml(320193, "0001234567-26-000001", "form4.xml")

    assert calls, "sec_client.get が呼ばれていません"
    assert result == "<ownershipDocument></ownershipDocument>"


def test_rotator_fetch_company_tickers_uses_sec_client_cik_map(monkeypatch):
    calls = []

    def fake_cik_map(cfg=None, ttl_days=None):
        calls.append(ttl_days)
        return {"AAPL": "0000320193"}

    monkeypatch.setattr(us_fundamental_rotator.sec_client, "cik_map", fake_cik_map)
    result = us_fundamental_rotator._fetch_company_tickers(SimpleNamespace())

    assert calls == [None]
    assert result == {"AAPL": 320193}


def test_rotator_fetch_company_tickers_force_refresh_bypasses_cache(monkeypatch):
    calls = []

    def fake_cik_map(cfg=None, ttl_days=None):
        calls.append(ttl_days)
        return {"AAPL": "0000320193"}

    monkeypatch.setattr(us_fundamental_rotator.sec_client, "cik_map", fake_cik_map)
    us_fundamental_rotator._fetch_company_tickers(SimpleNamespace(), force_refresh=True)

    assert calls == [0], "force_refresh=True は ttl_days=0 で sec_client.cik_map を呼ぶ必要があります"


def test_rotator_fetch_submissions_uses_sec_client(monkeypatch):
    calls = []

    def fake_submissions(cik, cfg=None):
        calls.append(cik)
        return {"filings": {"recent": {"form": [], "filingDate": []}}}

    monkeypatch.setattr(us_fundamental_rotator.sec_client, "submissions", fake_submissions)
    result = us_fundamental_rotator._fetch_submissions(320193, SimpleNamespace())

    assert calls == [320193]
    assert result is not None


def test_rotator_fetch_xbrl_facts_uses_sec_client_get_json_with_timeout_30(monkeypatch):
    calls = []

    def fake_get_json(url, cfg=None, **kw):
        calls.append((url, kw.get("timeout")))
        return {"facts": {}}

    monkeypatch.setattr(us_fundamental_rotator.sec_client, "get_json", fake_get_json)
    result = us_fundamental_rotator._fetch_xbrl_facts(320193, SimpleNamespace())

    assert calls, "sec_client.get_json が呼ばれていません"
    url, timeout = calls[0]
    assert "320193" in url
    assert timeout == 30, "XBRL取得は現行挙動維持のため timeout=30 を明示する必要があります"
    assert result == {"facts": {}}


# ==============================================================
# 5. Form 4 XML パース結果が移行前後で変わらないこと（特性化テスト）
# ==============================================================

_FORM4_XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Financial Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTransaction>
    <transactionDate><value>2026-06-15</value></transactionDate>
    <transactionCoding><transactionType>P</transactionType></transactionCoding>
    <transactionAmounts>
      <transactionShares><value>3000</value></transactionShares>
      <transactionPricePerShare><value>150.25</value></transactionPricePerShare>
    </transactionAmounts>
  </nonDerivativeTransaction>
  <nonDerivativeTransaction>
    <transactionDate><value>2026-06-20</value></transactionDate>
    <transactionCoding><transactionType>S</transactionType></transactionCoding>
    <transactionAmounts>
      <transactionShares><value>500</value></transactionShares>
      <transactionPricePerShare><value>152.00</value></transactionPricePerShare>
    </transactionAmounts>
  </nonDerivativeTransaction>
</ownershipDocument>"""

# 実測値（移行後の us_insider_form4_scraper._parse_form4_xml を実際に実行して取得）。
_EXPECTED_PARSED_FORM4 = {
    "officer_title": "chief financial officer",
    "is_director": True,
    "is_officer": True,
    "is_ten_pct_owner": False,
    "transactions": [
        {
            "type": "P",
            "date": "2026-06-15",
            "shares": 3000.0,
            "price": 150.25,
            "value_usd": 450750.0,
            "is_deriv": False,
        },
        {
            "type": "S",
            "date": "2026-06-20",
            "shares": 500.0,
            "price": 152.0,
            "value_usd": 76000.0,
            "is_deriv": False,
        },
    ],
}


def test_parse_form4_xml_unchanged_by_migration():
    result = us_insider_form4_scraper._parse_form4_xml(_FORM4_XML_FIXTURE)
    assert result == _EXPECTED_PARSED_FORM4
