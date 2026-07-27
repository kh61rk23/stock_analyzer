# ==============================================================
# correlation_heatmap.py  v1.0  (3-2 / C-4)
# 保有銘柄・業種間の相関係数行列を計算
# ==============================================================
# 【役割】
#   portfolio_holdings.csv の保有銘柄 + 業種代表銘柄の
#   日次リターン相関行列を計算し、以下を出力する:
#     A. correlation_heatmap.csv    → Excel に相関マトリクスシートとして追加
#     B. correlation_latest.json   → alert_notifier / weekly_report が参照
#
# 【計算内容】
#   1. ポートフォリオ保有銘柄間の相関（集中リスク検出）
#   2. 保有銘柄 × 業種代表銘柄の相関（セクター偏り検出）
#   3. JP × US 横断相関（S&P500 / ナスダック / 日経 vs 個別株）
#   4. 相関クラスター分析（相関 > 0.7 のグループを特定）
#
# 【アラート条件】
#   portfolio 内で相関 > 0.8 の銘柄ペアを「集中リスク警告」として通知
#
# 【依存】yfinance / config.py / portfolio_holdings.csv（任意）
# 【呼出】from correlation_heatmap import run_correlation_heatmap
# ==============================================================

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 定数 ──────────────────────────────────────────────────────
LOOKBACK_DAYS     = 90    # 相関計算期間（営業日）
MIN_DATA_POINTS   = 20    # 最低データ点数（これ未満はスキップ）

import finance_logic
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'CH_HIGH_CORR_THRESH': 'HIGH_CORR_THRESH',
    'CH_LOW_CORR_THRESH': 'LOW_CORR_THRESH',
    'B4_CORRELATION_HEATMAP_LOOKBACK_DAYS': 'LOOKBACK_DAYS',
    'B4_CORRELATION_HEATMAP_CACHE_HOURS': 'CACHE_HOURS',
    'B5_CORRELATION_HEATMAP_FETCH_DELAY': 'FETCH_DELAY',
    'B6_CORRELATION_HEATMAP_MIN_DATA_POINTS': 'MIN_DATA_POINTS',
}

HIGH_CORR_THRESH  = 0.80  # 集中リスク警告閾値
LOW_CORR_THRESH   = -0.50 # 分散効果（ヘッジ）警告閾値
FETCH_DELAY       = 0.25  # yfinance レート制限（秒）
CACHE_HOURS       = 24    # キャッシュ有効時間

# ベンチマーク（相関の基準として常に含める）
BENCHMARK_TICKERS = {
    "^N225":  "日経225",
    "^GSPC":  "S&P500",
    "^TNX":   "米10年債",
    "GC=F":   "金",
    "CL=F":   "原油WTI",
}


# ==============================================================
# BLOCK 1: データ取得
# ==============================================================

def _load_portfolio_tickers(cfg) -> dict:
    """
    portfolio_holdings.csv から保有銘柄の ticker と name を取得する。
    ファイルがなければ空辞書を返す。

    Returns:
        {ticker: {"name": str, "sector_code": str}}
    """
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "portfolio_holdings.csv",
        Path(__file__).parent / "data" / "portfolio_holdings.csv",
        Path(__file__).parent / "portfolio_holdings.csv",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            result = {}
            with open(p, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    t = row.get("ticker", "").strip()
                    if t:
                        result[t] = {
                            "name":        row.get("name", t)[:20],
                            "sector_code": row.get("sector_code", ""),
                        }
            return result
        except Exception:
            pass
    return {}


def _build_target_tickers(cfg, portfolio_tickers: dict) -> dict:
    """
    相関計算対象ティッカーを組み立てる。
    ポートフォリオ + 業種代表 + ベンチマーク の和集合。

    Returns:
        {ticker: label}
    """
    targets = {}

    # ① ポートフォリオ保有銘柄
    for t, info in portfolio_tickers.items():
        targets[t] = f"[保有] {info['name']}"

    # ② 業種代表銘柄（SECTOR_REPRESENTATIVE_TICKERS）
    rep = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
    sector_names = {
        code: info.get("name_ja", code)
        for code, info in getattr(cfg, "JAPAN_SECTORS", {}).items()
    }
    for code, tickers in rep.items():
        sname = sector_names.get(code, code)
        for t in tickers[:1]:  # 代表1銘柄のみ（計算量削減）
            if t not in targets:
                targets[t] = f"[{sname}] {t}"

    # ③ ベンチマーク
    for t, label in BENCHMARK_TICKERS.items():
        targets[t] = label

    return targets


def _fetch_returns(tickers: list, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """
    yfinance で各銘柄の日次リターン時系列を取得する。

    Returns:
        {ticker: [float, ...]}  日次リターン（前日比%）のリスト
    """
    try:
        import yfinance as yf
    except ImportError:
        return {}

    end   = datetime.now()
    start = end - timedelta(days=int(lookback_days * 1.6))  # 営業日カバーのため多め取得

    returns = {}
    for t in tickers:
        try:
            hist = yf.Ticker(t).history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                auto_adjust=True,
            )
            if hist is None or len(hist) < MIN_DATA_POINTS:
                continue
            closes = hist["Close"].dropna().tolist()
            # 日次リターン（%）
            ret = [
                round((closes[i] - closes[i-1]) / closes[i-1] * 100, 4)
                for i in range(1, len(closes))
            ]
            if len(ret) >= MIN_DATA_POINTS:
                returns[t] = ret[-lookback_days:]
            time.sleep(FETCH_DELAY)
        except Exception:
            pass

    return returns


# ==============================================================
# BLOCK 2: 相関行列計算
# ==============================================================

def _pearson(xs: list, ys: list) -> Optional[float]:
    """2リストの Pearson 相関係数（numpy 不使用）。共通実装は finance_logic.pearson（U-7）。"""
    return finance_logic.pearson(xs, ys, min_n=MIN_DATA_POINTS, round_digits=3)


def calc_correlation_matrix(returns: dict) -> dict:
    """
    全ティッカーの相関行列を計算する。

    Returns:
        {
          "tickers":  [t1, t2, ...],
          "matrix":   [[corr_11, corr_12, ...], [corr_21, ...], ...],
          "high_pairs": [{"t1": str, "t2": str, "corr": float, "type": "risk"/"hedge"}],
        }
    """
    tickers = sorted(returns.keys())
    n = len(tickers)
    matrix = []

    high_pairs = []

    for i, ti in enumerate(tickers):
        row = []
        for j, tj in enumerate(tickers):
            if i == j:
                row.append(1.0)
            elif j < i:
                row.append(matrix[j][i])   # 対称行列の再利用
            else:
                corr = _pearson(returns[ti], returns[tj])
                row.append(corr if corr is not None else None)
                # 高相関ペアを記録
                if corr is not None:
                    if corr >= HIGH_CORR_THRESH:
                        high_pairs.append({"t1": ti, "t2": tj, "corr": corr, "type": "risk"})
                    elif corr <= LOW_CORR_THRESH:
                        high_pairs.append({"t1": ti, "t2": tj, "corr": corr, "type": "hedge"})
        matrix.append(row)

    high_pairs.sort(key=lambda x: abs(x["corr"]), reverse=True)

    return {
        "tickers":    tickers,
        "matrix":     matrix,
        "high_pairs": high_pairs[:20],
    }


# ==============================================================
# BLOCK 3: 集中リスク分析
# ==============================================================

def analyze_concentration_risk(
    corr_result:       dict,
    portfolio_tickers: dict,
    labels:            dict,
) -> dict:
    """
    ポートフォリオ内の集中リスクを分析する。

    Returns:
        {
          "risk_pairs":   [高相関の保有ペア],
          "hedge_pairs":  [低相関の分散効果ペア],
          "max_corr":     float,
          "avg_corr":     float,
          "risk_level":   "high" / "medium" / "low",
          "summary":      str,
        }
    """
    port_set = set(portfolio_tickers.keys())

    # ポートフォリオ内ペアに絞る
    risk_pairs  = []
    hedge_pairs = []
    all_corrs   = []

    for pair in corr_result.get("high_pairs", []):
        t1, t2 = pair["t1"], pair["t2"]
        both_in_portfolio = t1 in port_set and t2 in port_set
        if not both_in_portfolio:
            continue
        corr = pair["corr"]
        all_corrs.append(abs(corr))
        entry = {
            **pair,
            "label1": labels.get(t1, t1),
            "label2": labels.get(t2, t2),
        }
        if pair["type"] == "risk":
            risk_pairs.append(entry)
        else:
            hedge_pairs.append(entry)

    # 全保有ペアの平均相関も計算
    tickers = corr_result.get("tickers", [])
    port_idx = [i for i, t in enumerate(tickers) if t in port_set]
    matrix   = corr_result.get("matrix", [])
    pair_corrs = []
    for i in range(len(port_idx)):
        for j in range(i+1, len(port_idx)):
            pi, pj = port_idx[i], port_idx[j]
            v = matrix[pi][pj] if matrix and pi < len(matrix) and pj < len(matrix[pi]) else None
            if v is not None:
                pair_corrs.append(v)

    avg_corr = round(sum(pair_corrs) / len(pair_corrs), 3) if pair_corrs else None
    max_corr = round(max(pair_corrs), 3) if pair_corrs else None

    # リスクレベル判定
    if risk_pairs and len(risk_pairs) >= 3:
        risk_level = "high"
    elif risk_pairs:
        risk_level = "medium"
    else:
        risk_level = "low"

    # サマリーテキスト
    if risk_pairs:
        pair_str = " / ".join(f"{p['t1']}&{p['t2']}({p['corr']:.2f})" for p in risk_pairs[:3])
        summary = f"⚠️ 高相関ペア{len(risk_pairs)}組: {pair_str}"
    else:
        summary = "✅ ポートフォリオ内に高相関の集中リスクなし"

    if hedge_pairs:
        summary += f" | 分散効果{len(hedge_pairs)}組あり"

    return {
        "risk_pairs":  risk_pairs,
        "hedge_pairs": hedge_pairs,
        "avg_corr":    avg_corr,
        "max_corr":    max_corr,
        "risk_level":  risk_level,
        "summary":     summary,
    }


# ==============================================================
# BLOCK 4: CSV 出力（Excel 相関マトリクスシート用）
# ==============================================================

def _write_correlation_csv(corr_result: dict, labels: dict, output_path: Path) -> None:
    """
    相関行列を CSV に書き出す（BOM 付き UTF-8 / Excel 対応）。

    フォーマット:
      行1:    ヘッダー（空セル, ticker1, ticker2, ...）
      行2以降: ticker_i, corr(i,1), corr(i,2), ...
    """
    tickers = corr_result["tickers"]
    matrix  = corr_result["matrix"]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        # ヘッダー行
        header = ["銘柄"] + [f"{labels.get(t, t)}\n({t})" for t in tickers]
        writer.writerow(header)
        # データ行
        for i, ti in enumerate(tickers):
            row_label = f"{labels.get(ti, ti)} ({ti})"
            row_vals  = []
            for j in range(len(tickers)):
                v = matrix[i][j]
                row_vals.append(f"{v:.3f}" if v is not None else "N/A")
            writer.writerow([row_label] + row_vals)


def _write_high_pairs_csv(
    concentration: dict, output_path: Path
) -> None:
    """高相関・低相関ペア一覧を CSV 出力する。"""
    rows = []
    for pair in concentration.get("risk_pairs", []):
        rows.append({
            "type": "集中リスク",
            "ticker1": pair["t1"], "label1": pair.get("label1", ""),
            "ticker2": pair["t2"], "label2": pair.get("label2", ""),
            "correlation": pair["corr"],
            "note": "高相関（分散効果が低い）",
        })
    for pair in concentration.get("hedge_pairs", []):
        rows.append({
            "type": "分散効果",
            "ticker1": pair["t1"], "label1": pair.get("label1", ""),
            "ticker2": pair["t2"], "label2": pair.get("label2", ""),
            "correlation": pair["corr"],
            "note": "低相関（ヘッジ効果あり）",
        })
    if not rows:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ==============================================================
# BLOCK 5: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "correlation_latest.json"


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
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_correlation_heatmap(cfg) -> dict:
    """
    相関ヒートマップを計算して CSV と JSON を出力する。

    stock_analyzer_main.py から以下のように呼び出す:
        _ch = _load_local_module("correlation_heatmap", ...)
        ch_result = _ch.run_correlation_heatmap(cfg)
        results["correlation_heatmap"] = ch_result

    Returns:
        {
          "tickers":       [str, ...],
          "matrix":        [[float, ...], ...],
          "concentration": dict,   # 集中リスク分析結果
          "csv_path":      str,    # correlation_heatmap.csv のパス
          "pairs_csv":     str,    # high_pairs.csv のパス
          "summary":       str,    # アラート用テキスト
          "timestamp":     str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📊 相関ヒートマップ計算開始...")

    cached = _load_cache(cfg)
    if cached:
        print(f"    ✅ キャッシュ使用（{cached.get('cached_at', '')[:10]}）")
        return cached

    # 対象ティッカーを構築
    portfolio_tickers = _load_portfolio_tickers(cfg)
    labels = _build_target_tickers(cfg, portfolio_tickers)
    all_tickers = list(labels.keys())

    print(f"    対象: {len(all_tickers)}銘柄（保有{len(portfolio_tickers)} + 業種代表 + ベンチマーク）")

    # 日次リターン取得
    returns = _fetch_returns(all_tickers)
    valid_n = len(returns)
    print(f"    ✅ リターン取得: {valid_n}/{len(all_tickers)}銘柄")

    if valid_n < 2:
        print("    ⚠️  データ不足のため相関計算をスキップ")
        return {"skipped": True, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # 相関行列計算
    corr_result  = calc_correlation_matrix(returns)
    concentration = analyze_concentration_risk(corr_result, portfolio_tickers, labels)

    print(f"    {concentration['summary']}")

    # CSV 出力
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    heatmap_csv = data_dir / "dashboard" / "correlation_heatmap.csv"
    pairs_csv   = data_dir / "dashboard" / "correlation_high_pairs.csv"

    try:
        _write_correlation_csv(corr_result, labels, heatmap_csv)
        print(f"    💾 correlation_heatmap.csv 出力: {heatmap_csv}")
    except Exception as e:
        print(f"    ⚠️  CSV出力失敗: {e}")
        heatmap_csv = None

    try:
        _write_high_pairs_csv(concentration, pairs_csv)
    except Exception:
        pairs_csv = None

    result = {
        "tickers":       corr_result["tickers"],
        "labels":        labels,
        "high_pairs":    corr_result["high_pairs"][:10],
        "concentration": concentration,
        "csv_path":      str(heatmap_csv) if heatmap_csv else "",
        "pairs_csv":     str(pairs_csv) if pairs_csv else "",
        "summary":       concentration["summary"],
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_cache(cfg, result)
    print(f"    ✅ 相関ヒートマップ完了: {valid_n}銘柄 × {valid_n}銘柄")
    return result
