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
from datetime import datetime
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

def _row_quarter(row):
    """earnings_history の行から四半期末日を取り出す。

    2026-09-17: yfinance の earnings_history は四半期日付を**インデックス**に持ち
    （列は epsActual / epsEstimate / epsDifference / surprisePercent のみ）、
    row.get("quarter") は常に None だった。quarter が全行 "" になり新しい順の
    並べ替えが効かず、yfinance の古い順のまま history[0] を「直近」として扱っていた
    （6674.T は直近3四半期が上振れなのに最古の下振れで -2pt、6594.T は逆に +2pt）。
    """
    dt = row.get("quarter") or row.get("Quarter")
    if dt is None and hasattr(row.name, "strftime"):
        dt = row.name
    return dt


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
        import yf_cache as yf
        t    = yf.Ticker(ticker)
        hist = t.earnings_history
        if hist is None or (hasattr(hist, 'empty') and hist.empty):
            return []

        records = []
        for _, row in hist.iterrows():
            try:
                est = row.get("epsEstimate") or row.get("EPSEstimate")
                act = row.get("epsActual")   or row.get("EPSActual")
                dt  = _row_quarter(row)
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
        import yf_cache as yf
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
                dt  = _row_quarter(row)
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

# 2026-09-29: 24時間でファイル全体を作り直す方式だと、前日の通過銘柄（約25社）の
# 分だけしか持てなかった。銘柄ごとに analyzed_at を持たせ、TICKER_TTL_DAYS 以内なら
# 使い回す（決算データは四半期に1度しか変わらない）。
TICKER_TTL_DAYS = 7


def _load_cache(cfg, max_age_days: float | None = None) -> dict:
    """有効期限内の銘柄だけを返す（{ticker: result}）。"""
    ttl = TICKER_TTL_DAYS if max_age_days is None else max_age_days
    try:
        p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        file_at = str(data.get("cached_at", "2000-01-01"))
        out = {}
        for t, r in (data.get("results") or {}).items():
            at = str((r or {}).get("analyzed_at") or file_at)
            try:
                age = (datetime.now() - datetime.fromisoformat(at[:19])).total_seconds() / 86400
            except ValueError:
                continue
            # 2026-10-01: 利益の加速度（profit_accel）が無い旧形式の結果は分析し直す
            if age <= ttl and "profit_accel" in (r or {}):
                out[t] = r
        return out
    except Exception:
        return {}


def _load_no_data(cfg) -> dict:
    """結果が出なかった銘柄 {ticker: analyzed_at}（TICKER_TTL_DAYS 以内のもの）。"""
    try:
        p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME
        raw = json.loads(p.read_text(encoding="utf-8")).get("no_data") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for t, at in raw.items():
        try:
            if (datetime.now() - datetime.fromisoformat(str(at)[:19])).days < TICKER_TTL_DAYS:
                out[t] = at
        except ValueError:
            continue
    return out


def _save_cache(cfg, results: dict, no_data: dict | None = None) -> None:
    try:
        p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        if no_data is None:
            no_data = _load_no_data(cfg)
        p.write_text(
            json.dumps(
                {"cached_at": datetime.now().isoformat(), "results": results,
                 "no_data": no_data},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _next_earnings_date(ticker: str) -> str | None:
    """次回決算予定日（YYYY-MM-DD）。取れなければ None。"""
    try:
        import yf_cache as yf
        cal = yf.Ticker(ticker).calendar
        dates = (cal or {}).get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            d = dates[0] if isinstance(dates, (list, tuple)) else dates
            return str(d)[:10]
    except Exception:
        pass
    return None


def calc_profit_acceleration(quarterly: list, annual: list) -> dict | None:
    """利益の加速度（純関数）。

    2026-10-01: 旧来の「EPS加速」は yfinance の earningsQuarterlyGrowth×4 − earningsGrowth
    だったが、両者はどちらも前年同期比でほぼ同値（7203.T 0.756/0.869）。結果は
    「増益率の約3倍」で、増益なら一律に加速扱いになっていた（121社中48社が高確率）。
    ここでは 直近四半期の前年同期比 − 通期の前年比 を加速度とする。
    quarterly / annual は [(期末日 "YYYY-MM-DD", 値)]。日本株は四半期が
    「直近期と前年同期」の2列しか無いことが多いので、列順ではなく日付で前年同期を探す。
    赤字を含む比較は伸び率が意味を持たないので None。
    """
    from datetime import date as _d

    def _clean(rows):
        out = []
        for dt, v in rows or []:
            try:
                v = float(v)
                if v != v:
                    continue
                out.append((_d.fromisoformat(str(dt)[:10]), v))
            except (TypeError, ValueError):
                continue
        return sorted(out, reverse=True)

    def _yoy(cur, base):
        return cur / base - 1 if cur > 0 and base > 0 else None

    q, a = _clean(quarterly), _clean(annual)
    if not q or len(a) < 2:
        return None
    d0, v0 = q[0]
    prev = [v for d, v in q[1:] if 345 <= (d0 - d).days <= 385]
    qy = _yoy(v0, prev[0]) if prev else None
    ay = _yoy(a[0][1], a[1][1]) if 345 <= (a[0][0] - a[1][0]).days <= 385 else None
    if qy is None or ay is None:
        return None
    return {"q_yoy": round(qy, 4), "annual_yoy": round(ay, 4), "accel": round(qy - ay, 4),
            "quarter": d0.isoformat()}


def calc_accel_from_quarters(q_rows: list, annual_rows: list) -> dict | None:
    """EdinetDB の単四半期・年次の純利益から加速度を出す（純関数）。

    q_rows: [(fiscal_year, "Q1".."Q4", value)]、annual_rows: [(fiscal_year, value)]。
    最新の四半期と前年同四半期を比べ、最新2年度の年次の伸びとの差を返す。
    """
    order = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
    qs = {(int(fy), q): float(v) for fy, q, v in q_rows if q in order and v is not None}
    if not qs:
        return None
    fy, q = max(qs, key=lambda k: (k[0], order[k[1]]))
    prev = qs.get((fy - 1, q))
    ann = {int(y): float(v) for y, v in annual_rows if v is not None}
    years = sorted(ann, reverse=True)
    if prev is None or len(years) < 2 or years[0] - years[1] != 1:
        return None
    cur, a0, a1 = qs[(fy, q)], ann[years[0]], ann[years[1]]
    if cur <= 0 or prev <= 0 or a0 <= 0 or a1 <= 0:
        return None
    qy, ay = cur / prev - 1, a0 / a1 - 1
    return {"q_yoy": round(qy, 4), "annual_yoy": round(ay, 4), "accel": round(qy - ay, 4),
            "quarter": f"FY{fy}{q}", "source": "edinetdb"}


_JP_Q_CACHE: dict | None = None
_JP_Q_LOCK = __import__("threading").Lock()


def _jp_quarterly_accel(ticker: str) -> dict | None:
    """日本株の加速度を BQ（edinet.financial_items_q / financial_items）から。初回に1回だけ全社分を読む。"""
    global _JP_Q_CACHE
    if not ticker.upper().endswith(".T"):
        return None
    with _JP_Q_LOCK:
        if _JP_Q_CACHE is None:
            _JP_Q_CACHE = {}
            try:
                from edinet_bq_client import EDINETBQClient
                bq = EDINETBQClient()
                p = bq.project
                qrows = bq.client.query(
                    f"SELECT company_code, fiscal_year, quarter, value FROM `{p}.edinet.financial_items_q` "
                    "WHERE item = 'net_income'").result()
                arows = bq.client.query(
                    f"SELECT company_code, fiscal_year, value FROM `{p}.edinet.financial_items` "
                    "WHERE item = 'net_income' AND fiscal_year >= EXTRACT(YEAR FROM CURRENT_DATE()) - 3").result()
                for r in qrows:
                    _JP_Q_CACHE.setdefault(r.company_code, {"q": [], "a": []})["q"].append(
                        (r.fiscal_year, r.quarter, r.value))
                for r in arows:
                    _JP_Q_CACHE.setdefault(r.company_code, {"q": [], "a": []})["a"].append(
                        (r.fiscal_year, r.value))
            except Exception as exc:
                print(f"    ⚠️ [earnings_surprise] EdinetDB四半期の読込失敗: {type(exc).__name__}: {str(exc)[:80]}")
    d = _JP_Q_CACHE.get(ticker.upper().replace(".T", ""))
    return calc_accel_from_quarters(d["q"], d["a"]) if d else None


def _fetch_profit_acceleration(ticker: str) -> dict | None:
    """四半期・通期の損益計算書から加速度を出す（純利益、無ければ希薄化EPS）。"""
    try:
        import yf_cache as yf
        t = yf.Ticker(ticker)
        qis, ais = t.quarterly_income_stmt, t.income_stmt
        if qis is None or ais is None:
            return _jp_quarterly_accel(ticker)
        # 伸び率どうしの比較なので、四半期と通期で別の行（純利益/EPS）を使ってよい
        rows = ("Net Income", "Net Income Common Stockholders", "Diluted EPS", "Basic EPS")
        for qr in (r for r in rows if r in qis.index):
            for ar in (r for r in rows if r in ais.index):
                r = calc_profit_acceleration(list(zip(qis.columns, qis.loc[qr].values)),
                                             list(zip(ais.columns, ais.loc[ar].values)))
                if r:
                    return r
        # 2026-10-03: yfinance に四半期が無い日本株は EdinetDB の単四半期で補う
        return _jp_quarterly_accel(ticker)
    except Exception:
        return _jp_quarterly_accel(ticker)


def apply_surprise_flags(candidates: list, cache: dict, cfg=None,
                         today: str | None = None) -> dict:
    """当日の分析結果で候補の高確率／ネガティブ判定をやり直す（純関数）。

    以前はスクリーナーの時点（前日の分析結果）で判定していた。理由と次回決算日も付ける。
    Returns: {"high_prob": n, "negative": n}
    """
    # 2026-10-01: 判定は当日の分析結果（利益の加速度×会社予想の保守度）で行う。
    #   高確率: 加速 +15pt 以上 × 保守度70以上 × 直近決算が下振れでない
    #   ネガ  : (加速 −15pt 以下 × 保守度45以下) または 2期以上連続の下振れ
    # 分析結果が無い銘柄は、スクリーナー時点の仮判定を残さず「判定なし」にする。
    accel_min, cons_min, neg_accel, neg_cons = 0.15, 70.0, -0.15, 45.0
    today = today or datetime.now().strftime("%Y-%m-%d")
    n_hi = n_neg = 0
    for c in candidates or []:
        r = (cache or {}).get(c.get("ticker"))
        if not r:
            c["is_high_prob_surprise"] = c["is_negative_risk"] = False
            continue
        pa = r.get("profit_accel") or {}
        acc = pa.get("accel")
        cons = float(r.get("conservatism_score") or 50)
        last = ((r.get("latest_q") or {}).get("surprise_pct"))
        c["conservatism_score"] = cons
        c["profit_accel"] = acc
        avg = r.get("avg_beat_pct")
        # 加速度が出せない銘柄（yfinance の日本株は四半期が1期分だけのことが多い）は、
        # 会社予想が一貫して保守的で3期以上連続上振れのときだけ高確率とする
        streak_only = (acc is None and cons >= 80 and int(r.get("consecutive_pos") or 0) >= 3
                       and avg is not None and 0 < avg <= 100)
        c["is_high_prob_surprise"] = streak_only or (
            acc is not None and acc >= accel_min and cons >= cons_min
            and (last is None or last >= 0))
        c["is_negative_risk"] = ((acc is not None and acc <= neg_accel and cons <= neg_cons)
                                 or int(r.get("consecutive_neg") or 0) >= 2)
        ne = r.get("next_earnings")
        c["next_earnings"] = ne
        try:
            c["days_to_earnings"] = (datetime.strptime(ne, "%Y-%m-%d")
                                     - datetime.strptime(today, "%Y-%m-%d")).days if ne else None
        except ValueError:
            c["days_to_earnings"] = None
        c["surprise_reason"] = (
            (f"純利益 直近四半期 前年比{pa['q_yoy'] * 100:+.0f}% vs 通期{pa['annual_yoy'] * 100:+.0f}%"
             f"（加速{acc * 100:+.0f}pt）" if acc is not None else "利益の加速度 算出不可")
            + f" × 会社予想の保守度{cons:.0f}点"
            + (f"（過去{r.get('beat_count', 0)}回上振れ・平均{avg:+.1f}%）" if avg is not None else "")
            + (f" | 直近{r.get('consecutive_pos')}期連続上振れ" if r.get("consecutive_pos") else "")
            + (f" | 直近{r.get('consecutive_neg')}期連続下振れ" if r.get("consecutive_neg") else ""))
        n_hi += c["is_high_prob_surprise"]
        n_neg += c["is_negative_risk"]
    return {"high_prob": n_hi, "negative": n_neg}


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
        import yf_cache as yf
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
        "next_earnings":      _next_earnings_date(ticker),   # 2026-09-29
        "profit_accel":       _fetch_profit_acceleration(ticker),   # 2026-10-01
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

    # 銘柄ごとのキャッシュ（7日有効）。期限切れ・未分析の銘柄だけ取り直す
    cached = _load_cache(cfg)

    # 対象銘柄を決定
    if not tickers:
        # SECTOR_REPRESENTATIVE_TICKERS から全銘柄を収集
        rep = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
        tickers = list({t for ts in rep.values() for t in ts})
        # screener通過銘柄があれば追加
        screener_candidates = getattr(cfg, "EDINET_SCREENER_CANDIDATES", [])
        tickers = list(set(tickers + screener_candidates))

    # 2026-10-02: アナリスト予想が無く結果が出ない銘柄（実測364社中232社）も
    # 毎日取り直していた。「データ無し」も7日間は記録を使う
    no_data = _load_no_data(cfg)
    todo = [t for t in dict.fromkeys(tickers) if t not in cached and t not in no_data]
    print(f"    対象: {len(set(tickers))}銘柄（キャッシュ有効 {len(set(tickers)) - len(todo)} / "
          f"新規分析 {len(todo)}）")

    # 2026-09-29: 1銘柄ずつ（info＋履歴＋拡張履歴＋決算日）を5並列に
    from concurrent.futures import ThreadPoolExecutor
    workers = max(1, int(getattr(cfg, "EARNINGS_SURPRISE_WORKERS", 5)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = list(pool.map(analyze_ticker, todo))
    now_iso = datetime.now().isoformat(timespec="seconds")
    results = dict(_load_cache(cfg, max_age_days=30))   # 30日より古いものは捨てる
    for t, r in zip(todo, fetched):
        if r:
            r["analyzed_at"] = now_iso
            results[t] = r
            no_data.pop(t, None)
        else:
            no_data[t] = now_iso
    _save_cache(cfg, results, no_data)
    summary = _build_summary({t: results[t] for t in tickers if t in results})
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
