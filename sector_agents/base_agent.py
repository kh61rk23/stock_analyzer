# ==============================================================
# sector_agents/base_agent.py
# 全業種エージェント 共通ロジック v1.1
# ==============================================================
# 【v1.1 変更点（2-B）】
#   - build_result() に EDINET リスク取得を統合
#   - fetch_edinet_risks() を追加（edinet_client の呼び出しラッパー）
#   - build_insight_text() にリスクセクションを追加
#   ※ 各エージェント（1028_agent.py 等）は変更不要
# ==============================================================
# ==============================================================
# 【役割】
#   各業種エージェント（1028_agent.py 等）が共通で使う
#   財務データ取得・スコア計算・結果フォーマット処理をまとめる。
#
# 【依存】
#   config.py のみ（stock_analyzer_main.py は import しない）
#
# 【各エージェントからの使い方】
#   from sector_agents.base_agent import (
#       fetch_financials, build_macro_signals, build_result
#   )
# ==============================================================

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 1: yfinance 財務データ取得
# ==============================================================

def fetch_financials(tickers: list, max_retries: int = 2) -> list:
    """
    yfinance で複数銘柄の財務指標を取得する。

    取得する指標:
      - shortName       : 銘柄名
      - marketCap       : 時価総額
      - trailingPE      : 株価収益率（PER）
      - priceToBook     : 株価純資産倍率（PBR）
      - returnOnEquity  : 自己資本利益率（ROE）
      - returnOnAssets  : 総資産利益率（ROA）
      - operatingMargins: 営業利益率
      - revenueGrowth   : 売上成長率
      - currentPrice    : 現在株価
      - 52WeekChange    : 52週騰落率

    Returns:
        list of dict: 取得できた銘柄ごとの指標辞書
                      取得失敗の銘柄はスキップ（全体が止まらない設計）
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  ⚠️  yfinance が未インストールです")
        return []

    results = []

    for ticker in tickers:
        for attempt in range(1, max_retries + 1):
            try:
                info = yf.Ticker(ticker).info or {}

                # ── 各指標を安全に取得（None の場合は None のまま） ──
                def _get(key: str) -> Optional[float]:
                    v = info.get(key)
                    try:
                        return float(v) if v is not None else None
                    except (TypeError, ValueError):
                        return None

                results.append({
                    "ticker":          ticker,
                    "name":            info.get("shortName") or info.get("longName") or ticker,
                    "market_cap":      _get("marketCap"),
                    "per":             _get("trailingPE"),
                    "pbr":             _get("priceToBook"),
                    "roe":             _get("returnOnEquity"),       # 小数表記（0.12 = 12%）
                    "roa":             _get("returnOnAssets"),       # 同上
                    "op_margin":       _get("operatingMargins"),     # 同上
                    "revenue_growth":  _get("revenueGrowth"),        # 同上
                    "current_price":   _get("regularMarketPrice") or _get("currentPrice"),
                    "week52_change":   _get("52WeekChange"),         # 同上
                    "fetch_ok":        True,
                })
                break  # 成功したらリトライ不要

            except Exception as e:
                if attempt == max_retries:
                    print(f"    ⚠️  {ticker} 取得失敗: {str(e)[:60]}")
                    results.append({
                        "ticker": ticker, "name": ticker,
                        "fetch_ok": False,
                    })
                else:
                    time.sleep(2)

    return results


# ==============================================================
# BLOCK 2: 財務指標のフォーマット表示文字列
# ==============================================================

def fmt_pct(v: Optional[float], digits: int = 1) -> str:
    """小数（0.12）を「12.0%」形式の文字列に変換。None は 'N/A'"""
    if v is None:
        return "N/A"
    return f"{v * 100:.{digits}f}%"

def fmt_val(v: Optional[float], digits: int = 2) -> str:
    """数値を文字列に変換。None は 'N/A'"""
    if v is None:
        return "N/A"
    return f"{v:.{digits}f}"

def fmt_cap(v: Optional[float]) -> str:
    """時価総額を「〇兆〇千億円」形式に変換（円）"""
    if v is None:
        return "N/A"
    # yfinance は円建て銘柄でも円で返す
    if v >= 1e12:
        return f"{v/1e12:.1f}兆円"
    if v >= 1e8:
        return f"{v/1e8:.0f}億円"
    return f"{v:.0f}円"


# ==============================================================
# BLOCK 3: マクロシグナル評価（config.MACRO_RULES 参照）
# ==============================================================

def build_macro_signals(sector_code: str, cfg) -> list:
    """
    config.MACRO_RULES を参照して、指定業種に関係する
    マクロシグナルのリストを返す。

    Returns:
        list of dict: [
          {
            "factor":  "interest_rate_rise",
            "label":   "金利上昇",
            "signal":  "tailwind",         # tailwind / headwind / watch
            "rationale": "預貸金利ザヤ拡大...",
          }
        ]
    """
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
# BLOCK 4: 総合スコア計算
# ==============================================================

def calc_composite_score(
    companies:     list,
    macro_signals: list,
) -> tuple:
    """
    財務指標 × マクロシグナルから業種の総合スコアと
    リスクレベル（high / medium / low）を算出する。

    スコアリング基準:
      財務スコア（最大 40 点）:
        ROE ≥ 10% : +15点
        ROE ≥  5% : + 8点
        ROA ≥  5% : +10点
        ROA ≥  2% : + 5点
        営業利益率 ≥ 10% : +15点
        営業利益率 ≥  5% : + 8点
      マクロスコア（最大 30 点）:
        tailwind 1件 : +10点
        watch    1件 : + 0点
        headwind 1件 : -10点

    Returns:
        (score: float, risk_level: str)
    """
    fin_score   = 0.0
    macro_score = 0.0

    # 財務スコア：取得できた銘柄の平均値で評価
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

    # マクロスコア
    for s in macro_signals:
        if s["signal"] == "tailwind":
            macro_score += 10
        elif s["signal"] == "headwind":
            macro_score -= 10

    total_score = fin_score + macro_score

    # リスクレベル判定
    headwinds = sum(1 for s in macro_signals if s["signal"] == "headwind")
    if headwinds >= 2 or total_score < 10:
        risk_level = "high"
    elif headwinds == 1 or total_score < 25:
        risk_level = "medium"
    else:
        risk_level = "low"

    return round(total_score, 1), risk_level


def _safe_avg(values: list) -> Optional[float]:
    """None を除いた平均値を返す。全て None なら None を返す"""
    v = [x for x in values if x is not None]
    return sum(v) / len(v) if v else None


# ==============================================================
# BLOCK 5: インサイトテキスト生成
# ==============================================================

def build_insight_text(
    sector_name: str,
    companies:   list,
    macro_signals: list,
    score:       float,
    risk_level:  str,
    edinet_risks: Optional[dict] = None,   # 2-B 追加: {ticker: [リスク要因リスト]}
) -> str:
    """
    分析結果を Markdown 形式のインサイトテキストとして生成する。
    edinet_risks が指定されていれば「有報リスク」セクションも追記する。
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    icon_map  = {"tailwind": "💚 追い風", "headwind": "🔴 逆風", "watch": "🟡 注意"}
    risk_icon = {"high": "🔴 高", "medium": "🟡 中", "low": "💚 低"}.get(risk_level, "⚪")

    lines = [
        f"## {sector_name} 分析レポート",
        f"",
        f"**更新**: {today} | **総合スコア**: {score}点 | **リスク**: {risk_icon}",
        f"",
        f"### マクロシグナル",
        f"",
    ]

    if macro_signals:
        for s in macro_signals:
            icon = icon_map.get(s["signal"], "⚪")
            lines.append(f"- {icon} **{s['label']}**: {s['rationale']}")
    else:
        lines.append("- ⚪ 現在特筆すべきマクロシグナルなし")

    lines += ["", "### 主要企業 財務サマリー", ""]
    lines.append("| 銘柄 | ROE | ROA | 営業利益率 | PBR | 52週騰落 |")
    lines.append("|------|-----|-----|-----------|-----|---------|")

    for c in companies:
        if not c.get("fetch_ok"):
            lines.append(f"| {c['ticker']} | — | — | — | — | — |")
            continue
        lines.append(
            f"| {c['name'][:16]} "
            f"| {fmt_pct(c.get('roe'))} "
            f"| {fmt_pct(c.get('roa'))} "
            f"| {fmt_pct(c.get('op_margin'))} "
            f"| {fmt_val(c.get('pbr'))}x "
            f"| {fmt_pct(c.get('week52_change'))} |"
        )

    # ── EDINET リスクセクション（2-B 追加）──────────────────────
    if edinet_risks:
        # 企業名マッピング（ticker → 名称）
        ticker_to_name = {c["ticker"]: c.get("name", c["ticker"])
                          for c in companies if c.get("fetch_ok")}

        lines += ["", "### 有報「事業のリスク」（EDINET 自動取得）", ""]
        for ticker, risks in edinet_risks.items():
            name = ticker_to_name.get(ticker, ticker)
            lines.append(f"**{name}** (`{ticker}`)")
            for risk in risks:
                lines.append(f"- {risk}")
            lines.append("")

    return "\n".join(lines)


# ==============================================================
# BLOCK 6: 結果辞書ビルダー（SECTOR_AGENT_RESULT_SCHEMA 準拠）
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
    各エージェントの run_analysis() がこれを返す。

    【2-B 追加】
    cfg.ENABLE_EDINET が True かつ cfg.EDINET_API_KEY が設定されている場合、
    edinet_client.get_sector_risk_factors() を呼び出してリスク要因を取得し、
    insight_text と結果辞書に追記する。
    失敗しても例外を上位に伝播しない（空リストで継続）。
    """
    score, risk_level = calc_composite_score(companies, macro_signals)

    # ── EDINET リスク取得（2-B）─────────────────────────────────
    edinet_risks = {}
    if getattr(cfg, "ENABLE_EDINET", False) and getattr(cfg, "EDINET_API_KEY", ""):
        try:
            # edinet_client を動的 import（stock_analyzer/ 直下に配置されている前提）
            _edinet = _load_edinet_client(cfg)
            if _edinet:
                ok_tickers = [c["ticker"] for c in companies if c.get("fetch_ok")]
                max_co     = getattr(cfg, "EDINET_MAX_COMPANIES", 2)
                print(f"    📄 EDINET リスク取得中（上位 {max_co} 社）...")
                edinet_risks = _edinet.get_sector_risk_factors(
                    ok_tickers, cfg, max_companies=max_co
                )
        except Exception as e:
            print(f"    ⚠️  EDINET 取得エラー（スキップ）: {str(e)[:80]}")

    insight_text = build_insight_text(
        sector_name, companies, macro_signals, score, risk_level,
        edinet_risks=edinet_risks,
    )

    # 結果を insights/sectors/ に保存
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        out_path = cfg.SECTOR_DIR / f"{sector_code}_{today}.md"
        cfg.SECTOR_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(insight_text, encoding="utf-8")
    except Exception:
        pass

    # edinet_risks をフラットなリスト形式に変換して格納
    edinet_flat = [
        {"ticker": t, "risk": r}
        for t, risks in edinet_risks.items()
        for r in risks
    ]

    return {
        "sector_code":     sector_code,
        "sector_name_ja":  sector_name,
        "analysis_date":   datetime.now().strftime("%Y-%m-%d"),
        "composite_score": score,
        "macro_signals":   macro_signals,
        "top_companies":   companies,
        "edinet_risks":    edinet_flat,      # 2-B 追加
        "arxiv_papers":    [],
        "insight_text":    insight_text,
        "risk_level":      risk_level,
    }


def _load_edinet_client(cfg):
    """
    edinet_client.py を動的 import する。
    stock_analyzer/ 直下（cfg.DRIVE_BASE）に配置されていることを前提とする。
    """
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
