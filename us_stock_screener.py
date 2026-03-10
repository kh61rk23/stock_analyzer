# ==============================================================
# us_stock_screener.py  v1.0
# ==============================================================
# 【概要】
#   S&P500（500社）＋独自ユニバース（最大5,000社）を対象に
#   米国株のスクリーニングを行い、買い候補を抽出する。
#   GitHub Actions 6時間制約内に収まるよう5並列で取得する。
#
# 【設計方針】
#   - ビジネスモデル別にフィルター閾値を変える
#     （SaaSに黒字必須を課すと正常な高成長株が全滅するため）
#   - S&P500との重複はフラグ管理し除外しない
#     （S&P500所属銘柄は信頼性が高いため優先表示）
#   - yfinance 429エラー時は自動リトライ（最大3回・指数バックオフ）
#   - 取得結果はdaily_cacheに保存し、翌日は再取得しない
#
# 【config.py で必要な設定】
#   ENABLE_US_SCREENER       : True で有効化
#   US_SCREENER_MAX_WORKERS  : 並列数（推奨5）
#   US_SCREENER_MAX_TICKERS  : 総取得上限（推奨5500）
#   US_CUSTOM_UNIVERSE_DIR   : custom_universe/ のパス
#   US_SCREENER_CATEGORIES   : 有効カテゴリリスト
#   US_SCREENER_TOP_N        : アラートメール掲載件数（推奨10）
#
# 【出力】
#   results["us_screener"]:
#     {
#       "candidates":      [Candidate, ...],  # 全取得銘柄
#       "passed":          [Candidate, ...],  # フィルター通過銘柄
#       "pass_count":      int,
#       "screened_count":  int,
#       "top_picks":       [Candidate, ...],  # スコア上位N社
#       "by_category":     {category: [...]}, # カテゴリ別通過銘柄
#       "timestamp":       str,
#       "skipped":         bool,
#     }
#
#   insights/us_screener/us_screener_YYYY-MM-DD.md も生成
# ==============================================================

import json
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: ビジネスモデル別フィルター定義
# ==============================================================

# ビジネスモデルタグ → スクリーニング条件
# None は「チェックしない（制限なし）」を意味する
BUSINESS_MODEL_FILTERS = {
    "subscription": {
        # SaaS・ストリーミング・保険テック
        # 高成長期は赤字・高PERが当たり前のため緩くする
        "per_max":          80,
        "pbr_max":          None,
        "roe_min":          None,
        "op_margin_min":    None,
        "de_max":           None,
        "fcf_positive":     False,
        "revenue_growth_min": 0.10,   # 売上成長10%以上は必須
        "rec_mean_max":     2.8,
    },
    "marketplace": {
        # Airbnb・Uber・プラットフォーム型
        "per_max":          100,
        "pbr_max":          None,
        "roe_min":          None,
        "op_margin_min":    None,
        "de_max":           None,
        "fcf_positive":     False,
        "revenue_growth_min": 0.15,
        "rec_mean_max":     2.8,
    },
    "hardware": {
        # 半導体・医療機器・産業機械
        "per_max":          35,
        "pbr_max":          10.0,
        "roe_min":          0.08,
        "op_margin_min":    0.10,
        "de_max":           200,
        "fcf_positive":     True,
        "revenue_growth_min": None,
        "rec_mean_max":     2.5,
    },
    "asset_heavy": {
        # REIT・公益・インフラ（高D/Eは構造的・FCFはFFOで代替）
        "per_max":          30,
        "pbr_max":          3.0,
        "roe_min":          0.05,
        "op_margin_min":    0.10,
        "de_max":           500,      # REIT等は高D/Eが通常
        "fcf_positive":     False,    # FFOベースのため除外
        "revenue_growth_min": None,
        "rec_mean_max":     2.8,
    },
    "commodity": {
        # エネルギー・素材・農業
        "per_max":          20,
        "pbr_max":          3.0,
        "roe_min":          0.06,
        "op_margin_min":    0.08,
        "de_max":           150,
        "fcf_positive":     True,
        "revenue_growth_min": None,
        "rec_mean_max":     2.5,
    },
    "biotech": {
        # 臨床段階のバイオ・創薬（赤字が前提）
        "per_max":          None,
        "pbr_max":          None,
        "roe_min":          None,
        "op_margin_min":    None,
        "de_max":           100,      # バイオは低債務が望ましい
        "fcf_positive":     False,
        "revenue_growth_min": None,
        "rec_mean_max":     2.5,
    },
    "financial": {
        # 銀行・保険・資産運用
        "per_max":          18,
        "pbr_max":          2.0,
        "roe_min":          0.10,
        "op_margin_min":    None,
        "de_max":           None,     # 金融のD/Eは業種構造のため除外
        "fcf_positive":     False,
        "revenue_growth_min": None,
        "rec_mean_max":     2.5,
    },
    "mixed": {
        # S&P500等の混合カテゴリ：標準フィルター
        "per_max":          35,
        "pbr_max":          10.0,
        "roe_min":          0.08,
        "op_margin_min":    0.05,
        "de_max":           200,
        "fcf_positive":     True,
        "revenue_growth_min": None,
        "rec_mean_max":     2.5,
    },
}

# デフォルトフィルター（business_modelが未定義の場合）
DEFAULT_FILTER = BUSINESS_MODEL_FILTERS["mixed"]


# ==============================================================
# BLOCK 2: ユニバース読み込み
# ==============================================================

def load_universe(cfg) -> list:
    """
    config.US_SCREENER_CATEGORIES に従って
    custom_universe/ の JSON ファイルを読み込み、
    全銘柄リストを返す。

    Returns:
        list of dict:
          {"ticker": str, "category": str, "business_model": str}
    """
    universe_dir = Path(getattr(cfg, "DRIVE_BASE", "")) / \
                   getattr(cfg, "US_CUSTOM_UNIVERSE_DIR", "data/custom_universe")

    # フォールバック候補
    candidates = [
        universe_dir,
        Path(__file__).parent / getattr(cfg, "US_CUSTOM_UNIVERSE_DIR",
                                        "data/custom_universe"),
        Path("/content") / getattr(cfg, "US_CUSTOM_UNIVERSE_DIR",
                                   "data/custom_universe"),
    ]

    found_dir = None
    for d in candidates:
        if d.exists():
            found_dir = d
            break

    if not found_dir:
        print(f"  ⚠️  custom_universe ディレクトリが見つかりません")
        print(f"     検索パス: {[str(c) for c in candidates]}")
        return []

    categories = getattr(cfg, "US_SCREENER_CATEGORIES", [])
    max_total  = getattr(cfg, "US_SCREENER_MAX_TICKERS", 5500)

    all_entries = []
    seen_tickers = set()

    for cat in categories:
        path = found_dir / f"{cat}.json"
        if not path.exists():
            print(f"    ⚠️  {cat}.json が見つかりません（スキップ）")
            continue

        try:
            data    = json.loads(path.read_text(encoding="utf-8"))
            tickers = data.get("tickers", [])
            model   = data.get("business_model", "mixed")
            desc    = data.get("description", cat)

            added = 0
            for ticker in tickers:
                t = str(ticker).strip().upper()
                if not t or t in seen_tickers:
                    continue
                seen_tickers.add(t)
                all_entries.append({
                    "ticker":         t,
                    "category":       cat,
                    "business_model": model,
                    "description":    desc,
                    "in_sp500":       cat == "sp500",
                })
                added += 1

            print(f"    📂 {cat}: {added}社 読込（{model}）")

        except Exception as e:
            print(f"    ⚠️  {cat}.json 読込エラー: {str(e)[:60]}")

    # 上限チェック
    if len(all_entries) > max_total:
        print(f"    ⚠️  上限 {max_total:,}社 に切り詰めます "
              f"（読込: {len(all_entries):,}社）")
        all_entries = all_entries[:max_total]

    print(f"    ✅ ユニバース合計: {len(all_entries):,}社（重複除去済み）")
    return all_entries


# ==============================================================
# BLOCK 3: 1銘柄のyfinanceデータ取得
# ==============================================================

def _fetch_one(entry: dict, max_retries: int = 3) -> Optional[dict]:
    """
    1銘柄のyfinanceデータを取得する。
    429エラー時は指数バックオフでリトライ。

    Returns:
        dict（取得成功） or None（失敗）
    """
    ticker = entry["ticker"]

    for attempt in range(max_retries):
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info or {}

            # 有効な銘柄かチェック（価格データがなければスキップ）
            price = (info.get("regularMarketPrice")
                     or info.get("currentPrice")
                     or info.get("previousClose"))
            if not price:
                return None

            def _get(key: str) -> Optional[float]:
                v = info.get(key)
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None

            # 各指標取得
            market_cap = _get("marketCap")
            per        = _get("trailingPE")
            pbr        = _get("priceToBook")
            roe        = _get("returnOnEquity")
            roa        = _get("returnOnAssets")
            op_margin  = _get("operatingMargins")
            rev_growth = _get("revenueGrowth")
            de_ratio   = _get("debtToEquity")
            fcf        = _get("freeCashflow")
            op_cf      = _get("operatingCashflow")
            net_income = _get("netIncomeToCommon")
            total_assets = _get("totalAssets")
            rec_mean   = _get("recommendationMean")
            rec_key    = info.get("recommendationKey", "")
            target_mean = _get("targetMeanPrice")
            analyst_cnt = info.get("numberOfAnalystOpinions")
            div_yield  = _get("dividendYield")
            beta       = _get("beta")
            week52_chg = _get("52WeekChange")
            gross_margin = _get("grossMargins")
            ebitda_margin = _get("ebitdaMargins")
            revenue    = _get("totalRevenue")
            earnings_growth = _get("earningsGrowth")

            # アナリスト目標株価乖離率
            analyst_upside = None
            if target_mean and price and price > 0:
                analyst_upside = (target_mean - price) / price

            # アクルーアル比率
            accrual_ratio = None
            if net_income is not None and op_cf is not None \
                    and total_assets and total_assets > 0:
                accrual_ratio = (net_income - op_cf) / total_assets

            return {
                "ticker":           ticker,
                "name":             (info.get("shortName")
                                     or info.get("longName") or ticker),
                "sector":           info.get("sector", ""),
                "industry":         info.get("industry", ""),
                "category":         entry["category"],
                "business_model":   entry["business_model"],
                "in_sp500":         entry["in_sp500"],
                "fetch_ok":         True,

                # 株価・規模
                "current_price":    price,
                "market_cap":       market_cap,
                "market_cap_b":     market_cap / 1e9 if market_cap else None,

                # バリュエーション
                "per":              per,
                "pbr":              pbr,
                "div_yield":        div_yield,

                # 収益性
                "roe":              roe,
                "roa":              roa,
                "op_margin":        op_margin,
                "gross_margin":     gross_margin,
                "ebitda_margin":    ebitda_margin,

                # 成長率
                "revenue_growth":   rev_growth,
                "earnings_growth":  earnings_growth,

                # 財務健全性
                "debt_to_equity":   de_ratio,
                "free_cashflow":    fcf,
                "operating_cashflow": op_cf,
                "total_revenue":    revenue,

                # アクルーアル
                "accrual_ratio":    accrual_ratio,

                # アナリスト
                "rec_mean":         rec_mean,
                "rec_key":          rec_key,
                "analyst_upside":   analyst_upside,
                "analyst_count":    analyst_cnt,
                "target_price":     target_mean,

                # テクニカル
                "week52_change":    week52_chg,
                "beta":             beta,
            }

        except Exception as e:
            err_str = str(e).lower()
            # 429 レート制限：指数バックオフ
            if "429" in err_str or "too many" in err_str:
                wait = (2 ** attempt) * 3  # 3s, 6s, 12s
                time.sleep(wait)
            elif attempt < max_retries - 1:
                time.sleep(2)
            else:
                return None

    return None


# ==============================================================
# BLOCK 4: バッチ並列取得
# ==============================================================

def _fetch_batch(entries: list, max_workers: int = 5,
                 progress_interval: int = 100) -> list:
    """
    ThreadPoolExecutor で並列取得する。
    進捗を progress_interval 件ごとに表示する。
    """
    results  = []
    total    = len(entries)
    done_cnt = 0
    start    = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, e): e for e in entries}

        for future in as_completed(futures):
            done_cnt += 1
            try:
                result = future.result(timeout=15)
                if result:
                    results.append(result)
            except Exception:
                pass

            if done_cnt % progress_interval == 0 or done_cnt == total:
                elapsed = time.time() - start
                rate    = done_cnt / elapsed if elapsed > 0 else 0
                eta_min = (total - done_cnt) / rate / 60 if rate > 0 else 0
                ok_cnt  = len(results)
                print(f"    [{done_cnt:,}/{total:,}] "
                      f"取得成功:{ok_cnt:,}社 "
                      f"速度:{rate:.1f}社/秒 "
                      f"残り:{eta_min:.0f}分")

    return results


# ==============================================================
# BLOCK 5: スクリーニングフィルター
# ==============================================================

def _apply_filter(c: dict) -> tuple:
    """
    1銘柄にビジネスモデル別フィルターを適用する。

    Returns:
        (pass_filter: bool, fail_reasons: list[str])
    """
    model  = c.get("business_model", "mixed")
    flt    = BUSINESS_MODEL_FILTERS.get(model, DEFAULT_FILTER)
    fails  = []

    # PER
    per = c.get("per")
    if flt["per_max"] is not None and per is not None:
        if per <= 0 or per > flt["per_max"]:
            fails.append(f"PER={per:.1f} > {flt['per_max']}")

    # PBR
    pbr = c.get("pbr")
    if flt["pbr_max"] is not None and pbr is not None:
        if pbr <= 0 or pbr > flt["pbr_max"]:
            fails.append(f"PBR={pbr:.1f} > {flt['pbr_max']}")

    # ROE
    roe = c.get("roe")
    if flt["roe_min"] is not None and roe is not None:
        if roe < flt["roe_min"]:
            fails.append(f"ROE={roe*100:.1f}% < {flt['roe_min']*100:.0f}%")

    # 営業利益率
    opm = c.get("op_margin")
    if flt["op_margin_min"] is not None and opm is not None:
        if opm < flt["op_margin_min"]:
            fails.append(f"OPM={opm*100:.1f}% < {flt['op_margin_min']*100:.0f}%")

    # D/E比率
    de = c.get("debt_to_equity")
    if flt["de_max"] is not None and de is not None:
        if de > flt["de_max"]:
            fails.append(f"D/E={de:.0f} > {flt['de_max']}")

    # FCF
    fcf = c.get("free_cashflow")
    if flt["fcf_positive"] and fcf is not None:
        if fcf < 0:
            fails.append(f"FCF<0 ({fcf/1e6:.0f}M)")

    # 売上成長率
    rg = c.get("revenue_growth")
    if flt["revenue_growth_min"] is not None and rg is not None:
        if rg < flt["revenue_growth_min"]:
            fails.append(f"RevGrowth={rg*100:.1f}% < "
                         f"{flt['revenue_growth_min']*100:.0f}%")

    # アナリスト推奨
    rec = c.get("rec_mean")
    if flt["rec_mean_max"] is not None and rec is not None:
        if rec > flt["rec_mean_max"]:
            fails.append(f"Rec={rec:.1f} > {flt['rec_mean_max']}")

    return len(fails) == 0, fails


# ==============================================================
# BLOCK 6: 複合スコア計算（US株版）
# ==============================================================

def _calc_score(c: dict) -> float:
    """
    米国株用の複合スコア（0〜100）を計算する。

    スコアリング:
      収益性      (max 25): ROE / ROA / 営業利益率
      成長性      (max 20): 売上成長率 / 利益成長率
      バリュ      (max 15): PER / PBR 相対水準
      財務健全性  (max 15): D/E / FCF
      アナリスト  (max 15): 推奨平均 / 目標株価乖離
      モメンタム  (max 10): 52週騰落率
    """
    score = 0.0

    # ── 収益性 (max 25) ──────────────────────────────────────
    roe = c.get("roe")
    if roe is not None:
        if roe >= 0.25:   score += 10
        elif roe >= 0.15: score += 7
        elif roe >= 0.08: score += 4

    opm = c.get("op_margin")
    if opm is not None:
        if opm >= 0.25:   score += 8
        elif opm >= 0.15: score += 5
        elif opm >= 0.05: score += 2

    gm = c.get("gross_margin")
    if gm is not None:
        if gm >= 0.70:    score += 7
        elif gm >= 0.50:  score += 4
        elif gm >= 0.30:  score += 1

    # ── 成長性 (max 20) ──────────────────────────────────────
    rg = c.get("revenue_growth")
    if rg is not None:
        if rg >= 0.30:    score += 12
        elif rg >= 0.15:  score += 8
        elif rg >= 0.05:  score += 4

    eg = c.get("earnings_growth")
    if eg is not None:
        if eg >= 0.30:    score += 8
        elif eg >= 0.15:  score += 5
        elif eg >= 0.05:  score += 2

    # ── バリュエーション (max 15) ─────────────────────────────
    per = c.get("per")
    if per is not None and per > 0:
        if per <= 10:     score += 8
        elif per <= 20:   score += 5
        elif per <= 30:   score += 2

    pbr = c.get("pbr")
    if pbr is not None and pbr > 0:
        if pbr <= 1.5:    score += 7
        elif pbr <= 3.0:  score += 4
        elif pbr <= 6.0:  score += 1

    # ── 財務健全性 (max 15) ───────────────────────────────────
    fcf = c.get("free_cashflow")
    if fcf is not None:
        if fcf > 0:       score += 8
        # アクルーアル
        acr = c.get("accrual_ratio")
        if acr is not None:
            if acr < -0.02:   score += 7
            elif acr < 0.02:  score += 4
            elif acr > 0.05:  score -= 5   # 利益水増し疑い

    # ── アナリスト (max 15) ───────────────────────────────────
    rec = c.get("rec_mean")
    if rec is not None:
        if rec <= 1.5:    score += 8
        elif rec <= 2.0:  score += 5
        elif rec <= 2.5:  score += 2

    upside = c.get("analyst_upside")
    if upside is not None:
        if upside >= 0.30:  score += 7
        elif upside >= 0.15: score += 4
        elif upside >= 0.05: score += 1

    # ── モメンタム (max 10) ───────────────────────────────────
    w52 = c.get("week52_change")
    if w52 is not None:
        if w52 >= 0.30:   score += 10
        elif w52 >= 0.10: score += 6
        elif w52 >= 0:    score += 3
        elif w52 < -0.30: score -= 3

    # S&P500構成銘柄はボーナス+3
    if c.get("in_sp500"):
        score += 3

    return round(min(max(score, 0), 100), 1)


# ==============================================================
# BLOCK 7: レポート生成
# ==============================================================

def _build_report(passed: list, screened: int, cfg) -> str:
    """スクリーニング結果のMarkdownレポートを生成する"""
    today = datetime.now().strftime("%Y-%m-%d")
    top_n = getattr(cfg, "US_SCREENER_TOP_N", 10)
    top   = sorted(passed, key=lambda x: x.get("composite_score", 0),
                   reverse=True)[:top_n]

    lines = [
        f"# 🇺🇸 米国株スクリーニングレポート",
        f"**{today}** | スクリーニング: {screened:,}社 → 通過: {len(passed)}社",
        "",
        "---",
        "",
        f"## 🏆 スコア上位 {len(top)}社",
        "",
        "| 銘柄 | 社名 | カテゴリ | スコア | PER | ROE | OPM | RevGrowth | 推奨 | 目標乖離 | アクルーアル |",
        "|-----|------|---------|-------|-----|-----|-----|-----------|-----|---------|------------|",
    ]

    def _p(v, mult=100, sfx="%", digits=1):
        if v is None: return "N/A"
        return f"{v*mult:.{digits}f}{sfx}"

    def _f(v, digits=1):
        if v is None: return "N/A"
        return f"{v:.{digits}f}"

    for c in top:
        acr   = c.get("accrual_ratio")
        acr_s = f"{acr*100:+.1f}%" if acr is not None else "N/A"
        acr_i = "⚠️" if acr and acr > 0.05 else ("✅" if acr and acr < -0.02 else "")
        lines.append(
            f"| {c['ticker']} "
            f"| {c.get('name','')[:14]} "
            f"| {c.get('category','')} "
            f"| {c.get('composite_score',0):.0f} "
            f"| {_f(c.get('per'))}x "
            f"| {_p(c.get('roe'))} "
            f"| {_p(c.get('op_margin'))} "
            f"| {_p(c.get('revenue_growth'))} "
            f"| {c.get('rec_key','N/A')} "
            f"| {_p(c.get('analyst_upside'))} "
            f"| {acr_i}{acr_s} |"
        )

    # カテゴリ別サマリー
    lines += ["", "## 📊 カテゴリ別通過数", ""]
    cat_counts = {}
    for c in passed:
        cat = c.get("category", "other")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        lines.append(f"- {cat}: {cnt}社")

    lines += ["", "---",
              f"*自動生成: us_stock_screener.py v1.0 | {today}*"]
    return "\n".join(lines)


# ==============================================================
# BLOCK 8: 日次キャッシュ管理
# ==============================================================

def _load_daily_cache(cache_path: Path) -> dict:
    """当日の取得済み結果を読み込む（なければ空dict）"""
    today = datetime.now().strftime("%Y-%m-%d")
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("date") == today:
            return data.get("results", {})
    except Exception:
        pass
    return {}


def _save_daily_cache(cache_path: Path, results_dict: dict):
    """当日の取得済み結果を保存する"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"date": today, "results": results_dict},
                       ensure_ascii=False, separators=(',', ':')),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"    ⚠️  USスクリーナーキャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 9: メインエントリポイント
# ==============================================================

def run_us_stock_screener(cfg, results: dict = None) -> dict:
    """
    stock_analyzer_main.py の run_all_tasks() から呼ばれる。

    Args:
        cfg     : config モジュール
        results : run_all_tasks の results dict（us_sectors 参照用）

    Returns:
        dict（results["us_screener"] に格納される）
    """
    if not getattr(cfg, "ENABLE_US_SCREENER", False):
        print("  ⏩ 米国株スクリーニング: スキップ（ENABLE_US_SCREENER=False）")
        return {"skipped": True, "candidates": [], "passed": [], "pass_count": 0}

    print("\n  🇺🇸 米国株スクリーニング開始...")
    start_time = time.time()

    # キャッシュパス
    data_dir   = Path(getattr(cfg, "DATA_DIR", "/tmp"))
    cache_path = data_dir / "us_screener_daily_cache.json"
    daily_cache = _load_daily_cache(cache_path)

    # ── ユニバース読み込み ─────────────────────────────────────
    print("  📂 ユニバース読み込み中...")
    universe = load_universe(cfg)
    if not universe:
        print("  ⚠️  ユニバースが空です。custom_universe/ を確認してください。")
        return {"skipped": True, "candidates": [], "passed": [],
                "pass_count": 0, "error": "empty_universe"}

    # キャッシュ済みをスキップ
    cached_tickers = set(daily_cache.keys())
    to_fetch = [e for e in universe if e["ticker"] not in cached_tickers]
    print(f"  📊 取得対象: {len(to_fetch):,}社 "
          f"（キャッシュ済み: {len(cached_tickers):,}社）")

    # ── yfinance 並列取得 ─────────────────────────────────────
    max_workers = getattr(cfg, "US_SCREENER_MAX_WORKERS", 5)
    fresh_results = []
    if to_fetch:
        print(f"  🔄 {max_workers}並列で取得中（約"
              f"{len(to_fetch)*2.2/60:.0f}分の見込み）...")
        fresh_results = _fetch_batch(to_fetch, max_workers=max_workers)

        # キャッシュ更新
        for r in fresh_results:
            daily_cache[r["ticker"]] = r
        _save_daily_cache(cache_path, daily_cache)

    # キャッシュ済みデータをマージ
    all_candidates = fresh_results.copy()
    meta_map = {e["ticker"]: e for e in universe}
    for ticker, cached_data in daily_cache.items():
        if ticker not in {r["ticker"] for r in fresh_results}:
            # カテゴリ情報を補完
            if ticker in meta_map:
                cached_data["category"]       = meta_map[ticker]["category"]
                cached_data["business_model"] = meta_map[ticker]["business_model"]
                cached_data["in_sp500"]       = meta_map[ticker]["in_sp500"]
            all_candidates.append(cached_data)

    print(f"  ✅ 総取得: {len(all_candidates):,}社")

    # ── フィルター適用 ─────────────────────────────────────────
    print("  🔍 フィルター適用中...")
    passed = []
    for c in all_candidates:
        if not c.get("fetch_ok"):
            continue
        ok, fails = _apply_filter(c)
        c["pass_filters"] = ok
        c["fail_reasons"] = fails
        if ok:
            c["composite_score"] = _calc_score(c)
            passed.append(c)

    passed_sorted = sorted(passed,
                           key=lambda x: x.get("composite_score", 0),
                           reverse=True)
    top_n  = getattr(cfg, "US_SCREENER_TOP_N", 10)
    top_picks = passed_sorted[:top_n]

    # カテゴリ別通過銘柄
    by_category: dict = {}
    for c in passed_sorted:
        cat = c.get("category", "other")
        by_category.setdefault(cat, []).append(c)

    # ── レポート保存 ───────────────────────────────────────────
    try:
        today     = datetime.now().strftime("%Y-%m-%d")
        out_dir   = Path(cfg.INSIGHT_DIR) / "us_screener"
        out_dir.mkdir(parents=True, exist_ok=True)
        report_md = _build_report(passed_sorted, len(all_candidates), cfg)
        report_path = out_dir / f"us_screener_{today}.md"
        report_path.write_text(report_md, encoding="utf-8")
        print(f"    📄 レポート保存: {report_path}")
    except Exception as e:
        print(f"    ⚠️  レポート保存失敗: {str(e)[:60]}")
        report_path = None

    elapsed = time.time() - start_time
    print(f"\n  🏁 米国株スクリーニング完了")
    print(f"     スクリーニング: {len(all_candidates):,}社")
    print(f"     通過: {len(passed_sorted)}社")
    print(f"     所要時間: {elapsed/60:.1f}分")
    print(f"     TOP3: "
          + " / ".join(c["ticker"] for c in top_picks[:3]))

    return {
        "candidates":     all_candidates,
        "passed":         passed_sorted,
        "pass_count":     len(passed_sorted),
        "screened_count": len(all_candidates),
        "top_picks":      top_picks,
        "by_category":    by_category,
        "report_path":    str(report_path) if report_path else "",
        "elapsed_min":    round(elapsed / 60, 1),
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "skipped":        False,
    }


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util, sys

    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break

    print("=== us_stock_screener.py v1.0 単体テスト ===")
    print(f"ENABLE_US_SCREENER: {getattr(cfg, 'ENABLE_US_SCREENER', False)}")
    print(f"MAX_WORKERS: {getattr(cfg, 'US_SCREENER_MAX_WORKERS', 5)}")
    print(f"CATEGORIES: {getattr(cfg, 'US_SCREENER_CATEGORIES', [])}")

    result = run_us_stock_screener(cfg, {})
    print(f"\n通過銘柄数: {result['pass_count']}")
    print(f"TOP5:")
    for c in result.get("top_picks", [])[:5]:
        print(f"  {c['ticker']:8s} スコア:{c.get('composite_score',0):.0f} "
              f"PER:{c.get('per','N/A')} ROE:{c.get('roe','N/A')}")
