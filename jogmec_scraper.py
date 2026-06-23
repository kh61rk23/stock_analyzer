# ==============================================================
# jogmec_scraper.py  v1.0  (Wave 6 - G-C1)
# ==============================================================
# 【役割】
#   JOGMEC（独立行政法人エネルギー・金属鉱物資源機構）の
#   公開権益DBをスクレイピングし、企業×国×資源の権益情報を
#   geopolitics.company_country_exposure テーブルに格納する。
#
# 【データソース】
#   JOGMEC 石油・天然ガス開発状況（公開ページ）
#     https://oilgas-info.jogmec.go.jp/
#   JOGMEC 金属資源情報（公開ページ）
#     https://mric.jogmec.go.jp/
#
# 【スクレイピング戦略】
#   JOGMEC は機械可読なAPIを提供していないため、
#   以下の段階的フォールバック構造で情報を取得する:
#
#   ① 公開PDFの静的テーブルをパース（石油・ガス開発状況表）
#      → 企業名・国名・権益種別・持分比率を抽出
#   ② JOGMECプレスリリース RSS フィード
#      → 最新の権益取得・売却情報を補完
#   ③ IMPORTANT_TICKERS_JP ベースの手動マスターデータ
#      → ①②で取得できなかった主要企業の既知権益を静的補完
#
# 【出力】
#   geopolitics.company_country_exposure テーブルへの MERGE
#   source = "jogmec" でマーキング
#
# 【実行頻度】
#   月次推奨（JOGMEC の更新頻度に合わせる）
#   stock_analyzer_main.py からは週次スキップ可（ENABLE_JOGMEC_SCRAPER フラグ）
#
# 【依存】
#   edinet_bq_client.py / config.py / country_profile_builder.py
# ==============================================================

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ==============================================================
# BLOCK 1: JOGMEC スクレイピング対象URL定数
# ==============================================================

JOGMEC_OIL_GAS_TOP = (
    "https://oilgas-info.jogmec.go.jp/info_reader/index/"
)
JOGMEC_METAL_TOP = (
    "https://mric.jogmec.go.jp/wp-content/uploads/"
)
JOGMEC_PRESS_RSS = (
    "https://www.jogmec.go.jp/rss/press.xml"
)

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
        "+https://github.com/user/stock-analyzer)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_FETCH_TIMEOUT  = 25
_MAX_RETRIES    = 3
_RETRY_WAIT_SEC = 5

# ==============================================================
# BLOCK 2: 企業名→ティッカー変換マスター（JP株）
# ==============================================================
# JOGMEC の資料には証券コードではなく企業名が記載されているため
# 正規化マッピングで ticker に変換する

COMPANY_NAME_TO_TICKER_JP: dict[str, str] = {
    # エネルギー
    "INPEX":              "1605.T",
    "国際石油開発帝石":    "1605.T",
    "ENEOS":              "5020.T",
    "ENEOSホールディングス": "5020.T",
    "出光興産":            "5019.T",
    "コスモエネルギー":    "5021.T",
    "東京ガス":            "9531.T",
    "大阪ガス":            "9532.T",
    "東邦ガス":            "9533.T",
    "石油資源開発":        "1662.T",
    # 商社
    "三菱商事":            "8058.T",
    "住友商事":            "8053.T",
    "伊藤忠商事":          "8001.T",
    "丸紅":                "8002.T",
    "三井物産":            "8031.T",
    "豊田通商":            "8015.T",
    "双日":                "2768.T",
    # 素材・資源
    "住友金属鉱山":        "5713.T",
    "三菱マテリアル":      "5711.T",
    "三井金属鉱業":        "5706.T",
    "JX金属":              "5713.T",
    "東邦亜鉛":            "5707.T",
    "DOWAホールディングス": "5714.T",
    "太平洋金属":          "5541.T",
    # 電力
    "東京電力":            "9501.T",
    "関西電力":            "9503.T",
    "中部電力":            "9502.T",
    "九州電力":            "9508.T",
    "北海道電力":          "9509.T",
}

# 国名（日本語・英語） → ISO 3166-1 alpha-3 変換マスター
COUNTRY_NAME_TO_CODE: dict[str, str] = {
    # 日本語表記
    "サウジアラビア":         "SAU",
    "アラブ首長国連邦":       "ARE",
    "UAE":                   "ARE",
    "イラン":                 "IRN",
    "イラク":                 "IRQ",
    "クウェート":             "KWT",
    "カタール":               "QAT",
    "ロシア":                 "RUS",
    "アメリカ":               "USA",
    "アメリカ合衆国":          "USA",
    "カナダ":                 "CAN",
    "ナイジェリア":            "NGA",
    "リビア":                 "LBY",
    "アルジェリア":            "DZA",
    "オーストラリア":          "AUS",
    "チリ":                   "CHL",
    "ペルー":                 "PER",
    "南アフリカ":             "ZAF",
    "コンゴ民主共和国":        "COD",
    "インドネシア":            "IDN",
    "フィリピン":             "PHL",
    "カザフスタン":            "KAZ",
    "モンゴル":               "MNG",
    "ギニア":                 "GIN",
    "ボツワナ":               "BWA",
    "ブラジル":               "BRA",
    "アルゼンチン":            "ARG",
    "ウクライナ":             "UKR",
    "タイ":                   "THA",
    "ベトナム":               "VNM",
    "インド":                 "IND",
    "中国":                   "CHN",
    "韓国":                   "KOR",
    "台湾":                   "TWN",
    "シンガポール":            "SGP",
    "マレーシア":             "MYS",
    "エジプト":               "EGY",
    "トルコ":                 "TUR",
    "イスラエル":             "ISR",
    "パキスタン":             "PAK",
    "ドイツ":                 "DEU",
    "フランス":               "FRA",
    "イギリス":               "GBR",
    "英国":                   "GBR",
    "オランダ":               "NLD",
    "ポーランド":             "POL",
    "メキシコ":               "MEX",
    "ザンビア":               "ZMB",
    "モザンビーク":            "MOZ",
    "パプアニューギニア":      "PNG",
    "ウズベキスタン":          "UZB",
    "ミャンマー":             "MMR",
    "カンボジア":             "KHM",
    "ラオス":                 "LAO",
    "バングラデシュ":          "BGD",
    "コロンビア":             "COL",
    "ベネズエラ":             "VEN",
    "エチオピア":             "ETH",
    "タンザニア":             "TZA",
    "ジンバブエ":             "ZWE",
    # 英語表記（プレスリリース向け）
    # ※ "UAE" は日本語セクションで定義済み（同値 "ARE"）のため重複削除（2026-06-11）
    "Saudi Arabia":          "SAU",
    "Iran":                  "IRN",
    "Iraq":                  "IRQ",
    "Kuwait":                "KWT",
    "Qatar":                 "QAT",
    "Russia":                "RUS",
    "United States":         "USA",
    "Canada":                "CAN",
    "Nigeria":               "NGA",
    "Libya":                 "LBY",
    "Algeria":               "DZA",
    "Australia":             "AUS",
    "Chile":                 "CHL",
    "Peru":                  "PER",
    "South Africa":          "ZAF",
    "Indonesia":             "IDN",
    "Philippines":           "PHL",
    "Kazakhstan":            "KAZ",
    "Mongolia":              "MNG",
    "Guinea":                "GIN",
    "Botswana":              "BWA",
    "Brazil":                "BRA",
    "Argentina":             "ARG",
    "Ukraine":               "UKR",
    "Thailand":              "THA",
    "Vietnam":               "VNM",
    "India":                 "IND",
    "China":                 "CHN",
    "Malaysia":              "MYS",
    "Egypt":                 "EGY",
    "Turkey":                "TUR",
    "Mozambique":            "MOZ",
    "Papua New Guinea":      "PNG",
    "Zambia":                "ZMB",
    "Colombia":              "COL",
    "Venezuela":             "VEN",
    "Myanmar":               "MMR",
}

# ==============================================================
# BLOCK 3: 手動マスターデータ（静的補完）
# ==============================================================
# JOGMEC公開情報・各社IRページから整備した既知権益の静的マスター。
# ①②のスクレイピングで取得できない場合のフォールバックとして使用。
# source = "jogmec_manual" でマーキングされる。
#
# 各エントリ:
#   ticker          : yfinance ティッカー
#   country_code    : ISO 3166-1 alpha-3
#   exposure_type   : SPEC_db_schema 3-3 の7種類
#   exposure_strength: 0〜1（売上比率・持分比率等の推定値）
#   commodity_code  : 関連資源コード（GEO_MONITORED_COMMODITIES 参照）
#   rationale       : 判定根拠1行

MANUAL_EXPOSURE_MASTER: list[dict] = [
    # ── INPEX（石油・ガス権益最大手）────────────────────────────
    {"ticker": "1605.T", "country_code": "AUS", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.45, "commodity_code": "lng",
     "rationale": "イクシス LNG プロジェクト（オーストラリア）主要持分保有"},
    {"ticker": "1605.T", "country_code": "ARE", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.12, "commodity_code": "crude_oil",
     "rationale": "ADNOC（アブダビ国営石油）権益参加"},
    {"ticker": "1605.T", "country_code": "KAZ", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.08, "commodity_code": "crude_oil",
     "rationale": "カシャガン油田（カザフスタン）権益参加"},
    {"ticker": "1605.T", "country_code": "IDN", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.10, "commodity_code": "lng",
     "rationale": "マセラ LNG（インドネシア）開発権益"},
    # ── ENEOS（石油精製・権益）──────────────────────────────────
    {"ticker": "5020.T", "country_code": "ARE", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.08, "commodity_code": "crude_oil",
     "rationale": "アブダビ上流権益（ADNOC）"},
    {"ticker": "5020.T", "country_code": "SAU", "exposure_type": "PROCUREMENT",
     "exposure_strength": 0.30, "commodity_code": "crude_oil",
     "rationale": "サウジアラビアからの原油調達依存度（推定）"},
    # ── 出光興産 ─────────────────────────────────────────────────
    {"ticker": "5019.T", "country_code": "AUS", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.15, "commodity_code": "crude_oil",
     "rationale": "西豪州石油権益（INPEX 共同）"},
    {"ticker": "5019.T", "country_code": "SAU", "exposure_type": "PROCUREMENT",
     "exposure_strength": 0.25, "commodity_code": "crude_oil",
     "rationale": "サウジアラミコとの長期調達契約"},
    # ── 三菱商事 ─────────────────────────────────────────────────
    {"ticker": "8058.T", "country_code": "AUS", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.18, "commodity_code": "coal",
     "rationale": "豪州 BMA（BHP との合弁）原料炭権益"},
    {"ticker": "8058.T", "country_code": "CHL", "exposure_type": "EQUITY_STAKE",
     "exposure_strength": 0.20, "commodity_code": "copper",
     "rationale": "Anglo American Sur 銅鉱山持分保有"},
    {"ticker": "8058.T", "country_code": "BRA", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.12, "commodity_code": "iron_ore",
     "rationale": "Vale との鉄鉱石権益・長期供給契約"},
    {"ticker": "8058.T", "country_code": "IDN", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.10, "commodity_code": "coal",
     "rationale": "インドネシア炭鉱権益（PT Adaro 等）"},
    # ── 三井物産 ─────────────────────────────────────────────────
    {"ticker": "8031.T", "country_code": "AUS", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.20, "commodity_code": "lng",
     "rationale": "ノース・ウェスト・シェルフ LNG（豪州）権益"},
    {"ticker": "8031.T", "country_code": "SAU", "exposure_type": "JOINT_VENTURE",
     "exposure_strength": 0.10, "commodity_code": "crude_oil",
     "rationale": "サウジアラビアでの石化合弁（PETRORABiG 等）"},
    {"ticker": "8031.T", "country_code": "MOZ", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.15, "commodity_code": "lng",
     "rationale": "モザンビーク・コラル FLNG プロジェクト権益"},
    # ── 住友商事 ─────────────────────────────────────────────────
    {"ticker": "8053.T", "country_code": "COD", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.15, "commodity_code": "cobalt",
     "rationale": "コンゴ民主共和国コバルト・銅鉱山権益（Boss Mining 等）"},
    {"ticker": "8053.T", "country_code": "AUS", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.10, "commodity_code": "coal",
     "rationale": "豪州炭鉱権益（Meadowbank 等）"},
    # ── 伊藤忠商事 ────────────────────────────────────────────────
    {"ticker": "8001.T", "country_code": "CHL", "exposure_type": "EQUITY_STAKE",
     "exposure_strength": 0.18, "commodity_code": "copper",
     "rationale": "Compañia Minera del Pacífico 銅権益持分"},
    {"ticker": "8001.T", "country_code": "IDN", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.08, "commodity_code": "coal",
     "rationale": "インドネシア炭鉱権益"},
    # ── 丸紅 ───────────────────────────────────────────────────────
    {"ticker": "8002.T", "country_code": "AUS", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.14, "commodity_code": "coal",
     "rationale": "豪州 QCoal 炭鉱権益"},
    {"ticker": "8002.T", "country_code": "BRA", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.10, "commodity_code": "iron_ore",
     "rationale": "ブラジル鉄鉱石権益・輸送権益"},
    # ── 住友金属鉱山 ─────────────────────────────────────────────
    {"ticker": "5713.T", "country_code": "PHL", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.30, "commodity_code": "nickel",
     "rationale": "Coral Bay Nickel（フィリピン）持分保有・ニッケル権益"},
    {"ticker": "5713.T", "country_code": "CHL", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.20, "commodity_code": "copper",
     "rationale": "Sierra Gorda 銅鉱山（チリ）持分31.5%"},
    {"ticker": "5713.T", "country_code": "COD", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.10, "commodity_code": "cobalt",
     "rationale": "DRCコバルト権益（Sierra Gorda 副産物を含む）"},
    # ── 三菱マテリアル ───────────────────────────────────────────
    {"ticker": "5711.T", "country_code": "CAN", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.15, "commodity_code": "copper",
     "rationale": "カナダ銅鉱山権益（Oyu Tolgoi 等への間接参加）"},
    {"ticker": "5711.T", "country_code": "AUS", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.12, "commodity_code": "copper",
     "rationale": "豪州銅金鉱山権益"},
    # ── 三井金属鉱業 ─────────────────────────────────────────────
    {"ticker": "5706.T", "country_code": "CHL", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.20, "commodity_code": "copper",
     "rationale": "チリ銅権益（Caserones 銅鉱山）持分保有"},
    {"ticker": "5706.T", "country_code": "PER", "exposure_type": "RESOURCE_CONCESSION",
     "exposure_strength": 0.12, "commodity_code": "zinc",
     "rationale": "ペルー亜鉛権益"},
    # ── トヨタ自動車 ─────────────────────────────────────────────
    {"ticker": "7203.T", "country_code": "CHN", "exposure_type": "PRODUCTION_FACILITY",
     "exposure_strength": 0.22, "commodity_code": None,
     "rationale": "中国における現地生産・合弁会社（一汽豊田・広汽豊田）"},
    {"ticker": "7203.T", "country_code": "USA", "exposure_type": "PRODUCTION_FACILITY",
     "exposure_strength": 0.18, "commodity_code": None,
     "rationale": "米国ケンタッキー・テキサス工場（TMMK 等）"},
    # ── ソニーグループ ───────────────────────────────────────────
    {"ticker": "6758.T", "country_code": "CHN", "exposure_type": "PRODUCTION_FACILITY",
     "exposure_strength": 0.15, "commodity_code": None,
     "rationale": "中国における電子部品・カメラセンサー生産拠点"},
    {"ticker": "6758.T", "country_code": "USA", "exposure_type": "SALES_NETWORK",
     "exposure_strength": 0.30, "commodity_code": None,
     "rationale": "北米エンタメ・ゲーム（PlayStation）売上依存度"},
]

# ==============================================================
# BLOCK 4: HTTPユーティリティ
# ==============================================================

def _fetch_url(url: str, timeout: int = _FETCH_TIMEOUT) -> str | None:
    """URL を取得してテキスト文字列を返す。失敗時は None。"""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = Request(url, headers=_REQUEST_HEADERS)
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("shift_jis", errors="replace")
        except (HTTPError, URLError) as e:
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_WAIT_SEC * attempt)
            else:
                print(f"    ⚠️  fetch失敗 ({url[:60]}): {e}")
                return None
        except Exception as e:
            print(f"    ⚠️  fetch例外 ({url[:60]}): {e}")
            return None
    return None


# ==============================================================
# BLOCK 5: JOGMEC プレスリリース RSS パーサー
# ==============================================================

def _parse_jogmec_press_rss(xml_text: str) -> list[dict]:
    """
    JOGMEC プレスリリース RSS から権益取得・売却情報を抽出する。

    抽出パターン:
      - 「〜権益を取得」「〜プロジェクトへの参加」
      - 「〜鉱山の持分」「〜開発に参加」

    Returns:
        list of {ticker, country_code, exposure_type,
                 exposure_strength, commodity_code, rationale}
    """
    results: list[dict] = []
    if not xml_text:
        return results

    items = re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)

    for item in items:
        title_m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        desc_m  = re.search(r"<description>(.*?)</description>", item, re.DOTALL)

        title = title_m.group(1) if title_m else ""
        desc  = desc_m.group(1) if desc_m else ""
        text  = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title + " " + desc)
        text  = re.sub(r"<[^>]+>", "", text)  # HTMLタグ除去

        # 国名を抽出
        found_country = None
        for name, code in COUNTRY_NAME_TO_CODE.items():
            if name in text:
                found_country = code
                break
        if not found_country:
            continue

        # 企業名を抽出
        found_ticker = None
        for name, ticker in COMPANY_NAME_TO_TICKER_JP.items():
            if name in text:
                found_ticker = ticker
                break
        if not found_ticker:
            continue

        # 資源種別を推定
        commodity = None
        for kw, code in [
            ("石油", "crude_oil"), ("原油", "crude_oil"),
            ("LNG", "lng"), ("天然ガス", "natural_gas"),
            ("石炭", "coal"), ("鉄鉱石", "iron_ore"),
            ("銅", "copper"), ("リチウム", "lithium"),
            ("コバルト", "cobalt"), ("ニッケル", "nickel"),
            ("レアアース", "rare_earth"), ("金", "gold"),
            ("ウラン", "uranium"), ("アルミ", "aluminum"),
        ]:
            if kw in text:
                commodity = code
                break

        # exposure_type を推定
        if re.search(r"権益.*?取得|開発.*?参加|持分.*?取得", text):
            exp_type = "RESOURCE_CONCESSION"
        elif re.search(r"合弁|JV|ジョイントベンチャー", text):
            exp_type = "JOINT_VENTURE"
        elif re.search(r"出資|株式取得|持分譲受", text):
            exp_type = "EQUITY_STAKE"
        else:
            exp_type = "RESOURCE_CONCESSION"

        results.append({
            "ticker":            found_ticker,
            "country_code":      found_country,
            "exposure_type":     exp_type,
            "exposure_strength": 0.10,   # RSSからは強度不明のため保守的に0.10
            "commodity_code":    commodity,
            "rationale":         text[:120].strip(),
            "source":            "jogmec_rss",
        })

    return results


# ==============================================================
# BLOCK 6: BQ MERGE クエリ生成
# ==============================================================

def _build_exposure_merge_query(
    project_id: str,
    rows: list[dict],
) -> str:
    """
    geopolitics.company_country_exposure への MERGE クエリを生成する。
    (ticker, market, country_code, exposure_type, commodity_code) を
    複合キーとして UPSERT する。

    exposure_id は UUID で新規生成（WHEN NOT MATCHED 時のみ使用）。
    company_id は taxonomy.company の company_code = ticker で解決する
    が、Phase G では簡略化のため ticker をそのまま company_id 代わりに
    格納する（taxonomy.company との JOIN は analytics 側で行う）。
    """
    if not rows:
        return ""

    value_rows = []
    for r in rows:
        def _s(v):
            if v is None:
                return "NULL"
            return "'" + str(v).replace("'", "\\'") + "'"

        def _f(v):
            if v is None:
                return "NULL"
            return str(float(v))

        def _b(v):
            if v is None:
                return "FALSE"
            return "TRUE" if v else "FALSE"

        exp_id = "'" + str(uuid.uuid4()) + "'"
        value_rows.append(
            f"  ({exp_id}, "
            f"{_s(r.get('ticker', ''))}, "
            f"{_s(r.get('ticker', ''))}, "   # company_id = ticker（暫定）
            f"{_s(r.get('market', 'JP'))}, "
            f"{_s(r.get('country_code', ''))}, "
            f"{_s(r.get('exposure_type', 'RESOURCE_CONCESSION'))}, "
            f"{_f(r.get('exposure_strength', 0.10))}, "
            f"{_s(r.get('commodity_code'))}, "
            f"{_f(r.get('revenue_share_est'))}, "
            f"{_f(r.get('employee_share_est'))}, "
            f"{_f(r.get('asset_share_est'))}, "
            f"{_s(r.get('source', 'jogmec'))}, "
            f"{_s(r.get('evidence_url'))}, "
            f"{_s(r.get('rationale', ''))}, "
            f"TRUE, "           # auto_derived
            f"FALSE, "          # verified_flag
            f"{_b(r.get('review_flag', False))}, "
            f"DATE('{r.get('valid_from', date.today().isoformat())}'), "
            f"NULL, "           # valid_to
            f"CURRENT_TIMESTAMP())"
        )

    values_sql = ",\n".join(value_rows)

    return f"""
MERGE `{project_id}.geopolitics.company_country_exposure` AS T
USING (
  SELECT * FROM UNNEST([
    STRUCT
      exposure_id STRING,
      ticker STRING,
      company_id STRING,
      market STRING,
      country_code STRING,
      exposure_type STRING,
      exposure_strength FLOAT64,
      commodity_code STRING,
      revenue_share_est FLOAT64,
      employee_share_est FLOAT64,
      asset_share_est FLOAT64,
      source STRING,
      evidence_url STRING,
      rationale STRING,
      auto_derived BOOL,
      verified_flag BOOL,
      review_flag BOOL,
      valid_from DATE,
      valid_to DATE,
      updated_at TIMESTAMP
    >
{values_sql}
  ])
) AS S
ON  T.ticker        = S.ticker
AND T.country_code  = S.country_code
AND T.exposure_type = S.exposure_type
AND IFNULL(T.commodity_code, '') = IFNULL(S.commodity_code, '')
WHEN MATCHED AND T.verified_flag = FALSE THEN
  UPDATE SET
    T.exposure_strength   = S.exposure_strength,
    T.revenue_share_est   = S.revenue_share_est,
    T.employee_share_est  = S.employee_share_est,
    T.asset_share_est     = S.asset_share_est,
    T.source              = S.source,
    T.evidence_url        = S.evidence_url,
    T.rationale           = S.rationale,
    T.auto_derived        = S.auto_derived,
    T.review_flag         = S.review_flag,
    T.updated_at          = S.updated_at
WHEN NOT MATCHED THEN
  INSERT (
    exposure_id, ticker, company_id, market, country_code,
    exposure_type, exposure_strength, commodity_code,
    revenue_share_est, employee_share_est, asset_share_est,
    source, evidence_url, rationale,
    auto_derived, verified_flag, review_flag,
    valid_from, valid_to, updated_at
  )
  VALUES (
    S.exposure_id, S.ticker, S.company_id, S.market, S.country_code,
    S.exposure_type, S.exposure_strength, S.commodity_code,
    S.revenue_share_est, S.employee_share_est, S.asset_share_est,
    S.source, S.evidence_url, S.rationale,
    S.auto_derived, S.verified_flag, S.review_flag,
    S.valid_from, S.valid_to, S.updated_at
  )
"""


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_jogmec_scraper(
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
    use_manual_master: bool = True,
    use_rss: bool = True,
    dry_run: bool = False,
    target_tickers: list[str] | None = None,
) -> dict:
    """
    JOGMEC 権益情報を収集し BQ に MERGE する。

    処理フロー:
      ① 手動マスターデータ（MANUAL_EXPOSURE_MASTER）を常にベースとして使用
      ② JOGMEC プレスリリース RSS から差分情報を追加
      ③ 重複排除（同一 ticker × country_code × exposure_type × commodity_code）
      ④ BQ MERGE 実行

    Args:
        cfg              : config モジュール
        project_id       : GCPプロジェクトID
        use_manual_master: 手動マスターを使用するか
        use_rss          : JOGMEC RSS をスクレイピングするか
        dry_run          : True の場合 BQ 実行をスキップ
        target_tickers   : 指定した場合は対象ティッカーのみ処理

    Returns:
        {
          "success":    bool,
          "total":      int,
          "merged":     int,
          "sources":    list[str],
          "records":    list[dict],
          "error":      str,
          "timestamp":  str,
        }
    """
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
    print(f"\n⛏️  jogmec_scraper: 権益情報収集開始")
    print(f"   プロジェクト: {_pid}")

    all_records: list[dict] = []
    sources_used: list[str] = []

    # ── ① 手動マスターデータ ─────────────────────────────────
    if use_manual_master:
        manual = [
            dict(r, market="JP",
                 source=r.get("source", "jogmec_manual"),
                 valid_from=date.today().isoformat(),
                 review_flag=False)
            for r in MANUAL_EXPOSURE_MASTER
        ]
        if target_tickers:
            manual = [r for r in manual if r["ticker"] in target_tickers]
        all_records.extend(manual)
        sources_used.append("jogmec_manual")
        print(f"   📋 手動マスター: {len(manual)} 件ロード")

    # ── ② JOGMEC プレスリリース RSS ──────────────────────────
    if use_rss:
        print("   📡 JOGMEC RSS 取得中...")
        try:
            rss_text = _fetch_url(JOGMEC_PRESS_RSS)
            if rss_text:
                rss_records = _parse_jogmec_press_rss(rss_text)
                if target_tickers:
                    rss_records = [
                        r for r in rss_records
                        if r.get("ticker") in target_tickers
                    ]
                # 手動マスターに存在しないレコードのみ追加
                existing_keys = {
                    (r["ticker"], r["country_code"],
                     r["exposure_type"], r.get("commodity_code"))
                    for r in all_records
                }
                new_rss = []
                for r in rss_records:
                    key = (r.get("ticker"), r.get("country_code"),
                           r.get("exposure_type"), r.get("commodity_code"))
                    if key not in existing_keys:
                        r["market"] = "JP"
                        r["valid_from"] = date.today().isoformat()
                        r["review_flag"] = True  # RSS由来は要確認フラグ
                        new_rss.append(r)
                        existing_keys.add(key)

                all_records.extend(new_rss)
                sources_used.append("jogmec_rss")
                print(f"   ✅ JOGMEC RSS: {len(new_rss)} 件の新規権益情報取得")
            else:
                print("   ⚠️  JOGMEC RSS: 取得失敗（手動マスターのみで続行）")
        except Exception as e:
            print(f"   ⚠️  JOGMEC RSS 処理エラー: {str(e)[:80]}")

    if not all_records:
        return {
            "success":   False,
            "total":     0,
            "merged":    0,
            "sources":   sources_used,
            "records":   [],
            "error":     "取得レコードが0件",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    print(f"   📊 統計:")
    print(f"      総レコード数: {len(all_records)} 件")
    by_country = {}
    for r in all_records:
        cc = r.get("country_code", "UNK")
        by_country[cc] = by_country.get(cc, 0) + 1
    top_countries = sorted(by_country.items(), key=lambda x: -x[1])[:5]
    print(f"      上位国: {top_countries}")

    if dry_run:
        print(f"   [DRY RUN] BQ実行スキップ")
        return {
            "success":   True,
            "total":     len(all_records),
            "merged":    0,
            "sources":   sources_used,
            "records":   all_records,
            "error":     "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── BQ MERGE 実行 ─────────────────────────────────────────
    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _bq_mod = importlib.import_module("edinet_bq_client")
        _bq     = _bq_mod.EDINETBQClient()
        raw_bq  = _bq.client
    except Exception:
        try:
            from google.cloud import bigquery as _bq_lib
            from google.colab import auth as _auth
            _auth.authenticate_user()
            raw_bq = _bq_lib.Client(
                project=_pid, location="asia-northeast1"
            )
        except Exception as e:
            return {
                "success":   False,
                "total":     len(all_records),
                "merged":    0,
                "sources":   sources_used,
                "records":   all_records,
                "error":     f"BQ Client 取得失敗: {e}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    try:
        # 500件ずつバッチ分割して MERGE（BQ クエリサイズ制限対策）
        BATCH_SIZE = 500
        merged_total = 0
        for i in range(0, len(all_records), BATCH_SIZE):
            batch = all_records[i:i + BATCH_SIZE]
            merge_sql = _build_exposure_merge_query(_pid, batch)
            job = raw_bq.query(merge_sql)
            job.result()
            merged_total += len(batch)
            print(f"   MERGE進捗: {merged_total}/{len(all_records)} 件")

        print(f"   ✅ BQ MERGE 完了: {merged_total} 件")
        return {
            "success":   True,
            "total":     len(all_records),
            "merged":    merged_total,
            "sources":   sources_used,
            "records":   all_records,
            "error":     "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        err = str(e)
        print(f"   ❌ BQ MERGE 失敗: {err[:200]}")
        return {
            "success":   False,
            "total":     len(all_records),
            "merged":    0,
            "sources":   sources_used,
            "records":   all_records,
            "error":     err[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ==============================================================
# BLOCK 8: 参照ユーティリティ（geo_signal.py 等から呼ばれる）
# ==============================================================

def get_exposure_by_ticker(
    ticker: str,
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
) -> list[dict]:
    """
    指定ティッカーの全権益エクスポージャーを BQ から取得する。
    geo_signal.py の geo_impact_bonus 計算に使用。

    Returns:
        list of {country_code, exposure_type, exposure_strength,
                 commodity_code, composite_risk_score}
        取得失敗時は空リスト
    """
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _bq_mod = importlib.import_module("edinet_bq_client")
        raw_bq  = _bq_mod.EDINETBQClient().client

        query = f"""
        SELECT
          e.country_code,
          e.exposure_type,
          e.exposure_strength,
          e.commodity_code,
          IFNULL(r.composite_risk_score, 0.0) AS composite_risk_score
        FROM `{_pid}.geopolitics.company_country_exposure` AS e
        LEFT JOIN (
          SELECT country_code, composite_risk_score
          FROM (
            SELECT country_code, composite_risk_score,
                   ROW_NUMBER() OVER (
                     PARTITION BY country_code ORDER BY risk_date DESC
                   ) AS rn
            FROM `{_pid}.geopolitics.country_risk_daily`
          )
          WHERE rn = 1
        ) AS r ON e.country_code = r.country_code
        WHERE e.ticker = '{ticker}'
          AND (e.valid_to IS NULL OR e.valid_to > CURRENT_DATE())
        ORDER BY e.exposure_strength DESC
        """
        rows = list(raw_bq.query(query).result())
        return [
            {
                "country_code":        row.country_code,
                "exposure_type":       row.exposure_type,
                "exposure_strength":   float(row.exposure_strength or 0.0),
                "commodity_code":      row.commodity_code,
                "composite_risk_score": float(row.composite_risk_score or 0.0),
            }
            for row in rows
        ]
    except Exception as e:
        print(f"  ⚠️  get_exposure_by_ticker [{ticker}]: {str(e)[:80]}")
        return []


# ==============================================================
# 単体テスト / Colab実行エントリ
# ==============================================================

if __name__ == "__main__":
    import sys

    dry_run_flag = "--dry-run" in sys.argv
    target_flag  = None
    for i, arg in enumerate(sys.argv):
        if arg == "--target" and i + 1 < len(sys.argv):
            target_flag = sys.argv[i + 1].split(",")

    print("=" * 60)
    print("jogmec_scraper.py v1.0")
    print("=" * 60)

    result = run_jogmec_scraper(
        cfg             = None,
        project_id      = "stock-analyzer-edinet",
        use_manual_master = True,
        use_rss         = True,
        dry_run         = dry_run_flag,
        target_tickers  = target_flag,
    )

    print(f"\n{'✅ 完了' if result['success'] else '❌ 失敗'}")
    print(f"   総レコード数: {result['total']}")
    print(f"   MERGE件数:   {result['merged']}")
    print(f"   ソース:       {result['sources']}")
    if result["error"]:
        print(f"   エラー: {result['error']}")