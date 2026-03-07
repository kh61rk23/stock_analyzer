# ==============================================================
# paid_media_searcher.py  v1.0
# 有料メディア記事タイトル・スニペット取得モジュール
# ==============================================================
# 【役割】
#   Bloomberg / Caixin / The Economist / Barron's / 日経（日本語版）
#   は有料会員限定のため RSS で本文取得不可。
#   Google Custom Search API（CSE）で各メディアのサイト内検索を行い、
#   記事タイトル + スニペット（本文冒頭 ~150 文字）を取得する。
#   本文は取得しないため著作権上の問題はない。
#
# 【実現可能性の根拠】
#   - Google CSE 無料枠: 100クエリ/日
#   - 対象 5 メディア × 3クエリ = 15クエリ/日 → 無料枠に収まる
#   - スニペット 150 文字で業種キーワードマッチングは十分可能
#
# 【必要な設定（config.py に追加）】
#   GOOGLE_CSE_API_KEY = "AIzaXXXXXX"   # Google Cloud Console で取得
#   GOOGLE_CSE_ID      = "XXXXXXXXX"    # CSE 管理画面で取得
#   ENABLE_PAID_MEDIA_SEARCH = True
#
# 【Google CSE の取得手順】→ 説明書.txt を参照
#
# 【出力スキーマ】
#   run_news_pipeline() の articles リストと同一形式:
#   [{"title", "url", "summary", "source", "priority", "lang",
#     "pub_date", "matched_sectors", "importance_score"}, ...]
#
# 【依存】requests / config.py
# 【呼出】from paid_media_searcher import fetch_paid_media_articles
# ==============================================================

import time
from datetime import datetime
from typing import Optional

GCSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"

# ==============================================================
# BLOCK 0: 対象メディア定義
# ==============================================================
# num: 1クエリで取得する記事数（CSE最大10件/クエリ）
# queries: 追加で発行するキーワード（業種テーマ別）
# dateRestrict: d1=過去1日, d2=過去2日
PAID_MEDIA_SOURCES = [
    {
        "id":          "bloomberg",
        "name":        "Bloomberg",
        "site":        "bloomberg.com",
        "priority":    "S",
        "lang":        "en",
        "num":         10,
        "dateRestrict": "d2",
        "queries":     ["markets", "economy", "Japan stocks"],
    },
    {
        "id":          "nikkei_jp",
        "name":        "日経新聞（日本語版）",
        "site":        "nikkei.com",
        "priority":    "S",
        "lang":        "ja",
        "num":         10,
        "dateRestrict": "d2",
        "queries":     ["株式", "経済", "金利"],
    },
    {
        "id":          "caixin",
        "name":        "Caixin Global（財新）",
        "site":        "caixinglobal.com",
        "priority":    "A",
        "lang":        "en",
        "num":         10,
        "dateRestrict": "d2",
        "queries":     ["China economy", "PMI"],
    },
    {
        "id":          "economist",
        "name":        "The Economist",
        "site":        "economist.com",
        "priority":    "A",
        "lang":        "en",
        "num":         10,
        "dateRestrict": "d2",
        "queries":     ["finance", "markets"],
    },
    {
        "id":          "barrons",
        "name":        "Barron's",
        "site":        "barrons.com",
        "priority":    "A",
        "lang":        "en",
        "num":         10,
        "dateRestrict": "d2",
        "queries":     ["stocks", "investing"],
    },
]


# ==============================================================
# BLOCK 1: Google CSE 検索
# ==============================================================

def _search_gcse(api_key: str, cse_id: str, site: str,
                 query: str, num: int = 10,
                 date_restrict: str = "d2",
                 timeout: int = 10) -> list:
    """
    Google Custom Search API で site:XXX 検索を実行する。

    Returns:
        [{"title": str, "link": str, "snippet": str}, ...]
        失敗時は []
    """
    try:
        import requests
    except ImportError:
        return []

    params = {
        "key":         api_key,
        "cx":          cse_id,
        "q":           f"site:{site} {query}",
        "num":         min(num, 10),   # CSE 最大 10件/クエリ
        "dateRestrict": date_restrict,
        "fields":      "items(title,link,snippet)",  # 必要フィールドのみ
    }
    try:
        resp = requests.get(GCSE_ENDPOINT, params=params, timeout=timeout)
        if resp.status_code == 429:
            print(f"    ⚠️  CSE レート制限（429）: {site}")
            return []
        if resp.status_code != 200:
            print(f"    ⚠️  CSE HTTP {resp.status_code}: {site}")
            return []
        data = resp.json()
        return data.get("items", [])
    except Exception as e:
        print(f"    ⚠️  CSE 例外: {str(e)[:60]}")
        return []


# ==============================================================
# BLOCK 2: 記事データ正規化
# ==============================================================

def _normalize_item(item: dict, source_def: dict) -> Optional[dict]:
    """
    CSE の item を news_pipeline.py の Article 形式に変換する。
    title または link が空の場合は None を返す。
    """
    title   = item.get("title", "").strip()
    url     = item.get("link",  "").strip()
    snippet = item.get("snippet", "").strip()

    if not title or not url:
        return None

    return {
        "title":    title,
        "url":      url,
        "summary":  snippet[:300],
        "source":   source_def["name"],
        "priority": source_def["priority"],
        "lang":     source_def["lang"],
        "pub_date": "",   # CSE は公開日を返さないため空
    }


# ==============================================================
# BLOCK 3: メインエントリポイント
# ==============================================================

def fetch_paid_media_articles(cfg) -> list:
    """
    設定された有料メディアに対して Google CSE 検索を実行し、
    news_pipeline.py の Article 形式のリストを返す。

    config.py に以下が必要:
        GOOGLE_CSE_API_KEY  : str
        GOOGLE_CSE_ID       : str
        ENABLE_PAID_MEDIA_SEARCH : bool  (default: True)

    Returns:
        [Article, ...]  ※ matched_sectors / importance_score は未付与
                         news_pipeline.py の match_sectors() で付与される
    """
    api_key = getattr(cfg, "GOOGLE_CSE_API_KEY", "")
    cse_id  = getattr(cfg, "GOOGLE_CSE_ID", "")
    enabled = getattr(cfg, "ENABLE_PAID_MEDIA_SEARCH", True)

    if not enabled:
        print("  ⏩ 有料メディア検索: スキップ（ENABLE_PAID_MEDIA_SEARCH=False）")
        return []

    if not api_key or not cse_id:
        print("  ⏩ 有料メディア検索: スキップ（GOOGLE_CSE_API_KEY/ID 未設定）")
        return []

    print("  🔍 有料メディア記事検索（Google CSE）...")
    all_articles = []
    query_count  = 0
    seen_urls    = set()

    for src in PAID_MEDIA_SOURCES:
        src_articles = []

        for q in src["queries"]:
            items = _search_gcse(
                api_key      = api_key,
                cse_id       = cse_id,
                site         = src["site"],
                query        = q,
                num          = src["num"],
                date_restrict= src["dateRestrict"],
            )
            query_count += 1

            for item in items:
                art = _normalize_item(item, src)
                if art and art["url"] not in seen_urls:
                    seen_urls.add(art["url"])
                    src_articles.append(art)

            time.sleep(0.3)   # レート制限対策

        print(f"    [{src['priority']}] {src['name']}: {len(src_articles)}件")
        all_articles.extend(src_articles)

    print(
        f"  ✅ 有料メディア検索完了: "
        f"合計{len(all_articles)}件 / CSEクエリ{query_count}回"
    )
    return all_articles
