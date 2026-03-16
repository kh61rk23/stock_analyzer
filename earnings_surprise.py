# ==============================================================
# earnings_surprise.py  v1.0  (A-7)
# 決算サプライズ自動検出 → screener ボーナス + アラート通知
# ==============================================================
# 【役割】
#   yfinance の earningsHistory / earnings_dates から
#   直近4四半期の EPS 予想 vs 実績を自動取得し、
#   決算サプライズスコアを算出する。
#
# 【決算サプライズスコア（surprise_score）の定義】
#   各四半期の surprisePct（超過率%）を加重平均:
#     直近Q1: 重み40%  / 1Q前: 重み30%
#     2Q前:   重み20%  / 3Q前: 重み10%
#   正規化: 100点満点（-50%〜+50%を0〜100点にマッピング）
#
# 【screener への統合】
#   2四半期連続ポジサプライズ銘柄に quality_score +5点ボーナス
#   連続ネガサプライズ銘柄に -5点ペナルティ
#   → stock_screener.py の _calc_scores() から参照
#
# 【出力スキーマ（run_earnings_surprise の戻り値）】
#   {
#     "results":    {ticker: SurpriseResult},
#     "top_pos":    [SurpriseResult, ...],  # ポジサプライズ上位5
#     "top_neg":    [SurpriseResult, ...],  # ネガサプライズ下位5
#     "total":      int,
#     "timestamp":  str,
#   }
#   SurpriseResult = {
#     "ticker":           str,
#     "name":             str,
#     "surprise_score":   float,   # 0〜100（50=中立）
#     "surprise_label":   str,     # "大幅サプライズ" / "サプライズ" / "中立" / etc.
#     "consecutive_pos":  int,     # 連続ポジサプライズ四半期数
#     "consecutive_neg":  int,     # 連続ネガサプライズ四半期数
#     "screener_bonus":   float,   # quality_score への加算点（-5〜+5）
#     "latest_q":         dict,    # 直近四半期の詳細
#     "history":          list,    # 最大4四半期分の履歴
#   }
#
# 【依存】yfinance / config.py
# 【呼出】from earnings_surprise import run_earnings_surprise,
#                                        get_screener_bonus
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
FETCH_DELAY        = 0.3   # yfinance レート制限対策（秒）
HISTORY_QUARTERS   = 4     # 取得する四半期数
CACHE_HOURS        = 24    # キャッシュ有効時間（時間）

# 加重平均の重み（直近から過去へ）
QUARTER_WEIGHTS    = [0.40, 0.30, 0.20, 0.10]

# サプライズ閾値
SURPRISE_LARGE_POS =  10.0   # +10%超: 大幅ポジサプライズ
SURPRISE_POS       =   3.0   # +3%超:  ポジサプライズ
SURPRISE_NEG       =  -3.0   # -3%未満: ネガサプライズ
SURPRISE_LARGE_NEG = -10.0   # -10%未満: 大幅ネガサプライズ

# screener ボーナス設定
BONUS_CONSEC_POS2  =  5.0   # 2四半期連続ポジサプライズ
BONUS_CONSEC_POS1  =  2.0   # 1四半期ポジサプライズ
PENALTY_CONSEC_NEG2 = -5.0  # 2四半期連続ネガサプライズ
PENALTY_CONSEC_NEG1 = -2.0  # 1四半期ネガサプライズ

# キャッシュパス（DATA_DIR から計算）
CACHE_FILENAME = "earnings_surprise_cache.json"


# ==============================================================
# BLOCK 1: yfinance から決算サプライズデータ取得
# ==============================================================

def _fetch_earnings_history(ticker: str) -> list:
    """
    yfinance から最大4四半期の EPS 予想 vs 実績を取得する。

    Returns:
        [
          {
            "quarter":      "2025-12-31",
            "eps_estimate":  1.23,
            "eps_actual":    1.45,
            "surprise_pct":  17.9,   # (actual - estimate) / |estimate| * 100
          },
          ...
        ]
        取得失敗時は []
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # earnings_history（四半期EPS履歴）
        hist = t.earnings_history
        if hist is None or (hasattr(hist, 'empty') and hist.empty):
            # フォールバック: quarterly_earnings
            qe = getattr(t, 'quarterly_earnings', None)
            if qe is None or (hasattr(qe, 'empty') and qe.empty):
                return []
            records = []
            for dt, row in qe.iterrows():
                records.append({
                    "quarter":      str(dt)[:10],
                    "eps_estimate": None,
                    "eps_actual":   float(row.get("Reported EPS", 0) or 0),
                    "surprise_pct": None,
                })
            return sorted(records, key=lambda x: x["quarter"], reverse=True)[:HISTORY_QUARTERS]

        records = []
        for _, row in hist.iterrows():
            try:
                est = row.get("epsEstimate") or row.get("EPSEstimate")
                act = row.get("epsActual")   or row.get("EPSActual")
                dt  = row.get("quarter")     or row.get("Quarter")
                if est is None or act is None:
                    continue
                est_f = float(est)
                act_f = float(act)
                # ゼロ除算ガード
                if abs(est_f) < 0.001:
                    surp_pct = None
                else:
                    surp_pct = round((act_f - est_f) / abs(est_f) * 100, 2)
                records.append({
                    "quarter":      str(dt)[:10] if dt else "",
                    "eps_estimate": round(est_f, 4),
                    "eps_actual":   round(act_f, 4),
                    "surprise_pct": surp_pct,
                })
            except (TypeError, ValueError):
                continue

        # 新しい順にソートして最大4件
        records.sort(key=lambda x: x["quarter"], reverse=True)
        return records[:HISTORY_QUARTERS]

    except Exception:
        return []


# ==============================================================
# BLOCK 2: サプライズスコア算出
# ==============================================================

def _calc_surprise_score(history: list) -> float:
    """
    最大4四半期の surprise_pct から加重平均サプライズスコア（0〜100）を計算する。

    surprise_pct:
      +50% 以上 → 100点
       0%       → 50点
      -50% 以下 →  0点
    """
    if not history:
        return 50.0   # データなし = 中立

    weighted_sum = 0.0
    weight_total = 0.0

    for i, rec in enumerate(history[:HISTORY_QUARTERS]):
        sp = rec.get("surprise_pct")
        if sp is None:
            continue
        w = QUARTER_WEIGHTS[i] if i < len(QUARTER_WEIGHTS) else 0.05
        # -50〜+50% を 0〜100点に変換
        score_i = max(0.0, min(100.0, (sp + 50.0)))
        weighted_sum += score_i * w
        weight_total += w

    if weight_total == 0:
        return 50.0
    return round(weighted_sum / weight_total, 1)


def _calc_consecutive(history: list) -> tuple[int, int]:
    """
    連続ポジ/ネガサプライズ四半期数を返す。

    Returns:
        (consecutive_pos, consecutive_neg)
        例: 直近3四半期がポジなら (3, 0)
    """
    pos = 0
    neg = 0
    for rec in history:
        sp = rec.get("surprise_pct")
        if sp is None:
            break
        if sp >= SURPRISE_POS:
            if neg > 0:
                break
            pos += 1
        elif sp <= SURPRISE_NEG:
            if pos > 0:
                break
            neg += 1
        else:
            break   # 中立で連続は途切れる

    return pos, neg


def _surprise_label(score: float, consec_pos: int, consec_neg: int) -> str:
    """スコアと連続状況から日本語ラベルを生成する。"""
    if score >= 80:
        label = "🔥 大幅ポジサプライズ"
    elif score >= 60:
        label = "📈 ポジサプライズ"
    elif score >= 45:
        label = "➡️ 中立"
    elif score >= 30:
        label = "📉 ネガサプライズ"
    else:
        label = "💥 大幅ネガサプライズ"

    if consec_pos >= 2:
        label += f"（{consec_pos}Q連続↑）"
    elif consec_neg >= 2:
        label += f"（{consec_neg}Q連続↓）"

    return label


def _calc_screener_bonus(consec_pos: int, consec_neg: int) -> float:
    """screener の quality_score に加算するボーナス点を返す。"""
    if consec_pos >= 2:
        return BONUS_CONSEC_POS2
    elif consec_pos >= 1:
        return BONUS_CONSEC_POS1
    elif consec_neg >= 2:
        return PENALTY_CONSEC_NEG2
    elif consec_neg >= 1:
        return PENALTY_CONSEC_NEG1
    return 0.0


# ==============================================================
# BLOCK 3: キャッシュ管理
# ==============================================================

def _load_cache(cfg) -> dict:
    """CACHE_HOURS 以内のキャッシュを返す。期限切れ or なければ {}。"""
    try:
        p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(hours=CACHE_HOURS):
            return data.get("results", {})
    except Exception:
        pass
    return {}


def _save_cache(cfg, results: dict) -> None:
    try:
        p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"cached_at": datetime.now().isoformat(), "results": results},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


# ==============================================================
# BLOCK 4: 単一銘柄の分析
# ==============================================================

def analyze_ticker(ticker: str) -> Optional[dict]:
    """
    1銘柄の決算サプライズを分析する。

    Returns:
        SurpriseResult dict | None（データ取得不可の場合）
    """
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        name = info.get("longName") or info.get("shortName") or ticker
    except Exception:
        name = ticker

    history = _fetch_earnings_history(ticker)
    if not history:
        return None

    score          = _calc_surprise_score(history)
    consec_pos, consec_neg = _calc_consecutive(history)
    label          = _surprise_label(score, consec_pos, consec_neg)
    bonus          = _calc_screener_bonus(consec_pos, consec_neg)

    return {
        "ticker":           ticker,
        "name":             name[:30],
        "surprise_score":   score,
        "surprise_label":   label,
        "consecutive_pos":  consec_pos,
        "consecutive_neg":  consec_neg,
        "screener_bonus":   bonus,
        "latest_q":         history[0] if history else {},
        "history":          history,
    }


# ==============================================================
# BLOCK 5: screener 統合インターフェース
# ==============================================================

def get_screener_bonus(ticker: str, surprise_cache: dict) -> float:
    """
    stock_screener.py の _calc_scores() から呼び出す。
    キャッシュ済みの surprise 結果から screener_bonus を返す。

    Args:
        ticker:         例 "6758.T"
        surprise_cache: run_earnings_surprise() の results辞書

    Returns:
        float: quality_score への加算点（-5〜+5、データなしは 0.0）
    """
    result = surprise_cache.get(ticker)
    if not result:
        return 0.0
    return float(result.get("screener_bonus", 0.0))


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_earnings_surprise(cfg, tickers: Optional[list] = None) -> dict:
    """
    決算サプライズを一括分析する。

    stock_analyzer_main.py から以下のように呼び出す:
        _es = _load_local_module("earnings_surprise", ...)
        es_result = _es.run_earnings_surprise(cfg, screener_tickers)
        results["earnings_surprise"] = es_result

    Args:
        cfg:     config モジュール
        tickers: 分析対象 ticker リスト。None の場合は SECTOR_REPRESENTATIVE_TICKERS から自動取得

    Returns:
        {
          "results":   {ticker: SurpriseResult},
          "top_pos":   ポジサプライズ上位5銘柄,
          "top_neg":   ネガサプライズ下位5銘柄,
          "total":     分析銘柄数,
          "hit_pos":   ポジサプライズ銘柄数,
          "hit_neg":   ネガサプライズ銘柄数,
          "timestamp": str,
        }
    """
    print("\n  📊 決算サプライズ分析開始...")

    # キャッシュ確認
    cached = _load_cache(cfg)
    if cached:
        print(f"    ✅ キャッシュ使用（{len(cached)}銘柄）")
        return _build_summary(cached)

    # 対象銘柄を決定
    if not tickers:
        # SECTOR_REPRESENTATIVE_TICKERS から全銘柄を収集
        rep = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
        tickers = list({t for ts in rep.values() for t in ts})
        # screener通過銘柄があれば追加
        screener_candidates = getattr(cfg, "EDINET_SCREENER_CANDIDATES", [])
        tickers = list(set(tickers + screener_candidates))

    n = len(tickers)
    print(f"    対象: {n}銘柄 分析中...")

    results = {}
    for i, ticker in enumerate(tickers, 1):
        result = analyze_ticker(ticker)
        if result:
            results[ticker] = result
            bonus = result["screener_bonus"]
            if abs(bonus) >= 2:
                print(f"      {ticker}: {result['surprise_label']} bonus={bonus:+.0f}pt")
        time.sleep(FETCH_DELAY)
        if i % 10 == 0:
            print(f"      ... {i}/{n}完了")

    _save_cache(cfg, results)
    summary = _build_summary(results)
    print(
        f"    ✅ 決算サプライズ完了: {summary['total']}銘柄 "
        f"ポジ{summary['hit_pos']}社 / ネガ{summary['hit_neg']}社"
    )
    return summary


def _build_summary(results: dict) -> dict:
    """results辞書から集計サマリーを構築する。"""
    all_res = list(results.values())
    sorted_by_score = sorted(all_res, key=lambda x: x["surprise_score"], reverse=True)

    hit_pos = sum(1 for r in all_res if r["screener_bonus"] > 0)
    hit_neg = sum(1 for r in all_res if r["screener_bonus"] < 0)

    return {
        "results":   results,
        "top_pos":   sorted_by_score[:5],
        "top_neg":   sorted_by_score[-5:],
        "total":     len(all_res),
        "hit_pos":   hit_pos,
        "hit_neg":   hit_neg,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 7: アラートテキスト生成
# ==============================================================

def format_surprise_alert(summary: dict, top_n: int = 5) -> str:
    """
    alert_notifier.py 向けのアラートテキストを生成する。

    出力例:
      📋 決算サプライズ検出（12社）
      ポジサプライズ上位:
        6758.T ソニー: 🔥 大幅ポジサプライズ（2Q連続↑） score=82
        ...
    """
    total   = summary.get("total", 0)
    hit_pos = summary.get("hit_pos", 0)
    hit_neg = summary.get("hit_neg", 0)

    if total == 0:
        return "📋 決算サプライズ: データなし"

    lines = [f"📋 決算サプライズ検出（ポジ{hit_pos}社 / ネガ{hit_neg}社）"]

    top_pos = summary.get("top_pos", [])
    if top_pos:
        pos_notable = [r for r in top_pos if r["screener_bonus"] > 0][:top_n]
        if pos_notable:
            lines.append("  📈 ポジサプライズ上位:")
            for r in pos_notable:
                lines.append(
                    f"    {r['ticker']} {r['name'][:14]}: "
                    f"{r['surprise_label']} (score={r['surprise_score']:.0f})"
                )

    top_neg = summary.get("top_neg", [])
    if top_neg:
        neg_notable = [r for r in top_neg if r["screener_bonus"] < 0][:3]
        if neg_notable:
            lines.append("  📉 ネガサプライズ注意:")
            for r in neg_notable:
                lines.append(
                    f"    {r['ticker']} {r['name'][:14]}: "
                    f"{r['surprise_label']}"
                )

    return "\n".join(lines)
