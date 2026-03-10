# ==============================================================
# sector_agents/base_agent.py  v2.0
# ==============================================================
# 【v1.1 → v2.0 変更点】
#   ① fetch_financials(): yfinance 全取得可能フィールドを追加（40+項目）
#      追加: 配当・アナリスト目標株価・FCF・D/Eレシオ・機関保有率・ベータ等
#   ② build_result(): edinet_client v3.0 の get_sector_data() に対応
#      analysis + financials + gov_meta を一括取得
#   ③ calc_composite_score(): アナリスト推奨スコアを加算（最大+5点）
#   ④ build_insight_text(): 財務トレンド・配当・アナリスト情報セクション追加
# ==============================================================

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


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
        import yfinance as yf
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
                    "dividend_yield":      _f("dividendYield"),
                    "dividend_rate":       _f("dividendRate"),
                    "payout_ratio":        _f("payoutRatio"),
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

def build_macro_signals(sector_code: str, cfg) -> list:
    signals = []
    for factor, rule in cfg.MACRO_RULES.items():
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

def calc_composite_score(companies: list, macro_signals: list) -> tuple:
    """
    財務スコア(40) + マクロスコア(30) + アナリストスコア(5) の合計。

    アナリストスコア:
      recommendationMean ≤ 2.0 (Buy相当): +5点
      recommendationMean ≤ 2.5 (Outperform相当): +3点
      analyst_upside ≥ 20%: +2点追加
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

    total = fin_score + macro_score + analyst_score

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
        f"",
        f"**更新**: {today} | **総合スコア**: {score}点 | **リスク**: {risk_icon}",
        f"",
        f"### マクロシグナル", "",
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
        acr_str = f"{acr*100:+.1f}%" if acr is not None else "N/A"
        acr_icon = "⚠️" if acr and acr > 0.05 else ("✅" if acr and acr < -0.02 else "")
        rec_str = c.get("rec_key", "N/A")
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
    sector_code:   str,
    sector_name:   str,
    companies:     list,
    macro_signals: list,
    cfg,
) -> dict:
    """
    SECTOR_AGENT_RESULT_SCHEMA に準拠した辞書を組み立てる。

    【v2.0変更】
    edinet_client.get_sector_data() を使用:
      - analysis + financials + gov_meta を一括取得
      - 日次キャッシュを利用して1日1回のみAPIを叩く
      - 予算残がなければスキップ（エラーにしない）
    """
    score, risk_level = calc_composite_score(companies, macro_signals)

    # ── EdinetDB + 政府EDINET 一括取得 ──────────────────────────
    edinet_data  = {}  # {ticker: full_company_data}
    edinet_risks = {}  # {ticker: [リスク文字列リスト]} ← 後方互換用

    db_api_key = (
        getattr(cfg, "EDINETDB_API_KEY", "")
        or getattr(cfg, "EDINET_API_KEY", "")
    )
    if getattr(cfg, "ENABLE_EDINET", False) and db_api_key:
        try:
            _edinet = _load_edinet_client(cfg)
            if _edinet:
                ok_tickers = [c["ticker"] for c in companies if c.get("fetch_ok")]
                max_co     = getattr(cfg, "EDINET_MAX_COMPANIES", 2)
                data_dir   = getattr(cfg, "DATA_DIR", None)
                cache_dir  = (Path(data_dir) / "edinet_cache") if data_dir else None
                print(f"    📄 EdinetDB 取得中（最大 {max_co} 社）...")
                # スキップ対象業種はEdinetDBを叩かない
                skip_sectors = getattr(cfg, "EDINET_SKIP_SECTORS", [])
                if sector_code in skip_sectors:
                    print(f"    ⏩ {sector_name}: EdinetDB スキップ（優先度低）")
                else:
                    result_data = _edinet.get_sector_data(ok_tickers, cfg, max_co, cache_dir)
                    edinet_data = result_data.get("ticker_data", {})
                    calls_used  = result_data.get("calls_used", 0)
                    print(f"    ✅ EdinetDB: {len(edinet_data)} 社取得 | 本日コール数: {calls_used}/80")
                    # 後方互換: risk_factors リストに変換
                    for ticker, tdata in edinet_data.items():
                        a = tdata.get("analysis", {})
                        if a and a.get("risk_factors"):
                             edinet_risks[ticker] = a["risk_factors"]
        except Exception as e:
            print(f"    ⚠️  EdinetDB 取得エラー（スキップ）: {str(e)[:80]}")

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
    }

    # score_explainer 連携
    try:
        import sys, importlib
        sys.path.insert(0, str(Path(__file__).parent.parent))
        _se = importlib.import_module("score_explainer")
        result = _se.enrich_result_with_reason(result, gov_sector_signals=None, macro_snapshot=None)
    except Exception as e:
        print(f"    ⚠️  score_explainer連携エラー: {e}")

    return result


def _load_edinet_client(cfg):
    import importlib.util, sys
    candidates = [
        Path(getattr(cfg, "DRIVE_BASE", "")) / "edinet_client.py",
        Path(__file__).parents[1] / "edinet_client.py",
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
        Path(__file__).parents[1] / "viewer" / "data_bridge.py",
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
