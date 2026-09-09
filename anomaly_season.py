# ==============================================================
# anomaly_season.py  v1.0  (4-4 / D-1)
# 過去10年月次リターンから統計的アノマリーを検出
# ==============================================================
# 【役割】
#   「1月効果」「5月に売れ（Sell in May）」「配当落ち月安」など、
#   株式市場に存在する季節的アノマリー（統計的な規則性）を
#   過去10年の月次リターンデータから自動検出する。
#
# 【検出ロジック】
#   各業種・銘柄について月別平均リターンをt検定で評価:
#     H₀: 特定月の平均リターン = 0（アノマリーなし）
#     H₁: 特定月の平均リターン ≠ 0（アノマリーあり）
#   p値 < 0.05 → 統計的に有意なアノマリー
#   p値 < 0.01 → 高度に有意なアノマリー
#
# 【composite_score への影響】
#   「現在月が統計的に上昇しやすい月」の業種に +N pt ボーナス
#   「現在月が統計的に下落しやすい月」の業種に -N pt ペナルティ
#   補正量: p値と平均リターンに比例（最大 ±8pt）
#
# 【出力スキーマ】
#   {
#     "current_month":  int,         # 現在月（1〜12）
#     "anomalies": {
#       sector_code: {
#         "monthly_stats": {1: {mean, std, n, t_stat, p_value}, ...12},
#         "current_anomaly": {"mean": float, "p_value": float, "signal": str},
#         "top_positive": [(month, mean, p), ...],  # 有意に強い月
#         "top_negative": [(month, mean, p), ...],  # 有意に弱い月
#       }
#     },
#     "market_anomaly": dict,  # 日経225全体のアノマリー
#     "adjustments":   {sector_code: float},  # composite_scoreへの加算
#     "calendar_md":   str,    # Markdownカレンダー形式レポート
#     "timestamp":     str,
#   }
#
# 【依存】yfinance のみ（scipy 不使用・純粋実装）
# 【呼出】from anomaly_season import run_anomaly_season
# ==============================================================

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_ANOMALY_SEASON_LOOKBACK_YEARS': 'LOOKBACK_YEARS',
    'B4_ANOMALY_SEASON_CACHE_HOURS': 'CACHE_HOURS',
    'B5_ANOMALY_SEASON_FETCH_DELAY': 'FETCH_DELAY',
    'B6_ANOMALY_SEASON_MIN_SAMPLES': 'MIN_SAMPLES',
    'B6_ANOMALY_SEASON_P_SIGNIFICANT': 'P_SIGNIFICANT',
    'B6_ANOMALY_SEASON_MAX_ADJUSTMENT': 'MAX_ADJUSTMENT',
}

LOOKBACK_YEARS     = 10      # 過去N年のデータを使用
MIN_SAMPLES        = 5       # t検定に必要な最低サンプル数
P_SIGNIFICANT      = 0.05    # 有意水準
MAX_ADJUSTMENT     = 8.0     # composite_score 補正の上限（±pt）
FETCH_DELAY        = 0.3
CACHE_FILENAME     = "anomaly_season_cache.json"
CACHE_HOURS        = 24 * 7  # 週次データ = 7日キャッシュ

# 日本の季節性アノマリーの典型パターン（参考・検証用ラベル）
KNOWN_ANOMALIES = {
    1:  "1月効果（年初の上昇傾向）",
    3:  "3月末配当落ち（下落傾向）",
    4:  "4月効果（新年度・年金資金流入）",
    5:  "Sell in May（5月売り）",
    9:  "9月配当落ち（下落傾向）",
    12: "年末ラリー（大型株集中）",
}

# 業種別の主要ティッカー（アノマリー計算の代表銘柄）
# stock_screener.py の JP_SECTOR_TICKERS より先頭1〜2銘柄を使用
SECTOR_LEAD_TICKERS = {
    "1001": "1332.T", "1002": "1662.T", "1003": "1802.T",
    "1004": "2802.T", "1005": "3401.T", "1006": "3861.T",
    "1007": "4063.T", "1008": "4502.T", "1009": "5019.T",
    "1010": "5108.T", "1011": "5201.T", "1012": "5401.T",
    "1013": "5711.T", "1014": "5631.T", "1015": "6301.T",
    "1016": "6758.T", "1017": "7203.T", "1018": "7733.T",
    "1019": "7974.T", "1020": "9501.T", "1021": "9020.T",
    "1022": "9101.T", "1023": "9202.T", "1024": "9301.T",
    "1025": "9432.T", "1026": "8001.T", "1027": "9983.T",
    "1028": "8306.T", "1029": "8601.T", "1030": "8725.T",
    "1031": "8591.T", "1032": "8801.T", "1033": "9984.T",
}

# 市場全体の代表ティッカー（日経225 / S&P500 / TOPIX）
MARKET_TICKERS = {
    "nikkei":  "^N225",
    "sp500":   "^GSPC",
    "topix":   "1306.T",   # TOPIX ETF
}


# ==============================================================
# BLOCK 1: t分布 p値（scipy 不使用・純粋実装）
# ==============================================================

def _betainc_approx(a: float, b: float, x: float, steps: int = 200) -> float:
    """不完全ベータ関数の数値積分近似（Simpson法）。"""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    h = x / steps
    total = 0.0
    for i in range(steps + 1):
        xi = i * h
        if xi <= 0 or xi >= 1:
            fi = 0.0
        else:
            fi = math.exp((a - 1) * math.log(xi) + (b - 1) * math.log(1 - xi) - lbeta)
        w = 1 if i in (0, steps) else (4 if i % 2 == 1 else 2)
        total += w * fi
    return total * h / 3


def t_pvalue(t_stat: float, df: int, two_tailed: bool = True) -> float:
    """
    t統計量から p値を計算する（scipy 不使用）。

    不完全ベータ関数を使用した近似計算。
    精度は scipy.stats.t.sf と比較して ±0.003 程度。
    """
    if df <= 0:
        return 1.0
    x = df / (df + t_stat ** 2)
    p_one_tail = _betainc_approx(df / 2, 0.5, x) / 2
    return min(1.0, p_one_tail * 2 if two_tailed else p_one_tail)


# ==============================================================
# BLOCK 2: 月次リターン時系列の取得
# ==============================================================

def _fetch_monthly_returns(ticker: str, years: int = LOOKBACK_YEARS) -> dict:
    """
    yfinance から月次リターンの時系列を取得する。

    Returns:
        {year_month: return_pct, ...}
        例: {"2015-01": 2.5, "2015-02": -1.3, ...}
    """
    try:
        import yf_cache as yf  # type: ignore

        end   = datetime.now()
        start = end.replace(year=end.year - years)

        hist = yf.Ticker(ticker).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=True,
        )
        if hist is None or len(hist) < 12:
            return {}

        # 月次リサンプル（月末終値）
        hist.index = hist.index.tz_localize(None) if hasattr(hist.index, 'tz_localize') else hist.index
        monthly = hist["Close"].resample("ME").last()
        if len(monthly) < 12:
            # "ME" が使えない古い pandas の場合のフォールバック
            monthly = hist["Close"].resample("M").last()

        result = {}
        closes = monthly.tolist()
        dates  = monthly.index.tolist()

        for i in range(1, len(closes)):
            if closes[i-1] > 0:
                ret = (closes[i] - closes[i-1]) / closes[i-1] * 100
                key = dates[i].strftime("%Y-%m")
                result[key] = round(ret, 4)

        return result

    except Exception:
        return {}


def _group_by_month(monthly_returns: dict) -> dict:
    """
    月次リターン辞書を「月」でグループ化する。

    Returns:
        {1: [2.5, -1.3, 3.2, ...], 2: [...], ..., 12: [...]}
    """
    grouped = {m: [] for m in range(1, 13)}
    for ym, ret in monthly_returns.items():
        try:
            month = int(ym.split("-")[1])
            grouped[month].append(ret)
        except (IndexError, ValueError):
            continue
    return grouped


# ==============================================================
# BLOCK 3: 統計的アノマリー検定
# ==============================================================

def _calc_monthly_stats(monthly_groups: dict) -> dict:
    """
    月別の記述統計量と t検定結果を計算する。

    Returns:
        {
          month: {
            "mean":    float,   # 平均リターン%
            "std":     float,   # 標準偏差
            "n":       int,     # サンプル数
            "t_stat":  float,   # t統計量
            "p_value": float,   # p値（両側検定）
            "signal":  str,     # "positive" / "negative" / "neutral"
          }
        }
    """
    stats = {}
    for month, returns in monthly_groups.items():
        n = len(returns)
        if n < MIN_SAMPLES:
            stats[month] = {
                "mean": None, "std": None, "n": n,
                "t_stat": None, "p_value": None, "signal": "insufficient",
            }
            continue

        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std  = math.sqrt(variance) if variance > 0 else 0

        if std < 1e-9:
            t_stat  = 0.0
            p_value = 1.0
        else:
            t_stat  = mean / (std / math.sqrt(n))
            p_value = t_pvalue(t_stat, n - 1)

        p_value = round(p_value, 4)
        if p_value < P_SIGNIFICANT and mean > 0:
            signal = "positive"
        elif p_value < P_SIGNIFICANT and mean < 0:
            signal = "negative"
        else:
            signal = "neutral"

        stats[month] = {
            "mean":    round(mean, 3),
            "std":     round(std, 3),
            "n":       n,
            "t_stat":  round(t_stat, 3),
            "p_value": p_value,
            "signal":  signal,
        }
    return stats


def _get_top_anomalies(monthly_stats: dict, top_n: int = 3) -> tuple:
    """有意なポジティブ/ネガティブ月をランキングする。"""
    positive = []
    negative = []

    for month, s in monthly_stats.items():
        if s.get("mean") is None or s.get("p_value") is None:
            continue
        if s["signal"] == "positive":
            positive.append((month, s["mean"], s["p_value"]))
        elif s["signal"] == "negative":
            negative.append((month, s["mean"], s["p_value"]))

    positive.sort(key=lambda x: (-x[1], x[2]))   # 平均高い順
    negative.sort(key=lambda x: (x[1], x[2]))    # 平均低い順

    return positive[:top_n], negative[:top_n]


# ==============================================================
# BLOCK 4: composite_score 補正量計算
# ==============================================================

def calc_season_adjustment(
    monthly_stats: dict,
    current_month: int,
) -> float:
    """
    現在月のアノマリーから composite_score への補正量を計算する。

    補正量の計算式:
      adj = avg_return × (1 - p_value) × scale_factor
      scale_factor = MAX_ADJUSTMENT / expected_max_return（≒5%）
    """
    s = monthly_stats.get(current_month, {})
    if not s or s.get("mean") is None or s.get("p_value") is None:
        return 0.0

    mean    = s["mean"]
    p_value = s["p_value"]
    signal  = s["signal"]

    if signal == "neutral":
        return 0.0

    # p値が低いほど（有意なほど）補正量を大きくする
    significance_weight = max(0, 1 - p_value / P_SIGNIFICANT)

    # 平均リターンをスケール（5%のリターン → MAX_ADJUSTMENTの50%程度）
    raw_adj = mean * significance_weight * (MAX_ADJUSTMENT / 10.0)

    return round(max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, raw_adj)), 1)


# ==============================================================
# BLOCK 5: レポート生成
# ==============================================================

MONTH_NAMES_JP = {
    1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
    7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月",
}

def format_anomaly_calendar(result: dict, sector_names: dict) -> str:
    """
    アノマリーカレンダーを Markdown 形式で生成する。

    出力例:
      ## 📅 季節アノマリーカレンダー（過去10年統計）
      ### 現在月（3月）のアノマリー
      | 業種 | 平均リターン | p値 | シグナル | 補正 |
      ...
    """
    current_month = result.get("current_month", datetime.now().month)
    anomalies     = result.get("anomalies", {})
    adjustments   = result.get("adjustments", {})
    market        = result.get("market_anomaly", {})

    lines = [
        "## 📅 季節アノマリーカレンダー（過去10年統計）",
        "",
        f"**現在月**: {MONTH_NAMES_JP[current_month]} "
        f"{'（' + KNOWN_ANOMALIES[current_month] + '）' if current_month in KNOWN_ANOMALIES else ''}",
        "",
    ]

    # 市場全体のアノマリー
    nikkei_stats = market.get("nikkei", {}).get("monthly_stats", {})
    nk = nikkei_stats.get(current_month, {})
    if nk.get("mean") is not None:
        sig_icon = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(nk["signal"], "⚪")
        lines += [
            f"### {sig_icon} 日経225 {MONTH_NAMES_JP[current_month]}アノマリー",
            f"  平均リターン: {nk['mean']:+.1f}%  p値: {nk['p_value']:.3f}  "
            f"標本数: {nk['n']}年  シグナル: {nk['signal']}",
            "",
        ]

    # 業種別アノマリー（有意なもののみ）
    active = [
        (code, data)
        for code, data in anomalies.items()
        if data.get("current_anomaly", {}).get("signal") != "neutral"
        and data.get("current_anomaly", {}).get("mean") is not None
    ]

    if active:
        lines += [
            f"### 業種別 {MONTH_NAMES_JP[current_month]}の有意なアノマリー",
            "",
            "| 業種 | 平均リターン | p値 | シグナル | composite補正 |",
            "|------|------------|-----|---------|-------------|",
        ]
        for code, data in sorted(active,
                                  key=lambda x: abs(x[1]["current_anomaly"].get("mean", 0)),
                                  reverse=True)[:10]:
            ca    = data["current_anomaly"]
            name  = sector_names.get(code, code)
            sign  = "📈" if ca["signal"] == "positive" else "📉"
            adj   = adjustments.get(code, 0)
            adj_s = f"{adj:+.1f}pt" if adj != 0 else "—"
            lines.append(
                f"| {sign} {name[:8]} | {ca['mean']:+.1f}% | "
                f"{ca['p_value']:.3f} | {ca['signal']} | {adj_s} |"
            )
        lines.append("")

    # 全月のサマリーカレンダー（日経225）
    lines += ["### 📆 日経225 全月サマリー（平均リターン）", ""]
    calendar_row = ""
    for m in range(1, 13):
        s = nikkei_stats.get(m, {})
        mean = s.get("mean", 0) or 0
        p    = s.get("p_value", 1) or 1
        icon = "🟢" if (p < 0.05 and mean > 0) else ("🔴" if (p < 0.05 and mean < 0) else "⚪")
        calendar_row += f"| {m}月{icon}{mean:+.1f}% "
    lines.append(calendar_row + "|")
    lines.append("")

    return "\n".join(lines)


# ==============================================================
# BLOCK 6: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / CACHE_FILENAME

def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(hours=CACHE_HOURS):
            return data
    except Exception:
        pass
    return None

def _save_cache(cfg, data: dict) -> None:
    try:
        p = _cache_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        data["cached_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_anomaly_season(cfg, sector_results: Optional[dict] = None) -> dict:
    """
    季節アノマリーを計算してcomposite_score補正量を返す。

    stock_analyzer_main.py から週次で呼び出す:
        _as = _load_local_module("anomaly_season", ...)
        as_result = _as.run_anomaly_season(cfg, results.get("sectors",{}))
        results["anomaly_season"] = as_result
        # セクタースコアに補正を適用
        for code, delta in as_result.get("adjustments",{}).items():
            if code in results["sectors"] and delta != 0:
                results["sectors"][code]["composite_score"] = round(
                    max(-30, min(100,
                        results["sectors"][code].get("composite_score",0) + delta)), 1)
                results["sectors"][code]["season_adjustment"] = delta

    Returns:
        anomaly_season result dict
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📅 季節アノマリー分析開始...")

    cached = _load_cache(cfg)
    if cached:
        adj = cached.get("adjustments", {})
        n_sig = sum(1 for v in adj.values() if v != 0)
        print(f"    ✅ キャッシュ使用（有意補正: {n_sig}業種）")
        return cached

    current_month = datetime.now().month
    print(f"    現在月: {current_month}月 / 過去{LOOKBACK_YEARS}年データ取得中...")

    # 業種名マップ
    sector_names = {
        code: info.get("name_ja", code)
        for code, info in getattr(cfg, "JAPAN_SECTORS", {}).items()
    }
    # config になければ SECTOR_LEAD_TICKERS のキーから類推
    for code in SECTOR_LEAD_TICKERS:
        if code not in sector_names:
            sector_names[code] = code

    # ── 業種別アノマリー計算 ────────────────────────────────
    anomalies   = {}
    adjustments = {}

    for sector_code, ticker in SECTOR_LEAD_TICKERS.items():
        monthly_rets = _fetch_monthly_returns(ticker)
        if not monthly_rets:
            continue

        grouped = _group_by_month(monthly_rets)
        stats   = _calc_monthly_stats(grouped)
        top_pos, top_neg = _get_top_anomalies(stats)

        current_stats = stats.get(current_month, {})
        adj = calc_season_adjustment(stats, current_month)

        if adj != 0:
            adjustments[sector_code] = adj

        anomalies[sector_code] = {
            "ticker":          ticker,
            "monthly_stats":   stats,
            "current_anomaly": current_stats,
            "top_positive":    top_pos,
            "top_negative":    top_neg,
        }

        if adj != 0:
            print(f"      [{sector_code}] {sector_names.get(sector_code,'?')}: "
                  f"{current_month}月 avg={current_stats.get('mean','N/A')}"
                  f"% p={current_stats.get('p_value','N/A')} adj={adj:+.1f}pt")
        time.sleep(FETCH_DELAY)

# ── [Wave 3 - FE-5] PEAD Adjustment 計算 ──────────────────────
    # pead_monitor が検出した PEAD 候補セクターに追加補正を加える。
    # 正 PEAD（上昇ドリフト）が季節アノマリーと同方向なら効果を強化。
    # 陰性 PEAD（下落ドリフト）は季節の上昇シグナルを弱める。
    pead_adjustments: dict = {}
    try:
        from pead_monitor import load_pead_candidates as _load_pead
        _pead_cache = _load_pead(cfg)

        # セクター別 PEAD スコアを集計（最大スコアを採用）
        _pead_by_sector: dict[str, dict] = {}
        for _ticker, _entry in _pead_cache.items():
            _sc  = str(_entry.get("sector_code", ""))
            _ps  = float(_entry.get("pead_score", 0.0))
            _dir = str(_entry.get("direction", "positive"))
            if _sc and _ps > 0:
                if _sc not in _pead_by_sector or _ps > _pead_by_sector[_sc]["score"]:
                    _pead_by_sector[_sc] = {"score": _ps, "direction": _dir}

        # PEAD スコアを季節補正に加味（最大 ±2.0pt）
        for _sc, _pead_info in _pead_by_sector.items():
            _ps  = _pead_info["score"]
            _dir = _pead_info["direction"]
            if _ps < 2.0:          # スコアが低すぎる場合はスキップ
                continue
            # PEAD adj: 正方向は +pt、陰性は -pt（季節補正量の方向に揃える）
            _sign = 1.0 if _dir == "positive" else -1.0
            _pead_adj = round(min(2.0, _ps * 0.2) * _sign, 1)
            pead_adjustments[_sc] = _pead_adj

        if pead_adjustments:
            _n_pead = len(pead_adjustments)
            print(f"    📈 PEAD補正: {_n_pead}業種")
    except Exception as _pe:
        pass   # PEAD module 未実行時はスキップ

    # 季節アノマリー + PEAD 補正を合算（combined_adjustments）
    combined_adjustments: dict = dict(adjustments)
    for _sc, _padj in pead_adjustments.items():
        _existing = combined_adjustments.get(_sc, 0.0)
        combined_adjustments[_sc] = round(
            max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, _existing + _padj)), 1
        )

    # ── 市場全体アノマリー ──────────────────────────────────
    market_anomaly = {}
    for mkt_name, mkt_ticker in MARKET_TICKERS.items():
        mkt_rets    = _fetch_monthly_returns(mkt_ticker)
        if mkt_rets:
            mkt_grouped = _group_by_month(mkt_rets)
            mkt_stats   = _calc_monthly_stats(mkt_grouped)
            market_anomaly[mkt_name] = {
                "ticker":       mkt_ticker,
                "monthly_stats": mkt_stats,
                "current_month_stats": mkt_stats.get(current_month, {}),
            }
        time.sleep(FETCH_DELAY)

    n_sig = sum(1 for v in adjustments.values() if v != 0)
    print(f"    ✅ アノマリー分析完了: {len(anomalies)}業種 / 有意補正{n_sig}業種")

    result = {
        "current_month":       current_month,
        "anomalies":           anomalies,
        "market_anomaly":      market_anomaly,
        "adjustments":         adjustments,           # 季節アノマリー単体
        "pead_adjustments":    pead_adjustments,      # Wave 3 - FE-5 PEAD補正単体
        "combined_adjustments": combined_adjustments, # Wave 3: 季節+PEAD合算
        "lookback_years":      LOOKBACK_YEARS,
        "timestamp":           datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["calendar_md"] = format_anomaly_calendar(result, sector_names)

    _save_cache(cfg, result)
    return result
