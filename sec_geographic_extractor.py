# ==============================================================
# sec_geographic_extractor.py  v1.0  (G-C1)
# ==============================================================
# 【役割】
#   SEC EDGAR の 10-K（年次報告書）から Geographic Segments /
#   Geographic Areas / Risk Factors セクションを抽出し、
#   企業の国別売上エクスポージャーを
#   geopolitics.company_country_exposure テーブルに格納する。
#
# 【対象】
#   IMPORTANT_TICKERS_US に定義された米国株ティッカー
#   + run 時に追加指定された ticker
#
# 【データフロー】
#   ① SEC EDGAR Full-Text Search API で最新 10-K の accession_number 取得
#   ② EDGAR Viewer API で 10-K のセクション一覧を取得
#   ③ Geographic / Risk セクションのテキストを抽出
#   ④ 正規表現 + キーワードマッチングで国別売上比率を推定
#   ⑤ geopolitics.company_country_exposure に MERGE
#
# 【SEC EDGAR API エンドポイント】
#   会社情報検索:
#     https://efts.sec.gov/LATEST/search-index?q="{ticker}"&dateRange=custom
#         &startdt={Y-1}-01-01&enddt={Y}-12-31&forms=10-K
#   Filing 詳細:
#     https://data.sec.gov/submissions/CIK{cik:010d}.json
#   XBRL インラインビューア:
#     https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany
#         &CIK={cik}&type=10-K&dateb=&owner=include&count=1
#
# 【地理セグメント抽出ロジック】
#   10-K テキストから以下のパターンを検索:
#   a) "United States: $X billion" 形式の数値テーブル
#   b) "X% of revenue was generated outside the US" 形式の文章
#   c) "Geographic breakdown" テーブル（HTML/XBRL）
#
# 【依存】
#   edinet_bq_client.py / config.py / jogmec_scraper.py（BQ MERGE共有）
# ==============================================================

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import sec_client

# ==============================================================
# BLOCK 1: SEC EDGAR API 定数
# ==============================================================

EDGAR_SUBMISSIONS_BASE = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EDGAR_SEARCH_BASE      = (
    "https://efts.sec.gov/LATEST/search-index"
    "?q=%22{ticker}%22&forms=10-K&dateRange=custom"
    "&startdt={start}&enddt={end}"
)
EDGAR_FILING_DOC_BASE  = (
    "https://www.sec.gov/Archives/edgar/full-index/"
    "{year}/QTR{qtr}/company.idx"
)
EDGAR_VIEWER_BASE      = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&CIK={cik}&type=10-K"
    "&dateb=&owner=include&count=1&search_text="
)
EDGAR_DOC_BASE = "https://www.sec.gov"

# U-2 / F-2: SECは連絡先を含まないUser-Agentを403で拒否する。
# us_8k_monitor.py:116-122 の _sec_user_agent() と同型（優先順: 環境変数 →
# cfg.SEC_USER_AGENT（cfg未指定時は import config でのフォールバック取得）→ 固定文字列）。
# U-1b: 実体は sec_client.user_agent() に統合済み。本関数は tests/test_sec_user_agent.py
# の既存回帰テストとの互換のために薄いラッパーとして残す。
_SEC_UA_FALLBACK = "StockAnalyzer stockanalyzer@example.com"


def _sec_user_agent(cfg=None) -> str:
    return sec_client.user_agent(cfg)


from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B5_SEC_GEOGRAPHIC_EXTRACTOR_FETCH_TIMEOUT': '_FETCH_TIMEOUT',
    'B6_SEC_GEOGRAPHIC_EXTRACTOR_MAX_RETRIES': '_MAX_RETRIES',
}

# U-1b: レート制限(旧 _SEC_RATE_LIMIT)は sec_client.get() の共通スロットル
# （RATE_LIMIT_RPS）に、429/5xxリトライ待機(旧 _RETRY_WAIT_SEC 固定6秒)は
# sec_client.get() の指数バックオフ（3s/6s/12s）にそれぞれ統合された。
# _FETCH_TIMEOUT=30秒は sec_client.get() の既定15秒より長い（XBRL等の
# 特大レスポンス向け）ため、呼び出し側で明示的に timeout=_FETCH_TIMEOUT を渡す。
_FETCH_TIMEOUT   = 30
_MAX_RETRIES     = 3

# ==============================================================
# BLOCK 2: ティッカー→CIK 変換マスター（主要銘柄）
# ==============================================================
# SEC EDGAR はティッカーではなく CIK（Central Index Key）で管理する。
# IMPORTANT_TICKERS_US の全銘柄を事前に登録しておく。

TICKER_TO_CIK: dict[str, str] = {
    # エネルギー
    "XOM":   "0000034088",
    "CVX":   "0000093410",
    "COP":   "0001163165",
    "EOG":   "0000821189",
    "SLB":   "0000087347",
    # 素材・資源
    "FCX":   "0000831259",
    "NEM":   "0001164727",
    "AA":    "0000004281",
    "MP":    "0001801170",
    "ALB":   "0000915779",
    # テック
    "NVDA":  "0001045810",
    "QCOM":  "0000804328",
    "AMAT":  "0000796343",
    "LRCX":  "0000707549",
    "ASML":  "0000937556",
    # その他主要銘柄（追加が必要な場合は拡張）
    "AAPL":  "0000320193",
    "MSFT":  "0000789019",
    "GOOGL": "0001652044",
    "AMZN":  "0001018724",
    "META":  "0001326801",
    "TSLA":  "0001318605",
    "INTC":  "0000050863",
    "AMD":   "0000002488",
    "MU":    "0000723125",
    "TXN":   "0000097476",
}

# 国名キーワード → ISO 3166-1 alpha-3（英語表記・10-K頻出パターン）
GEO_KEYWORD_TO_CODE: dict[str, str] = {
    # 主要地域表記
    "United States":          "USA",
    "U.S.":                   "USA",
    "Americas":               "USA",   # 主にUS扱い（厳密には地域）
    "China":                  "CHN",
    "Greater China":          "CHN",
    "Japan":                  "JPN",
    "South Korea":            "KOR",
    "Korea":                  "KOR",
    "Taiwan":                 "TWN",
    "Europe":                 "DEU",   # 欧州はドイツで代表（近似）
    "Germany":                "DEU",
    "France":                 "FRA",
    "United Kingdom":         "GBR",
    "UK":                     "GBR",
    "Netherlands":            "NLD",
    "Rest of Europe":         "DEU",
    "Asia Pacific":           "CHN",   # APAC は中国で代表（近似）
    "Asia-Pacific":           "CHN",
    "Southeast Asia":         "SGP",
    "India":                  "IND",
    "Middle East":            "SAU",   # 中東はサウジで代表（近似）
    "Africa":                 "ZAF",
    "Latin America":          "BRA",
    "Brazil":                 "BRA",
    "Mexico":                 "MEX",
    "Canada":                 "CAN",
    "Australia":              "AUS",
    "Singapore":              "SGP",
    "Malaysia":               "MYS",
    "Indonesia":              "IDN",
    "Russia":                 "RUS",
    "Saudi Arabia":           "SAU",
    "Israel":                 "ISR",
}

# exposure_type の推定ロジック（セクション種別）
_SECTION_TO_EXPOSURE_TYPE = {
    "geographic_segments": "SALES_NETWORK",
    "geographic_areas":    "SALES_NETWORK",
    "risk_factors":        "SALES_NETWORK",   # リスクセクションは売上依存として扱う
    "operations":          "PRODUCTION_FACILITY",
}

# ==============================================================
# BLOCK 3: HTTP ユーティリティ（SEC EDGAR 専用・sec_client 経由）
# ==============================================================

def _fetch_sec(url: str, cfg=None, as_json: bool = False) -> Any:
    """
    SEC EDGAR API へのリクエスト（sec_client.get()/get_json() 経由）。

    レート制限・429/5xxリトライは sec_client 側の共通実装に委譲する。
    タイムアウトのみ本ファイル独自の _FETCH_TIMEOUT（30秒。XBRL等の
    特大レスポンス向け）を明示的に渡し、sec_client の既定15秒を上書きする。

    Args:
        url      : リクエスト URL
        cfg      : config モジュール（User-Agent / レート制限等の解決に使用）
        as_json  : True の場合 JSON パースして返す

    Returns:
        str（as_json=False）/ dict または list（as_json=True）/ None（失敗）
    """
    accept = "application/json, text/html, */*"
    if as_json:
        return sec_client.get_json(
            url, cfg=cfg, accept=accept,
            timeout=_FETCH_TIMEOUT, max_retries=_MAX_RETRIES,
        )

    resp = sec_client.get(
        url, cfg=cfg, accept=accept,
        timeout=_FETCH_TIMEOUT, max_retries=_MAX_RETRIES,
    )
    if resp is None:
        return None
    return resp.content.decode("utf-8", errors="replace")


# ==============================================================
# BLOCK 4: EDGAR Submissions API（CIK → 最新 10-K ファイリング取得）
# ==============================================================

def _get_latest_10k_url(cik: str, cfg=None) -> tuple[str | None, str | None]:
    """
    EDGAR Submissions API から最新 10-K の
    (accession_number, primary_document_url) を取得する。

    Args:
        cik: CIK番号（ゼロパディング10桁）
        cfg: config モジュール

    Returns:
        (accession_number, document_url) or (None, None)
    """
    # CIK の数値部分を抽出（ゼロパディング除去）
    cik_num = int(cik.lstrip("0") or "0")
    url = EDGAR_SUBMISSIONS_BASE.format(cik=cik_num)

    data = _fetch_sec(url, cfg=cfg, as_json=True)
    if not data:
        return None, None

    try:
        filings = data.get("filings", {}).get("recent", {})
        forms   = filings.get("form", [])
        acc_nos = filings.get("accessionNumber", [])
        docs    = filings.get("primaryDocument", [])
        dates   = filings.get("filingDate", [])

        # 最新の 10-K を検索
        for i, form in enumerate(forms):
            if form in ("10-K", "10-K/A"):
                acc_no  = acc_nos[i].replace("-", "")
                doc_name = docs[i]
                cik_pad  = str(cik_num).zfill(10)
                doc_url  = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik_num}/{acc_no}/{doc_name}"
                )
                return acc_nos[i], doc_url

    except Exception as e:
        print(f"    ⚠️  10-K URL 解析失敗: {str(e)[:60]}")

    return None, None


# ==============================================================
# BLOCK 5: 10-K テキスト解析（地理セグメント抽出）
# ==============================================================

def _narrow_to_target_sections(text: str, cfg=None) -> str:
    """config.GEO_SEC_TARGET_SECTIONS の見出し以降に絞ったテキストを返す。

    この定数は「10-K のどのセクションを抽出対象にするか」の宣言。

    絞り込みは**後退しない**設計にしてある:
      - 該当見出しが1つも見つからなければ元の全文をそのまま返す
      - 見つかった範囲の合計が短すぎる（<2000文字）場合も全文に戻す
    これにより、見出し表記が想定と違う文書でも全文スキャンと同じ抽出結果になる。
    """
    sections = getattr(cfg, "GEO_SEC_TARGET_SECTIONS", None) if cfg else None
    if not sections:
        return text

    chunks: list[str] = []
    for name in sections:
        for m in re.finditer(re.escape(str(name)), text, re.IGNORECASE):
            # 見出しから 20000 文字を対象範囲とする（セクション末尾の
            # 確実な検出は文書ごとに揺れるため、固定幅で切る）
            chunks.append(text[m.start():m.start() + 20000])

    narrowed = "\n".join(chunks)
    if len(narrowed) < 2000:
        return text
    return narrowed


def _extract_geographic_data(text: str, cfg=None) -> list[dict]:
    """
    10-K のテキストから地理別売上データを抽出する。

    抽出パターン（優先順）:
      P1: 数値テーブル形式
          "United States  $45.2 billion  62%"
          "China           $8.1 billion  11%"
      P2: 文章形式
          "approximately 38% of our net revenues were generated
           outside of the United States"
      P3: 国名 + パーセンテージの近接パターン
          "revenues from China represented 19% of total revenues"

    Returns:
        list of {country_code, revenue_share_est, pattern_type}
    """
    results: list[dict] = []
    # config.GEO_SEC_TARGET_SECTIONS で対象セクションに
    # 絞ってからスキャンする（該当なしなら全文）
    text_clean = re.sub(r"\s+", " ", _narrow_to_target_sections(text, cfg))

    # ── P1: テーブル形式（国名 + 金額/比率） ────────────────────
    # パターン: 国名に続いて数値（$X.X billion/million または X%）
    table_pattern = re.compile(
        r"(United States|U\.S\.|China|Japan|South Korea|Korea|Taiwan|"
        r"Europe|Germany|France|United Kingdom|UK|Netherlands|India|"
        r"Singapore|Malaysia|Indonesia|Australia|Brazil|Canada|Mexico|"
        r"Asia\s*[\-–]?\s*Pacific|Middle East|Latin America)"
        r"[\s\|\,\t]*"
        r"(?:\$[\d,\.]+\s*(?:billion|million|B|M))?"
        r"[\s\|\,\t]*"
        r"(\d{1,3}(?:\.\d{1,2})?)[\s]*%",
        re.IGNORECASE,
    )
    for m in table_pattern.finditer(text_clean):
        region    = m.group(1).strip()
        share_pct = float(m.group(2))
        if share_pct <= 0 or share_pct > 100:
            continue
        code = GEO_KEYWORD_TO_CODE.get(region)
        if not code:
            # 部分一致で探す
            for kw, c in GEO_KEYWORD_TO_CODE.items():
                if kw.lower() in region.lower():
                    code = c
                    break
        if code:
            results.append({
                "country_code":      code,
                "revenue_share_est": round(share_pct / 100.0, 4),
                "pattern_type":      "P1_table",
            })

    # ── P2: 文章形式（outside US の比率） ────────────────────────
    p2_pattern = re.compile(
        r"approximately\s+(\d{1,3}(?:\.\d)?)\s*%\s*"
        r"(?:of\s+(?:our\s+)?(?:net\s+)?revenues?\s+(?:were|was)\s+)"
        r"(?:generated\s+)?outside\s+(?:of\s+)?(?:the\s+)?United\s+States",
        re.IGNORECASE,
    )
    for m in p2_pattern.finditer(text_clean):
        outside_pct = float(m.group(1))
        if 0 < outside_pct <= 100:
            # US外 = outside_pct, US = 100 - outside_pct
            results.append({
                "country_code":      "USA",
                "revenue_share_est": round((100 - outside_pct) / 100.0, 4),
                "pattern_type":      "P2_sentence_us",
            })

    # ── P3: 個別国のパーセンテージ言及 ──────────────────────────
    p3_pattern = re.compile(
        r"(?:revenues?\s+from\s+|sales\s+in\s+|operations\s+in\s+)"
        r"(China|Japan|South Korea|Korea|Taiwan|Germany|France|"
        r"United Kingdom|UK|India|Brazil|Canada|Australia|Singapore|"
        r"Malaysia|Indonesia|Mexico|Saudi Arabia|Israel)"
        r"(?:\s+\w+){0,5}\s+"
        r"(?:represented|accounted\s+for|were\s+approximately)\s+"
        r"(\d{1,3}(?:\.\d{1,2})?)\s*%",
        re.IGNORECASE,
    )
    existing_codes = {r["country_code"] for r in results}
    for m in p3_pattern.finditer(text_clean):
        country = m.group(1).strip()
        pct     = float(m.group(2))
        if pct <= 0 or pct > 100:
            continue
        code = GEO_KEYWORD_TO_CODE.get(country)
        if code and code not in existing_codes:
            results.append({
                "country_code":      code,
                "revenue_share_est": round(pct / 100.0, 4),
                "pattern_type":      "P3_mention",
            })
            existing_codes.add(code)

    # 重複排除（同一 country_code は最大値を採用）
    deduped: dict[str, dict] = {}
    for r in results:
        cc = r["country_code"]
        if cc not in deduped or r["revenue_share_est"] > deduped[cc]["revenue_share_est"]:
            deduped[cc] = r

    return list(deduped.values())


def _extract_risk_countries(text: str) -> list[str]:
    """
    Risk Factors セクションのテキストから
    リスク言及のある国コードのリストを返す。

    Returns:
        list[str]: country_code のリスト（重複なし）
    """
    mentioned: list[str] = []
    seen: set[str] = set()

    risk_pattern = re.compile(
        r"(?:risk|uncertainty|instability|sanction|tariff|"
        r"export\s+control|geopolit)\w*\b"
        r"(?:\s+\w+){0,10}\s+"
        r"(China|Russia|Iran|North\s+Korea|Venezuela|Myanmar|Cuba|"
        r"Taiwan|Hong\s+Kong|Ukraine|Israel|Middle\s+East|"
        r"Saudi\s+Arabia|Japan|South\s+Korea|India)",
        re.IGNORECASE,
    )

    for m in risk_pattern.finditer(text):
        country = m.group(1).strip()
        code    = GEO_KEYWORD_TO_CODE.get(country)
        if code and code not in seen:
            mentioned.append(code)
            seen.add(code)

    return mentioned


# ==============================================================
# BLOCK 6: メイン処理（ティッカー単位）
# ==============================================================

def _process_ticker(
    ticker: str,
    cfg=None,
    min_share: float = 0.03,
) -> list[dict]:
    """
    1ティッカーの 10-K を取得・解析して exposure レコードリストを返す。

    Args:
        ticker    : 米国株ティッカー（例: "NVDA"）
        cfg       : config モジュール
        min_share : 格納最低閾値（revenue_share_est がこれ以上のみ格納）

    Returns:
        list of exposure records
    """
    cik = TICKER_TO_CIK.get(ticker.upper())
    if not cik:
        # CIK 未登録の場合は EDGAR 検索 API で取得を試みる
        try:
            search_url = (
                f"https://efts.sec.gov/LATEST/search-index"
                f"?q=%22{quote(ticker)}%22&forms=10-K"
            )
            search_data = _fetch_sec(search_url, cfg=cfg, as_json=True)
            if search_data:
                hits = search_data.get("hits", {}).get("hits", [])
                if hits:
                    cik_raw = hits[0].get("_source", {}).get("entity_id", "")
                    if cik_raw:
                        cik = str(int(cik_raw)).zfill(10)
        except Exception:
            pass

    if not cik:
        print(f"    ⚠️  [{ticker}] CIK 未解決 → スキップ")
        return []

    print(f"    📄 [{ticker}] CIK={cik} → 最新10-K 取得中...")

    acc_no, doc_url = _get_latest_10k_url(cik, cfg=cfg)
    if not doc_url:
        print(f"    ⚠️  [{ticker}] 10-K URL 取得失敗 → スキップ")
        return []

    # 10-K 本文取得
    doc_text = _fetch_sec(doc_url, cfg=cfg)
    if not doc_text:
        print(f"    ⚠️  [{ticker}] 10-K 本文取得失敗 → スキップ")
        return []

    # テキスト長チェック（極端に短い場合はパース失敗と判断）
    if len(doc_text) < 5000:
        print(f"    ⚠️  [{ticker}] 10-K テキスト不足 ({len(doc_text)}文字) → スキップ")
        return []

    print(f"    ✅ [{ticker}] 10-K 取得完了 ({len(doc_text):,}文字)")

    # 地理セグメントデータ抽出
    geo_data = _extract_geographic_data(doc_text, cfg)

    # リスク関連国抽出
    risk_section_match = re.search(
        r"(?:Item\s+1A|RISK\s+FACTORS)(.*?)(?:Item\s+1B|Item\s+2)",
        doc_text,
        re.DOTALL | re.IGNORECASE,
    )
    risk_countries: list[str] = []
    if risk_section_match:
        risk_countries = _extract_risk_countries(risk_section_match.group(1))

    records: list[dict] = []
    today_str = date.today().isoformat()

    # 地理セグメントからレコード生成
    _min = getattr(cfg, "GEO_EXPOSURE_MIN_STRENGTH", min_share) if cfg else min_share
    _review_thresh = getattr(cfg, "GEO_EXPOSURE_REVIEW_THRESHOLD", 0.20) if cfg else 0.20

    for gd in geo_data:
        share = gd["revenue_share_est"]
        if share < _min:
            continue
        records.append({
            "ticker":            ticker,
            "market":            "US",
            "country_code":      gd["country_code"],
            "exposure_type":     "SALES_NETWORK",
            "exposure_strength": share,
            "revenue_share_est": share,
            "employee_share_est": None,
            "asset_share_est":   None,
            "commodity_code":    None,
            "source":            "sec_auto",
            "evidence_url":      doc_url,
            "rationale":         (
                f"10-K Geographic Segment ({gd['pattern_type']}): "
                f"{share*100:.1f}%"
            ),
            "review_flag":       share < _review_thresh,
            "valid_from":        today_str,
        })

    # リスク言及国からレコード生成（重複除外）
    existing_codes = {r["country_code"] for r in records}
    for code in risk_countries:
        if code not in existing_codes:
            records.append({
                "ticker":            ticker,
                "market":            "US",
                "country_code":      code,
                "exposure_type":     "SALES_NETWORK",
                "exposure_strength": 0.05,   # リスク言及のみ = 保守的推定
                "revenue_share_est": None,
                "employee_share_est": None,
                "asset_share_est":   None,
                "commodity_code":    None,
                "source":            "sec_auto",
                "evidence_url":      doc_url,
                "rationale":         "10-K Risk Factors: 地政学リスク言及",
                "review_flag":       True,
                "valid_from":        today_str,
            })
            existing_codes.add(code)

    print(f"    📊 [{ticker}] 抽出: {len(records)} 件"
          f"（geo={len(geo_data)} / risk={len(risk_countries)}）")
    return records


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_sec_geographic_extractor(
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
    target_tickers: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    SEC EDGAR 10-K から地理別売上情報を抽出し BQ に MERGE する。

    Args:
        cfg             : config モジュール
        project_id      : GCPプロジェクトID
        target_tickers  : 指定した場合そのティッカーのみ処理
                          None の場合 IMPORTANT_TICKERS_US を使用
        dry_run         : True の場合 BQ 実行スキップ

    Returns:
        {
          "success":    bool,
          "total":      int,
          "merged":     int,
          "tickers":    list[str],
          "records":    list[dict],
          "error":      str,
          "timestamp":  str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
    print(f"\n🇺🇸 sec_geographic_extractor: SEC 10-K 地理情報抽出開始")
    print(f"   プロジェクト: {_pid}")

    # 対象ティッカーリストを決定
    if target_tickers:
        tickers = [t.upper() for t in target_tickers]
    else:
        tickers = [
            t.replace(".T", "").upper()
            for t in (
                getattr(cfg, "IMPORTANT_TICKERS_US", []) if cfg else []
            )
        ]
        if not tickers:
            tickers = list(TICKER_TO_CIK.keys())

    print(f"   対象ティッカー: {len(tickers)} 件")

    all_records: list[dict] = []
    processed_tickers: list[str] = []

    for i, ticker in enumerate(tickers):
        try:
            records = _process_ticker(ticker, cfg=cfg)
            all_records.extend(records)
            processed_tickers.append(ticker)
        except Exception as e:
            print(f"    ⚠️  [{ticker}] 処理エラー: {str(e)[:80]}")
        # SEC EDGAR レート制限遵守（ティッカー間0.5秒待機）
        if i < len(tickers) - 1:
            time.sleep(0.5)

    print(f"\n   📊 抽出完了: {len(all_records)} 件 / {len(processed_tickers)} 銘柄")

    if not all_records:
        return {
            "success":   False,
            "total":     0,
            "merged":    0,
            "tickers":   processed_tickers,
            "records":   [],
            "error":     "抽出レコード0件",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    if dry_run:
        print(f"   [DRY RUN] BQ実行スキップ")
        return {
            "success":   True,
            "total":     len(all_records),
            "merged":    0,
            "tickers":   processed_tickers,
            "records":   all_records,
            "error":     "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── BQ MERGE（jogmec_scraper の共通関数を利用） ──────────────
    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _js     = importlib.import_module("jogmec_scraper")
        _bq_mod = importlib.import_module("edinet_bq_client")
        raw_bq  = _bq_mod.EDINETBQClient().client
    # 一次経路（edinet_bq_client＝Actions の BQ_CREDENTIALS 経路）の失敗理由も返す。
    # Colabフォールバックの「No module named 'google.colab'」だけだと真因が隠れるため。
    except Exception as _primary_err:
        try:
            from google.cloud import bigquery as _bq_lib
            from google.colab import auth as _auth
            _auth.authenticate_user()
            raw_bq = _bq_lib.Client(project=_pid, location="asia-northeast1")
            import importlib, sys as _sys
            from pathlib import Path as _Path
            for p in ["/content", str(_Path(__file__).parent)]:
                if p not in _sys.path:
                    _sys.path.insert(0, p)
            _js = importlib.import_module("jogmec_scraper")
        except Exception as e:
            return {
                "success":   False,
                "total":     len(all_records),
                "merged":    0,
                "tickers":   processed_tickers,
                "records":   all_records,
                "error":     (f"BQ Client 取得失敗: {_primary_err} "
                              f"/ Colabフォールバックも失敗: {e}"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    try:
        BATCH_SIZE   = 500
        merged_total = 0
        for i in range(0, len(all_records), BATCH_SIZE):
            batch     = all_records[i:i + BATCH_SIZE]
            merge_sql = _js._build_exposure_merge_query(_pid, batch)
            job       = raw_bq.query(merge_sql)
            job.result()
            merged_total += len(batch)
            print(f"   MERGE進捗: {merged_total}/{len(all_records)} 件")

        print(f"   ✅ BQ MERGE 完了: {merged_total} 件")
        return {
            "success":   True,
            "total":     len(all_records),
            "merged":    merged_total,
            "tickers":   processed_tickers,
            "records":   all_records,
            "error":     "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        err = str(e)
        print(f"   ❌ BQ MERGE 失敗: {err[:200]}")
        return {
            "success":   False,
            "total":     len(all_records),
            "merged":    0,
            "tickers":   processed_tickers,
            "records":   all_records,
            "error":     err[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ==============================================================
# BLOCK 8: 参照ユーティリティ（us_stock_screener.py から呼ばれる）
# ==============================================================

_US_GEO_CACHE: dict | None = None


def _load_us_geo_exposure(_pid: str) -> dict:
    """米国の権益を全社まとめて1本で読み、{ticker: [行]} にする（失敗したら空の dict）。"""
    import importlib, sys as _sys
    from pathlib import Path as _Path
    for p in ["/content", str(_Path(__file__).parent)]:
        if p not in _sys.path:
            _sys.path.insert(0, p)
    raw_bq = importlib.import_module("edinet_bq_client").EDINETBQClient().client
    query = f"""
    SELECT
      e.ticker,
      e.country_code,
      e.exposure_type,
      e.exposure_strength,
      e.revenue_share_est,
      IFNULL(r.composite_risk_score, 0.0) AS composite_risk_score
    FROM `{_pid}.geopolitics.company_country_exposure` AS e
    LEFT JOIN (
      SELECT country_code, composite_risk_score
      FROM `{_pid}.geopolitics.country_risk_daily`
      QUALIFY ROW_NUMBER() OVER (PARTITION BY country_code ORDER BY risk_date DESC) = 1
    ) AS r ON e.country_code = r.country_code
    WHERE e.market = 'US'
      AND (e.valid_to IS NULL OR e.valid_to > CURRENT_DATE())
    ORDER BY e.ticker, e.exposure_strength DESC
    """
    out: dict = {}
    for row in raw_bq.query(query).result():
        out.setdefault(row.ticker, []).append({
            "country_code":         row.country_code,
            "exposure_type":        row.exposure_type,
            "exposure_strength":    float(row.exposure_strength or 0.0),
            "revenue_share_est":    float(row.revenue_share_est or 0.0)
                                    if row.revenue_share_est else None,
            "composite_risk_score": float(row.composite_risk_score or 0.0),
        })
    return out


def get_us_geo_exposure(
    ticker: str,
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
) -> list[dict]:
    """
    指定米国株ティッカーの地理エクスポージャー。us_stock_screener.py の geo_impact_bonus 計算に使用。
    最初の呼び出しで米国の全社分を1本のクエリで読み、以降はその写しから返す（候補ごとに BQ を引かない）。

    Returns:
        list of {country_code, exposure_type, exposure_strength,
                 revenue_share_est, composite_risk_score}
        取得失敗時・権益の無い銘柄は空リスト
    """
    global _US_GEO_CACHE
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
    if _US_GEO_CACHE is None:
        try:
            _US_GEO_CACHE = _load_us_geo_exposure(_pid)
        except Exception as e:
            print(f"  ⚠️  get_us_geo_exposure（一括読み）: {str(e)[:80]}")
            _US_GEO_CACHE = {}
    return list(_US_GEO_CACHE.get(str(ticker or "").strip(), []))


# ==============================================================
# 単体テスト / Colab実行エントリ
# ==============================================================

if __name__ == "__main__":
    import sys

    dry_run_flag = "--dry-run" in sys.argv
    target_flag  = None
    for i, arg in enumerate(sys.argv):
        if arg == "--target" and i + 1 < len(sys.argv):
            target_flag = sys.argv[i + 1].split(",")

    print("=" * 60)
    print("sec_geographic_extractor.py v1.0")
    print("=" * 60)

    result = run_sec_geographic_extractor(
        cfg             = None,
        project_id      = "stock-analyzer-edinet",
        target_tickers  = target_flag or ["NVDA", "XOM", "FCX"],
        dry_run         = dry_run_flag,
    )

    print(f"\n{'✅ 完了' if result['success'] else '❌ 失敗'}")
    print(f"   抽出銘柄数:   {len(result['tickers'])}")
    print(f"   総レコード数: {result['total']}")
    print(f"   MERGE件数:   {result['merged']}")
    if result.get("records"):
        print(f"\n   サンプル（先頭3件）:")
        for r in result["records"][:3]:
            print(f"   {r['ticker']} → {r['country_code']} "
                  f"{r['exposure_type']} "
                  f"strength={r['exposure_strength']:.2f} "
                  f"source={r['source']}")
    if result["error"]:
        print(f"   エラー: {result['error']}")
