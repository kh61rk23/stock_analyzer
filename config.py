# ==============================================================
# config.py
# 自律型投資AIエージェント基盤 — 設定ファイル v2.0
# 【v2.0 変更点】
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
    INSIGHT_DIR / "ideas",
    INSIGHT_DIR / "edinet_rotation",
    INSIGHT_DIR / "screener",
]

# ==============================================================
# BLOCK 1: API / 実行設定
# ==============================================================

SPREADSHEET_ID   = "1CePiavYj_6OR0cOz84V4RkA0c0S6IwZlcHCv8_bsOjc"
CREDENTIALS_PATH = str(VIEWER_BASE / "service_account.json")
WRITE_SHEET      = True   # True にすると Google Sheets に書き込む

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
    # [A-6] 多通貨モニタリング追加（日次で自動取得）
    "eurjpy":  "EURJPY=X",    # EUR/JPY
    "cnyjpy":  "CNYJPY=X",    # CNY/JPY（人民元）
    "audjpy":  "AUDJPY=X",    # AUD/JPY（リスクオン/オフ先行指標）
    "gbpjpy":  "GBPJPY=X",    # GBP/JPY（英国系製薬・金融）
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

EDINET_API_KEY: str = "REDACTED_EDINET_API_KEY"  # ここは金融庁のEDINETになる
EDINETDB_API_KEY: str = "REDACTED_EDINETDB" # ここは民間のEDINETDBなので使う
ESTAT_API_KEY: str = "REDACTED_ESTAT_API_KEY"

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
ENABLE_EDINET_ROTATION: bool   = True # ローテーション実行中
EDINET_ROTATION_PER_DAY: int   = 33   # 1日あたりのローテーション取得社数
EDINET_ROTATION_CACHE_DAYS: int = 30  # analysis再取得間隔（日）
EDINET_DAILY_BUDGET: int        = 80  # 1日の最大APIコール数（バッファ20）
EDINET_RETRY_BUDGET: int        = 5   # リトライ用予備コール数
EDINET_WATCHLIST_TICKERS: list  = []  # 空ならPRIORITY_TICKERSを参照
EDINET_SCREENER_CANDIDATES: list = [] # 前日screener通過銘柄（自動注入される）

# EDINETDB を有効化するフラグ（APIキーが設定されていれば自動的に有効）
ENABLE_EDINET: bool = False  
# APIキー設定後に True に変更。
# 現在はEDINETDBの業種ごとの検索を止めており、
# 別フラグの箇所にて、EDINETDBを動かしている。

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
# BLOCK 10: 政策金利トラッカー設定（3-C 追加）
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
POLICY_FRED_API_KEY: str = "REDACTED_POLICY_FRED_API_KEY"    # ← 任意。取得後ここに貼り付ける

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

ENABLE_SCREENER: bool = True         # スクリーニング有効化フラグ
ENABLE_RULE_OPTIMIZER:      bool = True   # ルール重み最適化 [4-8]
ENABLE_JP_US_BRIDGE:        bool = True   # JP×US横断分析
ENABLE_SCREENER_FEEDBACK:   bool = True   # screenerフィードバックループ
ENABLE_ANOMALY_SEASON:  bool = True   # 季節アノマリー [4-4]
ENABLE_MARKET_BREADTH: bool = True   # 市場幅指標 [3-7]
ENABLE_CREDIT_DATA:      bool  = True    # 信用残高データ取得 [3-5]
CREDIT_SQUEEZE_THRESH:   float = 1.0     # 踏み上げ候補の倍率閾値
CREDIT_OVERHEAT_THRESH:  float = 10.0   # 過熱（崩落リスク）の倍率閾値
ENABLE_CORRELATION_HEATMAP: bool = True   # 相関ヒートマップ [3-2]
ENABLE_SCENARIO_ANALYSIS: bool = True   # シナリオ分析（フェーズ3）
ENABLE_PERFORMANCE_TRACKER: bool = True      # パフォーマンストラッカー有効化（2-3追加）
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
SEC_USER_AGENT: str = "StockAnalyzer REDACTED_EMAIL"
SUMMARY_MAX_TICKERS: int = 10         # 要約する最大銘柄数（コスト管理）

# Step C': テーマETFトラッキング設定
ENABLE_ETF_TRACKER: bool = True     # テーマETFトラッキングの有効化
# Step D: Excel ダッシュボード CSV エクスポート
ENABLE_EXCEL_EXPORT: bool = True  # CSV エクスポートの有効化
ETF_TOP_N: int = 3                  # アラートに表示するトップテーマ数
ETF_THEME_IDS = None                # None=全テーマ / 例: ["quantum","ai_infra"]

# ==============================================================
# BLOCK 14: ニュースRSSパイプライン設定（Step B）
# ==============================================================
ENABLE_NEWS: bool = True          # ニュースRSS取得の有効化
# Google Custom Search API（有料メディア記事検索）
GOOGLE_CSE_API_KEY: str = "REDACTED_GOOGLE"   # Google Cloud Console で取得
GOOGLE_CSE_ID:      str = "033b43821c3a5442b"   # cse.google.com で取得
ENABLE_PAID_MEDIA_SEARCH: bool = True  # API設定後に True に変更

# Step C: 個別株スクリーニング設定
SCREEN_TOP_N_SECTORS: int   = 7      # スクリーニング対象業種数（スコア上位N）
SCREEN_PER_MAX:       float = 30.0   # PER 上限
SCREEN_PBR_MAX:       float = 5.0    # PBR 上限
SCREEN_ROE_MIN:       float = 0.05   # ROE 下限（5% = 0.05）
SCREEN_MCAP_MIN_B:    float = 80.0  # 時価総額下限（80億）
SCREEN_VOL_MIN:       int   = 50_000 # 出来高下限（5万株/日）

NEWS_TOP_N:  int  = 10            # アラートに表示する重要記事の最大件数
NEWS_FEEDS_ENABLED = None         # None=全フィード有効 / リスト指定で絞り込み
                                  # 例: ["reuters_business", "nhk_top"]
NEWS_ALERT_MAX_ARTICLES: int = 5  # Gmailアラートに含める記事数

# ==============================================================
# BLOCK 13: 自律サイクル設定（4-C 追加）
# ==============================================================

# ── デイリーサイクル（毎朝自動実行）──────────────────────────
# 実行対象モジュールキー（ENABLE_* フラグが True の場合のみ実際に動く）
DAILY_MODULES = [
    "gov",           # 政府データパイプライン
    "macro",         # マクロ指標取得
    "sector",        # セクターエージェント
    "tdnet",           # TDnet 重要適時開示モニター（M-4）
    "economic_cycle",  # 経済サイクルフェーズ判定（D-1 / ダリオ4象限）
    "timeline",      # 月次トレンド＋デイリー騰落
    "policy",        # 政策金利トラッカー
    "us_sector",     # 米国S&P500 11セクター分析
    "bond_commodity",  # 債券イールドカーブ + 全コモディティ分析
    "us_macro",        # ★ US マクロ拡張（Phase US-2）
    "anomaly",         # 統計的異常値検出（価格・出来高・スコア）
    "imf",           # IMF グローバルマクロパイプライン
    "etf_tracker",   # テーマETFトラッキング（Step C'）
    "news",          # ニュースRSSパイプライン（Step B）
    "screener",      # スキャナーの追加
    "excel_export",  # Excel ダッシュボード CSV エクスポート（Step D）
    "signal_aggregator",  # 統合シグナル [4-9]
    "inference",          # 推論エンジン [4-10]
    "us_screener",   # 米国株スクリーニング0310
    "edinet_rotation", # edinetのローテーション
    "us_fundamentals", # ★ US財務DBローテーター（Phase US-1）
    "us_8k",           # ★ US 8-K リアルタイム監視（Phase US-3）
    "us_sector_bridge",  # ★ US-5 SIC/JP業種連携
    "multi_currency", # 多通貨モニタリング [A-6]
    "options_monitor", # オプション市場センチメント [4-5]
    "support_resistance",   # サポート・レジスタンス検出 [4-6]
    "volume_profile",   # 出来高プロファイル分析 [4-7]
    "notion_obsidian",   # Notion/Obsidian書き込み [6-4]（毎日実行）
    "policy_radar",     # ★ 政策レーダー追加
]

# ── ウィークリーサイクル（月曜朝に追加実行）───────────────────
WEEKLY_MODULES = [
    "arxiv",         # ArXiv 論文取得
    "semantic_scholar", # ★ 論文追加分析
    "correlation",   # 米国債×業種 相関監査
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
    "longterm_scenario",   # 10年単位のシナリオ R-5追加
    "google_trends",    # ★ RESEARCH-3追加
    "finbert",          # ★ RESEARCH-5追加
    "factor_crowding",  # ★ RESEARCH-2追加
]

# ウィークリー実行の曜日（0=月曜 〜 6=日曜）
WEEKLY_RUN_WEEKDAY: int = 0

# ==============================================================
# BLOCK 14: アラート通知設定（5-B 追加）
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
BOND_COMMODITY_CACHE_DAYS: int = 1   # コモディティデータキャッシュ保持日数

# コモディティ → composite_score 補正の最大値（業種ごとの上限）
BOND_COMMODITY_DELTA_CAP: float = 15.0

# ==============================================================
# BLOCK 15: Step E代替 - 事業概要フェッチャー設定
# ==============================================================

# 有効化フラグ（DeepL APIキーなしでも英語原文モードで動作）
ENABLE_BIZ_SUMMARY: bool = True

# DeepL API 無料キー（末尾が :fx のもの）
# 取得: https://www.deepl.com/pro-api （無料枠: 50万字/月）
DEEPL_API_KEY: str = "REDACTED_DEEPL_API_KEY"  # 例: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx"

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

# アナリスト強気推奨アラート
ALERT_ON_STRONG_BUY: bool = True

# 財務リスクウォッチ（高負債・FCFマイナス・低流動比率）
ALERT_ON_FINANCIAL_RISK: bool = True

ALERT_ON_MACRO_DELTA:    bool = True   # マクロ急変アラート [GAP-7]
ALERT_ON_BUYBACK:        bool = True   # 自己株式取得アラート [M-4]
ALERT_ON_DIV_UP:         bool = True   # 増配・特別配当アラート [M-4]
ALERT_ON_FOREIGN_SPIKE:  bool = True   # 外国人持株急増アラート [P-4]

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
# BLOCK 18: stock_viewer Googleスプレッドシート連携
# ==============================================================

# viewer と共有しているスプレッドシートID
# （viewer の サイドバー → SpreadsheetID に表示されているもの）
VIEWER_SPREADSHEET_ID: str = "1IedVe2bSPerao6oGvhOX-nZKtQbR_M6c"  # 例: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

# service_account.json のパス（GitHub Actionsではシークレットから生成）
VIEWER_SERVICE_ACCOUNT_PATH: str = "service_account.json"

# ウォッチリストのシート名（viewer側のシート名に合わせる）
VIEWER_WATCHLIST_SHEET: str = "watchlist"

# ==============================================================
# BLOCK 19: 米国株スクリーニング設定（BLOCK 19）
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

# ==============================================================
# BLOCK 22: ポジションサイズ設定 / ストレステスト / フェーズ4〜6 フラグ群
# ==============================================================
TOTAL_ASSETS_JPY:       float = 10_000_000.0  # 総資産額（円）← 実際の金額に変更
POSITION_RISK_PCT:      float = 0.10          # 1トレードのリスク許容率（10%）
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
ENABLE_COMPS_ANALYZER:      bool = True   # 同業種内比較 [5-2]
ENABLE_BACKTESTER:          bool = True   # バックテスト [5-3]
ENABLE_SIGNAL_AGGREGATOR: bool = True   # 統合シグナル [4-9]
ENABLE_INFERENCE_ENGINE:  bool = True   # 推論エンジン [4-10]
ENABLE_SELF_EVALUATOR:    bool = True   # 自己検証 [5-5]
ENABLE_SCREENER_HEATMAP:  bool = True   # セクターヒートマップ
ENABLE_PORTFOLIO_OPTIMIZER: bool = True   # ポートフォリオ最適化 [6-1]
ENABLE_DCF_VALUATION:      bool = True    # DCF理論株価 [6-3]
ENABLE_NOTION_OBSIDIAN:    bool = True    # Notion/Obsidian連携 [6-4]

# Notion API設定（未設定でもObsidianのみ動作）
NOTION_API_KEY:            str  = "REDACTED_NOTION"     # Notion Integration Secret
NOTION_DATABASE_ID:        str  = "331b571e4f7e8092b27dded753e5c3fe"     # スクリーナー銘柄DB の ID

# Obsidian Vault パス（Google Drive上）
# 例: DRIVE_BASE / "obsidian_vault"
OBSIDIAN_VAULT_DIR:        str  = ""     # 空の場合DRIVE_BASE/stock_analyzer_vaultを使用
OBSIDIAN_VAULT_DIR = str(DRIVE_BASE / "obsidian_vault")

# signal_aggregatorのデフォルト重み（rule_optimizerが月次で調整）
SIGNAL_WEIGHTS = {
    "sector_score":      0.30,
    "macro_signals":     0.18,
    "technical_score":   0.15,
    "market_breadth":    0.10,
    "options_sentiment": 0.08,
    "anomaly_season":    0.06,
    "us_lead_signal":    0.07,
    "etf_heat":          0.03,
    "yield_curve":       0.03,
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
LONGTERM_HOLD_EQUITY_RATIO_MIN: float = 0.45  # 自己資本比率45%以上
LONGTERM_HOLD_ROE_MIN:          float = 0.08  # ROE 8%以上
LONGTERM_HOLD_MIN_CONDITIONS:   int   =  3    # 有効条件の最低充足数

# ==============================================================
# BLOCK I-2: 会社予想保守度スコア設定
# ==============================================================
ENABLE_CONSERVATISM_SCORE: bool     = True
CONSERVATISM_MIN_QUARTERS: int      = 4     # 最低何四半期で計算するか
ALERT_ON_HIGH_PROB_SURPRISE: bool   = True  # 決算サプライズ高確率候補アラート
ALERT_ON_NEGATIVE_RISK: bool        = True  # ネガティブリスクアラート

# ==============================================================
# BLOCK I-3: 経営者スコア設定
# ==============================================================
ENABLE_CEO_EVALUATOR: bool    = True
CEO_MIN_TENURE_YEARS: int     = 2      # 何年以上で暫定スコアを出すか
CEO_QUALITY_BONUS_HIGH: float = 10.0   # ceo_score >= 80 → quality_score +10pt
CEO_QUALITY_BONUS_MID:  float =  5.0   # ceo_score >= 65 → quality_score +5pt
M_A_ROIC_THRESHOLD: float     = 0.08   # M&A のROIC基準（8%以上を成功とみなす）

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
DRIFT_ALERT_THRESHOLD: float    = 15.0  # 乖離アラート発動閾値（pt）
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
ENABLE_MIDTERM_TRACKER:    bool  = True
MIDTERM_ACHIEVEMENT_HIGH:  float = 0.90   # 90%以上 → 高達成
MIDTERM_ACHIEVEMENT_RISK:  float = 0.70   # 70%未満（最終2年前時点）→ 未達リスク

# ==============================================================
# BLOCK P-4: 株主構成分析設定
# ==============================================================
ENABLE_SHAREHOLDER_ANALYZER: bool   = True
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
SECTOR_TIER_TOP_THRESHOLD:  float = 65.0   # TOP_TIER: この値以上
SECTOR_TIER_MID_THRESHOLD:  float = 45.0   # MID_TIER: この値以上 TOP未満
# この値未満は BOT_TIER（screener対象外）

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
SCORE_MIN_COMPOSITE:   float =  0.0   # ★ -30 → 0 に変更（スケール統一）
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
ENABLE_IC_TRACKER:         bool  = True
IC_STABLE_MIN_RATE:        float = 0.80   # 80%以上でIC > 0 = 安定シグナル
IC_REVIEW_CONSECUTIVE_NEG: int   =  3     # 3週連続IC < 0 で見直し推奨

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
US_DAILY_FETCH_LIMIT:      int   = 20    # 1日あたり最大取得社数
US_FUNDAMENTALS_CACHE_DAYS: int  = 30   # 財務データの再取得間隔（日）
US_WATCHLIST_TICKERS:      list  = [    # 優先取得銘柄（watchlist）
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "BRK-B", "JPM", "V",
]

# ==============================================================
# BLOCK US-2: 米国マクロ拡張パイプライン設定
# ==============================================================
ENABLE_US_MACRO: bool = True

# FRED APIキーは既存の POLICY_FRED_API_KEY を共用（再設定不要）

# composite_us_macro_score の sector スコアへの反映上限（pt）
US_MACRO_MAX_SECTOR_ADJ: float = 5.0

# Treasury API から国債残高を取得するか（APIキー不要）
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
# BLOCK 問題C: EDINET × sector_tier 連携設定
# ==============================================================
EDINET_TIER_TOP_BOOST:  int =  30   # TOP業種の優先度スコア加算
EDINET_TIER_BOT_PENALTY: int = -20  # BOT業種の優先度スコア減算

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
# BLOCK B-2: 政府統計先行指標ルール設定
# ==============================================================
ENABLE_GOV_RULES: bool = True

# ==============================================================
# BLOCK D-1: 投資判断カード設定
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