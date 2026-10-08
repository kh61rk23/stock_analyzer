# ============================================================
# colab_analyzer_start_v3.py
# 自律型投資AIエージェント基盤 — Colab 起動セル v3.0
# ============================================================
# このファイルの内容を Colab の「1つのセル」に貼り付けて実行
#
# 【機能】
#   [ADD-1] config.py が Drive にない場合は自動生成して配置
#   [ADD-2] Streamlit + pyngrok 起動を統合
#   [ADD-3] 整合性チェック（config.py の必須属性を確認）
#   [ADD-4] フォルダ生成を cfg.ALL_DIRS から行う（config と同期）
#   [ADD-5] ngrok 固定ドメイン対応（NGROK_TOKEN を設定）
#
# 実行すると自動で以下を完了する:
#   Step 1: パッケージインストール
#   Step 2: Google Drive マウント + フォルダ作成
#   Step 3: Drive からファイルコピー（なければ config.py を自動生成）
#   Step 4: モジュール読み込み + 整合性チェック
#   Step 5: 即時実行（ArXiv + 政府データ）
#   Step 6: スケジューラー起動（毎日 08:00 バックグラウンド）
#   Step 7: Streamlit + ngrok 起動
# ============================================================

import subprocess, sys, os, time, shutil, threading, json
from pathlib import Path

# ============================================================
# ─── ここだけ変更すれば動く（設定セクション）───────────────
# ============================================================

ANALYZER_DIR    = "/content/drive/MyDrive/stock_analyzer"
SCHEDULE_HOUR   = 9      # 自動実行する時刻（時）※GitHub Actions と同じ 09:00 に統一
SCHEDULE_MINUTE = 0      # 自動実行する時刻（分）
RUN_NOW         = True   # True: 起動直後にすぐ 1 回実行する
LAUNCH_UI       = True   # True: Streamlit UI を起動する
NGROK_TOKEN     = ""     # ngrok 固定ドメインのトークン（空文字 = 無料モード）
                         # 例: "2xxxxxxxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
STREAMLIT_PORT  = 8501

# ============================================================
# STEP 1: パッケージインストール
# ============================================================

print("=" * 62)
print("  📦 [1/7] パッケージインストール")
print("=" * 62)

packages = [
    "pandas", "numpy", "yfinance", "requests", "openpyxl",
    "matplotlib", "scipy", "gspread", "google-auth",
    "schedule", "feedparser", "streamlit", "plotly",
    "pyngrok", "urllib3",
]
result = subprocess.run(
    [sys.executable, "-m", "pip", "install", "-q"] + packages,
    capture_output=True, text=True,
)
if result.returncode != 0:
    print(f"  ⚠️  インストール警告: {result.stderr[:200]}")
print("✅ パッケージインストール完了")

# ============================================================
# STEP 2: Google Drive マウント + フォルダ作成
# ============================================================

print("\n" + "=" * 62)
print("  📂 [2/7] Google Drive マウント")
print("=" * 62)

from google.colab import drive
drive.mount("/content/drive")

# v3.0 では config.ALL_DIRS と同期するフォルダを作成
# config.py を読む前なので直接列挙する
_folders = [
    "", "data", "results", "logs",
    "insights", "insights/arxiv",
    "insights/sectors", "insights/macro",
]
for folder in _folders:
    Path(f"{ANALYZER_DIR}/{folder}").mkdir(parents=True, exist_ok=True)

print(f"✅ Drive マウント完了 / フォルダ作成完了: {ANALYZER_DIR}")

# ============================================================
# STEP 3: ファイルコピー（config.py は Drive になければ自動生成）
# ============================================================

print("\n" + "=" * 62)
print("  📋 [3/7] ファイルコピー / 自動生成")
print("=" * 62)

# ── config.py が Drive にない場合の自動生成テンプレート ──────
# このテンプレートは最小構成です。
#   詳細な設定は Drive の config.py を直接編集してください。
_CONFIG_PY_TEMPLATE = '''# ==============================================================
# config.py  — 自動生成版（colab_analyzer_start_v3.py により生成）
# 詳細な設定については 改修方針と説明書.md を参照してください。
# ==============================================================
from pathlib import Path

DRIVE_BASE   = Path("/content/drive/MyDrive/stock_analyzer")
VIEWER_BASE  = Path("/content/drive/MyDrive/stock_viewer")
DATA_DIR     = DRIVE_BASE / "data"
RESULT_DIR   = DRIVE_BASE / "results"
INSIGHT_DIR  = DRIVE_BASE / "insights"
ARXIV_DIR    = INSIGHT_DIR / "arxiv"
SECTOR_DIR   = INSIGHT_DIR / "sectors"
MACRO_DIR    = INSIGHT_DIR / "macro"
LOG_DIR      = DRIVE_BASE / "logs"
ALL_DIRS     = [DATA_DIR, RESULT_DIR, INSIGHT_DIR, ARXIV_DIR,
                SECTOR_DIR, MACRO_DIR, LOG_DIR]

SPREADSHEET_ID   = "1CePiavYj_6OR0cOz84V4RkA0c0S6IwZlcHCv8_bsOjc"
CREDENTIALS_PATH = str(VIEWER_BASE / "service_account.json")
WRITE_SHEET      = False
RUN_SCHEDULE_HOUR   = 9   # ※未使用の旧定数。実際の起動時刻は上部 SCHEDULE_HOUR を参照
RUN_SCHEDULE_MINUTE = 0
ENABLE_GOV_PIPELINE = True
ENABLE_ARXIV        = True
ENABLE_SECTOR       = False
ENABLE_MACRO        = False
LOG_MAX_ENTRIES  = 500
LOG_FILENAME     = "run_history.json"

ARXIV_FEEDS = {
    "q-fin.PM": {"primary": "https://export.arxiv.org/rss/q-fin.PM",
                 "fallback": "https://rss.arxiv.org/rss/q-fin.PM",
                 "label": "ポートフォリオ管理"},
    "q-fin.TR": {"primary": "https://export.arxiv.org/rss/q-fin.TR",
                 "fallback": "https://rss.arxiv.org/rss/q-fin.TR",
                 "label": "取引・市場微細構造"},
    "q-fin.ST": {"primary": "https://export.arxiv.org/rss/q-fin.ST",
                 "fallback": "https://rss.arxiv.org/rss/q-fin.ST",
                 "label": "統計ファイナンス"},
    "q-fin.RM": {"primary": "https://export.arxiv.org/rss/q-fin.RM",
                 "fallback": "https://rss.arxiv.org/rss/q-fin.RM",
                 "label": "リスク管理"},
    "econ.EM":  {"primary": "https://export.arxiv.org/rss/econ.EM",
                 "fallback": "https://rss.arxiv.org/rss/econ.EM",
                 "label": "計量経済学"},
}
ARXIV_MAX_PER_FEED         = 30
ARXIV_RETRY_COUNT          = 3
ARXIV_RETRY_WAIT_SEC       = 5
ARXIV_REQUEST_TIMEOUT      = 20
ARXIV_INVESTMENT_THRESHOLD = 3
ARXIV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StockAnalyzer/1.0)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
ARXIV_KEYWORDS = [
    ("japan", 3, 2), ("nikkei", 3, 2), ("topix", 3, 2),
    ("emerging market", 2, 1), ("asia", 2, 1),
    ("momentum", 2, 1), ("factor investing", 3, 2), ("anomaly", 2, 1),
    ("alpha", 2, 1), ("return prediction", 3, 2),
    ("machine learning", 2, 1), ("deep learning", 2, 1),
    ("lstm", 2, 1), ("transformer", 2, 1), ("sentiment", 2, 1),
    ("alternative data", 3, 2), ("satellite", 3, 2),
    ("earnings", 2, 1), ("dividend", 2, 1),
    ("arbitrage", 2, 1), ("mean reversion", 2, 1), ("event study", 2, 1),
    ("volatility", 2, 1), ("tail risk", 3, 2), ("crisis", 1, 1),
    ("equity", 1, 1), ("stock market", 2, 1), ("portfolio", 1, 1),
    ("commodity", 2, 1), ("inflation", 2, 1),
    ("interest rate", 2, 1), ("yield curve", 2, 1),
    ("consumer price", 2, 1), ("supply chain", 2, 1),
    ("climate", 2, 1), ("weather", 1, 1),
]

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

MACRO_RULES = {
    "interest_rate_rise": {
        "label": "金利上昇",
        "signal": {"tailwind": ["1028","1029","1030","1031"], "headwind": ["1032","1021","1022"], "watch": ["1017","1015","1016"]},
        "rationale": {"1028": "預貸金利ザヤ拡大", "1032": "住宅ローン金利上昇→需要減", "1021": "借入コスト上昇"},
    },
    "interest_rate_fall": {
        "label": "金利低下",
        "signal": {"tailwind": ["1032","1021","1022","1003"], "headwind": ["1028","1030"], "watch": []},
        "rationale": {"1032": "住宅ローン低下→需要回復", "1028": "預貸金利ザヤ縮小"},
    },
    "yen_depreciation": {
        "label": "円安進行",
        "signal": {"tailwind": ["1017","1015","1016","1007","1022"], "headwind": ["1004","1027","1021"], "watch": ["1008","1025"]},
        "rationale": {"1017": "輸出採算改善", "1004": "輸入原材料コスト上昇"},
    },
    "yen_appreciation": {
        "label": "円高進行",
        "signal": {"tailwind": ["1004","1027","1009"], "headwind": ["1017","1015","1016","1022"], "watch": []},
        "rationale": {"1017": "輸出採算悪化", "1004": "輸入コスト低下"},
    },
    "oil_price_rise": {
        "label": "原油高",
        "signal": {"tailwind": ["1009"], "headwind": ["1021","1022","1023","1004"], "watch": ["1007","1017"]},
        "rationale": {"1009": "石油元売りの収益増", "1021": "燃料費上昇"},
    },
    "us_recession_risk": {
        "label": "米景気後退リスク",
        "signal": {"tailwind": ["1020","1008"], "headwind": ["1022","1017","1025"], "watch": ["1028","1016"]},
        "rationale": {"1020": "内需ディフェンシブ", "1022": "世界貿易縮小リスク"},
    },
    "ai_semiconductor_boom": {
        "label": "AI・半導体ブーム",
        "signal": {"tailwind": ["1016","1025","1018","1015"], "headwind": [], "watch": ["1007"]},
        "rationale": {"1016": "半導体需要増", "1025": "AIクラウド投資の恩恵"},
    },
    "high_inflation": {
        "label": "インフレ高進",
        "signal": {"tailwind": ["1009","1012","1013"], "headwind": ["1027","1033"], "watch": ["1004","1028"]},
        "rationale": {"1009": "資源価格上昇", "1027": "コスト増・価格転嫁困難"},
    },
    "geopolitical_risk": {
        "label": "地政学リスク上昇",
        "signal": {"tailwind": ["1009","1012","1020"], "headwind": ["1022","1026","1017"], "watch": ["1025","1016"]},
        "rationale": {"1009": "資源価格高騰", "1022": "航路変更コスト増"},
    },
}

SECTOR_AGENT_RESULT_SCHEMA = {
    "sector_code": str, "sector_name_ja": str, "analysis_date": str,
    "macro_signals": list, "top_companies": list,
    "arxiv_papers": list, "insight_text": str, "risk_level": str,
}
IMPLEMENTED_AGENTS = []

MACRO_TICKERS = {
    "usdjpy": "USDJPY=X", "us10y": "^TNX",
    "oil_wti": "CL=F", "gold": "GC=F",
    "vix": "^VIX", "sp500": "^GSPC", "nikkei": "^N225",
}
SECTOR_REPRESENTATIVE_TICKERS = {
    "1028": ["8306.T", "8316.T", "8411.T"],
    "1032": ["8801.T", "8802.T"],
    "1017": ["7203.T", "7267.T"],
    "1025": ["9432.T", "9984.T"],
    "1016": ["6758.T", "6501.T"],
    "1022": ["9101.T", "9104.T", "9107.T"],
}
GOV_VEGPRICE_API = "https://api.cultivationdata.net/yasai_kakaku?div=all&type=csv"
'''

# ファイルコピー処理
_files_to_copy = [
    ("config.py",              True,  _CONFIG_PY_TEMPLATE),  # (ファイル名, 必須?, 自動生成テンプレ)
    ("arxiv_monitor.py",       True,  None),
    ("stock_analyzer_main.py", True,  None),
    ("gov_data_pipeline.py",   False, None),  # 任意（なければスキップ）
]

for fname, required, template in _files_to_copy:
    drive_src = Path(f"{ANALYZER_DIR}/{fname}")
    local_dst = Path(f"/content/{fname}")

    if drive_src.exists():
        shutil.copy(str(drive_src), str(local_dst))
        print(f"  ✅ {fname} — Drive からコピー")
    elif template is not None:
        # Drive にない場合は自動生成（config.py のみ）
        local_dst.write_text(template, encoding="utf-8")
        shutil.copy(str(local_dst), str(drive_src))  # Drive にも保存
        print(f"  🔧 {fname} — 自動生成（Drive にも保存）")
    elif required:
        print(f"  ❌ {fname} — Drive にありません！")
        print(f"     {ANALYZER_DIR}/{fname} にアップロードしてください。")
    else:
        print(f"  ⚠️  {fname} — Drive にありません（スキップ）")

# ============================================================
# STEP 4: モジュール読み込み + 整合性チェック
# ============================================================

print("\n" + "=" * 62)
print("  ⚙️  [4/7] モジュール読み込み + 整合性チェック")
print("=" * 62)

import importlib.util

def _load_module(name: str, path: str):
    """指定パスから Python モジュールを動的ロードする"""
    p = Path(path)
    if not p.exists():
        print(f"  ❌ {name}: {path} が見つかりません")
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, str(p))
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[name] = mod
        return mod
    except Exception as e:
        print(f"  ❌ {name} ロード失敗: {e}")
        return None

# 必須モジュールをロード
cfg_mod  = _load_module("config",              "/content/config.py")
main_mod = _load_module("stock_analyzer_main", "/content/stock_analyzer_main.py")

if cfg_mod is None:
    print("❌ config.py のロードに失敗。処理を中止します。")
    raise SystemExit(1)

if main_mod is None:
    print("❌ stock_analyzer_main.py のロードに失敗。処理を中止します。")
    raise SystemExit(1)

# ── 整合性チェック（v3.0 新規追加）─────────────────────────
_REQUIRED_CFG_ATTRS = [
    "DRIVE_BASE", "ALL_DIRS", "ARXIV_FEEDS", "ARXIV_HEADERS",
    "ARXIV_KEYWORDS", "MACRO_RULES", "JAPAN_SECTORS",
    "IMPLEMENTED_AGENTS", "MACRO_TICKERS",
]
_missing = [a for a in _REQUIRED_CFG_ATTRS if not hasattr(cfg_mod, a)]
if _missing:
    print(f"  ⚠️  config.py に不足している属性: {_missing}")
    print(f"  Drive の config.py を更新してください。")
else:
    print(f"  ✅ config.py 整合性チェック OK（{len(_REQUIRED_CFG_ATTRS)} 項目）")

main_mod.setup_dirs()
print("✅ モジュール読み込み完了")

# ============================================================
# STEP 5: 即時実行
# ============================================================

if RUN_NOW:
    print("\n" + "=" * 62)
    print("  ▶  [5/7] 起動直後の即時実行")
    print("=" * 62)
    try:
        results = main_mod.run_all_tasks()
        print(f"\n  ✅ 即時実行完了")
    except Exception as e:
        print(f"  ❌ 実行エラー: {e}")
else:
    print("\n  ⏩ [5/7] 即時実行スキップ（RUN_NOW=False）")

# ============================================================
# STEP 6: バックグラウンドスケジューラー起動
# ============================================================

print("\n" + "=" * 62)
print(f"  ⏰ [6/7] スケジューラー起動（毎日 {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}）")
print("=" * 62)

import schedule  # type: ignore

def _scheduled_job():
    import datetime as _dt
    print(f"\n⏰ 定時実行: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    try:
        main_mod.run_all_tasks()
    except Exception as e:
        print(f"  ❌ スケジュール実行エラー: {e}")

schedule.every().day.at(f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}").do(_scheduled_job)

def _schedule_loop():
    while True:
        schedule.run_pending()
        time.sleep(60)

scheduler_thread = threading.Thread(target=_schedule_loop, daemon=True)
scheduler_thread.start()
print(f"✅ スケジューラー起動（毎日 {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}）")
print(f"  ⚠️  Colab セッション中のみ有効")

# ============================================================
# STEP 7: Streamlit + ngrok 起動（v3.0 新規追加）
# ============================================================

print("\n" + "=" * 62)
print("  🌐 [7/7] Streamlit + ngrok 起動")
print("=" * 62)

if LAUNCH_UI:
    try:
        from pyngrok import ngrok, conf

        # ngrok トークン設定
        if NGROK_TOKEN:
            conf.get_default().auth_token = NGROK_TOKEN
            print(f"  ✅ ngrok 固定ドメイントークン設定済み")
        else:
            print(f"  ℹ️  NGROK_TOKEN 未設定（無料モード / 毎回 URL が変わります）")
            print(f"     固定ドメインを使う場合は NGROK_TOKEN を設定してください。")

        # Streamlit をバックグラウンドで起動
        # stock_analyzer_main.py を --ui フラグ付きで起動
        _streamlit_proc = subprocess.Popen(
            [
                sys.executable, "-m", "streamlit", "run",
                "/content/stock_analyzer_main.py",
                "--server.port",              str(STREAMLIT_PORT),
                "--server.headless",          "true",
                "--server.enableCORS",        "false",
                "--server.enableXsrfProtection", "false",
                "--browser.gatherUsageStats", "false",
                "--",  # 以降の引数は Streamlit の sys.argv に渡される
                "--ui",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Streamlit の起動を最大 20 秒待つ
        print(f"  ⏳ Streamlit 起動待ち（最大20秒）...")
        import urllib.request
        for _i in range(20):
            time.sleep(1)
            try:
                urllib.request.urlopen(
                    f"http://localhost:{STREAMLIT_PORT}/_stcore/health",
                    timeout=2,
                )
                break
            except Exception:
                pass

        # ngrok トンネル作成
        _tunnel    = ngrok.connect(STREAMLIT_PORT, bind_tls=True)
        public_url = _tunnel.public_url

        print(f"\n  {'='*58}")
        print(f"  🌐 Streamlit UI にアクセスしてください:")
        print(f"  👉  {public_url}")
        print(f"  {'='*58}")
        print(f"  ※ Colab セッションが終了すると URL も無効になります")
        if not NGROK_TOKEN:
            print(f"  ※ 固定 URL にするには NGROK_TOKEN を設定してください")

    except ImportError:
        print("  ❌ pyngrok が未インストールです。以下を別セルで実行してください:")
        print("     !pip install pyngrok -q")
    except Exception as e:
        print(f"  ⚠️  Streamlit/ngrok 起動失敗: {e}")
        print(f"  手動起動するには別セルで以下を実行してください:")
        print(f"     !streamlit run /content/stock_analyzer_main.py \\")
        print(f"       --server.port {STREAMLIT_PORT} -- --ui &")
else:
    print("  ⏩ Streamlit 起動スキップ（LAUNCH_UI=False）")
    print(f"  手動起動: 別セルで以下を実行してください")
    print(f"    !streamlit run /content/stock_analyzer_main.py \\")
    print(f"      --server.port {STREAMLIT_PORT} -- --ui &")

# ============================================================
# 完了サマリー
# ============================================================

print("\n" + "=" * 62)
print("  🟢 自律型投資AIエージェント基盤 v3.0（Step 1）稼働中")
print("=" * 62)
print(f"  📁 データ:   {ANALYZER_DIR}/data/")
print(f"  📊 結果:     {ANALYZER_DIR}/results/")
print(f"  📝 知見:     {ANALYZER_DIR}/insights/")
print(f"  📚 ArXiv:   {ANALYZER_DIR}/insights/arxiv/")
print(f"  📋 ログ:     {ANALYZER_DIR}/logs/run_history.json")
print(f"  🔍 取得ログ: {ANALYZER_DIR}/logs/arxiv_fetch_log.json")
print(f"  ⏰ 次回自動: 毎日 {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}")
print()
print("  ── 別セルで使える手動操作コマンド ───────────────────")
print("  main_mod.run_all_tasks()        # 全タスク実行")
print("  main_mod.run_arxiv_monitor()    # ArXiv のみ")
print("  main_mod.fetch_macro_snapshot() # マクロ指標取得")
print("=" * 62)
