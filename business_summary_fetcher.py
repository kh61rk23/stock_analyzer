# ==============================================================
# business_summary_fetcher.py  v1.0  (Step E 代替)
# ==============================================================
# 【概要】
#   yfinance.info["longBusinessSummary"] で英語の事業概要を取得し、
#   DeepL API 無料枠（50万字/月）で日本語に翻訳して「何で稼いでいるか」
#   を1〜2文で自動出力する。
#
# 【Claude API不要・完全無料】
#   yfinance   : 無料・無制限
#   DeepL API  : 無料枠 50万字/月（翻訳1社あたり約300字 → 約1,600社/月）
#
# 【月間文字数の試算】
#   33業種 × 2社 × 300字 = 約20,000字/月 → 無料枠の4%
#
# 【config.py で必要な設定】
#   DEEPL_API_KEY      : DeepL API キー（無料枠: https://www.deepl.com/pro-api）
#   ENABLE_BIZ_SUMMARY : True で有効化
#   BIZ_SUMMARY_MAX_COMPANIES : 業種あたり取得社数（推奨: 2）
#   BIZ_SUMMARY_CACHE_DAYS    : キャッシュ有効日数（推奨: 90）
#
# 【出力例（メールに追記される内容）】
#   📋 ソニーグループ: 半導体イメージセンサー・ゲーム・音楽・映画の
#      4事業を展開。イメージセンサーは世界シェア約50%を保有。
#
# 【呼び出し元】
#   base_agent.py の build_result() から自動呼び出し
#   stock_analyzer_main.py の run_all_tasks() から直接呼び出し可能
# ==============================================================

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cache_dir: Path, ticker: str) -> Path:
    safe = ticker.replace(".", "_")
    return cache_dir / f"bizsummary_{safe}.json"


def _load_cache(path: Path, days: int) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime > timedelta(days=days):
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(path: Path, data: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 2: DeepL 翻訳
# ==============================================================

# 403/456はキー無効・quota超過であり同一実行内でリトライしても
# 回復しない。1社ごとにHTTPエラーを繰り返さないよう実行内フラグでfail-fastする。
_deepl_disabled_this_run = False


def _translate_deepl(text: str, api_key: str) -> Optional[str]:
    """
    DeepL API 無料枠で英語→日本語翻訳。
    Free API URL: api-free.deepl.com
    Pro API URL : api.deepl.com
    （無料キーは "-:fx" で終わる）
    """
    global _deepl_disabled_this_run

    if not text or not api_key:
        return None

    if _deepl_disabled_this_run:
        # 403/quota超過を検出済み: 静かに原文使用へフォールバック
        return None

    import urllib.error
    import urllib.request
    import urllib.parse

    # 無料枠かProかをキーの末尾で判定
    base_url = "https://api-free.deepl.com/v2/translate" \
               if api_key.endswith(":fx") \
               else "https://api.deepl.com/v2/translate"

    # 事業概要の核心部分のみ（最初の3文・最大600字）を翻訳してコスト削減
    sentences = text.replace("  ", " ").split(". ")
    short_text = ". ".join(sentences[:3])[:600].strip()
    if not short_text.endswith("."):
        short_text += "."

    try:
        payload = urllib.parse.urlencode({
            "auth_key": api_key,
            "text":     short_text,
            "source_lang": "EN",
            "target_lang": "JA",
        }).encode("utf-8")

        req = urllib.request.Request(
            base_url,
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["translations"][0]["text"]

    except urllib.error.HTTPError as e:
        if e.code in (403, 456):
            # キー無効/quota超過: 実行内リトライは無意味なので以降スキップ
            _deepl_disabled_this_run = True
            print("      ⚠️  DeepL無効（403/quota）: 以降は英語原文のまま保存"
                  "（DEEPL_API_KEY要点検）")
        else:
            print(f"      ⚠️  DeepL 翻訳エラー: {str(e)[:60]}")
        return None

    except Exception as e:
        print(f"      ⚠️  DeepL 翻訳エラー: {str(e)[:60]}")
        return None


# ==============================================================
# BLOCK 3: 事業概要の要約（翻訳後テキストを1〜2文に圧縮）
# ==============================================================

def _summarize_to_core(translated: str) -> str:
    """
    翻訳済み日本語テキストから「何で稼いでいるか」の核心1〜2文を抽出。
    DeepLが返す文章は3文程度なのでそのまま使う（追加APIコール不要）。
    """
    if not translated:
        return ""
    # 句点で分割して最初の2文
    parts = translated.replace("。\n", "。").split("。")
    core  = "。".join(p.strip() for p in parts[:2] if len(p.strip()) > 10)
    if core and not core.endswith("。"):
        core += "。"
    return core[:200]


# ==============================================================
# BLOCK 4: 銘柄ごとの事業概要取得（メインロジック）
# ==============================================================

def fetch_business_summary(ticker: str, cfg,
                            cache_dir: Optional[Path] = None) -> dict:
    """
    1銘柄の事業概要（日本語）を取得する。

    Args:
        ticker    : "8306.T" 形式
        cfg       : config モジュール
        cache_dir : キャッシュ保存先（None の場合はキャッシュなし）

    Returns:
        dict:
          {
            "ticker":          str,
            "name":            str,
            "summary_en":      str,   # yfinance 原文（英語）
            "summary_ja":      str,   # DeepL 翻訳済み（日本語）
            "core_summary":    str,   # 1〜2文に圧縮した「何で稼いでいるか」
            "translated":      bool,  # DeepL翻訳に成功したか
            "from_cache":      bool,
            "char_count":      int,   # 翻訳に使用した文字数
          }
    """
    result = {
        "ticker":       ticker,
        "name":         ticker,
        "summary_en":   "",
        "summary_ja":   "",
        "core_summary": "",
        "translated":   False,
        "from_cache":   False,
        "char_count":   0,
    }

    # キャッシュ確認
    cache_days = getattr(cfg, "BIZ_SUMMARY_CACHE_DAYS", 90)
    if cache_dir:
        cp = _cache_path(cache_dir, ticker)
        cached = _load_cache(cp, cache_days)
        if cached:
            cached["from_cache"] = True
            return cached

    # yfinance で事業概要取得
    try:
        import yf_cache as yf  # type: ignore
        info = yf.Ticker(ticker).info or {}
        name        = info.get("shortName") or info.get("longName") or ticker
        summary_en  = info.get("longBusinessSummary", "")
        result["name"]       = name
        result["summary_en"] = summary_en
    except Exception as e:
        print(f"      ⚠️  {ticker} yfinance 取得失敗: {str(e)[:60]}")
        return result

    if not summary_en:
        print(f"      ⚠️  {ticker}: longBusinessSummary が空")
        return result

    print(f"      📝 {name}: 事業概要 {len(summary_en)}字 取得")

    # DeepL 翻訳
    deepl_key = getattr(cfg, "DEEPL_API_KEY", "")
    if deepl_key:
        translated = _translate_deepl(summary_en, deepl_key)
        if translated:
            core = _summarize_to_core(translated)
            result["summary_ja"]   = translated
            result["core_summary"] = core
            result["translated"]   = True
            result["char_count"]   = len(summary_en[:600])
            print(f"      ✅ 翻訳完了: {core[:60]}...")
        else:
            # 翻訳失敗時は英語原文の先頭を返す
            result["core_summary"] = summary_en[:150] + "..."
    else:
        # DeepLキー未設定: 英語のまま返す
        result["core_summary"] = summary_en[:200] + "..."
        print(f"      ⚠️  DEEPL_API_KEY 未設定 → 英語原文を使用")

    # キャッシュ保存
    if cache_dir:
        cp = _cache_path(cache_dir, ticker)
        _save_cache(cp, result)

    return result


# ==============================================================
# BLOCK 5: セクター単位での一括取得
# ==============================================================

def fetch_sector_summaries(tickers: list, cfg,
                            max_companies: int = 2,
                            cache_dir: Optional[Path] = None) -> dict:
    """
    複数銘柄の事業概要を一括取得する。

    Returns:
        dict: {ticker: summary_dict}
    """
    if cache_dir is None:
        data_dir = getattr(cfg, "DATA_DIR", None)
        if data_dir:
            cache_dir = Path(data_dir) / "biz_summary_cache"

    results = {}
    for ticker in tickers[:max_companies]:
        try:
            data = fetch_business_summary(ticker, cfg, cache_dir)
            if data.get("core_summary"):
                results[ticker] = data
            time.sleep(0.5)
        except Exception as e:
            print(f"      ⚠️  {ticker} 事業概要取得エラー: {str(e)[:60]}")
    return results


# ==============================================================
# BLOCK 6: 全銘柄一括実行（run_all_tasks から呼ばれるエントリ）
# ==============================================================

def run_business_summary_fetcher(cfg, results: dict) -> dict:
    """
    stock_analyzer_main.py の run_all_tasks() から呼ばれる。
    全セクターの上位銘柄の事業概要を取得して results に追記する。

    結果は results["biz_summaries"] に格納:
      {
        "ticker_summaries": { "8306.T": {...}, ... },
        "total_chars_used": int,  # DeepL 使用文字数
        "skipped":          bool,
      }
    """
    print("\n  📋 事業概要フェッチャー (Step E代替) 開始...")

    if not getattr(cfg, "ENABLE_BIZ_SUMMARY", False):
        print("  ⏩ 事業概要取得: スキップ（ENABLE_BIZ_SUMMARY=False）")
        return {"ticker_summaries": {}, "total_chars_used": 0, "skipped": True}

    data_dir  = getattr(cfg, "DATA_DIR", None)
    cache_dir = Path(data_dir) / "biz_summary_cache" if data_dir else None
    max_co    = getattr(cfg, "BIZ_SUMMARY_MAX_COMPANIES", 2)

    sector_results = results.get("sectors", {})
    ticker_summaries = {}
    total_chars = 0

    for code, sr in sector_results.items():
        if not isinstance(sr, dict):
            continue
        top_cos = sr.get("top_companies", [])
        tickers = [c["ticker"] for c in top_cos if c.get("fetch_ok")][:max_co]
        if not tickers:
            continue

        sec_name = sr.get("sector_name_ja", code)
        print(f"    [{code}] {sec_name}: {len(tickers)}社")
        sums = fetch_sector_summaries(tickers, cfg, max_co, cache_dir)
        ticker_summaries.update(sums)
        total_chars += sum(s.get("char_count", 0) for s in sums.values())

    # screener通過銘柄にも適用
    screener = results.get("screener", {})
    candidates = screener.get("candidates", [])
    passed = [c for c in candidates if c.get("pass_filters")][:5]
    for c in passed:
        ticker = c.get("ticker", "")
        if ticker and ticker not in ticker_summaries:
            try:
                data = fetch_business_summary(ticker, cfg, cache_dir)
                if data.get("core_summary"):
                    ticker_summaries[ticker] = data
                    total_chars += data.get("char_count", 0)
                time.sleep(0.5)
            except Exception:
                pass

    print(f"    ✅ 事業概要取得完了: {len(ticker_summaries)}社 | "
          f"DeepL使用: {total_chars:,}字")

    # insights/ に保存
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        out_dir  = Path(cfg.INSIGHT_DIR) / "biz_summary"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"biz_summary_{today}.md"
        lines = [f"# 事業概要サマリー {today}", ""]
        for ticker, d in ticker_summaries.items():
            lines.append(f"## {d.get('name', ticker)} (`{ticker}`)")
            lines.append(d.get("core_summary", ""))
            lines.append("")
        out_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass

    return {
        "ticker_summaries": ticker_summaries,
        "total_chars_used": total_chars,
        "skipped":          False,
    }


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util, sys
    
    # 1. 未定義エラーを防ぐための初期値
    cfg = None 

    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            # spec と loader の存在を確定させる
            if spec is None or spec.loader is None:
                continue
                
            cfg = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break

    # 2. cfg が見つからなかった場合のフォールバック
    if cfg is None:
        print("  ⚠️ config.py が見つからないため、デフォルト値を使用します。")
        class DefaultCfg:
            DEEPL_API_KEY = ""
            DATA_DIR = "/tmp"
        cfg = DefaultCfg()

    print("=== business_summary_fetcher v1.0 単体テスト ===")
    deepl = getattr(cfg, "DEEPL_API_KEY", "")
    print(f"DeepL APIキー: {'あり' if deepl else 'なし（英語原文モード）'}")

    cache_dir = Path(getattr(cfg, "DATA_DIR", "/tmp")) / "biz_summary_cache"
    data = fetch_business_summary("6758.T", cfg, cache_dir)  # ソニー

    print(f"\n銘柄: {data['name']}")
    print(f"翻訳済み: {data['translated']}")
    print(f"核心要約: {data['core_summary']}")
    print(f"使用文字数: {data['char_count']}")
