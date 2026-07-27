# ==============================================================
# bond_commodity_pipeline.py  v1.0
# 債券イールドカーブ + 全コモディティ統合分析パイプライン
# ==============================================================
# 【機能概要】
#   - 米国・日本 国債イールドカーブ（FRED API / yfinance fallback）
#   - 全コモディティ先物・ETFプロキシ (7カテゴリ 40+ 銘柄)
#     エネルギー / 貴金属 / ベースメタル / 穀物 / ソフト / 畜産 / 特殊
#   - 逆イールド検知・リセッション確率スコア算出
#   - コモディティ変動 → JP33業種・US11セクター 影響スコア自動補正
#   - composite_score への統合用 sector_delta dict を出力
#
# 【出力ファイル】
#   insights/macro/bond_commodity_YYYY-MM-DD.md  週次MDレポート
#   data/bond_commodity_latest.json              最新データ（他モジュール連携用）
#
# 【データソース】
#   FRED API: DGS2/5/10/30 / T10Y2Y / T10Y3M / IRLTLT01JPM156N
#   yfinance: 全コモディティ先物・ETF / 米国債ETF fallback
#   APIキー:  FRED は config.POLICY_FRED_API_KEY（未設定時は yfinance fallback）
#
# 【GitHub Actions 処理時間目安】
#   債券取得: ~5秒 / コモディティ40銘柄: ~30秒 / レポート生成: ~5秒
#   合計: ~40秒/回（デイリー実行推奨）
# ==============================================================

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 1: コモディティユニバース定義（7カテゴリ 全銘柄）
# ==============================================================
# 各銘柄:
#   ticker   : yfinance ティッカー（先物=F / ETF）
#   name_ja  : 日本語名
#   unit     : 価格単位
#   type     : "future" | "etf"（ETFはプロキシ扱い）
#
# 先物ティッカー凡例:
#   CL=F Brent=BZ=F NG=F RB=F HO=F: エネルギー先物
#   GC=F SI=F PL=F PA=F           : 貴金属先物
#   HG=F ALI=F                    : ベースメタル先物（銅・アルミ）
#   ZW=F ZC=F ZS=F ZR=F ZO=F ZM=F ZL=F: 穀物先物
#   KC=F CC=F SB=F CT=F OJ=F LBR=F    : ソフト先物
#   LE=F HE=F GF=F                : 畜産先物
#   URA LIT SLX DBB               : 特殊ETFプロキシ
# ==============================================================

COMMODITY_UNIVERSE: dict = {
    # ─────────────────────────────────────────────────────────────
    # カテゴリ1: エネルギー
    # ─────────────────────────────────────────────────────────────
    "energy": {
        "name_ja": "エネルギー",
        "emoji": "🛢️",
        "items": {
            "WTI":    {"ticker": "CL=F",  "name_ja": "WTI原油",        "unit": "USD/bbl", "type": "future"},
            "BRENT":  {"ticker": "BZ=F",  "name_ja": "Brent原油",      "unit": "USD/bbl", "type": "future"},
            "NATGAS": {"ticker": "NG=F",  "name_ja": "天然ガス",        "unit": "USD/MMBtu","type": "future"},
            "GASOLINE":{"ticker":"RB=F",  "name_ja": "ガソリン(RBOB)",  "unit": "USD/gal", "type": "future"},
            "HEATOIL":{"ticker": "HO=F",  "name_ja": "暖房油",          "unit": "USD/gal", "type": "future"},
        },
    },
    # ─────────────────────────────────────────────────────────────
    # カテゴリ2: 貴金属
    # ─────────────────────────────────────────────────────────────
    "precious": {
        "name_ja": "貴金属",
        "emoji": "🥇",
        "items": {
            "GOLD":      {"ticker": "GC=F", "name_ja": "金",       "unit": "USD/oz",  "type": "future"},
            "SILVER":    {"ticker": "SI=F", "name_ja": "銀",       "unit": "USD/oz",  "type": "future"},
            "PLATINUM":  {"ticker": "PL=F", "name_ja": "プラチナ", "unit": "USD/oz",  "type": "future"},
            "PALLADIUM": {"ticker": "PA=F", "name_ja": "パラジウム","unit": "USD/oz", "type": "future"},
        },
    },
    # ─────────────────────────────────────────────────────────────
    # カテゴリ3: ベースメタル（工業金属）
    # ─────────────────────────────────────────────────────────────
    "base_metals": {
        "name_ja": "ベースメタル",
        "emoji": "⚙️",
        "items": {
            "COPPER":    {"ticker": "HG=F",  "name_ja": "銅",           "unit": "USD/lb",  "type": "future"},
            "ALUMINUM":  {"ticker": "ALI=F", "name_ja": "アルミニウム", "unit": "USD/lb",  "type": "future"},
            # ニッケル・亜鉛はETFプロキシ（LME先物はyfinanceでは未対応）
            "NICKEL":    {"ticker": "JJN",   "name_ja": "ニッケル",     "unit": "ETF/NAV", "type": "etf"},
            "ZINC":      {"ticker": "DBB",   "name_ja": "亜鉛含むベースメタル","unit": "ETF/NAV","type": "etf"},
            "STEEL_ETF": {"ticker": "SLX",   "name_ja": "鉄鋼",         "unit": "ETF/NAV", "type": "etf"},
        },
    },
    # ─────────────────────────────────────────────────────────────
    # カテゴリ4: 穀物
    # ─────────────────────────────────────────────────────────────
    "grains": {
        "name_ja": "穀物",
        "emoji": "🌾",
        "items": {
            "WHEAT":    {"ticker": "ZW=F", "name_ja": "小麦",     "unit": "USc/bu",  "type": "future"},
            "CORN":     {"ticker": "ZC=F", "name_ja": "トウモロコシ","unit": "USc/bu","type": "future"},
            "SOYBEANS": {"ticker": "ZS=F", "name_ja": "大豆",     "unit": "USc/bu",  "type": "future"},
            "RICE":     {"ticker": "ZR=F", "name_ja": "米（精米）","unit": "USD/cwt", "type": "future"},
            "OATS":     {"ticker": "ZO=F", "name_ja": "オーツ麦", "unit": "USc/bu",  "type": "future"},
            "SOYOIL":   {"ticker": "ZL=F", "name_ja": "大豆油",   "unit": "USc/lb",  "type": "future"},
            "SOYMEAL":  {"ticker": "ZM=F", "name_ja": "大豆粉",   "unit": "USD/ton", "type": "future"},
        },
    },
    # ─────────────────────────────────────────────────────────────
    # カテゴリ5: ソフトコモディティ（嗜好品・農産物）
    # ─────────────────────────────────────────────────────────────
    "softs": {
        "name_ja": "ソフトコモディティ",
        "emoji": "☕",
        "items": {
            "COFFEE":   {"ticker": "KC=F",  "name_ja": "コーヒー",      "unit": "USc/lb",  "type": "future"},
            "COCOA":    {"ticker": "CC=F",  "name_ja": "カカオ",        "unit": "USD/ton", "type": "future"},
            "SUGAR":    {"ticker": "SB=F",  "name_ja": "砂糖 #11",      "unit": "USc/lb",  "type": "future"},
            "COTTON":   {"ticker": "CT=F",  "name_ja": "綿花",          "unit": "USc/lb",  "type": "future"},
            "OJ":       {"ticker": "OJ=F",  "name_ja": "オレンジジュース","unit":"USc/lb",  "type": "future"},
            "LUMBER":   {"ticker": "LBR=F", "name_ja": "木材（Lumber）", "unit": "USD/MBF", "type": "future"},
        },
    },
    # ─────────────────────────────────────────────────────────────
    # カテゴリ6: 畜産
    # ─────────────────────────────────────────────────────────────
    "livestock": {
        "name_ja": "畜産",
        "emoji": "🐄",
        "items": {
            "CATTLE":   {"ticker": "LE=F", "name_ja": "生牛",      "unit": "USc/lb",  "type": "future"},
            "HOGS":     {"ticker": "HE=F", "name_ja": "赤豚（豚）","unit": "USc/lb",  "type": "future"},
            "FEEDER":   {"ticker": "GF=F", "name_ja": "フィーダー牛","unit":"USc/lb", "type": "future"},
        },
    },
    # ─────────────────────────────────────────────────────────────
    # カテゴリ7: 特殊・戦略的コモディティ（ETFプロキシ）
    # ─────────────────────────────────────────────────────────────
    "special": {
        "name_ja": "特殊・戦略的コモディティ",
        "emoji": "⚡",
        "items": {
            "URANIUM":  {"ticker": "URA",  "name_ja": "ウラン（原子力）","unit":"ETF/NAV","type": "etf"},
            "LITHIUM":  {"ticker": "LIT",  "name_ja": "リチウム・電池材料","unit":"ETF/NAV","type":"etf"},
            "COPPER_M": {"ticker": "COPX", "name_ja": "銅採掘（COPX）",  "unit":"ETF/NAV","type": "etf"},
            "RAREEARTH":{"ticker": "REMX", "name_ja": "レアアース・希少金属","unit":"ETF/NAV","type":"etf"},
            "PALM_OIL": {"ticker": "JO",   "name_ja": "パーム油（プロキシ）","unit":"ETF/NAV","type":"etf"},
        },
    },
}

# ==============================================================
# BLOCK 2: JP33業種・US11セクター 影響マッピング
# ==============================================================
# 構造: {commodity_key: {"jp": [(sector_code, direction, magnitude)],
#                         "us": [(etf_symbol,  direction, magnitude)}
# direction: "+" = 価格上昇が恩恵  "-" = 価格上昇がコスト増
# magnitude: 1=小 / 2=中 / 3=大
#
# JP33業種コード早見表（主要のみ）:
#   1001=水産農林 1002=鉱業  1004=食料品 1007=化学  1009=石油石炭
#   1012=鉄鋼    1013=非鉄  1015=機械   1016=電気機器 1017=輸送機器
#   1020=電気ガス 1021=陸運  1022=海運   1023=空運   1028=銀行
# ==============================================================

SECTOR_IMPACT_MAP: dict = {
    # ── エネルギー ──────────────────────────────────────────────
    "WTI":     {"jp": [("1009","+",3),("1002","+",2),("1021","-",3),("1022","-",2),
                        ("1023","-",3),("1017","-",2),("1004","-",1),("1007","-",1)],
                "us": [("XLE","+",3),("XLI","-",2),("XLY","-",2),("XLP","-",1)]},
    "BRENT":   {"jp": [("1009","+",3),("1002","+",2),("1021","-",2),("1023","-",3)],
                "us": [("XLE","+",3),("XLI","-",2)]},
    "NATGAS":  {"jp": [("1009","+",3),("1020","+",2),("1007","-",2),("1004","-",1)],
                "us": [("XLE","+",3),("XLU","+",2),("XLB","-",1)]},
    "GASOLINE":{"jp": [("1021","-",2),("1023","-",2),("1009","+",2)],
                "us": [("XLE","+",2),("XLY","-",1)]},
    "HEATOIL": {"jp": [("1009","+",2),("1020","+",1),("1021","-",1)],
                "us": [("XLE","+",2),("XLU","+",1)]},

    # ── 貴金属 ────────────────────────────────────────────────
    "GOLD":     {"jp": [("1013","+",2),("1029","+",1)],
                 "us": [("XLB","+",2),("XLF","-",1)]},  # 金高=リスクオフ=金融株↓
    "SILVER":   {"jp": [("1013","+",2),("1016","-",1)],  # 工業用途もあり
                 "us": [("XLB","+",2),("XLI","-",1)]},
    "PLATINUM": {"jp": [("1013","+",2),("1017","-",1)],  # 触媒用途
                 "us": [("XLB","+",1),("XLI","-",1)]},
    "PALLADIUM":{"jp": [("1013","+",2),("1017","-",2)],  # 自動車排ガス触媒
                 "us": [("XLB","+",2),("XLY","-",2)]},

    # ── ベースメタル ─────────────────────────────────────────
    "COPPER":    {"jp": [("1013","+",3),("1016","-",2),("1017","-",2),
                          ("1015","-",1),("1020","-",1)],
                  "us": [("XLB","+",3),("XLI","-",2),("XLY","-",1)]},
    "ALUMINUM":  {"jp": [("1013","+",3),("1017","-",3),("1004","-",1)],
                  "us": [("XLB","+",3),("XLY","-",2),("XLI","-",1)]},
    "NICKEL":    {"jp": [("1013","+",2),("1012","-",2),("1017","-",1)],
                  "us": [("XLB","+",2),("XLI","-",1)]},
    "ZINC":      {"jp": [("1013","+",2),("1012","-",2)],
                  "us": [("XLB","+",2),("XLI","-",1)]},
    "STEEL_ETF": {"jp": [("1012","+",3),("1017","-",2),("1003","-",1)],
                  "us": [("XLB","+",3),("XLI","-",1)]},

    # ── 穀物 ────────────────────────────────────────────────
    "WHEAT":     {"jp": [("1001","+",2),("1004","-",3)],
                  "us": [("XLP","-",2)]},
    "CORN":      {"jp": [("1001","+",2),("1004","-",2)],
                  "us": [("XLP","-",2)]},
    "SOYBEANS":  {"jp": [("1001","+",2),("1004","-",2),("1007","-",1)],
                  "us": [("XLP","-",2)]},
    "RICE":      {"jp": [("1001","+",3),("1004","-",2)],
                  "us": [("XLP","-",1)]},
    "OATS":      {"jp": [("1004","-",1)],
                  "us": [("XLP","-",1)]},
    "SOYOIL":    {"jp": [("1004","-",2),("1007","-",1)],
                  "us": [("XLP","-",2)]},
    "SOYMEAL":   {"jp": [("1004","-",2)],
                  "us": [("XLP","-",1)]},

    # ── ソフトコモディティ ──────────────────────────────────
    "COFFEE":    {"jp": [("1004","-",2)],
                  "us": [("XLP","-",1)]},
    "COCOA":     {"jp": [("1004","-",1)],
                  "us": [("XLP","-",1)]},
    "SUGAR":     {"jp": [("1004","-",2),("1007","-",1)],
                  "us": [("XLP","-",2)]},
    "COTTON":    {"jp": [("1005","-",3)],  # 繊維製品
                  "us": [("XLY","-",1)]},
    "OJ":        {"jp": [("1004","-",1)],
                  "us": [("XLP","-",1)]},
    "LUMBER":    {"jp": [("1003","-",2),("1006","-",2)],  # 建設・パルプ紙
                  "us": [("XLRE","-",2),("XLI","-",1)]},

    # ── 畜産 ────────────────────────────────────────────────
    "CATTLE":    {"jp": [("1004","-",2)],
                  "us": [("XLP","-",1)]},
    "HOGS":      {"jp": [("1004","-",2)],
                  "us": [("XLP","-",1)]},
    "FEEDER":    {"jp": [("1004","-",1)],
                  "us": [("XLP","-",1)]},

    # ── 特殊 ────────────────────────────────────────────────
    "URANIUM":   {"jp": [("1020","+",3)],  # 電力・原子力
                  "us": [("XLU","+",3)]},
    "LITHIUM":   {"jp": [("1013","+",3),("1016","+",2),("1017","+",2)],
                  "us": [("XLB","+",3),("XLY","+",2)]},
    "COPPER_M":  {"jp": [("1013","+",2)],
                  "us": [("XLB","+",2)]},
    "RAREEARTH": {"jp": [("1016","+",2),("1013","+",2)],
                  "us": [("XLB","+",2),("XLK","+",1)]},
    "PALM_OIL":  {"jp": [("1004","-",2),("1007","-",1)],
                  "us": [("XLP","-",2)]},
}

# FRED シリーズ定義
FRED_SERIES: dict = {
    "US_1M":   {"id": "DGS1MO", "name": "米1ヶ月"},
    "US_3M":   {"id": "DGS3MO", "name": "米3ヶ月"},
    "US_6M":   {"id": "DGS6MO", "name": "米6ヶ月"},
    "US_1Y":   {"id": "DGS1",   "name": "米1年"},
    "US_2Y":   {"id": "DGS2",   "name": "米2年"},
    "US_5Y":   {"id": "DGS5",   "name": "米5年"},
    "US_7Y":   {"id": "DGS7",   "name": "米7年"},
    "US_10Y":  {"id": "DGS10",  "name": "米10年"},
    "US_20Y":  {"id": "DGS20",  "name": "米20年"},
    "US_30Y":  {"id": "DGS30",  "name": "米30年"},
    "SP_10Y2Y":{"id": "T10Y2Y", "name": "スプレッド10Y-2Y"},
    "SP_10Y3M":{"id": "T10Y3M", "name": "スプレッド10Y-3M"},
    "JP_10Y":  {"id": "IRLTLT01JPM156N", "name": "日本10年（月次）"},
}

# yfinance fallback（FRED未設定時）
YIELD_FALLBACK_TICKERS: dict = {
    "US_3M":  "^IRX",
    "US_5Y":  "^FVX",
    "US_10Y": "^TNX",
    "US_30Y": "^TYX",
}

# リセッション確率スコアの重みパラメータ

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'BCP_RECESSION_WEIGHTS': 'RECESSION_WEIGHTS',
}

RECESSION_WEIGHTS = {
    "inversion_2y10y":   40,  # 10Y-2Y逆転: 最も信頼度が高い指標
    "inversion_3m10y":   30,  # 10Y-3M逆転: FRBが重視
    "inversion_days":    20,  # 逆転継続日数（30日超で満点）
    "yield_drop_speed":  10,  # 長期金利の急低下（パニック的）
}

# COMMODITY_CACHE_DAYS は削除（2026-07-25 バッチ2）。
# キャッシュ処理そのものが未実装で、この定数を読むコードは存在しなかった。
# config.py 側の BOND_COMMODITY_CACHE_DAYS も同時に削除している。
# 実装する場合は config を正本にすること（モジュール側に定数を作らない）。

# ==============================================================
# BLOCK 3: FRED API 債券データ取得
# ==============================================================

def _fetch_fred_series(series_id: str, fred_api_key: str,
                        observation_start: str | None = None) -> Optional[list]:
    """FRED API から系列データを取得。最新値リスト（降順）を返す。"""
    import requests
    if not fred_api_key:
        return None
    if observation_start is None:
        observation_start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id":          series_id,
                "api_key":            fred_api_key,
                "file_type":          "json",
                "observation_start":  observation_start,
                "sort_order":         "desc",
                "limit":              30,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None
        obs = r.json().get("observations", [])
        # ドット（.）は欠損値のため除外
        return [o for o in obs if o.get("value", ".") != "."]
    except Exception:
        return None


def fetch_bond_yields(fred_api_key: str) -> dict:
    """
    全国債利回りを取得。
    FRED API 優先 → yfinance fallback。
    戻り値: {series_key: {"value": float, "date": str, "name": str}}
    """
    import yfinance as yf  # type: ignore
    results = {}

    if fred_api_key:
        print("    FRED API で債券利回りを取得中...")
        for key, meta in FRED_SERIES.items():
            obs = _fetch_fred_series(meta["id"], fred_api_key)
            if obs:
                latest = obs[0]
                # 1ヶ月前値（変化量計算用）
                prev_val = None
                if len(obs) >= 22:  # 約1ヶ月前
                    prev_val = float(obs[21]["value"])
                results[key] = {
                    "value":    float(latest["value"]),
                    "date":     latest["date"],
                    "name":     meta["name"],
                    "change_1m": round(float(latest["value"]) - prev_val, 3)
                                 if prev_val else None,
                    "source":   "FRED",
                }
            time.sleep(0.1)  # FRED API レート制限（120req/min）
    else:
        print("    FRED API KEY 未設定 → yfinance fallback...")

    # yfinance fallback（FRED未取得分を補完）
    for key, yticker in YIELD_FALLBACK_TICKERS.items():
        if key not in results:
            try:
                tk = yf.Ticker(yticker)
                hist = tk.history(period="2mo", interval="1d")
                if hist is not None and not hist.empty:
                    val = float(hist["Close"].iloc[-1])
                    prev_val = float(hist["Close"].iloc[-22]) if len(hist) >= 22 else None
                    results[key] = {
                        "value":     val,
                        "date":      hist.index[-1].strftime("%Y-%m-%d"),
                        "name":      FRED_SERIES.get(key, {}).get("name", key),
                        "change_1m": round(val - prev_val, 3) if prev_val else None,
                        "source":    "yfinance",
                    }
            except Exception:
                pass

    return results


# ==============================================================
# BLOCK 4: コモディティ価格取得
# ==============================================================

def fetch_commodity_prices() -> dict:
    """
    全コモディティ先物・ETFの現在価格と1W/1M/3Mリターンを取得。
    戻り値: {commodity_key: {"price": float, "ret_1w": float, ...}}
    """
    import yfinance as yf  # type: ignore

    all_items = {}
    for cat_key, cat_data in COMMODITY_UNIVERSE.items():
        for item_key, item_meta in cat_data["items"].items():
            all_items[item_key] = {
                "ticker":   item_meta["ticker"],
                "name_ja":  item_meta["name_ja"],
                "unit":     item_meta["unit"],
                "type":     item_meta["type"],
                "category": cat_key,
            }

    results = {}
    tickers_list = [v["ticker"] for v in all_items.values()]

    print(f"    コモディティ {len(tickers_list)} 銘柄を取得中...")
    try:
        # 一括取得で API コール数を最小化
        data = yf.download(
            tickers_list,
            period="4mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"    WARNING: 一括取得失敗: {e} → 個別取得にフォールバック")
        data = None

    for item_key, meta in all_items.items():
        ticker = meta["ticker"]
        try:
            # 一括取得データから該当ティッカーの系列を取得
            if data is not None and not data.empty:
                try:
                    if len(tickers_list) == 1:
                        # 単一銘柄: Closeが直接Series
                        hist_close = data["Close"] if "Close" in data.columns else None
                    else:
                        # 複数銘柄: data["Close"] がDataFrame、列名がティッカー
                        close_df = data["Close"] if "Close" in data.columns else None
                        if close_df is not None and hasattr(close_df, "columns") and ticker in close_df.columns:
                            hist_close = close_df[ticker]
                        else:
                            hist_close = None
                except Exception:
                    hist_close = None

            # fallback: 個別取得
            if hist_close is None or hist_close.dropna().empty:
                tk = yf.Ticker(ticker)
                hist = tk.history(period="4mo", interval="1d")
                if hist is not None and not hist.empty:
                    hist_close = hist["Close"]
                else:
                    results[item_key] = {**meta, "price": None, "error": "取得失敗"}
                    continue

            hist_close = hist_close.dropna()
            if hist_close.empty:
                results[item_key] = {**meta, "price": None, "error": "データなし"}
                continue

            price = float(hist_close.iloc[-1])
            price_date = hist_close.index[-1]
            if hasattr(price_date, "strftime"):
                price_date = price_date.strftime("%Y-%m-%d")

            def _ret(days: int) -> Optional[float]:
                if len(hist_close) < days:
                    return None
                old = float(hist_close.iloc[-days])
                return round((price - old) / old * 100, 2) if old != 0 else None

            # 1W≈5営業日 / 1M≈22営業日 / 3M≈66営業日
            results[item_key] = {
                **meta,
                "price":      round(price, 4),
                "price_date": price_date,
                "ret_1w":     _ret(5),
                "ret_1m":     _ret(22),
                "ret_3m":     _ret(66),
                "error":      None,
            }
        except Exception as e:
            results[item_key] = {**meta, "price": None, "error": str(e)[:60]}

    ok_count = sum(1 for v in results.values() if v.get("price") is not None)
    print(f"    OK: {ok_count}/{len(all_items)} 銘柄取得成功")
    return results


# ==============================================================
# BLOCK 5: イールドカーブ分析
# ==============================================================

def analyze_yield_curve(yields: dict) -> dict:
    """
    イールドカーブの形状・逆転状況・リセッション確率スコアを算出。
    戻り値:
      shape: "normal" | "flat" | "inverted" | "humped"
      spread_2y10y: float
      spread_3m10y: float
      is_inverted_2y10y: bool
      is_inverted_3m10y: bool
      recession_score: int (0〜100)
      recession_label: str
    """
    def _get(key: str) -> Optional[float]:
        v = yields.get(key, {})
        if isinstance(v, dict):
            return v.get("value")
        return None

    y_3m  = _get("US_3M")
    y_2y  = _get("US_2Y")
    y_5y  = _get("US_5Y")
    y_10y = _get("US_10Y")
    y_30y = _get("US_30Y")

    # スプレッド計算
    sp_2y10y = round(y_10y - y_2y, 3)   if (y_10y and y_2y)  else None
    sp_3m10y = round(y_10y - y_3m, 3)   if (y_10y and y_3m)  else None
    sp_2y30y = round(y_30y - y_2y, 3)   if (y_30y and y_2y)  else None

    # FRED の直接スプレッドを優先
    if "SP_10Y2Y" in yields:
        sp_2y10y = yields["SP_10Y2Y"].get("value", sp_2y10y)
    if "SP_10Y3M" in yields:
        sp_3m10y = yields["SP_10Y3M"].get("value", sp_3m10y)

    is_inv_2y10y = (sp_2y10y is not None and sp_2y10y < 0)
    is_inv_3m10y = (sp_3m10y is not None and sp_3m10y < 0)

    # カーブ形状判定
    shape = "unknown"
    if y_2y and y_5y and y_10y and y_30y:
        if y_2y > y_10y and y_10y > y_30y:
            shape = "fully_inverted"
        elif y_2y > y_10y:
            shape = "inverted"
        elif abs(y_10y - y_2y) < 0.20:
            shape = "flat"
        elif y_5y > y_10y and y_5y > y_2y:
            shape = "humped"
        else:
            shape = "normal"

    shape_ja = {
        "normal":         "正常（順イールド）",
        "flat":           "フラット（中立）",
        "humped":         "逆ヒュームド",
        "inverted":       "逆イールド（部分）",
        "fully_inverted": "完全逆イールド（警戒）",
        "unknown":        "不明",
    }.get(shape, shape)

    # ── リセッション確率スコア（0〜100）────────────────────────
    # 各コンポーネントを0〜1に正規化して加重合算
    score = 0.0

    # ① 10Y-2Y逆転（最も信頼度高）
    if sp_2y10y is not None:
        # -2.0% 以下で満点 / +0.5% 以上でゼロ
        s1 = max(0.0, min(1.0, (-sp_2y10y) / 2.0)) if sp_2y10y < 0.5 else 0.0
        score += s1 * RECESSION_WEIGHTS["inversion_2y10y"]

    # ② 10Y-3M逆転（FRBが重視）
    if sp_3m10y is not None:
        s2 = max(0.0, min(1.0, (-sp_3m10y) / 2.0)) if sp_3m10y < 0.5 else 0.0
        score += s2 * RECESSION_WEIGHTS["inversion_3m10y"]

    # ③ 10Y金利の急低下（パニック的逃避）
    y10y_change = yields.get("US_10Y", {}).get("change_1m")
    if y10y_change is not None and y10y_change < -0.30:
        s4 = min(1.0, abs(y10y_change) / 1.0)
        score += s4 * RECESSION_WEIGHTS["yield_drop_speed"]

    recession_score = int(round(min(100, score)))

    # ラベル
    if recession_score >= 70:
        recession_label = "🔴 高リスク（リセッション警戒）"
    elif recession_score >= 45:
        recession_label = "🟡 中リスク（注視）"
    elif recession_score >= 20:
        recession_label = "🟢 低リスク（正常範囲）"
    else:
        recession_label = "🟢 極低リスク（景気拡張）"

    return {
        "shape":              shape,
        "shape_ja":           shape_ja,
        "spread_2y10y":       sp_2y10y,
        "spread_3m10y":       sp_3m10y,
        "spread_2y30y":       sp_2y30y,
        "is_inverted_2y10y":  is_inv_2y10y,
        "is_inverted_3m10y":  is_inv_3m10y,
        "recession_score":    recession_score,
        "recession_label":    recession_label,
    }

# ==============================================================
# BLOCK 5.5: [F-1] イールドカーブ逆転 → 業種 composite_score 補正
# ==============================================================
# Sloan(1996)アクルーアル補正と並ぶ「算出優位型アルファ」の核心。
# FRED 2Y/10Y スプレッドは広く知られているが、
# 「業種別定量補正」への自動換算は手作業では困難 → 情報優位が発生する。

# ── 金利感応業種の補正マップ ──────────────────────────────────
# 各タプル: (high_adj, medium_adj, inversion_extra)
#   recession_score >= 70 → high_adj を加算
#   recession_score >= 45 → medium_adj を加算
#   is_inverted_2y10y=True → inversion_extra を追加加算
# 根拠:
#   銀行(1028): 逆イールド=利鞘圧縮→利益直撃。貸倒引当金増。
#   不動産(1032): 金利上昇・信用収縮で物件価格下落リスク。
#   保険(1030): 長短スプレッド縮小で運用益低下。
#   電気ガス(1020): 景気後退で工場稼働減→需要減退。
#   小売(1027): リセッション時の消費抑制直撃。
#   電気機器(1016): 景気敏感株。リセッション懸念で最初に売られる。
#   輸送機器(1017): 耐久財消費減退。機械(1015): 設備投資凍結。
#   医薬品(1008)/食料品(1004): ディフェンシブ→相対優位でプラス。
RECESSION_SECTOR_IMPACT: dict = {
    "1028": {"high": -12, "medium": -6, "inversion_extra": -5},   # 銀行
    "1032": {"high": -10, "medium": -5, "inversion_extra": -3},   # 不動産
    "1030": {"high":  -8, "medium": -4, "inversion_extra": -2},   # 保険
    "1020": {"high":  -7, "medium": -3, "inversion_extra":  0},   # 電気・ガス
    "1027": {"high":  -6, "medium": -3, "inversion_extra":  0},   # 小売
    "1016": {"high":  -5, "medium": -2, "inversion_extra":  0},   # 電気機器
    "1017": {"high":  -5, "medium": -2, "inversion_extra":  0},   # 輸送用機器
    "1015": {"high":  -4, "medium": -2, "inversion_extra":  0},   # 機械
    "1031": {"high":  -4, "medium": -2, "inversion_extra": -2},   # その他金融
    "1029": {"high":  -4, "medium": -2, "inversion_extra": -2},   # 証券
    "1008": {"high":   3, "medium":  1, "inversion_extra":  2},   # 医薬品（ディフェンシブ）
    "1004": {"high":   2, "medium":  1, "inversion_extra":  1},   # 食料品（ディフェンシブ）
    "1033": {"high":   1, "medium":  0, "inversion_extra":  1},   # サービス
}


def apply_recession_score_to_sectors(
    recession_score: int,
    curve_analysis:  dict,
    sector_results:  dict,
) -> dict:
    """
    [F-1] イールドカーブ逆転・リセッションスコアを業種 composite_score に反映する。

    Args:
        recession_score: analyze_yield_curve() の recession_score (0〜100)
        curve_analysis:  analyze_yield_curve() の戻り値辞書
        sector_results:  {sector_code: result_dict} ← in-place 更新

    Returns:
        sector_results（in-place 変更済み・同一オブジェクト）

    副作用:
        各 result_dict に以下のキーを追加:
          composite_score_pre_recession: float  元スコア（ロールバック用）
          composite_score:               float  補正後スコア（上書き）
          recession_adjustment:          float  加算量（正=プラス補正）
    """
    is_inv_2y10y = curve_analysis.get("is_inverted_2y10y", False)
    is_inv_3m10y = curve_analysis.get("is_inverted_3m10y", False)
    sp_2y10y     = curve_analysis.get("spread_2y10y")

    applied = []

    for code, impact in RECESSION_SECTOR_IMPACT.items():
        res = sector_results.get(code)
        if not isinstance(res, dict):
            continue

        base_score = float(res.get("composite_score", 0))

        # ① リセッションスコアによる基本補正
        if recession_score >= 70:
            adj = float(impact["high"])
        elif recession_score >= 45:
            adj = float(impact["medium"])
        else:
            adj = 0.0

        # ② 逆イールド追加補正（2y10y逆転 OR 3m10y逆転）
        if (is_inv_2y10y or is_inv_3m10y) and impact.get("inversion_extra", 0) != 0:
            adj += float(impact["inversion_extra"])

        # ③ 深刻な逆転（スプレッド -0.5% 未満）は補正を30%増幅
        if sp_2y10y is not None and sp_2y10y < -0.5 and adj < 0:
            adj *= 1.3

        adj = round(adj, 1)
        if adj == 0.0:
            continue

        new_score = round(max(-30.0, min(100.0, base_score + adj)), 1)

        # 元スコアを保存してから上書き
        res["composite_score_pre_recession"] = base_score
        res["composite_score"]               = new_score
        res["recession_adjustment"]          = adj

        name = res.get("sector_name_ja", code)
        sign = "+" if adj >= 0 else ""
        applied.append(f"{name}:{sign}{adj:.0f}")

    if applied:
        print(f"    📉 [F-1] リセッション補正: {', '.join(applied)}")
    elif recession_score < 20:
        print(f"    ✅ [F-1] リセッションスコア{recession_score}/100: 補正なし（低リスク）")

    return sector_results

# ==============================================================
# BLOCK 6: コモディティ変動 → セクタースコア補正量算出
# ==============================================================

def calc_sector_deltas(commodity_data: dict) -> dict:
    """
    1M リターンに基づいてJP業種・USセクターのスコア補正量を算出する。

    ロジック:
      1Mリターン +10%超 → magnitude × 3ポイント補正
      1Mリターン +5〜10% → magnitude × 1.5ポイント補正
      ±5%以内 → 補正なし
      -5〜-10% → -magnitude × 1.5ポイント補正
      -10%超 → -magnitude × 3ポイント補正

    戻り値: {"jp": {sector_code: delta}, "us": {etf_symbol: delta}}
    """
    jp_deltas: dict = {}
    us_deltas: dict = {}

    for item_key, cdata in commodity_data.items():
        ret_1m = cdata.get("ret_1m")
        if ret_1m is None or item_key not in SECTOR_IMPACT_MAP:
            continue

        # 変動幅に応じた係数（影響スケール）
        if ret_1m > 10:
            coeff = 3.0
        elif ret_1m > 5:
            coeff = 1.5
        elif ret_1m > 2:
            coeff = 0.5
        elif ret_1m < -10:
            coeff = -3.0
        elif ret_1m < -5:
            coeff = -1.5
        elif ret_1m < -2:
            coeff = -0.5
        else:
            continue  # ±2%以内は補正なし

        impact = SECTOR_IMPACT_MAP[item_key]
        for (sector_code, direction, magnitude) in impact.get("jp", []):
            delta = coeff * magnitude * (1 if direction == "+" else -1)
            jp_deltas[sector_code] = round(jp_deltas.get(sector_code, 0) + delta, 2)

        for (etf_sym, direction, magnitude) in impact.get("us", []):
            delta = coeff * magnitude * (1 if direction == "+" else -1)
            us_deltas[etf_sym] = round(us_deltas.get(etf_sym, 0) + delta, 2)

    # スコアを ±15ポイントにクリップ（extreme な単一要因による過補正防止）
    jp_deltas = {k: max(-15, min(15, v)) for k, v in jp_deltas.items()}
    us_deltas = {k: max(-15, min(15, v)) for k, v in us_deltas.items()}

    return {"jp": jp_deltas, "us": us_deltas}

# ==============================================================
# BLOCK 6.5: FRED金利シグナル → GICSセクタースコア接続（Wave 1 - U-2）
# ==============================================================

def calc_rate_sensitivity_deltas(yields_data: dict, cfg) -> dict:
    """
    FRED から取得した US10Y 利回り変化量を US_RATE_SENSITIVITY_RULES に接続し、
    GICSセクターETFごとのスコア補正量を算出する（U-2）。

    算出式:
      $delta = magnitude \times change\\_1m\\_bps$
      ただし $|change\\_1m\\_bps| < rate\\_threshold\\_bp$ なら発動しない。
      発動した場合は $[-max\\_delta, +max\\_delta]$ にクリップする。

    Args:
        yields_data: fetch_yields() の返り値
                     {"us10y": {"latest": float, "change_1m": float, ...}, ...}
        cfg:         config モジュール（US_RATE_SENSITIVITY_RULES を参照）

    Returns:
        {"XLF": delta_pt, "XLRE": delta_pt, ...}
        補正が発動しなかった ETF は含まない
    """
    rate_deltas: dict = {}
    try:
        rules = getattr(cfg, "US_RATE_SENSITIVITY_RULES", {})
        if not rules:
            return rate_deltas

        # US10Y 1M変化量を bps に変換（%pt × 100）
        us10y_data       = yields_data.get("us10y", {})
        change_1m_pct    = float(us10y_data.get("change_1m", 0) or 0)
        change_1m_bps    = round(change_1m_pct * 100, 1)   # %pt → bps

        if change_1m_bps == 0:
            return rate_deltas

        applied = []
        for etf_sym, rule in rules.items():
            try:
                threshold = float(rule.get("rate_threshold_bp", 25))
                magnitude = float(rule.get("magnitude", 0))
                max_delta = float(rule.get("max_delta", 5))

                # 閾値未満は発動しない（ノイズ除去）
                if abs(change_1m_bps) < threshold:
                    continue

                # $delta = magnitude \times change\_1m\_bps$
                raw_delta = magnitude * change_1m_bps
                # max_delta でクリップ（方向を維持）
                clipped = max(-max_delta, min(max_delta, raw_delta))
                clipped = round(clipped, 2)

                if clipped != 0.0:
                    rate_deltas[etf_sym] = clipped
                    sign = "+" if clipped >= 0 else ""
                    applied.append(f"{etf_sym}:{sign}{clipped:.1f}")

            except Exception:
                continue

        if applied:
            direction = "上昇" if change_1m_bps > 0 else "低下"
            print(
                f"    📈 [U-2] 金利感応補正 (US10Y {change_1m_bps:+.0f}bps {direction}): "
                f"{', '.join(applied[:6])}"
                f"{'...' if len(applied) > 6 else ''}"
            )

    except Exception as e:
        print(f"    ⚠️  [U-2] 金利感応補正エラー（スキップ）: {str(e)[:60]}")

    return rate_deltas

# ==============================================================
# BLOCK 7: MDレポート生成
# ==============================================================

def _sparkline(ret: Optional[float]) -> str:
    """1Mリターンをシンプルなアイコンで表現"""
    if ret is None:
        return "  ―  "
    if ret >= 5:
        return f"🔺{ret:+.1f}%"
    if ret >= 2:
        return f"▲{ret:+.1f}%"
    if ret <= -5:
        return f"🔻{ret:+.1f}%"
    if ret <= -2:
        return f"▼{ret:+.1f}%"
    return f" →{ret:+.1f}%"


def generate_md_report(
    yields: dict,
    curve_analysis: dict,
    commodity_data: dict,
    sector_deltas: dict,
    today_str: str,
) -> str:
    """全データを統合したMarkdownレポートを生成する"""

    lines = [
        f"# 📊 債券・コモディティ統合レポート {today_str}",
        "",
        f"*生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} JST*",
        "",
        "---",
        "",
        "## 🏛️ 1. 米国国債イールドカーブ",
        "",
    ]

    # ── 利回り一覧表 ──
    lines += ["| 年限 | 利回り | 1Mチェンジ | ソース |",
              "|------|--------|-----------|-------|"]
    display_keys = ["US_1M","US_3M","US_6M","US_1Y","US_2Y","US_5Y","US_10Y","US_20Y","US_30Y"]
    for k in display_keys:
        if k not in yields:
            continue
        d = yields[k]
        val    = d.get("value")
        chg    = d.get("change_1m")
        chg_s  = f"{chg:+.3f}%" if chg else "―"
        arrow  = "↑" if (chg and chg > 0) else ("↓" if (chg and chg < 0) else "→")
        lines.append(f"| {d['name']} | {val:.3f}% | {arrow} {chg_s} | {d.get('source','―')} |")

    lines += [
        "",
        "### スプレッド（逆イールド判定）",
        "",
    ]
    ca = curve_analysis
    inv2 = "⚠️ **逆転中**" if ca.get("is_inverted_2y10y") else "✅ 正常"
    inv3 = "⚠️ **逆転中**" if ca.get("is_inverted_3m10y") else "✅ 正常"
    sp2  = ca.get("spread_2y10y")
    sp3  = ca.get("spread_3m10y")

    lines += [
        f"- **10Y − 2Y**: {sp2:+.3f}% {inv2}" if sp2 is not None else "- 10Y − 2Y: データなし",
        f"- **10Y − 3M**: {sp3:+.3f}% {inv3}" if sp3 is not None else "- 10Y − 3M: データなし",
        f"- **カーブ形状**: {ca.get('shape_ja','不明')}",
        "",
        "### リセッション確率スコア",
        "",
        f"> ## {ca.get('recession_score', 0)} / 100  {ca.get('recession_label','')}",
        "",
        "> **スコア計算根拠**: 10Y−2Y逆転(40点) + 10Y−3M逆転(30点) + ",
        "> 逆転継続日数(20点) + 長期金利急低下(10点) で算出",
        "",
        "---",
        "",
        "## 🛢️ 2. コモディティ全銘柄 価格・リターン",
        "",
    ]

    # カテゴリごとに表示
    for cat_key, cat_data in COMMODITY_UNIVERSE.items():
        emoji = cat_data.get("emoji", "")
        name_ja = cat_data.get("name_ja", cat_key)
        lines += [f"### {emoji} {name_ja}", ""]
        lines += ["| 銘柄 | 現在価格 | 1W | 1M | 3M | 種別 |",
                  "|------|---------|-----|-----|-----|------|"]

        for item_key in cat_data["items"]:
            if item_key not in commodity_data:
                continue
            d = commodity_data[item_key]
            price_s = f"{d['price']:.3f}" if d.get("price") else "―"
            unit    = d.get("unit", "")
            if d.get("error") and not d.get("price"):
                lines.append(f"| {d['name_ja']} | ⚠️ 取得失敗 | ― | ― | ― | {d.get('type','')} |")
                continue
            lines.append(
                f"| {d['name_ja']} ({d['ticker']}) "
                f"| {price_s} {unit} "
                f"| {_sparkline(d.get('ret_1w'))} "
                f"| {_sparkline(d.get('ret_1m'))} "
                f"| {_sparkline(d.get('ret_3m'))} "
                f"| {d.get('type','')} |"
            )
        lines.append("")

    # ── セクター影響サマリー ──
    lines += [
        "---",
        "",
        "## 🏭 3. セクター影響サマリー（1Mリターンベース）",
        "",
        "### 🇯🇵 日本株 JP33業種 スコア補正量",
        "",
    ]
    jp_d = sector_deltas.get("jp", {})
    if jp_d:
        lines += ["| 業種コード | 補正量 | 方向 |", "|-----------|--------|------|"]
        for code, delta in sorted(jp_d.items(), key=lambda x: abs(x[1]), reverse=True)[:15]:
            arrow = "🔺" if delta > 0 else "🔻"
            lines.append(f"| {code} | {delta:+.1f}pt | {arrow} |")
    else:
        lines.append("（コモディティ変動が小さく補正なし）")

    lines += [
        "",
        "### 🇺🇸 US セクター ETF 補正量",
        "",
    ]
    us_d = sector_deltas.get("us", {})
    if us_d:
        lines += ["| ETF | 補正量 | 方向 |", "|-----|--------|------|"]
        for etf, delta in sorted(us_d.items(), key=lambda x: abs(x[1]), reverse=True):
            arrow = "🔺" if delta > 0 else "🔻"
            lines.append(f"| {etf} | {delta:+.1f}pt | {arrow} |")
    else:
        lines.append("（コモディティ変動が小さく補正なし）")

    # ── 注目コモディティ TOP5 ──
    lines += [
        "",
        "---",
        "",
        "## 🌟 4. 注目コモディティ（1M変化 大きい順）",
        "",
    ]
    valid_items = [
        (k, v) for k, v in commodity_data.items()
        if v.get("ret_1m") is not None
    ]
    top_gainers = sorted(valid_items, key=lambda x: x[1]["ret_1m"], reverse=True)[:5]
    top_losers  = sorted(valid_items, key=lambda x: x[1]["ret_1m"])[:5]

    lines += ["**📈 上昇TOP5（1M）**", ""]
    for k, d in top_gainers:
        lines.append(f"- {d['name_ja']} ({d['ticker']}): **{d['ret_1m']:+.1f}%**")

    lines += ["", "**📉 下落TOP5（1M）**", ""]
    for k, d in top_losers:
        lines.append(f"- {d['name_ja']} ({d['ticker']}): **{d['ret_1m']:+.1f}%**")

    lines += [
        "",
        "---",
        f"*データソース: FRED API / yfinance | 生成: {today_str}*",
    ]

    return "\n".join(lines)


# ==============================================================
# BLOCK 8: JSON出力 & ファイル保存
# ==============================================================

def save_outputs(cfg, yields: dict, curve_analysis: dict,
                 commodity_data: dict, sector_deltas: dict,
                 md_report: str, today_str: str):
    """MDレポートとJSONを所定ディレクトリへ保存"""
    macro_dir = Path(cfg.INSIGHT_DIR) / "macro"
    data_dir  = Path(cfg.DATA_DIR)
    macro_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # MDレポート
    md_path = macro_dir / f"bond_commodity_{today_str}.md"
    md_path.write_text(md_report, encoding="utf-8")
    print(f"    MD: {md_path}")

    # JSON（他モジュール連携用）
    # history や大型データは除外してスリムに保存
    slim_commodities = {}
    for k, v in commodity_data.items():
        slim_commodities[k] = {
            x: v[x] for x in
            ("name_ja","ticker","category","type","price","price_date",
             "ret_1w","ret_1m","ret_3m","unit","error")
            if x in v
        }

    payload = {
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "date":           today_str,
        "yields":         yields,
        "curve_analysis": curve_analysis,
        "commodities":    slim_commodities,
        "sector_deltas":  sector_deltas,
    }
    json_path = data_dir / "bond_commodity_latest.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"    JSON: {json_path}")

    return str(md_path), str(json_path)


# ==============================================================
# BLOCK 9: ロガー簡易互換
# ==============================================================

class _SimpleLogger:
    """stock_analyzer_main の RunLogger がない環境での簡易代替"""
    def add(self, module: str, status: str, message: str):
        icon = {"success": "✅", "error": "❌", "warn": "⚠️"}.get(status, "ℹ️")
        print(f"  {icon} [{module}] {message}")


# ==============================================================
# BLOCK 10: メインエントリポイント
# ==============================================================

def run_bond_commodity_pipeline(cfg, logger=None) -> dict:
    """
    外部から呼び出すエントリポイント。
    stock_analyzer_main.py の run_all_tasks() からは以下で呼ぶ:

        _bc = _load_local_module("bond_commodity_pipeline",
                                 "/content/bond_commodity_pipeline.py",
                                 cfg.DRIVE_BASE / "bond_commodity_pipeline.py")
        if _bc:
            bc_result = _bc.run_bond_commodity_pipeline(cfg, logger)
            results["bond_commodity"] = bc_result

    戻り値:
        {
          "curve_analysis":  dict,    # イールドカーブ分析結果
          "recession_score": int,     # 0〜100
          "sector_deltas":   dict,    # JP/USスコア補正量
          "top_movers":      dict,    # 上昇/下落TOP5コモディティ
          "ok_count":        int,     # 取得成功銘柄数
          "md_path":         str,
          "json_path":       str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if logger is None:
        logger = _SimpleLogger()

    today_str  = datetime.now().strftime("%Y-%m-%d")
    fred_key   = getattr(cfg, "POLICY_FRED_API_KEY", "") or ""

    print("\n  📊 債券・コモディティパイプライン開始...")

    # ── Step 1: 債券利回り取得 ────────────────────────────────
    print("  [1/5] 債券利回り取得...")
    try:
        yields = fetch_bond_yields(fred_key)
        logger.add("bond_commodity", "success", f"債券利回り {len(yields)} 系列取得")
    except Exception as e:
        logger.add("bond_commodity", "error", f"債券取得失敗: {str(e)[:80]}")
        yields = {}

    # ── Step 2: イールドカーブ分析 ───────────────────────────
    print("  [2/5] イールドカーブ分析...")
    curve_analysis = analyze_yield_curve(yields)
    logger.add(
        "bond_commodity", "success",
        f"カーブ形状: {curve_analysis['shape_ja']} | "
        f"リセッションスコア: {curve_analysis['recession_score']}/100"
    )

    # ── Step 3: コモディティ価格取得 ─────────────────────────
    print("  [3/5] コモディティ全銘柄取得...")
    try:
        commodity_data = fetch_commodity_prices()
        ok_count = sum(1 for v in commodity_data.values() if v.get("price") is not None)
        logger.add("bond_commodity", "success", f"コモディティ {ok_count} 銘柄取得成功")
    except Exception as e:
        logger.add("bond_commodity", "error", f"コモディティ取得失敗: {str(e)[:80]}")
        commodity_data = {}
        ok_count = 0

    # ── Step 4: セクタースコア補正量算出 ─────────────────────
    print("  [4/5] セクタースコア補正量算出...")
    sector_deltas = calc_sector_deltas(commodity_data)
    jp_affected = len([v for v in sector_deltas["jp"].values() if abs(v) > 0])
    us_affected = len([v for v in sector_deltas["us"].values() if abs(v) > 0])
    logger.add(
        "bond_commodity", "success",
        f"JP補正対象: {jp_affected}業種 | US補正対象: {us_affected}セクター"
    )

# ── [U-2] FRED金利シグナル → GICSセクタースコア接続 ───────────
    try:
        rate_deltas = calc_rate_sensitivity_deltas(yields, cfg)
        if rate_deltas:
            # commodity ベースの US deltas と合算してクリップ
            for etf_sym, delta in rate_deltas.items():
                existing = sector_deltas["us"].get(etf_sym, 0.0)
                merged   = round(existing + delta, 2)
                # 最終クリップ: ±20pt（金利＋コモディティの複合効果上限）
                sector_deltas["us"][etf_sym] = max(-20.0, min(20.0, merged))
            us_affected = len([v for v in sector_deltas["us"].values() if abs(v) > 0])
            logger.add(
                "bond_commodity", "success",
                f"[U-2] 金利感応補正後 US補正対象: {us_affected}セクター",
            )
    except Exception as _u2e:
        logger.add("bond_commodity", "error", f"[U-2] 金利感応補正失敗: {str(_u2e)[:80]}")
        print(f"    ⚠️  [U-2] 金利感応補正エラー（スキップ）: {str(_u2e)[:80]}")

    # ── Step 5: レポート生成・保存 ────────────────────────────
    print("  [5/5] MDレポート・JSON生成...")
    md_report = generate_md_report(
        yields, curve_analysis, commodity_data, sector_deltas, today_str
    )
    try:
        md_path, json_path = save_outputs(
            cfg, yields, curve_analysis, commodity_data,
            sector_deltas, md_report, today_str
        )
    except Exception as e:
        logger.add("bond_commodity", "error", f"ファイル保存失敗: {str(e)[:80]}")
        md_path = json_path = ""

    # TOP movers（結果サマリー用）
    valid = [(k, v) for k, v in commodity_data.items() if v.get("ret_1m") is not None]
    top_gainers = sorted(valid, key=lambda x: x[1]["ret_1m"], reverse=True)[:5]
    top_losers  = sorted(valid, key=lambda x: x[1]["ret_1m"])[:5]
    top_movers  = {
        "gainers": [{"key": k, "name": v["name_ja"],
                     "ret_1m": v["ret_1m"]} for k, v in top_gainers],
        "losers":  [{"key": k, "name": v["name_ja"],
                     "ret_1m": v["ret_1m"]} for k, v in top_losers],
    }
    # ── [F-1] リセッションスコア → 業種 composite_score 補正 ─────────
    # sector_results は run_all_tasks() 側にあるため、
    # ここでは curve_analysis と recession_score を戻り値に含める。
    # 実際の apply_recession_score_to_sectors() 呼び出しは
    # stock_analyzer_main.py の bond_commodity 実行ブロック直後で行う。
    # （sector_results が確定した後に呼ぶ必要があるため）
    print("  ✅ 債券・コモディティパイプライン完了")

    return {
        "curve_analysis":  curve_analysis,
        "recession_score": curve_analysis.get("recession_score", 0),
        "sector_deltas":   sector_deltas,
        "top_movers":      top_movers,
        "ok_count":        ok_count,
        "md_path":         md_path,
        "json_path":       json_path,
        "rate_deltas":     rate_deltas if "rate_deltas" in dir() else {},
    }


# ==============================================================
# スタンドアロン実行（テスト用）
# ==============================================================
if __name__ == "__main__":

    class _MockCfg:
        POLICY_FRED_API_KEY = ""
        DRIVE_BASE  = Path("/tmp/bc_test")
        DATA_DIR    = Path("/tmp/bc_test/data")
        INSIGHT_DIR = Path("/tmp/bc_test/insights")

    cfg = _MockCfg()
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (cfg.INSIGHT_DIR / "macro").mkdir(parents=True, exist_ok=True)

    result = run_bond_commodity_pipeline(cfg)
    print("\n=== 結果サマリー ===")
    print(f"リセッションスコア: {result['recession_score']}/100")
    print(f"コモディティ取得: {result['ok_count']} 銘柄")
    print(f"JP補正業種: {len(result['sector_deltas']['jp'])}件")
    print(f"US補正セクター: {len(result['sector_deltas']['us'])}件")
    if result.get("top_movers", {}).get("gainers"):
        print("上昇TOP3:")
        for item in result["top_movers"]["gainers"][:3]:
            print(f"  {item['name']}: {item['ret_1m']:+.1f}%")
