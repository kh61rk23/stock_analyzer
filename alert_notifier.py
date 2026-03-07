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
            names = [s.get("sector_name", s.get("sector_code", "")) for s in golden]
            alerts.append({
                "trigger":  "golden_cross",
                "title":    f"📈 ゴールデンクロス {len(golden)}業種",
                "body":     "  " + " / ".join(names),
                "severity": "medium",
            })
        if dead:
            names = [s.get("sector_name", s.get("sector_code", "")) for s in dead]
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

    # ── ⑥ 注目業種トップ3（根拠付き）─────────────────────────
    # score_explainer.py の enrich_result_with_reason() で
    # "reason_text" が付与されていれば根拠文も表示する
    sector_results = results.get("sector_results", {})
    if sector_results:
        sorted_sectors = sorted(
            sector_results.items(),
            key=lambda x: x[1].get("composite_score", 0),
            reverse=True,
        )
        lines = []
        for code, r in sorted_sectors[:3]:
            name   = r.get("sector_name_ja", code)
            score  = r.get("composite_score", 0)
            reason = r.get("reason_text", "")
            line   = f"  {name}: {score:.0f}点"
            if reason:
                line += f"\n    {reason}"
            lines.append(line)
        if lines:
            alerts.append({
                "trigger":  "top_sectors",
                "title":    "🏆 注目業種トップ3（本日）",
                "body":     "\n".join(lines),
                "severity": "info",
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
        rows = ""
        severity_color = {"high": "#ef4444", "medium": "#f59e0b", "info": "#3b82f6"}
        for al in alerts:
            color = severity_color.get(al["severity"], "#6b7280")
            rows += f"""
<tr>
  <td style="padding:8px;border-bottom:1px solid #e5e7eb;">
    <span style="color:{color};font-weight:bold;">{al['title']}</span><br>
    <pre style="margin:4px 0;font-size:12px;color:#374151;">{al['body']}</pre>
  </td>
</tr>"""

        return f"""
<html><body>
<h2 style="color:#1f2937;">📊 投資AIエージェント アラート</h2>
<p style="color:#6b7280;">{now_str}</p>
<table style="border-collapse:collapse;width:100%;max-width:600px;">
{rows}
</table>
<hr>
<p style="color:#9ca3af;font-size:11px;">
  このメールは stock_analyzer_main.py から自動送信されました。
</p>
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
