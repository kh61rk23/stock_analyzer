# ==============================================================
# correlation_audit.py  v1.0  (3-B)
# 米国債 × 33業種 相関監査 + ヒートマップ
# ==============================================================
# 【役割】
#   ① 米10年債利回り(^TNX) と業種代表銘柄の日次リターンの
#     ローリング相関係数を計算 → 金利感応度を時系列で可視化
#       正相関: 金利上昇が追い風（銀行・保険など）
#       負相関: 金利上昇が逆風（不動産・成長株など）
#
#   ② 業種間の日次リターン相関行列を計算
#     → ポートフォリオ分散効果の観点で「高相関クラスター」を検出
#
# 【返り値スキーマ】
#   {
#     "rolling_corr":  {sector_code: [{"date":str, "corr":float}, ...]},
#     "sector_matrix": [[float,...],...],   # NxN 業種間相関行列
#     "sector_labels": [str, ...],          # 業種名(短縮8文字)
#     "sector_codes":  [str, ...],          # 業種コード(行列と同順)
#     "high_pos": [{sector_code,sector_name,latest_corr,interpretation}, ...],
#     "high_neg": [{sector_code,sector_name,latest_corr,interpretation}, ...],
#     "analysis_month": "2026-02",
#     "timestamp":      "2026-02-27 08:00:00",
#   }
#   失敗時は {} を返す(例外を上位に伝播しない)
#
# 【依存】config.py / yfinance / pandas / numpy
# 【呼出】from correlation_audit import run_correlation_audit
# ==============================================================

from datetime import datetime, timedelta
from pathlib import Path
import time


# ==============================================================
# BLOCK 1: 価格データ取得
# ==============================================================

def fetch_price_data(tickers: list, days: int = 180, max_retries: int = 2):
    """
    複数銘柄の日次終値 DataFrame を一括取得する。
    ^TNX（米10年債利回り）も同一呼び出しで取得できる。

    Returns:
        pd.DataFrame | None  ※ 列=ticker、行=日付
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        print("  ⚠️  yfinance/pandas 未インストール")
        return None

    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days + 15)

    for attempt in range(1, max_retries + 1):
        try:
            raw = yf.download(
                tickers,
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=True,
                progress=False,
                timeout=30,
            )
            if raw is None or raw.empty:
                time.sleep(3)
                continue
            import pandas as pd
            close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) \
                    else raw[["Close"]].rename(columns={"Close": tickers[0]})
            return close.dropna(how="all").tail(days)
        except Exception as e:
            if attempt == max_retries:
                print(f"    ⚠️  価格データ取得失敗: {str(e)[:60]}")
                return None
            time.sleep(3)
    return None


# ==============================================================
# BLOCK 2: リターン計算ユーティリティ
# ==============================================================

def to_daily_returns(prices_df):
    """日次終値 DataFrame → 前日比変化率 DataFrame"""
    return prices_df.pct_change().dropna()


def sector_avg_return(returns_df, tickers: list):
    """
    業種内銘柄の日次平均リターン Series を返す。
    returns_df に存在するティッカーのみ使用（欠損銘柄は除外）。
    """
    valid = [t for t in tickers if t in returns_df.columns]
    if not valid:
        return None
    return returns_df[valid].mean(axis=1)


# ==============================================================
# BLOCK 3: 米10年債 × 業種 ローリング相関
# ==============================================================

def calc_rolling_corr(us10y_ret, sector_ret, window: int = 20) -> list:
    """
    米10年債利回り変化率と業種リターンの
    N 営業日ローリング相関係数を計算する。

    Args:
        us10y_ret  : pd.Series（米10年債日次変化率）
        sector_ret : pd.Series（業種平均日次リターン）
        window     : 営業日数（20 ≈ 1 ヶ月）

    Returns:
        [{"date": "YYYY-MM-DD", "corr": float}, ...]
    """
    import pandas as pd
    df = pd.DataFrame({"us10y": us10y_ret, "sector": sector_ret}).dropna()
    rolling = df["us10y"].rolling(window=window).corr(df["sector"])
    result  = rolling.dropna().reset_index()
    result.columns = ["date", "corr"]
    result["date"] = result["date"].dt.strftime("%Y-%m-%d")
    result["corr"] = result["corr"].round(3)
    return result.to_dict(orient="records")


def run_rolling_corr_all(cfg, prices_df) -> dict:
    """
    全業種について米10年債とのローリング相関を計算する。

    Returns:
        {sector_code: [{"date": "YYYY-MM-DD", "corr": float}, ...]}
    """
    rep    = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
    window = getattr(cfg, "CORR_ROLLING_WINDOW", 20)

    if "^TNX" not in prices_df.columns:
        print("    ⚠️  ^TNX が取得データに含まれていません")
        return {}

    returns_df = to_daily_returns(prices_df)
    us10y_ret  = returns_df["^TNX"].dropna()
    result     = {}

    for code, tickers in rep.items():
        s_ret = sector_avg_return(returns_df, tickers)
        if s_ret is None:
            continue
        records = calc_rolling_corr(us10y_ret, s_ret, window=window)
        if records:
            result[code] = records

    return result


# ==============================================================
# BLOCK 4: 業種間相関行列
# ==============================================================

def calc_sector_matrix(cfg, prices_df) -> tuple:
    """
    業種代表銘柄の日次平均リターンで N×N 業種間相関行列を計算する。

    Returns:
        (matrix: list[list[float]],
         labels: list[str],     # 業種名（短縮）
         codes:  list[str])     # 業種コード（matrix と同順）
    """
    rep        = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
    returns_df = to_daily_returns(prices_df)

    import pandas as pd
    sector_series = {}
    labels = []
    codes  = []

    for code, tickers in rep.items():
        s_ret = sector_avg_return(returns_df, tickers)
        if s_ret is None:
            continue
        name = cfg.JAPAN_SECTORS.get(code, {}).get("name_ja", code)
        sector_series[code] = s_ret
        labels.append(name[:8])   # 表示幅のため短縮
        codes.append(code)

    if len(sector_series) < 2:
        return [], [], []

    df     = pd.DataFrame(sector_series).dropna()
    matrix = df.corr().values.round(3).tolist()
    return matrix, labels, codes


# ==============================================================
# BLOCK 5: 注目業種の抽出
# ==============================================================

def _interpret_corr(c: float) -> str:
    """相関係数に投資的な解釈テキストを付与する。"""
    if c >= 0.6:  return "金利上昇と強く連動（追い風期待大）"
    if c >= 0.3:  return "金利上昇とやや連動"
    if c <= -0.6: return "金利上昇に強く逆行（逆風リスク大）"
    if c <= -0.3: return "金利上昇とやや逆行"
    return "相関弱い（独自要因が優勢）"


def extract_notable(rolling_corr: dict, cfg) -> tuple:
    """
    直近ウィンドウの相関係数が高い（正/負）業種を上位 N 件抽出する。

    Returns:
        (high_pos, high_neg)
        各要素: {sector_code, sector_name, latest_corr, interpretation}
    """
    pos_thr = getattr(cfg, "CORR_POS_THRESHOLD",  0.3)
    neg_thr = getattr(cfg, "CORR_NEG_THRESHOLD", -0.3)
    top_n   = getattr(cfg, "CORR_TOP_N", 5)

    items = []
    for code, records in rolling_corr.items():
        if not records:
            continue
        corr = records[-1]["corr"]
        name = cfg.JAPAN_SECTORS.get(code, {}).get("name_ja", code)
        items.append({
            "sector_code":    code,
            "sector_name":    name,
            "latest_corr":    corr,
            "interpretation": _interpret_corr(corr),
        })

    sorted_all = sorted(items, key=lambda x: x["latest_corr"], reverse=True)
    high_pos   = [x for x in sorted_all if x["latest_corr"] >= pos_thr][:top_n]
    high_neg   = sorted(
        [x for x in sorted_all if x["latest_corr"] <= neg_thr],
        key=lambda x: x["latest_corr"]
    )[:top_n]
    return high_pos, high_neg


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_correlation_audit(cfg) -> dict:
    """
    米10年債 × 33業種 相関監査を実行する。
    失敗時は {} を返し、例外を上位に伝播しない。
    """
    print("\n  🔗 相関監査 開始...")
    t0 = datetime.now()

    days = getattr(cfg, "CORR_LOOKBACK_DAYS", 180)
    rep  = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})

    # 取得ティッカー: ^TNX + 全業種代表銘柄（重複除去・順序保持）
    all_tickers = ["^TNX"]
    for tickers in rep.values():
        for t in tickers:
            if t not in all_tickers:
                all_tickers.append(t)

    print(f"    ティッカー取得: {len(all_tickers)} 銘柄 × {days} 日分...")
    prices_df = fetch_price_data(all_tickers, days=days)

    if prices_df is None or "^TNX" not in prices_df.columns:
        print("    ⚠️  ^TNX を含む価格データの取得に失敗しました")
        return {}

    # ① ローリング相関（^TNX × 各業種）
    window = getattr(cfg, "CORR_ROLLING_WINDOW", 20)
    print(f"    📐 米10年債×業種 ローリング相関（窓幅: {window}営業日）...")
    rolling_corr = run_rolling_corr_all(cfg, prices_df)
    print(f"      → {len(rolling_corr)} 業種")

    # ② 業種間相関行列
    print(f"    📐 業種間相関行列...")
    matrix, labels, codes = calc_sector_matrix(cfg, prices_df)
    print(f"      → {len(labels)}×{len(labels)} 行列")

    # ③ 注目業種抽出
    high_pos, high_neg = extract_notable(rolling_corr, cfg)

    elapsed = (datetime.now() - t0).seconds
    print(f"\n  ✅ 相関監査完了（{elapsed}秒）"
          f"  正相関:{len(high_pos)}業種 / 負相関:{len(high_neg)}業種")

    result = {
        "rolling_corr":   rolling_corr,
        "sector_matrix":  matrix,
        "sector_labels":  labels,
        "sector_codes":   codes,
        "high_pos":       high_pos,
        "high_neg":       high_neg,
        "analysis_month": datetime.now().strftime("%Y-%m"),
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_correlation_report(result, cfg)
    return result


# ==============================================================
# BLOCK 7: Markdown レポート保存
# ==============================================================

def _save_correlation_report(result: dict, cfg) -> None:
    """insights/macro/correlation_YYYY-MM.md に保存する。"""
    month     = datetime.now().strftime("%Y-%m")
    macro_dir = Path(getattr(cfg, "MACRO_DIR", ""))
    if not str(macro_dir):
        return

    try:
        macro_dir.mkdir(parents=True, exist_ok=True)
        days   = getattr(cfg, "CORR_LOOKBACK_DAYS",  180)
        window = getattr(cfg, "CORR_ROLLING_WINDOW",   20)

        lines = [
            f"# 米10年債×業種 相関監査レポート（{month}）", "",
            f"**分析期間**: 直近{days}日 / **ローリング窓幅**: {window}営業日", "",
            "## 📈 正相関業種（金利上昇が追い風）", "",
            "| 業種 | コード | 相関係数 | 解釈 |",
            "|------|--------|---------|------|",
        ]
        for x in result.get("high_pos", []):
            lines.append(
                f"| {x['sector_name']} | {x['sector_code']} "
                f"| **{x['latest_corr']:+.3f}** | {x['interpretation']} |"
            )
        lines += ["", "## 📉 負相関業種（金利上昇が逆風）", "",
                  "| 業種 | コード | 相関係数 | 解釈 |",
                  "|------|--------|---------|------|"]
        for x in result.get("high_neg", []):
            lines.append(
                f"| {x['sector_name']} | {x['sector_code']} "
                f"| **{x['latest_corr']:+.3f}** | {x['interpretation']} |"
            )

        # 業種間高相関ペア TOP10
        matrix = result.get("sector_matrix", [])
        labels = result.get("sector_labels", [])
        if matrix and labels:
            pairs = []
            for i in range(len(labels)):
                for j in range(i + 1, len(labels)):
                    pairs.append((matrix[i][j], labels[i], labels[j]))
            pairs.sort(reverse=True)
            lines += ["", "## 業種間 高相関ペア TOP10", "",
                      "| 業種A | 業種B | 相関係数 |",
                      "|-------|-------|---------|"]
            for corr, a, b in pairs[:10]:
                lines.append(f"| {a} | {b} | {corr:+.3f} |")

        out = macro_dir / f"correlation_{month}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"    💾 相関レポート保存: correlation_{month}.md")

    except Exception as e:
        print(f"    ⚠️  相関レポート保存失敗: {str(e)[:60]}")


# 単体テスト
if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    result = run_correlation_audit(cfg)
    print(f"\n正相関: {[x['sector_name'] for x in result.get('high_pos', [])]}")
    print(f"負相関: {[x['sector_name'] for x in result.get('high_neg', [])]}")
    print(f"行列:   {len(result.get('sector_labels',[]))}×{len(result.get('sector_labels',[]))}")
