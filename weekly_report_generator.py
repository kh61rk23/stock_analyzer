# ==============================================================
# weekly_report_generator.py  v1.0  (Step F)
# ==============================================================
# 【概要】
#   毎週月曜に run_all_tasks() の全モジュール結果を統合し、
#   「今週の注目セクターTOP3」「リスク要因サマリー」等を含む
#   Markdown 週次レポートを自動生成して Gmail/LINE に配信する。
#
# 【入力】results dict（run_all_tasks の返り値）
#
# 【出力】
#   insights/weekly/weekly_YYYY-MM-DD.md    ← Markdownレポート
#   results["weekly_report"] に dict を格納
#
# 【メールへの反映】
#   alert_notifier.py の extract_alerts() が
#   results["weekly_report"] を参照して週次サマリーアラートを追加
#
# 【レポート構成】
#   1. グローバルマクロサマリー（IMF + 政策金利 + 米国債）
#   2. 日本株 注目業種 TOP3 / BOTTOM3
#   3. 米国セクター注目点
#   4. テーマETF ホット/クール
#   5. 個別銘柄スクリーニング結果
#   6. 事業概要ハイライト（Step E代替）
#   7. ニュース・政策注目点
#   8. 来週のアクション候補
# ==============================================================

import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: ユーティリティ
# ==============================================================

def _pct(v, digits: int = 1) -> str:
    if v is None: return "N/A"
    try:    return f"{float(v)*100:.{digits}f}%"
    except: return "N/A"

def _score(v) -> str:
    if v is None: return "N/A"
    try:    return f"{float(v):.0f}点"
    except: return "N/A"

def _arrow(v) -> str:
    """数値の変化方向を矢印で表す"""
    if v is None: return "→"
    try:
        f = float(v)
        if f > 0.02:   return "↑↑"
        elif f > 0:    return "↑"
        elif f < -0.02: return "↓↓"
        elif f < 0:    return "↓"
        else:          return "→"
    except: return "→"


# ==============================================================
# BLOCK 2: セクション生成関数（JP業種）
# ==============================================================

def _section_jp_sectors(results: dict) -> list:
    """日本株業種スコアのTOP3/BOTTOM3"""
    lines = ["## 🇯🇵 日本株 注目業種", ""]
    sector_results = results.get("sectors", {})
    if not sector_results:
        lines.append("*データなし*"); lines.append(""); return lines

    sorted_sec = sorted(
        [(k, v) for k, v in sector_results.items() if isinstance(v, dict)],
        key=lambda x: x[1].get("composite_score", 0),
        reverse=True,
    )

    # TOP3
    lines.append("### 📈 注目業種 TOP3")
    lines.append("")
    for i, (code, r) in enumerate(sorted_sec[:3], 1):
        name   = r.get("sector_name_ja", code)
        score  = r.get("composite_score", 0)
        risk   = r.get("risk_level", "")
        reason = r.get("reason_text", "")
        risk_icon = {"low": "💚", "medium": "🟡", "high": "🔴"}.get(risk, "⚪")

        lines.append(f"**{i}. {name}** — {score:.0f}点 {risk_icon}")

        # マクロシグナル要約
        signals = r.get("macro_signals", [])
        tailwinds = [s["label"] for s in signals if s.get("signal") == "tailwind"]
        headwinds = [s["label"] for s in signals if s.get("signal") == "headwind"]
        if tailwinds: lines.append(f"  💚 追い風: {' / '.join(tailwinds[:3])}")
        if headwinds: lines.append(f"  🔴 逆風: {' / '.join(headwinds[:2])}")
        if reason: lines.append(f"  💬 {reason[:80]}")

        # 代表銘柄のハイライト
        top_cos = r.get("top_companies", [])
        for c in top_cos[:2]:
            if not c.get("fetch_ok"): continue
            cname = c.get("name", "")[:14]
            per_s = f"PER:{c['per']:.1f}x" if c.get("per") else ""
            dy_s  = f"配当:{c['dividend_yield']*100:.1f}%" if c.get("dividend_yield") else ""
            up_s  = f"目標乖離:{c['analyst_upside']*100:+.0f}%" if c.get("analyst_upside") is not None else ""
            parts = [x for x in [per_s, dy_s, up_s] if x]
            if parts: lines.append(f"  📌 {cname}: {' | '.join(parts)}")
        lines.append("")

    # BOTTOM3
    lines.append("### 📉 警戒業種 BOTTOM3")
    lines.append("")
    for code, r in sorted_sec[-3:]:
        name  = r.get("sector_name_ja", code)
        score = r.get("composite_score", 0)
        signals = r.get("macro_signals", [])
        headwinds = [s["label"] for s in signals if s.get("signal") == "headwind"]
        hw_str = " / ".join(headwinds[:3]) if headwinds else "特になし"
        lines.append(f"- **{name}** — {score:.0f}点 | 逆風: {hw_str}")
    lines.append("")
    return lines


# ==============================================================
# BLOCK 3: 米国セクター
# ==============================================================

def _section_us_sectors(results: dict) -> list:
    lines = ["## 🇺🇸 米国セクター注目点", ""]
    us = results.get("us_sectors", {})
    if not us or us.get("skipped"):
        lines.append("*データなし*"); lines.append(""); return lines

    sector_list = us.get("sector_list", [])
    top3   = us.get("top3",    [])
    bottom3 = us.get("bottom3", [])

    if top3:
        lines.append(f"**週間パフォーマンス上位**: {' ＞ '.join(top3[:3])}")
    if bottom3:
        lines.append(f"**週間パフォーマンス下位**: {' ＜ '.join(bottom3[:3])}")

    # 上位セクターの詳細
    if sector_list:
        lines.append("")
        lines.append("| セクター | スコア | 週次リターン | 傾向 |")
        lines.append("|---------|-------|------------|-----|")
        for s in sector_list[:5]:
            sname  = s.get("name", s.get("ticker", ""))
            score  = s.get("composite_score", s.get("score", 0))
            ret1w  = s.get("return_1w", s.get("perf_1w"))
            trend  = s.get("trend", "")
            ret_s  = f"{ret1w:+.1f}%" if ret1w is not None else "N/A"
            lines.append(f"| {sname} | {score:.0f} | {ret_s} | {trend} |")
    lines.append("")
    return lines


# ==============================================================
# BLOCK 4: テーマETF
# ==============================================================

def _section_etf(results: dict) -> list:
    lines = ["## 🌡️  テーマETF 動向", ""]
    etf = results.get("etf_tracker", {})
    if not etf or etf.get("skipped"):
        lines.append("*データなし*"); lines.append(""); return lines

    # ★修正: thematic_etf_tracker の出力は "themes" キー / 各要素は theme_name / heat_score
    etf_list = etf.get("themes", [])
    if not etf_list:
        lines.append("*データなし*"); lines.append(""); return lines

    sorted_etf = sorted(
        [e for e in etf_list if isinstance(e, dict)],
        key=lambda x: x.get("heat_score", 0) or 0,
        reverse=True,
    )

    hot  = [e for e in sorted_etf if e.get("signal") == "hot"][:3]
    cool = [e for e in sorted_etf if e.get("signal") == "cooling"][:3]

    SIGNAL_ICONS = {"hot": "🔥", "warming": "📈", "neutral": "➡️", "cooling": "❄️"}
    if hot:
        lines.append("**🔥 ホット テーマ**")
        for e in hot:
            name  = e.get("theme_name", "")[:20]
            score = e.get("heat_score")
            etfs  = "/".join(e.get("etfs", []))
            sc_s  = f"熱量:{score:.0f}" if score is not None else ""
            lines.append(f"  🔥 {name}（{etfs}）: {sc_s}".strip())

    if cool:
        lines.append("**❄️  クール テーマ**")
        for e in cool:
            name  = e.get("theme_name", "")[:20]
            score = e.get("heat_score")
            sc_s  = f"熱量:{score:.0f}" if score is not None else ""
            lines.append(f"  ❄️  {name}: {sc_s}".strip())

    lines.append("")
    return lines


# ==============================================================
# BLOCK 5: スクリーニング結果
# ==============================================================

def _section_screener(results: dict) -> list:
    lines = ["## 🔍 個別銘柄スクリーニング", ""]
    screener = results.get("screener", {})
    if not screener:
        lines.append("*データなし*"); lines.append(""); return lines

    candidates = screener.get("candidates", [])
    passed     = [c for c in candidates if c.get("pass_filters")]
    total      = screener.get("screened_count", len(candidates))
    pass_count = screener.get("pass_count", len(passed))

    lines.append(f"**スクリーニング結果**: {total}社中 {pass_count}社 通過")
    lines.append("")

    if not passed:
        lines.append("*今週の通過銘柄なし*")
        lines.append("")
        return lines

    # ── [指示箇所] Top candidates 表示ループの直前に「戦略別セクション」を挿入 ──
    strategy_summary = results.get("strategy_summary")
    if strategy_summary:
        try:
            # モジュール読み込みの整理
            import strategy_output_schema as _sos
            from config import JAPAN_SECTORS
            
            sector_names = {k: v.get("name_ja", k) for k, v in JAPAN_SECTORS.items()}
            
            # 戦略別セクションのテキストを生成して追加
            strat_lines = _sos.format_strategy_section(
                candidates, strategy_summary, sector_names
            )
            if strat_lines:
                lines.extend(strat_lines)
                lines.append("") # セクション間の余白
        except Exception:
            pass
    # ── [D-1] 投資判断カード ───────────────────────────────────────
    candidates = results.get("screener", {}).get("candidates", [])
    buy_cards  = [
        c for c in candidates
        if c.get("investment_card", {}).get("verdict") == "BUY"
           and c.get("pass_filters")
    ]
    if buy_cards:
        lines += ["### 🃏 投資判断カード（D-1）", ""]
        for c in buy_cards[:6]:
            card   = c.get("investment_card", {})
            ticker = c.get("ticker", "")
            score  = c.get("composite_score", 0)
            lines.append(f"#### {ticker}（score={score:.0f}）")
            lines.append("")
            for summary_line in card.get("summary", "").split("\n"):
                if summary_line.strip():
                    lines.append(f"> {summary_line}")
            lines.append("")

    # ── ここから Top candidates 表示ループ ──
    lines.append("| 銘柄 | 業種 | スコア | PER | PBR | ROE | 配当 | 推奨 |")
    lines.append("|------|------|-------|-----|-----|-----|-----|-----|")
    
    # スコア順に上位10件を表示
    top_candidates = sorted(passed, key=lambda x: x.get("composite_score", 0), reverse=True)[:10]
    for c in top_candidates:
        name  = (c.get("name", c.get("ticker", "")))[:12]
        sec   = c.get("sector_name", "")[:8]
        score = c.get("composite_score", 0)
        per_s = f"{c['per']:.1f}x"  if c.get("per")  else "N/A"
        pbr_s = f"{c['pbr']:.2f}x"  if c.get("pbr")  else "N/A"
        roe_s = _pct(c.get("roe"))
        dy_s  = _pct(c.get("dividend_yield"))
        rec   = c.get("rec_key", "N/A")
        lines.append(f"| {name} | {sec} | {score:.0f} | {per_s} | {pbr_s} | {roe_s} | {dy_s} | {rec} |")

    lines.append("")
    return lines
        
def _section_strategy_breakdown(results: dict) -> list:
    """[GAP-4] 戦略別スクリーニング結果セクション"""
    screener = results.get("screener", {})
    if not screener or screener.get("skipped"):
        return []

    lines = ["## 🎯 戦略別スクリーニング結果（GAP-4）", ""]

    def _fmt_candidates(cands: list, cols: list) -> list:
        if not cands:
            return ["*該当なし*", ""]
        header = "| " + " | ".join(c[0] for c in cols) + " |"
        sep    = "|" + "|".join("------" for _ in cols) + "|"
        rows   = [header, sep]
        for c in cands[:8]:
            row = "| " + " | ".join(
                str(c.get(col[1], "-") or "-")[:col[2]] if len(col) > 2
                else str(c.get(col[1], "-") or "-")
                for col in cols
            ) + " |"
            rows.append(row)
        rows.append("")
        return rows

    # コア戦略
    core_top = screener.get("core_top", [])
    lines.append("### 🔵 コア戦略（低回転ファクター投資）")
    lines.append("")
    lines += _fmt_candidates(core_top, [
        ("銘柄", "name", 14), ("ticker", "ticker", 8),
        ("sector", "sector_name", 8), ("score", "composite_score", 5),
        ("確信Lv", "conviction_level", 4),
    ])

    # サテライト1: イベントドリブン
    ev_top = screener.get("satellite_event_top", [])
    lines.append("### 🎯 サテライト1: イベントドリブン（決算サプライズ高確率）")
    lines.append("")
    lines += _fmt_candidates(ev_top, [
        ("銘柄", "name", 14), ("ticker", "ticker", 8),
        ("保守度", "conservatism_score", 5), ("加速", "acc_bonus", 4),
        ("score", "composite_score", 5),
    ])

    # サテライト2: 小型株・特殊状況
    sc_top = screener.get("satellite_smallcap_top", [])
    lines.append("### 🔶 サテライト2: 小型株・特殊状況（アクティビスト候補）")
    lines.append("")
    lines += _fmt_candidates(sc_top, [
        ("銘柄", "name", 14), ("ticker", "ticker", 8),
        ("活動Sc", "activist_score", 5), ("NetCash", "net_cash_ratio", 5),
        ("時価総額億", "market_cap_b", 6),
    ])

    # サテライト3: 情報コスト優位
    info_top = screener.get("satellite_info_top", [])
    lines.append("### 🔍 サテライト3: 情報コスト優位（未発見銘柄）")
    lines.append("")
    lines += _fmt_candidates(info_top, [
        ("銘柄", "name", 14), ("ticker", "ticker", 8),
        ("未発見Sc", "discovery_score", 5), ("アナリスト", "analyst_count", 5),
        ("score", "composite_score", 5),
    ])

    return lines

def _section_earnings_watchlist(results: dict) -> list:
    """[I-2] 今週の注目決算銘柄（保守度スコア × 加速スコア上位）"""
    screener = results.get("screener", {})
    candidates = screener.get("candidates", [])

    # 保守度高 × 加速スコア高の候補を抽出
    watchlist = [
        c for c in candidates
        if (c.get("conservatism_score", 0) or 0) >= 60
        and (c.get("acc_bonus", 0) or 0) >= 4.0
    ]
    watchlist.sort(
        key=lambda x: (x.get("conservatism_score", 0) or 0)
                    + (x.get("acc_bonus", 0) or 0) * 2,
        reverse=True,
    )

    if not watchlist:
        return []

    lines = ["## 🎯 今週の注目決算候補（I-2 保守度×加速スコア）", ""]
    lines.append("| 銘柄 | 保守度 | 加速 | 複合フラグ | composite |")
    lines.append("|------|-------|------|----------|---------|")
    for c in watchlist[:8]:
        flag = "🎯高確率" if c.get("is_high_prob_surprise") else ""
        lines.append(
            f"| {c.get('name','')[:14]}（{c['ticker']}）"
            f" | {c.get('conservatism_score', 0):.0f}pt"
            f" | {c.get('acc_bonus', 0):+.0f}pt"
            f" | {flag}"
            f" | {c.get('composite_score', 0):.0f} |"
        )
    lines.append("")
    return lines

def _section_midterm_tracker(results: dict) -> list:
    """[I-5] 中期経営計画達成状況セクション"""
    mt_result = results.get("midterm_tracker", {})
    if not mt_result or mt_result.get("total", 0) == 0:
        return []
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, __file__.rsplit("/", 1)[0])
        _mt = _imp.import_module("midterm_plan_tracker")
        return _mt.format_midterm_section(mt_result.get("results", {}))
    except Exception:
        return []
    
# ==============================================================
# BLOCK 6: 事業概要ハイライト（Step E代替）
# ==============================================================

def _section_biz_summaries(results: dict) -> list:
    lines = ["## 📋 注目銘柄 事業概要", ""]
    biz = results.get("biz_summaries", {})
    summaries = biz.get("ticker_summaries", {}) if isinstance(biz, dict) else {}

    if not summaries:
        lines.append("*データなし（ENABLE_BIZ_SUMMARY=False or DeepL未設定）*")
        lines.append("")
        return lines

    for ticker, d in list(summaries.items())[:6]:
        name = d.get("name", ticker)
        core = d.get("core_summary", "")
        if core:
            translated = d.get("translated", False)
            src_icon   = "🌐" if translated else "🇺🇸"
            lines.append(f"**{name}** (`{ticker}`) {src_icon}")
            lines.append(f"> {core}")
            lines.append("")

    return lines


# ==============================================================
# BLOCK 7: グローバルマクロ
# ==============================================================

def _section_macro(results: dict) -> list:
    lines = ["## 🌍 グローバルマクロ サマリー", ""]

    # 政策金利
    policy = results.get("policy", {})
    if isinstance(policy, dict):
        rates = policy.get("current_rates", {})
        if rates:
            lines.append("**主要中銀 政策金利**")
            for bank, info in rates.items():
                if isinstance(info, dict):
                    rate = info.get("rate", info.get("current", "N/A"))
                    chg  = info.get("change_pt", 0)
                    chg_s = f"({chg:+.3f}%pt)" if chg else ""
                    lines.append(f"  {bank}: {rate}% {chg_s}")
            lines.append("")

    # マクロシグナル（macro結果から）
    # BATCH-A修正後: macro_signals は results["macro_signals"] に格納される
    macro_signals_list = results.get("macro_signals", [])
    if macro_signals_list:
        headwinds = [s for s in macro_signals_list if s.get("signal") == "headwind"]
        tailwinds = [s for s in macro_signals_list if s.get("signal") == "tailwind"]
        if tailwinds:
            lines.append(f"💚 **追い風シグナル**: {' / '.join(s.get('label','') for s in tailwinds[:4])}")
        if headwinds:
            lines.append(f"🔴 **逆風シグナル**: {' / '.join(s.get('label','') for s in headwinds[:4])}")
        lines.append("")

    # IMF（imfキーがあれば）
    imf = results.get("imf")
    if isinstance(imf, dict) and not imf.get("skipped"):
        imf_signals = imf.get("signals", [])
        if imf_signals:
            lines.append("**IMF グローバルマクロ**")
            for s in imf_signals[:3]:
                label  = s.get("label", "")
                signal = s.get("signal", "")
                icon   = {"tailwind": "💚", "headwind": "🔴", "watch": "🟡"}.get(signal, "⚪")
                lines.append(f"  {icon} {label}")
            lines.append("")

    return lines

def _section_us_macro(results: dict) -> list:
    """[Phase US-2] 米国マクロ拡張指標セクション"""
    um = results.get("us_macro", {})
    if not um or um.get("skipped") or not um.get("indicators"):
        return []

    indicators = um.get("indicators", {})
    score      = um.get("composite_us_macro_score", 50)

    lines = ["## 🇺🇸 米国マクロ拡張指標（Phase US-2）", ""]
    icon  = "🟢" if score >= 65 else ("🔴" if score < 40 else "🟡")
    lines.append(f"**総合スコア**: {icon} {score:.0f}/100")
    lines.append("")
    lines.append("| 指標 | 値 | シグナル | 補足 |")
    lines.append("|------|-----|---------|------|")

    INDICATOR_LABELS = {
        "payems":   "非農業部門雇用",
        "unrate":   "失業率",
        "cpi":      "CPI前年比",
        "pce":      "PCE価格指数",
        "indpro":   "鉱工業生産",
        "houst":    "住宅着工",
        "umcsent":  "消費者信頼感",
        "hy_spread":"HYスプレッド",
        "icsa":     "失業保険申請",
        "lei":      "景気先行指数",
        "m2sl":     "M2マネーサプライ",
        "dgorder":  "耐久財受注",
        "rsxfs":    "小売売上（除自動車）",
    }
    SIGNAL_ICONS = {"tailwind": "📈", "headwind": "📉", "neutral": "➡️"}

    for key, label in INDICATOR_LABELS.items():
        ind = indicators.get(key)
        if not ind:
            continue
        val     = ind.get("value")
        yoy     = ind.get("yoy_pct") or ind.get("yoy")
        mom     = ind.get("mom_pct") or ind.get("mom_change")
        signal  = ind.get("signal", "neutral")
        icon_s  = SIGNAL_ICONS.get(signal, "➡️")
        unit    = ind.get("unit", "")
        note    = f"YoY:{yoy:+.1f}%" if yoy is not None else (f"MoM:{mom:+.1f}" if mom is not None else "")
        lines.append(
            f"| {label} | {val} {unit[:3]} | {icon_s} {signal} | {note} |"
            if val else f"| {label} | N/A | - | - |"
        )

    # 国債残高
    td = um.get("treasury_debt")
    if td:
        lines.append(f"| 米国国債残高 | ${td.get('total_debt_trillion','?')}兆ドル | ➡️ | {td.get('as_of_date','')} |")

    lines.append("")
    return lines

# ==============================================================
# BLOCK 8: ニュースハイライト
# ==============================================================

def _section_news(results: dict) -> list:
    lines = ["## 📰 今週の注目ニュース", ""]
    news = results.get("news", {})
    if not news or not isinstance(news, dict):
        lines.append("*データなし*"); lines.append(""); return lines

    top_articles = news.get("top_articles", [])
    for a in top_articles[:5]:
        title  = a.get("title", "")[:70]
        source = a.get("source", "")
        date_s = a.get("published", "")[:10] if a.get("published") else ""
        lines.append(f"- [{source}] {title} {date_s}")

    lines.append("")
    return lines

def _section_policy_radar(results: dict) -> list:
    """政策レーダー：今後30日のイベント + 業種別政策シグナル"""
    pr = results.get("policy_radar", {})
    if not pr or pr.get("skipped") or pr.get("total_signals", 0) == 0:
        return []

    lines = ["## 🏛️ 政策レーダー（政府・官庁先読みシグナル）", ""]

    # イベントカレンダー
    events = pr.get("events_30d", [])
    if events:
        lines.append("### 📅 今後30日の政策イベント")
        lines.append("")
        for ev in events[:5]:
            boost_icon = "⚡" if ev.get("boost_active") else "📌"
            lines.append(
                f"- {boost_icon} **{ev['name']}** "
                f"（{ev['date_estimate']} / 残{ev['days_until']}日）"
                + (f" → 恩恵業種: {', '.join(ev.get('sectors',[])[:3])}" if ev.get("boost_active") else "")
            )
        lines.append("")

    # 高信頼度シグナル上位
    all_sigs = pr.get("signals", [])
    top_sigs = sorted(all_sigs, key=lambda x: abs(x.get("pts_adj", 0)), reverse=True)[:6]
    if top_sigs:
        lines.append("### 📊 高信頼度政策シグナル")
        lines.append("")
        lines.append("| キーワード | ソース | 信頼度 | 加算 | 時間軸 |")
        lines.append("|----------|-------|-------|------|-------|")
        for s in top_sigs:
            pts = s.get("pts_adj", 0)
            icon = "📈" if pts > 0 else "📉"
            lines.append(
                f"| {icon} {s.get('keyword','')} "
                f"| {s.get('source','')[:8]} "
                f"| {s.get('credibility',0):.2f} "
                f"| {pts:+.1f}pt "
                f"| {s.get('horizon_months','?')}ヶ月 |"
            )
        lines.append("")

    return lines

def _section_finbert_sentiment(results: dict) -> list:
    """[RESEARCH-5] FinBERT業種別感情分析セクション"""
    sentiment = results.get("sentiment", {})
    if not sentiment or sentiment.get("skipped") or not sentiment.get("by_sector"):
        return []

    from datetime import date
    # 月初のみ詳細版
    is_monthly = (date.today().day <= 7)
    if not is_monthly:
        return []

    by_sector = sentiment.get("by_sector", {})
    if not by_sector:
        return []

    cfg_sectors = results.get("_config_sectors", {})  # Japan_sectors の参照
    lines = ["## 🧠 FinBERT ニュース感情分析（月次）", ""]
    lines.append(f"**分析対象**: {sentiment.get('articles_scored',0)}件 "
                 f"/ **モデル**: {sentiment.get('model_used','')[:30]}")
    lines.append("")

    # スコア降順でソート
    sorted_sectors = sorted(
        by_sector.items(),
        key=lambda x: x[1].get("avg_sentiment", 0.5), reverse=True
    )

    lines.append("| 業種 | 感情スコア | Pos | Neg | トレンド |")
    lines.append("|------|-----------|-----|-----|---------|")
    for code, d in sorted_sectors[:10]:
        trend_icon = {"improving": "📈", "deteriorating": "📉", "stable": "➡️"}.get(
            d.get("sentiment_trend", "stable"), "➡️"
        )
        lines.append(
            f"| [{code}] | {d.get('avg_sentiment', 0.5):.2f} "
            f"| {d.get('positive_count', 0)} "
            f"| {d.get('negative_count', 0)} "
            f"| {trend_icon} {d.get('sentiment_trend', 'stable')} |"
        )
    lines.append("")
    return lines

# [n-n] ポジションサイジングセクション
def _section_position_sizer(results: dict) -> list:
    ps = results.get("position_sizer", {})
    if not ps or ps.get("skipped"):
        return []
    text = ps.get("summary_text", "")
    return text.split("\n") if text else []

# [n-n] ストレステストセクション
def _section_stress_test(results: dict) -> list:
    st = results.get("stress_test", {})
    if not st or st.get("skipped"):
        return []
    text = st.get("summary_text", "")
    return text.split("\n") if text else []

# [2-3] パフォーマンストラッカーセクション
def _section_performance(results: dict) -> list:
    perf = results.get("performance", {})
    if not perf or perf.get("skipped"):
        return []
    text = perf.get("summary_text", "")
    return text.split("\n") if text else []

# [n-n] 市場幅指標セクション
def _section_market_breadth(results: dict) -> list:
    mb = results.get("market_breadth", {})
    if not mb:
        return []
    text = mb.get("summary_text", "")
    return text.split("\n") if text else []

# [n-n] 分割カレンダーセクション
def _section_dividend_calendar(results: dict) -> list:
    dc = results.get("dividend_calendar", {})
    if not dc:
        return []
    text = dc.get("alert_text", "")
    return text.split("\n") if text else []

# [n-n] 季節アノマリーセクション
def _section_anomaly_season(results: dict) -> list:
    a = results.get("anomaly_season", {})
    if not a:
        return []
    text = a.get("calendar_md", "")
    return text.split("\n") if text else []

# [n-n] オプション市場センチメントセクション
def _section_options_monitor(results: dict) -> list:
    om = results.get("options_monitor", {})
    if not om:
        return []
    text = om.get("summary_text", "")
    return text.split("\n") if text else []
# [n-n] SR/VP サマリーセクション
def _section_sr_vp(results: dict) -> list:
    lines = []
    sr = results.get("support_resistance", {})
    vp = results.get("volume_profile", {})

    breakouts = sr.get("top_breakout", [])
    if breakouts:
        lines.append("## 🚀 ブレイクアウト候補（SR分析）")
        for b in breakouts[:5]:
            lines.append(
                f"  {b.get('ticker','?')}: 位置{b.get('position_pct',0):.0f}% "
                f"{b.get('label','')} bonus={b.get('sr_bonus',0):+.1f}pt"
            )
        lines.append("")

    poc_cheap = vp.get("top_poc_cheap", [])
    if poc_cheap:
        lines.append("## 📊 VP割安候補（出来高プロファイル）")
        for p in poc_cheap[:5]:
            lines.append(
                f"  {p.get('ticker','?')}: POC乖離{p.get('poc_gap_pct',0):+.1f}% "
                f"zone={p.get('current_zone','?')} {p.get('label','')}"
            )
        lines.append("")
    return lines   # ★追加

def _section_jp_us_bridge(results: dict) -> list:
    bridge = results.get("jp_us_bridge", {})
    if not bridge or bridge.get("skipped"):
        return []
    return bridge.get("summary_text", "").split("\n")

def _section_rule_optimizer(results: dict, cfg=None) -> list:
    lines = []
    
    # 1. Rule Optimizer セクションの処理
    ro = results.get("rule_optimizer", {})
    if ro and ro.get("updated"):
        report_text = ro.get("report_md", "")
        if report_text:
            lines.extend(report_text.split("\n"))

    # 2. 監査ログのサマリーを追記（セクションの成否に関わらず判定）
    audit = results.get("learning_audit")
    if not audit and cfg:
        # resultsにない場合は最新ログをロード（_alは定義済みと想定）
        try:
            audit = _al.load_latest_audit(cfg)
        except NameError:
            pass # _alが未定義の場合の安全策

    if audit:
        try:
            # 動的インポートは整理し、フォーマット処理を実行
            from . import learning_audit_log as _al_mod
            audit_text = _al_mod.format_audit_for_report(audit)
            if audit_text:
                if lines: lines.append("") # 既存行があれば改行を挟む
                lines.append(audit_text)
        except (ImportError, Exception):
            pass

    return lines

def _section_alpha_attribution(results: dict) -> list:
    """[GAP-8] シグナル別アルファ帰属分析セクション"""
    aa_res = results.get("alpha_attribution", {})
    attribution = aa_res.get("attribution", {})
    if not attribution or not attribution.get("signals"):
        return []

    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, __file__.rsplit("/", 1)[0])
        _aa = _imp.import_module("alpha_attributer")
        # cfg が必要なため results から config を取得
        # フォールバック: 既存のattribution結果を直接フォーマット
        lines = _aa.format_attribution_section.__doc__ and [] or []

        lines = ["## 📐 シグナル別アルファ帰属分析（GAP-8）", ""]
        period = attribution.get("period_weeks", 0)
        lines.append(f"**分析期間**: 直近{period}週")
        lines.append("")

        top = attribution.get("top_signals", [])
        if top:
            lines.append("### ✅ 有効シグナル（IC上位）")
            lines.append("")
            lines.append("| シグナル | IC | 発動時α | 勝率 |")
            lines.append("|---------|-----|--------|------|")
            for sig_name in top[:5]:
                d = attribution.get("signals", {}).get(sig_name, {})
                ic    = d.get("ic")
                alpha = d.get("avg_alpha_when_fired_pct")
                wr    = d.get("win_rate_when_fired")
                if ic is not None:
                    lines.append(
                        f"| {sig_name[:22]} | {ic:.3f} "
                        f"| {alpha:+.2f}% " if alpha else f"| {sig_name[:22]} | {ic:.3f} | N/A "
                        f"| {wr*100:.0f}% |" if wr else "| N/A |"
                    )
            lines.append("")

        weak = attribution.get("weak_signals", [])
        if weak:
            lines.append("### ⚠️ 要見直しシグナル（IC < 0）")
            lines.append("")
            for sig_name in weak[:3]:
                d = attribution.get("signals", {}).get(sig_name, {})
                lines.append(f"- **{sig_name}**: IC={d.get('ic', 'N/A')}")
            lines.append("")

        return lines

    except Exception:
        return []
    
def _section_backtest(results: dict) -> list:
    bt = results.get("backtest", {})
    if not bt or bt.get("skipped"):
        return []
    return bt.get("report_md", "").split("\n")

#推論エンジン — 今週の投資判断サマリー（最重要セクション）
def _section_inference(results: dict) -> list:
    ie = results.get("inference", {})
    if not ie:
        return []
    text = ie.get("summary_md", "")
    return text.split("\n") if text else []

#統合シグナル — 業種別スコア表
def _section_signal_aggregator(results: dict) -> list:
    sa = results.get("signal_aggregator", {})
    if not sa or sa.get("skipped"):
        return []
    text = sa.get("summary_text", "")
    return text.split("\n") if text else []

def _section_comps(results: dict) -> list:
    ca = results.get("comps_analyzer", {})
    top = ca.get("top_cheap", [])
    if not top:
        return []
    lines = ["## 📊 同業種内割安候補（comps分析）", ""]
    lines += ["| 銘柄 | compsスコア | ボーナス | 評価 |",
              "|-----|-----------|---------|-----|"]
    for d in top[:10]:
        lines.append(
            f"| {d.get('ticker','?')} | {d.get('comps_score',50):.0f} "
            f"| {d.get('comps_bonus',0):+.1f}pt | {d.get('label','')} |")
    return lines

#    自己検証レポート — llm_rule_generator 連携の核心データ
def _section_self_evaluator(results: dict) -> list:
    se = results.get("self_evaluator", {})
    if not se or se.get("skipped"):
        return []
    return se.get("report_md", "").split("\n")

#    screenerセクターヒートマップ
def _section_screener_heatmap(results: dict) -> list:
    ssh = results.get("screener_heatmap", {})
    if not ssh or ssh.get("skipped"):
        return []
    return ssh.get("heatmap_md", "").split("\n")

def _section_portfolio_optimizer(results: dict) -> list:
    po = results.get("portfolio_optimizer", {})
    if not po or po.get("skipped"):
        return []
    return po.get("report_md", "").split("\n")

def _section_dcf(results: dict) -> list:
    dcf = results.get("dcf_valuation", {})
    top = dcf.get("top_undervalued", [])
    if not top:
        return []
    lines = ["## 📐 DCF割安候補（理論株価ベース）", ""]
    lines += ["| 銘柄 | 理論株価 | 現在株価 | 割引率 | ボーナス |",
              "|-----|--------|--------|-------|-------|"]
    for d in top[:5]:
        intr = d.get("dcf",{}).get("intrinsic_per_share",0)
        curr = d.get("inputs",{}).get("current_price",0)
        disc = d.get("discount_pct",0)
        bonus = d.get("dcf_bonus",0)
        lines.append(f"| {d.get('name','')[:10]} "
                     f"| {intr:,.0f}円 | {curr:,.0f}円 "
                     f"| {disc:+.1f}% | {bonus:+.0f}pt |")
    return lines
# ==============================================================
# BLOCK 9: アクション候補（来週の投資判断ガイド）
# ==============================================================
def _section_scenario(results: dict) -> list:
    """シナリオ分析セクションを生成する。"""
    sa = results.get("scenario_analysis", {})
    if not sa:
        return []
    text = sa.get("summary_text", "")
    return text.split("\n") if text else []

def _section_longterm_scenario(results: dict) -> list:
    """[R-5] 10年シナリオ × 業種評価マトリクス（月初のみ詳細版）"""
    ls = results.get("longterm_scenario", {})
    if not ls or ls.get("skipped"):
        return []

    from datetime import date
    is_monthly = (date.today().day <= 7)

    # summary_text は既に整形済み
    summary = ls.get("summary_text", "")
    if not summary:
        return []

    lines = summary.split("\n")

    # 月初以外は省略版（先頭20行のみ）
    if not is_monthly:
        lines = lines[:20] + ["", "*(詳細は月初レポートを参照)*", ""]

    return lines

def _section_action_guide(results: dict) -> list:
    lines = ["## 🎯 来週のアクション候補", ""]

    sector_results = results.get("sectors", {})
    screener       = results.get("screener", {})

    # エントリー検討: スコア高 × screener通過
    passed = [c for c in screener.get("candidates", []) if c.get("pass_filters")]
    top_passed = sorted(passed, key=lambda x: x.get("composite_score", 0), reverse=True)[:5]

    if top_passed:
        lines.append("### 💡 エントリー検討候補")
        lines.append("")
        for c in top_passed:
            name   = c.get("name", c.get("ticker", ""))[:16]
            score  = c.get("composite_score", 0)
            reason_parts = []
            if c.get("per") and c["per"] < 20:        reason_parts.append(f"PER割安({c['per']:.1f}x)")
            if c.get("roe") and c["roe"] >= 0.10:     reason_parts.append(f"ROE高({c['roe']*100:.0f}%)")
            if c.get("analyst_upside") and c["analyst_upside"] >= 0.15:
                reason_parts.append(f"上昇余地({c['analyst_upside']*100:+.0f}%)")
            if c.get("dividend_yield") and c["dividend_yield"] >= 0.03:
                reason_parts.append(f"高配当({c['dividend_yield']*100:.1f}%)")
            reason = " / ".join(reason_parts) if reason_parts else "スクリーニング通過"
            lines.append(f"- **{name}** スコア{score:.0f}点: {reason}")
        lines.append("")

    # 警戒: スコア低い業種
    sorted_sec = sorted(
        [(k, v) for k, v in sector_results.items() if isinstance(v, dict)],
        key=lambda x: x[1].get("composite_score", 0),
    )
    if sorted_sec:
        lines.append("### ⚠️  警戒・ポジション縮小候補業種")
        lines.append("")
        for code, r in sorted_sec[:3]:
            name  = r.get("sector_name_ja", code)
            score = r.get("composite_score", 0)
            signals = r.get("macro_signals", [])
            hws = [s["label"] for s in signals if s.get("signal") == "headwind"]
            hw_s = " / ".join(hws[:2]) if hws else "スコア低"
            lines.append(f"- **{name}** スコア{score:.0f}点: 逆風 {hw_s}")
        lines.append("")

    return lines

def _section_risk_constraints(results: dict) -> list:
    """[C-1/C-2] VaR制約・相関制約の適用状況セクション"""
    var_c  = results.get("var_constraint", {})
    corr_c = results.get("corr_constraint", {})

    if not var_c and not corr_c:
        return []

    lines = ["## 🛡️ リスク制約フィルター（C-1/C-2）", ""]

    # C-1: VaR制約
    if var_c:
        var_pct = var_c.get("var_pct")
        thr     = var_c.get("threshold_pct", 3.0)
        constrained = var_c.get("constrained", False)
        icon = "🔴" if constrained else "🟢"
        lines.append(f"### {icon} C-1: VaR制約")
        lines.append("")
        if var_pct is not None:
            lines.append(f"- 現在のVaR(95%/1日): **{var_pct:.2f}%** "
                         f"（閾値: {thr:.1f}%）")
        if constrained:
            lines.append(f"- **制約発動**: Buy候補を上位{var_c.get('max_buy_count',2)}銘柄に制限")
            lines.append(f"- 理由: {var_c.get('reason', '')}")
        else:
            lines.append("- 制約なし（VaR正常水準）")
        lines.append("")

    # C-2: 相関制約
    if corr_c and corr_c.get("constrained"):
        clusters = corr_c.get("clusters_applied", [])
        excluded = corr_c.get("excluded_tickers", [])
        lines.append("### 🔗 C-2: 相関制約（重複業種除外）")
        lines.append("")
        lines.append(f"- 適用クラスタ数: {len(clusters)} / 除外銘柄: {len(excluded)}社")
        for cl in clusters[:4]:
            lines.append(
                f"  - **{cl['name']}**: "
                f"採用={cl['selected']} / 除外={cl['excluded']}"
            )
        lines.append("")

    return lines

# ==============================================================
# BLOCK 10: メインレポート生成
# ==============================================================

def generate_weekly_report(cfg, results: dict) -> dict:
    """
    全セクションを結合して週次レポートを生成する。

    Returns:
        dict:
          {
            "report_md":   str,   # Markdown全文
            "report_path": str,   # 保存先パス
            "summary":     dict,  # extract_alerts() 用サマリー
            "skipped":     bool,
          }
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    week_num  = datetime.now().isocalendar()[1]
    dow_names = ["月", "火", "水", "木", "金", "土", "日"]
    dow       = dow_names[datetime.now().weekday()]

    print(f"\n  📊 週次レポート生成中 ({today} {dow}曜)...")

    # 全セクションを構築
    sections = []
    # ★修正: タイトルを先頭、推論・signal_aggregatorをその直後に配置
    sections.append([
        f"# 📊 週次投資判断レポート",
        f"**{today}（第{week_num}週）** — stock_analyzer 自動生成",
        "",
        "---",
        "",
    ])
    sections.append(_section_inference(results))
    sections.append(_section_signal_aggregator(results))
    sections.append(_section_macro(results))
    sections.append(_section_us_macro(results))     # ★ Phase US-2追加
    sections.append(_section_anomaly_season(results))
    sections.append(_section_market_breadth(results))
    sections.append(_section_jp_sectors(results))
    sections.append(_section_us_sectors(results))
    sections.append(_section_etf(results))
    sections.append(_section_risk_constraints(results))   # ★ C-1/C-2追加
    sections.append(_section_screener(results))
    sections.append(_section_strategy_breakdown(results))
    sections.append(_section_earnings_watchlist(results))   # ★ I-2追加
    sections.append(_section_midterm_tracker(results))     # ★ I-5追加
    sections.append(_section_scenario(results))
    sections.append(_section_longterm_scenario(results))   # ★ R-5追加
    sections.append(_section_performance(results))
    sections.append(_section_position_sizer(results))
    sections.append(_section_biz_summaries(results))
    sections.append(_section_news(results))
    sections.append(_section_policy_radar(results))   # ★ policy_radar追加
    sections.append(_section_finbert_sentiment(results))   # ★ RESEARCH-5追加
    sections.append(_section_action_guide(results))
    sections.append(_section_stress_test(results))
    sections.append(_section_dividend_calendar(results))
    sections.append(_section_options_monitor(results))
    sections.append(_section_sr_vp(results))
    sections.append(_section_jp_us_bridge(results))
    sections.append(_section_rule_optimizer(results))
    sections.append(_section_alpha_attribution(results))    # ★ GAP-8追加
    sections.append(_section_backtest(results))
    sections.append(_section_comps(results))
    sections.append(_section_self_evaluator(results))
    sections.append(_section_screener_heatmap(results))
    sections.append(_section_portfolio_optimizer(results))
    sections.append(_section_dcf(results))
    # ── [R-3] 投資ジャーナルセクション ──────────────────────────
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, str(Path(__file__).parent))
        _ij = _imp.import_module("investment_journal")
        sections.append(_ij.format_journal_section(cfg))    # 記入待ち
        sections.append(_ij.format_pattern_section(cfg))    # 月次パターン
    except Exception:
        pass

    # フッター
    sections.append([
        "---",
        f"*自動生成: stock_analyzer_main.py | {today}*",
        "",
    ])

    # Markdown全文結合
    report_md = "\n".join(line for sec in sections for line in sec)

    # insights/weekly/ に保存
    report_path = ""
    try:
        weekly_dir = Path(cfg.INSIGHT_DIR) / "weekly"
        weekly_dir.mkdir(parents=True, exist_ok=True)
        report_path = str(weekly_dir / f"weekly_{today}.md")
        Path(report_path).write_text(report_md, encoding="utf-8")
        print(f"    ✅ レポート保存: {report_path}")
    except Exception as e:
        print(f"    ⚠️  レポート保存失敗: {e}")

    # extract_alerts() 用サマリー辞書を生成
    sector_results = results.get("sectors", {})
    sorted_sec = sorted(
        [(k, v) for k, v in sector_results.items() if isinstance(v, dict)],
        key=lambda x: x[1].get("composite_score", 0),
        reverse=True,
    )

    top3_names    = [v.get("sector_name_ja", k) for k, v in sorted_sec[:3]]
    bottom3_names = [v.get("sector_name_ja", k) for k, v in sorted_sec[-3:]]

    screener   = results.get("screener", {})
    passed     = [c for c in screener.get("candidates", []) if c.get("pass_filters")]
    top_picks  = sorted(passed, key=lambda x: x.get("composite_score", 0), reverse=True)[:3]
    pick_names = [c.get("name", c.get("ticker", "")) for c in top_picks]

    summary = {
        "report_date":    today,
        "top3_sectors":   top3_names,
        "bottom3_sectors":bottom3_names,
        "screener_pass":  len(passed),
        "top_picks":      pick_names,
        "report_path":    report_path,
        "report_md":      report_md[:500],  # アラートメール用の先頭500字
    }

    print(f"    📈 注目業種TOP3: {' > '.join(top3_names)}")
    print(f"    📉 警戒業種:     {' / '.join(bottom3_names)}")
    print(f"    🔍 スクリーニング通過: {len(passed)}社")

    return {
        "report_md":   report_md,
        "report_path": report_path,
        "summary":     summary,
        "skipped":     False,
    }


# ==============================================================
# BLOCK 11: Gmail/LINE 向けフォーマット生成
# ==============================================================

def build_weekly_email_html(report_md: str, summary: dict) -> str:
    """
    週次レポートをメール用 HTML に変換する。
    alert_notifier.py の send_gmail() に渡す用。
    Markdown の見出し・太字・リストを最小限のHTMLに変換する。
    """
    import re
    today    = summary.get("report_date", datetime.now().strftime("%Y-%m-%d"))
    top3     = summary.get("top3_sectors", [])
    bot3     = summary.get("bottom3_sectors", [])
    n_pass   = summary.get("screener_pass", 0)
    picks    = summary.get("top_picks", [])

    # Markdown → HTML 最小変換
    def md_to_html(text: str) -> str:
        lines = text.split("\n")
        html_lines = []
        in_table = False
        for line in lines:
            # テーブル
            if line.startswith("|"):
                if not in_table:
                    html_lines.append('<table style="border-collapse:collapse;width:100%;font-size:12px;margin:8px 0;">')
                    in_table = True
                cells = [c.strip() for c in line.split("|")[1:-1]]
                is_sep = all(set(c) <= set("-: ") for c in cells if c)
                if is_sep:
                    continue
                tag = "th" if not html_lines or "th" not in html_lines[-1] else "td"
                row = "".join(
                    f'<{tag} style="border:1px solid #e5e7eb;padding:4px 6px;text-align:left;">'
                    f'{c}</{tag}>' for c in cells
                )
                html_lines.append(f"<tr>{row}</tr>")
                continue
            elif in_table:
                html_lines.append("</table>")
                in_table = False

            # 見出し
            if line.startswith("## "):
                html_lines.append(
                    f'<h3 style="color:#1e3a5f;margin:20px 0 8px;'
                    f'padding-bottom:4px;border-bottom:2px solid #dbeafe;">'
                    f'{line[3:]}</h3>'
                )
            elif line.startswith("### "):
                html_lines.append(
                    f'<h4 style="color:#2563eb;margin:14px 0 6px;">'
                    f'{line[4:]}</h4>'
                )
            elif line.startswith("# "):
                pass  # タイトルはヘッダーで表示済みのためスキップ
            elif line.startswith("> "):
                html_lines.append(
                    f'<blockquote style="border-left:3px solid #93c5fd;'
                    f'margin:4px 0;padding:4px 10px;color:#374151;'
                    f'background:#f0f7ff;font-size:12px;">'
                    f'{line[2:]}</blockquote>'
                )
            elif line.startswith("- "):
                html_lines.append(
                    f'<li style="margin:3px 0;font-size:13px;">'
                    f'{_inline_md(line[2:])}</li>'
                )
            elif line.startswith("---"):
                html_lines.append('<hr style="border:none;border-top:1px solid #e5e7eb;margin:16px 0;">')
            elif line.strip() == "":
                html_lines.append("<br>")
            else:
                html_lines.append(
                    f'<p style="margin:4px 0;font-size:13px;color:#374151;">'
                    f'{_inline_md(line)}</p>'
                )

        if in_table:
            html_lines.append("</table>")
        return "\n".join(html_lines)

    def _inline_md(text: str) -> str:
        """**bold** と `code` のみインライン変換"""
        import re
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'`(.+?)`', r'<code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">\1</code>', text)
        return text

    body_html = md_to_html(report_md)

    top3_str  = " ＞ ".join(top3) if top3 else "N/A"
    picks_str = " / ".join(picks) if picks else "なし"

    return f"""
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#f9fafb;margin:0;padding:16px;">
<div style="max-width:680px;margin:0 auto;background:white;
            border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1);overflow:hidden;">

  <!-- ヘッダー -->
  <div style="background:linear-gradient(135deg,#1e3a5f,#1d4ed8);
              padding:24px;color:white;">
    <h1 style="margin:0;font-size:20px;">📊 週次投資判断レポート</h1>
    <p style="margin:8px 0 0;font-size:13px;opacity:.85;">{today} 自動生成 — stock_analyzer</p>
  </div>

  <!-- KPIバッジ -->
  <div style="display:flex;gap:0;border-bottom:1px solid #e5e7eb;">
    <div style="flex:1;padding:12px 16px;text-align:center;border-right:1px solid #e5e7eb;">
      <div style="font-size:11px;color:#6b7280;">注目業種TOP1</div>
      <div style="font-size:14px;font-weight:bold;color:#1d4ed8;">{top3[0] if top3 else 'N/A'}</div>
    </div>
    <div style="flex:1;padding:12px 16px;text-align:center;border-right:1px solid #e5e7eb;">
      <div style="font-size:11px;color:#6b7280;">スクリーニング通過</div>
      <div style="font-size:14px;font-weight:bold;color:#059669;">{n_pass}社</div>
    </div>
    <div style="flex:1;padding:12px 16px;text-align:center;">
      <div style="font-size:11px;color:#6b7280;">注目銘柄</div>
      <div style="font-size:12px;font-weight:bold;color:#7c3aed;">{picks_str[:20]}</div>
    </div>
  </div>

  <!-- レポート本文 -->
  <div style="padding:8px 24px 24px;">
    {body_html}
  </div>

  <!-- フッター -->
  <div style="padding:14px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;">
    <p style="margin:0;font-size:11px;color:#9ca3af;">
      データソース: yfinance / EdinetDB / 金融庁EDINET / IMF / DeepL
      &nbsp;|&nbsp; stock_analyzer_main.py より自動送信
    </p>
  </div>
</div>
</body></html>
"""


def build_weekly_line_message(summary: dict) -> str:
    """
    LINE 向け週次サマリー（短縮版）を生成する。
    """
    today   = summary.get("report_date", datetime.now().strftime("%Y-%m-%d"))
    top3    = summary.get("top3_sectors", [])
    bot3    = summary.get("bottom3_sectors", [])
    n_pass  = summary.get("screener_pass", 0)
    picks   = summary.get("top_picks", [])

    lines = [
        f"【週次レポート】{today}",
        "",
        f"📈 注目業種TOP3",
    ]
    for i, name in enumerate(top3[:3], 1):
        lines.append(f"  {i}. {name}")

    lines += [
        "",
        f"📉 警戒業種: {' / '.join(bot3[:3])}",
        f"🔍 スクリーナー通過: {n_pass}社",
    ]
    if picks:
        lines.append(f"💡 注目銘柄: {' / '.join(picks[:3])}")

    return "\n".join(lines)


# ==============================================================
# BLOCK 12: run_all_tasks エントリポイント
# ==============================================================

def run_weekly_report(cfg, results: dict) -> dict:
    """
    stock_analyzer_main.py の run_all_tasks() から呼ばれる。
    WEEKLY_MODULES に "weekly_report" を追加して使用する。

    処理フロー:
      1. generate_weekly_report() でMarkdown生成
      2. Gmail に HTML 版を送信（ALERT_GMAIL_* 設定を流用）
      3. LINE に短縮版を送信（ALERT_LINE_TOKEN を流用）
      4. results["weekly_report"] に結果を格納
         → alert_notifier.py が⑦アラートとして追加表示
    """
    if not getattr(cfg, "ENABLE_WEEKLY_REPORT", True):
        print("  ⏩ 週次レポート: スキップ（ENABLE_WEEKLY_REPORT=False）")
        return {"skipped": True}
    # [2-3] パフォーマンストラッカー（月曜日のみ、+20秒/週）
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, str(Path(__file__).parent))
        _pt_mod = _imp.import_module("performance_tracker")
        results["performance"] = _pt_mod.run_performance_tracker(cfg)
    except Exception as _e:
        print(f"  ⚠️  performance_tracker エラー（スキップ）: {_e}")
        results["performance"] = {}
    report_result = generate_weekly_report(cfg, results)
    report_md     = report_result.get("report_md", "")
    summary       = report_result.get("summary", {})

    sent_channels = []
    errors        = []

    # ── Gmail 送信 ──────────────────────────────────────────────
    gmail_from = getattr(cfg, "ALERT_GMAIL_FROM",     "")
    gmail_pwd  = getattr(cfg, "ALERT_GMAIL_PASSWORD", "")
    gmail_to   = getattr(cfg, "ALERT_GMAIL_TO",       "")
    if gmail_from and gmail_pwd and gmail_to:
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text      import MIMEText

            today   = summary.get("report_date", datetime.now().strftime("%Y-%m-%d"))
            subject = f"【週次投資判断レポート】{today}"
            html    = build_weekly_email_html(report_md, summary)

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = gmail_from
            msg["To"]      = gmail_to
            msg.attach(MIMEText(html, "html", "utf-8"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(gmail_from, gmail_pwd)
                server.sendmail(gmail_from, gmail_to, msg.as_string())

            sent_channels.append("Gmail")
            print(f"    ✅ 週次レポート Gmail 送信成功 → {gmail_to}")
        except Exception as e:
            err = f"Gmail: {str(e)[:80]}"
            errors.append(err)
            print(f"    ⚠️  {err}")

    # ── LINE 送信 ───────────────────────────────────────────────
    line_token   = getattr(cfg, "ALERT_LINE_TOKEN",   "")
    line_user_id = getattr(cfg, "ALERT_LINE_USER_ID", "")
    if line_token and line_user_id:
        try:
            import urllib.request
            import json as _json
            line_msg = build_weekly_line_message(summary)
            payload  = _json.dumps({
                "to":       line_user_id,
                "messages": [{"type": "text", "text": line_msg}],
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.line.me/v2/bot/message/push",
                data=payload,
                headers={
                    "Authorization":  f"Bearer {line_token}",
                    "Content-Type":   "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    sent_channels.append("LINE")
                    print("    ✅ 週次レポート LINE 送信成功")
                else:
                    errors.append(f"LINE: status={resp.status}")
        except Exception as e:
            errors.append(f"LINE: {str(e)[:60]}")
            print(f"    ⚠️  LINE送信エラー: {str(e)[:60]}")

    report_result["sent_channels"] = sent_channels
    report_result["errors"]        = errors
    return report_result


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util, sys
    from pathlib import Path

    cfg = None
    # 設定ファイルの読み込み
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        path_obj = Path(p)
        if path_obj.exists():
            spec = importlib.util.spec_from_file_location("config", p)
            # spec と loader が存在することを確定させる
            if spec is not None and spec.loader is not None:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    # 簡易テスト用ダミーresults
    dummy_results = {
        "sectors": {
            "1016": {
                "sector_name_ja": "電気機器",
                "composite_score": 72,
                "risk_level": "low",
                "reason_text": "AI・半導体需要が追い風",
                "macro_signals": [
                    {"label": "円安", "signal": "tailwind"},
                    {"label": "金利上昇", "signal": "headwind"},
                ],
                "top_companies": [
                    {"ticker": "6758.T", "name": "ソニーグループ", "fetch_ok": True,
                     "per": 18.5, "dividend_yield": 0.006, "analyst_upside": 0.12,
                     "rec_key": "buy"},
                ],
            },
        },
        "screener": {
            "candidates": [],
            "screened_count": 0,
            "pass_count": 0,
        },
    }

    # cfg が正しく読み込めている場合のみ実行する（論理的な安全装置）
    if cfg is not None:
        result = run_weekly_report(cfg, dummy_results)
        print("\n--- レポート先頭 300字 ---")
        # report_md が存在するか、文字列かどうかのチェック
        report_text = result.get("report_md", "")
        print(report_text[:300])
    else:
        print("ERROR: config.py が読み込めなかったため、レポート生成をスキップします。")
