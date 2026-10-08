# ==============================================================
# nedo_selection_scraper.py  v1.0  (I-2)
# NEDO採択情報スクレイパー + 企業名→証券コード名寄せ（Ph1）
# ==============================================================
# 【役割】
#   NEDO（国立研究開発法人新エネルギー・産業技術総合開発機構）の
#   公式サイトから採択企業名を取得し、証券コードに名寄せして
#   BigQuery `edinet.policy_support` テーブルへ格納する。
#
# 【Ph1 スコープ】
#   ① 採択情報ページのHTMLスクレイピング（configのURL一覧を走査）
#   ② BeautifulSoup によるテーブルパース（列名自動認識）
#   ③ 企業名→証券コード名寄せ（4段階ストラテジー）
#   ④ BQ格納用スキーマへの変換（edinet_bq_client経由）
#   ⑤ ローカルキャッシュ（CACHE_DIR/nedo_cache.json TTL:7日）
#
# 【名寄せ4段階ストラテジー】
#   Level 1 - 完全一致        : 正規化後の名前が完全一致     (confidence=1.00)
#   Level 2 - 部分一致        : 一方が他方を包含する        (confidence=0.85)
#   Level 3 - SequenceMatcher : difflib 類似度 >= FUZZY_THRESHOLD (confidence=0.75)
#   Level 4 - 不一致          : 名寄せ失敗 → securities_code="" (confidence=0.00)
#
# 【出力スキーマ（BQ: edinet.policy_support）】
#   securities_code  : str   証券コード（4桁）/ 不明時は ""
#   company_name     : str   名寄せ後の正式社名
#   raw_company_name : str   採択ページ記載の原文社名
#   project_name     : str   採択事業名
#   adoption_date    : str   採択日（YYYY-MM-DD）
#   project_type     : str   事業種別（green_innovation / energy / ai_iot / ...）
#   support_org      : str   支援機関（デフォルト: "NEDO"）
#   support_amount_m : float 補助金額（百万円）/ 非開示時は None
#   source_url       : str   出典URL
#   scraped_at       : str   スクレイプ日時（ISO 8601 UTC）
#   match_confidence : float 名寄せ確信度（0.0〜1.0）
#   match_method     : str   "exact"/"partial"/"fuzzy"/"unmatched"
#
# 【依存】
#   requests, beautifulsoup4（pip install requests beautifulsoup4）
#   config.py（NEDO_ADOPTION_URLS / CACHE_DIR）
#   edinet_bq_client.py（BQ書き込み）
#   すべての外部呼び出しを try/except で保護。失敗時は空リストを返す。
# ==============================================================

from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin


# ==============================================================
# BLOCK 0: 定数・設定
# ==============================================================

CACHE_FILENAME   = "nedo_cache.json"
CACHE_TTL_DAYS   = 7

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'NSS_FUZZY_THRESHOLD': 'FUZZY_THRESHOLD',
    'B4_NEDO_SELECTION_SCRAPER_CACHE_TTL_DAYS': 'CACHE_TTL_DAYS',
    'B5_NEDO_SELECTION_SCRAPER_REQUEST_TIMEOUT': 'REQUEST_TIMEOUT',
    'B6_NEDO_SELECTION_SCRAPER_REQUEST_INTERVAL': 'REQUEST_INTERVAL',
}

FUZZY_THRESHOLD  = 0.82    # SequenceMatcher 採用閾値（0.0〜1.0）
REQUEST_TIMEOUT  = 20
REQUEST_INTERVAL = 1.5     # スクレイプ間隔（秒）NEDO サーバー負荷軽減

# NEDO の決定ページは案件ごとに URL が変わり（SR3_ / CD3_ / EF3_ …）固定リストは
# すぐ 404 になるため、公募一覧を「状況＝決定」で絞った一覧から毎回たどる。
# config.NEDO_ADOPTION_URLS を設定した場合だけ、それを優先する。
_NEDO_DECISION_LIST_URL = "https://www.nedo.go.jp/form/event.php?f=koubo.html&state=10000175"
_NEDO_BASE_URL = "https://www.nedo.go.jp/"
_DEFAULT_NEDO_URLS: list[str] = []   # 互換のため残す（空＝一覧から自動発見）

# 決定ページで社名とみなす法人格の目印
_ORG_MARKERS: tuple = ("株式会社", "合同会社", "有限会社", "（株）", "(株)", "㈱",
                       "Inc.", "Corporation", "Co., Ltd", "LLC")

# 採択ページで会社名を示す列ヘッダーの候補（優先順）
_COMPANY_HEADER_PATTERNS: list[str] = [
    "機関名", "代表機関名", "企業名", "機関名称", "実施機関",
    "代表者機関", "参加機関", "共同実施機関", "委託先",
    "institution", "company", "organization",
]

# 採択日を示す列ヘッダーの候補
_DATE_HEADER_PATTERNS: list[str] = [
    "採択日", "決定日", "交付決定日", "採択年月日",
    "adoption_date", "date",
]

# 事業名を示す列ヘッダーの候補
_PROJECT_HEADER_PATTERNS: list[str] = [
    "テーマ名", "研究開発テーマ", "課題名", "事業名", "プロジェクト名",
    "研究テーマ", "title", "project",
]

# 補助金額を示す列ヘッダーの候補
_AMOUNT_HEADER_PATTERNS: list[str] = [
    "補助金額", "助成金額", "交付予定額", "採択金額", "委託費",
    "amount", "grant",
]

# 事業種別の自動分類キーワード（URLと事業名から判定）
_PROJECT_TYPE_KEYWORDS: dict[str, list[str]] = {
    "green_innovation": ["グリーンイノベーション", "GI基金", "脱炭素", "カーボンニュートラル"],
    "energy":           ["省エネ", "再生可能エネルギー", "蓄電池", "水素", "燃料電池"],
    "ai_iot":           ["AI", "IoT", "Connected Industries", "デジタル", "ロボット"],
    "medical":          ["医療", "バイオ", "ライフサイエンス", "創薬", "医薬品"],
    "materials":        ["素材", "材料", "マテリアル", "ナノ"],
    "robotics":         ["ロボット", "自動化", "無人化"],
    "moonshot":         ["ムーンショット", "Moonshot"],
}

# 法人種別の除去パターン（正規化に使用）
_LEGAL_SUFFIXES: list[str] = [
    "株式会社", "（株）", "(株)", "㈱",
    "有限会社", "（有）", "(有)",
    "合同会社", "合名会社", "合資会社",
    "一般社団法人", "公益社団法人",
    "一般財団法人", "公益財団法人",
    "特定非営利活動法人", "NPO法人",
    "国立大学法人", "公立大学法人", "学校法人",
    "独立行政法人", "国立研究開発法人",
    "株式会社　",   # 全角スペース付き
]

# HTTPリクエストヘッダー
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
        "+https://github.com/user/stock-analyzer)"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*",
}


# ==============================================================
# BLOCK 1: 名前正規化
# ==============================================================

def _normalize_company_name(name: str) -> str:
    """
    会社名を名寄せ用に正規化する。

    処理内容:
      1. 全角→半角（英数字・記号）
      2. 法人種別サフィックスの除去
      3. 空白・記号の正規化
      4. 小文字化

    Examples:
        "トヨタ自動車株式会社"   → "トヨタ自動車"
        "株式会社ホンダ"         → "ホンダ"
        "ＳＯＮＹ株式会社"      → "sony"
    """
    if not name:
        return ""
    try:
        # 全角→半角（NFKC正規化）
        normalized = unicodedata.normalize("NFKC", name)

        # 法人種別の除去（前後どちらにあっても対応）
        for suffix in _LEGAL_SUFFIXES:
            normalized = normalized.replace(suffix, "")

        # 括弧・記号を除去
        normalized = re.sub(r"[（）()「」【】『』・　\s]", "", normalized)

        # 小文字化（英字の大文字小文字を無視したマッチのため）
        normalized = normalized.lower().strip()

        return normalized
    except Exception:
        return name.strip().lower()


def _is_valid_company_name(name: str) -> bool:
    """採択情報として有効な会社名かどうかを判定する。"""
    if not name or len(name) < 2:
        return False
    # 明らかに無効なケースを除外
    excluded = {"（参考）", "合計", "小計", "合計額", "代表機関", "備考"}
    return name.strip() not in excluded


# ==============================================================
# BLOCK 2: 会社名マップ構築（名寄せ用データソース）
# ==============================================================

def _to_jp_code(code) -> str:
    """"6501.T" / "6501" / "130A.T" を4桁の証券コードに揃える。日本株以外は ""。

    スクリーナー候補の ticker（"6501.T"）と edinet.company 由来の4桁コードを
    同じ形に揃えないと、base_agent の照合で行ごとに食い違いボーナスが付かない。
    """
    s = str(code or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    return s if re.fullmatch(r"[0-9][0-9A-Z]{3}", s) else ""


def _build_company_name_map(
    results:   dict,
    bq_client=None,
) -> dict[str, str]:
    """
    証券コード → 正規化済み会社名 の辞書を構築する。

    データソース優先順:
      1. results["screener"]["candidates"] (最速・API不要)
      2. EDINETBQClient().query_to_df("SELECT securities_code, company_name...") (包括的)
      3. 空 dict (フォールバック)

    Returns:
        {normalized_company_name: securities_code, ...}
        キーは normalized_company_name（名寄せの比較対象として使用）
    """
    name_map: dict[str, str] = {}

    # ── Source 1: screener candidates ────────────────────────
    try:
        screener = results.get("screener", {})
        candidates = screener.get("candidates", screener.get("passed", []))
        for c in candidates:
            code = _to_jp_code(c.get("ticker", c.get("code", "")))
            name = str(c.get("name", c.get("company_name", "")) or "")
            if code and name:
                normalized = _normalize_company_name(name)
                if normalized:
                    name_map[normalized] = code
    except Exception:
        pass

    # ── Source 2: BigQuery financials テーブル ─────────────────
    if bq_client is not None and len(name_map) < 100:
        try:
            # edinet.financials には securities_code / company_name 列が無い。
            # 社名と証券コードを持つ edinet.company（約3,800社）から作る。
            _ds = getattr(bq_client, "dataset", "edinet")
            df = bq_client.query_to_df(
                f"""
                SELECT SUBSTR(sec_code, 1, 4) AS securities_code, name AS company_name
                FROM `{_ds}.company`
                WHERE name IS NOT NULL AND sec_code IS NOT NULL
                  AND NOT IFNULL(is_delisted, FALSE)
                LIMIT 5000
                """
            )
            for _, row in df.iterrows():
                code = str(row.get("securities_code", "") or "")
                name = str(row.get("company_name", "") or "")
                if code and name:
                    normalized = _normalize_company_name(name)
                    if normalized and normalized not in name_map:
                        name_map[normalized] = code
        except Exception:
            pass

    return name_map


# ==============================================================
# BLOCK 3: 名寄せエンジン
# ==============================================================

def match_company_to_code(
    raw_name:   str,
    name_map:   dict[str, str],
    threshold:  float = FUZZY_THRESHOLD,
) -> dict:
    """
    会社名（採択情報原文）を証券コードに名寄せする。

    戦略（優先順）:
      Level 1 完全一致     confidence=1.00
      Level 2 部分一致     confidence=0.85
      Level 3 SequenceMatcher >= threshold  confidence=0.75
      Level 4 不一致       confidence=0.00

    Returns:
        {
          "securities_code":  str,
          "matched_name":     str,
          "match_confidence": float,
          "match_method":     str,
        }
    """
    normalized_target = _normalize_company_name(raw_name)

    if not normalized_target or not name_map:
        return {
            "securities_code":  "",
            "matched_name":     raw_name,
            "match_confidence": 0.0,
            "match_method":     "unmatched",
        }

    # Level 1: 完全一致
    if normalized_target in name_map:
        return {
            "securities_code":  name_map[normalized_target],
            "matched_name":     raw_name,
            "match_confidence": 1.00,
            "match_method":     "exact",
        }

    # Level 2: 部分一致（一方が他方を包含する）
    best_partial: Optional[tuple] = None
    best_partial_len = 0
    for known_name, code in name_map.items():
        if not known_name:
            continue
        # 3文字以下の社名での包含一致は別会社への誤結合が多い
        # （例: 「アトム」が多数の社名に含まれる）。部分一致は4文字以上に限る。
        if min(len(known_name), len(normalized_target)) < 4:
            continue
        if known_name in normalized_target or normalized_target in known_name:
            # 長いマッチを優先（例: "トヨタ"より"トヨタ自動車"を優先）
            match_len = max(len(known_name), len(normalized_target))
            if match_len > best_partial_len:
                best_partial_len = match_len
                best_partial = (code, known_name)

    if best_partial:
        return {
            "securities_code":  best_partial[0],
            "matched_name":     raw_name,
            "match_confidence": 0.85,
            "match_method":     "partial",
        }

    # Level 3: SequenceMatcher ファジーマッチ
    best_ratio   = 0.0
    best_code    = ""
    best_known   = ""
    for known_name, code in name_map.items():
        if not known_name:
            continue
        ratio = SequenceMatcher(
            None, normalized_target, known_name
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_code  = code
            best_known = known_name

    if best_ratio >= threshold:
        return {
            "securities_code":  best_code,
            "matched_name":     raw_name,
            "match_confidence": round(0.75 * best_ratio / 1.0, 3),
            "match_method":     "fuzzy",
        }

    # Level 4: 不一致
    return {
        "securities_code":  "",
        "matched_name":     raw_name,
        "match_confidence": 0.0,
        "match_method":     "unmatched",
    }


# ==============================================================
# BLOCK 4: ページ取得・HTMLパース
# ==============================================================

def _fetch_page(url: str) -> Optional[str]:
    """指定URLのHTMLを取得する。失敗時は None を返す。"""
    try:
        import requests
        resp = requests.get(url, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        print(f"    ⚠️  HTTP {resp.status_code}: {url[:80]}")
        return None
    except ImportError:
        print("    ⚠️  requests が未インストールです（pip install requests）")
        return None
    except Exception as e:
        print(f"    ⚠️  取得失敗: {url[:60]}... ({str(e)[:50]})")
        return None


def _detect_column_idx(headers: list[str], patterns: list[str]) -> int:
    """
    ヘッダーリストから対象列のインデックスを返す。
    該当なしの場合は -1。
    """
    for i, h in enumerate(headers):
        h_norm = unicodedata.normalize("NFKC", str(h)).strip().lower()
        for pat in patterns:
            pat_norm = unicodedata.normalize("NFKC", pat).strip().lower()
            if pat_norm in h_norm or h_norm in pat_norm:
                return i
    return -1


def _infer_project_type(text: str) -> str:
    """URLや事業名のテキストから事業種別を推定する。"""
    for ptype, keywords in _PROJECT_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text.lower():
                return ptype
    return "general"


def _parse_amount(text: str) -> Optional[float]:
    """
    補助金額テキストを百万円単位のfloatに変換する。
    例: "1億2000万円" → 120.0、"3,500百万円" → 3500.0
    """
    try:
        if not text:
            return None
        # 全角→半角
        text = unicodedata.normalize("NFKC", text).strip()
        # 「非開示」「未定」などは None
        if re.search(r"[非未公開]|N/A|TBD", text, re.IGNORECASE):
            return None

        # 億円 + 万円 の組み合わせ
        oku  = re.search(r"([\d,\.]+)\s*億", text)
        man  = re.search(r"([\d,\.]+)\s*万", text)
        hyaku = re.search(r"([\d,\.]+)\s*百万", text)
        total_man = 0.0
        if oku:
            total_man += float(oku.group(1).replace(",", "")) * 10_000
        if man:
            total_man += float(man.group(1).replace(",", ""))
        if hyaku:
            return float(hyaku.group(1).replace(",", ""))
        if total_man > 0:
            return round(total_man / 100, 2)   # 万円 → 百万円

        # 数字のみ（百万円単位想定）
        nums = re.findall(r"[\d,\.]+", text)
        if nums:
            return float(nums[0].replace(",", ""))
    except Exception:
        pass
    return None


def _fetch_bytes(url: str) -> Optional[bytes]:
    """添付PDFなどをバイト列で取得する。失敗時は None。"""
    try:
        import requests
        time.sleep(REQUEST_INTERVAL)
        resp = requests.get(url, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.content
        print(f"    ⚠️  HTTP {resp.status_code}: {url[:80]}")
    except Exception as e:
        print(f"    ⚠️  取得失敗: {url[:60]}... ({str(e)[:50]})")
    return None


def _pdf_text(data: bytes) -> str:
    """PDF本文をテキスト化する（PyMuPDF・requirements 既存）。"""
    try:
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc)
    except Exception:
        return ""


def _extract_org_names(text: str) -> list[str]:
    """本文・PDFテキストから法人格付きの社名を抜き出す。

    NEDO の実施予定先一覧は「株式会社アトム」「KDDI株式会社」のように社名が
    独立した行で並ぶ。テーマ名の途中に句読点で区切られた断片が混ざるため、
    法人格の目印を含む短い断片だけを採る（大学・国研は上場企業の名寄せ対象外）。
    """
    names: list[str] = []
    for line in re.split(r"[\r\n]+", text or ""):
        for part in re.split(r"[、，／]|\s{2,}", line):
            name = part.strip(" 　・●○‐－-:：\t")
            if not name or len(name) > 60:
                continue
            if not any(marker in name for marker in _ORG_MARKERS):
                continue
            # 「A株式会社（幹事）」のような末尾の役割注記を落とす
            name = re.sub(r"[（(][^）()]{0,12}[）)]$", "", name).strip()
            if _is_valid_company_name(name) and name not in names:
                names.append(name)
    return names


def discover_decision_urls(max_pages: int = 20, fetch=None) -> list[str]:
    """公募一覧（状況＝決定）から決定ページのURLを新しい順に集める。"""
    fetch = fetch or _fetch_page
    html = fetch(_NEDO_DECISION_LIST_URL)
    if not html:
        return []
    urls: list[str] = []
    for href in re.findall(r'href="(/koubo/[A-Z]{2}\d_\d{6}\.html)"', html):
        url = urljoin(_NEDO_BASE_URL, href)
        if url not in urls:
            urls.append(url)
    return urls[:max(0, int(max_pages))]


def parse_nedo_decision_page(html: str, source_url: str = "",
                             fetch_bytes=None) -> list[dict]:
    """「実施体制の決定」ページから実施予定先（社名）を抜き出す。

    次の2形式に対応する:
      ① 本文の「N．実施予定先」見出し直下に社名（例: SR3_100022）
      ② 添付PDF「資料1：実施予定先一覧」に表で社名（例: CD3_100434）
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("    ⚠️  beautifulsoup4 が未インストールです（pip install beautifulsoup4）")
        return []
    soup = BeautifulSoup(html or "", "html.parser")
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    title = re.sub(r"^(決定|公募|予告)\s*", "", title)

    date_text = ""
    date_p = soup.find("p", class_="text-right")
    for candidate in (date_p.get_text(" ", strip=True) if date_p else "",
                      soup.get_text(" ", strip=True)):
        m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", candidate)
        if m:
            date_text = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            break
    project_type = _infer_project_type(source_url + " " + title)

    names: list[str] = []
    for heading in soup.find_all(["h2", "h3", "h4"]):
        if not re.search(r"実施予定先|採択先|委託先|助成先", heading.get_text()):
            continue
        chunk = []
        for sib in heading.find_next_siblings():
            if sib.name in ("h2", "h3", "h4"):
                break
            chunk.append(sib.get_text("\n", strip=True))
        names.extend(_extract_org_names("\n".join(chunk)))

    if fetch_bytes is not None:
        for a in soup.find_all("a", href=True):
            label = a.get_text(" ", strip=True)
            if "審査委員" in label or not a["href"].lower().endswith(".pdf"):
                continue
            if not re.search(r"実施予定先|採択(結果|先|事業|テーマ)?(一覧)?", label):
                continue
            data = fetch_bytes(urljoin(source_url or _NEDO_BASE_URL, a["href"]))
            if data:
                names.extend(_extract_org_names(_pdf_text(data)))

    records, seen = [], set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        records.append({
            "raw_company_name": name,
            "project_name":     title[:200],
            "adoption_date":    date_text,
            "project_type":     project_type,
            "support_amount_m": None,
            "source_url":       source_url,
        })
    return records


def parse_nedo_html(
    html:     str,
    source_url: str = "",
) -> list[dict]:
    """
    NEDO採択結果HTMLを解析し、採択情報リストを返す。

    NEDO ページの典型的なテーブル構造:
      <table> <tr><th>機関名</th><th>テーマ名</th><th>採択日</th>...</tr>
              <tr><td>会社A</td><td>事業X</td><td>2024-03-15</td>...</tr>
              ...
      </table>

    Returns:
        [
          {
            "raw_company_name": str,
            "project_name":     str,
            "adoption_date":    str,
            "project_type":     str,
            "support_amount_m": float|None,
            "source_url":       str,
          }, ...
        ]
    """
    records: list[dict] = []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("    ⚠️  beautifulsoup4 が未インストールです（pip install beautifulsoup4）")
        return records

    try:
        soup = BeautifulSoup(html, "html.parser")
        page_title = soup.find("title")
        page_title_text = page_title.get_text(strip=True) if page_title else ""
        project_type = _infer_project_type(source_url + " " + page_title_text)

        # すべての <table> タグを探索
        tables = soup.find_all("table")
        if not tables:
            # テーブルがない場合はリスト要素も試みる
            tables = soup.find_all("dl")

        for table in tables:
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            # ヘッダー行を特定（<th> を含む行、または最初の行）
            header_row = None
            for row in rows:
                if row.find("th"):
                    header_row = row
                    break
            if header_row is None:
                header_row = rows[0]

            headers = [
                th.get_text(strip=True)
                for th in header_row.find_all(["th", "td"])
            ]
            if not headers:
                continue

            # 各列のインデックスを検出
            company_idx = _detect_column_idx(headers, _COMPANY_HEADER_PATTERNS)
            date_idx    = _detect_column_idx(headers, _DATE_HEADER_PATTERNS)
            project_idx = _detect_column_idx(headers, _PROJECT_HEADER_PATTERNS)
            amount_idx  = _detect_column_idx(headers, _AMOUNT_HEADER_PATTERNS)

            if company_idx < 0:
                continue  # 会社名列が見つからないテーブルはスキップ

            # データ行の解析
            data_rows = [r for r in rows if r != header_row]
            for row in data_rows:
                cells = [td.get_text(strip=True)
                         for td in row.find_all(["td", "th"])]
                if not cells or len(cells) <= company_idx:
                    continue

                raw_name = cells[company_idx].strip()
                if not _is_valid_company_name(raw_name):
                    continue

                # 複数機関が「、」「/」区切りで記載されている場合は分割
                if "、" in raw_name or "／" in raw_name:
                    sub_names = re.split(r"[、／]", raw_name)
                else:
                    sub_names = [raw_name]

                for sub_name in sub_names:
                    sub_name = sub_name.strip()
                    if not _is_valid_company_name(sub_name):
                        continue

                    project_name = (
                        cells[project_idx].strip()
                        if project_idx >= 0 and len(cells) > project_idx
                        else page_title_text
                    )
                    adoption_date = (
                        cells[date_idx].strip()
                        if date_idx >= 0 and len(cells) > date_idx
                        else ""
                    )
                    amount_text = (
                        cells[amount_idx].strip()
                        if amount_idx >= 0 and len(cells) > amount_idx
                        else ""
                    )

                    # 日付の正規化（YYYY-MM-DD形式に変換）
                    adoption_date_normalized = _normalize_date(adoption_date)

                    records.append({
                        "raw_company_name": sub_name,
                        "project_name":     project_name[:200],
                        "adoption_date":    adoption_date_normalized,
                        "project_type":     project_type,
                        "support_amount_m": _parse_amount(amount_text),
                        "source_url":       source_url,
                    })

    except Exception as e:
        print(f"    ⚠️  HTML解析エラー（{source_url[:50]}）: {str(e)[:80]}")

    return records


def _normalize_date(date_str: str) -> str:
    """
    日付文字列をYYYY-MM-DD形式に正規化する。
    変換できない場合は空文字列を返す。
    """
    if not date_str:
        return ""
    try:
        text = unicodedata.normalize("NFKC", date_str).strip()
        # 「令和X年M月D日」パターン
        reiwa = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
        if reiwa:
            y = int(reiwa.group(1)) + 2018
            return f"{y}-{int(reiwa.group(2)):02d}-{int(reiwa.group(3)):02d}"
        # 「2024年3月15日」パターン
        jp = re.search(r"(\d{4})\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
        if jp:
            return (f"{int(jp.group(1))}-"
                    f"{int(jp.group(2)):02d}-"
                    f"{int(jp.group(3)):02d}")
        # 「2024/03/15」「2024-03-15」パターン
        slash = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
        if slash:
            return (f"{int(slash.group(1))}-"
                    f"{int(slash.group(2)):02d}-"
                    f"{int(slash.group(3)):02d}")
    except Exception:
        pass
    return ""


# ==============================================================
# BLOCK 5: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / CACHE_FILENAME


def _load_cache(cfg) -> Optional[dict]:
    """TTL内のキャッシュを返す。失効・未存在の場合は None。"""
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = data.get("cached_at", "")
        if cached_at:
            age = (datetime.now() -
                   datetime.fromisoformat(cached_at)).days
            if age < CACHE_TTL_DAYS:
                return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    try:
        data["cached_at"] = datetime.now().isoformat()
        p = _cache_path(cfg)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [NEDO] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 6: BQ格納用レコード変換
# ==============================================================

def _build_bq_rows(
    raw_records: list[dict],
    name_map:    dict[str, str],
) -> list[dict]:
    """
    パース済み採択情報に名寄せを適用してBQ格納用rowsを返す。

    Returns:
        BQ `edinet.policy_support` テーブル互換のリスト
    """
    rows  = []
    now_utc = datetime.now(timezone.utc).isoformat()

    for rec in raw_records:
        try:
            match_result = match_company_to_code(
                rec["raw_company_name"], name_map
            )
            rows.append({
                "securities_code":  match_result["securities_code"],
                "company_name":     (
                    match_result["matched_name"]
                    if match_result["match_confidence"] >= 0.75
                    else rec["raw_company_name"]
                ),
                "raw_company_name": rec["raw_company_name"],
                "project_name":     rec.get("project_name", ""),
                "adoption_date":    rec.get("adoption_date", ""),
                "project_type":     rec.get("project_type", "general"),
                "support_org":      "NEDO",
                "support_amount_m": rec.get("support_amount_m"),
                "source_url":       rec.get("source_url", ""),
                "scraped_at":       now_utc,
                "match_confidence": match_result["match_confidence"],
                "match_method":     match_result["match_method"],
            })
        except Exception:
            continue

    return rows


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_nedo_selection_scraper(
    cfg,
    results:    dict = None,
    bq_client=None,
    force:      bool = False,
) -> dict:
    """
    stock_analyzer_main.py / 独立実行 から呼ばれるエントリポイント。

    処理フロー:
      1. キャッシュ確認（TTL内なら即返却、force=True で強制再取得）
      2. NEDO採択URLリストを取得（cfg.NEDO_ADOPTION_URLS or デフォルト）
      3. 各URLをスクレイプしてHTMLパース → 採択情報リスト構築
      4. results["screener"] + BQ から会社名マップを構築
      5. 名寄せ（4段階ストラテジー）
      6. BQ `edinet.policy_support` に格納（bq_client が渡された場合のみ）
      7. キャッシュ保存して結果を返す

    Args:
        cfg:        config モジュール
        results:    run_all_tasks() の results dict（名寄せ用データ取得）
        bq_client:  EDINETBQClient インスタンス（None 可、省略時はBQ書き込みなし）
        force:      True でキャッシュを無視して強制再取得

    Returns:
        {
          "rows":              list,   # BQ格納済みレコード
          "scraped_count":     int,    # スクレイプ件数
          "matched_count":     int,    # 名寄せ成功件数（confidence >= 0.75）
          "unmatched_count":   int,    # 名寄せ失敗件数
          "bq_inserted":       bool,   # BQ格納成功フラグ
          "urls_attempted":    int,    # スクレイプ試行URL数
          "urls_succeeded":    int,    # スクレイプ成功URL数
          "cached":            bool,
          "timestamp":         str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🏛️  NEDO採択情報スクレイパー（Wave 1 - I-2）実行中...")

    # ── キャッシュ確認 ───────────────────────────────────────────
    if not force:
        cached = _load_cache(cfg)
        if cached:
            n_rows = len(cached.get("rows", []))
            n_mat  = cached.get("matched_count", 0)
            print(f"    ✅ キャッシュ使用: {n_rows}件（名寄せ成功:{n_mat}件）")
            cached["cached"] = True
            return cached

    # ── URLリストの取得 ──────────────────────────────────────────
    url_list: list[str] = list(getattr(cfg, "NEDO_ADOPTION_URLS", None) or [])
    if not url_list:
        url_list = discover_decision_urls(int(getattr(cfg, "NEDO_DECISION_MAX_PAGES", 20)))
    print(f"    📡 対象URL: {len(url_list)}件")

    # ── スクレイピング ────────────────────────────────────────────
    all_raw_records: list[dict] = []
    urls_succeeded  = 0

    for url in url_list:
        try:
            print(f"    🌐 取得中: {url[:70]}...")
            html = _fetch_page(url)
            if html:
                page_records = parse_nedo_decision_page(
                    html, source_url=url, fetch_bytes=_fetch_bytes)
                if not page_records:          # 表形式の旧ページ向けの解析に落とす
                    page_records = parse_nedo_html(html, source_url=url)
                all_raw_records.extend(page_records)
                urls_succeeded += 1
                print(
                    f"       → {len(page_records)}件の採択情報を取得"
                )
            time.sleep(REQUEST_INTERVAL)
        except Exception as e:
            print(f"    ⚠️  URL処理エラー ({url[:50]}...): {str(e)[:60]}")
            continue

    print(f"    📋 スクレイプ合計: {len(all_raw_records)}件")

    if not all_raw_records:
        print("    ⚠️  採択情報が取得できませんでした（HTML構造が変更された可能性）")
        result = {
            "rows": [], "scraped_count": 0,
            "matched_count": 0, "unmatched_count": 0,
            "bq_inserted": False,
            "urls_attempted": len(url_list), "urls_succeeded": urls_succeeded,
            "cached": False,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return result

    # ── 会社名マップ構築 ─────────────────────────────────────────
    print("    🔤 会社名マップを構築中...")
    name_map = _build_company_name_map(
        results or {},
        bq_client,
    )
    print(f"    ✅ 名寄せ辞書: {len(name_map)}社登録")

    # ── 名寄せ + BQ形式変換 ──────────────────────────────────────
    bq_rows = _build_bq_rows(all_raw_records, name_map)
    matched_count   = sum(
        1 for r in bq_rows if r["match_confidence"] >= 0.75
    )
    unmatched_count = len(bq_rows) - matched_count

    print(
        f"    🔗 名寄せ結果: "
        f"成功{matched_count}件 / 未一致{unmatched_count}件"
    )

    # 名寄せ成功した銘柄のサマリー表示（上位5件）
    matched = [r for r in bq_rows if r["match_confidence"] >= 0.75]
    for r in matched[:5]:
        print(
            f"       [{r['match_method']}:{r['match_confidence']:.2f}] "
            f"{r['raw_company_name'][:20]} → "
            f"{r['securities_code']} "
            f"({r['project_type']})"
        )

    # ── BigQuery 格納 ────────────────────────────────────────────
    bq_inserted = False
    if bq_client is not None and bq_rows:
        try:
            # 週次で同じ決定ページを取り直すため、追記（insert）だと
            # 同じ採択が毎週重複して積まれボーナスが水増しされる。出典URL×社名で冪等に MERGE。
            bq_inserted = bq_client.merge_rows(
                "policy_support", bq_rows, ["source_url", "raw_company_name"]
            )
            if bq_inserted:
                print(
                    f"    ✅ BQ `edinet.policy_support` 格納: "
                    f"{len(bq_rows)}件"
                )
            else:
                print("    ⚠️  BQ格納失敗（insert_rows がエラーを返しました）")
        except Exception as _bqe:
            print(f"    ⚠️  BQ格納エラー（スキップ）: {str(_bqe)[:80]}")

    # ── キャッシュ保存 ───────────────────────────────────────────
    result = {
        "rows":            bq_rows,
        "scraped_count":   len(all_raw_records),
        "matched_count":   matched_count,
        "unmatched_count": unmatched_count,
        "bq_inserted":     bq_inserted,
        "urls_attempted":  len(url_list),
        "urls_succeeded":  urls_succeeded,
        "cached":          False,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_cache(cfg, result)

    print(
        f"  ✅ NEDO採択スクレイパー完了: "
        f"{len(all_raw_records)}件スクレイプ / "
        f"名寄せ成功{matched_count}件 / "
        f"BQ格納{'✅' if bq_inserted else '⏩'}"
    )
    return result


# ==============================================================
# BLOCK 8: 単体テスト
# ==============================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("  🏛️  nedo_selection_scraper.py v1.0 単体テスト")
    print("=" * 60)

    # ── 名前正規化テスト ─────────────────────────────────────────
    print("\n  【名前正規化テスト】")
    test_names = [
        "トヨタ自動車株式会社",
        "株式会社本田技術研究所",
        "ＳＯＮＹ株式会社",
        "パナソニック（株）",
        "国立大学法人東京大学",
        "一般社団法人環境技術研究所",
    ]
    for name in test_names:
        print(f"    '{name}' → '{_normalize_company_name(name)}'")

    # ── 名寄せテスト ─────────────────────────────────────────────
    print("\n  【名寄せテスト（ダミーマップ）】")
    dummy_map = {
        "トヨタ自動車":  "7203",
        "ソニーグループ": "6758",
        "パナソニックホールディングス": "6752",
        "日立製作所":    "6501",
        "東京エレクトロン": "8035",
    }
    test_targets = [
        "トヨタ自動車株式会社",          # Level 1 完全一致（正規化後）
        "ＳＯＮＹ株式会社",             # Level 2 部分一致
        "パナソニック（株）",            # Level 3 ファジー
        "存在しない会社株式会社",         # Level 4 不一致
    ]
    for target in test_targets:
        result = match_company_to_code(target, dummy_map)
        print(
            f"    '{target[:20]}' → "
            f"code={result['securities_code'] or '(未一致)'} "
            f"({result['match_method']}: {result['match_confidence']:.2f})"
        )

    # ── 日付正規化テスト ─────────────────────────────────────────
    print("\n  【日付正規化テスト】")
    test_dates = [
        "令和6年3月15日", "2024年3月15日", "2024/03/15",
        "2024-3-5", "非公開",
    ]
    for d in test_dates:
        print(f"    '{d}' → '{_normalize_date(d)}'")

    # ── 補助金額パーステスト ─────────────────────────────────────
    print("\n  【補助金額パーステスト】")
    test_amounts = [
        "1億2000万円", "3,500百万円", "非開示", "約500万円", "",
    ]
    for a in test_amounts:
        print(f"    '{a}' → {_parse_amount(a)}")

    print("\n  ✅ 単体テスト完了（実際のNEDOページアクセスなし）")
