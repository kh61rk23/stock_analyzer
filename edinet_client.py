# ==============================================================
# edinet_client.py
# EDINET API v2 クライアント v1.0
# ==============================================================
# 【役割】
#   EDINET API v2 から有価証券報告書を取得し、
#   「事業のリスク」セクションを自動抽出する。
#
# 【依存】
#   config.py のみ（stock_analyzer_main.py は import しない）
#
# 【EDINET API v2 仕様】
#   書類一覧 API:
#     GET https://disclosure.edinet-api.go.jp/api/v2/documents.json
#         ?date=YYYY-MM-DD&type=2&Subscription-Key=<KEY>
#   書類取得 API:
#     GET https://disclosure.edinet-api.go.jp/api/v2/documents/{docID}
#         ?type=1  (ZIP: XBRL+HTML)
#
# 【API キーの取得方法】
#   1. https://disclosure.edinet-api.go.jp/ にアクセス
#   2. 「API 利用登録」から無料登録（メールアドレスのみ）
#   3. 発行されたキーを config.py の EDINET_API_KEY に設定
#   ※ キー未設定の場合は取得をスキップして空リストを返す
#
# 【キャッシュ設計】
#   同一企業の有報 HTML は cfg.DATA_DIR / "edinet_cache" に保存。
#   同一日に2回以上取得してもネットワーク通信は発生しない。
# ==============================================================

import re
import json
import time
import zipfile
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: EDINET 書類一覧取得
# ==============================================================

def _get_headers(api_key: str) -> dict:
    """Subscription-Key 付きヘッダーを返す"""
    h = {"User-Agent": "StockAnalyzer/1.0 (research)"}
    if api_key:
        h["Subscription-Key"] = api_key
    return h


def search_annual_report(
    sec_code:  str,
    api_key:   str,
    lookback_days: int = 400,
) -> Optional[dict]:
    """
    証券コード（例: "8306"）で有価証券報告書（docTypeCode="120"）を検索し、
    最新 1 件の書類情報を返す。

    Args:
        sec_code     : "8306" 形式（.T を除いたもの）
        api_key      : EDINET API Subscription-Key
        lookback_days: 何日前まで遡って検索するか（デフォルト 400 日）

    Returns:
        dict | None: EDINET 書類情報（docID, filerName, periodEnd 等）
                     見つからない場合は None
    """
    try:
        import requests
    except ImportError:
        return None

    headers = _get_headers(api_key)
    base    = "https://disclosure.edinet-api.go.jp/api/v2/documents.json"

    # 直近 lookback_days 日を 5 日刻みで検索（全日取得すると時間がかかる）
    for days_ago in range(0, lookback_days, 5):
        target_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        params = {"date": target_date, "type": "2"}
        if api_key:
            params["Subscription-Key"] = api_key

        try:
            resp = requests.get(base, params=params, headers=headers, timeout=15)
            if resp.status_code == 400:
                # date が未来または無効
                continue
            if resp.status_code != 200:
                continue

            data = resp.json()
            results = data.get("results", [])

            for doc in results:
                if doc.get("docTypeCode") != "120":   # 120 = 有価証券報告書
                    continue
                code = str(doc.get("secCode", "")).rstrip("0")  # "83060" → "8306"
                if code == sec_code:
                    return doc

        except Exception:
            time.sleep(1)
            continue

    return None


# ==============================================================
# BLOCK 2: 書類 ZIP 取得と HTML 抽出
# ==============================================================

def fetch_document_html(
    doc_id:   str,
    api_key:  str,
    cache_dir: Optional[Path] = None,
) -> Optional[str]:
    """
    EDINET から書類 ZIP を取得し、有報 HTML 全文を返す。
    cache_dir が指定されていれば、取得済みファイルをキャッシュする。

    Args:
        doc_id    : EDINET 書類 ID（例: "S100XXXX"）
        api_key   : Subscription-Key
        cache_dir : キャッシュ保存先ディレクトリ

    Returns:
        str | None: HTML テキスト全文
    """
    # キャッシュ確認
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{doc_id}.html"
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="replace")

    try:
        import requests
    except ImportError:
        return None

    url = f"https://disclosure.edinet-api.go.jp/api/v2/documents/{doc_id}"
    params = {"type": "1"}  # type=1: 書類 ZIP（XBRL + HTML）
    if api_key:
        params["Subscription-Key"] = api_key

    headers = _get_headers(api_key)

    try:
        resp = requests.get(url, params=params, headers=headers,
                            timeout=60, stream=True)
        if resp.status_code != 200:
            return None

        # ZIP を解凍して有報 HTML ファイルを特定
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        html_text = _extract_main_html(zf)

        # キャッシュ保存
        if html_text and cache_dir:
            cache_path.write_text(html_text, encoding="utf-8")

        return html_text

    except Exception as e:
        print(f"    ⚠️  EDINET 書類取得失敗 ({doc_id}): {str(e)[:60]}")
        return None


def _extract_main_html(zf: "zipfile.ZipFile") -> Optional[str]:
    """
    ZIP 内の有報本体 HTML を特定して返す。
    EDINET ZIP の命名規則: XBRL/ と PublicDoc/ の 2 フォルダ構成。
    PublicDoc/ 内の最大サイズ HTML が有報本体。
    """
    candidates = [
        name for name in zf.namelist()
        if name.endswith(".htm") and "PublicDoc" in name
        and not name.endswith("_ixy.htm")   # インラインXBRLを除外
    ]
    if not candidates:
        # フォールバック: .htm 拡張子の最大ファイル
        candidates = [n for n in zf.namelist() if n.endswith(".htm")]

    if not candidates:
        return None

    # 最大サイズのファイルを本体と判定
    main = max(candidates, key=lambda n: zf.getinfo(n).file_size)

    try:
        raw = zf.read(main)
        for enc in ["utf-8", "shift_jis", "cp932"]:
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


# ==============================================================
# BLOCK 3: 「事業のリスク」セクション抽出
# ==============================================================

# 有報の「事業のリスク」セクション見出しパターン（複数の表記に対応）
_RISK_SECTION_PATTERNS = [
    r"事業等のリスク",
    r"事業のリスク",
    r"リスク要因",
    r"Risk\s+Factors",
]

_NEXT_SECTION_PATTERNS = [
    r"経営者による財政状態",
    r"財政状態、経営成績及びキャッシュ・フローの状況",
    r"経営上の重要な契約等",
    r"MD&A",
    r"第\s*4\s*経営者による",
]


def extract_risk_section(html: str) -> str:
    """
    有報 HTML から「事業等のリスク」セクションのテキストを抽出する。

    Returns:
        str: 抽出されたテキスト（最大 6000 字）。見つからない場合は空文字。
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n")
    except ImportError:
        # BeautifulSoup がない場合は正規表現で簡易除去
        text = re.sub(r"<[^>]+>", "", html)

    return _extract_from_text(text)


def _extract_from_text(text: str) -> str:
    """
    プレーンテキストから「事業等のリスク」セクションを正規表現で切り出す。
    """
    # セクション開始位置を検索
    start_pos = -1
    for pattern in _RISK_SECTION_PATTERNS:
        m = re.search(pattern, text)
        if m:
            start_pos = m.start()
            break

    if start_pos < 0:
        return ""

    # セクション終了位置（次のセクション見出し）を検索
    section_text = text[start_pos: start_pos + 8000]
    end_pos = len(section_text)

    for pattern in _NEXT_SECTION_PATTERNS:
        m = re.search(pattern, section_text[200:])  # 見出し自体をスキップ
        if m:
            end_pos = min(end_pos, 200 + m.start())

    extracted = section_text[:end_pos].strip()

    # 空白・改行の正規化
    extracted = re.sub(r"\n{3,}", "\n\n", extracted)
    extracted = re.sub(r"[ \t]{2,}", " ", extracted)

    return extracted[:6000]  # 最大 6000 字


# ==============================================================
# BLOCK 4: リスク要因の箇条書き抽出
# ==============================================================

def parse_risk_items(raw_text: str, max_items: int = 5) -> list:
    """
    抽出された「事業のリスク」テキストから
    リスク要因を箇条書きリスト（list of str）に変換する。

    抽出ロジック:
      1. 「①②③…」「（1）（2）…」「・」などの列挙パターンで分割
      2. 各項目の冒頭 120 字を取得
      3. max_items 件に絞る

    Returns:
        list of str: リスク要因のテキストリスト（最大 max_items 件）
    """
    if not raw_text:
        return []

    # 列挙パターンで分割
    patterns = [
        r"(?:①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩)",
        r"(?:\（[0-9１-９]+\）|\([0-9]+\))",
        r"(?:^[0-9]+[．.。])",
        r"(?:^[■●▶▸])",
        r"(?:^・(?!・))",
    ]

    items = []
    combined = "|".join(patterns)
    parts = re.split(combined, raw_text, flags=re.MULTILINE)

    for part in parts[1:]:  # 最初の要素は見出し部分なのでスキップ
        cleaned = part.strip()
        if len(cleaned) < 15:   # 短すぎるものは除外
            continue
        # 最初の 120 字（改行があればそこまで）
        first_line = cleaned.split("\n")[0][:120].strip()
        if first_line:
            items.append(first_line)
        if len(items) >= max_items:
            break

    # 列挙パターンが見つからない場合は段落で分割
    if not items:
        paragraphs = [p.strip() for p in raw_text.split("\n\n") if len(p.strip()) > 20]
        for para in paragraphs[1:max_items + 1]:
            items.append(para[:120].strip())

    return items[:max_items]


# ==============================================================
# BLOCK 5: メインエントリポイント（ticker → リスクリスト）
# ==============================================================

def get_risk_factors(
    ticker:    str,
    cfg,
) -> list:
    """
    yfinance 形式のティッカー（例: "8306.T"）を受け取り、
    EDINET から取得した「事業のリスク」の箇条書きリストを返す。

    Args:
        ticker : "8306.T" 形式
        cfg    : config モジュール（EDINET_API_KEY, DATA_DIR を参照）

    Returns:
        list of str: リスク要因リスト（最大 cfg.EDINET_MAX_RISK_ITEMS 件）
                     APIキー未設定・取得失敗時は []
    """
    api_key = getattr(cfg, "EDINET_API_KEY", "")

    if not api_key:
        return []   # APIキー未設定 → スキップ（エラーにしない）

    # 証券コードに変換（"8306.T" → "8306"）
    sec_code = ticker.replace(".T", "").replace(".OS", "").strip()

    print(f"      📄 EDINET 検索中: {ticker} (証券コード: {sec_code})")

    # 書類検索
    doc_info = search_annual_report(
        sec_code,
        api_key,
        lookback_days=getattr(cfg, "EDINET_LOOKBACK_DAYS", 400),
    )
    if not doc_info:
        print(f"      ⚠️  {ticker}: 有報が見つかりませんでした")
        return []

    doc_id      = doc_info.get("docID", "")
    filer_name  = doc_info.get("filerName", ticker)
    period_end  = doc_info.get("periodEnd", "")
    print(f"      📋 {filer_name} ({period_end}) docID={doc_id}")

    # キャッシュディレクトリ
    cache_dir = getattr(cfg, "DATA_DIR", None)
    if cache_dir:
        cache_dir = Path(cache_dir) / "edinet_cache"

    # HTML 取得
    html = fetch_document_html(doc_id, api_key, cache_dir)
    if not html:
        return []

    # リスクセクション抽出
    raw_text = extract_risk_section(html)
    if not raw_text:
        print(f"      ⚠️  {ticker}: 「事業のリスク」セクションが抽出できませんでした")
        return []

    max_items = getattr(cfg, "EDINET_MAX_RISK_ITEMS", 5)
    items     = parse_risk_items(raw_text, max_items)
    print(f"      ✅ {ticker}: リスク要因 {len(items)} 件抽出")
    return items


def get_sector_risk_factors(
    tickers: list,
    cfg,
    max_companies: int = 2,
) -> dict:
    """
    複数銘柄のリスク要因を取得する。
    実行時間が長くなるため max_companies で上位 N 社に限定する。

    Returns:
        dict: {ticker: [リスク要因リスト]}
    """
    results = {}
    api_key = getattr(cfg, "EDINET_API_KEY", "")

    if not api_key:
        return results

    for ticker in tickers[:max_companies]:
        try:
            risks = get_risk_factors(ticker, cfg)
            if risks:
                results[ticker] = risks
            time.sleep(1)   # API レート制限対策
        except Exception as e:
            print(f"      ⚠️  {ticker} EDINET エラー: {str(e)[:60]}")
            continue

    return results


# ==============================================================
# BLOCK 6: リスクテキスト Markdown 整形
# ==============================================================

def format_risk_markdown(
    sector_name:  str,
    risk_by_ticker: dict,
    ticker_to_name: dict,
) -> str:
    """
    {ticker: [リスク要因リスト]} を Markdown セクションに整形する。

    Args:
        sector_name    : 業種名（表示用）
        risk_by_ticker : get_sector_risk_factors() の返り値
        ticker_to_name : {ticker: 企業名} の辞書（表示用名称）

    Returns:
        str: Markdown テキスト
    """
    if not risk_by_ticker:
        return ""

    lines = [
        "",
        "### 有報「事業のリスク」（EDINET 自動取得）",
        "",
    ]
    for ticker, risks in risk_by_ticker.items():
        name = ticker_to_name.get(ticker, ticker)
        lines.append(f"**{name}** (`{ticker}`)")
        for risk in risks:
            lines.append(f"- {risk}")
        lines.append("")

    return "\n".join(lines)


# ==============================================================
# 単体テスト用
# ==============================================================

if __name__ == "__main__":
    import importlib.util, sys
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break

    print("=== EDINET クライアント 単体テスト ===")
    print(f"API キー設定: {'あり' if getattr(cfg,'EDINET_API_KEY','') else 'なし（スキップ）'}")

    risks = get_risk_factors("8306.T", cfg)
    if risks:
        print(f"\n三菱UFJ 事業のリスク ({len(risks)} 件):")
        for r in risks:
            print(f"  - {r}")
    else:
        print("リスク取得なし（APIキー未設定 or 取得失敗）")
