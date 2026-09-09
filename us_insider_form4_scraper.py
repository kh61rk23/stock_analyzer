# ==============================================================
# us_insider_form4_scraper.py  v1.0  (Wave 3 - U-3)
# SEC EDGAR Form 4 インサイダー取引監視・スコアリング
# ==============================================================
# 【役割】
#   SEC EDGAR の Form 4（役員・主要株主の取引報告書）を取得・パースし、
#   直近 N 日間のインサイダー取引をスコアリングして `insider_bonus` を返す。
#   us_stock_screener.py の `_calc_scores()` から try/except 経由で呼ばれる。
#
# 【データ取得フロー】
#   1. CIK マッピング: SEC company_tickers.json でティッカー → CIK を解決
#   2. 提出書類リスト: data.sec.gov/submissions/CIK{cik}.json からForm 4を抽出
#   3. Form 4 XML 取得: 直近 N 日分のみ個別 XML をパース
#   4. yfinance フォールバック: EDGAR が取得不可の場合に insider_transactions を使用
#
# 【スコアリング設計（insider_bonus）】
#   $insider\_bonus \in [-5.0, +5.0]$
#
#   購入シグナル（プラス）:
#     CEO 購入 > $1M              : +4.0pt
#     CEO/CFO 購入 > $500K        : +3.0pt
#     役員複数（≥3名）が同週購入   : +2.5pt（重複加算あり）
#     役員購入（一般） > $100K     : +1.5pt
#     役員購入 < $100K            : +0.5pt
#
#   売却シグナル（マイナス）:
#     CEO 大量売却（>1% of shares) : -3.0pt
#     役員複数（≥3名）同週売却      : -2.0pt
#     一般売却                     : -0.5pt
#
#   上限クランプ: $\max(-5.0, \min(5.0, \sum bonuses))$
#
# 【生成キャッシュ】
#   insider_form4_cache.json  TTL: 1日（日次更新で十分）
#
# 【呼び出しパターン（us_stock_screener.py から）】
#   from us_insider_form4_scraper import calc_insider_bonus
#   try:
#       insider_bonus = calc_insider_bonus(ticker, cfg=cfg)
#   except Exception:
#       insider_bonus = 0.0
#
# 【依存】
#   sec_client.py（HTTP呼び出し・User-Agent・CIK解決を一本化。Wave J1 / U-1c）
#   xml.etree.ElementTree（Form 4 XML パース。標準ライブラリ）
#   yfinance（フォールバック用）
#   ★ 全関数を try/except で保護。ネットワーク失敗時は bonus=0.0 を返す。
# ==============================================================

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import sec_client

# ==============================================================
# BLOCK 0: 定数・エンドポイント
# ==============================================================

# SEC EDGAR エンドポイント
# 2026-07-27是正（Wave J1 / U-1c）: company_tickers.json 取得・submissions API
# 組み立ては sec_client.cik_map() / sec_client.submissions() に一本化した。
# 個別 Form 4 XML ドキュメントの取得のみ sec_client に汎用APIが無いため
# 本ファイルで URL を組み立て、実際の GET は sec_client.get() を使う。
_EDGAR_ARCHIVE      = ("https://www.sec.gov/Archives/edgar/data/"
                       "{cik}/{acc_no_slash}/{doc_name}")

# 2026-07-13是正（U-2 / F-2）由来の統一フォールバック文字列。
# 実際の User-Agent 解決は sec_client.user_agent() に委譲済みで本定数は
# 直接は使われないが、tests/test_sec_user_agent.py のダミーUA再混入検知
# （機械検査）の対象文字列のため残す。
_SEC_UA_FALLBACK = "StockAnalyzer stockanalyzer@example.com"


def _sec_user_agent(cfg=None) -> str:
    """sec_client.user_agent() の薄いラッパー（既存呼び出し互換のため維持）。"""
    return sec_client.user_agent(cfg)


# タイムアウト・リトライ設定
_HTTP_TIMEOUT  = 15    # 秒
_FETCH_DELAY   = 0.5   # SEC EDGAR レート制限対応（10 req/sec 以下推奨）
_MAX_FORM4_PARSE = 10  # 1銘柄あたり最大パースするForm 4 件数

# キャッシュ設定
_CACHE_TTL_DAYS     = 1    # insider_form4_cache.json TTL
_TICKER_MAP_TTL     = 7    # company_tickers.json の再取得間隔（日）
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_US_INSIDER_FORM4_SCRAPER_WINDOW_DAYS_DEFAULT': '_WINDOW_DAYS_DEFAULT',
    'B5_US_INSIDER_FORM4_SCRAPER_HTTP_TIMEOUT': '_HTTP_TIMEOUT',
    'B5_US_INSIDER_FORM4_SCRAPER_FETCH_DELAY': '_FETCH_DELAY',
    'B6_US_INSIDER_FORM4_SCRAPER_MAX_FORM4_PARSE': '_MAX_FORM4_PARSE',
}

_WINDOW_DAYS_DEFAULT = 30  # デフォルト集計ウィンドウ（日）

# 役職ランク（スコアリング重み付け用）
_ROLE_WEIGHTS: dict[str, float] = {
    "chief executive officer":   5.0,
    "ceo":                       5.0,
    "chief financial officer":   4.0,
    "cfo":                       4.0,
    "chief operating officer":   3.5,
    "coo":                       3.5,
    "president":                 3.0,
    "chairman":                  3.0,
    "director":                  2.0,
    "10%":                       2.0,   # 10% owner
    "officer":                   1.5,   # 一般役員
}

# ==============================================================
# BLOCK 1: HTTP ユーティリティ
# ==============================================================
# 2026-07-27是正（Wave J1 / U-1c）: 旧 _fetch_url / _fetch_json（urllib直叩き）
# は削除。sec.gov への全アクセスは sec_client.get() / get_json() に一本化した
# （個別 Form 4 XML ドキュメント取得は BLOCK 5 の _fetch_form4_xml を参照）。


# ==============================================================
# BLOCK 2: キャッシュ I/O
# ==============================================================

def _cache_dir(cfg=None) -> Path:
    try:
        return Path(getattr(cfg, "CACHE_DIR", "/tmp"))
    except Exception:
        return Path("/tmp")


def _load_form4_cache(cfg=None) -> dict:
    """insider_form4_cache.json を読み込む。TTL切れ・失敗時は空 dict。"""
    try:
        p = _cache_dir(cfg) / "insider_form4_cache.json"
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        # TTL チェック（1日）
        cutoff = (datetime.now() - timedelta(days=_CACHE_TTL_DAYS)
                  ).strftime("%Y-%m-%d")
        return {
            t: v for t, v in raw.items()
            if isinstance(v, dict) and v.get("fetched_at", "")[:10] >= cutoff
        }
    except Exception:
        return {}


def _save_form4_cache(ticker: str, data: dict, cfg=None) -> None:
    """insider_form4_cache.json に 1 銘柄分を追記保存する。"""
    try:
        cache = _load_form4_cache(cfg)
        cache[ticker] = {**data, "fetched_at": datetime.now().isoformat()}
        p = _cache_dir(cfg) / "insider_form4_cache.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 3: CIK マッピング
# ==============================================================

def _load_cik_map(cfg=None) -> dict[str, str]:
    """
    SEC EDGAR の company_tickers.json から ティッカー→CIK マッピングを返す。

    2026-07-27是正（U-1c）: 実体は sec_client.cik_map() の共通ディスクキャッシュ
    （TTL: config.py SEC_CIK_MAP_TTL_DAYS＝本ファイルの旧 _TICKER_MAP_TTL と同じ
    7日）に置き換えた。旧実装はプロセス内メモリのみのキャッシュだったため
    プロセスを跨ぐたびに毎回 SEC へ再取得していたが、ディスクキャッシュにより
    プロセスを跨いでも TTL 内は再取得しなくなる。

    Returns:
        {"AAPL": "0000320193", "MSFT": "0000789019", ...}
    """
    try:
        return sec_client.cik_map(cfg=cfg)
    except Exception:
        return {}


def _get_cik(ticker: str, cfg=None) -> Optional[str]:
    """
    ティッカーに対応する CIK 文字列（10桁ゼロパディング）を返す。

    Returns:
        "0000320193" 形式 | None
    """
    try:
        return sec_client.get_cik(ticker, cfg=cfg)
    except Exception:
        return None


# ==============================================================
# BLOCK 4: Form 4 提出書類リスト取得
# ==============================================================

def _fetch_recent_form4_filings(
    cik:        str,
    days:       int = _WINDOW_DAYS_DEFAULT,
) -> list[dict]:
    """
    SEC EDGAR submissions API から直近 days 日以内の Form 4 提出を返す。

    Args:
        cik:  "0000320193" 形式の CIK
        days: 集計ウィンドウ（日）

    Returns:
        [
          {
            "accession_number": str,   # "0001234567-24-000001" 形式
            "filing_date":      str,   # "YYYY-MM-DD"
            "primary_document": str,   # "xyzw-form4.xml"
            "cik_int":          int,
          }, ...
        ]
    """
    try:
        cik_int = int(cik)
        raw     = sec_client.submissions(cik_int)
        if not raw or not isinstance(raw, dict):
            return []

        filings = raw.get("filings", {}).get("recent", {})
        forms   = filings.get("form", [])
        dates   = filings.get("filingDate", [])
        acc_nos = filings.get("accessionNumber", [])
        docs    = filings.get("primaryDocument", [])

        cutoff  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        result  = []

        for i, form in enumerate(forms):
            if str(form).strip() != "4":
                continue
            date = dates[i] if i < len(dates) else ""
            if date < cutoff:
                continue   # 集計ウィンドウ外

            acc   = acc_nos[i] if i < len(acc_nos) else ""
            doc   = docs[i]    if i < len(docs)    else ""

            result.append({
                "accession_number": acc,
                "filing_date":      date,
                "primary_document": doc,
                "cik_int":          cik_int,
            })

        return result[:_MAX_FORM4_PARSE * 3]   # 最大パース件数の3倍まで取得

    except Exception:
        return []


# ==============================================================
# BLOCK 5: Form 4 XML パース
# ==============================================================

def _fetch_form4_xml(cik_int: int, accession_number: str, doc_name: str) -> Optional[str]:
    """
    個別 Form 4 XML ファイルを取得する。

    Args:
        cik_int:          CIK 整数値
        accession_number: "0001234567-24-000001" 形式
        doc_name:         "form4.xml" 等の一次ドキュメント名

    Returns:
        XML 文字列 | None
    """
    try:
        # アクセッション番号のハイフンを除去してパス用に変換
        acc_no_slash = accession_number.replace("-", "")
        url = _EDGAR_ARCHIVE.format(
            cik        = cik_int,
            acc_no_slash = acc_no_slash,
            doc_name   = doc_name,
        )
        time.sleep(_FETCH_DELAY)
        resp = sec_client.get(url, accept="application/xml", timeout=_HTTP_TIMEOUT)
        return resp.text if resp is not None else None
    except Exception:
        return None


def _parse_form4_xml(xml_text: str) -> Optional[dict]:
    """
    Form 4 XML を解析してトランザクション情報を返す。

    Returns:
        {
          "officer_title":      str,          # 役職名（小文字）
          "is_director":        bool,
          "is_officer":         bool,
          "is_ten_pct_owner":   bool,
          "transactions":       list[dict],   # 各トランザクション
        }
        ※ パース失敗時は None
    """
    try:
        root = ET.fromstring(xml_text)
        ns   = ""   # Form 4 は通常 namespace なし

        # ── 報告者の役割取得 ─────────────────────────────────────
        rel = root.find(".//reportingOwnerRelationship")
        if rel is None:
            # namespace 付きで再試行
            for elem in root.iter():
                if "reportingOwnerRelationship" in elem.tag:
                    rel = elem
                    break

        is_director = False
        is_officer  = False
        is_10pct    = False
        title_raw   = ""

        if rel is not None:
            def _text(tag: str) -> str:
                el = rel.find(tag)
                return (el.text or "").strip() if el is not None else ""

            is_director = _text("isDirector") == "1"
            is_officer  = _text("isOfficer")  == "1"
            is_10pct    = _text("isTenPercentOwner") == "1"
            title_raw   = _text("officerTitle").lower()

        # ── 非デリバティブ取引の取得 ─────────────────────────────
        transactions: list[dict] = []

        for tx_tag in ("nonDerivativeTransaction", "derivativeTransaction"):
            for tx in root.findall(f".//{tx_tag}"):
                try:
                    def _txval(path: str) -> str:
                        el = tx.find(path)
                        return (el.text or "").strip() if el is not None else ""

                    tx_type   = _txval(".//transactionType")
                    tx_date   = _txval(".//transactionDate/value")
                    tx_shares_raw = _txval(".//transactionShares/value")
                    tx_price_raw  = _txval(".//transactionPricePerShare/value")

                    tx_shares = float(tx_shares_raw) if tx_shares_raw else 0.0
                    tx_price  = float(tx_price_raw)  if tx_price_raw  else 0.0
                    tx_value  = tx_shares * tx_price

                    if tx_type in ("P", "S") and tx_date:
                        transactions.append({
                            "type":       tx_type,   # "P"=buy / "S"=sell
                            "date":       tx_date,
                            "shares":     tx_shares,
                            "price":      tx_price,
                            "value_usd":  tx_value,
                            "is_deriv":   tx_tag == "derivativeTransaction",
                        })
                except Exception:
                    continue

        if not transactions and not is_officer and not is_director:
            return None

        return {
            "officer_title":    title_raw,
            "is_director":      is_director,
            "is_officer":       is_officer,
            "is_ten_pct_owner": is_10pct,
            "transactions":     transactions,
        }

    except ET.ParseError:
        return None
    except Exception:
        return None


# ==============================================================
# BLOCK 6: インサイダー取引スコアリング
# ==============================================================

def _get_role_weight(parsed: dict) -> float:
    """
    役職情報からスコアリング重みを返す。

    Returns:
        float: 1.5（一般役員）〜 5.0（CEO）
    """
    try:
        title = parsed.get("officer_title", "").lower()
        # タイトルで前方一致検索
        for keyword, weight in sorted(_ROLE_WEIGHTS.items(),
                                      key=lambda x: -x[1]):
            if keyword in title:
                return weight
        # タイトル不明でも is_officer / is_director から判定
        if parsed.get("is_officer"):
            return 1.5
        if parsed.get("is_director"):
            return 2.0
        if parsed.get("is_ten_pct_owner"):
            return 2.0
        return 1.0
    except Exception:
        return 1.0


def _calc_transaction_bonus(
    tx:          dict,
    role_weight: float,
) -> float:
    """
    1件のトランザクションからボーナス（正・負）を算出する。

    Returns:
        float: -5.0〜+5.0
    """
    try:
        tx_type  = tx.get("type", "")
        val_usd  = float(tx.get("value_usd", 0.0))
        is_deriv = tx.get("is_deriv", False)

        if is_deriv:
            # デリバティブ（オプション行使等）はスコア影響を半減
            role_weight *= 0.5

        if tx_type == "P":   # 購入
            if val_usd >= 1_000_000:
                base = 4.0
            elif val_usd >= 500_000:
                base = 3.0
            elif val_usd >= 100_000:
                base = 1.5
            elif val_usd >= 10_000:
                base = 0.5
            else:
                base = 0.2
            return min(5.0, base * (role_weight / 5.0))   # 役職スケール

        elif tx_type == "S":   # 売却
            if val_usd >= 5_000_000:
                base = -3.0
            elif val_usd >= 1_000_000:
                base = -2.0
            elif val_usd >= 100_000:
                base = -0.5
            else:
                base = -0.1
            return max(-5.0, base * (role_weight / 5.0))

        return 0.0

    except Exception:
        return 0.0


def _score_filings(
    parsed_list: list[dict],
    window_days: int = _WINDOW_DAYS_DEFAULT,
) -> dict:
    """
    パース済み Form 4 リストを集計してスコアリング結果を返す。

    Args:
        parsed_list: _parse_form4_xml() の返り値リスト
        window_days: 集計ウィンドウ（日）

    Returns:
        {
          "insider_bonus":    float,      # -5.0〜+5.0
          "buy_count":        int,
          "sell_count":       int,
          "buyer_roles":      list[str],
          "net_buy_value":    float,      # 純購入額（USD）
          "summary":          str,
        }
    """
    try:
        cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
        total_bonus = 0.0
        buy_count   = 0
        sell_count  = 0
        buyer_roles: list[str] = []
        net_value   = 0.0

        for parsed in parsed_list:
            if not parsed:
                continue
            role_w = _get_role_weight(parsed)
            title  = parsed.get("officer_title", "officer")

            for tx in parsed.get("transactions", []):
                tx_date = tx.get("date", "")
                if tx_date and tx_date[:10] < cutoff:
                    continue   # ウィンドウ外

                bonus = _calc_transaction_bonus(tx, role_w)
                total_bonus += bonus

                val = float(tx.get("value_usd", 0.0))
                if tx.get("type") == "P":
                    buy_count += 1
                    net_value += val
                    if title:
                        buyer_roles.append(title)
                elif tx.get("type") == "S":
                    sell_count += 1
                    net_value  -= val

        # 複数役員が同週購入 → 相乗ボーナス
        if buy_count >= 3:
            total_bonus += 2.0
        elif buy_count >= 2:
            total_bonus += 1.0

        # クランプ
        final_bonus = round(max(-5.0, min(5.0, total_bonus)), 2)

        summary = (
            f"購入{buy_count}件/売却{sell_count}件 "
            f"net_value=${net_value:,.0f} "
            f"bonus={final_bonus:+.1f}pt"
        )

        return {
            "insider_bonus":  final_bonus,
            "buy_count":      buy_count,
            "sell_count":     sell_count,
            "buyer_roles":    buyer_roles[:5],
            "net_buy_value":  round(net_value, 0),
            "summary":        summary,
        }

    except Exception:
        return {
            "insider_bonus": 0.0,
            "buy_count":     0,
            "sell_count":    0,
            "buyer_roles":   [],
            "net_buy_value": 0.0,
            "summary":       "スコアリングエラー",
        }


# ==============================================================
# BLOCK 7: yfinance フォールバック
# ==============================================================

def _calc_insider_bonus_yf(ticker: str, days: int | None = None) -> float:
    """
    yfinance の get_insider_transactions() を使ったフォールバック実装。
    EDGAR が取得不可の場合に使用。

    Returns:
        float: insider_bonus (-5.0〜+5.0)
    """
    try:
        days = _WINDOW_DAYS_DEFAULT if days is None else days
        import yf_cache as yf
        t    = yf.Ticker(ticker)
        raw  = t.get_insider_transactions()
        if raw is None or raw.empty:
            return 0.0

        # 直近 days 日以内のデータをフィルタ
        cutoff = datetime.now() - timedelta(days=days)
        try:
            raw = raw[raw.index >= cutoff]
        except Exception:
            pass

        # Shares 列の正負から購買/売却を判定
        total_bonus = 0.0
        buy_count   = 0
        sell_count  = 0

        for _, row in raw.iterrows():
            try:
                shares = float(row.get("Shares", 0) or 0)
                value  = float(row.get("Value", 0) or 0)
                insider = str(row.get("Insider", "")).lower()
                tx_type = "P" if shares > 0 else "S"

                role_w = 1.5
                for kw, wt in _ROLE_WEIGHTS.items():
                    if kw in insider:
                        role_w = wt
                        break

                tx = {"type": tx_type, "value_usd": abs(value), "is_deriv": False}
                total_bonus += _calc_transaction_bonus(tx, role_w)
                if tx_type == "P":
                    buy_count += 1
                else:
                    sell_count += 1
            except Exception:
                continue

        if buy_count >= 3:
            total_bonus += 2.0
        elif buy_count >= 2:
            total_bonus += 1.0

        return round(max(-5.0, min(5.0, total_bonus)), 2)

    except Exception:
        return 0.0


# ==============================================================
# BLOCK 8: メイン公開 API
# ==============================================================

def fetch_form4_filings(
    ticker: str,
    days:   int | None = None,
    cfg=None,
) -> list[dict]:
    """
    指定ティッカーの直近 days 日以内 Form 4 提出リストを返す。

    Args:
        ticker: "AAPL" 形式
        days:   集計ウィンドウ（日）
        cfg:    config モジュール（None 可）

    Returns:
        [{"accession_number": str, "filing_date": str, ...}, ...]
        ※ 取得失敗時は空リスト
    """
    try:
        sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
        days = _WINDOW_DAYS_DEFAULT if days is None else days
        cik = _get_cik(ticker, cfg)
        if not cik:
            return []
        time.sleep(_FETCH_DELAY)
        return _fetch_recent_form4_filings(cik, days)
    except Exception:
        return []


def get_insider_summary(
    ticker: str,
    days:   int | None = None,
    cfg=None,
) -> dict:
    """
    インサイダー取引の詳細サマリーを返す。

    キャッシュ確認 → EDGAR取得・パース → スコアリング → キャッシュ保存の順で処理。
    EDGAR 取得失敗時は yfinance フォールバックを使用。

    Args:
        ticker: "AAPL" 形式
        days:   集計ウィンドウ（日）
        cfg:    config モジュール（None 可）

    Returns:
        {
          "ticker":         str,
          "insider_bonus":  float,     # us_stock_screener に渡す値
          "buy_count":      int,
          "sell_count":     int,
          "buyer_roles":    list[str],
          "net_buy_value":  float,
          "source":         str,       # "edgar" | "yfinance" | "cache" | "error"
          "summary":        str,
          "fetched_at":     str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    days = _WINDOW_DAYS_DEFAULT if days is None else days
    _default = {
        "ticker":        ticker,
        "insider_bonus": 0.0,
        "buy_count":     0,
        "sell_count":    0,
        "buyer_roles":   [],
        "net_buy_value": 0.0,
        "source":        "error",
        "summary":       "データ取得失敗",
        "fetched_at":    datetime.now().isoformat(),
    }

    try:
        # ── ① キャッシュ確認 ─────────────────────────────────────
        cache = _load_form4_cache(cfg)
        if ticker in cache:
            cached = cache[ticker]
            return {**_default, **cached, "source": "cache"}

        # ── ② EDGAR CIK 取得 ─────────────────────────────────────
        cik = _get_cik(ticker, cfg)
        if not cik:
            # CIK 未解決 → yfinance フォールバック
            bonus = _calc_insider_bonus_yf(ticker, days)
            result = {
                **_default,
                "insider_bonus": bonus,
                "source":        "yfinance",
                "summary":       f"yfinance fallback (CIK未解決) bonus={bonus:+.1f}pt",
            }
            _save_form4_cache(ticker, result, cfg)
            return result

        # ── ③ Form 4 提出リスト取得 ───────────────────────────────
        filings = _fetch_recent_form4_filings(cik, days)
        if not filings:
            # 提出なし or 取得失敗 → yfinance フォールバック
            bonus = _calc_insider_bonus_yf(ticker, days)
            result = {
                **_default,
                "insider_bonus": bonus,
                "source":        "yfinance",
                "summary":       f"yfinance fallback (Form4なし) bonus={bonus:+.1f}pt",
            }
            _save_form4_cache(ticker, result, cfg)
            return result

        # ── ④ 個別 Form 4 XML をパース ───────────────────────────
        cik_int     = int(cik)
        parsed_list = []

        for filing in filings[:_MAX_FORM4_PARSE]:
            try:
                acc_no  = filing["accession_number"]
                doc     = filing["primary_document"]
                if not doc or not doc.lower().endswith(".xml"):
                    # XML 以外（HTML）のケースは暫定スキップ
                    continue
                xml_text = _fetch_form4_xml(cik_int, acc_no, doc)
                if xml_text:
                    parsed = _parse_form4_xml(xml_text)
                    if parsed:
                        parsed_list.append(parsed)
            except Exception:
                continue

        # ── ⑤ スコアリング ───────────────────────────────────────
        if parsed_list:
            score_result = _score_filings(parsed_list, days)
            result = {
                **_default,
                **score_result,
                "ticker":     ticker,
                "source":     "edgar",
                "fetched_at": datetime.now().isoformat(),
            }
        else:
            # XML パース失敗 → yfinance フォールバック
            bonus = _calc_insider_bonus_yf(ticker, days)
            result = {
                **_default,
                "insider_bonus": bonus,
                "source":        "yfinance",
                "summary":       f"yfinance fallback (XMLパース失敗) bonus={bonus:+.1f}pt",
            }

        _save_form4_cache(ticker, result, cfg)
        return result

    except Exception as e:
        result = {**_default, "summary": f"エラー: {str(e)[:60]}"}
        return result


def calc_insider_bonus(
    ticker: str,
    days:   int | None = None,
    cfg=None,
) -> float:
    """
    インサイダーボーナス（-5.0〜+5.0）を返す単純ラッパー。

    us_stock_screener.py の _calc_scores() から try/except 経由で呼ぶ:
        from us_insider_form4_scraper import calc_insider_bonus
        try:
            insider_bonus = calc_insider_bonus(ticker, cfg=cfg)
        except Exception:
            insider_bonus = 0.0

    Returns:
        float: insider_bonus
    """
    try:
        result = get_insider_summary(ticker, days, cfg)
        return float(result.get("insider_bonus", 0.0))
    except Exception:
        return 0.0


# ==============================================================
# BLOCK 9: バッチ処理ユーティリティ
# ==============================================================

def batch_calc_insider_bonuses(
    tickers:    list[str],
    days:       int | None = None,
    cfg=None,
    max_tickers: int  = 30,
) -> dict[str, float]:
    """
    複数ティッカーのインサイダーボーナスを一括算出する。

    Args:
        tickers:     ["AAPL", "MSFT", ...] 形式
        days:        集計ウィンドウ（日）
        cfg:         config モジュール（None 可）
        max_tickers: 1回の呼び出しで処理する最大件数

    Returns:
        {"AAPL": 2.5, "MSFT": 0.0, ...}
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    results: dict[str, float] = {}
    for t in tickers[:max_tickers]:
        try:
            results[t] = calc_insider_bonus(t, days, cfg)
        except Exception:
            results[t] = 0.0
    return results


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print("  us_insider_form4_scraper.py v1.0 単体テスト")
    print("=" * 60)

    class _TmpCfg:
        CACHE_DIR = Path(tempfile.mkdtemp())

    _cfg = _TmpCfg()

    # ── スコアリングロジック単体テスト ────────────────────────
    print("\n── _score_filings テスト（ネットワーク不要）──")

    mock_parsed_list = [
        # CEO が $2M 購入
        {
            "officer_title":    "chief executive officer",
            "is_officer":       True,
            "is_director":      True,
            "is_ten_pct_owner": False,
            "transactions": [
                {"type": "P", "date": datetime.now().strftime("%Y-%m-%d"),
                 "shares": 10000, "price": 200.0, "value_usd": 2_000_000,
                 "is_deriv": False},
            ],
        },
        # CFO が $300K 購入
        {
            "officer_title":    "chief financial officer",
            "is_officer":       True,
            "is_director":      False,
            "is_ten_pct_owner": False,
            "transactions": [
                {"type": "P", "date": datetime.now().strftime("%Y-%m-%d"),
                 "shares": 1500, "price": 200.0, "value_usd": 300_000,
                 "is_deriv": False},
            ],
        },
        # Director が $50K 売却
        {
            "officer_title":    "director",
            "is_officer":       False,
            "is_director":      True,
            "is_ten_pct_owner": False,
            "transactions": [
                {"type": "S", "date": datetime.now().strftime("%Y-%m-%d"),
                 "shares": 250, "price": 200.0, "value_usd": 50_000,
                 "is_deriv": False},
            ],
        },
    ]

    score = _score_filings(mock_parsed_list)
    print(f"  insider_bonus  : {score['insider_bonus']:+.2f}pt")
    print(f"  buy_count      : {score['buy_count']}")
    print(f"  sell_count     : {score['sell_count']}")
    print(f"  net_buy_value  : ${score['net_buy_value']:,.0f}")
    print(f"  summary        : {score['summary']}")

    # ── 純売却ケース ──────────────────────────────────────────
    print("\n── 純売却ケース ──")
    sell_parsed = [
        {
            "officer_title":    "chief executive officer",
            "is_officer":       True,
            "is_director":      False,
            "is_ten_pct_owner": False,
            "transactions": [
                {"type": "S", "date": datetime.now().strftime("%Y-%m-%d"),
                 "shares": 50000, "price": 200.0, "value_usd": 10_000_000,
                 "is_deriv": False},
            ],
        },
    ]
    score2 = _score_filings(sell_parsed)
    print(f"  CEO 大量売却 → bonus={score2['insider_bonus']:+.2f}pt")

    # ── キャッシュ保存・読み込みテスト ───────────────────────
    print("\n── キャッシュ I/O テスト ──")
    mock_result = {
        "ticker": "AAPL", "insider_bonus": 2.5,
        "buy_count": 2, "sell_count": 0,
        "buyer_roles": ["ceo", "cfo"],
        "net_buy_value": 2_300_000,
        "source": "edgar",
        "summary": "test",
    }
    _save_form4_cache("AAPL", mock_result, _cfg)
    loaded = _load_form4_cache(_cfg)
    print(f"  保存後ロード: AAPL.insider_bonus = "
          f"{loaded.get('AAPL', {}).get('insider_bonus', 'N/A')}")

    # ── Form 4 XML パーステスト ───────────────────────────────
    print("\n── _parse_form4_xml テスト（モック XML）──")
    mock_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTransaction>
    <transactionDate><value>2026-05-20</value></transactionDate>
    <transactionCoding><transactionType>P</transactionType></transactionCoding>
    <transactionAmounts>
      <transactionShares><value>5000</value></transactionShares>
      <transactionPricePerShare><value>195.50</value></transactionPricePerShare>
    </transactionAmounts>
  </nonDerivativeTransaction>
</ownershipDocument>"""

    parsed = _parse_form4_xml(mock_xml)
    if parsed:
        print(f"  officer_title: {parsed['officer_title']}")
        print(f"  is_officer   : {parsed['is_officer']}")
        print(f"  transactions : {len(parsed['transactions'])}件")
        if parsed["transactions"]:
            tx = parsed["transactions"][0]
            print(f"  tx: type={tx['type']} value=${tx['value_usd']:,.0f}")
    else:
        print("  ⚠️  パース失敗")

    print("\n✅ 単体テスト完了")
    print("  ※ 実EDGAR接続テストは環境により calc_insider_bonus('AAPL') で確認")
