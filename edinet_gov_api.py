"""
edinet_gov_api.py — 政府 EDINET API v2 の応答の読み分け（edinet_gov_docs / edinet_gov_text / edinet_gov_xbrl が共用）

EDINET API v2 は、キーの誤り・回数制限・障害も HTTP 200 で返し、本文の JSON に
{"StatusCode": 401, "message": "..."} や {"metadata": {"status": "404", ...}} を入れてくる。
メンテナンス中は JSON でもない（HTML）ことがある。HTTP の状態だけを見ると「HTTP 200 で失敗」としか分からず、
書類一覧では失敗をその日の書類0件と取り違える。ここで本文まで見て理由を返し、続けて失敗したら打ち切る。
"""
from __future__ import annotations

import re

API = "https://api.edinet-fsa.go.jp/api/v2"

# 続けて失敗したらその回を打ち切る回数（メンテナンス中に1秒おきに数百回叩き続けないため）
STREAK_LIMIT = 10
# これが返ったら続けても無駄なので即打ち切る（キーの誤り・回数制限）
FATAL_CODES = {"401", "403", "429"}
# 書類そのものが取れない（ID の誤り・取り下げ・その形式が無い）。取り直しても同じなので、その書類は無しとして進める
GONE_CODES = {"400", "404"}


def _body_reason(r) -> tuple[str, str]:
    """失敗した応答の (状態コード, 理由)。状態コードは本文の StatusCode / metadata.status、無ければ HTTP の状態。"""
    head = (r.content or b"")[:2000]
    try:
        js = r.json()
    except Exception:
        js = None
    if isinstance(js, dict):
        meta = js.get("metadata") if isinstance(js.get("metadata"), dict) else {}
        code = str(js.get("StatusCode") or meta.get("status") or r.status_code)
        msg = str(js.get("message") or meta.get("message") or "").strip()
        return code, f"API {code} {msg[:80]}".strip()
    text = head.decode("utf-8", errors="replace")
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if m:
        return str(r.status_code), f"HTTP {r.status_code} HTML「{m.group(1).strip()[:60]}」（メンテナンス・遮断の疑い）"
    snippet = re.sub(r"\s+", " ", text).strip()[:60]
    return str(r.status_code), f"HTTP {r.status_code} ZIP/JSON でない応答「{snippet}」"


def check_zip(r) -> tuple[bool, str, str]:
    """書類取得（type=1/5）の応答。(成功か, 状態コード, 理由)。本体は ZIP で、先頭の PK で見分ける。"""
    if r.status_code == 200 and (r.content or b"").startswith(b"PK"):
        return True, "200", ""
    code, why = _body_reason(r)
    return False, code, why


def check_list(r) -> tuple[list | None, str, str]:
    """書類一覧（documents.json）の応答。(results か None, 状態コード, 理由)。
    正常は metadata.status が "200"。それ以外を空の一覧として扱わない。"""
    try:
        js = r.json() if r.status_code == 200 else None
    except Exception:
        js = None
    if isinstance(js, dict) and isinstance(js.get("metadata"), dict) and str(js["metadata"].get("status")) == "200":
        return list(js.get("results") or []), "200", ""
    code, why = _body_reason(r)
    return None, code, why


class Streak:
    """続けて失敗した回数を数え、打ち切るかを返す。理由ごとの件数も持つ（通知に1行で出す）。"""

    def __init__(self, limit: int = STREAK_LIMIT):
        self.limit = limit
        self.run = 0
        self.reasons: dict[str, int] = {}
        self.stopped = ""
        self.fatal = False

    def ok(self) -> None:
        self.run = 0

    def fail(self, code: str, why: str) -> bool:
        """失敗を1件数える。打ち切るべきなら True（理由は self.stopped）。"""
        self.run += 1
        key = re.sub(r"\s+", " ", why)[:100]
        self.reasons[key] = self.reasons.get(key, 0) + 1
        if code in FATAL_CODES:
            self.fatal = True
            self.stopped = f"{why}（続けても無駄なので打ち切り）"
        elif self.run >= self.limit:
            self.stopped = f"{self.run}回続けて失敗・最後は {why}（API の停止・メンテナンスの疑い。次の回にやり直す）"
        return bool(self.stopped)

    def summary(self) -> str:
        return " / ".join(f"{k}×{v}" for k, v in sorted(self.reasons.items(), key=lambda kv: -kv[1]))

    def put(self, stat: dict) -> None:
        """集計（stat）に失敗の内訳と打ち切りの理由を書き足す。"""
        if self.reasons:
            stat["error_reasons"] = self.summary()
        if self.stopped:
            stat["api_stopped"] = self.stopped
            stat["api_fatal"] = self.fatal


def outcome(stat: dict, progress_key: str) -> tuple[str, int]:
    """CLI の結果の印と終了コード。API の一時停止（メンテナンス等）は次の回にやり直せるので ⚠️ で終了コード0、
    キーの誤り・回数制限と、停止以外の理由で1件も進まなかったときは ❌ で1。"""
    if stat.get("skipped"):
        return "⏩", 0
    if stat.get("api_stopped"):
        return ("❌", 1) if stat.get("api_fatal") else ("⚠️", 0)
    if stat.get("errors") and not stat.get(progress_key):
        return "❌", 1
    return "✅", 0
