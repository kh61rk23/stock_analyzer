# ==============================================================
# gov_data_pipeline.py
# 政府データ取得パイプライン  v1.0
# ==============================================================
# 概要:
#   農水省の食品価格動向調査（野菜）を週次で自動取得し、
#   外食関連株の株価と組み合わせて分析するパイプラインです。
#
# 実行環境: Google Colab（無料）
# 保存先  : Google Drive /content/drive/MyDrive/stock_analyzer/
#
# 【収集する政府データ】
#   - 農水省 食品価格動向調査（野菜）  ← メインデータ
#   - e-Stat API（総務省 CPI・統計）   ← 拡張オプション
#
# 【分析対象の外食関連銘柄（変更可能）】
#   9861.T  吉野家HD
#   3197.T  すかいらーくHD
#   8160.T  くらコーポ
#   7581.T  サイゼリヤ
#   3053.T  ペッパーフード
#   9887.T  松屋フーズ
#
# ==============================================================

import subprocess, sys, os, shutil

# ── パッケージインストール ──────────────────────────────────
def install_packages():
    pkgs = ["pandas", "yfinance", "requests", "openpyxl",
            "matplotlib", "scipy", "gspread", "google-auth"]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + pkgs)
    print("✅ パッケージインストール完了")

install_packages()

# ── インポート ──────────────────────────────────────────────
import pandas as pd
import yfinance as yf
import requests
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from scipy import stats

# ==============================================================
# BLOCK 0: 設定
# ==============================================================

# Google Drive のベースディレクトリ
# 実行環境に応じてパスを切り替え
import os as _os
if _os.environ.get("STOCK_ANALYZER_BASE"):
    # GitHub Actions: run_headless.py が設定した環境変数を使用
    DRIVE_BASE  = Path(_os.environ["STOCK_ANALYZER_BASE"])
else:
    # Colab: 従来のパス
    DRIVE_BASE  = Path("/content/drive/MyDrive/stock_analyzer")

DATA_DIR     = DRIVE_BASE / "data"
RESULT_DIR   = DRIVE_BASE / "results"
INSIGHT_DIR  = DRIVE_BASE / "insights"

# 野菜価格API（農水省データを非公式APIで提供、無料・無認証）
VEGPRICE_API_ALL = "https://api.cultivationdata.net/yasai_kakaku?div=all&type=csv"
VEGPRICE_API_LATEST = "https://api.cultivationdata.net/yasai_kakaku?type=csv"

# 分析対象の外食関連銘柄
RESTAURANT_STOCKS = {
    "9861.T": "吉野家HD",
    "3197.T": "すかいらーくHD",
    "8160.T": "くらコーポ",
    "7581.T": "サイゼリヤ",
    "9887.T": "松屋フーズ",
    "3053.T": "ペッパーフード",
}

# 野菜品目（日本語表記）
VEG_COLUMNS_JA = {
    "キャベツ": "cabbage",
    "ねぎ":     "negi",
    "レタス":   "lettuce",
    "ばれいしょ": "potato",
    "たまねぎ": "onion",
    "きゅうり": "cucumber",
    "トマト":   "tomato",
    "にんじん": "carrot",
    "はくさい": "hakusai",
    "だいこん": "daikon",
}

# ==============================================================
# BLOCK 1: Google Drive セットアップ
# ==============================================================

def setup_drive():
    """Google Drive マウントとフォルダ作成"""
    try:
        from google.colab import drive
        drive.mount("/content/drive")
        print("✅ Google Drive マウント完了")
    except Exception:
        print("⚠️  Colab 以外で実行中（Drive マウントスキップ）")

    # GitHub Actions では DRIVE_BASE 配下は既に run_headless.py が作成済み
    # Colab では従来通り作成
    for d in [DATA_DIR, RESULT_DIR, INSIGHT_DIR]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass  # GitHub Actions では /content/ へのアクセス不可のためスキップ
    print(f"✅ フォルダ準備完了: {DRIVE_BASE}")


# ==============================================================
# BLOCK 2: 農水省 野菜価格データ取得
# ==============================================================

def fetch_vegprice_all() -> pd.DataFrame:
    """
    農水省 食品価格動向調査（野菜）の全件データを取得。
    ソース: cultivationdata.net（農水省データの非公式 API）
    更新:  毎週火曜日（農水省の公表サイクルに同期）
    単位:  円/kg（小売価格）
    """
    print("📥 野菜価格データを取得中...")
    r = requests.get(VEGPRICE_API_ALL, timeout=15)
    r.raise_for_status()

    from io import StringIO
    df = pd.read_csv(StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"週": "date"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    # "-" を NaN に変換、数値型に
    df = df.replace("-", float("nan"))
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    print(f"✅ 野菜価格データ取得完了: {len(df)}週分 ({df.index[0].date()} ～ {df.index[-1].date()})")
    return df


def save_vegprice(df: pd.DataFrame):
    """野菜価格データを CSV と JSON で保存"""
    csv_path = DATA_DIR / "vegprice_all.csv"
    json_path = DATA_DIR / "vegprice_latest.json"

    df.to_csv(csv_path, encoding="utf-8-sig")

    # 最新データを JSON で保存（スプレッドシート連携用）
    latest = df.iloc[-1].dropna().to_dict()
    latest["date"] = df.index[-1].strftime("%Y/%m/%d")
    latest["fetched_at"] = datetime.now().isoformat()
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    print(f"✅ 保存完了: {csv_path}")
    print(f"✅ 保存完了: {json_path}")
    return csv_path


# ==============================================================
# BLOCK 3: 外食株の株価データ取得
# ==============================================================

def fetch_stock_prices(tickers: dict, period: str = "3y") -> pd.DataFrame:
    """
    外食関連株の週次株価データを yfinance から取得。
    period: "1y" / "2y" / "3y"
    """
    print(f"📥 株価データを取得中（{period}）...")
    dfs = []
    for ticker, name in tickers.items():
        try:
            hist = yf.Ticker(ticker).history(period=period, interval="1wk")
            if hist.empty:
                print(f"  ⚠️  {name}({ticker}): データなし")
                continue
            s = hist["Close"].rename(name)
            dfs.append(s)
            print(f"  ✅ {name}({ticker}): {len(hist)}週分")
            time.sleep(0.5)  # API 負荷対策
        except Exception as e:
            print(f"  ❌ {name}({ticker}): {e}")

    if not dfs:
        raise RuntimeError("株価データを1件も取得できませんでした")

    df = pd.concat(dfs, axis=1)
    df.index = df.index.tz_localize(None)
    df = df.sort_index()
    return df


def save_stock_prices(df: pd.DataFrame):
    csv_path = DATA_DIR / "restaurant_stocks.csv"
    df.to_csv(csv_path, encoding="utf-8-sig")
    print(f"✅ 株価データ保存: {csv_path}")
    return csv_path


# ==============================================================
# BLOCK 4: データ統合（野菜価格 × 株価）
# ==============================================================

def merge_data(veg_df: pd.DataFrame, stock_df: pd.DataFrame) -> pd.DataFrame:
    """
    野菜価格（週次）と株価（週次）を結合する。
    野菜価格の日付（月曜日）に最も近い株価を紐付ける。
    """
    # 野菜価格の週を金曜日に揃える（株価は週足で金曜終値が多い）
    veg_reindexed = veg_df.copy()
    veg_reindexed.index = veg_reindexed.index + timedelta(days=4)  # 月曜→金曜

    merged = pd.merge_asof(
        stock_df.sort_index(),
        veg_reindexed.sort_index(),
        left_index=True,
        right_index=True,
        direction="backward",
        tolerance=timedelta(days=10),
    )
    print(f"✅ データ統合完了: {len(merged)}行")
    return merged


# ==============================================================
# BLOCK 5: 相関分析（野菜価格 → 株価の関係）
# ==============================================================

def analyze_correlation(merged_df: pd.DataFrame,
                        lag_weeks: int = 4) -> pd.DataFrame:
    """
    野菜価格の変化率 → n週後の株価リターンの相関を計算。

    lag_weeks: 何週後の株価と比較するか
      - 0: 同期（即日反応仮説）
      - 4: 1ヶ月後（業績反映の遅れを考慮）
      - 8: 2ヶ月後（四半期決算への先行を考慮）

    なぜ変化率にするか:
      価格水準（絶対値）より「上がった/下がった」の方向性が重要なため。
    """
    veg_cols   = [c for c in merged_df.columns if c in VEG_COLUMNS_JA]
    stock_cols = [c for c in merged_df.columns if c in RESTAURANT_STOCKS.values()]

    # 変化率に変換
    veg_returns   = merged_df[veg_cols].pct_change()
    stock_returns = merged_df[stock_cols].pct_change().shift(-lag_weeks)

    results = []
    for veg in veg_cols:
        for stock in stock_cols:
            x = veg_returns[veg].dropna()
            y = stock_returns[stock].dropna()
            common = x.index.intersection(y.index)
            if len(common) < 20:
                continue
            xc, yc = x[common], y[common]
            r, p = stats.pearsonr(xc, yc)
            results.append({
                "野菜品目":    veg,
                "銘柄":        stock,
                "相関係数r":   round(r, 4),
                "p値":         round(p, 4),
                "統計的有意":  "★有意" if p < 0.05 else "―",
                "関係の方向":  "逆相関（価格↑→株価↓）" if r < -0.1 else
                               "正相関（価格↑→株価↑）" if r > 0.1 else "ほぼ無関係",
                "ラグ(週)":    lag_weeks,
                "サンプル数":  len(common),
            })

    df = pd.DataFrame(results).sort_values("相関係数r")
    return df


def print_top_correlations(corr_df: pd.DataFrame, top_n: int = 10):
    """有意な相関のトップNを表示"""
    significant = corr_df[corr_df["統計的有意"] == "★有意"]
    print(f"\n{'='*60}")
    print(f"  統計的に有意な相関（p<0.05）: {len(significant)}件")
    print(f"{'='*60}")
    print("【逆相関が強い組み合わせ（野菜価格上昇→株価下落）】")
    top_neg = significant[significant["相関係数r"] < 0].head(top_n // 2)
    print(top_neg[["野菜品目","銘柄","相関係数r","p値","ラグ(週)"]].to_string(index=False))
    print("\n【正相関が強い組み合わせ（野菜価格上昇→株価上昇）】")
    top_pos = significant[significant["相関係数r"] > 0].tail(top_n // 2)
    print(top_pos[["野菜品目","銘柄","相関係数r","p値","ラグ(週)"]].to_string(index=False))


# ==============================================================
# BLOCK 6: 可視化
# ==============================================================

def plot_veg_vs_stock(merged_df: pd.DataFrame,
                      veg_col: str, stock_col: str,
                      lag_weeks: int = 4):
    """
    指定した野菜品目と銘柄の時系列チャートを生成して保存。
    """
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(f"{veg_col}価格 × {stock_col}株価\n（ラグ={lag_weeks}週）",
                 fontsize=14, fontweight="bold")

    # ① 野菜価格の時系列
    ax1.plot(merged_df.index, merged_df[veg_col], color="#E74C3C", linewidth=1.5,
             label=f"{veg_col}（円/kg）")
    ax1.set_ylabel("価格（円/kg）")
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.3)

    # ② 株価の時系列
    ax2.plot(merged_df.index, merged_df[stock_col], color="#2980B9", linewidth=1.5,
             label=f"{stock_col}株価（円）")
    ax2.set_ylabel("株価（円）")
    ax2.legend(loc="upper left")
    ax2.grid(alpha=0.3)

    # ③ 変化率の散布（相関の視覚化）
    vr = merged_df[veg_col].pct_change() * 100
    sr = merged_df[stock_col].pct_change().shift(-lag_weeks) * 100
    common = vr.dropna().index.intersection(sr.dropna().index)
    ax3.scatter(vr[common], sr[common], alpha=0.5, s=20, color="#8E44AD")
    ax3.axhline(0, color="gray", linewidth=0.5)
    ax3.axvline(0, color="gray", linewidth=0.5)
    ax3.set_xlabel(f"{veg_col}変化率（%）")
    ax3.set_ylabel(f"{stock_col} {lag_weeks}週後リターン（%）")
    ax3.grid(alpha=0.3)
    # 回帰直線
    if len(common) > 10:
        xv, yv = vr[common].values, sr[common].values
        mask = ~(pd.isna(xv) | pd.isna(yv))
        slope, intercept, r, p, _ = stats.linregress(xv[mask], yv[mask])
        xline = [xv[mask].min(), xv[mask].max()]
        ax3.plot(xline, [slope*x + intercept for x in xline], "r--", linewidth=1.5,
                 label=f"r={r:.3f}, p={p:.3f}")
        ax3.legend()

    plt.tight_layout()
    fname = RESULT_DIR / f"chart_{veg_col}_{stock_col}_lag{lag_weeks}.png"
    plt.savefig(fname, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  📊 チャート保存: {fname}")
    return fname


def plot_correlation_heatmap(corr_df: pd.DataFrame, lag_weeks: int):
    """相関係数のヒートマップを生成"""
    import matplotlib.ticker as ticker
    try:
        import seaborn as sns
        pivot = corr_df.pivot(index="野菜品目", columns="銘柄", values="相関係数r")
        fig, ax = plt.subplots(figsize=(10, 6))
        sns.heatmap(pivot, annot=True, fmt=".2f", cmap="RdBu_r",
                    vmin=-0.5, vmax=0.5, ax=ax)
        ax.set_title(f"野菜価格変化率 × 株価リターン 相関ヒートマップ\n（ラグ={lag_weeks}週）")
    except ImportError:
        # seaborn がない場合は matplotlib でシンプルなグリッド表示
        veg_cols   = corr_df["野菜品目"].unique()
        stock_cols = corr_df["銘柄"].unique()
        matrix = pd.DataFrame(index=veg_cols, columns=stock_cols, dtype=float)
        for _, row in corr_df.iterrows():
            matrix.loc[row["野菜品目"], row["銘柄"]] = row["相関係数r"]
        fig, ax = plt.subplots(figsize=(10, 6))
        im = ax.imshow(matrix.values.astype(float), cmap="RdBu_r", vmin=-0.5, vmax=0.5)
        ax.set_xticks(range(len(stock_cols)))
        ax.set_xticklabels(stock_cols, rotation=45, ha="right")
        ax.set_yticks(range(len(veg_cols)))
        ax.set_yticklabels(veg_cols)
        plt.colorbar(im, ax=ax)
        ax.set_title(f"野菜価格変化率 × 株価リターン 相関ヒートマップ（ラグ={lag_weeks}週）")

    fname = RESULT_DIR / f"heatmap_lag{lag_weeks}.png"
    plt.tight_layout()
    plt.savefig(fname, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  📊 ヒートマップ保存: {fname}")
    return fname


# ==============================================================
# BLOCK 7: 知見をMarkdownに記録（UKIシステムの「md蓄積」）
# ==============================================================

def save_insight_md(corr_df: pd.DataFrame, lag_weeks: int,
                    veg_df: pd.DataFrame, extra_notes: str = ""):
    """
    分析結果を Markdown ファイルとして Google Drive に保存。
    ファイルは日付でバージョン管理される。
    """
    today      = datetime.now().strftime("%Y-%m-%d")
    fname      = INSIGHT_DIR / f"insight_{today}_vegprice_lag{lag_weeks}.md"
    latest_veg = veg_df.iloc[-1].dropna().to_dict()
    latest_date = veg_df.index[-1].strftime("%Y/%m/%d")

    significant = corr_df[corr_df["統計的有意"] == "★有意"].copy()
    top_neg = significant[significant["相関係数r"] < -0.1].head(5)
    top_pos = significant[significant["相関係数r"] > 0.1].tail(5)

    content = f"""# INSIGHT: 野菜価格 × 外食株 相関分析

## 分析日時
{datetime.now().strftime("%Y-%m-%d %H:%M")}

## 分析条件
- ラグ（先行週数）: {lag_weeks}週
- 分析期間: {corr_df["サンプル数"].max()}週分のデータを使用
- 対象銘柄: {", ".join(RESTAURANT_STOCKS.values())}

## 最新の野菜価格（{latest_date}週）
| 品目 | 価格（円/kg） |
|------|-------------|
"""
    for veg, price in latest_veg.items():
        if veg != "date":
            content += f"| {veg} | {price:.0f} |\n"

    content += f"""
## 統計的に有意な逆相関（野菜価格↑ → 株価↓）
| 野菜品目 | 銘柄 | 相関係数r | p値 |
|---------|------|---------|-----|
"""
    for _, row in top_neg.iterrows():
        content += f"| {row['野菜品目']} | {row['銘柄']} | {row['相関係数r']} | {row['p値']} |\n"

    content += f"""
## 統計的に有意な正相関（野菜価格↑ → 株価↑）
| 野菜品目 | 銘柄 | 相関係数r | p値 |
|---------|------|---------|-----|
"""
    for _, row in top_pos.iterrows():
        content += f"| {row['野菜品目']} | {row['銘柄']} | {row['相関係数r']} | {row['p値']} |\n"

    content += f"""
## 投資アイディアとしての解釈
- 統計的有意な組み合わせが存在する場合、野菜価格は{lag_weeks}週後の株価に先行する可能性がある
- ただし相関≠因果。マクロ環境（円安・インフレ等）による擬似相関に注意
- 農水省データの次回更新で同方向の変化が続けばシグナルとして使える可能性

## 次のアクション
- [ ] ラグを変えて（0週/4週/8週）比較してみる
- [ ] 他のコスト要因（電力費・人件費）との多変量回帰を試す
- [ ] 有意な組み合わせのバックテストを実施する

## 備考・メモ
{extra_notes if extra_notes else "（なし）"}

---
*このファイルは gov_data_pipeline.py により自動生成*
"""

    with open(fname, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"✅ 知見ファイル保存: {fname}")
    return fname


# ==============================================================
# BLOCK 8: Google Sheets への書き込み（任意）
# ==============================================================

def write_to_gsheet(df_veg_latest: pd.Series,
                    corr_df: pd.DataFrame,
                    credentials_json_path: str,
                    spreadsheet_id: str):
    """
    野菜価格最新値と有意な相関結果をスプレッドシートに書き込む。
    credentials_json_path: service_account.json のパス
    spreadsheet_id: スプレッドシートの ID
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds  = Credentials.from_service_account_file(
            credentials_json_path,
            scopes=["https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive"]
        )
        gc     = gspread.authorize(creds)
        sh     = gc.open_by_key(spreadsheet_id)

        # シート1: 野菜価格最新値
        try:
            ws = sh.worksheet("野菜価格")
        except Exception:
            ws = sh.add_worksheet("野菜価格", rows=200, cols=20)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        header  = ["更新日時"] + list(df_veg_latest.index)
        values  = [now_str] + [str(v) for v in df_veg_latest.values]
        ws.insert_row(values, index=2)
        if ws.row_count == 2:
            ws.insert_row(header, index=1)

        # シート2: 有意な相関結果
        try:
            ws2 = sh.worksheet("相関分析")
        except Exception:
            ws2 = sh.add_worksheet("相関分析", rows=500, cols=15)

        sig = corr_df[corr_df["統計的有意"] == "★有意"].copy()
        sig["記録日"] = now_str
        if not sig.empty:
            ws2.clear()
            ws2.update([sig.columns.tolist()] + sig.values.tolist())

        print("✅ スプレッドシート書き込み完了")
    except Exception as e:
        print(f"⚠️  スプレッドシート書き込み失敗（スキップ）: {e}")


# ==============================================================
# BLOCK 9: メイン実行
# ==============================================================

def main(
    lag_weeks_list: list = [0, 4, 8],
    write_sheet: bool = False,
    credentials_path: str = "/content/drive/MyDrive/stock_viewer/service_account.json",
    spreadsheet_id: str = "1CePiavYj_6OR0cOz84V4RkA0c0S6IwZlcHCv8_bsOjc",
    extra_notes: str = "",
):
    """
    パイプライン全体を実行する。
    
    引数:
      lag_weeks_list   : 試すラグのリスト（0=即日, 4=1ヶ月後, 8=2ヶ月後）
      write_sheet      : True にするとスプレッドシートにも書き込む
      credentials_path : service_account.json のパス
      spreadsheet_id   : スプレッドシート ID
      extra_notes      : 知見ファイルに追記するメモ
    """
    print("=" * 60)
    print("  政府データ取得パイプライン 開始")
    print("=" * 60)

    # セットアップ
    setup_drive()

    # 野菜価格取得
    veg_df = fetch_vegprice_all()
    save_vegprice(veg_df)

    # 株価取得
    stock_df = fetch_stock_prices(RESTAURANT_STOCKS, period="3y")
    save_stock_prices(stock_df)

    # データ統合
    merged = merge_data(veg_df, stock_df)

    # ラグ別に相関分析
    all_corr = []
    for lag in lag_weeks_list:
        print(f"\n▶ ラグ {lag}週 の相関分析中...")
        corr_df = analyze_correlation(merged, lag_weeks=lag)
        print_top_correlations(corr_df)
        all_corr.append(corr_df)

        # ヒートマップ
        plot_correlation_heatmap(corr_df, lag_weeks=lag)

        # 有意な組み合わせのチャートを生成
        sig = corr_df[corr_df["統計的有意"] == "★有意"]
        for _, row in sig.head(3).iterrows():  # 上位3つのみ
            try:
                plot_veg_vs_stock(merged, row["野菜品目"], row["銘柄"], lag)
            except Exception as e:
                print(f"  ⚠️  チャート生成スキップ: {e}")

    # 代表ラグ（4週）で知見記録
    best_corr = all_corr[lag_weeks_list.index(4)] if 4 in lag_weeks_list else all_corr[0]
    save_insight_md(best_corr, 4, veg_df, extra_notes)

    # Sheets 書き込み（オプション）
    if write_sheet:
        write_to_gsheet(
            veg_df.iloc[-1].dropna(),
            best_corr,
            credentials_path,
            spreadsheet_id,
        )

    # 結果を CSV で保存
    all_corr_combined = pd.concat(all_corr)
    csv_path = RESULT_DIR / f"correlation_{datetime.now().strftime('%Y%m%d')}.csv"
    all_corr_combined.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n✅ 相関分析結果 CSV: {csv_path}")

    print("\n" + "=" * 60)
    print("  ✅ パイプライン完了")
    print(f"  📁 結果: {RESULT_DIR}")
    print(f"  📝 知見: {INSIGHT_DIR}")
    print("=" * 60)


# ==============================================================
# 実行（Colab でこのセルを実行するだけ）
# ==============================================================

if __name__ == "__main__":
    main(
        lag_weeks_list=[0, 4, 8],   # 比較するラグ週数
        write_sheet=False,          # True: スプレッドシートにも書き込む
        extra_notes="初回実行。農水省データと外食株3年分の相関を確認。",
    )
