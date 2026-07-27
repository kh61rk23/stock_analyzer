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
    _udir_cfg = getattr(cfg, "US_CUSTOM_UNIVERSE_DIR", "data/custom_universe")

    # フォールバック候補（優先順）
    candidates = [
        Path(__file__).parent / _udir_cfg,              # リポジトリ直下/data/custom_universe ← 正解
        Path(getattr(cfg, "DRIVE_BASE", "")) / _udir_cfg,  # stock_data/data/custom_universe
        Path("/content") / _udir_cfg,                   # Colab環境
    ]

    found_dir = None
    for d in candidates:
        if d.exists():
            found_dir = d
            break

    if not found_dir:
        print("  ⚠️  custom_universe ディレクトリが見つかりません")
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

    [U-5] 取得・正規化・リトライは market_data.fetch_quote() へ移した。
          本関数は「fetch_quote の返り値を US 従来のキー名へ写像し、
          entry 由来のフィールドと XBRL 補完を足すだけの薄い層」であり、
          返り値のキー集合・値は U-5 以前と同一。

    Returns:
        dict（取得成功） or None（失敗）
    """
    ticker = entry["ticker"]

    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, str(Path(__file__).parent))
        _md = _imp.import_module("market_data")

        # リトライ（429 → 3s/6s/12s、その他 → 2s）は fetch_quote 側に集約済み
        q = _md.fetch_quote(ticker, "US", max_retries=max_retries)
        if q is None:
            return None

        raw = q["_raw_info"]

        # [F-4] market_cap_b は JP=億円 / US=十億USD で **同名だが単位が違う**。
        # 互換性維持のため単位の是正はここでは行わない（要件定義書 §3-3(a)）。
        # 消費側で市場をまたいで比較する場合は、必ず生値
        # market_data.fetch_quote()["market_cap_raw"] を使うこと。
        market_cap = q["market_cap_raw"]

        # ── [Phase US-1] XBRL財務データで補完 ──────────────────
        _xbrl_accel = None
        _xbrl_fcf   = None
        try:
            _ufr      = _imp.import_module("us_fundamental_rotator")
            xbrl_data = _ufr.lookup_us_fundamentals_by_path(
                Path(__file__).parent / "stock_data" / "data", ticker
            )
            if xbrl_data:
                _xbrl_accel = xbrl_data.get("eps_accel_score_xbrl")
                _xbrl_fcf   = xbrl_data.get("latest_fcf")
        except Exception:
            pass

        return {
            "ticker":           ticker,
            "name":             q["short_name"],
            "sector":           q["sector"],
            "industry":         q["industry"],
            "category":         entry["category"],
            "business_model":   entry["business_model"],
            "in_sp500":         entry["in_sp500"],
            "fetch_ok":         True,

            # 株価・規模
            "current_price":    q["current_price"],
            "market_cap":       market_cap,
            "market_cap_b":     market_cap / 1e9 if market_cap else None,  # 十億USD

            # バリュエーション
            "per":              q["per"],
            "pbr":              q["pbr"],
            "div_yield":        q["div_yield"],

            # 収益性
            "roe":              q["roe"],
            "roa":              q["roa"],
            "op_margin":        q["op_margin"],
            "gross_margin":     q["gross_margin"],
            "ebitda_margin":    q["ebitda_margin"],

            # 成長率
            "revenue_growth":   q["revenue_growth"],
            "earnings_growth":  q["earnings_growth"],

            # 財務健全性
            "debt_to_equity":   q["debt_to_equity"],
            "free_cashflow":    q["free_cashflow"],
            "operating_cashflow": q["operating_cashflow"],
            "total_revenue":    q["total_revenue"],

            # アクルーアル
            "accrual_ratio":    q["accrual_ratio"],

            # アナリスト
            "rec_mean":         q["rec_mean"],
            # rec_key / analyst_count は従来値を1ビットも変えないために生 info
            # から取る（q["rec_key"] は None を "" へ、q["analyst_count"] は
            # int へ正規化しており、US 従来挙動＝素通しと食い違うため）。
            "rec_key":          raw.get("recommendationKey", ""),
            "analyst_upside":   q["analyst_upside"],
            "analyst_count":    raw.get("numberOfAnalystOpinions"),
            "target_price":     q["target_price"],

            # テクニカル
            "week52_change":    q["week52_change"],
            "beta":             q["beta"],

            # [Phase US-1] XBRL財務補完（取得失敗時はNone）
            "eps_accel_score_xbrl": _xbrl_accel,
            "latest_fcf_xbrl":      _xbrl_fcf,
        }

    except Exception:
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

def _calc_catalyst_bonus_us(
    ticker:   str,
    gics:     str,
    cat_data: dict,
    cfg=None,
) -> float:
    """FE-7: US株版カタリストボーナス（最大 +5pt）。実体は screener_common へ移設（U-3）。"""
    from screener_common import calc_catalyst_bonus
    return calc_catalyst_bonus(ticker, gics, cat_data, cfg)

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
# BLOCK 4.5: Wave 2 FE関数（FE-1 / FE-2 US版）
# ==============================================================

def _calc_risk_adjusted_score_us(
    ticker:          str,
    composite_score: float,
    c:               dict,
    cfg=None,
) -> float:
    """FE-1: US株版 ATR基準リスク調整スコア。実体は screener_common へ移設（U-3）。"""
    from screener_common import calc_risk_adjusted_score
    return calc_risk_adjusted_score(ticker, composite_score, c, cfg)


def _calc_revision_score_us(
    ticker: str,
    c:      dict,
    cfg=None,
) -> float:
    """FE-2: US株版アナリスト修正スコア（上限+5pt）。実体は screener_common へ移設（U-3）。"""
    from screener_common import calc_revision_score
    return calc_revision_score(ticker, c, "US", cfg)


def _check_pead_trigger_us(
    ticker:         str,
    revision_score: float,
    cfg=None,
) -> bool:
    """FE-2: US株版 PEAD トリガー判定。実体は screener_common へ移設（U-3）。"""
    from screener_common import check_pead_trigger
    return check_pead_trigger(ticker, revision_score, cfg)

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
# BLOCK 5.5: GICSマクロ感応度補正（Wave 1 - U-1）
# ==============================================================

# yfinance sector 文字列 → US セクターETFシンボル変換マップ
_GICS_ETF_MAP: dict = {
    "Technology":             "XLK",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Financial Services":     "XLF",
    "Healthcare":             "XLV",
    "Energy":                 "XLE",
    "Basic Materials":        "XLB",
    "Industrials":            "XLI",
    "Communication Services": "XLC",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    # yfinance 表記ゆれ対応
    "Financial":              "XLF",
    "Health Care":            "XLV",
    "Consumer Staples":       "XLP",
    "Information Technology": "XLK",
}


def _get_active_macro_factors(results: dict) -> set:
    """
    results["bond_commodity"] から現在アクティブなマクロファクターを判定する。

    発動閾値:
      interest_rate_rise  : US10Y 1M変化 >= +0.10%pt（+10bps）
      interest_rate_fall  : US10Y 1M変化 <= -0.10%pt
      oil_price_rise      : WTI/Brent 1Mリターン >= +5.0%
      recession_risk      : recession_score >= 45 OR スプレッド < -0.20%

    Returns:
        set[str]: アクティブファクターのセット
    """
    active: set = set()
    try:
        bc = results.get("bond_commodity", {})
        if not bc:
            return active

        # ── 金利方向（US10Y 1M変化、単位: %pt）─────────────────
        yields_data = bc.get("yields", {})
        us10y_chg   = float(
            yields_data.get("us10y", {}).get("change_1m", 0) or 0
        )
        if us10y_chg >= 0.10:
            active.add("interest_rate_rise")
        elif us10y_chg <= -0.10:
            active.add("interest_rate_fall")

        # ── 原油価格方向（1Mリターン %）──────────────────────────
        com_data = bc.get("commodity_data", bc.get("commodities", {}))
        oil_ret  = float(
            com_data.get("wti",  {}).get("ret_1m", 0)
            or com_data.get("oil",  {}).get("ret_1m", 0)
            or com_data.get("brent", {}).get("ret_1m", 0)
            or 0
        )
        if oil_ret >= 5.0:
            active.add("oil_price_rise")

        # ── リセッションリスク────────────────────────────────────
        recession_sc = int(bc.get("recession_score", 0) or 0)
        curve        = bc.get("curve_analysis", {})
        spread_2y10y = curve.get("spread_2y10y")
        inverted     = curve.get("is_inverted_2y10y", False)
        if (recession_sc >= 45
                or inverted
                or (spread_2y10y is not None
                    and float(spread_2y10y) < -0.20)):
            active.add("recession_risk")

    except Exception:
        pass
    return active


def _apply_gics_macro_correction(
    score:          float,
    c:              dict,
    cfg,
    active_factors: set,
) -> float:
    """
    US_SECTOR_RULES に基づきGICSセクター別マクロ感応度補正を適用する（U-1）。

    補正量の上限クリップ: ±10pt（単一銘柄への過補正防止）
    最終スコア: 0〜100 にクリップ

    Args:
        score:          現在の composite_score
        c:              銘柄データ辞書（"sector" or "category" キーを参照）
        cfg:            config モジュール
        active_factors: _get_active_macro_factors() の返り値

    Returns:
        float: 補正後スコア
    """
    try:
        if not active_factors:
            return score

        # yfinance sector 名 → ETFシンボル変換
        sector_name = str(c.get("sector", c.get("category", "")) or "")
        etf_sym     = _GICS_ETF_MAP.get(sector_name, "")
        if not etf_sym:
            return score

        rules       = getattr(cfg, "US_SECTOR_RULES", {})
        sector_rule = rules.get(etf_sym, {})
        sensitivities = sector_rule.get("macro_sensitivity", {})
        if not sensitivities:
            return score

        total_delta = 0.0
        for factor in active_factors:
            rule = sensitivities.get(factor)
            if rule:
                total_delta += float(rule.get("score_delta", 0))

        # ±10pt クリップ（1銘柄への過補正を防止）
        total_delta = max(-10.0, min(10.0, total_delta))
        if total_delta == 0.0:
            return score

        corrected = round(max(0.0, min(100.0, score + total_delta)), 1)
        c["gics_macro_delta"] = round(total_delta, 1)   # デバッグ用に記録
        return corrected

    except Exception:
        return score

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
        "# 🇺🇸 米国株スクリーニングレポート",
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

    # [Wave 3 - FE-7] カタリストカレンダーをループ外で一括読み込み
    _cat_data_us: dict = {}
    try:
        from catalyst_calendar import load_catalyst_calendar as _load_cat_us
        _cat_data_us = _load_cat_us(cfg) or {}
    except Exception:
        pass

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
    """当日の取得済み結果を読み込む（なければ空dict）。当日分のみ有効。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if not cache_path.exists():
        return {}
    try:
        raw  = json.loads(cache_path.read_text(encoding="utf-8"))
        # 日付チェック：当日のキャッシュのみ使用
        if raw.get("date") != today:
            return {}
        return raw.get("results", {})
    except Exception:
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
    # ── [U-1] GICS マクロ感応度補正用ファクター取得 ────────────────
    _active_mac = _get_active_macro_factors(results if results else {})
    if _active_mac:
        print(f"    📡 [U-1] GICSマクロ補正: {_active_mac}")
    passed = []
    # ── [Wave 3 - FE-7] カタリストカレンダーをループ外で一括読み込み ──
    _cat_data_us: dict = {}
    try:
        from catalyst_calendar import load_catalyst_calendar as _load_cat_us
        _cat_data_us = _load_cat_us(cfg) or {}
    except Exception:
        pass
    for c in all_candidates:
        if not c.get("fetch_ok"):
            continue
        ok, fails = _apply_filter(c)
        c["pass_filters"] = ok
        c["fail_reasons"] = fails
        if ok:
            c["composite_score"] = _calc_score(c)
            # ── [U-1] GICSマクロ感応度補正 ──────────────────────
            if _active_mac:
                c["composite_score"] = _apply_gics_macro_correction(
                    c["composite_score"], c, cfg, _active_mac
                )

            # ── [Wave 3 - U-3] Form 4 インサイダーボーナス ───────
            try:
                from us_insider_form4_scraper import calc_insider_bonus as _ins_b
                _ins_bonus = _ins_b(c.get("ticker", ""), cfg=cfg)
                if _ins_bonus != 0.0:
                    c["insider_bonus"]   = _ins_bonus
                    c["composite_score"] = round(
                        min(100.0, max(0.0,
                            c["composite_score"] + _ins_bonus)), 1
                    )
            except Exception:
                c.setdefault("insider_bonus", 0.0)

            # ── [Wave 3 - BM Ph3] BM-06 SaaS 正規化 ─────────────
            try:
                from business_model_normalizer import (
                    detect_bm_norm_type    as _det_bm_us,
                    calc_bm_bonus          as _calc_bm_us,
                    NORM_SAAS,
                )
                _gics = str(c.get("gics_sector", c.get("sector", "")))
                _bm_us = _det_bm_us(sector_code=_gics, market="US")
                if _bm_us == NORM_SAAS:
                    _bm_bonus_us = _calc_bm_us(
                        ticker      = c.get("ticker", ""),
                        data        = c,
                        sector_code = _gics,
                        market      = "US",
                    )
                    if _bm_bonus_us != 0.0:
                        c["bm_bonus_us"]     = _bm_bonus_us
                        c["composite_score"] = round(
                            min(100.0, max(0.0,
                                c["composite_score"] + _bm_bonus_us)), 1
                        )
            except Exception:
                c.setdefault("bm_bonus_us", 0.0)

            # ── [Wave 3 - FE-7] Catalyst Bonus ───────────────────
            try:
                _gics_for_cat = str(c.get("gics_sector", ""))
                _cat_b_us = _calc_catalyst_bonus_us(
                    c.get("ticker", ""), _gics_for_cat, _cat_data_us, cfg
                )
                if _cat_b_us > 0:
                    c["catalyst_bonus"]  = _cat_b_us
                    c["composite_score"] = round(
                        min(100.0, c["composite_score"] + _cat_b_us), 1
                    )
            except Exception:
                c.setdefault("catalyst_bonus", 0.0)
            # ── [Wave 2 - Size Ph2] size_tier取得 + composite調整 ─
            try:
                from size_decay_manager import (
                    resolve_size_tier as _res_tier_us,
                    get_sector_score_weight_multiplier as _sec_mul,
                )
                _st = _res_tier_us(
                    ticker        = c.get("ticker", ""),
                    market_cap_raw= c.get("market_cap"),
                    market        = "US",
                )
                c["size_tier"] = _st
                # Micro/Nano はセクター依存度を低減（composite に乗数補正）
                _mul = _sec_mul(_st)
                if _mul < 1.0:
                    # sector相当分（推定35%）にのみ乗数を適用
                    _sector_portion = c["composite_score"] * 0.35
                    _other_portion  = c["composite_score"] * 0.65
                    c["composite_score"] = round(
                        min(100.0, _sector_portion * _mul + _other_portion), 1
                    )
            except Exception:
                c.setdefault("size_tier", "Mid")
            # ── [Pearl Ph1] 光る原石スコアを追加 ───────────────
            try:
                from pearl_score_calculator import calc_pearl_score as _cpearl
                if getattr(cfg, "ENABLE_PEARL_SCORE", True):
                    _pr = _cpearl(
                        c, cfg, market="US",
                        ticker=c.get("ticker", ""),
                    )
                    c["pearl_score"] = _pr.get("pearl_score", 0.0)
                    c["pearl_bonus"] = _pr.get("pearl_bonus", 0.0)
                    c["pearl_tier"]  = _pr.get("pearl_tier")
                    # pearl_bonus を composite_score に加算（上限100pt）
                    c["composite_score"] = round(
                        min(100.0, c["composite_score"] + c["pearl_bonus"]),
                        1,
                    )
                else:
                    c["pearl_score"] = 0.0
                    c["pearl_bonus"] = 0.0
                    c["pearl_tier"]  = None
            except Exception:
                c.setdefault("pearl_score", 0.0)
                c.setdefault("pearl_bonus", 0.0)
                c.setdefault("pearl_tier",  None)
            # ── [Wave 6 - U-4] institutional_bonus（13F機関投資家）──
            # institutional_signal_scorer の scored リストから
            # 当該ティッカーの adj_pt を取得し composite_score に加算する。
            # results["institutional_scorer"] 未格納時・モジュール未実行時は
            # 0.0 のままスキップし、composite_score に一切影響を与えない。
            # ★ try/except 厳重保護: 外部依存なし・dict参照のみだが
            #   KeyError/TypeError を完全に封じてシステム全停止を防ぐ
            try:
                if getattr(cfg, "ENABLE_INSTITUTIONAL_13F", True):
                    _inst_scorer = (results or {}).get(
                        "institutional_scorer", {}
                    )
                    if _inst_scorer:
                        _inst_scored = _inst_scorer.get("scored", [])
                        _ticker_now  = c.get("ticker", "")
                        # scored リストから当該ティッカーの adj_pt を取得
                        _inst_adj = next(
                            (
                                float(s.get("adj_pt", 0.0))
                                for s in _inst_scored
                                if s.get("ticker") == _ticker_now
                            ),
                            0.0,
                        )
                        # adj_pt を INSTITUTIONAL_BONUS_MAX/MIN でクリップ
                        _bonus_max = float(
                            getattr(cfg, "INSTITUTIONAL_BONUS_MAX", 8.0)
                        )
                        _bonus_min = float(
                            getattr(cfg, "INSTITUTIONAL_BONUS_MIN", -5.0)
                        )
                        _inst_bonus = round(
                            max(_bonus_min, min(_bonus_max, _inst_adj)), 2
                        )
                        if _inst_bonus != 0.0:
                            c["institutional_bonus"] = _inst_bonus
                            c["composite_score"]     = round(
                                min(100.0, max(0.0,
                                    c["composite_score"] + _inst_bonus)),
                                1,
                            )
                            # L3 Override 発動中の銘柄にフラグを付与
                            _l3_tickers = _inst_scorer.get(
                                "override_tickers", []
                            )
                            if _ticker_now in _l3_tickers:
                                c["l3_override"]     = True
                                c["l3_institution"]  = next(
                                    (
                                        s.get("institution", "")
                                        for s in _inst_scored
                                        if s.get("ticker") == _ticker_now
                                    ),
                                    "",
                                )
                        else:
                            c.setdefault("institutional_bonus", 0.0)
                    else:
                        c.setdefault("institutional_bonus", 0.0)
                else:
                    c.setdefault("institutional_bonus", 0.0)
            except Exception:
                c.setdefault("institutional_bonus", 0.0)
            # ── [Wave 7] cross_size_signal US固有ロジック ─────────
            # cross_size_signal.py の "jp_us_cross" シグナルのうち
            # 当該ティッカーが target_tickers に含まれる場合、
            # composite_score にシグナル強度に応じたボーナスを加算する。
            # Feature Flag: ENABLE_CROSS_SIZE_SIGNAL
            try:
                if getattr(cfg, "ENABLE_CROSS_SIZE_SIGNAL", False):
                    from cross_size_signal import get_signals_for_ticker as _get_sigs_us
                    import config as _cfg_css_us
                    _sigs_us = _get_sigs_us(
                        ticker = c.get("ticker", ""),
                        cfg    = _cfg_css_us,
                    )
                    _css_bonus_us = 0.0
                    for _sig in _sigs_us:
                        # jp_us_cross のうち bullish シグナルのみ採用
                        if (
                            _sig.get("signal_type") == "jp_us_cross"
                            and _sig.get("direction") == "bullish"
                        ):
                            # $bonus = strength \\times confidence \\times 0.5$
                            _css_bonus_us += round(
                                float(_sig.get("strength", 0.0))
                                * float(_sig.get("confidence", 0.0))
                                * 0.5,
                                2,
                            )
                        # upward_spillover も対象（US Large→US Mid/Small 伝播）
                        elif (
                            _sig.get("signal_type") == "upward_spillover"
                            and _sig.get("direction") == "bullish"
                            and c.get("size_tier", "Mid") in ("Small", "Micro")
                        ):
                            _css_bonus_us += round(
                                float(_sig.get("strength", 0.0))
                                * float(_sig.get("confidence", 0.0))
                                * 0.3,
                                2,
                            )
                    # ボーナスを上限 5.0pt でクリップして加算
                    _css_bonus_us = round(min(5.0, max(0.0, _css_bonus_us)), 2)
                    if _css_bonus_us > 0:
                        c["cross_size_bonus"]  = _css_bonus_us
                        c["composite_score"]   = round(
                            min(100.0, c["composite_score"] + _css_bonus_us), 1
                        )
                else:
                    c.setdefault("cross_size_bonus", 0.0)
            except Exception:
                c.setdefault("cross_size_bonus", 0.0)

            # ── [Wave 7] independence_score + latent_pearl ────────
            # independence_scores.json のキャッシュを参照し、
            # 市場独立性スコアを付与する。latent_pearl_score が
            # 閾値以上の銘柄は composite_score にボーナスを加算する。
            try:
                if getattr(cfg, "ENABLE_INDEPENDENCE_SCORE", False):
                    from independence_score_calculator import (
                        get_independence_score as _get_ind_us,
                        get_combined_score     as _get_comb_us,
                    )
                    _ind_us   = _get_ind_us(c.get("ticker", ""))
                    _comb_us  = _get_comb_us(c.get("ticker", ""))
                    c["independence_score"] = float(_ind_us or 0.0)
                    c["combined_score"]     = float(_comb_us or 0.0)
                else:
                    c.setdefault("independence_score", 0.0)
                    c.setdefault("combined_score",     0.0)
            except Exception:
                c.setdefault("independence_score", 0.0)
                c.setdefault("combined_score",     0.0)

            try:
                if getattr(cfg, "ENABLE_LATENT_PEARL_MANAGER", False):
                    from latent_pearl_manager import (
                        get_latent_pearl_score as _get_lps_us,
                        is_latent_pearl        as _is_lp_us,
                    )
                    import config as _cfg_lp_us
                    _lp_score_us = float(
                        _get_lps_us(c.get("ticker", ""), _cfg_lp_us) or 0.0
                    )
                    _is_lp_us_flag = _is_lp_us(
                        c.get("ticker", ""), _cfg_lp_us, min_score=30.0
                    )
                    c["latent_pearl_score"] = _lp_score_us
                    c["is_latent_pearl"]    = _is_lp_us_flag
                    # $bonus = \\min(5.0, latent\\_pearl\\_score / 20)$
                    if _is_lp_us_flag:
                        _lp_bonus_us = round(min(5.0, _lp_score_us / 20.0), 1)
                        c["latent_pearl_bonus"] = _lp_bonus_us
                        c["composite_score"]    = round(
                            min(100.0, c["composite_score"] + _lp_bonus_us), 1
                        )
                    else:
                        c.setdefault("latent_pearl_bonus", 0.0)
                else:
                    c.setdefault("latent_pearl_score",  0.0)
                    c.setdefault("is_latent_pearl",     False)
                    c.setdefault("latent_pearl_bonus",  0.0)
            except Exception:
                c.setdefault("latent_pearl_score",  0.0)
                c.setdefault("is_latent_pearl",     False)
                c.setdefault("latent_pearl_bonus",  0.0)

            # ── [Wave 7] taxonomy×size 交差評価ボーナス（US版）───
            # size_tier_master.json の taxonomy_size_correction を参照し、
            # US銘柄の composite_score に交差評価補正を直接加算する。
            # $tax\\_boost = \\min(3.0, (correction - 1.0) \\times 10)$
            try:
                if getattr(cfg, "ENABLE_SIZE_DECAY_MANAGER", False):
                    from size_tier_classifier import get_taxonomy_size_correction as _get_tsc
                    import config as _cfg_tsc
                    _tsc = float(
                        _get_tsc(c.get("ticker", ""), _cfg_tsc) or 1.0
                    )
                    c["taxonomy_size_correction"] = _tsc
                    _tsc_boost = round(min(3.0, max(-3.0, (_tsc - 1.0) * 10.0)), 2)
                    if abs(_tsc_boost) >= 0.1:
                        c["taxonomy_size_bonus"] = _tsc_boost
                        c["composite_score"]     = round(
                            min(100.0, max(0.0, c["composite_score"] + _tsc_boost)), 1
                        )
                    else:
                        c.setdefault("taxonomy_size_bonus", 0.0)
                else:
                    c.setdefault("taxonomy_size_correction", 1.0)
                    c.setdefault("taxonomy_size_bonus",      0.0)
            except Exception:
                c.setdefault("taxonomy_size_correction", 1.0)
                c.setdefault("taxonomy_size_bonus",      0.0)
            # ── [Wave 6 - 第4セッション] 縮退補正（US株） ────────
            # rule_optimizer.evaluate_contraction_flag() の判定結果を参照し、
            # 非防御セクターは composite_score を縮退係数で補正する。
            # 防御セクター（Consumer Staples / Health Care 等）は相対ボーナスを加算。
            # ★ try/except 厳重保護: 縮退ロジック未実行時も composite_score に影響なし
            try:
                _contraction_us = (results or {}).get(
                    "rule_optimizer", {}
                ).get("contraction_flag", {})
                _mode_us = _contraction_us.get("mode", "Normal")
                if _mode_us != "Normal":
                    import sys as _sys_cu, importlib as _imp_cu
                    from pathlib import Path as _pl_cu
                    _sys_cu.path.insert(0, str(_pl_cu(__file__).parent))
                    _ro_cu  = _imp_cu.import_module("rule_optimizer")
                    _cfg_cu = _imp_cu.import_module("config")
                    _gics_us = str(c.get("gics_sector", c.get("sector", "")))
                    # US銘柄の防御セクター判定（GICSセクター名で判定）
                    _is_def_us = _ro_cu.is_defensive_sector(
                        sector_code = _gics_us,
                        sector_name = _gics_us,
                        market      = "US",
                        cfg         = _cfg_cu,
                    )
                    _coeff_us = float(_contraction_us.get("coefficient", 1.00))
                    if _is_def_us:
                        # 防御セクター: 相対ボーナス加算
                        _bonus_map_us = {
                            "Warning":   float(getattr(_cfg_cu, "DEFENSIVE_RELATIVE_BONUS_WARNING",   3.0)),
                            "Emergency": float(getattr(_cfg_cu, "DEFENSIVE_RELATIVE_BONUS_EMERGENCY", 7.0)),
                        }
                        _bonus_us = _bonus_map_us.get(_mode_us, 0.0)
                        c["composite_score"] = round(
                            min(100.0, c["composite_score"] + _bonus_us), 1
                        )
                        c["contraction_defensive"] = True
                        c["contraction_bonus"]     = _bonus_us
                    else:
                        # 非防御セクター: 中心50点からの縮退
                        # $adjusted = 50 + (score - 50) \times coeff$
                        _adj_us = round(
                            max(0.0, min(100.0,
                                50.0 + (c["composite_score"] - 50.0) * _coeff_us
                            )), 1
                        )
                        c["contraction_adjusted"]  = True
                        c["pre_contraction_score"] = c["composite_score"]
                        c["composite_score"]       = _adj_us
                    c["contraction_mode"] = _mode_us
            except Exception:
                c.setdefault("contraction_mode", "Normal")
            # ── [Wave 6 - G-C] SEC Geographic Extractor 接続 ──────
            # sec_geographic_extractor.get_us_geo_exposure() で
            # geopolitics.company_country_exposure から権益エクスポージャーを取得し
            # geo_impact_bonus の事前データとして c に格納する。
            # ★ try/except 厳重保護: BQ接続失敗・モジュール未配置時も
            #   composite_score に影響を与えずシステム全停止を防ぐ
            try:
                if getattr(cfg, "ENABLE_GEO_SIGNAL", False):
                    import sys as _sys_geo, importlib as _imp_geo
                    from pathlib import Path as _pl_geo
                    _sys_geo.path.insert(
                        0, str(_pl_geo.Path(__file__).parent)
                    )
                    _sge = _imp_geo.import_module(
                        "sec_geographic_extractor"
                    )
                    _geo_exp = _sge.get_us_geo_exposure(
                        ticker     = c.get("ticker", ""),
                        cfg        = cfg,
                        project_id = getattr(
                            cfg, "GEO_BQ_PROJECT_ID",
                            "stock-analyzer-edinet"
                        ),
                    )
                    # exposure リストを候補データに格納
                    # （geo_impact_bonus 計算は geo_signal.py で行う）
                    c["geo_exposures"] = _geo_exp
                else:
                    c.setdefault("geo_exposures", [])
            except ModuleNotFoundError:
                c.setdefault("geo_exposures", [])
            except Exception:
                c.setdefault("geo_exposures", [])
            # ── [Wave 2 - FE-1] risk_adjusted_score ─────────────
            try:
                c["risk_adjusted_score"] = _calc_risk_adjusted_score_us(
                    ticker=c.get("ticker", ""),
                    composite_score=c["composite_score"],
                    c=c,
                    cfg=cfg,
                )
            except Exception:
                c["risk_adjusted_score"] = c.get("composite_score", 0.0)

            # ── [Wave 2 - FE-2] revision_score + PEAD判定 ────────
            try:
                _rev = _calc_revision_score_us(c.get("ticker", ""), c, cfg)
                c["revision_score"] = _rev
                c["pead_trigger"]   = _check_pead_trigger_us(
                    c.get("ticker", ""), _rev, cfg
                )
                # composite_score に revision_score を加算（上限100pt）
                c["composite_score"] = round(
                    min(100.0, c["composite_score"] + _rev), 1
                )
            except Exception:
                c.setdefault("revision_score", 0.0)
                c.setdefault("pead_trigger",   False)
            # ──────────────────────────────────────────────────────
            # ★ passed.append(c) は全スコア加算完了後に実行する
            # （Wave 7ボーナス・縮退補正・FE-1・FE-2適用済みスコアを反映）
            passed.append(c)
    # ── Wave 7 デフォルトフィールド補完 ────────────────────────── 
    # 上記 try ブロックが全てスキップされた銘柄にデフォルト値を設定する
    for _c_def in passed:
        _c_def.setdefault("cross_size_bonus",         0.0)
        _c_def.setdefault("independence_score",        0.0)
        _c_def.setdefault("combined_score",            0.0)
        _c_def.setdefault("latent_pearl_score",        0.0)
        _c_def.setdefault("is_latent_pearl",           False)
        _c_def.setdefault("latent_pearl_bonus",        0.0)
        _c_def.setdefault("taxonomy_size_correction",  1.0)
        _c_def.setdefault("taxonomy_size_bonus",       0.0)

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
    _c_mode_log = (results or {}).get("rule_optimizer", {}).get(
        "contraction_flag", {}
    ).get("mode", "Normal")
    _c_icon_log = {"Normal": "✅", "Warning": "⚠️", "Emergency": "🚨"}.get(
        _c_mode_log, "✅"
    )
    print("\n  🏁 米国株スクリーニング完了")
    print(f"     スクリーニング: {len(all_candidates):,}社")
    print(f"     通過: {len(passed_sorted)}社")
    print(f"     所要時間: {elapsed/60:.1f}分")
    print(f"     {_c_icon_log} 縮退モード: {_c_mode_log}")
    print(f"     TOP3: "
          + " / ".join(c["ticker"] for c in top_picks[:3]))

    # 縮退モードのサマリーを集計
    _contraction_mode_summary = "Normal"
    _contraction_defensive_count = 0
    _contraction_adjusted_count  = 0
    try:
        _contraction_mode_summary = (
            (results or {}).get("rule_optimizer", {})
            .get("contraction_flag", {})
            .get("mode", "Normal")
        )
        _contraction_defensive_count = sum(
            1 for c in passed_sorted if c.get("contraction_defensive")
        )
        _contraction_adjusted_count = sum(
            1 for c in passed_sorted if c.get("contraction_adjusted")
        )
    except Exception:
        pass

    return {
        "candidates":                all_candidates,
        "passed":                    passed_sorted,
        "pass_count":                len(passed_sorted),
        "screened_count":            len(all_candidates),
        "top_picks":                 top_picks,
        "by_category":               by_category,
        "report_path":               str(report_path) if report_path else "",
        "elapsed_min":               round(elapsed / 60, 1),
        "timestamp":                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "skipped":                   False,
        # Wave 6 - 第4セッション: 縮退情報
        "contraction_mode":          _contraction_mode_summary,
        "contraction_defensive_count": _contraction_defensive_count,
        "contraction_adjusted_count":  _contraction_adjusted_count,
    }


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util

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
