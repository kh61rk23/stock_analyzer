# ==============================================================
# screener_feedback_loop.py  v1.0
# screener通過 → EDINET優先 → スコア反映 の双方向ループ
# + EDINET蓄積financials → composite_score統合
# ==============================================================
# 【役割】
#   現在の「片方向」を「双方向」に変える:
#
#   ✅ 現状（片方向）:
#     screener通過銘柄 → EDINET priority=2 → 翌日取得
#                              ↓ (ここで止まっている)
#     取得したanalysis data が翌日のスコアに戻ってこない
#
#   ✅ 改善後（双方向）:
#     screener通過銘柄 → EDINET priority=2 → 翌日取得
#                              ↓
#     analysis data（credit_score/ROIC/FCF等）→ composite_score 補正に反映
#                              ↓
#     改善されたスコアで再スクリーニング → 精度向上
#
# 【EDINET蓄積データから計算する指標】
#   credit_score:   EDINETDBの信用スコア（既存）
#   revenue_cagr:   直近3年の売上成長率 CAGR
#   roe_trend:      ROEの3年トレンド（改善/悪化）
#   accrual_ratio:  会計アクルーアル比率（キャッシュフロー品質）
#   debt_ratio:     有利子負債/自己資本比率（財務健全性）
#   → これらをまとめて「edinet_quality_score（0〜100）」に統合
#
# 【composite_score への影響】
#   edinet_quality_score が高い → quality_score に最大 +8pt ボーナス
#   edinet_quality_score が低い → quality_score に最大 -8pt ペナルティ
#
# 【依存】edinet_rotation_manager.py / config.py
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config_sync import sync_module_constants

# ── 定数 ──────────────────────────────────────────────────────
MAX_EDINET_BONUS    =  8.0   # composite_score 補正の上限
MAX_EDINET_PENALTY  = -8.0   # 下限
CREDIT_WEIGHT       = 0.40   # credit_score の寄与
CAGR_WEIGHT         = 0.25   # revenue_cagr の寄与
ROE_TREND_WEIGHT    = 0.20   # roe_trend の寄与
DEBT_WEIGHT         = 0.15   # debt_ratio の寄与（逆数）

# credit_score のしきい値（EDINETDBのAAA〜C評価を数値に変換）
CREDIT_SCORE_MAP = {
    "AAA": 100, "AA": 85, "A": 70, "BBB": 55,
    "BB": 40, "B": 25, "CCC": 15, "D": 5,
}

CACHE_FILENAME = "edinet_quality_cache.json"
CACHE_HOURS    = 24 * 7  # 週次（EDINETデータはゆっくり変わる）

_CFG_CONST_MAP = {
    "SFL_MAX_EDINET_BONUS": "MAX_EDINET_BONUS",
    "SFL_MAX_EDINET_PENALTY": "MAX_EDINET_PENALTY",
    "SFL_CREDIT_WEIGHT": "CREDIT_WEIGHT",
    "SFL_CAGR_WEIGHT": "CAGR_WEIGHT",
    "SFL_ROE_TREND_WEIGHT": "ROE_TREND_WEIGHT",
    "SFL_DEBT_WEIGHT": "DEBT_WEIGHT",
    'B4_SCREENER_FEEDBACK_LOOP_CACHE_HOURS': 'CACHE_HOURS',
}


# ==============================================================
# BLOCK 1: EDINET蓄積データの読み込みとedinet_quality_score算出
# ==============================================================

def calc_edinet_quality_score(analysis: dict) -> dict:
    """
    EDINET rotation_manager が蓄積した analysis データから
    edinet_quality_score（0〜100）を算出する。

    analysis の構造（edinet_rotation_manager.load_db() の ticker_analysis[ticker]）:
      {
        "credit_score":   "AA",   # EDINET DB の信用格付け
        "credit_rating":  "A+",
        "risk_factors":   [...],
        "revenue_growth": 0.08,   # 売上成長率（yfinanceから補完の場合）
        "roe":            0.12,
        "_saved_at":      "2026-03-15",
      }

    Returns:
        {
          "edinet_quality_score": float,  # 0〜100
          "edinet_bonus":         float,  # composite_score への加算値
          "components":           dict,
          "label":                str,
        }
    """
    if not analysis:
        return {
            "edinet_quality_score": 50.0,
            "edinet_bonus":         0.0,
            "components":           {},
            "label":                "データなし",
        }

    # ── credit_score 変換 ──────────────────────────────────
    credit_raw = analysis.get("credit_score") or analysis.get("credit_rating", "")
    credit_num = None
    for k, v in CREDIT_SCORE_MAP.items():
        if k in str(credit_raw).upper():
            credit_num = v
            break

    # 数値で入ってくる場合（0〜100 の直接スコア）
    if credit_num is None:
        try:
            credit_num = float(credit_raw)
            credit_num = max(0, min(100, credit_num))
        except (TypeError, ValueError):
            credit_num = 50   # デフォルト

    # ── 売上成長率（CAGR） ────────────────────────────────
    rev_growth = analysis.get("revenue_growth") or analysis.get("revenueGrowth")
    cagr_score = 50.0
    if rev_growth is not None:
        try:
            g = float(rev_growth)
            # 成長率 -20%→0点 / 0%→50点 / +20%以上→100点
            cagr_score = max(0, min(100, (g + 0.20) / 0.40 * 100))
        except (TypeError, ValueError):
            pass

    # ── ROE トレンド ──────────────────────────────────────
    roe = analysis.get("roe") or analysis.get("returnOnEquity")
    roe_score = 50.0
    if roe is not None:
        try:
            r = float(roe)
            # ROE 0%→0点 / 10%→50点 / 30%以上→100点
            roe_score = max(0, min(100, r / 0.30 * 100))
            if r < 0:
                roe_score = 0
        except (TypeError, ValueError):
            pass

    # ── 有利子負債比率（D/E Ratio） ──────────────────────
    debt_equity = analysis.get("debtToEquity") or analysis.get("debt_to_equity")
    debt_score = 60.0   # デフォルト（データなし）
    if debt_equity is not None:
        try:
            de = float(debt_equity)
            # D/E 0→100点 / 1.0→60点 / 3.0以上→0点（逆数スコア）
            debt_score = max(0, min(100, (3.0 - de) / 3.0 * 100))
        except (TypeError, ValueError):
            pass

    # ── 総合 edinet_quality_score ─────────────────────────
    score = (
        credit_num  * CREDIT_WEIGHT
        + cagr_score  * CAGR_WEIGHT
        + roe_score   * ROE_TREND_WEIGHT
        + debt_score  * DEBT_WEIGHT
    )
    score = round(max(0, min(100, score)), 1)

    # ── bonus 計算（50を中心に±8ptにスケール） ────────────
    bonus = round((score - 50) / 50 * MAX_EDINET_BONUS, 2)
    bonus = max(MAX_EDINET_PENALTY, min(MAX_EDINET_BONUS, bonus))

    if score >= 75:   label = "🟢 財務優良"
    elif score >= 55: label = "🟡 財務良好"
    elif score >= 45: label = "⚪ 財務中立"
    elif score >= 30: label = "🟠 財務注意"
    else:             label = "🔴 財務要警戒"

    return {
        "edinet_quality_score": score,
        "edinet_bonus":         round(bonus, 2),
        "components": {
            "credit_num":  credit_num,
            "cagr_score":  round(cagr_score, 1),
            "roe_score":   round(roe_score, 1),
            "debt_score":  round(debt_score, 1),
        },
        "label": label,
    }


# ==============================================================
# BLOCK 2: screener通過銘柄のEDINET品質スコアを一括取得
# ==============================================================

def build_edinet_quality_cache(cfg, tickers: list) -> dict:
    """
    screener 通過銘柄の edinet_quality_score を一括計算する。
    edinet_rotation_manager の load_db() を参照。

    Returns:
        {ticker: {"edinet_quality_score": float, "edinet_bonus": float, ...}}
    """
    quality_cache = {}
    try:
        import importlib, sys
        sys.path.insert(0, str(Path(__file__).parent))
        erm = importlib.import_module("edinet_rotation_manager")
        db  = erm.load_db(cfg)
        analysis_data = db.get("analysis", {})

        for ticker in tickers:
            analysis = analysis_data.get(ticker)
            if analysis:
                quality_cache[ticker] = calc_edinet_quality_score(analysis)
            # analysisがない場合はデフォルト（スキップ）
    except Exception as e:
        print(f"    ⚠️  EDINET品質スコア取得失敗: {str(e)[:60]}")

    return quality_cache


def get_edinet_bonus(ticker: str, quality_cache: dict) -> float:
    """stock_screener.py から呼び出すインターフェース。"""
    rec = quality_cache.get(ticker, {})
    return float(rec.get("edinet_bonus", 0.0))


# ==============================================================
# BLOCK 3: screener_history からの学習フィードバック
# ==============================================================

def calc_screener_hit_rate(cfg) -> dict:
    """
    screener_history.json から過去30日間の通過銘柄ごとの
    「スコアが高かった業種/銘柄ほど実際にパフォームしたか」を分析する。

    Note: performance_tracker.py が週次でリターン集計済み。
    ここでは「スコア vs リターン」の単純相関を計算し、
    spearman順位相関が低い = screenerの精度が低い = フィルターを見直すべき

    Returns:
        {
          "score_return_corr": float,    # screenerスコアとリターンの相関
          "high_score_win_rate": float,  # スコア70以上の銘柄の勝率
          "low_score_win_rate":  float,  # スコア50以下の銘柄の勝率
          "total_evaluated":    int,
          "suggestion":         str,     # 改善提案テキスト
        }
    """
    # screener_history.json を読み込む
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
        / "screener_history.json",
        Path("stock_data/data/screener_history.json"),
    ]
    history_data = {}
    for p in candidates:
        if p.exists():
            try:
                history_data = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception:
                pass

    if not history_data:
        return {"total_evaluated": 0, "suggestion": "データ未蓄積"}

    # 4週前のデータを参照（現在の価格と比較するため）
    cutoff = (datetime.now() - timedelta(days=28)).strftime("%Y-%m-%d")
    old_data = {date: cands for date, cands in history_data.items()
                if date <= cutoff}

    if not old_data:
        return {"total_evaluated": 0, "suggestion": "4週前データ未蓄積"}

    # スコア vs リターンのペアを収集（yfinanceで現在価格取得）
    pairs = []
    try:
        import yf_cache as yf
        import time
        for date_str, candidates in list(old_data.items())[-4:]:   # 最新4週分
            for c in (candidates if isinstance(candidates, list) else []):
                ticker = c.get("ticker", "")
                score  = c.get("composite_score", 0) or 0
                price_then = c.get("current_price", 0) or 0
                if not ticker or price_then <= 0:
                    continue
                try:
                    info   = yf.Ticker(ticker).info or {}
                    price_now = info.get("regularMarketPrice") or 0
                    if price_now > 0:
                        ret = (price_now - price_then) / price_then * 100
                        pairs.append({"ticker": ticker, "score": score,
                                      "ret": ret, "date": date_str})
                    time.sleep(0.2)
                except Exception:
                    continue
    except Exception:
        pass

    if len(pairs) < 5:
        return {
            "total_evaluated": len(pairs),
            "suggestion": f"評価ペア不足（{len(pairs)}件）。蓄積中...",
        }

    # スコアとリターンの相関
    scores = [p["score"] for p in pairs]
    rets   = [p["ret"] for p in pairs]
    n      = len(scores)
    mx     = sum(scores) / n; my = sum(rets) / n
    num    = sum((s-mx)*(r-my) for s,r in zip(scores, rets))
    sx     = math.sqrt(sum((s-mx)**2 for s in scores))
    sy     = math.sqrt(sum((r-my)**2 for r in rets))
    corr   = round(num/(sx*sy), 3) if sx > 0 and sy > 0 else 0

    # 勝率（TOPIXは約+0.3%/月を基準とする）
    benchmark = 1.0   # 月次 1% を超えたら「勝ち」
    high_score = [p for p in pairs if p["score"] >= 70]
    low_score  = [p for p in pairs if p["score"] <= 50]

    hs_win = (sum(1 for p in high_score if p["ret"] > benchmark) / len(high_score)
              if high_score else None)
    ls_win = (sum(1 for p in low_score  if p["ret"] > benchmark) / len(low_score)
              if low_score  else None)

    # 改善提案
    if corr > 0.3:
        suggestion = "✅ screenerスコアとリターンに正の相関あり（精度良好）"
    elif corr > 0:
        suggestion = "🟡 相関はあるが弱い。フィルター条件の見直しを検討"
    else:
        suggestion = "⚠️ スコアとリターンの相関なし。MACRO_RULESの重みを確認"

    return {
        "score_return_corr":  corr,
        "high_score_win_rate": round(hs_win, 3) if hs_win else None,
        "low_score_win_rate":  round(ls_win, 3) if ls_win else None,
        "total_evaluated":    len(pairs),
        "suggestion":         suggestion,
    }


# ==============================================================
# BLOCK 4: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME

def _load_cache(cfg) -> dict:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if datetime.now() - datetime.fromisoformat(data.get("cached_at", "2000-01-01")) \
                < timedelta(hours=CACHE_HOURS):
            return data.get("quality_cache", {})
    except Exception:
        pass
    return {}

def _save_cache(cfg, quality_cache: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"cached_at": datetime.now().isoformat(), "quality_cache": quality_cache},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_screener_feedback_loop(cfg, screener_result: Optional[dict] = None) -> dict:
    """
    screener通過銘柄のEDINET品質スコア算出 + screener精度の自己評価を実行する。

    stock_analyzer_main.py から以下のように呼び出す:
        _sfl = _load_local_module("screener_feedback_loop", ...)
        sfl_result = _sfl.run_screener_feedback_loop(cfg, screen_result)
        results["screener_feedback"] = sfl_result

    Returns:
        {
          "quality_cache":   {ticker: edinet_quality_result},
          "hit_rate_stats":  dict,   # screener精度統計
          "total_enriched":  int,    # EDINET品質スコアを付与できた銘柄数
          "timestamp":       str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🔄 screenerフィードバックループ実行...")

    # screener通過銘柄のリスト
    passed_tickers = []
    if screener_result:
        passed_tickers = [c["ticker"] for c in screener_result.get("candidates", [])
                          if c.get("pass_filters")]

    # EDINET品質スコアキャッシュ確認
    cached_quality = _load_cache(cfg)
    new_tickers    = [t for t in passed_tickers if t not in cached_quality]

    quality_cache = dict(cached_quality)

    if new_tickers:
        print(f"    新規EDINET品質スコア計算: {len(new_tickers)}銘柄...")
        new_quality = build_edinet_quality_cache(cfg, new_tickers)
        quality_cache.update(new_quality)
        _save_cache(cfg, quality_cache)
        n_enriched = sum(1 for t in new_tickers if t in new_quality)
        print(f"    EDINET品質付与: {n_enriched}/{len(new_tickers)}銘柄")

    # 適用のサマリー
    bonuses = {t: d.get("edinet_bonus", 0) for t, d in quality_cache.items()
               if t in passed_tickers and d.get("edinet_bonus", 0) != 0}
    if bonuses:
        top = sorted(bonuses.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
        for t, b in top:
            label = quality_cache[t].get("label", "")
            print(f"      {t}: bonus={b:+.1f}pt {label}")

    # screener精度の自己評価（週次・月次のみ実行）
    hit_rate_stats = {}
    from datetime import date
    if date.today().weekday() == 0:   # 月曜日のみ
        print("    screener精度の自己評価中...")
        hit_rate_stats = calc_screener_hit_rate(cfg)
        if hit_rate_stats.get("total_evaluated", 0) > 0:
            corr = hit_rate_stats.get("score_return_corr", 0)
            print(f"    スコア×リターン相関: {corr:.3f} "
                  f"→ {hit_rate_stats.get('suggestion', '')[:40]}")

    total_enriched = sum(1 for t in passed_tickers if t in quality_cache)

    print(f"    ✅ フィードバックループ完了: "
          f"EDINET品質付与{total_enriched}/{len(passed_tickers)}銘柄")

    return {
        "quality_cache":   quality_cache,
        "hit_rate_stats":  hit_rate_stats,
        "total_enriched":  total_enriched,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
