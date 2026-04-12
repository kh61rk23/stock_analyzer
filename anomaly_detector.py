# ==============================================================
# anomaly_detector.py  v1.0
# 統計的異常値検出 — 価格・出来高・スコアの外れ値を検出してアラート生成
# ==============================================================
# 【機能概要】
#   - 価格異常:  Z スコア / IQR 法で前日比・期間内変動の外れ値を検出
#   - 出来高異常: 過去 N 日平均との比率で急増を検出
#   - 複合異常:  「出来高急増 かつ 価格急変」= 最重要シグナル
#   - スコア異常: composite_score の業種間での外れ値（上位・下位）
#   - 対象:     JP業種代表銘柄 / US screener 通過銘柄 / マクロ指標ティッカー
#
# 【出力】
#   insights/macro/anomaly_YYYY-MM-DD.md   — 当日異常銘柄レポート
#   data/anomaly_latest.json               — 他モジュール（alert_notifier等）連携用
#
# 【alert_notifier.py との連携】
#   results["anomaly"] に本モジュールの返値を格納することで
#   extract_alerts() から参照し、異常アラートを Gmail/LINE 送信できる。
#   → anomaly_diff.txt の差分4を alert_notifier.py に適用すること。
#
# 【GitHub Actions 処理時間目安】
#   JP代表銘柄 33業種 × 最大3銘柄 = 最大99銘柄
#   US screener 結果銘柄（通常 10〜30 銘柄）
#   マクロ指標 7 ティッカー
#   yfinance 一括取得: ~20秒 / 分析: ~5秒
#   合計: ~25秒/日
# ==============================================================

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: 定数・閾値定義
# ==============================================================

# ── Z スコア閾値 ─────────────────────────────────────────────
# Z = (x - mean) / std で計算。この値を超えると異常とみなす。
# 2.0 = 上位/下位 2.3% / 2.5 = 上位/下位 0.6% / 3.0 = 上位/下位 0.1%
Z_THRESHOLD_HIGH   = 2.5   # 高感度異常（出来高急増に使用）
Z_THRESHOLD_MEDIUM = 2.0   # 中感度異常（価格変動に使用）

# ── IQR 倍率閾値 ──────────────────────────────────────────────
# IQR = Q3 - Q1。この倍率を超えると外れ値とみなす。
# 1.5 = 標準的外れ値 / 3.0 = 極端な外れ値
IQR_MULTIPLIER = 1.5

# ── 出来高倍率閾値 ────────────────────────────────────────────
VOLUME_SPIKE_RATIO = 3.0    # 過去 N 日平均の何倍以上で「急増」とするか
VOLUME_LOOKBACK    = 20     # 出来高の基準期間（営業日数）

# ── 価格変動率閾値（絶対値）─────────────────────────────────
PRICE_CHANGE_HIGH   = 0.05  # 1日 ±5% 以上 = HIGH
PRICE_CHANGE_MEDIUM = 0.03  # 1日 ±3% 以上 = MEDIUM

# ── データ取得期間 ────────────────────────────────────────────
HISTORY_PERIOD     = "3mo"  # 基準統計を計算するための価格履歴期間
MIN_DATA_POINTS    = 20     # 統計計算に必要な最小データ点数

# ── スコア異常閾値（業種 composite_score）────────────────────
SCORE_TOP_THRESHOLD    = 75.0   # この値以上 = 強い上昇シグナル
SCORE_BOTTOM_THRESHOLD = 30.0   # この値以下 = 強い下落シグナル
SCORE_CHANGE_THRESHOLD = 15.0   # 前日比でこの値以上変化 = 急変シグナル

# ── レポート出力最大件数 ─────────────────────────────────────
MAX_REPORT_ITEMS = 20   # レポートに記載する異常銘柄の上限

# 異常重要度ラベル
SEVERITY_ICON = {
    "critical": "🚨",
    "high":     "🔴",
    "medium":   "🟡",
    "low":      "🟢",
}


# ==============================================================
# BLOCK 2: データ取得
# ==============================================================

def _collect_target_tickers(cfg) -> dict:
    """
    検出対象ティッカーを収集する。
    戻り値: {ticker: {"label": str, "category": str}}

    収集元:
      1. SECTOR_REPRESENTATIVE_TICKERS (JP業種代表銘柄)
      2. マクロ指標ティッカー (MACRO_TICKERS)
      3. screener 通過銘柄（data/screener_latest.json があれば）
      4. us_screener 通過銘柄（data/us_screener_latest.json があれば）
    """
    targets = {}

    # ── 1. JP業種代表銘柄 ────────────────────────────────────
    sector_reps = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
    jp_sectors  = getattr(cfg, "JAPAN_SECTORS", {})
    for code, tickers_list in sector_reps.items():
        sector_name = jp_sectors.get(code, {})
        if isinstance(sector_name, dict):
            sector_name = sector_name.get("name_ja", code)
        else:
            sector_name = str(sector_name)
        for t in tickers_list[:3]:  # 代表3銘柄まで
            targets[t] = {
                "label":    f"JP:{sector_name}",
                "category": "jp_sector_rep",
                "sector":   code,
            }

    # ── 2. マクロ指標ティッカー ──────────────────────────────
    macro_tickers = getattr(cfg, "MACRO_TICKERS", {})
    macro_names = {
        "usdjpy":  "ドル円",
        "us10y":   "米10年債",
        "oil_wti": "WTI原油",
        "gold":    "金",
        "vix":     "VIX恐怖指数",
        "sp500":   "S&P500",
        "nikkei":  "日経平均",
    }
    for key, ticker in macro_tickers.items():
        targets[ticker] = {
            "label":    f"MACRO:{macro_names.get(key, key)}",
            "category": "macro",
            "sector":   None,
        }

    # ── 3. screener 通過銘柄（JSONキャッシュ）───────────────
    data_dir = Path(getattr(cfg, "DATA_DIR", "/tmp"))
    screener_json = data_dir / "screener_latest.json"
    if screener_json.exists():
        try:
            sc_data = json.loads(screener_json.read_text(encoding="utf-8"))
            for item in sc_data.get("passed", [])[:20]:
                t = item.get("ticker", "")
                if t and t not in targets:
                    targets[t] = {
                        "label":    f"JP_SCREENER:{item.get('sector_name_ja', '')}",
                        "category": "jp_screener",
                        "sector":   item.get("sector_code"),
                    }
        except Exception:
            pass

    # ── 4. us_screener 通過銘柄 ──────────────────────────────
    us_screener_json = data_dir / "us_screener_latest.json"
    if us_screener_json.exists():
        try:
            us_data = json.loads(us_screener_json.read_text(encoding="utf-8"))
            for item in us_data.get("passed", [])[:20]:
                t = item.get("ticker", "")
                if t and t not in targets:
                    targets[t] = {
                        "label":    f"US_SCREENER:{item.get('sector', '')}",
                        "category": "us_screener",
                        "sector":   None,
                    }
        except Exception:
            pass

    return targets


def fetch_price_history(tickers: list) -> dict:
    """
    yfinance 一括取得で対象銘柄の価格・出来高履歴を取得する。
    戻り値: {ticker: {"close": [float], "volume": [float], "dates": [str]}}
    """
    import yfinance as yf  # type: ignore

    if not tickers:
        return {}

    print(f"    {len(tickers)} 銘柄の価格履歴を取得中...")
    results = {}

    try:
        # 一括取得
        data = yf.download(
            tickers,
            period=HISTORY_PERIOD,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"    WARNING: 一括取得失敗 ({e}) → 個別取得にフォールバック")
        data = None

    for ticker in tickers:
        try:
            if data is not None and not data.empty:
                if len(tickers) == 1:
                    hist_close  = data["Close"]
                    hist_volume = data.get("Volume")
                elif ticker in data.columns.get_level_values(0):
                    hist_close  = data["Close"][ticker]
                    hist_volume = data["Volume"][ticker] if "Volume" in data else None
                else:
                    hist_close = hist_volume = None
            else:
                hist_close = hist_volume = None

            # fallback: 個別取得
            if hist_close is None or hist_close.dropna().empty:
                tk   = yf.Ticker(ticker)
                hist = tk.history(period=HISTORY_PERIOD, interval="1d")
                if hist is not None and not hist.empty:
                    hist_close  = hist["Close"]
                    hist_volume = hist["Volume"]
                else:
                    continue

            hist_close  = hist_close.dropna()
            if len(hist_close) < MIN_DATA_POINTS:
                continue

            dates = [
                d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
                for d in hist_close.index
            ]

            results[ticker] = {
                "close":  [float(v) for v in hist_close],
                "volume": ([float(v) for v in hist_volume.dropna()]
                           if hist_volume is not None and not hist_volume.dropna().empty
                           else []),
                "dates":  dates,
            }
        except Exception:
            continue

    ok = len(results)
    print(f"    OK: {ok}/{len(tickers)} 銘柄取得成功")
    return results


# ==============================================================
# BLOCK 3: 統計的異常検出エンジン
# ==============================================================

def _zscore(values: list) -> list:
    """Z スコアのリストを計算。標準偏差が 0 の場合は全て 0.0 を返す。"""
    if len(values) < 2:
        return [0.0] * len(values)
    mean = sum(values) / len(values)
    var  = sum((v - mean) ** 2 for v in values) / len(values)
    std  = var ** 0.5
    if std == 0:
        return [0.0] * len(values)
    return [(v - mean) / std for v in values]


def _iqr_bounds(values: list, multiplier: float = IQR_MULTIPLIER):
    """IQR 法による外れ値境界 (lower, upper) を計算する。"""
    if len(values) < 4:
        return None, None
    sorted_v = sorted(values)
    n = len(sorted_v)
    q1 = sorted_v[n // 4]
    q3 = sorted_v[(3 * n) // 4]
    iqr = q3 - q1
    return q1 - multiplier * iqr, q3 + multiplier * iqr


def _pct_change(values: list) -> list:
    """前日比変化率リスト（先頭は 0.0）"""
    if len(values) < 2:
        return [0.0] * len(values)
    changes = [0.0]
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev != 0:
            changes.append((values[i] - prev) / abs(prev))
        else:
            changes.append(0.0)
    return changes


def detect_price_anomaly(ticker: str, hist: dict) -> Optional[dict]:
    """
    価格異常を検出する。

    判定ロジック（3種類の手法を組み合わせ）:
      A. 1日変化率の絶対値が閾値を超える（PRICE_CHANGE_HIGH / MEDIUM）
      B. Z スコアが Z_THRESHOLD_MEDIUM を超える（直近価格の統計的外れ値）
      C. IQR 法で外れ値と判定される（頑健な外れ値検出）

    戻り値: 異常あり → dict / 正常 → None
    """
    close  = hist.get("close", [])
    dates  = hist.get("dates", [])
    if len(close) < MIN_DATA_POINTS or not dates:
        return None

    latest_price = close[-1]
    latest_date  = dates[-1]

    # A. 直近 1 日変化率
    changes  = _pct_change(close)
    last_chg = changes[-1]
    abs_chg  = abs(last_chg)

    severity = None
    method   = []

    if abs_chg >= PRICE_CHANGE_HIGH:
        severity = "high"
        method.append(f"1日変動{last_chg*100:+.2f}%（閾値±{PRICE_CHANGE_HIGH*100:.0f}%）")
    elif abs_chg >= PRICE_CHANGE_MEDIUM:
        severity = "medium"
        method.append(f"1日変動{last_chg*100:+.2f}%（閾値±{PRICE_CHANGE_MEDIUM*100:.0f}%）")

    # B. Z スコア（直近20日の変化率）
    recent_changes = changes[-VOLUME_LOOKBACK:]
    z_scores       = _zscore(recent_changes)
    last_z         = z_scores[-1]
    if abs(last_z) >= Z_THRESHOLD_MEDIUM:
        z_sev = "high" if abs(last_z) >= Z_THRESHOLD_HIGH else "medium"
        if severity is None or z_sev == "high":
            severity = z_sev
        method.append(f"Zスコア={last_z:.2f}（閾値±{Z_THRESHOLD_MEDIUM:.1f}）")

    # C. IQR 外れ値（終値そのもの）
    lb, ub = _iqr_bounds(close[:-1])  # 直近を除く過去データで境界計算
    if lb is not None:
        if latest_price < lb or latest_price > ub:
            if severity is None:
                severity = "medium"
            direction = "上方" if latest_price > ub else "下方"
            method.append(f"IQR外れ値（{direction}逸脱: lb={lb:.2f} ub={ub:.2f}）")

    if not severity:
        return None

    direction_label = "急騰" if last_chg > 0 else "急落"
    return {
        "ticker":        ticker,
        "anomaly_type":  "price",
        "severity":      severity,
        "latest_price":  round(latest_price, 4),
        "price_change":  round(last_chg * 100, 2),
        "z_score":       round(last_z, 2),
        "direction":     direction_label,
        "date":          latest_date,
        "methods":       method,
        "summary":       f"{direction_label} {last_chg*100:+.2f}% (Z={last_z:.2f})",
    }


def detect_volume_anomaly(ticker: str, hist: dict) -> Optional[dict]:
    """
    出来高異常を検出する。

    判定ロジック:
      A. 直近出来高 ÷ 過去 VOLUME_LOOKBACK 日平均 >= VOLUME_SPIKE_RATIO
      B. 出来高の Z スコアが Z_THRESHOLD_HIGH を超える

    戻り値: 異常あり → dict / 正常 → None
    """
    volume = hist.get("volume", [])
    dates  = hist.get("dates", [])
    if len(volume) < MIN_DATA_POINTS:
        return None

    latest_vol = volume[-1]
    if latest_vol == 0:
        return None

    # A. 倍率チェック
    baseline_vols = volume[-VOLUME_LOOKBACK - 1 : -1]  # 直近を除く N 日
    if not baseline_vols:
        return None
    avg_vol   = sum(baseline_vols) / len(baseline_vols)
    ratio     = latest_vol / avg_vol if avg_vol > 0 else 0.0

    # B. Z スコア
    z_list    = _zscore(volume[-VOLUME_LOOKBACK:])
    last_z    = z_list[-1]

    severity  = None
    method    = []

    if ratio >= VOLUME_SPIKE_RATIO:
        severity = "high" if ratio >= VOLUME_SPIKE_RATIO * 2 else "medium"
        method.append(f"出来高={ratio:.1f}倍（閾値{VOLUME_SPIKE_RATIO:.0f}倍）")

    if abs(last_z) >= Z_THRESHOLD_HIGH:
        z_sev = "high"
        if severity is None or z_sev == "high":
            severity = z_sev
        method.append(f"出来高Zスコア={last_z:.2f}（閾値±{Z_THRESHOLD_HIGH:.1f}）")

    if not severity:
        return None

    latest_date = dates[-1] if dates else "unknown"
    return {
        "ticker":       ticker,
        "anomaly_type": "volume",
        "severity":     severity,
        "volume_ratio": round(ratio, 2),
        "z_score":      round(last_z, 2),
        "avg_volume":   round(avg_vol, 0),
        "latest_volume": round(latest_vol, 0),
        "date":         latest_date,
        "methods":      method,
        "summary":      f"出来高急増 {ratio:.1f}倍 (Z={last_z:.2f})",
    }


def detect_combined_anomaly(
    price_anom: Optional[dict],
    volume_anom: Optional[dict],
) -> Optional[dict]:
    """
    価格異常 AND 出来高異常が同時発生している場合に複合異常を生成する。
    複合異常は単体より重要度を1段階上げる（最大 critical）。
    """
    if price_anom is None or volume_anom is None:
        return None

    sevmap   = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    revsev   = {0: "low", 1: "medium", 2: "high", 3: "critical"}
    p_level  = sevmap.get(price_anom["severity"], 1)
    v_level  = sevmap.get(volume_anom["severity"], 1)
    combined = min(3, max(p_level, v_level) + 1)  # 1段階上げ・上限 critical

    return {
        "ticker":        price_anom["ticker"],
        "anomaly_type":  "combined",
        "severity":      revsev[combined],
        "price_change":  price_anom.get("price_change"),
        "direction":     price_anom.get("direction"),
        "volume_ratio":  volume_anom.get("volume_ratio"),
        "date":          price_anom["date"],
        "methods":       price_anom["methods"] + volume_anom["methods"],
        "summary": (
            f"{price_anom.get('direction','')} {price_anom.get('price_change',0):+.2f}% "
            f"+ 出来高{volume_anom.get('volume_ratio',0):.1f}倍"
        ),
    }


# ==============================================================
# BLOCK 4: セクタースコア異常検出
# ==============================================================

def detect_score_anomalies(results: dict) -> list:
    """
    results["sectors"] の composite_score から業種間の外れ値を検出する。

    検出内容:
      1. スコア TOP 業種（>= SCORE_TOP_THRESHOLD）
      2. スコア BOTTOM 業種（<= SCORE_BOTTOM_THRESHOLD）
      3. スコア分布の Z スコア外れ値（業種間での統計的外れ値）

    戻り値: [{"sector_code", "sector_name", "score", "severity", "summary"}]
    """
    sectors = results.get("sectors", {})
    if not sectors:
        return []

    score_items = []
    for code, data in sectors.items():
        if isinstance(data, dict):
            score = data.get("composite_score")
            name  = data.get("sector_name_ja", code)
            if score is not None:
                score_items.append((code, name, float(score)))

    if len(score_items) < 3:
        return []

    scores   = [s for _, _, s in score_items]
    z_scores = _zscore(scores)

    anomalies = []
    for i, (code, name, score) in enumerate(score_items):
        z = z_scores[i]
        severity = None
        direction = None
        reason   = []

        if score >= SCORE_TOP_THRESHOLD:
            severity  = "high" if score >= 85 else "medium"
            direction = "high"
            reason.append(f"スコア={score:.1f}（上位閾値{SCORE_TOP_THRESHOLD}）")
        elif score <= SCORE_BOTTOM_THRESHOLD:
            severity  = "high" if score <= 20 else "medium"
            direction = "low"
            reason.append(f"スコア={score:.1f}（下位閾値{SCORE_BOTTOM_THRESHOLD}）")

        if abs(z) >= Z_THRESHOLD_MEDIUM:
            z_direction = "high" if z > 0 else "low"
            if severity is None:
                severity  = "medium"
                direction = z_direction
            reason.append(f"Zスコア={z:.2f}（業種間外れ値）")

        if severity:
            anomalies.append({
                "sector_code":  code,
                "sector_name":  name,
                "score":        round(score, 1),
                "z_score":      round(z, 2),
                "severity":     severity,
                "direction":    direction,
                "reasons":      reason,
                "summary":      f"スコア={score:.1f} Z={z:.2f}",
            })

    # 重要度・スコアでソート
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    anomalies.sort(key=lambda x: (sev_order.get(x["severity"], 4),
                                  -abs(x["score"] - 50)))
    return anomalies


# ==============================================================
# BLOCK 5: 全銘柄スキャン
# ==============================================================

def run_anomaly_scan(cfg, results: dict) -> dict:
    """
    全対象銘柄をスキャンして異常を検出する。

    戻り値:
      {
        "price_anomalies":    [dict],  # 価格異常リスト
        "volume_anomalies":   [dict],  # 出来高異常リスト
        "combined_anomalies": [dict],  # 複合異常リスト（最重要）
        "score_anomalies":    [dict],  # スコア異常リスト
        "all_anomalies":      [dict],  # 全異常をマージ・ソートしたリスト
        "scan_count":         int,     # スキャン銘柄数
        "anomaly_count":      int,     # 異常検出数
        "targets":            dict,    # スキャン対象ティッカー情報
      }
    """
    # 対象ティッカーの収集
    targets = _collect_target_tickers(cfg)
    tickers_list = list(targets.keys())

    if not tickers_list:
        return {
            "price_anomalies": [], "volume_anomalies": [],
            "combined_anomalies": [], "score_anomalies": [],
            "all_anomalies": [], "scan_count": 0,
            "anomaly_count": 0, "targets": {},
        }

    # 価格・出来高データ取得
    hist_data = fetch_price_history(tickers_list)

    price_anom_map  = {}
    volume_anom_map = {}

    for ticker, hist in hist_data.items():
        pa = detect_price_anomaly(ticker, hist)
        va = detect_volume_anomaly(ticker, hist)

        # ターゲットメタデータを付与
        meta = targets.get(ticker, {})
        if pa:
            pa["label"]    = meta.get("label", ticker)
            pa["category"] = meta.get("category", "unknown")
            price_anom_map[ticker] = pa
        if va:
            va["label"]    = meta.get("label", ticker)
            va["category"] = meta.get("category", "unknown")
            volume_anom_map[ticker] = va

    # 複合異常（価格+出来高）
    combined_anomalies = []
    for ticker in set(price_anom_map) & set(volume_anom_map):
        ca = detect_combined_anomaly(price_anom_map[ticker], volume_anom_map[ticker])
        if ca:
            ca["label"]    = targets.get(ticker, {}).get("label", ticker)
            ca["category"] = targets.get(ticker, {}).get("category", "unknown")
            combined_anomalies.append(ca)

    # スコア異常
    score_anomalies = detect_score_anomalies(results)

    # 全異常マージ・重要度順ソート
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_anomalies: list = []
    # combined を最優先
    all_anomalies.extend(combined_anomalies)
    all_anomalies.extend(list(price_anom_map.values()))
    all_anomalies.extend(list(volume_anom_map.values()))
    # score_anomalies は別フォーマットなので type を付けて追加
    for sa in score_anomalies:
        all_anomalies.append({**sa, "anomaly_type": "score"})

    # 重複除去（ticker + anomaly_type でユニーク化、combined 優先）
    seen = set()
    deduped = []
    for item in sorted(all_anomalies,
                        key=lambda x: sev_order.get(x.get("severity"), 4)):
        key = (item.get("ticker", item.get("sector_code", "")),
               item.get("anomaly_type", ""))
        # combined は price/volume に優先するためそのまま追加
        if item.get("anomaly_type") == "combined":
            deduped.append(item)
            seen.add((item.get("ticker", ""), "price"))
            seen.add((item.get("ticker", ""), "volume"))
        elif key not in seen:
            deduped.append(item)
            seen.add(key)

    total_anomalies = len(deduped)
    print(f"    異常検出: {total_anomalies}件 "
          f"（複合:{len(combined_anomalies)} "
          f"価格:{len(price_anom_map)} "
          f"出来高:{len(volume_anom_map)} "
          f"スコア:{len(score_anomalies)}）")

    return {
        "price_anomalies":    list(price_anom_map.values()),
        "volume_anomalies":   list(volume_anom_map.values()),
        "combined_anomalies": combined_anomalies,
        "score_anomalies":    score_anomalies,
        "all_anomalies":      deduped[:MAX_REPORT_ITEMS],
        "scan_count":         len(hist_data),
        "anomaly_count":      total_anomalies,
        "targets":            targets,
    }


# ==============================================================
# BLOCK 6: MDレポート生成
# ==============================================================

def generate_anomaly_report(scan_result: dict, today_str: str) -> str:
    """異常検出結果をMarkdownレポートに変換する"""

    combined = scan_result.get("combined_anomalies", [])
    price_a  = scan_result.get("price_anomalies",   [])
    volume_a = scan_result.get("volume_anomalies",  [])
    score_a  = scan_result.get("score_anomalies",   [])
    total    = scan_result.get("anomaly_count", 0)
    scanned  = scan_result.get("scan_count", 0)

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    lines = [
        f"# 🔍 異常検知レポート {today_str}",
        "",
        f"スキャン銘柄数: **{scanned}** | 異常検出: **{total}件**",
        f"（複合:{len(combined)} / 価格:{len(price_a)} / "
        f"出来高:{len(volume_a)} / スコア:{len(score_a)}）",
        "",
        f"*生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} JST*",
        "",
        "---",
        "",
    ]

    # ── 複合異常（最重要）───────────────────────────────────
    if combined:
        lines += [
            "## 🚨 複合異常（価格急変 + 出来高急増）",
            "",
            "| 銘柄 | カテゴリ | 変動 | 出来高倍率 | 重要度 | 日付 |",
            "|------|---------|------|-----------|-------|------|",
        ]
        for item in sorted(combined,
                           key=lambda x: sev_order.get(x.get("severity"), 4)):
            icon = SEVERITY_ICON.get(item["severity"], "")
            lines.append(
                f"| {item['ticker']} | {item.get('label','')} "
                f"| {item.get('direction','')} {item.get('price_change',0):+.2f}% "
                f"| {item.get('volume_ratio',0):.1f}x "
                f"| {icon} {item['severity'].upper()} "
                f"| {item.get('date','')} |"
            )
        lines += ["", "**検出手法:**", ""]
        for item in combined:
            for m in item.get("methods", []):
                lines.append(f"- [{item['ticker']}] {m}")
        lines.append("")

    # ── 価格異常 ─────────────────────────────────────────────
    if price_a:
        lines += [
            "## 🔴 価格異常",
            "",
            "| 銘柄 | カテゴリ | 変動 | Z スコア | 重要度 |",
            "|------|---------|------|---------|-------|",
        ]
        for item in sorted(price_a,
                           key=lambda x: sev_order.get(x.get("severity"), 4)):
            icon = SEVERITY_ICON.get(item["severity"], "")
            lines.append(
                f"| {item['ticker']} | {item.get('label','')} "
                f"| {item.get('direction','')} {item.get('price_change',0):+.2f}% "
                f"| {item.get('z_score',0):.2f} "
                f"| {icon} {item['severity'].upper()} |"
            )
        lines.append("")

    # ── 出来高異常 ───────────────────────────────────────────
    if volume_a:
        lines += [
            "## 🟡 出来高異常",
            "",
            "| 銘柄 | カテゴリ | 倍率 | Z スコア | 重要度 |",
            "|------|---------|------|---------|-------|",
        ]
        for item in sorted(volume_a,
                           key=lambda x: sev_order.get(x.get("severity"), 4)):
            icon = SEVERITY_ICON.get(item["severity"], "")
            lines.append(
                f"| {item['ticker']} | {item.get('label','')} "
                f"| {item.get('volume_ratio',0):.1f}x "
                f"| {item.get('z_score',0):.2f} "
                f"| {icon} {item['severity'].upper()} |"
            )
        lines.append("")

    # ── セクタースコア異常 ────────────────────────────────────
    if score_a:
        lines += [
            "## 📊 セクタースコア異常",
            "",
            "| 業種 | スコア | Z スコア | 方向 | 重要度 |",
            "|------|-------|---------|------|-------|",
        ]
        for item in score_a[:10]:
            icon = SEVERITY_ICON.get(item["severity"], "")
            direction_ja = "🔺高スコア" if item.get("direction") == "high" else "🔻低スコア"
            lines.append(
                f"| {item['sector_name']} ({item['sector_code']}) "
                f"| {item['score']:.1f} "
                f"| {item['z_score']:.2f} "
                f"| {direction_ja} "
                f"| {icon} {item['severity'].upper()} |"
            )
        lines.append("")

    if total == 0:
        lines += [
            "## ✅ 異常なし",
            "",
            "本日の全スキャン対象に統計的異常値は検出されませんでした。",
            "",
        ]

    lines += [
        "---",
        "",
        "**検出手法:** Z スコア法 / IQR 法 / 変動率閾値",
        f"**閾値設定:** 価格変動 ±{PRICE_CHANGE_MEDIUM*100:.0f}%(MEDIUM) "
        f"/ ±{PRICE_CHANGE_HIGH*100:.0f}%(HIGH) "
        f"| 出来高 {VOLUME_SPIKE_RATIO:.0f}倍 "
        f"| Z スコア ±{Z_THRESHOLD_MEDIUM:.1f}(MEDIUM) "
        f"/ ±{Z_THRESHOLD_HIGH:.1f}(HIGH)",
        f"*データソース: yfinance | 生成: {today_str}*",
    ]

    return "\n".join(lines)


# ==============================================================
# BLOCK 7: ファイル保存
# ==============================================================

def save_anomaly_outputs(cfg, scan_result: dict, md_report: str,
                          today_str: str) -> tuple:
    """MDレポートとJSONを所定ディレクトリへ保存する"""
    macro_dir = Path(cfg.INSIGHT_DIR) / "macro"
    data_dir  = Path(cfg.DATA_DIR)
    macro_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # MD レポート
    md_path = macro_dir / f"anomaly_{today_str}.md"
    md_path.write_text(md_report, encoding="utf-8")
    print(f"    MD: {md_path}")

    # JSON（history等の大型データを除いてスリム化）
    slim_result = {
        k: v for k, v in scan_result.items()
        if k != "targets"  # targets は行数が多いので省略
    }
    slim_result["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    slim_result["date"]         = today_str

    json_path = data_dir / "anomaly_latest.json"
    json_path.write_text(
        json.dumps(slim_result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"    JSON: {json_path}")

    return str(md_path), str(json_path)


# ==============================================================
# BLOCK 8: ロガー簡易互換
# ==============================================================

class _SimpleLogger:
    def add(self, module: str, status: str, message: str):
        icon = {"success": "✅", "error": "❌", "warn": "⚠️"}.get(status, "ℹ️")
        print(f"  {icon} [{module}] {message}")


# ==============================================================
# BLOCK 9: メインエントリポイント
# ==============================================================

def run_anomaly_detector(cfg, results: dict, logger=None) -> dict:
    """
    外部から呼び出すエントリポイント。
    stock_analyzer_main.py の run_all_tasks() からは以下で呼ぶ:

        _ad = _load_local_module("anomaly_detector",
                                 "/content/anomaly_detector.py",
                                 cfg.DRIVE_BASE / "anomaly_detector.py")
        if _ad:
            ad_result = _ad.run_anomaly_detector(cfg, results, logger)
            results["anomaly"] = ad_result

    ※ sectors 分析の後に実行すること（score_anomalies に sectors が必要）
    ※ screener の後に実行すること（screener_latest.json があれば活用する）

    戻り値:
        {
          "combined_anomalies": [dict],  # 複合異常（最重要）
          "price_anomalies":    [dict],
          "volume_anomalies":   [dict],
          "score_anomalies":    [dict],
          "all_anomalies":      [dict],  # ソート済み全件（上限 MAX_REPORT_ITEMS）
          "anomaly_count":      int,
          "scan_count":         int,
          "md_path":            str,
          "json_path":          str,
        }
    """
    if logger is None:
        logger = _SimpleLogger()

    today_str = datetime.now().strftime("%Y-%m-%d")

    print("\n  🔍 異常検知スキャン開始...")

    # ── Step 1: スキャン実行 ─────────────────────────────────
    print("  [1/3] 銘柄スキャン...")
    try:
        scan_result = run_anomaly_scan(cfg, results)
        ac = scan_result.get("anomaly_count", 0)
        sc = scan_result.get("scan_count", 0)
        logger.add(
            "anomaly_detector", "success",
            f"スキャン:{sc}銘柄 異常:{ac}件 "
            f"（複合:{len(scan_result.get('combined_anomalies',[]))} "
            f"価格:{len(scan_result.get('price_anomalies',[]))} "
            f"出来高:{len(scan_result.get('volume_anomalies',[]))} "
            f"スコア:{len(scan_result.get('score_anomalies',[]))}）"
        )
    except Exception as e:
        logger.add("anomaly_detector", "error", f"スキャン失敗: {str(e)[:80]}")
        return {
            "combined_anomalies": [], "price_anomalies": [],
            "volume_anomalies": [], "score_anomalies": [],
            "all_anomalies": [], "anomaly_count": 0,
            "scan_count": 0, "md_path": "", "json_path": "",
        }

    # ── Step 2: レポート生成 ─────────────────────────────────
    print("  [2/3] MDレポート生成...")
    md_report = generate_anomaly_report(scan_result, today_str)

    # ── Step 3: ファイル保存 ─────────────────────────────────
    print("  [3/3] ファイル保存...")
    try:
        md_path, json_path = save_anomaly_outputs(
            cfg, scan_result, md_report, today_str
        )
    except Exception as e:
        logger.add("anomaly_detector", "error", f"保存失敗: {str(e)[:80]}")
        md_path = json_path = ""

    critical_count = sum(
        1 for a in scan_result.get("all_anomalies", [])
        if a.get("severity") in ("critical", "high")
    )
    if critical_count:
        logger.add(
            "anomaly_detector", "warn",
            f"HIGH/CRITICAL 異常: {critical_count}件 → alert_notifier でアラート送信予定"
        )

    print(f"  ✅ 異常検知完了: {scan_result.get('anomaly_count',0)}件")

    return {
        **scan_result,
        "md_path":   md_path,
        "json_path": json_path,
    }


# ==============================================================
# スタンドアロン実行（テスト用）
# ==============================================================
if __name__ == "__main__":
    from pathlib import Path

    class _MockCfg:
        DATA_DIR    = Path("/tmp/anomaly_test/data")
        INSIGHT_DIR = Path("/tmp/anomaly_test/insights")
        SECTOR_REPRESENTATIVE_TICKERS = {
            "1028": ["8306.T", "8316.T"],
            "1017": ["7203.T"],
            "1016": ["6758.T"],
        }
        JAPAN_SECTORS = {
            "1028": {"name_ja": "銀行業"},
            "1017": {"name_ja": "輸送用機器"},
            "1016": {"name_ja": "電気機器"},
        }
        MACRO_TICKERS = {
            "sp500":  "^GSPC",
            "nikkei": "^N225",
            "vix":    "^VIX",
        }

    cfg = _MockCfg()
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (cfg.INSIGHT_DIR / "macro").mkdir(parents=True, exist_ok=True)

    # モックの results（screener等は空）
    mock_results = {
        "sectors": {
            "1028": {"composite_score": 80.0, "sector_name_ja": "銀行業"},
            "1017": {"composite_score": 55.0, "sector_name_ja": "輸送用機器"},
            "1016": {"composite_score": 25.0, "sector_name_ja": "電気機器"},
        }
    }

    result = run_anomaly_detector(cfg, mock_results)
    print("\n=== テスト結果 ===")
    print(f"スキャン銘柄数: {result.get('scan_count', 0)}")
    print(f"異常検出数:     {result.get('anomaly_count', 0)}")
    print(f"複合異常:       {len(result.get('combined_anomalies', []))}")
    print(f"スコア異常:     {len(result.get('score_anomalies', []))}")
    if result.get("all_anomalies"):
        print("\n検出された異常:")
        for a in result["all_anomalies"][:5]:
            print(f"  [{a.get('severity','?').upper()}] "
                  f"{a.get('ticker', a.get('sector_code','?'))} "
                  f"- {a.get('summary','')}")
