"""edinet_gov_api: HTTP 200 で返る失敗の読み分けと、続けて失敗したときの打ち切り。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import edinet_gov_api as ega


class Resp:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def json(self):
        return json.loads(self.content.decode("utf-8"))


KEY_ERR = b'{"StatusCode": 401,"message": "Access denied due to invalid subscription key."}'
HTML = "<html><head><title>ただいまメンテナンス中です</title></head><body></body></html>".encode()


def test_zip_ok():
    assert ega.check_zip(Resp(b"PK\x03\x04rest")) == (True, "200", "")


def test_zip_key_error_reads_body():
    ok, code, why = ega.check_zip(Resp(KEY_ERR))
    assert not ok and code == "401" and "invalid subscription key" in why


def test_zip_html_maintenance():
    ok, code, why = ega.check_zip(Resp(HTML))
    assert not ok and code == "200" and "メンテナンス中" in why and "HTML" in why


def test_zip_metadata_status():
    body = json.dumps({"metadata": {"status": "404", "message": "Not Found"}}).encode()
    ok, code, why = ega.check_zip(Resp(body))
    assert not ok and code == "404" and "Not Found" in why


def test_list_ok_and_empty_day():
    body = json.dumps({"metadata": {"status": "200"}, "results": [{"docID": "S1"}]}).encode()
    assert ega.check_list(Resp(body))[0] == [{"docID": "S1"}]
    empty = json.dumps({"metadata": {"status": "200"}, "results": []}).encode()
    assert ega.check_list(Resp(empty))[0] == []


def test_list_error_is_not_an_empty_day():
    # 失敗の JSON を書類0件の日として記録しない
    for body in (KEY_ERR, HTML, b"", json.dumps({"metadata": {"status": "500", "message": "x"}}).encode()):
        results, _code, why = ega.check_list(Resp(body))
        assert results is None and why


def test_streak_stops_after_limit_and_resets():
    s = ega.Streak(limit=3)
    assert not s.fail("200", "HTML")
    assert not s.fail("200", "HTML")
    s.ok()
    assert not s.fail("200", "HTML")
    assert not s.fail("200", "HTML")
    assert s.fail("200", "HTML") and "3回続けて失敗" in s.stopped and not s.fatal
    assert s.summary() == "HTML×5"


def test_streak_fatal_codes_stop_at_once():
    for code in ("401", "403", "429"):
        s = ega.Streak()
        assert s.fail(code, f"API {code}") and s.fatal


def test_outcome():
    assert ega.outcome({"skipped": True}, "days") == ("⏩", 0)
    assert ega.outcome({"errors": 0, "days": 3}, "days") == ("✅", 0)
    assert ega.outcome({"errors": 5, "days": 0}, "days") == ("❌", 1)
    assert ega.outcome({"errors": 5, "days": 0, "api_stopped": "停止", "api_fatal": False}, "days") == ("⚠️", 0)
    assert ega.outcome({"errors": 1, "days": 0, "api_stopped": "キー", "api_fatal": True}, "days") == ("❌", 1)


def test_put_writes_reasons():
    s = ega.Streak(limit=2)
    s.fail("500", "API 500 x")
    s.fail("500", "API 500 x")
    st = {}
    s.put(st)
    assert st["api_stopped"] and st["api_fatal"] is False and st["error_reasons"] == "API 500 x×2"


def test_gone_codes_are_not_fatal_and_reported():
    body = json.dumps({"StatusCode": 400, "message": "Bad Request"}).encode()
    ok, code, _why = ega.check_zip(Resp(body))
    assert not ok and code in ega.GONE_CODES and code not in ega.FATAL_CODES
