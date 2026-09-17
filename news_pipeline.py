# ==============================================================
# news_pipeline.py  v1.0
# ニュースRSS取得・業種キーワードマッチング・アラート連携
# ==============================================================
# 【役割】
#   ① 世界各地の信頼性・速報性の高いRSSフィードを毎日取得
#   ② 記事タイトル・サマリーを業種キーワードとマッチング
#   ③ 業種別の関連ニュースリストをresultsに格納
#   ④ alert_notifier.py と連携してGmailに組み込み
#
# 【情報源カテゴリ】
#   優先度S: Reuters / AP / NHK / Nikkei Asia / CNBC / SEC EDGAR 8-K
#   優先度A: Mining.com / World Nuclear News / Electrek / Quantum Insider
#
# 【出力スキーマ】
#   {
#     "articles":      [Article, ...],   # 全取得記事
#     "by_sector":     {code: [Article, ...]},  # 業種別マッチ記事
#     "top_articles":  [Article, ...],   # 重要度上位N件
#     "fetch_count":   int,
#     "match_count":   int,
#     "timestamp":     str,
#   }
#   Article = {title, url, summary, source, pub_date,
#               matched_sectors, importance_score, investment_relevant}
#
# 【依存】requests / feedparser / config.py
# 【呼出】from news_pipeline import run_news_pipeline
# ==============================================================

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: RSSフィード定義
# ==============================================================

RSS_FEEDS = [
    # ── 優先度S: 速報性・信頼性 最高位 ──────────────────────
    {
        "id":       "reuters_business",
        "name":     "Reuters Business",
        "url":      "https://feeds.reuters.com/reuters/businessNews",
        "priority": "S",
        "lang":     "en",
    },
    {
        "id":       "reuters_markets",
        "name":     "Reuters Markets",
        "url":      "https://feeds.reuters.com/reuters/marketsNews",
        "priority": "S",
        "lang":     "en",
    },
    {
        "id":       "ap_business",
        "name":     "AP Business",
        "url":      "https://feeds.apnews.com/rss/apf-business",
        "priority": "S",
        "lang":     "en",
    },
    {
        "id":       "nhk_business",
        "name":     "NHK ビジネス",
        "url":      "https://www.nhk.or.jp/rss/news/cat6.xml",
        "priority": "S",
        "lang":     "ja",
    },
    {
        "id":       "nhk_top",
        "name":     "NHK トップ",
        "url":      "https://www.nhk.or.jp/rss/news/cat0.xml",
        "priority": "S",
        "lang":     "ja",
    },
    {
        "id":       "nikkei_asia",
        "name":     "Nikkei Asia",
        "url":      "https://asia.nikkei.com/rss/feed/nar",
        "priority": "S",
        "lang":     "en",
    },
    {
        "id":       "cnbc_world",
        "name":     "CNBC World Economy",
        "url":      "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "priority": "S",
        "lang":     "en",
    },
    {
        "id":       "cnbc_finance",
        "name":     "CNBC Finance",
        "url":      "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "priority": "S",
        "lang":     "en",
    },
    # ── 優先度A: テーマ株・専門分野 ──────────────────────────
    {
        "id":       "mining_com",
        "name":     "Mining.com",
        "url":      "https://www.mining.com/feed/",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "world_nuclear",
        "name":     "World Nuclear News",
        "url":      "https://www.world-nuclear-news.org/rss",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "electrek",
        "name":     "Electrek",
        "url":      "https://electrek.co/feed/",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "marketwatch_top",
        "name":     "MarketWatch Top Stories",
        "url":      "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "nikkei_jp",
        "name":     "日経新聞（ヘッドライン）",
        "url":      "https://feeds.nikkei.com/business.rss",
        "priority": "A",
        "lang":     "ja",
    },
    # ── 優先度S: 公的機関・一次情報源 ───────────────────────
    {
        "id":       "sec_edgar_8k",
        "name":     "SEC EDGAR 8-K",
        "url":      "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom",
        "priority": "S",
        "lang":     "en",
    },
    {
        "id":       "fed_press",
        "name":     "Fed（連邦準備制度）Press Releases",
        "url":      "https://www.federalreserve.gov/feeds/press_all.xml",
        "priority": "S",
        "lang":     "en",
    },
    {
        "id":       "ecb_press",
        "name":     "ECB Press Releases",
        "url":      "https://www.ecb.europa.eu/rss/press.html",
        "priority": "S",
        "lang":     "en",
    },
    {
        "id":       "boj_press",
        "name":     "日銀 公表資料",
        "url":      "https://www.boj.or.jp/rss/whatsnew.xml",
        "priority": "S",
        "lang":     "ja",
    },
    {
        "id":       "mof_press",
        "name":     "財務省 報道発表",
        "url":      "https://www.mof.go.jp/rss/release.xml",
        "priority": "S",
        "lang":     "ja",
    },
    {
        "id":       "bis_press",
        "name":     "BIS（国際決済銀行）",
        "url":      "https://www.bis.org/doclist/bis_fsi_publs.rss",
        "priority": "S",
        "lang":     "en",
    },
    # ── 優先度A: テーマ・地域専門 ────────────────────────────
    {
        "id":       "quantum_insider",
        "name":     "Quantum Insider",
        "url":      "https://thequantuminsider.com/feed/",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "defense_news",
        "name":     "Defense News",
        "url":      "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "arabian_business",
        "name":     "Arabian Business",
        "url":      "https://www.arabianbusiness.com/rss",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "globes_en",
        "name":     "Globes（イスラエル英語版）",
        "url":      "https://en.globes.co.il/en/rss",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "hindu_business",
        "name":     "The Hindu Business Line",
        "url":      "https://www.thehindubusinessline.com/feeder/default.rss",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "opensecrets",
        "name":     "OpenSecrets（政治資金）",
        "url":      "https://www.opensecrets.org/news/feed",
        "priority": "A",
        "lang":     "en",
    },
    {
        "id":       "scmp_business",
        "name":     "South China Morning Post Business",
        "url":      "https://www.scmp.com/rss/91/feed",
        "priority": "A",
        "lang":     "en",
    },
]


# ==============================================================
# BLOCK 1: 業種キーワードマッピング
# ==============================================================
# 各業種コードに対して「この単語が記事に含まれたらマッチ」を定義
# 日本語・英語の両方に対応

# 2026-09-17: 英字は単語単位の照合に変えたため（_keyword_hit）、部分一致で偶然
# 拾えていた原油・IPO・フィンテック等の記事が落ちないよう語を補った。
SECTOR_KEYWORDS = {
    "1001": ["水産", "農林", "漁業", "養殖", "fishery", "agriculture", "farming"],
    "1002": ["鉱業", "石油", "天然ガス", "LNG", "原油", "mining", "crude oil", "natural gas"],
    "1003": ["建設", "ゼネコン", "工事", "不動産開発", "construction", "infrastructure", "contractor"],
    "1004": ["食料", "食品", "飲料", "農産物", "food", "beverage", "agriculture commodity"],
    "1005": ["繊維", "アパレル", "衣料", "textile", "apparel", "fashion", "clothing"],
    "1006": ["パルプ", "紙", "印刷", "paper", "pulp", "printing"],
    "1007": ["化学", "石化", "ナフサ", "chemical", "petrochemical", "naphtha", "plastics"],
    "1008": ["医薬品", "製薬", "創薬", "FDA", "臨床試験", "pharma", "drug", "biotech", "clinical trial"],
    "1009": ["石油精製", "ガソリン", "原油価格", "refinery", "gasoline", "WTI", "OPEC", "crude", "oil price"],
    "1010": ["タイヤ", "ゴム", "EV タイヤ", "tire", "rubber"],
    "1011": ["ガラス", "セメント", "セラミック", "glass", "cement", "ceramic"],
    "1012": ["鉄鋼", "鉄", "steel", "iron ore", "blast furnace"],
    "1013": ["非鉄金属", "銅", "アルミ", "リチウム", "copper", "aluminum", "lithium", "rare earth",
             "gold", "silver"],
    "1014": ["金属部品", "加工", "metal parts", "precision parts"],
    "1015": ["機械", "工作機械", "ロボット", "設備投資", "machinery", "robot", "capex", "automation"],
    "1016": ["電気機器", "半導体", "AI", "データセンター", "電子部品",
             "semiconductor", "electronics", "AI chip", "data center", "chip"],
    "1017": ["自動車", "EV", "電気自動車", "トヨタ", "ホンダ",
             "automotive", "electric vehicle", "Tesla", "Toyota", "Honda",
             "automaker", "carmaker"],
    "1018": ["精密機器", "光学", "医療機器", "カメラ", "precision instrument", "medical device", "optical", "camera"],
    "1019": ["ゲーム", "玩具", "任天堂", "ソニー", "コンテンツ", "game", "Nintendo", "entertainment"],
    "1020": ["電力", "ガス", "原子力", "再生エネ", "電気料金",
             "electricity", "power grid", "nuclear energy", "renewable", "uranium"],
    "1021": ["鉄道", "トラック", "物流", "JR", "宅配",
             "railway", "logistics", "trucking", "freight"],
    "1022": ["海運", "コンテナ", "バルク", "shipping", "container ship", "bulk carrier", "freight rate"],
    "1023": ["航空", "空運", "ANA", "JAL", "airline", "aviation", "jet fuel"],
    "1024": ["倉庫", "港湾", "フォワーダー", "warehouse", "port", "forwarding", "3PL"],
    "1025": ["IT", "情報通信", "ソフトウェア", "クラウド", "DX",
             "software", "cloud", "SaaS", "digital transformation", "cybersecurity",
             "fintech"],
    "1026": ["商社", "資源", "トレーディング", "trading company", "commodities trading"],
    "1027": ["小売", "百貨店", "コンビニ", "EC", "アマゾン",
             "retail", "e-commerce", "Amazon", "consumer spending"],
    "1028": ["銀行", "金融政策", "日銀", "金利", "FRB",
             "bank", "Fed", "interest rate", "monetary policy", "BOJ",
             "Federal Reserve", "FOMC"],
    "1029": ["証券", "株式市場", "ボラティリティ", "securities", "stock market", "volatility", "VIX", "IPO"],
    "1030": ["保険", "自然災害", "台風", "地震", "insurance", "natural disaster", "hurricane"],
    "1031": ["消費者金融", "クレジット", "リース", "consumer finance", "credit card", "leasing"],
    "1032": ["不動産", "住宅", "REIT", "real estate", "housing", "property", "REIT"],
    "1033": ["サービス", "外食", "観光", "インバウンド", "人材",
             "service", "tourism", "inbound", "staffing", "hospitality"],
}

# マクロ全体に影響するキーワード（全業種に関連）
MACRO_KEYWORDS = [
    "日銀", "BOJ", "FRB", "Fed", "Federal Reserve", "FOMC", "ECB", "利上げ", "利下げ", "金利",
    "インフレ", "GDP", "景気後退", "recession", "inflation", "rate hike",
    "円安", "円高", "yen", "dollar", "為替",
    "VIX", "リスクオフ", "risk-off", "risk off",
    "地政学", "制裁", "戦争", "geopolitical", "sanction", "関税", "tariff",
]

# 重要度加算キーワード（スコア+1）
HIGH_IMPORTANCE_KEYWORDS = [
    "緊急", "速報", "急落", "急騰", "破綻", "デフォルト", "利上げ",
    "breaking", "surge", "crash", "collapse", "emergency", "rate hike",
    "Fed", "FRB", "日銀", "BOJ", "ECB", "OPEC",
]


# ==============================================================
# BLOCK 2: RSS取得
# ==============================================================

def _fetch_feed(feed_def: dict, timeout: int = 15) -> list:
    """
    1つのRSSフィードを取得してArticleリストを返す。
    requests で生XML取得 → feedparser に渡す方式。
    （feedparser直呼び出しはSSL/UA問題で0件になることがある）

    Returns:
        [{"title": str, "url": str, "summary": str,
          "source": str, "pub_date": str}, ...]
    """
    try:
        import requests
        import feedparser
    except ImportError:
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
            "+https://github.com/stockanalyzer)"
        )
    }

    try:
        resp = requests.get(feed_def["url"], headers=headers,
                            timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return []
        parsed = feedparser.parse(resp.content)
    except Exception:
        return []

    articles = []
    cutoff   = datetime.now() - timedelta(days=2)  # 2日以内の記事のみ

    for entry in parsed.entries[:30]:  # 1フィードあたり最大30件
        try:
            # 公開日の取得
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub:
                pub_dt   = datetime(*pub[:6])
                pub_str  = pub_dt.strftime("%Y-%m-%d %H:%M")
                if pub_dt < cutoff:
                    continue  # 古い記事はスキップ
            else:
                pub_str  = ""

            title   = entry.get("title", "").strip()
            url     = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()
            # HTML タグを簡易除去
            import re
            summary = re.sub(r"<[^>]+>", "", summary)[:300]

            if not title:
                continue

            articles.append({
                "title":    title,
                "url":      url,
                "summary":  summary,
                "source":   feed_def["name"],
                "priority": feed_def["priority"],
                "lang":     feed_def["lang"],
                "pub_date": pub_str,
            })
        except Exception:
            continue

    return articles


def fetch_all_feeds(feeds: list, cfg) -> list:
    """
    設定された全フィードを取得する。
    config.py の NEWS_FEEDS_ENABLED で有効なフィードIDを絞れる。
    """
    enabled_ids = getattr(cfg, "NEWS_FEEDS_ENABLED", None)  # None=全て有効

    all_articles = []
    for feed in feeds:
        if enabled_ids is not None and feed["id"] not in enabled_ids:
            continue
        arts = _fetch_feed(feed)
        all_articles.extend(arts)
        print(f"    [{feed['priority']}] {feed['name']}: {len(arts)}件")
        time.sleep(0.5)  # レート制限対策

    return all_articles


# ==============================================================
# BLOCK 3: 業種キーワードマッチング
# ==============================================================

_ASCII_KEYWORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&/+-]*$")


def _keyword_hit(keyword: str, text: str) -> bool:
    """キーワードが記事本文に含まれるかを判定する。

    2026-09-17: 英字キーワードも部分一致で照合しており、"AI" が "said" /
    "Taiwan" に、"IT" が "with" に、"EC" が "tech" に、"ANA" が "Analysis" に、
    "port" が "report" に、"Fed" が "Federal" に当たっていた（9/17 は Defense News の
    軍事記事の大半が IT・半導体業種にマッチ）。
      - 3文字以下の英字は単語単位。大文字を含む略語は大文字のまま（代名詞 "it" を拾わない）
      - 4文字以上の英字は語頭一致（"ports" "chips" "sales" は拾い、"report" は拾わない）
      - 日本語は従来どおり部分一致
    """
    if not keyword:
        return False
    if not _ASCII_KEYWORD.match(keyword):
        return keyword.lower() in text.lower()
    if len(keyword) <= 3:
        flags = 0 if any(ch.isupper() for ch in keyword) else re.IGNORECASE
        pattern = rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])"
        return re.search(pattern, text, flags) is not None
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(keyword)}", text, re.IGNORECASE) is not None


# 経済・企業活動の語。事件・事故の表題でもこれを含めば投資ニュースとして残す。
INVESTMENT_KEYWORDS = [
    "株", "市場", "相場", "決算", "業績", "売上", "利益", "赤字", "黒字", "上場",
    "買収", "出資", "投資", "増産", "減産", "受注", "値上げ", "価格", "金利",
    "利上げ", "利下げ", "為替", "円安", "円高", "物価", "インフレ", "景気",
    "経済", "関税", "輸出", "輸入", "原油", "半導体", "企業", "工場", "生産",
    "日銀", "財政", "国債", "債券", "規制", "補助金", "制裁", "株主", "配当",
    "資金調達", "倒産", "経営", "リコール", "損害", "賠償", "億円", "兆円",
    "stock", "share", "market", "earnings", "profit", "revenue", "sale",
    "investor", "investment", "IPO", "bond", "yield", "rate hike", "rate cut",
    "inflation", "GDP", "economy", "economic", "export", "tariff", "price",
    "acquisition", "merger", "takeover", "deal", "Fed", "central bank",
    "semiconductor", "factory", "supply chain", "valuation", "debt", "stake",
    "sanction", "dividend", "buyback", "shareholder", "billion", "million",
    "recall", "lawsuit", "regulator", "yen", "dollar", "OPEC", "BOJ", "ECB",
]

# 投資に関係しない一般ニュース（事件・事故・天気・スポーツ・芸能・人事外の政治）の目印。
# 表題だけで判定し、上の経済の語を含む記事は除外しない。
NON_INVESTMENT_KEYWORDS = [
    "事件", "殺人", "死亡", "遺体", "逮捕", "容疑", "被告", "判決", "刺され",
    "強盗", "交通事故", "火事", "火災", "行方不明", "天気", "大雨", "熱中症",
    "野球", "サッカー", "大相撲", "五輪", "芸能", "結婚", "不倫", "総書記",
    "murder", "killed", "shooting", "arrested", "sentenced", "convicted",
    "football", "baseball", "Olympic", "celebrity", "weather",
]


def is_investment_relevant(article: dict) -> bool:
    """記事が投資判断に関係するかを返す（2026-09-17 追加）。

    9/17 の注目ニュースに「洋菓子店4人死亡事件」（"ガソリン"・"住宅"で業種マッチ）や
    「空自グローバルホーク墜落」（"said" の "ai" で半導体にマッチ）が入っていた。
      1. 表題に事件・事故・天気・スポーツ等の語があり、経済の語が無い → 除外
      2. 業種キーワードにも経済の語にも当たらない → 除外
    業種・業界ニュース（鉱山の新規発見、EVの新型車など）は 2 で残る。
    """
    title = str(article.get("title", "") or "")
    text = title + " " + str(article.get("summary", "") or "")
    economic = any(_keyword_hit(kw, text) for kw in INVESTMENT_KEYWORDS)
    if not economic and any(_keyword_hit(kw, title) for kw in NON_INVESTMENT_KEYWORDS):
        return False
    if economic:
        return True
    return any(
        _keyword_hit(kw, text)
        for kws in SECTOR_KEYWORDS.values() for kw in kws
    )


def _importance_score(article: dict) -> int:
    """
    記事の重要度スコアを計算する（0〜5）。
    Priority S の一次情報源 + 高重要度キーワード含有で加算。
    """
    score = 0
    if article.get("priority") == "S":
        score += 1

    text = article.get("title", "") + " " + article.get("summary", "")

    for kw in HIGH_IMPORTANCE_KEYWORDS:
        if _keyword_hit(kw, text):
            score += 1
            break  # 1ヒットで+1（累積しない）

    return min(score, 5)


def match_sectors(articles: list, sector_keywords: dict,
                  macro_keywords: list) -> list:
    """
    全記事に対して業種キーワードマッチングを実行し、
    matched_sectors と importance_score を付与して返す。
    """
    enriched = []
    for art in articles:
        text    = art["title"] + " " + art.get("summary", "")
        matched = []

        # 業種マッチ
        for code, keywords in sector_keywords.items():
            for kw in keywords:
                if _keyword_hit(kw, text):
                    matched.append(code)
                    break

        # マクロキーワードは全業種コード "macro" で格納
        for kw in macro_keywords:
            if _keyword_hit(kw, text):
                matched.append("macro")
                break

        a = dict(art)
        a["matched_sectors"]   = list(set(matched))
        a["importance_score"]  = _importance_score(a)
        a["investment_relevant"] = is_investment_relevant(a)
        enriched.append(a)

    return enriched


def group_by_sector(articles: list) -> dict:
    """
    matched_sectorsで業種別に記事を集約する。
    各業種内は importance_score 降順でソート。
    """
    by_sector: dict = {}
    for art in articles:
        for code in art.get("matched_sectors", []):
            by_sector.setdefault(code, []).append(art)

    for code in by_sector:
        by_sector[code].sort(
            key=lambda x: x.get("importance_score", 0), reverse=True
        )
    return by_sector


# ==============================================================
# BLOCK 4: レポート保存
# ==============================================================

def _save_news_report(result: dict, cfg) -> None:
    """insights/news/news_YYYY-MM-DD.md に保存する。"""
    today     = datetime.now().strftime("%Y-%m-%d")
    news_dir  = Path(getattr(cfg, "INSIGHT_DIR", "insights")) / "news"

    try:
        news_dir.mkdir(parents=True, exist_ok=True)
        now   = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# ニュースレポート（{today}）",
            f"**生成日時**: {now}  |  "
            f"**取得記事数**: {result['fetch_count']}  |  "
            f"**マッチ記事数**: {result['match_count']}",
            "",
        ]

        # 重要記事トップ10
        top = result.get("top_articles", [])[:10]
        if top:
            lines += ["## 🔥 重要ニュース TOP10", ""]
            for i, a in enumerate(top, 1):
                stars = "★" * a.get("importance_score", 0)
                lines.append(
                    f"{i}. [{a['source']}] **{a['title'][:80]}**  "
                    f"`{stars}` {a.get('pub_date', '')}"
                )
                if a.get("url"):
                    lines.append(f"   {a['url']}")
            lines.append("")

        # 業種別ニュース（マッチ件数上位10業種）
        by_sector = result.get("by_sector", {})
        if by_sector:
            lines += ["## 📊 業種別ニュース", ""]
            sorted_sectors = sorted(
                by_sector.items(),
                key=lambda x: len(x[1]), reverse=True
            )
            for code, arts in sorted_sectors[:10]:
                name = (
                    "マクロ全体" if code == "macro"
                    else f"業種 {code}"
                )
                lines.append(f"### {name}（{len(arts)}件）")
                for a in arts[:3]:
                    lines.append(f"- [{a['source']}] {a['title'][:70]}")
                lines.append("")

        out = news_dir / f"news_{today}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"  💾 ニュースレポート保存: news_{today}.md")

    except Exception as e:
        print(f"  ⚠️  ニュースレポート保存失敗: {str(e)[:60]}")


def _save_news_cache(articles: list, cfg) -> None:
    """articles を JSON キャッシュとして保存（当日分）"""
    today    = datetime.now().strftime("%Y-%m-%d")
    data_dir = Path(getattr(cfg, "DATA_DIR", "data"))
    cache    = data_dir / "news_cache" / f"news_{today}.json"
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(articles, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_news_pipeline(cfg) -> dict:
    """
    ニュースRSSパイプラインを実行する。

    Returns:
        {
          "articles":     全取得記事リスト,
          "by_sector":    業種別マッチ記事 dict,
          "top_articles": 重要度上位N件,
          "fetch_count":  取得記事数,
          "match_count":  少なくとも1業種にマッチした記事数,
          "timestamp":    実行日時,
        }
    """
    print("\n  📰 ニュースRSSパイプライン 開始...")
    t0 = datetime.now()

    # フィード取得
    print(f"  📡 {len(RSS_FEEDS)}フィードを取得中...")
    articles = fetch_all_feeds(RSS_FEEDS, cfg)
    # 有料メディア記事を Google CSE で補完
    # 2026-09-12: 無効時はこの if で関数ごと呼ばれず、paid_media_searcher 側の
    # スキップ表示（「⏩ 有料メディア検索: スキップ」）にも到達しないため、
    # ワークフローのログ上は「何も起きていない」ようにしか見えなかった。
    # 止めていることが実行ログから判るよう、ここで明示する。
    if not getattr(cfg, "ENABLE_PAID_MEDIA_SEARCH", False):
        print("  ⏩ 有料メディア検索（Google CSE）: スキップ"
              "（ENABLE_PAID_MEDIA_SEARCH=False / APIキー要点検）")
    if getattr(cfg, "ENABLE_PAID_MEDIA_SEARCH", False):
        try:
            import importlib.util, sys
            for p in ["/content/paid_media_searcher.py",
                      str(__file__).replace("news_pipeline.py",
                                            "paid_media_searcher.py")]:
                if __import__("pathlib").Path(p).exists():
                    spec = importlib.util.spec_from_file_location(
                        "paid_media_searcher", p)
                    _pm = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(_pm)
                    paid_articles = _pm.fetch_paid_media_articles(cfg)
                    articles.extend(paid_articles)
                    break
        except Exception as e:
            print(f"  ⚠️  有料メディア検索エラー: {str(e)[:60]}")
    print(f"  → 合計 {len(articles)} 件取得")

    if not articles:
        print("  ⚠️  記事が0件でした（フィード接続失敗の可能性）")
        return {
            "articles": [], "by_sector": {}, "top_articles": [],
            "fetch_count": 0, "match_count": 0,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # キーワードマッチング
    enriched    = match_sectors(articles, SECTOR_KEYWORDS, MACRO_KEYWORDS)
    # 投資に関係しない記事（事件・事故・天気など）は業種別・注目ニュースから外す。
    # 全記事（articles）には判定結果を付けて残す（taxonomy_hotness 等が全件を使う）。
    matched     = [a for a in enriched
                   if a["matched_sectors"] and a["investment_relevant"]]
    n_excluded  = sum(1 for a in enriched
                      if a["matched_sectors"] and not a["investment_relevant"])
    by_sector   = group_by_sector(matched)

    # 重要度上位N件を抽出
    top_n       = getattr(cfg, "NEWS_TOP_N", 10)
    # 業種に当たらない経済ニュース（社債発行・輸出統計など）も注目ニュースの候補に含める
    top_articles = sorted(
        [a for a in enriched if a["investment_relevant"]],
        key=lambda x: x.get("importance_score", 0), reverse=True
    )[:top_n]

    elapsed = (datetime.now() - t0).seconds
    print(
        f"  ✅ ニュースRSS完了（{elapsed}秒）: "
        f"取得{len(articles)}件 / マッチ{len(matched)}件 / "
        f"投資無関係で除外{n_excluded}件 / 業種数{len(by_sector)}業種"
    )

    result = {
        "articles":     enriched,
        "by_sector":    by_sector,
        "top_articles": top_articles,
        "fetch_count":  len(articles),
        "match_count":  len(matched),
        "excluded_count": n_excluded,
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_news_report(result, cfg)
    # ── [RESEARCH-5] sentiment フィールドの初期化（FinBERT が後から上書き）──
    for a in enriched:
        a.setdefault("sentiment_label", "NEUTRAL")
        a.setdefault("sentiment_score", 0.5)
    _save_news_cache(enriched, cfg)
    return result


# ==============================================================
# BLOCK 6: alert_notifier 連携用フォーマッタ
# ==============================================================

def format_news_for_alert(result: dict, sector_results: dict,
                           max_articles: int = 5) -> str:
    """
    alert_notifier.py のアラート本文用に
    「本日の注目ニュース」セクションを生成する。

    sector_results が与えられた場合は
    上位スコア業種の関連ニュースを優先して表示する。
    """
    lines = []

    # 上位スコア業種の関連ニュース
    by_sector = result.get("by_sector", {})
    if sector_results and by_sector:
        sorted_secs = sorted(
            sector_results.items(),
            key=lambda x: x[1].get("composite_score", 0) if isinstance(x[1], dict) else 0,
            reverse=True
        )
        shown = 0
        for code, _ in sorted_secs[:5]:
            arts = [a for a in by_sector.get(code, []) if is_investment_relevant(a)]
            if arts and shown < max_articles:
                a = arts[0]
                lines.append(
                    f"  [{a['source']}] {a['title'][:60]}"
                )
                shown += 1

    # 残りは重要度順で補完
    if len(lines) < max_articles:
        top = result.get("top_articles", [])
        shown_urls = {l for l in lines}
        for a in top:
            if len(lines) >= max_articles:
                break
            if not is_investment_relevant(a):
                continue
            title_line = f"  [{a['source']}] {a['title'][:60]}"
            if title_line not in shown_urls:
                lines.append(title_line)

    return "\n".join(lines)


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

    result = run_news_pipeline(cfg)
    print(f"\n取得: {result['fetch_count']}件")
    print(f"マッチ: {result['match_count']}件")
    print(f"業種数: {len(result['by_sector'])}業種")
    print("\n--- 重要ニュース TOP5 ---")
    for a in result["top_articles"][:5]:
        print(f"  [{a['source']}] {a['title'][:60]}")
