# ==============================================================
# data_bridge.py  —  データ通信・キャッシュ管理層
# 依存: finance_logic（純計算）, yfinance, gspread, streamlit, requests
# 役割: yfinance / EDINET DB / Alpha Vantage との通信、
#       ディスクキャッシュ管理、Google Drive パス管理、Google Sheets 連携
#
# ★ Google Drive パスはこのファイルで一元定義し全モジュールで使用
# ★ EDINET DB 連携 (edinetdb.jp) —— 日本株は財務系指標を優先採用
#
# データソース優先順位:
#   米国株: yfinance → Alpha Vantage → ディスクキャッシュ
#   日本株: yfinance（株価・チャート）+
#           EDINET DB（財務指標・セクター等）→ マージ後キャッシュ
# ==============================================================

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import gspread
import streamlit as st
from google.oauth2.service_account import Credentials

# yfinance は実行内キャッシュ（yf_cache）経由で使う。
# base_agent.fetch_financials() は本モジュールの fetch_stock_data() を
# **最優先の取得経路**として銘柄ごとに呼ぶため、ここが素の yfinance だと
# 同一銘柄の .info / history を各モジュールが取り直すことになる。
# yf_cache が無い環境（viewer 単体起動など）では素の yfinance に落ちる。
try:
    import yf_cache as yf
except ImportError:  # pragma: no cover - viewer 単体起動時のみ
    import yfinance as yf

# ★ 日次バッチ（GitHub Actions・streamlitランタイム無し）から本モジュールが
#   importされると @st.cache_data 呼出のたびに「No runtime found」WARNINGが大量出力される。
#   streamlit自身のimportでloggerが再構成されるため、import後にここで抑制する
#   （main側で事前に設定しても効かない）。UI実行時の動作には影響しない。
import logging as _logging
_logging.getLogger("streamlit.runtime.caching.cache_data_api").setLevel(_logging.ERROR)

# 純計算ロジックを import（循環なし）
from finance_logic import (
    _safe_float, _safe_int, _latest,
    is_japan_stock, compute_div_yield, fix_div_yield,
)


# ==============================================================
# SECTION 1: パス・設定値（全モジュール共通の一元定義）
# ==============================================================

# ★ Google Drive 格納フォルダ（Colab 環境）— ここで一元管理
DRIVE_DIR = Path("/content/drive/MyDrive/stock_viewer")

# Streamlit アプリ側キャッシュ（メモリ解放後はディスクへ）
CACHE_DIR = Path("/tmp/stockview_cache")

# キャッシュ有効期間（秒）
CACHE_TTL_SECONDS = 7200   # 2時間

# デフォルトスプレッドシートID
DEFAULT_SPREADSHEET_ID = "1CePiavYj_6OR0cOz84V4RkA0c0S6IwZlcHCv8_bsOjc"

# ngrok 固定ドメイン（情報表示用）
NGROK_DOMAIN = "joey-unsatisfiable-gluily.ngrok-free.dev"

# Alpha Vantage API キーの環境変数名
AV_API_KEY_ENV = "ALPHAVANTAGE_API_KEY"

# ── EDINET DB 設定 ─────────────────────────────────────────────
# edinetdb.jp — 全上場企業3848社の財務データ REST API（毎日8:00 JST更新）
# API キーはコードに直書きせず、他モジュールと同じく環境変数（GitHub Secrets → run_headless.py の ENV_MAP）から読む。
EDINET_DB_API_KEY = os.getenv("EDINETDB_API_KEY", "")
EDINET_DB_BASE    = "https://edinetdb.jp/v1"
EDINET_DB_TIMEOUT = 10   # リクエストタイムアウト（秒）

# EDINET コード解決の in-memory キャッシュ（セッション中は再利用）
# { "7203": "E02144", "6758": "E01777", ... }
_edinet_code_cache: dict = {}

# service_account.json 検索優先順位: Drive 優先
SA_FILE_CANDIDATES = [
    DRIVE_DIR / "service_account.json",        # ★ Drive 優先（毎回アップロード不要）
    Path("/content/service_account.json"),      # Colab にアップロードした場合
    Path("service_account.json"),               # ローカル実行
    Path("/content/credentials.json"),
    Path("credentials.json"),
]

# Google Sheets スコープ
SHEET_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
WS_WATCHLIST = "watchlist"
WS_SETTINGS  = "settings"
TICKER_COL   = "ticker"
PURCHASE_COL = "Purchase_Price"   # スプレッドシート D 列ヘッダー名

# ── 数値定数（マジックナンバー排除）──────────────────────────────
PRICE_ANOMALY_THRESHOLD = 0.30     # 前日比30%超を異常値として検出
PCT_BOUNDARY            = 1.5      # EDINET % 判定境界（この値超 = %表記）
USDJPY_DEFAULT          = 150.0    # USD/JPY 取得失敗時のデフォルトレート

# ── EDINET DB データバージョン（ソート修正後に変更 → 古いキャッシュを自動無効化）──
EDINET_CACHE_VERSION = "v2"   # このバージョンと一致しないキャッシュは再取得対象

# ── 起動時ディレクトリ自動作成 ─────────────────────────────────
CACHE_DIR.mkdir(parents=True, exist_ok=True)
try:
    if DRIVE_DIR.parent.exists():   # Drive マウント済みの場合のみ
        DRIVE_DIR.mkdir(parents=True, exist_ok=True)
        (DRIVE_DIR / "cache").mkdir(parents=True, exist_ok=True)
except Exception:
    pass


# ==============================================================
# SECTION 2: ディスクキャッシュ（L2）
# ==============================================================

def _cache_path(ticker: str) -> Path:
    """ティッカーから安全なキャッシュファイルパスを生成"""
    safe = ticker.replace(".", "_").replace("/", "_")
    return CACHE_DIR / f"{safe}.json"


def _read_cache(ticker: str) -> Optional[dict]:
    """TTL 内のキャッシュを読み込む。期限切れ or 存在しない場合は None"""
    p = _cache_path(ticker)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            data = json.load(f)
        # バージョン不一致 → 古いキャッシュとして即時廃棄（ソート修正前データを排除）
        if data.get("_edinet_cache_ver") != EDINET_CACHE_VERSION:
            return None
        if time.time() - data.get("_cached_at", 0) < CACHE_TTL_SECONDS:
            data["_from_cache"] = True
            return data
        return None
    except Exception:
        return None


def _write_cache(ticker: str, data: dict):
    """データをディスクへ書き込む。history キー（大型データ）は除外"""
    p = _cache_path(ticker)
    try:
        to_save = {k: v for k, v in data.items() if k != "history"}
        to_save["_cached_at"]       = time.time()
        to_save["_edinet_cache_ver"] = EDINET_CACHE_VERSION
        with open(p, "w") as f:
            json.dump(to_save, f, ensure_ascii=False, default=str)
    except Exception:
        pass


def _read_stale_cache(ticker: str) -> Optional[dict]:
    """TTL 切れでも存在するキャッシュを強制読み込み（最終フォールバック）"""
    p = _cache_path(ticker)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            data = json.load(f)
        data["_from_cache"] = True
        data["_stale"]      = True
        return data
    except Exception:
        return None


# ==============================================================
# SECTION 3: Alpha Vantage フォールバック
# ==============================================================

def _fetch_alpha_vantage(ticker: str) -> Optional[dict]:
    """
    Alpha Vantage OVERVIEW エンドポイントから財務概要を取得する。
    yfinance が失敗した場合のフォールバックとして使用。
    ALPHAVANTAGE_API_KEY 環境変数が未設定の場合はスキップ（None を返す）。
    """
    import requests
    api_key = os.environ.get(AV_API_KEY_ENV, "")
    if not api_key:
        return None
    try:
        r = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "OVERVIEW",
                "symbol":   ticker.replace(".T", ""),
                "apikey":   api_key,
            },
            timeout=8,
        )
        d = r.json()
        # レート制限到達時は "Information" または "Note" キーが返る
        if "Information" in d or "Note" in d:
            return None
        if "Symbol" not in d:
            return None
        return {
            "ticker":          ticker,
            "name":            d.get("Name", ticker),
            "exchange":        d.get("Exchange", ""),
            "currency":        d.get("Currency", "USD"),
            "curr_symbol":     "$",
            "sector":          d.get("Sector", "―"),
            "industry":        d.get("Industry", "―"),
            "description":     d.get("Description", ""),
            "employees":       _safe_int(d.get("FullTimeEmployees")),
            "country":         d.get("Country", "―"),
            "website":         d.get("OfficialSite", ""),
            "price":           None,
            "per":             _safe_float(d.get("TrailingPE")),
            "pbr":             _safe_float(d.get("PriceToBookRatio")),
            "div_yield":       fix_div_yield(d.get("DividendYield")),  # AV は常に小数
            "annual_dividend": None,
            "market_cap":      _safe_int(d.get("MarketCapitalization")),
            "roe":             _safe_float(d.get("ReturnOnEquityTTM")),
            "roe_source":      "alphavantage",
            "roa":             _safe_float(d.get("ReturnOnAssetsTTM")),
            "payout_ratio":    _safe_float(d.get("PayoutRatio")),
            "cf_ni_ratio":     None,
            "history":         None,
            "_source":         "alphavantage",
            "fetched_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "is_anomaly":      False,
            "error":           None,
        }
    except Exception:
        return None


# ==============================================================
# SECTION 3.5: EDINET DB API 連携（日本株専用）
# ==============================================================
#
# edinetdb.jp — 金融庁EDINETの有価証券報告書を毎日自動取得し、
# 名寄せ・構造化済みの財務データを REST API で提供するサービス。
# JP GAAP / IFRS / US GAAP の差異を吸収し、全企業を同一指標で比較可能。
#
# 認証: リクエストヘッダー "X-API-Key: {EDINET_DB_API_KEY}"
# 制限: β版 1,000回/日（正式リリース後は Free 100回/日）
#
# 使用エンドポイント（計3種、1銘柄あたり最大3リクエスト）:
#   GET /v1/search?q={sec_code_4digit}   → EDINETコード解決（APIキー不要）
#   GET /v1/companies/{edinet_code}      → 企業基本情報（セクター等）
#   GET /v1/companies/{edinet_code}/financials → 財務時系列（P/L・B/S・CF）
#   GET /v1/companies/{edinet_code}/ratios    → 財務比率（ROE・ROA等）
# ==============================================================

def _resolve_edinet_code(sec_code_4digit: str) -> Optional[str]:
    """
    4桁の証券コード（例: "7203"）から EDINET コード（例: "E02144"）を解決する。
    結果はモジュールレベルの _edinet_code_cache に保存し、再取得を抑制する。

    EDINET DB の /v1/search は APIキー不要。
    sec_code フィールドは EDINET DB 内では5桁（4桁 + 末尾0）で管理されるため、
    前方一致で比較する。
    """
    import requests

    cached = _edinet_code_cache.get(sec_code_4digit)
    if cached:
        return cached

    try:
        r = requests.get(
            f"{EDINET_DB_BASE}/search",
            params={"q": sec_code_4digit},
            timeout=EDINET_DB_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        results = r.json().get("data", [])
        for item in results:
            sc = str(item.get("sec_code", ""))
            # EDINET DB は 5桁 sec_code（例: "72030"）で返す
            # 前方4桁が入力と一致するエントリを採用
            if sc.startswith(sec_code_4digit):
                edinet_code = item.get("edinet_code")
                if edinet_code:
                    _edinet_code_cache[sec_code_4digit] = edinet_code
                    return edinet_code
        return None
    except Exception:
        return None


def _fetch_edinet_db_company(edinet_code: str) -> Optional[dict]:
    """
    /v1/companies/{edinet_code} — 企業基本情報を取得する。
    主な取得フィールド: name, industry, accounting_standard,
                        credit_score, credit_rating
    """
    import requests
    try:
        r = requests.get(
            f"{EDINET_DB_BASE}/companies/{edinet_code}",
            headers={"X-API-Key": EDINET_DB_API_KEY},
            timeout=EDINET_DB_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        return r.json().get("data")
    except Exception:
        return None


def _fetch_edinet_db_financials(edinet_code: str) -> Optional[list]:
    """
    /v1/companies/{edinet_code}/financials — 財務時系列データを取得する。
    返値は最新年度が先頭のリスト。
    主な取得フィールド（年度ごと）:
        fiscal_year, revenue, operating_income, net_income,
        total_assets, equity, operating_cf, investing_cf, financing_cf
    """
    import requests
    try:
        r = requests.get(
            f"{EDINET_DB_BASE}/companies/{edinet_code}/financials",
            headers={"X-API-Key": EDINET_DB_API_KEY},
            timeout=EDINET_DB_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        return data if data else None
    except Exception:
        return None


def _fetch_edinet_db_ratios(edinet_code: str) -> Optional[list]:
    """
    /v1/companies/{edinet_code}/ratios — 財務比率時系列データを取得する。
    返値は最新年度が先頭のリスト。
    主な取得フィールド（年度ごと）:
        fiscal_year, roe_official, roa, equity_ratio,
        current_ratio, per, pbr, dividend_yield, payout_ratio,
        operating_margin, net_margin, debt_to_equity
    """
    import requests
    try:
        r = requests.get(
            f"{EDINET_DB_BASE}/companies/{edinet_code}/ratios",
            headers={"X-API-Key": EDINET_DB_API_KEY},
            timeout=EDINET_DB_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        return data if data else None
    except Exception:
        return None


def _fetch_edinet_db(ticker: str) -> Optional[dict]:
    """
    EDINET DB REST API から日本株の財務データを取得し、
    yfinance データに上書きするためのキー付き辞書を返す。

    引数:
        ticker: 日本株ティッカー（例: "7203.T" または "7203"）

    戻り値:
        成功: yfinance dict と同一キー体系を持つ上書き用辞書
              （_source キーに "edinet_db" を含む）
        失敗: None

    処理フロー:
        1. 4桁証券コードを抽出し /v1/search で EDINET コードを解決
        2. /v1/companies/{code}/financials で最新年度の財務データ取得
        3. /v1/companies/{code}/ratios で最新年度の財務比率取得
        4. /v1/companies/{code} で企業基本情報取得
        5. 取得値を既存 yfinance dict キー体系にマッピングして返す
    """
    # ── STEP 1: 4桁証券コードの抽出 ─────────────────────────────
    raw = ticker.upper().replace(".T", "").strip()
    if not raw.isdigit():
        # 数字のみでない（米国株等）は呼び出し不可
        return None

    sec_code_4digit = raw  # 例: "7203"

    # ── STEP 2: EDINET コード解決 ───────────────────────────────
    edinet_code = _resolve_edinet_code(sec_code_4digit)
    if not edinet_code:
        return None

    # ── STEP 3: 財務時系列・財務比率・企業情報を並列取得 ──────────
    # ★ リクエスト数を最小化するため、取得失敗は許容（部分取得で継続）
    financials = _fetch_edinet_db_financials(edinet_code)
    ratios      = _fetch_edinet_db_ratios(edinet_code)
    company     = _fetch_edinet_db_company(edinet_code)

    # 最低限どれか1つ取得できなければ中断
    if not financials and not ratios and not company:
        return None

    # ── STEP 4: 最新年度データを取得（fiscal_year 降順ソート後に先頭を選択）──
    #
    # EDINET DB は API の返却順序が保証されていない（昇順で返ることがある）。
    # fiscal_year を文字列として降順ソートすることで、
    # "2024" > "2023" > ... の順になり、常に最新年度が先頭になる。
    # fiscal_year が存在しない要素は末尾に送る（"0" をデフォルトとして使用）。
    def _sort_by_fy(lst: list) -> list:
        try:
            return sorted(
                lst,
                key=lambda x: str(x.get("fiscal_year", "0")),
                reverse=True,   # 降順: 新しい年度が先頭
            )
        except Exception:
            return lst  # ソート失敗は無視して元のリストを使用

    sorted_fin   = _sort_by_fy(financials) if financials else []
    sorted_ratio = _sort_by_fy(ratios)     if ratios     else []

    latest_fin   = sorted_fin[0]   if sorted_fin   else {}
    latest_ratio = sorted_ratio[0] if sorted_ratio else {}

    # ── STEP 5: フィールドマッピング ────────────────────────────
    # 財務時系列 (financials) → 絶対値（円単位）
    revenue              = _safe_float(latest_fin.get("revenue"))
    operating_income     = _safe_float(latest_fin.get("operating_income"))
    net_income           = _safe_float(latest_fin.get("net_income"))
    total_assets         = _safe_float(latest_fin.get("total_assets"))
    # equity フィールド名は API によって異なる可能性があるため候補を列挙
    stockholders_equity  = (
        _safe_float(latest_fin.get("equity"))
        or _safe_float(latest_fin.get("stockholders_equity"))
        or _safe_float(latest_fin.get("net_assets"))
    )
    op_cf = (
        _safe_float(latest_fin.get("operating_cf"))
        or _safe_float(latest_fin.get("operating_cash_flow"))
        or _safe_float(latest_fin.get("cash_flows_from_operating"))
    )
    invest_cf = (
        _safe_float(latest_fin.get("investing_cf"))
        or _safe_float(latest_fin.get("investment_cf"))
        or _safe_float(latest_fin.get("investing_activities_cf"))
        or _safe_float(latest_fin.get("investing_cash_flow"))
        or _safe_float(latest_fin.get("cash_flows_from_investing"))
        or _safe_float(latest_fin.get("investing_activities"))
    )
    finance_cf = (
        _safe_float(latest_fin.get("financing_cf"))
        or _safe_float(latest_fin.get("financing_activities_cf"))
        or _safe_float(latest_fin.get("financing_cash_flow"))
        or _safe_float(latest_fin.get("cash_flows_from_financing"))
        or _safe_float(latest_fin.get("financing_activities"))
    )

    # 財務比率 (ratios) → 比率値（%表記の場合は /100 して小数化）
    def _pct_to_float(v) -> Optional[float]:
        """% 表記（例: 12.5 → 0.125）または既に小数の値を統一的に小数化"""
        f = _safe_float(v)
        if f is None:
            return None
        # EDINET DB は % 表記（12.5%）で返すため 1.0 超なら /100
        return f / 100.0 if abs(f) > PCT_BOUNDARY else f

    roe            = _pct_to_float(latest_ratio.get("roe_official"))
    roa            = _pct_to_float(latest_ratio.get("roa"))
    equity_ratio   = _pct_to_float(latest_ratio.get("equity_ratio"))
    current_ratio  = _safe_float(latest_ratio.get("current_ratio"))
    per_edinet     = _safe_float(latest_ratio.get("per"))
    pbr_edinet     = _safe_float(latest_ratio.get("pbr"))
    div_yield_edinet = _pct_to_float(latest_ratio.get("dividend_yield"))
    payout_ratio   = _pct_to_float(latest_ratio.get("payout_ratio"))
    op_cf_margin   = _pct_to_float(latest_ratio.get("operating_margin"))
    debt_to_equity = _safe_float(latest_ratio.get("debt_to_equity"))

    # 企業基本情報 (company)
    industry_raw = None
    edinet_name  = None
    accounting_standard = None
    credit_score = None
    credit_rating = None
    if company:
        industry_raw        = company.get("industry")
        edinet_name         = company.get("name")
        accounting_standard = company.get("accounting_standard", "JP")
        credit_score        = _safe_float(company.get("credit_score"))
        credit_rating       = company.get("credit_rating")

    # CF/純利益比率（自己計算）
    cf_ni_ratio = None
    try:
        if op_cf is not None and net_income is not None and net_income != 0:
            cf_ni_ratio = op_cf / net_income
    except Exception:
        pass

    # ROE ソース
    roe_source = "EDINET DB" if roe is not None else None

    # ── STEP 6: 上書きキー辞書を組み立てて返す ──────────────────
    override: dict = {
        "_edinet_code":          edinet_code,
        "_edinet_source":        True,
        "_accounting_standard":  accounting_standard,
        "_credit_score":         credit_score,
        "_credit_rating":        credit_rating,
        "_edinet_financials_fy":
            latest_fin.get("fiscal_year") or latest_ratio.get("fiscal_year"),
    }

    # ── None でない値のみ上書き対象に追加 ────────────────────────
    # yfinance 既存フィールドと同名キーで上書きする（マージ関数が使用）
    _optional_fields = {
        # P/L 絶対値
        "revenue":              revenue,
        "net_income":           net_income,
        # B/S 絶対値
        "total_assets":         total_assets,
        "stockholders_equity":  stockholders_equity,
        # CF 絶対値
        "op_cf":                op_cf,
        "invest_cf":            invest_cf,
        "finance_cf":           finance_cf,
        # 財務比率
        "roe":                  roe,
        "roe_source":           roe_source,
        "roa":                  roa,
        "equity_ratio":         equity_ratio,
        "current_ratio":        current_ratio,
        "payout_ratio":         payout_ratio,
        "op_cf_margin":         op_cf_margin,
        "cf_ni_ratio":          cf_ni_ratio,
        "debt_to_equity":       debt_to_equity,
    }

    # PER / PBR は yfinance のリアルタイム値を優先する（比率はリアルタイム株価依存）
    # ただし yfinance で None の場合のみ EDINET DB 値を採用
    _price_based = {
        "per":       per_edinet,
        "pbr":       pbr_edinet,
        "div_yield": div_yield_edinet,
    }

    # セクター・業種は EDINET DB 側が詳細なため上書き
    if industry_raw:
        _optional_fields["industry"] = industry_raw
    if edinet_name:
        _optional_fields["name"] = edinet_name

    # None 以外のみ追加
    for k, v in _optional_fields.items():
        if v is not None:
            override[k] = v
    for k, v in _price_based.items():
        if v is not None:
            override[f"_edinet_{k}"] = v   # キープレフィックスで区別（マージ関数で制御）

    return override


def _merge_edinet_into_yfinance(base: dict, edinet: dict) -> dict:
    """
    yfinance 取得データ（base）に EDINET DB データ（edinet）を上書きマージする。

    マージ規則:
      A) 財務絶対値・比率（revenue, net_income, roe, roa 等）:
         EDINET DB 値を優先して上書き。
         （有報ベースのため精度が高い）
      B) リアルタイム株価依存値（per, pbr, div_yield）:
         yfinance 値が None の場合のみ EDINET DB 値で補完。
         （PER・PBR は株価を使うため yfinance のリアルタイム値を優先）
      C) 株価・チャート・出来高（price, history 等）:
         yfinance 値を維持（EDINET DB は取扱なし）
      D) _source フィールド:
         "yfinance+edinet_db" に更新して出所を明示する。

    引数:
        base   : _fetch_yfinance() の返値
        edinet : _fetch_edinet_db() の返値
    戻り値:
        マージ済み辞書（base を変更したコピー）
    """
    merged = dict(base)   # base を破壊しないようコピー

    # ── A) 財務絶対値・比率の上書き ──────────────────────────────
    OVERWRITE_KEYS = {
        "revenue", "net_income", "total_assets", "stockholders_equity",
        "op_cf", "invest_cf", "finance_cf",
        "roe", "roe_source", "roa", "equity_ratio", "current_ratio",
        "payout_ratio", "op_cf_margin", "cf_ni_ratio", "debt_to_equity",
        "name", "industry",
        # EDINET DB メタデータ
        "_edinet_code", "_edinet_source", "_accounting_standard",
        "_credit_score", "_credit_rating", "_edinet_financials_fy",
    }
    for k in OVERWRITE_KEYS:
        if k in edinet:
            merged[k] = edinet[k]

    # ── B) リアルタイム株価依存値の補完（yfinance 優先）───────────
    PRICE_BASED_KEYS = ("per", "pbr", "div_yield")
    for k in PRICE_BASED_KEYS:
        edinet_val = edinet.get(f"_edinet_{k}")
        if edinet_val is not None and merged.get(k) is None:
            merged[k] = edinet_val

    # ── C) 株価・チャートは変更しない（何もしない）───────────────
    # merged["price"], merged["history"] などはそのまま

    # ── D) _source 更新 ────────────────────────────────────────
    base_src = merged.get("_source", "yfinance")
    if base_src and "edinet_db" not in base_src:
        merged["_source"] = f"{base_src}+edinet_db"

    return merged


# ==============================================================
# SECTION 4: yfinance データ取得（ROE自己計算 / CF-NI / セクター情報）
# ==============================================================

def _fetch_yfinance(ticker: str) -> Optional[dict]:
    """
    yfinance から財務指標・株価・キャッシュフローデータを取得し辞書で返す。
    取得失敗（price が None）の場合は None を返す。
    """
    try:
        tk    = yf.Ticker(ticker)
        info  = tk.info or {}
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            return None

        currency    = info.get("currency", "USD")
        curr_symbol = "¥" if currency == "JPY" else "$"
        exchange    = info.get("exchange", "")

        w52_low = info.get("fiftyTwoWeekLow")
        dev_from_low = None
        try:
            if price and w52_low and w52_low > 0:
                dev_from_low = (price - w52_low) / w52_low
        except Exception:
            pass

        cf  = tk.cashflow
        is_ = tk.income_stmt
        bs  = tk.balance_sheet

        op_cf = _latest(cf,
            "Operating Cash Flow",
            "Cash Flow From Continuing Operating Activities",
            "Total Cash From Operating Activities",
        )
        capex = _latest(cf,
            "Capital Expenditure",
            "Purchase Of Property Plant And Equipment",
        )
        net_income = _latest(is_,
            "Net Income", "Net Income Common Stockholders",
            "Net Income From Continuing Operations",
        )
        total_assets        = _latest(bs, "Total Assets")
        curr_assets         = _latest(bs, "Current Assets", "Total Current Assets")
        curr_liab           = _latest(bs, "Current Liabilities", "Total Current Liabilities")
        total_liab          = _latest(bs, "Total Liabilities Net Minority Interest", "Total Liab")
        total_debt          = _latest(bs,
            "Total Debt", "Long Term Debt And Capital Lease Obligation", "Long Term Debt")
        cash = _latest(bs,
            "Cash And Cash Equivalents",
            "Cash Cash Equivalents And Short Term Investments",
        )
        stockholders_equity = _latest(bs,
            "Stockholders Equity", "Common Stockholders Equity",
            "Total Equity Gross Minority Interest",
        )
        revenue = _latest(is_, "Total Revenue")

        # FCF = 営業CF - |CAPEX|
        fcf = None
        try:
            if op_cf is not None and capex is not None:
                fcf = op_cf - abs(capex)
        except Exception:
            pass

        # ネット D/E = (有利子負債 - 現金) / 自己資本
        net_de = None
        try:
            if (total_debt is not None and cash is not None
                    and stockholders_equity and stockholders_equity != 0):
                net_de = (total_debt - cash) / stockholders_equity
        except Exception:
            pass

        # 総資産回転率 = 売上高 / 総資産
        asset_turnover = None
        try:
            if revenue and total_assets and total_assets != 0:
                asset_turnover = revenue / total_assets
        except Exception:
            pass

        # アクルーアル = 純利益 - 営業CF
        accrual, accrual_method = None, None
        try:
            if net_income is not None and op_cf is not None:
                accrual        = net_income - op_cf
                accrual_method = "算出（純利益 - 営業CF）"
        except Exception:
            pass

        accrual_ratio = None
        try:
            if accrual is not None and total_assets and total_assets != 0:
                accrual_ratio = accrual / total_assets
        except Exception:
            pass

        debt_to_equity = None
        try:
            if total_liab and stockholders_equity and stockholders_equity != 0:
                debt_to_equity = total_liab / stockholders_equity
        except Exception:
            pass

        current_ratio = None
        try:
            if curr_assets and curr_liab and curr_liab != 0:
                current_ratio = curr_assets / curr_liab
        except Exception:
            pass

        equity_ratio = None
        try:
            if stockholders_equity and total_assets and total_assets != 0:
                equity_ratio = stockholders_equity / total_assets
        except Exception:
            pass

        op_cf_margin = None
        try:
            if op_cf and revenue and revenue != 0:
                op_cf_margin = op_cf / revenue
        except Exception:
            pass

        # [ROE] BS/IS 自己計算 → 失敗なら yfinance 標準値
        roe, roe_source = None, "yfinance標準値"
        try:
            if net_income is not None and stockholders_equity and stockholders_equity != 0:
                roe        = net_income / stockholders_equity
                roe_source = "BS/IS自己計算"
        except Exception:
            pass
        if roe is None:
            roe = _safe_float(info.get("returnOnEquity"))

        # 営業CF / 純利益 比率
        cf_ni_ratio = None
        try:
            if op_cf is not None and net_income is not None and net_income != 0:
                cf_ni_ratio = op_cf / net_income
        except Exception:
            pass

        # 配当利回り（銘柄タイプ対応版）
        div_yield = compute_div_yield(info, is_japan_stock(ticker), price)

        # 年間配当金（YoC 計算用）
        annual_dividend = None
        try:
            dps = _safe_float(info.get("dividendRate"))
            if dps:
                annual_dividend = dps
            elif div_yield and price:
                annual_dividend = div_yield * float(price)
        except Exception:
            pass

        # 株価履歴（1年分・ローソク足用）
        hist = tk.history(period="1y", interval="1d")

        # 異常値チェック（前日比±30%超）
        is_anomaly = False
        try:
            if len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                if prev and abs(price - prev) / prev > PRICE_ANOMALY_THRESHOLD:
                    is_anomaly = True
        except Exception:
            pass

        return {
            "ticker":              ticker,
            "name":                info.get("longName", info.get("shortName", ticker)),
            "exchange":            exchange,
            "currency":            currency,
            "curr_symbol":         curr_symbol,
            "sector":              info.get("sector",   "―") or "―",
            "industry":            info.get("industry", "―") or "―",
            "description":         info.get("longBusinessSummary", "") or "",
            "employees":           _safe_int(info.get("fullTimeEmployees")),
            "country":             info.get("country",  "―") or "―",
            "website":             info.get("website",  "") or "",
            "price":               price,
            "per":                 _safe_float(info.get("trailingPE")),
            "pbr":                 _safe_float(info.get("priceToBook")),
            "div_yield":           div_yield,
            "annual_dividend":     annual_dividend,
            "market_cap":          _safe_int(info.get("marketCap")),
            "w52_low":             w52_low,
            "w52_high":            info.get("fiftyTwoWeekHigh"),
            "dev_from_low":        dev_from_low,
            "roe":                 roe,
            "roe_source":          roe_source,
            "roa":                 _safe_float(info.get("returnOnAssets")),
            "debt_to_equity":      debt_to_equity,
            "net_de_ratio":        net_de,
            "current_ratio":       current_ratio,
            "equity_ratio":        equity_ratio,
            "asset_turnover":      asset_turnover,
            "payout_ratio":        _safe_float(info.get("payoutRatio")),
            "op_cf_margin":        op_cf_margin,
            "fcf":                 fcf,
            "accrual":             accrual,
            "accrual_ratio":       accrual_ratio,
            "accrual_method":      accrual_method,
            "net_income":          net_income,
            "op_cf":               op_cf,
            "capex":               capex,
            "revenue":             revenue,
            "total_assets":        total_assets,
            "cash":                cash,
            "total_debt":          total_debt,
            "stockholders_equity": stockholders_equity,
            "cf_ni_ratio":         cf_ni_ratio,
            "history":             hist,
            "_source":             "yfinance",
            "fetched_at":          datetime.now().strftime("%Y-%m-%d %H:%M"),
            "is_anomaly":          is_anomaly,
            "error":               None,
            # EDINET DB 拡張フィールド（日本株取得後に _merge_edinet_into_yfinance で設定）
            "_edinet_code":        None,
            "_edinet_source":      False,
            "_accounting_standard": None,
            "_credit_score":        None,
            "_credit_rating":       None,
            "_edinet_financials_fy": None,
            "invest_cf":            None,   # yfinance では個別取得しない
            "finance_cf":           None,   # yfinance では個別取得しない
        }
    except Exception:
        return None


# ==============================================================
# SECTION 5: フォールバック統合取得（@st.cache_data）
# ==============================================================

@st.cache_data(ttl=7200, show_spinner=False)
def fetch_stock_data(ticker: str) -> dict:
    """
    データ取得統合関数。フォールバック順序:
      L1: @st.cache_data メモリキャッシュ（TTL 2時間）
      L2: ディスクキャッシュ（/tmp/stockview_cache/{ticker}.json）
      L3: yfinance（全銘柄）+ EDINET DB マージ（日本株のみ）
      L4: Alpha Vantage フォールバック（米国株・環境変数要）
      L5: ディスクキャッシュ（TTL 切れ stale データ）
      L6: エラー辞書

    日本株のみ L3 で EDINET DB を追加呼び出しし、財務系指標を上書きする。
    株価・チャート（price, history）は常に yfinance のリアルタイム値を使用。
    """
    # L2: ディスクキャッシュ
    cached = _read_cache(ticker)
    if cached:
        return cached

    # L3: yfinance 取得
    data = _fetch_yfinance(ticker)
    if data:
        # ── 日本株のみ EDINET DB をマージ ──────────────────────
        if is_japan_stock(ticker):
            try:
                edinet_data = _fetch_edinet_db(ticker)
                if edinet_data:
                    data = _merge_edinet_into_yfinance(data, edinet_data)
            except Exception:
                # EDINET DB 取得失敗は致命的エラーではない → yfinance データのみで継続
                pass
        _write_cache(ticker, data)
        return data

    # L4: Alpha Vantage フォールバック（米国株向け）
    av = _fetch_alpha_vantage(ticker)
    if av:
        _write_cache(ticker, av)
        return av

    # L5: stale キャッシュ
    stale = _read_stale_cache(ticker)
    if stale:
        stale["_warning"] = "最新データ取得失敗。古いキャッシュを表示しています。"
        return stale

    # L6: エラー辞書
    return {
        "ticker": ticker, "name": ticker, "_source": "none",
        "error": "すべてのデータソースから取得できませんでした",
    }


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_history_for_period(ticker: str, period: str = "1y"):
    """指定期間の株価履歴を取得。@st.cache_data TTL=1時間"""
    try:
        tk   = yf.Ticker(ticker)
        hist = tk.history(period=period, interval="1d")
        return None if (hist is None or hist.empty) else hist
    except Exception:
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def get_usdjpy_rate() -> float:
    """現在のドル円レートを取得。エラー時は 150 円固定"""
    try:
        info = yf.Ticker("USDJPY=X").info or {}
        rate = info.get("regularMarketPrice") or info.get("currentPrice")
        if rate and 80 < float(rate) < 300:
            return float(rate)
    except Exception:
        pass
    return USDJPY_DEFAULT


# ==============================================================
# SECTION 6: Google Sheets 連携
# ==============================================================

@st.cache_resource(show_spinner=False)
def get_gspread_client(credentials_json: str):
    """service_account JSON 文字列から gspread クライアントを生成（キャッシュ）"""
    info  = json.loads(credentials_json)
    creds = Credentials.from_service_account_info(info, scopes=SHEET_SCOPE)
    return gspread.authorize(creds)


def _get_or_create_ws(sh, name: str, rows: int = 500, cols: int = 10):
    """ワークシートを取得。存在しない場合は新規作成"""
    try:
        return sh.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=rows, cols=cols)


def load_tickers_from_sheet(client, spreadsheet_id: str):
    """
    watchlist シートから銘柄リストと取得単価（Purchase_Price）を読み込む。
    戻り値: (tickers: list, purchase_prices: dict{ticker: float})
    D列 Purchase_Price 列の読み込みに対応。
    """
    try:
        sh   = client.open_by_key(spreadsheet_id)
        ws   = _get_or_create_ws(sh, WS_WATCHLIST)
        recs = ws.get_all_records()
        if not recs:
            ws.append_row([TICKER_COL])
            return [], {}
        tickers, purchase_prices = [], {}
        for r in recs:
            t = str(r.get(TICKER_COL, "")).strip()
            if not t:
                continue
            tickers.append(t)
            try:
                pp_raw = r.get(PURCHASE_COL, "")
                pp     = _safe_float(str(pp_raw))
                if pp and pp > 0:
                    purchase_prices[t] = pp
            except Exception:
                pass
        return tickers, purchase_prices
    except Exception as e:
        st.warning(f"watchlist 読み込みエラー: {e}")
        return [], {}


def save_tickers_to_sheet(client, spreadsheet_id: str,
                           tickers: list, purchase_prices: dict = None) -> bool:
    """watchlist シートへ銘柄リストと取得単価を保存する"""
    try:
        sh  = client.open_by_key(spreadsheet_id)
        ws  = _get_or_create_ws(sh, WS_WATCHLIST)
        pp  = purchase_prices or {}
        ws.clear()
        header = [TICKER_COL, "", "", PURCHASE_COL]
        rows   = [[t, "", "", pp.get(t, "")] for t in tickers]
        ws.update("A1", [header] + rows)
        return True
    except Exception as e:
        st.error(f"watchlist 保存エラー: {e}")
        return False


def load_settings_from_sheet(client, spreadsheet_id: str) -> dict:
    """settings シートからアラート閾値を読み込む"""
    from finance_logic import get_default_thresholds
    defaults = get_default_thresholds()
    try:
        sh   = client.open_by_key(spreadsheet_id)
        ws   = _get_or_create_ws(sh, WS_SETTINGS, rows=50, cols=2)
        recs = ws.get_all_records()
        result = dict(defaults)
        for r in recs:
            k = str(r.get("key", "")).strip()
            if k in defaults:
                v = _safe_float(r.get("value"))
                if v is not None:
                    result[k] = v
        return result
    except Exception as e:
        st.warning(f"settings 読み込みエラー: {e}")
        return dict(defaults)


def save_settings_to_sheet(client, spreadsheet_id: str, thresholds: dict) -> bool:
    """settings シートへアラート閾値を保存する"""
    try:
        sh = client.open_by_key(spreadsheet_id)
        ws = _get_or_create_ws(sh, WS_SETTINGS, rows=50, cols=2)
        ws.clear()
        ws.update("A1", [["key", "value"]] + [[k, v] for k, v in thresholds.items()])
        return True
    except Exception as e:
        st.error(f"settings 保存エラー: {e}")
        return False


# ==============================================================
# SECTION 7: service_account.json 自動読み込み
# ==============================================================

def try_load_service_account_file() -> Optional[str]:
    """
    service_account.json を自動検出して文字列で返す。
    優先順位:
      1. Google Drive /content/drive/MyDrive/stock_viewer/service_account.json
      2. /content/service_account.json  (Colab にアップロードした場合)
      3. ローカルカレントディレクトリ
    JSON 構文エラーの場合はスキップして次候補を試みる。
    """
    for path in SA_FILE_CANDIDATES:
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8")
                json.loads(content)   # 構文チェック
                return content
        except Exception:
            continue
    return None


# ==============================================================
# SECTION 8: 金融庁 EDINET API — 有報新規提出検知（認証不要・無料）
# ==============================================================
#
# 金融庁公開 API: https://api.edinet-api.edinet.fsa.go.jp/api/v2/documents.json
# 認証: 不要（APIキー不要・登録不要）
# 取得: 指定日の全提出書類リスト（type=2 で全書類を取得）
# 対象書類:
#   docTypeCode = "120" → 有価証券報告書
#   docTypeCode = "140" → 四半期報告書
#   docTypeCode = "160" → 半期報告書
#   docTypeCode = "300" → 大量保有報告書
# キャッシュ: TTL=43200秒（12時間）→ 1日最大2回のリクエストに抑制
# ==============================================================

EDINET_GOV_API_BASE = "https://api.edinet-api.edinet.fsa.go.jp/api/v2"

EDINET_TARGET_DOC_TYPES = {
    "120": "有価証券報告書",
    "140": "四半期報告書",
    "160": "半期報告書",
    "300": "大量保有報告書",
}


@st.cache_data(ttl=43200, show_spinner=False)
def fetch_edinet_submissions(date_str: str) -> Optional[list]:
    """
    金融庁 EDINET API から指定日の提出書類リストを取得する。

    引数:
        date_str: 対象日付（例: "2026-02-25"）。当日を指定するのが通常の使い方。

    戻り値:
        成功: 書類情報の list（1日分全件）
        失敗: None

    各要素の主なフィールド:
        edinetCode      — EDINETコード（例: "E02144"）
        filerName       — 提出者名（例: "トヨタ自動車株式会社"）
        docTypeCode     — 書類種別コード（例: "120"）
        secCode         — 証券コード5桁（例: "72030"）
        periodEnd       — 対象期間終了日
        submitDateTime  — 提出日時
        docID           — 書類管理番号（EDINET 検索ページへのリンク用）
    """
    import requests
    try:
        r = requests.get(
            f"{EDINET_GOV_API_BASE}/documents.json",
            params={"date": date_str, "type": 2},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        results = r.json().get("results", [])
        return results if results else None
    except Exception:
        return None


def check_watchlist_submissions(tickers: list, date_str: str = None) -> list:
    """
    ウォッチリスト内の日本株銘柄に対して、指定日の有報等の提出を検知する。

    引数:
        tickers  : ウォッチリスト銘柄リスト（例: ["7203.T", "9984.T", "AAPL"]）
        date_str : 検索対象日（None = 当日）

    戻り値:
        該当書類情報のリスト（0件なら空リスト）
        各要素:
            ticker    — ウォッチリスト内のティッカー（例: "7203.T"）
            name      — 提出者名（例: "トヨタ自動車株式会社"）
            doc_type  — 書類種別名（例: "有価証券報告書"）
            period_end — 対象期間終了日
            submitted  — 提出日時
            doc_id    — 書類管理番号

    照合ロジック:
        ① _edinet_code_cache に EDINET コードが存在 → EDINET コードで完全一致照合
        ② キャッシュなし → 証券コード前4桁で前方一致照合
        米国株は対象外（is_japan_stock() で除外）
    """
    if not tickers:
        return []

    if date_str is None:
        from datetime import date
        date_str = date.today().strftime("%Y-%m-%d")

    submissions = fetch_edinet_submissions(date_str)
    if not submissions:
        return []

    # ウォッチリストの証券コード4桁セットを構築
    jp_tickers     = [t for t in tickers if is_japan_stock(t)]
    watch_sec4     = {t.upper().replace(".T", "") for t in jp_tickers}
    watch_edinet   = {
        _edinet_code_cache[code4]
        for code4 in watch_sec4
        if code4 in _edinet_code_cache
    }
    # 証券コード4桁 → ティッカー の逆引きマップ
    code4_to_ticker = {
        t.upper().replace(".T", ""): t for t in jp_tickers
    }
    # EDINETコード → ティッカー の逆引きマップ
    edinet_to_ticker = {
        ec: code4_to_ticker[code4]
        for code4, ec in _edinet_code_cache.items()
        if code4 in code4_to_ticker
    }

    hits = []
    for doc in submissions:
        doc_type = str(doc.get("docTypeCode", ""))
        if doc_type not in EDINET_TARGET_DOC_TYPES:
            continue

        edinet_code = str(doc.get("edinetCode", ""))
        sec_code_5  = str(doc.get("secCode", ""))
        sec_code_4  = sec_code_5[:4] if len(sec_code_5) >= 4 else ""

        # 照合: EDINETコード優先 → 証券コード前4桁
        matched_ticker = (
            edinet_to_ticker.get(edinet_code)
            or code4_to_ticker.get(sec_code_4)
        )
        if not matched_ticker:
            continue

        hits.append({
            "ticker":     matched_ticker,
            "name":       doc.get("filerName", "―"),
            "doc_type":   EDINET_TARGET_DOC_TYPES[doc_type],
            "period_end": str(doc.get("periodEnd", "―")),
            "submitted":  str(doc.get("submitDateTime", "―")),
            "doc_id":     str(doc.get("docID", "")),
        })

    return hits


# ==============================================================
# SECTION 8.5: EDINET DB テキスト取得 + Claude API 要約
# ==============================================================
#
# 有報テキストブロック（MD&A・リスク情報・事業概要）を EDINET DB から取得し、
# Anthropic Claude API で日本語要約を生成する。
#
# 環境変数:
#   ANTHROPIC_API_KEY — Claude API キー（未設定時は要約ボタンを非表示）
#
# エンドポイント:
#   GET /v1/companies/{edinet_code}/documents       → 有報文書リスト
#   GET /v1/companies/{edinet_code}/documents/{id}/text → テキスト本文
#
# キャッシュ: テキストは TTL=604800秒（7日間）で長期キャッシュ
# ==============================================================

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_API_BASE    = "https://api.anthropic.com/v1"
ANTHROPIC_MODEL       = "claude-haiku-4-5-20251001"   # 要約用: 速い・安い

SUMMARY_MAX_INPUT     = 3000   # 要約に渡すテキストの最大文字数
SUMMARY_MAX_TOKENS    = 300    # 要約の最大トークン数


@st.cache_data(ttl=604800, show_spinner=False)
def fetch_edinet_text_blocks(ticker: str) -> Optional[dict]:
    """
    EDINET DB から有報テキストブロックを取得する。

    引数:
        ticker: 日本株ティッカー（例: "7203.T"）

    戻り値（成功時）:
        {
            "business_description": str,   # 事業の概況
            "risk_factors":         str,   # 事業リスク
            "mda":                  str,   # MD&A（経営者による分析）
        }
    失敗時: None

    エンドポイント: /v1/companies/{edinet_code}/documents/latest/sections
    """
    import requests

    raw = ticker.upper().replace(".T", "").strip()
    if not raw.isdigit():
        return None

    edinet_code = _resolve_edinet_code(raw)
    if not edinet_code:
        return None

    try:
        # 最新有報のテキストセクションを取得
        r = requests.get(
            f"{EDINET_DB_BASE}/companies/{edinet_code}/documents/latest/sections",
            headers={"X-API-Key": EDINET_DB_API_KEY},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json().get("data", {})
        if not data:
            return None

        # フィールド名の候補（EDINET DB の仕様揺れを吸収）
        def _pick(d: dict, *keys) -> str:
            for k in keys:
                v = d.get(k, "")
                if v and str(v).strip():
                    return str(v).strip()
            return ""

        return {
            "business_description": _pick(
                data, "business_description", "business_overview",
                "business_summary", "overview"
            ),
            "risk_factors": _pick(
                data, "risk_factors", "risks", "business_risks"
            ),
            "mda": _pick(
                data, "mda", "management_discussion",
                "management_discussion_and_analysis",
                "results_of_operations"
            ),
        }
    except Exception:
        return None


def summarize_with_claude(text: str, section_name: str) -> Optional[str]:
    """
    Anthropic Claude API を使って有報テキストを日本語で要約する。

    引数:
        text         : 要約対象のテキスト（長文可）
        section_name : セクション名（プロンプトに埋め込む）

    戻り値:
        成功: 要約文字列（200字以内）
        失敗: None

    環境変数 ANTHROPIC_API_KEY が設定されていない場合は None を返す。
    テキストは SUMMARY_MAX_INPUT 文字に切り詰めてからAPIに渡す。
    """
    import requests

    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV, "")
    if not api_key:
        return None

    truncated = text[:SUMMARY_MAX_INPUT]

    prompt = (
        f"以下は有価証券報告書の「{section_name}」セクションの本文です。\n"
        f"投資家向けに重要なポイントを200字以内で日本語で要約してください。\n"
        f"箇条書きではなく、一段落の文章で出力してください。\n\n"
        f"【本文】\n{truncated}"
    )

    try:
        r = requests.post(
            f"{ANTHROPIC_API_BASE}/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      ANTHROPIC_MODEL,
                "max_tokens": SUMMARY_MAX_TOKENS,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if r.status_code != 200:
            return None
        content = r.json().get("content", [])
        if content and content[0].get("type") == "text":
            return content[0]["text"].strip()
        return None
    except Exception:
        return None
