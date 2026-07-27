# ==============================================================
# factor_crowding_monitor.py  v1.0  (RESEARCH-2)
# ファクター・クラウディング検知
# ==============================================================
# 【役割】
#   composite_score 上位銘柄の価格相関が急上昇した場合に
#   「ファクタークラウディング（混雑）」を検知し、
#   rule_optimizer の重み削減トリガーとして機能する。
#
# 【検知ロジック（2段階）】
#   Stage1: 単純平均相関（実装済み）
#     → screener 上位N銘柄の過去30日リターン相関の平均値
#     → avg_corr >= CROWDING_THRESHOLD（0.6）でアラート
#
#   Stage2: 残差相関（高度化版・オプション）
#     → TOPIX リターンを除去した銘柄固有の相関
#     → 市場全体のリスクオフ（ベータ上昇）との区別が可能
#     → ENABLE_RESIDUAL_CORR = True で有効化
#
# 【解消局面の検知】
#   前週 avg_corr >= 0.6 → 今週 avg_corr < 0.4 → unwind_alert = True
#   → 最も危険なタイミング（解消売り連鎖）
#
# 【ファクター別クラウディング（BLOCK 5）】
#   バリュー上位（低PER）/ モメンタム上位（高acc_bonus）等
#   でそれぞれ個別計算 → どのファクターが混雑しているかを特定
#
# 【出力スキーマ（crowding_history.json）】
#   {
#     "YYYY-WW": {
#       "avg_correlation":      float,  # Stage1 平均相関
#       "residual_avg_corr":    float,  # Stage2 残差相関（オプション）
#       "top_n_tickers":        [str],
#       "crowding_score":       int,    # 0〜100
#       "crowding_alert":       bool,
#       "unwind_alert":         bool,
#       "factor_crowding":      {factor_name: {"avg_corr", "alert"}},
#     }
#   }
#
# 【依存】yfinance / config.py / screener_history.json
# ==============================================================

from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================


from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'FCM_CROWDING_THRESHOLD': 'CROWDING_THRESHOLD',
    'FCM_UNWIND_THRESHOLD': 'UNWIND_THRESHOLD',
    'FCM_CROWDING_SCORE_CAP': 'CROWDING_SCORE_CAP',
    'B4_FACTOR_CROWDING_MONITOR_LOOKBACK_DAYS': 'LOOKBACK_DAYS',
    'B4_FACTOR_CROWDING_MONITOR_HISTORY_RETAIN_WEEKS': 'HISTORY_RETAIN_WEEKS',
    'B5_FACTOR_CROWDING_MONITOR_TOP_N_DEFAULT': 'TOP_N_DEFAULT',
    'B5_FACTOR_CROWDING_MONITOR_FETCH_DELAY': 'FETCH_DELAY',
}

CROWDING_THRESHOLD    = 0.60   # avg_corr >= これ → クラウディングアラート
UNWIND_THRESHOLD      = 0.40   # 前週高 → 今週これ未満 → 解消売りアラート
CROWDING_SCORE_CAP    = 100    # クラウディングスコア上限
TOP_N_DEFAULT         = 20     # 上位N銘柄（デフォルト）
LOOKBACK_DAYS         = 30     # リターン計算期間
FETCH_DELAY           = 0.15   # yfinance レート制限対策

HISTORY_FILE          = "crowding_history.json"
HISTORY_RETAIN_WEEKS  = 12     # 12週分保持

# TOPIX のティッカー（Stage2 残差相関用）
TOPIX_TICKER          = "^N225"   # 日経225でTOPIX代替（yfinanceで安定取得）

# ファクター別クラウディング対象（candidate のどのフィールドでソートするか）
FACTOR_DEFINITIONS = {
    "value":      {"sort_key": "per", "ascending": True,  "label": "バリュー（低PER）"},
    "momentum":   {"sort_key": "acc_bonus", "ascending": False, "label": "モメンタム（EPS加速）"},
    "quality":    {"sort_key": "quality_score", "ascending": False, "label": "クオリティ"},
    "composite":  {"sort_key": "composite_score", "ascending": False, "label": "総合スコア"},
}

# クラウディングスコア → rule_optimizer 重み削減率
CROWDING_REDUCTION_RATE = 0.85   # スコア80以上で各ファクター重みをこの比率に削減


# ==============================================================
# BLOCK 1: 上位銘柄のリターン取得
# ==============================================================

def _data_dir(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data"))


def _load_screener_history(cfg) -> dict:
    p = _data_dir(cfg) / "screener_history.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_crowding_history(cfg) -> dict:
    p = _data_dir(cfg) / HISTORY_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_crowding_history(cfg, history: dict) -> None:
    p = _data_dir(cfg) / HISTORY_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  crowding_history 保存失敗: {e}")


def _get_top_n_tickers(
    candidates:   list,
    sort_key:     str  = "composite_score",
    ascending:    bool = False,
    n:            int  = TOP_N_DEFAULT,
) -> list[str]:
    """
    candidates リストから指定フィールド順に上位N銘柄のティッカーを返す。
    pass_filters=True の銘柄のみを対象とする。
    """
    passed = [c for c in candidates if c.get("pass_filters")]
    if not passed:
        # フィルター通過がなければ全体を対象
        passed = candidates

    sorted_cands = sorted(
        [c for c in passed if c.get(sort_key) is not None],
        key=lambda x: x.get(sort_key, 0),
        reverse=(not ascending),
    )
    return [c["ticker"] for c in sorted_cands[:n] if c.get("ticker")]


def fetch_returns(
    tickers:      list[str],
    lookback_days: int = LOOKBACK_DAYS,
) -> Optional[dict]:
    """
    yfinance で各銘柄の過去 lookback_days 日の日次リターンを取得する。

    Returns:
        {ticker: [daily_return, ...]} | None
        リターンが取得できない銘柄は除外する。
    """
    try:
        import yfinance as yf
    except ImportError:
        print("    ⚠️  yfinance 未インストール → クラウディング検知スキップ")
        return None

    end   = datetime.now()
    start = end - timedelta(days=lookback_days + 10)   # 少し余裕を持つ

    try:
        df = yf.download(
            tickers,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )
    except Exception as e:
        print(f"    ⚠️  yfinance download 失敗: {str(e)[:60]}")
        return None

    if df is None or df.empty:
        return None

    # Close 列を取得（単一銘柄 or 複数銘柄で構造が変わる）
    try:
        if hasattr(df.columns, "levels"):   # MultiIndex
            close = df["Close"]
        else:
            close = df[["Close"]] if "Close" in df.columns else df
    except Exception:
        close = df

    returns_dict = {}
    for col in close.columns if hasattr(close, "columns") else [close.name]:
        try:
            series = close[col].dropna()
            if len(series) < 10:
                continue
            rets = series.pct_change().dropna().tolist()
            returns_dict[str(col)] = rets
        except Exception:
            continue

    return returns_dict if returns_dict else None


# ==============================================================
# BLOCK 2: 相関行列計算と平均相関の算出
# ==============================================================

def _pearson_corr(x: list, y: list) -> Optional[float]:
    """2リストの Pearson 相関係数（純粋 Python 実装）。"""
    n = min(len(x), len(y))
    if n < 5:
        return None
    x, y = x[:n], y[:n]
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    dx  = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    dy  = math.sqrt(sum((yi - my) ** 2 for yi in y))
    if dx < 1e-9 or dy < 1e-9:
        return None
    return num / (dx * dy)


def calc_avg_correlation(returns_dict: dict) -> Optional[float]:
    """
    銘柄間の平均相関係数を計算する（Stage1: 単純平均相関）。

    全ペアの相関係数の平均を返す。
    対角成分（自己相関=1.0）は除外する。

    Returns:
        avg_corr: float（-1〜1）| None
    """
    tickers = list(returns_dict.keys())
    n = len(tickers)
    if n < 2:
        return None

    # 全ペアの相関を計算
    corr_sum   = 0.0
    pair_count = 0
    for i in range(n):
        for j in range(i + 1, n):
            r = _pearson_corr(
                returns_dict[tickers[i]],
                returns_dict[tickers[j]],
            )
            if r is not None:
                corr_sum   += r
                pair_count += 1

    if pair_count == 0:
        return None

    return round(corr_sum / pair_count, 4)


# ==============================================================
# BLOCK 3: クラウディングスコア（0〜100）の正規化
# ==============================================================

def calc_crowding_score(avg_corr: float) -> int:
    """
    平均相関係数（-1〜1）を 0〜100 のクラウディングスコアに変換する。

    スコア定義:
      avg_corr <= 0.2 → score  0  （分散良好）
      avg_corr == 0.4 → score 40  （やや集中）
      avg_corr == 0.6 → score 70  （クラウディング警戒）
      avg_corr >= 0.8 → score 100 （極度の集中）
    """
    if avg_corr <= 0.2:
        return 0
    if avg_corr >= 0.8:
        return CROWDING_SCORE_CAP

    # 0.2〜0.8 を 0〜100 に線形変換
    score = int((avg_corr - 0.2) / 0.6 * CROWDING_SCORE_CAP)
    return min(CROWDING_SCORE_CAP, max(0, score))


# ==============================================================
# BLOCK 4: 解消局面の検知（前週比急落）
# ==============================================================

def detect_unwind_alert(
    current_avg_corr: float,
    history:          dict,
) -> bool:
    """
    前週のクラウディングが高く、今週急落した場合に True を返す。
    → 解消売り連鎖（最も危険なタイミング）を検知する。
    """
    if not history:
        return False

    # 前週のデータを取得
    sorted_weeks = sorted(history.keys(), reverse=True)
    if not sorted_weeks:
        return False

    prev_week_data = history.get(sorted_weeks[0], {})
    prev_avg_corr  = prev_week_data.get("avg_correlation")

    if prev_avg_corr is None:
        return False

    # 前週クラウディング（≥0.6）→ 今週急落（< UNWIND_THRESHOLD）
    return (prev_avg_corr >= CROWDING_THRESHOLD and
            current_avg_corr < UNWIND_THRESHOLD)


# ==============================================================
# BLOCK 5: ファクター別クラウディング分析
# ==============================================================

def calc_factor_crowding(
    candidates:      list,
    lookback_days:   int = LOOKBACK_DAYS,
    n_per_factor:    int = 10,
    cfg = None,
) -> dict:
    """
    ファクター別（バリュー/モメンタム/クオリティ/総合）に
    クラウディングスコアを個別計算する。

    Returns:
        {
          "value":     {"avg_corr": float, "score": int, "alert": bool},
          "momentum":  {...},
          "quality":   {...},
          "composite": {...},
        }
    """
    factor_results = {}

    for factor_name, factor_def in FACTOR_DEFINITIONS.items():
        sort_key  = factor_def["sort_key"]
        ascending = factor_def["ascending"]
        label     = factor_def["label"]

        tickers = _get_top_n_tickers(candidates, sort_key, ascending, n_per_factor)
        if len(tickers) < 3:
            factor_results[factor_name] = {
                "label":    label,
                "avg_corr": None,
                "score":    0,
                "alert":    False,
                "note":     "銘柄数不足",
            }
            continue

        returns_dict = fetch_returns(tickers, lookback_days)
        if not returns_dict:
            factor_results[factor_name] = {
                "label":    label,
                "avg_corr": None,
                "score":    0,
                "alert":    False,
                "note":     "リターン取得失敗",
            }
            continue

        avg_corr = calc_avg_correlation(returns_dict)
        score    = calc_crowding_score(avg_corr) if avg_corr is not None else 0
        alert    = (avg_corr is not None and avg_corr >= CROWDING_THRESHOLD)

        factor_results[factor_name] = {
            "label":    label,
            "avg_corr": avg_corr,
            "score":    score,
            "alert":    alert,
            "tickers":  tickers[:5],   # 上位5銘柄のみ記録
        }

        if alert:
            print(f"    ⚠️  [{label}] クラウディング検知: "
                  f"avg_corr={avg_corr:.3f} / score={score}")
        time.sleep(FETCH_DELAY)

    return factor_results


# ==============================================================
# BLOCK 5.5: Stage2 残差相関（オプション）
# ==============================================================

def calc_residual_avg_correlation(
    returns_dict: dict,
    lookback_days: int = LOOKBACK_DAYS,
) -> Optional[float]:
    """
    TOPIX リターンを除去した銘柄固有の残差相関の平均値を計算する。
    市場全体のリスクオフ（ベータ上昇）との区別に使用する。

    処理:
      1. TOPIX（^N225で代替）の日次リターンを取得
      2. 各銘柄リターン = β × TOPIX_リターン + ε の ε を推定
      3. ε の行列で相関を計算

    Returns:
        residual_avg_corr: float | None
    """
    # TOPIX リターン取得
    topix_rets_raw = fetch_returns([TOPIX_TICKER], lookback_days)
    if not topix_rets_raw:
        return None

    topix_rets = topix_rets_raw.get(TOPIX_TICKER, [])
    if len(topix_rets) < 10:
        return None

    # 各銘柄の残差（market-adjusted return）を計算
    residual_dict: dict[str, list] = {}
    for ticker, rets in returns_dict.items():
        n = min(len(rets), len(topix_rets))
        if n < 10:
            continue

        r_stock  = rets[-n:]
        r_market = topix_rets[-n:]

        # OLS で β を推定（純粋 Python）
        m_stock  = sum(r_stock) / n
        m_market = sum(r_market) / n
        cov_sm   = sum((r_stock[i] - m_stock) * (r_market[i] - m_market) for i in range(n))
        var_m    = sum((r_market[i] - m_market) ** 2 for i in range(n))
        beta     = cov_sm / var_m if var_m > 1e-9 else 1.0

        # 残差 = 実績リターン - β × 市場リターン
        residuals = [
            r_stock[i] - beta * r_market[i]
            for i in range(n)
        ]
        residual_dict[ticker] = residuals

    if len(residual_dict) < 2:
        return None

    return calc_avg_correlation(residual_dict)


# ==============================================================
# BLOCK 6: crowding_history.json への記録
# ==============================================================

def _current_week_key() -> str:
    """現在の週番号キー（例: "2026-W14"）を返す。"""
    today   = date.today()
    iso_cal = today.isocalendar()
    return f"{iso_cal[0]}-W{iso_cal[1]:02d}"


def _prune_history(history: dict) -> dict:
    """12週分を超えた古いエントリを削除する。"""
    sorted_keys = sorted(history.keys(), reverse=True)
    keep = sorted_keys[:HISTORY_RETAIN_WEEKS]
    return {k: history[k] for k in keep}


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_factor_crowding_monitor(cfg, results: dict = None) -> dict:
    """
    ファクター・クラウディング検知を実行する。

    stock_analyzer_main.py から WEEKLY_MODULES として呼び出す:
        _fcm = _load_local_module("factor_crowding_monitor", ...)
        fcm_result = _fcm.run_factor_crowding_monitor(cfg, results)
        results["factor_crowding"] = fcm_result

    処理:
      1. screener 候補 or screener_history から上位銘柄を取得
      2. 過去30日の日次リターンを yfinance で取得
      3. 平均相関係数を計算してクラウディングスコアを算出
      4. 前週比でアンワインドアラートを判定
      5. ファクター別クラウディングを分析
      6. crowding_history.json に記録

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の results dict
                  results["screener"]["candidates"] から上位銘柄を取得

    Returns:
        {
          "avg_correlation":   float,
          "residual_avg_corr": float | None,
          "crowding_score":    int,
          "crowding_alert":    bool,
          "unwind_alert":      bool,
          "top_tickers":       [str],
          "factor_crowding":   dict,
          "week_key":          str,
          "prev_avg_corr":     float | None,
          "timestamp":         str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_FACTOR_CROWDING", True):
        return {"skipped": True}

    print("\n  🔍 ファクター・クラウディング検知 開始...")
    t0 = datetime.now()

    results = results or {}

    # ── Step1: 対象銘柄の収集 ────────────────────────────────
    top_n = getattr(cfg, "CROWDING_TOP_N", TOP_N_DEFAULT)

    candidates = []
    screener_result = results.get("screener", {})
    if screener_result.get("candidates"):
        candidates = screener_result["candidates"]
    else:
        # screener_history から直近の候補を取得
        sh = _load_screener_history(cfg)
        if sh:
            latest_key = max(sh.keys())
            candidates = sh.get(latest_key, [])

    if not candidates:
        print("    ⚠️  候補銘柄なし → スキップ")
        return {"skipped": True, "reason": "候補銘柄なし"}

    top_tickers = _get_top_n_tickers(candidates, "composite_score", False, top_n)
    if len(top_tickers) < 3:
        print(f"    ⚠️  対象銘柄が少なすぎます（{len(top_tickers)}銘柄）→ スキップ")
        return {"skipped": True, "reason": f"銘柄数不足（{len(top_tickers)}）"}

    print(f"    対象銘柄: {len(top_tickers)}社（上位composite_score）")
    print(f"    代表: {top_tickers[:5]}...")

    # ── Step2: リターン取得 ──────────────────────────────────
    lookback = getattr(cfg, "CROWDING_LOOKBACK_DAYS", LOOKBACK_DAYS)
    returns_dict = fetch_returns(top_tickers, lookback)
    if not returns_dict or len(returns_dict) < 2:
        print("    ⚠️  リターン取得失敗 → スキップ")
        return {"skipped": True, "reason": "リターン取得失敗"}

    print(f"    リターン取得: {len(returns_dict)}銘柄（/{len(top_tickers)}銘柄）")

    # ── Step3: 平均相関とスコア算出（Stage1） ────────────────
    avg_corr    = calc_avg_correlation(returns_dict)
    score       = calc_crowding_score(avg_corr) if avg_corr is not None else 0
    alert       = (avg_corr is not None and avg_corr >= CROWDING_THRESHOLD)

    print(f"    📊 Stage1 avg_corr={avg_corr:.4f} / score={score} / alert={alert}")

    # ── Step4: Stage2 残差相関（オプション） ─────────────────
    residual_avg_corr = None
    if getattr(cfg, "ENABLE_RESIDUAL_CORR", False):
        print("    🔬 Stage2 残差相関計算中（TOPIX除去）...")
        residual_avg_corr = calc_residual_avg_correlation(returns_dict, lookback)
        if residual_avg_corr is not None:
            r_score = calc_crowding_score(residual_avg_corr)
            r_alert = residual_avg_corr >= CROWDING_THRESHOLD
            print(f"    残差相関: {residual_avg_corr:.4f} / score={r_score} / alert={r_alert}")

    # ── Step5: 解消局面検知 ──────────────────────────────────
    history      = _load_crowding_history(cfg)
    unwind_alert = detect_unwind_alert(avg_corr, history) if avg_corr else False

    week_key      = _current_week_key()
    sorted_weeks  = sorted(history.keys(), reverse=True)
    prev_avg_corr = history.get(sorted_weeks[0], {}).get("avg_correlation") if sorted_weeks else None

    if unwind_alert:
        print(f"    🚨 クラウディング解消売りアラート: "
              f"前週{prev_avg_corr:.3f} → 今週{avg_corr:.3f}")
    elif alert:
        print(f"    ⚠️  クラウディングアラート: avg_corr={avg_corr:.3f}")
    else:
        print(f"    ✅ クラウディング正常: avg_corr={avg_corr:.3f}")

    # ── Step6: ファクター別クラウディング ────────────────────
    factor_crowding = {}
    if getattr(cfg, "ENABLE_FACTOR_CROWDING_DETAIL", True):
        print("    📊 ファクター別クラウディング計算中...")
        factor_crowding = calc_factor_crowding(candidates, lookback, cfg=cfg)

    # ── Step7: 履歴保存 ──────────────────────────────────────
    history[week_key] = {
        "avg_correlation":    avg_corr,
        "residual_avg_corr":  residual_avg_corr,
        "top_n_tickers":      top_tickers,
        "crowding_score":     score,
        "crowding_alert":     alert,
        "unwind_alert":       unwind_alert,
        "factor_crowding":    {
            k: {
                "avg_corr": v.get("avg_corr"),
                "score":    v.get("score"),
                "alert":    v.get("alert"),
            }
            for k, v in factor_crowding.items()
        },
        "timestamp": t0.strftime("%Y-%m-%d %H:%M:%S"),
    }
    history = _prune_history(history)
    _save_crowding_history(cfg, history)

    elapsed = (datetime.now() - t0).seconds
    print(f"    ✅ クラウディング検知完了（{elapsed}秒）")

    return {
        "avg_correlation":    avg_corr,
        "residual_avg_corr":  residual_avg_corr,
        "crowding_score":     score,
        "crowding_alert":     alert,
        "unwind_alert":       unwind_alert,
        "top_tickers":        top_tickers,
        "factor_crowding":    factor_crowding,
        "week_key":           week_key,
        "prev_avg_corr":      prev_avg_corr,
        "skipped":            False,
        "timestamp":          t0.strftime("%Y-%m-%d %H:%M:%S"),
    }
