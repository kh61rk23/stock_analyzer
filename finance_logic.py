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


def get_default_thresholds() -> dict[str, float]:
    """ビューアの設定シート初期値を、新しい辞書として返す。"""
    return {
        "per_max":           15.0,
        "pbr_max":            1.5,
        "psr_max":            5.0,
        "pcfr_max":          20.0,
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


def as_number(v) -> Optional[float]:
    """数値なら float で返す。bool・None・NaN・Inf・非数値は None。

    ★ isinstance(v, (int, float)) で判定してはいけない。
      numpy.float32 / numpy.int64 / numpy.int32 は Python の int/float の
      サブクラスではないため isinstance が False になり、pandas 由来の値
      （.sum() / .mean() の戻り値など）が丸ごと弾かれ、ml/dataset_builder の
      特徴量が0行になる。
      float() での変換は numpy 型も通るため、判定は変換の成否で行う。

    bool を弾くのは、True/False が int のサブクラスであり
    特徴量やスコアとして混入すると 1.0/0.0 に化けるため。
    文字列も弾く（"80" のような値が数値として紛れ込むと、
    型の取り違えを隠してしまう）。文字列を変換したい場合は
    _safe_float を直接使うこと。
    """
    if v is None or isinstance(v, (bool, str, bytes)):
        return None
    return _safe_float(v)


def _safe_int(v) -> Optional[int]:
    """None / 空文字を安全に int へ変換。変換不能は None を返す"""
    try:
        return int(float(v)) if v not in (None, "None", "-", "") else None
    except (ValueError, TypeError):
        return None


# 東証の証券コード。4桁数字に加え、2桁目と4桁目に英字が入る新しい形（130A・1A2B 等）がある。
_JP_CODE_RE = r"\d[0-9A-Z]\d[0-9A-Z]"


def is_jp_code(code: str) -> bool:
    """4文字の東証証券コード（英字入りを含む）か。"""
    import re
    return bool(re.fullmatch(_JP_CODE_RE, str(code or "").strip().upper()))


def normalize_ticker(raw: str) -> str:
    """
    入力ティッカーを正規化する。
    東証の証券コード（4桁数字・英字入りの新コード）→ 末尾に .T を付与してTOKYO市場扱い
    """
    s = raw.strip().upper()
    return f"{s}.T" if s.isdigit() or is_jp_code(s) else s



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
    if is_jp_code(clean):
        return True, True, f"{clean}.T", ""
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
    """ティッカーが日本株（.T 末尾・数字のみ・英字入りの東証コード）かどうかを判定"""
    return ticker.upper().endswith(".T") or ticker.isdigit() or is_jp_code(ticker)


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


def _yf_yield_ratio(info: dict) -> Optional[float]:
    """yfinance の info から配当利回りを比率（0.04 = 4%）で返す。

    yfinance の dividendYield は常に「%値」（0.19 = 0.19%）で返る。
    比率で返る trailingAnnualDividendYield を優先し、無ければ %値として 1/100 する。
    """
    ttm = _safe_float(info.get("trailingAnnualDividendYield"))
    if ttm is not None and 0 < ttm <= 0.30:
        return ttm
    pct = _safe_float(info.get("dividendYield"))
    if pct is None:
        return None
    v = pct / 100.0
    return v if 0 <= v <= 0.30 else None


def compute_div_yield(info: dict, is_jp: bool, price) -> Optional[float]:
    """
    配当利回り計算 — 銘柄タイプ対応版

    米国株:
        ① dividendRate（年間配当額 $）/ 現在株価 を優先使用
           yfinance は US 株で dividendYield を稀に誤値（40%等）で返すため
           dividendRate から算出するほうが正確
        ② フォールバック: _yf_yield_ratio()（trailingAnnualDividendYield 優先）
    日本株:
        _yf_yield_ratio()
    """
    # 日本株に fix_div_yield() は使わない。「1.0 以下なら比率」判定だと、%値で返る
    # 1% 未満の銘柄が100倍に誤読される（0.19% → 19.0%）。
    # fix_div_yield は Alpha Vantage の比率値にも使うため関数自体は変えない。
    if is_jp:
        return _yf_yield_ratio(info)

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

    # 優先②: yfinance の利回り欄（%値。0.30 以下を比率とみなす旧判定は同じ誤読を起こす）
    return _yf_yield_ratio(info)


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


# ==============================================================
# SECTION 8: 統計ユーティリティ（U-7 共通化）
# ==============================================================

def pearson(
    xs: list,
    ys: list,
    *,
    min_n: int = 2,
    round_digits: Optional[int] = None,
) -> Optional[float]:
    """
    2リストの Pearson 相関係数を計算する（numpy 不使用・標準ライブラリのみ）。

    correlation_heatmap.py / jp_us_sector_bridge.py /
    jp_us_sector_correlation.py / rule_optimizer.py にあった 4つの
    `_pearson` 実装を統合したもの（U-7）。

    4実装は最低データ点数（3 / 5 / 20）と丸め桁数（3 / 4）が異なっていたため、
    その2点は呼び出し元ごとにキーワード引数で明示指定させることで、
    各ファイルの既存の挙動を変えずに委譲できるようにしてある。
    （xs/ys の位置合わせは全呼び出し元で常に等長リストが渡されるため、
      先頭 n 個・末尾 n 個のどちらを採用しても実結果は変わらない。
      本実装は先頭 n 個を採用する。）

    Args:
        xs, ys:        比較する数値リスト（長さが異なる場合は短い方に合わせる）
        min_n:          計算に必要な最低データ点数（未満は None を返す）
        round_digits:   丸め桁数。None なら丸めない

    Returns:
        相関係数（-1.0〜1.0）。データ不足 or 分散0の場合は None。
        NaN が混入した場合は判定式が NaN を捕捉できず NaN を返す
        （4実装とも同じ挙動であり、本移設で新たに導入したものではない）。
    """
    n = min(len(xs), len(ys))
    if n < min_n:
        return None
    xs = xs[:n]
    ys = ys[:n]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx < 1e-9 or dy < 1e-9:
        return None
    r = num / (dx * dy)
    return round(r, round_digits) if round_digits is not None else r


def linear_slope(
    values: list,
    *,
    min_n: int = 3,
    round_digits: int = 6,
) -> float:
    """等間隔の値列を最小二乗回帰し、1ステップ当たりの傾きを返す。

    sector_rotation_detector._calc_slope と
    jp_us_sector_correlation._calc_trend の共通実装。
    データ不足・分母0は0.0を返す。
    """
    n = len(values)
    if n < min_n:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    numerator = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    denominator = sum((x - mx) ** 2 for x in xs)
    if denominator < 1e-9:
        return 0.0
    return round(numerator / denominator, round_digits)
