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

import os
from pathlib import Path

# ==============================================================
# BLOCK 0: ディレクトリパス定数
# ==============================================================

# ベースパス（SSOT）。
#   GitHub Actions / ローカル実行では環境変数 STOCK_ANALYZER_BASE で上書きする
#   （run_headless.py が ${{ github.workspace }}/stock_data を渡す）。
#   未設定の場合（Colab）は従来どおり Google Drive 配下にフォールバックする。
#   ★ ここで DRIVE_BASE を確定させることで CACHE_DIR / SUBTHEME_JSON_PATH 等の
#     DRIVE_BASE 由来パスがすべて一括で正しい場所を指す。
#     （旧構成では run_headless 側の個別上書きが CACHE_DIR を取りこぼし、
#       score_prior 等が Colab パス /content に書こうとして
#       「Permission denied: '/content'」で全業種スコアが無効化していた。）
_BASE_ENV    = os.getenv("STOCK_ANALYZER_BASE", "").strip()
DRIVE_BASE   = Path(_BASE_ENV) if _BASE_ENV else Path("/content/drive/MyDrive/stock_analyzer")
VIEWER_BASE  = Path("/content/drive/MyDrive/stock_viewer")

DATA_DIR     = DRIVE_BASE / "data"
RESULT_DIR   = DRIVE_BASE / "results"
INSIGHT_DIR  = DRIVE_BASE / "insights"
ARXIV_DIR    = INSIGHT_DIR / "arxiv"
SECTOR_DIR   = INSIGHT_DIR / "sectors"       # Step 2 以降で使用
MACRO_DIR    = INSIGHT_DIR / "macro"         # Step 2 以降で使用
LOG_DIR      = DRIVE_BASE / "logs"
CACHE_DIR    = DRIVE_BASE / "cache"          # Wave 0 以降で使用

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
# BLOCK 0.5: サブテーマ JSON パス（Wave 2 - R-1）
# ==============================================================

SUBTHEME_JSON_PATH_JP = DRIVE_BASE / "data" / "jp_sector_subtheme.json"
SUBTHEME_JSON_PATH_US = DRIVE_BASE / "data" / "us_sector_subtheme.json"

# ==============================================================
# BLOCK 1: API / 実行設定
# ==============================================================

SPREADSHEET_ID   = "1CePiavYj_6OR0cOz84V4RkA0c0S6IwZlcHCv8_bsOjc"
CREDENTIALS_PATH = str(VIEWER_BASE / "service_account.json")
WRITE_SHEET      = True   # True にすると Google Sheets に書き込む

RUN_SCHEDULE_HOUR   = 6    # 毎朝何時に自動実行するか（0〜23）※GitHub Actions は 06:00 JST 実行に統一
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
# 【v1.0 との差分】新規追加
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
    # ── 2026-06-30 追加（残り25業種 / TSE33業種を網羅）────────
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
    "1033": ["6098.T", "9613.T", "4324.T"],   # サービス: リクルート・NTTデータ・電通
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

# ★ 2026-06-10 セキュリティ修正: APIキーのハードコードを廃止し環境変数化。
#   GitHub Actions は Secrets→run_headless.py の ENV_MAP 経由で注入（従来から登録済み）。
#   Colab / ローカル手動実行時は os.environ に設定してから起動すること。
#   旧キーはコードに露出していたため失効・再発行を推奨。
EDINET_API_KEY: str = os.getenv("EDINET_API_KEY", "")      # 金融庁 EDINET
EDINETDB_API_KEY: str = os.getenv("EDINETDB_API_KEY", "")  # 民間 EdinetDB
ESTAT_API_KEY: str = os.getenv("ESTAT_API_KEY", "")

# 深掘り分析対象ティッカー（S3-4）
# ★ 2026-06-10 SSOT修正: deep_dive_trigger.py が実行時に cfg.__dict__ へ
#   注入する動的属性だったため、契約の明示としてデフォルト値をここに定義。
#   edinet_rotation_manager.py が priority=0（最優先）で処理する。
EDINET_DEEP_DIVE_TICKERS: list = []

# BigQuery 接続設定
# ★ 2026-06-10 SSOT修正: edinet_bq_client.py は「config優先→環境変数
#   フォールバック」設計だが、config 側に定義が無く ImportError 経由の
#   フォールバックが常態化していた。SSOT としてここに定義する
#   （値は従来どおり環境変数由来のため挙動変更なし）。
BQ_PROJECT_ID: str       = os.getenv("BQ_PROJECT_ID", "")
BQ_CREDENTIALS_PATH: str = os.getenv("BQ_CREDENTIALS_PATH", "")
BQ_DATASET: str          = os.getenv("BQ_DATASET", "edinet")

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
ENABLE_EDINET: bool = True
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
POLICY_FRED_API_KEY: str = os.getenv("POLICY_FRED_API_KEY", "")  # ★ 2026-06-10 環境変数化（GitHub Secrets 登録済み）

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
GOOGLE_CSE_API_KEY: str = os.getenv("GOOGLE_CSE_API_KEY", "")   # ★ 2026-06-10 環境変数化（GitHub Secrets 登録済み）
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
    "event_horizon",   # ★ 2026-06-13 EVT-1: イベント事前アラート（日次）
    "options_advisor", # ★ 2026-06-13 OPT-1: オプション戦略提案（日次）
    "signal_aggregator",  # 統合シグナル [4-9]
    "inference",          # 推論エンジン [4-10]
    "edinet_rotation", # edinetのローテーション
    "us_fundamentals", # ★ US財務DBローテーター（Phase US-1）← screenerより先に実行
    "us_screener",   # 米国株スクリーニング（US財務DB補完後に実行）
    "us_8k",           # ★ US 8-K リアルタイム監視（Phase US-3）
    "us_sector_bridge",  # ★ US-5 SIC/JP業種連携
    "multi_currency", # 多通貨モニタリング [A-6]
    "options_monitor", # オプション市場センチメント [4-5]
    "support_resistance",   # サポート・レジスタンス検出 [4-6]
    "volume_profile",   # 出来高プロファイル分析 [4-7]
    "notion_obsidian",   # Notion/Obsidian書き込み [6-4]（毎日実行）
    "policy_radar",     # ★ 政策レーダー追加
    "policy_signal_applier",       # Wave 3 - Policy Ph2（17.8番・日次）
    "pead_monitor",                # Wave 3 - FE-5（screener直後・日次）
    "ml_meta",                     # Wave R5-1 - S7 ML特徴量スナップショット（日次蓄積）
    "score_calculator",            # Wave R5-2 Phase A - E10 JP/US共通スコア
]

# ── ウィークリーサイクル（月曜朝に追加実行）───────────────────
WEEKLY_MODULES = [
    "arxiv",         # ArXiv 論文取得
    "semantic_scholar", # ★ 論文追加分析
    "congress_monitor",  # ★ 2026-06-10 配線復元: US法案議会進行追跡 [Ph4]（L4キャッシュ減衰で日次適用）
    "ontology_export",   # ★ 2026-06-12 ONT-1: エンティティグラフBQ追記（週次）
    "correlation",   # 米国債×業種 相関監査
    "size_tier_classifier",      # Wave0 Size Ph1（sector実行直後・週次）
    "business_model_classifier", # Wave0 BM Ph1（size_tier_classifierの直後・週次）
    "jp_us_sector_correlation",   # Wave0 Idea2-A（correlation_auditの直後）
    "sector_rotation_detector",    # Wave0 Idea2-B（signal_aggregatorの直前・必須）
    "portfolio_diversity_filter",  # Wave 3 - FE-6（screener直後・Ignitionの前）
    "ignition_detector",           # Wave 2 - FE-4（sector_rotation_detectorの直後）
    "catalyst_calendar",           # Wave 3 - FE-7（NEDO/policy_radar直後）
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
    "technology_mapper",        # Wave 6 - B: 企業×技術マッピング
    "cluster_engine",           # Wave 6 - B: 技術クラスター生成
    "emerging_detector",        # Wave 6 - B: 萌芽テーマ検知
    "cluster_graph_exporter",   # Wave 6 - B: クラスター結果出力
    "factor_crowding",  # ★ RESEARCH-2追加
]

# ウィークリー実行の曜日（0=月曜 〜 6=日曜）
WEEKLY_RUN_WEEKDAY: int = 0

# ==============================================================
# BLOCK 14b: アラート通知設定（5-B 追加）
# ★ 2026-06-10 SSOT修正: 旧表記「BLOCK 14」はニュースRSS設定と
#   番号衝突していたため 14b に改番（MODULE_INDEX の表記と一致）。
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
DEEPL_API_KEY: str = os.getenv("DEEPL_API_KEY", "")  # ★ 2026-06-10 環境変数化（GitHub Secrets 登録済み / 末尾 :fx の無料キー）

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
# BLOCK 17: 米国株スクリーニング設定
# ★ 2026-06-10 SSOT修正: 旧表記「BLOCK 19」は EdinetDB ローテーション
#   （BLOCK 19）と番号衝突していたため BLOCK 17 に改番（MODULE_INDEX と一致）。
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
# ★ 2026-06-10 セキュリティ修正: APIキーのハードコードを廃止し環境変数化。
#   GitHub Actions では Secrets に NOTION_API_KEY を登録すること。
#   旧キー（ntn_4870...）はコードに露出していたため失効・再発行を推奨。
NOTION_API_KEY:            str  = os.getenv("NOTION_API_KEY", "")  # Notion Integration Secret
NOTION_DATABASE_ID:        str  = "331b571e4f7e8092b27dded753e5c3fe"     # スクリーナー銘柄DB の ID

# Obsidian Vault パス（Google Drive上）
# ★ 2026-06-10 SSOT修正: 「空文字定義→直後に無条件上書き」の二重定義を単一化。
#   実効値は従来どおり DRIVE_BASE/obsidian_vault（挙動変更なし）。
OBSIDIAN_VAULT_DIR: str = str(DRIVE_BASE / "obsidian_vault")

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
# ★ 2026-06-10 SSOT修正: DRIFT_ALERT_THRESHOLD はここ（15.0）と BLOCK 35
#   （20.0）で二重定義され後勝ちで 20.0 が有効だった。実効値を維持するため
#   本ブロックの定義を削除し BLOCK 35 を正とする（drift_detector.py 参照）。
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

# S&P500 ティッカー一覧（us_fundamental_rotator.py のローテーション対象）
# US_SCREENER_CATEGORIES の "sp500" カテゴリ JSON から動的に読み込む
# → ここには直接列挙せず、rotator 起動時に custom_universe/sp500.json を参照する
SP500_TICKERS: list = []   # ★ 実値は run_us_fundamental_rotator() 内で補完する

# ==============================================================
# BLOCK US-2: 米国マクロ拡張パイプライン設定
# ==============================================================
# ★ 2026-06-10 バグ修正: us_macro_pipeline.py がリポジトリに存在しない
#   （未実装）にもかかわらずフラグが True で、毎日サイレントスキップ
#   されていた。→ 2026-07-10（Wave US-2）us_macro_pipeline.py を実装し有効化。
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
# BLOCK B-2b: 政府統計先行指標ルール 有効化フラグ
# ★ 2026-06-10 SSOT修正: ルール本体（BLOCK B-2）と番号衝突していたため
#   B-2b に改番。GOV_RULES 本体は BLOCK B-2 を参照。
# ==============================================================
ENABLE_GOV_RULES: bool = True

# ==============================================================
# BLOCK D-1b: 投資判断カード設定
# ★ 2026-06-10 SSOT修正: 旧表記「BLOCK D-1」は経済サイクルフェーズ判定
#   （BLOCK D-1）と番号衝突していたため D-1b に改番。
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
# XBRL タグマッピング（E2追加 / 2026-04-11）
# ============================================================
# キー: BigQuery financials テーブルのカラム名
# 値 : EDINET XBRL のローカルタグ名（名前空間プレフィックスなし）
# 日本基準（J-GAAP）のみ対応。IFRS は E5 フェーズで追加予定。
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
# BLOCK 24: Pearl Score 設定（Wave 0 - Pearl Ph1）
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

# Ph1 で有効化するディメンション（D2・D5 は将来拡張用プレースホルダー）
PEARL_ENABLED_DIMENSIONS: list = ["D1", "D3", "D4"]

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
# BLOCK 25: 規模分類 / 業態判定設定（Wave 0 - Size Ph1 / BM Ph1）
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
# BLOCK 26: 議員取引監視設定（Wave 0 - Policy Ph1）
# ==============================================================
ENABLE_POLITICIAN_MONITOR:   bool  = True
ALERT_POLITICIAN_MIN_SCORE:  int   = 60      # この点数以上でLINE即時通知
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
    """火・金かどうかを判定（TZ=Asia/Tokyo 前提で datetime.now() を使う）"""
    from datetime import datetime
    dt = now or datetime.now()
    return dt.weekday() in POLITICIAN_RUN_WEEKDAYS

# ==============================================================
# BLOCK 23: US GICSセクター設定（Wave 1 - U-1 / U-2）
# ★ 2026-06-10 SSOT修正: 旧表記「BLOCK 27」は Wave 6 縮退モード設定
#   （BLOCK 27）と番号衝突していたため BLOCK 23 に改番。
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
# BLOCK 28: 先行指標ルール設定（Wave 1 - A-1 / A-2）
# ==============================================================

# ── A-1: 貿易品目 → 業種コードマッピング ─────────────────────────
# gov_data_pipeline.fetch_trade_item_signals() が参照する。
# statsCode / searchWord はe-Stat財務省貿易統計から対応する品目表を使用する。
# weight: スコア増幅係数（1.0 = 基準。1.5 = 1.5倍の影響を与える）
# ★ 2026-06-10 SSOT修正: 旧名 LEADING_INDICATOR_RULES は BLOCK R-2 と
#   同名衝突し後勝ち上書きが発生していたため GOV_TRADE_JOB_RULES に改名。
GOV_TRADE_JOB_RULES: dict = {
    "trade_items": {
        "semiconductor_exports": {
            "name_ja":    "半導体・電子部品輸出",
            "statsCode":  "00350300",
            "searchWord": "半導体等電子部品",
            "sectors":    ["1016", "1018"],       # 電気機器, 精密機器
            "direction":  "higher_is_good",
            "weight":     1.5,
            "stat_id":    "trade_semiconductor",
            "description": "半導体輸出増は電気機器・精密機器メーカーの受注先行指標",
        },
        "auto_exports": {
            "name_ja":    "自動車輸出",
            "statsCode":  "00350300",
            "searchWord": "自動車",
            "sectors":    ["1017"],                # 輸送用機器
            "direction":  "higher_is_good",
            "weight":     1.3,
            "stat_id":    "trade_auto",
            "description": "自動車輸出増は輸送用機器メーカーの生産・収益先行指標",
        },
        "machinery_exports": {
            "name_ja":    "一般機械輸出",
            "statsCode":  "00350300",
            "searchWord": "一般機械",
            "sectors":    ["1015"],                # 機械
            "direction":  "higher_is_good",
            "weight":     1.2,
            "stat_id":    "trade_machinery",
            "description": "一般機械輸出増は機械業種の設備投資受注先行指標",
        },
        "chemical_exports": {
            "name_ja":    "化学製品輸出（有機化合物）",
            "statsCode":  "00350300",
            "searchWord": "有機化合物",
            "sectors":    ["1007"],                # 化学
            "direction":  "higher_is_good",
            "weight":     1.1,
            "stat_id":    "trade_chemical",
            "description": "有機化合物輸出増は化学業種の生産・販売先行指標",
        },
        "steel_exports": {
            "name_ja":    "鉄鋼輸出",
            "statsCode":  "00350300",
            "searchWord": "鉄鋼",
            "sectors":    ["1012"],                # 鉄鋼
            "direction":  "higher_is_good",
            "weight":     1.1,
            "stat_id":    "trade_steel",
            "description": "鉄鋼輸出増は鉄鋼業の生産・価格の先行指標",
        },
        "pharma_imports": {
            "name_ja":    "医薬品輸入",
            "statsCode":  "00350300",
            "searchWord": "医薬品",
            "sectors":    ["1008"],                # 医薬品
            "direction":  "higher_is_good",
            "weight":     1.0,
            "stat_id":    "trade_pharma",
            "description": "医薬品輸入増は国内医薬品需要の高まりを示す先行指標",
        },
        "crude_oil_imports": {
            "name_ja":    "原油輸入",
            "statsCode":  "00350300",
            "searchWord": "原油",
            "sectors":    ["1009", "1020"],        # 石油石炭, 電力ガス
            "direction":  "lower_is_good",         # 輸入増=コスト増=逆風
            "weight":     1.2,
            "stat_id":    "trade_crude_oil",
            "description": "原油輸入増はエネルギーコスト上昇→石油精製・電力ガスに逆風",
        },
    },

    # ── A-2: 職種別求人倍率 → 業種コードマッピング ──────────────────
    # ★ Ph1 プレースホルダー: 統計ID確定後に実装
    # 厚生労働省「一般職業紹介状況（職業別）」
    #   statsCode 候補: "00450022"（要e-Stat API確認）
    "job_openings": {
        "it_specialist": {
            "name_ja":    "IT・情報系専門職求人倍率",
            "statsCode":  "",                      # TODO: 統計ID調査後に設定
            "searchWord": "情報処理・通信技術者",
            "sectors":    ["1025", "1016"],        # 情報通信, 電気機器
            "direction":  "higher_is_good",
            "weight":     1.3,
            "stat_id":    "job_it",
            "placeholder": True,
            "description": "IT専門職求人増は情報通信・電気機器の事業拡大先行指標",
        },
        "construction": {
            "name_ja":    "建設・土木系技術者求人倍率",
            "statsCode":  "",                      # TODO: 統計ID調査後に設定
            "searchWord": "建設・土木・測量技術者",
            "sectors":    ["1003", "1015"],        # 建設, 機械
            "direction":  "higher_is_good",
            "weight":     1.2,
            "stat_id":    "job_construction",
            "placeholder": True,
            "description": "建設求人増は建設・機械発注増の先行指標",
        },
        "healthcare": {
            "name_ja":    "医療・介護系求人倍率",
            "statsCode":  "",                      # TODO: 統計ID調査後に設定
            "searchWord": "医療・福祉",
            "sectors":    ["1008", "1033"],        # 医薬品, サービス
            "direction":  "higher_is_good",
            "weight":     1.1,
            "stat_id":    "job_healthcare",
            "placeholder": True,
            "description": "医療介護求人増は医薬品・サービス業の需要拡大先行指標",
        },
        "manufacturing": {
            "name_ja":    "製造技能系求人倍率",
            "statsCode":  "",                      # TODO: 統計ID調査後に設定
            "searchWord": "生産工程・労務",
            "sectors":    ["1015", "1016", "1017"], # 機械, 電気機器, 輸送用機器
            "direction":  "higher_is_good",
            "weight":     1.2,
            "stat_id":    "job_manufacturing",
            "placeholder": True,
            "description": "製造求人増は機械・電気・自動車の生産拡大先行指標",
        },
        "logistics": {
            "name_ja":    "運輸・物流系求人倍率",
            "statsCode":  "",                      # TODO: 統計ID調査後に設定
            "searchWord": "輸送・機械運転",
            "sectors":    ["1021", "1022", "1023"], # 陸運, 海運, 空運
            "direction":  "higher_is_good",
            "weight":     1.1,
            "stat_id":    "job_logistics",
            "placeholder": True,
            "description": "物流求人増は陸海空運の事業拡大シグナル",
        },
    },
}

# ==============================================================
# BLOCK 29: Wave 2 Feature Engineering 定数（FE-1 / FE-2 / FE-3）
# ==============================================================

# ── FE-1: リスク調整スコア（ATR基準） ────────────────────────────
# _calc_risk_adjusted_score() が参照する。
# ATR_WINDOW: ATR計算ウィンドウ（日数）
# RISK_ADJ_PENALTY_LOW_VOL : ボラ低銘柄への乗数（≤1.0 = ボーナス扱い）
# RISK_ADJ_PENALTY_HIGH_VOL: ボラ高銘柄への乗数（>1.0 = ペナルティ扱い）
ATR_WINDOW                 = 14
RISK_ADJ_PENALTY_LOW_VOL   = 0.85   # $risk\_adj = composite \times 0.85$ (低ボラ優遇)
RISK_ADJ_PENALTY_HIGH_VOL  = 1.20   # $risk\_adj = composite \div 1.20$ (高ボラ割引)
GARCH_ENABLED              = False   # True にすると ARCH ライブラリが必要

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
# BLOCK 30: Ignition Detector 定数（Wave 2 - FE-4）
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

# カテゴリD（将来拡張用）
IGNITION_CATEGORY_D_ENABLED       = False

# キャッシュ TTL
IGNITION_ALERT_TTL_DAYS           = 14    # ignition_alerts.json
IGNITION_HISTORY_TTL_DAYS         = 180   # ignition_history.json

# ==============================================================
# BLOCK 31: Wave 3 - Policy Ph2 定数
# ==============================================================
# policy_signal_applier.py / politician_signal_scorer.py が参照する。
# POLITICIAN_CACHE_LAYERS : 4層減衰キャッシュの層数
# EMERGENCY_VIX_THRESHOLD : この値超で EMERGENCY_OVERRIDE_FLAG を立てる
# SCORE_MAX_POLICY_L4_ADJ : Level 4 Override 時の composite_score 最大加算量

POLITICIAN_CACHE_LAYERS    = 4
EMERGENCY_VIX_THRESHOLD    = 45.0   # VIX > 45 → 緊急介入フラグ
SCORE_MAX_POLICY_L4_ADJ    = 25.0   # $composite\_score + min(bonus, 25.0)$

# ==============================================================
# BLOCK 32: Wave 3 - FE-5 PEAD監視定数
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
# BLOCK 33: Wave 3 - FE-6 ポートフォリオ相関管理定数
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
# BLOCK 34: Wave 3 - FE-7 カタリストカレンダー定数
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
# BLOCK 35: Wave 4 全定数（E3 / Policy Ph3-Ph4 / S3-S4 / FE-F）
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

# ── Policy Ph3 追加設定（Wave 6 - U-4）────────────────────────
# 既存の INSTITUTION_TIER_MAP / SCORE_MAX_INSTITUTION_ADJ を流用し、
# 以下の追加定数のみ Wave 6 で新規追加する。

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

SCORE_MAX_POLICY_L4_ADJ_W4          = 25.0   # Wave 4 L4専用（BLOCK31と区別）
EMERGENCY_DAILY_DROP_THRESHOLD       = -0.04  # 日次-4%超で緊急フラグ
POLICY_STRUCTURAL_CACHE_TTL_MONTHS   = 12
PDF_URL_CACHE_TTL_DAYS               = 30
ENABLE_PDF_EXTRACTION                = True   # pdfplumber 有効フラグ

# ★ 2026-06-10 配線復元に伴い追加: congress_monitor 実行フラグ・APIキー
#   CONGRESS_API_KEY 未設定時はキャッシュのみで動作（API取得スキップ）。
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

# ── モジュール鮮度特性定義（S4/Phase 0用）──────────────────────
# 各指標の遅行特性と先行性を定義し、鮮度係数計算の半減期パラメータを設定。
# $freshness\_weight = exp(-\ln(2) \times days / half\_life)$
# ★ 2026-06-10 SSOT修正: 旧名 LEADING_INDICATOR_RULES は BLOCK R-2 の
#   先行指標ライブラリを後勝ち上書きで無効化していたため
#   MODULE_FRESHNESS_RULES に改名。
# ★ 注意: 現時点で本定数を参照するモジュールは存在しない
#   （score_prior_manager.py は SCORE_PRIOR_FRESHNESS_HALF_LIFE を使用）。
#   Phase 0 の鮮度係数をモジュール別に細分化する際の定義として保持する。

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

# ── ALL_DIRS への Wave 4 フォルダ追加 ────────────────────────────
# 既存 ALL_DIRS リストに Wave 4 で使用するディレクトリを追加
# （setup_dirs() が一括作成するため、ここで定義する）
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
# BLOCK Wave5: テキストシグナル定数（Wave 5 - E4 / Idea3-P3）
# ==============================================================

# ── Wave 5 - E4: テキストシグナルキャッシュ定数 ─────────────────
# TEXT_SIGNAL_CACHE_FILE : text_signal.py が生成するキャッシュ JSON ファイル名
# TEXT_SIGNAL_TTL_DAYS   : キャッシュ有効期間（日）
# TEXT_SIGNAL_FIRST_RUN_LIMIT : 初回実行時のテキスト抽出対象銘柄数の上限
# ENABLE_TEXT_SIGNAL     : False にすると text_signal を完全スキップ

TEXT_SIGNAL_CACHE_FILE       = "text_signal_cache.json"
TEXT_SIGNAL_TTL_DAYS         = 30
TEXT_SIGNAL_FIRST_RUN_LIMIT  = 50
ENABLE_TEXT_SIGNAL           = True

# ── Wave 5 - E4: リスクカテゴリ定義 ─────────────────────────────
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

# ── Wave 5 - E4: テキストシグナルスコア範囲 ─────────────────────
# YoY 差分スコアおよびリスクカテゴリスコアの上下限値（クランプ用）
# $text\_risk\_bonus \in [TEXT\_SIGNAL\_SCORE\_MIN, 0]$

TEXT_SIGNAL_SCORE_MIN  = -10.0   # ネガティブ方向上限（リスク検出）
TEXT_SIGNAL_SCORE_MAX  =   0.0   # テキストリスクはネガティブ側のみ

# ── Wave 5 - Idea3-P3: policy_radar 実現度判定定数 ────────────────
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
# BLOCK Wave6: 地政学・海外権益・リスク設定（Wave 6 - G-N / G-C / G-X）
# ==============================================================

# ── Wave 6 - G-N: 国別リスク監視設定 ──────────────────────────

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

# ── Wave 6 - G-C: 企業×海外権益マッピング設定 ──────────────────

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

# ── Wave 6 - G-C 追加: スクレイパー実行制御 ─────────────────────

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

# ── Wave 6 - G-X: 地政学×権益統合シグナル設定 ──────────────────

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

# geo_signal の有効フラグ
# 初回運用時は False で起動し、BQ に country_risk_daily / company_country_exposure
# のデータが蓄積されてから True に切り替える。
# True にすると base_agent.py と us_stock_screener.py が geo_impact_bonus を計算する。
ENABLE_GEO_SIGNAL = False

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

# geo_signal の analytics.company_scores への書き込み有効フラグ
# True にすると run_all_tasks() 後に geo_impact_bonus を BQ に書き込む
ENABLE_GEO_SCORE_PERSIST = False   # 初期は False（BQ 書き込みコスト削減）

# ── Wave 6 - G-N: ALL_DIRS への Wave 6 フォルダ追加 ──────────────
_WAVE6_DIRS = [
    CACHE_DIR / "geopolitics",        # country_risk_updater キャッシュ
    INSIGHT_DIR / "geopolitics",      # 地政学レポート保存先
    INSIGHT_DIR / "geo_exposure",     # 権益マッピング結果
]
for _d in _WAVE6_DIRS:
    if _d not in ALL_DIRS:
        ALL_DIRS.append(_d)

# ==============================================================
# BLOCK 36: Wave 6 - U-4: 機関投資家 13F 監視 追加設定
# ==============================================================
# 【既存定数との関係】
#   以下の定数は BLOCK 35 で定義済みのため、ここでは定義しない:
#     INSTITUTION_TIER_MAP         (機関名→Tier辞書)
#     SCORE_MAX_INSTITUTION_ADJ    (最大加算量 15pt)
#     INSTITUTION_HISTORY_TTL_DAYS (キャッシュTTL 180日)
#     INSTITUTION_MIN_NEW_POSITION_USD (大手アラート閾値)
# ★ 2026-06-10 SSOT修正: 本ブロックにあった以下8定数の再定義を削除
#   （BLOCK 35 と同値の完全重複だったため。BLOCK 35 が正）:
#     ENABLE_INSTITUTIONAL_13F / EDGAR_SUBMISSIONS_URL / EDGAR_ARCHIVES_URL
#     INSTITUTIONAL_BONUS_MAX / INSTITUTIONAL_BONUS_MIN
#     INSTITUTIONAL_CACHE_PATH / INSTITUTIONAL_QUARTER_MONTHS
#     INSTITUTIONAL_QUARTER_DAY_FROM

# ==============================================================
# BLOCK 37: Wave 6 - I-1/I-3/I-4: オルタナティブデータ監視設定
# ★ 2026-06-10 SSOT修正: 旧表記「BLOCK 26」は議員取引監視設定
#   （BLOCK 26）と番号衝突していたため BLOCK 37 に改番。
# ==============================================================
# 【I-1: 求人票モニター（job_posting_monitor.py）】
#   求人サイトの技術キーワード変化から「人材投資シグナル」を生成する。
#   $hiring\_score = base\_score + surge\_bonus$ (急増時)
#
# 【I-3: 役員転職トラッカー（executive_transition_tracker.py）】
#   EDINET 有報の役員略歴YoY差分から「経営方針転換シグナル」を生成する。
#   $exec\_score = new\_hire\_count \times EXECUTIVE\_NEW\_HIRE\_BONUS$
#
# 【I-4: 施設着工モニター（facility_expansion_monitor.py）】
#   自治体・経産省の建設許可情報から「設備投資シグナル」を生成する。
#   $facility\_score = project\_count \times FACILITY\_EXPANSION\_BONUS$

# ── I-1: 求人票モニター設定 ─────────────────────────────────
ENABLE_JOB_POSTING_MONITOR: bool = True
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
ENABLE_EXECUTIVE_TRANSITION: bool = True
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
ENABLE_FACILITY_EXPANSION: bool   = True
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

# ── Wave 6 - I系: ALL_DIRS への追加 ─────────────────────────
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
# BLOCK 27: Wave 6 - 第4セッション
#           緊急縮退モード（Emergency Contraction）設定
#           C / Policy Ph5 / E5 — rule_optimizer.py 連携
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

# ── Wave 6 - 第4セッション: ALL_DIRS への追加 ─────────────────
_WAVE6_C_DIRS = [
    CACHE_DIR / "contraction",   # 縮退履歴・縮退フラグキャッシュ保存先
]
for _d in _WAVE6_C_DIRS:
    if _d not in ALL_DIRS:
        ALL_DIRS.append(_d)

# ==============================================================
# BLOCK Wave7: Wave 7 - 第1セッション 全定数
# （データ収集・高度アルファ計算統合層）
# ==============================================================

# ── Wave7-A: 標準化動向トラッカー（industry_standard_tracker.py）─

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

# ── Wave7-B: 学術カンファレンス追跡（academic_conf_tracker.py）──

# academic_conf_cache.json の TTL（日）
ACADEMIC_CONF_CACHE_TTL_DAYS: int = 180

# 追跡対象カンファレンス設定
# None の場合はモジュール側のデフォルト定数が使われる
ACADEMIC_CONF_CONFIG: dict | None = None  # 本番時は辞書を設定

# ── Wave7-C: 中期経営計画トラッカー（midterm_plan_tracker.py）──

# midterm_plan_cache.json の TTL（日）
MIDTERM_TRACKER_TTL_DAYS: int = 90

# ★ 2026-06-10 SSOT修正: MIDTERM_ACHIEVEMENT_HIGH / MIDTERM_ACHIEVEMENT_RISK
#   は BLOCK I-5 で定義済み（同値 0.90 / 0.70）のため、ここでの再定義を削除。

# 中計目標定義（ticker → plan_def 辞書）
# None の場合はモジュール側の _DEFAULT_MIDTERM_PLANS が使われる
MIDTERM_PLAN_TARGETS: dict | None = None  # 本番時は実際の中計目標を設定

# ── Wave7-D: 規模別シグナル減衰管理（size_decay_manager.py）────

# size_decay_state.json / nano_evaluation_queue.json の TTL（日）
SIZE_DECAY_TTL_DAYS: int = 7

# Tier 別減衰定数 λ（$decay = e^{-\\lambda t}$）
SIZE_DECAY_LAMBDAS: dict = {
    "Large": 0.15,
    "Mid":   0.10,
    "Small": 0.07,
    "Micro": 0.05,
    "Nano":  0.03,
}

# ★ 2026-06-10 SSOT修正: NANO_EVALUATION_MAX_PER_DAY は BLOCK 25
#   （Size Ph1）で定義済み（同値 5）のため、ここでの再定義を削除。

# ── Wave7-E: 規模帯横断シグナル（cross_size_signal.py）──────────

# cross_size_signal_cache.json の TTL（日）
CROSS_SIZE_SIGNAL_TTL_DAYS: int = 3

# ── Wave7-F: 市場独立性スコア（independence_score_calculator.py）

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

# ── Wave7-G: 潜伏銘柄管理（latent_pearl_manager.py）────────────

# latent_pearl_cache.json の TTL（日）
LATENT_PEARL_TTL_DAYS: int = 30

# 潜伏Pearl → 通常Pearl への昇格閾値
LATENT_PEARL_PROMOTION_THRESHOLD: float = 70.0

# 潜伏Pearlスコア計算の重み（合計 1.0）
LATENT_PEARL_SCORE_WEIGHTS: dict = {
    "independence": 0.35,
    "cross_signal": 0.30,
    "catalyst":     0.20,
    "midterm":      0.15,
}

# ── Wave7-H: Pearl Score D3統合定数（pearl_score_calculator.py）

# D3 への EDINET セグメント文脈ボーナス上限
D3_EDINET_SEGMENT_BONUS_MAX: float = 15.0

# D3 への中計スコア連携ボーナス上限
D3_MIDTERM_BONUS_MAX: float = 10.0

# D3 へのカタリストフラグ連携ボーナス上限
D3_CATALYST_BONUS_MAX: float = 8.0

# 潜伏Pearl昇格銘柄への pearl_score 上乗せ上限
LATENT_PEARL_PROMOTION_BONUS_MAX: float = 8.0

# ── Wave7-I: ALL_DIRS への Wave 7 キャッシュディレクトリ追加 ───

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

# ── Wave7-J: WEEKLY_MODULES / DAILY_MODULES への登録定義 ────────
# 実際の登録は stock_analyzer_main.py の Wave 7 第3セッションで行う。
# ここでは実行順序の仕様を定義する。

# DAILY_MODULES 実行順（size_decay → cross_size → independence → latent_pearl）
WAVE7_DAILY_MODULE_ORDER: list = [
    "size_decay_manager",
    "cross_size_signal",
    "independence_score_calculator",
    "latent_pearl_manager",
]

# WEEKLY_MODULES 実行順（標準化 → 学術 → 中計）
WAVE7_WEEKLY_MODULE_ORDER: list = [
    "industry_standard_tracker",
    "academic_conf_tracker",
    "midterm_plan_tracker",
]

# ── Wave7-K: Feature Flag（全Wave 7モジュールのON/OFF制御）─────

# 標準化トラッカー有効フラグ（デフォルトOFF: 本番確認後ONに変更）
ENABLE_STANDARDIZATION_TRACKER: bool = False

# 学術カンファレンス追跡有効フラグ
ENABLE_ACADEMIC_CONF_TRACKER: bool = False

# 中計トラッカー有効フラグ
ENABLE_MIDTERM_PLAN_TRACKER: bool = False

# 規模別減衰管理有効フラグ
ENABLE_SIZE_DECAY_MANAGER: bool = False

# 規模帯横断シグナル有効フラグ
ENABLE_CROSS_SIZE_SIGNAL: bool = False

# 市場独立性スコア有効フラグ
ENABLE_INDEPENDENCE_SCORE: bool = False

# 潜伏銘柄管理有効フラグ
ENABLE_LATENT_PEARL_MANAGER: bool = False

# D3 EDINET セグメント統合有効フラグ
ENABLE_D3_EDINET_SEGMENT: bool = False

# D3 中計連携有効フラグ
ENABLE_D3_MIDTERM_INTEGRATION: bool = False

# D3 カタリスト連携有効フラグ
ENABLE_D3_CATALYST_INTEGRATION: bool = False

# 潜伏Pearl昇格ボーナス有効フラグ
ENABLE_LATENT_PEARL_BONUS: bool = False
# ==============================================================
# BLOCK 38: 観測性 — モジュール実行台帳設定（Wave OBS-1）
# ==============================================================
# module_ledger.py が参照する。日次の全モジュール成否を台帳化し、
# 連続失敗・配線漏れ（記録なし）を可視化する。
# 背景: 2026-06 の全数バグ掃討で発見された「サイレント失敗」の再発防止。

ENABLE_MODULE_LEDGER: bool = True
LEDGER_CONSEC_FAIL_DAYS: int = 3    # この日数以上の連続失敗で Gmail アラート
LEDGER_HISTORY_DAYS: int = 14       # 連続失敗・最終成功日の遡及日数

# ==============================================================
# BLOCK 39: 投資部（マルチエージェント審議）連携設定（Wave DEPT-1）
# ==============================================================
# agent_packet_exporter.py が参照する。SPEC_investment_dept.md 参照。
# vault パスはローカル実行専用（GitHub Actions では使用しない）。

ENABLE_INVESTMENT_DEPT: bool = True
INVESTMENT_DEPT_VAULT_DIR: str = os.getenv(
    "INVESTMENT_DEPT_VAULT_DIR",
    "/Users/kentaryobe/Library/Mobile Documents/iCloud~md~obsidian/"
    "Documents/Vault2_ClaudeCode/CEO室/departments/B-investment",
)

# ── BLOCK 38b: パイプライン実行フラグの正式定義（Wave PIPE-1）─────
# ★ 2026-06-12 SSOT修正: 以下5フラグは stock_analyzer_main / 各モジュールが
#   getattr デフォルト True で参照していたが config に定義が無かった
#   （pipeline_registry_gen.py --audit で検出）。現状動作を維持して明文化。
ENABLE_DEEP_DIVE_TRIGGER: bool = True      # 深掘りトリガー [S3-4]
ENABLE_TECHNOLOGY_MAPPER: bool = True      # 技術マッピング [Wave6-B T-1]
ENABLE_CLUSTER_ENGINE: bool = True         # クラスターエンジン [Wave6-B T-2]
ENABLE_EMERGING_DETECTOR: bool = True      # 萌芽テーマ検知 [Wave6-B T-3]
ENABLE_CLUSTER_GRAPH_EXPORT: bool = True   # クラスター結果出力 [Wave6-B T-4]

# ── BLOCK 39b: オントロジー層 設定（Wave ONT-1 本番配線・2026-06-12）──
# ontology_exporter.py が参照。週次（WEEKLY_MODULES "ontology_export"）で
# エンティティグラフを BQ `ontology` データセットへ追記する。
# BQ認証なし環境では JSONL ローカル出力に自動フォールバック。
# 初回のみ ontology_bq_schema.py を Colab 等で手動実行しておくこと。
ENABLE_ONTOLOGY_EXPORT: bool = True

# ==============================================================
# BLOCK 40: イベントホライズン（事前アラート）設定（Wave EVT-1）
# ==============================================================
# event_horizon_scanner.py / catalyst_calendar.py が参照。
# ★ 従来 catalyst_calendar.py 内のデフォルト値を getattr で参照していた
#   6つの日程表を SSOT としてここに正式定義（値は同一・挙動変更なし）。
#   日程改定時はここだけを編集する。

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
# BLOCK 41: オプション戦略アドバイザー設定（Wave OPT-1）
# ==============================================================
# options_strategy_advisor.py が参照。
# ★ 投資部ガードレール準拠: 提案は defined-risk（買い）または
#   カバード戦略（covered call / cash-secured put）のみ。
#   裸売り・ショートは提案禁止（B-investment 鉄則8と同期）。

ENABLE_OPTIONS_ADVISOR: bool = True
OPTIONS_INDEX_ETFS: list = ["SPY", "QQQ"]   # 常時監視する指数ETF
OPTIONS_MAX_UNIVERSE: int = 10              # yfinanceチェーン取得の上限銘柄数
OPTIONS_IV_RICH_RATIO: float = 1.3          # IV/HV がこれ以上 → プレミアム売り環境
OPTIONS_IV_CHEAP_RATIO: float = 0.8         # IV/HV がこれ以下 → プレミアム買い環境
OPTIONS_HV_WINDOW_DAYS: int = 20            # ヒストリカル・ボラティリティ算出窓
OPTIONS_MIN_SCORE_FOR_CSP: float = 70.0     # cash-secured put 提案の最低composite

# ==============================================================
# BLOCK 44: 出口戦略エンジン設定（Wave R0-6 / E1）
# ==============================================================
# exit_rule_engine.py が参照。投資哲学.md の売却ルールのコード化。
# 設計正本: 統合ファイルなど/05_提案_2026-07-05/要件定義書_統合改修_20260705.md §R0-6
# ★ 発注は常に健太が手動（D-01）。本エンジンは注文票生成まで。

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
# BLOCK 53: データ信頼性・鮮度SLA設定（Wave R0-1 / E7'）
# ==============================================================
# data_reliability.py が参照。D-05: J-Quants不使用・全て無料
# （yfinanceリトライ → Stooqフォールバック → キャッシュ）。

ENABLE_DATA_SLA: bool = True
# Wave R0-2: usデータセットへのBQ格納（job/executive/facility の3モジュール）
ENABLE_US_BQ_EXPORT: bool = True
# D-09（2026-07-05決定）: Notion連携は廃止（Obsidian一本化）。
# notion_obsidian_writer.py の Notion API 呼び出しのみ停止（Obsidian出力は継続）。
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
# BLOCK 42: 統合バックテスト基盤設定（Wave R1-1 / S1）
# ==============================================================
# backtest/ パッケージが参照。設計正本: 要件定義書_統合改修_20260705.md §R1-1。
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
# BLOCK 43: ペーパートレード仮想口座設定（Wave R1-2 / S2）
# ==============================================================
# paper/paper_broker.py が参照。全イベントは events.jsonl に追記し
# 口座はイベントから再構築可能（イベントソーシング）。

ENABLE_PAPER_TRADING: bool = True
PAPER_INITIAL_CAPITAL: int = 2_000_000    # 戦略別の初期仮想資金（円）
PAPER_STRATEGIES: list = ["screener_topN", "ignition", "pead"]
# S6(commodity)はR4-2実装後に、S4(options)はIBKRペーパー利用のため対象外
PAPER_LEDGER_PATH: str = "stock_data/paper"   # accounts.json / events.jsonl の置き場

# ==============================================================
# BLOCK 48: ポジションサイジングv2設定（Wave R1-5 / E5）
# ==============================================================
# position_sizer.py の calc_position_size_v2 が参照。
# D-15（2026-07-05確定）: σターゲット=15%。

VOL_TARGET_ANNUAL: float = 0.15      # ポートフォリオ年率ボラ上限（D-15）
CONVICTION_MULT: dict = {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.25, 5: 1.5}
CORR_PENALTY_TH: float = 0.6         # 平均60日相関がこれを超えたらペナルティ
CORR_PENALTY_MULT: float = 0.7       # 相関ペナルティ係数
MAX_SINGLE_POS_PCT: float = 10.0     # 1銘柄上限（総資産比%）
CASINO_SINGLE_POS_PCT: float = 25.0  # カジノ枠戦略の1銘柄上限（カジノ枠比%）
CASINO_BUDGET_PCT: float = 15.0      # カジノ枠 = min(総資産×15%, 200万)（D-13）
CASINO_BUDGET_CAP_YEN: int = 2_000_000

# ==============================================================
# BLOCK 55: taxonomy信号設定（Wave R2-5,R2-6 / T-4,T-5）
# ==============================================================
# taxonomy_hotness.py（node_hotness算出）と signal_aggregator.py
# （カテゴリV）が参照。設計正本: HANDOFF_2026-06-30 §4-3。
# ※ BLOCK 54 は S12信用ユニット用に予約済みのため 55 を使用。

ENABLE_TAXONOMY_SIGNAL: bool = True
TAXONOMY_HOTNESS_BASELINE_DAYS: int = 20   # 加熱判定のベースライン日数
TAXONOMY_LEADING_ONLY_TH: float = 65.0     # 「先行だけ点灯」の先行閾値
TAXONOMY_MARKET_DEFAULT: str = "JP"        # T-3 get_company_taxonomy_scores の既定market

# ==============================================================
# BLOCK 49: ドローダウン制御設定（Wave R3-3 / E9）
# ==============================================================
# dd_controller.py が参照。D-16=c（より許容）で2026-07-05確定。
# 設計正本: 要件定義書_統合改修_20260705.md §R3-3。売却強制なし（推奨のみ・D-01）。

ENABLE_DD_CONTROL: bool = True
DD_CAUTION: float = 0.15        # NORMAL→CAUTION 発火（dd>=15%）
DD_DEFENSE: float = 0.25        # CAUTION→DEFENSE 発火（dd>=25%）
DD_CAUTION_EXIT: float = 0.10   # CAUTION→NORMAL 復帰（dd<10%・ヒステリシス）
DD_DEFENSE_EXIT: float = 0.20   # DEFENSE→CAUTION 復帰（dd<20%・ヒステリシス）

# ==============================================================
# BLOCK 52: 税・NISA最適化設定（Wave R3-5 / S10）
# ==============================================================
# tax_optimizer.py が参照。税務の最終判断は健太（レポートに明記）。
# 設計正本: 要件定義書_統合改修_20260705.md §R3-5。

ENABLE_TAX_OPTIMIZER: bool = True
TAX_RATE_NISA: float = 0.20315        # 特定口座の譲渡益課税率（NISAは非課税=0）
OLD_NISA_EXPIRY_YEAR: int = 2040      # 旧NISA非課税期限（この年まで）
NISA_GROWTH_ANNUAL_LIMIT: int = 2_400_000   # 新NISA 成長投資枠 年間上限
NISA_GROWTH_TOTAL_LIMIT: int = 12_000_000   # 新NISA 成長投資枠 生涯上限
NISA_LONG_HOLD_MONTHS: int = 12       # NISA推奨の想定保有下限（>1年）

# ==============================================================
# BLOCK 45: イベントドリブン短期戦略設定（Wave R4-1 / S5）
# ==============================================================
# event_strategy_runner.py が参照。プレイブックはYAML（playbooks/）で外部化。
# 設計正本: 要件定義書_統合改修_20260705.md §R4-1。発注は健太手動（D-01）。

ENABLE_EVENT_STRATEGY: bool = True
PLAYBOOK_DIR: str = "playbooks"
PROMOTION_MIN_TRADES: int = 30      # 実弾昇格M2: 取引数下限
PROMOTION_MIN_PF: float = 1.3       # 実弾昇格M2: ProfitFactor下限
PROMOTION_MAX_P: float = 0.10       # 実弾昇格M2: bootstrap p上限

# ==============================================================
# BLOCK 46: コモディティ/マクロ・ローテーション設定（Wave R4-2 / S6）
# ==============================================================
# commodity_rotation.py が参照（月初実行）。東証ETF・無料データで完結。
# 設計正本: 要件定義書_統合改修_20260705.md §R4-2。配分≤総資産10%。

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
# BLOCK 50: マルチ戦略資金配分メタ設定（Wave R4-4 / S11）
# ==============================================================
# allocation_meta.py が参照（月次）。分数ケリー×リスクパリティ。
# 設計正本: 要件定義書_統合改修_20260705.md §R4-4。健太承認後にconfig反映。

ENABLE_ALLOCATION_META: bool = True
KELLY_FRACTION: float = 0.25       # 分数ケリー（フルケリーの1/4）
ALLOC_MIN_HISTORY_M: int = 3       # 配分計算に必要な最小月数
CULL_PF_TH: float = 1.0            # 淘汰: 直近6ヶ月PFがこれ未満
CULL_MONTHS: int = 6               # 淘汰判定の観測月数
CULL_P_TH: float = 0.5             # 淘汰: bootstrap p がこれ超

# ==============================================================
# BLOCK 51: MLメタモデル設定（Wave R5-1 / S7）
# ==============================================================
# ml/ が参照。LightGBM回帰・Purged Walk-Forward CV。
# 設計正本: 要件定義書_統合改修_20260705.md §R5-1。フラグ変更は健太承認。

# ※ 既存 BLOCK RESEARCH-4（旧XGBoost ML_CORRECTION_*）とは別系統のため ML_META_* で命名。
ML_META_TARGET_HORIZON_D: int = 20   # 目的変数の先読み営業日数
ML_META_EMBARGO_D: int = 20          # Purged CV の embargo（リーク防止・最重要）
ML_META_PROMOTE_IC: float = 0.02     # 昇格: OOS rank IC 下限
ML_META_PROMOTE_T: float = 2.0       # 昇格: t統計量 下限
ML_META_ENABLED: bool = False        # 補正テーブルの日次適用（昇格＋健太承認後にTrue）
ML_META_MAX_PT: float = 10.0         # composite への補正上限（±pt）
ML_META_N_DECILES: int = 10          # 予測を十分位に量子化（→補正テーブルのキー）
ML_META_CV_FOLDS: int = 5            # Purged Walk-Forward CV のフォールド数
# 特徴量スナップショット（日次追記）／学習後の補正テーブル（Colabからエクスポート）の置き場。
ML_META_DATASET_PATH: str = str(DATA_DIR / "ml_dataset" / "features.jsonl")
ML_META_CORRECTION_PATH: str = str(DATA_DIR / "ml_dataset" / "correction_table.json")
ML_META_DATASET_MAX_DAYS: int = 500  # features.jsonl の保持日数（古い日付から間引き）
ENABLE_ML_META_DATASET: bool = True  # 特徴量スナップショット蓄積（学習用・補正はしない）

# ==============================================================
# BLOCK 56: JP/US共通スコア設定（Wave R5-2 Phase A / E10）
# ==============================================================
# score_calculator.py が参照。SPEC_core §8-2 の統合方針の第一段階。
# Phase A対象 = 保有銘柄 ＋ us_screener 上位N のみ（要件定義書 §R5-2）。
ENABLE_SCORE_CALCULATOR: bool = True
UNIFIED_SCORE_US_TOP_N: int = 50     # us_screener からの対象上位件数
