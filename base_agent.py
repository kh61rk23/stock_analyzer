# ==============================================================
# base_agent.py  v2.0
# ==============================================================
# 【v1.1 → v2.0 変更点】
#   ① fetch_financials(): yfinance 全取得可能フィールドを追加（40+項目）
#      追加: 配当・アナリスト目標株価・FCF・D/Eレシオ・機関保有率・ベータ等
#   ② build_result(): edinet_client v3.0 の get_sector_data() に対応
#      analysis + financials + gov_meta を一括取得
#   ③ calc_composite_score(): アナリスト推奨スコアを加算（最大+5点）
#   ④ build_insight_text(): 財務トレンド・配当・アナリスト情報セクション追加
# ==============================================================

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from edinet_bq_client import EDINETBQClient as _BQClient

# ==============================================================
# BLOCK 1: yfinance 財務データ取得（v2.0 全フィールド）
# ==============================================================

def fetch_financials(tickers: list, max_retries: int = 2) -> list:
    """
    yfinance で複数銘柄の財務指標を全フィールド取得する。

    取得フィールド一覧:
      【基本株価】 currentPrice, previousClose, dayHigh, dayLow
                  fiftyDayAverage, twoHundredDayAverage
                  fiftyTwoWeekHigh, fiftyTwoWeekLow
                  volume, averageVolume, marketCap
      【バリュエーション】 trailingPE, forwardPE, priceToBook (PBR)
                  priceToSalesTrailing12Months (PSR)
                  enterpriseValue, enterpriseToRevenue, enterpriseToEbitda
                  trailingEps, forwardEps, bookValue
      【利益率】   grossMargins, operatingMargins, ebitdaMargins, profitMargins
      【成長率】   revenueGrowth, earningsGrowth, earningsQuarterlyGrowth
      【収益性】   returnOnEquity (ROE), returnOnAssets (ROA)
      【CF・財務】 freeCashflow, operatingCashflow, totalCash, totalDebt
                  totalRevenue, currentRatio, quickRatio, debtToEquity
      【配当】     dividendYield, dividendRate, payoutRatio
                  fiveYearAvgDividendYield, exDividendDate, lastDividendDate
      【アナリスト】targetMeanPrice, targetHighPrice, targetLowPrice
                  recommendationMean, recommendationKey, numberOfAnalystOpinions
      【その他】   beta, 52WeekChange, heldPercentInstitutions,
                  heldPercentInsiders, shortRatio, fullTimeEmployees
    """
    try:
        import yf_cache as yf
    except ImportError:
        print("  ⚠️  yfinance が未インストールです")
        return []

    # viewer の data_bridge 経由で取得を試みる（フォールバック豊富）
    _db = _load_data_bridge()
    if _db:
        results = []
        for ticker in tickers:
            try:
                raw = _db.fetch_stock_data(ticker)
                if raw and not raw.get("error"):
                    results.append(_normalize_viewer_data(ticker, raw))
            except Exception:
                pass
        if results:
            return results
    # data_bridge 失敗時は以降の yfinance 処理に流れる
    
    results = []

    for ticker in tickers:
        for attempt in range(1, max_retries + 1):
            try:
                info = yf.Ticker(ticker).info or {}
                # ── 空dictチェック（yfinance が無効ティッカーで {} を返す場合） ──
                # quoteType が存在しない = 上場廃止・ティッカー誤り・API障害
                if not info or not info.get("quoteType"):
                    if attempt < max_retries:
                        time.sleep(3)
                        continue  # リトライ
                    print(f"    ⚠️  {ticker}: yfinance が空データを返しました（上場廃止・ティッカー誤りの可能性）")
                    results.append({"ticker": ticker, "name": ticker, "fetch_ok": False})
                    break

                def _f(key: str) -> Optional[float]:
                    v = info.get(key)
                    try:
                        return float(v) if v is not None else None
                    except (TypeError, ValueError):
                        return None

                def _i(key: str) -> Optional[int]:
                    v = info.get(key)
                    try:
                        return int(v) if v is not None else None
                    except (TypeError, ValueError):
                        return None
                    
                # --- [修正箇所] ここから ---
                def _fv(key: str, lo: float, hi: float):
                    """値を取得し、範囲外は None を返すバリデーション付き取得。"""
                    v = _f(key)
                    if v is None:
                        return None
                    return v if lo <= v <= hi else None

                # アナリスト推奨（1.0=強気買い〜5.0=強気売り）
                rec_mean = _f("recommendationMean")
                rec_key  = info.get("recommendationKey", "")

                # 目標株価乖離率（targetMeanPrice / currentPrice - 1）
                cur_price    = _f("regularMarketPrice") or _f("currentPrice")
                target_mean  = _f("targetMeanPrice")
                upside = None
                if cur_price and target_mean and cur_price > 0:
                    upside = (target_mean / cur_price) - 1.0  # 正 = 上昇余地

                results.append({
                    # ── 識別 ──────────────────────────────────────
                    "ticker":        ticker,
                    "name":          info.get("shortName") or info.get("longName") or ticker,
                    "fetch_ok":      True,

                    # ── 基本株価 ──────────────────────────────────
                    "current_price":       cur_price,
                    "prev_close":          _f("previousClose"),
                    "day_high":            _f("dayHigh"),
                    "day_low":             _f("dayLow"),
                    "ma50":                _f("fiftyDayAverage"),
                    "ma200":               _f("twoHundredDayAverage"),
                    "week52_high":         _f("fiftyTwoWeekHigh"),
                    "week52_low":          _f("fiftyTwoWeekLow"),
                    "volume":              _i("volume"),
                    "avg_volume":          _i("averageVolume"),
                    "market_cap":          _f("marketCap"),
                    "week52_change":       _f("52WeekChange"),

                    # ── バリュエーション ───────────────────────────
                    "per":                 _f("trailingPE"),
                    "forward_per":         _f("forwardPE"),
                    "pbr":                 _f("priceToBook"),
                    "psr":                 _f("priceToSalesTrailing12Months"),
                    "ev":                  _f("enterpriseValue"),
                    "ev_revenue":          _f("enterpriseToRevenue"),
                    "ev_ebitda":           _f("enterpriseToEbitda"),
                    "eps_trailing":        _f("trailingEps"),
                    "eps_forward":         _f("forwardEps"),
                    "book_value":          _f("bookValue"),

                    # ── 利益率 ────────────────────────────────────
                    "gross_margin":        _f("grossMargins"),
                    "op_margin":           _f("operatingMargins"),
                    "ebitda_margin":       _f("ebitdaMargins"),
                    "net_margin":          _f("profitMargins"),

                    # ── 成長率 ────────────────────────────────────
                    "revenue_growth":      _f("revenueGrowth"),
                    "earnings_growth":     _f("earningsGrowth"),
                    "earnings_qgrowth":    _f("earningsQuarterlyGrowth"),

                    # ── 収益性 ────────────────────────────────────
                    "roe":                 _f("returnOnEquity"),
                    "roa":                 _f("returnOnAssets"),

                    # ── CF・財務健全性 ─────────────────────────────
                    "free_cashflow":       _f("freeCashflow"),
                    "operating_cashflow":  _f("operatingCashflow"),
                    "total_cash":          _f("totalCash"),
                    "total_debt":          _f("totalDebt"),
                    "total_revenue":       _f("totalRevenue"),
                    "current_ratio":       _f("currentRatio"),
                    "quick_ratio":         _f("quickRatio"),
                    "debt_to_equity":      _f("debtToEquity"),

                    # ── 配当 ──────────────────────────────────────
                    "dividend_yield":  _fv("dividendYield", 0.0, 0.30),
                    "dividend_rate":   _f("dividendRate"),
                    "payout_ratio":    _fv("payoutRatio", 0.0, 2.0),
                    "div_yield_5y_avg":    _f("fiveYearAvgDividendYield"),
                    "ex_dividend_date":    info.get("exDividendDate"),
                    "last_dividend_date":  info.get("lastDividendDate"),

                    # ── アナリスト ────────────────────────────────
                    "target_mean":         target_mean,
                    "target_high":         _f("targetHighPrice"),
                    "target_low":          _f("targetLowPrice"),
                    "analyst_upside":      upside,
                    "rec_mean":            rec_mean,
                    "rec_key":             rec_key,
                    "analyst_count":       _i("numberOfAnalystOpinions"),

                    # ── その他 ────────────────────────────────────
                    "beta":                _f("beta"),
                    "inst_ownership":      _f("heldPercentInstitutions"),
                    "insider_ownership":   _f("heldPercentInsiders"),
                    "short_ratio":         _f("shortRatio"),
                    "employees":           _i("fullTimeEmployees"),
                })
                break

            except Exception as e:
                if attempt == max_retries:
                    print(f"    ⚠️  {ticker} 取得失敗: {str(e)[:60]}")
                    results.append({"ticker": ticker, "name": ticker, "fetch_ok": False})
                else:
                    time.sleep(2)

    return results


# ==============================================================
# BLOCK 2: 数値フォーマット
# ==============================================================

def fmt_pct(v: Optional[float], digits: int = 1) -> str:
    if v is None: return "N/A"
    return f"{v * 100:.{digits}f}%"

def fmt_val(v: Optional[float], digits: int = 2) -> str:
    if v is None: return "N/A"
    return f"{v:.{digits}f}"

def fmt_cap(v: Optional[float]) -> str:
    if v is None: return "N/A"
    if v >= 1e12: return f"{v/1e12:.1f}兆円"
    if v >= 1e8:  return f"{v/1e8:.0f}億円"
    return f"{v:.0f}円"

def fmt_yen(v: Optional[float]) -> str:
    if v is None: return "N/A"
    return f"¥{v:,.0f}"

def _safe_avg(values: list) -> Optional[float]:
    v = [x for x in values if x is not None]
    return sum(v) / len(v) if v else None


# ==============================================================
# BLOCK 3: マクロシグナル評価
# ==============================================================

def build_macro_signals(sector_code: str, cfg,
                        macro_snapshot: dict = None) -> list:
    """
    macro_snapshot が渡された場合はライブ相場で条件判定する。
    渡されない場合（後方互換）は全ルールをスキャンして返す。
    """
    # ライブ相場値
    snap     = macro_snapshot or {}
    usdjpy   = snap.get("usdjpy")
    us10y    = snap.get("us10y")
    oil      = snap.get("oil_wti")
    vix      = snap.get("vix")

    # どのファクターが「現在アクティブか」を判定
    active_factors = set()
    if us10y  is not None:
        active_factors.add("interest_rate_rise" if us10y > 4.0
                           else "interest_rate_fall")
    if usdjpy is not None:
        active_factors.add("yen_depreciation"   if usdjpy > 140
                           else "yen_appreciation")
    if oil    is not None and oil > 80:
        active_factors.add("oil_price_rise")
    if vix    is not None and vix > 25:
        active_factors.add("us_recession_risk")

    signals = []
    for factor, rule in cfg.MACRO_RULES.items():
        # macro_snapshot が渡されていれば現在アクティブなルールのみ処理
        if macro_snapshot is not None and factor not in active_factors:
            continue
        for sig_type, codes in rule.get("signal", {}).items():
            if sector_code in codes:
                signals.append({
                    "factor":    factor,
                    "label":     rule.get("label", factor),
                    "signal":    sig_type,
                    "rationale": rule.get("rationale", {}).get(sector_code, ""),
                })
    return signals


# ==============================================================
# BLOCK 4: 総合スコア計算（v2.0 アナリスト推奨加算）
# ==============================================================

def calc_composite_score(
    companies:                list,
    macro_signals:            list,
    edinet_credit_bonus:      float = 0.0,
    policy_support_bonus:     float = 0.0,   # Wave 1 - I-2: NEDO政策支援ボーナス
    bm_bonus:                 float = 0.0,   # Wave 2 - BM Ph2: 業態別正規化ボーナス
    edinet_segment_bonus:     float = 0.0,   # Wave 4 - E3: セグメント収益性ボーナス
    edinet_text_risk_bonus:   float = 0.0,   # Wave 5 - E4: テキストリスクボーナス
    geo_impact_bonus:         float = 0.0,   # Wave 6 - G-X: 地政学×権益統合シグナル
    cluster_phase_bonus:      float = 0.0,   # Wave 6 - B: 技術クラスターフェーズボーナス
    real_estate_bonus:        float = 0.0,   # D-②-2: corporate property enrichment
) -> tuple:
    """
    財務スコア(40) + マクロスコア(30) + アナリストスコア(5) + EDINET信用ボーナス(最大+5/-5)
    + セグメントボーナス(最大+7/-5) + テキストリスクボーナス($-10$ 〜 $0$) の合計。

    edinet_credit_bonus:
      edinet_rotation_manager の蓄積DBから取得した credit_score を
      業種内平均に変換して加算する補正値。
      build_result() 側で計算して渡す。データ未取得時は 0.0。

    アナリストスコア:
      recommendationMean <= 2.0 (Buy相当): +5点
      recommendationMean <= 2.5 (Outperform相当): +3点
      analyst_upside >= 20%: +2点追加
    """
    fin_score     = 0.0
    macro_score   = 0.0
    analyst_score = 0.0

    ok_companies = [c for c in companies if c.get("fetch_ok")]
    if ok_companies:
        avg_roe = _safe_avg([c.get("roe") for c in ok_companies])
        avg_roa = _safe_avg([c.get("roa") for c in ok_companies])
        avg_opm = _safe_avg([c.get("op_margin") for c in ok_companies])

        if avg_roe is not None:
            fin_score += 15 if avg_roe >= 0.10 else (8 if avg_roe >= 0.05 else 0)
        if avg_roa is not None:
            fin_score += 10 if avg_roa >= 0.05 else (5 if avg_roa >= 0.02 else 0)
        if avg_opm is not None:
            fin_score += 15 if avg_opm >= 0.10 else (8 if avg_opm >= 0.05 else 0)
        # アクルーアルスコア（利益の質）
        # viewerからデータが取れている場合のみ評価
        ok_accrual = [c.get("accrual_ratio") for c in ok_companies
                      if c.get("accrual_ratio") is not None]
        if ok_accrual:
            avg_accrual = sum(ok_accrual) / len(ok_accrual)
            if avg_accrual < -0.02:    fin_score += 8   # 超優良
            elif avg_accrual < 0.02:   fin_score += 4   # 良好
            elif avg_accrual > 0.05:   fin_score -= 8   # 利益水増し疑い

        # アナリスト推奨（平均）
        avg_rec    = _safe_avg([c.get("rec_mean") for c in ok_companies])
        avg_upside = _safe_avg([c.get("analyst_upside") for c in ok_companies])
        if avg_rec is not None:
            analyst_score += 5 if avg_rec <= 2.0 else (3 if avg_rec <= 2.5 else 0)
        if avg_upside is not None and avg_upside >= 0.20:
            analyst_score += 2

    for s in macro_signals:
        if s["signal"] == "tailwind":   macro_score += 10
        elif s["signal"] == "headwind": macro_score -= 10

    total = fin_score + macro_score + analyst_score + edinet_credit_bonus + bm_bonus
    # Wave 2 - BM Ph2: bm_bonus が非ゼロの場合にログ出力
    if bm_bonus != 0.0:
        try:
            print(f"    🏦 BM Ph2ボーナス加算: {bm_bonus:+.2f}pt")
        except Exception:
            pass
    # ---- EDINET 直接DB ボーナス（E2追加）----
    # ★ 2026-06-11 バグ修正: 旧実装は未定義変数 ticker を参照して毎回
    #   NameError（業種単位の本関数に単一 ticker は存在しない）。
    #   geo_impact_bonus と同じ方式で fetch_ok 企業の平均ボーナスとして算出。
    edinet_hr_bonus = 0.0
    try:
        _hr_vals = []
        for _c in ok_companies:
            _code = str(_c.get("ticker", "") or "").split(".")[0]
            if _code:
                _hr_vals.append(float(_calc_edinet_hr_bonus(_code)))
        if _hr_vals:
            edinet_hr_bonus = round(sum(_hr_vals) / len(_hr_vals), 2)
    except Exception:
        edinet_hr_bonus = 0.0
    total += edinet_hr_bonus
    # ── [I-2] NEDO政策支援ボーナスを加算 ────────────────────────────
    # try/except 保護: BQ取得失敗・型不正時はシステム全停止を防ぎ 0.0 のまま継続
    try:
        _ps_bonus = float(policy_support_bonus)
        if _ps_bonus > 0:
            total += _ps_bonus
            print(
                f"    🏛️  政策支援ボーナス（NEDO採択）: +{_ps_bonus:.2f}pt"
            )
    except Exception:
        pass   # 失敗時は 0.0 のまま継続
    # ── [Wave 4 - E3] セグメント収益性ボーナスを加算 ─────────────────
    # try/except 保護: segment_signal 取得失敗時はシステム全停止を防ぎ 0.0 のまま継続
    # $edinet\_segment\_bonus \in [SEGMENT\_BONUS\_MIN(-5), SEGMENT\_BONUS\_MAX(+7)]$
    try:
        _seg_bonus = float(edinet_segment_bonus)
        if _seg_bonus != 0.0:
            total += _seg_bonus
            print(
                f"    📊 セグメントボーナス（E3）: {_seg_bonus:+.2f}pt"
            )
    except Exception:
        pass   # 失敗時は 0.0 のまま継続
    # ── [Wave 5 - E4] テキストリスクボーナスを加算 ───────────────────
    # try/except 保護: text_signal 取得失敗時はシステム全停止を防ぎ 0.0 のまま継続
    # $edinet\_text\_risk\_bonus \in [TEXT\_SIGNAL\_SCORE\_MIN(-10), 0]$
    try:
        _txt_bonus = float(edinet_text_risk_bonus)
        if _txt_bonus != 0.0:
            total += _txt_bonus
            print(
                f"    📝 テキストリスクボーナス（Wave5-E4）: {_txt_bonus:+.2f}pt"
            )
    except Exception:
        pass   # 失敗時は 0.0 のまま継続
    # ── [Wave 6 - G-X] 地政学×権益統合シグナルを加算 ─────────────────
    # try/except 保護: geo_signal 取得失敗時はシステム全停止を防ぎ 0.0 のまま継続
    # $geo\_impact\_bonus \in [GEO\_IMPACT\_MIN(-15), GEO\_IMPACT\_MAX(+15)]$
    try:
        _geo_bonus = float(geo_impact_bonus)
        if _geo_bonus != 0.0:
            total += _geo_bonus
            print(
                f"    🌐 地政学ボーナス（Wave6-G-X）: {_geo_bonus:+.2f}pt"
            )
    except Exception:
        pass
    # ── [Wave 6 - B] 技術クラスターフェーズボーナスを加算 ──────────────
    # try/except 保護: cluster_phase_cache 未生成・読込失敗時もシステム停止させない
    # $cluster\_phase\_bonus \in [CLUSTER\_BONUS\_MIN(-3), CLUSTER\_BONUS\_MAX(+8)]$
    try:
        _cluster_bonus = float(cluster_phase_bonus)
        if _cluster_bonus != 0.0:
            total += _cluster_bonus
            print(
                f"    🔬 クラスターフェーズボーナス（Wave6-B）: {_cluster_bonus:+.2f}pt"
            )
    except Exception:
        pass   # 失敗時は 0.0 のまま継続   # 失敗時は 0.0 のまま継続
    try:
        _re_bonus = max(-2.0, min(3.0, float(real_estate_bonus)))
        if _re_bonus:
            total += _re_bonus
            print(f"    🏢 不動産ボーナス（D-②）: {_re_bonus:+.2f}pt")
    except Exception:
        pass
    total = max(-30.0, min(100.0, total))   # ★追い風多数/逆風多数時の異常値防止

    headwinds = sum(1 for s in macro_signals if s["signal"] == "headwind")
    if headwinds >= 2 or total < 10:    risk_level = "high"
    elif headwinds == 1 or total < 25:  risk_level = "medium"
    else:                               risk_level = "low"

    return round(total, 1), risk_level


# ==============================================================
# BLOCK 5: インサイトテキスト生成（v2.0 全フィールド対応）
# ==============================================================

def build_insight_text(
    sector_name: str,
    companies:   list,
    macro_signals: list,
    score:       float,
    risk_level:  str,
    edinet_data: Optional[dict] = None,  # {ticker: full_company_data}
) -> str:
    today    = datetime.now().strftime("%Y-%m-%d")
    icon_map = {"tailwind": "💚 追い風", "headwind": "🔴 逆風", "watch": "🟡 注意"}
    risk_icon = {"high": "🔴 高", "medium": "🟡 中", "low": "💚 低"}.get(risk_level, "⚪")

    lines = [
        f"## {sector_name} 分析レポート",
        "",
        f"**更新**: {today} | **総合スコア**: {score}点 | **リスク**: {risk_icon}",
        "",
        "### マクロシグナル", "",
    ]

    if macro_signals:
        for s in macro_signals:
            lines.append(f"- {icon_map.get(s['signal'], '⚪')} **{s['label']}**: {s['rationale']}")
    else:
        lines.append("- ⚪ 現在特筆すべきマクロシグナルなし")

    # ── 財務サマリーテーブル（v2.0拡張: 配当・FCF・アナリスト追加）──
    lines += ["", "### 主要企業 財務サマリー", ""]
    lines.append("| 銘柄 | 時価総額 | ROE | 営業利益率 | PBR | PER | 配当利回り | 目標株価乖離 | アナリスト |")
    lines.append("|------|---------|-----|-----------|-----|-----|-----------|------------|----------|")

    for c in companies:
        if not c.get("fetch_ok"):
            lines.append(f"| {c['ticker']} | — | — | — | — | — | — | — | — |")
            continue
        acr = c.get("accrual_ratio")
        rec_str = c.get("rec_key", "N/A")
        upside_v = c.get("analyst_upside")
        upside_str = f"{upside_v*100:+.1f}%" if upside_v is not None else "N/A"
        lines.append(
            f"| {c['name'][:14]} "
            f"| {fmt_cap(c.get('market_cap'))} "
            f"| {fmt_pct(c.get('roe'))} "
            f"| {fmt_pct(c.get('op_margin'))} "
            f"| {fmt_val(c.get('pbr'))}x "
            f"| {fmt_val(c.get('per'), 1)}x "
            f"| {fmt_pct(c.get('dividend_yield'))} "
            f"| {upside_str} "
            f"| {rec_str} |"
        )

    # ── 財務健全性サマリー ──────────────────────────────────────
    ok = [c for c in companies if c.get("fetch_ok")]
    if ok:
        lines += ["", "### 財務健全性 & キャッシュフロー", ""]
        lines.append("| 銘柄 | D/Eレシオ | 流動比率 | FCF | 純利益率 | β値 | 機関保有率 |")
        lines.append("|------|----------|---------|-----|---------|-----|----------|")
        for c in ok:
            de   = fmt_val(c.get("debt_to_equity"), 1)
            cr   = fmt_val(c.get("current_ratio"), 1)
            fcf_v = c.get("free_cashflow")
            fcf  = fmt_cap(fcf_v) if fcf_v else "N/A"
            lines.append(
                f"| {c['name'][:14]} "
                f"| {de} "
                f"| {cr} "
                f"| {fcf} "
                f"| {fmt_pct(c.get('net_margin'))} "
                f"| {fmt_val(c.get('beta'), 2)} "
                f"| {fmt_pct(c.get('inst_ownership'))} |"
            )

    # ── EdinetDB セクション（v2.0: financials + analysis + gov_meta）──
    if edinet_data:
        lines += ["", "### 有報・財務トレンド（EdinetDB / 金融庁EDINET）", ""]
        for ticker, tdata in edinet_data.items():
            if not tdata:
                continue
            name = tdata.get("company_name", ticker)
            analysis = tdata.get("analysis", {})
            fin      = tdata.get("financials", {})
            gov      = tdata.get("gov_meta", {})

            # 信用スコア
            if analysis:
                score_v  = analysis.get("credit_score", 0)
                rating   = analysis.get("credit_rating", "")
                r_icon   = {"excellent": "💚", "good": "🟡", "fair": "🟠", "poor": "🔴"}.get(rating, "⚪")
                lines.append(f"**{name}** (`{ticker}`) {r_icon} 信用スコア: {score_v}/100 ({rating})")

                biz = analysis.get("business_summary", "")
                if biz:
                    lines.append(f"> {biz[:200]}")

                risks = analysis.get("risk_factors", [])
                if risks:
                    lines.append("**リスク要因**:")
                    for r in risks[:5]:
                        lines.append(f"- {r}")

            # 財務トレンド（6年）
            if fin:
                years = fin.get("years", [])
                rev   = fin.get("revenue", [])
                oi    = fin.get("operating_income", [])
                rev_cagr = fin.get("revenue_cagr")
                oi_cagr  = fin.get("op_income_cagr")
                if years and rev:
                    rev_str = " → ".join(
                        f"{int(v/1e8):,}億" if v else "N/A" for v in rev[-3:]
                    )
                    oi_str = " → ".join(
                        f"{int(v/1e8):,}億" if v else "N/A" for v in oi[-3:]
                    )
                    lines.append(f"📈 売上高({years[-3] if len(years)>=3 else ''}〜): {rev_str}")
                    lines.append(f"📊 営業利益: {oi_str}")
                    cagr_parts = []
                    if rev_cagr is not None: cagr_parts.append(f"売上CAGR: {rev_cagr*100:+.1f}%/年")
                    if oi_cagr  is not None: cagr_parts.append(f"営業利益CAGR: {oi_cagr*100:+.1f}%/年")
                    if cagr_parts: lines.append("  ".join(cagr_parts))

            # 政府EDINET メタ
            if gov:
                lines.append(
                    f"📋 有報: {gov.get('submit_date','N/A')} 提出 "
                    f"| 期末: {gov.get('period_end','N/A')}"
                )
            lines.append("")

    return "\n".join(lines)


# ==============================================================
# BLOCK 6: 結果辞書ビルダー（v2.0 - EdinetDB v3.0対応）
# ==============================================================

def build_result(
    sector_code:    str,
    sector_name:    str,
    companies:      list,
    macro_signals:  list,
    cfg,
    macro_snapshot: dict = None,   # ★追加: score_explainer に渡すライブ相場値
) -> dict:
    """
#    SECTOR_AGENT_RESULT_SCHEMA に準拠した辞書を組み立てる。

#    【v2.0変更】
#    edinet_client.get_sector_data() を使用:
#      - analysis + financials + gov_meta を一括取得
#      - 日次キャッシュを利用して1日1回のみAPIを叩く
#      - 予算残がなければスキップ（エラーにしない）
    """
    # ── [S-2残] EDINET credit_score → quality補正（edinet_rotation DBから取得）────
    edinet_credit_bonus = 0.0
    if getattr(cfg, "ENABLE_EDINET_ROTATION", False):
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _erm = _imp.import_module("edinet_rotation_manager")
            _db  = _erm.load_db(cfg)
            _analysis = _db.get("analysis", {})   # {ticker: analysis_dict}

            # 業種内対象銘柄の credit_score を収集
            _ok_tickers = [c["ticker"] for c in companies if c.get("fetch_ok")]
            _credit_scores = []
            for _tk in _ok_tickers:
                _adict = _analysis.get(_tk, {})
                _cs = _adict.get("credit_score") if isinstance(_adict, dict) else None
                if _cs is not None:
                    try:
                        _credit_scores.append(float(_cs))
                    except (TypeError, ValueError):
                        pass

            if _credit_scores:
                avg_credit = sum(_credit_scores) / len(_credit_scores)
                # tier補正: credit_score は 0〜100点スケール（EdinetDB）
                if   avg_credit >= 80:  raw_bonus =  5.0
                elif avg_credit >= 60:  raw_bonus =  2.0
                elif avg_credit >= 40:  raw_bonus =  0.0
                else:                   raw_bonus = -5.0

                # ── [GAP-5] DBカバレッジによる割り引き ──────────
                # 取得済み銘柄数 / 業種全銘柄数 でカバレッジ率を算出
                _total_tickers = len(_ok_tickers)
                _coverage = (len(_credit_scores) / _total_tickers
                             if _total_tickers > 0 else 0.0)
                _cov_thresh_full  = getattr(cfg, "EDINET_COVERAGE_FULL",  0.7)
                _cov_thresh_half  = getattr(cfg, "EDINET_COVERAGE_HALF",  0.3)
                if _coverage >= _cov_thresh_full:
                    _cov_factor = 1.0    # 70%以上取得済み → フル適用
                elif _coverage >= _cov_thresh_half:
                    _cov_factor = 0.5    # 30〜70% → 半分に割り引き
                else:
                    _cov_factor = 0.0    # 30%未満 → 適用しない（データ不足）

                edinet_credit_bonus = round(raw_bonus * _cov_factor, 2)
                print(f"    📋 EDINET信用補正: avg_credit={avg_credit:.0f}/100 "
                      f"coverage={_coverage*100:.0f}% "
                      f"→ {edinet_credit_bonus:+.2f}pt"
                      f"（{len(_credit_scores)}/{_total_tickers}社）")
        except Exception as _e:
            print(f"    ⚠️  EDINET信用補正エラー（スキップ）: {str(_e)[:60]}")

    # ── [I-2] NEDO政策支援ボーナスを事前計算 ─────────────────────────
    # 処理フロー:
    #   1. ok_companies の ticker を最大5社取得（BQ負荷制限）
    #   2. get_policy_support_bonus() で採択情報を照合
    #   3. 業種内採択率（採択社数 / 全社数）でスケーリング
    #   4. 上限: max_bonus=15pt（get_policy_support_bonus 側でクリップ済み）
    # ★ try/except 厳重保護: BQ接続失敗時も 0.0 で継続しシステム全停止を防ぐ
    _policy_support_bonus = 0.0
    if getattr(cfg, "ENABLE_NEDO_POLICY_BONUS", True):
        try:
            _bq_ps    = _BQClient()
            _ok_ticks = [
                str(c.get("ticker", c.get("code", "")) or "")
                for c in companies
                if c.get("fetch_ok")
                and (c.get("ticker") or c.get("code"))
            ]
            if _ok_ticks:
                _ps_bonuses: list = []
                # 上位5社のみ照合（1セクターあたりのBQコール数を制限）
                for _tk in _ok_ticks[:5]:
                    _b = _bq_ps.get_policy_support_bonus(_tk)
                    if _b > 0:
                        _ps_bonuses.append((_tk, _b))

                if _ps_bonuses:
                    # 採択率 = 採択社数 / 全取得済み社数
                    _adoption_rate = len(_ps_bonuses) / max(len(_ok_ticks), 1)
                    _avg_bonus     = (
                        sum(b for _, b in _ps_bonuses) / len(_ps_bonuses)
                    )
                    # 業種全体への波及量 = 平均ボーナス × 採択率
                    _policy_support_bonus = round(_avg_bonus * _adoption_rate, 2)
                    _tickers_adopted = [t for t, _ in _ps_bonuses]
                    print(
                        f"    🏛️  NEDO採択企業: {_tickers_adopted} "
                        f"→ セクターボーナス +{_policy_support_bonus:.2f}pt"
                    )
        except Exception as _pse:
            print(
                f"    ⚠️  政策支援ボーナス取得エラー（スキップ）: "
                f"{str(_pse)[:80]}"
            )

# ==============================================================
    # [Wave 2 - BM Ph2] 業態別 bm_bonus 計算（BM-01商社・BM-02銀行から着手）
    # ★ try/except 厳重保護: import失敗・計算エラー時も 0.0 で継続
    # ==============================================================
    _bm_bonus = 0.0
    try:
        from business_model_normalizer import calc_bm_bonus as _calc_bm_bonus
        _bm_values: list = []
        for _co in [c for c in companies if c.get("fetch_ok")]:
            try:
                _b = _calc_bm_bonus(
                    ticker      = str(_co.get("ticker", "")),
                    data        = _co,
                    sector_code = sector_code,
                    market      = "JP",
                )
                _bm_values.append(float(_b))
            except Exception:
                pass   # 個別銘柄の失敗はスキップして継続

        if _bm_values:
            _bm_bonus = round(sum(_bm_values) / len(_bm_values), 2)
            if _bm_bonus != 0.0:
                print(
                    f"    🏦 BM Ph2 ({sector_code}): "
                    f"avg bonus={_bm_bonus:+.2f}pt "
                    f"({len(_bm_values)}社平均)"
                )
    except Exception as _bme:
        print(
            f"    ⚠️  BM Ph2 ボーナス取得エラー（スキップ）: "
            f"{str(_bme)[:60]}"
        )

    # ==============================================================
    # [Wave 2 - R-1] supply_chain_position をJSONから参照
    # ★ try/except 厳重保護: JSON未存在・import失敗時は "unknown" で継続
    # ==============================================================
    _supply_chain_pos = "unknown"
    try:
        from subtheme_loader import get_supply_chain_position as _get_scp
        _supply_chain_pos = _get_scp(
            sector_code = sector_code,
            market      = "JP",
            cfg         = cfg,
        )
        if _supply_chain_pos and _supply_chain_pos != "unknown":
            print(
                f"    🔗 supply_chain_position ({sector_code}): "
                f"{_supply_chain_pos}"
            )
    except Exception as _scpe:
        print(
            f"    ⚠️  supply_chain_position 取得エラー（スキップ）: "
            f"{str(_scpe)[:60]}"
        )

    # ==============================================================
    # [Wave 4 - E3] edinet_segment_bonus 計算
    # segment_signal.calc_sector_segment_bonus() で業種平均を計算して渡す。
    # ★ try/except 厳重保護: BQ未接続・segment_signal未配置時も 0.0 で継続
    # ==============================================================
    _segment_bonus = 0.0
    if getattr(cfg, "ENABLE_EDINET_ROTATION", False):
        try:
            import sys as _sys, importlib as _imp, pathlib as _pl
            _sys.path.insert(0, str(_pl.Path(__file__).parent))
            _ss = _imp.import_module("segment_signal")
            _segment_bonus = _ss.calc_sector_segment_bonus(
                sector_code = sector_code,
                companies   = companies,
                cfg         = cfg,
                max_tickers = 3,   # build_result 内は BQ 負荷を抑えて上位3社
            )
        except ModuleNotFoundError:
            pass   # segment_signal.py 未配置時は静かにスキップ
        except Exception as _sse:
            print(
                f"    ⚠️  セグメントボーナス取得エラー（スキップ）: "
                f"{str(_sse)[:60]}"
            )

# ==============================================================
    # [Wave 5 - E4] edinet_text_risk_bonus 計算
    # text_extractor → finbert_analyzer → text_signal の順で処理し、
    # リスク検出ボーナス（ペナルティ）を取得する。
    # $edinet\_text\_risk\_bonus \in [TEXT\_SIGNAL\_SCORE\_MIN(-10), 0]$
    # ★ try/except 厳重保護: 全モジュール未配置時も 0.0 で継続しシステム全停止を防ぐ
    # ==============================================================
    _edinet_text_risk_bonus = 0.0
    if getattr(cfg, "ENABLE_TEXT_SIGNAL", False):
        try:
            import sys as _sys_ts, importlib as _imp_ts, pathlib as _pl_ts
            _sys_ts.path.insert(0, str(_pl_ts.Path(__file__).parent))

            _te  = _imp_ts.import_module("text_extractor")
            _fa  = _imp_ts.import_module("finbert_analyzer")
            _tsi = _imp_ts.import_module("text_signal")

            _ok_tickers_ts = [
                str(c.get("ticker", c.get("code", "")) or "")
                for c in companies
                if c.get("fetch_ok") and (c.get("ticker") or c.get("code"))
            ]

            if _ok_tickers_ts:
                # バッチ抽出（50社ガードレールは batch_extract_sections 内で適用）
                _sections_map = _te.batch_extract_sections(cfg, _ok_tickers_ts)

                # セクション別感情スコアを計算
                _analysis_map: dict = {}
                for _tk_ts, _sec_dict in _sections_map.items():
                    try:
                        _a = _fa.analyze_sections(_sec_dict, nlp=None, cfg=cfg)
                        if _a:
                            _analysis_map[_tk_ts] = _a
                    except Exception as _fa_e:
                        print(f"    ⚠️  [text_signal] analyze_sections失敗 ({_tk_ts}): "
                              f"{str(_fa_e)[:60]}")

                # テキストリスクボーナスをバッチ計算
                _bonus_map = _tsi.batch_calc_text_risk_bonus(
                    cfg              = cfg,
                    analysis_results = _analysis_map,
                    prev_results     = None,          # 初回はYoY差分=0
                    sections_by_code = _sections_map,
                )

                # 業種内平均をセクターボーナスとして集約
                _bonus_values = [v for v in _bonus_map.values() if v != 0.0]
                if _bonus_values:
                    _raw_avg = sum(_bonus_values) / len(_bonus_values)
                    _score_min = getattr(cfg, "TEXT_SIGNAL_SCORE_MIN", -10.0)
                    _score_max = getattr(cfg, "TEXT_SIGNAL_SCORE_MAX",   0.0)
                    _edinet_text_risk_bonus = round(
                        max(_score_min, min(_score_max, _raw_avg)), 2
                    )
                    print(
                        f"    📝 テキストリスクボーナス ({sector_code}): "
                        f"{_edinet_text_risk_bonus:+.2f}pt "
                        f"({len(_bonus_values)}社平均)"
                    )

        except ModuleNotFoundError as _mnfe:
            print(f"    ⚠️  text_signal系モジュール未配置（スキップ）: {str(_mnfe)[:60]}")
        except Exception as _tse:
            print(f"    ⚠️  edinet_text_risk_bonus取得エラー（スキップ）: {str(_tse)[:80]}")

    # ==============================================================
    # [Wave 6 - B] cluster_phase_bonus 計算
    # cluster_phase_cache.json から sector_code に対応する
    # クラスターフェーズボーナスを取得する。
    # ★ try/except 厳重保護: cluster_engine 未実行・キャッシュ未存在時も 0.0 で継続
    # ==============================================================
    _cluster_phase_bonus = 0.0
    if getattr(cfg, "ENABLE_CLUSTER_ENGINE", True):
        try:
            from technology_cluster_engine import get_cluster_phase_bonus
            _market = "US" if str(sector_code).startswith("US_") else "JP"
            _cp_bonus, _cp_phase, _cp_cid = get_cluster_phase_bonus(
                sector_code=str(sector_code),
                market=_market,
                cfg=cfg,
            )
            _cluster_phase_bonus = float(_cp_bonus)
            if abs(_cluster_phase_bonus) >= 1.0:
                print(
                    f"    🔬 [Wave6-B] クラスターボーナス "
                    f"({sector_code}): "
                    f"{_cluster_phase_bonus:+.2f}pt "
                    f"phase={_cp_phase} cluster={_cp_cid}"
                )
        except ModuleNotFoundError:
            pass   # technology_cluster_engine.py 未配置時は静かにスキップ
        except Exception as _cpe:
            print(
                f"    ⚠️  cluster_phase_bonus取得エラー（スキップ）: "
                f"{str(_cpe)[:60]}"
            )

    # ==============================================================
    # [Wave 6 - G-X] geo_impact_bonus 計算
    # geo_signal.calc_geo_impact_bonus_jp() で業種内銘柄の地政学影響を集計。
    # ★ try/except 厳重保護: geo_signal 未配置・BQ未接続時も 0.0 で継続
    # ==============================================================
    _geo_impact_bonus = 0.0
    if getattr(cfg, "ENABLE_GEO_SIGNAL", False):
        try:
            import sys as _sys_geo, importlib as _imp_geo
            import pathlib as _pl_geo
            _sys_geo.path.insert(
                0, str(_pl_geo.Path(__file__).parent)
            )
            _gs = _imp_geo.import_module("geo_signal")

            # 業種内の ok_companies を対象に geo_impact_bonus を計算
            _ok_tickers_geo = [
                str(c.get("ticker", c.get("code", "")) or "")
                for c in companies
                if c.get("fetch_ok")
                and (c.get("ticker") or c.get("code"))
            ]
            _geo_bonuses: list[float] = []
            for _tk_geo in _ok_tickers_geo[:5]:   # 上位5社（BQ負荷制限）
                try:
                    _b = _gs.calc_geo_impact_bonus_jp(
                        ticker      = _tk_geo,
                        sector_code = sector_code,
                        cfg         = cfg,
                    )
                    _geo_bonuses.append(float(_b))
                except Exception:
                    pass

            if _geo_bonuses:
                _geo_impact_bonus = round(
                    sum(_geo_bonuses) / len(_geo_bonuses), 4
                )
                if abs(_geo_impact_bonus) >= 1.0:
                    print(
                        f"    🌐 [G-X] セクター地政学ボーナス "
                        f"({sector_code}): "
                        f"{_geo_impact_bonus:+.2f}pt "
                        f"({len(_geo_bonuses)}社平均)"
                    )

        except ModuleNotFoundError:
            pass   # geo_signal.py 未配置時は静かにスキップ
        except Exception as _gse:
            print(
                f"    ⚠️  geo_impact_bonus取得エラー（スキップ）: "
                f"{str(_gse)[:60]}"
            )

    _real_estate_bonus = 0.0
    if getattr(cfg, "ENABLE_REAL_ESTATE_SIGNAL", True):
        try:
            from real_estate_signal import calc_real_estate_bonus
            _bq_re = _BQClient()
            _values = []
            for company in [c for c in companies if c.get("fetch_ok")][:5]:
                ticker = str(company.get("ticker", company.get("code", "")) or "")
                if not ticker:
                    continue
                holdings = _bq_re.get_real_estate_holdings(ticker)
                if holdings:
                    _values.append(calc_real_estate_bonus(holdings, cfg))
            if _values:
                _real_estate_bonus = round(sum(_values) / len(_values), 4)
        except Exception as exc:
            print(f"    ⚠️  不動産ボーナス取得エラー（スキップ）: {str(exc)[:60]}")

    score, risk_level = calc_composite_score(
        companies,
        macro_signals,
        edinet_credit_bonus,
        _policy_support_bonus,        # Wave 1 - I-2: NEDO政策支援ボーナス
        _bm_bonus,                    # Wave 2 - BM Ph2: 業態別正規化ボーナス
        _segment_bonus,               # Wave 4 - E3: セグメント収益性ボーナス
        _edinet_text_risk_bonus,      # Wave 5 - E4: テキストリスクボーナス
        _geo_impact_bonus,            # Wave 6 - G-X: 地政学×権益統合シグナル
        _cluster_phase_bonus,         # Wave 6 - B: 技術クラスターフェーズボーナス
        _real_estate_bonus,
    )

    # ── EdinetDB only ───────────────────────────────────────────
    edinet_data  = {}  # {ticker: full_company_data}
    edinet_risks = {}  # {ticker: [リスク文字列リスト]} ← 後方互換用

    if getattr(cfg, "ENABLE_EDINET_BQ_ANALYSIS", True):
        try:
            ok_tickers = [c["ticker"] for c in companies if c.get("fetch_ok")]
            max_co = getattr(cfg, "EDINET_MAX_COMPANIES", 2)
            if ok_tickers:
                print(f"    📄 EDINET BQ既存データ参照中（最大 {max_co} 社）...")
                edinet_data = _BQClient().get_company_analysis_bundle(
                    ok_tickers, max_companies=max_co
                )
                print(f"    ✅ EDINET BQ: {len(edinet_data)} 社参照 | 外部API: 0回")
                for ticker, tdata in edinet_data.items():
                    analysis = tdata.get("analysis", {})
                    if analysis.get("risk_factors"):
                        edinet_risks[ticker] = analysis["risk_factors"]
        except Exception as e:
            print(f"    ⚠️  EDINET BQ参照エラー（スキップ）: {str(e)[:120]}")

    insight_text = build_insight_text(
        sector_name, companies, macro_signals, score, risk_level,
        edinet_data=edinet_data,
    )

    # insights/sectors/ に保存
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        out_path = cfg.SECTOR_DIR / f"{sector_code}_{today}.md"
        cfg.SECTOR_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(insight_text, encoding="utf-8")
    except Exception:
        pass

    # edinet_risks をフラット形式に変換
    edinet_flat = [
        {"ticker": t, "risk": r}
        for t, risks in edinet_risks.items()
        for r in risks
    ]

    result = {
        "sector_code":     sector_code,
        "sector_name_ja":  sector_name,
        "analysis_date":   datetime.now().strftime("%Y-%m-%d"),
        "composite_score": score,
        "macro_signals":   macro_signals,
        "top_companies":   companies,
        "edinet_risks":    edinet_flat,
        "edinet_data":     edinet_data,   # v2.0追加: 全EdinetDBデータ
        "arxiv_papers":    [],
        "insight_text":    insight_text,
        "risk_level":      risk_level,
        "reason_text":     "",
        # ── Wave 2 追加フィールド ──────────────────────────────
        "supply_chain_position": _supply_chain_pos,  # Wave 2 - R-1
        "bm_bonus":              _bm_bonus,           # Wave 2 - BM Ph2
        # ── Wave 4 - E3 追加フィールド ────────────────────────
        "edinet_segment_bonus":  _segment_bonus,      # Wave 4 - E3: セグメント収益性
        # ── Wave 5 - E4 追加フィールド ────────────────────────
        "edinet_text_risk_bonus": _edinet_text_risk_bonus,  # Wave 5 - E4: テキストリスク
        # ── Wave 6 - G-X 追加フィールド ───────────────────────
        "geo_impact_bonus":      _geo_impact_bonus,   # Wave 6 - G-X: 地政学×権益
        # ── Wave 6 - B 追加フィールド ────────────────────────
        "cluster_phase_bonus":   _cluster_phase_bonus, # Wave 6 - B: 技術クラスターフェーズボーナス
        "real_estate_bonus":     _real_estate_bonus,
    }

    # score_explainer 連携
    try:
        import sys, importlib
        sys.path.insert(0, str(Path(__file__).parent))
        _se = importlib.import_module("score_explainer")
        result = _se.enrich_result_with_reason(
          result,
          gov_sector_signals = None,
          macro_snapshot     = macro_snapshot,   # ← Noneから実値に変更
          score_history      = [],               # ← 新規引数（空リストで安全）
      )
    except Exception as e:
        print(f"    ⚠️  score_explainer連携エラー: {e}")

    return result


def _load_edinet_client(cfg):
    import importlib.util
    candidates = [
        Path(getattr(cfg, "DRIVE_BASE", "")) / "edinet_client.py",
        Path(__file__).parent / "edinet_client.py",
        Path("/content/edinet_client.py"),
    ]
    for path in candidates:
        if path.exists():
            spec   = importlib.util.spec_from_file_location("edinet_client", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    print("    ⚠️  edinet_client.py が見つかりません")
    return None

def _load_data_bridge(cfg=None):
    """viewer の data_bridge.py を動的 import する"""
    import importlib.util
    candidates = [
        Path(__file__).parent / "viewer" / "data_bridge.py",
        Path("/content/viewer/data_bridge.py"),
        Path("/content/data_bridge.py"),
    ]
    for path in candidates:
        if path.exists():
            spec   = importlib.util.spec_from_file_location("data_bridge", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None

def _normalize_viewer_data(ticker: str, raw: dict) -> dict:
    """
    viewer の fetch_stock_data() 戻り値を
    base_agent の fetch_financials() 戻り値形式に変換する。
    """
    return {
        # 識別
        "ticker":       ticker,
        "name":         raw.get("name", ticker),
        "fetch_ok":     True,

        # 株価
        "current_price":      raw.get("price"),
        "market_cap":         raw.get("market_cap"),
        "week52_high":        raw.get("w52_high"),
        "week52_low":         raw.get("w52_low"),
        "week52_change":      raw.get("dev_from_low"),  # 近似値

        # バリュエーション
        "per":                raw.get("per"),
        "pbr":                raw.get("pbr"),
        "psr":                raw.get("_psr"),          # compute_psr済みなら
        "eps_trailing":       None,
        "book_value":         None,

        # 利益率
        "op_margin":          None,
        "net_margin":         None,
        "gross_margin":       None,

        # 成長率
        "revenue_growth":     None,

        # 収益性
        "roe":                raw.get("roe"),
        "roa":                raw.get("roa"),

        # 財務健全性
        "free_cashflow":      raw.get("fcf"),
        "operating_cashflow": raw.get("op_cf"),
        "total_cash":         raw.get("cash"),
        "total_debt":         raw.get("total_debt"),
        "total_revenue":      raw.get("revenue"),
        "current_ratio":      raw.get("current_ratio"),
        "debt_to_equity":     raw.get("debt_to_equity"),

        # 配当
        "dividend_yield":     raw.get("div_yield"),
        "payout_ratio":       raw.get("payout_ratio"),

        # アクルーアル（viewer が計算済み）
        "accrual_ratio":      raw.get("accrual_ratio"),

        # EdinetDB拡張
        "credit_score":       raw.get("_credit_score"),
        "credit_rating":      raw.get("_credit_rating"),

        # アナリスト（viewerはAlphaVantage経由なのでNone多め）
        "rec_mean":           None,
        "rec_key":            None,
        "analyst_upside":     None,
        "analyst_count":      None,

        # その他
        "beta":               None,
        "inst_ownership":     None,
    }

def _calc_edinet_hr_bonus(securities_code: str) -> float:
    """
    EDINET 直接取得 DB から人的資本・FCF 指標を取得し、
    composite_score へのボーナス点（-5〜+8 pt）を返す。

    指標と加点ロジック:
      労働生産性（営業利益/従業員数）が業種平均を超える → +3 pt
      FCF イールド（free_cf/net_sales）が正かつ 5% 超 → +3 pt
      役員報酬/従業員平均給与比が業種平均の 2σ 超 → -5 pt（ガバナンスリスク）
      データ未取得（BQ に未格納）→ 0 pt（スキップ）
    """
    if not securities_code:
        return 0.0

    try:
        bq = _BQClient()
        fin = bq.get_financials_single(securities_code)
        if not fin:
            return 0.0  # BQ 未取得銘柄はスキップ

        bonus = 0.0

        # ---- 労働生産性ボーナス ----
        op_income   = fin.get("operating_income")
        num_emp     = fin.get("num_employees")
        if op_income is not None and num_emp and num_emp > 0:
            productivity = op_income / num_emp  # 1人あたり営業利益
            # 簡易閾値: 5百万円超（精緻化は E3 のセクター平均比較で行う）
            if productivity > 5_000_000:
                bonus += 3.0
            elif productivity < 0:
                bonus -= 2.0

        # ---- FCF イールドボーナス ----
        free_cf   = fin.get("free_cf")
        net_sales = fin.get("net_sales")
        if free_cf is not None and net_sales and net_sales > 0:
            fcf_yield = free_cf / net_sales
            if fcf_yield > 0.05:
                bonus += 3.0
            elif fcf_yield < -0.05:
                bonus -= 2.0

        # ---- 役員報酬ガバナンスペナルティ ----
        avg_salary    = fin.get("avg_salary")
        max_director  = fin.get("max_director_remuneration")
        if avg_salary and avg_salary > 0 and max_director is not None:
            ratio = max_director / avg_salary
            if ratio > 30:   # 役員報酬が従業員平均給与の30倍超
                bonus -= 5.0

        return max(-5.0, min(8.0, bonus))   # クランプ

    except Exception as e:
        # BQ 接続失敗等は既存スコアリングに影響しない
        print(f"[edinet_hr_bonus] {securities_code}: エラー ({e}), bonus=0.0 で続行")
        return 0.0

# ==============================================================
# BLOCK 7: 統合セクター分析（33 agent ファイル統合後の単一入口）
# ==============================================================

# 全業種のticker解決結果（1プロセス1回だけBQに問い合わせる）
# 業種分析を並列実行すると同時に初期化されうるため lock で保護する
# （無いと同じバルククエリが並列数だけ走る）。
_SECTOR_TICKER_CACHE: dict | None = None
_SECTOR_TICKER_LOCK = threading.Lock()


def _normalize_ticker(t: str) -> str:
    t = str(t or "").strip()
    if not t:
        return ""
    return t if t.endswith(".T") else f"{t}.T"


def resolve_sector_tickers(sector_code: str, cfg, limit: int = 5) -> list[str]:
    """
    業種別 ticker 解決（3段フォールバック）:
      1. BQ ticker_subtheme_mapping
      2. edinet_rotation meta.json（sector_code 付き）
      3. config.SECTOR_REPRESENTATIVE_TICKERS
    """
    sector_code = str(sector_code)
    limit = max(1, min(int(limit), 10))

    # 1) BQ（33業種で33本のクエリを撃たないよう、初回に全業種を1本で取る）
    global _SECTOR_TICKER_CACHE
    try:
        bq = _BQClient()
        with _SECTOR_TICKER_LOCK:
            if _SECTOR_TICKER_CACHE is None:
                _SECTOR_TICKER_CACHE = (
                    bq.get_tickers_by_sector_bulk(limit=10) or {})
            _cache = _SECTOR_TICKER_CACHE
        from_bq = list(_cache.get(sector_code, []))[:limit]
        if from_bq:
            print(f"    📋 tickers[{sector_code}] source=BQ ({len(from_bq)}件)")
            return from_bq
    except Exception as e:
        print(f"    ⚠️  tickers[{sector_code}] BQ失敗: {str(e)[:60]}")

    # 2) edinet meta.json
    try:
        import importlib
        erm = importlib.import_module("edinet_rotation_manager")
        meta = erm.load_meta(cfg) or {}
        matched = [
            _normalize_ticker(t)
            for t, info in meta.items()
            if str((info or {}).get("sector_code", "")) == sector_code
        ]
        matched = [t for t in matched if t]
        if matched:
            matched.sort(
                key=lambda t: (meta.get(t, {}).get("priority", 99), t)
            )
            print(f"    📋 tickers[{sector_code}] source=edinet_meta ({len(matched)}件)")
            return matched[:limit]
    except Exception as e:
        print(f"    ⚠️  tickers[{sector_code}] meta失敗: {str(e)[:60]}")

    # 3) config 固定リスト（移行期フォールバック）
    #   ★ 旧コードは存在しないキー SECTOR_TARGET_TICKERS を参照しており、
    #     Tier3 が常に空＝全業種「解決不可」だった（producer/consumer 不整合）。
    #     正本は SECTOR_REPRESENTATIVE_TICKERS（config.py）。
    fallback = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {}) or {}
    static = [_normalize_ticker(t) for t in fallback.get(sector_code, [])]
    static = [t for t in static if t]
    if static:
        print(f"    📋 tickers[{sector_code}] source=config ({len(static)}件)")
        return static[:limit]

    print(f"    ⚠️  tickers[{sector_code}] 解決不可")
    return []


def load_sector_note(sector_code: str, cfg) -> str:
    notes = getattr(cfg, "SECTOR_NOTES", {}) or {}
    note = notes.get(str(sector_code), "")
    if note:
        return note if note.startswith("\n") else "\n" + note
    return ""


def run_sector_analysis(
    sector_code: str,
    cfg,
    macro_snapshot: dict | None = None,
) -> dict:
    """
    33業種共通のセクター分析入口。
    旧 {code}_agent.run_analysis() の代替。
    """
    sector_code = str(sector_code)
    sector_info = getattr(cfg, "JAPAN_SECTORS", {}).get(sector_code, {})
    sector_name = sector_info.get("name_ja", sector_code)

    print(f"  🏭 [{sector_code}] {sector_name} 分析開始...")

    tickers = resolve_sector_tickers(sector_code, cfg, limit=5)
    companies = fetch_financials(tickers) if tickers else []
    macro_signals = build_macro_signals(sector_code, cfg, macro_snapshot)

    ok = sum(1 for c in companies if c.get("fetch_ok"))
    tw = sum(1 for s in macro_signals if s.get("signal") == "tailwind")
    hw = sum(1 for s in macro_signals if s.get("signal") == "headwind")
    print(f"    財務データ: {ok}/{len(tickers)} 成功 | マクロ: 追い風 {tw} / 逆風 {hw}")

    result = build_result(
        sector_code, sector_name, companies, macro_signals, cfg,
        macro_snapshot=macro_snapshot,
    )

    note = load_sector_note(sector_code, cfg)
    if note:
        result["insight_text"] += note
        result["sector_note"] = note.strip()

    print(
        f"    ✅ {sector_name}: スコア {result.get('composite_score')}点 "
        f"/ リスク {result.get('risk_level')}"
    )
    return result
