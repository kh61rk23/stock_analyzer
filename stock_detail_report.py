# ==============================================================
# stock_detail_report.py  v1.0  (5-1 / 拡張C)
# screener通過銘柄ごとの1ページMDレポート自動生成
# ==============================================================
# 【役割】
#   screener を通過した銘柄を対象に、財務・テクニカル・EDINET・
#   ニュースを1枚のMarkdownスコアカードに統合する。
#   これにより「スコアは高いが実態はどうか」を即座に判断できる。
#
# 【出力スコアカード構成】
#   ┌─────────────────────────────────────────┐
#   │ 銘柄名 (ticker)  [composite_score]点      │
#   │ 業種: XX / 市場: Prime                    │
#   ├─── 📊 スコアサマリー ─────────────────────┤
#   │ sector:XX value:XX quality:XX tech:XX      │
#   │ EDINET: 🟢財務優良 (+4.9pt)               │
#   ├─── 💰 財務指標 ──────────────────────────┤
#   │ PER: 15.2 / PBR: 1.8 / ROE: 18.5%         │
#   │ ROIC推定: 12.3% / FCFマージン: 8.2%       │
#   │ D/E: 0.45 / 自己資本比率: 62.3%           │
#   ├─── 📈 テクニカル ────────────────────────┤
#   │ RSI(14): 42.5 → 中立底値圏               │
#   │ MACD: クロスアップ ✅                     │
#   │ 200日MA: 上 ✅ / VWAPライン: 上 ✅        │
#   │ 52週位置: 68% / ブレイクアウト: なし       │
#   ├─── 🏢 事業概要 ──────────────────────────┤
#   │ 主要事業（yfinance longBusinessSummary）   │
#   ├─── 📋 EDINET分析 ─────────────────────────┤
#   │ 信用スコア: AA / リスク要因: [上位3件]     │
#   ├─── ⚠️ リスクフラグ ────────────────────────┤
#   │ 信用倍率: 12.4倍（過熱注意）              │
#   │ インサイダー売り: 2件（-3pt）              │
#   └─────────────────────────────────────────┘
#
# 【保存先】
#   stock_data/insights/screener/detail_YYYY-MM-DD/
#     {ticker}.md
#   stock_data/insights/screener/detail_latest/
#     {ticker}.md  （← 最新版を常に上書き）
#
# 【依存】yfinance / technical_scorer / support_resistance / config.py
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

FETCH_DELAY = 0.25
MAX_TICKERS = 30   # 1日あたり最大レポート生成数（処理時間制限）


# ==============================================================
# BLOCK 1: データ収集
# ==============================================================

def _fetch_detail_data(ticker: str) -> dict:
    """
    1銘柄の詳細データを yfinance から取得する。
    ROIC / FCF マージン / 自己資本比率 も計算する。
    """
    try:
        import yfinance as yf # type: ignore
        t    = yf.Ticker(ticker)
        info = t.info or {}

        # 基本財務
        name    = info.get("longName") or info.get("shortName") or ticker
        sector  = info.get("sector", "")
        industry= info.get("industry", "")
        per     = info.get("trailingPE")
        pbr     = info.get("priceToBook")
        roe     = info.get("returnOnEquity")
        roa     = info.get("returnOnAssets")
        mc      = info.get("marketCap", 0) or 0
        price   = info.get("regularMarketPrice") or info.get("currentPrice") or 0
        summary = info.get("longBusinessSummary", "")[:300]

        # D/E と自己資本比率
        debt_equity  = info.get("debtToEquity")
        total_assets = info.get("totalAssets")
        total_equity = info.get("bookValue")
        equity_ratio = None
        if total_assets and total_equity and float(total_assets) > 0:
            shares = info.get("sharesOutstanding") or 1
            eq = float(total_equity) * shares
            equity_ratio = round(eq / float(total_assets) * 100, 1)

        # ROIC 推定（営業利益×(1-税率) / (有利子負債+純資産)）
        ebit       = info.get("ebit")
        tax_rate   = info.get("effectiveTaxRate") or 0.30
        total_debt = info.get("totalDebt") or 0
        roic = None
        if ebit and total_equity and total_debt is not None:
            try:
                nopat   = float(ebit) * (1 - float(tax_rate))
                shares_ = info.get("sharesOutstanding") or 1
                capital = float(total_equity) * shares_ + float(total_debt)
                if capital > 0:
                    roic = round(nopat / capital * 100, 2)
            except Exception:
                pass

        # FCF マージン（営業CF - 投資CF）/ 売上高
        fcf_margin = None
        try:
            cf = t.cashflow
            revenue = info.get("totalRevenue")
            if cf is not None and not cf.empty and revenue:
                op_cf  = float(cf.loc["Operating Cash Flow"].iloc[0]) if "Operating Cash Flow" in cf.index else None
                cap_ex = float(cf.loc["Capital Expenditure"].iloc[0]) if "Capital Expenditure" in cf.index else None
                if op_cf and cap_ex:
                    fcf = op_cf + cap_ex   # capex は負の値
                    fcf_margin = round(fcf / float(revenue) * 100, 1)
        except Exception:
            pass

        # 配当
        div_yield = info.get("dividendYield") or 0
        div_rate  = info.get("dividendRate") or 0

        # アナリスト推奨
        rec = info.get("recommendationKey", "")

        return {
            "name":          name[:30],
            "sector":        sector,
            "industry":      industry,
            "price":         round(float(price), 2) if price else None,
            "market_cap_b":  round(mc / 1e8, 1),
            "per":           round(float(per), 1) if per else None,
            "pbr":           round(float(pbr), 2) if pbr else None,
            "roe":           round(float(roe) * 100, 1) if roe else None,
            "roa":           round(float(roa) * 100, 1) if roa else None,
            "roic":          roic,
            "fcf_margin":    fcf_margin,
            "debt_equity":   round(float(debt_equity), 2) if debt_equity else None,
            "equity_ratio":  equity_ratio,
            "div_yield":     round(float(div_yield) * 100, 2) if div_yield else None,
            "div_rate":      div_rate,
            "recommendation": rec,
            "summary":       summary,
        }
    except Exception:
        return {}


def _get_technical_summary(ticker: str, tech_cache: dict) -> dict:
    """technical_scorer キャッシュから指標サマリーを取得する。"""
    rec = tech_cache.get(ticker, {})
    if not rec:
        return {}
    return {
        "score":       rec.get("score"),
        "label":       rec.get("label", ""),
        "rsi":         rec.get("rsi_d"),
        "macd_crossup": (rec.get("macd") or {}).get("crossup", False),
        "above_ma200": (rec.get("ma_cross") or {}).get("above_ma200"),
        "signals":     rec.get("signals", [])[:4],
    }


def _get_sr_summary(ticker: str, sr_cache: dict) -> dict:
    """support_resistance キャッシュからサマリーを取得する。"""
    rec = sr_cache.get(ticker, {})
    if not rec:
        return {}
    return {
        "position_pct":  rec.get("position_pct"),
        "breakout":      rec.get("breakout", False),
        "near_bottom":   rec.get("near_bottom", False),
        "above_vwap":    rec.get("above_vwap"),
        "vwap_20d":      rec.get("vwap_20d"),
        "label":         rec.get("label", ""),
    }


def _get_edinet_summary(ticker: str, edinet_db: dict) -> dict:
    """EDINET蓄積データからサマリーを取得する。"""
    analysis = edinet_db.get("analysis", {}).get(ticker, {})
    if not analysis:
        return {}
    risks = analysis.get("risk_factors", [])
    return {
        "credit_score":  analysis.get("credit_score", "N/A"),
        "credit_rating": analysis.get("credit_rating", ""),
        "risk_factors":  [str(r)[:60] for r in risks[:3]],
    }


# ==============================================================
# BLOCK 2: スコアカードMarkdown生成
# ==============================================================

def _score_bar(score: float, width: int = 10) -> str:
    """スコアをプログレスバーで表示する。"""
    if score is None:
        return "N/A"
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled) + f" {score:.0f}/100"


def _icon(val, good_thresh=60, bad_thresh=40) -> str:
    if val is None:
        return "⚪"
    return "🟢" if val >= good_thresh else ("🔴" if val <= bad_thresh else "🟡")


def generate_scorecard_md(
    ticker:       str,
    candidate:    dict,
    detail:       dict,
    tech:         dict,
    sr:           dict,
    edinet:       dict,
    bonus_items:  list,   # [(label, pts), ...] 適用されたボーナス一覧
) -> str:
    """
    1銘柄の詳細スコアカードをMarkdownで生成する。
    """
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    name    = detail.get("name") or candidate.get("name", ticker)
    comp_s  = candidate.get("composite_score", 0)
    sec_s   = candidate.get("sector_score", 0)
    val_s   = candidate.get("value_score", 0)
    qual_s  = candidate.get("quality_score", 0)
    tech_s  = candidate.get("technical_score", 50)
    sec_name = candidate.get("sector_name", "")
    per     = candidate.get("per") or detail.get("per")
    pbr     = candidate.get("pbr") or detail.get("pbr")
    roe     = candidate.get("roe") or detail.get("roe")

    # ── ヘッダー ─────────────────────────────────────────
    lines = [
        f"# 📊 {name} ({ticker})",
        f"**生成日時**: {now}  |  **composite_score**: {comp_s:.1f}/100",
        f"**業種**: {sec_name}  |  **市場**: {detail.get('sector', 'JP')}",
        "",
    ]

    # ── スコアサマリー ────────────────────────────────────
    lines += [
        "## スコアサマリー",
        "",
        f"| 指標 | スコア | バー |",
        f"|------|-------|-----|",
        f"| 総合スコア | **{comp_s:.1f}** | {_score_bar(comp_s)} |",
        f"| セクター   | {sec_s:.1f} | {_score_bar(sec_s)} |",
        f"| バリュー   | {val_s:.1f} | {_score_bar(val_s)} |",
        f"| クォリティ | {qual_s:.1f} | {_score_bar(qual_s)} |",
        f"| テクニカル | {tech_s:.1f} | {_score_bar(tech_s)} |",
        "",
    ]

    # ボーナス一覧
    if bonus_items:
        lines.append("**適用ボーナス**: " + " / ".join(
            f"{label}({pts:+.1f}pt)" for label, pts in bonus_items[:6]))
        lines.append("")

    # ── 財務指標 ─────────────────────────────────────────
    per_s  = f"{per:.1f}" if per else "N/A"
    pbr_s  = f"{pbr:.2f}" if pbr else "N/A"
    roe_s  = f"{roe:.1f}%" if roe else "N/A"
    roic_s = f"{detail['roic']:.1f}%" if detail.get("roic") else "N/A"
    fcf_s  = f"{detail['fcf_margin']:.1f}%" if detail.get("fcf_margin") else "N/A"
    de_s   = f"{detail['debt_equity']:.2f}" if detail.get("debt_equity") else "N/A"
    er_s   = f"{detail['equity_ratio']:.1f}%" if detail.get("equity_ratio") else "N/A"
    mc_s   = f"{detail.get('market_cap_b',0):.0f}億円" if detail.get("market_cap_b") else "N/A"
    div_s  = f"{detail['div_yield']:.2f}%" if detail.get("div_yield") else "なし"
    rec_s  = detail.get("recommendation", "N/A")

    lines += [
        "## 💰 財務指標",
        "",
        f"| 指標 | 値 | 評価 |",
        f"|------|---|-----|",
        f"| PER | {per_s} | {_icon(100 - (per or 50) * 2, 50, 20)} |",
        f"| PBR | {pbr_s} | {_icon(100 - (pbr or 3) * 20, 50, 20)} |",
        f"| ROE | {roe_s} | {_icon((roe or 0) / 0.30 * 100)} |",
        f"| ROIC推定 | {roic_s} | {_icon((detail.get('roic') or 0) / 0.15 * 100)} |",
        f"| FCFマージン | {fcf_s} | {_icon((detail.get('fcf_margin') or 0) / 0.10 * 100)} |",
        f"| D/E比率 | {de_s} | {_icon(100 - (detail.get('debt_equity') or 1) * 30)} |",
        f"| 自己資本比率 | {er_s} | {_icon((detail.get('equity_ratio') or 50))} |",
        f"| 時価総額 | {mc_s} | ⚪ |",
        f"| 配当利回り | {div_s} | ⚪ |",
        f"| アナリスト推奨 | {rec_s} | ⚪ |",
        "",
    ]

    # ── テクニカル指標 ────────────────────────────────────
    if tech:
        rsi    = tech.get("rsi")
        rsi_s  = f"{rsi:.1f}" if rsi else "N/A"
        rsi_icon = "🔴" if rsi and rsi > 70 else ("🟢" if rsi and rsi < 30 else "🟡")
        ma_s   = "✅ MA200上" if tech.get("above_ma200") else "❌ MA200下"
        macd_s = "✅ クロスアップ" if tech.get("macd_crossup") else "➡️ —"
        pos    = sr.get("position_pct")
        pos_s  = f"{pos:.0f}%" if pos is not None else "N/A"
        vwap_s = "✅ VWAP上" if sr.get("above_vwap") else ("❌ VWAP下" if sr.get("above_vwap") is False else "—")
        bo_s   = "🚀 ブレイクアウト！" if sr.get("breakout") else ("🎯 底値圏" if sr.get("near_bottom") else "—")

        lines += [
            "## 📈 テクニカル指標",
            "",
            f"| 指標 | 値 | 状態 |",
            f"|------|---|-----|",
            f"| RSI(14) | {rsi_s} | {rsi_icon} |",
            f"| MACD | — | {macd_s} |",
            f"| 200日MA | — | {ma_s} |",
            f"| VWAP(20日) | — | {vwap_s} |",
            f"| 52週位置 | {pos_s} | {bo_s} |",
        ]
        if tech.get("signals"):
            lines.append("")
            lines.append("**シグナル**: " + " / ".join(tech["signals"][:4]))
        lines.append("")
    else:
        lines += ["## 📈 テクニカル指標", "", "データ取得中...", ""]

    # ── 事業概要 ──────────────────────────────────────────
    if detail.get("summary"):
        lines += [
            "## 🏢 事業概要",
            "",
            detail["summary"],
            "",
        ]

    # ── EDINET分析 ────────────────────────────────────────
    if edinet:
        cs = edinet.get("credit_score", "N/A")
        cr = edinet.get("credit_rating", "")
        risks = edinet.get("risk_factors", [])
        lines += [
            "## 📋 EDINET分析",
            "",
            f"**信用スコア**: {cs}  **格付け**: {cr}",
        ]
        if risks:
            lines.append("")
            lines.append("**主要リスク要因**:")
            for r in risks[:3]:
                lines.append(f"  - {r}")
        lines.append("")
    else:
        lines += ["## 📋 EDINET分析", "", "（EDINETデータ蓄積中）", ""]

    # ── フッター ─────────────────────────────────────────
    lines += [
        "---",
        f"*stock_analyzer 自動生成 — {now}*",
        f"*screener composite_score: {comp_s:.1f} / フィルター: 通過*",
    ]

    return "\n".join(lines)


# ==============================================================
# BLOCK 3: メインエントリポイント
# ==============================================================

def run_stock_detail_report(cfg, screener_result: dict, results: dict) -> dict:
    """
    screener通過銘柄ごとの詳細スコアカードMDを生成・保存する。

    stock_analyzer_main.py の screener ブロック後に呼び出す:
        _sdr = _load_local_module("stock_detail_report", ...)
        sdr_result = _sdr.run_stock_detail_report(cfg, screen_result, results)
        results["stock_detail_report"] = sdr_result

    Returns:
        {"generated": int, "paths": [str,...], "timestamp": str}
    """
    print("\n  📋 銘柄詳細レポート生成開始...")

    passed = [c for c in screener_result.get("candidates", []) if c.get("pass_filters")]
    passed = sorted(passed, key=lambda x: x.get("composite_score", 0), reverse=True)
    passed = passed[:MAX_TICKERS]

    if not passed:
        print("    ⏩ screener通過銘柄なし")
        return {"generated": 0, "paths": [], "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # キャッシュ取得
    tech_cache  = results.get("technical_scorer", {}).get("results", {})
    sr_result   = results.get("support_resistance", {}).get("results", {})
    edinet_db   = {}
    try:
        import importlib, sys
        sys.path.insert(0, str(Path(__file__).parent))
        erm = importlib.import_module("edinet_rotation_manager")
        edinet_db = erm.load_db(cfg)
    except Exception:
        pass

    # 出力ディレクトリ
    today     = datetime.now().strftime("%Y-%m-%d")
    base_dir  = Path(getattr(cfg, "INSIGHT_DIR", "stock_data/insights")) / "screener"
    dated_dir = base_dir / f"detail_{today}"
    latest_dir = base_dir / "detail_latest"
    dated_dir.mkdir(parents=True, exist_ok=True)
    latest_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    paths = []

    for c in passed:
        ticker = c.get("ticker", "")
        if not ticker:
            continue
        print(f"      {ticker} レポート生成中...", end=" ")

        detail = _fetch_detail_data(ticker)
        tech   = _get_technical_summary(ticker, tech_cache)
        sr     = _get_sr_summary(ticker, sr_result)
        edinet = _get_edinet_summary(ticker, edinet_db)

        # ボーナス一覧を収集
        bonus_items = []
        for key, label in [
            ("surprise_bonus", "決算サプライズ"),
            ("cr_bonus",       "信用倍率"),
            ("edinet_bonus",   "EDINET品質"),
            ("sr_bonus",       "SR位置"),
            ("vp_bonus",       "VP"),
        ]:
            val = c.get(key, 0) or 0
            if abs(val) >= 0.5:
                bonus_items.append((label, val))

        md_text = generate_scorecard_md(
            ticker, c, detail, tech, sr, edinet, bonus_items)

        # 保存
        fname    = f"{ticker.replace('.', '_')}.md"
        dated_p  = dated_dir  / fname
        latest_p = latest_dir / fname

        try:
            dated_p.write_text(md_text, encoding="utf-8")
            latest_p.write_text(md_text, encoding="utf-8")
            generated += 1
            paths.append(str(dated_p))
            print(f"✅ score={c.get('composite_score',0):.1f}")
        except Exception as e:
            print(f"❌ 保存失敗: {e}")

        time.sleep(FETCH_DELAY)

    print(f"    ✅ 詳細レポート生成完了: {generated}/{len(passed)}銘柄")
    return {"generated": generated, "paths": paths,
            "today_dir": str(dated_dir),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
