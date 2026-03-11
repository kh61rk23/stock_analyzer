# ==============================================================
# alert_notifier.py  v1.0  (5-B)
# シグナル変化時の LINE / Gmail アラート通知
# ==============================================================
# 【役割】
#   run_all_tasks() の results を受け取り、以下の条件に合致した
#   場合に LINE Notify / Gmail で通知を送信する。
#
# 【通知トリガー条件】（config で個別 ON/OFF 可能）
#   ① 政策金利変化     : policy_tracker の new_events が 1件以上
#   ② ゴールデン/デッドクロス : timeline の長期分析結果
#   ③ 高スコアアイデア  : idea_score >= ALERT_MIN_IDEA_SCORE
#   ④ 強いマクロ逆風   : headwind シグナルが ALERT_HEADWIND_THRESHOLD 件以上
#
# 【通知チャネル】
#   LINE Notify : ALERT_LINE_TOKEN を設定（無料・簡単）
#   Gmail       : ALERT_GMAIL_FROM / ALERT_GMAIL_PASSWORD（App Password）
#                 / ALERT_GMAIL_TO を設定
#   どちらか片方だけでも動作する。両方空の場合はスキップ。
#
# 【返り値】run_alert_notifier(cfg, results) -> dict
#   {
#     "alerts_sent":   int,   # 送信した通知数
#     "channels":      [...], # 使用したチャネル名
#     "messages":      [...], # 送信したメッセージリスト
#     "errors":        [...], # エラーメッセージ（失敗時）
#     "skipped":       bool,  # 通知条件に合致しなかった場合 True
#   }
#
# 【依存】config.py / urllib（標準ライブラリ） / smtplib（標準ライブラリ）
# 【呼出】from alert_notifier import run_alert_notifier
# ==============================================================

import smtplib
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# ==============================================================
# BLOCK 1: アラート条件判定
# ==============================================================

def extract_alerts(cfg, results: dict) -> list:
    """
    results から通知すべきアラートを抽出してリストで返す。

    Returns:
        [
          {
            "trigger":  "policy_change" | "golden_cross" | "dead_cross"
                      | "high_score_idea" | "macro_headwind",
            "title":   str,   # 通知タイトル（1行）
            "body":    str,   # 通知本文（複数行可）
            "severity":"high" | "medium" | "info",
          },
          ...
        ]
    """
    alerts = []

    # ── ① 政策金利変化 ────────────────────────────────────────
    if getattr(cfg, "ALERT_ON_POLICY_CHANGE", True):
        policy = results.get("policy", {})
        new_events = policy.get("new_events", [])
        if new_events:
            lines = []
            for ev in new_events:
                chg = ev.get("change_pt", 0)
                lines.append(
                    f"  [{ev.get('bank','')}] {ev.get('label','')} "
                    f"{chg:+.3f}%pt → {ev.get('direction','')}"
                )
            alerts.append({
                "trigger":  "policy_change",
                "title":    f"⚡ 政策金利変化 {len(new_events)}件検出",
                "body":     "\n".join(lines),
                "severity": "high" if any(abs(ev.get("change_pt",0)) >= 0.25
                                          for ev in new_events) else "medium",
            })

    # ── ② ゴールデン/デッドクロス ─────────────────────────────
    if getattr(cfg, "ALERT_ON_GOLDEN_CROSS", True):
        tl = results.get("timeline", {})
        lt = tl.get("long_term", {})

        golden = lt.get("golden_cross_sectors", [])
        dead   = lt.get("dead_cross_sectors",   [])

        if golden:
            names = [s.get("sector_name", s.get("sector_code", "")) if isinstance(s, dict) else str(s) for s in golden]
            alerts.append({
                "trigger":  "golden_cross",
                "title":    f"📈 ゴールデンクロス {len(golden)}業種",
                "body":     "  " + " / ".join(names),
                "severity": "medium",
            })
        if dead:
            names = [s.get("sector_name", s.get("sector_code", "")) if isinstance(s, dict) else str(s) for s in dead]
            alerts.append({
                "trigger":  "dead_cross",
                "title":    f"📉 デッドクロス {len(dead)}業種",
                "body":     "  " + " / ".join(names),
                "severity": "medium",
            })

    # ── ③ 高スコアアイデア ────────────────────────────────────
    if getattr(cfg, "ALERT_ON_HIGH_SCORE_IDEA", True):
        min_score = getattr(cfg, "ALERT_MIN_IDEA_SCORE", 70.0)
        idea_result = results.get("ideas", {})
        ideas = idea_result.get("ideas", []) if isinstance(idea_result, dict) else []
        high_ideas = [i for i in ideas if i.get("idea_score", 0) >= min_score]
        if high_ideas:
            lines = [
                f"  #{i['rank']} [{i['sector_name']}] スコア:{i['idea_score']} — {i['title'][:35]}"
                for i in high_ideas[:5]
            ]
            alerts.append({
                "trigger":  "high_score_idea",
                "title":    f"💡 高スコアアイデア {len(high_ideas)}件（閾値:{min_score}）",
                "body":     "\n".join(lines),
                "severity": "info",
            })

    # ── ④ 強いマクロ逆風 ─────────────────────────────────────
    if getattr(cfg, "ALERT_ON_MACRO_HEADWIND", True):
        threshold = getattr(cfg, "ALERT_HEADWIND_THRESHOLD", 2)
        macro_signals = results.get("macro_signals", [])
        # macro_signals が results に直接入っていない場合は macro キーを確認
        if not macro_signals and isinstance(results.get("macro"), dict):
            macro_signals = results["macro"].get("signals", [])
        headwinds = [s for s in macro_signals if s.get("signal") == "headwind"]
        if len(headwinds) >= threshold:
            names = [s.get("label", "") for s in headwinds]
            alerts.append({
                "trigger":  "macro_headwind",
                "title":    f"🔴 マクロ逆風集中 {len(headwinds)}件",
                "body":     "  " + " / ".join(names),
                "severity": "medium",
            })
            
    # ── ⑤ 本日の注目ニュース ─────────────────────────────────
    news_result = results.get("news", {})
    if news_result and isinstance(news_result, dict):
        top_articles = news_result.get("top_articles", [])
        max_n = getattr(cfg, "NEWS_ALERT_MAX_ARTICLES", 5)
        if top_articles:
            lines = []
            sector_results_for_news = results.get("sectors", {})
            # 上位スコア業種の関連ニュースを優先
            by_sector = news_result.get("by_sector", {})
            shown_titles = set()
            sorted_secs = sorted(
                sector_results_for_news.items(),
                key=lambda x: x[1].get("composite_score", 0)
                    if isinstance(x[1], dict) else 0,
                reverse=True,
            )
            for code, _ in sorted_secs[:5]:
                arts = by_sector.get(code, [])
                if arts:
                    a = arts[0]
                    t = a["title"][:60]
                    if t not in shown_titles:
                        lines.append(f"  [{a['source']}] {t}")
                        shown_titles.add(t)
                    if len(lines) >= max_n:
                        break
            # 残りを重要度順で補完
            for a in top_articles:
                if len(lines) >= max_n:
                    break
                t = a["title"][:60]
                if t not in shown_titles:
                    lines.append(f"  [{a['source']}] {t}")
                    shown_titles.add(t)
            if lines:
                alerts.append({
                    "trigger":  "news_top",
                    "title":    f"📰 本日の注目ニュース（{len(lines)}件）",
                    "body":     "\n".join(lines),
                    "severity": "info",
                })
    # ── ⑥ テーマETF 熱量ランキング ───────────────────────────
    etf_result = results.get("etf_tracker", {})
    if etf_result and isinstance(etf_result, dict):
        top_themes = etf_result.get("top_themes", [])
        hot_themes = [t for t in etf_result.get("themes", [])
                      if t["signal"] == "hot"]
        display = hot_themes if hot_themes else top_themes
        if display:
            SIGNAL_ICONS = {
                "hot": "🔥", "warming": "📈",
                "neutral": "➡️", "cooling": "❄️",
            }
            lines = []
            for t in display[:3]:
                icon   = SIGNAL_ICONS.get(t["signal"], "➡️")
                etfs   = "/".join(t["etfs"])
                arxiv  = (f"ArXiv+{t['arxiv_detail']['growth_rate']:.0f}%"
                          if t["arxiv_detail"].get("growth_rate") is not None
                          else "")
                lines.append(
                    f"  {icon} {t['theme_name']}({etfs}): "
                    f"熱量{t['heat_score']:.0f}点  {arxiv}"
                )
            alerts.append({
                "trigger":  "etf_theme",
                "title":    f"🌡️ テーマETF熱量 TOP{len(lines)}",
                "body":     "\n".join(lines),
                "severity": "high" if hot_themes else "info",
            })
            
    # ── ⑥ 注目業種トップ3（根拠・代表銘柄・EdinetDB情報付き） ────
    sector_results = results.get("sectors", results.get("sector_results", {}))
    if sector_results:
        sorted_sectors = sorted(
            sector_results.items(),
            key=lambda x: x[1].get("composite_score", 0) if isinstance(x[1], dict) else 0,
            reverse=True,
        )
        lines = []
        for code, r in sorted_sectors[:3]:
            if not isinstance(r, dict):
                continue
            name   = r.get("sector_name_ja", code)
            score  = r.get("composite_score", 0)
            reason = r.get("reason_text", "")
            line   = f"  {name}: {score:.0f}点"
            if reason:
                line += f"\n    💬 {reason}"

            # 代表銘柄の財務ハイライト（yfinance全フィールドから抽出）
            top_cos = r.get("top_companies", [])
            for c in top_cos[:2]:
                if not c.get("fetch_ok"):
                    continue
                cname = c.get("name", c.get("ticker", ""))[:12]
                per_s = f"PER:{c['per']:.1f}x" if c.get("per") else ""
                dy_s  = f"配当:{c['dividend_yield']*100:.1f}%" if c.get("dividend_yield") else ""
                up_s  = f"目標株価乖離:{c['analyst_upside']*100:+.0f}%" if c.get("analyst_upside") is not None else ""
                rec_s = c.get("rec_key", "")
                de_s  = f"D/E:{c['debt_to_equity']:.1f}" if c.get("debt_to_equity") else ""
                parts = [x for x in [per_s, dy_s, up_s, rec_s, de_s] if x]
                if parts:
                    line += f"\n    📌 {cname}: {' | '.join(parts)}"

            # EdinetDB 信用スコア（v3.0対応）
            edinet_data = r.get("edinet_data", {})
            for ticker, tdata in list(edinet_data.items())[:1]:
                if not tdata:
                    continue
                analysis = tdata.get("analysis", {})
                if analysis:
                    cs = analysis.get("credit_score", 0)
                    cr = analysis.get("credit_rating", "")
                    r_icon = {"excellent": "💚", "good": "🟡", "fair": "🟠", "poor": "🔴"}.get(cr, "⚪")
                    line += f"\n    {r_icon} 信用スコア: {cs}/100"
                    risks = analysis.get("risk_factors", [])
                    if risks:
                        line += f"\n    ⚠️  主要リスク: {risks[0][:60]}"
                fin = tdata.get("financials", {})
                if fin:
                    rev_cagr = fin.get("revenue_cagr")
                    if rev_cagr is not None:
                        line += f"\n    📈 売上CAGR(3年): {rev_cagr*100:+.1f}%/年"
                gov = tdata.get("gov_meta", {})
                if gov:
                    line += f"\n    📋 有報提出: {gov.get('submit_date','N/A')}"

            lines.append(line)

        if lines:
            alerts.append({
                "trigger":  "top_sectors",
                "title":    "🏆 注目業種トップ3（本日）",
                "body":     "\n".join(lines),
                "severity": "info",
            })

    # ── ⑦ 高配当銘柄アラート（新規） ──────────────────────────
    min_dy = getattr(cfg, "ALERT_MIN_DIVIDEND_YIELD", 0.04)  # デフォルト4%
    if getattr(cfg, "ALERT_ON_HIGH_DIVIDEND", True):
        high_div_lines = []
        for code, r in sector_results.items():
            if not isinstance(r, dict):
                continue
            for c in r.get("top_companies", []):
                if not c.get("fetch_ok"):
                    continue
                dy = c.get("dividend_yield")
                if dy and dy >= min_dy:
                    pr = c.get("payout_ratio")
                    pr_s = f"配当性向:{pr*100:.0f}%" if pr else ""
                    high_div_lines.append(
                        f"  {c.get('name','')[:14]}: {dy*100:.1f}% {pr_s}"
                    )
        if high_div_lines:
            alerts.append({
                "trigger":  "high_dividend",
                "title":    f"💰 高配当銘柄 {len(high_div_lines)}社（{min_dy*100:.0f}%以上）",
                "body":     "\n".join(high_div_lines[:8]),
                "severity": "info",
            })

    # ── ⑧ アナリスト強気推奨アラート（新規） ─────────────────
    if getattr(cfg, "ALERT_ON_STRONG_BUY", True):
        buy_lines = []
        for code, r in sector_results.items():
            if not isinstance(r, dict):
                continue
            for c in r.get("top_companies", []):
                if not c.get("fetch_ok"):
                    continue
                rec = c.get("rec_mean")
                upside = c.get("analyst_upside")
                if rec and rec <= 2.0 and upside and upside >= 0.15:
                    buy_lines.append(
                        f"  {c.get('name','')[:14]}: 推奨{rec:.1f} / 目標株価乖離{upside*100:+.0f}%"
                    )
        if buy_lines:
            alerts.append({
                "trigger":  "analyst_strong_buy",
                "title":    f"🎯 アナリスト強気推奨 {len(buy_lines)}社",
                "body":     "\n".join(buy_lines[:8]),
                "severity": "info",
            })

    # ── ⑨ 財務悪化ウォッチ（D/E高・FCFマイナス）（新規） ────────
    if getattr(cfg, "ALERT_ON_FINANCIAL_RISK", True):
        risk_lines = []
        for code, r in sector_results.items():
            if not isinstance(r, dict):
                continue
            sec_name = r.get("sector_name_ja", code)
            for c in r.get("top_companies", []):
                if not c.get("fetch_ok"):
                    continue
                de  = c.get("debt_to_equity")
                fcf = c.get("free_cashflow")
                cr  = c.get("current_ratio")
                flags = []
                if de and de > 200:     flags.append(f"D/E:{de:.0f}(高負債)")
                if fcf and fcf < 0:     flags.append(f"FCF:{fmt_cap(fcf)}(マイナス)")
                if cr and cr < 1.0:     flags.append(f"流動比率:{cr:.2f}(1未満)")
                if flags:
                    risk_lines.append(
                        f"  [{sec_name}] {c.get('name','')[:12]}: {' / '.join(flags)}"
                    )
        if risk_lines:
            alerts.append({
                "trigger":  "financial_risk",
                "title":    f"⚠️  財務リスクウォッチ {len(risk_lines)}社",
                "body":     "\n".join(risk_lines[:8]),
                "severity": "medium",
            })

    # ── ⑩ 週次レポートサマリー（月曜のみ）────────────────────
    weekly = results.get("weekly_report", {})
    if weekly and not weekly.get("skipped") and isinstance(weekly, dict):
        summary = weekly.get("summary", {})
        top3    = summary.get("top3_sectors", [])
        bot3    = summary.get("bottom3_sectors", [])
        picks   = summary.get("top_picks", [])
        n_pass  = summary.get("screener_pass", 0)
        lines   = []
        if top3:
            lines.append(f"  📈 注目業種: {' ＞ '.join(top3)}")
        if bot3:
            lines.append(f"  📉 警戒業種: {' / '.join(bot3)}")
        if picks:
            lines.append(f"  🔍 スクリーニング通過 {n_pass}社 | 上位: {' / '.join(picks)}")
        # レポート本文の先頭300字を添付
        report_head = (weekly.get("report_md", "")[:300]
                       .replace("# ", "").replace("## ", "").replace("**", ""))
        if report_head:
            lines.append("")
            lines.append(report_head.strip())
        if lines:
            alerts.append({
                "trigger":  "weekly_report",
                "title":    "📊 週次投資判断レポート",
                "body":     "\n".join(lines),
                "severity": "info",
            })
    
    # ──11 債券・コモディティ: リセッション警戒アラート ──────────
    bc_result = results.get("bond_commodity", {})
    recession_score = bc_result.get("recession_score", 0)
    if recession_score >= 70:
        alerts.append({
            "type":    "recession_warning",
            "level":   "HIGH",
            "title":   f"⚠️ リセッション警戒: スコア {recession_score}/100",
            "body":    (
                f"イールドカーブ分析によりリセッションリスクが高まっています。\n"
                f"スコア: {recession_score}/100\n"
                f"カーブ形状: {bc_result.get('curve_analysis',{}).get('shape_ja','不明')}"
            ),
        })
    elif recession_score >= 45:
        alerts.append({
            "type":  "recession_watch",
            "level": "MEDIUM",
            "title": f"📊 リセッション注視: スコア {recession_score}/100",
            "body":  f"イールドカーブが注意水準です。カーブ形状: "
                     f"{bc_result.get('curve_analysis',{}).get('shape_ja','不明')}",
        })
        
     # ── ⑤ 異常検知アラート ───────────────────────────────────
    if getattr(cfg, "ALERT_ON_ANOMALY", True):
        min_sev = getattr(cfg, "ALERT_ANOMALY_MIN_SEVERITY", "high")
        sev_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        min_rank = sev_rank.get(min_sev, 2)

        anomaly_result = results.get("anomaly", {})
        all_anomalies  = anomaly_result.get("all_anomalies", [])

        # 閾値以上の重要度の異常のみ抽出
        trigger_items = [
            a for a in all_anomalies
            if sev_rank.get(a.get("severity", "low"), 0) >= min_rank
        ]

        if trigger_items:
            # 複合異常を先頭に
            combined = [a for a in trigger_items if a.get("anomaly_type") == "combined"]
            others   = [a for a in trigger_items if a.get("anomaly_type") != "combined"]

            lines = []
            if combined:
                lines.append("【複合異常 🚨】")
                for a in combined[:3]:
                    lines.append(
                        f"  {a.get('ticker','?')} ({a.get('label','')}) "
                        f"— {a.get('summary','')}"
                    )
            if others:
                lines.append("【その他異常】")
                for a in others[:5]:
                    ticker_or_sector = a.get("ticker") or a.get("sector_code", "?")
                    lines.append(
                        f"  [{a.get('severity','?').upper()}] "
                        f"{ticker_or_sector} ({a.get('label', a.get('sector_name',''))}) "
                        f"— {a.get('summary','')}"
                    )

            severity_level = "high" if any(
                a.get("severity") == "critical" for a in trigger_items
            ) else "medium"

            alerts.append({
                "trigger":  "anomaly_detection",
                "title":    f"🔍 異常検知: {len(trigger_items)}件の統計的外れ値",
                "body":     "\n".join(lines),
                "severity": severity_level,
            })
    return alerts


# ==============================================================
# BLOCK 2: メッセージ本文生成
# ==============================================================

def build_message(alerts: list, mode: str = "line") -> str:
    """
    アラートリストからテキストメッセージを生成する。

    Args:
        alerts : extract_alerts() の返り値
        mode   : "line"（LINE向け短め）| "email"（メール向け詳細）
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if mode == "line":
        parts = [f"【投資AIエージェント】{now_str}", ""]
        for al in alerts:
            parts.append(al["title"])
            parts.append(al["body"])
            parts.append("")
        return "\n".join(parts).strip()

    else:  # email HTML
        severity_color = {"high": "#ef4444", "medium": "#f59e0b", "info": "#3b82f6"}
        severity_label = {"high": "🔴 重要", "medium": "🟡 注意", "info": "ℹ️  情報"}
        rows = ""
        for al in alerts:
            color = severity_color.get(al["severity"], "#6b7280")
            label = severity_label.get(al["severity"], "")
            # body の改行を <br> に変換（pre タグより視認性が高い）
            body_html = al["body"].replace("\n", "<br>").replace("    ", "&nbsp;&nbsp;&nbsp;&nbsp;")
            rows += f"""
<tr>
  <td style="padding:12px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top;">
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
      <span style="background:{color};color:white;font-size:11px;
                   padding:2px 6px;border-radius:3px;">{label}</span>
      <span style="color:{color};font-weight:bold;font-size:14px;">{al['title']}</span>
    </div>
    <div style="font-size:13px;color:#374151;line-height:1.7;
                padding-left:8px;border-left:3px solid {color};">
      {body_html}
    </div>
  </td>
</tr>"""

        # EdinetDB API使用状況バッジ
        edinet_info = ""
        try:
            from pathlib import Path
            import json
            # 日次キャッシュからAPI使用状況を読み込む（あれば表示）
            # DATA_DIRは環境依存なので存在しない場合はスキップ
            pass
        except Exception:
            pass

        return f"""
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#f9fafb;margin:0;padding:16px;">
<div style="max-width:640px;margin:0 auto;background:white;
            border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1);overflow:hidden;">

  <!-- ヘッダー -->
  <div style="background:linear-gradient(135deg,#1e3a5f,#2563eb);
              padding:20px 24px;color:white;">
    <h2 style="margin:0;font-size:18px;">📊 投資AIエージェント デイリーレポート</h2>
    <p style="margin:6px 0 0;font-size:12px;opacity:.85;">{now_str} 自動生成</p>
  </div>

  <!-- アラート件数バッジ -->
  <div style="padding:12px 24px;background:#f0f7ff;border-bottom:1px solid #dbeafe;">
    <span style="font-size:13px;color:#1d4ed8;">
      本日のシグナル: <strong>{len(alerts)}件</strong>
      &nbsp;|&nbsp;
      高重要度: <strong style="color:#ef4444;">{sum(1 for a in alerts if a['severity']=='high')}件</strong>
      &nbsp;|&nbsp;
      注意: <strong style="color:#f59e0b;">{sum(1 for a in alerts if a['severity']=='medium')}件</strong>
    </span>
  </div>

  <!-- アラート本文 -->
  <table style="border-collapse:collapse;width:100%;padding:0 16px;">
    {rows}
  </table>

  <!-- フッター -->
  <div style="padding:16px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;">
    <p style="margin:0;font-size:11px;color:#9ca3af;">
      stock_analyzer_main.py より自動送信 | データソース: yfinance / EdinetDB / 金融庁EDINET / IMF
    </p>
  </div>
</div>
</body></html>
"""


# ==============================================================
# BLOCK 3: LINE Notify 送信
# ==============================================================

def send_line(token: str, message: str) -> tuple:
    """
    LINE Notify でメッセージを送信する。
    ネットワーク無効環境では例外をキャッチして失敗を返す。

    Returns:
        (success: bool, error_msg: str)
    """
    if not token:
        return False, "LINE トークン未設定"

    try:
        data    = urllib.parse.urlencode({"message": message}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/x-www-form-urlencoded",
        }
        req  = urllib.request.Request(
            "https://notify-api.line.me/api/notify",
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            if '"status":200' in body:
                return True, ""
            return False, f"LINE API: {body[:100]}"
    except Exception as e:
        return False, f"LINE 送信エラー: {str(e)[:80]}"


# ==============================================================
# BLOCK 4: Gmail 送信
# ==============================================================

def send_gmail(
    from_addr: str,
    app_password: str,
    to_addr: str,
    subject: str,
    html_body: str,
) -> tuple:
    """
    Gmail SMTP (App Password) でメールを送信する。

    Returns:
        (success: bool, error_msg: str)
    """
    if not from_addr or not app_password or not to_addr:
        return False, "Gmail 設定未完了（FROM/PASSWORD/TO を確認）"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to_addr
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(from_addr, app_password)
            smtp.sendmail(from_addr, to_addr, msg.as_string())
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, "Gmail 認証失敗（App Password を確認してください）"
    except Exception as e:
        return False, f"Gmail 送信エラー: {str(e)[:80]}"


# ==============================================================
# BLOCK 5: テスト通知
# ==============================================================

def send_test_notification(cfg) -> dict:
    """
    設定確認用のテスト通知を送信する。
    UI のボタンから呼び出す。

    Returns:
        {"line": bool, "gmail": bool, "errors": [...]}
    """
    test_msg  = (
        f"【テスト通知】{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        "alert_notifier.py の通知設定が正常に動作しています。"
    )
    errors = []
    results = {"line": False, "gmail": False, "errors": []}

    # LINE
    token = getattr(cfg, "ALERT_LINE_TOKEN", "")
    if token:
        ok, err = send_line(token, test_msg)
        results["line"] = ok
        if err:
            results["errors"].append(f"LINE: {err}")
            print(f"    ⚠️  LINE: {err}")
        else:
            print("    ✅ LINE テスト通知 送信成功")

    # Gmail
    frm  = getattr(cfg, "ALERT_GMAIL_FROM", "")
    pwd  = getattr(cfg, "ALERT_GMAIL_PASSWORD", "")
    to   = getattr(cfg, "ALERT_GMAIL_TO", "")
    if frm and pwd and to:
        html = f"<p>{test_msg.replace(chr(10),'<br>')}</p>"
        ok, err = send_gmail(frm, pwd, to, "【テスト】投資AIエージェント通知", html)
        results["gmail"] = ok
        if err:
            results["errors"].append(f"Gmail: {err}")
            print(f"    ⚠️  Gmail: {err}")
        else:
            print("    ✅ Gmail テスト通知 送信成功")

    return results


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_alert_notifier(cfg, results: dict) -> dict:
    """
    run_all_tasks から呼び出されるエントリポイント。
    results からアラート条件を判定し、設定チャネルへ送信する。

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の返り値
    """
    print("\n  🔔 アラートノティファイアー 開始...")

    line_token = getattr(cfg, "ALERT_LINE_TOKEN",      "")
    gmail_from = getattr(cfg, "ALERT_GMAIL_FROM",      "")
    gmail_pwd  = getattr(cfg, "ALERT_GMAIL_PASSWORD",  "")
    gmail_to   = getattr(cfg, "ALERT_GMAIL_TO",        "")

    has_line  = bool(line_token)
    has_gmail = bool(gmail_from and gmail_pwd and gmail_to)

    if not has_line and not has_gmail:
        print("    ⏩ 通知チャネル未設定（LINE/Gmail どちらも未設定）")
        return {"alerts_sent": 0, "channels": [], "messages": [],
                "errors": [], "skipped": True}

    # アラート抽出
    alerts = extract_alerts(cfg, results)
    if not alerts:
        print("    ✅ 通知条件に合致するシグナルなし")
        return {"alerts_sent": 0, "channels": [], "messages": [],
                "errors": [], "skipped": True}

    print(f"    ⚡ {len(alerts)} 件のアラート検出")
    for al in alerts:
        print(f"      [{al['severity'].upper()}] {al['title']}")

    channels = []
    messages = []
    errors   = []
    sent     = 0

    # LINE 送信
    if has_line:
        line_msg = build_message(alerts, mode="line")
        ok, err  = send_line(line_token, line_msg)
        if ok:
            channels.append("LINE")
            messages.append(line_msg)
            sent += 1
            print("    ✅ LINE 通知 送信成功")
        else:
            errors.append(f"LINE: {err}")
            print(f"    ⚠️  LINE: {err}")

    # Gmail 送信
    if has_gmail:
        now_str   = datetime.now().strftime("%Y-%m-%d")
        subject   = f"【投資AIエージェント】{len(alerts)}件のアラート ({now_str})"
        html_body = build_message(alerts, mode="email")
        ok, err   = send_gmail(gmail_from, gmail_pwd, gmail_to, subject, html_body)
        if ok:
            channels.append("Gmail")
            messages.append(subject)
            sent += 1
            print("    ✅ Gmail 通知 送信成功")
        else:
            errors.append(f"Gmail: {err}")
            print(f"    ⚠️  Gmail: {err}")

    return {
        "alerts_sent": sent,
        "channels":    channels,
        "messages":    messages,
        "errors":      errors,
        "skipped":     False,
    }


# 単体テスト
if __name__ == "__main__":
    import importlib.util
    from pathlib import Path
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    print("=== テスト通知送信 ===")
    r = send_test_notification(cfg)
    print(f"LINE: {r['line']} / Gmail: {r['gmail']}")
    if r["errors"]:
        for e in r["errors"]:
            print(f"  エラー: {e}")
