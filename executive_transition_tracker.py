# ==============================================================
# executive_transition_tracker.py  v1.0  (Wave 6 - I-3)
# ==============================================================
# 【役割】
#   EDINET 有報（有価証券報告書）の役員略歴セクションを
#   YoY（前年同期比）で差分抽出し、経営陣のスキルセット変化・
#   外部人材登用・経営方針転換シグナルを生成する。
#
# 【シグナル生成ロジック】
#   1. EDINET API から対象企業の直近2期の有報を取得
#   2. 役員略歴テキストから EXECUTIVE_SKILL_KEYWORDS にマッチする
#      スキルカテゴリを付与する
#   3. 前期との差分（新任役員のスキル増減）を集計する
#   4. 外部人材登用数 >= EXECUTIVE_TRANSITION_THRESHOLD で
#      「経営方針転換シグナル」を発火し sector_boost を生成する
#
# 【キャッシュ】
#   CACHE_DIR/executive/executive_transition_cache.json
#     TTL: EXECUTIVE_CACHE_TTL_DAYS（90日）
#     スキーマ:
#       {
#         "last_updated":  str,
#         "results":       {ticker: transition_dict},
#         "sector_boosts": {sector_code: float},
#         "transition_alerts": [{ticker, company, skills, ...}],
#       }
#
# 【実行頻度】
#   stock_analyzer_main.py から週次バッチ（月曜）で呼ばれる。
#   TTL 90日以内はキャッシュを返す。
#
# 【依存】
#   config.py (BLOCK 26: EXECUTIVE_SKILL_KEYWORDS /
#              EXECUTIVE_NEW_HIRE_BONUS /
#              EXECUTIVE_TRANSITION_THRESHOLD /
#              EXECUTIVE_CACHE_TTL_DAYS /
#              EXECUTIVE_CACHE_PATH /
#              EDINET_API_KEY / EDINET_LOOKBACK_DAYS)
#   requests / re（標準ライブラリ）
#
# 【呼び出し方（stock_analyzer_main.py から）】
#   _ett = _load_local_module("executive_transition_tracker", ...)
#   ett_result = _ett.run_executive_transition_tracker(cfg, results)
#   results["executive_transition"] = ett_result
# ==============================================================

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _exec_cache_dir(cfg) -> Path:
    """executive キャッシュディレクトリを返す（なければ作成）。"""
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    d = base / "executive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_cache(cfg) -> dict:
    """
    executive_transition_cache.json を読み込む。
    TTL 切れの場合は空 dict を返す。

    Returns:
        {
          "last_updated":      str,
          "results":           {ticker: transition_dict},
          "sector_boosts":     {sector_code: float},
          "transition_alerts": [{...}],
        }
    """
    fname = getattr(cfg, "EXECUTIVE_CACHE_PATH",
                    "executive_transition_cache.json")
    p     = _exec_cache_dir(cfg) / fname
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ttl  = int(getattr(cfg, "EXECUTIVE_CACHE_TTL_DAYS", 90))
        last = data.get("last_updated", "")
        if last:
            age = (datetime.now() - datetime.fromisoformat(last)).days
            if age > ttl:
                return {}
        return data
    except Exception as e:
        print(f"    ⚠️  [exec_transition] キャッシュ読み込み失敗: {str(e)[:60]}")
        return {}


def _save_cache(cfg, data: dict) -> None:
    """executive_transition_cache.json を保存する。"""
    fname = getattr(cfg, "EXECUTIVE_CACHE_PATH",
                    "executive_transition_cache.json")
    p     = _exec_cache_dir(cfg) / fname
    try:
        data["last_updated"] = datetime.now().isoformat()
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [exec_transition] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: EDINET API アクセス
# ==============================================================

_EDINET_BASE    = "https://disclosure.edinet-api.go.jp/api/v2"
_REQUEST_WAIT   = 1.0   # APIコール間の待機秒数
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B5_EXECUTIVE_TRANSITION_TRACKER_REQUEST_TIMEOUT': '_REQUEST_TIMEOUT',
    'B6_EXECUTIVE_TRANSITION_TRACKER_REQUEST_WAIT': '_REQUEST_WAIT',
}

_REQUEST_TIMEOUT = 20   # タイムアウト秒数


def _edinet_get(url: str, params: dict, api_key: str) -> Optional[dict]:
    """
    EDINET API に GET リクエストを送りレスポンス JSON を返す。
    失敗時は None を返す。

    Args:
        url:     エンドポイント URL
        params:  クエリパラメータ（dict）
        api_key: EDINET API キー

    Returns:
        dict: JSON レスポンス | None
    """
    try:
        import urllib.request
        import urllib.parse
        params["Subscription-Key"] = api_key
        full_url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            full_url,
            headers={
                "User-Agent": "stock_analyzer/1.0 (contact@example.com)",
                "Accept":     "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"    ⚠️  [exec_transition] EDINET API失敗 ({url[:60]}): "
              f"{str(e)[:50]}")
        return None


def _fetch_doc_list(edinet_code: str, api_key: str,
                    lookback_days: int = 400) -> list[dict]:
    """
    EDINET API /documents.json から指定企業の書類一覧を取得する。
    有価証券報告書（docTypeCode="120"）のみを返す。

    Args:
        edinet_code:   EDINETコード（例: "E02144"）
        api_key:       EDINET API キー
        lookback_days: 取得対象の日数（直近2期分を拾うため400日）

    Returns:
        [{doc_id, submission_date, fiscal_year_end, ...}, ...]
        最新2件を返す（降順）
    """
    docs: list[dict] = []
    end_date   = datetime.now()
    start_date = end_date - timedelta(days=lookback_days)
    current    = end_date

    while current >= start_date and len(docs) < 2:
        date_str = current.strftime("%Y-%m-%d")
        url      = f"{_EDINET_BASE}/documents.json"
        resp     = _edinet_get(url, {"date": date_str, "type": 2}, api_key)
        if resp:
            for item in resp.get("results", []):
                if (item.get("edinetCode") == edinet_code
                        and item.get("docTypeCode") == "120"   # 有報
                        and item.get("docID")):
                    docs.append({
                        "doc_id":          item["docID"],
                        "submission_date": item.get("submitDateTime", ""),
                        "fiscal_year_end": item.get("periodEnd", ""),
                        "company_name":    item.get("filerName", ""),
                    })
                    if len(docs) >= 2:
                        break
        current -= timedelta(days=1)
        time.sleep(_REQUEST_WAIT)

    return docs


def _fetch_doc_html(doc_id: str, api_key: str) -> Optional[str]:
    """
    EDINET API /documents/{doc_id} から HTML 書類を取得する。
    type=2（HTML）を指定して役員略歴セクションを含む HTML を返す。

    Args:
        doc_id:  書類管理番号（例: "S100XXXX"）
        api_key: EDINET API キー

    Returns:
        str: HTML テキスト | None
    """
    try:
        import urllib.request
        import urllib.parse
        params   = {"type": 2, "Subscription-Key": api_key}
        full_url = (
            f"{_EDINET_BASE}/documents/{doc_id}"
            + "?" + urllib.parse.urlencode(params)
        )
        req = urllib.request.Request(
            full_url,
            headers={
                "User-Agent": "stock_analyzer/1.0 (contact@example.com)",
                "Accept":     "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            return raw.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    ⚠️  [exec_transition] HTML取得失敗 ({doc_id}): "
              f"{str(e)[:50]}")
        return None


# ==============================================================
# BLOCK 3: 役員略歴テキスト抽出
# ==============================================================

def _extract_officer_section(html: str) -> str:
    """
    HTML から役員略歴セクションのテキストを抽出する。

    対象セクション名（有報の標準項目）:
      「役員の状況」「役員略歴」「取締役」「監査役」等

    Args:
        html: EDINET から取得した HTML テキスト

    Returns:
        str: 役員略歴セクションのテキスト（HTML タグ除去済み）
    """
    # HTML タグを除去
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)  # HTML エンティティ除去
    text = re.sub(r"\s+", " ", text).strip()

    # 役員関連セクションを切り出す正規表現
    # セクション開始: 「役員の状況」「役員一覧」「取締役」等
    # セクション終了: 次の大セクション（「コーポレートガバナンス」等）
    patterns = [
        r"役員の状況(.{100,8000})(?:コーポレート|報酬|会計|所有|主要|株主)",
        r"役員略歴(.{100,8000})(?:コーポレート|報酬|会計|所有|主要|株主)",
        r"取締役(?:及び監査役)?の状況(.{100,6000})(?:コーポレート|報酬|会計)",
    ]

    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            return m.group(1)[:5000]   # 最大5000文字

    # フォールバック: テキスト全体から役員関連テキストを抽出
    officer_texts = []
    lines = text.split(" ")
    in_section = False
    for i, line in enumerate(lines):
        if any(kw in line for kw in ["取締役", "監査役", "執行役員", "代表"]):
            in_section = True
        if in_section:
            officer_texts.append(line)
        if in_section and len(officer_texts) > 500:
            break

    return " ".join(officer_texts)[:5000]


def _parse_officers(text: str) -> list[dict]:
    """
    役員略歴テキストから役員情報を抽出する。

    抽出項目:
      - name: 役員名（推定）
      - title: 役職（取締役・執行役員等）
      - career_text: 略歴テキスト

    Args:
        text: _extract_officer_section() の出力

    Returns:
        [{"name": str, "title": str, "career_text": str}, ...]
    """
    officers: list[dict] = []

    # 役職パターン
    title_patterns = [
        r"(代表取締役(?:社長|会長|副社長)?)",
        r"(取締役(?:副社長|専務|常務)?(?:\s*\([^)]+\))?)",
        r"(監査役(?:\s*\([^)]+\))?)",
        r"(執行役員(?:\s*\([^)]+\))?)",
        r"(社外取締役)",
        r"(社外監査役)",
        r"(Chief\s+(?:Executive|Financial|Technology|Digital|Operating)\s+Officer)",
        r"(CEO|CFO|CTO|CDO|COO)",
    ]

    for pat in title_patterns:
        for m in re.finditer(pat, text):
            start = max(0, m.start() - 20)
            end   = min(len(text), m.end() + 300)
            career_text = text[start:end]

            # 役員名（役職の直前20文字から推定）
            name_text = text[max(0, m.start() - 50): m.start()]
            # 日本語名前パターン（漢字2〜4文字）
            name_m = re.search(r"([一-龯]{2,4}(?:\s[一-龯]{1,4})?)", name_text)
            name   = name_m.group(1) if name_m else ""

            if len(career_text) > 30:
                officers.append({
                    "name":        name,
                    "title":       m.group(1),
                    "career_text": career_text,
                })

    return officers


# ==============================================================
# BLOCK 4: スキルカテゴリ付与
# ==============================================================

def _assign_skill_categories(
    career_text: str,
    skill_keywords: dict,
) -> list[str]:
    """
    役員略歴テキストからスキルカテゴリを付与する。

    Args:
        career_text:    役員の略歴テキスト
        skill_keywords: {category: [kw1, kw2, ...]}
                        （config.EXECUTIVE_SKILL_KEYWORDS）

    Returns:
        [category, ...]: マッチしたカテゴリのリスト
    """
    matched: list[str] = []
    text_lower = career_text.lower()

    for cat, kw_list in skill_keywords.items():
        for kw in kw_list:
            if kw.lower() in text_lower:
                if cat not in matched:
                    matched.append(cat)
                break

    return matched


# ==============================================================
# BLOCK 5: YoY 差分抽出
# ==============================================================

def _compare_officers(
    curr_officers: list[dict],
    prev_officers: list[dict],
    skill_keywords: dict,
) -> dict:
    """
    今期と前期の役員情報を比較し、差分を抽出する。

    差分検知ロジック:
      - new_officers:  今期にいて前期にいない役員（新任）
      - left_officers: 前期にいて今期にいない役員（退任）
      - skill_change:  新任役員のスキルカテゴリ集計

    Args:
        curr_officers:  今期の役員リスト
        prev_officers:  前期の役員リスト
        skill_keywords: config.EXECUTIVE_SKILL_KEYWORDS

    Returns:
        {
          "new_officers":   [{"name", "title", "skills"}, ...],
          "left_officers":  [{"name", "title"}, ...],
          "new_skill_map":  {category: count},
          "new_hire_count": int,   # 外部人材登用とみなす新任数
        }
    """
    # 前期役員名セット（名前 or 役職で照合）
    prev_titles = {
        o.get("title", "") for o in prev_officers
    }
    prev_names  = {
        o.get("name",  "") for o in prev_officers if o.get("name")
    }

    new_officers:  list[dict] = []
    left_officers: list[dict] = []
    new_skill_map: dict[str, int] = {}

    # 新任役員の検出
    for o in curr_officers:
        name  = o.get("name",  "")
        title = o.get("title", "")
        # 同一名前または同一役職が前期にあればスキップ
        if name and name in prev_names:
            continue
        if title and title in prev_titles:
            continue

        skills = _assign_skill_categories(
            o.get("career_text", ""), skill_keywords
        )
        new_officers.append({
            "name":        name,
            "title":       title,
            "skills":      skills,
            "career_text": o.get("career_text", "")[:200],
        })
        for s in skills:
            new_skill_map[s] = new_skill_map.get(s, 0) + 1

    # 退任役員の検出
    curr_names  = {o.get("name",  "") for o in curr_officers if o.get("name")}
    curr_titles = {o.get("title", "") for o in curr_officers}
    for o in prev_officers:
        name  = o.get("name",  "")
        title = o.get("title", "")
        if (name and name not in curr_names) or \
           (title and title not in curr_titles and not name):
            left_officers.append({"name": name, "title": title})

    return {
        "new_officers":   new_officers,
        "left_officers":  left_officers,
        "new_skill_map":  new_skill_map,
        "new_hire_count": len(new_officers),
    }


# ==============================================================
# BLOCK 6: sector_boost 計算
# ==============================================================

def _build_sector_boosts(
    ticker_results: dict,
    results:        dict,
    cfg,
) -> dict[str, float]:
    """
    企業別の転換シグナルを JP業種コードに集約して sector_boost を計算する。

    ticker_results: {ticker: {"transition_detected": bool,
                               "new_skill_map": dict, ...}}

    ticker → sector_code マッピングは screener candidates から取得する。

    $boost = new\\_hire\\_count \times EXECUTIVE\\_NEW\\_HIRE\\_BONUS$
    上限: SCORE_MAX_INSTITUTION_ADJ（共用）

    Returns:
        {sector_code: total_boost_pt}
    """
    bonus_per_hire = float(getattr(cfg, "EXECUTIVE_NEW_HIRE_BONUS",  4.0))
    threshold      = int(getattr(cfg, "EXECUTIVE_TRANSITION_THRESHOLD", 2))
    max_adj        = float(getattr(cfg, "SCORE_MAX_INSTITUTION_ADJ",  15.0))

    # ticker → sector_code マッピング構築
    ticker_sector: dict[str, str] = {}
    screener = results.get("screener", {})
    for cand in (screener.get("candidates", [])
                 if isinstance(screener, dict) else []):
        t  = cand.get("ticker", "")
        sc = cand.get("sector_code", "")
        if t and sc:
            ticker_sector[t] = sc

    boosts: dict[str, float] = {}

    for ticker, res in ticker_results.items():
        if not res.get("transition_detected"):
            continue
        new_hire_count = int(res.get("new_hire_count", 0))
        if new_hire_count < threshold:
            continue
        sc = ticker_sector.get(ticker, "")
        if not sc:
            continue
        adj = round(new_hire_count * bonus_per_hire, 2)
        adj = round(min(max_adj, adj), 2)
        boosts[sc] = round(
            min(max_adj, boosts.get(sc, 0.0) + adj), 2
        )

    return boosts


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_executive_transition_tracker(cfg, results: dict) -> dict:
    """
    役員転職トラッカーのメイン関数。
    stock_analyzer_main.py の週次バッチから呼ばれる。

    処理フロー:
      1. キャッシュ確認（TTL 90日以内はキャッシュを返す）
      2. screener 通過銘柄の edinet_code を取得
      3. EDINET API から直近2期の有報を取得
      4. 役員略歴を抽出・YoY 差分を計算
      5. 外部人材登用数が閾値以上の企業でシグナル発火
      6. sector_boost を生成してキャッシュ保存

    Args:
        cfg:     config モジュール
        results: stock_analyzer_main.results

    Returns:
        {
          "sector_boosts":      {sector_code: float},
          "transition_alerts":  [{ticker, company, new_skills, ...}],
          "results":            {ticker: transition_dict},
          "total_analyzed":     int,
          "total_transitions":  int,
          "cached":             bool,
          "timestamp":          str,
          "skipped":            bool,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_EXECUTIVE_TRANSITION", True):
        print("  ⏩ 役員転職トラッカー: スキップ（ENABLE_EXECUTIVE_TRANSITION=False）")
        return {"skipped": True, "sector_boosts": {}, "transition_alerts": []}

    # W4: 政府EDINET documents.json の直接取得は撤去済み。
    # EdinetDB側の役員履歴契約へ移行するまで、明示的にfail-softする。
    return {
        "skipped": True,
        "reason": "retired: government EDINET direct fetch",
        "sector_boosts": {},
        "transition_alerts": [],
    }

    print("\n  👔 [exec_transition] 役員転職トラッカー 開始...")
    t0 = datetime.now()

    # ── キャッシュ確認 ────────────────────────────────────────
    cache = _load_cache(cfg)
    if cache:
        print(
            f"    📦 [exec_transition] キャッシュ利用 "
            f"(転換={len(cache.get('transition_alerts', []))}件): "
            f"TTL内のためスキップ"
        )
        return {**cache, "cached": True}

    api_key        = getattr(cfg, "EDINET_API_KEY", "")
    skill_keywords = getattr(cfg, "EXECUTIVE_SKILL_KEYWORDS", {})
    threshold      = int(getattr(cfg, "EXECUTIVE_TRANSITION_THRESHOLD", 2))

    if not api_key:
        print("    ⚠️  [exec_transition] EDINET_API_KEY 未設定 → スキップ")
        return {
            "skipped":        True,
            "sector_boosts":  {},
            "transition_alerts": [],
            "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 対象銘柄の edinet_code 取得 ──────────────────────────
    # screener 通過銘柄から edinet_code を収集する
    # edinet_code は edinet_rotation_manager が candidates に付与している
    screener   = results.get("screener", {})
    candidates = (screener.get("candidates", [])
                  if isinstance(screener, dict) else [])

    # edinet_code を持つ通過銘柄のみ対象
    target_companies: list[dict] = []
    for c in candidates:
        if not c.get("pass_filters"):
            continue
        edinet_code = c.get("edinet_code", "")
        if edinet_code:
            target_companies.append({
                "ticker":      c.get("ticker", ""),
                "edinet_code": edinet_code,
                "company":     c.get("name", c.get("ticker", "")),
                "sector_code": c.get("sector_code", ""),
            })

    # edinet_code がない場合は edinet DB から取得を試みる
    if not target_companies:
        try:
            import sys as _sys_erm, importlib as _imp_erm
            _sys_erm.path.insert(0, str(Path(cfg.DRIVE_BASE)))
            _erm = _imp_erm.import_module("edinet_rotation_manager")
            _edb = _erm.load_db(cfg)
            for ticker, edata in list(_edb.items())[:30]:   # 上位30社
                ec = edata.get("edinet_code", "")
                if ec:
                    target_companies.append({
                        "ticker":      ticker,
                        "edinet_code": ec,
                        "company":     edata.get("company_name", ticker),
                        "sector_code": edata.get("sector_code", ""),
                    })
        except Exception as _e:
            print(f"    ⚠️  [exec_transition] edinet_rotation_manager "
                  f"読み込み失敗: {str(_e)[:60]}")

    if not target_companies:
        print("    ⚠️  [exec_transition] edinet_code を持つ対象銘柄なし → スキップ")
        return {
            "skipped":        True,
            "sector_boosts":  {},
            "transition_alerts": [],
            "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    print(f"    🎯 [exec_transition] 対象: {len(target_companies)}社")

    # ── 各銘柄の役員略歴を取得・比較 ─────────────────────────
    ticker_results:    dict[str, dict]  = {}
    transition_alerts: list[dict]       = []
    max_companies = 20   # API負荷制限：1回の実行で最大20社まで

    for company in target_companies[:max_companies]:
        ticker      = company["ticker"]
        edinet_code = company["edinet_code"]
        company_name = company["company"]

        print(f"    🔍 [exec_transition] {company_name} ({ticker}) を分析中...")

        try:
            # 直近2期の有報書類一覧を取得
            docs = _fetch_doc_list(
                edinet_code = edinet_code,
                api_key     = api_key,
                lookback_days = 400,   # 2期分（約13ヶ月×2）
            )

            if len(docs) < 1:
                print(f"    ⚠️  [exec_transition] {ticker}: 有報なし → スキップ")
                ticker_results[ticker] = {
                    "transition_detected": False,
                    "reason":              "no_filings",
                }
                continue

            # 今期の役員略歴取得
            curr_html = _fetch_doc_html(docs[0]["doc_id"], api_key)
            if not curr_html:
                ticker_results[ticker] = {
                    "transition_detected": False,
                    "reason":              "html_fetch_failed",
                }
                continue

            curr_text     = _extract_officer_section(curr_html)
            curr_officers = _parse_officers(curr_text)

            # 前期の役員略歴取得（2期目がある場合）
            prev_officers: list[dict] = []
            if len(docs) >= 2:
                prev_html = _fetch_doc_html(docs[1]["doc_id"], api_key)
                if prev_html:
                    prev_text     = _extract_officer_section(prev_html)
                    prev_officers = _parse_officers(prev_text)

            # YoY 差分抽出
            diff = _compare_officers(curr_officers, prev_officers, skill_keywords)

            new_hire_count = diff["new_hire_count"]
            transition_detected = new_hire_count >= threshold

            ticker_results[ticker] = {
                "transition_detected": transition_detected,
                "new_hire_count":      new_hire_count,
                "new_skill_map":       diff["new_skill_map"],
                "new_officers":        diff["new_officers"][:5],
                "left_officers":       diff["left_officers"][:5],
                "company":             company_name,
                "fiscal_year_end":     docs[0].get("fiscal_year_end", ""),
            }

            if transition_detected:
                alert = {
                    "ticker":         ticker,
                    "company":        company_name,
                    "new_hire_count": new_hire_count,
                    "new_skills":     diff["new_skill_map"],
                    "new_officers":   diff["new_officers"][:3],
                    "sector_code":    company.get("sector_code", ""),
                    "detected_at":    datetime.now().strftime("%Y-%m-%d"),
                }
                transition_alerts.append(alert)
                print(
                    f"    🚨 [exec_transition] 転換シグナル: {company_name} "
                    f"新任{new_hire_count}名 スキル={diff['new_skill_map']}"
                )
            else:
                print(
                    f"    ✅ [exec_transition] {ticker}: "
                    f"新任{new_hire_count}名 (閾値{threshold}名未満)"
                )

        except Exception as e:
            print(f"    ⚠️  [exec_transition] {ticker} 処理エラー（スキップ）: "
                  f"{str(e)[:60]}")
            ticker_results[ticker] = {
                "transition_detected": False,
                "reason":              f"error: {str(e)[:50]}",
            }

        time.sleep(_REQUEST_WAIT)

    # ── sector_boost 計算 ─────────────────────────────────────
    sector_boosts = _build_sector_boosts(ticker_results, results, cfg)

    if sector_boosts:
        print(f"    📈 [exec_transition] sector_boost: {sector_boosts}")

    # ── キャッシュ保存 ────────────────────────────────────────
    new_cache = {
        "results":           ticker_results,
        "sector_boosts":     sector_boosts,
        "transition_alerts": transition_alerts,
    }
    _save_cache(cfg, new_cache)

    # ── BQ格納（Wave R0-2 / SPEC_db_schema.md §6-3）─────────────
    if bool(getattr(cfg, "ENABLE_US_BQ_EXPORT", True)):
        try:
            from edinet_bq_client import insert_rows
            _today = datetime.now().strftime("%Y-%m-%d")
            _rows = []
            for _tk, _r in (ticker_results or {}).items():
                if not isinstance(_r, dict) or _r.get("reason", "").startswith("error"):
                    continue
                _skill = _r.get("new_skill_map") or _r.get("skill_categories") or {}
                _rows.append({
                    "detection_date":      _today,
                    "ticker":              str(_tk),
                    "edinet_code":         _r.get("edinet_code"),
                    "company_name":        _r.get("company_name"),
                    "new_hire_count":      int(_r.get("new_hire_count", 0) or 0),
                    "skill_categories":    json.dumps(_skill, ensure_ascii=False),
                    "transition_detected": bool(_r.get("transition_detected", False)),
                    "fiscal_year_end":     _r.get("fiscal_year_end"),
                    "sector_code":         str(_r.get("sector_code", "") or ""),
                    "sector_boost_pt":     float(_r.get("sector_boost_pt", 0) or 0),
                    "updated_at":          datetime.now().isoformat(),
                })
            if _rows and insert_rows(rows=_rows, table_ref="us.executive_transitions"):
                print(f"    ☁️  [exec_transition] BQ格納 {len(_rows)}行 (us.executive_transitions)")
        except Exception as _bqe:
            print(f"    ⚠️  [exec_transition] BQ格納スキップ: {str(_bqe)[:60]}")

    elapsed = (datetime.now() - t0).seconds
    total_transitions = len(transition_alerts)
    print(
        f"\n  ✅ [exec_transition] 完了 ({elapsed}秒): "
        f"分析{len(ticker_results)}社 / "
        f"転換シグナル{total_transitions}社 / "
        f"sector_boost={sector_boosts}"
    )

    return {
        "sector_boosts":      sector_boosts,
        "transition_alerts":  transition_alerts,
        "results":            ticker_results,
        "total_analyzed":     len(ticker_results),
        "total_transitions":  total_transitions,
        "cached":             False,
        "timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "skipped":            False,
    }


# ==============================================================
# BLOCK 8: 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util

    cfg = None
    for p in ["/content/config.py", "config.py"]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            if spec and spec.loader:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    if cfg is None:
        print("ERROR: config.py が見つかりません")
        raise SystemExit(1)

    print("=== executive_transition_tracker 単体テスト ===\n")

    # ── スキルカテゴリ付与検証 ────────────────────────────────
    test_keywords = {
        "dx_transformation": ["CTO", "DX推進", "デジタル変革"],
        "global_expansion":  ["グローバル", "海外事業"],
        "financial_discipline": ["CFO", "投資銀行"],
    }

    career_dx  = "2020年 A社 CTO就任。DX推進担当として全社デジタル変革を推進。"
    career_gl  = "2018年 海外事業部長。グローバル展開を主導。"
    career_fin = "2015年 外資系投資銀行 CFO。M&A多数実績。"
    career_na  = "2010年 総務部入社。庶務担当。"

    assert _assign_skill_categories(career_dx,  test_keywords) == ["dx_transformation"]
    assert _assign_skill_categories(career_gl,  test_keywords) == ["global_expansion"]
    assert _assign_skill_categories(career_fin, test_keywords) == ["financial_discipline"]
    assert _assign_skill_categories(career_na,  test_keywords) == []
    print("  ✅ スキルカテゴリ付与 PASS")

    # ── YoY 差分検証 ─────────────────────────────────────────
    curr = [
        {"name": "田中太郎", "title": "代表取締役社長",
         "career_text": "元 外資系投資銀行 CFO。M&A経験豊富。"},
        {"name": "鈴木花子", "title": "社外取締役",
         "career_text": "デジタル変革 CTO 経験者。DX推進専門家。"},
        {"name": "佐藤次郎", "title": "取締役",
         "career_text": "当社入社25年。営業部門出身。"},
    ]
    prev = [
        {"name": "山田一郎", "title": "代表取締役社長",
         "career_text": "当社生え抜き30年。"},
        {"name": "佐藤次郎", "title": "取締役",
         "career_text": "当社入社25年。営業部門出身。"},
    ]

    diff = _compare_officers(curr, prev, test_keywords)
    print(f"\n  新任役員数: {diff['new_hire_count']}")
    print(f"  新任スキル: {diff['new_skill_map']}")
    print(f"  退任役員数: {len(diff['left_officers'])}")

    assert diff["new_hire_count"] == 2, \
        f"new_hire_count={diff['new_hire_count']} ≠ 2"
    assert "financial_discipline" in diff["new_skill_map"], \
        "financial_discipline スキル未検知"
    assert "dx_transformation" in diff["new_skill_map"], \
        "dx_transformation スキル未検知"
    assert len(diff["left_officers"]) == 1, \
        f"left_officers={len(diff['left_officers'])} ≠ 1"
    print("  ✅ YoY差分検証 PASS")

    print("\n  ✅ 全テスト PASS")
