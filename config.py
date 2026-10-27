# ==============================================================
# config.py — 自律型投資AIエージェント基盤の設定（SSOT）
# ==============================================================
# 【役割】全モジュールがこのファイルだけを参照する（設定の一元管理）。
#
# 【重要ルール】このファイル自身は他のモジュールを一切 import しない（循環参照を構造的に防ぐ）。
#
# 【変更方法】業種ルール・API URL・閾値の変更は「このファイルのみ」編集する。
#
# 【参照方法（各モジュールで）】
#   import config as cfg
#   cfg.DATA_DIR / cfg.MACRO_RULES など
# ==============================================================

import os
from datetime import timedelta, timezone
from pathlib import Path

# ==============================================================
# BLOCK 0-tz: タイムゾーン（SSOT）
# ==============================================================
# 本システムの「今日」は一貫して日本時間で判定する（週次実行の曜日判定・
# 日付スタンプ・営業日判定がすべて JST 前提）。
# datetime.now()（tz-naive）は workflow の TZ: Asia/Tokyo に依存するので、
# 曜日・日付に依存する箇所はこの定数を明示的に渡すこと。
JST = timezone(timedelta(hours=9))

# ==============================================================
# BLOCK 0: ディレクトリパス定数
# ==============================================================

# ベースパス（SSOT）。
#   GitHub Actions / ローカル実行では環境変数 STOCK_ANALYZER_BASE で上書きする
#   （run_headless.py が ${{ github.workspace }}/stock_data を渡す）。
#   未設定の場合は、Google Driveが実在するColabだけDriveのパスへフォールバックし、
#   GitHub Actions・ローカルCLIではリポジトリ直下 stock_data を使用する。
#   ここで DRIVE_BASE を確定させるので、CACHE_DIR / SUBTHEME_JSON_PATH 等の
#   DRIVE_BASE 由来パスはすべて同じ場所を指す（個別に上書きすると取りこぼして
#   Colab の /content に書こうとし、Permission denied になる）。
_BASE_ENV    = os.getenv("STOCK_ANALYZER_BASE", "").strip()
_COLAB_DRIVE = Path("/content/drive/MyDrive")
if _BASE_ENV:
    DRIVE_BASE = Path(_BASE_ENV).expanduser()
elif _COLAB_DRIVE.is_dir():
    DRIVE_BASE = _COLAB_DRIVE / "stock_analyzer"
else:
    DRIVE_BASE = Path(__file__).resolve().parent / "stock_data"
VIEWER_BASE  = Path("/content/drive/MyDrive/stock_viewer")

DATA_DIR     = DRIVE_BASE / "data"
RESULT_DIR   = DRIVE_BASE / "results"
INSIGHT_DIR  = DRIVE_BASE / "insights"
ARXIV_DIR    = INSIGHT_DIR / "arxiv"
SECTOR_DIR   = INSIGHT_DIR / "sectors"       # Step 2 以降で使用
MACRO_DIR    = INSIGHT_DIR / "macro"         # Step 2 以降で使用
LOG_DIR      = DRIVE_BASE / "logs"
CACHE_DIR    = DRIVE_BASE / "cache"          # キャッシュ置き場

# setup_dirs() で一括作成するフォルダ一覧
ALL_DIRS = [
    DATA_DIR, RESULT_DIR, INSIGHT_DIR,
    ARXIV_DIR, SECTOR_DIR, MACRO_DIR,
    LOG_DIR,
    INSIGHT_DIR / "ideas",
    INSIGHT_DIR / "edinet_rotation",
    INSIGHT_DIR / "screener",
    CACHE_DIR,
    INSIGHT_DIR / "politician",
    INSIGHT_DIR / "correlation",
    INSIGHT_DIR / "rotation",
]

# ==============================================================
# BLOCK 0.5: サブテーマ JSON パス
# ==============================================================

SUBTHEME_JSON_PATH_JP = DRIVE_BASE / "data" / "jp_sector_subtheme.json"
SUBTHEME_JSON_PATH_US = DRIVE_BASE / "data" / "us_sector_subtheme.json"

# ==============================================================
# BLOCK 1: API / 実行設定
# ==============================================================

SPREADSHEET_ID   = "1CePiavYj_6OR0cOz84V4RkA0c0S6IwZlcHCv8_bsOjc"
CREDENTIALS_PATH = str(VIEWER_BASE / "service_account.json")
WRITE_SHEET      = True   # True にすると Google Sheets に書き込む

RUN_SCHEDULE_HOUR   = 9    # 毎朝何時に自動実行するか（0〜23）※GitHub Actions は 09:00 JST 実行に統一
RUN_SCHEDULE_MINUTE = 0

# モジュール有効フラグ（False にすると当該モジュールをスキップ）
ENABLE_GOV_PIPELINE = True
ENABLE_ARXIV        = True
ENABLE_SECTOR       = True  # 33業種: base_agent.run_sector_analysis() で一括処理
ENABLE_MACRO        = True  # Step 2 以降で True に変更

LOG_MAX_ENTRIES = 500
LOG_FILENAME    = "run_history.json"

# ==============================================================
# BLOCK 2: ArXiv 設定
# ==============================================================
# primary は arXiv API（export.arxiv.org/api/query）。日付に依存せず最新N件を返す。
#   RSS（export.arxiv.org/rss・rss.arxiv.org）は「当日分のみの日次ダイジェスト」で、
#   q-fin.PM 等の低頻度カテゴリでは0件が正常応答になるため fallback に置く。
# 取得は requests で生XMLを取り feedparser に渡す（feedparser 直接呼び出しは SSL/UA 問題で0件になることがある）。

ARXIV_FEEDS = {
    "q-fin.PM": {
        "primary":  "https://export.arxiv.org/api/query?search_query=cat:q-fin.PM&sortBy=submittedDate&sortOrder=descending&max_results=50",
        "fallback": "https://export.arxiv.org/rss/q-fin.PM",
        "label":    "ポートフォリオ管理",
    },
    "q-fin.TR": {
        "primary":  "https://export.arxiv.org/api/query?search_query=cat:q-fin.TR&sortBy=submittedDate&sortOrder=descending&max_results=50",
        "fallback": "https://export.arxiv.org/rss/q-fin.TR",
        "label":    "取引・市場微細構造",
    },
    "q-fin.ST": {
        "primary":  "https://export.arxiv.org/api/query?search_query=cat:q-fin.ST&sortBy=submittedDate&sortOrder=descending&max_results=50",
        "fallback": "https://export.arxiv.org/rss/q-fin.ST",
        "label":    "統計ファイナンス",
    },
    "q-fin.RM": {
        "primary":  "https://export.arxiv.org/api/query?search_query=cat:q-fin.RM&sortBy=submittedDate&sortOrder=descending&max_results=50",
        "fallback": "https://export.arxiv.org/rss/q-fin.RM",
        "label":    "リスク管理",
    },
    "econ.EM": {
        "primary":  "https://export.arxiv.org/api/query?search_query=cat:econ.EM&sortBy=submittedDate&sortOrder=descending&max_results=50",
        "fallback": "https://export.arxiv.org/rss/econ.EM",
        "label":    "計量経済学",
    },
}

ARXIV_MAX_PER_FEED         = 30    # 1フィードあたり最大取得件数（v1.0: 20）
ARXIV_RETRY_COUNT          = 3     # 1 URL あたりのリトライ回数（v1.0: なし）
ARXIV_RETRY_WAIT_SEC       = 5     # リトライ間隔秒（指数バックオフ: 5s, 10s）
ARXIV_REQUEST_TIMEOUT      = 20    # HTTP タイムアウト（秒）
ARXIV_INVESTMENT_THRESHOLD = 3     # このスコア以上を「投資アイデア候補」とする
ARXIV_TRANSLATE_TITLES = True      # DeepL 無料キー（:fx）があればタイトルを日本語に訳して analytics.arxiv_papers に残す

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
# (keyword, タイトル点数, サマリー点数) のタプルリスト。
#   ・タイトルヒット時とサマリーヒット時で点数を区別する
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
# JAPAN_SECTORS のキー

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
# BLOCK R-1: 横断産業シグナルルール（CROSS_SECTOR_RULES）
# ==============================================================
# 【設計思想】
#   MACRO_RULES が「金利・為替等の外部マクロ→業種」の連鎖を定義するのに対し、
#   CROSS_SECTOR_RULES は「業種A の変化→業種B への波及」を定義する。
#   gov_data_pipeline / macro_snapshot の指標値を条件として評価する。
#
# 【構造】
#   {
#     "rule_id": {
#       "label":     str,          # 表示名
#       "trigger": {               # 発動条件
#         "source":  str,          # "gov" / "macro" / "bond_commodity"
#         "key":     str,          # results["source"]["key"] で取得する値
#         "op":      str,          # ">" / ">=" / "<" / "<=" / "up3m"（3ヶ月連続上昇）
#         "val":     float,        # 閾値
#       },
#       "lag_weeks": int,          # 先行週数（シグナルの遅延を記録用に保持）
#       "signal": {
#         "tailwind": [sector_code, ...],
#         "headwind": [sector_code, ...],
#       },
#       "rationale": {sector_code: str},
#     }
#   }
#
# 【評価タイミング】
#   run_all_tasks() の macro 完了直後・sector 分析直前に
#   evaluate_cross_sector_signals() で評価し、
#   results["cross_sector_signals"] に格納する。

CROSS_SECTOR_RULES = {

    # ── 工作機械受注↑ → 電気機器・精密機器・機械（3〜6ヶ月先行）────
    "machine_tool_orders_up": {
        "label": "工作機械受注↑（設備投資先行シグナル）",
        "trigger": {
            "source": "gov",
            "key":    "machine_tool_orders_yoy",  # gov_data_pipeline が格納するキー
            "op":     ">=",
            "val":    5.0,   # 前年比+5%以上
        },
        "lag_weeks": 12,   # 3ヶ月先行
        "signal": {
            "tailwind": ["1016", "1018", "1015"],  # 電気機器・精密機器・機械
            "headwind": [],
        },
        "rationale": {
            "1016": "設備投資増 → 半導体・電子部品製造装置の受注増加（3〜6ヶ月先行）",
            "1018": "工作機械受注は精密機器需要の3ヶ月先行指標",
            "1015": "機械全般の設備投資需要拡大",
        },
    },

    # ── 工作機械受注↓ → 電気機器・精密機器に逆風 ────────────────
    "machine_tool_orders_down": {
        "label": "工作機械受注↓（設備投資減速シグナル）",
        "trigger": {
            "source": "gov",
            "key":    "machine_tool_orders_yoy",
            "op":     "<=",
            "val":    -10.0,  # 前年比-10%以下
        },
        "lag_weeks": 12,
        "signal": {
            "tailwind": [],
            "headwind": ["1016", "1018", "1015"],
        },
        "rationale": {
            "1016": "設備投資減速 → 製造装置受注の先行減少",
            "1018": "精密機器需要の冷え込みを示唆",
        },
    },

    # ── 建設着工件数↑ → 非鉄・ガラス土石・金属製品（2ヶ月先行）───
    "construction_starts_up": {
        "label": "建設着工件数↑（建設資材需要先行）",
        "trigger": {
            "source": "gov",
            "key":    "construction_starts_yoy",  # gov_data_pipeline が格納するキー
            "op":     ">=",
            "val":    3.0,
        },
        "lag_weeks": 8,   # 2ヶ月先行
        "signal": {
            "tailwind": ["1013", "1011", "1014", "1003"],
            "headwind": [],
        },
        "rationale": {
            "1013": "建設着工↑ → 銅・アルミ等の非鉄金属需要増（2ヶ月先行）",
            "1011": "ガラス・セメント等の建設資材需要増",
            "1014": "金属製品（鋼管・建設金物）需要増",
            "1003": "建設会社の受注増加に直結",
        },
    },

    # ── 海運運賃（BDI代理）↑ → 鉄鋼・非鉄・化学（2週先行）────────
    "shipping_rates_up": {
        "label": "海運運賃↑（資源輸送需要増・2週先行）",
        "trigger": {
            "source": "macro",
            "key":    "bdi",    # macro_snapshot に BDI を追加した場合
            "op":     ">=",
            "val":    2000,     # BDI 2000ポイント以上（活況水準）
        },
        "lag_weeks": 2,
        "signal": {
            "tailwind": ["1012", "1013", "1007", "1022"],
            "headwind": [],
        },
        "rationale": {
            "1012": "鉄鉱石・石炭輸送量増 → 鉄鋼の需給改善を示唆",
            "1013": "非鉄金属の輸送量増 → 需要拡大の先行シグナル",
            "1007": "化学原料輸送増 → 化学製品需要拡大",
            "1022": "運賃上昇 → 海運会社の収益改善に直結",
        },
    },

    # ── 米国ISM製造業↑ → JP輸出業種（即時〜2週）─────────────────
    "us_ism_manufacturing_up": {
        "label": "米国ISM製造業↑（JP輸出業種の受注先行）",
        "trigger": {
            "source": "macro",
            "key":    "us_ism",   # macro_snapshot に ISM を追加した場合
            "op":     ">=",
            "val":    52.0,       # 50超=拡大、52以上=明確な拡大
        },
        "lag_weeks": 1,
        "signal": {
            "tailwind": ["1017", "1016", "1015", "1018"],
            "headwind": [],
        },
        "rationale": {
            "1017": "米国製造業拡大 → 自動車・部品の輸出受注増（即時）",
            "1016": "電気機器の輸出受注増加",
            "1015": "産業機械の輸出受注増加",
            "1018": "精密機器・計測器の輸出増加",
        },
    },

    # ── 米国ISM製造業↓ → JP輸出業種に逆風 ──────────────────────
    "us_ism_manufacturing_down": {
        "label": "米国ISM製造業↓（JP輸出業種の受注減速警戒）",
        "trigger": {
            "source": "macro",
            "key":    "us_ism",
            "op":     "<=",
            "val":    48.0,       # 48以下=明確な収縮
        },
        "lag_weeks": 1,
        "signal": {
            "tailwind": [],
            "headwind": ["1017", "1016", "1015"],
        },
        "rationale": {
            "1017": "米国製造業収縮 → 自動車輸出受注の先行減少",
            "1016": "電気機器の輸出受注減速懸念",
        },
    },

    # ── 銀行貸出態度↑ → 不動産・建設（1〜2ヶ月先行）────────────
    "bank_lending_attitude_up": {
        "label": "銀行貸出態度改善（不動産・建設の資金調達緩和）",
        "trigger": {
            "source": "gov",
            "key":    "bank_lending_attitude",  # 日銀短観の貸出態度DI
            "op":     ">=",
            "val":    5.0,   # DI +5以上（明確な緩和姿勢）
        },
        "lag_weeks": 6,
        "signal": {
            "tailwind": ["1032", "1003", "1021"],
            "headwind": [],
        },
        "rationale": {
            "1032": "融資緩和 → 不動産取得・開発資金の調達コスト低下",
            "1003": "建設資金の調達容易化 → 新規着工増加",
            "1021": "設備投資融資緩和 → 輸送インフラ整備加速",
        },
    },

    # ── 消費者信頼感↑ → 小売・食料品・サービス（1〜2ヶ月先行）───
    "consumer_confidence_up": {
        "label": "消費者信頼感↑（内需消費先行シグナル）",
        "trigger": {
            "source": "gov",
            "key":    "consumer_confidence",   # 消費者態度指数
            "op":     ">=",
            "val":    40.0,   # 40超 = 改善傾向（日本は低水準が続くため閾値低め）
        },
        "lag_weeks": 4,
        "signal": {
            "tailwind": ["1027", "1004", "1033"],
            "headwind": [],
        },
        "rationale": {
            "1027": "消費意欲改善 → 小売売上高の増加（1〜2ヶ月先行）",
            "1004": "食料品への消費支出増加",
            "1033": "サービス消費（外食・旅行等）の拡大",
        },
    },

    # ── 半導体在庫指数↓（在庫調整完了） → 電気機器・精密機器────
    "semiconductor_inventory_low": {
        "label": "半導体在庫調整完了（電気機器・精密機器の回復先行）",
        "trigger": {
            "source": "gov",
            "key":    "semiconductor_inventory_yoy",  # 鉱工業生産在庫指数
            "op":     "<=",
            "val":    -5.0,   # 前年比-5%以下 = 在庫削減完了に近い
        },
        "lag_weeks": 12,   # 3ヶ月先行
        "signal": {
            "tailwind": ["1016", "1018"],
            "headwind": [],
        },
        "rationale": {
            "1016": "半導体在庫調整の完了 → 新規受注増加の先行シグナル（3ヶ月先行）",
            "1018": "精密機器（製造装置等）の需要回復を示唆",
        },
    },
}

# ==============================================================
# BLOCK R-2: 先行指標ライブラリ（LEADING_INDICATOR_RULES）
# ==============================================================
# 【設計思想】
#   政府統計・経済指標Aが、数週〜数ヶ月後に業種Bの業績に先行する
#   統計的に確認された「先行指標ペア」を辞書で管理する。
#   CROSS_SECTOR_RULES の trigger.op と同形式で評価可能。
#
# 【評価方法】
#   evaluate_cross_sector_signals() が CROSS_SECTOR_RULES と同様に
#   evaluate_leading_indicators() を呼び出して評価する。
#   発動シグナルは results["leading_indicator_signals"] に格納。
#
# 【lag_weeks の意味】
#   このシグナルが発動してから lag_weeks 後に対象業種に影響が出る。
#   有効期限管理（問題A）もこれを使って自動失効させる。

LEADING_INDICATOR_RULES = {

    # ── 工作機械受注 → 設備投資（3〜6ヶ月先行）────────────────
    "machine_orders_lead_capex_up": {
        "label": "工作機械受注↑ → 設備投資拡大（3〜6ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "machine_tool_orders_yoy",
            "op":     ">=",
            "val":    5.0,
        },
        "lag_weeks": 14,
        "signal": {"tailwind": ["1016", "1018", "1015"], "headwind": []},
        "rationale": {
            "1016": "設備投資拡大 → 製造装置・半導体設備の受注が3〜6ヶ月後に増加",
            "1018": "精密計測機器・FA機器の需要先行指標",
            "1015": "汎用機械の受注増加が3〜4ヶ月後に顕在化",
        },
    },

    # ── 建設着工件数 → 資材需要（2ヶ月先行）────────────────────
    "construction_leads_materials_up": {
        "label": "建設着工件数↑ → 建設資材需要（2ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "construction_starts_yoy",
            "op":     ">=",
            "val":    3.0,
        },
        "lag_weeks": 8,
        "signal": {"tailwind": ["1011", "1013", "1014", "1003"], "headwind": []},
        "rationale": {
            "1011": "ガラス・セメント・建設骨材の需要が着工2ヶ月後に増加",
            "1013": "配管・電線用銅の需要が先行して増加",
            "1014": "建設金物・鋼管の受注が1〜2ヶ月後に顕在化",
            "1003": "着工増 → 施工量増加 → 建設会社の売上先行シグナル",
        },
    },

    # ── 消費者態度指数 → 小売売上（1〜2ヶ月先行）─────────────
    "consumer_confidence_leads_retail_up": {
        "label": "消費者態度指数↑ → 小売・食料品消費（1〜2ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "consumer_confidence",
            "op":     ">=",
            "val":    40.0,
        },
        "lag_weeks": 6,
        "signal": {"tailwind": ["1027", "1004", "1033"], "headwind": []},
        "rationale": {
            "1027": "消費意欲改善 → 1〜2ヶ月後の小売売上増加に先行",
            "1004": "食料品消費は態度指数と1ヶ月程度の相関が確認されている",
            "1033": "外食・レジャー等のサービス消費が最も感応度が高い",
        },
    },

    # ── 消費者態度指数↓ → 小売・サービスに逆風 ─────────────
    "consumer_confidence_leads_retail_down": {
        "label": "消費者態度指数↓ → 小売・サービス消費減速（1〜2ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "consumer_confidence",
            "op":     "<=",
            "val":    35.0,
        },
        "lag_weeks": 6,
        "signal": {"tailwind": [], "headwind": ["1027", "1033"]},
        "rationale": {
            "1027": "消費意欲の悪化が小売売上に1〜2ヶ月後に波及",
            "1033": "サービス消費が最も早く反応する",
        },
    },

    # ── 機械受注 → 鉱工業生産（2〜3ヶ月先行）──────────────────
    "machinery_orders_lead_production_up": {
        "label": "機械受注↑ → 鉱工業生産拡大（2〜3ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "machinery_orders_yoy",
            "op":     ">=",
            "val":    5.0,
        },
        "lag_weeks": 10,
        "signal": {"tailwind": ["1015", "1016", "1012"], "headwind": []},
        "rationale": {
            "1015": "設備受注増 → 2〜3ヶ月後の生産設備稼働率上昇",
            "1016": "電気機器の生産量増加が受注から10週後に顕在化",
            "1012": "鋼材需要の先行シグナル（設備製造に鋼材使用）",
        },
    },

    # ── 有効求人倍率上昇 → 小売・サービス（1〜2ヶ月先行）──────
    "job_ratio_leads_consumption_up": {
        "label": "有効求人倍率↑ → 消費・小売（1〜2ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "job_offers_ratio",   # 有効求人倍率
            "op":     ">=",
            "val":    1.3,
        },
        "lag_weeks": 6,
        "signal": {"tailwind": ["1027", "1004", "1033"], "headwind": []},
        "rationale": {
            "1027": "雇用改善 → 可処分所得増 → 消費拡大が1〜2ヶ月後に現れる",
            "1004": "食料品消費の下支え要因として機能",
            "1033": "人材採用増加そのものがサービス業の需要拡大と連動",
        },
    },

    # ── 鉱工業生産↑ → 非鉄・素材（即時〜1ヶ月先行）─────────
    "industrial_production_leads_materials_up": {
        "label": "鉱工業生産↑ → 非鉄・素材需要（即時〜1ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "industrial_production_yoy",
            "op":     ">=",
            "val":    3.0,
        },
        "lag_weeks": 4,
        "signal": {"tailwind": ["1013", "1007", "1012"], "headwind": []},
        "rationale": {
            "1013": "工業生産増 → 工業用非鉄金属（銅・アルミ）の即時需要増",
            "1007": "化学原料需要が生産量に連動して増加",
            "1012": "鉄鋼の需要は工業生産と高い相関を持つ",
        },
    }, 
}

# ==============================================================
# BLOCK B-2: 政府統計先行指標ルール（GOV_RULES）
# ==============================================================
# 【設計思想】
#   MACRO_RULES が金融マクロ（金利・為替・VIX）→業種の連鎖を定義するのに対し、
#   GOV_RULES は gov_data_pipeline（e-Stat）が取得する実物経済指標
#   （CPI/鉱工業生産/機械受注/建設着工/賃金等）→業種の先行連鎖を定義する。
#   LEADING_INDICATOR_RULES と重複する部分は GOV_RULES に集約して一元管理する。
#
# 【評価タイミング】
#   gov_data_pipeline 実行後に evaluate_gov_rules(results, cfg) を呼び出す。
#   LEADING_INDICATOR_RULES と同じ形式で出力し、
#   results["leading_indicator_signals"] に追加する。
#
# 【trigger.key の対応】
#   gov_data_pipeline が results["gov_data"]["stats"][id]["yoy"] 等に格納する値を参照。
#   source: "gov" = results["gov_data"] 以下
#
# 【補正点数の根拠】
#   MACRO_RULES（±10pt）より小さめの ±3〜5pt を基本とする。
#   実物指標は発表ラグがあるため、即時性を割り引いた設定。
# ==============================================================

GOV_RULES = {

    # ── CPI 前年比↑（高インフレ）→ 内需・消費業種に逆風 ─────────
    "cpi_high_headwind": {
        "label": "CPI高騰 → 内需消費業種に逆風（購買力低下）",
        "trigger": {
            "source": "gov",
            "key":    "cpi_yoy",
            "op":     ">=",
            "val":    3.0,        # 前年比3%以上 = 高インフレ
        },
        "lag_weeks":  2,
        "pts_adj":   -4.0,
        "signal": {
            "tailwind": ["1009"],                           # 石油（インフレ受益）
            "headwind": ["1027", "1033", "1004", "1019"],  # 小売・サービス・食料品・その他製品
        },
        "rationale": {
            "1027": "食品・生活費上昇で消費者の可処分所得が圧縮 → 小売売上が減少",
            "1033": "外食・娯楽支出が削減されやすい",
            "1004": "原材料コスト上昇が利益を圧迫",
            "1009": "資源価格上昇が石油・石炭企業の収益を押し上げる",
        },
    },

    # ── CPI 前年比↓（インフレ鎮静）→ 金融緩和期待 ───────────────
    "cpi_easing_tailwind": {
        "label": "CPI鎮静 → 金融緩和期待・内需回復",
        "trigger": {
            "source": "gov",
            "key":    "cpi_yoy",
            "op":     "<=",
            "val":    1.5,        # 前年比1.5%以下 = 低インフレ
        },
        "lag_weeks":  4,
        "pts_adj":    3.0,
        "signal": {
            "tailwind": ["1028", "1032", "1027", "1033"],  # 銀行・不動産・小売・サービス
            "headwind": [],
        },
        "rationale": {
            "1028": "利下げ期待 → 銀行の貸出需要拡大・債券含み益",
            "1032": "住宅ローン金利低下期待 → 不動産需要増",
            "1027": "実質購買力回復 → 消費支出増",
        },
    },

    # ── 鉱工業生産↑ → 機械・電気機器（2〜4週先行）─────────────
    "indpro_up_machinery": {
        "label": "鉱工業生産↑ → 機械・電気機器需要（2〜4週先行）",
        "trigger": {
            "source": "gov",
            "key":    "industrial_production_yoy",
            "op":     ">=",
            "val":    2.0,        # 前年比+2%以上
        },
        "lag_weeks":  3,
        "pts_adj":    4.0,
        "signal": {
            "tailwind": ["1015", "1016", "1018", "1012"],  # 機械・電気機器・精密機器・鉄鋼
            "headwind": [],
        },
        "rationale": {
            "1015": "工場稼働率上昇 → 産業機械・ロボットの受注増",
            "1016": "半導体・電子部品の生産増加が先行",
            "1018": "精密機器（FA・検査装置）の需要が製造業拡大に連動",
            "1012": "鉄鋼需要が工業生産と2〜3ヶ月のラグで連動",
        },
    },

    # ── 鉱工業生産↓ → 製造業全般に逆風 ─────────────────────────
    "indpro_down_headwind": {
        "label": "鉱工業生産↓ → 製造業全般に逆風",
        "trigger": {
            "source": "gov",
            "key":    "industrial_production_yoy",
            "op":     "<=",
            "val":    -2.0,
        },
        "lag_weeks":  3,
        "pts_adj":   -4.0,
        "signal": {
            "tailwind": [],
            "headwind": ["1015", "1016", "1012", "1007"],
        },
        "rationale": {
            "1015": "設備投資削減 → 機械受注の先行減少",
            "1016": "電子部品需要の急落（在庫調整局面）",
        },
    },

    # ── 機械受注↑ → 電気機器・精密機器（3〜6ヶ月先行）─────────
    "machinery_orders_up": {
        "label": "機械受注↑ → 電気機器・精密機器（3〜6ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "machinery_orders_yoy",
            "op":     ">=",
            "val":    5.0,
        },
        "lag_weeks":  12,    # 3ヶ月先行
        "pts_adj":    4.0,
        "signal": {
            "tailwind": ["1016", "1018", "1015"],
            "headwind": [],
        },
        "rationale": {
            "1016": "設備投資増 → 半導体・電子部品製造装置の受注増（3〜6ヶ月先行）",
            "1018": "機械受注は精密機器需要の3ヶ月先行指標",
            "1015": "機械全般の設備投資需要拡大",
        },
    },

    # ── 機械受注↓ ──────────────────────────────────────────────
    "machinery_orders_down": {
        "label": "機械受注↓ → 電気機器・精密機器（逆風）",
        "trigger": {
            "source": "gov",
            "key":    "machinery_orders_yoy",
            "op":     "<=",
            "val":    -10.0,
        },
        "lag_weeks":  12,
        "pts_adj":   -4.0,
        "signal": {
            "tailwind": [],
            "headwind": ["1016", "1018", "1015"],
        },
        "rationale": {
            "1016": "設備投資減速 → 製造装置受注の先行減少",
            "1018": "精密機器需要の冷え込みを示唆",
        },
    },

    # ── 建設着工件数↑ → 建設・不動産・鉄鋼（2〜3ヶ月先行）────
    "construction_starts_up": {
        "label": "建設着工↑ → 建設・不動産・鉄鋼（2〜3ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "construction_starts_yoy",
            "op":     ">=",
            "val":    3.0,
        },
        "lag_weeks":  8,
        "pts_adj":    3.0,
        "signal": {
            "tailwind": ["1003", "1032", "1012", "1013"],
            "headwind": [],
        },
        "rationale": {
            "1003": "建設着工増 → 建設会社の受注増に直結",
            "1032": "新規着工増 → 不動産開発会社の売上先行増",
            "1012": "鉄筋・形鋼の需要増",
            "1013": "銅管・アルミ建材の需要増",
        },
    },

    # ── 建設着工件数↓ ───────────────────────────────────────────
    "construction_starts_down": {
        "label": "建設着工↓ → 建設・不動産に逆風",
        "trigger": {
            "source": "gov",
            "key":    "construction_starts_yoy",
            "op":     "<=",
            "val":    -5.0,
        },
        "lag_weeks":  8,
        "pts_adj":   -3.0,
        "signal": {
            "tailwind": [],
            "headwind": ["1003", "1032", "1012"],
        },
        "rationale": {
            "1003": "受注パイプラインの縮小",
            "1032": "分譲・賃貸の供給過剰懸念",
        },
    },

    # ── 賃金上昇↑ → 消費関連業種に追い風（1〜3ヶ月先行）────────
    "wages_up_consumption": {
        "label": "賃金上昇 → 消費関連業種に追い風（1〜3ヶ月先行）",
        "trigger": {
            "source": "gov",
            "key":    "wages_yoy",
            "op":     ">=",
            "val":    2.0,        # 前年比+2%以上
        },
        "lag_weeks":  4,
        "pts_adj":    3.5,
        "signal": {
            "tailwind": ["1027", "1033", "1004", "1019"],
            "headwind": [],
        },
        "rationale": {
            "1027": "可処分所得増 → 小売消費増",
            "1033": "外食・娯楽・旅行への支出増",
            "1004": "食品消費の量・質の向上",
        },
    },

    # ── 輸出↑ → 輸出製造業に追い風 ─────────────────────────────
    "exports_up": {
        "label": "輸出増加 → 輸出製造業に追い風",
        "trigger": {
            "source": "gov",
            "key":    "trade_exports_yoy",
            "op":     ">=",
            "val":    5.0,
        },
        "lag_weeks":  2,
        "pts_adj":    3.0,
        "signal": {
            "tailwind": ["1017", "1015", "1016", "1007"],
            "headwind": [],
        },
        "rationale": {
            "1017": "自動車・部品の輸出増",
            "1015": "産業機械の輸出増",
            "1016": "電子部品・半導体の輸出増",
        },
    },

    # ── 輸出↓ → 輸出製造業に逆風 ─────────────────────────────────
    "exports_down": {
        "label": "輸出減少 → 輸出製造業に逆風",
        "trigger": {
            "source": "gov",
            "key":    "trade_exports_yoy",
            "op":     "<=",
            "val":    -5.0,
        },
        "lag_weeks":  2,
        "pts_adj":   -3.0,
        "signal": {
            "tailwind": [],
            "headwind": ["1017", "1015", "1016"],
        },
        "rationale": {
            "1017": "海外需要失速 → 自動車輸出台数減",
            "1016": "電子部品の世界需要減",
        },
    },

    # ── 完全失業率↓（雇用改善）→ 内需消費に追い風 ───────────────
    "unemployment_down": {
        "label": "失業率低下（雇用改善）→ 消費・サービス業に追い風",
        "trigger": {
            "source": "gov",
            "key":    "unemployment_yoy",
            "op":     "<=",
            "val":    -0.2,       # 前年比-0.2%pt以上の改善
        },
        "lag_weeks":  4,
        "pts_adj":    2.5,
        "signal": {
            "tailwind": ["1027", "1033", "1003", "1021"],
            "headwind": [],
        },
        "rationale": {
            "1027": "雇用安定 → 消費マインド改善",
            "1033": "サービス業の人手不足緩和・需要拡大",
            "1003": "建設業の人材確保改善",
        },
    },
}

# ==============================================================
# BLOCK 5: セクターエージェント インターフェース仕様
# ==============================================================
#
# base_agent.run_sector_analysis() 参照
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
# Step 2 以降: base_agent.run_sector_analysis() が全33業種を処理
# IMPLEMENTED_AGENTS は daily ループ対象の業種コードリスト
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
    # [A-6] 多通貨モニタリング追加（日次で自動取得）
    "eurjpy":  "EURJPY=X",    # EUR/JPY
    "cnyjpy":  "CNYJPY=X",    # CNY/JPY（人民元）
    "audjpy":  "AUDJPY=X",    # AUD/JPY（リスクオン/オフ先行指標）
    "gbpjpy":  "GBPJPY=X",    # GBP/JPY（英国系製薬・金融）
}

# 業種代表銘柄（逆風・追い風アイコン表示のデフォルト対象）
SECTOR_REPRESENTATIVE_TICKERS = {
    # ── 既存定義（8業種）──────────────────────────────────────
    "1028": ["8306.T", "8316.T", "8411.T"],   # 銀行業: 三菱UFJ・三井住友・みずほ
    "1032": ["8801.T", "8802.T", "8830.T"],   # 不動産業: 三井不動産・三菱地所・住友不動産
    "1017": ["7203.T", "7267.T", "7201.T"],   # 輸送用機器: トヨタ・ホンダ・日産
    "1025": ["9432.T", "9984.T", "4689.T"],   # 情報・通信: NTT・ソフトバンクG・LY
    "1016": ["6758.T", "6501.T", "6702.T"],   # 電気機器: ソニー・日立・富士通
    "1022": ["9101.T", "9104.T", "9107.T"],   # 海運業: 日本郵船・商船三井・川崎汽船
    "1009": ["5019.T", "5020.T"],             # 石油・石炭製品: 出光興産・ENEOS
    "1004": ["2802.T", "2914.T", "2501.T"],   # 食料品: 味の素・JT・サッポロHD
    # ── TSE33業種の残り（33業種を網羅）────────
    "1001": ["1332.T", "1333.T"],             # 水産・農林業: ニッスイ・マルハニチロ
    "1002": ["1605.T", "1662.T"],             # 鉱業: INPEX・石油資源開発
    "1003": ["1801.T", "1802.T", "1812.T"],   # 建設業: 大成建設・大林組・鹿島
    "1005": ["3402.T", "3401.T"],             # 繊維製品: 東レ・帝人
    "1006": ["3861.T", "3863.T"],             # パルプ・紙: 王子HD・日本製紙
    "1007": ["4063.T", "4188.T", "4452.T"],   # 化学: 信越化学・三菱ケミカル・花王
    "1008": ["4502.T", "4568.T", "4503.T"],   # 医薬品: 武田・第一三共・アステラス
    "1010": ["5108.T", "5101.T"],             # ゴム製品: ブリヂストン・横浜ゴム
    "1011": ["5201.T", "5333.T"],             # ガラス・土石: AGC・日本碍子
    "1012": ["5401.T", "5411.T"],             # 鉄鋼: 日本製鉄・JFE
    "1013": ["5713.T", "5802.T", "5714.T"],   # 非鉄金属: 住友鉱山・住友電工・DOWA
    "1014": ["5947.T", "5938.T"],             # 金属製品: リンナイ・LIXIL
    "1015": ["6301.T", "6367.T", "6273.T"],   # 機械: コマツ・ダイキン・SMC
    "1018": ["7741.T", "7733.T", "4543.T"],   # 精密機器: HOYA・オリンパス・テルモ
    "1019": ["7974.T", "7832.T", "7951.T"],   # その他製品: 任天堂・バンナム・ヤマハ
    "1020": ["9501.T", "9503.T", "9531.T"],   # 電気・ガス: 東電・関電・東京ガス
    "1021": ["9020.T", "9022.T", "9064.T"],   # 陸運業: JR東日本・JR東海・ヤマトHD
    "1023": ["9201.T", "9202.T"],             # 空運業: JAL・ANA
    "1024": ["9301.T", "9302.T"],             # 倉庫・運輸: 三菱倉庫・三井倉庫
    "1026": ["8058.T", "8031.T", "8001.T"],   # 卸売業: 三菱商事・三井物産・伊藤忠
    "1027": ["9983.T", "3382.T", "8267.T"],   # 小売業: ファストリ・セブン&アイ・イオン
    "1029": ["8604.T", "8601.T"],             # 証券: 野村HD・大和証券G
    "1030": ["8766.T", "8725.T", "8630.T"],   # 保険: 東京海上・MS&AD・SOMPO
    "1031": ["8591.T", "8593.T"],             # その他金融: オリックス・三菱HCキャピタル
    "1033": ["6098.T", "4324.T"],   # サービス: リクルート・電通（NTTデータ 9613 は上場廃止で除外）
}

# ==============================================================
# BLOCK 7: 政府データ API
# ==============================================================

GOV_VEGPRICE_API = "https://api.cultivationdata.net/yasai_kakaku?div=all&type=csv"

# ==============================================================
# BLOCK 8: EDINET 設定
# ==============================================================

# APIキーはコードに書かず環境変数から読む（GitHub Actions は Secrets→run_headless.py の ENV_MAP 経由）。
# 有報の構造化データは EdinetDB（下の EDINETDB_API_KEY）、有報PDFは EDINET の公開ダウンロード（キー不要・officer_extractor.py）。
# 金融庁の政府 EDINET API v2（EDINET_GOV_API_KEY）は IFRS 会社の投資不動産の公正価値の取り込み（edinet_gov_xbrl.py）だけが使う。
EDINETDB_API_KEY: str = os.getenv("EDINETDB_API_KEY", "")  # 民間 EdinetDB
EDINET_GOV_API_KEY: str = os.getenv("EDINET_GOV_API_KEY", "")  # 金融庁 EDINET API v2
FUDOSAN_API_KEY: str = os.getenv("FUDOSAN_API_KEY", "")
ESTAT_API_KEY: str = os.getenv("ESTAT_API_KEY", "")

# 深掘り分析対象ティッカー。deep_dive_trigger.py が実行時に上書きする（ここは既定値）。
#   edinet_rotation_manager.py が priority=0（最優先）で処理する。
EDINET_DEEP_DIVE_TICKERS: list = []

# BigQuery 接続設定（値は環境変数由来。edinet_bq_client.py は config を優先して読む）
BQ_PROJECT_ID: str       = os.getenv("BQ_PROJECT_ID", "")
BQ_CREDENTIALS_PATH: str = os.getenv("BQ_CREDENTIALS_PATH", "")
BQ_DATASET: str          = os.getenv("BQ_DATASET", "edinet")

# ── BigQuery 費用ガード ──────────
# 1クエリでスキャンできるバイト数の上限。超えるクエリは実行前に失敗し、
# 課金は 0 になる（BigQuery の maximum_bytes_billed 仕様）。
# edinet_bq_client が default_query_job_config に設定するため、
# 各所の QueryJobConfig を書き換えなくても全クエリに効く。
# 無料枠は月1TBで全テーブル合計は数十MB。既定10GBは「事故だけを止め、正常な運用は妨げない」水準。
BQ_MAX_BYTES_BILLED: int = int(
    os.getenv("BQ_MAX_BYTES_BILLED", str(10 * 1024 ** 3))
)

# 有報の検索期間（日）: 直近 60 日以内の有報を検索（60回/社の検索）
EDINET_LOOKBACK_DAYS = 60

# 抽出するリスク要因の最大件数
EDINET_MAX_RISK_ITEMS = 5

# EDINET 取得対象の上位企業数（業種あたり。多すぎると実行時間が増える）
EDINET_MAX_COMPANIES = 2

# EDINET キャッシュ有効期間（日）: この日数以内に取得済みなら再取得しない
EDINET_CACHE_DAYS = 100

# ==============================================================
# BLOCK 19: EdinetDB ローテーション設定
# ==============================================================
ENABLE_EDINET_ROTATION: bool   = False # D-①: legacy direct/XBRL rotation retired
EDINET_ROTATION_PER_DAY: int   = 33   # 1日あたりのローテーション取得社数
EDINET_ROTATION_CACHE_DAYS: int = 30  # analysis再取得間隔（日）
EDINET_DAILY_BUDGET: int        = 80  # 1日の最大APIコール数（バッファ20）
EDINET_RETRY_BUDGET: int        = 5   # リトライ用予備コール数
EDINET_WATCHLIST_TICKERS: list  = []  # 空ならPRIORITY_TICKERSを参照
EDINET_SCREENER_CANDIDATES: list = [] # 前日screener通過銘柄（自動注入される）

# 日次分析は既存の BigQuery edinet.* を正本とし、EdinetDB RESTを呼ばない。
ENABLE_EDINET: bool = False                 # legacy 業種別REST取得
ENABLE_EDINET_BQ_ANALYSIS: bool = True      # BQ既存データのread-only分析
# 日次分析でも EdinetDB を叩き、月次コール枠（EDINETDB_MONTHLY_GUARD）を使い切ったらスキップする。
# 取得ローテを回す前に BQ の取得済みを台帳へ反映するので（EDINETDB_RECONCILE_WITH_BQ）、
# 既に BQ にあるものへは API を使わない。
ENABLE_EDINETDB_INGEST: bool = os.getenv(
    "ENABLE_EDINETDB_INGEST", "1"
).strip().lower() in {"1", "true", "yes", "on"}
# 取得ローテ前に BQ の取得済みを台帳へ反映する（読み取り1クエリ・API消費なし）
EDINETDB_RECONCILE_WITH_BQ: bool = True
ENABLE_EDINET_DIRECT_FETCH: bool = False
EDINETDB_BASE_URL: str = os.getenv("EDINETDB_BASE_URL", "https://edinetdb.jp/v1")
# EdinetDB の Web サイト（edinetdb.jp / edinetdb.com）をプログラムで読むこと（スクレイピング・クローリング）は
# 利用規約で禁止（https://edinetdb.com/legal/terms・robots.txt にも明記。違反は IP 単位の遮断あり）。データは API だけで取る。
# 会社ページを読む経路（取り込み前の財務の有無の確認・edinetdb_web_backfill）はこの値が False の間は動かない。
EDINETDB_WEB_SCRAPE_ALLOWED: bool = False
# 取り込み前に EdinetDB の公開Webページで財務の有無を確認し、無ければ次の候補へ（規約上使えないので False）
EDINETDB_WEB_PRECHECK: bool = False
EDINETDB_WEB_BASE: str = "https://edinetdb.jp/company/"
EDINETDB_WEB_RECHECK_DAYS: int = 7     # 財務なしの会社を再確認するまでの日数
EDINETDB_WEB_MAX_CHECKS: int = 300  # 1日のWeb確認の上限（1秒に1回＝最大約5分）。API 枠を使い切るまで次の候補を確かめ続けるための安全上限
EDINETDB_LOOKBACK_DAYS: int = 7
# EdinetDB の上限は「1日100回・月は100×在籍日数」（edinetdb.jp/pricing）。
# 日次予算は手動確認用に少し残して使い切る。月の実残量は /usage の monthly_remaining で抑える。
# 政府 EDINET API による IFRS 会社の投資不動産の公正価値の取り込み（edinet_gov_xbrl.py --ingest）。キーが無ければ動かない
EDINET_GOV_FV_DAILY_LIMIT: int = 30       # 1日に有報 ZIP を取る社数の上限
EDINET_GOV_MIN_INTERVAL_SEC: float = 1.0  # 政府 API への連続アクセスの間隔の下限
EDINETDB_DAILY_BUDGET: int = 97  # 100回の手前で止める（手動確認用に3回残す）
EDINETDB_MONTHLY_GUARD: int = 3100
EDINETDB_COMPANY_CACHE_DAYS: int = 7   # 会社マスタ（社名・上場区分）の取り直し間隔
# 財務三表の細目（edinet.financial_items）。取得済みの会社は残り枠で /financials だけ取り直して細目を埋める（1社1回）
EDINETDB_ITEMS_BACKFILL: bool = False  # 年次はWeb補完で取るので API での細目補完はしない
# 年次財務も API（/financials）で取る（1社3回＝財務・セグメント・本文〔EDINETDB_TEXTBLOCK_EVERY_N 回に1回〕）。
# True にすると年次財務を会社ページ（edinetdb_web_backfill）に任せて API を節約するが、規約上使えない
EDINETDB_ANNUAL_FROM_WEB: bool = False
EDINETDB_QUARTERLY_DAILY_CALLS: int = 40
EDINETDB_QUARTERLY_REFRESH_DAYS: int = 30
EDINETDB_WEB_DAILY_LIMIT: int = 100      # 日次のWeb更新（新年度の有報が出た会社＋未取得）の社数上限
EDINETDB_ITEMS_MERGE_CHUNK: int = 4000
EDINETDB_REFRESH_DAYS: int = 180
# 政府 EDINET の書類一覧で直近この日数に有報を出した会社を、取り込みの先頭（P0）で取る（鮮度に関係なく・提出の翌日以降に1回）
EDINETDB_NEW_FILING_DAYS: int = 30
EDINETDB_TEXTBLOCK_EVERY_N: int = 2
EDINETDB_ROTATION_META: str = "edinetdb_rotation_meta.json"
EDINETDB_DOC_INDEX_LIMIT: int = 10
# doc_index の取得窓（日）。有報は年1回しか出ないため、7日窓では
# 構造的にほぼ常に0件になっていた（実測 inserted 0）。年次サイクルを跨ぐ。
EDINETDB_DOC_INDEX_LOOKBACK_DAYS: int = 400
EDINETDB_DOC_INDEX_RESERVE: int = 11  # /usage 1 + 開示10社
# 送信間隔の下限。連続GETが速すぎると429を誘発し、その再試行が日次予算を空にして全件失敗する。
EDINETDB_MIN_INTERVAL_SEC: float = 0.5

# D-②: P1 corporate real-estate enrichment; all missing data is neutral.
ENABLE_FUDOSAN_INGEST: bool = True
ENABLE_FUDOSANDB_INGEST: bool = ENABLE_FUDOSAN_INGEST  # 後方互換
ENABLE_REAL_ESTATE_SIGNAL: bool = True
FUDOSAN_BASE_URL: str = os.getenv(
    "FUDOSAN_BASE_URL",
    os.getenv("FUDOSANDB_BASE_URL", "https://fudosandb.jp/v1"),
)
# FUDOSAN_BACKFILL_CUTOFF 以前だけ効く取り込み枠。cutoff 後は下の POST_CUTOFF_BUDGET（契約上の上限は PLAN_DAILY_LIMIT=100回/日）が効くので、いまは使われない
FUDOSAN_DAILY_BUDGET: int = 900
FUDOSAN_BACKFILL_CUTOFF: str = "2026-07-30"
FUDOSAN_POST_CUTOFF_BUDGET: int = 90
FUDOSAN_PLAN_DAILY_LIMIT: int = 100       # 不動産DB の契約上の1日の上限（8/1以降）。メールの【API使用】の分母。自動取り込みは POST_CUTOFF_BUDGET まで
FUDOSAN_REFRESH_DAYS: int = 365
FUDOSAN_ROTATION_META: str = "fudosan_rotation_meta.json"
FUDOSAN_HOLDINGS_TABLE: str = "corporate_holdings_p1"
FUDOSAN_AREA_TABLE: str = "area_profile_p1"
# 送信間隔の下限（EdinetDBと同じく、429の再試行で予算を空にしないため）。
FUDOSAN_MIN_INTERVAL_SEC: float = 0.5
RE_BONUS_MIN: float = -2.0
RE_BONUS_MAX: float = 3.0
# 推定時価（土地面積×参照地価）の補正係数。キーは「参照地価の種別|地域」（地域 metro＝三大都市圏の都府県、other＝それ以外）。
# 値は開示時価（賃貸等不動産・IFRS 投資不動産）との比較で、時価÷簿価の対数比の中央値を exp で戻したもの。
# 10社未満の層は載せない（係数1.0）。BIマート m_re_holdings・m_re_holdings_pref の cal CTE と同じ値にする
# （tests/test_real_estate_calibration.py が一致を検査する）。
RE_EST_CALIBRATION: dict = {"industrial|metro": 1.02, "commercial|metro": 0.77}
# 有報の注記「賃貸等不動産関係」（rental_property_ingestor.py・EdinetDB real-estate）。EdinetDB の日次枠を日次取り込みと分け合う
ENABLE_ALERT_STORE: bool = True          # 日次メールの全節と銘柄を analytics.alert_sections / alert_items へ（alert_store.py）
ENABLE_RENTAL_PROPERTY_FV: bool = True    # True＝毎日 RENTAL_FV_DAILY_BUDGET 社を取る。False＝RENTAL_FV_TRIAL_LIMIT 社を1回だけ試行
RENTAL_FV_DAILY_BUDGET: int = 15          # 1日に取る社数（＝EdinetDB の呼び出し回数）。残りは日次取り込みへ
RENTAL_FV_TRIAL_LIMIT: int = 10
# 未取得なら優先して取る会社（EDINET コード）。IFRS の投資不動産の公正価値が EdinetDB real-estate で取れるかを
# 投資不動産の大きい IFRS 会社で確かめるため（NTT・住友商事・三井物産・J.フロント・IHI）
RENTAL_FV_PRIORITY_CODES: tuple = ("E04430", "E02528", "E02513", "E03516", "E02128")
FUDOSANDB_BASE_URL: str = FUDOSAN_BASE_URL  # 後方互換
FUDOSANDB_DAILY_BUDGET: int = FUDOSAN_DAILY_BUDGET  # 後方互換

# 政策DB（seisakudb.jp）。1日1000回まで（成功したリクエストだけ数える）。データは月次更新。
SEISAKUDB_API_KEY: str = os.getenv("SEISAKUDB_API_KEY", "")   # GitHub Secrets
ENABLE_SEISAKUDB: bool = True
SEISAKUDB_DAILY_BUDGET: int = 950        # 1日に使う回数（API の上限は1000・50回は手動確認用に残す）。個社（約3,800社）は約4日で1周。1回約0.6秒で約10分かかるので、日次の分析ではなく朝の取得 workflow（fetch_stages.jp_filings）で取る
SEISAKUDB_RESERVE: int = 0               # 手動確認用に残す回数
SEISAKUDB_RECENT_LIMIT: int = 500        # 直近イベントの取得件数
SEISAKUDB_TOP_LIMIT: int = 2000          # 公的支出ランキングの取得件数
SEISAKUDB_MIN_INTERVAL_SEC: float = 0.5  # 送信間隔の下限（300回で約3分。連打すると429を誘発する）
# 公的支出の特徴量（銘柄ごとの受給額・経済安保認定）。スコア加点は承認後に True にする。
ENABLE_PUBLIC_FUNDING_FEATURES: bool = True
ENABLE_PUBLIC_FUNDING_BONUS: bool = False
SEISAKU_BONUS_ECON_SECURITY: float = 1.5   # 経済安保の認定あり
SEISAKU_BONUS_GOV_DEMAND: float = 1.0      # 直近1年の公的支出が売上の一定割合
SEISAKU_BONUS_RECENT_AWARD: float = 0.5    # 直近180日に受給あり
SEISAKU_RATIO_MIN: float = 0.01
SEISAKU_RATIO_MAX: float = 0.30            # 超えると政府依存が強すぎるので加点しない
SCORE_MAX_PUBLIC_FUNDING_ADJ: float = 3.0  # 公的支出加点の上限
ALERT_ON_SEISAKUDB: bool = True           # 日次メールに政策DB節を出す
# 対象銘柄の選定は fudosan_ingestor が保有銘柄・screener候補から動的に行う。

# ==============================================================
# BLOCK 9: タイムライン設定（3-A）
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
# BLOCK 10: 政策金利トラッカー設定（3-C）
# ==============================================================

# 政策金利トラッカーを有効化するフラグ
ENABLE_POLICY_TRACKER: bool = True
# FEDカレンダー / CME FedWatch取得
ENABLE_FED_CALENDAR:   bool = True
# 米国セクター分析（S&P500 11セクター）
ENABLE_US_SECTOR: bool = True

# ── FRED API（任意・無料）──────────────────────────────────
# 設定すると FFレート(FEDFUNDS)・IOER 実績値も取得できる。
# APIキー取得: https://fred.stlouisfed.org/docs/api/api_key.html
# 未設定（空文字）の場合は yfinance 市場プロキシのみ使用する。
POLICY_FRED_API_KEY: str = os.getenv("POLICY_FRED_API_KEY", "")  # GitHub Secrets

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
# BLOCK 11: アイデアジェネレーター設定（4-A）
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
# BLOCK 12: アイデアエバリュエーター設定（4-B）
# ==============================================================

# アイデアエバリュエーターを有効化するフラグ
ENABLE_IDEA_EVALUATOR: bool = True

# 週次集計を曜日に関わらず強制実行するフラグ（通常は月曜日のみ）
EVAL_FORCE_WEEKLY: bool = False

# 評価データ保存先: INSIGHT_DIR/ideas/evaluations.json
# 週次レポート: INSIGHT_DIR/ideas/weekly_summary_YYYY-WXX.md
# → INSIGHT_DIR を共用（BLOCK 1 で定義済み）

ENABLE_SCREENER: bool = True         # スクリーニング有効化フラグ
ENABLE_RULE_OPTIMIZER:      bool = True   # ルール重み最適化 [4-8]
# ルール重み・IC・学習監査ログの月次判定。週次（月曜）の回が「月の最後の N 日」に入ったら月1回動く
RULE_OPTIMIZER_MONTH_END_WINDOW_DAYS: int = 7
ENABLE_JP_US_BRIDGE:        bool = True   # JP×US横断分析
ENABLE_SCREENER_FEEDBACK:   bool = True   # screenerフィードバックループ
ENABLE_ANOMALY_SEASON:  bool = True   # 季節アノマリー [4-4]
ENABLE_MARKET_BREADTH: bool = True   # 市場幅指標 [3-7]
ENABLE_CREDIT_DATA:      bool  = True    # 信用残高データ取得 [3-5]
CREDIT_SQUEEZE_THRESH:   float = 1.0     # 踏み上げ候補の倍率閾値
CREDIT_OVERHEAT_THRESH:  float = 10.0   # 過熱（崩落リスク）の倍率閾値
# 市場全体データでは絶対値の閾値だと7割が「過熱」になるため、その週の分位で判定する。
# 絶対値は銘柄数が少ないときの予備。
CREDIT_SQUEEZE_PCTL:  float = 10.0   # 倍率が下位この%以下 → 踏み上げ候補
CREDIT_OVERHEAT_PCTL: float = 90.0   # 倍率が上位この%以上 → 過熱
CREDIT_CAUTION_PCTL:  float = 75.0   # 倍率が上位この%以上 → 注意（加減点なし）
ENABLE_CORRELATION_HEATMAP: bool = True   # 相関ヒートマップ [3-2]
ENABLE_SCENARIO_ANALYSIS: bool = True   # シナリオ分析（フェーズ3）
ENABLE_PERFORMANCE_TRACKER: bool = True      # パフォーマンストラッカー有効化（2-3）
ENABLE_MULTI_CURRENCY:    bool = True   # 多通貨モニタリング [A-6]
ENABLE_EARNINGS_SURPRISE: bool = True   # 決算サプライズ検出 [A-7]

# IMF グローバルマクロパイプライン設定
ENABLE_IMF: bool = True
IMF_INDICATORS = [
    "NGDP_RPCH", "PCPIPCH", "LUR", "BCA_NGDPD", "GGXWDG_NGDP"
]
IMF_COUNTRIES = ["JPN", "USA", "CHN", "DEU", "EA19", "W"]

# Step E: SEC 10-K / 有報 Claude API 要約設定
ENABLE_SEC_SUMMARIZER: bool = False   # ANTHROPIC_API_KEY 設定後に True へ変更
ANTHROPIC_API_KEY: str = ""           # GitHub Secrets で設定（下記参照）
SEC_USER_AGENT: str = os.getenv("SEC_USER_AGENT", "")  # SEC EDGAR の名乗り（連絡先メール入り）。値は GitHub Secret SEC_USER_AGENT にだけ置く（公開に備えてコードに書かない）
SUMMARY_MAX_TICKERS: int = 10         # 要約する最大銘柄数（コスト管理）

# Step C': テーマETFトラッキング設定
ENABLE_ETF_TRACKER: bool = True     # テーマETFトラッキングの有効化
ARXIV_THEME_TIME_BUDGET_SEC: int = 150  # テーマ別 arXiv 論文数の問い合わせに使う上限秒数。超えたら打ち切り、7日以内の前回分を使う
# Step D: Excel ダッシュボード CSV エクスポート（使わない方針のため停止中）
ENABLE_EXCEL_EXPORT: bool = False  # Excel/CSVダッシュボード出力（使わない）
ETF_TOP_N: int = 3                  # アラートに表示するトップテーマ数
ETF_THEME_IDS = None                # None=全テーマ / 例: ["quantum","ai_infra"]

# ==============================================================
# BLOCK 14: ニュースRSSパイプライン設定（Step B）
# ==============================================================
ENABLE_NEWS: bool = True          # ニュースRSS取得の有効化
# Google Custom Search API（有料メディア記事検索）
GOOGLE_CSE_API_KEY: str = os.getenv("GOOGLE_CSE_API_KEY", "")  # GitHub Secrets
GOOGLE_CSE_ID:      str = "033b43821c3a5442b"   # cse.google.com で取得
# Google CSE API が 403 を返し続けているため停止中（quota・キー・請求設定のいずれか。キーは GitHub Secret）。
# キーを点検・復旧したら True に戻す。
ENABLE_PAID_MEDIA_SEARCH: bool = False

# Step C: 個別株スクリーニング設定
SCREEN_TOP_N_SECTORS: int   = 7      # スクリーニング対象業種数（スコア上位N）
# 対象は BQ に財務がある全社×全業種。下位業種（BOT_TIER）は評価しない。取得は並列（SCREEN_FETCH_WORKERS）
SCREEN_EXPAND_UNIVERSE: bool = True
# BQの財務は約3,800社あるため、業種ごとに直近売上の上位N社に絞る（全社だと実行時間・例外が増える）
SCREEN_EXPAND_PER_SECTOR: int = 25
SCREEN_FETCH_WORKERS: int    = 5
SCREEN_PER_MAX:       float = 30.0   # PER 上限
SCREEN_PBR_MAX:       float = 5.0    # PBR 上限
SCREEN_ROE_MIN:       float = 0.05   # ROE 下限（5% = 0.05）
SCREEN_MCAP_MIN_B:    float = 0.0   # 時価総額下限（0=足切りしない。小型株を構造的に落とさないため）
SCREEN_VOL_MIN:       int   = 50_000 # 出来高下限（5万株/日）

NEWS_TOP_N:  int  = 10            # アラートに表示する重要記事の最大件数
NEWS_FEEDS_ENABLED = None         # None=全フィード有効 / リスト指定で絞り込み
                                  # 例: ["reuters_business", "nhk_top"]
NEWS_ALERT_MAX_ARTICLES: int = 5  # Gmailアラートに含める記事数

# ==============================================================
# BLOCK 13: 自律サイクル設定（4-C）
# ==============================================================

# ── デイリーサイクル（毎朝自動実行）──────────────────────────
# 実行対象モジュールキー（ENABLE_* フラグが True の場合のみ実際に動く）
DAILY_MODULES = [
    "key_health",    # APIキー生存判定（Issue J-2・最序盤で実行）
        # 13F（機関投資家の保有）。取得は四半期の提出期間（1/4/7/10月15日〜末日）だけで、それ以外はキャッシュ参照
    "institutional_13f",
        # 日次でも EdinetDB を叩く。月次コール枠（EDINETDB_MONTHLY_GUARD）を使い切ったら
        # edinetdb_ingestor 側が自動でスキップする。取得ローテの前に BQ の取得済みを台帳へ反映するため
        # （EDINETDB_RECONCILE_WITH_BQ）、既にBQにある企業へAPIは使わない。
    "edinetdb_ingest",  # DB統合W3: EdinetDB→BQ（company/financials/segments/text）
    "edinet_doc_index", # DB統合W3: 開示書類インデックス（有報は年1回=窓400日）
    "fudosan_ingest",   # DB統合W5: 企業保有不動産→BQ
    "seisakudb",        # 政策DB: 上場企業の公的支出・経済安保認定→BQ
    "gov",           # 政府データパイプライン
    "macro",         # マクロ指標取得
    "sector",        # セクターエージェント
    "tdnet",           # TDnet 重要適時開示モニター（M-4）
    "economic_cycle",  # 経済サイクルフェーズ判定（D-1 / ダリオ4象限）
    "timeline",      # 月次トレンド＋デイリー騰落
    "policy",        # 政策金利トラッカー
    "us_sector",     # 米国S&P500 11セクター分析
    "bond_commodity",  # 債券イールドカーブ + 全コモディティ分析
    "us_macro",  # US マクロ拡張
    "anomaly",         # 統計的異常値検出（価格・出来高・スコア）
    "imf",           # IMF グローバルマクロパイプライン
    "etf_tracker",   # テーマETFトラッキング（Step C'）
    "edinetdb_extras",  # EdinetDB の追加の窓口（決算短信・政策保有株・関係会社・主要販売先・1日 EDINETDB_EXTRAS_DAILY_CALLS 回）
    "tech_map",      # 技術マップのまとめ（tech_profile.run_digest・スクリーナー通過銘柄×技術ノード×熱度 → メール）
    "news",          # ニュースRSSパイプライン（Step B）
    "screener",      # スキャナーの追加
    "excel_export",  # Excel ダッシュボード CSV エクスポート（Step D）
    "event_horizon",  # イベント事前アラート（日次）
    "options_advisor",  # オプション戦略提案（日次）
    "signal_aggregator",  # 統合シグナル [4-9]
    "inference",          # 推論エンジン [4-10]
        # "edinet_rotation",  ← 廃止（機能していなかった）
    "us_fundamentals",  # US財務DBローテーター ← screenerより先に実行
    "us_screener",   # 米国株スクリーニング（US財務DB補完後に実行）
    "us_8k",  # US 8-K 監視
    "us_sector_bridge",  # SIC/JP業種連携
    "multi_currency", # 多通貨モニタリング [A-6]
    "options_monitor", # オプション市場センチメント [4-5]
    "support_resistance",   # サポート・レジスタンス検出 [4-6]
    "volume_profile",   # 出来高プロファイル分析 [4-7]
    "notion_obsidian",   # Notion/Obsidian書き込み [6-4]（毎日実行）
    "policy_radar",  # 政策レーダー
    "policy_signal_applier",       # Policy Ph2（17.8番・日次）
    "pead_monitor",                # FE-5（screener直後・日次）
    "ml_meta",                     # S7 ML特徴量スナップショット（日次蓄積）
    "score_calculator",            # E10 JP/US共通スコア
    "deliberation_queue",          # 投資審議の DB キュー（score_calculator の後）
        # size_decay_manager / cross_size_signal は stock_analyzer_main が _in_mode("size_tier") でゲートする。
        #   ここに "size_tier" が無いと日次・週次では永久に実行されない。
    "size_tier",                   # 規模別減衰管理 + 規模帯横断シグナル
]

# ── ウィークリーサイクル（月曜朝に追加実行）───────────────────
WEEKLY_MODULES = [
    "arxiv",         # ArXiv 論文取得
    "semantic_scholar",  # 論文追加分析
    "congress_monitor",  # US法案議会進行追跡 [Ph4]（L4キャッシュ減衰で日次適用）
    "ontology_export",  # エンティティグラフBQ追記（週次）
    "correlation",   # 米国債×業種 相関監査
    "size_tier_classifier",      # Size Ph1（sector実行直後・週次）
    "business_model_classifier", # BM Ph1（size_tier_classifierの直後・週次）
        # "jp_us_sector_correlation",  ← 廃止（機能していなかった）
    "sector_rotation_detector",    # Idea2-B（signal_aggregatorの直前・必須）
    "portfolio_diversity_filter",  # FE-6（screener直後・Ignitionの前）
    "ignition_detector",           # FE-4（sector_rotation_detectorの直後）
    "catalyst_calendar",           # FE-7（NEDO/policy_radar直後）
    "credit_data",   # 信用残高週次取得 [3-5]
    "idea",          # アイデア生成
    "evaluation",    # アイデア評価・週次集計
    "sec_summary",   # SEC 10-K / 有報 Claude API 日本語3行要約（Step E）
    "biz_summary",   # Step E代替: yfinance事業概要 + DeepL翻訳
    "weekly_report", # Step F: 週次投資判断サマリー自動生成
    "market_breadth",   # 市場幅指標（マーケットブレス）[3-7]
    "stress_test",   # ストレステスト / VaR [3-6]
    "insider_monitor",   # インサイダー取引モニター [4-2]
    "dividend_calendar",   # 配当権利確定カレンダー [4-3]
    "anomaly_season",   # 季節アノマリー検出 [4-4]
    "rule_optimizer",     # マクロルール重み最適化 [4-8]
    "jp_us_bridge",       # JP×US横断相関
    "screener_feedback",  # screenerフィードバック
    "backtester",   # バックテスト [5-3]
    "self_evaluator",    # パフォーマンス自己検証 [5-5]
    "screener_heatmap",  # セクターヒートマップ
    "portfolio_optimizer",   # ポートフォリオ最適化 [6-1]
    "dcf",               # DCF理論株価 [6-3]（週次のみ / 7日キャッシュ）
    "longterm_scenario",   # 10年単位のシナリオ R-5
    "google_trends",  # Google Trends
    "finbert",  # FinBERT
    "technology_mapper",        # B: 企業×技術マッピング
    "cluster_engine",           # B: 技術クラスター生成
    "emerging_detector",        # B: 萌芽テーマ検知
    "cluster_graph_exporter",   # B: クラスター結果出力
    "factor_crowding",  # ファクター混雑度
        # "job_posting",  ← 廃止（機能していなかった）
    "executive_transition",  # I-3: 役員転職トラッカー
        # "facility_expansion",  ← 廃止（機能していなかった）
        # 政策支援（BQ edinet.policy_support）の書き手。読む側は base_agent.get_policy_support_bonus。
    "nedo",                  # I-2: NEDO採択情報 → policy_support
]

# ウィークリー実行の曜日（0=月曜 〜 6=日曜）
WEEKLY_RUN_WEEKDAY: int = 0

# ==============================================================
# BLOCK 14b: アラート通知設定
# ==============================================================

# アラート通知を有効化するマスタフラグ
ENABLE_ALERT: bool = True

# ==============================================================
# BLOCK 20: 債券・コモディティパイプライン設定
# ==============================================================

ENABLE_BOND_COMMODITY: bool = True   # True=有効 / False=スキップ

# ==============================================================
# BLOCK 21: 異常検知設定
# ==============================================================

ENABLE_ANOMALY_DETECTOR: bool = True

ALERT_ON_SCORE_JUMP:          bool  = True   # 業種スコア急変アラート
ALERT_SCORE_JUMP_THRESHOLD:   float = 15.0   # 急変判定閾値（点）

# 価格変動率閾値（絶対値）
ANOMALY_PRICE_CHANGE_HIGH:   float = 0.05   # ±5% 以上 = HIGH
ANOMALY_PRICE_CHANGE_MEDIUM: float = 0.03   # ±3% 以上 = MEDIUM

# 出来高急増倍率（過去 20 日平均比）
ANOMALY_VOLUME_SPIKE_RATIO:  float = 3.0    # 3倍以上 = 急増

# Z スコア閾値
ANOMALY_Z_HIGH:              float = 2.5    # HIGH 異常
ANOMALY_Z_MEDIUM:            float = 2.0    # MEDIUM 異常

# セクタースコア異常閾値
ANOMALY_SCORE_TOP:           float = 75.0   # この値以上 = 高スコア異常
ANOMALY_SCORE_BOTTOM:        float = 30.0   # この値以下 = 低スコア異常

# アラート通知するかどうか（HIGH/CRITICAL のみ通知）
ALERT_ON_ANOMALY:            bool  = True
ALERT_ANOMALY_MIN_SEVERITY:  str   = "high"  # "medium" | "high" | "critical"

# このファイルは GitHub リポジトリ直下に置けば自動検出される
# （GitHub Actions = ③ / Colab直接実行 = ① or ② の順で探す）
# コモディティ→composite_score の補正は signal_aggregator のカテゴリI（"yield_curve"）が担う。
#   別途直接補正すると同じ情報の二重計上になる（signal_dedup の原則に反する）。

# ==============================================================
# BLOCK 15: Step E代替 - 事業概要フェッチャー設定
# ==============================================================

# 有効化フラグ（DeepL APIキーなしでも英語原文モードで動作）
ENABLE_BIZ_SUMMARY: bool = True

# DeepL API 無料キー（末尾が :fx のもの）
# 取得: https://www.deepl.com/pro-api （無料枠: 50万字/月）
DEEPL_API_KEY: str = os.getenv("DEEPL_API_KEY", "")  # GitHub Secrets（末尾 :fx の無料キー）

# 業種あたり取得社数（2社 × 33業種 = 66社/日 → DeepL約20,000字/月）
BIZ_SUMMARY_MAX_COMPANIES: int = 2

# キャッシュ有効日数（事業概要は頻繁に変わらないため長め）
BIZ_SUMMARY_CACHE_DAYS: int = 90

# ==============================================================
# BLOCK 16: Step F - 週次投資判断レポート設定
# ==============================================================

# 週次レポート有効化（WEEKLY_MODESで "weekly_report" が必要）
ENABLE_WEEKLY_REPORT: bool = True

# LINE/Gmail 配信は既存の ENABLE_ALERT / ALERT_* 設定を流用
# （週次レポートアラートは alert_notifier.py が自動追加）

# ── v2.0 追加アラート設定 ────────────────────────────────────
# 高配当アラート（配当利回りがこの値以上の銘柄を通知）
ALERT_ON_HIGH_DIVIDEND: bool = True
ALERT_MIN_DIVIDEND_YIELD: float = 0.04   # 4%以上

# ── 日次メール表示件数・米国株サマリー ──────
# 注目業種トップ3で業種ごとに出す代表銘柄の件数。
ALERT_SECTOR_TOP_COMPANIES: int = 3  # 業種のスクリーナー上位を出す

# 米国株サマリー（us_screener / us_sectors）を日次メールに載せるか（alert_notifier.py）。
ALERT_ON_US_SUMMARY: bool = True
ALERT_US_TOP_PICKS:    int = 10  # USスクリーナー通過銘柄の表示件数
ALERT_INTL_TOP_PICKS_PER_COUNTRY: int = 3  # 米国株スクリーナーの米国以外の通過銘柄を、国ごとに何社まで「🌏 海外株」節に出すか

# アナリスト強気推奨アラート
ALERT_ON_STRONG_BUY: bool = True

# 財務リスクウォッチ（高負債・FCFマイナス・低流動比率）
ALERT_ON_FINANCIAL_RISK: bool = True

ALERT_ON_MACRO_DELTA:    bool = True   # マクロ急変アラート [GAP-7]
ALERT_ON_BUYBACK:        bool = True   # 自己株式取得アラート [M-4]
ALERT_ON_DIV_UP:         bool = True   # 増配・特別配当アラート [M-4]
ALERT_ON_FOREIGN_SPIKE:  bool = True   # 外国人持株急増アラート [P-4]
ALERT_ON_EDINETDB_INGEST: bool = True  # その日EdinetDBから取り込んだ銘柄をメールに載せる

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

# EdinetDB取得をスキップする業種コード
# （優先度低・APIコール節約のため）
EDINET_SKIP_SECTORS: list = [
     "1001",  # 水産・農林業
#    "1002",  # 鉱業
     "1006",  # パルプ・紙
#    "1010",  # ゴム製品
#    "1011",  # ガラス・土石製品
#    "1014",  # 金属製品
#    "1019",  # その他製品
     "1024",  # 倉庫・運輸関連
]


# ==============================================================
# BLOCK 17: 米国株スクリーニング設定
# ==============================================================
ENABLE_US_SCREENER: bool = True
US_SCREENER_MAX_WORKERS: int = 5
US_SCREENER_MAX_TICKERS: int = 5500
US_CUSTOM_UNIVERSE_DIR: str = "data/custom_universe"
US_SCREENER_CATEGORIES: list = [
    "sp500", "nasdaq_growth", "saas_cloud", "ai_semicon",
    "biotech", "adr", "fintech", "healthcare_svc", "reit",
    "clean_energy", "defense_cyber", "consumer_dtc",
    "infrastructure", "materials_agtech", "media_entertainment",
    "quantum", "lithium", "uranium", "water", "genomics", "robotics",
    "metaverse", "blockchain", "longevity", "supply_chain"
]
US_SCREENER_TOP_N: int = 25
# XBRL の補完を点数に使う（us_stock_screener._apply_xbrl_complement）。
# EPS の加速（eps_accel_score_xbrl・-10〜+10）× この重みを素点に足す（0.3 で ±3点）。0 で止める
US_SCREENER_EPS_ACCEL_WEIGHT: float = 0.3
# yfinance の FCF が無い銘柄だけ、XBRL の直近12か月 FCF（latest_fcf_xbrl）で埋める（足切りの FCF>0 と素点の両方に効く）
US_SCREENER_FCF_FILL_XBRL: bool = True
# インサイダー(Form 4)照会は SEC EDGAR への逐次アクセスで1社あたり約2秒。
# 通過全社に掛けるとフィルター処理だけで13分を超えるため、素点上位のみに絞る。
# 0 で照会を完全に止める（insider_bonus は全社 0.0 = 補正なし）。
US_INSIDER_MAX_TICKERS: int = 40

# ── 銘柄スナップショット ─────────────────────────────
# スクリーナー通過銘柄の日次スナップショットを BQ analytics.stock_snapshot へ蓄積する
# （RSI/MACD/MA200/VWAP/52週位置/FCFマージン/D-E比率/自己資本比率/配当利回り/
#  sector_score/technical_score を時系列で追うため）。md生成とは独立で、本フラグはBQ書き込みのみ。
ENABLE_STOCK_SNAPSHOT: bool = True

# yf_cache が実行中に集めた日足を BQ analytics.price_daily へ書き出す（事後検証用・追加取得はしない）。
ENABLE_PRICE_HISTORY_WRITER: bool = True

# 業種分析の並列度。待ち時間（BQ/yfinance/EDINET）が支配的なので並列化が効く（逐次だと33業種で約12分）。
# 後続の補正処理は逐次のまま。1で逐次実行。
SECTOR_ANALYSIS_WORKERS: int = 4

# ==============================================================
# BLOCK 22: ポジションサイズ設定 / ストレステスト / フェーズ4〜6 フラグ群
# ==============================================================
TOTAL_ASSETS_JPY:       float = 10_000_000.0  # 総資産額（円）← 実際の金額に変更
POSITION_RISK_PCT:      float = 0.01  # 1トレードのリスク許容率（1%ルール。レポートの注記と一致させる）
POSITION_ATR_MULT:      float = 2.0           # 逆指値 = 現在価格 - ATR × この倍率
ENABLE_POSITION_SIZER:  bool  = True          # ポジションサイジング [3-3]
ENABLE_STRESS_TEST:  bool  = True       # ストレステスト [3-6]
USDJPY_DEFAULT:      float = 150.0      # USD/JPY デフォルト値（snapshot取得失敗時）
# TOTAL_ASSETS_JPY は position_sizer.py で追加済みのものを共用
ENABLE_TECHNICAL_SCORER: bool = True   # テクニカルスコア [4-1]
ENABLE_INSIDER_MONITOR: bool = True   # インサイダーモニター [4-2]
ENABLE_DIVIDEND_CALENDAR: bool = True   # 配当カレンダー [4-3]
ENABLE_OPTIONS_MONITOR: bool = True   # オプション市場センチメント [4-5]
ENABLE_SUPPORT_RESISTANCE: bool = True   # SR自動検出 [4-6]
ENABLE_VOLUME_PROFILE: bool = True   # 出来高プロファイル [4-7]
ENABLE_STOCK_DETAIL_REPORT: bool = True   # 銘柄詳細レポート [5-1]
STOCK_DETAIL_KEEP_DATED: bool = False     # 銘柄mdの日付別 detail_YYYY-MM-DD/ も残すか（正本は analytics.stock_snapshot。既定は detail_latest/ だけ）
ENABLE_COMPS_ANALYZER:      bool = True   # 同業種内比較 [5-2]
ENABLE_BACKTESTER:          bool = True   # バックテスト [5-3]
ENABLE_SIGNAL_AGGREGATOR: bool = True   # 統合シグナル [4-9]
ENABLE_INFERENCE_ENGINE:  bool = True   # 推論エンジン [4-10]
ENABLE_SELF_EVALUATOR:    bool = True   # 自己検証 [5-5]
ENABLE_SCREENER_HEATMAP:  bool = True   # セクターヒートマップ
ENABLE_PORTFOLIO_OPTIMIZER: bool = True   # ポートフォリオ最適化 [6-1]
ENABLE_DCF_VALUATION:      bool = True    # DCF理論株価 [6-3]
ENABLE_NOTION_OBSIDIAN:    bool = True    # Notion/Obsidian連携 [6-4]

# Notion API設定（未設定でもObsidianのみ動作）。キーは環境変数（GitHub Secrets の NOTION_API_KEY）。
NOTION_API_KEY:            str  = os.getenv("NOTION_API_KEY", "")  # Notion Integration Secret
NOTION_DATABASE_ID:        str  = "331b571e4f7e8092b27dded753e5c3fe"     # スクリーナー銘柄DB の ID

# Obsidian Vault パス（DRIVE_BASE/obsidian_vault）
OBSIDIAN_VAULT_DIR: str = str(DRIVE_BASE / "obsidian_vault")

# signal_aggregator のデフォルト重み（正本はここ）。signal_aggregator が起動時に読み込み
# （sync_default_weights_from_config）、両者の一致は tests/test_signal_weights_ssot.py が強制する。
#
#   【重みの位置づけ】これは「初期値」。実運用では rule_optimizer が
#   月次で調整した rule_weights.json の値が優先される。
#   合計は必ず 1.00 にすること（signal_aggregator が起動時に検証する）。
SIGNAL_WEIGHTS = {
    "sector_score":           0.21,   # A: composite_score
    "macro_signals":          0.13,   # B: MACRO_RULES シグナル
    "technical_score":        0.15,   # C: テクニカル
    "market_breadth":         0.10,   # D: 市場幅
    "options_sentiment":      0.08,   # E: オプション恐怖/強欲
    "anomaly_season":         0.03,   # F: 季節アノマリー
    "us_lead_signal":         0.03,   # G: JP×US先行シグナル
    "etf_heat":               0.01,   # H: テーマETF熱量
    "yield_curve":            0.01,   # I: イールドカーブ
    "pearl_signal":           0.04,   # J: 光る原石スコア（Pearl Ph1）
    "independence_signal":    0.04,   # K: Independence Score（Independence Ph1）
    "politician_signal":      0.02,   # L: 議員取引シグナル
    "institutional_signal":   0.01,   # M: 機関投資家13Fシグナル（Policy Ph3）
    "geo_signal":             0.04,   # N: 地政学×権益統合シグナル（G-X）
    "cluster_phase_bonus":    0.01,   # 技術クラスターフェーズボーナス（B）
    "hiring_signal":          0.01,   # O: 人材投資シグナル（I-1）
    "exec_transition":        0.01,   # P: 経営方針転換シグナル（I-3）
    "facility_signal":        0.01,   # Q: 設備投資シグナル（I-4）
    "standardization_signal": 0.01,   # R: 標準化動向シグナル
    "academic_signal":        0.01,   # S: 学術カンファレンスシグナル
    "midterm_signal":         0.01,   # T: 中計達成率シグナル
    "taxonomy_size_bonus":    0.01,   # U: taxonomy×size交差評価
    "taxonomy_signal":        0.02,   # V: node_hotness（T-5）
}

# ==============================================================
# BLOCK I-1: EPS加速度スコア設定
# ==============================================================
ENABLE_EARNINGS_ACCELERATION: bool = True   # EPS加速度スコアを有効化
# 加速度の閾値（四半期年換算成長率 - 年率成長率）
# この値以上で加速判定
ACCEL_STRONG_UP:   float =  0.30   # 強加速 → +10pt
ACCEL_MID_UP:      float =  0.15   # 中加速 → +7pt
ACCEL_MILD_UP:     float =  0.05   # 軽加速 → +4pt
ACCEL_MILD_DOWN:   float = -0.05   # 軽減速 → -4pt
ACCEL_MID_DOWN:    float = -0.15   # 中減速 → -7pt
ACCEL_STRONG_DOWN: float = -0.30   # 強減速 → -10pt

# ==============================================================
# BLOCK GAP-5: EDINET DBカバレッジ割り引き設定
# ==============================================================
EDINET_COVERAGE_FULL: float = 0.7   # 70%以上 → bonus フル適用
EDINET_COVERAGE_HALF: float = 0.3   # 30〜70% → bonus 50%割り引き
                                     # 30%未満 → bonus 適用しない

# ==============================================================
# BLOCK M-3: アクティビスト候補フィルター設定
# ==============================================================
ENABLE_ACTIVIST_FILTER: bool  = True
ACTIVIST_SCORE_THRESHOLD: float = 70.0   # 「触媒待ち割安」判定の閾値
ACTIVIST_TOP_N: int            = 10      # activist_top の取得件数

# alert_notifier.py との連携:
# TDnet RSS で自社株買い/増配が検知された銘柄が
# activist_score >= ACTIVIST_SCORE_THRESHOLD の場合に優先アラート
ACTIVIST_CATALYST_ALERT: bool  = True

# ==============================================================
# BLOCK M-1: ネットキャッシュリッチスクリーナー設定
# ==============================================================
NET_CASH_RICH_THRESHOLD: float = 0.30  # ネットキャッシュ/時価総額 > 30%

# ==============================================================
# BLOCK P-3: 長期保有候補フラグ設定
# ==============================================================
# stock_screener._calc_longterm_hold_flag()（片山晃モデル「10年持てる銘柄」）
# が参照する。is_longterm_hold は investment_journal / longterm_scenario /
# screener の long_term_candidates が消費する。
# ROE は比率で持つ（実装側で ×100 してパーセント比較する）。
LONGTERM_HOLD_EQUITY_RATIO_MIN: float = 0.60  # 自己資本比率60%以上
LONGTERM_HOLD_ROE_MIN:          float = 0.10  # ROE 10%以上（比率）
LONGTERM_HOLD_MIN_CONDITIONS:   int   =  3    # 有効条件の最低充足数

# [P-3 × R-5] 長期保有候補がいる業種で、長期スタンス判定の
# long_high 閾値（既定65.0pt）をこのpt数だけ緩める。
# longterm_score は業種の10年シナリオ＝マクロの見立てだが、P-3 は
# 「自己資本60%以上×増益基調×ROE10%以上」を満たす企業が実在するという
# 銘柄レベルの実証。見立てに実物の裏付けが付く分だけ長期側の確信を上げる。
# 0.0 にすれば P-3 はスタンス判定に影響しない。
LONGTERM_HOLD_STANCE_RELAX_PT: float = 5.0

# ==============================================================
# BLOCK I-2: 会社予想保守度スコア設定
# ==============================================================
ENABLE_CONSERVATISM_SCORE: bool     = True
# 保守度スコアに使う四半期数（earnings_surprise が読む）
CONSERVATISM_MIN_QUARTERS: int      = 8     # 保守度計算に使う四半期数
ALERT_ON_HIGH_PROB_SURPRISE: bool   = True  # 決算サプライズ高確率候補アラート
ALERT_ON_NEGATIVE_RISK: bool        = True  # ネガティブリスクアラート

# ==============================================================
# BLOCK I-3: 経営者スコア設定
# ==============================================================
ENABLE_CEO_EVALUATOR: bool    = True
CEO_MIN_TENURE_YEARS: int     = 2      # 何年以上で暫定スコアを出すか
CEO_QUALITY_BONUS_HIGH: float = 10.0   # ceo_score >= 80 → quality_score +10pt
CEO_QUALITY_BONUS_MID:  float =  5.0   # ceo_score >= 65 → quality_score +5pt
# M&A の成否は ROIC ではなく、のれん減損比率（goodwill_impairment / total_assets）で見る
#   （ceo_evaluator._axis3_capital_allocation）。

# ==============================================================
# BLOCK I-4: 確信度別ポジションサイジング設定
# ==============================================================
ENABLE_CONVICTION_SIZING: bool  = True
# 確信度レベルの閾値（L5/L4/L3/L2の下限スコア）
CONVICTION_THRESHOLDS: list     = [90, 75, 60, 45]
# 各ファクターの重み（合計が1.0でなくても正規化される）
CONVICTION_W_COMPOSITE:    float = 0.20
CONVICTION_W_ACCEL:        float = 0.20   # I-1
CONVICTION_W_BQ:           float = 0.15   # P-2
CONVICTION_W_CONSERVATISM: float = 0.15   # I-2
CONVICTION_W_ACTIVIST:     float = 0.10   # M-3
CONVICTION_W_GOVERNANCE:   float = 0.10   # M-2（暫定: activist近似）
CONVICTION_W_CEO:          float = 0.10   # I-3
# ポートフォリオ制約
MAX_SINGLE_STOCK_PCT:  float = 0.15   # 単一銘柄15%上限
MAX_SINGLE_SECTOR_PCT: float = 0.25   # 単一業種25%上限
MAX_LEVEL5_TOTAL_PCT:  float = 0.40   # Level5合計40%上限

# ==============================================================
# BLOCK R-3: 投資ジャーナル（知識蓄積DB）設定
# ==============================================================
ENABLE_INVESTMENT_JOURNAL: bool    = True
JOURNAL_AUTO_EXIT_WEEKS: int       = 18   # 何週screenerを通過しなければ自動exit
MISS_FACTOR_ALERT_THRESHOLD: int   =  3   # miss_factor が何回でアラートを出すか
ENABLE_MISS_FACTOR_ALERT: bool     = True

# ==============================================================
# BLOCK GAP-1: confidence_prior / ベイズ的更新設定
# ==============================================================
ENABLE_BAYESIAN_UPDATE: bool    = True
CONFIDENCE_PRIOR_MAX_DAYS: int  = 30    # 信頼度1.0になる基準日数
# DRIFT_ALERT_THRESHOLD は BLOCK 35 で定義（二重定義すると後勝ちで黙って上書きされる）。
CONFIDENCE_PRIOR_MIN_HIST: int  =  3    # 最低何日分あれば更新するか

# ==============================================================
# BLOCK GAP-8: アルファ帰属分析設定
# ==============================================================
ENABLE_ALPHA_ATTRIBUTION: bool = True
FORCE_ATTRIBUTION_CALC:   bool = False   # True にすると月末以外でも IC 計算を実行
ATTRIBUTION_MIN_FIRE:     int  = 3       # IC計算に必要な最低発動回数
ATTRIBUTION_LOOKBACK_W:   int  = 26      # 分析対象週数（半年）

# ==============================================================
# BLOCK I-5: 中期経営計画達成率トラッカー設定
# ==============================================================
# 有効フラグは BLOCK Wave7 の ENABLE_MIDTERM_PLAN_TRACKER が正本。
MIDTERM_ACHIEVEMENT_HIGH:  float = 0.90   # 90%以上 → 高達成
MIDTERM_ACHIEVEMENT_RISK:  float = 0.70   # 70%未満（最終2年前時点）→ 未達リスク

# ==============================================================
# BLOCK P-4: 株主構成分析設定
# ==============================================================
# 停止中（読み先の旧EDINET API蓄積DBが廃止され対象が0銘柄）。
# 再開するには EdinetDB 等から大株主データを取り込む経路が必要。
ENABLE_SHAREHOLDER_ANALYZER: bool   = False
FOREIGN_INVESTOR_SPIKE_PCT:  float  = 3.0   # 外国人持株前年比+3%以上でアラート
ACTIVIST_HOLDINGS_THRESHOLD: float  = 0.05  # 単一外国人/機関が5%超でアクティビスト参戦
SHAREHOLDER_SCORE_TOP_N:     int    = 10    # analysis 対象の上位 N 銘柄

# ==============================================================
# BLOCK M-4: TDnet 重要適時開示モニター設定
# ==============================================================
ENABLE_TDNET_MONITOR: bool  = True
# 検知カテゴリ（コメントアウトで無効化）
TDNET_MONITOR_CATEGORIES: list = [
    "BUYBACK",     # 自己株式取得
    "CANCEL",      # 自己株式消却
    "DIV_UP",      # 増配・特別配当
    "SPLIT",       # 株式分割
    "MBO",         # MBO/TOB
    "MGMT_CHANGE", # 役員異動
]

# ==============================================================
# BLOCK GAP-2: sector_tier 動的切り替え設定
# ==============================================================
# スコア順位の割合で TOP/MID/BOT に分ける（固定閾値だと大半の業種がBOTに偏り、
# スクリーナー対象が数業種に絞られてしまう）。
SECTOR_TIER_TOP_SHARE:      float = 1 / 3  # 上位この割合を TOP_TIER
SECTOR_TIER_BOT_SHARE:      float = 1 / 3  # 下位この割合を BOT_TIER（screener対象外）。残りが MID

# momentumによるtier格上げ条件（slope pt/週）
SECTOR_TIER_UPGRADE_SLOPE:   float =  2.5  # MID→TOPに格上げ
SECTOR_TIER_DOWNGRADE_SLOPE: float = -2.5  # TOP→MIDに格下げ

# 危機モード時にTOP_TIERの最大業種数を制限する
SECTOR_TIER_CRISIS_MAX_TOP:  int   =  3    # 危機時はTOP業種を3つに絞る

# ==============================================================
# BLOCK SCORE-CAP: 補正量の上限管理（問題B）
# ==============================================================
# composite_score への各補正の合計上限を設定する。
# これを超えた補正は比例縮小して適用する。
SCORE_MAX_MACRO_ADJ:   float = 20.0   # MACRO_RULES 全シグナル合計の上限
# composite_score の下限（0未満にしない / 元の設計は -30 だったが 0 に修正）
SCORE_MIN_COMPOSITE:   float =  0.0  # スケール統一（0〜100）
SCORE_MAX_COMPOSITE:   float = 100.0
SCORE_MAX_CROSS_ADJ:   float = 10.0   # CROSS_SECTOR_RULES 合計の上限
SCORE_MAX_CYCLE_ADJ:   float = 10.0   # D-1 経済サイクル補正の上限
SCORE_MAX_TOTAL_ADJ:   float = 30.0   # 全補正の絶対合計上限

# ==============================================================
# BLOCK D-1: 経済サイクルフェーズ判定設定（D-1 / ダリオ4象限）
# ==============================================================

# 経済サイクルフェーズ判定を有効化するフラグ
ENABLE_ECONOMIC_CYCLE: bool = True

# GDP成長率の「成長/後退」境界値（前期比%）
# この値を上回れば「成長↑」、下回れば「成長↓」
CYCLE_GDP_GROWTH_THRESHOLD: float = 0.0

# CPI変化率の「インフレ/デフレ」境界値（前期比%）
# この値を上回れば「インフレ↑」、下回れば「インフレ↓」
CYCLE_CPI_CHANGE_THRESHOLD: float = 2.0

# フェーズ別セクター補正量（空辞書の場合はモジュールのデフォルト値を使用）
# カスタマイズする場合は以下のように定義する:
# CYCLE_PHASE_SECTOR_ADJUSTMENTS = {
#     "A": {"1009": +8, "1028": +4, "1004": -4, ...},
#     "B": {"1016": +5, "1017": +5, "1025": +5, ...},
#     "C": {"1008": +3, "1016": -8, "1017": -8, ...},
#     "D": {"1008": +5, "1004": +4, "1017": -6, ...},
# }
CYCLE_PHASE_SECTOR_ADJUSTMENTS: dict = {}  # 空=economic_cycle_detector のデフォルト値を使用

# フェーズ判定結果のキャッシュファイル名
CYCLE_PHASE_HISTORY_FILE: str = "cycle_phase_history.json"

# ==============================================================
# BLOCK D-2: 長期債務サイクル指標設定
# ==============================================================
ENABLE_DEBT_CYCLE: bool = True

# FRED APIで取得する系列（POLICY_FRED_API_KEYが必要）
DEBT_CYCLE_FRED_SERIES = {
    "hy_spread":   "BAMLH0A0HYM2",      # HYスプレッド（リスクプレミアム）
    "credit_gdp":  "CRDQJAPABIS",        # 日本 民間信用/GDP（BIS）
    "vix":         "VIXCLS",             # VIX（補助）
}

# debt_cycle_score の閾値
DEBT_CYCLE_DELEVER_THRESH: int = 70  # スコア70以上 → デレバレッジ局面（全体-10pt）
DEBT_CYCLE_EXPAND_THRESH:  int = 30  # スコア30以下 → レバレッジ拡大初期（株式+5pt）

# ==============================================================
# BLOCK A-1: IC トラッカー設定
# ==============================================================
# alpha_attributer.update_ic_tracker() が参照。直近12週の週次IC系列から
# シグナルの安定性を判定する。learning_audit_log がその結果を読む。
ENABLE_IC_TRACKER:         bool  = True
# 「直近12週のうち IC >= 0 だった週の比率」がこの値以上なら安定シグナル。
# 例: 0.80 = 12週中10週以上でICが非負なら「安定して効いている」とみなす。
# ICの大きさ（絶対値）ではなく“プラスであり続けた頻度”で見る点に注意。
IC_STABLE_MIN_RATE:        float = 0.80
# 直近この週数が連続で IC < 0 なら rule_optimizer 見直し推奨。
# 判定に必要な最低週数も兼ねる（これ未満の系列は安定判定の対象外）。
IC_REVIEW_CONSECUTIVE_NEG: int   =  3

# ==============================================================
# BLOCK A-2: コスト控除後リターン計算設定
# ==============================================================
TRADE_COMMISSION_PCT: float = 0.002   # 往復手数料（0.2% = 各0.1%）
TRADE_SPREAD_PCT:     float = 0.001   # 往復スプレッド（0.1%）
CAPITAL_GAINS_TAX:    float = 0.20315 # 譲渡益税（20.315%）
USE_NISA_ACCOUNT:     bool  = False   # NISA口座はTrueで非課税

# ==============================================================
# BLOCK C-1: VaR制約付きスクリーニング設定
# ==============================================================
ENABLE_VAR_CONSTRAINT:           bool  = True
VAR_CONSTRAINT_THRESHOLD_PCT:    float = 3.0   # VaR%がこれ以上でBuy制限
SCREENER_MAX_BUY_NORMAL:         int   = 10    # 通常時のBuy候補最大数
SCREENER_MAX_BUY_CONSTRAINED:    int   =  2    # VaR超過時のBuy候補最大数

# ==============================================================
# BLOCK C-2: 相関制約設定
# ==============================================================
ENABLE_CORR_CONSTRAINT:           bool  = True
MAX_CANDIDATES_PER_CORR_CLUSTER:  int   =  1     # 1クラスタから最大1銘柄
CORR_CLUSTER_THRESHOLD:           float = 0.75   # この相関以上を同一クラスタと見なす

# ==============================================================
# BLOCK R-5: 10年シナリオプランナー設定
# ==============================================================
ENABLE_LONGTERM_SCENARIO: bool = True

# シナリオ確率ウェイト初期値（9シナリオの合計が1.0になるよう正規化される）
# 手動で調整する場合はここを編集する（年2回推奨）
# キー形式: "{tech}_{geo}_{japan}"
# tech: fast/standard/slow
# geo:  cooperation/decoupling/conflict
# japan: revival/stagnation/transform
SCENARIO_WEIGHTS_INIT: dict = {
    "fast_cooperation_revival":     0.15,
    "fast_decoupling_revival":      0.10,
    "fast_cooperation_transform":   0.08,
    "standard_cooperation_revival": 0.18,
    "standard_decoupling_stagnation": 0.12,
    "standard_cooperation_transform": 0.10,
    "slow_decoupling_stagnation":   0.12,
    "slow_conflict_stagnation":     0.08,
    "slow_conflict_transform":      0.07,
}

# 業種別長期スコアマトリクスの上書き（空の場合はデフォルト値を使用）
LONGTERM_SECTOR_MATRIX: dict = {}

# ==============================================================
# BLOCK RESEARCH-1: HMMレジーム検知設定
# ==============================================================
ENABLE_REGIME_DETECTOR: bool = False    # 30日データ蓄積後に True に変更
HMM_MIN_TRAIN_DAYS:     int  = 30       # 最低学習日数
HMM_REFIT_WEEKLY:       bool = True     # 毎週月曜日に再学習するか

# ==============================================================
# BLOCK RESEARCH-3: Googleトレンド設定
# ==============================================================
ENABLE_GOOGLE_TRENDS: bool = True

# 業種別キーワード（空の場合は google_trends_pipeline.py のデフォルトを使用）
TREND_KEYWORDS: dict = {}   # 上書きしたい場合はここに定義する

# LEADING_INDICATOR_RULES への統合設定
TREND_TAILWIND_PTS:  float = 3.0    # 追い風シグナルの加算点
TREND_MOM_THRESHOLD: float = 0.20   # 前月比+20%超でブースト判定

# ==============================================================
# BLOCK RESEARCH-4: XGBoost ML補正レイヤー設定
# ==============================================================
ENABLE_ML_CORRECTION: bool  = False   # 26週データ蓄積後に True に変更
FORCE_ML_TRAIN:       bool  = False   # True で月末以外でも学習実行
ML_CORRECTION_MAX_PT: float =  5.0   # 補正量の上限（±5pt）

# ==============================================================
# BLOCK RESEARCH-5: FinBERT感情分析設定
# ==============================================================
ENABLE_FINBERT:     bool  = True
FINBERT_MODEL_NAME: str   = "ProsusAI/finbert"   # Hugging Face モデルID
FINBERT_BATCH_SIZE: int   = 8                     # バッチサイズ（CPUメモリ制約）

# ==============================================================
# BLOCK policy_radar: 政府答弁・官僚PDF先回り予測設定
# ==============================================================
ENABLE_POLICY_RADAR:         bool = True
# NEDO採択情報スクレイパー（週次）。BQ edinet.policy_support への唯一の
# 書き込み経路で、base_agent の政策支援ボーナスの前提データになる。
ENABLE_NEDO_SCRAPER:         bool = True
# 公募一覧（状況＝決定）から1回にたどる決定ページの上限（決定ページは一覧から自動発見する）。
NEDO_DECISION_MAX_PAGES:     int  = 20
POLICY_RADAR_MAX_FEEDS:      int  = 10         # 取得する官庁RSS源の上限
KOKKAI_SEARCH_KEYWORDS: list = [               # 国会議事録API検索語（上限5件）
    "半導体", "防衛費", "子育て支援", "インバウンド", "GX"
]
POLICY_KEYWORD_MAP: dict = {}   # 空の場合は policy_radar.py のデフォルトを使用
GOV_RSS_FEEDS:      list = []   # 空の場合は policy_radar.py のデフォルトを使用

# ==============================================================
# BLOCK US-1: 米国株財務DBローテーター設定
# ==============================================================
ENABLE_US_FUNDAMENTALS:    bool  = True
US_DAILY_FETCH_LIMIT:      int   = 100  # 1日あたり最大取得社数（約900社を約9日で一巡）
US_FUNDAMENTALS_CACHE_DAYS: int  = 30   # 財務データの再取得間隔（日）
US_WATCHLIST_TICKERS:      list  = [    # 優先取得銘柄（watchlist）
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "BRK-B", "JPM", "V",
]

# S&P500 ティッカー一覧（us_fundamental_rotator.py のローテーション対象）
# US_SCREENER_CATEGORIES の "sp500" カテゴリ JSON から動的に読み込む
# → ここには直接列挙せず、rotator 起動時に custom_universe/sp500.json を参照する
SP500_TICKERS: list = []  # 実値は run_us_fundamental_rotator() 内で補完する

# ==============================================================
# BLOCK US-2: 米国マクロ拡張パイプライン設定（us_macro_pipeline.py）
# ==============================================================
ENABLE_US_MACRO: bool = True

# FRED APIキーは既存の POLICY_FRED_API_KEY を共用（再設定不要）

# composite_us_macro_score の sector スコアへの反映上限（pt）
US_MACRO_MAX_SECTOR_ADJ: float = 5.0

# ── US-2: FRED拡張マクロ系列（先行指標） ─────────────────────
# us_macro_pipeline.py が参照。各系列を fetch_fred_data で取得。
# キーは内部名、値は (FRED系列ID, 表示名, 単位)。
US_MACRO_FRED_SERIES: dict = {
    "unrate":     ("UNRATE",        "失業率", "%"),
    "cpi":        ("CPIAUCSL",      "CPI(総合)", "index"),      # 前年比を算出
    "hy_spread":  ("BAMLH0A0HYM2",  "HYスプレッド", "%"),
    "icsa":       ("ICSA",          "失業保険新規申請", "件"),
    "permit":     ("PERMIT",        "住宅着工許可", "千戸"),
    "t10y2y":     ("T10Y2Y",        "10Y-2Yスプレッド", "%"),
    "indpro":     ("INDPRO",        "鉱工業生産", "index"),
}
# 先行シグナル判定閾値
US_MACRO_HY_SPREAD_ALERT: float = 5.0    # HYスプレッド>=5% → リスクオフ先行
US_MACRO_T10Y2Y_INVERT: float = 0.0      # 10Y-2Y<0 → 逆イールド（景気後退先行）
US_MACRO_ICSA_SPIKE_PCT: float = 15.0    # ICSA前月比+15%超 → 雇用悪化先行

# Treasury API から国債残高を取得するか（APIキー不要）
# 注意: treasury_debt を作る処理は未実装（weekly_report_generator が読む側だけある）。
#   このフラグは現状どちらに倒しても効果がない。実装するなら us_macro_pipeline.py に追加する。
ENABLE_TREASURY_DATA: bool = True

# ==============================================================
# BLOCK US-3: 8-K リアルタイム監視設定
# ==============================================================
ENABLE_US_8K_MONITOR:   bool = True
US_8K_MAX_ITEM_FETCH:   int  = 30    # Item検知を行う最大銘柄数（API負荷対策）

# ==============================================================
# BLOCK signal_dedup: シグナル重複排除設定
# ==============================================================
ENABLE_SIGNAL_DEDUP:          bool  = True
SCORE_MAX_TOTAL_ADJ_POS:      float = 20.0   # 全シグナル合計の上限（+pt）
SCORE_MAX_TOTAL_ADJ_NEG:      float = -20.0  # 全シグナル合計の下限（-pt）
MAX_SAME_DIRECTION_SIGNALS:   int   =  2     # 同業種・同方向の採用上限件数

# ==============================================================
# BLOCK US-4: Cboe オプション統合設定
# ==============================================================
ENABLE_VIX_TERM_STRUCTURE: bool = True
ENABLE_SKEW_INDEX:         bool = True
ENABLE_IV_SPIKE_DETECT:    bool = True
IV_SPIKE_THRESHOLD:        int  = 70    # IV Rank 70%ile超でiv_spike

# ==============================================================
# BLOCK US-5: SIC/CIK マッピング + JP業種連携設定
# ==============================================================
ENABLE_US_SECTOR_BRIDGE: bool = True
US_SECTOR_MAPPING_CACHE_DAYS: int = 30   # SICマッピングの再取得間隔


# ==============================================================
# BLOCK 問題D: 動的スクリーニング候補ビルダー設定
# ==============================================================
ENABLE_DYNAMIC_SCREENER:     bool = True
DYNAMIC_SCREENER_MAX_ADD:    int  = 50    # 動的追加の上限社数
EDINET_CREDIT_THRESHOLD:     int  = 80    # 動的追加のcredit_score閾値

# ==============================================================
# BLOCK cache_freshness: キャッシュ鮮度管理設定
# ==============================================================
ENABLE_CACHE_FRESHNESS: bool = True

# 各キャッシュの半減期（時間）。デフォルト値は cache_freshness_manager.py に定義。
# カスタマイズしたい場合のみここで指定する（空 dict = デフォルト値を使用）。
CACHE_HALF_LIFE_HOURS: dict = {
    # 例: macro_snapshot を 12時間に短縮したい場合:
    # "macro_snapshot": 12.0,
}

# ==============================================================
# BLOCK B-1: TDnet × EDINET 連携設定
# ==============================================================
ENABLE_B1_TDNET_EDINET: bool = True
# TDnetの決算短信カテゴリID（tdnet_monitor.pyの"category"フィールドに対応）
B1_KESSAN_CATEGORIES: list = [
    "kessan", "financial_results", "決算短信", "earnings", "1", "q_report"
]

# ==============================================================
# BLOCK B-2b: 政府統計先行指標ルール 有効化フラグ（ルール本体は BLOCK B-2）
# ==============================================================
ENABLE_GOV_RULES: bool = True

# ==============================================================
# BLOCK D-1b: 投資判断カード設定
# ==============================================================
ENABLE_INVESTMENT_CARD: bool = True

# ==============================================================
# BLOCK RESEARCH-2: ファクター・クラウディング検知設定
# ==============================================================
ENABLE_FACTOR_CROWDING:        bool  = True
ENABLE_FACTOR_CROWDING_DETAIL: bool  = True   # ファクター別詳細分析
ENABLE_RESIDUAL_CORR:          bool  = False  # Stage2 残差相関（重い・デフォルトOFF）

CROWDING_THRESHOLD:   float = 0.60   # avg_corr >= これ → クラウディングアラート
UNWIND_THRESHOLD:     float = 0.40   # 前週高 → 今週これ未満 → 解消売りアラート
CROWDING_TOP_N:       int   = 20     # 上位N銘柄（クラウディング計算対象）
CROWDING_LOOKBACK_DAYS: int = 30     # リターン計算期間（日）

# rule_optimizer が自動削減する閾値（クラウディングスコアがこれ以上で削減）
CROWDING_REDUCTION_TRIGGER:  int   = 80     # スコア80以上で自動削減
CROWDING_REDUCTION_RATE:     float = 0.85   # 重みをこの比率で削減

# ==============================================================
# BLOCK ⑤: 論文スコアリング強化（Semantic Scholar API）設定
# ==============================================================
ENABLE_SEMANTIC_SCHOLAR: bool = True  # True で重み付き論文スコア有効化
# 注: SS APIは無料・APIキー不要だが、1論文あたり約0.1秒の追加処理が発生
# 上位候補のみに限定するため WEEKLY_MODULES での実行を推奨

# ============================================================
# XBRL タグマッピング
# ============================================================
# キー: BigQuery financials テーブルのカラム名
# 値 : EDINET XBRL のローカルタグ名（名前空間プレフィックスなし）
# 日本基準（J-GAAP）のみ対応。
XBRL_TAG_MAP = {
    # ---- 損益計算書（PL）----
    "net_sales":                   "NetSales",
    "operating_income":            "OperatingIncome",
    "ordinary_income":             "OrdinaryIncome",
    "net_income":                  "ProfitLoss",              # 連結
    # ---- 貸借対照表（BS）----
    "total_assets":                "Assets",
    "equity":                      "NetAssets",
    "interest_bearing_debt":       "InterestBearingDebt",
    "cash":                        "CashAndDeposits",
    # ---- キャッシュフロー（CF）----
    "operating_cf":                "NetCashProvidedByUsedInOperatingActivities",
    "investing_cf":                "NetCashProvidedByUsedInInvestingActivities",
    # free_cf は edinet_direct_fetcher.py 内で operating_cf + investing_cf から計算
    # ---- 人的資本 ----
    "num_employees":               "NumberOfEmployees",
    "avg_age":                     "AverageAgeYearsInformationAboutEmployees",
    "avg_salary":                  "AverageAnnualSalaryInformationAboutEmployees",
    # ---- 役員報酬 ----
    "total_director_remuneration": "TotalAmountOfRemunerationEtcDirectors",
    "max_director_remuneration":   "RemunerationEtcOfHighestPaidDirector",
    # ---- 設備投資 ----
    "capex":                       "PurchaseOfPropertyPlantAndEquipmentIntangibleAssets",
    "depreciation":                "DepreciationAndAmortization",
}

# セグメント情報のタグマッピング
# 値: EDINET XBRL のセグメント数値タグ名
XBRL_SEGMENT_TAGS = {
    "segment_sales":   "NetSalesByReportableSegment",
    "segment_profit":  "ProfitLossByReportableSegment",
    "segment_assets":  "AssetsByReportableSegment",
}

# EDINET API リクエスト設定
EDINET_REQUEST_INTERVAL = 1.5   # レート制限対策（秒）
EDINET_REQUEST_TIMEOUT  = 30    # タイムアウト（秒）

# ==============================================================
# BLOCK 24: Pearl Score 設定
# ==============================================================

ENABLE_PEARL_SCORE:           bool  = True
PEARL_MIN_SCORE_ALERT:        float = 80.0    # LINE即時通知閾値（SS tier）
PEARL_MCAP_MAX_ALERT_B:       float = 500.0   # 時価総額上限（億円）。超過は通知対象外
PEARL_BONUS_CAP:              float = 15.0    # quality_score への最大加算ボーナス（pt）
PEARL_CACHE_TTL_DAYS:         int   = 7       # pearl_candidates.json TTL
PEARL_HISTORY_TTL_DAYS:       int   = 180     # pearl_history.json TTL
PEARL_DIRECTION_TTL_DAYS:     int   = 90      # direction_momentum.json TTL
PEARL_SMART_MONEY_TTL_DAYS:   int   = 90      # smart_money_trail.json TTL

# Pearl ティア閾値（スコア 0〜100）
PEARL_TIER_SS_MIN: float = 80.0
PEARL_TIER_S_MIN:  float = 70.0
PEARL_TIER_A_MIN:  float = 60.0
PEARL_TIER_B_MIN:  float = 50.0

# Pearl ティア → quality_score ボーナスマッピング
PEARL_TIER_BONUS: dict = {
    "SS": 15.0,
    "S":  10.0,
    "A":   5.0,
    "B":   2.0,
}

# 次元の有効/無効は PEARL_DIMENSION_WEIGHTS の重みを 0.0 にすることで表現する。

# 各ディメンションの重み（合計 = 1.0）
# D1: 方向性 / D3: 利益品質・M-Score proxy / D4: 発見ギャップ
PEARL_DIMENSION_WEIGHTS: dict = {
    "D1": 0.35,
    "D3": 0.40,
    "D4": 0.25,
}

# Semantic Scholar API キャッシュ（citation_cache.json）
SEMANTIC_SCHOLAR_CACHE_TTL_DAYS: int   = 30   # TTL: 30日
SEMANTIC_SCHOLAR_API_URL:        str   = (
    "https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"
    "?fields=citationCount,influentialCitationCount,authors"
)

# ==============================================================
# BLOCK 25: 規模分類 / 業態判定設定
# ==============================================================

# ── Size Tier（JP 時価総額基準、単位: 億円）──────────────────
SIZE_TIER_THRESHOLDS_JP: dict = {
    "Large": 10_000,   # 1兆円以上
    "Mid":    1_000,   # 1000億円以上
    "Small":    100,   # 100億円以上
    "Micro":     50,   # 50億円以上
    "Nano":       0,   # 50億円未満
}

# ── Size Tier（US 時価総額基準、単位: 億USD）──────────────────
SIZE_TIER_THRESHOLDS_US: dict = {
    "Large": 1_000,   # 1000億USD以上（Mega/Large Cap）
    "Mid":     200,   # 200億USD以上（Large Cap）
    "Small":    20,   # 20億USD以上（Mid Cap）
    "Micro":     5,   # 5億USD以上（Small Cap）
    "Nano":      0,   # 5億USD未満（Micro Cap）
}

# ペニー株フラグ（JP: 株価300円未満）
PENNY_FLAG_THRESHOLD_JP: float = 300.0

# Nano 評価上限（API 負荷軽減のため、1日あたりの評価数上限）
NANO_EVALUATION_MAX_PER_DAY: int = 5

# size_tier_master.json TTL
SIZE_TIER_CACHE_TTL_DAYS: int = 7

# ── 業態定義（BM-01〜BM-12）─────────────────────────────────
BUSINESS_MODEL_TYPES: dict = {
    "BM-01": "装置産業（重工業・鉄鋼・化学）",
    "BM-02": "消耗品製造（食品・日用品・医薬品）",
    "BM-03": "組立製造（自動車・電機・精密機器）",
    "BM-04": "ソフトウェア・SaaS",
    "BM-05": "プラットフォーム（マーケットプレイス）",
    "BM-06": "金融サービス（銀行・保険・証券）",
    "BM-07": "不動産・REIT",
    "BM-08": "インフラ・公益（電力・ガス・通信・運輸）",
    "BM-09": "小売・流通・卸売",
    "BM-10": "サービス（ヘルスケア・教育・外食）",
    "BM-11": "資源・エネルギー採掘",
    "BM-12": "持株会社・コングロマリット",
}

# 業種コード（JP東証33業種）→ 業態マッピング
BM_SECTOR_MAP: dict = {
    "1001": ["BM-11"],               # 水産・農林
    "1002": ["BM-11"],               # 鉱業
    "1003": ["BM-01"],               # 建設
    "1004": ["BM-02"],               # 食料品
    "1005": ["BM-01"],               # 繊維製品
    "1006": ["BM-01"],               # パルプ・紙
    "1007": ["BM-01"],               # 化学
    "1008": ["BM-02"],               # 医薬品
    "1009": ["BM-11"],               # 石油・石炭製品
    "1010": ["BM-01"],               # ゴム製品
    "1011": ["BM-01"],               # ガラス・土石製品
    "1012": ["BM-01"],               # 鉄鋼
    "1013": ["BM-01"],               # 非鉄金属
    "1014": ["BM-01"],               # 金属製品
    "1015": ["BM-03"],               # 機械
    "1016": ["BM-03", "BM-04"],      # 電気機器
    "1017": ["BM-03"],               # 輸送用機器
    "1018": ["BM-03"],               # 精密機器
    "1019": ["BM-01"],               # その他製品
    "1020": ["BM-08"],               # 電力・ガス
    "1021": ["BM-08"],               # 陸運
    "1022": ["BM-08"],               # 海運
    "1023": ["BM-08"],               # 空運
    "1024": ["BM-08"],               # 倉庫・運輸関連
    "1025": ["BM-08"],               # 情報通信
    "1026": ["BM-09"],               # 卸売業
    "1027": ["BM-09"],               # 小売業
    "1028": ["BM-06"],               # 銀行業
    "1029": ["BM-06"],               # 証券・商品先物
    "1030": ["BM-06"],               # 保険業
    "1031": ["BM-06"],               # その他金融
    "1032": ["BM-07"],               # 不動産業
    "1033": ["BM-10"],               # サービス業
}

# 業態別 ROE ベンチマーク（超過リターン判定の基準値）
BM_ROE_BENCHMARKS: dict = {
    "BM-01": 0.06,
    "BM-02": 0.10,
    "BM-03": 0.08,
    "BM-04": 0.15,
    "BM-05": 0.15,
    "BM-06": 0.08,
    "BM-07": 0.05,
    "BM-08": 0.07,
    "BM-09": 0.08,
    "BM-10": 0.10,
    "BM-11": 0.07,
    "BM-12": 0.06,
}

# 業態自動判定の財務比率閾値
BM_DETECTION_THRESHOLDS: dict = {
    "recurring_revenue_ratio_min": 0.70,      # BM-04/05: 繰返し売上比率
    "asset_turnover_heavy_max":    0.50,      # BM-01: 資産回転率≤0.5 = 装置産業
    "gross_margin_saas_min":       0.60,      # BM-04: 粗利率60%以上
    "debt_ratio_bank_min":         0.80,      # BM-06: 負債比率80%以上
    "revenue_per_employee_saas":   20_000_000, # BM-04: 1人あたり売上2000万円以上
}

# ==============================================================
# BLOCK 26: 議員取引監視設定
# ==============================================================
ENABLE_POLITICIAN_MONITOR:   bool  = True
ALERT_POLITICIAN_MIN_SCORE:  int   = 60      # この点数以上でLINE即時通知
# 高スコア議員取引アラート／JP波及補正に使う取引の鮮度（日）。
# 「新規」は履歴に無いという意味でしかなく、初回は年初以降の全取引が新規になる。
# STOCK Act の開示期限は取引から最大45日のため、余裕を見て60日。
POLITICIAN_ALERT_MAX_AGE_DAYS: int = 60
ALERT_BUFFETT_THRESHOLD:     bool  = True    # 著名委員会所属議員を優先スコアリング
POLITICIAN_HISTORY_MAX_ROWS: int   = 5000    # historian_history.json 最大保持行数

# 議員取引キャッシュのTTL設定
POLITICIAN_CACHE_TTL_DAYS:   int   = 180     # politician_history.json 保持日数
EVENT_CACHE_TTL_DAYS:        int   = 7       # event_cache.json 保持日数

# 取得対象年（空リストの場合は当年のみ）
POLITICIAN_TARGET_YEARS:     list  = []      # 例: [2024, 2025] で複数年取得

# 実行曜日（datetime.weekday(): 0=月 1=火 ... 4=金 6=日）
POLITICIAN_RUN_WEEKDAYS: tuple[int, ...] = (1, 4)
def is_politician_run_day(now=None) -> bool:
    """火・金かどうかを判定（JST 固定。env の TZ 設定に依存しない）"""
    from datetime import datetime
    dt = now or datetime.now(JST)
    return dt.weekday() in POLITICIAN_RUN_WEEKDAYS

# ==============================================================
# BLOCK 23: US GICSセクター設定
# ==============================================================

# ── GICSセクター別マクロ感応度ルール（U-1）─────────────────────────
# us_stock_screener._calc_score() がこの辞書を参照し、
# アクティブなマクロファクターに応じて composite_score を加算/減算する。
#
# key:         US セクターETFシンボル（"XLK" 等）
# score_delta: 1ファクターあたりの加算量（正=追い風/負=逆風）
# factor キー一覧:
#   "interest_rate_rise"  / "interest_rate_fall"
#   "oil_price_rise"
#   "recession_risk"
#   "yen_depreciation"    / "yen_appreciation"
US_SECTOR_RULES: dict = {
    "XLK":  {
        "name_ja":          "情報技術",
        "macro_sensitivity": {
            "interest_rate_rise": {
                "score_delta": -4,
                "reason": "DCF割引率上昇→グロース株バリュエーション圧縮",
            },
            "interest_rate_fall": {
                "score_delta": +4,
                "reason": "割引率低下でグロース評価が改善",
            },
            "recession_risk": {
                "score_delta": -3,
                "reason": "IT設備投資削減・広告費カットリスク",
            },
        },
    },
    "XLY":  {
        "name_ja":          "一般消費財",
        "macro_sensitivity": {
            "interest_rate_rise": {
                "score_delta": -3,
                "reason": "消費ローン・住宅ローン金利上昇→耐久財需要抑制",
            },
            "recession_risk": {
                "score_delta": -5,
                "reason": "景気後退で耐久財・高額商品需要が激減",
            },
            "oil_price_rise": {
                "score_delta": -2,
                "reason": "ガソリン高→家計の可処分所得が縮小",
            },
        },
    },
    "XLF":  {
        "name_ja":          "金融",
        "macro_sensitivity": {
            "interest_rate_rise": {
                "score_delta": +6,
                "reason": "NIM（純利鞘）拡大で銀行・証券の収益が改善",
            },
            "interest_rate_fall": {
                "score_delta": -4,
                "reason": "NIM圧縮→銀行収益低下",
            },
            "recession_risk": {
                "score_delta": -8,
                "reason": "不良債権増加・信用コスト急騰・貸し渋り",
            },
        },
    },
    "XLV":  {
        "name_ja":          "ヘルスケア",
        "macro_sensitivity": {
            "recession_risk": {
                "score_delta": +3,
                "reason": "ディフェンシブ・景気後退でも医療需要は不変",
            },
            "interest_rate_rise": {
                "score_delta": -1,
                "reason": "R&D資金調達コスト増（軽微）",
            },
        },
    },
    "XLE":  {
        "name_ja":          "エネルギー",
        "macro_sensitivity": {
            "oil_price_rise": {
                "score_delta": +8,
                "reason": "原油価格上昇で探鉱・生産コンパニーの収益に直結",
            },
            "recession_risk": {
                "score_delta": -4,
                "reason": "需要減退でエネルギー消費量が低下",
            },
            "interest_rate_rise": {
                "score_delta": -1,
                "reason": "探鉱・開発コスト増（軽微）",
            },
        },
    },
    "XLB":  {
        "name_ja":          "素材",
        "macro_sensitivity": {
            "recession_risk": {
                "score_delta": -4,
                "reason": "工業・建設需要の減退で素材需要が低下",
            },
            "interest_rate_rise": {
                "score_delta": -2,
                "reason": "設備投資減退→素材需要間接減",
            },
            "oil_price_rise": {
                "score_delta": -1,
                "reason": "エネルギーコスト増（製造コスト上昇）",
            },
        },
    },
    "XLI":  {
        "name_ja":          "資本財",
        "macro_sensitivity": {
            "recession_risk": {
                "score_delta": -5,
                "reason": "設備投資が凍結され受注が急減",
            },
            "interest_rate_rise": {
                "score_delta": -3,
                "reason": "設備投資コスト増加→受注減退",
            },
        },
    },
    "XLC":  {
        "name_ja":          "通信サービス",
        "macro_sensitivity": {
            "interest_rate_rise": {
                "score_delta": -3,
                "reason": "借入依存度の高い通信会社の金利負担が増大",
            },
            "recession_risk": {
                "score_delta": -1,
                "reason": "デジタル広告収入減少リスク（軽微）",
            },
        },
    },
    "XLU":  {
        "name_ja":          "公益事業",
        "macro_sensitivity": {
            "interest_rate_rise": {
                "score_delta": -5,
                "reason": "ボンドプロキシ・調達コスト増・配当利回りの相対魅力低下",
            },
            "interest_rate_fall": {
                "score_delta": +4,
                "reason": "配当利回りの相対的魅力が回復",
            },
            "recession_risk": {
                "score_delta": +2,
                "reason": "ディフェンシブ・公益需要は景気に非弾力的",
            },
            "oil_price_rise": {
                "score_delta": -3,
                "reason": "発電燃料調達コストの上昇",
            },
        },
    },
    "XLRE": {
        "name_ja":          "不動産",
        "macro_sensitivity": {
            "interest_rate_rise": {
                "score_delta": -6,
                "reason": "REIT評価下落・住宅ローン金利急騰・物件評価額低下",
            },
            "interest_rate_fall": {
                "score_delta": +5,
                "reason": "不動産評価上昇・借入コスト低下でREIT需要回復",
            },
            "recession_risk": {
                "score_delta": -4,
                "reason": "空室率上昇・賃料低下・物件流動性低下",
            },
        },
    },
    "XLP":  {
        "name_ja":          "生活必需品",
        "macro_sensitivity": {
            "recession_risk": {
                "score_delta": +4,
                "reason": "最強ディフェンシブ・食料・日用品需要は景気に非弾力的",
            },
            "oil_price_rise": {
                "score_delta": -2,
                "reason": "物流・包材・農業燃料コスト上昇",
            },
            "interest_rate_rise": {
                "score_delta": -1,
                "reason": "影響軽微（事業安定性が高いため）",
            },
        },
    },
}

# ── GICSセクター別金利感応度ルール（U-2）─────────────────────────
# bond_commodity_pipeline.py の calc_rate_sensitivity_deltas() が参照する。
# FRED API から取得した US10Y / US2Y の変化量（bps）に対して
# GICSセクターETFのスコア補正量を算出するためのルール。
#
# sensitivity:        "positive"（金利上昇有利）/ "negative"（不利）/ "neutral"
# magnitude:          1bps変化あたりの score_delta の目安（スケーリング係数）
#                     例: magnitude=0.06 かつ rate_change=50bps → delta=3.0pt
# rate_threshold_bp:  この bps 以上変化したときのみ発動（ノイズ除去）
# max_delta:          単一ルールの上限クリップ（±pt）
US_RATE_SENSITIVITY_RULES: dict = {
    "XLF":  {
        "sensitivity":       "positive",
        "magnitude":          0.12,
        "rate_threshold_bp":  25,
        "max_delta":          8,
        "reason":            "NIM拡大で銀行・証券収益が直接改善",
    },
    "XLRE": {
        "sensitivity":       "negative",
        "magnitude":         -0.12,
        "rate_threshold_bp":  25,
        "max_delta":          8,
        "reason":            "REIT借入コスト増・物件評価額下落・住宅ローン急騰",
    },
    "XLU":  {
        "sensitivity":       "negative",
        "magnitude":         -0.10,
        "rate_threshold_bp":  25,
        "max_delta":          6,
        "reason":            "ボンドプロキシ・配当利回りの相対魅力が低下",
    },
    "XLK":  {
        "sensitivity":       "negative",
        "magnitude":         -0.08,
        "rate_threshold_bp":  25,
        "max_delta":          6,
        "reason":            "DCF割引率上昇でグロース株の将来CF現在価値が大幅圧縮",
    },
    "XLC":  {
        "sensitivity":       "negative",
        "magnitude":         -0.06,
        "rate_threshold_bp":  25,
        "max_delta":          5,
        "reason":            "借入依存度の高い通信会社の金利負担増",
    },
    "XLY":  {
        "sensitivity":       "negative",
        "magnitude":         -0.06,
        "rate_threshold_bp":  25,
        "max_delta":          5,
        "reason":            "消費ローン・住宅ローン金利上昇で耐久財需要が抑制",
    },
    "XLI":  {
        "sensitivity":       "negative",
        "magnitude":         -0.04,
        "rate_threshold_bp":  25,
        "max_delta":          4,
        "reason":            "設備投資コスト増加→受注減退",
    },
    "XLB":  {
        "sensitivity":       "negative",
        "magnitude":         -0.04,
        "rate_threshold_bp":  25,
        "max_delta":          4,
        "reason":            "工業材料需要の設備投資減退に連動",
    },
    "XLE":  {
        "sensitivity":       "neutral",
        "magnitude":          0.01,
        "rate_threshold_bp":  50,
        "max_delta":          2,
        "reason":            "油価に強く依存・金利感応度は相対的に低い",
    },
    "XLV":  {
        "sensitivity":       "neutral",
        "magnitude":         -0.02,
        "rate_threshold_bp":  50,
        "max_delta":          2,
        "reason":            "R&D調達コスト増（軽微）・需要は金利に非弾力的",
    },
    "XLP":  {
        "sensitivity":       "neutral",
        "magnitude":         -0.02,
        "rate_threshold_bp":  50,
        "max_delta":          2,
        "reason":            "生活必需品需要は金利に非弾力的・影響軽微",
    },
}


# ==============================================================
# BLOCK 29: Feature Engineering 定数（FE-1 / FE-2 / FE-3）
# ==============================================================

# ── FE-1: リスク調整スコア（ATR基準） ────────────────────────────
# _calc_risk_adjusted_score() が参照する。
# ATR_WINDOW: ATR計算ウィンドウ（日数）
# RISK_ADJ_PENALTY_LOW_VOL : ボラ低銘柄への乗数（≤1.0 = ボーナス扱い）
# RISK_ADJ_PENALTY_HIGH_VOL: ボラ高銘柄への乗数（>1.0 = ペナルティ扱い）
ATR_WINDOW                 = 14
RISK_ADJ_PENALTY_LOW_VOL   = 0.85   # $risk\_adj = composite \times 0.85$ (低ボラ優遇)
RISK_ADJ_PENALTY_HIGH_VOL  = 1.20   # $risk\_adj = composite \div 1.20$ (高ボラ割引)
# GARCH_ENABLED: 未実装（GARCH推定の処理も arch ライブラリも無い）。現行のボラ調整は
#   ATR_WINDOW ベースの RISK_ADJ_PENALTY_* が担う。
GARCH_ENABLED              = False

# ── FE-2: アナリスト修正トラッカー ───────────────────────────────
# ANALYST_REVISION_WINDOW_DAYS: upgrade/downgrade 集計ウィンドウ（日数）
# PEAD_TRIGGER_MIN_REVISION   : PEAD トリガー発動最小 revision_score
# PEAD_TRIGGER_DAYS           : PEAD シグナル有効期間（日数）
ANALYST_REVISION_WINDOW_DAYS = 30
PEAD_TRIGGER_MIN_REVISION    = 0.3
PEAD_TRIGGER_DAYS            = 5

# ── FE-3: M-Score proxy ペナルティ閾値 ──────────────────────────
# Beneish(1999)の操作判定閾値。D3スコアに乗数ペナルティを適用。
# M_SCORE_PENALTY_THRESHOLD : この値超 → D3 × M_SCORE_STRONG_MULTIPLIER
# M_SCORE_MEDIUM_THRESHOLD  : この値超（かつ閾値以下）→ D3 × M_SCORE_MEDIUM_MULTIPLIER
M_SCORE_PENALTY_THRESHOLD  = -1.78   # 強操作リスク閾値（Beneish 1999）
M_SCORE_MEDIUM_THRESHOLD   = -2.22   # 中操作リスク閾値（Beneish 1999）
M_SCORE_STRONG_MULTIPLIER  = 0.50    # 強ペナルティ乗数（D3 を半減）
M_SCORE_MEDIUM_MULTIPLIER  = 0.75    # 中ペナルティ乗数（D3 を0.75倍）

# ==============================================================
# BLOCK 30: Ignition Detector 定数（FE-4）
# ==============================================================
# ignition_detector.py が参照する。
# カテゴリA（資金フロー）・B（ナラティブ）・C（価格）のシグナルが
# IGNITION_CONVERGENCE_WINDOW_DAYS 以内に同一セクター/テーマへ収束した
# 場合に convergence_score を算出し、LINE通知トリガーを判定する。

# 収束ウィンドウ（日数）
IGNITION_CONVERGENCE_WINDOW_DAYS  = 14

# LINE通知閾値
IGNITION_MIN_SCORE_LINE            = 3.0   # convergence_score ≥ 3.0 → LINE即時通知
IGNITION_MAX_SCORE_LINE_CRITICAL   = 3.5   # ≥ 3.5 かつ反復検出 → 最高警戒LINE

# 最高警戒判定: IGNITION_CRITICAL_REPEAT_DAYS 以内に2回以上検出で発動
IGNITION_CRITICAL_REPEAT_DAYS     = 14

# カテゴリD（将来拡張用・未実装。ignition_detector は A/B/C の3系統のみ）
IGNITION_CATEGORY_D_ENABLED       = False

# キャッシュ TTL
IGNITION_ALERT_TTL_DAYS           = 14    # ignition_alerts.json
IGNITION_HISTORY_TTL_DAYS         = 180   # ignition_history.json

# ==============================================================
# BLOCK 31: Policy Ph2 定数
# ==============================================================
# policy_signal_applier.py / politician_signal_scorer.py が参照する。
# POLITICIAN_CACHE_LAYERS : 4層減衰キャッシュの層数
# EMERGENCY_VIX_THRESHOLD : この値超で EMERGENCY_OVERRIDE_FLAG を立てる
# SCORE_MAX_POLICY_L4_ADJ : Level 4 Override 時の composite_score 最大加算量

POLITICIAN_CACHE_LAYERS    = 4
EMERGENCY_VIX_THRESHOLD    = 45.0   # VIX > 45 → 緊急介入フラグ
SCORE_MAX_POLICY_L4_ADJ    = 25.0   # $composite\_score + min(bonus, 25.0)$
# 個別銘柄の総合点へ乗せる政策系加点（政策・L4・議会L4）の合計上限。業種単位の上限25のまま
# 銘柄に乗せると、元の点が高い業種の上位銘柄が全部100点に張り付き順位が付かなくなる。
SCORE_MAX_POLICY_STOCK_ADJ = 10.0
# スクリーナー候補に審議KPI 9項目（PBR〜投資資本IRR）を付ける（表示専用）
ENABLE_CANDIDATE_KPI: bool = True

# ==============================================================
# BLOCK 32: FE-5 PEAD監視定数
# ==============================================================
# pead_monitor.py が参照する。
# PEAD_SCORE_ALERT_THRESHOLD: この値以上かつ出来高条件で LINE通知（トリガー㉒）
# PEAD_DECAY_DAILY          : 毎日スコアに乗じる減衰係数（$score \times 0.85$）
# PEAD_VOLUME_MIN_RATIO     : トリガー発動に必要な出来高倍率（平均比）
# PEAD_MAX_HOLDING_DAYS_JP  : JP株の PEAD 有効期間上限（日）

PEAD_SCORE_ALERT_THRESHOLD = 5.0
PEAD_DECAY_DAILY           = 0.85
PEAD_VOLUME_MIN_RATIO      = 1.5
PEAD_MAX_HOLDING_DAYS_JP   = 60

# ==============================================================
# BLOCK 33: FE-6 ポートフォリオ相関管理定数
# ==============================================================
# portfolio_diversity_filter.py が参照する。
# DIVERSITY_CORRELATION_THRESHOLD: この値超のペアを「高相関」と判定
# DIVERSITY_HHI_WARNING          : HHI がこの値超で警告（Gmail週次 トリガー㉓）
# DIVERSITY_HHI_CRITICAL         : HHI がこの値超で危険レベル
# DIVERSITY_OPTION_B_ENABLED     : Option B（強制分散リバランス）の有効化フラグ
# DIVERSITY_CORR_WINDOW_DAYS     : 相関行列の計算ウィンドウ（日）
# HHI: $HHI = \sum_{i} w_i^2$（Herfindahl-Hirschman Index）

DIVERSITY_CORRELATION_THRESHOLD = 0.70
DIVERSITY_HHI_WARNING           = 0.25
DIVERSITY_HHI_CRITICAL          = 0.35
DIVERSITY_OPTION_B_ENABLED      = False
DIVERSITY_CORR_WINDOW_DAYS      = 60

# ==============================================================
# BLOCK 34: FE-7 カタリストカレンダー定数
# ==============================================================
# catalyst_calendar.py が参照する。
# CATALYST_BONUS_MAX         : _calc_catalyst_bonus() の上限加算量（+5pt）
# CATALYST_PROXIMITY_DAYS    : 接近日数の区間定義（[14, 30, 60]日）
# CATALYST_PROXIMITY_SCORES  : 各区間に対応するスコア（[1.0, 0.6, 0.3]）
#   $proximity\_score = SCORES[i]$ if $days\_until \leq DAYS[i]$
# CATALYST_CALENDAR_TTL_DAYS : catalyst_calendar.json の TTL（日）
# CATALYST_HISTORY_TTL_DAYS  : catalyst_history.json の TTL（日）

CATALYST_BONUS_MAX          = 5.0
CATALYST_PROXIMITY_DAYS     = [14, 30, 60]
CATALYST_PROXIMITY_SCORES   = [1.0, 0.6, 0.3]
CATALYST_CALENDAR_TTL_DAYS  = 30
CATALYST_HISTORY_TTL_DAYS   = 365

# カテゴリ別スコア重み（A+B+C = 1.0）
IGNITION_WEIGHT_CATEGORY_A        = 0.40   # 資金フロー（最重要）
IGNITION_WEIGHT_CATEGORY_B        = 0.35   # ナラティブ
IGNITION_WEIGHT_CATEGORY_C        = 0.25   # 価格シグナル

# ==============================================================
# BLOCK 35: E3 / Policy Ph3-Ph4 / S3-S4 / FE-F 定数
# ==============================================================
# 参照モジュール:
#   E3      → segment_mapper.py / segment_signal.py / base_agent.py
#   Ph3     → institutional_13f_monitor.py / institutional_signal_scorer.py
#             policy_signal_applier.py / alert_notifier.py
#   Ph4     → congress_monitor.py / policy_signal_applier.py / policy_radar.py
#   S3      → deep_dive_trigger.py / stock_analyzer_main.py
#             edinet_rotation_manager.py
#   S4/FE-F → score_prior_manager.py / drift_detector.py
#             rule_optimizer.py / stock_analyzer_main.py
# ==============================================================

# ── E3: セグメント名寄せ ─────────────────────────────────────────
# GEMINI_API_KEY          : Gemini API (無料枠) キー - google.colab.userdata から取得
# SEGMENT_SIMILARITY_THRESHOLD : コサイン類似度がこの値以下→Gemini判定に委譲
# SEGMENT_GEMINI_MODEL    : Gemini API で使用するモデル名
# SEGMENT_VERIFIED_AUTO   : sentence-transformers の類似度がこの値超→verified_flag=True
# SEGMENT_MAPPER_TTL_DAYS : segment_mapper キャッシュ有効期間（日）
# SEGMENT_BONUS_MAX       : edinet_segment_bonus の上限（+7pt）
# SEGMENT_BONUS_MIN       : edinet_segment_bonus の下限（-5pt）
# HHI: $HHI = \sum_{i} s_i^2$（セグメント集中度、si=売上構成比）
try:
    from google.colab import userdata as _ud
    GEMINI_API_KEY = _ud.get("GEMINI_API_KEY") or ""
except Exception:
    GEMINI_API_KEY = ""

SEGMENT_SIMILARITY_THRESHOLD = 0.85
SEGMENT_GEMINI_MODEL          = "gemini-1.5-flash"
SEGMENT_VERIFIED_AUTO         = 0.90
SEGMENT_MAPPER_TTL_DAYS       = 90
SEGMENT_BONUS_MAX             = 7.0
SEGMENT_BONUS_MIN             = -5.0

# ── Policy Ph3: 機関投資家（13F）定数 ────────────────────────────
# INSTITUTION_TIER_MAP    : 機関名 → Tier（1=Buffett, 2=Ackman, 3=大手）
# SCORE_MAX_INSTITUTION_ADJ: 機関投資家シグナルによる最大加算量（+15pt）
# INSTITUTION_HISTORY_TTL_DAYS : institutional_history.json TTL（日）
# POLICY_MEDIUM_CACHE_TTL_DAYS : policy_medium_cache.json TTL（日）
# INSTITUTION_MIN_NEW_POSITION_USD: 大手機関アラートの最小取得額（ドル）
# 重み: $institutional\_signal = 0.02$（signal_aggregator DEFAULT_WEIGHTS）

INSTITUTION_TIER_MAP: dict = {
    # Tier 1: 超大型バリュー投資家（最高優先度）
    "Berkshire Hathaway":            1,
    "Berkshire Hathaway Inc":        1,
    # Tier 2: 著名アクティビスト
    "Pershing Square Capital":       2,
    "Pershing Square Capital Management": 2,
    "Elliott Investment Management": 2,
    "Third Point":                   2,
    "Third Point LLC":               2,
    # Tier 3: 大手機関投資家（運用残高1兆ドル超）
    "BlackRock":                     3,
    "BlackRock Inc":                 3,
    "Vanguard Group":                3,
    "State Street":                  3,
    "Fidelity":                      3,
    "T. Rowe Price":                 3,
    "Capital Research":              3,
}
SCORE_MAX_INSTITUTION_ADJ         = 15.0
INSTITUTION_HISTORY_TTL_DAYS      = 180
POLICY_MEDIUM_CACHE_TTL_DAYS      = 90
INSTITUTION_MIN_NEW_POSITION_USD  = 1_000_000_000   # 10億ドル
# Tier1/2（Buffett・アクティビスト）の新規取得の通知・L3発動の下限。
#   Berkshire の13Fには子会社運用分の数十万ドルの保有も載るため足切りする。
INSTITUTION_MIN_NEW_POSITION_USD_ACTIVE = 100_000_000   # 1億ドル

# ── Policy Ph3 追加設定（INSTITUTION_TIER_MAP / SCORE_MAX_INSTITUTION_ADJ と併用）────

# 13F取得の有効フラグ（False でモジュール全体をスキップ）
ENABLE_INSTITUTIONAL_13F: bool = True

# SEC EDGAR API エンドポイント
EDGAR_SUBMISSIONS_URL: str = "https://data.sec.gov/submissions"
EDGAR_ARCHIVES_URL:    str = "https://www.sec.gov/Archives/edgar/data"

# institutional_bonus の composite_score への適用上限（us_stock_screener.py 参照）
INSTITUTIONAL_BONUS_MAX: float =  8.0   # 最大 +8pt
INSTITUTIONAL_BONUS_MIN: float = -5.0   # 最小 -5pt（機関投資家売越時）

# institutional_history.json のキャッシュパス（CACHE_DIR 配下）
INSTITUTIONAL_CACHE_PATH: str = "institutional_history.json"

# 四半期提出期間の判定補助設定
# 具体的な判定ロジックは institutional_13f_monitor._is_quarter_filing_period() に実装済み
# ここでは月次設定のみ定数として保持する
INSTITUTIONAL_QUARTER_MONTHS: tuple = (1, 4, 7, 10)  # 提出期間の月
INSTITUTIONAL_QUARTER_DAY_FROM: int = 15              # 提出期間開始日（各月15日〜）

# ── Policy Ph4: 構造変化L4・congress_monitor 定数 ─────────────────
# SCORE_MAX_POLICY_L4_ADJ    : L4 Override 最大加算量（+25pt）← BLOCK31の値を上書き
# EMERGENCY_DAILY_DROP_THRESHOLD: 緊急オーバーライド発動の日次下落率閾値
# POLICY_STRUCTURAL_CACHE_TTL_MONTHS: policy_structural_cache.json TTL（月）
# PDF_URL_CACHE_TTL_DAYS     : pdf_url_cache.json TTL（日）
# CONGRESS_BILL_STAGES       : 議会進行ステージ定義（進行順）
# POLICY_SECTOR_IMPACT_MAP   : 法案種別 → 影響業種コード＆スコア方向の定義
# $L4\_override = min(impact\_score, SCORE\_MAX\_POLICY\_L4\_ADJ)$

SCORE_MAX_POLICY_L4_ADJ_W4          = 25.0   # L4専用（BLOCK31と区別）
EMERGENCY_DAILY_DROP_THRESHOLD       = -0.04  # 日次-4%超で緊急フラグ
POLICY_STRUCTURAL_CACHE_TTL_MONTHS   = 12
PDF_URL_CACHE_TTL_DAYS               = 30
ENABLE_PDF_EXTRACTION                = True   # pdfplumber 有効フラグ

# congress_monitor 実行フラグ・APIキー（未設定時はキャッシュのみで動作）。
#   キー取得: https://api.congress.gov/sign-up/
ENABLE_CONGRESS_MONITOR: bool = True
CONGRESS_API_KEY: str = os.getenv("CONGRESS_API_KEY", "")
CONGRESS_SESSION: int = 119   # 第119議会（2025-2027）

CONGRESS_BILL_STAGES: list = [
    "introduced",          # 提出
    "committee",           # 委員会審議
    "floor_vote",          # 本会議採決
    "passed",              # 可決
    "signed",              # 成立
]

# 法案種別 → 影響業種コード＆方向（+pt=追い風, -pt=逆風）
# $impact\_score = base\_score \times stage\_multiplier$
POLICY_SECTOR_IMPACT_MAP: dict = {
    "semiconductor_subsidy": {
        "sectors": ["1016", "1025"],
        "base_score": 15.0,
        "direction": "tailwind",
        "description": "半導体補助金・輸出優遇→電気機器・情報通信に追い風",
    },
    "clean_energy": {
        "sectors": ["1020", "1009"],
        "base_score": 12.0,
        "direction": "tailwind",
        "description": "再エネ推進法案→電力ガス・石油に影響",
    },
    "tariff_steel": {
        "sectors": ["1012", "1013"],
        "base_score": -10.0,
        "direction": "headwind",
        "description": "鉄鋼関税→鉄鋼・非鉄に逆風",
    },
    "pharma_pricing": {
        "sectors": ["1008"],
        "base_score": -8.0,
        "direction": "headwind",
        "description": "医薬品価格規制法案→医薬品に逆風",
    },
    "defense_spending": {
        "sectors": ["1015", "1016", "1018"],
        "base_score": 10.0,
        "direction": "tailwind",
        "description": "防衛予算増→機械・電気機器・精密機器に追い風",
    },
    "financial_regulation": {
        "sectors": ["1028", "1029", "1030"],
        "base_score": -6.0,
        "direction": "headwind",
        "description": "金融規制強化→銀行・証券・保険に逆風",
    },
    "infrastructure": {
        "sectors": ["1003", "1014", "1021"],
        "base_score": 8.0,
        "direction": "tailwind",
        "description": "インフラ投資法案→建設・金属製品・陸運に追い風",
    },
}

# 議会ステージ別スコア乗数（成立に近いほど影響大）
CONGRESS_STAGE_MULTIPLIER: dict = {
    "introduced":  0.20,
    "committee":   0.40,
    "floor_vote":  0.70,
    "passed":      0.90,
    "signed":      1.00,
}

# ── S3: Stage3 リエンジニアリング定数 ───────────────────────────
# SECTOR_TIER_THRESHOLDS : sector_tier（TOP/MID/BOT）分類の閾値
# DEEP_DIVE_CONFIDENCE_DROP: confidence がこの値以上下落で deep_dive 発動
# DEEP_DIVE_RANK_JUMP      : screener順位がこの値以上急変で deep_dive 発動
# DEEP_DIVE_HISTORY_TTL    : deep_dive_history.json TTL（日）
# CONFIDENCE_PRIOR_ALPHA   : confidence_prior の EMA 更新率

SECTOR_TIER_THRESHOLDS: dict = {
    "TOP": 65.0,   # composite_score >= 65 → TOP tier
    "BOT": 35.0,   # composite_score <= 35 → BOT tier（それ以外は MID）
}
DEEP_DIVE_CONFIDENCE_DROP = 15.0   # confidence の1週間低下量（pt）
DEEP_DIVE_RANK_JUMP       = 10     # screener順位の急変幅（位）
DEEP_DIVE_HISTORY_TTL     = 30     # deep_dive_history.json TTL（日）
CONFIDENCE_PRIOR_ALPHA    = 0.15   # EMA 更新率（S3-3 confidence_prior）
# 業種スコアの confidence_prior（EMA）更新。前回値は score_history.json から読み、
#   結果は sector_results と score_history に残す（スコア自体は変えない）。
ENABLE_S3_CONFIDENCE_PRIOR: bool = True

# 経営者スコア（ceo_evaluator）の経営トップ・就任日の取得元。有報PDFの「役員の状況」を読む（officer_extractor.py）。
ENABLE_OFFICER_EXTRACTOR: bool = True
OFFICER_DAILY_LIMIT: int = 40          # 1日に読む有報の上限（1冊1〜3秒）

# ── S4/FE-F: スコア事前確率・ファクターモメンタム定数 ─────────────
# SCORE_PRIOR_TTL_DAYS     : score_prior_manager のキャッシュ TTL（日）
# SCORE_PRIOR_FRESHNESS_HALF_LIFE: 鮮度係数の半減期（日）
#   $freshness = exp(-\lambda \times days\_elapsed)$, $\lambda = \ln(2) / half\_life$
# DRIFT_ALERT_THRESHOLD    : 急変ドリフト検出閾値（pt/week）
# DRIFT_DECAY_THRESHOLD    : 緩慢劣化検出閾値（連続低下週数）
# FACTOR_HHI_MAX           : ファクター重み集中度の上限
#   $HHI = \sum_{i} w_i^2 \leq FACTOR\_HHI\_MAX$

SCORE_PRIOR_TTL_DAYS              = 90
SCORE_PRIOR_FRESHNESS_HALF_LIFE   = 14    # 2週間で重みが半減
DRIFT_ALERT_THRESHOLD             = 20.0  # 1週間で20pt超の変化を「急変」と判定
DRIFT_DECAY_THRESHOLD             = 4     # 4週連続低下で「緩慢な劣化」を検出
DRIFT_HISTORY_TTL_DAYS            = 180   # drift_history.json TTL（日）

# ── FE-F: ファクター定義（4ファクター対応）────────────────────────
# FACTOR_DEFINITIONS: rule_optimizer.py の週次ファクターリターン計算に使用
# ── BLOCK 29 との接続メモ ────────────────────────────────────────
# low_volatility は FE-1（ATR）の計算結果を流用する。
# FE-1 の ATR ティッカーは BLOCK 29 の FE_ATR_TICKERS で定義済み。
# rule_optimizer.calc_weekly_factor_returns() は
# FACTOR_DEFINITIONS["low_volatility"]["source"] = "atr" を参照し、
# score_history の weekly_score_changes の std を ATR プロキシとして使用する。
# （完全な ATR 取得は FE-1 の atr_pipeline.py に委譲）
# 各ファクターの計算ソース・方向・重み・危機局面での重み変化を定義
# low_volatility: FE-1の ATR を流用（危機局面で weight_crisis に切り替え）
# profitability_rmw: Fama-French RMW（Robust Minus Weak）
# $factor\_return = \sum_{i \in long} r_i - \sum_{j \in short} r_j$

FACTOR_DEFINITIONS: dict = {
    "momentum_12m": {
        "source":        "score_history",
        "lookback_days": 252,
        "direction":     "higher_is_good",
        "weight_normal": 0.30,
        "weight_crisis": 0.15,   # 危機局面（VIX>30）で削減
        "description":   "12ヶ月スコアモメンタム（Fama-French UMD）",
    },
    "value_hml": {
        "source":        "screener",
        "lookback_days": 126,
        "direction":     "lower_per_is_good",
        "weight_normal": 0.25,
        "weight_crisis": 0.30,   # 危機局面でバリュー効果が強まる
        "description":   "バリューファクター（High Minus Low PBR逆数）",
    },
    "low_volatility": {
        "source":        "atr",   # FE-1 の ATR を流用
        "lookback_days": 60,
        "direction":     "lower_atr_is_good",
        "weight_normal": 0.20,
        "weight_crisis": 0.40,   # 危機局面で低ボラ効果が最大化
        "description":   "低ボラティリティファクター（ATR逆数ベース）",
    },
    "profitability_rmw": {
        "source":        "financials",
        "lookback_days": 252,
        "direction":     "higher_op_margin_is_good",
        "weight_normal": 0.25,
        "weight_crisis": 0.15,
        "description":   "収益性ファクター（Fama-French RMW - Robust Minus Weak）",
    },
}

# ファクター重みの初期値（rule_optimizer が EMA 更新する）
FACTOR_WEIGHTS_DEFAULT: dict = {
    "momentum_12m":      0.30,
    "value_hml":         0.25,
    "low_volatility":    0.20,
    "profitability_rmw": 0.25,
}

# ファクター重み集中度の上限（HHI）
# $HHI = \sum_{i} w_i^2 \leq 0.35$ を超えたら均等化方向にリバランス
FACTOR_HHI_MAX = 0.35

# ルックバック期間加重平均の減衰係数（F-3）
# 直近期間ほど高い重みを付与する指数減衰
# $w_t = exp(-\mu \times (T - t))$, $\mu = 0.05$
FACTOR_LOOKBACK_DECAY_MU = 0.05

# HMMレジーム連動縮退係数（F-5）
# regime="bear" or "crisis" 時に momentum_12m を縮退させる乗数
FACTOR_REGIME_SHRINK: dict = {
    "bull":   1.00,
    "normal": 1.00,
    "bear":   0.70,   # bear では momentum を 70% に縮退
    "crisis": 0.40,   # crisis では momentum を 40% に縮退（低ボラに移行）
}

# ── モジュール鮮度特性定義 ──────────────────────
# 各指標の遅行特性と先行性を定義し、鮮度係数計算の半減期パラメータを設定。
# $freshness\_weight = exp(-\ln(2) \times days / half\_life)$
# 注意: 現時点で本定数を参照するモジュールは無い（score_prior_manager.py は
#   SCORE_PRIOR_FRESHNESS_HALF_LIFE を使用）。鮮度係数をモジュール別に細分化する際の定義。
#   LEADING_INDICATOR_RULES（BLOCK R-2）と同名にすると後勝ちで上書きするので名前を変えないこと。

MODULE_FRESHNESS_RULES: dict = {
    "gov_pipeline": {
        "half_life_days": 14,
        "lead_lag":       "coincident",
        "weight_in_phase0": 0.15,
        "description":    "政府統計（e-Stat）: 概ね一致系指標",
    },
    "policy_tracker": {
        "half_life_days": 7,
        "lead_lag":       "leading",
        "weight_in_phase0": 0.20,
        "description":    "政策金利変化: 先行性高・即時影響",
    },
    "arxiv": {
        "half_life_days": 60,
        "lead_lag":       "leading",
        "weight_in_phase0": 0.05,
        "description":    "ArXiv学術論文: 長期先行指標・半減期長",
    },
    "news_pipeline": {
        "half_life_days": 3,
        "lead_lag":       "coincident",
        "weight_in_phase0": 0.10,
        "description":    "ニュースRSS: 即時性高・劣化も速い",
    },
    "edinet_rotation": {
        "half_life_days": 30,
        "lead_lag":       "lagging",
        "weight_in_phase0": 0.10,
        "description":    "EDINET有報: 遅行指標・四半期更新",
    },
    "screener": {
        "half_life_days": 7,
        "lead_lag":       "coincident",
        "weight_in_phase0": 0.15,
        "description":    "スクリーナー通過銘柄: 週次更新・鮮度重要",
    },
    "institutional_13f": {
        "half_life_days": 45,
        "lead_lag":       "lagging",
        "weight_in_phase0": 0.10,
        "description":    "13F機関投資家: 四半期開示・遅行指標",
    },
    "congress_monitor": {
        "half_life_days": 21,
        "lead_lag":       "leading",
        "weight_in_phase0": 0.10,
        "description":    "議会法案進行: 可決に近づくほど先行性上昇",
    },
    "segment_signal": {
        "half_life_days": 90,
        "lead_lag":       "lagging",
        "weight_in_phase0": 0.05,
        "description":    "セグメント収益性: 年次有報ベース・最遅行",
    },
}

# ── ALL_DIRS への追加フォルダ（setup_dirs() が一括作成する）────────────────
_WAVE4_DIRS = [
    CACHE_DIR / "institutional",   # institutional_history.json
    CACHE_DIR / "congress",        # congress_bill_cache.json
    CACHE_DIR / "segment",         # segment_mapper キャッシュ
    CACHE_DIR / "score_prior",     # score_prior_manager キャッシュ
    INSIGHT_DIR / "drift",         # drift_detector レポート
]
for _d in _WAVE4_DIRS:
    if _d not in ALL_DIRS:
        ALL_DIRS.append(_d)

# ==============================================================
# BLOCK Wave5: テキストシグナル定数
# ==============================================================

# ── テキストシグナルキャッシュ定数 ─────────────────
# TEXT_SIGNAL_CACHE_FILE : text_signal.py が生成するキャッシュ JSON ファイル名
# TEXT_SIGNAL_TTL_DAYS   : キャッシュ有効期間（日）
# TEXT_SIGNAL_FIRST_RUN_LIMIT : 初回実行時のテキスト抽出対象銘柄数の上限
# ENABLE_TEXT_SIGNAL     : False にすると text_signal を完全スキップ

TEXT_SIGNAL_CACHE_FILE       = "text_signal_cache.json"
TEXT_SIGNAL_TTL_DAYS         = 30
TEXT_SIGNAL_FIRST_RUN_LIMIT  = 50
ENABLE_TEXT_SIGNAL           = True
# 有報テキストのリスク減点（base_agent の edinet_text_risk_bonus）だけを止めるフラグ（当面無効）。
# ENABLE_TEXT_SIGNAL は text_extractor（edinet_exposure_extractor の代替経路も使う）を兼ねるので別にしてある。
# 無効の理由: ①前期比成分が仮実装で常に0 ②英語FinBERTに日本語を入力
# ③「事業等のリスク」欄のキーワード数で減点＝リスクを丁寧に書く会社ほど減点される。作り直すなら設計から。
ENABLE_TEXT_RISK_BONUS       = False

# ── リスクカテゴリ定義 ─────────────────────────────
# text_signal.py でリスクをカテゴリ別に分類するためのキーワードマップ。
# 各カテゴリのキーワードにヒットしたセクションのネガティブスコアを
# そのカテゴリに帰属させる。

TEXT_SIGNAL_RISK_CATEGORIES: dict = {
    "geopolitical": [
        "地政学", "輸出規制", "安全保障", "制裁", "関税",
        "米中", "ウクライナ", "台湾", "軍事", "紛争",
        "サプライチェーン", "経済安保", "輸入規制", "戦争リスク",
    ],
    "financial": [
        "流動性リスク", "信用リスク", "為替リスク", "金利リスク",
        "与信", "不良債権", "資金繰り", "債務超過", "損失",
        "引当金", "のれん減損", "減損リスク", "財務悪化",
        "負債", "デフォルト", "信用格付け", "格下げ",
    ],
}

# ── テキストシグナルスコア範囲 ─────────────────────
# YoY 差分スコアおよびリスクカテゴリスコアの上下限値（クランプ用）
# $text\_risk\_bonus \in [TEXT\_SIGNAL\_SCORE\_MIN, 0]$

TEXT_SIGNAL_SCORE_MIN  = -10.0   # ネガティブ方向上限（リスク検出）
TEXT_SIGNAL_SCORE_MAX  =   0.0   # テキストリスクはネガティブ側のみ

# ── policy_radar 実現度判定定数 ────────────────
# POLICY_RADAR_INTENT_CACHE_TTL_DAYS : policy_radar_cache の TTL（日）
# POLICY_RADAR_ENABLE_FINBERT_INTENT : FinBERT 文意分類の有効フラグ

POLICY_RADAR_INTENT_CACHE_TTL_DAYS    = 30
POLICY_RADAR_ENABLE_FINBERT_INTENT    = True

# FinBERT 文意分類: 3クラス判定の確信度閾値
# $p(確定) \geq INTENT\_CONFIRMED\_THRESHOLD$ → 確定
# $p(否決) \geq INTENT\_REJECTED\_THRESHOLD$  → 否決
# それ以外 → 検討中

POLICY_INTENT_CONFIRMED_THRESHOLD = 0.70
POLICY_INTENT_REJECTED_THRESHOLD  = 0.65

# 文意3分類ラベル
POLICY_INTENT_LABELS: dict = {
    "confirmed":   "確定",
    "considering": "検討中",
    "rejected":    "否決",
}

# 文意クラス別の実現度スコア乗数（既存 _SOURCE_WEIGHTS への補正係数）
# 「確定」ならスコアをそのまま使用、「検討中」は削減、「否決」はゼロ
POLICY_INTENT_MULTIPLIERS: dict = {
    "confirmed":   1.00,
    "considering": 0.50,
    "rejected":    0.00,
}

# ==============================================================
# BLOCK Wave6: 地政学・海外権益・リスク設定（G-N / G-C / G-X）
# ==============================================================

# ── G-N: 国別リスク監視設定 ──────────────────────────

# BigQuery プロジェクトID（geopolitics データセットの所在）
GEO_BQ_PROJECT_ID = "stock-analyzer-edinet"

# 国別リスク更新の有効フラグ
# False にすると run_all_tasks() での country_risk_updater 実行をスキップ
ENABLE_COUNTRY_RISK_UPDATE = True

# GDELT API スキップフラグ
# True にすると外務省RSS + 国務省RSS のみで composite_risk_score を算出
# GitHub Actions のネットワーク制限・レート制限時に使用
GEO_SKIP_GDELT = False

# 国別リスク高閾値（composite_risk_score がこの値以上で「高リスク」判定）
# $composite\_risk\_score \geq GEO_RISK_HIGH\_THRESHOLD$ → 高リスク
GEO_RISK_HIGH_THRESHOLD = 50.0

# 国別リスク中閾値（composite_risk_score がこの値以上で「中リスク」判定）
# $composite\_risk\_score \geq GEO_RISK_MED\_THRESHOLD$ → 中リスク
GEO_RISK_MED_THRESHOLD = 25.0

# 週次レポートに掲載する高リスク国の最大件数
GEO_REPORT_TOP_N_COUNTRIES = 10

# 国別リスクスコアのキャッシュ TTL（秒）
# 日次更新なので 86400 秒（24時間）以内は BQ 再取得しない
GEO_RISK_CACHE_TTL_SEC = 86400

# 監視対象シーレーン（リスク波及分析に使用）
# geo_signal.py がこのリストを参照して関係国を特定する
GEO_MONITORED_SEALANES: list = [
    "strait_of_hormuz",    # ホルムズ海峡（中東原油輸送の要衝）
    "strait_of_malacca",   # マラッカ海峡（アジア〜中東・欧州のシーレーン）
    "south_china_sea",     # 南シナ海（東アジア最重要航路）
    "suez_canal",          # スエズ運河（欧州〜アジア航路）
    "bab_el_mandeb",       # バブ・エル・マンデブ海峡（紅海入口）
    "taiwan_strait",       # 台湾海峡（東アジア安全保障の焦点）
    "cape_of_good_hope",   # 喜望峰（スエズ迂回路）
    "cape_horn",           # ホーン岬（南米迂回路）
    "arctic_route",        # 北極海航路（ロシア制裁時の代替）
]

# 監視対象資源コード（権益マッピング・シグナル計算に使用）
# jogmec_scraper / edinet_exposure_extractor がこのリストを参照
GEO_MONITORED_COMMODITIES: list = [
    "crude_oil",           # 原油
    "lng",                 # 液化天然ガス
    "natural_gas",         # 天然ガス
    "coal",                # 石炭
    "iron_ore",            # 鉄鉱石
    "copper",              # 銅
    "lithium",             # リチウム（EV電池材料）
    "cobalt",              # コバルト（EV電池材料）
    "nickel",              # ニッケル（EV電池材料）
    "rare_earth",          # レアアース
    "gold",                # 金
    "uranium",             # ウラン
    "aluminum",            # アルミニウム
    "platinum",            # 白金族
    "wheat",               # 小麦（食糧安全保障）
    "soybeans",            # 大豆（食糧安全保障）
    "palm_oil",            # パーム油
]

# ── G-C: 企業×海外権益マッピング設定 ──────────────────

# 日本株の権益マッピング優先ティッカーリスト
# edinet_exposure_extractor / jogmec_scraper が優先的に処理する
IMPORTANT_TICKERS_JP: list = [
    # エネルギー（石油・ガス権益保有企業）
    "5020.T",   # ENEOS ホールディングス
    "5019.T",   # 出光興産
    "1605.T",   # INPEX（旧国際石油開発帝石）
    "9531.T",   # 東京ガス
    "9532.T",   # 大阪ガス
    # 商社（多様な海外権益）
    "8058.T",   # 三菱商事
    "8053.T",   # 住友商事
    "8001.T",   # 伊藤忠商事
    "8002.T",   # 丸紅
    "8031.T",   # 三井物産
    # 素材・資源（鉱物権益）
    "5713.T",   # 住友金属鉱山
    "5711.T",   # 三菱マテリアル
    "5706.T",   # 三井金属鉱業
    "5707.T",   # 東邦亜鉛
    # 製造（海外生産拠点比率高）
    "7203.T",   # トヨタ自動車
    "7267.T",   # 本田技研工業
    "6758.T",   # ソニーグループ
    "6501.T",   # 日立製作所
    "6702.T",   # 富士通
    "6752.T",   # パナソニックホールディングス
]

# 米国株の権益マッピング優先ティッカーリスト
# sec_geographic_extractor が優先的に処理する
IMPORTANT_TICKERS_US: list = [
    # エネルギー
    "XOM",    # エクソンモービル
    "CVX",    # シェブロン
    "COP",    # コノコフィリップス
    "EOG",    # EOGリソーシズ
    "SLB",    # シュルンベルジェ
    # 素材・資源
    "FCX",    # フリーポート・マクモラン（銅）
    "NEM",    # ニューモント（金）
    "AA",     # アルコア（アルミ）
    "MP",     # MPマテリアルズ（レアアース）
    "ALB",    # アルベマール（リチウム）
    # テック（地政学リスク大）
    "NVDA",   # エヌビディア（中国・台湾依存）
    "QCOM",   # クアルコム（中国売上依存）
    "AMAT",   # アプライドマテリアルズ
    "LRCX",   # ラムリサーチ
    "ASML",   # ASMLホールディング（半導体製造装置）
]

# 権益マッピングの自動抽出信頼度の最低閾値
# この値未満の exposure_strength は BQ に格納しない
GEO_EXPOSURE_MIN_STRENGTH = 0.05
# BQ（edinet.text_analysis）に本文が無い会社だけ有報 HTML を取りに行く上限社数（1回の実行。1社ずつで遅く、月次の1ステップ900秒に収めるため）
GEO_EDINET_HTML_FALLBACK_MAX: int = 5

# exposure_review_ui での手動確認が必要な exposure_strength の上限
# この値未満は「要確認」フラグを立てる
GEO_EXPOSURE_REVIEW_THRESHOLD = 0.20

# EDINET有報テキスト抽出の対象セクション
# edinet_exposure_extractor がこのセクションリストを参照
GEO_EDINET_TARGET_SECTIONS: list = [
    "事業の概況",
    "主要な事業内容",
    "海外事業",
    "地域別事業概況",
    "リスク情報",
    "重要な会計上の見積り",
]

# SEC EDGAR 10-K の対象セクション（英語）
# sec_geographic_extractor がこのセクションリストを参照
GEO_SEC_TARGET_SECTIONS: list = [
    "Geographic Information",
    "Geographic Segments",
    "International Operations",
    "Geographic Areas",
    "Risk Factors",
    "Business Overview",
]

# ── G-C: スクレイパー実行制御 ─────────────────────

# JOGMEC スクレイパーの有効フラグ
# False にすると run_all_tasks() での jogmec_scraper 実行をスキップ
ENABLE_JOGMEC_SCRAPER = True

# JOGMEC スクレイパーの実行頻度（日数）
# 毎日ではなく月次相当（30日）で実行する
JOGMEC_SCRAPER_INTERVAL_DAYS = 30

# SEC Geographic Extractor の有効フラグ
ENABLE_SEC_GEO_EXTRACTOR = True

# SEC Geographic Extractor の実行頻度（日数）
SEC_GEO_EXTRACTOR_INTERVAL_DAYS = 30

# EDINET Exposure Extractor の有効フラグ
ENABLE_EDINET_EXPOSURE_EXTRACTOR = True

# EDINET Exposure Extractor の実行頻度（日数）
EDINET_EXPOSURE_INTERVAL_DAYS = 30

# exposure_review_ui のポート番号（Streamlit 起動時）
EXPOSURE_REVIEW_UI_PORT = 8502

# ── G-X: 地政学×権益統合シグナル設定 ──────────────────

# geo_impact_bonus の上限・下限（composite_score への加算点）
# $geo\_impact\_bonus \in [GEO\_IMPACT\_MIN, GEO\_IMPACT\_MAX]$
GEO_IMPACT_MAX = 15.0    # 地政学的追い風の最大加点
GEO_IMPACT_MIN = -15.0   # 地政学的逆風の最大減点

# composite_risk_score → geo_impact への変換係数
# リスクスコアが高いほど影響が大きくなる非線形変換に使用
# $raw\_impact = exposure\_strength \times risk\_score \times GEO\_IMPACT\_SCALE$
GEO_IMPACT_SCALE = 0.30

# 方向性係数テーブル
# 資源コード → 日本株への影響方向（+1=追い風 / -1=逆風）
# 例: 原油高は石油株に追い風だが、輸送・素材株には逆風
GEO_COMMODITY_DIRECTION_JP: dict = {
    # エネルギー資源
    "crude_oil":    {"1009": +1.0, "1021": -0.8, "1022": -0.6,
                     "1007": -0.5, "1004": -0.4, "1023": -0.7},
    "lng":          {"1009": +1.0, "1020": -0.5, "1007": -0.3},
    "natural_gas":  {"1009": +0.8, "1020": -0.5},
    "coal":         {"1009": +0.7, "1012": +0.3, "1020": -0.3},
    # 金属資源
    "iron_ore":     {"1012": +0.9, "1013": +0.3, "1015": -0.3,
                     "1017": -0.2},
    "copper":       {"1013": +1.0, "1016": -0.4, "1015": -0.3},
    "lithium":      {"1016": +0.8, "1017": +0.6, "1013": +0.4},
    "cobalt":       {"1016": +0.7, "1017": +0.5},
    "nickel":       {"1013": +1.0, "1016": +0.4, "1017": +0.3},
    "rare_earth":   {"1016": +0.9, "1018": +0.7, "1015": +0.5},
    "aluminum":     {"1013": +0.8, "1017": -0.3, "1016": -0.2},
    "platinum":     {"1007": +0.5, "1017": +0.4},
    # 農産物
    "wheat":        {"1004": -0.6, "1027": -0.4},
    "soybeans":     {"1004": -0.5, "1007": -0.3},
    "palm_oil":     {"1004": -0.4, "1007": -0.2},
    # その他
    "gold":         {"1028": -0.2},   # 金高は市場不安の代理変数
    "uranium":      {"1020": +0.5},   # 核燃料→電力に追い風
}

# 方向性係数テーブル（US株・GICSセクターETF別）
# 資源コード → US セクターETFへの影響方向
GEO_COMMODITY_DIRECTION_US: dict = {
    "crude_oil":    {"XLE": +1.0, "XLI": -0.5, "XLP": -0.3,
                     "XLY": -0.3, "XLB": -0.2},
    "lng":          {"XLE": +0.9, "XLU": -0.4},
    "natural_gas":  {"XLE": +0.8, "XLU": -0.3},
    "coal":         {"XLE": +0.6},
    "iron_ore":     {"XLB": +0.8, "XLI": +0.3},
    "copper":       {"XLB": +1.0, "XLK": -0.3, "XLI": -0.2},
    "lithium":      {"XLK": +0.7, "XLY": +0.5, "XLB": +0.6},
    "cobalt":       {"XLK": +0.6, "XLY": +0.4},
    "nickel":       {"XLB": +0.9, "XLY": +0.3},
    "rare_earth":   {"XLK": +1.0, "XLI": +0.5},
    "aluminum":     {"XLB": +0.7, "XLI": -0.2},
    "platinum":     {"XLB": +0.5},
    "wheat":        {"XLP": -0.5, "XLY": -0.3},
    "soybeans":     {"XLP": -0.4},
    "gold":         {"XLF": -0.3},
    "uranium":      {"XLU": +0.6},
}

# Phase T クラスター伝播（taxonomy_node の related_commodities 連鎖）
# 資源コード → 連鎖的に影響を受ける技術ノードIDリスト
# geo_signal.py がこのマップを参照してスコア伝播を実施する
GEO_TAXONOMY_PROPAGATION: dict = {
    "lithium":    ["ev_battery", "semiconductor_logic", "automotive"],
    "cobalt":     ["ev_battery", "aerospace"],
    "rare_earth": ["semiconductor_design", "wind_energy", "ev_motor"],
    "copper":     ["ev_battery", "semiconductor_equipment", "connector"],
    "nickel":     ["ev_battery", "stainless_steel"],
    "crude_oil":  ["petrochemical", "synthetic_fiber", "plastic"],
}

# 資源→技術ノードの伝播と、銘柄とノードの関与度の出どころ。
#   "legacy": 上の GEO_TAXONOMY_PROPAGATION と geo_signal の業種コード手書き表
#   "seed"  : 承認済み seed（data/taxonomy_seed）の related_commodities の逆引き、
#             関与度は BQ taxonomy.company_taxonomy_map（行の無い銘柄は手書き表）
GEO_TAXONOMY_SOURCE = "legacy"

# geo_signal の有効フラグ。True にすると base_agent.py と us_stock_screener.py が geo_impact_bonus を計算する。
# 権益側（company_country_exposure）は geo_exposure_runner.py が供給する。
# exposure が空の間も aggregate_geo_signal() は None を返し、signal_aggregator は w_geo=0.0 として
# 他のシグナルで再正規化するので、データが溜まるまでスコアは変わらない。
ENABLE_GEO_SIGNAL = True

# geo_signal の signal_aggregator での重み（DEFAULT_WEIGHTS に追加）
# signal_aggregator.py の DEFAULT_WEIGHTS 合計=1.00 を維持するため
# 既存ウェイトを按分削減して追加する（Step 16 で実装）
GEO_SIGNAL_WEIGHT = 0.04   # 4%（pearl_signal の 5% から 1% 移譲相当）

# signal_aggregator.DEFAULT_WEIGHTS の geo_signal 追加後の重みバランス確認メモ:
#   既存合計 = 1.00（Step 3 時点）
#   geo_signal 追加 = +0.04
#   pearl_signal 削減 = -0.01（5% → 4%）
#   sector_score 削減 = -0.01（26% → 25%）
#   macro_signals 削減= -0.01（15% → 14%）
#   us_lead_signal 削減= -0.01（5% → 4%）
#   → 調整後合計 = 1.00
# 上記の重みバランスは signal_aggregator.py の DEFAULT_WEIGHTS で実装する（Step 16）。


# ── G-N: ALL_DIRS への追加 ──────────────
_WAVE6_DIRS = [
    CACHE_DIR / "geopolitics",        # country_risk_updater キャッシュ
    INSIGHT_DIR / "geopolitics",      # 地政学レポート保存先
    INSIGHT_DIR / "geo_exposure",     # 権益マッピング結果
]
for _d in _WAVE6_DIRS:
    if _d not in ALL_DIRS:
        ALL_DIRS.append(_d)

# ==============================================================
# BLOCK 36: U-4 機関投資家 13F 監視 追加設定
# ==============================================================
# INSTITUTION_TIER_MAP / SCORE_MAX_INSTITUTION_ADJ / INSTITUTION_HISTORY_TTL_DAYS /
# INSTITUTION_MIN_NEW_POSITION_USD と ENABLE_INSTITUTIONAL_13F 等は BLOCK 35 で定義（ここで再定義しない）。

# ==============================================================
# BLOCK 37: I-1/I-3/I-4 オルタナティブデータ監視設定（求人票・役員転職・施設着工は廃止済み）
# ==============================================================

# ── I-1: 求人票モニター設定 ─────────────────────────────────
ENABLE_JOB_POSTING_MONITOR: bool = False  # 廃止（Wantedly検索件数で業種加点・質が低い）
JOB_POSTING_CACHE_TTL_DAYS: int  = 30     # hiring_signal_cache.json の TTL
JOB_POSTING_CACHE_PATH: str      = "hiring_signal_cache.json"  # CACHE_DIR 配下

# 技術キーワード定義（カテゴリ別）
# キーワードが求人票に含まれる数を集計し、前月比で急増を検知する
JOB_TECH_KEYWORDS: dict = {
    "ai_ml": [
        "機械学習", "深層学習", "LLM", "生成AI", "MLエンジニア",
        "Machine Learning", "Deep Learning", "AI Engineer", "Data Scientist",
        "強化学習", "コンピュータビジョン", "自然言語処理",
    ],
    "semiconductor": [
        "半導体", "VLSI", "FPGA", "ASIC", "RTL", "EDA",
        "プロセス開発", "前工程", "後工程", "パッケージング",
    ],
    "ev_battery": [
        "EV", "電気自動車", "リチウムイオン", "全固体電池",
        "BMS", "バッテリーマネジメント", "モーター制御",
    ],
    "biotech": [
        "バイオ", "創薬", "ゲノム", "mRNA", "細胞治療",
        "バイオインフォマティクス", "臨床試験",
    ],
    "dx_cloud": [
        "DX", "クラウドネイティブ", "Kubernetes", "DevOps",
        "SRE", "インフラ", "マイクロサービス",
    ],
}

# 急増判定閾値（前月比でこの倍率以上の求人数増加を「急増」と判定）
JOB_SURGE_THRESHOLD_RATIO: float = 1.5    # 1.5倍以上で急増

# 急増時の sector_boost 加算ポイント
JOB_SURGE_SCORE_BONUS: float     = 5.0

# キーワードカテゴリ → 対応 JP業種コード（JAPAN_SECTORS のキー）
JOB_SURGE_SECTOR_MAP: dict = {
    "ai_ml":        ["1016", "1025", "1033"],   # 電気機器・情報通信・サービス
    "semiconductor": ["1016", "1018"],           # 電気機器・精密機器
    "ev_battery":   ["1017", "1016"],            # 輸送機器・電気機器
    "biotech":      ["1008"],                    # 医薬品
    "dx_cloud":     ["1025", "1033"],            # 情報通信・サービス
}

# ── I-3: 役員転職トラッカー設定 ─────────────────────────────
ENABLE_EXECUTIVE_TRANSITION: bool = False  # W4: 政府EDINET直取得に依存するため停止
EXECUTIVE_CACHE_TTL_DAYS: int     = 90     # executive_transition_cache.json の TTL
EXECUTIVE_CACHE_PATH: str         = "executive_transition_cache.json"  # CACHE_DIR 配下

# 外部人材登用スキルキーワード定義
# 役員略歴にこのキーワードが含まれる場合、スキルカテゴリを付与する
EXECUTIVE_SKILL_KEYWORDS: dict = {
    "dx_transformation": [
        "CTO", "CDO", "最高デジタル責任者", "デジタル変革",
        "情報システム", "IT戦略", "DX推進",
    ],
    "global_expansion": [
        "グローバル", "海外事業", "国際", "Global",
        "海外赴任経験", "外資系出身",
    ],
    "financial_discipline": [
        "CFO", "最高財務責任者", "IR", "資本政策",
        "M&A", "投資銀行", "アナリスト出身",
    ],
    "turnaround": [
        "再建", "リストラクチャリング", "事業再生",
        "コスト削減", "構造改革",
    ],
}

# 外部人材登用1件あたりの sector_boost 加算ポイント
EXECUTIVE_NEW_HIRE_BONUS: float     = 4.0

# この人数以上の役員交代で「経営方針転換シグナル」とみなす閾値
EXECUTIVE_TRANSITION_THRESHOLD: int = 2

# ── I-4: 施設着工モニター設定 ───────────────────────────────
ENABLE_FACILITY_EXPANSION: bool   = False  # 廃止（取得元3つが301/404/403で取得0件）
FACILITY_CACHE_TTL_DAYS: int      = 180    # facility_project_cache.json の TTL
FACILITY_CACHE_PATH: str          = "facility_project_cache.json"  # CACHE_DIR 配下

# 対象とする最小施設面積（㎡）。これ未満の着工は小規模とみなし除外する
FACILITY_MIN_AREA_SQM: float      = 10000.0

# 施設種別キーワード → 対応 JP業種コード
# facility_expansion_monitor.py が着工情報のテキストをこのキーワードで分類する
FACILITY_SECTOR_MAP: dict = {
    "工場":          ["1015", "1016", "1017"],   # 機械・電気機器・輸送機器
    "物流":          ["1024", "1021"],           # 倉庫・陸運
    "データセンター": ["1025", "1016"],           # 情報通信・電気機器
    "研究所":        ["1007", "1008", "1016"],   # 化学・医薬品・電気機器
    "半導体":        ["1016", "1018"],           # 電気機器・精密機器
}

# 大規模着工1件あたりの sector_boost 加算ポイント
FACILITY_EXPANSION_BONUS: float   = 6.0

# 着工シグナルの有効期間（日）。この期間を超えたシグナルは自動失効
FACILITY_EXPANSION_TTL_DAYS: int  = 90

# ── I系: ALL_DIRS への追加 ─────────────────────────
_WAVE6_I_DIRS = [
    CACHE_DIR / "hiring",         # job_posting_monitor キャッシュ
    CACHE_DIR / "executive",      # executive_transition_tracker キャッシュ
    CACHE_DIR / "facility",       # facility_expansion_monitor キャッシュ
    INSIGHT_DIR / "hiring",       # 求人票シグナルレポート保存先
    INSIGHT_DIR / "executive",    # 役員転職レポート保存先
    INSIGHT_DIR / "facility",     # 施設着工レポート保存先
]
for _d in _WAVE6_I_DIRS:
    if _d not in ALL_DIRS:
        ALL_DIRS.append(_d)

# ==============================================================
# BLOCK 27: 緊急縮退モード（Emergency Contraction）設定
# ==============================================================
# 【設計方針】
#   ・緊急縮退フラグが立つと「全カテゴリのシグナル感度を一律に減算」する。
#   ・防御セクター（ディフェンシブ株）は縮退時でも相対スコアを維持する。
#   ・フラグ判定はマクロ閾値の複合条件で行い、False Positive を防ぐ。
#   ・この定数群は rule_optimizer.py / signal_aggregator.py のみが参照する。
# ==============================================================

# ── 27-A: 緊急縮退トリガー閾値 ───────────────────────────────
# rule_optimizer.py の evaluate_contraction_flag() が参照する。
# 条件を複数定義し、有効条件数が CONTRACTION_TRIGGER_COUNT 以上で発動する。

# VIX（恐怖指数）の緊急縮退トリガー値。この値以上で1条件成立。
CONTRACTION_VIX_THRESHOLD: float = 30.0

# 10年米国債利回り（%）の急騰トリガー。前週比でこの値(%)以上上昇で1条件成立。
CONTRACTION_US10Y_RISE_PCT: float = 0.30

# 市場幅スコア（breadth_score_jp / 0〜100）の下限。この値以下で1条件成立。
CONTRACTION_BREADTH_SCORE_MIN: float = 35.0

# PCR（プット/コール比）の上限。この値以上で1条件成立。
CONTRACTION_PCR_THRESHOLD: float = 1.40

# geo_risk（地政学リスクスコア）の上限。この値以上で1条件成立。
# risk_score は 0〜100（高いほどリスク大）。
CONTRACTION_GEO_RISK_SCORE: float = 70.0

# シグナルスコアの急落幅（前週比）。上位業種の平均が週次でこの値以上下落で1条件成立。
CONTRACTION_SCORE_DROP_THRESHOLD: float = 12.0

# 上記条件のうち何個以上成立したら縮退フラグを発動するか。
# Warning: 2条件以上 / Emergency: 4条件以上
CONTRACTION_WARNING_TRIGGER_COUNT: int  = 2
CONTRACTION_EMERGENCY_TRIGGER_COUNT: int = 4

# ── 27-B: 縮退モード別・スコア減算係数 ──────────────────────
# signal_aggregator.py の各カテゴリ重み付きスコアに乗算する係数。
# Normal=1.0（無補正）/ Warning=0.85 / Emergency=0.65。
# 防御セクターにはこの係数を適用しない（相対維持）。

CONTRACTION_MODE_COEFFICIENTS: dict = {
    "Normal":    1.00,   # 通常時: 補正なし
    "Warning":   0.85,   # 警戒時: シグナル感度を15%抑制
    "Emergency": 0.65,   # 緊急時: シグナル感度を35%抑制
}

# ── 27-C: カテゴリ別の縮退感度係数（全カテゴリに均一適用が基本） ──
# 特定カテゴリのシグナルを縮退時により強く・弱く減算したい場合に個別定義する。
# 値が 1.0 → モード係数をそのまま適用。1.0 より大 → より強く抑制。
# 例: etf_heat（テーマ株）は緊急時により強く抑制する。
CONTRACTION_CATEGORY_SENSITIVITY: dict = {
    "sector_score":         1.00,
    "macro_signals":        1.00,
    "technical_score":      1.00,
    "market_breadth":       1.00,
    "options_sentiment":    1.00,
    "anomaly_season":       1.00,
    "us_lead_signal":       1.00,
    "etf_heat":             1.20,   # テーマETF: 緊急時により強く抑制
    "yield_curve":          1.00,
    "pearl_signal":         1.00,
    "independence_signal":  1.00,
    "politician_signal":    1.00,
    "institutional_signal": 1.00,
    "geo_signal":           0.90,   # 地政学: 緊急時は情報価値が高い→やや緩く
    "cluster_phase_bonus":  1.10,
    "hiring_signal":        1.00,
    "exec_transition":      1.00,
    "facility_signal":      1.00,
}

# ── 27-D: 防御セクター（ディフェンシブ）定義 ─────────────────
# リスクオフ局面でスコアを相対的に維持・保護する業種コード。
# JP33業種コード（JAPAN_SECTORS の key と対応）で定義する。

DEFENSIVE_SECTOR_CODES_JP: list = [
    "1007",  # 化学（医薬品原料）
    "1008",  # 医薬品
    "1026",  # 電力・ガス
    "1022",  # 陸運
    "1023",  # 海運（生活必需品輸送）
    "1024",  # 倉庫・運輸関連
    "1009",  # ゴム製品（タイヤ等・インフラ系）
    "1028",  # 小売業（食品スーパー系）
    "1029",  # 銀行業（メガバンク限定: 安定配当）
]

# 米国GICSセクター（us_stock_screener.py 参照用）
DEFENSIVE_GICS_SECTORS_US: list = [
    "Consumer Staples",      # 生活必需品
    "Health Care",           # ヘルスケア
    "Utilities",             # 公益事業
    "Communication Services",# 通信（防衛的性質）
]

# 防御セクター判定用キーワード（sector_name_ja / gics_sector の文字列マッチ）
DEFENSIVE_SECTOR_KEYWORDS: list = [
    "医薬品", "電力", "ガス", "食料", "食品", "生活",
    "公益", "通信", "インフラ", "保険", "Healthcare",
    "Utilities", "Staples", "Pharma",
]

# ── 27-E: 防御セクター保護ロジック設定 ───────────────────────
# 緊急縮退時、防御セクターのスコアに適用する「保護乗数」。
# 1.0 → 縮退係数を無効化（スコアを元のまま維持）。
# 1.0 より大 → 相対的に上昇（防御株の相対優位を表現）。
DEFENSIVE_PROTECTION_MULTIPLIER: float = 1.00   # 縮退しない（中立維持）

# Warning時に防御セクターへ加算する相対ボーナスポイント（0〜10）。
DEFENSIVE_RELATIVE_BONUS_WARNING: float   = 3.0

# Emergency時に防御セクターへ加算する相対ボーナスポイント（0〜15）。
DEFENSIVE_RELATIVE_BONUS_EMERGENCY: float = 7.0

# ── 27-F: 縮退モード判定結果の格納キー（results dict内） ──────
# rule_optimizer.run_rule_optimizer() の出力が results[このキー] に格納される。
CONTRACTION_RESULT_KEY: str = "rule_optimizer"

# ── 27-G: 縮退状態の履歴保存パス ─────────────────────────────
# rule_optimizer.py が縮退状態の時系列をここに追記する（JSON Lines形式）。
CONTRACTION_HISTORY_PATH: str = "contraction_history.jsonl"  # CACHE_DIR配下

# ── 縮退モード: ALL_DIRS への追加 ─────────────────
_WAVE6_C_DIRS = [
    CACHE_DIR / "contraction",   # 縮退履歴・縮退フラグキャッシュ保存先
]
for _d in _WAVE6_C_DIRS:
    if _d not in ALL_DIRS:
        ALL_DIRS.append(_d)

# ==============================================================
# BLOCK Wave7: データ収集・高度アルファ計算統合層
# ==============================================================

# ── 標準化動向トラッカー（industry_standard_tracker.py）─

# standardization_cache.json の TTL（日）
STANDARDIZATION_CACHE_TTL_DAYS: int = 180

# 標準化トレンド判定の閾値（contribution 総数）
STANDARDIZATION_TREND_THRESHOLDS: dict = {
    "rising":   200,   # 200件以上 → rising
    "stable":    50,   # 50〜200件 → stable
    "declining":  0,   # 50件未満  → declining
}

# 追跡対象の標準化機関・リリース・トピック設定
# industry_standard_tracker.py の _DEFAULT_STANDARDS_CONFIG を上書きする場合に使用
# None の場合はモジュール側のデフォルト定数が使われる
STANDARDS_TRACKING_CONFIG: dict | None = None  # 本番時は辞書を設定

# ── 学術カンファレンス追跡（academic_conf_tracker.py）──

# academic_conf_cache.json の TTL（日）
ACADEMIC_CONF_CACHE_TTL_DAYS: int = 180

# 追跡対象カンファレンス設定
# None の場合はモジュール側のデフォルト定数が使われる
ACADEMIC_CONF_CONFIG: dict | None = None  # 本番時は辞書を設定

# ── 中期経営計画トラッカー（midterm_plan_tracker.py）──

# midterm_plan_cache.json の TTL（日）
MIDTERM_TRACKER_TTL_DAYS: int = 90

# MIDTERM_ACHIEVEMENT_HIGH / MIDTERM_ACHIEVEMENT_RISK は BLOCK I-5 で定義。

# 中計目標定義（ticker → plan_def 辞書）
# None の場合はモジュール側の _DEFAULT_MIDTERM_PLANS が使われる
MIDTERM_PLAN_TARGETS: dict | None = None  # 本番時は実際の中計目標を設定

# ── 規模別シグナル減衰管理（size_decay_manager.py）────

# 古いシグナルは TTL で捨てずに指数減衰させる: calc_decay_factor() が days_elapsed から
#   decay = e^(-λt) を計算する（Tier別λは下の SIZE_DECAY_LAMBDA）。一律に捨てると減衰履歴ごと失われる。

# Tier 別減衰定数 λ（$decay = e^{-\\lambda t}$）
SIZE_DECAY_LAMBDAS: dict = {
    "Large": 0.15,
    "Mid":   0.10,
    "Small": 0.07,
    "Micro": 0.05,
    "Nano":  0.03,
}

# NANO_EVALUATION_MAX_PER_DAY は BLOCK 25 で定義。

# ── 規模帯横断シグナル（cross_size_signal.py）──────────

# cross_size_signal_cache.json の TTL（日）
CROSS_SIZE_SIGNAL_TTL_DAYS: int = 3

# ── 市場独立性スコア（independence_score_calculator.py）

# independence_scores.json の TTL（日）
INDEPENDENCE_SCORE_TTL_DAYS: int = 30

# 1回の実行で処理する最大銘柄数
INDEPENDENCE_MAX_TICKERS_PER_RUN: int = 200

# ローリング回帰ウィンドウ（日数）
INDEPENDENCE_ROLLING_WINDOW_DAYS: int = 60

# 独立性スコアのティア閾値
INDEPENDENCE_TIER_THRESHOLDS: dict = {
    "HIGH": 65.0,
    "MID":  40.0,
}

# ── 潜伏銘柄管理（latent_pearl_manager.py）────────────

# latent_pearl_cache.json の TTL（日）
LATENT_PEARL_TTL_DAYS: int = 1  # 日次で再計算する

# 潜伏Pearl → 通常Pearl への昇格閾値
LATENT_PEARL_PROMOTION_THRESHOLD: float = 70.0

# 潜伏Pearlスコア計算の重み（合計 1.0）
LATENT_PEARL_SCORE_WEIGHTS: dict = {
    "independence": 0.35,
    "cross_signal": 0.30,
    "catalyst":     0.20,
    "midterm":      0.15,
}

# ── Pearl Score D3統合定数（pearl_score_calculator.py）

# D3 への EDINET セグメント文脈ボーナス上限
D3_EDINET_SEGMENT_BONUS_MAX: float = 15.0

# D3 への中計スコア連携ボーナス上限
D3_MIDTERM_BONUS_MAX: float = 10.0

# D3 へのカタリストフラグ連携ボーナス上限
D3_CATALYST_BONUS_MAX: float = 8.0

# 潜伏Pearl昇格銘柄への pearl_score 上乗せ上限
LATENT_PEARL_PROMOTION_BONUS_MAX: float = 8.0

# ── ALL_DIRS へのキャッシュディレクトリ追加 ───

_WAVE7_DIRS = [
    CACHE_DIR,   # standardization_cache / academic_conf_cache /
                 # midterm_plan_cache / size_decay_state /
                 # nano_evaluation_queue / cross_size_signal_cache /
                 # independence_scores / latent_pearl_cache
                 # はすべて CACHE_DIR 直下に配置するため追加不要
                 # （CACHE_DIR は既に ALL_DIRS に含まれている）
]
# CACHE_DIR が既に ALL_DIRS に含まれていることを確認して追加
for _d in _WAVE7_DIRS:
    if _d not in ALL_DIRS:
        ALL_DIRS.append(_d)

# ── WEEKLY_MODULES / DAILY_MODULES の実行順序の仕様 ────────

# DAILY_MODULES 実行順（size_decay → cross_size → independence → latent_pearl）
WAVE7_DAILY_MODULE_ORDER: list = [
    "size_decay_manager",
    "cross_size_signal",
    "independence_score_calculator",
    "latent_pearl_manager",
]

# WEEKLY_MODULES 実行順（中計 → 学術 → 標準化）。実装が正（pipeline_registry_gen --audit が整合を検査）。
#   midterm_plan_tracker は screener 段で走る。中計達成率は pearl_score_calculator の D3ボーナス
#   （ENABLE_D3_MIDTERM_INTEGRATION）が参照するため screener より前に確定している必要がある。
#   残る2本は signal_aggregator 段。
WAVE7_WEEKLY_MODULE_ORDER: list = [
    "midterm_plan_tracker",
    "academic_conf_tracker",
    "industry_standard_tracker",
]

# ── Feature Flag（データ収集・高度アルファ層のON/OFF）─────
#   size_decay_manager / cross_size_signal は DAILY_MODULES の "size_tier" キーで動く。
#   【スコアへの影響】ON のとき以下が composite/pearl スコアに効く:
#     - independence_score / cross_size_signal → signal_aggregator のカテゴリ
#     - latent_pearl 昇格ボーナス（ENABLE_LATENT_PEARL_BONUS 経由・最大+8pt）
#     - midterm 達成率 → D3ボーナス（ENABLE_D3_MIDTERM_INTEGRATION 経由）
#   いずれも上流データが溜まるまでは 0.0。

# 標準化トラッカー有効フラグ
ENABLE_STANDARDIZATION_TRACKER: bool = True

# 学術カンファレンス追跡有効フラグ
ENABLE_ACADEMIC_CONF_TRACKER: bool = True

# 中計トラッカー有効フラグ
ENABLE_MIDTERM_PLAN_TRACKER: bool = True

# 規模別減衰管理有効フラグ
ENABLE_SIZE_DECAY_MANAGER: bool = True

# 規模帯横断シグナル有効フラグ
ENABLE_CROSS_SIZE_SIGNAL: bool = True

# 市場独立性スコア有効フラグ
ENABLE_INDEPENDENCE_SCORE: bool = True

# 潜伏銘柄管理有効フラグ
ENABLE_LATENT_PEARL_MANAGER: bool = True

# ── D3統合・潜伏Pearlボーナスの有効フラグ ──
#   pearl_score_calculator.py が読む。False にするとボーナスが止まる。
#   各ボーナスは上流データが無ければ 0.0（ENABLE_INDEPENDENCE_SCORE / ENABLE_MIDTERM_PLAN_TRACKER 等）。

# D3 EDINET セグメント統合有効フラグ
ENABLE_D3_EDINET_SEGMENT: bool = True

# D3 中計連携有効フラグ
ENABLE_D3_MIDTERM_INTEGRATION: bool = True

# D3 カタリスト連携有効フラグ
ENABLE_D3_CATALYST_INTEGRATION: bool = True

# 潜伏Pearl昇格ボーナス有効フラグ
ENABLE_LATENT_PEARL_BONUS: bool = True
# ==============================================================
# BLOCK 38: 観測性 — モジュール実行台帳設定（OBS-1）
# ==============================================================
# module_ledger.py が参照する。日次の全モジュール成否を台帳化し、
# 連続失敗・配線漏れ（記録なし）を可視化する。
# 背景: 2026-06 の全数バグ掃討で発見された「サイレント失敗」の再発防止。

ENABLE_MODULE_LEDGER: bool = True
LEDGER_CONSEC_FAIL_DAYS: int = 3    # この日数以上の連続失敗で Gmail アラート
LEDGER_HISTORY_DAYS: int = 14       # 連続失敗・最終成功日の遡及日数
LEDGER_UNRECORDED_DAYS: int = 2     # DAILY_MODULES の「記録なし」がこの日数続いたら Gmail アラート（配線漏れの疑い）
# 別ワークフロー（record_external）の記録が来なくなったら気づくための許容日数（土日・祝日の休みを含めて数える）
# daily_run.yml の外側ステップ（module_ledger.py steps で記録）は日曜に止まるので土→月の2日を許す。
# daily_state_push は commit 直前に記録して一緒に push する。push に失敗した日は記録ごと消えるので「来ない」で気づく。
LEDGER_EXTERNAL_EXPECT: dict = {
    "price_universe_jp": 4,
    "price_universe_us": 4,          # 米国株は火〜土
    "us_company_facts_update": 4,
    "report_store": 3,
    "bi_export": 3,
    "daily_state_push": 3,
}

# 運用の監視（ops_health.py → 日次メール「🩺 運用の監視」）。run_all_tasks の外で止まるものを見る。
ALERT_ON_OPS_HEALTH: bool = True
OPS_ACTIONS_MONTHLY_MINUTES: int = 0       # Actions の月の分数の上限。0＝見ない（public リポジトリは上限なし。private に戻すなら Free は 2000）
OPS_ACTIONS_WARN_RATIO: float = 0.8        # 使用量がこの割合を超える（または月末見込みが枠を超える）と警告
OPS_MAC_SYNC_STALE_HOURS: int = 48         # Mac の同期（obsidian_sync all）の最終成功からの許容時間
OPS_WEEKLY_LOOP_STALE_DAYS: int = 9        # 週次ループ（日曜21時）の最終成功からの許容日数
OPS_HOLDINGS_STALE_DAYS: int = 14          # 保有銘柄（家計簿締め）の as_of からの許容日数
OPS_BI_MART_STALE_DAYS: int = 3            # bi.m_screener（bi/manifest.json の generated_at）の許容日数。日曜休みで土→月は2日
OPS_DELIB_GRACE_DAYS: int = 2              # 審議の間隔 DELIBERATION_INTERVAL_DAYS に足す猶予
OPS_DELIB_CLAIM_STALE_HOURS: int = 24      # claimed / running のまま放置とみなす時間
OPS_DELIB_ROUTINE_START: str = "2026-10-18"  # クラウド審議 routine の初回予定日（これより前の空白は数えない）

# ==============================================================
# BLOCK 39: 投資部（マルチエージェント審議）連携設定（DEPT-1）
# ==============================================================
# agent_packet_exporter.py が参照する。SPEC_investment_dept.md 参照。
# vault パスはローカル実行専用（GitHub Actions では使用しない）。

ENABLE_INVESTMENT_DEPT: bool = True
# ── 隔週審議の間隔 ──────────────────────────
# deliberation_scheduler.py が参照。launchd は毎週日曜20:00に起動するが、
# 実際に走らせるかはこの間隔で判定する（launchd に隔週指定が無いため）。
# 前回実行日は stock_data/data/deliberation_state.json に残る。
DELIBERATION_INTERVAL_DAYS: int = 14

# ── 審議キュー ────────────────────────────────
# 日次・週次メールに「次に審議へ上げる候補」を何件出すか。
# 先頭1件が隔週ジョブ（deliberation_scheduler）で実際に選定される銘柄。
DELIBERATION_QUEUE_TOP_N: int = 5
ALERT_ON_DELIBERATION_QUEUE: bool = True
# 実働の正本はローカル側 Vault2_ClaudeCode_Local（iCloud側 Vault2_ClaudeCode は凍結・書込禁止）。
INVESTMENT_DEPT_VAULT_DIR: str = os.getenv(
    "INVESTMENT_DEPT_VAULT_DIR",
    os.path.join(os.path.expanduser("~"), "Documents/Obsidian_Local/Vault2_ClaudeCode_Local/"
                 "CEO室/departments/B-investment"),
)

# ── BLOCK 38b: パイプライン実行フラグ（stock_analyzer_main / 各モジュールが読む）─────
ENABLE_DEEP_DIVE_TRIGGER: bool = True      # 深掘りトリガー [S3-4]
ENABLE_TECHNOLOGY_MAPPER: bool = True      # 技術マッピング [B T-1]
ENABLE_CLUSTER_ENGINE: bool = True         # クラスターエンジン [B T-2]
ENABLE_EMERGING_DETECTOR: bool = True      # 萌芽テーマ検知 [B T-3]
ENABLE_CLUSTER_GRAPH_EXPORT: bool = True   # クラスター結果出力 [B T-4]

# ── BLOCK 39b: オントロジー層 設定 ──
# ontology_exporter.py が参照。週次（WEEKLY_MODULES "ontology_export"）で
# エンティティグラフを BQ `ontology` データセットへ追記する。
# BQ認証なし環境では JSONL ローカル出力に自動フォールバック。
# 初回のみ ontology_bq_schema.py を手動実行しておくこと。
ENABLE_ONTOLOGY_EXPORT: bool = True

# ==============================================================
# BLOCK 40: イベントホライズン（事前アラート）設定
# ==============================================================
# event_horizon_scanner.py / catalyst_calendar.py が参照する日程表。日程改定時はここだけを編集する。

FED_FOMC_DATES: list = [
    "2026-01-29", "2026-03-19", "2026-05-07",
    "2026-06-18", "2026-07-30", "2026-09-17",
    "2026-10-29", "2026-12-17",
]
BOJ_MEETING_DATES: list = [
    "2026-01-24", "2026-03-19", "2026-04-30",
    "2026-06-17", "2026-07-31", "2026-09-19",
    "2026-10-30", "2026-12-19",
]
US_CPI_DATES: list = [
    "2026-01-15", "2026-02-12", "2026-03-12",
    "2026-04-10", "2026-05-13", "2026-06-11",
    "2026-07-15", "2026-08-13", "2026-09-11",
    "2026-10-15", "2026-11-12", "2026-12-11",
]
US_GDP_DATES: list = [
    "2026-01-29", "2026-04-29", "2026-07-30", "2026-10-29",
]
BOJ_TANKAN_DATES: list = [
    "2026-04-02", "2026-07-02", "2026-10-01", "2027-01-05",
]
PMI_DATES: list = [
    "2026-01-05", "2026-02-02", "2026-03-02",
    "2026-04-01", "2026-05-04", "2026-06-01",
    "2026-07-01", "2026-08-03", "2026-09-01",
    "2026-10-01", "2026-11-02", "2026-12-01",
]
# 米雇用統計（原則第1金曜）
US_NFP_DATES: list = [
    "2026-01-09", "2026-02-06", "2026-03-06",
    "2026-04-03", "2026-05-01", "2026-06-05",
    "2026-07-02", "2026-08-07", "2026-09-04",
    "2026-10-02", "2026-11-06", "2026-12-04",
]

ENABLE_EVENT_HORIZON: bool = True
EVENT_HORIZON_ALERT_DAYS: int = 5      # この日数前からアラート開始（毎日カウントダウン）
EVENT_HORIZON_EARNINGS_TTL_DAYS: int = 3   # 決算日キャッシュTTL（yfinance呼び出し抑制）

# ==============================================================
# BLOCK 41: オプション戦略アドバイザー設定
# ==============================================================
# options_strategy_advisor.py が参照。
# 投資部ガードレール準拠: 提案は defined-risk（買い）または
#   カバード戦略（covered call / cash-secured put）のみ。
#   裸売り・ショートは提案禁止（B-investment 鉄則8と同期）。

ENABLE_OPTIONS_ADVISOR: bool = True
OPTIONS_INDEX_ETFS: list = ["SPY", "QQQ"]   # 常時監視する指数ETF
OPTIONS_MAX_UNIVERSE: int = 10              # yfinanceチェーン取得の上限銘柄数
# 実行時刻（米国時間外）では ATM の IV が取れないため、HV の過去1年分布での位置で環境を判定する。
OPTIONS_HV_RICH_PCTL: float = 70.0          # HVがこの分位以上 → 高ボラ＝プレミアム売り環境
OPTIONS_HV_CHEAP_PCTL: float = 30.0         # HVがこの分位以下 → 低ボラ＝プレミアム買い環境
OPTIONS_HV_WINDOW_DAYS: int = 20            # ヒストリカル・ボラティリティ算出窓
OPTIONS_MIN_SCORE_FOR_CSP: float = 70.0     # cash-secured put 提案の最低composite

# ==============================================================
# BLOCK 44: 出口戦略エンジン設定
# ==============================================================
# exit_rule_engine.py が参照。投資哲学.md の売却ルールのコード化。
# 発注は常に利用者が手動（D-01）。本エンジンは注文票生成まで。

ENABLE_EXIT_ENGINE: bool = True
EXIT_ONKABU_FIRE_PCT: float = 200.0   # R-A: 恩株化発火（+200%=3倍）
EXIT_ONKABU_NEAR_PCT: float = 180.0   # R-A: 接近通知
EXIT_SHORT_FIRE_PCT:  float = 20.0    # R-S: 短期(<6ヶ月)利確発火
EXIT_SHORT_NEAR_PCT:  float = 15.0    # R-S: 接近通知
EXIT_MID_FIRE_PCT:    float = 100.0   # R-M: 中期(1-2年)2倍発火
EXIT_MID_NEAR_PCT:    float = 90.0    # R-M: 接近通知
EXIT_RULE40_TH:       float = 40.0    # R-40: Rule of 40 閾値（SaaS）
EXIT_RULE40_NEAR_TH:  float = 45.0    # R-40: 接近通知
EXIT_PBR_FIRE:        float = 1.2     # R-PBR: 段階利確開始
EXIT_PBR_FULL:        float = 1.5     # R-PBR: 全売却推奨
EXIT_PBR_NEAR:        float = 1.1     # R-PBR: 接近通知
EXIT_STORY_DROP_FIRE_PCT: float = 40.0  # R-STORY: composite低下率発火
EXIT_STORY_DROP_NEAR_PCT: float = 30.0  # R-STORY: 接近通知

# ==============================================================
# BLOCK 53: データ信頼性・鮮度SLA設定
# ==============================================================
# data_reliability.py が参照。J-Quants不使用・全て無料
# （yfinanceリトライ → Stooqフォールバック → キャッシュ）。

ENABLE_DATA_SLA: bool = True
# usデータセットへのBQ格納
ENABLE_US_BQ_EXPORT: bool = True
ENABLE_US_FUNDAMENTALS_BQ: bool = True
US_FUNDAMENTALS_BQ_TABLE: str = "fundamentals"
# Notion連携は廃止（Obsidian一本化）。notion_obsidian_writer.py の Notion API 呼び出しのみ停止（Obsidian出力は継続）。
ENABLE_NOTION_API: bool = False
STOOQ_ENABLED: bool = True
DATA_RETRY_SCHEDULE: list = [2, 4, 8]        # リトライ間隔（秒・指数バックオフ）
EDINET_RETRY_DAILY_LIMIT: int = 10           # 繰越キューの1日処理上限（予算80コール/日を保護）
DATA_SLA_DAYS: dict = {                      # 種別ごとの鮮度SLA（日）
    "price": 2,
    "financials": 100,
    "news": 3,
}

# ==============================================================
# BLOCK 42: 統合バックテスト基盤設定
# ==============================================================
# backtest/ パッケージが参照。
# 共通スキーマ②「仮想約定」: 約定価格 = 翌営業日始値 × (1±slippage)。

BT_SLIPPAGE_BPS_JP: int = 20         # 日本株スリッページ（bps）
BT_SLIPPAGE_BPS_US: int = 10         # 米国株スリッページ（bps）
BT_TAX_RATE: float = 0.20315         # 特定口座の譲渡益課税率（NISAは0）
BT_COMMISSION_BPS: int = 0           # 手数料（大手ネット証券の国内現物無料化を既定）
BT_WF_SPLITS: int = 4                # walk-forward 分割数（expanding window）
BT_BOOTSTRAP_N: int = 500            # circular block bootstrap の反復回数 B
BT_BOOTSTRAP_BLOCK: int = 20         # ブロック長（営業日）
BT_P_THRESHOLD: float = 0.10         # p>=0.10 →『統計的裏付けなし』朱記
BT_SHORT_STOCK_LOAN_RATE: float = 0.011   # 貸株料 年1.1%（日割り）
BT_SHORT_NEGATIVE_RATE: float = 0.04      # 逆日歩 保守的に年4%固定
BT_BENCHMARK_TICKER: str = "1306.T"       # 受入テスト用ベンチマーク（TOPIX ETF）

# ==============================================================
# BLOCK 43: ペーパートレード仮想口座設定
# ==============================================================
# paper/paper_broker.py が参照。全イベントは events.jsonl に追記し
# 口座はイベントから再構築可能（イベントソーシング）。

ENABLE_PAPER_TRADING: bool = True
PAPER_INITIAL_CAPITAL: int = 2_000_000    # 戦略別の初期仮想資金（円）
PAPER_STRATEGIES: list = ["screener_topN", "ignition", "pead"]
# S6(commodity)・S4(options)は対象外。
# playbooks/*.yaml のイベント戦略は paper_broker が adapter 登録済みのものを自動で対象に加える
#   （ここに書かなくてよい。YAMLを置けば口座ができる）。
PAPER_INCLUDE_PLAYBOOK_STRATEGIES: bool = True
PAPER_LEDGER_PATH: str = "stock_data/paper"   # accounts.json / events.jsonl の置き場

# ==============================================================
# BLOCK 48: ポジションサイジングv2設定
# ==============================================================
# position_sizer.py の calc_position_size_v2 が参照。σターゲット=15%。

VOL_TARGET_ANNUAL: float = 0.15      # ポートフォリオ年率ボラ上限（D-15）
CONVICTION_MULT: dict = {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.25, 5: 1.5}
CORR_PENALTY_TH: float = 0.6         # 平均60日相関がこれを超えたらペナルティ
CORR_PENALTY_MULT: float = 0.7       # 相関ペナルティ係数
MAX_SINGLE_POS_PCT: float = 10.0     # 1銘柄上限（総資産比%）
CASINO_SINGLE_POS_PCT: float = 25.0  # カジノ枠戦略の1銘柄上限（カジノ枠比%）
CASINO_BUDGET_PCT: float = 15.0      # カジノ枠 = min(総資産×15%, 200万)（D-13）
CASINO_BUDGET_CAP_YEN: int = 2_000_000

# ==============================================================
# BLOCK 55: taxonomy信号設定
# ==============================================================
# taxonomy_hotness.py（node_hotness算出）と signal_aggregator.py（カテゴリV）が参照。
# ※ BLOCK 54 は S12信用ユニット用に予約。

ENABLE_TAXONOMY_SIGNAL: bool = True
TAXONOMY_HOTNESS_BASELINE_DAYS: int = 20   # 加熱判定のベースライン日数
TAXONOMY_LEADING_ONLY_TH: float = 65.0     # 「先行だけ点灯」の先行閾値
TAXONOMY_MARKET_DEFAULT: str = "JP"        # T-3 get_company_taxonomy_scores の既定market
# 業種スコアの関与度加重に使う、1業種あたりの参照銘柄数
TAXONOMY_SECTOR_TICKER_LIMIT: int = 10

# ── taxonomy 台帳の定期投入 ────────────────
# taxonomy_loader.py と taxonomy_company_mapper.py の有効フラグ。
# 週次（月曜）に daily_run.yml から実行する。どちらも node_id /
# (company_id, node_id) キーの MERGE で冪等。
# taxonomy_loader は _meta.status が APPROVED のバッチしか投入しないため、
# 自動実行でも未承認データが入ることはない。
ENABLE_TAXONOMY_LOADER: bool = True
ENABLE_TAXONOMY_COMPANY_MAPPER: bool = True

# ==============================================================
# BLOCK 49: ドローダウン制御設定
# ==============================================================
# dd_controller.py が参照。売却強制なし（推奨のみ・D-01）。

ENABLE_DD_CONTROL: bool = True
DD_CAUTION: float = 0.15        # NORMAL→CAUTION 発火（dd>=15%）
DD_DEFENSE: float = 0.25        # CAUTION→DEFENSE 発火（dd>=25%）
DD_CAUTION_EXIT: float = 0.10   # CAUTION→NORMAL 復帰（dd<10%・ヒステリシス）
DD_DEFENSE_EXIT: float = 0.20   # DEFENSE→CAUTION 復帰（dd<20%・ヒステリシス）

# ==============================================================
# BLOCK 52: 税・NISA最適化設定
# ==============================================================
# tax_optimizer.py が参照。税務の最終判断は利用者（レポートに明記）。

ENABLE_TAX_OPTIMIZER: bool = True
TAX_RATE_NISA: float = 0.20315        # 特定口座の譲渡益課税率（NISAは非課税=0）
OLD_NISA_EXPIRY_YEAR: int = 2040      # 旧NISA非課税期限（この年まで）
NISA_GROWTH_ANNUAL_LIMIT: int = 2_400_000   # 新NISA 成長投資枠 年間上限
NISA_GROWTH_TOTAL_LIMIT: int = 12_000_000   # 新NISA 成長投資枠 生涯上限
NISA_LONG_HOLD_MONTHS: int = 12       # NISA推奨の想定保有下限（>1年）

# ==============================================================
# BLOCK 45: イベントドリブン短期戦略設定
# ==============================================================
# event_strategy_runner.py が参照。プレイブックはYAML（playbooks/）で外部化。発注は利用者手動（D-01）。

ENABLE_EVENT_STRATEGY: bool = True
PLAYBOOK_DIR: str = "playbooks"
PROMOTION_MIN_TRADES: int = 30      # 実弾昇格M2: 取引数下限
PROMOTION_MIN_PF: float = 1.3       # 実弾昇格M2: ProfitFactor下限
PROMOTION_MAX_P: float = 0.10       # 実弾昇格M2: bootstrap p上限

# ==============================================================
# BLOCK 46: コモディティ/マクロ・ローテーション設定
# ==============================================================
# commodity_rotation.py が参照（月初実行）。東証ETF・無料データで完結。配分≤総資産10%。

ENABLE_COMMODITY_ROTATION: bool = True
CMD_MAX_ALLOC_PCT: float = 10.0    # コモディティ配分の総資産比上限
CMD_MOM_LOOKBACK: tuple = (252, 21)  # 12-1モメンタム（約12ヶ月, 直近1ヶ月）
# ユニバース: {ticker: カテゴリ}（実装時に上場状況を再確認・差し替え可）
CMD_ETF_UNIVERSE: dict = {
    "1326.T": "gold",       # SPDRゴールド・シェア
    "1540.T": "gold",       # 純金上場信託
    "1671.T": "energy",     # WTI原油price連動
    "1684.T": "broad",      # 総合コモディティ（穀物含む想定・要確認）
    "1687.T": "grains",     # 穀物系（上場状況を実装時に再確認）
}
# economic_cycle 4象限 → 許可カテゴリ（他象限=現金・エントリーなし）
# A:成長↑インフレ↑ / B:成長↑インフレ↓ / C:成長↓インフレ↑ / D:成長↓インフレ↓
CMD_REGIME_ALLOW: dict = {
    "A": ["energy", "grains", "broad"],   # インフレ↑成長↑: エネルギー・穀物
    "C": ["gold", "broad"],               # インフレ↑成長↓: 金・総合
    "B": [],                              # 株式優位→コモディティは現金
    "D": [],                              # デフレ→現金
}

# ==============================================================
# BLOCK 50: マルチ戦略資金配分メタ設定
# ==============================================================
# allocation_meta.py が参照（月次）。分数ケリー×リスクパリティ。利用者承認後にconfig反映。

ENABLE_ALLOCATION_META: bool = True
KELLY_FRACTION: float = 0.25       # 分数ケリー（フルケリーの1/4）
ALLOC_MIN_HISTORY_M: int = 3       # 配分計算に必要な最小月数
CULL_PF_TH: float = 1.0            # 淘汰: 直近6ヶ月PFがこれ未満
CULL_MONTHS: int = 6               # 淘汰判定の観測月数
CULL_P_TH: float = 0.5             # 淘汰: bootstrap p がこれ超

# ==============================================================
# BLOCK 51: MLメタモデル設定
# ==============================================================
# ml/ が参照。LightGBM回帰・Purged Walk-Forward CV。フラグ変更は利用者承認。

# ※ 既存 BLOCK RESEARCH-4（旧XGBoost ML_CORRECTION_*）とは別系統のため ML_META_* で命名。
ML_META_TARGET_HORIZON_D: int = 20   # 目的変数の先読み営業日数
ML_META_EMBARGO_D: int = 20          # Purged CV の embargo（リーク防止・最重要）
ML_META_PROMOTE_IC: float = 0.02     # 昇格: OOS rank IC 下限
ML_META_PROMOTE_T: float = 2.0       # 昇格: t統計量 下限
ML_META_ENABLED: bool = False        # 補正テーブルの日次適用（昇格＋利用者承認後にTrue）
ML_META_MAX_PT: float = 10.0         # composite への補正上限（±pt）
ML_META_N_DECILES: int = 10          # 予測を十分位に量子化（→補正テーブルのキー）
ML_META_CV_FOLDS: int = 5            # Purged Walk-Forward CV のフォールド数
# 特徴量スナップショット（日次追記）／学習後の補正テーブル（Colabからエクスポート）の置き場。
ML_META_DATASET_PATH: str = str(DATA_DIR / "ml_dataset" / "features.jsonl")
ML_META_CORRECTION_PATH: str = str(DATA_DIR / "ml_dataset" / "correction_table.json")
ML_META_DATASET_MAX_DAYS: int = 500  # features.jsonl の保持日数（古い日付から間引き）
ENABLE_ML_META_DATASET: bool = True  # 特徴量スナップショット蓄積（学習用・補正はしない）

# ==============================================================
# BLOCK 56: JP/US共通スコア設定
# ==============================================================
# score_calculator.py が参照。対象 = 保有銘柄 ＋ us_screener 上位N のみ（ユニバースは広げない）。
ENABLE_SCORE_CALCULATOR: bool = True
UNIFIED_SCORE_US_TOP_N: int = 50     # us_screener からの対象上位件数

# ── Phase B: 共通成分による調整 ──────────────
# composite = clamp(base + dcf_adj + tech_adj + geo_adj, 0, 100)
# 各成分は results（technical_scorer / dcf_valuation / sectors 等）から
# 「読むだけ」で取得する。score_calculator は yfinance / BQ を叩かない。
# 成分が取れない銘柄は当該成分を 0 として base のみで算出する。
# UNIFIED_SCORE_PHASE_B_ENABLED=False で Phase A と完全に同じ出力へ戻る
#   （非常口。tests/test_score_calculator.py が同値性を保証）。
UNIFIED_SCORE_PHASE_B_ENABLED: bool = True
UNIFIED_SCORE_DCF_MAX_ADJ:  float = 5.0   # DCF調整の上限（±pt）
UNIFIED_SCORE_TECH_MAX_ADJ: float = 5.0   # テクニカル調整の上限（±pt）
UNIFIED_SCORE_GEO_MAX_ADJ:  float = 3.0   # 地政学調整の上限（±pt）
UNIFIED_SCORE_RE_MAX_ADJ:   float = 0.5   # 不動産調整の上限（±pt）。含み益ファクターは校正・開示時価での置き換え後も時点整合バックテストで通常PBRを上回らない（採用基準未達）。開示時価が1年分たまる2027年の有報シーズン後に再検証するまで小さく残す

# ==============================================================
# BLOCK 57: APIキー生存判定設定（Issue J-2 / api_key_health）
# ==============================================================
# api_key_health.py が参照。各APIキーの生存/失効を実行ログ冒頭で
# 1行サマリ＋詳細表として一望できるようにする。
# ENABLE_KEY_HEALTH=False で本ステージ全体をスキップ。
# KEY_HEALTH_CSE_ENABLED は Google CSE の日次 quota を消費するため既定 False
# （True にした時のみ CSE を num=1 で1件だけ実クエリして判定する）。
ENABLE_KEY_HEALTH: bool = True
KEY_HEALTH_CSE_ENABLED: bool = False

# ==============================================================
# BLOCK 58: 月次・四半期ランナー設定
# ==============================================================
# monthly_runner.py が参照。月初に走る「提案・レビュー系」を1本に束ねる唯一の実行主体で、
# GitHub Actions（daily_run.yml の月初判定）と司令塔シェル（invest-monthly）の両方から起動される。
#
# 対象月は既定で「前月」。月初に閉じたばかりの月をレビューする
# （commodity_rotation だけは翌月への配分提案なので当月）。
ENABLE_MONTHLY_RUNNER: bool = True
# 各ステップの個別スイッチ。False にするとそのステップだけスキップされる。
ENABLE_SIGNAL_CULLING: bool = True          # E4 シグナル淘汰レポート（月次）
ENABLE_SUBTHEME_REFRESH: bool = True        # 業種×サブテーマ表の作り直し（setup_subtheme_bq.py --refresh）
ENABLE_MONTHLY_STRATEGY_REVIEW: bool = True  # E12 月次戦略レビュー（集約・最後に実行）
# tax_optimizer を走らせる月（四半期＋11月末の枠取り前）。
TAX_OPTIMIZER_MONTHS: tuple = (1, 4, 7, 10, 11)
# 月初判定の許容日（1〜この日までを「月初」とみなす。Actions障害の翌日復旧を許容）
MONTHLY_RUN_MAX_DAY: int = 3
# 月次の各ステップを同じ対象月で試す上限回数（直らない失敗を月末まで毎日やり直して Actions の枠を使わないため）
MONTHLY_STEP_MAX_ATTEMPTS: int = 3

# ==============================================================
# BLOCK 59: 実装モジュール定数の集約
# ==============================================================
# 各モジュールが config_sync.sync_module_constants() を run_* の冒頭で1回呼び、
# モジュール定数をここの値で上書きする（参照箇所を書き換えない方式）。
# 命名はモジュール別プレフィックスで衝突を避けている。

# ── market_breadth.py（市場幅指標 [3-7]）────────────────────
MB_SCORE_HEALTHY_THRESH: float = 70.0   # これ以上で「healthy」判定
MB_SCORE_WEAK_THRESH:    float = 30.0   # これ以下で「weak」判定
MB_SCORE_HEALTHY_BONUS:  float =  3.0   # healthy 時の composite 加点
MB_SCORE_WEAK_PENALTY:   float = -5.0   # weak 時の composite 減点
MB_DIVERGENCE_PENALTY:   float = -3.0   # 指数と幅の乖離時の減点
# 4指標の合成ウェイト（合計 1.00）
MB_WEIGHT_AD_RATIO:      float = 0.30   # 騰落レシオ
MB_WEIGHT_NH_RATIO:      float = 0.25   # 新高値/新安値
MB_WEIGHT_MA200_RATIO:   float = 0.30   # 200日線超え銘柄比率
MB_WEIGHT_RSI50_RATIO:   float = 0.15   # RSI50超え銘柄比率
MB_MA200_PERIOD:         int   = 200    # 長期移動平均の期間（営業日）
MB_RSI_PERIOD:           int   = 14     # RSI 期間
MB_NH_NL_PERIOD:         int   = 252    # 新高値/新安値の観測期間（約1年）
MB_CACHE_HOURS:          int   = 24     # キャッシュ有効時間

# ── volume_profile.py（出来高プロファイル [4-7]）─────────────
VP_LOOKBACK_DAYS:        int   = 60     # プロファイル構築の遡及日数
VP_HVN_MULTIPLIER:       float = 1.5    # 平均比これ以上で HVN（高出来高帯）
VP_LVN_MULTIPLIER:       float = 0.5    # 平均比これ以下で LVN（低出来高帯）
VP_BONUS_ABOVE_VAH:      float = 10.0   # 価値エリア上限超え → 加点
VP_PENALTY_BELOW_VAL:    float = -10.0  # 価値エリア下限割れ → 減点
VP_BONUS_POC_CHEAP:      float =  8.0   # POC より割安 → 加点
VP_PENALTY_POC_DEAR:     float = -8.0   # POC より割高 → 減点
VP_BONUS_HVN:            float =  5.0   # HVN 圏内 → 加点
VP_PENALTY_LVN:          float = -5.0   # LVN 圏内 → 減点
VP_POC_DEVIATION_THRESH: float = 0.05   # POC 乖離の判定閾値（±5%）
VP_CACHE_HOURS:          int   = 24

# ── support_resistance.py（サポレジ検出 [4-6]）───────────────
SR_LOOKBACK_52W:         int   = 252    # 52週高安の観測期間（営業日）
SR_LOOKBACK_VWAP:        int   = 20     # VWAP の算出期間
SR_PEAK_WINDOW:          int   = 5      # ピーク検出のウィンドウ
SR_BREAKOUT_THRESH:      float = 0.95   # レジスタンス比これ以上で上抜け間近
SR_BOTTOM_THRESH:        float = 1.10   # サポート比これ以下で底値圏
SR_BONUS_BREAKOUT:       float = 20.0   # 上抜け → 加点
SR_BONUS_UPPER_ZONE:     float =  8.0   # 上方ゾーン → 加点
SR_BONUS_BOTTOM_ZONE:    float = 12.0   # 底値圏 → 加点（逆張り妙味）
SR_BONUS_VWAP_ABOVE:     float =  5.0   # VWAP 上 → 加点
SR_PENALTY_VWAP_BELOW:   float = -5.0   # VWAP 下 → 減点
SR_BONUS_RESISTANCE_BREAK: float = 10.0 # レジスタンス突破 → 加点
SR_CACHE_HOURS:          int   = 24

# ── technical_scorer.py（テクニカルスコア）───────────────────
TS_RSI_PERIOD:           int   = 14
TS_BB_PERIOD:            int   = 20     # ボリンジャーバンド期間
TS_CACHE_HOURS:          int   = 24
# 総合スコアの合成ウェイト（合計 1.00）
TS_WEIGHT_SECTOR:        float = 0.35
TS_WEIGHT_VALUE:         float = 0.25
TS_WEIGHT_QUALITY:       float = 0.20
TS_WEIGHT_TECHNICAL:     float = 0.20

# ── モジュール定数（優先度1）────────────────────
# run_* の入口で config_sync が取り込むため、環境変数による上書きも反映される。

# rule_optimizer.py
RO_WEIGHT_MIN: float = 0.30
RO_WEIGHT_MAX: float = 2.00
RO_WEIGHT_INIT: float = 1.00
RO_DEFAULT_SIGNAL_WEIGHTS: dict = {
    "macro_phase_weight": 0.20, "sector_composite_weight": 0.20,
    "momentum_weight": 0.15, "earnings_accel_weight": 0.15,
    "quality_weight": 0.10, "technical_weight": 0.10,
    "activist_weight": 0.10,
}
RO_SIGNAL_WEIGHT_EMA_ALPHA: float = 0.05
RO_SIGNAL_WEIGHT_MIN: float = 0.05
RO_SIGNAL_WEIGHT_MAX: float = 0.40
RO_FACTOR_WEIGHT_EMA: float = 0.10

# earnings_surprise.py
ES_QUARTER_WEIGHTS: list = [0.40, 0.30, 0.20, 0.10]
ES_BONUS_CONSEC_POS2: float = 5.0
ES_BONUS_CONSEC_POS1: float = 2.0
ES_PENALTY_CONSEC_NEG2: float = -5.0
ES_PENALTY_CONSEC_NEG1: float = -2.0
ES_SURPRISE_COMBO_ACCEL_MIN: float = 70.0
ES_SURPRISE_COMBO_CONSERV_MIN: float = 70.0

# latent_pearl_manager.py（重み・昇格閾値は既存 LATENT_PEARL_* を継続）
LATENT_PEARL_CONDITION_A_INDEP_MIN: float = 65.0
LATENT_PEARL_CONDITION_A_CATALYST_MIN: float = 3.0
LATENT_PEARL_CONDITION_B_STRENGTH_MIN: float = 3.0
LATENT_PEARL_CONDITION_D_MIDTERM_MIN: float = 80.0
LATENT_PEARL_CROSS_SIGNAL_NORM_MAX: float = 10.0

# anomaly_detector.py（既存 ANOMALY_* へIQR判定を追加）
ANOMALY_IQR_MULTIPLIER: float = 1.5

# insider_monitor.py
IM_CONSECUTIVE_THRESH: int = 2
IM_BONUS_BUY_SINGLE: float = 3.0
IM_BONUS_BUY_CONSEC: float = 6.0
IM_PENALTY_SELL_SINGLE: float = -3.0
IM_PENALTY_SELL_CONSEC: float = -6.0
IM_BONUS_LARGE_HOLDING: float = 4.0

# politician_signal_scorer.py
PSS_SENATE_COMMITTEE_MULTIPLIER: dict = {
    "finance": 1.8, "banking, housing, and urban affairs": 1.7, "banking": 1.7,
    "budget": 1.5, "appropriations": 1.5, "armed services": 1.6,
    "intelligence": 1.6, "homeland security and governmental affairs": 1.4,
    "commerce, science, and transportation": 1.5, "commerce": 1.5,
    "energy and natural resources": 1.4, "environment and public works": 1.3,
    "foreign relations": 1.4, "judiciary": 1.3,
    "health, education, labor, and pensions": 1.2,
    "agriculture, nutrition, and forestry": 1.2,
    "small business and entrepreneurship": 1.1,
}
PSS_HOUSE_COMMITTEE_MULTIPLIER: dict = {
    "ways and means": 1.6, "financial services": 1.5, "intelligence": 1.5,
    "armed services": 1.4, "energy and commerce": 1.4, "foreign affairs": 1.3,
    "judiciary": 1.2, "appropriations": 1.2,
    "science, space, and technology": 1.2, "natural resources": 1.2,
    "agriculture": 1.1, "transportation and infrastructure": 1.1,
    "oversight and accountability": 1.1, "homeland security": 1.2, "rules": 1.1,
}
PSS_VOTE_CATEGORY_MAX_BONUS: dict = {
    "tax": 15.0, "semiconductor": 12.0, "defense": 12.0, "healthcare": 10.0,
    "energy": 10.0, "trade": 10.0, "financial": 8.0, "other": 5.0,
}
PSS_L4_SCORE_THRESHOLD: float = 80.0
PSS_L4_MULTIPLIER: float = 0.5
PSS_SENATE_CHAMBER_BONUS: float = 5.0

# screener_feedback_loop.py
SFL_MAX_EDINET_BONUS: float = 8.0
SFL_MAX_EDINET_PENALTY: float = -8.0
SFL_CREDIT_WEIGHT: float = 0.40
SFL_CAGR_WEIGHT: float = 0.25
SFL_ROE_TREND_WEIGHT: float = 0.20
SFL_DEBT_WEIGHT: float = 0.15

# options_monitor.py（IV_SPIKE_THRESHOLD は既存設定を継続）
OM_PCR_FEAR_THRESH: float = 1.2
OM_PCR_GREED_THRESH: float = 0.6
OM_TERM_CONTANGO_RATIO: float = 0.90
OM_TERM_BACKWD_RATIO: float = 1.10
OM_TERM_SCORE_WEIGHT: float = 0.20

# credit_data.py（既存 CREDIT_* 閾値へ注意・補正値を追加）
CREDIT_CAUTION_THRESHOLD: float = 6.0
CREDIT_BONUS_SQUEEZE: float = 3.0
CREDIT_PENALTY_OVERHEAT: float = -5.0

# ignition_detector.py（既存 IGNITION_* 設定へカテゴリ検出閾値を追加）
IGNITION_CATEGORY_DETECT_THRESHOLD: float = 0.30

# ── モジュール定数（優先度1・続き）────────────────────
ACT_ACADEMIC_SCORE_MAX: float = 100.0
AA_IC_HIGH_THRESHOLD: float = 0.10
AA_IC_MID_THRESHOLD: float = 0.05
AA_IC_NEG_THRESHOLD: float = 0.00
BCP_RECESSION_WEIGHTS: dict = {
    "inversion_2y10y": 40, "inversion_3m10y": 30,
    "inversion_days": 20, "yield_drop_speed": 10,
}
CFM_STALE_THRESHOLD: float = 0.30
CFM_IGNORE_THRESHOLD: float = 0.05
CC_CATALYST_BONUS_MAX: float = 5.0
CC_TRIPLE_THRESHOLD: int = 3
CEO_TENURE_OBSERVING_MAX: float = 1.0
CEO_TENURE_FULL_MIN: float = 4.0
CEO_ACTIVIST_RENEWAL_THRESHOLD: float = 60.0
CM_DEFAULT_STAGE_MULTIPLIER: dict = {
    "introduced": 0.2, "committee": 0.4, "floor_vote": 0.7,
    "passed": 0.9, "signed": 1.0,
}
CH_HIGH_CORR_THRESH: float = 0.8
CH_LOW_CORR_THRESH: float = -0.5
CRU_GDELT_CIRCUIT_THRESHOLD: int = 3
CSS_SOURCE_SIGNAL_THRESHOLD: float = 5.0
CSS_NANO_CONTRARIAN_CONFIDENCE_MAX: float = 0.55
DC_BONUS_UPCOMING_30D: float = 3.0
DC_PENALTY_POST_EX: float = -2.0
DD_DEFAULT_SPIKE_THRESHOLD: float = 20.0
FCM_CROWDING_THRESHOLD: float = 0.6
FCM_UNWIND_THRESHOLD: float = 0.4
FCM_CROWDING_SCORE_CAP: int = 100
GTP_TREND_TAILWIND_THRESHOLD: int = 60
GTP_TREND_HEADWIND_THRESHOLD: int = 40
GTP_TREND_MOM_BOOST_THRESHOLD: float = 0.2
ISC_ALPHA_SCORE_MAX_ANNUAL: float = 0.3
ISC_CATALYST_SCORE_MAX: float = 10.0
ISC_COMBINED_WEIGHTS: dict = {"independence": 0.7, "catalyst": 0.3}
IE_THRESHOLDS: dict = {
    "vix_fear": 25.0, "vix_crisis": 35.0, "vix_calm": 15.0,
    "pcr_fear": 1.2, "pcr_greed": 0.6, "us10y_high": 4.5, "us10y_low": 3.5,
    "yen_weak": 148.0, "yen_strong": 135.0, "oil_high": 85.0,
    "recession_risk": 60, "recession_low": 30, "breadth_healthy": 65,
    "breadth_weak": 35, "arxiv_hot": 70,
}
I13F_CHANGE_MULTIPLIER: dict = {
    "new_position": 1.0, "increased": 0.5, "decreased": -0.5,
    "sold": -1.0, "unchanged": 0.0,
}
JUSC_CORR_THRESHOLD: float = 0.7
LS_DEFAULT_WEIGHTS: dict = {
    f"{t}_{g}_{j}": 1.0 / 9
    for t in ("fast", "standard", "slow")
    for g in ("cooperation", "decoupling", "conflict")
    for j in ("revival", "stagnation", "transform")
}
LS_STANCE_THRESHOLDS: dict = {"long_high": 65.0, "short_high": 60.0}
MPT_TRAJECTORY_THRESHOLDS: dict = {
    "ahead": 1.1, "on_track": 0.9, "behind": 0.7, "critical": 0.0,
}
MPT_METRIC_WEIGHTS: dict = {
    "net_sales": 0.25, "operating_income": 0.3, "operating_margin": 0.2,
    "roe": 0.15, "roa": 0.1,
}
MLSC_ML_CORRECTION_MAX: float = 5.0
MLSC_TRAIN_WEEKS_MIN: int = 26
NSS_FUZZY_THRESHOLD: float = 0.82
PM_PEAD_SCORE_ALERT: float = 5.0
PM_PEAD_VOLUME_MIN: float = 1.5
PM_MIN_SURPRISE_BONUS: float = 0.5
PTR_SCORE_CORR_MIN_N: int = 5
PR_SOURCE_WEIGHTS: dict = {
    "閣議決定": 1.0, "内閣決定": 1.0, "首相": 0.9, "総理": 0.9,
    "経済対策": 0.85, "大臣": 0.7, "副大臣": 0.6,
    "パブリックコメント": 0.6, "パブコメ": 0.6, "中間報告": 0.4,
    "審議会": 0.4, "諮問": 0.35, "検討": 0.25, "研究会": 0.2,
    "議員立法": 0.3, "委員会": 0.35,
}
PR_CUMULATIVE_THRESHOLD_FOR_L3: int = 3
PT_L3_SOURCE_THRESHOLD: int = 3
PT_L3_REALIZATION_SCORE_THRESHOLD: float = 1.5
PMON_COMMITTEE_MULTIPLIER: dict = {
    "ways and means": 1.6, "financial services": 1.5, "intelligence": 1.5,
    "armed services": 1.4, "energy and commerce": 1.4, "foreign affairs": 1.3,
    "judiciary": 1.2, "appropriations": 1.2,
    "science, space, and technology": 1.2, "natural resources": 1.2,
    "agriculture": 1.1,
}
PDF_CORR_THRESHOLD_DEFAULT: float = 0.7
PDF_HHI_ALERT_THRESHOLD: float = 0.3
PO_REBALANCE_THRESH: float = 0.05
PORTFOLIO_LOOKBACK_DAYS: int = 252
PORTFOLIO_MIN_RETURN_OBSERVATIONS: int = 60
PS_ATR_MULTIPLIER: float = 2.0
PS_CONVICTION_THRESHOLDS: list = [90, 75, 60, 45]
PS_CONVICTION_MULTIPLIERS: dict = {5: 1.5, 4: 1.0, 3: 0.7, 2: 0.4, 1: 0.0}
RD_CONF_ADJ_MAX: float = 0.2
RD_CONF_ADJ_MIN: float = -0.15
RD_TRANSITION_ALERT_PROB_THRESHOLD: float = 0.6
SCB_EDINET_CREDIT_THRESHOLD: int = 80
SSH_SCORE_TREND_WEEKS: int = 13
SE_PEARL_HIT_ALPHA_THRESHOLD: float = 3.0
SHA_INST_HIGH_THRESHOLD: float = 70.0
SHA_INST_LOW_THRESHOLD: float = 20.0
SD_SCORE_MAX_TOTAL_ADJ_POS: float = 20.0
SD_SCORE_MAX_TOTAL_ADJ_NEG: float = -20.0
SDM_DEFAULT_THRESHOLDS_JP: dict = {
    "Large": 10000.0, "Mid": 1000.0, "Small": 100.0, "Micro": 50.0, "Nano": 0.0,
}
SDM_DEFAULT_THRESHOLDS_US: dict = {
    "Large": 1000.0, "Mid": 200.0, "Small": 20.0, "Micro": 5.0, "Nano": 0.0,
}
SDM_TIER_COMPOSITE_WEIGHTS: dict = {
    "Large": {"sector_score": 0.45, "value_score": 0.25, "quality_score": 0.3},
    "Mid": {"sector_score": 0.4, "value_score": 0.3, "quality_score": 0.3},
    "Small": {"sector_score": 0.33, "value_score": 0.3, "quality_score": 0.37},
    "Micro": {"sector_score": 0.25, "value_score": 0.3, "quality_score": 0.45},
    "Nano": {"sector_score": 0.2, "value_score": 0.25, "quality_score": 0.55},
}
SDM_SECTOR_WEIGHT_MULTIPLIER: dict = {
    "Large": 1.0, "Mid": 1.0, "Small": 0.9, "Micro": 0.65, "Nano": 0.4,
}
SDM_EFFECTIVE_SIGNAL_MIN_THRESHOLD: float = 0.1
SDM_NANO_PRIORITY_WEIGHTS: dict = {
    "liquidity_rank_norm": 0.3, "roe_excess_norm": 0.25,
    "days_in_queue": 0.2, "taxonomy_correction": 0.25,
}
STC_DEFAULT_THRESHOLDS_JP: dict = {
    "Large": 10000, "Mid": 1000, "Small": 100, "Micro": 50, "Nano": 0,
}
STC_DEFAULT_THRESHOLDS_US: dict = {
    "Large": 1000, "Mid": 200, "Small": 20, "Micro": 5, "Nano": 0,
}
SS_DISCOVERY_ANALYST_MAX: int = 3
SS_DISCOVERY_VOLUME_RATIO: float = 0.5
DISCOVERY_COMPOSITE_PERCENTILE: float = 0.80
COMPS_VALUE_BLEND_WEIGHT: float = 0.30
COMPS_VALUE_DELTA_CAP: float = 10.0
VALUATION_TOTAL_ADJUSTMENT_CAP: float = 10.0
ST_CORR_CLUSTER_THRESHOLD: float = 0.75
TH_SEMI_NEWS_WEIGHT: float = 0.7
TH_SEMI_POLICY_WEIGHT: float = 0.3
TET_SIGNAL_THRESHOLDS: dict = {"hot": 70, "warming": 50, "neutral": 35}
BMD_DEFAULT_THRESHOLDS: dict = {
    "recurring_revenue_ratio_min": 0.7, "asset_turnover_heavy_max": 0.5,
    "gross_margin_saas_min": 0.6, "debt_ratio_bank_min": 0.8,
    "revenue_per_employee_saas": 20000000,
}
BMN_BONUS_MAX: float = 10.0
BMN_BONUS_MIN: float = -5.0
LAL_SIGNIFICANT_WEIGHT_DELTA: float = 0.15
LAL_IC_UNDERPERFORM_THRESHOLD: float = 0.05
PSC_DEFAULT_WEIGHTS: dict = {"D1": 0.25, "D2": 0.15, "D3": 0.3, "D4": 0.15, "D5": 0.15}
PSC_M_SCORE_PENALTY_THRESHOLD: float = -1.78
PSC_M_SCORE_MEDIUM_THRESHOLD: float = -2.22
PSC_M_SCORE_STRONG_MULTIPLIER: float = 0.5
PSC_M_SCORE_MEDIUM_MULTIPLIER: float = 0.75
PSC_DEFAULT_TIER_BONUS: dict = {"SS": 15, "S": 10, "A": 5, "B": 2}
TXS_YOY_SECTION_WEIGHTS: dict = {"risk": 1.0, "business": 0.4, "strategy": 0.3, "governance": 0.2}
TXS_CATEGORY_MAX_PENALTY: float = 5.0
TXS_KEYWORD_UNIT_PENALTY: float = 0.5
TXS_CATALYST_SCORE_MAX: float = 10.0
TXS_CATALYST_SUPPLY_THRESHOLD: float = 2.0
SAM_DELTA_THRESHOLDS: dict = {
    "usdjpy": 3.0, "us10y": 0.2, "oil_wti": 5.0,
    "vix": 5.0, "sp500": 100.0, "gold": 30.0,
}

# ── モジュール定数（優先度2：観測・保持期間）────────────────────
B4_ALPHA_ATTRIBUTER_LOOKBACK_WEEKS = 26  # LOOKBACK_WEEKS
B4_ANOMALY_DETECTOR_VOLUME_LOOKBACK = 20  # VOLUME_LOOKBACK
B4_ANOMALY_DETECTOR_HISTORY_PERIOD = '3mo'  # HISTORY_PERIOD
B4_ANOMALY_SEASON_LOOKBACK_YEARS = 10  # LOOKBACK_YEARS
B4_ANOMALY_SEASON_CACHE_HOURS = 24 * 7  # CACHE_HOURS
B4_BACKTESTER_HOLD_DAYS = 28  # HOLD_DAYS
B4_BACKTESTER_LOOKBACK_DAYS = 90  # LOOKBACK_DAYS
B4_CACHE_FRESHNESS_MANAGER_DEFAULT_HALF_LIFE_HOURS = {'yfinance_price': 4.0, 'options_monitor': 26.0, 'macro_snapshot': 26.0, 'us_8k': 20.0, 'policy_radar': 720.0, 'gov_data': 168.0, 'google_trends': 168.0, 'sentiment': 168.0, 'us_macro': 26.0, 'edinet': 720.0, 'sec_edgar': 720.0, 'rule_weights': 720.0, 'longterm_scenario': 168.0}  # DEFAULT_HALF_LIFE_HOURS
B4_CATALYST_CALENDAR_HISTORY_TTL_DAYS = 365  # _HISTORY_TTL_DAYS
B4_CATALYST_CALENDAR_TRIPLE_WINDOW_DAYS = 14  # _TRIPLE_WINDOW_DAYS
B4_CEO_EVALUATOR_SCORES_RETAIN_YEARS = 5  # SCORES_RETAIN_YEARS
B4_COMPS_ANALYZER_CACHE_HOURS = 24  # CACHE_HOURS
B4_CONGRESS_MONITOR_DEFAULT_CACHE_TTL_MONTHS = 12  # _DEFAULT_CACHE_TTL_MONTHS
B4_CORRELATION_HEATMAP_LOOKBACK_DAYS = 90  # LOOKBACK_DAYS
B4_CORRELATION_HEATMAP_CACHE_HOURS = 24  # CACHE_HOURS
B4_CREDIT_DATA_CACHE_HOURS = 24 * 7  # CACHE_HOURS
B4_DCF_VALUATION_CACHE_HOURS = 24 * 7  # CACHE_HOURS
B4_DIVIDEND_CALENDAR_ALERT_WINDOW_DAYS = 30  # ALERT_WINDOW_DAYS
B4_DIVIDEND_CALENDAR_URGENT_WINDOW_DAYS = 7  # URGENT_WINDOW_DAYS
B4_DIVIDEND_CALENDAR_CACHE_HOURS = 24  # CACHE_HOURS
B4_EARNINGS_SURPRISE_HISTORY_QUARTERS = 4  # HISTORY_QUARTERS
B4_EARNINGS_SURPRISE_CACHE_HOURS = 24  # CACHE_HOURS
B4_EARNINGS_SURPRISE_CONSERVATISM_QUARTERS = 8  # CONSERVATISM_QUARTERS
B4_EMERGING_TECHNOLOGY_DETECTOR_CACHE_TTL_DAYS = 14  # _CACHE_TTL_DAYS
B4_FACTOR_CROWDING_MONITOR_LOOKBACK_DAYS = 30  # LOOKBACK_DAYS
B4_FACTOR_CROWDING_MONITOR_HISTORY_RETAIN_WEEKS = 12  # HISTORY_RETAIN_WEEKS
B4_FED_CALENDAR_FETCHER_BOJ_PROXIMITY_DAYS = 7  # BOJ_PROXIMITY_DAYS
B4_FINBERT_ANALYZER_CACHE_VALID_DAYS = 7  # CACHE_VALID_DAYS
B4_GOOGLE_TRENDS_PIPELINE_CACHE_VALID_DAYS = 7  # CACHE_VALID_DAYS
B4_GOOGLE_TRENDS_PIPELINE_LOOKBACK_WEEKS = 13  # LOOKBACK_WEEKS
B4_INDEPENDENCE_SCORE_CALCULATOR_ROLLING_WINDOW_DAYS = 60  # _ROLLING_WINDOW_DAYS
B4_INSIDER_MONITOR_CACHE_HOURS = 24 * 7  # CACHE_HOURS
B4_JP_US_SECTOR_BRIDGE_CORR_LOOKBACK_WEEKS = 52  # CORR_LOOKBACK_WEEKS
B4_JP_US_SECTOR_BRIDGE_ROTATION_LOOKBACK = 8  # ROTATION_LOOKBACK
B4_JP_US_SECTOR_BRIDGE_CACHE_HOURS = 24 * 7  # CACHE_HOURS
B4_JP_US_SECTOR_CORRELATION_LOOKBACK_DAYS = 60  # LOOKBACK_DAYS
B4_JP_US_SECTOR_CORRELATION_FETCH_BUFFER_DAYS = 90  # FETCH_BUFFER_DAYS
B4_JP_US_SECTOR_CORRELATION_CACHE_TTL_DAYS = 7  # CACHE_TTL_DAYS
B4_MARKET_BREADTH_HISTORY_PERIOD = '13mo'  # HISTORY_PERIOD
B4_ML_SCORE_CORRECTOR_LOOKBACK_WEEKS = 26  # LOOKBACK_WEEKS
B4_MOMENTUM_FACTOR_LOOKBACK_DAYS = 28  # LOOKBACK_DAYS
B4_MULTI_CURRENCY_MONITOR_CACHE_HOURS = 8  # CACHE_HOURS
B4_NEDO_SELECTION_SCRAPER_CACHE_TTL_DAYS = 7  # CACHE_TTL_DAYS
B4_OPTIONS_MONITOR_CACHE_HOURS = 4  # CACHE_HOURS
B4_PEAD_MONITOR_CACHE_TTL_DAYS = 60  # _CACHE_TTL_DAYS
B4_PEAD_MONITOR_VOLUME_WINDOW_DAYS = 20  # _VOLUME_WINDOW_DAYS
B4_PEAD_MONITOR_PRICE_DRIFT_WINDOW = 5  # _PRICE_DRIFT_WINDOW
B4_PERFORMANCE_TRACKER_HISTORY_RETAIN_DAYS = 90  # HISTORY_RETAIN_DAYS
B4_PERFORMANCE_TRACKER_DRIFT_LOG_RETAIN_DAYS = 90  # DRIFT_LOG_RETAIN_DAYS
B4_POLICY_SIGNAL_APPLIER_DECAY_TTL_DAYS = 30  # _DECAY_TTL_DAYS
B4_PORTFOLIO_DIVERSITY_FILTER_CORR_WINDOW_DAYS = 60  # _CORR_WINDOW_DAYS
B4_PORTFOLIO_OPTIMIZER_CACHE_HOURS = 24  # CACHE_HOURS
B4_POSITION_SIZER_ATR_PERIOD = 14  # ATR_PERIOD
B4_REGIME_DETECTOR_MIN_TRAIN_DAYS = 30  # MIN_TRAIN_DAYS
B4_RULE_OPTIMIZER_LOOKBACK_WEEKS = 12  # LOOKBACK_WEEKS
B4_RULE_OPTIMIZER_FACTOR_LOOKBACK_DEF = 12  # FACTOR_LOOKBACK_DEF
B4_SCREENER_FEEDBACK_LOOP_CACHE_HOURS = 24 * 7  # CACHE_HOURS
B4_SCREENER_SECTOR_HEATMAP_CACHE_HOURS = 24  # CACHE_HOURS
B4_SECTOR_ROTATION_DETECTOR_AVG_WINDOW_DAYS = 84  # AVG_WINDOW_DAYS
B4_SECTOR_ROTATION_DETECTOR_TREND_WINDOW_DAYS = 28  # TREND_WINDOW_DAYS
B4_SECTOR_ROTATION_DETECTOR_CACHE_TTL_DAYS = 7  # CACHE_TTL_DAYS
B4_SELF_EVALUATOR_HOLD_DAYS = 28  # HOLD_DAYS
B4_SELF_EVALUATOR_LOOKBACK_WEEKS = 8  # LOOKBACK_WEEKS
B4_SHAREHOLDER_ANALYZER_HISTORY_RETAIN_QUARTERS = 8  # HISTORY_RETAIN_QUARTERS
B4_SIGNAL_CULLING_REPORT_FWD_DAYS = 20  # FWD_DAYS
B4_STRESS_TESTER_VAR_LOOKBACK = 252  # VAR_LOOKBACK
B4_STRESS_TESTER_MDD_LOOKBACK = 504  # MDD_LOOKBACK
B4_TAXONOMY_HOTNESS_HISTORY_DAYS = 60  # HISTORY_DAYS
B4_TAXONOMY_HOTNESS_BASELINE_DAYS = 20  # BASELINE_DAYS
B4_TECHNICAL_SCORER_HISTORY_PERIOD = '2y'  # HISTORY_PERIOD
B4_US_8K_MONITOR_CACHE_VALID_HOURS = 20  # CACHE_VALID_HOURS
B4_US_FUNDAMENTAL_ROTATOR_CACHE_DAYS = 30  # CACHE_DAYS
B4_US_INSIDER_FORM4_SCRAPER_WINDOW_DAYS_DEFAULT = 30  # _WINDOW_DAYS_DEFAULT
B4_US_SECTOR_BRIDGE_ENHANCED_MAPPING_CACHE_DAYS = 30  # MAPPING_CACHE_DAYS
B4_WEEKLY_FEEDBACK_RUNNER_STALE_DAYS = 7  # STALE_DAYS

# ── モジュール定数（優先度3：API・実行時パラメータ）────────────────────
B5_DATA_RELIABILITY_DEFAULT_RETRY_SCHEDULE = [2, 4, 8]  # _DEFAULT_RETRY_SCHEDULE
B5_POLITICIAN_MONITOR_REQUEST_TIMEOUT = 20  # _REQUEST_TIMEOUT
B5_POLITICIAN_MONITOR_RETRY_COUNT = 3  # _RETRY_COUNT
B5_POLITICIAN_MONITOR_RETRY_WAIT_SEC = 5  # _RETRY_WAIT_SEC
B5_NEDO_SELECTION_SCRAPER_REQUEST_TIMEOUT = 20  # REQUEST_TIMEOUT
B5_STRESS_TESTER_FETCH_DELAY = 0.25  # FETCH_DELAY
B5_JOB_POSTING_MONITOR_REQUEST_TIMEOUT = 15  # _REQUEST_TIMEOUT
B5_DB_REST_CLIENT_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}  # RETRYABLE_STATUS_CODES
B5_FACILITY_EXPANSION_MONITOR_REQUEST_TIMEOUT = 20  # _REQUEST_TIMEOUT
B5_ARXIV_MONITOR_SS_TIMEOUT = 15  # _SS_TIMEOUT
B5_JOGMEC_SCRAPER_FETCH_TIMEOUT = 25  # _FETCH_TIMEOUT
B5_JOGMEC_SCRAPER_RETRY_WAIT_SEC = 5  # _RETRY_WAIT_SEC
B5_CORRELATION_HEATMAP_FETCH_DELAY = 0.25  # FETCH_DELAY
B5_GOOGLE_TRENDS_PIPELINE_FETCH_SLEEP_SEC = 5.0  # FETCH_SLEEP_SEC
B5_PORTFOLIO_OPTIMIZER_FETCH_DELAY = 0.3  # FETCH_DELAY
B5_ANOMALY_SEASON_FETCH_DELAY = 0.3  # FETCH_DELAY
B5_US_FUNDAMENTAL_ROTATOR_DAILY_FETCH_LIMIT = 100  # DAILY_FETCH_LIMIT
B5_US_FUNDAMENTAL_ROTATOR_REQUEST_DELAY = 0.12  # REQUEST_DELAY
B5_US_FUNDAMENTAL_ROTATOR_REQUEST_TIMEOUT = 15  # REQUEST_TIMEOUT
B5_PAID_MEDIA_SEARCHER_CSE_DEAD_LIMIT = 3  # _CSE_DEAD_LIMIT
# 下記は各モジュールの _CFG_CONST_MAP が参照する（config に無いと sync_module_constants が黙って何もしない）。
B5_STOCK_SNAPSHOT_WRITER_MAX_ROWS = 2000      # MAX_ROWS
B5_PRICE_HISTORY_WRITER_LOOKBACK_DAYS = 7     # LOOKBACK_DAYS
B5_PRICE_HISTORY_WRITER_MAX_ROWS = 20000      # MAX_ROWS
# 全上場の日次終値（price_universe.py）。取得はチャンクに分けて間隔を空ける（429で全滅させない）。
B5_PRICE_UNIVERSE_CHUNK_SIZE = 300                # CHUNK_SIZE
B5_PRICE_UNIVERSE_CHUNK_PAUSE_SEC = 3.0           # CHUNK_PAUSE_SEC
B5_PRICE_UNIVERSE_CHUNK_RETRY_WAIT_SEC = 60.0     # CHUNK_RETRY_WAIT_SEC
B5_PRICE_UNIVERSE_CHUNK_FAIL_RATE = 0.6           # CHUNK_FAIL_RATE（未満のチャンクは Yahoo の制限中とみなし後回し）
B5_PRICE_UNIVERSE_MAX_FAILED_CHUNKS_IN_ROW = 6    # MAX_FAILED_CHUNKS_IN_ROW（連続失敗で本走をやめ後回し分の再取得へ）
B5_PRICE_UNIVERSE_COOLDOWN_MAX_SEC = 600.0        # COOLDOWN_MAX_SEC（制限中の待ちを倍々に延ばす上限）
B5_PRICE_UNIVERSE_RETRY_CHUNK_SIZE = 100          # RETRY_CHUNK_SIZE（後回し分を取り直すチャンク）
B5_PRICE_UNIVERSE_RETRY_ROUNDS = 2                # RETRY_ROUNDS
B5_PRICE_UNIVERSE_RETRY_ROUND_WAIT_SEC = 300.0    # RETRY_ROUND_WAIT_SEC
B5_PRICE_UNIVERSE_TIME_BUDGET_SEC = 3000.0        # TIME_BUDGET_SEC（ジョブ timeout 90分より短く）
B5_PRICE_UNIVERSE_YF_THREADS = 8                  # YF_THREADS（yfinance の同時接続数）
B5_PRICE_UNIVERSE_MIN_RATE_JP = 0.90              # MIN_RATE_JP
B5_PRICE_UNIVERSE_MIN_RATE_US = 0.85              # MIN_RATE_US
B5_PRICE_UNIVERSE_DEAD_AFTER_MISSES = 3           # DEAD_AFTER_MISSES
B5_PRICE_UNIVERSE_DEAD_RETRY_DAYS = 30            # DEAD_RETRY_DAYS
B5_PRICE_UNIVERSE_REFRESH_DAYS = 7                # UNIVERSE_REFRESH_DAYS
B5_PRICE_UNIVERSE_DAILY_PERIOD = "5d"             # DAILY_PERIOD
B5_PRICE_UNIVERSE_BACKFILL_PERIOD = "1y"          # BACKFILL_PERIOD
B5_PRICE_UNIVERSE_LOAD_BATCH_ROWS = 100000        # LOAD_BATCH_ROWS
B5_PRICE_UNIVERSE_SEC_MIN_INTERVAL_SEC = 0.5      # SEC_MIN_INTERVAL_SEC
# Google CSE の無料枠は100クエリ/日。超過分は課金されるため、
# quota切れの403を待たずにコード側で打ち切る（0で無制限）。
B5_PAID_MEDIA_SEARCHER_CSE_CALL_LIMIT = 90  # _CSE_CALL_LIMIT
B5_US_INSIDER_FORM4_SCRAPER_HTTP_TIMEOUT = 15  # _HTTP_TIMEOUT
B5_US_INSIDER_FORM4_SCRAPER_FETCH_DELAY = 0.5  # _FETCH_DELAY
B5_EDINET_CLIENT_DAILY_BUDGET = 80  # DAILY_BUDGET
B5_PEAD_MONITOR_FETCH_DELAY = 0.5  # _FETCH_DELAY
B5_SELF_EVALUATOR_FETCH_DELAY = 0.25  # FETCH_DELAY
B5_SEC_GEOGRAPHIC_EXTRACTOR_FETCH_TIMEOUT = 30  # _FETCH_TIMEOUT
# SEC のリトライ・レート制限は BLOCK 60（SEC_MAX_RETRIES / SEC_RATE_LIMIT_RPS）。
# B5_SEC_GEOGRAPHIC_EXTRACTOR_FETCH_TIMEOUT は timeout=30 の明示指定で使っている。
B5_ACADEMIC_CONF_TRACKER_REQUEST_TIMEOUT = 20  # _REQUEST_TIMEOUT
B5_EARNINGS_SURPRISE_FETCH_DELAY = 0.3  # FETCH_DELAY
B5_US_8K_MONITOR_REQUEST_TIMEOUT = 10  # REQUEST_TIMEOUT
B5_US_8K_MONITOR_FETCH_DELAY = 0.15  # FETCH_DELAY
B5_FINBERT_ANALYZER_DEFAULT_BATCH_SIZE = 8  # DEFAULT_BATCH_SIZE
B5_DIVIDEND_CALENDAR_FETCH_DELAY = 0.3  # FETCH_DELAY
B5_SUPPORT_RESISTANCE_FETCH_DELAY = 0.3  # FETCH_DELAY
B5_OPTIONS_MONITOR_FETCH_DELAY = 0.4  # FETCH_DELAY
B5_DCF_VALUATION_FETCH_DELAY = 0.3  # FETCH_DELAY
B5_CATALYST_CALENDAR_FETCH_DELAY = 0.5  # _FETCH_DELAY
B5_INDUSTRY_STANDARD_TRACKER_REQUEST_TIMEOUT = 20  # _REQUEST_TIMEOUT
B5_INSIDER_MONITOR_FETCH_DELAY = 0.12  # FETCH_DELAY
B5_PORTFOLIO_DIVERSITY_FILTER_FETCH_DELAY = 0.4  # _FETCH_DELAY
B5_BACKTESTER_FETCH_DELAY = 0.25  # FETCH_DELAY
B5_FACTOR_CROWDING_MONITOR_TOP_N_DEFAULT = 20  # TOP_N_DEFAULT
B5_FACTOR_CROWDING_MONITOR_FETCH_DELAY = 0.15  # FETCH_DELAY
B5_COMPS_ANALYZER_FETCH_DELAY = 0.2  # FETCH_DELAY
B5_EXECUTIVE_TRANSITION_TRACKER_REQUEST_TIMEOUT = 20  # _REQUEST_TIMEOUT
B5_VOLUME_PROFILE_FETCH_DELAY = 0.3  # FETCH_DELAY
B5_PERFORMANCE_TRACKER_RETURN_FETCH_DELAY = 0.25  # RETURN_FETCH_DELAY
B5_TECHNICAL_SCORER_FETCH_DELAY = 0.25  # FETCH_DELAY
B5_US_SECTOR_BRIDGE_ENHANCED_REQUEST_DELAY = 0.12  # REQUEST_DELAY
B5_US_SECTOR_BRIDGE_ENHANCED_REQUEST_TIMEOUT = 10  # REQUEST_TIMEOUT
B5_API_KEY_HEALTH_STATE_LIMITED = 'limited'  # STATE_LIMITED
B5_API_KEY_HEALTH_PROBE_TIMEOUT = 5  # PROBE_TIMEOUT
B5_API_KEY_HEALTH_PROBE_TIMEOUT_SLOW = 15  # PROBE_TIMEOUT_SLOW
B5_POSITION_SIZER_FETCH_DELAY = 0.3  # FETCH_DELAY
B5_STOCK_DETAIL_REPORT_FETCH_DELAY = 0.25  # FETCH_DELAY
B5_COUNTRY_RISK_UPDATER_FETCH_TIMEOUT = 20  # _FETCH_TIMEOUT
B5_COUNTRY_RISK_UPDATER_RETRY_WAIT = 5  # _RETRY_WAIT
B5_COUNTRY_RISK_UPDATER_GDELT_TOTAL_FAIL_LIMIT = 8  # _GDELT_TOTAL_FAIL_LIMIT

# ── モジュール定数（優先度4：投資方針・資金管理・統計モデルの可変数値）────────────────────
B6_LEARNING_AUDIT_LOG_MANUAL_REVIEW_TRIGGER = 2  # MANUAL_REVIEW_TRIGGER
B6_REGIME_DETECTOR_N_STATES = 4  # N_STATES
B6_REGIME_DETECTOR_N_FEATURES = 7  # N_FEATURES
B6_IGNITION_DETECTOR_DEFAULT_CRITICAL_SCORE = 3.5  # _DEFAULT_CRITICAL_SCORE
B6_IGNITION_DETECTOR_DEFAULT_CRITICAL_REPEAT = 14  # _DEFAULT_CRITICAL_REPEAT
B6_IGNITION_DETECTOR_DEFAULT_ALERT_TTL = 14  # _DEFAULT_ALERT_TTL
B6_IGNITION_DETECTOR_DEFAULT_HISTORY_TTL = 180  # _DEFAULT_HISTORY_TTL
B6_NEDO_SELECTION_SCRAPER_REQUEST_INTERVAL = 1.5  # REQUEST_INTERVAL
B6_SIZE_DECAY_MANAGER_DEFAULT_NANO_MAX_PER_DAY = 5  # _DEFAULT_NANO_MAX_PER_DAY
B6_SIGNAL_CULLING_REPORT_T_PROMOTE = 2.0  # T_PROMOTE
B6_MULTI_CURRENCY_MONITOR_SIGNAL_DELTA = 5.0  # SIGNAL_DELTA
B6_JOB_POSTING_MONITOR_REQUEST_WAIT = 2.0  # _REQUEST_WAIT
B6_SCORE_PRIOR_MANAGER_DEFAULT_HALF_LIFE = 14  # _DEFAULT_HALF_LIFE
B6_SCORE_PRIOR_MANAGER_DEFAULT_MIN_OBS = 7  # _DEFAULT_MIN_OBS
B6_ALPHA_ATTRIBUTER_MIN_FIRE_COUNT = 3  # MIN_FIRE_COUNT
B6_FACILITY_EXPANSION_MONITOR_REQUEST_WAIT = 2.0  # _REQUEST_WAIT
B6_RULE_OPTIMIZER_EMA_ALPHA = 0.1  # EMA_ALPHA
B6_RULE_OPTIMIZER_MIN_FIRE_COUNT = 3  # MIN_FIRE_COUNT
B6_RULE_OPTIMIZER_CORR_SCALE = 0.5  # CORR_SCALE
B6_ARXIV_MONITOR_SS_REQUEST_INTERVAL = 3.0  # _SS_REQUEST_INTERVAL
B6_NOTION_OBSIDIAN_WRITER_MAX_NOTION_PAGES = 20  # MAX_NOTION_PAGES
B6_JOGMEC_SCRAPER_MAX_RETRIES = 3  # _MAX_RETRIES
B6_CORRELATION_HEATMAP_MIN_DATA_POINTS = 20  # MIN_DATA_POINTS
B6_IMF_PIPELINE_FETCH_YEARS = 3  # FETCH_YEARS
B6_POLICY_SIGNAL_APPLIER_DEFAULT_EMERGENCY_VIX = 45.0  # _DEFAULT_EMERGENCY_VIX
B6_POLICY_SIGNAL_APPLIER_DEFAULT_L4_MAX_ADJ = 25.0  # _DEFAULT_L4_MAX_ADJ
B6_GOOGLE_TRENDS_PIPELINE_TREND_TAILWIND_PTS = 3.0  # TREND_TAILWIND_PTS
B6_PORTFOLIO_OPTIMIZER_N_SIMULATIONS = 2000  # N_SIMULATIONS
B6_PORTFOLIO_OPTIMIZER_RISK_FREE_RATE = 0.005  # RISK_FREE_RATE
B6_ANOMALY_DETECTOR_MIN_DATA_POINTS = 20  # MIN_DATA_POINTS
B6_JP_US_SECTOR_BRIDGE_CORR_SIGNIFICANT = 0.45  # CORR_SIGNIFICANT
B6_JP_US_SECTOR_BRIDGE_LAG_ADJUSTMENT_PTS = 3.0  # LAG_ADJUSTMENT_PTS
B6_DRIFT_DETECTOR_DEFAULT_DECAY_WEEKS = 4  # _DEFAULT_DECAY_WEEKS
B6_DRIFT_DETECTOR_DEFAULT_HISTORY_TTL = 180  # _DEFAULT_HISTORY_TTL
B6_JP_US_SECTOR_CORRELATION_MIN_DATA_POINTS = 20  # MIN_DATA_POINTS
B6_ANOMALY_SEASON_MIN_SAMPLES = 5  # MIN_SAMPLES
B6_ANOMALY_SEASON_P_SIGNIFICANT = 0.05  # P_SIGNIFICANT
B6_ANOMALY_SEASON_MAX_ADJUSTMENT = 8.0  # MAX_ADJUSTMENT
B6_US_INSIDER_FORM4_SCRAPER_MAX_FORM4_PARSE = 10  # _MAX_FORM4_PARSE
B6_DEEP_DIVE_TRIGGER_DEFAULT_CONFIDENCE_DROP = 15.0  # _DEFAULT_CONFIDENCE_DROP
B6_DEEP_DIVE_TRIGGER_DEFAULT_RANK_JUMP = 10  # _DEFAULT_RANK_JUMP
B6_DEEP_DIVE_TRIGGER_DEFAULT_HISTORY_TTL = 30  # _DEFAULT_HISTORY_TTL
B6_TEXT_SIGNAL_YOY_SCALE_FACTOR = 5.0  # _YOY_SCALE_FACTOR
B6_PEAD_MONITOR_PEAD_DECAY_DAILY = 0.85  # _PEAD_DECAY_DAILY
B6_PEAD_MONITOR_MAX_CANDIDATES = 200  # _MAX_CANDIDATES
B6_PEARL_SCORE_CALCULATOR_D2_CONFIDENCE_DISCOUNT_US = 0.7  # _D2_CONFIDENCE_DISCOUNT_US
B6_PEARL_SCORE_CALCULATOR_MARKET_RETURN_JP = 0.1  # _MARKET_RETURN_JP
B6_PEARL_SCORE_CALCULATOR_MARKET_RETURN_US = 0.15  # _MARKET_RETURN_US
B6_CROSS_SIZE_SIGNAL_JP_US_CROSS_DECAY = 0.7  # _JP_US_CROSS_DECAY
B6_SELF_EVALUATOR_MIN_SAMPLES = 5  # MIN_SAMPLES
B6_SELF_EVALUATOR_MAX_LOG_ENTRIES = 52  # MAX_LOG_ENTRIES
B6_SELF_EVALUATOR_PEARL_BACKTEST_MAX_ENTRY = 52  # PEARL_BACKTEST_MAX_ENTRY
B6_SEC_GEOGRAPHIC_EXTRACTOR_MAX_RETRIES = 3  # _MAX_RETRIES
B6_POLITICIAN_SIGNAL_SCORER_L4_MAX_ADJ = 25.0  # _L4_MAX_ADJ
B6_EARNINGS_SURPRISE_SURPRISE_POS = 3.0  # SURPRISE_POS
B6_EARNINGS_SURPRISE_CONS_HIGH_MEAN = 15.0  # CONS_HIGH_MEAN
B6_EARNINGS_SURPRISE_CONS_MID_MEAN = 10.0  # CONS_MID_MEAN
B6_EARNINGS_SURPRISE_CONS_LOW_MEAN = 0.0  # CONS_LOW_MEAN
B6_EARNINGS_SURPRISE_CONS_HIGH_STD = 10.0  # CONS_HIGH_STD
B6_EARNINGS_SURPRISE_CONS_MID_STD = 15.0  # CONS_MID_STD
B6_EARNINGS_SURPRISE_CONS_LOW_STD = 20.0  # CONS_LOW_STD
B6_EARNINGS_SURPRISE_SURPRISE_COMBO_NEG_ACCEL = 30.0  # SURPRISE_COMBO_NEG_ACCEL
B6_EARNINGS_SURPRISE_SURPRISE_COMBO_NEG_CONSERV = 30.0  # SURPRISE_COMBO_NEG_CONSERV
B6_INVESTMENT_JOURNAL_AUTO_EXIT_WEEKS_DEF = 12  # AUTO_EXIT_WEEKS_DEF
B6_US_8K_MONITOR_MAX_8K_PER_DAY = 200  # MAX_8K_PER_DAY
B6_FINBERT_ANALYZER_MAX_TEXT_LEN = 128  # MAX_TEXT_LEN
B6_FINBERT_ANALYZER_SENTIMENT_STRONG_POS = 0.7  # SENTIMENT_STRONG_POS
B6_FINBERT_ANALYZER_SENTIMENT_STRONG_NEG = 0.3  # SENTIMENT_STRONG_NEG
B6_FINBERT_ANALYZER_SENTIMENT_TAILWIND_PTS = 3.0  # SENTIMENT_TAILWIND_PTS
B6_LONGTERM_SCENARIO_LONGTERM_HOLD_STANCE_RELAX_PT = 5.0  # LONGTERM_HOLD_STANCE_RELAX_PT
B6_EMERGING_TECHNOLOGY_DETECTOR_W_CLUSTER = 0.5  # _W_CLUSTER
B6_EMERGING_TECHNOLOGY_DETECTOR_W_ARXIV = 0.3  # _W_ARXIV
B6_EMERGING_TECHNOLOGY_DETECTOR_W_NEWS = 0.2  # _W_NEWS
B6_ML_SCORE_CORRECTOR_TEST_SPLIT_WEEKS = 4  # TEST_SPLIT_WEEKS
B6_ML_SCORE_CORRECTOR_XGB_PARAMS = {'max_depth': 3, 'n_estimators': 100, 'learning_rate': 0.05, 'subsample': 0.8, 'colsample_bytree': 0.8, 'reg_lambda': 1.0, 'reg_alpha': 0.5, 'random_state': 42, 'n_jobs': -1}  # XGB_PARAMS
B6_CEO_EVALUATOR_CAGR_EXCELLENT = 0.15  # CAGR_EXCELLENT
B6_CEO_EVALUATOR_CAGR_GOOD = 0.08  # CAGR_GOOD
B6_CEO_EVALUATOR_CAGR_NEUTRAL = 0.0  # CAGR_NEUTRAL
B6_CEO_EVALUATOR_MIDTERM_EXCELLENT = 0.9  # MIDTERM_EXCELLENT
B6_CEO_EVALUATOR_MIDTERM_GOOD = 0.7  # MIDTERM_GOOD
B6_CEO_EVALUATOR_MIDTERM_FAIR = 0.5  # MIDTERM_FAIR
B6_SUPPORT_RESISTANCE_MIN_PEAK_DISTANCE = 10  # MIN_PEAK_DISTANCE
B6_SUPPORT_RESISTANCE_CLUSTER_RANGE_PCT = 0.015  # CLUSTER_RANGE_PCT
B6_OPTIONS_MONITOR_VIX_EXTREME_FEAR = 30  # VIX_EXTREME_FEAR
B6_OPTIONS_MONITOR_SKEW_EXTREME = 145  # SKEW_EXTREME
B6_OPTIONS_MONITOR_SKEW_ELEVATED = 130  # SKEW_ELEVATED
B6_DCF_VALUATION_DEFAULT_RF_JP = 0.015  # DEFAULT_RF_JP
B6_DCF_VALUATION_DEFAULT_RF_US = 0.045  # DEFAULT_RF_US
B6_DCF_VALUATION_MARKET_RISK_PREM = 0.055  # MARKET_RISK_PREM
B6_DCF_VALUATION_DEFAULT_BETA = 1.0  # DEFAULT_BETA
B6_DCF_VALUATION_DEFAULT_GROWTH = 0.02  # DEFAULT_GROWTH
B6_DCF_VALUATION_FORECAST_YEARS = 5  # FORECAST_YEARS
B6_DCF_VALUATION_FCF_GROWTH_RATE = 0.05  # FCF_GROWTH_RATE
B6_DCF_VALUATION_WACC_RANGE = [-0.02, 0.0, +0.02]  # WACC_RANGE
B6_DCF_VALUATION_GROWTH_RANGE = [-0.01, 0.0, +0.01]  # GROWTH_RANGE
B6_DCF_VALUATION_DCF_LARGE_DISCOUNT = 20.0  # DCF_LARGE_DISCOUNT
B6_DCF_VALUATION_DCF_SMALL_DISCOUNT = 10.0  # DCF_SMALL_DISCOUNT
B6_SECTOR_ROTATION_DETECTOR_MOMENTUM_DEADZONE = 0.02  # MOMENTUM_DEADZONE
B6_SECTOR_ROTATION_DETECTOR_MIN_DATA_POINTS = 8  # MIN_DATA_POINTS
B6_CONGRESS_MONITOR_DEFAULT_L4_MAX_ADJ = 25.0  # _DEFAULT_L4_MAX_ADJ
B6_SCREENER_CANDIDATE_BUILDER_DYNAMIC_SCREENER_MAX_ADD = 50  # DYNAMIC_SCREENER_MAX_ADD
B6_SHAREHOLDER_ANALYZER_FOREIGN_SPIKE_PCT = 3.0  # FOREIGN_SPIKE_PCT
B6_SHAREHOLDER_ANALYZER_FOREIGN_SURGE_PCT = 5.0  # FOREIGN_SURGE_PCT
B6_SHAREHOLDER_ANALYZER_ACTIVIST_HOLDINGS_PCT = 5.0  # ACTIVIST_HOLDINGS_PCT
B6_PORTFOLIO_DIVERSITY_FILTER_HHI_WARNING_DEFAULT = 0.25  # _HHI_WARNING_DEFAULT
B6_PORTFOLIO_DIVERSITY_FILTER_HHI_CRITICAL_DEFAULT = 0.35  # _HHI_CRITICAL_DEFAULT
B6_PORTFOLIO_DIVERSITY_FILTER_MIN_DATA_POINTS = 20  # _MIN_DATA_POINTS
B6_PORTFOLIO_DIVERSITY_FILTER_MIN_CORR_PAIRS_FOR_ALERT = 3  # _MIN_CORR_PAIRS_FOR_ALERT
B6_BACKTESTER_MIN_RECORDS = 10  # MIN_RECORDS
B6_EXECUTIVE_TRANSITION_TRACKER_REQUEST_WAIT = 1.0  # _REQUEST_WAIT
B6_TAXONOMY_HOTNESS_COMBINE_SEMI = 0.6  # COMBINE_SEMI
B6_TAXONOMY_HOTNESS_COMBINE_LEADING = 0.4  # COMBINE_LEADING
B6_EDINET_DIRECT_FETCHER_XBRL_TYPE = 5  # XBRL_TYPE
B6_VOLUME_PROFILE_VP_NUM_BUCKETS = 30  # VP_NUM_BUCKETS
B6_VOLUME_PROFILE_VALUE_AREA_PCT = 0.7  # VALUE_AREA_PCT
B6_SCREENER_SECTOR_HEATMAP_ROTATION_DELTA = 2  # ROTATION_DELTA
B6_MARKET_BREADTH_US_SAMPLE_SIZE = 100  # US_SAMPLE_SIZE
B6_PERFORMANCE_TRACKER_MIN_CANDIDATES = 3  # MIN_CANDIDATES
B6_TECHNICAL_SCORER_MACD_FAST = 12  # MACD_FAST
B6_TECHNICAL_SCORER_MACD_SLOW = 26  # MACD_SLOW
B6_TECHNICAL_SCORER_MACD_SIGNAL = 9  # MACD_SIGNAL
B6_TECHNICAL_SCORER_BB_STD = 2.0  # BB_STD
B6_TECHNICAL_SCORER_MA_SHORT = 50  # MA_SHORT
B6_TECHNICAL_SCORER_MA_LONG = 200  # MA_LONG
B6_STOCK_SCREENER_DISCOVERY_MCAP_MAX_B = 500.0  # DISCOVERY_MCAP_MAX_B
B6_MOMENTUM_FACTOR_MIN_DATA_POINTS = 5  # MIN_DATA_POINTS
B6_MIDTERM_PLAN_TRACKER_DEFAULT_ACHIEVEMENT_HIGH = 0.9  # _DEFAULT_ACHIEVEMENT_HIGH
B6_MIDTERM_PLAN_TRACKER_DEFAULT_ACHIEVEMENT_RISK = 0.7  # _DEFAULT_ACHIEVEMENT_RISK
B6_POSITION_SIZER_RISK_PCT_DEFAULT = 0.01  # RISK_PCT_DEFAULT
B6_POSITION_SIZER_MIN_POSITION_PCT = 0.02  # MIN_POSITION_PCT
B6_POSITION_SIZER_MAX_POSITION_PCT = 0.15  # MAX_POSITION_PCT
B6_POSITION_SIZER_LOT_SIZE_JP = 100  # LOT_SIZE_JP
B6_POSITION_SIZER_CONVICTION_PCT_RANGE = {5: (0.1, 0.15), 4: (0.06, 0.1), 3: (0.03, 0.06), 2: (0.01, 0.03), 1: (0.0, 0.0)}  # CONVICTION_PCT_RANGE
B6_POSITION_SIZER_REGIME_SIZE_MATRIX = {0: 1.0, 1: 0.75, 2: 0.5, 3: 0.0}  # REGIME_SIZE_MATRIX
B6_POSITION_SIZER_TRADING_DAYS_V2 = 252  # _TRADING_DAYS_V2
B6_COUNTRY_RISK_UPDATER_MAX_RETRIES = 3  # _MAX_RETRIES
B6_COUNTRY_RISK_UPDATER_GDELT_MAX_COUNTRIES = 15  # _GDELT_MAX_COUNTRIES
B6_STRATEGY_OUTPUT_SCHEMA_SATELLITE_SMALLCAP_DISCOVERY = 80  # SATELLITE_SMALLCAP_DISCOVERY

# ==============================================================
# BLOCK 60: SEC共通アクセス層設定（sec_client.py）
# ==============================================================
# SEC EDGAR へのアクセス（User-Agent・CIK解決・レート制限・リトライ・キャッシュ）は sec_client.py に集約。
#   SEC_RATE_LIMIT_RPS: 0.5秒間隔（=2 req/s）。SECのアクセス規約内で最も保守的な値。
#   TIMEOUT: 汎用HTTPは15秒。XBRL等の特大レスポンスは各モジュールが timeout= で個別指定する。
SEC_RATE_LIMIT_RPS: float = 2.0      # 現行の FETCH_DELAY=0.5 相当（最も保守的）
SEC_HTTP_TIMEOUT: int = 15           # 現行の最大値（通常経路。XBRL等の特大取得を除く）
SEC_CIK_MAP_TTL_DAYS: int = 7        # us_insider_form4_scraper の _TICKER_MAP_TTL 準拠
SEC_MAX_RETRIES: int = 3

# ==============================================================
# BLOCK 61: 市場別プロファイル索引（U-8）
# ==============================================================
# 既存の *_JP / *_US 設定値は削除・複製しない。市場横断コードが必要とする
# 設定名をここで索引化し、実値は各定数を正本とする。
# revision_multiplier / revision_cap は screener_common.py にだけ存在していた
# 市場差を移したもの。既存の JP/US スコア挙動は変えない。
MARKET_PROFILE: dict = {
    "JP": {
        "suffix": ".T",
        "revision_multiplier": 6.0,
        "revision_cap": 3.0,
        "size_thresholds": "SIZE_TIER_THRESHOLDS_JP",
        "slippage_bps": "BT_SLIPPAGE_BPS_JP",
        "defensive_sectors": "DEFENSIVE_SECTOR_CODES_JP",
        "sector_taxonomy": "TSE33",
    },
    "US": {
        "suffix": "",
        "revision_multiplier": 10.0,
        "revision_cap": 5.0,
        "size_thresholds": "SIZE_TIER_THRESHOLDS_US",
        "slippage_bps": "BT_SLIPPAGE_BPS_US",
        "defensive_sectors": "DEFENSIVE_GICS_SECTORS_US",
        "sector_taxonomy": "GICS11",
    },
}

# ==============================================================
# BLOCK 62: 全上場2段階スクリーナー（screener_universe.py / stock_screener.py）
# ==============================================================
# 一次採点: BQ の全上場を既存の評価部品（テクニカル・不動産ボーナス・アクルーアル tier・各キャッシュの加点）で採点し、
#   上位 SCREEN_STAGE2_TOP_N 社だけを従来の詳細評価（yfinance 等）に回す。規模（時価総額）はどの観点にも入れない。
# 観点の重み（合計は実行時に1へ正規化）。値が無い観点は中立50で coverage に記録する（不利にしない）。
ENABLE_SCREENER_UNIVERSE: bool = True
SCREEN_UNIVERSE_WEIGHTS: dict = {
    "move":      0.10,   # ① 前日の値動き（騰落・出来高倍率・20日高値との距離）
    "momentum":  0.15,   # ② モメンタム（1か月・3か月・12か月−1か月・52週高値への近さ）
    "technical": 0.10,   # ③ technical_scorer の採点（＋ SR/VP 加点はキャッシュのある銘柄だけ）
    "asset":     0.10,   # ④ 簿価・資産の裏付け（stock_real_estate の不動産ボーナス・資産バリュー倍率・修正PBR）
    "quality":   0.15,   # ⑤ 質（アクルーアル tier・EDINET品質・ROIC・CF率・F-score）
    "growth":    0.15,   # ⑥ 成長・業績イベント（成長率・増益年数・決算サプライズ）
    "value":     0.10,   # ⑦ 割安（益回り・FCF利回り・EV/EBITDA・総還元利回り・DCF・コンプス）
    "flow":      0.05,   # ⑧ 需給・株主（信用残・インサイダー・配当）
    "theme":     0.05,   # ⑨ テーマ（銘柄ごとの max(関与度×ノード熱度)）
    "sector":    0.05,   # ⑩ 業種スコア（BOT_TIER の業種も点数としては入れる）
}
SCREEN_UNIVERSE_MIN_TURNOVER: float = 10_000_000.0  # 20日平均売買代金（円/日）の下限。時価総額では外さない
SCREEN_UNIVERSE_STALE_DAYS: int = 14                # 最新株価が全体の最新日からこの日数より古い銘柄は対象外
SCREEN_UNIVERSE_PRICE_DAYS: int = 440               # 日足を読む期間（暦日。祝日を除いて約270営業日＝12か月−1か月の騰落に要る253営業日を超える）
SCREEN_UNIVERSE_LEADING_BONUS: float = 15.0         # 先行指標だけ点灯したノードの熱度に足す点（上限100）
# 既存の加点（pt）を0〜100へ写すときの上限。50＋50×pt/上限（0ptが中立50）
SCREEN_UNIVERSE_BONUS_CAPS: dict = {
    "re": 3.0, "accrual": 20.0, "edinet": 10.0, "surprise": 10.0, "dcf": 10.0, "comps": 10.0,
    "credit": 5.0, "insider": 10.0, "dividend": 5.0, "sr": 10.0, "vp": 10.0,
}
SCREEN_STAGE2_TOP_N: int = 200            # 一次上位のうち二次評価に回す社数
SCREEN_BLEND_UNIVERSE: float = 0.5        # final_score = これ×一次スコア + (1−これ)×二次の composite_score
SCREEN_UNIVERSE_SEED_N: int = 20          # 一次上位から EDINET_SCREENER_CANDIDATES（個別キャッシュの育成）へ渡す社数
SCREEN_UNIVERSE_TOP_REPORT_N: int = 50    # results["screener_universe"] に載せる上位件数


# ==============================================================
# BLOCK 63: 投資審議の DB キュー（deliberation_queue.py / tools/invest_queue.py）
# ==============================================================
# 日次でスクリーナーの最終候補から審議キューを作り BQ invest.deliberation_queue へ書く。
# 隔週の審議は Claude Code のクラウド定時実行が invest-dept スキル（クラウド審議モード）でキューを読み、
# 4エージェントの結果を invest.deliberations に書く。間隔は DELIBERATION_INTERVAL_DAYS（BLOCK 39）。
ENABLE_DELIBERATION_QUEUE: bool = True
DELIB_QUEUE_SIZE: int = 10          # 1日にキューへ入れる件数
DELIB_RECHECK_DAYS: int = 90        # 前回審議からこの日数を超えたら再審議の対象（保有の見直しにも使う）
DELIB_RESCORE_JUMP: float = 15.0    # 前回審議時より final_score がこの点以上上がったら再審議の対象
DELIB_HOLDING_MAX: int = 3          # キューに入れる保有見直しの上限
DELIB_PACKET_TOP_N: int = 5         # 審議パケットを作ってキューに保存する上位件数
DELIB_BATCH_SIZE: int = 3           # クラウド定時実行1回で審議する件数


# ==============================================================
# BLOCK 64: 状態の置き場（state_store.py）
# ==============================================================
# Actions が実行のたびに持ち越す状態（stock_data/ と保有銘柄のファイル）の置き場。
#   git  : git に commit・push して持ち越す（BQ には書かない）
#   dual : git にも commit しつつ BQ にも保存する。読み出し（checkout 後の状態）は git のまま
#   bq   : checkout 直後に BQ から戻し、実行後は BQ だけに保存する（git には commit しない）
# workflow は env の STATE_STORE_MODE（Actions の変数 vars.STATE_STORE_MODE、未設定なら bq）で上書きする。
STATE_STORE_MODE: str = os.getenv("STATE_STORE_MODE", "bq")
STATE_STORE_DATASET: str = "private"
STATE_STORE_TABLE: str = "state_files"
STATE_STORE_LOCATION: str = "asia-northeast1"
# 保存する対象（リポジトリ直下からの相対パス。末尾 / はフォルダごと）。
# stock_data/bi/ の parquet も入れる: マートから作り直せるが、作り直しは1マート1クエリかかり、
# Mac の board（/bi）は parquet を直接読むため。外部 API 由来の財務も含むので public の git には置けない。
STATE_STORE_PATHS: tuple = (
    "stock_data/",
    "data/portfolio_holdings.csv",
    "data/portfolio_summary.json",
    "data/holdings_domain.csv",
)
# 入れないもの（.gitignore と同じ重量物・作り直せる生成物）。前方一致。名前だけのものは全階層で一致
STATE_STORE_EXCLUDE: tuple = (
    "stock_data/models/",
    "stock_data/data/us_fundamentals_db/",
    "__pycache__/",
    ".DS_Store",
)
# gzip 後のバイト列をこの大きさで分けて1行ずつ持つ（行の上限 100MB と、tabledata.list の1応答の大きさに余裕を持たせる）
STATE_STORE_CHUNK_BYTES: int = 4 * 1024 ** 2
# 1回の load job に載せる量の目安（超えたら load job を分ける）
STATE_STORE_LOAD_BATCH_BYTES: int = 256 * 1024 ** 2
# 掃除（state_store.py prune）でパスごとに残す版の数
STATE_STORE_KEEP_VERSIONS: int = 3
# 掃除のクエリの上限（dryRun で見積もってこれを超えたら実行しない）
STATE_STORE_PRUNE_MAX_BYTES: int = 1024 ** 3

# ==============================================================
# BLOCK 65: 政府 EDINET の書類一覧と書類の中身（edinet_gov_docs.py）
# ==============================================================
# 書類一覧（documents.json）は全書類（投信を含む）を日ごとに edinet.gov_documents へ入れる（1日1回の呼び出し）。
# 中身（CSV＝XBRL の事実）は EDINET_GOV_FACT_DOC_TYPES の書類だけ edinet.gov_doc_facts へ入れる。
#   350 大量保有報告書 / 360 変更報告書 / 370 訂正報告書（大量保有）/ 180 臨時報告書 / 190 訂正臨時報告書
# 過去分は新しい日から古い日へ遡る。下限は EDINET_GOV_BACKFILL_FLOOR（API が出す範囲より古い日は空で返る）。
# BigQuery の保存が BQ_STORAGE_STOP_GB（全体・BLOCK 66）か EDINET_GOV_FACTS_MAX_GB（この表）を超えたら中身の取り込みを止める。
EDINET_GOV_FACT_DOC_TYPES: tuple = ("350", "360", "370", "180", "190")
EDINET_GOV_BACKFILL_FLOOR: str = "2016-01-01"
EDINET_GOV_RECENT_DAYS: int = 7            # 直近この日数は毎回一覧を取り直す（取り下げ・訂正・遅れて出る書類）
EDINET_GOV_TEXT_MAX_CHARS: int = 2000      # 文字の値（テキストブロック等）はここで切る
EDINET_GOV_FACTS_MAX_GB: float = 2.5       # edinet.gov_doc_facts だけでこれを超えたら中身を取らない（他のデータ源の余地を残す）
EDINET_GOV_DOCS_TIME_BUDGET_MIN: float = 30.0   # 1回の実行で使う時間（日次の取得 workflow の既定）

# ==============================================================
# BLOCK 66: BigQuery の保存量の上限（bq_storage.py）
# ==============================================================
# 無料枠は保存 10GB/月で、その月に保存している合計にかかる（溜めた分は毎月数え直される）。合計を 10GB 未満に保つ。
# データを溜める取り込みは、全表の論理サイズが BQ_STORAGE_STOP_GB 以上なら書かない。
# 日次メールの「運用の監視」は BQ_STORAGE_WARN_GB 以上で注意、BQ_STORAGE_HIGH_GB 以上で重要として出す。
BQ_STORAGE_STOP_GB: float = 8.0
BQ_STORAGE_WARN_GB: float = 7.0
BQ_STORAGE_HIGH_GB: float = 9.0

# ==============================================================
# BLOCK 67: 取得系 workflow の終了通知（workflow_notify.py）
# ==============================================================
# 取得系の workflow（fetch_jp_filings・edinet_gov_backfill・price_universe・us_company_facts・manual_task・
# edinetdb_web_backfill）の最後に、取り込んだ内容（ステップの成否・要点・表の行数の増減・保存量）を Gmail で送る。
# 日次（daily_run）は自分のメールがあるので送らない。
WORKFLOW_NOTIFY_ENABLED: bool = True

# ==============================================================
# BLOCK 68: SEC EDGAR の提出一覧・8-K の項目・13D/13G（sec_filings.py）
# ==============================================================
# 日次の提出一覧（daily-index/master.YYYYMMDD.idx・全書類）を us.sec_filings へ日ごとに入れ替えて溜める。
# 8-K（と 8-K/A）は書類の目次ページから項目番号（2.02 決算・5.02 役員交代 等）を読む。
# 13D/13G は XML 形式（2024-12 以降の SCHEDULE 13D/13G）を読み、報告者ごとに us.sec_13dg へ入れる。
# SEC は1秒10回まで（SEC_USER_AGENT で名乗る）。ここでは SEC_FILINGS_MIN_INTERVAL_SEC 空ける。
SEC_FILINGS_BACKFILL_FLOOR: str = "2016-01-01"
SEC_FILINGS_RECENT_DAYS: int = 5
SEC_FILINGS_MIN_INTERVAL_SEC: float = 0.15
SEC_FILINGS_MAX_GB: float = 0.6            # us.sec_filings＋sec_13dg＋sec_filers の合計の上限（米国の配分 1.0GB の内）
SEC_FILINGS_TIME_BUDGET_MIN: float = 20.0

# ==============================================================
# BLOCK 69: 有報の本文（政府 EDINET・edinet_gov_text.py）
# ==============================================================
# 全上場の最新の有報から節ごとの本文を edinet.gov_text_blocks へ（技術マップの語の照合・分析の材料）。
EDINET_GOV_TEXT_TIME_BUDGET_MIN: float = 60.0
EDINET_GOV_TEXT_SECTION_MAX_CHARS: int = 20000   # 1節の上限（超えた分は切る）
EDINET_GOV_TEXT_MAX_GB: float = 0.4              # この表の上限（1年で約 0.1GB の見込み）

# ==============================================================
# BLOCK 70: 技術マップのまとめ（tech_profile.py）
# ==============================================================
# スクリーナー通過銘柄に付いた技術ノードを熱度の高い順に並べ、メールの「🧭 技術ノードの熱度」節と審議パケット⑬に出す。
# 点数（スクリーナーの theme 観点・業種スコア）への重みは変えない（材料を届けるだけ）。
ENABLE_TECH_MAP_DIGEST: bool = True
TECH_MAP_DIGEST_NODES: int = 5       # メールに出すノード数
TECH_MAP_DIGEST_PER_NODE: int = 5    # 1ノードに出す候補銘柄の数

# ==============================================================
# BLOCK 71: EdinetDB の追加の窓口（edinetdb_extras.py）
# ==============================================================
# API の1日100回のうち、この回数を本体の取り込みから残して、追加の窓口（決算短信・政策保有株・関係会社・主要販売先）に使う
# 20回だと、本体の年次財務の一巡は約190日（0回なら約150日）。
EDINETDB_EXTRAS_DAILY_CALLS: int = 20
EDINETDB_EXTRAS_MAX_GB: float = 0.3

# ==============================================================
# BLOCK 72: 技術マップのセグメント経由の付け方（taxonomy_company_mapper.run_segment_path）
# ==============================================================
# セグメント名→業種の当て方の類似度がこれ未満なら使わない（業種の配下のノードを全部付けるので、弱い当て方は誤りが多い）
TAXONOMY_SEGMENT_MIN_SIM: float = 0.75
# セグメントがこの社数以上取れた回だけ、今回出なかった古いセグメント経由の行を失効させる
TAXONOMY_SEGMENT_RETIRE_MIN_COMPANIES: int = 200

# ==============================================================
# BLOCK 73: 日本の大量保有の監視（insider_monitor._extract_jp_large_holdings）
# ==============================================================
# 政府 EDINET の大量保有報告書（edinet.large_holdings）のうち、直近この日数に出た報告を見る
INSIDER_JP_LOOKBACK_DAYS: int = 90

# ==============================================================
# BLOCK 74: 需給のデータ（market_positioning.py：FINRA の空売り残・CFTC の建玉明細）
# ==============================================================
SHORT_INTEREST_BACKFILL_FLOOR: str = "2022-01-01"   # 取引所の上場株だけで1年 約30MB
COT_BACKFILL_FLOOR: str = "2015-01-01"
POSITIONING_RECENT_PERIODS: int = 2                  # 直近この回数（月・週）は毎回取り直す（訂正・遅れて出る分）
POSITIONING_MIN_INTERVAL_SEC: float = 1.0
POSITIONING_TIME_BUDGET_MIN: float = 30.0
POSITIONING_MAX_GB: float = 0.3

# ==============================================================
# BLOCK 75: 米国スクリーナーの XBRL 補完を us.company_facts から作る（us_facts_quarterly.py）
# ==============================================================
# 四半期の希薄化 EPS（Q4 は通期－Q1〜Q3）と直近12か月の FCF を全社分作り、EPS の加速と FCF の補完に使う。
# 1回の実行で1本だけ読む（約0.7GB・クエリの無料枠 1TB/月 の約2%）。無効なら us.fundamentals（約800社）だけ。
US_FACTS_COMPLEMENT_ENABLED: bool = True
US_FACTS_COMPLEMENT_YEARS: int = 4      # 何年分の期間を読むか（EPS の加速は8四半期を使う）
