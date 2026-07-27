# ==============================================================
# independence_score_calculator.py  v1.0  (Wave 7 - Step 7)
# 市場独立性スコア計算モジュール
# ==============================================================
# 【役割】
#   ローリング回帰（OLS: Ordinary Least Squares）により
#   銘柄の市場インデックスに対する独立性スコアを計算し、
#   independence_scores.json に保存する。
#   加えて、EDINETテキスト分析によるカタリスト候補の自動特定ロジックを持つ。
#   JP株に対して単因子OLS回帰（Ph1）または2因子OLS回帰（Ph2）を実行し、
#   「セクターの動きで説明されない独立リターン成分（残差ε）」を定量化する。
#   高ε（独立上昇）・低β_sector（セクター非依存）の銘柄をIndependence Scoreで評価し、
#   latent_pearl_manager.py へ候補を渡す。
#
# 【設計思想】
#   ・市場との相関が低い銘柄 → 市場全体の下落に連動しにくい
#   ・低β + 高独立性 + 高α → 「固有の上昇要因」を持つ真の独自株
#   ・EDINETの「新規事業」「研究開発」テキストとの組み合わせで
#     カタリスト候補を自動特定する
#
# 【スコア計算】
#   $\\beta = \\frac{Cov(r_i, r_m)}{Var(r_m)}$ （ローリング60日）
#   $\\alpha = \\bar{r_i} - \\beta \\times \\bar{r_m}$ （年率換算）
#   $independence\\_score = (1 - |\\beta|) \\times 50 + \\alpha\\_score \\times 50$
#   $\\alpha\\_score = \\min(1.0, \\max(0, \\alpha_{annual} / 0.30))$
#
# 【カタリスト候補自動特定（EDINET連携）】
#   「新規事業」「研究開発」「M&A」「事業譲渡」「中計上方修正」等の
#   キーワードをEDINETテキストから検出し、catalyst_flags を付与する。
#
# 【主要出力キャッシュ】
#   CACHE_DIR/independence_scores.json
#   TTL: 30日（日次更新）
#   スキーマ:
#     {
#       "last_updated": "YYYY-MM-DD",
#       "version": "1.0",
#       "scores": {
#         ticker: {
#           "ticker": str,
#           "beta_60d": float,
#           "alpha_annual": float,
#           "r_squared": float,
#           "independence_score": float,   # 0.0 ~ 100.0
#           "independence_tier": str,      # "HIGH"/"MID"/"LOW"
#           "catalyst_flags": list,
#           "catalyst_score": float,       # 0.0 ~ 10.0
#           "combined_score": float,       # independence × catalyst補正
#           "data_days": int,
#           "last_calculated": str,
#         }
#       },
#       "stats": { ... }
#     }
#
# 【依存】
#   yfinance（価格データ取得）
#   config.py（Wave 7追加定数）
#   EDINET rotation cache（カタリスト検出用テキスト）
#   ★ try/except で全ブロックを保護

# 【Ph1: JP単因子回帰（Wave 3 - Independence Ph1）】
#   $R_{stock} = \alpha + \beta_{jp\_sector} \times R_{sector} + \epsilon$
#   セクター代表銘柄のリターンを単一ファクターとして使用。
#
# 【Ph2: 2因子回帰（Wave 3 - Independence Ph2）】
#   $R_{stock} = \alpha + \beta_{jp} \times R_{jp\_sector} + \beta_{us} \times R_{us\_etf} + \epsilon$
#   JP_US_AFFINITY の US ETF リターンを第2ファクターとして追加。
#
# 【Independence Score 設計（0〜100点）】
#   成分1: 直近30日平均残差εのプラス方向スコア（max 60pt）
#   成分2: β_sector の低さ（セクター非依存度）（max 40pt）
#   $independence\_score = \epsilon\_score + \beta\_score$
#
# 【Latent Pearl 候補判定】
#   $independence\_score \ge 70$ → latent_pearl_manager へ登録候補
#
# 【生成キャッシュ】
#   independence_scores.json  TTL: 3日
#
# 【呼び出しパターン（stock_screener.py から）】
#   from independence_score_calculator import (
#       calc_independence_score, batch_calc_independence_scores,
#       load_independence_scores,
#   )
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: デフォルト定数
# ==============================================================

_CACHE_FILENAME   = "independence_scores.json"
_DEFAULT_TTL_DAYS = 30

# ローリング回帰ウィンドウ（日数）
_ROLLING_WINDOW_DAYS = 60

# インデックスティッカー
_INDEX_TICKER_JP = "^N225"   # 日経225
_INDEX_TICKER_US = "^GSPC"   # S&P500

# 独立性スコアのティア閾値
_INDEPENDENCE_TIERS = {
    "HIGH": 65.0,
    "MID":  40.0,
    "LOW":  0.0,
}

# α年率換算基準（30%を満点とする）

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'ISC_ALPHA_SCORE_MAX_ANNUAL': '_ALPHA_SCORE_MAX_ANNUAL',
    'ISC_CATALYST_SCORE_MAX': '_CATALYST_SCORE_MAX',
    'ISC_COMBINED_WEIGHTS': '_COMBINED_WEIGHTS',
    'B4_INDEPENDENCE_SCORE_CALCULATOR_ROLLING_WINDOW_DAYS': '_ROLLING_WINDOW_DAYS',
}

_ALPHA_SCORE_MAX_ANNUAL = 0.30

# カタリスト検出キーワード辞書
# {keyword: (weight, catalyst_type)}
_CATALYST_KEYWORDS: dict = {
    # 事業拡大系
    "新規事業":        (2.0, "new_business"),
    "新サービス":      (1.5, "new_business"),
    "新製品":          (1.5, "new_product"),
    "事業拡大":        (1.5, "new_business"),
    "海外展開":        (2.0, "expansion"),
    "新市場":          (1.5, "expansion"),
    # R&D系
    "研究開発":        (1.5, "rd_investment"),
    "R&D":             (1.5, "rd_investment"),
    "特許":            (1.0, "rd_investment"),
    "技術開発":        (1.5, "rd_investment"),
    "共同研究":        (2.0, "rd_collaboration"),
    # M&A系
    "買収":            (2.5, "ma"),
    "合併":            (2.5, "ma"),
    "M&A":             (2.5, "ma"),
    "資本業務提携":    (2.0, "partnership"),
    "業務提携":        (1.5, "partnership"),
    "合弁":            (1.5, "partnership"),
    # 財務改善系
    "自社株買い":      (2.0, "shareholder_return"),
    "増配":            (2.0, "shareholder_return"),
    "特別配当":        (2.5, "shareholder_return"),
    "資本効率":        (1.5, "capital_efficiency"),
    # ガバナンス系
    "中期経営計画":    (1.5, "midterm_plan"),
    "上方修正":        (3.0, "guidance_up"),
    "業績予想修正":    (2.5, "guidance_revision"),
    "黒字転換":        (3.0, "turnaround"),
    "構造改革":        (2.0, "restructuring"),
    # 技術・規制系
    "承認":            (2.0, "regulatory_approval"),
    "認証":            (1.5, "regulatory_approval"),
    "量産":            (2.5, "mass_production"),
    "受注":            (2.0, "order_win"),
    "大型受注":        (3.0, "order_win"),
}

# カタリストスコアの最大値
_CATALYST_SCORE_MAX = 10.0

# 独立性スコアとカタリストスコアの組み合わせ重み
_COMBINED_WEIGHTS = {
    "independence": 0.70,
    "catalyst":     0.30,
}

# ==============================================================
# BLOCK 0-B: Wave 3互換アクセサ（stock_screener.py連携用）
# ==============================================================

def batch_calc_independence_scores(
    candidates: list,
    cfg=None,
) -> dict:
    """
    Wave 3互換: 複数銘柄の独立性スコアをバッチ計算する。
    Wave 7版 calc_independence_score_for_ticker を内部で呼び出す。

    Args:
        candidates: [{"ticker": str, "sector_code": str, ...}, ...]
        cfg:        config モジュール

    Returns:
        {ticker: {"independence_score": float, "phase": str, ...}}
    """
    results: dict = {}
    for c in candidates:
        ticker = c.get("ticker", "")
        market = "US" if not str(ticker).endswith(".T") else "JP"
        if not ticker:
            continue
        try:
            # まずキャッシュを参照
            cached_score = get_independence_score(ticker, cfg)
            if cached_score is not None:
                results[ticker] = {
                    "independence_score": cached_score,
                    "combined_score":     get_combined_score(ticker, cfg) or 0.0,
                    "catalyst_flags":     get_catalyst_flags(ticker, cfg) or [],
                    "phase":              "cached",
                }
            else:
                # キャッシュなし → 個別計算
                score_data = calc_independence_score_for_ticker(ticker, market, cfg)
                results[ticker] = score_data
        except Exception:
            results[ticker] = {
                "independence_score": 0.0,
                "combined_score":     0.0,
                "catalyst_flags":     [],
                "phase":              "error",
            }
    return results


def get_latent_pearl_candidates(
    scores: dict,
    threshold: float = 65.0,
) -> list:
    """
    Wave 3互換: independence_score が閾値以上の潜伏Pearl候補を返す。

    Args:
        scores:    batch_calc_independence_scores() の返り値
        threshold: 独立性スコアの閾値

    Returns:
        [{"ticker": str, "independence_score": float, ...}, ...]
    """
    candidates = [
        {"ticker": ticker, **data}
        for ticker, data in scores.items()
        if float(data.get("independence_score", 0)) >= threshold
    ]
    return sorted(candidates, key=lambda x: x["independence_score"], reverse=True)


def calc_independence_score(
    ticker: str,
    sector_code: str = "",
    cfg=None,
) -> dict:
    """
    Wave 3互換シグネチャ: 単一銘柄の独立性スコアを計算して返す。
    内部では Wave 7版 calc_independence_score_for_ticker を呼び出す。

    Returns:
        {"independence_score": float, "phase": str, "ticker": str, ...}
    """
    market = "US" if not str(ticker).endswith(".T") else "JP"
    return calc_independence_score_for_ticker(ticker, market, cfg)


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / _CACHE_FILENAME


def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        last_updated = data.get("last_updated", "")
        ttl = int(getattr(cfg, "INDEPENDENCE_SCORE_TTL_DAYS", _DEFAULT_TTL_DAYS))
        if last_updated:
            age = (datetime.now() - datetime.strptime(last_updated, "%Y-%m-%d")).days
            if age < ttl:
                return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    try:
        data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        p = _cache_path(cfg)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  [independence] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: OLS ローリング回帰計算
# ==============================================================

def _fetch_price_returns(
    ticker: str,
    index_ticker: str,
    window_days: int = _ROLLING_WINDOW_DAYS,
) -> tuple:
    """
    yfinance から銘柄・インデックスの日次リターン系列を取得する。

    Returns:
        (stock_returns: list[float], index_returns: list[float], data_days: int)
        取得失敗時は ([], [], 0)
    """
    try:
        import yfinance as yf
        end   = datetime.now()
        start = end - timedelta(days=int(window_days * 1.5))  # バッファ込みで取得

        tk  = yf.Ticker(ticker)
        idx = yf.Ticker(index_ticker)

        hist_tk  = tk.history(start=start, end=end, interval="1d")
        hist_idx = idx.history(start=start, end=end, interval="1d")

        if hist_tk.empty or hist_idx.empty:
            return [], [], 0

        # 日付インデックスで結合
        close_tk  = hist_tk["Close"]
        close_idx = hist_idx["Close"]
        aligned   = close_tk.to_frame("stock").join(
            close_idx.to_frame("index"), how="inner"
        )

        if len(aligned) < 20:
            return [], [], 0

        # 日次リターンを計算
        aligned["r_stock"] = aligned["stock"].pct_change()
        aligned["r_index"] = aligned["index"].pct_change()
        aligned = aligned.dropna()

        # 最新 window_days 件に絞る
        aligned = aligned.tail(window_days)

        stock_rets = aligned["r_stock"].tolist()
        index_rets = aligned["r_index"].tolist()

        return stock_rets, index_rets, len(aligned)

    except ImportError:
        return [], [], 0
    except Exception:
        return [], [], 0


def _ols_beta_alpha(
    stock_returns: list,
    index_returns: list,
) -> tuple:
    """
    OLS 単純回帰で β・α・R² を計算する。

    $\\beta = \\frac{\\sum (r_m - \\bar{r_m})(r_i - \\bar{r_i})}{\\sum (r_m - \\bar{r_m})^2}$
    $\\alpha = \\bar{r_i} - \\beta \\times \\bar{r_m}$
    $R^2 = 1 - \\frac{SS_{res}}{SS_{tot}}$

    外部ライブラリ不使用（numpy なしの純 Python 実装）。

    Returns:
        (beta: float, alpha_daily: float, r_squared: float)
        計算失敗時は (1.0, 0.0, 0.0)
    """
    try:
        n = len(stock_returns)
        if n < 10:
            return 1.0, 0.0, 0.0

        mean_r = sum(index_returns) / n
        mean_i = sum(stock_returns) / n

        # Cov(r_i, r_m) と Var(r_m)
        cov_num = sum(
            (index_returns[k] - mean_r) * (stock_returns[k] - mean_i)
            for k in range(n)
        )
        var_denom = sum((index_returns[k] - mean_r) ** 2 for k in range(n))

        if var_denom == 0:
            return 1.0, 0.0, 0.0

        beta        = cov_num / var_denom
        alpha_daily = mean_i - beta * mean_r

        # R² 計算
        fitted   = [alpha_daily + beta * index_returns[k] for k in range(n)]
        ss_res   = sum((stock_returns[k] - fitted[k]) ** 2 for k in range(n))
        ss_tot   = sum((stock_returns[k] - mean_i) ** 2 for k in range(n))
        r_sq     = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        return round(beta, 6), round(alpha_daily, 8), round(max(0.0, r_sq), 6)

    except Exception:
        return 1.0, 0.0, 0.0


def _calc_independence_score(
    beta: float,
    alpha_annual: float,
) -> float:
    """
    β と α から独立性スコアを計算する。

    $independence\\_score = (1 - |\\beta|) \\times 50 + \\alpha\\_score \\times 50$
    $\\alpha\\_score = \\min(1.0, \\max(0, \\alpha_{annual} / 0.30))$

    β が低い（市場に連動しない）ほど、
    α が高い（市場超過リターン）ほどスコアが高くなる。

    Returns:
        float 0.0 ~ 100.0
    """
    try:
        beta_term   = max(0.0, 1.0 - abs(beta)) * 50.0
        alpha_score = min(1.0, max(0.0, alpha_annual / _ALPHA_SCORE_MAX_ANNUAL))
        alpha_term  = alpha_score * 50.0
        score       = beta_term + alpha_term
        return round(min(100.0, max(0.0, score)), 2)
    except Exception:
        return 0.0


def _judge_independence_tier(score: float, cfg=None) -> str:
    """独立性スコアからティアを判定する。

    2026-07-25 バッチ5: 閾値を config.INDEPENDENCE_TIER_THRESHOLDS から読む。
    従来はモジュール定数 _INDEPENDENCE_TIERS のみが効いており、config 側
    （同義・同値）は誰にも読まれていなかった。
    config 側は HIGH / MID の2キーのみ（LOW は「それ以外」なので不要）。
    """
    tiers = getattr(cfg, "INDEPENDENCE_TIER_THRESHOLDS", None) or _INDEPENDENCE_TIERS
    if score >= float(tiers.get("HIGH", _INDEPENDENCE_TIERS["HIGH"])):
        return "HIGH"
    elif score >= float(tiers.get("MID", _INDEPENDENCE_TIERS["MID"])):
        return "MID"
    else:
        return "LOW"


# ==============================================================
# BLOCK 3: EDINETカタリスト検出
# ==============================================================

def _load_edinet_text_for_ticker(ticker: str, cfg) -> str:
    """
    EDINET ローテーションキャッシュから銘柄のテキストを読み込む。

    edinet_rotation_cache.json の companies[company_code].texts を参照。
    company_code: ticker の ".T" 除去部分（例: "6758.T" → "6758"）
    """
    try:
        cache_dir    = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
        rotation_p   = cache_dir / "edinet_rotation_cache.json"
        if not rotation_p.exists():
            return ""

        rotation = json.loads(rotation_p.read_text(encoding="utf-8"))
        company_code = ticker.split(".")[0] if "." in ticker else ticker
        company_data = rotation.get("companies", {}).get(company_code, {})

        # texts フィールドが文字列リストの場合を想定
        texts = company_data.get("texts", [])
        if isinstance(texts, list):
            return " ".join(str(t) for t in texts)
        elif isinstance(texts, str):
            return texts
        return ""
    except Exception:
        return ""


def _detect_catalysts(text: str) -> tuple:
    """
    テキストからカタリストキーワードを検出し、スコアとフラグリストを返す。

    $catalyst\\_score = \\min(10.0, \\sum_{k} weight_k \\times count_k)$

    Returns:
        (catalyst_score: float, catalyst_flags: list[str])
    """
    try:
        if not text:
            return 0.0, []

        total_weight = 0.0
        flags = []
        detected_types: set = set()

        for keyword, (weight, c_type) in _CATALYST_KEYWORDS.items():
            count = text.count(keyword)
            if count > 0:
                # 同一カタリスト種別は重複加算しない（最大1回）
                if c_type not in detected_types:
                    total_weight += weight
                    detected_types.add(c_type)
                flags.append(f"{keyword}（×{count}回）")

        catalyst_score = round(min(_CATALYST_SCORE_MAX, total_weight), 2)
        return catalyst_score, flags[:10]  # フラグ上限10件

    except Exception:
        return 0.0, []


def _calc_combined_score(
    independence_score: float,
    catalyst_score: float,
) -> float:
    """
    独立性スコアとカタリストスコアを組み合わせた複合スコアを計算する。

    $combined = independence \\times 0.70 + (catalyst / 10) \\times 100 \\times 0.30$

    Returns:
        float 0.0 ~ 100.0
    """
    try:
        w = _COMBINED_WEIGHTS
        ind_term = independence_score * w["independence"]
        cat_term = (catalyst_score / _CATALYST_SCORE_MAX) * 100.0 * w["catalyst"]
        return round(min(100.0, max(0.0, ind_term + cat_term)), 2)
    except Exception:
        return 0.0


# ==============================================================
# BLOCK 4: 1銘柄の独立性スコア計算フロー
# ==============================================================

def calc_independence_score_for_ticker(
    ticker: str,
    market: str = "JP",
    cfg=None,
) -> dict:
    """
    1銘柄の独立性スコアを計算する。

    処理:
      1. yfinance から価格リターンを取得
      2. OLS 回帰で β・α・R² を計算
      3. 独立性スコアを計算
      4. EDINET テキストからカタリスト検出
      5. 複合スコアを計算

    Returns:
        完全な score_dict
    """
    index_ticker = _INDEX_TICKER_US if market == "US" else _INDEX_TICKER_JP
    today_str    = datetime.now().strftime("%Y-%m-%d")

    # 価格データ取得
    # 2026-07-25 バッチ5: 観測窓を config.INDEPENDENCE_ROLLING_WINDOW_DAYS から読む
    # （従来はモジュール定数 _ROLLING_WINDOW_DAYS のみが効いていた）
    stock_rets, index_rets, data_days = _fetch_price_returns(
        ticker, index_ticker,
        window_days=int(getattr(cfg, "INDEPENDENCE_ROLLING_WINDOW_DAYS",
                                _ROLLING_WINDOW_DAYS)),
    )

    # OLS 計算
    if data_days >= 20:
        beta, alpha_daily, r_squared = _ols_beta_alpha(stock_rets, index_rets)
        alpha_annual = alpha_daily * 252  # 年率換算（252営業日）
    else:
        beta, alpha_daily, r_squared = 1.0, 0.0, 0.0
        alpha_annual = 0.0

    # 独立性スコア
    indep_score = _calc_independence_score(beta, alpha_annual)
    indep_tier  = _judge_independence_tier(indep_score, cfg)

    # EDINET カタリスト検出（JP のみ）
    catalyst_score = 0.0
    catalyst_flags = []
    if market == "JP":
        edinet_text = _load_edinet_text_for_ticker(ticker, cfg)
        catalyst_score, catalyst_flags = _detect_catalysts(edinet_text)

    # 複合スコア
    combined_score = _calc_combined_score(indep_score, catalyst_score)

    return {
        "ticker":              ticker,
        "market":              market,
        "beta_60d":            round(beta, 4),
        "alpha_annual":        round(alpha_annual, 6),
        "alpha_annual_pct":    round(alpha_annual * 100, 2),
        "r_squared":           round(r_squared, 4),
        "independence_score":  indep_score,
        "independence_tier":   indep_tier,
        "catalyst_flags":      catalyst_flags,
        "catalyst_score":      catalyst_score,
        "combined_score":      combined_score,
        "data_days":           data_days,
        "last_calculated":     today_str,
    }


# ==============================================================
# BLOCK 5: バッチ計算フロー
# ==============================================================

def _get_target_tickers(cfg) -> list:
    """
    size_tier_master.json から評価対象銘柄リストを取得する。
    Nano銘柄は nano_evaluation_queue.json から取得する。
    """
    tickers = []
    try:
        p = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "size_tier_master.json"
        if p.exists():
            master = json.loads(p.read_text(encoding="utf-8"))
            # Large/Mid/Small/Micro のみ全件評価
            for mkey in ("jp", "us"):
                for ticker, entry in master.get(mkey, {}).items():
                    tier = entry.get("size_tier", "Nano")
                    if tier != "Nano":
                        market = mkey.upper()
                        tickers.append({"ticker": ticker, "market": market})
    except Exception:
        pass

    # Nano は評価キューから
    try:
        qp = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "nano_evaluation_queue.json"
        if qp.exists():
            queue_data = json.loads(qp.read_text(encoding="utf-8"))
            for item in queue_data.get("to_evaluate_next", []):
                if isinstance(item, str):
                    tickers.append({"ticker": item, "market": "JP"})
                elif isinstance(item, dict):
                    tickers.append({
                        "ticker": item.get("ticker", ""),
                        "market": item.get("market", "JP"),
                    })
    except Exception:
        pass

    return tickers


# ==============================================================
# BLOCK 6: 外部参照用アクセサ
# ==============================================================

def get_independence_score(ticker: str, cfg=None) -> Optional[float]:
    """
    銘柄の独立性スコアを independence_scores.json から取得する。
    latent_pearl_manager.py / cross_size_signal.py から参照。

    Returns:
        float 0.0〜100.0 または None
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return None
        return cache.get("scores", {}).get(ticker, {}).get("independence_score")
    except Exception:
        return None


def get_combined_score(ticker: str, cfg=None) -> Optional[float]:
    """
    銘柄の複合スコア（独立性×カタリスト）を返す。

    Returns:
        float 0.0〜100.0 または None
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return None
        return cache.get("scores", {}).get(ticker, {}).get("combined_score")
    except Exception:
        return None


def get_catalyst_flags(ticker: str, cfg=None) -> list:
    """
    銘柄のカタリストフラグリストを返す。

    Returns:
        ["上方修正（×2回）", ...] 形式のリスト
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return []
        return cache.get("scores", {}).get(ticker, {}).get("catalyst_flags", [])
    except Exception:
        return []


def get_high_independence_tickers(
    cfg=None,
    min_score: float = 65.0,
    top_n: int = 50,
) -> list:
    """
    独立性スコアが高い銘柄リストを返す。
    latent_pearl_manager.py の潜伏銘柄候補選定に使用。

    Returns:
        [{"ticker": str, "independence_score": float, ...}, ...]
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return []
        scores = cache.get("scores", {})
        filtered = [
            v for v in scores.values()
            if float(v.get("independence_score", 0)) >= min_score
        ]
        filtered.sort(key=lambda x: x.get("combined_score", 0), reverse=True)
        return filtered[:top_n]
    except Exception:
        return []


# ==============================================================
# BLOCK 7: メイン実行フロー
# ==============================================================

def run_independence_score_calculator(
    cfg=None,
    force_refresh: bool = False,
    max_tickers: int = 200,
) -> dict:
    """
    stock_analyzer_main.py の DAILY_MODULES から呼ばれるエントリポイント。

    処理フロー:
      1. キャッシュ有効 → 返却
      2. size_tier_master + nano_queue から対象銘柄取得
      3. yfinance で価格データ取得 & OLS 計算
      4. EDINET テキストからカタリスト検出
      5. independence_scores.json に保存

    Args:
        max_tickers: 1回の実行で処理する最大銘柄数

    Returns:
        {
          "scores":    dict,
          "stats":     dict,
          "cached":    bool,
          "timestamp": str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📐 市場独立性スコア計算（Wave 7 - Step 7）実行中...")

    # ── キャッシュ確認 ──────────────────────────────────────────
    if not force_refresh:
        cached = _load_cache(cfg)
        if cached:
            stats = cached.get("stats", {})
            print(
                f"    ✅ キャッシュ使用: "
                f"銘柄={stats.get('total_tickers', 0)} / "
                f"HIGH独立={stats.get('high_independence_count', 0)}"
            )
            return {
                "scores":    cached.get("scores", {}),
                "stats":     stats,
                "cached":    True,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    # ── 対象銘柄取得 ──────────────────────────────────────────
    target_list = _get_target_tickers(cfg)
    target_list = target_list[:max_tickers]
    print(f"    📋 評価対象: {len(target_list)} 銘柄")

    if not target_list:
        print("    ⚠️  対象銘柄なし → スキップ")
        return {
            "scores":    {},
            "stats":     {},
            "cached":    False,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    all_scores: dict = {}

    for i, item in enumerate(target_list, 1):
        ticker = item.get("ticker", "")
        market = item.get("market", "JP")
        if not ticker:
            continue
        try:
            score_data = calc_independence_score_for_ticker(ticker, market, cfg)
            all_scores[ticker] = score_data

            if i % 20 == 0:
                print(f"       進捗: {i}/{len(target_list)} 銘柄処理済み")

        except Exception as e:
            print(f"    ⚠️  [{ticker}] スコア計算エラー: {str(e)[:50]}")
            continue

    # ── 統計集計 ─────────────────────────────────────────────
    tier_counts = {"HIGH": 0, "MID": 0, "LOW": 0}
    for entry in all_scores.values():
        t = entry.get("independence_tier", "LOW")
        tier_counts[t] = tier_counts.get(t, 0) + 1

    top_combined = sorted(
        all_scores.values(),
        key=lambda x: x.get("combined_score", 0),
        reverse=True,
    )[:5]

    avg_beta = (
        sum(abs(v.get("beta_60d", 1.0)) for v in all_scores.values())
        / max(1, len(all_scores))
    )

    stats = {
        "total_tickers":          len(all_scores),
        "high_independence_count": tier_counts.get("HIGH", 0),
        "mid_independence_count":  tier_counts.get("MID", 0),
        "low_independence_count":  tier_counts.get("LOW", 0),
        "avg_beta_abs":            round(avg_beta, 4),
        "top_5_combined": [
            {"ticker": v["ticker"], "combined_score": v["combined_score"]}
            for v in top_combined
        ],
    }

    # ── 保存 ─────────────────────────────────────────────────
    cache_data = {
        "scores":  all_scores,
        "stats":   stats,
        "version": "1.0",
    }
    _save_cache(cfg, cache_data)

    print(
        f"    ✅ 独立性スコア完了: "
        f"銘柄={len(all_scores)} / "
        f"HIGH={tier_counts.get('HIGH',0)} / "
        f"平均|β|={avg_beta:.3f}"
    )
    if top_combined:
        top_str = " / ".join(
            f"{v['ticker']}:{v['combined_score']:.1f}" for v in top_combined[:3]
        )
        print(f"    🏆 複合スコア上位3: {top_str}")

    return {
        "scores":    all_scores,
        "stats":     stats,
        "cached":    False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 8: 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  📐 independence_score_calculator.py v1.0 単体テスト")
    print("=" * 60)

    # ── OLS 計算テスト（合成データ）──────────────────────────
    print("\n  【OLS 回帰テスト（合成データ）】")
    import random
    random.seed(42)
    n = 60
    idx_rets = [random.gauss(0.0005, 0.012) for _ in range(n)]

    # β=0.3（低β・独立性高）の合成株
    low_beta_rets  = [0.3 * r + random.gauss(0.001, 0.008) for r in idx_rets]
    # β=1.5（高β・独立性低）の合成株
    high_beta_rets = [1.5 * r + random.gauss(-0.0005, 0.010) for r in idx_rets]

    for label, rets in [("低β株(β≈0.3)", low_beta_rets), ("高β株(β≈1.5)", high_beta_rets)]:
        beta, alpha_d, r2 = _ols_beta_alpha(rets, idx_rets)
        alpha_y = alpha_d * 252
        indep   = _calc_independence_score(beta, alpha_y)
        tier    = _judge_independence_tier(indep)
        print(
            f"    {label}: β={beta:.4f} α年率={alpha_y*100:.2f}% "
            f"R²={r2:.4f} 独立性={indep:.1f}({tier})"
        )

    # ── カタリスト検出テスト ────────────────────────────────
    print("\n  【カタリスト検出テスト】")
    test_texts = [
        "当社は新規事業として海外展開を加速します。また大型受注を獲得しました。",
        "当社は上方修正を発表しました。黒字転換を見込んでいます。自社株買いも実施します。",
        "売上高は前年比で増加しました。通常の事業運営を継続しています。",
    ]
    for text in test_texts:
        score, flags = _detect_catalysts(text)
        print(f"    スコア={score:.1f} フラグ={flags}")

    # ── 複合スコアテスト ────────────────────────────────────
    print("\n  【複合スコアテスト】")
    test_cases = [
        (80.0, 8.0, "高独立×高カタリスト"),
        (70.0, 2.0, "高独立×低カタリスト"),
        (30.0, 9.0, "低独立×高カタリスト"),
        (40.0, 4.0, "中独立×中カタリスト"),
    ]
    for indep, cat, label in test_cases:
        combined = _calc_combined_score(indep, cat)
        print(f"    {label}: indep={indep} cat={cat} → combined={combined:.1f}")

    # ── アクセサテスト（キャッシュなし想定）────────────────
    print("\n  【アクセサテスト（キャッシュなし → None 返却確認）】")
    print(f"    get_independence_score('6758.T'): {get_independence_score('6758.T')}")
    print(f"    get_combined_score('NVDA')      : {get_combined_score('NVDA')}")
    print(f"    get_catalyst_flags('7203.T')    : {get_catalyst_flags('7203.T')}")

    print(f"\n  ✅ 単体テスト完了")
