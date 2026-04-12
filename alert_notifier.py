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

# ── fmt_cap: 時価総額・FCF等の大数値フォーマット ──────────────
def fmt_cap(val) -> str:
    """大きな数値を兆・億単位で整形（finance_logic.fmt_large 相当）"""
    if val is None:
        return "―"
    try:
        v = float(val)
        if abs(v) >= 1e12:
            return f"{v/1e12:.2f}兆"
        if abs(v) >= 1e8:
            return f"{v/1e8:.1f}億"
        if abs(v) >= 1e6:
            return f"{v/1e6:.2f}M"
        return f"{v:,.0f}"
    except Exception:
        return "―"

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
                # [バグ修正] yfinanceが一部銘柄で異常値を返すためバリデーション追加
                # dividendYield は小数（0.04 = 4%）で返る。30%超は明らかなデータ異常
                if dy and min_dy <= dy <= 0.30:
                    pr = c.get("payout_ratio")
                    # payoutRatio も小数（0.49 = 49%）で返る。2.0超（200%超）は異常
                    pr_valid = pr if (pr is not None and 0.0 <= pr <= 2.0) else None
                    pr_s = f"配当性向:{pr_valid*100:.0f}%" if pr_valid is not None else ""
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
            "trigger": "recession_warning",
            "severity": "high",
            "title":   f"⚠️ リセッション警戒: スコア {recession_score}/100",
            "body":    (
                f"イールドカーブ分析によりリセッションリスクが高まっています。\n"
                f"スコア: {recession_score}/100\n"
                f"カーブ形状: {bc_result.get('curve_analysis',{}).get('shape_ja','不明')}"
            ),
        })
    elif recession_score >= 45:
        alerts.append({
            "trigger": "recession_watch",
            "severity": "medium",
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
                    z   = a.get("z_score")
                    z_s = f" Z={z:.2f}" if z is not None else ""
                    # 複合異常は価格+出来高の両根拠を表示
                    sub = []
                    if a.get("price_change"):
                        sub.append(f"価格{a['price_change']*100:+.1f}%")
                    if a.get("volume_ratio"):
                        sub.append(f"出来高{a['volume_ratio']:.1f}倍")
                    detail = f"({', '.join(sub)})" if sub else ""
                    lines.append(
                        f"  {a.get('ticker','?')} ({a.get('label','')}) "
                        f"— {a.get('summary','')}{z_s} {detail}"
                    )
            if others:
                lines.append("【その他異常】")
                for a in others[:5]:
                    ticker_or_sector = a.get("ticker") or a.get("sector_code", "?")
                    z   = a.get("z_score")
                    z_s = f" Z={z:.2f}" if z is not None else ""
                    # anomaly_type 別の補足情報
                    atype = a.get("anomaly_type", "")
                    if atype == "volume":
                        ratio = a.get("volume_ratio") or a.get("spike_ratio")
                        extra = f" {ratio:.1f}倍急増" if ratio else ""
                    elif atype == "price":
                        chg = a.get("price_change")
                        extra = f" {chg*100:+.1f}%" if chg is not None else ""
                    elif atype == "score":
                        extra = f" スコア急変"
                    else:
                        extra = ""
                    lines.append(
                        f"  [{a.get('severity','?').upper()}] "
                        f"{ticker_or_sector} ({a.get('label', a.get('sector_name',''))})"
                        f"{extra}{z_s} — {a.get('summary','')}"
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

    # ── [D-3新規] セクタースコア急変検知 ─────────────────────────────
    # score_history.json の前日スコアと今日のスコアを比較し、
    # SCORE_JUMP_THRESHOLD 点以上の変動があれば通知する。
    # 依存: score_history.json（1-6実装後に蓄積）
    if getattr(cfg, "ALERT_ON_SCORE_JUMP", True):
        jump_threshold = getattr(cfg, "ALERT_SCORE_JUMP_THRESHOLD", 15.0)
        sector_results_sj = results.get("sectors", {})

        try:
            import json as _json
            from pathlib import Path as _Path
            from datetime import datetime as _dt, timedelta as _td

            sh_path = _Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "score_history.json"
            if sh_path.exists() and sector_results_sj:
                history = _json.loads(sh_path.read_text(encoding="utf-8"))
                # 前日エントリを探す（直近7日以内）
                today_s = _dt.now().strftime("%Y-%m-%d")
                prev_entry = None
                for i in range(1, 8):
                    cand = (_dt.now() - _td(days=i)).strftime("%Y-%m-%d")
                    if cand in history and cand != today_s:
                        prev_entry = history[cand]
                        prev_date  = cand
                        break

                if prev_entry:
                    jump_lines = []
                    for code, res in sector_results_sj.items():
                        if not isinstance(res, dict):
                            continue
                        curr_score = res.get("composite_score", 0)
                        prev_raw   = prev_entry.get(code)
                        if prev_raw is None:
                            continue
                        prev_score = (prev_raw.get("score") if isinstance(prev_raw, dict)
                                      else float(prev_raw))
                        if prev_score is None:
                            continue
                        diff = curr_score - prev_score
                        if abs(diff) >= jump_threshold:
                            name  = res.get("sector_name_ja", code)
                            sign  = "+" if diff >= 0 else ""
                            icon  = "📈" if diff > 0 else "📉"
                            jump_lines.append(
                                (abs(diff), f"  {icon} {name}: {prev_score:.0f}→{curr_score:.0f}点 ({sign}{diff:.0f}pt)")
                            )
                    if jump_lines:
                        jump_lines.sort(key=lambda x: -x[0])
                        alerts.append({
                            "trigger":  "score_jump",
                            "title":    f"⚡ 業種スコア急変: {len(jump_lines)}業種（前日比±{jump_threshold:.0f}点超）",
                            "body":     f"  基準日: {prev_date}\n" + "\n".join(l for _, l in jump_lines[:8]),# type: ignore
                            "severity": "high" if any(d >= 20 for d, _ in jump_lines) else "medium",
                        })
        except Exception as _e:
            pass  # score_history未蓄積の場合は静かにスキップ

    # ── [2-5新規] FEDカレンダー接近アラート ──────────────────────────
    fed_cal = results.get("fed_calendar", {})
    if fed_cal:
        fomc = fed_cal.get("fomc", {})
        boj  = fed_cal.get("boj", {})
        fed_adj  = fed_cal.get("macro_adjustment", 0)
        fed_prob = fed_cal.get("fed", {})

        alert_lines = []

        # FOMC 3日前アラート
        fomc_days = fomc.get("days_until_meeting")
        if fomc_days is not None and fomc_days <= 3:
            cut  = fed_prob.get("cut_prob_25bp")
            hold = fed_prob.get("hold_prob")
            prob_s = f" (利下げ{cut:.0f}% / 据え置き{hold:.0f}%)" if cut is not None else ""
            alert_lines.append(f"  🏛️ FOMC {fomc_days}日後{prob_s}")

        # 日銀 3日前アラート
        boj_days = boj.get("days_until_meeting")
        if boj_days is not None and boj_days <= 3:
            alert_lines.append(f"  🏦 日銀金融政策決定会合 {boj_days}日後")

        # FED マクロ調整値が大きい場合
        if abs(fed_adj) >= 4:
            reason = fed_cal.get("adjustment_rationale", "")
            alert_lines.append(f"  📊 マクロ調整: {fed_adj:+.1f}pt（{reason}）")

        if alert_lines:
            alerts.append({
                "trigger":  "fed_calendar",
                "title":    "📅 中央銀行イベント接近",
                "body":     "\n".join(alert_lines),
                "severity": "medium",
            })
    # ── ⑭ 決算サプライズアラート [A-7] ────────────────────────────
    es_result = results.get("earnings_surprise", {})
    if es_result and getattr(cfg, "ENABLE_EARNINGS_SURPRISE", True):
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _es_mod = _imp.import_module("earnings_surprise")
            alert_text = _es_mod.format_surprise_alert(es_result, top_n=5)
            hit_pos = es_result.get("hit_pos", 0)
            hit_neg = es_result.get("hit_neg", 0)
            if hit_pos > 0 or hit_neg > 0:
                alerts.append({
                    "trigger":  "earnings_surprise",
                    "title":    f"📋 決算サプライズ: ポジ{hit_pos}社 / ネガ{hit_neg}社",
                    "body":     alert_text,
                    "severity": "medium" if hit_pos >= 2 else "info",
                })
        except Exception:
            pass
    # ── ⑮ 相関集中リスクアラート [3-2] ────────────────────────────
    ch_result = results.get("correlation_heatmap", {})
    if ch_result and not ch_result.get("skipped"):
        conc = ch_result.get("concentration", {})
        risk_level = conc.get("risk_level", "low")
        if risk_level in ("high", "medium"):
            risk_pairs = conc.get("risk_pairs", [])
            lines = [f"  {conc.get('summary', '')}"]
            for p in risk_pairs[:5]:
                lines.append(
                    f"  {p.get('label1', p['t1'])} ×"
                    f" {p.get('label2', p['t2'])}: r={p['corr']:.2f}"
                )
            alerts.append({
                "trigger":  "correlation_risk",
                "title":    f"⚠️ ポートフォリオ集中リスク検出（{len(risk_pairs)}ペア）",
                "body":     "\n".join(lines),
                "severity": "high" if risk_level == "high" else "medium",
            })
    # ── ⑯ 信用残高週報アラート [3-5] ────────────────────────────
    cd_result = results.get("credit_data", {})
    if cd_result and not cd_result.get("skipped"):
        sq = cd_result.get("flag_short_squeeze", [])
        oh = cd_result.get("flag_overheated", [])
        if sq or oh:
            alerts.append({
                "trigger":  "credit_data",
                "title":    f"📊 信用残高週報: 踏み上げ{len(sq)}件 / 過熱{len(oh)}件",
                "body":     cd_result.get("summary_text", "")[:500],
                "severity": "medium" if oh else "info",
            })
    # ── ⑰ 市場幅ダイバージェンスアラート [3-7] ─────────────────────
    mb_result = results.get("market_breadth", {})
    if mb_result:
        div   = mb_result.get("divergence", {})
        score = mb_result.get("breadth_score_jp", 50.0)
        if div.get("detected") and div.get("severity") == "high":
            alerts.append({
                "trigger":  "market_breadth_divergence",
                "title":    f"⚠️ 市場幅ダイバージェンス検出（JPスコア:{score:.0f}）",
                "body":     f"  {div.get('description','')}\n"
                            f"  上位業種の composite_score にペナルティ適用",
                "severity": "high",
            })
        elif score <= 30:
            alerts.append({
                "trigger":  "market_breadth_weak",
                "title":    f"🔴 市場幅悪化（スコア:{score:.0f}/100）",
                "body":     mb_result.get("summary_text", "")[:300],
                "severity": "medium",
            })
    # ── ⑱ インサイダー取引アラート [4-2] ──────────────────────────
    im_result = results.get("insider_monitor", {})
    if im_result and im_result.get("alert_items"):
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _im_mod = _imp.import_module("insider_monitor")
            alert_text = _im_mod.format_insider_alert(im_result)
            alerts.append({
                "trigger":  "insider_monitor",
                "title":    f"🔍 インサイダー活動: {len(im_result['alert_items'])}銘柄",
                "body":     alert_text,
                "severity": "medium",
            })
        except Exception:
            pass
    # ── ⑲ 配当権利確定アラート [4-3] ───────────────────────────────
    dc_result = results.get("dividend_calendar", {})
    if dc_result:
        upcoming = dc_result.get("upcoming_30d", [])
        urgent   = dc_result.get("urgent_7d", [])
        if upcoming:
            sev = "high" if urgent else "info"
            alerts.append({
                "trigger":  "dividend_calendar",
                "title":    f"📅 配当権利確定: {len(upcoming)}銘柄"
                            + (f"（🚨 {len(urgent)}銘柄が7日以内）" if urgent else ""),
                "body":     dc_result.get("alert_text", "")[:400],
                "severity": sev,
            })
    # ── ⑳ 季節アノマリーアラート [4-4] ─────────────────────────────
    as_result = results.get("anomaly_season", {})
    if as_result:
        adj = as_result.get("adjustments", {})
        n_sig = sum(1 for v in adj.values() if v != 0)
        if n_sig > 0:
            month = as_result.get("current_month", 0)
            from anomaly_season import MONTH_NAMES_JP, KNOWN_ANOMALIES
            month_name = MONTH_NAMES_JP.get(month, f"{month}月")
            known = KNOWN_ANOMALIES.get(month, "")
            alerts.append({
                "trigger":  "anomaly_season",
                "title":    f"📅 {month_name}アノマリー検出: {n_sig}業種に補正適用"
                            + (f"（{known}）" if known else ""),
                "body":     as_result.get("calendar_md", "")[:400],
                "severity": "info",
            })
    # ── ㉑ オプション市場極端値アラート [4-5] ────────────────────────
    om_result = results.get("options_monitor", {})
    if om_result:
        sent = om_result.get("sentiment", {})
        score  = sent.get("score", 50)
        label  = sent.get("label", "")
        signal = sent.get("signal", "neutral")
        # 極端値のみアラート（スコア < 25 or > 75）
        if score < 25 or score > 75:
            alerts.append({
                "trigger":  "options_sentiment",
                "title":    f"📊 市場センチメント極端値: {score:.0f}/100 {label}",
                "body":     om_result.get("summary_text", "")[:400],
                "severity": "high" if (score < 20 or score > 80) else "medium",
            })
    # ── ㉒ ブレイクアウトシグナルアラート [4-6] ────────────────────
    sr_result = results.get("support_resistance", {})
    breakouts = sr_result.get("top_breakout", [])
    if breakouts:
        items = " / ".join(
            f"{b.get('ticker','?')}(pos={b.get('position_pct',0):.0f}%)"
            for b in breakouts[:3]
        )
        alerts.append({
            "trigger":  "breakout_signal",
            "title":    f"🚀 ブレイクアウト候補: {len(breakouts)}銘柄 — {items}",
            "body":     "\n".join(
                f"  {b.get('ticker','?')} {b.get('label','')} "
                f"位置{b.get('position_pct',0):.0f}% bonus={b.get('sr_bonus',0):+.1f}pt"
                for b in breakouts[:5]
            ),
            "severity": "medium",
        })
    # ── 24 推論エンジン警告アラート [4-10] ──────────────────────
    ie_result = results.get("inference", {})
    if ie_result:
        phase = ie_result.get("phase_code", "neutral")
        if phase == "crisis":
            alerts.append({
                "trigger":  "inference_crisis",
                "title":    f"🚨 危機的状況 — {ie_result.get('phase_label','')}",
                "body":     ie_result.get("primary_action", {}).get("action", ""),
                "severity": "critical",
            })
        elif phase == "risk_off":
            action = ie_result.get("primary_action", {})
            if action.get("confidence", 0) >= 0.70:
                alerts.append({
                    "trigger":  "inference_risk_off",
                    "title":    f"⚠️ リスクオフ推奨 — {ie_result.get('phase_label','')}",
                    "body":     action.get("action", ""),
                    "severity": "high",
                })

    # ── ㉓ 統合シグナル極端値アラート [4-9] ─────────────────────
    sa_result = results.get("signal_aggregator", {})
    if sa_result and not sa_result.get("skipped"):
        top_buy  = sa_result.get("top_buy", [])
        top_avd  = sa_result.get("top_avoid", [])
        regime   = sa_result.get("market_regime", "")
        if len(top_buy) >= 4 and "リスクオン" in regime:
            alerts.append({
                "trigger":  "signal_agg_bullish",
                "title":    f"📊 統合シグナル強気: {', '.join(top_buy[:3])} など{len(top_buy)}業種",
                "body":     sa_result.get("summary_text", "")[:400],
                "severity": "medium",
            })
    # ── ㉔ 自己検証精度劣化アラート [5-5] ────────────────────────
    se_result = results.get("self_evaluator", {})
    if se_result and not se_result.get("skipped"):
        s = se_result.get("summary", {})
        if s.get("trend") == "degrading":
            alerts.append({
                "trigger":  "self_eval_degrading",
                "title":    f"⚠️ screener精度劣化検出 — "
                            f"通算勝率{s.get('lifetime_win_rate',0):.1%}",
                "body":     f"悪因子: {s.get('worst_factor','N/A')} "
                            f"(miss率{s.get('worst_factor_miss_rate',0):.0%})\n"
                            f"rule_optimizer.py の実行を推奨",
                "severity": "medium",
            })
    # ── ㉕ ポートフォリオリバランシングアラート [6-1] ─────────────
    po_result = results.get("portfolio_optimizer", {})
    if po_result and not po_result.get("skipped"):
        n_rebal = sum(1 for r in po_result.get("rebalance", [])
                      if r.get("action") != "HOLD")
        if n_rebal >= 2:
            alerts.append({
                "trigger":  "portfolio_rebalance",
                "title":    f"💼 リバランシング推奨: {n_rebal}銘柄に乖離",
                "body":     po_result.get("report_md", "")[:400],
                "severity": "medium",
            })
    # ── ⑮ [M-4] TDnet 触媒発生アラート ───────────────────────
    tdnet_result = results.get("tdnet", {})
    if tdnet_result and not tdnet_result.get("skipped"):
        # アクティビスト候補での触媒発生（最優先）
        activist_hits = tdnet_result.get("activist_hits", [])
        if activist_hits:
            lines = []
            for ev in activist_hits[:5]:
                lines.append(
                    f"  ⚡ {ev['name']}（{ev['ticker']}）"
                    f"activist={ev.get('activist_score',0):.0f}pt\n"
                    f"     📋 {ev['title'][:45]}"
                )
            alerts.append({
                "trigger":  "tdnet_activist_catalyst",
                "title":    f"🎯 触媒発生！アクティビスト候補 {len(activist_hits)}社",
                "body":     "\n".join(lines),
                "severity": "high",
            })

        # 自己株式取得（買い戻し）
        buyback = tdnet_result.get("buyback", [])
        if buyback and getattr(cfg, "ALERT_ON_BUYBACK", True):
            lines = [
                f"  💴 {ev['name']}（{ev['ticker']}）: {ev['title'][:40]}"
                for ev in buyback[:6]
            ]
            alerts.append({
                "trigger":  "tdnet_buyback",
                "title":    f"💴 自己株式取得 {len(buyback)}社（本日）",
                "body":     "\n".join(lines),
                "severity": "medium",
            })

        # 増配・特別配当
        div_up = tdnet_result.get("div_up", [])
        if div_up and getattr(cfg, "ALERT_ON_DIV_UP", True):
            lines = [
                f"  💰 {ev['name']}（{ev['ticker']}）: {ev['title'][:40]}"
                for ev in div_up[:6]
            ]
            alerts.append({
                "trigger":  "tdnet_div_up",
                "title":    f"💰 増配・特別配当 {len(div_up)}社（本日）",
                "body":     "\n".join(lines),
                "severity": "medium",
            })
    # ── ⑯ [I-2] 決算サプライズ高確率候補アラート ─────────────
    screener_res = results.get("screener", {})
    if screener_res and getattr(cfg, "ALERT_ON_HIGH_PROB_SURPRISE", True):
        high_prob = [
            c for c in screener_res.get("candidates", [])
            if c.get("is_high_prob_surprise") and c.get("pass_filters")
        ]
        neg_risk = [
            c for c in screener_res.get("candidates", [])
            if c.get("is_negative_risk")
        ]

        if high_prob:
            lines = []
            for c in sorted(high_prob,
                            key=lambda x: x.get("conservatism_score", 0),
                            reverse=True)[:5]:
                lines.append(
                    f"  🎯 {c.get('name','')[:14]}（{c['ticker']}）"
                    f" 加速+{c.get('acc_bonus',0):.0f}pt"
                    f" × 保守{c.get('conservatism_score',0):.0f}pt"
                )
            alerts.append({
                "trigger":  "high_prob_surprise",
                "title":    f"🎯 決算サプライズ高確率候補 {len(high_prob)}社",
                "body":     "\n".join(lines),
                "severity": "high",
            })

        if neg_risk:
            lines = [
                f"  🔴 {c.get('name','')[:14]}（{c['ticker']}）"
                for c in neg_risk[:3]
            ]
            alerts.append({
                "trigger":  "negative_surprise_risk",
                "title":    f"🔴 ネガティブサプライズリスク {len(neg_risk)}社",
                "body":     "\n".join(lines),
                "severity": "medium",
            })
     # ── ⑰ [R-3] miss_factor 閾値超え → ルール見直し推奨 ──────
    if getattr(cfg, "ENABLE_MISS_FACTOR_ALERT", True):
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _ij = _imp.import_module("investment_journal")
            miss_alerts = _ij.get_miss_factor_alerts(cfg)
            if miss_alerts:
                lines = [
                    f"  ⚠️ {a['factor']}: {a['count']}回"
                    for a in miss_alerts[:5]
                ]
                alerts.append({
                    "trigger":  "miss_factor_review",
                    "title":    f"📔 ルール見直し推奨: miss_factor {len(miss_alerts)}件",
                    "body":     "\n".join(lines),
                    "severity": "medium",
                })
        except Exception:
            pass
        # ── [GAP-1] ベイズ更新後ドリフトイベント ─────────────
        drift_events = results.get("drift_events", [])
        if drift_events:
            drift_lines = []
            for d in sorted(drift_events, key=lambda x: abs(x["drift"]), reverse=True)[:6]:
                icon = "📈" if d["direction"] == "up" else "📉"
                drift_lines.append(
                    f"  {icon} [{d['code']}] {d['prev']:.0f}→{d['today']:.0f}pt "
                    f"({'+' if d['drift']>0 else ''}{d['drift']:.0f}pt / {d['severity']})"
                )
            alerts.append({
                "trigger":  "bayesian_drift",
                "title":    f"⚡ スコア急変（ベイズ検出）: {len(drift_events)}業種",
                "body":     "\n".join(drift_lines),
                "severity": "high" if any(d["severity"] == "high"
                                          for d in drift_events) else "medium",
            })
    # ── ⑱ [P-4] 株主構成アラート ────────────────────────────────
    sa_result = results.get("shareholder", {})
    if sa_result and not sa_result.get("skipped"):
        # アクティビスト参戦（最優先）
        activist_list = sa_result.get("activist_entries", [])
        if activist_list:
            sa_results_map = sa_result.get("results", {})
            lines = []
            for tk in activist_list[:5]:
                sr = sa_results_map.get(tk, {})
                detail = sr.get("activist_detail", {})
                lines.append(
                    f"  ⚡ {tk}: {detail.get('alert', '')}"
                )
            alerts.append({
                "trigger":  "activist_entry_detected",
                "title":    f"⚡⚡ アクティビスト参戦疑い {len(activist_list)}社",
                "body":     "\n".join(lines),
                "severity": "high",
            })

        # 外国人持株急増
        foreign_spike = sa_result.get("foreign_spike", [])
        if foreign_spike and getattr(cfg, "ALERT_ON_FOREIGN_SPIKE", True):
            sa_results_map = sa_result.get("results", {})
            lines = []
            for tk in foreign_spike[:5]:
                sr = sa_results_map.get(tk, {})
                fp = sr.get('foreign_pct')
                fp_s = f"{fp:.0f}%" if isinstance(fp, (int, float)) else "?%"
                lines.append(
                    f"  🌏 {tk}: 外国人{fp_s}"
                    f" | スコア{sr.get('shareholder_score', 0):.0f}pt"
                )
            alerts.append({
                "trigger":  "foreign_investor_spike",
                "title":    f"🌏 外国人持株急増 {len(foreign_spike)}社",
                "body":     "\n".join(lines),
                "severity": "medium",
            })
        # ── [GAP-7] マクロ急変アラート ──────────────────────────────
    delta_alerts = results.get("macro_delta_alerts", [])
    if delta_alerts and getattr(cfg, "ALERT_ON_MACRO_DELTA", True):
        lines = []
        for da in delta_alerts[:5]:
            factor = da.get("key", "")
            icon   = "📈" if da.get("delta", 0) > 0 else "📉"
            lines.append(f"  {icon} {da.get('delta_str', '')}")
        alerts.append({
            "trigger":  "macro_delta",
            "title":    f"⚡ マクロ急変検出 {len(delta_alerts)}件（前日比）",
            "body":     "\n".join(lines),
            "severity": "high" if len(delta_alerts) >= 3 else "medium",
        })
        # ── ⑲ [I-5] 中計達成アラート ─────────────────────────────
    mt_result = results.get("midterm_tracker", {})
    if mt_result and not mt_result.get("skipped"):
        # 高信頼性中計発表
        new_plan_alerts = mt_result.get("new_plan_alerts", [])
        if new_plan_alerts:
            lines = []
            for pa in new_plan_alerts[:3]:
                alert_type = pa.get("alert_type", "")
                icon = "🌟" if alert_type == "high_reliability" else "⚠️"
                lines.append(
                    f"  {icon} {pa['ticker']}: {pa['event_title'][:35]}\n"
                    f"     {pa.get('note','')[:60]}"
                )
            alerts.append({
                "trigger":  "midterm_plan_announced",
                "title":    f"📋 中計発表 {len(new_plan_alerts)}社（達成率評価付き）",
                "body":     "\n".join(lines),
                "severity": "medium",
            })

        # 公約超過達成軌道
        overtrack = mt_result.get("overtrack_list", [])
        if overtrack:
            alerts.append({
                "trigger":  "midterm_overtrack",
                "title":    f"🚀 公約超過達成軌道 {len(overtrack)}社",
                "body":     "  " + " / ".join(overtrack[:5]),
                "severity": "info",
            })

        # 中計未達リスク
        risk_list = mt_result.get("risk_list", [])
        if risk_list:
            alerts.append({
                "trigger":  "midterm_risk",
                "title":    f"⚠️ 中計未達リスク {len(risk_list)}社",
                "body":     "  " + " / ".join(risk_list[:5]),
                "severity": "medium",
            })
            # ── ⑳ [RESEARCH-5] FinBERT感情急変アラート ─────────────────
    sentiment_result = results.get("sentiment", {})
    if sentiment_result and not sentiment_result.get("skipped"):
        by_sector = sentiment_result.get("by_sector", {})
        japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})
        # 感情悪化業種（deteriorating + negative_count多）
        deteriorating = [
            (code, d) for code, d in by_sector.items()
            if d.get("sentiment_trend") == "deteriorating"
            and d.get("negative_count", 0) >= 3
        ]
        if deteriorating:
            lines = []
            for code, d in sorted(deteriorating,
                                   key=lambda x: x[1].get("avg_sentiment", 0.5))[:4]:
                name = japan_sectors.get(code, {}).get("name_ja", code)
                lines.append(
                    f"  📉 {name}: "
                    f"感情スコア{d['avg_sentiment']:.2f} "
                    f"（ネガティブ{d['negative_count']}件）"
                )
            alerts.append({
                "trigger":  "sentiment_deteriorating",
                "title":    f"📉 ニュース感情悪化 {len(deteriorating)}業種（FinBERT）",
                "body":     "\n".join(lines),
                "severity": "medium",
            })

        # 感情改善業種（improving + positive_count多）
        improving = [
            (code, d) for code, d in by_sector.items()
            if d.get("sentiment_trend") == "improving"
            and d.get("positive_count", 0) >= 4
        ]
        if improving:
            lines = []
            for code, d in sorted(improving,
                                   key=lambda x: -x[1].get("avg_sentiment", 0.5))[:4]:
                name = japan_sectors.get(code, {}).get("name_ja", code)
                lines.append(
                    f"  📈 {name}: "
                    f"感情スコア{d['avg_sentiment']:.2f} "
                    f"（ポジティブ{d['positive_count']}件）"
                )
            alerts.append({
                "trigger":  "sentiment_improving",
                "title":    f"📈 ニュース感情改善 {len(improving)}業種（FinBERT）",
                "body":     "\n".join(lines),
                "severity": "info",
            })
    # ── [US-3] US 8-K イベントアラート ────────────────────────────
    k8_result = results.get("us_8k", {})
    if k8_result and not k8_result.get("skipped"):
        screener_hits = k8_result.get("screener_hits", [])
        earnings      = k8_result.get("earnings_tickers", [])
        ceo_chg       = k8_result.get("ceo_change_tickers", [])
        if screener_hits:
            alerts.append({
                "trigger":  "8k_screener_hit",
                "title":    f"⚡ 8-K × screener対象銘柄 {len(screener_hits)}社",
                "body":     "  " + " / ".join(screener_hits[:5]),
                "severity": "high",
            })
        if ceo_chg:
            alerts.append({
                "trigger":  "8k_ceo_change",
                "title":    f"👔 CEO交代 8-K {len(ceo_chg)}社",
                "body":     "  " + " / ".join(ceo_chg[:5]),
                "severity": "medium",
            })
    # ── [RESEARCH-2] ファクター・クラウディングアラート ──────────────
    fcm_result = results.get("factor_crowding", {})
    if fcm_result and not fcm_result.get("skipped"):
        if fcm_result.get("unwind_alert"):
            prev_c = fcm_result.get("prev_avg_corr", 0)
            curr_c = fcm_result.get("avg_correlation", 0)
            alerts.append({                      # type: ignore
                "trigger":  "crowding_unwind",
                "title":    "🚨 クラウディング解消売りアラート（RESEARCH-2）",
                "body":     (
                    f"  前週相関: {prev_c:.3f} → 今週: {curr_c:.3f}\n"
                    f"  ※ 解消売り連鎖の可能性。ポジション縮小を検討。\n"
                    f"  上位銘柄: {' / '.join(fcm_result.get('top_tickers', [])[:5])}"
                ),
                "severity": "high",
            })
        elif fcm_result.get("crowding_alert"):
            score = fcm_result.get("crowding_score", 0)
            alerts.append({              # type: ignore
                "trigger":  "crowding_alert",
                "title":    f"⚠️ ファクタークラウディング検知（score={score}）",
                "body":     (
                    f"  avg_corr={fcm_result.get('avg_correlation',0):.3f} "
                    f"（閾値0.60超）\n"
                    f"  上位銘柄: {' / '.join(fcm_result.get('top_tickers', [])[:5])}"
                ),
                "severity": "medium",
            })
        # ファクター別アラート
        for factor_name, f_data in fcm_result.get("factor_crowding", {}).items():
            if isinstance(f_data, dict) and f_data.get("alert"):
                alerts.append({      # type: ignore
                    "trigger":  f"crowding_{factor_name}",
                    "title":    f"🔍 [{f_data.get('label',factor_name)}] クラウディング",
                    "body":     f"  avg_corr={f_data.get('avg_corr',0):.3f}",
                    "severity": "low",
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
    
    cfg = None # スコープ外で使うために初期化
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            
            # --- ここで spec と loader の存在を確定させる ---
            if spec is not None and spec.loader is not None:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break
            # ----------------------------------------------

    if cfg:
        print("=== テスト通知送信 ===")
        r = send_test_notification(cfg)
        print(f"LINE: {r['line']} / Gmail: {r['gmail']}")
        if r["errors"]:
            for e in r["errors"]:
                print(f"  エラー: {e}")
    else:
        print("ERROR: 設定ファイル(cfg)を読み込めませんでした。")
