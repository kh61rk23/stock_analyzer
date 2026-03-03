# ==============================================================
# edinet_client.py  v2.0  (EdinetDB対応版)
# ==============================================================
# 【変更点 v1.0 → v2.0】
#   v1.0: 金融庁EDINET APIを直接叩き、ZIP解凍→HTML解析（重い・不安定）
#   v2.0: EdinetDB（民間API）を使用（軽い・構造化済み・リスク情報あり）
#
# 【EdinetDB APIの仕様】
#   ベースURL : https://edinetdb.jp/v1
#   認証      : X-API-Key ヘッダーにAPIキーを設定
#   レート制限 : Free 100回/日
#   エンドポイント:
#     GET /v1/search?q={証券コード}    → EDINETコード検索（認証不要）
#     GET /v1/companies/{edinet_code}/analysis → AI分析・リスク情報
#     GET /v1/companies/{edinet_code}/financials → 財務データ（最大6年）
#
# 【config.py で必要な設定】
#   EDINETDB_API_KEY : EdinetDB の APIキー（ダッシュボードで発行）
#   EDINET_MAX_COMPANIES : 業種あたり取得社数（推奨: 1〜2）
#   EDINET_CACHE_DAYS    : キャッシュ有効期間（推奨: 90〜100）
#   EDINET_MAX_RISK_ITEMS: 抽出するリスク項目数（推奨: 5）
#
# 【月間API使用量の試算】
#   33業種 × 2エンドポイント/社 × 1社 = 66回/実行
#   キャッシュ100日 → 実質66回/100日 ≒ 20回/月 ✅（無料枠100回/月以内）
#
# 【依存】config.py / requests / json（標準ライブラリ）
# 【呼出】from edinet_client import get_risk_factors, get_sector_risk_factors
# ==============================================================

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

BASE_URL = "https://edinetdb.jp/v1"


# ==============================================================
# BLOCK 1: 共通ユーティリティ
# ==============================================================

def _get_headers(api_key: str) -> dict:
    """EdinetDB 認証ヘッダーを返す"""
    return {"X-API-Key": api_key, "User-Agent": "StockAnalyzer/2.0"}


def _get_cache_path(cache_dir: Path, key: str) -> Path:
    """キャッシュファイルのパスを返す"""
    safe_key = key.replace("/", "_").replace(".", "_")
    return cache_dir / f"edinetdb_{safe_key}.json"


def _load_cache(cache_path: Path, cache_days: int) -> Optional[dict]:
    """キャッシュが有効期間内であれば読み込んで返す"""
    if not cache_path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(cache_path.stat().st_mtime)
        if datetime.now() - mtime > timedelta(days=cache_days):
            return None
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(cache_path: Path, data: dict):
    """データをキャッシュファイルに保存する"""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass


# ==============================================================
# BLOCK 2: EdinetDB 企業検索（証券コード → EDINETコード変換）
# ==============================================================

def search_company(sec_code: str, api_key: str,
                   cache_dir: Optional[Path] = None,
                   cache_days: int = 90) -> Optional[dict]:
    """
    証券コード（例: "8306"）からEdinetDBの企業情報を取得する。
    EDINETコード（例: "E03518"）が含まれる。

    Args:
        sec_code  : 4桁証券コード（"8306" 形式）
        api_key   : EdinetDB APIキー
        cache_dir : キャッシュ保存先
        cache_days: キャッシュ有効日数

    Returns:
        dict | None: 企業情報（edinet_code, name, industry 等）
    """
    try:
        import requests
    except ImportError:
        return None

    # キャッシュ確認
    if cache_dir:
        cache_path = _get_cache_path(cache_dir, f"search_{sec_code}")
        cached = _load_cache(cache_path, cache_days)
        if cached:
            return cached

    # 検索APIは認証不要だが、キー付きの方がレート制限が緩い
    headers = _get_headers(api_key) if api_key else {
        "User-Agent": "StockAnalyzer/2.0"
    }

    try:
        resp = requests.get(
            f"{BASE_URL}/search",
            params={"q": sec_code},
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return None

        data = resp.json().get("data", [])
        # 証券コードが完全一致する企業を探す（5桁形式 "83060" にも対応）
        for company in data:
            raw = str(company.get("sec_code", ""))
            code4 = raw[:4] if len(raw) >= 4 else raw
            if code4 == sec_code:
                if cache_dir:
                    _save_cache(cache_path, company)
                return company

        return None

    except Exception as e:
        print(f"      ⚠️  EdinetDB 企業検索エラー ({sec_code}): {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 3: EdinetDB AI分析・リスク情報取得
# ==============================================================

def get_analysis(edinet_code: str, api_key: str,
                 cache_dir: Optional[Path] = None,
                 cache_days: int = 90) -> Optional[dict]:
    """
    EdinetDB のAI分析エンドポイントからリスク情報・財務健全性スコアを取得する。

    Returns:
        dict | None:
          {
            "credit_score": int,         # 財務健全性スコア（0-100）
            "credit_rating": str,        # "excellent"/"good"/"fair"/"poor"
            "risk_summary": str,         # リスクの要約テキスト
            "business_summary": str,     # 事業概要
            "risk_factors": [str, ...],  # リスク要因リスト
          }
    """
    try:
        import requests
    except ImportError:
        return None

    if not api_key:
        return None

    # キャッシュ確認
    if cache_dir:
        cache_path = _get_cache_path(cache_dir, f"analysis_{edinet_code}")
        cached = _load_cache(cache_path, cache_days)
        if cached:
            return cached

    try:
        resp = requests.get(
            f"{BASE_URL}/companies/{edinet_code}/analysis",
            headers=_get_headers(api_key),
            timeout=20,
        )
        if resp.status_code == 429:
            print("      ⚠️  EdinetDB レート制限（100回/日）に達しました")
            return None
        if resp.status_code != 200:
            return None

        raw = resp.json().get("data", {})

        # リスクテキストを箇条書きリストに変換
        risk_text = raw.get("risk_factors", "") or raw.get("risks", "") or ""
        risk_items = _parse_risk_items(risk_text)

        result = {
            "credit_score":    raw.get("credit_score", 0),
            "credit_rating":   raw.get("credit_rating", ""),
            "risk_summary":    raw.get("risk_summary", "")[:300],
            "business_summary": raw.get("business_summary", "")[:300],
            "risk_factors":    risk_items,
        }

        if cache_dir:
            _save_cache(cache_path, result)
        return result

    except Exception as e:
        print(f"      ⚠️  EdinetDB 分析取得エラー ({edinet_code}): {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 4: リスクテキスト整形
# ==============================================================

def _parse_risk_items(risk_text: str, max_items: int = 5) -> list:
    """
    EdinetDBから返ってくるリスクテキストを箇条書きリストに変換する。
    テキストが段落区切りの場合と番号付きの場合の両方に対応。
    """
    if not risk_text:
        return []

    import re

    # 番号付きリスト（①②③ or (1)(2)(3) or 1. 2.）で分割
    patterns = [
        r"(?:①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩)",
        r"(?:\（[0-9１-９]+\）|\([0-9]+\))",
        r"(?:^[0-9]+[．\.。])",
        r"(?:^[■●▶・])",
    ]
    combined = "|".join(patterns)
    parts = re.split(combined, risk_text, flags=re.MULTILINE)

    items = []
    for part in parts[1:]:
        cleaned = part.strip()
        if len(cleaned) < 10:
            continue
        first = cleaned.split("\n")[0][:120].strip()
        if first:
            items.append(first)
        if len(items) >= max_items:
            break

    # 番号付きが見つからない場合は改行区切りで分割
    if not items:
        lines = [l.strip() for l in risk_text.split("\n") if len(l.strip()) > 15]
        items = [l[:120] for l in lines[:max_items]]

    return items[:max_items]


# ==============================================================
# BLOCK 5: メインエントリポイント（ticker → リスクリスト）
# ==============================================================

def get_risk_factors(ticker: str, cfg) -> list:
    """
    yfinance形式のティッカー（例: "8306.T"）を受け取り、
    EdinetDB から取得した「事業のリスク」の箇条書きリストを返す。

    Args:
        ticker : "8306.T" 形式
        cfg    : config モジュール

    Returns:
        list of str: リスク要因リスト（最大 cfg.EDINET_MAX_RISK_ITEMS 件）
    """
    # EdinetDB APIキーを取得（EDINETDB_API_KEY を優先、なければ EDINET_API_KEY）
    api_key = (
        getattr(cfg, "EDINETDB_API_KEY", "")
        or getattr(cfg, "EDINET_API_KEY", "")
    )
    if not api_key:
        return []

    # 証券コードに変換（"8306.T" → "8306"）
    sec_code = ticker.replace(".T", "").replace(".OS", "").strip()
    print(f"      📄 EdinetDB 検索中: {ticker} (証券コード: {sec_code})")

    # キャッシュディレクトリ設定
    cache_dir = None
    data_dir = getattr(cfg, "DATA_DIR", None)
    if data_dir:
        cache_dir = Path(data_dir) / "edinet_cache"

    cache_days = getattr(cfg, "EDINET_CACHE_DAYS", 90)

    # ① 企業検索（証券コード → EDINETコード）
    company = search_company(sec_code, api_key, cache_dir, cache_days)
    if not company:
        print(f"      ⚠️  {ticker}: EdinetDB に企業が見つかりませんでした")
        return []

    edinet_code  = company.get("edinet_code", "")
    company_name = company.get("name", ticker)
    print(f"      🏢 {company_name} (EDINETコード: {edinet_code})")

    if not edinet_code:
        return []

    time.sleep(0.5)  # レート制限対策

    # ② AI分析・リスク情報取得
    analysis = get_analysis(edinet_code, api_key, cache_dir, cache_days)
    if not analysis:
        print(f"      ⚠️  {ticker}: AI分析データが取得できませんでした")
        return []

    # 財務健全性スコアも表示
    score  = analysis.get("credit_score", 0)
    rating = analysis.get("credit_rating", "")
    print(f"      📊 財務健全性スコア: {score}/100 ({rating})")

    risk_items = analysis.get("risk_factors", [])
    max_items  = getattr(cfg, "EDINET_MAX_RISK_ITEMS", 5)

    if not risk_items:
        # risk_summaryから生成
        summary = analysis.get("risk_summary", "")
        if summary:
            risk_items = [summary[:120]]

    risk_items = risk_items[:max_items]
    print(f"      ✅ {ticker}: リスク要因 {len(risk_items)} 件取得")
    return risk_items


def get_sector_risk_factors(tickers: list, cfg,
                             max_companies: int = 2) -> dict:
    """
    複数銘柄のリスク要因を取得する。

    Returns:
        dict: {ticker: [リスク要因リスト]}
    """
    results = {}
    api_key = (
        getattr(cfg, "EDINETDB_API_KEY", "")
        or getattr(cfg, "EDINET_API_KEY", "")
    )
    if not api_key:
        return results

    for ticker in tickers[:max_companies]:
        try:
            risks = get_risk_factors(ticker, cfg)
            if risks:
                results[ticker] = risks
            time.sleep(1)
        except Exception as e:
            print(f"      ⚠️  {ticker} EdinetDB エラー: {str(e)[:60]}")
            continue

    return results


# ==============================================================
# BLOCK 6: Markdown整形（v1.0から変更なし）
# ==============================================================

def format_risk_markdown(sector_name: str, risk_by_ticker: dict,
                          ticker_to_name: dict) -> str:
    if not risk_by_ticker:
        return ""
    lines = ["", "### 有報「事業のリスク」（EdinetDB 自動取得）", ""]
    for ticker, risks in risk_by_ticker.items():
        name = ticker_to_name.get(ticker, ticker)
        lines.append(f"**{name}** (`{ticker}`)")
        for risk in risks:
            lines.append(f"- {risk}")
        lines.append("")
    return "\n".join(lines)


# ==============================================================
# 単体テスト
# ==============================================================
if __name__ == "__main__":
    import importlib.util, sys
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break

    print("=== EdinetDB クライアント v2.0 単体テスト ===")
    key = getattr(cfg, "EDINETDB_API_KEY", "") or getattr(cfg, "EDINET_API_KEY", "")
    print(f"APIキー設定: {'あり' if key else 'なし（スキップ）'}")

    risks = get_risk_factors("8306.T", cfg)
    if risks:
        print(f"\n三菱UFJ 事業のリスク ({len(risks)} 件):")
        for r in risks:
            print(f"  - {r}")
    else:
        print("リスク取得なし（APIキー未設定 or 取得失敗）")
