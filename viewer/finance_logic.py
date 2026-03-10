# ==============================================================
# finance_logic.py  —  純粋計算ロジック層
# 依存: 標準ライブラリのみ（streamlit / yfinance / gspread 不使用）
# 役割: 利回り計算、スコアリング、フォーマット、アラート判定、リンク生成
# ==============================================================

import math
from typing import Optional


# ==============================================================
# SECTION 1: 定数・設定値
# ==============================================================

DEFAULT_THRESHOLDS = {
    "per_max":           15.0,
    "pbr_max":            1.5,
    "psr_max":            5.0,   # PSR 上限: 5倍超は割高
    "pcfr_max":          20.0,   # PCFR 上限: 20倍超は割高
    "div_yield_min":      0.03,
    "roe_min":            0.10,
    "roa_min":            0.05,
    "equity_ratio_min":   0.40,
    "current_ratio_min":  1.5,
    "op_cf_margin_min":   0.05,
    "cf_ni_ratio_min":    1.0,
}

THRESHOLD_LABELS = {
    "per_max":           "PER 上限（倍）",
    "pbr_max":           "PBR 上限（倍）",
    "psr_max":           "PSR 上限（倍）",
    "pcfr_max":          "PCFR 上限（倍）",
    "div_yield_min":     "配当利回り 下限（%）",
    "roe_min":           "ROE 下限（%）",
    "roa_min":           "ROA 下限（%）",
    "equity_ratio_min":  "自己資本比率 下限（%）",
    "current_ratio_min": "流動比率 下限（倍）",
    "op_cf_margin_min":  "営業CFマージン 下限（%）",
    "cf_ni_ratio_min":   "営業CF/純利益比率 下限（倍）",
}

# パーセント入力が必要なキー（UI で 0.05 → 5.0% に変換するため）
PCT_KEYS = {
    "div_yield_min", "roe_min", "roa_min",
    "equity_ratio_min", "op_cf_margin_min",
}

# チャート期間マッピング（表示ラベル → yfinance period パラメータ）
CHART_PERIODS = {
    "1M": "1mo",
    "3M": "3mo",
    "6M": "6mo",
    "YTD": "ytd",
    "1Y": "1y",
}


# ==============================================================
# SECTION 2: 型変換ユーティリティ
# ==============================================================

def _safe_float(v) -> Optional[float]:
    """None / NaN / Inf を安全に float へ変換。変換不能は None を返す"""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _safe_int(v) -> Optional[int]:
    """None / 空文字を安全に int へ変換。変換不能は None を返す"""
    try:
        return int(float(v)) if v not in (None, "None", "-", "") else None
    except (ValueError, TypeError):
        return None


def normalize_ticker(raw: str) -> str:
    """
    入力ティッカーを正規化する。
    数字のみ（日本株コード）→ 末尾に .T を付与してTOKYO市場扱い
    """
    s = raw.strip().upper()
    return f"{s}.T" if s.isdigit() else s



def validate_ticker(raw: str) -> tuple:
    """
    ティッカーシンボルの形式バリデーション。
    返値: (is_valid: bool, is_japan: bool, normalized: str, error_msg: str)

    日本株: 4桁数字 → 自動で .T 付与 / 4桁数字.T → そのまま採用
    米国株: 英字始まり 1〜10文字（英字・数字・.・-）
    不正:  空文字 / 空白のみ / 16文字超 / 不正文字を含む場合
    """
    import re
    if not raw or not isinstance(raw, str):
        return False, False, "", "ティッカーが空です"
    s = raw.strip().upper()
    if not s:
        return False, False, "", "ティッカーが空です"
    if len(s) > 16:
        return False, False, "", f"ティッカーが長すぎます（{len(s)}文字 / 最大16文字）"
    clean = s.replace(".T", "")
    if clean.isdigit():
        if len(clean) != 4:
            return False, False, "", f"日本株コードは4桁です（入力: {clean}、{len(clean)}桁）"
        return True, True, f"{clean}.T", ""
    if re.match(r"^[A-Z][A-Z0-9\.\-]{0,9}$", s):
        return True, False, s, ""
    return False, False, "", (
        f"不明な形式: '{s}'\n"
        "日本株: 4桁数字（例: 7203）\n"
        "米国株: 英字始まり 1〜10文字（例: AAPL、BRK.B）"
    )


def roe_source_label(roe_source: str) -> str:
    """ROE データソース文字列を UI 表示用の短縮ラベルに変換する"""
    labels = {
        "EDINET DB":      "📋 EDINET確定値",
        "BS/IS自己計算":  "🔢 自己計算値",
        "yfinance標準値": "📡 yfinance推定値",
        "alphavantage":   "📡 AlphaVantage推定値",
    }
    return labels.get(roe_source, f"📡 {roe_source}" if roe_source else "―")


def is_japan_stock(ticker: str) -> bool:
    """ティッカーが日本株（.T 末尾 or 数字のみ）かどうかを判定"""
    return ticker.upper().endswith(".T") or ticker.isdigit()


# ==============================================================
# SECTION 3: 表示フォーマット関数
# ==============================================================

def fmt_large(val) -> str:
    """大きな数値を兆・億・B・M 単位で整形して返す"""
    if val is None:
        return "―"
    try:
        v = float(val)
        if abs(v) >= 1e12:
            return f"{v/1e12:.2f} 兆"
        if abs(v) >= 1e8:
            return f"{v/1e8:.1f} 億"
        if abs(v) >= 1e9:
            return f"{v/1e9:.2f} B"
        if abs(v) >= 1e6:
            return f"{v/1e6:.2f} M"
        return f"{v:,.0f}"
    except Exception:
        return "―"


def fmt_pct(val, decimals: int = 2) -> str:
    """小数（0.05）→ パーセント文字列（5.00%）に変換"""
    if val is None:
        return "―"
    try:
        return f"{float(val) * 100:.{decimals}f}%"
    except Exception:
        return "―"


def fmt_x(val, decimals: int = 2) -> str:
    """倍率表示（例: 15.23x）"""
    if val is None:
        return "―"
    try:
        return f"{float(val):.{decimals}f}x"
    except Exception:
        return "―"


def fmt_price(val, symbol: str) -> str:
    """株価表示（例: $123.45）"""
    if val is None:
        return "―"
    try:
        return f"{symbol}{float(val):,.2f}"
    except Exception:
        return "―"


# ==============================================================
# SECTION 4: 配当利回り計算
# ==============================================================

def fix_div_yield(raw) -> Optional[float]:
    """
    [JP専用] yfinance が日本株で % 表記（例: 4.36）を返す場合の正規化。
    1.0 超 → 既にパーセント表記 → 100 で割って小数に変換
    1.0 以下 → 既に小数（0.0436 = 4.36%）
    """
    v = _safe_float(raw)
    if v is None:
        return None
    return v / 100.0 if v > 1.0 else v


def compute_div_yield(info: dict, is_jp: bool, price) -> Optional[float]:
    """
    配当利回り計算 — 銘柄タイプ対応版

    米国株:
        ① dividendRate（年間配当額 $）/ 現在株価 を優先使用
           yfinance は US 株で dividendYield を稀に誤値（40%等）で返すため
           dividendRate から算出するほうが正確
        ② フォールバック: dividendYield（小数）。30%超は異常値として補正
    日本株:
        fix_div_yield() でパーセント表記を正規化（従来通り）
    """
    if is_jp:
        return fix_div_yield(info.get("dividendYield"))

    # ── 米国株 ──────────────────────────────────────────────────
    # 優先①: dividendRate / price
    try:
        dr = _safe_float(info.get("dividendRate"))
        p  = _safe_float(price)
        if dr is not None and p and p > 0:
            yld = dr / p
            if 0 <= yld <= 0.30:   # 30% 以内なら正常値
                return yld
    except Exception:
        pass

    # 優先②: dividendYield（小数）
    try:
        v = _safe_float(info.get("dividendYield"))
        if v is None:
            return None
        if v > 0.30:               # 30%超 → %表記の可能性
            v = v / 100.0
        if v > 0.30 or v < 0:     # それでも異常なら無効
            return None
        return v
    except Exception:
        return None


# ==============================================================
# SECTION 4.5: 派生指標計算（PSR / ROD / PCFR）
# ==============================================================

def compute_psr(market_cap, revenue) -> Optional[float]:
    """
    PSR（株価売上高倍率）= 時価総額 / 売上高
    成長株評価で使用。PERが計算できない赤字企業にも適用可能。
    一般的な目安: <1.0 割安 / 1.0〜5.0 適正 / >5.0 割高
    """
    mc = _safe_float(market_cap)
    rv = _safe_float(revenue)
    if mc is None or rv is None or rv == 0:
        return None
    return mc / rv


def compute_rod(net_income, total_debt) -> Optional[float]:
    """
    ROD（負債利益率 / Return on Debt）= 純利益 / 有利子負債
    負債を利益創出にどれだけ効率よく活用しているかを示す指標。
    数値が大きいほど負債効率が高い。
    """
    ni = _safe_float(net_income)
    td = _safe_float(total_debt)
    if ni is None or td is None or td == 0:
        return None
    return ni / td


def compute_pcfr(market_cap, op_cf) -> Optional[float]:
    """
    PCFR（株価キャッシュフロー倍率）= 時価総額 / 営業キャッシュフロー
    利益操作の影響を受けにくいCFベースの割安度指標。
    一般的な目安: <10倍 割安 / 10〜20倍 適正
    """
    mc = _safe_float(market_cap)
    cf = _safe_float(op_cf)
    if mc is None or cf is None or cf <= 0:
        return None
    return mc / cf


# ==============================================================
# SECTION 5: 財務データフレーム操作ヘルパー
# ==============================================================

def _latest(df, *names) -> Optional[float]:
    """
    pandas DataFrame（BS/IS/CF）から最新期の値を取得する汎用ヘルパー。
    複数のラベル候補を順番に試し、最初に見つかった非 NaN 値を返す。
    """
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            vals = df.loc[n].dropna()
            if not vals.empty:
                return float(vals.iloc[0])
    return None


# ==============================================================
# SECTION 6: アラート判定
# ==============================================================

def check_alert(metric_key: str, value, thresholds: dict) -> Optional[str]:
    """
    指標値が閾値を満たすか判定する。
    _max 系: value < threshold → "ok"（割安）、超過 → "ng"（割高）
    _min 系: value > threshold → "ok"（優良）、未達 → "ng"（要注意）
    返値: "ok" / "ng" / None（データなし）
    """
    if value is None:
        return None
    th = thresholds.get(metric_key)
    if th is None:
        return None
    try:
        v = float(value)
        if metric_key.endswith("_max"):
            return "ok" if v < th else "ng"
        if metric_key.endswith("_min"):
            return "ok" if v > th else "ng"
    except (TypeError, ValueError):
        pass
    return None


def _th_hint(key: str, thresholds: dict) -> str:
    """カードのサブテキストに表示する閾値ヒント文字列を生成"""
    th = thresholds.get(key)
    if th is None:
        return ""
    return f"閾値: {th*100:.1f}%" if key in PCT_KEYS else f"閾値: {th:.2f}"


# ==============================================================
# SECTION 7: 外部リンク URL 生成
# ==============================================================

def get_google_finance_url(ticker: str, exchange: str = "") -> str:
    """Google Finance の銘柄ページ URL を生成"""
    base = "https://www.google.com/finance/quote"
    if is_japan_stock(ticker):
        return f"{base}/{ticker.upper().replace('.T', '')}:TYO"
    ex_map = {
        "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ",
        "NYQ": "NYSE",   "NYA": "NYSE",   "ASE": "NYSE",
    }
    return f"{base}/{ticker}:{ex_map.get(exchange, exchange or 'NASDAQ')}"


def get_yahoo_finance_url(ticker: str) -> str:
    """Yahoo Finance（JP/US）の銘柄ページ URL を生成"""
    if is_japan_stock(ticker):
        return f"https://finance.yahoo.co.jp/quote/{ticker.upper()}"
    return f"https://finance.yahoo.com/quote/{ticker.upper()}"
