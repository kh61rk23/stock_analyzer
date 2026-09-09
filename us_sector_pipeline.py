# ==============================================================
# us_sector_pipeline.py  v1.0
# ==============================================================
# 【概要】
#   S&P500の11セクターをETFデータで自動スコアリングする。
#   既存の日本株33業種分析と同じロジックで米国セクターを評価し、
#   stock_analyzer_main.py の results["us_sectors"] に格納する。
#
# 【11セクターと対応ETF】
#   XLK  = Information Technology（情報技術）
#   XLF  = Financials（金融）
#   XLE  = Energy（エネルギー）
#   XLV  = Health Care（ヘルスケア）
#   XLY  = Consumer Discretionary（一般消費財）
#   XLP  = Consumer Staples（生活必需品）
#   XLI  = Industrials（資本財）
#   XLB  = Materials（素材）
#   XLRE = Real Estate（不動産）
#   XLU  = Utilities（公益事業）
#   XLC  = Communication Services（通信サービス）
#
# 【スコアリングロジック】
#   ① ETFの1M/3M/6M/12Mパフォーマンス（モメンタム）
#   ② 52週高値からの乖離（現在ポジション）
#   ③ 出来高トレンド（増加→注目度上昇）
#   ④ マクロ環境との整合性（金利・ドル・原油との相関）
#
# 【出力】
#   insights/us_sectors/us_sector_report_YYYY-MM-DD.md
#   insights/us_sectors/us_sector_scores_YYYY-MM-DD.json
# ==============================================================

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: セクター定義
# ==============================================================

US_SECTORS = [
    {
        "code":        "XLK",
        "name_en":     "Information Technology",
        "name_ja":     "情報技術",
        "etf":         "XLK",
        "top_holdings": ["AAPL", "MSFT", "NVDA"],
        "macro_sensitivity": {
            "interest_up":   -1,  # 金利上昇はグロース株に逆風
            "interest_down":  1,
            "dollar_up":      0,
            "dollar_down":    0,
            "oil_up":         0,
            "vix_up":        -1,
        },
        "description": "AI・クラウド・半導体が主要ドライバー。金利感応度が高い。",
    },
    {
        "code":        "XLF",
        "name_en":     "Financials",
        "name_ja":     "金融",
        "etf":         "XLF",
        "top_holdings": ["BRK-B", "JPM", "V"],
        "macro_sensitivity": {
            "interest_up":    1,  # 金利上昇→利ザヤ拡大
            "interest_down": -1,
            "dollar_up":      0,
            "dollar_down":    0,
            "oil_up":         0,
            "vix_up":        -1,
        },
        "description": "長短金利差が収益の主要ドライバー。規制環境も影響大。",
    },
    {
        "code":        "XLE",
        "name_en":     "Energy",
        "name_ja":     "エネルギー",
        "etf":         "XLE",
        "top_holdings": ["XOM", "CVX", "COP"],
        "macro_sensitivity": {
            "interest_up":    0,
            "interest_down":  0,
            "dollar_up":     -1,  # ドル高→原油安→逆風
            "dollar_down":    1,
            "oil_up":         1,  # 原油高→追い風
            "vix_up":        -1,
        },
        "description": "原油・天然ガス価格に業績が直結。地政学リスクに敏感。",
    },
    {
        "code":        "XLV",
        "name_en":     "Health Care",
        "name_ja":     "ヘルスケア",
        "etf":         "XLV",
        "top_holdings": ["LLY", "UNH", "JNJ"],
        "macro_sensitivity": {
            "interest_up":    0,
            "interest_down":  0,
            "dollar_up":      0,
            "dollar_down":    0,
            "oil_up":         0,
            "vix_up":         1,  # 景気後退局面でディフェンシブ
        },
        "description": "景気非循環型ディフェンシブ。AI創薬・GLP-1が直近のテーマ。",
    },
    {
        "code":        "XLY",
        "name_en":     "Consumer Discretionary",
        "name_ja":     "一般消費財",
        "etf":         "XLY",
        "top_holdings": ["AMZN", "TSLA", "HD"],
        "macro_sensitivity": {
            "interest_up":   -1,
            "interest_down":  1,
            "dollar_up":      0,
            "dollar_down":    0,
            "oil_up":        -1,  # エネルギーコスト増→可処分所得減
            "vix_up":        -1,
        },
        "description": "個人消費と連動。金利上昇・景気後退局面で最も圧迫される。",
    },
    {
        "code":        "XLP",
        "name_en":     "Consumer Staples",
        "name_ja":     "生活必需品",
        "etf":         "XLP",
        "top_holdings": ["PG", "KO", "PEP"],
        "macro_sensitivity": {
            "interest_up":   -1,
            "interest_down":  0,
            "dollar_up":      0,
            "dollar_down":    0,
            "oil_up":         0,
            "vix_up":         1,
        },
        "description": "景気非循環型ディフェンシブ。インフレ局面での価格転嫁力が鍵。",
    },
    {
        "code":        "XLI",
        "name_en":     "Industrials",
        "name_ja":     "資本財",
        "etf":         "XLI",
        "top_holdings": ["GE", "RTX", "CAT"],
        "macro_sensitivity": {
            "interest_up":    0,
            "interest_down":  0,
            "dollar_up":     -1,
            "dollar_down":    1,
            "oil_up":        -1,
            "vix_up":        -1,
        },
        "description": "製造業設備投資・インフラ支出に連動。防衛関連も含む。",
    },
    {
        "code":        "XLB",
        "name_en":     "Materials",
        "name_ja":     "素材",
        "etf":         "XLB",
        "top_holdings": ["LIN", "APD", "SHW"],
        "macro_sensitivity": {
            "interest_up":    0,
            "interest_down":  0,
            "dollar_up":     -1,
            "dollar_down":    1,
            "oil_up":         0,
            "vix_up":        -1,
        },
        "description": "金属・化学・鉱山。ドル安・景気拡大局面で追い風。",
    },
    {
        "code":        "XLRE",
        "name_en":     "Real Estate",
        "name_ja":     "不動産",
        "etf":         "XLRE",
        "top_holdings": ["PLD", "AMT", "EQIX"],
        "macro_sensitivity": {
            "interest_up":   -1,  # 金利上昇→REIT逆風
            "interest_down":  1,
            "dollar_up":      0,
            "dollar_down":    0,
            "oil_up":         0,
            "vix_up":        -1,
        },
        "description": "金利感応度が最も高いセクター。データセンターREITは例外的に好調。",
    },
    {
        "code":        "XLU",
        "name_en":     "Utilities",
        "name_ja":     "公益事業",
        "etf":         "XLU",
        "top_holdings": ["NEE", "SO", "DUK"],
        "macro_sensitivity": {
            "interest_up":   -1,
            "interest_down":  1,
            "dollar_up":      0,
            "dollar_down":    0,
            "oil_up":        -1,
            "vix_up":         1,
        },
        "description": "債券代替ディフェンシブ。AI電力需要でデータセンター向け電力が急増。",
    },
    {
        "code":        "XLC",
        "name_en":     "Communication Services",
        "name_ja":     "通信サービス",
        "etf":         "XLC",
        "top_holdings": ["META", "GOOGL", "NFLX"],
        "macro_sensitivity": {
            "interest_up":   -1,
            "interest_down":  1,
            "dollar_up":      0,
            "dollar_down":    0,
            "oil_up":         0,
            "vix_up":        -1,
        },
        "description": "広告・ストリーミング・SNS。AI活用による収益拡大が進行中。",
    },
]

# マクロスナップショットとの対応（fetch_macro_snapshotのキーに合わせる）
MACRO_KEY_MAP = {
    "interest_up":   "us10y",    # 4.0%超で interest_up=True
    "interest_down": "us10y",
    "dollar_up":     "usdjpy",   # 150円超で dollar_up=True（近似）
    "dollar_down":   "usdjpy",
    "oil_up":        "oil_wti",  # 80ドル超で oil_up=True
    "vix_up":        "vix",      # 20超で vix_up=True
}


# ==============================================================
# BLOCK 1: ETFデータ取得
# ==============================================================

def fetch_etf_data(etf_symbols: list, days: int = 365) -> dict:
    """
    yfinance でETFの価格データを取得する。

    Returns:
        {
          "XLK": {
            "current":    float,  # 現在値
            "high_52w":   float,  # 52週高値
            "low_52w":    float,  # 52週安値
            "ret_1m":     float,  # 1ヶ月リターン(%)
            "ret_3m":     float,  # 3ヶ月リターン(%)
            "ret_6m":     float,  # 6ヶ月リターン(%)
            "ret_12m":    float,  # 12ヶ月リターン(%)
            "vol_trend":  float,  # 出来高トレンド（最近20日/前20日）
            "fetch_ok":   bool,
          }, ...
        }
    """
    try:
        import yf_cache as yf
        import pandas as pd
    except ImportError:
        print("  ⚠️  yfinance/pandas 未インストール")
        return {}

    print(f"  📡 ETFデータ取得中: {' '.join(etf_symbols)}")
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days + 30)

    try:
        raw = yf.download(
            etf_symbols,
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=True,
            progress=False,
            timeout=30,
        )
    except Exception as e:
        print(f"  ⚠️  ETFデータ一括取得失敗: {str(e)[:60]}")
        return {}

    if raw is None or raw.empty:
        return {}

    # Close 価格を抽出
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]].rename(columns={"Close": etf_symbols[0]})

    vol_df = None
    if isinstance(raw.columns, pd.MultiIndex) and "Volume" in raw.columns.get_level_values(0):
        vol_df = raw["Volume"]

    result = {}
    for sym in etf_symbols:
        if sym not in close.columns:
            result[sym] = {"fetch_ok": False}
            continue
        prices = close[sym].dropna()
        if len(prices) < 20:
            result[sym] = {"fetch_ok": False}
            continue

        current  = float(prices.iloc[-1])
        high_52w = float(prices.tail(252).max())
        low_52w  = float(prices.tail(252).min())

        def _ret(n_days):
            if len(prices) > n_days:
                p_old = float(prices.iloc[-(n_days+1)])
                if p_old != 0:
                    return round((current - p_old) / p_old * 100, 2)
            return None

        vol_trend = None
        if vol_df is not None and sym in vol_df.columns:
            vols = vol_df[sym].dropna()
            if len(vols) >= 40:
                recent = float(vols.tail(20).mean())
                prev   = float(vols.iloc[-40:-20].mean())
                if prev != 0:
                    vol_trend = round(recent / prev, 2)

        result[sym] = {
            "current":   current,
            "high_52w":  high_52w,
            "low_52w":   low_52w,
            "ret_1m":    _ret(21),
            "ret_3m":    _ret(63),
            "ret_6m":    _ret(126),
            "ret_12m":   _ret(252),
            "vol_trend": vol_trend,
            "fetch_ok":  True,
        }

    return result


# ==============================================================
# BLOCK 2: スコアリング
# ==============================================================

def score_sector(sector_def: dict, etf_data: dict,
                 macro_snapshot: dict) -> dict:
    """
    1セクターのスコアを計算する。

    スコア構成:
      モメンタム（最大±40点）
        ret_1m:  ±10点
        ret_3m:  ±15点
        ret_12m: ±15点
      52週ポジション（最大+10点）
        現在値/52週高値 > 0.95 → +10点
        現在値/52週高値 < 0.80 → -10点
      出来高トレンド（最大±5点）
        vol_trend > 1.2 → +5点
        vol_trend < 0.8 → -5点
      マクロ整合性（最大±20点）
        各マクロ要因との整合: ±5点×最大4要因
    """
    etf    = etf_data.get(sector_def["etf"], {})
    if not etf.get("fetch_ok"):
        return {
            "score":        0,
            "components":   {},
            "signals":      [],
            "risk_level":   "unknown",
            "fetch_ok":     False,
        }

    score      = 0
    components = {}
    signals    = []

    # ── ① モメンタム ─────────────────────────────────────────
    for key, weight, threshold in [
        ("ret_1m",  10, 3.0),
        ("ret_3m",  15, 5.0),
        ("ret_12m", 15, 10.0),
    ]:
        val = etf.get(key)
        if val is not None:
            pts = round(min(abs(val) / threshold, 1.0) * weight
                        * (1 if val >= 0 else -1), 1)
            score += pts
            components[key] = pts

    # ── ② 52週ポジション ─────────────────────────────────────
    current  = etf["current"]
    high_52w = etf["high_52w"]
    if high_52w > 0:
        ratio = current / high_52w
        if ratio >= 0.95:
            score += 10
            components["position_52w"] = 10
            signals.append("52週高値圏（強いトレンド）")
        elif ratio >= 0.85:
            components["position_52w"] = 0
        else:
            score -= 10
            components["position_52w"] = -10
            signals.append(f"52週高値から{(1-ratio)*100:.0f}%下落")

    # ── ③ 出来高トレンド ─────────────────────────────────────
    vt = etf.get("vol_trend")
    if vt is not None:
        if vt > 1.2:
            score += 5
            components["vol_trend"] = 5
            signals.append(f"出来高増加（{vt:.1f}x）")
        elif vt < 0.8:
            score -= 5
            components["vol_trend"] = -5

    # ── ④ マクロ整合性 ────────────────────────────────────────
    macro_score = 0
    macro_sens  = sector_def.get("macro_sensitivity", {})

    us10y  = macro_snapshot.get("us10y",  0) or 0
    usdjpy = macro_snapshot.get("usdjpy", 0) or 0
    oil    = macro_snapshot.get("oil_wti", 0) or 0
    vix    = macro_snapshot.get("vix",    0) or 0

    # 金利
    if us10y > 4.0 and macro_sens.get("interest_up", 0) != 0:
        pts = macro_sens["interest_up"] * 5
        macro_score += pts
        if pts > 0:
            signals.append(f"高金利（{us10y:.2f}%）が追い風")
        elif pts < 0:
            signals.append(f"高金利（{us10y:.2f}%）が逆風")
    elif us10y < 3.5 and macro_sens.get("interest_down", 0) != 0:
        pts = macro_sens["interest_down"] * 5
        macro_score += pts

    # 原油
    if oil > 80 and macro_sens.get("oil_up", 0) != 0:
        pts = macro_sens["oil_up"] * 5
        macro_score += pts
        if pts > 0:
            signals.append(f"高原油（${oil:.0f}）が追い風")
        elif pts < 0:
            signals.append(f"高原油（${oil:.0f}）が逆風")

    # VIX
    if vix > 20 and macro_sens.get("vix_up", 0) != 0:
        pts = macro_sens["vix_up"] * 5
        macro_score += pts
        if pts > 0:
            signals.append(f"高VIX（{vix:.1f}）がディフェンシブ需要に追い風")
        elif pts < 0:
            signals.append(f"高VIX（{vix:.1f}）がリスクオフ逆風")

    # ドル円
    if usdjpy > 150 and macro_sens.get("dollar_up", 0) != 0:
        pts = macro_sens["dollar_up"] * 5
        macro_score += pts
        if pts > 0:
            signals.append(f"円安（{usdjpy:.1f}円）が追い風")
        elif pts < 0:
            signals.append(f"円安（{usdjpy:.1f}円）が逆風")
    elif usdjpy < 130 and macro_sens.get("dollar_down", 0) != 0:
        pts = macro_sens["dollar_down"] * 5
        macro_score += pts
        if pts > 0:
            signals.append(f"円高（{usdjpy:.1f}円）が追い風")
        elif pts < 0:
            signals.append(f"円高（{usdjpy:.1f}円）が逆風")

    score += macro_score
    components["macro"] = macro_score

    # リスクレベル
    if score >= 30:
        risk = "low"
    elif score >= 10:
        risk = "medium"
    elif score >= -10:
        risk = "medium"
    else:
        risk = "high"

    return {
        "score":      round(score, 1),
        "components": components,
        "signals":    signals,
        "risk_level": risk,
        "fetch_ok":   True,
        "etf_data":   {
            "current":  current,
            "ret_1m":   etf.get("ret_1m"),
            "ret_3m":   etf.get("ret_3m"),
            "ret_12m":  etf.get("ret_12m"),
            "high_52w": high_52w,
        },
    }


# ==============================================================
# BLOCK 3: レポート生成
# ==============================================================

def build_us_report(sector_results: list) -> str:
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 米国株 S&P500セクター分析レポート",
        f"**生成日時**: {now}", "",
        "---", "", "## 📊 11セクター スコアランキング", "",
        "| # | セクター | ETF | スコア | 1M | 3M | 12M | リスク |",
        "|---|---------|-----|--------|-----|-----|------|--------|",
    ]

    sorted_results = sorted(sector_results,
                             key=lambda x: x["score_result"]["score"],
                             reverse=True)

    for rank, r in enumerate(sorted_results, 1):
        s   = r["sector"]
        sr  = r["score_result"]
        etf = sr.get("etf_data", {})
        if not sr.get("fetch_ok"):
            lines.append(f"| {rank} | {s['name_ja']} | {s['etf']} | N/A | ― | ― | ― | unknown |")
            continue
        score  = sr["score"]
        r1m    = etf.get("ret_1m")
        r3m    = etf.get("ret_3m")
        r12m   = etf.get("ret_12m")
        risk   = sr["risk_level"]
        score_icon = "📈" if score > 20 else ("📉" if score < 0 else "➡️")
        r1m_s  = f"{r1m:+.1f}%" if r1m  is not None else "―"
        r3m_s  = f"{r3m:+.1f}%" if r3m  is not None else "―"
        r12m_s = f"{r12m:+.1f}%" if r12m is not None else "―"
        lines.append(
            f"| {rank} | {score_icon} **{s['name_ja']}** | `{s['etf']}` "
            f"| **{score:.0f}点** | {r1m_s} | {r3m_s} | {r12m_s} | {risk} |")

    lines += ["", "---", "", "## 🔍 セクター別 詳細", ""]
    for r in sorted_results:
        s   = r["sector"]
        sr  = r["score_result"]
        if not sr.get("fetch_ok"):
            continue
        score  = sr["score"]
        etf    = sr.get("etf_data", {})
        icon   = "📈" if score > 20 else ("📉" if score < -10 else "➡️")
        lines += [
            f"### {icon} {s['name_ja']}（{s['name_en']}）`{s['etf']}`",
            f"**スコア: {score:.0f}点** / リスク: {sr['risk_level']}",
            f"**現在値**: ${etf.get('current', 0):.2f} "
            f"/ 52週高値: ${etf.get('high_52w', 0):.2f}",
            "",
            f"**特徴**: {s['description']}",
        ]
        if s.get("top_holdings"):
            lines.append(f"**主要構成銘柄**: {' / '.join(s['top_holdings'])}")
        if sr.get("signals"):
            lines.append("**シグナル:**")
            for sig in sr["signals"]:
                lines.append(f"- {sig}")
        lines.append("")

    lines += ["---",
              f"*出典: Yahoo Finance (yfinance) / 分析日: {now}*", ""]
    return "\n".join(lines)


# ==============================================================
# BLOCK 4: CSV出力（Power BI / Excel連携用）
# ==============================================================

def export_csv(sector_results: list, output_path: Path):
    """
    Power BI / Excel での利用を想定したCSVを出力する。

    列構成:
      date, etf, sector_en, sector_ja, score, risk_level,
      ret_1m, ret_3m, ret_12m, current, high_52w, signals
    """
    today = datetime.now().strftime("%Y-%m-%d")
    rows  = [
        "date,etf,sector_en,sector_ja,score,risk_level,"
        "ret_1m,ret_3m,ret_12m,current,high_52w,signals"
    ]
    for r in sector_results:
        s   = r["sector"]
        sr  = r["score_result"]
        etf = sr.get("etf_data", {})
        sigs = "|".join(sr.get("signals", []))

        def _f(v):
            return f"{v:.2f}" if isinstance(v, float) else ""

        rows.append(
            f"{today},{s['etf']},{s['name_en']},{s['name_ja']},"
            f"{sr['score']:.1f},{sr['risk_level']},"
            f"{_f(etf.get('ret_1m'))},{_f(etf.get('ret_3m'))},"
            f"{_f(etf.get('ret_12m'))},{_f(etf.get('current'))},"
            f"{_f(etf.get('high_52w'))},{sigs}"
        )
    try:
        output_path.write_text("\n".join(rows), encoding="utf-8-sig")
        # utf-8-sig = BOM付き → Excelで文字化けしない
        print(f"  📊 CSV出力: {output_path}")
    except Exception as e:
        print(f"  ⚠️  CSV出力失敗: {e}")


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_us_sector_pipeline(cfg, macro_snapshot: Optional[dict] = None) -> dict:
    """
    S&P500 11セクター分析を実行する。

    Args:
        cfg:            config モジュール
        macro_snapshot: fetch_macro_snapshot() の戻り値（省略可）

    Returns:
        {
          "sectors":      { "XLK": {...}, ... },
          "sector_list":  [ {sector, score_result}, ... ] # ソート済み
          "report_path":  str,
          "csv_path":     str,
          "top3":         ["XLK", "XLF", ...],
          "bottom3":      ["XLRE", "XLU", ...],
          "skipped":      bool,
        }
    """
    print(f"\n{'='*60}")
    print("  🇺🇸 米国セクター分析 v1.0 実行開始")
    print(f"{'='*60}")

    # ディレクトリ準備
    us_dir = Path(cfg.INSIGHT_DIR) / "us_sectors"
    try:
        us_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # macro_snapshot が渡されない場合は空辞書
    if macro_snapshot is None:
        macro_snapshot = {}

    # ETFデータ取得（11銘柄を一括）
    etf_symbols = [s["etf"] for s in US_SECTORS]
    etf_data    = fetch_etf_data(etf_symbols)

    if not etf_data:
        print("  ❌ ETFデータ取得失敗")
        return {"sectors": {}, "sector_list": [], "skipped": True,
                "report_path": "", "csv_path": "", "top3": [], "bottom3": []}

    # スコアリング
    sector_results = []
    for s in US_SECTORS:
        sr = score_sector(s, etf_data, macro_snapshot)
        sector_results.append({"sector": s, "score_result": sr})
        status = f"スコア:{sr['score']:.0f} / リスク:{sr['risk_level']}"
        ok_str = "✅" if sr.get("fetch_ok") else "⚠️ データなし"
        print(f"    {ok_str} [{s['etf']}] {s['name_ja']}: {status}")

    # ソート（スコア降順）
    sorted_results = sorted(sector_results,
                             key=lambda x: x["score_result"]["score"],
                             reverse=True)

    # レポート保存
    today_str   = datetime.now().strftime("%Y-%m-%d")
    report_path = us_dir / f"us_sector_report_{today_str}.md"
    csv_path    = us_dir / f"us_sector_scores_{today_str}.csv"

    report_md = build_us_report(sorted_results)
    try:
        report_path.write_text(report_md, encoding="utf-8")
        print(f"  ✅ レポート保存: {report_path}")
    except Exception as e:
        print(f"  ⚠️  レポート保存失敗: {e}")
        report_path = None

    export_csv(sorted_results, csv_path)

    # セクター別辞書に変換
    sectors_dict = {r["sector"]["etf"]: {
        "name_ja":    r["sector"]["name_ja"],
        "name_en":    r["sector"]["name_en"],
        "score":      r["score_result"]["score"],
        "risk_level": r["score_result"]["risk_level"],
        "signals":    r["score_result"]["signals"],
        "etf_data":   r["score_result"].get("etf_data", {}),
    } for r in sorted_results}

    top3    = [r["sector"]["etf"] for r in sorted_results[:3]
               if r["score_result"].get("fetch_ok")]
    bottom3 = [r["sector"]["etf"] for r in sorted_results[-3:]
               if r["score_result"].get("fetch_ok")]

    print(f"\n  🏆 トップ3:   {' > '.join(top3)}")
    print(f"  ⚠️  ボトム3:  {' < '.join(bottom3[::-1])}")

    # US セクタースコアを日次ファイルに保存（jp_us_bridge が参照）
    # ★ 2026-06-11 バグ修正: 本ブロックは return 文の後に置かれた到達不能
    #   コードで一度も実行されず、jp_us_bridge の US スコア履歴
    #   （us_sector_scores_*.json）が永続的に空だった。さらに未定義変数
    #   result と存在しないキー composite_score を参照していたため、
    #   sectors_dict（キーは "score"）から正しく出力するよう修正して
    #   return の前に移設。保存先は report/csv と同じ us_dir
    #   （= INSIGHT_DIR/us_sectors = ブリッジの探索先）。
    try:
        out_path = us_dir / f"us_sector_scores_{today_str}.json"
        out_path.write_text(
            json.dumps({
                "date": today_str,
                "sectors": {
                    etf: {"composite_score": data.get("score", 0)}
                    for etf, data in sectors_dict.items()
                }
            }, ensure_ascii=False), encoding="utf-8")
        print(f"  💾 us_sector_scores_{today_str}.json 出力")
    except Exception as e:
        print(f"  ⚠️  USセクタースコア保存失敗: {e}")

    return {
        "sectors":     sectors_dict,
        "sector_list": sorted_results,
        "report_path": str(report_path) if report_path else "",
        "csv_path":    str(csv_path),
        "top3":        top3,
        "bottom3":     bottom3,
        "skipped":     False,
    }


def main(cfg=None, macro_snapshot=None):
    """stock_analyzer_main.py からの呼び出し用エントリ"""
    if cfg is None:
        import importlib.util
        for p in ["/content/config.py",
                  str(Path(__file__).parent / "config.py")]:
            if Path(p).exists():
                spec = importlib.util.spec_from_file_location("config", p)
                cfg  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break
    return run_us_sector_pipeline(cfg, macro_snapshot)


if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    result = run_us_sector_pipeline(cfg)
    print(f"\n分析完了: {len(result['sectors'])} セクター")
