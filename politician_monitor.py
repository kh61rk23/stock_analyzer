# ==============================================================
# politician_monitor.py  v1.0  (Policy Ph1)
# 米国下院 STOCK Act 議員取引監視モジュール
# ==============================================================
# 【役割】
#   米国下院の公開XML（Periodic Transaction Reports: PTR）を
#   週2回（火・金）取得・パース・差分検出し、
#   「誰が × 何を × どの業種と整合 × 取引規模」でスコアリングする。
#
# 【スコア構造】
#   base_score     = size_score（取引規模 20〜100pt）
#   committee_mult = 委員会所属による重み（1.0〜1.6）
#   sector_bonus   = GICS→JP業種整合時 +10pt
#   buy_bonus      = 買い取引 +8pt / 売り取引 +0pt
#   final_score    = min(100, base_score * committee_mult
#                        + sector_bonus + buy_bonus)
#   ★ ALERT_POLITICIAN_MIN_SCORE(デフォルト60) 以上 → LINE即時通知
#
# 【生成ファイル】
#   CACHE_DIR/politician_history.json  TTL: 180日, 日次更新
#   CACHE_DIR/event_cache.json         TTL:   7日, 日次更新
#
# 【依存】
#   config.py（BLOCK 26定数参照）
#   requests, xml.etree.ElementTree（標準）
#   ★ 外部 import は try/except で保護し、取得失敗時は空結果を返す
#
# 【呼出】
#   from politician_monitor import run_politician_monitor
#   result = run_politician_monitor(cfg)
#
# 【返り値スキーマ】
#   {
#     "new_transactions":  list,   # 今回新規検出した取引
#     "high_score_trades": list,   # score >= ALERT_POLITICIAN_MIN_SCORE
#     "jp_sector_signals": dict,   # {sector_code: delta_pt} JP波及シグナル
#     "stats": {
#       "total_fetched":   int,
#       "new_count":       int,
#       "high_score_count":int,
#       "fetch_error":     bool,
#     },
#     "cached":            bool,
#     "timestamp":         str,
#   }
# ==============================================================

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

# US下院 STOCK Act PTR インデックスXML URL
# 形式: {year}FD.xml = その年の全PTR一覧（DocID・氏名・FilingType）
# 置き場は financial-pdfs/ 配下（ptr-pdfs/ 配下は404）。
_PTR_INDEX_URL_TMPL = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.xml"
)
# 個別開示は **PDFのみ** で提供される（.xml は存在しない）。
_PTR_DETAIL_URL_TMPL = (
    "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{fileid}.pdf"
)

# PDF本文から1取引を切り出す。実データの並びは
#   Amazon.com, Inc. - Common Stock / (AMZN) [ST] / S (partial)
#   / 03/16/2026 03/16/2026 / $1,001 - $15,000
# 金額が行をまたぐ（"$15,001 -" 改行 "$50,000"）ことがあるため、
# 照合前に空白を1つへ潰してから当てる。
# 銘柄コードが数字始まりのもの（米国債のCUSIP "91282CGJ4" 等）は
# 株式ではないので拾わない。
_PTR_TX_RE = re.compile(
    r"\(([A-Z][A-Z0-9.\-]{0,9})\)\s*\[(\w{2,4})\]\s*"
    r"([PSE])\s*(\(partial\))?\s*"
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s*"
    r"(\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+)",
    re.IGNORECASE,
)
# 資産名は「(TICKER) [」の直前にある。ただし実データでは
# "Ferguson Enterprises Inc." 改行 "Stock" のように行を跨ぐため、
# 直前2行までを候補にして連結する（1行だけ見ると "Stock" になる）。
_PTR_ASSET_RE = re.compile(
    r"(?:([^\n]{0,80})\n)?([^\n]{0,80}?)\s*\(([A-Z][A-Z0-9.\-]{0,9})\)\s*\[")

# 資産名の前段に紛れ込む表ヘッダ・前取引の残骸を落とす
_PTR_ASSET_NOISE = re.compile(
    r"(\$[\d,]+|\d{2}/\d{2}/\d{4}|Gains >|\$200\?|^[PSE]$|\(partial\)"
    r"|Amount|Cap\.|Notification|Date|Owner Asset|Transaction|Type|ID)",
    re.IGNORECASE)


def _clean_asset_name(prev_line: str, line: str) -> str:
    """(TICKER) 直前の1〜2行から資産名を組み立てる。"""
    parts = []
    for chunk in (prev_line or "", line or ""):
        chunk = " ".join(chunk.split())
        if not chunk or _PTR_ASSET_NOISE.search(chunk):
            parts = [] if chunk and _PTR_ASSET_NOISE.search(chunk) else parts
            continue
        parts.append(chunk)
    name = " ".join(parts).strip(" -")
    # 直前行の取引種別カラム（"S" "P" が連結した "SP" 等）が
    # 資産名の頭に混ざることがあるので落とす
    return re.sub(r"^[PSE]{1,2}\s+", "", name).strip(" -")

# 上院 eFD の新規取得は行わない。efts.senate.gov は実在しないホストで、
# 実在する efdsearch.senate.gov は利用規約同意のセッションCookieを要求する
# （無いと403）。取得するには別実装が必要。
# スコアリング側（_score_senate_transaction / 委員会係数）は
# politician_history.json に残る既存の上院データを再スコアするため残置。

# 取引規模（House開示カテゴリ）→ size_score マッピング
_SIZE_SCORE_MAP = {
    "1":  20,   # $1,001 - $15,000
    "2":  40,   # $15,001 - $50,000
    "3":  60,   # $50,001 - $100,000
    "4":  70,   # $100,001 - $250,000
    "5":  80,   # $250,001 - $500,000
    "6":  90,   # $500,001 - $1,000,000
    "7": 100,   # Over $1,000,000
}

# 取引規模テキストラベル → size_score マッピング（XMLがテキストの場合）
_SIZE_LABEL_MAP = {
    "$1,001 - $15,000":          20,
    "$15,001 - $50,000":         40,
    "$50,001 - $100,000":        60,
    "$100,001 - $250,000":       70,
    "$250,001 - $500,000":       80,
    "$500,001 - $1,000,000":     90,
    "over $1,000,000":          100,
    "$1,000,001 - $5,000,000":  100,
    "over $5,000,000":          100,
}

# 重要委員会リスト → committee_multiplier

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'PMON_COMMITTEE_MULTIPLIER': '_COMMITTEE_MULTIPLIER',
    'B5_POLITICIAN_MONITOR_REQUEST_TIMEOUT': '_REQUEST_TIMEOUT',
    'B5_POLITICIAN_MONITOR_RETRY_COUNT': '_RETRY_COUNT',
    'B5_POLITICIAN_MONITOR_RETRY_WAIT_SEC': '_RETRY_WAIT_SEC',
}

_COMMITTEE_MULTIPLIER = {
    "ways and means":            1.6,
    "financial services":        1.5,
    "intelligence":              1.5,
    "armed services":            1.4,
    "energy and commerce":       1.4,
    "foreign affairs":           1.3,
    "judiciary":                 1.2,
    "appropriations":            1.2,
    "science, space, and technology": 1.2,
    "natural resources":         1.2,
    "agriculture":               1.1,
}

# GICS セクター（US株の主要セクター名）→ JP東証業種コードリスト
# 複数JPコードに波及させる（最大+5pt/業種）
_GICS_TO_JP_CODES: dict[str, list[str]] = {
    "Information Technology":     ["1016", "1025"],   # 電気機器, 情報通信
    "Technology":                 ["1016", "1025"],
    "Financials":                 ["1028", "1030"],   # 銀行業, 保険業
    "Financial":                  ["1028", "1030"],
    "Health Care":                ["1008"],            # 医薬品
    "Healthcare":                 ["1008"],
    "Energy":                     ["1009"],            # 石油・石炭製品
    "Materials":                  ["1007", "1012", "1013"],  # 化学, 鉄鋼, 非鉄
    "Industrials":                ["1015", "1016"],   # 機械, 電気機器
    "Consumer Discretionary":     ["1017", "1027"],   # 輸送用機器, 小売業
    "Consumer Staples":           ["1004"],            # 食料品
    "Real Estate":                ["1032"],            # 不動産業
    "Utilities":                  ["1020"],            # 電力・ガス
    "Communication Services":     ["1025"],            # 情報通信
    "Communications":             ["1025"],
    "Defense":                    ["1015"],            # 機械（防衛関連）
    "Aerospace":                  ["1015"],
    "Semiconductor":              ["1016"],            # 電気機器
    "Pharmaceuticals":            ["1008"],
    "Biotechnology":              ["1008"],
}

# HTTP リクエストヘッダー
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
        "+https://github.com/user/stock-analyzer)"
    ),
    "Accept": "application/xml, text/xml, */*",
}

_REQUEST_TIMEOUT = 20
_RETRY_COUNT     = 3
_RETRY_WAIT_SEC  = 5


# ==============================================================
# BLOCK 1: HTTP ユーティリティ
# ==============================================================

def _fetch_url(url: str) -> Optional[bytes]:
    """
    URL を取得して bytes を返す。失敗時は None。
    リトライあり（最大 _RETRY_COUNT 回）。
    """
    try:
        import requests
    except ImportError:
        print("  ⚠️  [politician] requests が未インストールです")
        return None

    for attempt in range(_RETRY_COUNT):
        try:
            resp = requests.get(
                url,
                headers=_HEADERS,
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.content
            print(f"    ⚠️  [politician] HTTP {resp.status_code}: {url[:80]}")
        except Exception as e:
            print(f"    ⚠️  [politician] 取得失敗({attempt+1}/{_RETRY_COUNT}): "
                  f"{str(e)[:60]}")
            if attempt < _RETRY_COUNT - 1:
                time.sleep(_RETRY_WAIT_SEC * (attempt + 1))
    return None


# ==============================================================
# BLOCK 2: キャッシュ管理
# ==============================================================

def _cache_dir(cfg) -> Path:
    """キャッシュディレクトリを返す（存在しなければ作成）。"""
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _to_iso_date(value) -> str:
    """取引日を "YYYY-MM-DD" に正規化する。解釈できなければ元の文字列を返す。

    下院PTRのPDFは "03/16/2026"（MM/DD/YYYY）で日付を持つ。
    ISO 前提の文字列比較（期限切れ判定・鮮度判定）と混ざると
    "03/.." < "2026-.." で常に古い扱いになるため、入口と比較の両方で揃える。
    """
    s = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def _load_politician_history(cfg) -> dict:
    """
    politician_history.json を読み込む。
    形式: {"transactions": [...], "last_updated": "YYYY-MM-DD"}
    """
    try:
        p = _cache_dir(cfg) / "politician_history.json"
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {"transactions": [], "last_updated": ""}


def _save_politician_history(cfg, history: dict) -> None:
    """politician_history.json を保存し、古い行を削除する。"""
    try:
        max_rows = int(getattr(cfg, "POLITICIAN_HISTORY_MAX_ROWS", 5000))
        ttl_days = int(getattr(cfg, "POLITICIAN_CACHE_TTL_DAYS", 180))
        cutoff   = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")

        txs = history.get("transactions", [])
        # TTL を超えた古い取引を削除
        # 取引日は PDF 由来の "MM/DD/YYYY" のことがあり、ISO の cutoff と文字列比較すると
        # 全件が期限切れ扱いで消える（履歴が空になり同じアラートが毎日再送される）。
        # 比較前に ISO へ揃える。
        txs = [t for t in txs
               if (_to_iso_date(t.get("transaction_date")) or "9999") >= cutoff]
        # 行数上限
        if len(txs) > max_rows:
            txs = txs[-max_rows:]

        history["transactions"] = txs
        history["last_updated"] = datetime.now().strftime("%Y-%m-%d")

        p = _cache_dir(cfg) / "politician_history.json"
        p.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [politician] history保存失敗: {str(e)[:60]}")


def _load_event_cache(cfg) -> dict:
    """event_cache.json（PTRインデックスキャッシュ）を読み込む。"""
    try:
        ttl_days = int(getattr(cfg, "EVENT_CACHE_TTL_DAYS", 7))
        p = _cache_dir(cfg) / "event_cache.json"
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            cached_at = raw.get("cached_at", "")
            if cached_at:
                age = (datetime.now() -
                       datetime.strptime(cached_at, "%Y-%m-%d")).days
                if age < ttl_days:
                    return raw
    except Exception:
        pass
    return {}


def _save_event_cache(cfg, data: dict) -> None:
    """event_cache.json を保存する。"""
    try:
        data["cached_at"] = datetime.now().strftime("%Y-%m-%d")
        p = _cache_dir(cfg) / "event_cache.json"
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [politician] event_cache保存失敗: {str(e)[:60]}")

# ==============================================================
# BLOCK 2.5: 上院データ取得（Policy Ph2）
# ==============================================================

def _score_senate_transaction(tx: dict, cfg=None) -> dict:
    """
    上院取引を Ph2 スコアリングする。
    politician_signal_scorer.py の score_transaction_ph2() に委譲し、
    失敗時は既存の score_transaction() にフォールバック。

    Returns:
        score_transaction() と同一フォーマットの dict（chamber="senate" 付き）
    """
    try:
        from politician_signal_scorer import score_transaction_ph2
        scored = score_transaction_ph2(
            tx         = tx,
            politician = str(tx.get("name", "")),
            chamber    = "senate",
            committees = tx.get("committees", []),
            cfg        = cfg,
        )
        scored["chamber"] = "senate"
        return scored
    except Exception:
        # フォールバック: 既存スコアリング
        try:
            result = score_transaction(tx, str(tx.get("name", "")), cfg)
            result["chamber"] = "senate"
            return result
        except Exception:
            return {**tx, "score": 0, "chamber": "senate"}

# ==============================================================
# BLOCK 3: XML 取得・パース
# ==============================================================

def _fetch_ptr_index(year: int) -> list[dict]:
    """
    PTR インデックスXMLを取得し、ファイルIDリストを返す。
    返り値: [{"file_id": str, "name": str, "year": int}, ...]
    """
    url = _PTR_INDEX_URL_TMPL.format(year=year)
    content = _fetch_url(url)
    if not content:
        return []

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"    ⚠️  [politician] XML解析失敗(index {year}): {e}")
        return []

    records = []
    skipped_non_ptr = 0
    # 一般的な要素名 <FilingData> または <Member>
    for elem in root.iter():
        tag = elem.tag.lower()
        if tag in ("filingdata", "member", "disclosure"):
            file_id = (
                _find_text(elem, ["DocID", "FileID", "ID"])
            )
            # インデックスは年次報告(W/C/X/D/A/H/T)も含む全提出の一覧で、
            # PTRのPDFが存在するのは FilingType=P だけ。絞らないと大半が404になり、
            # リトライ込みで無駄な通信が膨らむ。
            filing_type = (_find_text(elem, ["FilingType", "Type"]) or "").strip().upper()
            if filing_type and filing_type != "P":
                skipped_non_ptr += 1
                continue
            # 氏名は単一の <Name> ではなく <First>/<Last> に分かれている。
            # 議員名が取れないと委員会係数が常に 1.0 になり、スコアが実質無効化される。
            name = _find_text(elem, ["Name", "MemberName"])
            if not name:
                first = (_find_text(elem, ["First", "FirstName"]) or "").strip()
                last  = (_find_text(elem, ["Last", "LastName"]) or "").strip()
                name  = " ".join(x for x in (first, last) if x)
            if file_id:
                records.append({
                    "file_id": file_id.strip(),
                    "name":    (name or "").strip(),
                    "year":    year,
                })
    if skipped_non_ptr:
        print(f"    ℹ️  [politician] PTR以外の提出 {skipped_non_ptr}件を除外 "
              f"→ 対象 {len(records)}件")
    return records


def _find_text(elem, tag_candidates: list[str]) -> Optional[str]:
    """複数の候補タグ名を試してテキストを返す（大文字小文字非依存）。"""
    for tag in tag_candidates:
        child = elem.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        # 大文字小文字を無視して検索
        for child in elem:
            if child.tag.lower() == tag.lower():
                if child.text:
                    return child.text.strip()
    return None


def _fetch_ptr_detail(file_id: str, year: int) -> list[dict]:
    """
    個別PTRのPDFを取得し、取引リストを返す。

    下院は個別開示を **PDFでしか配信していない**（.xml は存在せず404）。
    PDFはテキスト埋め込み済みでOCR不要のため PyMuPDF で解析する。

    返り値: [{"asset","ticker","transaction_type","amount",
              "transaction_date","sector","comment","file_id","year"}, ...]
    """
    url = _PTR_DETAIL_URL_TMPL.format(year=year, fileid=file_id)
    content = _fetch_url(url)
    if not content:
        return []

    try:
        import fitz  # PyMuPDF（requirements.txt に既存）
    except ImportError:
        print("    \u26a0\ufe0f  [politician] PyMuPDF \u672a\u5c0e\u5165\u306e\u305f\u3081\u53d6\u5f15\u660e\u7d30\u3092\u89e3\u6790\u3067\u304d\u307e\u305b\u3093")
        return []

    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            raw = "\n".join(page.get_text() for page in doc)
    except Exception as e:
        print(f"    \u26a0\ufe0f  [politician] PDF\u89e3\u6790\u5931\u6557 ({file_id}): {str(e)[:60]}")
        return []

    # 資産名は改行位置に依存するので、空白を潰す前のテキストから拾う
    asset_by_ticker = {}
    for m in _PTR_ASSET_RE.finditer(raw):
        name = _clean_asset_name(m.group(1), m.group(2))
        if name:
            asset_by_ticker[m.group(3)] = name
    flat = " ".join(raw.split())

    transactions = []
    for g in _PTR_TX_RE.finditer(flat):
        ticker, sec_type, tx_type, _partial, tx_date, _notify, amount = g.groups()
        transactions.append({
            "asset":            asset_by_ticker.get(ticker, ticker),
            "ticker":           ticker,
            # score_transaction() は "p" / "s" の完全一致で売買を判定する
            "transaction_type": tx_type.lower(),
            "amount":           " ".join(amount.split()),
            "transaction_date": _to_iso_date(tx_date),   # 履歴・鮮度判定と形式を揃える
            "sector":           sec_type,
            "comment":          "",
            "file_id":          file_id,
            "year":             year,
        })
    return transactions


# ==============================================================
# BLOCK 4: 議員情報付与
# ==============================================================

# 著名な重要議員リスト（委員会情報を別途取得できない場合のフォールバック）
_NOTABLE_POLITICIANS = {
    "pelosi":       {"committee": "financial services", "party": "D"},
    "mccarthy":     {"committee": "appropriations",     "party": "R"},
    "ryan":         {"committee": "ways and means",     "party": "R"},
    "waters":       {"committee": "financial services", "party": "D"},
    "green":        {"committee": "financial services", "party": "D"},
    "smith":        {"committee": "ways and means",     "party": "R"},
    "neal":         {"committee": "ways and means",     "party": "D"},
    "reed":         {"committee": "ways and means",     "party": "R"},
    "johnson":      {"committee": "judiciary",          "party": "R"},
    "gaetz":        {"committee": "judiciary",          "party": "R"},
    "schiff":       {"committee": "intelligence",       "party": "D"},
    "nunes":        {"committee": "intelligence",       "party": "R"},
    "thornberry":   {"committee": "armed services",     "party": "R"},
    "smith_adam":   {"committee": "armed services",     "party": "D"},
}


def _get_committee_multiplier(name: str, cfg) -> tuple[float, str]:
    """
    議員名からコミッティ所属推定 → multiplier を返す。
    Returns (multiplier, committee_label)
    """
    name_lower = (name or "").lower()
    for keyword, info in _NOTABLE_POLITICIANS.items():
        if keyword in name_lower:
            committee = info["committee"]
            mult = _COMMITTEE_MULTIPLIER.get(committee, 1.0)
            return mult, committee

    # config に BUFFETT_THRESHOLD が True の場合、著名人を優先
    if getattr(cfg, "ALERT_BUFFETT_THRESHOLD", True):
        for keyword in ["pelosi", "paul", "warren", "tuberville"]:
            if keyword in name_lower:
                return 1.5, "notable_politician"

    return 1.0, "general"


# ==============================================================
# BLOCK 5: スコアリング
# ==============================================================

def _parse_size_score(amount_str: str) -> int:
    """取引規模文字列 → size_score (20〜100)"""
    if not amount_str:
        return 20

    # 数字コードの場合
    if amount_str.strip() in _SIZE_SCORE_MAP:
        return _SIZE_SCORE_MAP[amount_str.strip()]

    # テキストラベルの場合（大文字小文字無視）
    lower = amount_str.lower().replace(",", "")
    for label, score in _SIZE_LABEL_MAP.items():
        if label.lower().replace(",", "") in lower:
            return score

    # 金額が含まれている場合は数値解析
    try:
        # "$1,000,000" のような形式
        digits = "".join(c for c in amount_str if c.isdigit())
        if digits:
            val = int(digits)
            if val >= 5_000_000:  return 100
            if val >= 1_000_000:  return 100
            if val >= 500_000:    return 90
            if val >= 250_000:    return 80
            if val >= 100_000:    return 70
            if val >= 50_000:     return 60
            if val >= 15_000:     return 40
            return 20
    except Exception:
        pass

    return 30  # 不明時デフォルト


def _get_sector_alignment(
    sector_text: str,
    asset_text:  str,
) -> tuple[list[str], int]:
    """
    取引のセクター/資産名からJP業種コードと整合ボーナスを返す。
    Returns (jp_sector_codes, sector_bonus_pt)
    """
    combined = f"{sector_text} {asset_text}".strip()
    if not combined:
        return [], 0

    combined_lower = combined.lower()
    matched_codes: list[str] = []

    for gics_name, jp_codes in _GICS_TO_JP_CODES.items():
        if gics_name.lower() in combined_lower:
            matched_codes.extend(jp_codes)

    # 重複除去
    matched_codes = list(dict.fromkeys(matched_codes))

    bonus = 10 if matched_codes else 0
    return matched_codes, bonus


def score_transaction(tx: dict, politician_name: str, cfg) -> dict:
    """
    1件の取引をスコアリングして結果を返す。

    Args:
        tx:               _fetch_ptr_detail() の1要素
        politician_name:  議員名
        cfg:              config モジュール

    Returns:
        tx に "score", "committee", "jp_sector_codes" 等を追加した dict
    """
    try:
        # 取引規模スコア
        size_score = _parse_size_score(tx.get("amount", ""))

        # 委員会重み
        committee_mult, committee_label = _get_committee_multiplier(
            politician_name, cfg
        )

        # JP業種整合
        jp_codes, sector_bonus = _get_sector_alignment(
            tx.get("sector", ""),
            tx.get("asset",  ""),
        )

        # 買い/売りボーナス
        tx_type = tx.get("transaction_type", "").lower()
        if "purchase" in tx_type or "buy" in tx_type or "p" == tx_type:
            buy_bonus = 8
            direction = "buy"
        elif "sale" in tx_type or "sell" in tx_type or "s" == tx_type:
            buy_bonus = 0
            direction = "sell"
        else:
            buy_bonus = 3
            direction = "unknown"

        final_score = min(
            100,
            int(size_score * committee_mult) + sector_bonus + buy_bonus
        )

        return {
            **tx,
            "politician":       politician_name,
            "score":            final_score,
            "size_score":       size_score,
            "committee_mult":   round(committee_mult, 2),
            "committee":        committee_label,
            "sector_bonus":     sector_bonus,
            "buy_bonus":        buy_bonus,
            "direction":        direction,
            "jp_sector_codes":  jp_codes,
        }
    except Exception as e:
        return {
            **tx,
            "politician": politician_name,
            "score":      0,
            "error":      str(e)[:80],
        }


# ==============================================================
# BLOCK 6: 差分検出
# ==============================================================

def _make_tx_key(tx: dict) -> str:
    """取引の一意キーを生成する（重複検出用）。"""
    return (
        f"{tx.get('politician','')}|"
        f"{tx.get('asset','')}|"
        f"{tx.get('transaction_date','')}|"
        f"{tx.get('transaction_type','')}|"
        f"{tx.get('amount','')}"
    )


def detect_new_transactions(
    fetched:  list[dict],
    existing: list[dict],
) -> list[dict]:
    """
    fetched のうち existing にない新規取引を返す。
    """
    # 既存との比較に加え、同じ回の取得分どうしも重複を除く。
    # 修正届出（同一取引の再提出）が同じ回に入ると二重に「新規」になるため。
    seen = {_make_tx_key(t) for t in existing}
    out = []
    for t in fetched:
        key = _make_tx_key(t)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


# ==============================================================
# BLOCK 7: JP波及シグナル集計
# ==============================================================

def _build_jp_sector_signals(
    high_score_trades: list[dict],
    max_boost_pt: float = 5.0,
) -> dict[str, float]:
    """
    高スコア取引からJP業種への波及シグナルを集計する。
    買い取引 → +pt / 売り取引 → -pt（上限 ±max_boost_pt/業種）

    Returns:
        {sector_code: delta_pt, ...}
    """
    signals: dict[str, float] = {}
    for tx in high_score_trades:
        jp_codes  = tx.get("jp_sector_codes", [])
        direction = tx.get("direction", "unknown")
        score     = tx.get("score", 0)

        # スコア60以上のみ波及
        if score < 60:
            continue

        # 波及量: スコアに比例（最大 max_boost_pt）
        delta = round((score / 100) * max_boost_pt, 2)
        if direction == "sell":
            delta = -delta

        for code in jp_codes:
            current = signals.get(code, 0.0)
            # 上限クランプ
            new_val = max(-max_boost_pt, min(max_boost_pt, current + delta))
            signals[code] = round(new_val, 2)

    return signals


# ==============================================================
# BLOCK 8: メイン実行フロー
# ==============================================================

def run_politician_monitor(cfg) -> dict:
    """
    stock_analyzer_main.py の run_all_tasks() から呼ばれるエントリポイント。

    処理フロー:
      1. event_cache.json が有効 → キャッシュからPTRインデックスを使用
      2. キャッシュ失効 → 今年のPTRインデックスXMLをフェッチ
      3. 各PTRを取得・スコアリング（差分分のみ）
      4. politician_history.json に追記
      5. 高スコア取引を抽出してJP波及シグナルを生成
      6. 結果を返す

    Returns:
        {
          "new_transactions":  list,
          "high_score_trades": list,
          "jp_sector_signals": dict,
          "stats":             dict,
          "cached":            bool,
          "timestamp":         str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_POLITICIAN_MONITOR", True):
        print("  ⏩ 議員取引監視: スキップ（ENABLE_POLITICIAN_MONITOR=False）")
        return {"skipped": True, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    print(f"\n  🏛️  議員取引監視（Wave 3 Policy Ph2: 上院対応版）実行中...")

    # ── 1. 既存ヒストリー読み込み ──────────────────────────────
    history = _load_politician_history(cfg)
    existing_txs: list[dict] = history.get("transactions", [])

    # ── 2. 取得対象年を決定 ────────────────────────────────────
    current_year = datetime.now().year
    target_years: list[int] = list(getattr(cfg, "POLITICIAN_TARGET_YEARS", []) or [])
    if not target_years:
        target_years = [current_year]

    # ── 3. PTR インデックス取得（event_cache活用）──────────────
    event_cache  = _load_event_cache(cfg)
    all_ptr_records: list[dict] = event_cache.get("ptr_records", [])
    cache_hit    = bool(all_ptr_records)

    if not cache_hit:
        print(f"    📡 PTRインデックス取得中（{target_years}年分）...")
        fetch_errors = 0
        for year in target_years:
            try:
                records = _fetch_ptr_index(year)
                all_ptr_records.extend(records)
                print(f"    ✅ {year}年: {len(records)}件取得")
            except Exception as e:
                fetch_errors += 1
                print(f"    ⚠️  {year}年 インデックス取得失敗: {str(e)[:60]}")

        if all_ptr_records:
            _save_event_cache(cfg, {"ptr_records": all_ptr_records})
    else:
        print(f"    ✅ event_cache 使用: {len(all_ptr_records)}件")

    if not all_ptr_records:
        print("    ⚠️  PTRレコード0件 → スキップ")
        return {
            "new_transactions":  [],
            "high_score_trades": [],
            "jp_sector_signals": {},
            "stats": {
                "total_fetched": 0, "new_count": 0,
                "high_score_count": 0, "fetch_error": True,
            },
            "cached":    False,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 4. 差分: 既存にない file_id を対象に詳細取得 ──────────
    # 処理済み file_id は取引履歴とは別に持つ。取引日が保存期限（180日）より古い届出は
    # 保存時に取引ごと消えるため、履歴の file_id だけで判定すると毎日同じ届出を
    # 取り直して「新規」と数えてしまう。
    processed_ids = {str(x) for x in history.get("processed_file_ids", [])}
    existing_file_ids = {t.get("file_id") for t in existing_txs} | processed_ids

    # 未取得の PTR レコードのみ処理（最大200件/回 でAPI負荷を抑制）
    new_records = [
        r for r in all_ptr_records
        if r.get("file_id") not in existing_file_ids
    ]
    new_records = new_records[:200]

    print(f"    📋 新規PTRレコード: {len(new_records)}件を処理")

    all_fetched_txs: list[dict] = []
    detail_errors   = 0

    for i, record in enumerate(new_records):
        file_id = record.get("file_id", "")
        name    = record.get("name", "不明")
        year    = record.get("year", current_year)

        try:
            detail_txs = _fetch_ptr_detail(file_id, year)
            for tx in detail_txs:
                scored = score_transaction(tx, name, cfg)
                all_fetched_txs.append(scored)
            if file_id:
                processed_ids.add(str(file_id))
            # レート制限対策
            if (i + 1) % 10 == 0:
                time.sleep(1.0)
        except Exception as e:
            detail_errors += 1
            if detail_errors <= 5:
                print(f"    ⚠️  PTR詳細取得失敗 [{file_id}]: {str(e)[:60]}")

    # ── 5. 差分検出 ────────────────────────────────────────────
    truly_new = detect_new_transactions(all_fetched_txs, existing_txs)
    print(f"    📊 新規取引: {len(truly_new)}件")

    # ── 6. ヒストリーに追記して保存 ────────────────────────────
    history["transactions"] = existing_txs + truly_new
    history["processed_file_ids"] = sorted(processed_ids)[-20000:]
    _save_politician_history(cfg, history)

    # ── 6.5. 上院データ取得は停止中（efts.senate.gov が実在しないため） ──
    # 再開する場合、truly_new への追加は history 保存（上の行）より前に行うこと。
    senate_txs: list[dict] = []
    senate_errors = 0

    # ── 7. 高スコア取引を抽出 ──────────────────────────────────
    min_score = int(getattr(cfg, "ALERT_POLITICIAN_MIN_SCORE", 60))
    # 「新規」は“履歴に無い”という意味でしかなく、初回や履歴が空のときは
    # 年初以降の全取引が新規扱いになる。数ヶ月前の売買で今日の業種スコアを
    # 補正しないよう、取引日の鮮度で絞る。
    max_age = int(getattr(cfg, "POLITICIAN_ALERT_MAX_AGE_DAYS", 60))
    age_cut = (datetime.now() - timedelta(days=max_age)).strftime("%Y-%m-%d")
    high_score_trades = [
        t for t in truly_new
        if t.get("score", 0) >= min_score
        and (_to_iso_date(t.get("transaction_date")) or "9999") >= age_cut
    ]
    high_score_trades.sort(key=lambda x: x.get("score", 0), reverse=True)

    # ── 8. JP波及シグナル生成 ──────────────────────────────────
    jp_signals = _build_jp_sector_signals(high_score_trades)

    if high_score_trades:
        print(f"    ⭐ 高スコア取引: {len(high_score_trades)}件"
              f"（閾値:{min_score}点）")
        for t in high_score_trades[:3]:
            print(f"       [{t.get('score',0):.0f}pt] "
                  f"{t.get('politician','')} "
                  f"{'買' if t.get('direction')=='buy' else '売'} "
                  f"{t.get('asset','')[:30]}")
    if jp_signals:
        print(f"    🇯🇵 JP波及シグナル: "
              f"{len(jp_signals)}業種に補正")

    # house / senate 別に分離してカウント
    house_new   = [t for t in truly_new if t.get("chamber", "house") == "house"]
    senate_new  = [t for t in truly_new if t.get("chamber") == "senate"]

    result = {
        "new_transactions":  truly_new,
        "high_score_trades": high_score_trades,
        "jp_sector_signals": jp_signals,
        "stats": {
            "total_fetched":      len(all_fetched_txs),
            "new_count":          len(truly_new),
            "house_new_count":    len(house_new),
            "senate_new_count":   len(senate_new),
            "high_score_count":   len(high_score_trades),
            "fetch_error":        detail_errors > 0,
            "detail_errors":      detail_errors,
            "senate_errors":      senate_errors,
        },
        "cached":    cache_hit,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    print(f"  ✅ 議員取引監視完了: "
          f"新規{len(truly_new)}件 / 高スコア{len(high_score_trades)}件")
    return result


# ==============================================================
# BLOCK 9: 単体テスト用エントリポイント
# ==============================================================

if __name__ == "__main__":
    import importlib.util, sys

    cfg = None
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            if spec and spec.loader:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    if cfg is None:
        print("ERROR: config.py が読み込めません")
        sys.exit(1)

    result = run_politician_monitor(cfg)
    print("\n--- 結果サマリー ---")
    stats = result.get("stats", {})
    print(f"取得合計  : {stats.get('total_fetched', 0)}件")
    print(f"新規検出  : {stats.get('new_count', 0)}件")
    print(f"高スコア  : {stats.get('high_score_count', 0)}件")
    print(f"JP波及    : {result.get('jp_sector_signals', {})}")

    high = result.get("high_score_trades", [])
    if high:
        print("\n--- 高スコア取引 TOP5 ---")
        for t in high[:5]:
            print(f"  [{t.get('score',0):3d}pt] "
                  f"{t.get('politician',''):20s} "
                  f"{'買' if t.get('direction')=='buy' else '売'} "
                  f"{t.get('asset','')[:40]}")
