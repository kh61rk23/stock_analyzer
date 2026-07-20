# ==============================================================
# text_extractor.py  v1.0  (Wave 5 - E4-1)
# EDINET XBRL から有報テキストセクションを抽出する
# ==============================================================
# 【役割】
#   edinet_direct_fetcher.py が BigQuery に格納した XBRL データ、
#   または EDINET API から直接取得した HTML/XBRL から
#   以下4セクションのテキストを抽出し dict 形式で返す。
#
#   抽出セクション:
#     "business"  : 事業の内容（第一部 第1 事業の概況）
#     "risk"      : リスク情報（第一部 第2 リスク情報）
#     "strategy"  : 経営方針・経営戦略等（第一部 第4 経営方針）
#     "governance": コーポレート・ガバナンスの概要（第二部）
#
# 【取得ルート（3段フォールバック）】
#   1. BigQuery（edinet_bq_client 経由）
#      edinet_direct_fetcher が格納した xbrl_text_sections を優先参照
#   2. EDINET API（直接取得）
#      edinet_doc_indexer.py の doc_index から docID を取得し
#      書類一覧API → 添付書類（HTML）をダウンロードして解析
#   3. キャッシュ（stock_data/cache/text_extractor/）
#      TTL = TEXT_SIGNAL_TTL_DAYS (30日)
#
# 【出力スキーマ】
#   {
#     "securities_code": "1234",
#     "company_name":    str,
#     "period_end":      "2025-03",
#     "sections": {
#       "business":   str,   # 事業の内容 (最大 3000文字)
#       "risk":       str,   # リスク情報 (最大 5000文字)
#       "strategy":   str,   # 経営方針   (最大 3000文字)
#       "governance": str,   # ガバナンス  (最大 2000文字)
#     },
#     "source":  "bq" | "edinet_api" | "cache",
#     "fetched_at": str (ISO8601),
#   }
#   取得失敗時: None を返す（呼び出し側でフォールバック）
#
# 【依存】
#   config.py, edinet_bq_client.py（オプション）, requests（オプション）
#
# 【呼び出し】
#   from text_extractor import extract_sections, batch_extract_sections
# ==============================================================

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

# セクション最大文字数（FinBERT / text_signal の推論コスト制御）
_SECTION_MAX_CHARS: dict[str, int] = {
    "business":   3000,
    "risk":       5000,
    "strategy":   3000,
    "governance": 2000,
}

# XBRL 要素名 → セクションキーのマッピング（arelle パース済みデータ用）
# EDINET XBRL タクソノミ準拠の要素名を網羅
_XBRL_ELEMENT_MAP: dict[str, str] = {
    # 事業の内容
    "jpcrp030000-asr:DescriptionOfBusinessTextBlock":                     "business",
    "jpcrp030000-asr:BusinessDescriptionTextBlock":                       "business",
    "jpcrp030000-asr:BusinessDescriptionAndPrincipalBusinessLocationsTextBlock": "business",
    # リスク情報
    "jpcrp030000-asr:RiskFactorsTextBlock":                               "risk",
    "jpcrp030000-asr:SignificantRisksInBusinessActivitiesTextBlock":      "risk",
    # 経営方針・経営戦略
    "jpcrp030000-asr:ManagementPoliciesAndStrategiesTextBlock":           "strategy",
    "jpcrp030000-asr:ManagementPoliciesTargetsAndManagementStrategiesTextBlock": "strategy",
    "jpcrp030000-asr:BusinessPoliciesBusinessEnvironmentProblemsTextBlock": "strategy",
    # コーポレート・ガバナンス
    "jpcrp030000-asr:ExplanationAboutCorporateGovernanceTextBlock":       "governance",
    "jpcrp030000-asr:OutlineOfCorporateGovernanceTextBlock":              "governance",
}

# EDINET API HTML パース用: セクション見出しキーワード
# HTMLの <h3>/<h4> タグ内テキストとマッチングしてセクションを特定する
_HTML_SECTION_KEYWORDS: dict[str, list[str]] = {
    "business": [
        "事業の内容", "事業の概況", "主要な事業",
        "事業内容", "ビジネスの内容",
    ],
    "risk": [
        "リスク情報", "リスク要因", "事業等のリスク",
        "投資者が判断するために必要なリスク", "主要なリスク",
    ],
    "strategy": [
        "経営方針", "経営戦略", "経営上の目標",
        "対処すべき課題", "会社の経営の基本方針",
        "経営環境", "中期経営計画",
    ],
    "governance": [
        "コーポレート・ガバナンス", "コーポレートガバナンス",
        "企業統治", "ガバナンスの概要",
        "取締役会の構成", "内部統制",
    ],
}

# EDINET API エンドポイント
_EDINET_API_BASE        = "https://disclosure.edinet-fsa.go.jp/api/v2"
_EDINET_DOC_LIST_URL    = _EDINET_API_BASE + "/documents.json"
_EDINET_DOC_DETAIL_URL  = _EDINET_API_BASE + "/documents/{docID}"

# API レート制限対策（秒）
_EDINET_API_WAIT_SEC = 0.5


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_dir(cfg) -> Path:
    """text_extractor 専用キャッシュフォルダを返す。"""
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    return base / "text_extractor"


def _cache_path(cfg, securities_code: str) -> Path:
    return _cache_dir(cfg) / f"{securities_code}.json"


def _load_cache(cfg, securities_code: str) -> Optional[dict]:
    """TTL 以内のキャッシュがあれば返す。"""
    p = _cache_path(cfg, securities_code)
    if not p.exists():
        return None
    try:
        data    = json.loads(p.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(data.get("fetched_at", "2000-01-01"))
        ttl     = getattr(cfg, "TEXT_SIGNAL_TTL_DAYS", 30)
        if datetime.now() - fetched <= timedelta(days=ttl):
            return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    """抽出結果をキャッシュに保存する。"""
    code = data.get("securities_code", "unknown")
    p    = _cache_path(cfg, code)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  [text_extractor] キャッシュ保存失敗 ({code}): {e}")


# ==============================================================
# BLOCK 2: BigQuery からのテキスト取得
# ==============================================================

def _extract_from_bq(cfg, securities_code: str) -> Optional[dict]:
    """
    edinet_bq_client 経由で BigQuery に格納された XBRL テキストを取得する。

    Returns:
        sections dict または None（BQ 未格納・接続失敗時）
    """
    try:
        from edinet_bq_client import EDINETBQClient
        bq   = EDINETBQClient()
        # get_xbrl_text_sections は EDINET 生テキスト（element_name/text_value）の
        # BQ 格納を前提とした将来機能。現状その格納テーブル/メソッドは未実装の
        # ため、未提供時は静かに None を返し EDINET API ルートへフォールバックする。
        # （旧コードは未実装メソッドを直接呼び、23銘柄ぶん AttributeError を
        #   毎回ログに吐いていた / CLAUDE.md パターン②「幻のAPI」。）
        if not hasattr(bq, "get_xbrl_text_sections"):
            return None
        rows = bq.get_xbrl_text_sections(securities_code)
        if not rows:
            return None

        sections: dict[str, str] = {}
        company_name = ""
        period_end   = ""

        for row in rows:
            element  = row.get("element_name", "")
            text_val = row.get("text_value", "") or ""
            sec_key  = _XBRL_ELEMENT_MAP.get(element)

            if not company_name:
                company_name = row.get("company_name", "")
            if not period_end:
                period_end = str(row.get("period_end", ""))

            if sec_key and sec_key not in sections and text_val:
                # HTML タグを除去して最大文字数でトリム
                clean = _strip_html(text_val)
                sections[sec_key] = clean[:_SECTION_MAX_CHARS[sec_key]]

        if not sections:
            return None

        return {
            "securities_code": securities_code,
            "company_name":    company_name,
            "period_end":      period_end,
            "sections":        _fill_missing_sections(sections),
            "source":          "bq",
            "fetched_at":      datetime.now().isoformat(),
        }

    except ModuleNotFoundError:
        return None
    except Exception as e:
        print(f"    ⚠️  [text_extractor] BQ取得失敗 ({securities_code}): "
              f"{str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 3: EDINET API からのテキスト取得
# ==============================================================

def _get_doc_id_from_index(cfg, securities_code: str) -> Optional[str]:
    """
    edinet_doc_indexer が蓄積した doc_index から最新 docID を取得する。
    フォールバック: EDINET API に直接問い合わせる。

    Returns:
        docID 文字列 または None
    """
    # まず BQ の doc_index テーブルを確認
    try:
        from edinet_bq_client import EDINETBQClient
        bq    = EDINETBQClient()
        doc_id = bq.get_latest_doc_id(securities_code, doc_type="120")  # 120=有価証券報告書
        if doc_id:
            return doc_id
    except Exception:
        pass

    # EDINET API に直接問い合わせ（有報書類一覧取得）
    try:
        import urllib.request, urllib.parse
        # 直近1年分の書類一覧を検索（日付範囲を広めに設定）
        from_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        to_date   = datetime.now().strftime("%Y-%m-%d")
        params    = urllib.parse.urlencode({
            "date":       to_date,
            "type":       "2",   # 2=書類一覧（有報含む）
            "Subscription-Key": getattr(cfg, "EDINET_API_KEY", ""),
        })
        url = f"{_EDINET_DOC_LIST_URL}?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "stock_analyzer/1.0 (text_extractor)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = data.get("results", []) or []
        for item in results:
            if (str(item.get("edinetCode", "")) == securities_code
                    and item.get("docTypeCode") == "120"):
                return item.get("docID")
    except Exception as e:
        print(f"    ⚠️  [text_extractor] EDINET docID取得失敗 ({securities_code}): "
              f"{str(e)[:60]}")

    return None


def _download_edinet_html(cfg, doc_id: str) -> Optional[str]:
    """
    EDINET API から有報 HTML 本文をダウンロードして返す。
    書類ファイル一覧から本文 HTML（序数1）を取得する。

    Returns:
        HTML 文字列 または None
    """
    if not doc_id:
        return None

    api_key = getattr(cfg, "EDINET_API_KEY", "")
    if not api_key:
        return None

    try:
        import urllib.request
        # 書類ファイル一覧取得
        list_url = (
            f"{_EDINET_API_BASE}/documents/{doc_id}"
            f"?type=4&Subscription-Key={api_key}"
        )
        req = urllib.request.Request(
            list_url,
            headers={"User-Agent": "stock_analyzer/1.0 (text_extractor)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            files_data = json.loads(resp.read().decode("utf-8"))

        # 本文 HTML ファイルを特定（sequenceNumber=1 かつ docID に一致）
        html_url = None
        for f in files_data.get("results", []) or []:
            if (f.get("type") == "1"
                    and f.get("docID") == doc_id
                    and ".htm" in (f.get("filename", "")).lower()):
                html_url = (
                    f"{_EDINET_API_BASE}/documents/{doc_id}"
                    f"?type=1&Subscription-Key={api_key}"
                )
                break

        if not html_url:
            # fallback: type=1 直接取得を試みる
            html_url = (
                f"{_EDINET_API_BASE}/documents/{doc_id}"
                f"?type=1&Subscription-Key={api_key}"
            )

        time.sleep(_EDINET_API_WAIT_SEC)
        req_html = urllib.request.Request(
            html_url,
            headers={"User-Agent": "stock_analyzer/1.0 (text_extractor)"},
        )
        with urllib.request.urlopen(req_html, timeout=30) as resp:
            raw_bytes = resp.read()

        # エンコーディング推定（有報は UTF-8 または Shift_JIS）
        for enc in ("utf-8", "shift_jis", "cp932"):
            try:
                return raw_bytes.decode(enc)
            except UnicodeDecodeError:
                continue

        return None

    except Exception as e:
        print(f"    ⚠️  [text_extractor] HTMLダウンロード失敗 ({doc_id}): "
              f"{str(e)[:60]}")
        return None


def _parse_html_sections(html: str) -> dict[str, str]:
    """
    有報 HTML からセクション別テキストを抽出する。

    アルゴリズム:
      1. <h3>/<h4>/<p class="..."> タグ内テキストを見出しとして認識
      2. _HTML_SECTION_KEYWORDS と照合してセクション種別を判定
      3. 次の見出しが来るまでの本文テキストを蓄積
      4. 最大文字数でトリム

    Returns:
        {"business": str, "risk": str, ...}  欠損キーは空文字
    """
    sections: dict[str, str] = {}
    if not html:
        return sections

    # HTML タグを除去したプレーンテキストを生成
    plain = _strip_html(html)

    # 行ベースの簡易パーサー（arelle 非依存）
    lines        = plain.splitlines()
    current_key: Optional[str] = None
    buffer: list[str]          = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 見出し判定: 短い行（100文字未満）で既知キーワードを含む
        if len(stripped) < 100:
            matched_key = _match_section_keyword(stripped)
            if matched_key:
                # 前セクションのバッファを保存
                if current_key and buffer:
                    text = " ".join(buffer)
                    if current_key not in sections:
                        sections[current_key] = text[:_SECTION_MAX_CHARS[current_key]]
                current_key = matched_key
                buffer      = []
                continue

        # 本文蓄積（現セクション確定後のみ）
        if current_key:
            buffer.append(stripped)
            # バッファ上限チェック（メモリ節約）
            if sum(len(b) for b in buffer) > _SECTION_MAX_CHARS[current_key] * 2:
                break

    # 最後のセクションを保存
    if current_key and buffer and current_key not in sections:
        text = " ".join(buffer)
        sections[current_key] = text[:_SECTION_MAX_CHARS[current_key]]

    return sections


def _match_section_keyword(text: str) -> Optional[str]:
    """テキストが _HTML_SECTION_KEYWORDS のいずれかに該当するか判定する。"""
    for sec_key, keywords in _HTML_SECTION_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return sec_key
    return None


def _extract_from_edinet_api(cfg, securities_code: str) -> Optional[dict]:
    """
    EDINET API 経由で有報 HTML を取得しセクション抽出する。

    Returns:
        sections dict または None
    """
    doc_id = _get_doc_id_from_index(cfg, securities_code)
    if not doc_id:
        return None

    html = _download_edinet_html(cfg, doc_id)
    if not html:
        return None

    sections = _parse_html_sections(html)
    if not sections:
        return None

    return {
        "securities_code": securities_code,
        "company_name":    "",   # HTML から取得困難なため空文字
        "period_end":      "",
        "sections":        _fill_missing_sections(sections),
        "source":          "edinet_api",
        "fetched_at":      datetime.now().isoformat(),
    }


# ==============================================================
# BLOCK 4: テキスト前処理ユーティリティ
# ==============================================================

# HTML タグ除去用の正規表現（コンパイル済みで高速化）
_HTML_TAG_RE    = re.compile(r"<[^>]+>")
_WHITESPACE_RE  = re.compile(r"[\u3000\s]+")   # 全角スペース・改行・タブを統一


def _strip_html(text: str) -> str:
    """HTML タグと余分な空白を除去してプレーンテキストを返す。"""
    if not text:
        return ""
    clean = _HTML_TAG_RE.sub(" ", text)
    clean = _WHITESPACE_RE.sub(" ", clean)
    return clean.strip()


def _fill_missing_sections(sections: dict) -> dict:
    """不足しているセクションキーを空文字で補完する。"""
    return {
        sec: sections.get(sec, "")
        for sec in ("business", "risk", "strategy", "governance")
    }


# ==============================================================
# BLOCK 5: メイン抽出関数（公開 API）
# ==============================================================

def extract_sections(cfg, securities_code: str) -> Optional[dict]:
    """
    1銘柄の有報テキストセクションを抽出して返す。

    取得フロー:
      1. キャッシュ（TTL 以内）→ 即返却
      2. BigQuery（edinet_bq_client 経由）
      3. EDINET API 直接取得
      4. すべて失敗 → None を返す

    Args:
        cfg:              config モジュール
        securities_code:  証券コード（例: "6501"）

    Returns:
        {
          "securities_code": str,
          "company_name":    str,
          "period_end":      str,
          "sections": {
            "business":   str,
            "risk":       str,
            "strategy":   str,
            "governance": str,
          },
          "source":     "bq" | "edinet_api" | "cache",
          "fetched_at": str,
        }
        または None（取得失敗）
    """
    if not getattr(cfg, "ENABLE_TEXT_SIGNAL", True):
        return None

    securities_code = str(securities_code).strip()
    if not securities_code:
        return None

    # ── Step 1: キャッシュ確認 ────────────────────────────────
    cached = _load_cache(cfg, securities_code)
    if cached:
        return cached

    # ── Step 2: BigQuery 取得 ─────────────────────────────────
    result = _extract_from_bq(cfg, securities_code)
    if result:
        _save_cache(cfg, result)
        return result

    # ── Step 3: EDINET API 直接取得 ───────────────────────────
    # 2026-07-19是正: 旧コードは EDINETDB_API_KEY でも本ルートを起動していたが、
    # ダウンロード側（_download_edinet_html）は EDINET_API_KEY のみ参照するため
    # EDINETDB キーだけの環境では毎銘柄2回のHTTP無駄打ち＋失敗ログになっていた。
    # 本ルートは EDINET_API_KEY（金融庁）専用。
    api_key = getattr(cfg, "EDINET_API_KEY", "")
    if api_key:
        result = _extract_from_edinet_api(cfg, securities_code)
        if result:
            _save_cache(cfg, result)
            return result
        print(f"    ⚠️  [text_extractor] {securities_code}: 全ルートで取得失敗")
    else:
        print(f"    ⚠️  [text_extractor] {securities_code}: "
              f"キャッシュなし・EDINET_API_KEY未設定（再発行後に自動復旧）")

    # ── Step 4: すべて失敗 ────────────────────────────────────
    return None


def batch_extract_sections(
    cfg,
    securities_codes: list[str],
    max_companies:    Optional[int] = None,
) -> dict[str, dict]:
    """
    複数銘柄の有報テキストセクションを一括抽出する。

    初回実行ガードレール:
      max_companies が None の場合、config.TEXT_SIGNAL_FIRST_RUN_LIMIT
      (デフォルト 50) を上限として適用する。
      これにより初回の API 負荷・処理時間を抑制する。

    Args:
        cfg:              config モジュール
        securities_codes: 証券コードのリスト
        max_companies:    処理上限数（None の場合は config から取得）

    Returns:
        {securities_code: extract_sections() の返り値}
        取得失敗した銘柄はキーごと除外される（None を含まない）
    """
    if not getattr(cfg, "ENABLE_TEXT_SIGNAL", True):
        return {}

    # 初回実行ガードレール
    limit = max_companies
    if limit is None:
        limit = getattr(cfg, "TEXT_SIGNAL_FIRST_RUN_LIMIT", 50)

    target_codes = list(dict.fromkeys(
        str(c).strip() for c in securities_codes if c
    ))[:limit]

    total   = len(target_codes)
    results = {}

    print(f"\n  📄 [text_extractor] バッチ抽出開始: {total}社")
    if not getattr(cfg, "EDINET_API_KEY", ""):
        print("  🔑 [text_extractor] EDINET_API_KEY未設定 → "
              "EDINET APIルート無効（キャッシュのみ・キー再発行後に自動復旧）")

    for i, code in enumerate(target_codes, start=1):
        try:
            data = extract_sections(cfg, code)
            if data:
                results[code] = data
                src = data.get("source", "?")
                secs_found = [k for k, v in data.get("sections", {}).items() if v]
                print(f"    [{i}/{total}] {code}: {len(secs_found)}セクション取得 "
                      f"(source={src})")
            else:
                print(f"    [{i}/{total}] {code}: スキップ（取得失敗）")
        except Exception as e:
            print(f"    [{i}/{total}] {code}: 予期しないエラー → {str(e)[:60]}")

        # EDINET API 負荷軽減（連続リクエスト間にウェイト）
        if i < total:
            time.sleep(_EDINET_API_WAIT_SEC)

    succeeded = len(results)
    print(f"  ✅ [text_extractor] バッチ完了: {succeeded}/{total}社 取得成功")
    return results


# ==============================================================
# BLOCK 6: 単体テスト
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

    print("=== text_extractor 単体テスト ===\n")

    # ── HTML パーサーのテスト ──────────────────────────────────
    mock_html = """
    <html><body>
    <h3>事業の内容</h3>
    <p>当社グループは電子部品を製造販売しております。主力製品はMLCC（積層セラミックコンデンサ）であり、
    スマートフォン・自動車・産業機器向けに展開しています。</p>
    <h3>事業等のリスク</h3>
    <p>地政学リスクとして米中摩擦による輸出規制の強化が懸念されます。
    また、原材料価格の高騰による財務悪化リスクも存在します。</p>
    <h3>経営方針、経営環境及び対処すべき課題等</h3>
    <p>中期経営計画において、グローバル市場での競争力強化を最優先課題とします。</p>
    <h3>コーポレート・ガバナンスの概要</h3>
    <p>取締役会は社外取締役3名を含む7名で構成されています。</p>
    </body></html>
    """

    parsed = _parse_html_sections(mock_html)
    print("  ── HTML パーサーテスト ──")
    for key, text in parsed.items():
        print(f"  [{key}] {len(text)}文字: {text[:50]}...")

    # ── キャッシュ保存・読み込みテスト ───────────────────────
    mock_result = {
        "securities_code": "TEST01",
        "company_name":    "テスト株式会社",
        "period_end":      "2025-03",
        "sections":        _fill_missing_sections(parsed),
        "source":          "test",
        "fetched_at":      datetime.now().isoformat(),
    }
    _save_cache(cfg, mock_result)
    loaded = _load_cache(cfg, "TEST01")
    print(f"\n  ── キャッシュテスト ──")
    print(f"  保存・読み込み: {'✅ 成功' if loaded else '❌ 失敗'}")

    print("\n  ✅ 単体テスト完了")
