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

from config_sync import sync_module_constants

# ── 定数 ──────────────────────────────────────────────────────
FETCH_DELAY        = 0.3   # yfinance レート制限対策（秒）
HISTORY_QUARTERS   = 4     # 取得する四半期数
CACHE_HOURS        = 24    # キャッシュ有効時間（時間）

# 加重平均の重み（直近から過去へ）
QUARTER_WEIGHTS    = [0.40, 0.30, 0.20, 0.10]

# サプライズ閾値
SURPRISE_POS       =   3.0   # +3%超:  ポジサプライズ
SURPRISE_NEG       =  -3.0   # -3%未満: ネガサプライズ

# screener ボーナス設定
BONUS_CONSEC_POS2  =  5.0   # 2四半期連続ポジサプライズ
BONUS_CONSEC_POS1  =  2.0   # 1四半期ポジサプライズ
PENALTY_CONSEC_NEG2 = -5.0  # 2四半期連続ネガサプライズ
PENALTY_CONSEC_NEG1 = -2.0  # 1四半期ネガサプライズ

# キャッシュパス（DATA_DIR から計算）
CACHE_FILENAME = "earnings_surprise_cache.json"

# [I-2] 保守度スコア設定
CONSERVATISM_QUARTERS  = 8     # 保守度計算に使う四半期数（最大8）
CONSERVATISM_CACHE_FILE = "conservatism_cache.json"

# 保守度スコア閾値
CONS_HIGH_MEAN   =  15.0   # 平均乖離率+15%超 = 非常に保守的
CONS_MID_MEAN    =  10.0   # 平均乖離率+10%超 = 保守的
CONS_LOW_MEAN    =   0.0   # 平均乖離率+0%超  = やや保守的
CONS_HIGH_STD    =  10.0   # σ < 10%  = 高精度
CONS_MID_STD     =  15.0   # σ < 15%  = 中精度
CONS_LOW_STD     =  20.0   # σ < 20%  = 低精度

# I-1 × I-2 複合フラグ閾値
SURPRISE_COMBO_ACCEL_MIN    = 70.0   # I-1 earnings_accel_score 閾値
SURPRISE_COMBO_CONSERV_MIN  = 70.0   # I-2 conservatism_score 閾値
SURPRISE_COMBO_NEG_ACCEL    = 30.0   # ネガティブ複合フラグ閾値
SURPRISE_COMBO_NEG_CONSERV  = 30.0   # I-2 conservatism_score 閾値

_CFG_CONST_MAP = {
    "ES_QUARTER_WEIGHTS": "QUARTER_WEIGHTS",
    "ES_BONUS_CONSEC_POS2": "BONUS_CONSEC_POS2",
    "ES_BONUS_CONSEC_POS1": "BONUS_CONSEC_POS1",
    "ES_PENALTY_CONSEC_NEG2": "PENALTY_CONSEC_NEG2",
    "ES_PENALTY_CONSEC_NEG1": "PENALTY_CONSEC_NEG1",
    "ES_SURPRISE_COMBO_ACCEL_MIN": "SURPRISE_COMBO_ACCEL_MIN",
    "ES_SURPRISE_COMBO_CONSERV_MIN": "SURPRISE_COMBO_CONSERV_MIN",
    'B4_EARNINGS_SURPRISE_HISTORY_QUARTERS': 'HISTORY_QUARTERS',
    'B4_EARNINGS_SURPRISE_CACHE_HOURS': 'CACHE_HOURS',
    'B4_EARNINGS_SURPRISE_CONSERVATISM_QUARTERS': 'CONSERVATISM_QUARTERS',
    'B5_EARNINGS_SURPRISE_FETCH_DELAY': 'FETCH_DELAY',
    'B6_EARNINGS_SURPRISE_SURPRISE_POS': 'SURPRISE_POS',
    'B6_EARNINGS_SURPRISE_CONS_HIGH_MEAN': 'CONS_HIGH_MEAN',
    'B6_EARNINGS_SURPRISE_CONS_MID_MEAN': 'CONS_MID_MEAN',
    'B6_EARNINGS_SURPRISE_CONS_LOW_MEAN': 'CONS_LOW_MEAN',
    'B6_EARNINGS_SURPRISE_CONS_HIGH_STD': 'CONS_HIGH_STD',
    'B6_EARNINGS_SURPRISE_CONS_MID_STD': 'CONS_MID_STD',
    'B6_EARNINGS_SURPRISE_CONS_LOW_STD': 'CONS_LOW_STD',
    'B6_EARNINGS_SURPRISE_SURPRISE_COMBO_NEG_ACCEL': 'SURPRISE_COMBO_NEG_ACCEL',
    'B6_EARNINGS_SURPRISE_SURPRISE_COMBO_NEG_CONSERV': 'SURPRISE_COMBO_NEG_CONSERV',
}

# ── config 参照ヘルパー（2026-07-25 追加）─────────────────────
# analyze_ticker() は cfg を引数に取らない設計のため、フラグ参照用に
# ここで遅延ロードする。呼び出し側のシグネチャを変えずに済ませるための措置。
_CFG_CACHE = None


def _cfg_es():
    """config モジュールを返す（取得できなければ None）。"""
    global _CFG_CACHE
    if _CFG_CACHE is None:
        try:
            import importlib
            import sys as _s
            _p = str(Path(__file__).parent)
            if _p not in _s.path:
                _s.path.insert(0, _p)
            _CFG_CACHE = importlib.import_module("config")
        except Exception:
            _CFG_CACHE = False   # 再試行しない印
    return _CFG_CACHE or None

def _fetch_earnings_history_extended(ticker: str, n_quarters: int = None) -> list:
    """
    [I-2] 直近 n_quarters 四半期の EPS 予想 vs 実績を取得する（保守度計算用）。
    _fetch_earnings_history() と同様だが取得件数を拡張している。

    n_quarters を省略すると config.py BLOCK I-2 の
    CONSERVATISM_MIN_QUARTERS（既定8）を使う。

    ★ 2026-07-25 バッチ3: 四半期数を config から読むよう是正。
      従来はモジュール定数 CONSERVATISM_QUARTERS=8 だけが効いており、
      config 側 CONSERVATISM_MIN_QUARTERS=4 は誰にも読まれていなかった
      （名前も値も食い違う二重定義）。健太の指示により実装値 8 を正とし、
      config を 8 へ揃えたうえで config を正本にした（挙動は不変）。
      既定引数に置かず None 判定にしているのは、既定値が def 実行時に
      固定されて config の変更が反映されなくなるのを避けるため。
    """
    if n_quarters is None:
        n_quarters = int(getattr(_cfg_es(), "CONSERVATISM_MIN_QUARTERS",
                                 CONSERVATISM_QUARTERS))
    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        hist = t.earnings_history
        if hist is None or (hasattr(hist, 'empty') and hist.empty):
            return []

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
                if abs(est_f) < 0.001:
                    surp_pct = None
                else:
                    surp_pct = round((act_f - est_f) / abs(est_f) * 100, 2)
                records.append({
                    "quarter":      str(dt)[:10] if dt else "",
                    "eps_estimate": round(est_f, 4),
                    "eps_actual":   round(act_f, 4),
                    "surprise_pct": surp_pct,
                    "beat":         surp_pct > 3.0 if surp_pct is not None else None,
                })
            except (TypeError, ValueError):
                continue

        records.sort(key=lambda x: x["quarter"], reverse=True)
        return records[:n_quarters]
    except Exception:
        return []
    
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

    # [I-2] 保守度スコアを同時計算（拡張履歴で）
    # 2026-07-25: BLOCK I-2 の ENABLE_CONSERVATISM_SCORE を配線。
    # 実装はここに揃っていたのに有効フラグだけ誰にも読まれておらず、
    # False にしても保守度の追加取得（履歴8四半期のfetch）が止まらなかった。
    # 無効時は中立50・bonus 0 として下流の式を壊さずスキップする。
    if not getattr(_cfg_es(), "ENABLE_CONSERVATISM_SCORE", True):
        conserv_result = {"conservatism_score": 50.0, "skipped": True}
        conserv_score  = 50.0
    else:
        history_ext    = _fetch_earnings_history_extended(ticker)
        conserv_result = calc_conservatism_score(ticker, history_ext)
        conserv_score  = conserv_result["conservatism_score"]

    # conservatism_bonus: screener_bonus に上乗せ
    if conserv_score >= 80:    conserv_bonus = 5.0
    elif conserv_score >= 65:  conserv_bonus = 3.0
    elif conserv_score <= 25:  conserv_bonus = -4.0
    elif conserv_score <= 40:  conserv_bonus = -2.0
    else:                      conserv_bonus = 0.0

    # I-1 加速スコアは screener 側から渡されないため仮の0で複合フラグを生成
    # 実際の組み合わせは stock_screener.py の _screen_sector() 内で評価
    combo = calc_surprise_combo_flag(ticker, conserv_score, 50.0)

    return {
        "ticker":             ticker,
        "name":               name[:30],
        "surprise_score":     score,
        "surprise_label":     label,
        "consecutive_pos":    consec_pos,
        "consecutive_neg":    consec_neg,
        "screener_bonus":     bonus,
        "latest_q":           history[0] if history else {},
        "history":            history,
        # [I-2] 追加フィールド
        "conservatism_score": conserv_score,
        "conservatism_label": conserv_result["label"],
        "avg_beat_pct":       conserv_result["avg_beat_pct"],
        "std_beat_pct":       conserv_result["std_beat_pct"],
        "beat_count":         conserv_result["beat_count"],
        "is_conservative":    conserv_result["is_conservative"],
        "conservatism_bonus": conserv_bonus,
        "combo_flag":         combo,
    }


# ==============================================================
# BLOCK 5: screener 統合インターフェース
# ==============================================================
def get_screener_bonus(ticker: str, surprise_cache: dict,
                       include_conservatism: bool = True) -> float:
    """
    screener_bonus + conservatism_bonus の合計を返す。
    include_conservatism=False の場合はサプライズボーナスのみ。
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
    base = float(result.get("screener_bonus", 0.0))
    if include_conservatism:
        base += float(result.get("conservatism_bonus", 0.0))
    return round(base, 2)


def get_conservatism_score(ticker: str, surprise_cache: dict) -> float:
    """conservatism_score を返す。データなしは 50（中立）。"""
    result = surprise_cache.get(ticker)
    if not result:
        return 50.0
    return float(result.get("conservatism_score", 50.0))


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
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
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

# ==============================================================
# BLOCK 2.5: [I-2] 保守度スコア計算
# ==============================================================

def calc_conservatism_score(ticker: str, history_ext: list = None) -> dict:
    """
    [I-2] 会社予想の保守度スコアを計算する（井村俊哉モデル）。

    Step1: 過去8四半期の乖離率（実績EPS / 会社予想EPS - 1）を計算
    Step2: 平均乖離率(μ) × 安定性(σ) でベーススコアを算出
    Step3: 上方修正頻度を加算
    Step4: 期初予想の弱気度を評価（将来拡張用）

    Returns:
        {
          "conservatism_score":  float,  # 0〜100
          "avg_beat_pct":        float,  # 平均乖離率%
          "std_beat_pct":        float,  # 乖離率標準偏差
          "beat_count":          int,    # ポジサプライズ回数
          "total_quarters":      int,    # 有効四半期数
          "revision_bonus":      float,  # 上方修正頻度ボーナス
          "label":               str,    # 説明ラベル
          "is_conservative":     bool,   # 70点以上 = 保守的
        }
    """
    import math

    # 拡張履歴を取得（渡されていなければ取得）
    if history_ext is None:
        history_ext = _fetch_earnings_history_extended(ticker)

    # 有効な乖離率のみ抽出
    beats = [r["surprise_pct"] for r in history_ext
             if r.get("surprise_pct") is not None]

    if len(beats) < 2:
        return {
            "conservatism_score": 50.0,
            "avg_beat_pct":       None,
            "std_beat_pct":       None,
            "beat_count":         0,
            "total_quarters":     len(beats),
            "revision_bonus":     0.0,
            "label":              "データ不足（中立）",
            "is_conservative":    False,
        }

    # Step1: 統計計算
    mu  = sum(beats) / len(beats)
    var = sum((x - mu) ** 2 for x in beats) / len(beats)
    sigma = math.sqrt(var)

    # Step2: ベーススコア（μ × σ の組み合わせ）
    base_score = 50.0

    if mu > CONS_HIGH_MEAN:
        if sigma < CONS_HIGH_STD:   base_score = 95.0
        elif sigma < CONS_MID_STD:  base_score = 82.0
        elif sigma < CONS_LOW_STD:  base_score = 70.0
        else:                       base_score = 60.0
    elif mu > CONS_MID_MEAN:
        if sigma < CONS_HIGH_STD:   base_score = 80.0
        elif sigma < CONS_MID_STD:  base_score = 72.0
        elif sigma < CONS_LOW_STD:  base_score = 62.0
        else:                       base_score = 55.0
    elif mu > CONS_LOW_MEAN:
        if sigma < CONS_LOW_STD:    base_score = 58.0
        else:                       base_score = 52.0
    elif mu < -5.0:
        if sigma < CONS_MID_STD:    base_score = 20.0   # 楽観的かつ安定
        else:                       base_score = 30.0
    else:
        base_score = 40.0

    # Step3: 上方修正頻度ボーナス（直近4四半期）
    recent4 = [r for r in history_ext[:4] if r.get("beat") is not None]
    beat_in_4 = sum(1 for r in recent4 if r.get("beat"))
    revision_bonus = {4: 20.0, 3: 15.0, 2: 10.0, 1: 5.0, 0: 0.0}.get(beat_in_4, 0.0)
    base_score += revision_bonus

    # クランプ
    final_score = round(max(0.0, min(100.0, base_score)), 1)

    # ラベル生成
    if final_score >= 90:
        label = f"🎯 超保守的（平均+{mu:.1f}%/σ={sigma:.1f}%）"
    elif final_score >= 70:
        label = f"📈 保守的（平均+{mu:.1f}%/σ={sigma:.1f}%）"
    elif final_score >= 50:
        label = f"➡️ やや保守的（平均+{mu:.1f}%）"
    elif final_score >= 30:
        label = f"⚠️ やや楽観的（平均{mu:.1f}%）"
    else:
        label = f"🔴 楽観的（平均{mu:.1f}% → ネガサプライズリスク）"

    return {
        "conservatism_score": final_score,
        "avg_beat_pct":       round(mu, 2),
        "std_beat_pct":       round(sigma, 2),
        "beat_count":         sum(1 for b in beats if b > 0),
        "total_quarters":     len(beats),
        "revision_bonus":     revision_bonus,
        "label":              label,
        "is_conservative":    final_score >= SURPRISE_COMBO_CONSERV_MIN,
    }


def calc_surprise_combo_flag(
    ticker: str,
    conservatism_score: float,
    earnings_accel_score: float,
) -> dict:
    """
    [I-1 × I-2] EPS加速度スコアと保守度スコアの複合フラグを生成する。

    Returns:
        {
          "high_prob_surprise":    bool,  # 決算サプライズ高確率候補
          "negative_risk":         bool,  # ネガティブサプライズリスク
          "combo_label":           str,
          "alert_days_before":     int,   # 決算何日前にアラートするか
        }
    """
    high_prob = (
        earnings_accel_score >= SURPRISE_COMBO_ACCEL_MIN
        and conservatism_score  >= SURPRISE_COMBO_CONSERV_MIN
    )
    neg_risk = (
        earnings_accel_score <= SURPRISE_COMBO_NEG_ACCEL
        and conservatism_score  <= SURPRISE_COMBO_NEG_CONSERV
    )

    if high_prob:
        combo_label = (
            f"🎯 決算サプライズ高確率候補 "
            f"（加速{earnings_accel_score:.0f}pt×保守{conservatism_score:.0f}pt）"
        )
    elif neg_risk:
        combo_label = (
            f"🔴 ネガティブサプライズリスク "
            f"（減速{earnings_accel_score:.0f}pt×楽観{conservatism_score:.0f}pt）"
        )
    else:
        combo_label = ""

    return {
        "high_prob_surprise": high_prob,
        "negative_risk":      neg_risk,
        "combo_label":        combo_label,
        "alert_days_before":  21,   # 決算3週間前にアラート
    }

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
