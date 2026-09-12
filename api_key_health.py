# ==============================================================
# api_key_health.py  (Issue J-2 / APIキー生存判定)
# ==============================================================
# 目的:
#   各種APIキーが「生きているか / 失効しているか」を、実行ログの冒頭で
#   1行サマリ＋詳細表として一望できるようにする。従来はキー失効が
#   下流モジュールの雑多なエラー（DeepL403・CSE403・EDINET 0件 等）
#   として散らばり、原因特定にログ考古学が必要だった。
#
# 設計方針:
#   ・probe実行部（ネットワークI/O）と判定部（純粋ロジック）を分離する。
#     判定部はHTTPコード/レスポンス断片 → 状態のマッピングのみを行う純関数で、
#     テストがネットワークをモックせずに済むようにする。
#   ・プローブは全て軽量・1コールのみ・タイムアウト5秒。キーの値そのもの・
#     その一部を print/例外メッセージ/戻り値に一切含めない（マスキング必須）。
#   ・ネットワーク不通時は該当キーを ⚠️（判定不能）として扱い、クラッシュしない。
#
# 【依存】config.py / requests（無ければ全キー⚠️にフォールバック）/
#         smtplib・標準ライブラリ
# ==============================================================
from __future__ import annotations

import datetime as _dt
from typing import Optional

# ── 状態定数 ──────────────────────────────────────────────────
STATE_UNSET   = "unset"    # 未設定（キーが空）
STATE_ALIVE   = "alive"    # ✅ 生存
STATE_DEAD    = "dead"     # ❌ 失効・権限エラー
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B5_API_KEY_HEALTH_STATE_LIMITED': 'STATE_LIMITED',
    'B5_API_KEY_HEALTH_PROBE_TIMEOUT': 'PROBE_TIMEOUT',
    'B5_API_KEY_HEALTH_PROBE_TIMEOUT_SLOW': 'PROBE_TIMEOUT_SLOW',
}

STATE_LIMITED = "limited"  # ⚠️ レート制限・quota・判定不能
STATE_SKIPPED = "skipped"  # ⏭️ チェック対象外

ICON = {
    STATE_UNSET:   "⬜",
    STATE_ALIVE:   "✅",
    STATE_DEAD:    "❌",
    STATE_LIMITED: "⚠️",
    STATE_SKIPPED: "⏭️",
}

# サマリで数える順（未設定は末尾）
_SUMMARY_ORDER = [STATE_ALIVE, STATE_DEAD, STATE_LIMITED, STATE_SKIPPED, STATE_UNSET]

# プローブ全体のタイムアウト（秒/コール）
PROBE_TIMEOUT = 5

# 遅い相手向けのタイムアウト（秒/コール）
# 2026-09-12: e-Stat のプローブが 2026-07 以降ずっと
# 「⚠️ 判定不能（通信エラー・タイムアウト）」を出し続けていた。
# しかし同じ実行で本体の gov_pipeline は「政府統計取得完了: 14/8」で
# 成功しており、キーは生きている＝プローブ側の偽陽性だった。
# 原因は getStatsList（統計カタログの一覧）が limit=1 でも重く、
# 米国のランナーから日本の政府APIを叩くと 5 秒に収まらないこと
# （手元の日本からの実測では 0.2〜0.5 秒で返る）。
# 常時⚠️は本物の異常を埋もれさせるため、相手に合わせた値にする。
PROBE_TIMEOUT_SLOW = 15


# ==============================================================
# 純粋ロジック（判定部）— ネットワーク非依存・テスト対象
# ==============================================================
def is_unset(value: Optional[str]) -> bool:
    """キーが未設定（None / 空 / 空白のみ）かどうか。"""
    return value is None or str(value).strip() == ""


def mask_key(value: Optional[str]) -> str:
    """
    キー値をログ安全な形にマスクする。値の一部も復元できないようにする。
    実際の出力には使わないが、万一のデバッグ用途でも生値が漏れないことを保証する。
    """
    if is_unset(value):
        return "<unset>"
    return "<set:len=%d>" % len(str(value))


def classify_http_generic(status_code: Optional[int]) -> str:
    """
    汎用HTTP判定: 200=✅ / 401,403=❌ / それ以外=⚠️。
    EDINET_API_KEY・EDINETDB_API_KEY・GOOGLE_CSE_API_KEY 等で共用。
    status_code=None（通信不能・例外）は ⚠️（判定不能）。
    """
    if status_code == 200:
        return STATE_ALIVE
    if status_code in (401, 403):
        return STATE_DEAD
    return STATE_LIMITED


def classify_edinetdb(status_code: Optional[int]) -> str:
    """
    EdinetDB判定: 200=✅ / 401,403=❌ / 429（レート制限）=⚠️ / その他=⚠️。
    レート制限を失効と区別する（429 は ⚠️ であって ❌ ではない）。
    """
    if status_code == 200:
        return STATE_ALIVE
    if status_code in (401, 403):
        return STATE_DEAD
    return STATE_LIMITED


def classify_estat(status_field: Optional[int], has_auth_error: bool) -> str:
    """
    e-Stat判定: レスポンス JSON の STATUS が 0 なら✅。
    認証系エラー（appId不正等）なら❌。それ以外/取得失敗は⚠️。
    """
    if has_auth_error:
        return STATE_DEAD
    if status_field == 0:
        return STATE_ALIVE
    return STATE_LIMITED


def classify_fred(status_code: Optional[int], body_text: str = "") -> str:
    """
    FRED判定: 200=✅ / 400かつ本文に "api_key" を含む=❌（キー不正）/ それ以外=⚠️。
    """
    if status_code == 200:
        return STATE_ALIVE
    if status_code == 400 and "api_key" in (body_text or "").lower():
        return STATE_DEAD
    return STATE_LIMITED


def classify_deepl(status_code: Optional[int]) -> str:
    """
    DeepL判定: 200=✅ / 403=❌（キー失効・権限）/ 456=⚠️（quota超過）/ その他=⚠️。
    """
    if status_code == 200:
        return STATE_ALIVE
    if status_code == 403:
        return STATE_DEAD
    return STATE_LIMITED


def prev_business_day(today: Optional[_dt.date] = None) -> _dt.date:
    """
    前営業日（土日をスキップ）を返す。EDINET documents.json のプローブ日付に使う。
    祝日は考慮しない（0件でも 200 が返れば✅判定できるため実害なし）。
    """
    d = (today or _dt.date.today()) - _dt.timedelta(days=1)
    while d.weekday() >= 5:  # 5=土, 6=日
        d -= _dt.timedelta(days=1)
    return d


def build_summary(entries: dict) -> str:
    """
    判定結果 dict（{key_name: {"state": ...}}）から grep しやすいサマリ1行を生成する。
    例: 「🔑 [key_health] ✅6 ❌2 ⚠️1 ⏭️2 | ❌: EDINETDB_API_KEY, DEEPL_API_KEY」
    """
    counts = {s: 0 for s in _SUMMARY_ORDER}
    dead_keys = []
    for name in sorted(entries):
        state = entries[name].get("state", STATE_LIMITED)
        if state not in counts:
            state = STATE_LIMITED
        counts[state] += 1
        if state == STATE_DEAD:
            dead_keys.append(name)

    parts = []
    for s in _SUMMARY_ORDER:
        if counts[s] == 0 and s == STATE_UNSET:
            continue  # 未設定0件は冗長なので省く
        parts.append(f"{ICON[s]}{counts[s]}")

    line = "🔑 [key_health] " + " ".join(parts)
    if dead_keys:
        line += " | ❌: " + ", ".join(dead_keys)
    return line


def format_detail(name: str, state: str, message: str = "") -> str:
    """
    詳細行を生成する。例:
      「🔑 EDINETDB_API_KEY: ❌ HTTP 401（失効の疑い・再発行が必要）」
    message にはキー値・その一部を含めないこと（呼び出し側の責務）。
    """
    icon = ICON.get(state, ICON[STATE_LIMITED])
    body = f"🔑 {name}: {icon}"
    if message:
        body += f" {message}"
    return body


def ledger_status_of(entries: dict) -> str:
    """❌ が1件でもあれば "warn"、無ければ "success"。"""
    for v in entries.values():
        if v.get("state") == STATE_DEAD:
            return "warn"
    return "success"


# ==============================================================
# probe実行部（ネットワークI/O）— テスト対象外
# ==============================================================
def _requests():
    """requests を遅延 import。無ければ None（全キー⚠️フォールバック）。"""
    try:
        import requests  # type: ignore
        return requests
    except Exception:
        return None


def _entry(state: str, message: str) -> dict:
    return {"state": state, "icon": ICON.get(state, ICON[STATE_LIMITED]),
            "message": message}


def _probe_edinetdb(key: str) -> dict:
    """
    EdinetDB を認証必須の /companies/{code}/analysis で1コール判定。
    200=✅ / 401,403=❌ / 429=⚠️（レート制限・失効ではない）/ 他=⚠️。
    2026-07-16 Fable裁定: /search は認証不要のため失効キーでも200が返り
    偽✅になる（edinet_client.py L137に明記）→ 予算1コール消費を許容して
    認証必須エンドポイントに変更（予算80-100/日に対して1コールは無視できる）。
    対象は三菱UFJ（E03606）＝EdinetDBに確実に存在する大型固定コード。
    """
    req = _requests()
    if req is None:
        return _entry(STATE_LIMITED, "判定不能（requests 未導入）")
    try:
        resp = req.get(
            "https://edinetdb.jp/v1/companies/E03606/analysis",
            headers={"X-API-Key": key, "User-Agent": "StockAnalyzer/3.0"},
            timeout=PROBE_TIMEOUT,
        )
        state = classify_edinetdb(resp.status_code)
        if resp.status_code == 429:
            return _entry(STATE_LIMITED, "HTTP 429（レート制限・失効ではない）")
        return _entry(state, _http_msg(state, resp.status_code))
    except Exception:
        return _entry(STATE_LIMITED, "判定不能（通信エラー・タイムアウト）")


def _probe_fudosan(key: str) -> dict:
    """公式RESTの最小ページを1コールして認証状態を判定する。"""
    req = _requests()
    if req is None:
        return _entry(STATE_LIMITED, "判定不能（requests 未導入）")
    try:
        resp = req.get(
            "https://fudosandb.jp/v1/area-profiles",
            params={"page": 1, "per_page": 1},
            headers={"X-API-Key": key, "User-Agent": "StockAnalyzer/3.0"},
            timeout=PROBE_TIMEOUT,
        )
        state = classify_http_generic(resp.status_code)
        if resp.status_code == 429:
            return _entry(STATE_LIMITED, "HTTP 429（レート制限・失効ではない）")
        return _entry(state, _http_msg(state, resp.status_code))
    except Exception:
        return _entry(STATE_LIMITED, "判定不能（通信エラー・タイムアウト）")


def _probe_estat(key: str) -> dict:
    """e-Stat getStatsList?limit=1 を1コール。STATUS=0=✅ / 認証エラー=❌。"""
    req = _requests()
    if req is None:
        return _entry(STATE_LIMITED, "判定不能（requests 未導入）")
    try:
        resp = req.get(
            "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsList",
            params={"appId": key, "limit": 1},
            timeout=PROBE_TIMEOUT_SLOW,
        )
        status_field = None
        has_auth_error = False
        try:
            payload = resp.json()
            result = (payload.get("GET_STATS_LIST", {}) or {}).get("RESULT", {}) or {}
            status_field = result.get("STATUS")
            err_msg = str(result.get("ERROR_MSG", ""))
            # e-Stat は appId 不正時 STATUS!=0 かつ認証系メッセージを返す
            if status_field not in (0, None) and (
                "appId" in err_msg or "認証" in err_msg or "無効" in err_msg
            ):
                has_auth_error = True
        except Exception:
            pass
        state = classify_estat(status_field, has_auth_error)
        if state == STATE_ALIVE:
            return _entry(state, "STATUS 0（正常）")
        if state == STATE_DEAD:
            return _entry(state, "認証エラー（appId無効の疑い・再発行が必要）")
        return _entry(state, f"HTTP {resp.status_code} / STATUS {status_field}（判定不能）")
    except Exception:
        return _entry(STATE_LIMITED, "判定不能（通信エラー・タイムアウト）")


def _probe_fred(key: str) -> dict:
    """FRED series?series_id=GNPCA を1コール。200=✅ / 400 api_key=❌ / 他=⚠️。"""
    req = _requests()
    if req is None:
        return _entry(STATE_LIMITED, "判定不能（requests 未導入）")
    try:
        resp = req.get(
            "https://api.stlouisfed.org/fred/series",
            params={"series_id": "GNPCA", "api_key": key, "file_type": "json"},
            timeout=PROBE_TIMEOUT,
        )
        state = classify_fred(resp.status_code, resp.text[:500])
        if state == STATE_DEAD:
            return _entry(state, "HTTP 400 api_key（キー不正・再発行が必要）")
        return _entry(state, _http_msg(state, resp.status_code))
    except Exception:
        return _entry(STATE_LIMITED, "判定不能（通信エラー・タイムアウト）")


def _probe_deepl(key: str) -> dict:
    """
    DeepL /v2/usage を1コール（quota消費ゼロ）。200=✅ / 403=❌ / 456=⚠️。
    無料キー（末尾 :fx）は api-free ホストを使う。
    """
    req = _requests()
    if req is None:
        return _entry(STATE_LIMITED, "判定不能（requests 未導入）")
    host = "https://api-free.deepl.com" if str(key).endswith(":fx") \
        else "https://api.deepl.com"
    try:
        resp = req.get(
            f"{host}/v2/usage",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            timeout=PROBE_TIMEOUT,
        )
        state = classify_deepl(resp.status_code)
        if state == STATE_ALIVE:
            # quota残をログに出せる（値は使用量統計でありキー値ではない）
            used = limit = None
            try:
                j = resp.json()
                used, limit = j.get("character_count"), j.get("character_limit")
            except Exception:
                pass
            if used is not None and limit:
                return _entry(state, f"HTTP 200（quota {used}/{limit}）")
            return _entry(state, "HTTP 200（正常）")
        if state == STATE_DEAD:
            return _entry(state, "HTTP 403（失効の疑い・再発行が必要）")
        if resp.status_code == 456:
            return _entry(state, "HTTP 456（quota超過・失効ではない）")
        return _entry(state, _http_msg(state, resp.status_code))
    except Exception:
        return _entry(STATE_LIMITED, "判定不能（通信エラー・タイムアウト）")


def _probe_cse(key: str, cse_id: str) -> dict:
    """
    Google CSE を num=1 で1件だけ実クエリ（quota消費）。200=✅ / 401,403=❌ / 他=⚠️。
    quota保護のため呼び出し側で明示有効時のみ実行される。
    """
    req = _requests()
    if req is None:
        return _entry(STATE_LIMITED, "判定不能（requests 未導入）")
    try:
        resp = req.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": key, "cx": cse_id, "q": "test", "num": 1},
            timeout=PROBE_TIMEOUT,
        )
        state = classify_http_generic(resp.status_code)
        return _entry(state, _http_msg(state, resp.status_code))
    except Exception:
        return _entry(STATE_LIMITED, "判定不能（通信エラー・タイムアウト）")


def _probe_gmail_smtp(from_addr: str, password: str) -> dict:
    """
    Gmail SMTP に接続し AUTH まで行う（メール送信はしない）。成功=✅ / 認証失敗=❌。
    """
    import smtplib
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=PROBE_TIMEOUT) as smtp:
            smtp.login(from_addr, password)
        return _entry(STATE_ALIVE, "SMTP AUTH 成功")
    except smtplib.SMTPAuthenticationError:
        return _entry(STATE_DEAD, "SMTP認証失敗（App Password失効の疑い）")
    except Exception:
        return _entry(STATE_LIMITED, "判定不能（通信エラー・タイムアウト）")


def _probe_bq(cfg) -> dict:
    """既存 edinet_bq_client を生成し SELECT 1 を実行。成功=✅ / 失敗=❌。"""
    try:
        import importlib
        _bq = importlib.import_module("edinet_bq_client")
    except Exception:
        return _entry(STATE_LIMITED, "判定不能（edinet_bq_client 未ロード）")
    client = None
    # 2026-07-16 Fable裁定: 正式APIは class EDINETBQClient（edinet_bq_client.py L34）
    for factory in ("EDINETBQClient", "get_bq_client", "get_client", "make_client", "_get_client"):
        fn = getattr(_bq, factory, None)
        if callable(fn):
            try:
                client = fn(cfg)
            except TypeError:
                try:
                    client = fn()
                except Exception:
                    client = None
            except Exception:
                client = None
            if client is not None:
                break
    if client is None:
        return _entry(STATE_LIMITED, "判定不能（BQクライアント生成関数が見つからない）")
    # 2026-07-19是正: EDINETBQClient はラッパーで query() を持たない
    # （生の google.cloud.bigquery.Client は .client 属性に保持）。
    # 旧コードは client.query("SELECT 1") が AttributeError になり
    # 「認証・権限エラーの疑い」と誤報告していた（IAMは正常だった実害あり）。
    raw = getattr(client, "client", None) or client
    if not callable(getattr(raw, "query", None)):
        return _entry(STATE_LIMITED, "判定不能（query可能なBQクライアントが見つからない）")
    try:
        job = raw.query("SELECT 1")
        list(job.result(timeout=PROBE_TIMEOUT))
        return _entry(STATE_ALIVE, "SELECT 1 成功")
    except Exception as e:
        return _entry(STATE_DEAD,
                      f"SELECT 1 失敗（{type(e).__name__}・認証/権限エラーの疑い）")


def _http_msg(state: str, status_code: Optional[int]) -> str:
    """状態＋HTTPコードから定型メッセージを組む（キー値は絶対に含めない）。"""
    if state == STATE_ALIVE:
        return f"HTTP {status_code}（正常）"
    if state == STATE_DEAD:
        return f"HTTP {status_code}（失効の疑い・再発行が必要）"
    return f"HTTP {status_code}（判定不能・レート制限/quotaの可能性）"


# ==============================================================
# メインエントリ（main配線から呼ばれる）
# ==============================================================
def run_key_health(cfg) -> dict:
    """
    全キーを判定し、サマリ1行＋詳細行を print、判定 dict を返す。
    戻り値: {"summary": str, "entries": {key_name: {...}},
             "counts": {...}, "ledger_status": "success"|"warn"}
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    entries: dict = {}

    def _run(name: str, probe_fn, key_value) -> None:
        if is_unset(key_value):
            entries[name] = _entry(STATE_UNSET, "未設定（環境変数なし）")
            return
        try:
            entries[name] = probe_fn(key_value)
        except Exception:
            # probe実行部の想定外例外でもクラッシュさせない
            entries[name] = _entry(STATE_LIMITED, "判定不能（プローブ内例外）")

    g = lambda k: getattr(cfg, k, "") or ""

    # 2026-07-28: 既定値が他モジュールと食い違っていた。
    #   edinetdb_ingestor.py:408  → getattr(..., True)
    #   stock_analyzer_main.py:1407 → getattr(..., True)
    #   ここだけ False で、「ingest は動いているのに health は dormant と表示」
    #   という矛盾したログになっていた。True に揃える。
    # なお現在の config は既定 "0"（=False）なので、日次分析側の ingest
    # ステージ自体がスキップされている。EdinetDB から取りたい場合は
    # 環境変数 ENABLE_EDINETDB_INGEST=1 を設定する必要がある。
    if getattr(cfg, "ENABLE_EDINETDB_INGEST", True):
        _run("EDINETDB_API_KEY", _probe_edinetdb, g("EDINETDB_API_KEY"))
    else:
        entries["EDINETDB_API_KEY"] = _entry(
            STATE_SKIPPED,
            "ingest無効（ENABLE_EDINETDB_INGEST=0）。BQ既存データのみ参照"
        )
    _run("FUDOSAN_API_KEY",      _probe_fudosan,  g("FUDOSAN_API_KEY"))
    _run("ESTAT_API_KEY",        _probe_estat,    g("ESTAT_API_KEY"))
    _run("POLICY_FRED_API_KEY",  _probe_fred,     g("POLICY_FRED_API_KEY"))
    _run("DEEPL_API_KEY",        _probe_deepl,    g("DEEPL_API_KEY"))

    # ── GOOGLE_CSE: quota保護。既定 disabled → ⏭️。明示有効時のみ実クエリ ──
    cse_key = g("GOOGLE_CSE_API_KEY")
    cse_id  = g("GOOGLE_CSE_ID")
    if is_unset(cse_key):
        entries["GOOGLE_CSE_API_KEY"] = _entry(STATE_UNSET, "未設定（環境変数なし）")
    elif not getattr(cfg, "KEY_HEALTH_CSE_ENABLED", False):
        entries["GOOGLE_CSE_API_KEY"] = _entry(
            STATE_SKIPPED, "quota保護のためチェック無効（KEY_HEALTH_CSE_ENABLED=False）")
    elif is_unset(cse_id):
        entries["GOOGLE_CSE_API_KEY"] = _entry(
            STATE_LIMITED, "判定不能（GOOGLE_CSE_ID 未設定）")
    else:
        try:
            entries["GOOGLE_CSE_API_KEY"] = _probe_cse(cse_key, cse_id)
        except Exception:
            entries["GOOGLE_CSE_API_KEY"] = _entry(
                STATE_LIMITED, "判定不能（プローブ内例外）")

    # ── ALERT_GMAIL: SMTP接続+AUTHまで ──
    gmail_from = g("ALERT_GMAIL_FROM")
    gmail_pwd  = g("ALERT_GMAIL_PASSWORD")
    if is_unset(gmail_from) or is_unset(gmail_pwd):
        entries["ALERT_GMAIL_PASSWORD"] = _entry(
            STATE_UNSET, "未設定（FROM/PASSWORDのいずれかが空）")
    else:
        try:
            entries["ALERT_GMAIL_PASSWORD"] = _probe_gmail_smtp(gmail_from, gmail_pwd)
        except Exception:
            entries["ALERT_GMAIL_PASSWORD"] = _entry(
                STATE_LIMITED, "判定不能（プローブ内例外）")

    # ── BQ_CREDENTIALS: クライアント生成 + SELECT 1 ──
    bq_path = g("BQ_CREDENTIALS_PATH")
    bq_proj = g("BQ_PROJECT_ID")
    if is_unset(bq_path) and is_unset(bq_proj):
        entries["BQ_CREDENTIALS"] = _entry(
            STATE_UNSET, "未設定（BQ_CREDENTIALS_PATH / BQ_PROJECT_ID とも空）")
    else:
        try:
            entries["BQ_CREDENTIALS"] = _probe_bq(cfg)
        except Exception:
            entries["BQ_CREDENTIALS"] = _entry(
                STATE_LIMITED, "判定不能（プローブ内例外）")

    # ── 対象外（将来枠）: 今回はプローブしない ──
    # ANTHROPIC_API_KEY: SEC要約（ENABLE_SEC_SUMMARIZER）有効化時に probe を追加する枠。
    # GOOGLE_OAUTH_*   : OAuth トークンのリフレッシュ判定枠（要 token 永続化設計）。
    entries["ANTHROPIC_API_KEY"] = _entry(STATE_SKIPPED, "チェック対象外（将来枠）")
    entries["GOOGLE_OAUTH"]      = _entry(STATE_SKIPPED, "チェック対象外（将来枠）")

    # ── 出力 ──
    summary = build_summary(entries)
    print(f"  {summary}")
    for name in sorted(entries):
        e = entries[name]
        print(f"    {format_detail(name, e['state'], e.get('message', ''))}")

    counts = {s: 0 for s in _SUMMARY_ORDER}
    for e in entries.values():
        st = e.get("state", STATE_LIMITED)
        counts[st if st in counts else STATE_LIMITED] += 1

    return {
        "summary":       summary,
        "entries":       entries,
        "counts":        counts,
        "ledger_status": ledger_status_of(entries),
    }
