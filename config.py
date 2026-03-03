# ==============================================================
# config.py
# 自律型投資AIエージェント基盤 — 設定ファイル v1.4
# 【v1.4 変更点】3-C: 政策金利トラッカー設定（BLOCK 10）を追加
# ==============================================================
# 【役割】
#   全モジュールがこのファイルだけを参照することで
#   「設定の一元管理」を実現する。
#
# 【重要ルール】
#   ★ このファイル自身は他のモジュールを一切 import しない
#     → 循環参照ゼロを構造的に保証
#
# 【変更方法】
#   業種ルール・API URL・閾値の変更は「このファイルのみ」編集する。
#   arxiv_monitor.py / stock_analyzer_main.py は変更不要。
#
# 【参照方法（各モジュールで）】
#   import config as cfg
#   cfg.DATA_DIR / cfg.MACRO_RULES など
# ==============================================================

from pathlib import Path

# ==============================================================
# BLOCK 0: ディレクトリパス定数
# ==============================================================

DRIVE_BASE   = Path("/content/drive/MyDrive/stock_analyzer")
VIEWER_BASE  = Path("/content/drive/MyDrive/stock_viewer")

DATA_DIR     = DRIVE_BASE / "data"
RESULT_DIR   = DRIVE_BASE / "results"
INSIGHT_DIR  = DRIVE_BASE / "insights"
ARXIV_DIR    = INSIGHT_DIR / "arxiv"
SECTOR_DIR   = INSIGHT_DIR / "sectors"       # Step 2 以降で使用
MACRO_DIR    = INSIGHT_DIR / "macro"         # Step 2 以降で使用
LOG_DIR      = DRIVE_BASE / "logs"
AGENTS_DIR   = DRIVE_BASE / "sector_agents"  # Step 2 以降で使用

# setup_dirs() で一括作成するフォルダ一覧
ALL_DIRS = [
    DATA_DIR, RESULT_DIR, INSIGHT_DIR,
    ARXIV_DIR, SECTOR_DIR, MACRO_DIR,
    LOG_DIR, AGENTS_DIR,
]

# ==============================================================
# BLOCK 1: API / 実行設定
# ==============================================================

SPREADSHEET_ID   = "1CePiavYj_6OR0cOz84V4RkA0c0S6IwZlcHCv8_bsOjc"
CREDENTIALS_PATH = str(VIEWER_BASE / "service_account.json")
WRITE_SHEET      = False   # True にすると Google Sheets に書き込む

RUN_SCHEDULE_HOUR   = 8    # 毎朝何時に自動実行するか（0〜23）
RUN_SCHEDULE_MINUTE = 0

# モジュール有効フラグ（False にすると当該モジュールをスキップ）
ENABLE_GOV_PIPELINE = True
ENABLE_ARXIV        = True
ENABLE_SECTOR       = True   # 2-A 完了: 6業種エージェント実装済み
ENABLE_MACRO        = True  # Step 2 以降で True に変更

LOG_MAX_ENTRIES = 500
LOG_FILENAME    = "run_history.json"

# ==============================================================
# BLOCK 2: ArXiv 設定
# ==============================================================
# 【v1.0 との差分】
#   ・rss.arxiv.org → export.arxiv.org を primary に変更
#     （export.arxiv.org は ArXiv 公式ミラーで rss より安定）
#   ・primary / fallback の 2 URL 構成に変更
#   ・requests で生 XML を取得 → feedparser に渡す方式を採用
#     （feedparser 直接呼び出しは SSL/UA 問題で 0 件になる場合がある）

ARXIV_FEEDS = {
    "q-fin.PM": {
        "primary":  "https://export.arxiv.org/rss/q-fin.PM",
        "fallback": "https://rss.arxiv.org/rss/q-fin.PM",
        "label":    "ポートフォリオ管理",
    },
    "q-fin.TR": {
        "primary":  "https://export.arxiv.org/rss/q-fin.TR",
        "fallback": "https://rss.arxiv.org/rss/q-fin.TR",
        "label":    "取引・市場微細構造",
    },
    "q-fin.ST": {
        "primary":  "https://export.arxiv.org/rss/q-fin.ST",
        "fallback": "https://rss.arxiv.org/rss/q-fin.ST",
        "label":    "統計ファイナンス",
    },
    "q-fin.RM": {
        "primary":  "https://export.arxiv.org/rss/q-fin.RM",
        "fallback": "https://rss.arxiv.org/rss/q-fin.RM",
        "label":    "リスク管理",
    },
    "econ.EM": {
        "primary":  "https://export.arxiv.org/rss/econ.EM",
        "fallback": "https://rss.arxiv.org/rss/econ.EM",
        "label":    "計量経済学",
    },
}

ARXIV_MAX_PER_FEED         = 30    # 1フィードあたり最大取得件数（v1.0: 20）
ARXIV_RETRY_COUNT          = 3     # 1 URL あたりのリトライ回数（v1.0: なし）
ARXIV_RETRY_WAIT_SEC       = 5     # リトライ間隔秒（指数バックオフ: 5s, 10s）
ARXIV_REQUEST_TIMEOUT      = 20    # HTTP タイムアウト（秒）
ARXIV_INVESTMENT_THRESHOLD = 3     # このスコア以上を「投資アイデア候補」とする

# HTTP リクエストヘッダー（ArXiv が UA を検査するため必須）
# v1.0 では UA を指定していなかったため弾かれる場合があった
ARXIV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
        "+https://github.com/user/stock-analyzer)"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# 投資キーワード定義
# 【v1.0 との差分】
#   ・文字列リストから (keyword, タイトル点数, サマリー点数) のタプルリストに変更
#   ・タイトルヒット時とサマリーヒット時で点数を区別できるようになった
#   ・日本市場関連キーワードは高点数（3/2）を設定
ARXIV_KEYWORDS = [
    # ── 日本・アジア市場（高優先度）──────────────────────────
    ("japan",              3, 2),
    ("nikkei",             3, 2),
    ("topix",              3, 2),
    ("tokyo stock",        3, 2),
    ("japanese stock",     3, 2),
    ("emerging market",    2, 1),
    ("asia",               2, 1),
    # ── 投資手法・ファクター ──────────────────────────────────
    ("momentum",           2, 1),
    ("factor investing",   3, 2),
    ("anomaly",            2, 1),
    ("alpha",              2, 1),
    ("return prediction",  3, 2),
    ("machine learning",   2, 1),
    ("deep learning",      2, 1),
    ("lstm",               2, 1),
    ("transformer",        2, 1),
    ("nlp",                2, 1),
    ("large language",     2, 1),
    ("sentiment",          2, 1),
    # ── オルタナティブデータ ──────────────────────────────────
    ("alternative data",   3, 2),
    ("satellite",          3, 2),
    ("news",               1, 1),
    ("social media",       2, 1),
    ("earnings",           2, 1),
    ("revenue",            1, 1),
    ("dividend",           2, 1),
    # ── 戦略 ─────────────────────────────────────────────────
    ("arbitrage",          2, 1),
    ("mean reversion",     2, 1),
    ("trend following",    2, 1),
    ("pairs trading",      2, 1),
    ("short selling",      2, 1),
    ("event study",        2, 1),
    # ── リスク ───────────────────────────────────────────────
    ("volatility",         2, 1),
    ("drawdown",           2, 1),
    ("tail risk",          3, 2),
    ("systemic risk",      2, 1),
    ("crisis",             1, 1),
    # ── 市場全般 ─────────────────────────────────────────────
    ("equity",             1, 1),
    ("stock market",       2, 1),
    ("portfolio",          1, 1),
    ("hedge fund",         2, 1),
    # ── マクロ・コモディティ ──────────────────────────────────
    ("commodity",          2, 1),
    ("agriculture",        2, 1),
    ("inflation",          2, 1),
    ("interest rate",      2, 1),
    ("yield curve",        2, 1),
    ("consumer price",     2, 1),
    ("supply chain",       2, 1),
    ("climate",            2, 1),
    ("weather",            1, 1),
]

# ==============================================================
# BLOCK 3: 日本株 33業種 定義（東証業種コード準拠）
# ==============================================================
# 【v1.0 との差分】新規追加
# 将来: sector_agents/{code}_agent.py のファイル名キーとして使用

JAPAN_SECTORS = {
    "1001": {"name_ja": "水産・農林業",         "name_en": "Fishery, Agriculture & Forestry",    "group": "素材"},
    "1002": {"name_ja": "鉱業",                 "name_en": "Mining",                              "group": "素材"},
    "1003": {"name_ja": "建設業",               "name_en": "Construction",                        "group": "内需"},
    "1004": {"name_ja": "食料品",               "name_en": "Foods",                               "group": "内需"},
    "1005": {"name_ja": "繊維製品",             "name_en": "Textiles & Apparel",                  "group": "素材"},
    "1006": {"name_ja": "パルプ・紙",           "name_en": "Pulp & Paper",                        "group": "素材"},
    "1007": {"name_ja": "化学",                 "name_en": "Chemicals",                           "group": "素材"},
    "1008": {"name_ja": "医薬品",               "name_en": "Pharmaceutical",                      "group": "ヘルスケア"},
    "1009": {"name_ja": "石油・石炭製品",       "name_en": "Oil & Coal Products",                 "group": "エネルギー"},
    "1010": {"name_ja": "ゴム製品",             "name_en": "Rubber Products",                     "group": "素材"},
    "1011": {"name_ja": "ガラス・土石製品",     "name_en": "Glass & Ceramics Products",           "group": "素材"},
    "1012": {"name_ja": "鉄鋼",                 "name_en": "Iron & Steel",                        "group": "素材"},
    "1013": {"name_ja": "非鉄金属",             "name_en": "Nonferrous Metals",                   "group": "素材"},
    "1014": {"name_ja": "金属製品",             "name_en": "Metal Products",                      "group": "素材"},
    "1015": {"name_ja": "機械",                 "name_en": "Machinery",                           "group": "製造"},
    "1016": {"name_ja": "電気機器",             "name_en": "Electric Appliances",                 "group": "製造"},
    "1017": {"name_ja": "輸送用機器",           "name_en": "Transportation Equipment",            "group": "製造"},
    "1018": {"name_ja": "精密機器",             "name_en": "Precision Instruments",               "group": "製造"},
    "1019": {"name_ja": "その他製品",           "name_en": "Other Products",                      "group": "製造"},
    "1020": {"name_ja": "電気・ガス業",         "name_en": "Electric Power & Gas",                "group": "インフラ"},
    "1021": {"name_ja": "陸運業",               "name_en": "Land Transportation",                 "group": "インフラ"},
    "1022": {"name_ja": "海運業",               "name_en": "Marine Transportation",               "group": "インフラ"},
    "1023": {"name_ja": "空運業",               "name_en": "Air Transportation",                  "group": "インフラ"},
    "1024": {"name_ja": "倉庫・運輸関連業",     "name_en": "Warehousing & Harbor Transportation", "group": "インフラ"},
    "1025": {"name_ja": "情報・通信業",         "name_en": "Information & Communication",         "group": "テック"},
    "1026": {"name_ja": "卸売業",               "name_en": "Wholesale Trade",                     "group": "商業"},
    "1027": {"name_ja": "小売業",               "name_en": "Retail Trade",                        "group": "商業"},
    "1028": {"name_ja": "銀行業",               "name_en": "Banks",                               "group": "金融"},
    "1029": {"name_ja": "証券・商品先物取引業", "name_en": "Securities & Commodity Futures",      "group": "金融"},
    "1030": {"name_ja": "保険業",               "name_en": "Insurance",                           "group": "金融"},
    "1031": {"name_ja": "その他金融業",         "name_en": "Other Financing Business",            "group": "金融"},
    "1032": {"name_ja": "不動産業",             "name_en": "Real Estate",                         "group": "不動産"},
    "1033": {"name_ja": "サービス業",           "name_en": "Services",                            "group": "サービス"},
}

SECTOR_GROUPS = sorted(set(v["group"] for v in JAPAN_SECTORS.values()))

# ==============================================================
# BLOCK 4: マクロ経済 判定ロジック（Master Data）
# ==============================================================
# 【v1.0 との差分】新規追加（v1.0 にはこのロジック自体が存在しなかった）
#
# 【設計思想】
#   「金利上昇=銀行に追い風」などの産業構造ロジックを
#   コードから完全分離し、辞書データとして管理する（OCP 原則）。
#   stock_analyzer_main.py はこの辞書を読むだけ。
#   新しいマクロ要因の追加・既存ルールの変更は
#   このファイルのみを編集すれば全体に反映される。
#
# signal 値の意味:
#   "tailwind" → 追い風 💚
#   "headwind" → 逆風   🔴
#   "watch"    → 要注意 🟡
#
# sector_code は JAPAN_SECTORS のキーと 1:1 対応。

MACRO_RULES = {

    # ── 金利上昇（日銀利上げ / 米金利上昇） ──────────────────
    "interest_rate_rise": {
        "label": "金利上昇",
        "signal": {
            "tailwind": ["1028", "1029", "1030", "1031"],  # 銀行・証券・保険
            "headwind": ["1032", "1021", "1022"],           # 不動産・陸運・海運
            "watch":    ["1017", "1015", "1016"],           # 自動車・機械・電気
        },
        "rationale": {
            "1028": "預貸金利ザヤ拡大 → 利益率改善期待",
            "1029": "債券トレーディング収益が増加しやすい",
            "1030": "運用利回り改善 → 保険収益にプラス",
            "1032": "住宅ローン金利上昇 → 需要減・資産価値圧迫",
            "1021": "借入コスト上昇 → 資本集約型の陸運に逆風",
            "1022": "船舶調達コスト上昇の懸念",
            "1017": "販売金融コスト上昇 → 自動車需要への影響を注視",
        },
    },

    # ── 金利低下（緩和継続 / 利下げ） ────────────────────────
    "interest_rate_fall": {
        "label": "金利低下",
        "signal": {
            "tailwind": ["1032", "1021", "1022", "1003"],  # 不動産・陸運・海運・建設
            "headwind": ["1028", "1030"],                   # 銀行・保険
            "watch":    [],
        },
        "rationale": {
            "1032": "住宅ローン低下 → 不動産需要回復",
            "1003": "建設資金調達コスト低下 → 着工増加期待",
            "1028": "預貸金利ザヤ縮小 → 収益圧迫",
            "1030": "運用利回り低下 → 保険収益にマイナス",
        },
    },

    # ── 円安進行（USD/JPY 上昇） ──────────────────────────────
    "yen_depreciation": {
        "label": "円安進行",
        "signal": {
            "tailwind": ["1017", "1015", "1016", "1007", "1022"],  # 製造・化学・海運
            "headwind": ["1004", "1027", "1021"],                   # 食料品・小売・陸運
            "watch":    ["1008", "1025"],                           # 医薬・情報通信
        },
        "rationale": {
            "1017": "輸出採算改善（自動車・部品の海外売上増）",
            "1016": "電子部品・半導体の輸出採算改善",
            "1022": "ドル建て運賃の円換算増 → 収益改善",
            "1004": "輸入原材料（小麦・大豆等）コスト上昇",
            "1027": "輸入品コスト増、消費者の購買力低下",
            "1008": "原材料輸入コスト増の一方、海外収益の円換算増も",
        },
    },

    # ── 円高進行（USD/JPY 下落） ──────────────────────────────
    "yen_appreciation": {
        "label": "円高進行",
        "signal": {
            "tailwind": ["1004", "1027", "1009"],   # 食料品・小売・石油
            "headwind": ["1017", "1015", "1016", "1022"],  # 製造・機械・電気・海運
            "watch":    [],
        },
        "rationale": {
            "1017": "輸出採算悪化 → 自動車の海外収益が目減り",
            "1004": "輸入原材料コスト低下 → 食料品原材料安",
            "1027": "輸入品コスト低下 → 小売の仕入れ改善",
        },
    },

    # ── 原油高 ────────────────────────────────────────────────
    "oil_price_rise": {
        "label": "原油高",
        "signal": {
            "tailwind": ["1009"],                          # 石油・石炭
            "headwind": ["1021", "1022", "1023", "1004"],  # 輸送全般・食料品
            "watch":    ["1007", "1017"],                  # 化学・自動車
        },
        "rationale": {
            "1009": "資源価格上昇 → 石油元売りの収益増",
            "1021": "燃料費上昇 → 価格転嫁できないと収益圧迫",
            "1023": "航空燃料費上昇 → コスト増大",
            "1004": "輸送コスト上昇が食料品価格に転嫁",
            "1007": "ナフサ等の原料コスト上昇、製品価格への転嫁が課題",
        },
    },

    # ── 米国景気後退リスク ────────────────────────────────────
    "us_recession_risk": {
        "label": "米景気後退リスク",
        "signal": {
            "tailwind": ["1020", "1008"],           # 電力ガス・医薬（ディフェンシブ）
            "headwind": ["1022", "1017", "1025"],   # 海運・自動車・情報通信
            "watch":    ["1028", "1016"],           # 銀行・電気機器
        },
        "rationale": {
            "1020": "内需ディフェンシブ。景気後退の影響が相対的に小さい",
            "1008": "医薬品需要は景気連動性が低い",
            "1022": "世界貿易縮小 → 海運需要急減リスク",
            "1017": "米国向け輸出需要の減少リスク",
            "1028": "不良債権増加リスクと金利低下圧力の双方に注意",
        },
    },

    # ── AI・半導体ブーム ──────────────────────────────────────
    "ai_semiconductor_boom": {
        "label": "AI・半導体ブーム",
        "signal": {
            "tailwind": ["1016", "1025", "1018", "1015"],  # 電気機器・情報通信・精密・機械
            "headwind": [],
            "watch":    ["1007"],                          # 化学（半導体材料）
        },
        "rationale": {
            "1016": "半導体・電子部品の需要増加",
            "1025": "AIクラウド・データセンター投資の恩恵",
            "1018": "半導体製造装置・精密機器の需要増",
            "1007": "半導体製造材料（フォトレジスト等）の需要増（正方向の watch）",
        },
    },

    # ── インフレ高進 ──────────────────────────────────────────
    "high_inflation": {
        "label": "インフレ高進",
        "signal": {
            "tailwind": ["1009", "1012", "1013"],   # エネルギー・鉄鋼・非鉄
            "headwind": ["1027", "1033"],           # 小売・サービス
            "watch":    ["1004", "1028"],           # 食料品・銀行
        },
        "rationale": {
            "1009": "エネルギー価格上昇 → 資源企業に追い風",
            "1012": "鋼材価格上昇 → 鉄鋼メーカーの利益改善",
            "1027": "コスト増加を価格転嫁できない場合は収益圧迫",
            "1033": "人件費・光熱費上昇が収益を圧迫",
            "1028": "貸出金利上昇で利ざや改善の一方、不良債権リスクも増加",
        },
    },

    # ── 世界的な地政学リスク上昇 ────────────────────────────
    "geopolitical_risk": {
        "label": "地政学リスク上昇",
        "signal": {
            "tailwind": ["1009", "1012", "1020"],   # エネルギー・鉄鋼・電力（有事需要）
            "headwind": ["1022", "1026", "1017"],   # 海運・卸売・自動車（サプライチェーン）
            "watch":    ["1025", "1016"],           # 情報通信・電気機器（デカップリング）
        },
        "rationale": {
            "1009": "資源価格高騰リスク → エネルギー企業に追い風",
            "1022": "航路変更・保険コスト上昇 → 海運コスト増大",
            "1026": "サプライチェーン混乱 → 卸売業の調達リスク",
            "1025": "デカップリング加速 → 海外事業・部品調達に影響",
        },
    },
}

# ==============================================================
# BLOCK 5: セクターエージェント インターフェース仕様
# ==============================================================
# 【v1.0 との差分】新規追加
#
# Step 2 以降で sector_agents/{code}_agent.py を実装する際の
# 共通インターフェース定義。stock_analyzer_main.py が動的ロードする。
#
# 各エージェントが export すべき関数シグネチャ:
#   run_analysis(sector_code: str, config: module) -> dict
#
# 返り値の必須キー（型チェック用）:
SECTOR_AGENT_RESULT_SCHEMA = {
    "sector_code":    str,   # "1028"
    "sector_name_ja": str,   # "銀行業"
    "analysis_date":  str,   # "2026-02-24"
    "macro_signals":  list,  # [{"factor": "interest_rate_rise", "signal": "tailwind", ...}]
    "top_companies":  list,  # [{"ticker": "8306.T", "name": "三菱UFJ"}]
    "arxiv_papers":   list,  # 関連 ArXiv 論文
    "insight_text":   str,   # Markdown 形式のインサイト
    "risk_level":     str,   # "high" / "medium" / "low"
}

# Step 1 時点で実装済みのエージェント（空リスト）
# Step 2 以降: ["1028", "1032", "1017", ...] と追記していく
# 2-A 完了: 6業種エージェントを追加
# 追加方法: sector_agents/{code}_agent.py を実装したらここにコードを追記するだけ
IMPLEMENTED_AGENTS: list = [
    "1001",  # 水産・農林業
    "1002",  # 鉱業
    "1003",  # 建設業
    "1004",  # 食料品
    "1005",  # 繊維製品
    "1006",  # パルプ・紙
    "1007",  # 化学
    "1008",  # 医薬品
    "1009",  # 石油・石炭製品
    "1010",  # ゴム製品
    "1011",  # ガラス・土石製品
    "1012",  # 鉄鋼
    "1013",  # 非鉄金属
    "1014",  # 金属製品
    "1015",  # 機械
    "1016",  # 電気機器
    "1017",  # 輸送用機器（自動車）
    "1018",  # 精密機器
    "1019",  # その他製品
    "1020",  # 電気・ガス業
    "1021",  # 陸運業
    "1022",  # 海運業
    "1023",  # 空運業
    "1024",  # 倉庫・運輸関連業
    "1025",  # 情報・通信業
    "1026",  # 卸売業
    "1027",  # 小売業
    "1028",  # 銀行業
    "1029",  # 証券・商品先物取引業
    "1030",  # 保険業
    "1031",  # その他金融業
    "1032",  # 不動産業
    "1033",  # サービス業
]

# ==============================================================
# BLOCK 6: マクロ指標 / 代表銘柄
# ==============================================================

# yfinance マクロ指標ティッカー
MACRO_TICKERS = {
    "usdjpy":  "USDJPY=X",    # USD/JPY
    "us10y":   "^TNX",        # 米10年債利回り
    "oil_wti": "CL=F",        # WTI 原油先物
    "gold":    "GC=F",        # 金先物
    "vix":     "^VIX",        # VIX 恐怖指数
    "sp500":   "^GSPC",       # S&P500
    "nikkei":  "^N225",       # 日経平均
}

# 業種代表銘柄（逆風・追い風アイコン表示のデフォルト対象）
SECTOR_REPRESENTATIVE_TICKERS = {
    "1028": ["8306.T", "8316.T", "8411.T"],   # 三菱UFJ・三井住友・みずほ
    "1032": ["8801.T", "8802.T", "8830.T"],   # 三井不動産・三菱地所・住友不動産
    "1017": ["7203.T", "7267.T", "7201.T"],   # トヨタ・ホンダ・日産
    "1025": ["9432.T", "9984.T", "4689.T"],   # NTT・ソフトバンクG・LY
    "1016": ["6758.T", "6501.T", "6702.T"],   # ソニー・日立・富士通
    "1022": ["9101.T", "9104.T", "9107.T"],   # 日本郵船・商船三井・川崎汽船
    "1009": ["5019.T", "5020.T"],             # 出光興産・ENEOSホールディングス
    "1004": ["2802.T", "2914.T", "2501.T"],   # 味の素・JT・サッポロHD
}

# ==============================================================
# BLOCK 7: 政府データ API
# ==============================================================

GOV_VEGPRICE_API = "https://api.cultivationdata.net/yasai_kakaku?div=all&type=csv"

# ==============================================================
# BLOCK 8: EDINET API 設定（2-B 追加）
# ==============================================================
# 【APIキーの取得手順】
#   1. https://disclosure.edinet-api.go.jp/ にアクセス
#   2. 「API 利用登録」から無料登録（メールアドレスのみ）
#   3. 発行されたキーをここに設定
#
# ★ 空文字のままにしておくと EDINET 取得をスキップして
#   他の分析には影響を与えない（エラーにならない）

EDINET_API_KEY: str = ""   # ここは金融庁のEDINETなのでもう使わない
EDINETDB_API_KEY: str = "REDACTED_EDINETDB"　# ここは民間のEDINETDBなので使う

# 有報の検索期間（日）: 直近 60 日以内の有報を検索（60回/社の検索）
EDINET_LOOKBACK_DAYS = 60

# 抽出するリスク要因の最大件数
EDINET_MAX_RISK_ITEMS = 5

# EDINET 取得対象の上位企業数（業種あたり。多すぎると実行時間が増える）
EDINET_MAX_COMPANIES = 2

# EDINET キャッシュ有効期間（日）: この日数以内に取得済みなら再取得しない
EDINET_CACHE_DAYS = 100

# EDINET を有効化するフラグ（APIキーが設定されていれば自動的に有効）
ENABLE_EDINET: bool = True  # APIキー設定後に True に変更


# ==============================================================
# BLOCK 9: タイムライン（二時間軸）設定（3-A 追加）
# ==============================================================

# タイムライン分析を有効化するマスタフラグ
ENABLE_TIMELINE: bool = True

# ── 長期軸（月次トレンド）───────────────────────────────────
# 長期トレンド分析を有効化
ENABLE_TIMELINE_LONG: bool = True

# 取得する月次データの期間（ヶ月）
LONG_TERM_MONTHS: int = 12

# 移動平均の窓幅（ヶ月）: 3 = 3ヶ月MA
LONG_TERM_MA_WINDOW: int = 3

# 転換点と判定する価格/MA 乖離率の閾値（0.02 = 2%）
TREND_CHANGE_THRESHOLD: float = 0.02

# ── 短期軸（デイリー騰落）──────────────────────────────────
# 短期デイリー分析を有効化
ENABLE_TIMELINE_SHORT: bool = True

# 前日比計算に使用する日数（取得は days+10 日分）
SHORT_TERM_DAYS: int = 5

# 短期ランキングに表示する上位・下位件数
SHORT_TERM_TOP_N: int = 5

# ── レポート保存先 ──────────────────────────────────────────
# 長期: insights/macro/monthly_trend_YYYY-MM.md
# 短期: insights/macro/daily_movers_YYYY-MM-DD.md
# → MACRO_DIR を共用（BLOCK 1 で定義済み）

# ==============================================================
# BLOCK 10: 政策金利トラッカー設定（3-C 追加）
# ==============================================================

# 政策金利トラッカーを有効化するフラグ
ENABLE_POLICY_TRACKER: bool = True

# ── FRED API（任意・無料）──────────────────────────────────
# 設定すると FFレート(FEDFUNDS)・IOER 実績値も取得できる。
# APIキー取得: https://fred.stlouisfed.org/docs/api/api_key.html
# 未設定（空文字）の場合は yfinance 市場プロキシのみ使用する。
POLICY_FRED_API_KEY: str = ""    # ← 任意。取得後ここに貼り付ける

# 変化として検知する閾値（単位: %pt）
# 0.05 = 0.05%ポイント以上の変動を変化として記録
POLICY_CHANGE_THRESHOLD: float = 0.05

# ── 追跡対象（変更不要）───────────────────────────────────
# yfinance 取得対象:
#   frb_3m  : ^IRX     （米3ヶ月T-bill ≈ FFレートプロキシ）
#   us10y   : ^TNX     （米10年債利回り）
#   jp10y   : ^JP10YT=RR（日本10年債利回り）
#   usdjpy  : JPY=X    （USD/JPY）
# FRED 取得対象（APIキー設定時のみ）:
#   fedfunds: FEDFUNDS  （FFレート実績値）
#   ioer    : IORB      （超過準備金利率）

# ── レポート保存先 ──────────────────────────────────────────
# insights/macro/policy_changes_YYYY-MM.md（月次ファイルに追記）
# DATA_DIR/policy_cache.json  （前回値キャッシュ）
# DATA_DIR/policy_events.json （累積イベントログ）

# ==============================================================
# BLOCK 11: アイデアジェネレーター設定（4-A 追加）
# ==============================================================

# アイデアジェネレーターを有効化するフラグ
ENABLE_IDEA_GENERATOR: bool = True

# 生成するアイデアの上位件数
IDEA_TOP_N: int = 5

# キーワードマッチの最低ヒット数（1 = 1単語でもヒットすれば対象）
IDEA_KW_THRESHOLD: int = 1

# アイデア保存先: INSIGHT_DIR/ideas/idea_YYYY-MM-DD.json / .md
# → INSIGHT_DIR を共用（BLOCK 1 で定義済み）

# ==============================================================
# BLOCK 12: アイデアエバリュエーター設定（4-B 追加）
# ==============================================================

# アイデアエバリュエーターを有効化するフラグ
ENABLE_IDEA_EVALUATOR: bool = True

# 週次集計を曜日に関わらず強制実行するフラグ（通常は月曜日のみ）
EVAL_FORCE_WEEKLY: bool = False

# 評価データ保存先: INSIGHT_DIR/ideas/evaluations.json
# 週次レポート: INSIGHT_DIR/ideas/weekly_summary_YYYY-WXX.md
# → INSIGHT_DIR を共用（BLOCK 1 で定義済み）

# ==============================================================
# BLOCK 13: 自律サイクル設定（4-C 追加）
# ==============================================================

# ── デイリーサイクル（毎朝自動実行）──────────────────────────
# 実行対象モジュールキー（ENABLE_* フラグが True の場合のみ実際に動く）
DAILY_MODULES = [
    "gov",           # 政府データパイプライン
    "macro",         # マクロ指標取得
    "sector",        # セクターエージェント
    "timeline",      # 月次トレンド＋デイリー騰落
    "policy",        # 政策金利トラッカー
]

# ── ウィークリーサイクル（月曜朝に追加実行）───────────────────
WEEKLY_MODULES = [
    "arxiv",         # ArXiv 論文取得
    "correlation",   # 米国債×業種 相関監査
    "idea",          # アイデア生成
    "evaluation",    # アイデア評価・週次集計
]

# ウィークリー実行の曜日（0=月曜 〜 6=日曜）
WEEKLY_RUN_WEEKDAY: int = 0

# ==============================================================
# BLOCK 14: アラート通知設定（5-B 追加）
# ==============================================================

# アラート通知を有効化するマスタフラグ
ENABLE_ALERT: bool = True

# ── LINE Messaging API ─────────────────────────────────────────
# LINE Notify は 2025年3月終了。Messaging API を使用する。
# 取得方法:
#   1. https://developers.line.biz/console/ でチャネル作成（Messaging API）
#   2. チャネル設定 > チャネルアクセストークン（長期）を発行
#   3. ボットをLINEで友達追加 → プロフィールで「あなたのユーザーID」を確認
# 空文字の場合は LINE 通知をスキップ（エラーなし）
ALERT_LINE_TOKEN:   str = ""   # ← チャネルアクセストークン（長期）
ALERT_LINE_USER_ID: str = ""   # ← 通知先ユーザーID（Uから始まる文字列）

# ── Gmail ──────────────────────────────────────────────────────
# App Password の取得: Googleアカウント > セキュリティ > 2段階認証 > アプリパスワード
# 空文字の場合は Gmail 通知をスキップ
ALERT_GMAIL_FROM:     str = ""   # 送信元 Gmail アドレス（例: your@gmail.com）
ALERT_GMAIL_PASSWORD: str = ""   # App Password（16桁、スペースなし）
ALERT_GMAIL_TO:       str = ""   # 送信先アドレス（同じアドレスでも可）

# ── 通知トリガー条件（個別 ON/OFF）────────────────────────────
# ① 政策金利変化（ENABLE_POLICY_TRACKER=True 時に有効）
ALERT_ON_POLICY_CHANGE: bool = True

# ② ゴールデン/デッドクロス（ENABLE_TIMELINE=True 時に有効）
ALERT_ON_GOLDEN_CROSS: bool = True

# ③ 高スコアアイデア（ENABLE_IDEA_GENERATOR=True 時に有効）
ALERT_ON_HIGH_SCORE_IDEA: bool = True
ALERT_MIN_IDEA_SCORE: float = 70.0   # この値以上のアイデアが生成された時に通知

# ④ 強いマクロ逆風（ENABLE_MACRO=True 時に有効）
ALERT_ON_MACRO_HEADWIND: bool = True
ALERT_HEADWIND_THRESHOLD: int = 2    # この件数以上の headwind シグナルで通知
