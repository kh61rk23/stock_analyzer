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

    etf_list = etf.get("etfs", etf.get("results", []))
    if not etf_list:
        lines.append("*データなし*"); lines.append(""); return lines

    # スコア or リターンでソート
    sorted_etf = sorted(
        [e for e in etf_list if isinstance(e, dict)],
        key=lambda x: x.get("score", x.get("return_1m", 0)) or 0,
        reverse=True,
    )

    hot   = sorted_etf[:3]
    cool  = sorted_etf[-3:] if len(sorted_etf) >= 6 else []

    if hot:
        lines.append("**🔥 ホット テーマ**")
        for e in hot:
            name  = e.get("name", e.get("ticker", ""))[:20]
            ret1m = e.get("return_1m", e.get("perf_1m"))
            score = e.get("score")
            ret_s = f"{ret1m:+.1f}%" if ret1m is not None else ""
            sc_s  = f"スコア:{score:.0f}" if score is not None else ""
            lines.append(f"  🔥 {name}: {ret_s} {sc_s}".strip())

    if cool:
        lines.append("**❄️  クール テーマ**")
        for e in cool:
            name  = e.get("name", e.get("ticker", ""))[:20]
            ret1m = e.get("return_1m", e.get("perf_1m"))
            ret_s = f"{ret1m:+.1f}%" if ret1m is not None else ""
            lines.append(f"  ❄️  {name}: {ret_s}".strip())

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

    lines.append("| 銘柄 | 業種 | スコア | PER | PBR | ROE | 配当 | 推奨 |")
    lines.append("|------|------|-------|-----|-----|-----|-----|-----|")
    for c in sorted(passed, key=lambda x: x.get("composite_score", 0), reverse=True)[:10]:
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
    macro = results.get("macro")
    if isinstance(macro, dict):
        signals = macro.get("signals", [])
        headwinds = [s for s in signals if s.get("signal") == "headwind"]
        tailwinds = [s for s in signals if s.get("signal") == "tailwind"]
        if tailwinds:
            lines.append(f"💚 **追い風シグナル**: {' / '.join(s['label'] for s in tailwinds[:4])}")
        if headwinds:
            lines.append(f"🔴 **逆風シグナル**: {' / '.join(s['label'] for s in headwinds[:4])}")
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

# [2-3] パフォーマンストラッカーセクション
def _section_performance(results: dict) -> list:
    perf = results.get("performance", {})
    if not perf or perf.get("skipped"):
        return []
    text = perf.get("summary_text", "")
    return text.split("\n") if text else []

# ==============================================================
# BLOCK 9: アクション候補（来週の投資判断ガイド）
# ==============================================================

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
    sections.append([
        f"# 📊 週次投資判断レポート",
        f"**{today}（第{week_num}週）** — stock_analyzer 自動生成",
        "",
        "---",
        "",
    ])

    sections.append(_section_macro(results))
    sections.append(_section_jp_sectors(results))
    sections.append(_section_us_sectors(results))
    sections.append(_section_etf(results))
    sections.append(_section_screener(results))
    sections.append(_section_performance(results))
    sections.append(_section_biz_summaries(results))
    sections.append(_section_news(results))
    sections.append(_section_action_guide(results))

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
    line_token = getattr(cfg, "ALERT_LINE_TOKEN", "")
    if line_token:
        try:
            import urllib.request, urllib.parse
            line_msg = build_weekly_line_message(summary)
            data     = urllib.parse.urlencode({"message": line_msg}).encode("utf-8")
            req = urllib.request.Request(
                "https://notify-api.line.me/api/notify",
                data=data,
                headers={
                    "Authorization": f"Bearer {line_token}",
                    "Content-Type":  "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                if '"status":200' in body:
                    sent_channels.append("LINE")
                    print("    ✅ 週次レポート LINE 送信成功")
                else:
                    errors.append(f"LINE: {body[:60]}")
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
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
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

    result = run_weekly_report(cfg, dummy_results)
    print("\n--- レポート先頭 300字 ---")
    print(result["report_md"][:300])
