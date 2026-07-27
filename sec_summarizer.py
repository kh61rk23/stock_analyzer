# ==============================================================
# sec_summarizer.py  v1.0  (Step E)
# SEC 10-K / EDINET 有価証券報告書 → Claude API 日本語3行要約
# ==============================================================
# 【役割】
#   stock_screener.py のスクリーニング通過銘柄に対して、
#   米国株は SEC EDGAR から 10-K を取得、
#   日本株は EDINETDB API から有価証券報告書を取得し、
#   Claude API（claude-sonnet-4-5）で下記を日本語3行に要約する。
#     ① 何で稼いでいるか（主力事業・収益モデル）
#     ② 主なリスク（3つ以内）
#     ③ 今期見通し / 直近動向
#
# 【設計上の注意点】
#   - 10-K本文は非常に長い（50万〜200万文字）
#   - Claude API に渡すのは「ビジネス概要」「リスク要因」「経営概況」
#     の3セクションのみを抽出した要約版（最大3000文字）
#   - APIコスト節約のため 1銘柄 = 1回の API 呼び出し
#   - スクリーニング通過銘柄のみ対象（デフォルト上位10銘柄）
#
# 【APIキー設定】
#   config.py:
#     ANTHROPIC_API_KEY: str = ""   # Claude API キー
#     SEC_USER_AGENT:    str = "StockAnalyzer your@email.com"  # SEC必須
#     SUMMARY_MAX_TICKERS: int = 10  # 要約する最大銘柄数
#     ENABLE_SEC_SUMMARIZER: bool = True
#
# 【出力スキーマ】
#   {
#     "summaries": [TickerSummary, ...],
#     "success_count": int,
#     "fail_count": int,
#     "timestamp": str,
#   }
#   TickerSummary = {
#     ticker, name, market, filing_type,
#     business_summary,   # ① 何で稼いでいるか
#     risk_summary,       # ② 主なリスク
#     outlook_summary,    # ③ 今期見通し
#     full_summary,       # ①②③ を結合した3行テキスト
#     filing_url,
#     error,              # 失敗時のエラーメッセージ
#   }
#
# 【依存】requests / anthropic / config.py
# 【呼出】from sec_summarizer import run_sec_summarizer
# ==============================================================

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import sec_client


# ==============================================================
# BLOCK 1: SEC EDGAR 10-K テキスト取得（米国株）
# ==============================================================

def _get_cik_from_ticker(ticker: str, cfg) -> Optional[str]:
    """
    SEC EDGAR の ticker→CIK マッピングを取得する（sec_client 経由）。
    https://www.sec.gov/files/company_tickers.json

    挙動注記: sec_client.get_cik() は company_tickers.json を
    CIK_MAP_TTL_DAYS 日キャッシュする。旧実装は都度取得だったため、
    キャッシュ有りに変わる（改善／U-1a 申し送り）。
    """
    try:
        return sec_client.get_cik(ticker, cfg=cfg)
    except Exception:
        return None


def _get_latest_10k_accession(cik: str, cfg) -> Optional[str]:
    """
    最新の 10-K の accession number を返す。
    """
    try:
        data = sec_client.submissions(cik, cfg=cfg)
        if not data:
            return None
        filings = data.get("filings", {}).get("recent", {})
        forms  = filings.get("form", [])
        accns  = filings.get("accessionNumber", [])
        for form, accn in zip(forms, accns):
            if form == "10-K":
                return accn.replace("-", "")
    except Exception:
        pass
    return None


def _extract_10k_sections(cik: str, accession: str, cfg) -> Optional[dict]:
    """
    10-K の Item 1 (Business) / Item 1A (Risk Factors) /
    Item 7 (MD&A) を取得して最大3000文字に圧縮する。

    SEC EDGAR の viewer API を使って HTM ファイルを取得する。
    """
    try:
        # Filing インデックスを取得
        cik_plain = str(int(cik))
        acc_dash  = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"
        idx_url   = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_plain}/{accession}/{acc_dash}-index.json"
        )
        resp = sec_client.get(idx_url, cfg=cfg, accept="application/json",
                               timeout=15)
        if resp is None or resp.status_code != 200:
            # .htm 形式で直接試行
            return None
        files  = resp.json().get("directory", {}).get("item", [])

        # メイン 10-K HTM ファイルを特定
        main_file = None
        for f in files:
            name = f.get("name", "")
            if name.endswith(".htm") and "10k" in name.lower():
                main_file = name
                break
        if not main_file:
            for f in files:
                if f.get("type") == "10-K":
                    main_file = f.get("name")
                    break
        if not main_file:
            return None

        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_plain}/{accession}/{main_file}"
        )
        doc_resp = sec_client.get(doc_url, cfg=cfg, accept="text/html",
                                   timeout=30)
        if doc_resp is None or doc_resp.status_code != 200:
            return None

        text = doc_resp.text
        # HTML タグを除去
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        # セクション抽出（正規表現でItem1/1A/7を探す）
        def _extract_section(full_text: str, start_pattern: str,
                             end_pattern: str, max_chars: int = 1000) -> str:
            m = re.search(start_pattern, full_text, re.IGNORECASE)
            if not m:
                return ""
            start = m.end()
            m_end = re.search(end_pattern, full_text[start:], re.IGNORECASE)
            end   = start + m_end.start() if m_end else start + max_chars * 3
            return full_text[start:end][:max_chars].strip()

        business = _extract_section(
            text, r"item\s*1[^a-z].*?business", r"item\s*1a", 1200)
        risk     = _extract_section(
            text, r"item\s*1a.*?risk\s*factor", r"item\s*1b", 1200)
        mda      = _extract_section(
            text, r"item\s*7[^a-z].*?management", r"item\s*7a", 800)

        filing_url = doc_url
        return {
            "business": business[:1200],
            "risk":     risk[:1200],
            "mda":      mda[:800],
            "url":      filing_url,
        }

    except Exception as e:
        print(f"    ⚠️  10-K取得例外: {str(e)[:60]}")
        return None


def fetch_10k_text(ticker: str, cfg) -> Optional[dict]:
    """
    米国株の最新 10-K テキストを取得する。

    Returns:
        {"business": str, "risk": str, "mda": str, "url": str}
        失敗時は None
    """
    print(f"    🔍 SEC 10-K 取得中: {ticker}")

    cik = _get_cik_from_ticker(ticker, cfg)
    if not cik:
        print(f"    ⚠️  CIK 未発見: {ticker}")
        return None

    accession = _get_latest_10k_accession(cik, cfg)
    if not accession:
        print(f"    ⚠️  10-K アクセッション未発見: {ticker}")
        return None

    sections = _extract_10k_sections(cik, accession, cfg)
    if not sections:
        print(f"    ⚠️  10-K セクション取得失敗: {ticker}")
        return None

    print(f"    ✅ 10-K 取得成功: {ticker}")
    time.sleep(0.5)   # SEC レート制限対策（10リクエスト/秒）
    return sections


# ==============================================================
# BLOCK 2: EDINETDB 有価証券報告書テキスト取得（日本株）
# ==============================================================

def _ticker_to_edinetcode(ticker: str) -> str:
    """
    yfinance 形式のティッカー（例: 7203.T）から証券コードを取得する。
    """
    return ticker.replace(".T", "").replace(".t", "")


def fetch_yuho_text(ticker: str, cfg) -> Optional[dict]:
    """
    EDINETDB API から有価証券報告書のリスク・事業概要を取得する。
    """
    api_key    = getattr(cfg, "EDINETDB_API_KEY", "")
    sec_code   = _ticker_to_edinetcode(ticker)

    if not api_key:
        return None

    try:
        import requests

        # ① 直近の有価証券報告書 docID を取得
        search_url = "https://api.edinetdb.com/v2/documents"
        params = {
            "api_key":   api_key,
            "sec_code":  sec_code,
            "doc_type":  "120",   # 有価証券報告書
            "count":     1,
        }
        resp = requests.get(search_url, params=params, timeout=15)
        if resp.status_code != 200:
            return None

        docs = resp.json().get("results", [])
        if not docs:
            return None

        doc_id = docs[0].get("docID") or docs[0].get("doc_id")
        if not doc_id:
            return None

        # ② テキスト取得（事業内容・リスク要因）
        text_url = f"https://api.edinetdb.com/v2/documents/{doc_id}/text"
        text_resp = requests.get(
            text_url,
            params={"api_key": api_key, "section": "business,risk"},
            timeout=20,
        )
        if text_resp.status_code != 200:
            return None

        text_data = text_resp.json()
        business  = text_data.get("business", "")[:1200]
        risk      = text_data.get("risk",     "")[:1200]

        return {
            "business": business,
            "risk":     risk,
            "mda":      "",
            "url":      f"https://api.edinetdb.com/v2/documents/{doc_id}",
        }

    except Exception as e:
        print(f"    ⚠️  EDINETDB 例外 ({ticker}): {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 3: Claude API 要約
# ==============================================================

SUMMARY_PROMPT_TEMPLATE = """\
以下は企業の開示書類（10-K または有価証券報告書）から抜粋したテキストです。
このテキストを読んで、投資家向けに以下の3点を日本語で各1〜2文（合計3行）で答えてください。

【企業名・ティッカー】{ticker} ({name})

【事業概要テキスト】
{business}

【リスク要因テキスト】
{risk}

【経営概況テキスト】
{mda}

---
以下の形式で回答してください（必ずこの形式を守ること）:

①何で稼いでいるか: [主力事業と収益モデルを1〜2文で]
②主なリスク: [最重要リスクを3つ以内、箇条書き不可、1〜2文で]
③今期見通し: [直近の動向や業績見通しを1〜2文で。情報がない場合は「開示情報なし」]
"""


def _call_claude_summary(ticker: str, name: str,
                          sections: dict, api_key: str) -> Optional[dict]:
    """
    Claude API を呼び出して3行要約を生成する。

    Returns:
        {"business_summary": str, "risk_summary": str,
         "outlook_summary": str, "full_summary": str}
        失敗時は None
    """
    try:
        import requests

        prompt = SUMMARY_PROMPT_TEMPLATE.format(
            ticker   = ticker,
            name     = name[:40],
            business = sections.get("business", "（取得できませんでした）")[:1000],
            risk     = sections.get("risk",     "（取得できませんでした）")[:1000],
            mda      = sections.get("mda",      "（取得できませんでした）")[:600],
        )

        payload = {
            "model":      "claude-sonnet-4-5",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers = headers,
            json    = payload,
            timeout = 30,
        )
        if resp.status_code != 200:
            print(f"    ⚠️  Claude API HTTP {resp.status_code}: {ticker}")
            return None

        content = resp.json().get("content", [])
        text    = " ".join(c.get("text", "") for c in content
                           if c.get("type") == "text").strip()

        # 3行パース
        def _extract(label: str) -> str:
            m = re.search(rf"{label}[:：]\s*(.+?)(?=①|②|③|$)", text,
                          re.DOTALL)
            return m.group(1).strip()[:200] if m else ""

        b = _extract("①何で稼いでいるか")
        r = _extract("②主なリスク")
        o = _extract("③今期見通し")

        full = f"①{b}\n②{r}\n③{o}" if any([b, r, o]) else text[:300]

        return {
            "business_summary": b,
            "risk_summary":     r,
            "outlook_summary":  o,
            "full_summary":     full,
        }

    except Exception as e:
        print(f"    ⚠️  Claude API 例外 ({ticker}): {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 4: レポート保存
# ==============================================================

def _save_summary_report(result: dict, cfg) -> None:
    """insights/summaries/summary_YYYY-MM-DD.md に保存する。"""
    today      = datetime.now().strftime("%Y-%m-%d")
    summ_dir   = Path(getattr(cfg, "INSIGHT_DIR", "insights")) / "summaries"

    try:
        summ_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# 有報・10-K 日本語要約レポート（{today}）",
            f"**生成日時**: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
            f"**要約成功**: {result['success_count']}銘柄  |  "
            f"**失敗**: {result['fail_count']}銘柄",
            "",
        ]

        for s in result["summaries"]:
            if s.get("error"):
                lines.append(f"### ❌ {s['ticker']} ({s['name']}) — {s['error']}")
            else:
                mkt = "🇺🇸" if s["market"] == "US" else "🇯🇵"
                lines += [
                    f"### {mkt} {s['ticker']} — {s['name']}",
                    "",
                    s.get("full_summary", ""),
                    "",
                    f"📄 出典: {s.get('filing_url', '')}",
                    "",
                    "---",
                    "",
                ]

        out = summ_dir / f"summary_{today}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"  💾 要約レポート保存: summary_{today}.md")

    except Exception as e:
        print(f"  ⚠️  要約レポート保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_sec_summarizer(cfg, screener_result: dict) -> dict:
    """
    スクリーニング通過銘柄に対して有報・10-K 日本語要約を実行する。

    Args:
        cfg              : config モジュール
        screener_result  : results["screener"]
                           {"candidates": [Candidate, ...], ...}

    Returns:
        {
          "summaries":     [TickerSummary, ...],
          "success_count": int,
          "fail_count":    int,
          "timestamp":     str,
        }
    """
    print("\n  📄 SEC/有報 日本語3行要約（Step E）開始...")
    t0 = datetime.now()

    api_key  = getattr(cfg, "ANTHROPIC_API_KEY", "")
    max_tkr  = getattr(cfg, "SUMMARY_MAX_TICKERS", 10)

    if not api_key:
        print("  ⚠️  ANTHROPIC_API_KEY 未設定 → スキップ")
        return {
            "summaries": [], "success_count": 0, "fail_count": 0,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # スクリーニング通過銘柄を composite_score 降順で取得
    candidates = screener_result.get("candidates", [])
    passed = sorted(
        [c for c in candidates if c.get("pass_filters")],
        key=lambda x: x.get("composite_score", 0),
        reverse=True,
    )[:max_tkr]

    if not passed:
        print("  ⚠️  スクリーニング通過銘柄が0件 → スキップ")
        return {
            "summaries": [], "success_count": 0, "fail_count": 0,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    print(f"  対象: {len(passed)}銘柄（上位{max_tkr}件）")

    summaries     = []
    success_count = 0
    fail_count    = 0

    for cand in passed:
        ticker = cand.get("ticker", "")
        name   = cand.get("name",   ticker)
        market = cand.get("market", "JP")

        print(f"  📌 {ticker} ({name}) [{market}]")

        # 書類テキスト取得
        sections = None
        if market == "US":
            sections = fetch_10k_text(ticker, cfg)
        else:
            sections = fetch_yuho_text(ticker, cfg)

        if not sections:
            summaries.append({
                "ticker":           ticker,
                "name":             name,
                "market":           market,
                "filing_type":      "10-K" if market == "US" else "有価証券報告書",
                "business_summary": "",
                "risk_summary":     "",
                "outlook_summary":  "",
                "full_summary":     "",
                "filing_url":       "",
                "error":            "書類テキスト取得失敗",
            })
            fail_count += 1
            continue

        # Claude API 要約
        summary = _call_claude_summary(ticker, name, sections, api_key)
        if not summary:
            summaries.append({
                "ticker":       ticker,
                "name":         name,
                "market":       market,
                "filing_type":  "10-K" if market == "US" else "有価証券報告書",
                "full_summary": "",
                "filing_url":   sections.get("url", ""),
                "error":        "Claude API 要約失敗",
            })
            fail_count += 1
        else:
            summaries.append({
                "ticker":           ticker,
                "name":             name,
                "market":           market,
                "filing_type":      "10-K" if market == "US" else "有価証券報告書",
                "business_summary": summary["business_summary"],
                "risk_summary":     summary["risk_summary"],
                "outlook_summary":  summary["outlook_summary"],
                "full_summary":     summary["full_summary"],
                "filing_url":       sections.get("url", ""),
                "error":            "",
            })
            success_count += 1
            print(f"    ✅ 要約完了: {ticker}")

        time.sleep(1.0)   # Claude API レート制限対策

    elapsed = (datetime.now() - t0).seconds
    print(
        f"  ✅ 要約完了（{elapsed}秒）: "
        f"成功{success_count}件 / 失敗{fail_count}件"
    )

    result = {
        "summaries":     summaries,
        "success_count": success_count,
        "fail_count":    fail_count,
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_summary_report(result, cfg)
    return result


# ==============================================================
# 単体テスト
# ==============================================================
if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py",
              str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break

    mock_screener = {
        "candidates": [
            {"ticker": "8035.T", "name": "東京エレクトロン", "market": "JP",
             "pass_filters": True, "composite_score": 82.0},
        ]
    }
    result = run_sec_summarizer(cfg, mock_screener)
    for s in result["summaries"]:
        print(f"\n{s['ticker']} / {s['name']}")
        print(s.get("full_summary", s.get("error", "")))
