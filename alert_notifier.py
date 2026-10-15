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



def _tk(t) -> str:
    """「ティッカー 銘柄名」（名前が分からなければティッカーだけ）。2026-10-05"""
    try:
        from name_resolver import name_of
        n = name_of(t)
    except Exception:
        n = ""
    return f"{t} {n}" if n else str(t)


def _jname(c: dict) -> str:
    """銘柄名（日本株＝日本語名・米国株＝米国株名）。2026-10-05: yfinance の英語名を出していた"""
    try:
        from name_resolver import name_of
        n = name_of((c or {}).get("ticker") or (c or {}).get("symbol") or "")
    except Exception:
        n = ""
    return n or str((c or {}).get("name") or "")
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── 審議キューの機械可読マーカー ────────────────
# Claude の予定済みタスクが Gmail MCP でこのメールを読み、審議対象を
# 取り出すための固定プレフィックス。**変更するとタスク側の抽出が壊れる**
# ため、変えるときは以下も同時に更新すること:
#   ~/.claude/scheduled-tasks/stock-analyzer-deliberation/SKILL.md
#   tests/test_deliberation_queue.py
# 週次レポート（weekly_report_generator）も同じ文字列を使う。
DELIBERATION_QUEUE_MARKER = "DELIBERATION_QUEUE_V1:"


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
            
    # ── ④b イベントホライズン事前アラート [EVT-1] ────────────────
    evh = results.get("event_horizon", {})
    for a in evh.get("alerts", []):
        if isinstance(a, dict) and a.get("title"):
            alerts.append({
                "trigger":  a.get("trigger", "event_horizon"),
                "title":    a["title"],
                "body":     a.get("body", ""),
                "severity": a.get("severity", "medium"),
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
                        # 投資に関係しない記事（事件・事故など）は出さない（news_pipeline の investment_relevant）。
            def _relevant(a):
                return a.get("investment_relevant") is not False
            for code, _ in sorted_secs[:5]:
                arts = [a for a in by_sector.get(code, []) if _relevant(a)]
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
                if not _relevant(a):
                    continue
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
                ad     = t.get("arxiv_detail") or {}
                lines.append(
                    f"  {icon} {t['theme_name']}({etfs}): 熱量{t['heat_score']:.0f}点"
                    f"（ETF {t.get('etf_momentum_score', 0):.0f}点 × 論文 {t.get('arxiv_trend_score', 0):.0f}点）")
                                # 論文の中身が分かるよう件数の推移と代表論文を出す
                if ad.get("source") == "arxiv_api":
                    lines.append(f"      論文 直近4週{ad.get('recent_4w', 0)}件"
                                 f"（前4週{ad.get('prev_4w', 0)}件・{ad.get('growth_rate', 0):+.0f}%）"
                                 + ("・件数が少なく参考値" if (ad.get("confidence") or 1) < 1 else ""))
                    for pp in (ad.get("papers") or [])[:2]:
                        lines.append(f"      ・{pp.get('title', '')[:70]}（{pp.get('published', '')}）")
                elif ad.get("growth_rate") is not None:
                    lines.append(f"      論文 {ad['growth_rate']:+.0f}%（週次の論文プールから推定）")
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

                        # 代表銘柄の財務ハイライト。その業種のスクリーナー通過上位（総合点順）を
                        # ALERT_SECTOR_TOP_COMPANIES 件出す（BQ の業種一覧順だと毎回同じ銘柄になる）。
            _top_n = int(getattr(cfg, "ALERT_SECTOR_TOP_COMPANIES", 3))
            _scr = [c for c in ((results.get("screener") or {}).get("candidates") or [])
                    if c.get("pass_filters") and str(c.get("sector_code")) == str(code)]
            _scr.sort(key=lambda c: (-(c.get("composite_score") or 0), -(c.get("base_score") or 0)))
            for c in _scr[:_top_n]:
                _dy = c.get("dividend_yield")
                _pp = [f"{c.get('composite_score', 0):.0f}点"]
                if c.get("per"):
                    _pp.append(f"PER:{c['per']:.1f}x")
                if c.get("pbr"):
                    _pp.append(f"PBR:{c['pbr']:.1f}x")
                if c.get("roe") is not None:
                    _pp.append(f"ROE:{c['roe']:.0f}%")
                if _dy:
                    _pp.append(f"配当:{_dy*100:.1f}%")
                line += f"\n    📌 {_jname(c)[:16]}（{c.get('ticker', '')}）: {' | '.join(_pp)}"
            top_cos = [] if _scr else r.get("top_companies", [])
            for c in top_cos[:_top_n]:
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
                                                # 有報の個別リスク見出しを上位3件並べる（本文の先頭文は全社共通の前置き）
                        line += f"\n    ⚠️  主要リスク: {' / '.join(str(x) for x in risks[:3])[:80]}"
                fin = tdata.get("financials", {})
                if fin:
                    rev_cagr = fin.get("revenue_cagr")
                    if rev_cagr is not None:
                        line += f"\n    📈 売上CAGR(3年): {rev_cagr*100:+.1f}%/年"
                gov = tdata.get("gov_meta", {})
                if gov:
                                        # submit_date はキーがあって値が None のことがある。値が無いなら行ごと出さない。
                    _submit = gov.get("submit_date")
                    if _submit:
                        line += f"\n    📋 有報提出: {_submit}"

            lines.append(line)

        if lines:
            alerts.append({
                "trigger":  "top_sectors",
                "title":    "🇯🇵 注目業種トップ3（日本株・本日）",
                "body":     "\n".join(lines),
                "severity": "info",
            })

        # ── ⑥-2 米国株サマリー（us_screener / us_sectors） ──────────────────
    if getattr(cfg, "ALERT_ON_US_SUMMARY", True):
        try:
            _us_lines = []

            # (a) USセクター騰落（us_sector_pipeline）
            _us_sec = results.get("us_sectors", {}) or {}
            _sec_list = _us_sec.get("sector_list", []) if not _us_sec.get("skipped") else []
            _ok = [r for r in _sec_list
                   if isinstance(r, dict) and r.get("score_result", {}).get("fetch_ok")]
            if _ok:
                                # 全セクター（11）を出す
                _us_lines.append(f"  【セクター】{len(_ok)}セクター（点数順）")
                for r in sorted(_ok, key=lambda r: -((r.get("score_result") or {}).get("score") or 0)):
                    _s = r.get("sector", {}) or {}
                    _sr = r.get("score_result", {}) or {}
                    _nm = _s.get("name_ja") or _s.get("name_en") or _s.get("etf", "")
                    _risk = _sr.get("risk_level", "")
                    _risk_s = f" | リスク:{_risk}" if _risk else ""
                    _us_lines.append(
                        f"    {_nm}（{_s.get('etf','')}）: {_sr.get('score', 0):.0f}点{_risk_s}")

            # (b) USスクリーナー通過上位（us_stock_screener）
            _us_scr = results.get("us_screener", {}) or {}
            if not _us_scr.get("skipped"):
                _picks = _us_scr.get("top_picks") or _us_scr.get("passed") or []
                if _picks:
                    _pn = int(getattr(cfg, "ALERT_US_TOP_PICKS", 10))
                    _us_lines.append(
                        f"  【スクリーニング】{_us_scr.get('pass_count', 0)}社通過 "
                        f"/ {_us_scr.get('screened_count', 0)}社中")
                    for c in _picks[:_pn]:
                        if not isinstance(c, dict):
                            continue
                        _nm = str(_jname(c) or c.get("ticker", ""))[:20]
                        _pp = []
                        if c.get("per"):
                            _pp.append(f"PER:{c['per']:.1f}x")
                        if c.get("roe"):
                            _pp.append(f"ROE:{c['roe']*100:.0f}%")
                        if c.get("div_yield"):
                            _pp.append(f"配当:{c['div_yield']*100:.1f}%")
                        if c.get("analyst_upside") is not None:
                            _pp.append(f"目標乖離:{c['analyst_upside']*100:+.0f}%")
                        _detail = f" | {' | '.join(_pp)}" if _pp else ""
                        _us_lines.append(
                            f"    📌 {_nm}（{c.get('ticker','')}）: "
                            f"{c.get('composite_score', 0):.0f}点"
                            f" | {c.get('sector','')}{_detail}")

            if _us_lines:
                alerts.append({
                    "trigger":  "us_summary",
                    "title":    "🇺🇸 米国株サマリー（本日）",
                    "body":     "\n".join(_us_lines),
                    "severity": "info",
                })
        except Exception as _us_err:
            print(f"  ⚠️  [alert] 米国株サマリー生成失敗: {str(_us_err)[:80]}")

        # ── ⑥-3 審議キュー候補 ──────────────────
        # 隔週のB部門審議（E3）へ次に上げる銘柄を、毎日メールで見えるようにする。
        # 並び順は deliberation_scheduler.build_deliberation_queue と同一なので、
        # ここに出る先頭銘柄が隔週ジョブで実際に選定される銘柄になる。
    if getattr(cfg, "ALERT_ON_DELIBERATION_QUEUE", True):
        try:
            from deliberation_scheduler import get_queue_for_report
            _dq = get_queue_for_report(
                cfg, results,
                top_n=int(getattr(cfg, "DELIBERATION_QUEUE_TOP_N", 5)))
            _q = _dq.get("queue", [])
                        # 前回からの入れ替わり（連続日数・外れた銘柄と理由）
            _chg = {}
            try:
                from deliberation_scheduler import (queue_changes, resolve_deliberated,
                                                    _load_screener_candidates)
                from performance_tracker import load_screener_history
                _already, _ = resolve_deliberated(cfg)
                _today_c = ((results.get("screener") or {}).get("candidates")
                            or _load_screener_candidates(cfg))
                _chg = queue_changes(
                    load_screener_history(cfg), _q, _already,
                    top_n=int(getattr(cfg, "DELIBERATION_QUEUE_TOP_N", 5)),
                    today=datetime.now().strftime("%Y-%m-%d"), today_cands=_today_c)
            except Exception as _qc_err:
                print(f"  ⚠️  [alert] 審議キューの入れ替わり計算失敗: {str(_qc_err)[:80]}")
            if _q:
                _ql = []
                for _i, _c in enumerate(_q, 1):
                    _mark = "▶" if _i == 1 else " "
                    _sc = _c.get("composite_score")
                    _sc_s = f"{float(_sc):.0f}点" if _sc is not None else "—"
                    _nm = str(_jname(_c) or _c.get("ticker", ""))[:20]
                    _extra = []
                    if _c.get("sector_name"):
                        _extra.append(str(_c["sector_name"]))
                    elif _c.get("sector_code"):
                        _extra.append(str(_c["sector_code"]))
                    if _c.get("strategy_label"):
                        _extra.append(str(_c["strategy_label"]))
                    _ex_s = f" | {' / '.join(_extra)}" if _extra else ""
                    _stk = (_chg.get("streak") or {}).get(_c.get("ticker"))
                    _stk_s = ("" if not _stk else "（新規）" if _stk == 1
                              else "（2週間以上連続）" if _stk > 14 else f"（{_stk}日連続）")
                    _ql.append(
                        f"  {_mark} {_i}. {_nm}（{_c.get('ticker','')}）: "
                        f"{_sc_s}{_stk_s}{_ex_s}")
                    try:
                        from score_trail import summary as _trail_sum
                        _tr = _trail_sum(_c)
                        if _tr:
                            _ql.append(f"       点数の経路: {_tr}")
                    except Exception:
                        pass
                for _d in (_chg.get("dropped") or []):
                    _ql.append(f"  ↓ 外れた: {_jname(_d)[:20]}（{_d['ticker']}）"
                               f" — {_d['reason']}")
                if _chg.get("prev_date") and not _chg.get("new") and not _chg.get("dropped"):
                    _ql.append(f"  ＝ 前回（{_chg['prev_date']}）から入れ替わりなし")
                _ql.append("")
                _ql.append(f"  ▶ = 次回の審議で選定される銘柄"
                           f"（隔週 / {_dq.get('interval_days', 14)}日間隔）")
                _ql.append(f"  審議済み除外: {_dq.get('already_count', 0)}銘柄"
                           f"（出所: {_dq.get('source', '不明')}）")
                if _dq.get("partial"):
                    # 除外集合が台帳と最近照合されていないときだけ明示する。
                    # Actions からは vault の 40_判断台帳.md が読めず、Mac の obsidian_sync が毎日
                    # 照合して上げるミラーで代用している。照合が止まると審議済みが混じり得る。
                    _age = _dq.get("mirror_age_days")
                    if _age is not None:
                        _ql.append(f"  ⚠️ 審議済みの一覧が{_age}日前から判断台帳と照合されていません"
                                   "（Mac の同期が止まっている可能性）。審議済み銘柄が混じる可能性があります")
                    else:
                        _ql.append("  ⚠️ 判断台帳（正本）とそのミラーを参照できていないため、"
                                   "審議済み銘柄が混じる可能性があります")
                                # ── 機械可読マーカー ─────────────────
                                # Claude の予定済みタスクが Gmail MCP でこのメールを読み、
                                # 審議対象を取り出すための行。整形済みの日本語本文を
                                # スクレイピングすると書式変更で壊れるため、
                                # 「固定プレフィックス＋カンマ区切りティッカー」だけを置く。
                                # 本文は HTML で送られるが、この形なら正規表現で拾える。
                _ql.append("")
                _ql.append(f"{DELIBERATION_QUEUE_MARKER} "
                           + ",".join(str(c.get("ticker", "")) for c in _q))
                alerts.append({
                    "trigger":  "deliberation_queue",
                    "title":    f"🗳️ 審議キュー候補 {len(_q)}銘柄",
                    "body":     "\n".join(_ql),
                    "severity": "info",
                })
        except Exception as _dq_err:
            print(f"  ⚠️  [alert] 審議キュー生成失敗: {str(_dq_err)[:80]}")

        # ── ⑦ 高配当銘柄アラート ────────────────────────────────
        # 日本株スクリーナーの全取得銘柄と米国株スクリーナーから探す。
        # yfinance の配当利回りは %値のことがあるので比率にそろえてから比べる。
    if getattr(cfg, "ALERT_ON_HIGH_DIVIDEND", True):
        _hd = build_high_dividend_alert(cfg, results, sector_results)
        if _hd:
            alerts.append(_hd)

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
                        f"  {_jname(c)[:16]}（{c.get('ticker','')}）: 推奨{rec:.1f} / 目標株価乖離{upside*100:+.0f}%"
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
                        f"  [{sec_name}] {_jname(c)[:16]}（{c.get('ticker','')}）: {' / '.join(flags)}"
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
                                        # anomaly_detector は price_change を既に「%値」で格納する（ここで100倍しない）。
                    if a.get("price_change"):
                        sub.append(f"価格{a['price_change']:+.1f}%")
                    if a.get("volume_ratio"):
                        sub.append(f"出来高{a['volume_ratio']:.1f}倍")
                    detail = f"({', '.join(sub)})" if sub else ""
                    lines.append(
                        f"  {_tk(a.get('ticker','?'))} ({a.get('label','')}) "
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
                                                # 出来高異常は Z の絶対値で拾うので、減少側もここに来る（「急増」と決めつけない）。
                        _vol_word = "急増" if (ratio or 0) >= 1 else "急減"
                        extra = f" {ratio:.1f}倍{_vol_word}" if ratio else ""
                    elif atype == "price":
                        chg = a.get("price_change")
                        # price_change は既に%値（上の複合異常と同じ理由で *100 しない）
                        extra = f" {chg:+.1f}%" if chg is not None else ""
                    elif atype == "score":
                        # 業種スコアの水準・業種間の外れ値（前日比の急変は別アラート）
                        extra = " スコア水準"
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

    # ── [D-3] anomaly_detector が独立判定したセクタースコア急変 ──────
    # anomaly モード外でも通知機能を維持するため、結果が無い場合だけ同じ判定APIを呼ぶ。
    try:
        jump_result = (results.get("anomaly", {}) or {}).get("score_jump_result")
        if not isinstance(jump_result, dict):
            from anomaly_detector import detect_score_jumps as _detect_score_jumps
            jump_result = _detect_score_jumps(cfg, results)
        jump_items = jump_result.get("alerts", [])
        if jump_items:
            threshold = jump_result.get("threshold", 15.0)
            lines = []
            for item in jump_items[:8]:
                icon = "📈" if item.get("diff", 0) > 0 else "📉"
                lines.append(
                    f"  {icon} {item.get('sector_name', item.get('sector_code',''))}: "
                    f"{item.get('previous_score',0):.1f}→{item.get('current_score',0):.1f}点 "
                    f"({item.get('diff',0):+.1f}pt)"
                )
            alerts.append({
                "trigger":  "score_jump",
                "title":    f"⚡ 業種スコア急変: {len(jump_items)}業種（±{threshold:.1f}点以上）",
                "body":     f"  基準日: {jump_result.get('previous_date','')}\n" + "\n".join(lines),
                "severity": "high" if any(
                    item.get("severity") == "high" for item in jump_items
                ) else "medium",
                "_score_jump_items": jump_items,
            })
    except Exception:
        pass

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
            f"{_tk(b.get('ticker','?'))}(pos={b.get('position_pct',0):.0f}%)"
            for b in breakouts[:3]
        )
        alerts.append({
            "trigger":  "breakout_signal",
            "title":    f"🚀 ブレイクアウト候補: {len(breakouts)}銘柄 — {items}",
            "body":     "\n".join(
                f"  {_tk(b.get('ticker','?'))} {b.get('label','')} "
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
                # ネガティブサプライズ通知は ALERT_ON_NEGATIVE_RISK で別に止められる。
        neg_risk = [
            c for c in screener_res.get("candidates", [])
            if c.get("is_negative_risk")
        ] if getattr(cfg, "ALERT_ON_NEGATIVE_RISK", True) else []

                # 決算が近い順に最大10社、判定理由（EPS加速×会社予想の保守度・過去の上振れ）と次回決算日を付ける
        _n = int(getattr(cfg, "ALERT_SURPRISE_TOP_N", 10))

        def _surprise_lines(rows, icon):
            rows = sorted(rows, key=lambda x: (x.get("days_to_earnings") is None
                                               or x.get("days_to_earnings") < 0,
                                               x.get("days_to_earnings") or 0,
                                               -(x.get("conservatism_score") or 0)))
            out = []
            for c in rows[:_n]:
                d = c.get("days_to_earnings")
                when = (f"決算 {c['next_earnings']}（あと{d}日）" if c.get("next_earnings") and d is not None and d >= 0
                        else "次回決算日未取得")
                out.append(f"  {icon} {_jname(c)[:16]}（{c['ticker']}）| {when}")
                if c.get("surprise_reason"):
                    out.append(f"      {c['surprise_reason']}")
            return out

        if high_prob:
            alerts.append({
                "trigger":  "high_prob_surprise",
                "title":    f"🎯 決算サプライズ高確率候補 {len(high_prob)}社",
                "body":     "\n".join(_surprise_lines(high_prob, "🎯")),
                "severity": "high",
            })

        if neg_risk:
            alerts.append({
                "trigger":  "negative_surprise_risk",
                "title":    f"🔴 ネガティブサプライズリスク {len(neg_risk)}社",
                "body":     "\n".join(_surprise_lines(neg_risk, "🔴")),
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
                    f"  {icon} {_tk(pa['ticker'])}: {pa['event_title'][:35]}\n"
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
    # ── [Pearl Ph1] 光る原石 SS ティア即時通知 ────────────────────
    # 条件: pearl_score >= PEARL_MIN_SCORE_ALERT(80) AND
    #       市価総額 <= PEARL_MCAP_MAX_ALERT_B(500億円)
    if getattr(cfg, "ENABLE_PEARL_SCORE", True):
        try:
            _pearl_min   = float(getattr(cfg, "PEARL_MIN_SCORE_ALERT",   80.0))
            _pearl_mcap  = float(getattr(cfg, "PEARL_MCAP_MAX_ALERT_B", 500.0))
            _screener    = results.get("screener", {})
            _passed      = _screener.get("passed",
                           _screener.get("candidates", []))
            _ss_stocks   = [
                c for c in _passed
                if float(c.get("pearl_score", 0)) >= _pearl_min
                and (c.get("market_cap_b") is None
                     or float(c.get("market_cap_b", 999)) <= _pearl_mcap)
            ]
            _ss_stocks.sort(
                key=lambda x: float(x.get("pearl_score", 0)), reverse=True
            )
            if _ss_stocks:
                lines = []
                for _s in _ss_stocks[:5]:
                    _tier = _s.get("pearl_tier", "SS")
                    _kap  = _s.get("market_cap_b")
                    _kap_str = f"{_kap:.0f}億" if _kap is not None else "不明"
                    lines.append(
                        f"  💎 [{_tier}] "
                        f"{_tk(_s.get('ticker', _s.get('code','')))} "
                        f"Pearl:{float(_s.get('pearl_score',0)):.1f}pt "
                        f"時価総額:{_kap_str}"
                    )
                    # D1/D3/D4 の内訳も追記
                    _d1 = _s.get("d1_score") or _s.get("pearl_d1")
                    _d3 = _s.get("d3_score") or _s.get("pearl_d3")
                    _d4 = _s.get("d4_score") or _s.get("pearl_d4")
                    if _d1 is not None:
                        lines.append(
                            f"    D1方向性:{float(_d1):.0f} "
                            f"D3利益品質:{float(_d3 or 0):.0f} "
                            f"D4発見Gap:{float(_d4 or 0):.0f}"
                        )
                alerts.append({
                    "trigger":  "pearl_ss_alert",
                    "title":    f"💎 光る原石 SS ティア {len(_ss_stocks)}銘柄発見"
                                f"（Pearl≥{_pearl_min:.0f} / 時価総額≤{_pearl_mcap:.0f}億）",
                    "body":     "\n".join(lines),
                    "severity": "high",   # LINE即時通知対象
                })
        except Exception:
            pass
        # ── ⑭ 重要議員取引シグナルアラート（Policy Ph1）─────
    if getattr(cfg, "ENABLE_POLITICIAN_MONITOR", True):
        pm_result = results.get("politician_monitor", {})
        if pm_result and not pm_result.get("skipped"):
            min_score = int(getattr(cfg, "ALERT_POLITICIAN_MIN_SCORE", 60))
            high_trades = pm_result.get("high_score_trades", [])
            if high_trades:
                lines = []
                for t in high_trades[:5]:
                    direction = "買" if t.get("direction") == "buy" else "売"
                                        # 取引日を併記する（別日の同銘柄取引の区別・古い取引かどうかの判別用）。
                    _td = t.get("transaction_date", "")
                    lines.append(
                        f"  [{t.get('score', 0):.0f}pt] "
                        f"{t.get('politician', '')[:16]} "
                        f"{direction}: {t.get('asset', '')[:30]}"
                        + (f"（取引日 {_td}）" if _td else "")
                    )
                jp_signals = pm_result.get("jp_sector_signals", {})
                if jp_signals:
                    lines.append(
                        f"  🇯🇵 JP波及シグナル: {len(jp_signals)}業種に補正"
                    )
                alerts.append({
                    "trigger":  "politician_signal",
                    "title":    f"🏛️ 重要議員取引 {len(high_trades)}件"
                                f"（スコア≥{min_score}）",
                    "body":     "\n".join(lines),
                    "severity": "high",
                })

        # ── ⑲ screener急増業種アラート（Idea2-C）─────────────
    sh_result = results.get("screener_heatmap", {})
    if sh_result and not sh_result.get("skipped"):
        surge_sectors = sh_result.get("surge_sectors", [])
        if len(surge_sectors) >= 2:
            sector_names = {
                code: info.get("name_ja", code)
                for code, info in getattr(cfg, "JAPAN_SECTORS", {}).items()
            }
            lines = []
            for s in surge_sectors[:5]:
                code = s.get("sector_code", "")
                name = sector_names.get(code, code)
                lines.append(
                    f"  🚀 {name}({code}): "
                    f"今週{s.get('current', 0)}件 "
                    f"(前4週平均{s.get('avg_4w', 0):.1f}件 "
                    f"+{s.get('change_rate', 0):.0f}%)"
                )
            alerts.append({
                "trigger":  "screener_surge",
                "title":    f"🚀 screener急増業種 {len(surge_sectors)}業種"
                            f"（変化率+50%以上）",
                "body":     "\n".join(lines),
                "severity": "medium",
            })
        # ==============================================================
        # [FE-2] トリガー⑳: アナリスト修正×Ignition Cat-B 重複
        # 条件: セクター平均 revision_score > 0.5 かつ Ignition Cat-B detected
        # チャネル: Gmail 週次通知（severity: medium）
        # ==============================================================
    try:
        screener_res = results.get("screener", {})
        candidates   = (screener_res.get("passed")
                        or screener_res.get("candidates", []))
        ignition_res = results.get("ignition", {})
        ign_alerts   = ignition_res.get("alerts", [])

        # Ignition Cat-B が detected のセクターコードセットを構築
        catb_sectors = {
            str(a.get("sector_code", ""))
            for a in ign_alerts
            if a.get("category_b", {}).get("detected")
        }

        if catb_sectors and candidates:
            # セクター別 revision_score 平均を算出
            from collections import defaultdict as _dd
            _rev_by_sec: dict = _dd(list)
            for _c in candidates:
                _sc = str(_c.get("sector_code", ""))
                _rv = _c.get("revision_score")
                if _rv is not None:
                    _rev_by_sec[_sc].append(float(_rv))

            _hit_lines = []
            for _sc, _rvs in _rev_by_sec.items():
                if _sc not in catb_sectors:
                    continue
                _avg_rev = sum(_rvs) / len(_rvs)
                if _avg_rev > 0.5:
                    _sec_name = str(
                        results.get("sectors", {})
                        .get(_sc, {}).get("sector_name_ja", _sc)
                    )
                    _hit_lines.append(
                        f"  📈 {_sec_name}({_sc}): "
                        f"avg revision={_avg_rev:+.2f}pt"
                        f" / Ignition Cat-B検出"
                    )

            if _hit_lines:
                alerts.append({
                    "trigger":  "wave2_revision_ignition",
                    "title":    f"📊 [Wave2-⑳] アナリスト修正×Ignition重複"
                                f"（{len(_hit_lines)}業種）",
                    "body":     "\n".join(_hit_lines),
                    "severity": "medium",
                    "channel":  "gmail",   # Gmail 週次通知
                })
    except Exception:
        pass

        # ==============================================================
        # [FE-4] トリガー㉑: Ignition Detector 収束シグナル
        # 条件A: convergence_score ≥ 3.0    → LINE 即時通知（severity: high）
        # 条件B: ≥ 3.5 かつ is_critical True → 最高警戒LINE（severity: critical）
        # ==============================================================
    try:
        ignition_res = results.get("ignition", {})
        ign_alerts   = ignition_res.get("alerts", [])

        if not ign_alerts:
            # results に ignition がない場合は ignition_alerts.json から直接ロード
            try:
                from ignition_detector import load_current_alerts as _lca
                ign_alerts = _lca(cfg)
            except Exception:
                ign_alerts = []

        min_score  = float(getattr(cfg, "IGNITION_MIN_SCORE_LINE",      3.0))
        crit_score = float(getattr(cfg, "IGNITION_MAX_SCORE_LINE_CRITICAL", 3.5))

        _normal_lines   = []
        _critical_lines = []

        for _a in ign_alerts:
            _csc  = float(_a.get("convergence_score", 0.0))
            _sn   = _a.get("sector_name", _a.get("sector_code", ""))
            _sc   = _a.get("sector_code", "")
            _is_c = _a.get("is_critical", False)

            _cat_a = _a.get("category_a", {}).get("score", 0)
            _cat_b = _a.get("category_b", {}).get("score", 0)
            _cat_c = _a.get("category_c", {}).get("score", 0)
            _detail = (
                f"A(資金)={_cat_a:.2f} "
                f"B(ナラ)={_cat_b:.2f} "
                f"C(価格)={_cat_c:.2f}"
            )

            if _is_c and _csc >= crit_score:
                _critical_lines.append(
                    f"  🚨 {_sn}({_sc}): convergence={_csc:.2f} [{_detail}]"
                    f" ← 14日以内2回目検出"
                )
            elif _csc >= min_score:
                _normal_lines.append(
                    f"  🔥 {_sn}({_sc}): convergence={_csc:.2f} [{_detail}]"
                )

        # 通常 Ignition アラート
        if _normal_lines:
            alerts.append({
                "trigger":  "wave2_ignition_convergence",
                "title":    f"🔥 [Wave2-㉑] Ignition収束シグナル"
                            f"（{len(_normal_lines)}業種）",
                "body":     "\n".join(_normal_lines),
                "severity": "high",
                "channel":  "line",   # LINE 即時通知
            })

        # 最高警戒アラート（critical）
        if _critical_lines:
            alerts.append({
                "trigger":  "wave2_ignition_critical",
                "title":    f"🚨🚨 [Wave2-㉑最高警戒] Ignition反復検出"
                            f"（{len(_critical_lines)}業種）",
                "body":     "\n".join(_critical_lines),
                "severity": "high",
                "channel":  "line",   # 最高警戒 LINE
            })

    except Exception:
        pass

# ==============================================================
    # [Independence Ph1] トリガー⑰: Latent Pearl プロモーション
    # 条件: promoted_score ≥ 0.7 → LINE 即時通知
    # ==============================================================
    try:
        from latent_pearl_manager import get_promoted_tickers as _get_promo
        _promoted_tickers = _get_promo(cfg)
        if not _promoted_tickers:
            pass
        else:
                        # get_promoted_tickers は list[str] を返す。詳細は latent_pearl_cache から取って通知を組み立てる
            import json as _jal
            from pathlib import Path as _pal
            _lp_path_al = (
                _pal(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
                / "latent_pearl_cache.json"
            )
            _lp_detail: dict = {}
            if _lp_path_al.exists():
                try:
                    _lp_all = _jal.loads(_lp_path_al.read_text(encoding="utf-8"))
                    _lp_detail = _lp_all.get("latent_pearls", {})
                except Exception:
                    pass

            _new_promoted = [
                t for t in _promoted_tickers
                if float(
                    _lp_detail.get(t, {}).get("latent_pearl_score", 0)
                ) >= 70.0
            ]
            if _new_promoted:
                _lines = []
                for _ticker_al in _new_promoted[:5]:
                    _e_al = _lp_detail.get(_ticker_al, {})
                    _lines.append(
                        f"  ⭐ [{_ticker_al}] "
                        f"lp_score={_e_al.get('latent_pearl_score', 0):.1f}pt "
                        f"ind={_e_al.get('independence_score', 0):.0f}pt "
                        f"conds={'/'.join(_e_al.get('detection_conditions', []))}"
                    )
                alerts.append({
                    "trigger":  "wave7_latent_promoted",
                    "title":    f"⭐ [Wave7] 潜伏Pearl昇格（{len(_new_promoted)}銘柄）",
                    "body":     "\n".join(_lines),
                    "severity": "high",
                    "channel":  "line",
                })
    except Exception:
        pass

        # ==============================================================
        # [Independence Ph1] トリガー⑱: セクター反転検出
        # 条件: sector_reversal (LAGGING→IMPROVING) かつ Latent Pearl 存在
        #       → Gmail 週次通知
        # ==============================================================
    try:
                # latent_pearl_cache の JSON を直接読む
        import json as _jrev
        from pathlib import Path as _prev
        _lp_cache_path = (
            _prev(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
            / "latent_pearl_cache.json"
        )
        if not _lp_cache_path.exists():
            raise FileNotFoundError("latent_pearl_cache.json not found")
        _lp_raw   = _jrev.loads(_lp_cache_path.read_text(encoding="utf-8"))
        _lp_cache = _lp_raw.get("latent_pearls", {})
        _bridge   = results.get("jp_us_bridge", {})
        _rot      = _bridge.get("rotation_phases", {})

        _reversal_lines = []
        _lagging_phases = {"LAGGING", "WEAKENING"}
        for _sc, _phase_info in _rot.items():
            _curr_phase = str(_phase_info.get("phase", ""))
            if _curr_phase not in ("IMPROVING", "LEADING"):
                continue
            # このセクターに Latent Pearl が存在するか確認
            _sec_latents = [
                t for t, e in _lp_cache.items()
                if str(e.get("sector_code", "")) == str(_sc)
            ]
            if _sec_latents:
                _reversal_lines.append(
                    f"  🔄 セクター{_sc}: 局面={_curr_phase} "
                    f"潜伏銘柄={','.join(_sec_latents[:3])}"
                )

        if _reversal_lines:
            alerts.append({
                "trigger":  "wave3_sector_reversal",
                "title":    f"🔄 [Wave3-⑱] セクター反転検出"
                            f"（{len(_reversal_lines)}業種）",
                "body":     "\n".join(_reversal_lines),
                "severity": "medium",
                "channel":  "gmail",
            })
    except Exception:
        pass

        # ==============================================================
        # [FE-5] トリガー㉒: PEAD スコアアラート
        # 条件: pead_score ≥ threshold かつ volume_ratio ≥ 1.5 → LINE通知
        # ==============================================================
    try:
        from pead_monitor import get_alert_candidates as _get_pead_alerts
        _pead_alerts = _get_pead_alerts(cfg)

        if _pead_alerts:
            _pead_lines = [
                f"  📈 [{_tk(a['ticker'])}] "
                f"score={a.get('pead_score', 0):.1f}pt "
                f"vol={a.get('volume_ratio', 0):.2f}x "
                f"dir={a.get('direction', '')} "
                f"sector={a.get('sector_code', '')}"
                for a in _pead_alerts[:5]
            ]
            alerts.append({
                "trigger":  "wave3_pead_alert",
                "title":    f"📈 [Wave3-㉒] PEAD検出"
                            f"（{len(_pead_alerts)}銘柄）",
                "body":     "\n".join(_pead_lines),
                "severity": "high",
                "channel":  "line",
            })
    except Exception:
        pass

        # ==============================================================
        # [FE-6] トリガー㉓: ポートフォリオ集中警告
        # 条件: HHI > 0.30 かつ 高相関ペア ≥ 3組 → Gmail週次警告
        #       Ignition 抑制中は skip
        # ==============================================================
    try:
        _div = results.get("diversity", {})
        if (
            _div.get("should_alert")
            and not _div.get("alert_suppressed")
            and float(_div.get("hhi", 0)) > 0.30
            and int(_div.get("high_corr_pair_count", 0)) >= 3
        ):
            _conc = _div.get("concentrated_sectors", [])
            _pairs = _div.get("high_corr_pairs", [])[:3]
            _body_lines = [
                f"  HHI={_div.get('hhi', 0):.3f} [{_div.get('hhi_status', '?')}]",
                f"  高相関ペア: {_div.get('high_corr_pair_count', 0)}組",
            ]
            if _conc:
                _body_lines.append(
                    "  集中業種: "
                    + " / ".join(f"{s['sector_code']}({s['weight']*100:.0f}%)"
                                  for s in _conc[:3])
                )
            if _pairs:
                _body_lines.append(
                    "  高相関代表: "
                    + " / ".join(f"{_tk(p['ticker1'])}×{_tk(p['ticker2'])}"
                                  f"(ρ={p['correlation']:.2f})"
                                  for p in _pairs)
                )
            alerts.append({
                "trigger":  "wave3_diversity_warning",
                "title":    f"⚠️ [Wave3-㉓] ポートフォリオ集中警告"
                            f" HHI={_div.get('hhi',0):.3f}",
                "body":     "\n".join(_body_lines),
                "severity": "medium",
                "channel":  "gmail",
            })
    except Exception:
        pass

        # ==============================================================
        # [FE-7] トリガー㉔: Triple Catalyst 最優先通知
        # 条件: 3種以上のカタリストが14日以内に重複 → LINE最優先通知
        # ==============================================================
    try:
        _cat = results.get("catalyst", {})
        _triple_tickers = _cat.get("triple_tickers", [])

        if _triple_tickers:
            _triple_lines = [
                f"  ⭐ [{_tk(t['ticker'])}] "
                f"カタリスト{t['catalyst_count']}種重複 "
                f"最短{t['min_days']}日後 "
                f"[{'+'.join(t['types'][:3])}]"
                for t in _triple_tickers[:5]
            ]
            _crit_label = "🚨🚨 最優先" if len(_triple_tickers) >= 3 else "⭐"
            alerts.append({
                "trigger":  "wave3_triple_catalyst",
                "title":    f"{_crit_label} [Wave3-㉔] Triple Catalyst検出"
                            f"（{len(_triple_tickers)}銘柄）",
                "body":     "\n".join(_triple_lines),
                "severity": "high",
                "channel":  "line",
            })
    except Exception:
        pass

        # ==============================================================
        # [Policy Ph3] トリガー⑮: Buffett新規取得 → LINE即時
        # 条件: institutional_13f の new_tier1 に Tier1（Berkshire）の
        #       new_position が含まれる
        # チャネル: LINE 即時通知（severity: critical）
        # ==============================================================
    try:
        _inst = results.get("institutional_13f", {})
        _new_tier1 = _inst.get("new_tier1", [])
        if _new_tier1:
            _t1_lines = []
            for _t1 in _new_tier1[:5]:
                _val_b = _t1.get("value_usd", 0) / 1_000_000_000
                _t1_lines.append(
                    f"  🏆 {_t1.get('institution', '')} "
                    f"新規取得: {_tk(_t1.get('ticker', ''))} "
                    f"({_t1.get('name', '')[:20]})\n"
                    f"     取得額: ${_val_b:.1f}B "
                    f"株数: {_t1.get('shares', 0):,}"
                )
            alerts.append({
                "trigger":  "buffett_new_position",
                "title":    f"🏆🚨 Buffett新規取得！{len(_new_tier1)}銘柄 "
                            f"({_inst.get('quarter', '')})",
                "body":     "\n".join(_t1_lines),
                "severity": "critical",
                "channel":  "line",   # LINE即時通知
            })
    except Exception:
        pass

        # ==============================================================
        # [Policy Ph3] トリガー⑯: 大手機関新規取得10億ドル超 → Gmail
        # 条件: institutional_13f の new_large に
        #       value_usd >= INSTITUTION_MIN_NEW_POSITION_USD(10億ドル) の
        #       Tier2/Tier3 機関の new_position が含まれる
        # チャネル: Gmail 通知（severity: high）
        # ※ Tier1（Buffett）は⑮で処理済みのため除外
        # ==============================================================
    try:
        _inst     = results.get("institutional_13f", {})
        _new_large = [
            c for c in _inst.get("new_large", [])
            if c.get("tier", 3) >= 2   # Tier1は⑮で処理済みのため除外
        ]
        if _new_large:
            _min_usd = float(
                getattr(cfg, "INSTITUTION_MIN_NEW_POSITION_USD", 1_000_000_000)
            )
            _large_lines = []
            for _lg in _new_large[:5]:
                _val_b = _lg.get("value_usd", 0) / 1_000_000_000
                _tier_label = (
                    "アクティビスト" if _lg.get("tier") == 2
                    else "大手機関"
                )
                _large_lines.append(
                    f"  🏦 [{_tier_label}] {_lg.get('institution', '')[:25]}\n"
                    f"     新規取得: {_tk(_lg.get('ticker', ''))} "
                    f"({_lg.get('name', '')[:20]})\n"
                    f"     取得額: ${_val_b:.1f}B "
                    f"(閾値: ${_min_usd/1e9:.0f}B超)"
                )
            alerts.append({
                "trigger":  "large_institution_new_position",
                "title":    f"🏦 大手機関・アクティビスト新規取得 "
                            f"{len(_new_large)}件 "
                            f"(${_min_usd/1e9:.0f}B超) "
                            f"({_inst.get('quarter', '')})",
                "body":     "\n".join(_large_lines),
                "severity": "high",
                "channel":  "gmail",   # Gmail通知
            })
    except Exception:
        pass

        # ── ㉔ 出口戦略アラート（統合ブリーフ） ──
        # 既存Gmail 1通への統合方針（D-06）: 新規メールは増やさず本関数に集約。
    try:
        _ex = results.get("exit_alerts", {}) or {}
        _ex_alerts = _ex.get("alerts", [])
        if _ex_alerts:
            _fires = [a for a in _ex_alerts if a.get("level") == "fire"]
            _nears = [a for a in _ex_alerts if a.get("level") != "fire"]
            _lines = []
            for a in _fires:
                _lines.append(f"  🔔 [発火] {_tk(a.get('ticker',''))} "
                              f"{a.get('rule_id','')}: {a.get('detail','')[:90]}")
            for a in _nears[:10]:
                _lines.append(f"  👀 [接近] {_tk(a.get('ticker',''))} "
                              f"{a.get('rule_id','')}: {a.get('detail','')[:80]}")
            _tickets = _ex.get("tickets", [])
            if _tickets:
                _lines.append(f"  🎫 注文票 {len(_tickets)}件 発行済み"
                              f"（発注は手動 / stock_data/orders/order_tickets.jsonl）")
            alerts.append({
                "trigger":  "exit_rule_engine",
                "title":    f"🚪 出口戦略: 発火{len(_fires)}件 / 接近{len(_nears)}件",
                "body":     "\n".join(_lines),
                "severity": "high" if _fires else "medium",
                "channel":  "gmail",
            })
    except Exception:
        pass

        # ── ㉕ データ鮮度SLA違反 ──────────────────
    try:
        _sla = results.get("data_sla_report", {}) or {}
        _violations = _sla.get("violations", [])
        if _violations:
                        # status="missing" のとき age_days は None（経過日数が無意味なので文言を分ける）。
                        # パスは末尾だけ残し、欠けた場合は先頭に … を付けて省略を示す。
            def _sla_path(p: str, width: int = 50) -> str:
                p = str(p or "")
                return p if len(p) <= width else "…" + p[-(width - 1):]

            def _sla_line(v: dict) -> str:
                kind   = v.get("kind", "")
                status = v.get("status", "")
                limit  = v.get("limit_days", "?")
                path   = _sla_path(v.get("path", ""))
                age    = v.get("age_days")
                if status == "missing" or age is None:
                    return f"  ⏰ [{kind}] ファイルなし (SLA {limit}日): {path}"
                return (f"  ⏰ [{kind}] {status} 経過{age}日 "
                        f"(SLA {limit}日): {path}")

            _lines = [_sla_line(v) for v in _violations[:8]]
            alerts.append({
                "trigger":  "data_sla_violation",
                "title":    f"⏰ データ鮮度SLA違反 {len(_violations)}件",
                "body":     "\n".join(_lines),
                "severity": "medium",
                "channel":  "gmail",
            })
    except Exception:
        pass

        # ── EdinetDB 取り込み結果 ─────────────
        # 毎日「どの会社の有報データがBQに入ったか」をメールで確認できるようにする。
        # 0社の日も「0社だった」ことを知らせる（静かに止まっていないかの確認用）。
    if getattr(cfg, "ALERT_ON_EDINETDB_INGEST", True):
        ing = results.get("edinetdb_ingest") or {}
        if isinstance(ing, dict) and not ing.get("skipped") and "success" in ing:
            from pathlib import Path
            import json
            _dd = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
            _today = datetime.now().strftime("%Y-%m-%d")
            def _jl(p):
                try:
                    return json.loads(Path(p).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return {}
            _web = _jl(_dd / "edinetdb_web_last.json")
            if str(_web.get("date", "")) != _today:
                _web = {}
            try:
                import api_call_log
                _calls = api_call_log.summarize("edinetdb", _today, _dd)
            except Exception:
                _calls = None
            alerts.append(build_edinetdb_ingest_alert(
                ing, _web, _jl(_dd / "edinetdb_cache" / f"daily_{_today}.json"), 100, _calls))

        # 国債利回り（日米 10/5/3年・前日比/前週比）と政策金利（米・欧・日）
    if getattr(cfg, "ALERT_ON_RATES", True):
        try:
            from rates_monitor import get_rates, format_lines
            _rl = format_lines(get_rates(cfg))
            if _rl:
                alerts.append({"trigger": "rates", "title": "📈 金利（日米国債 10/5/3年・政策金利）",
                               "body": "\n".join(_rl), "severity": "info"})
        except Exception as _rte:
            print(f"    ⚠️ 金利節の生成失敗: {str(_rte)[:80]}")

        # 不動産DB（取り込みと業種スコアへの反映）
    if getattr(cfg, "ALERT_ON_REAL_ESTATE", True):
        try:
            _fd = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
            _fday = datetime.now().strftime("%Y-%m-%d")
            try:
                _fl = json.loads((_fd / "fudosandb_cache" / f"daily_{_fday}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _fl = None
            _fu = load_db_usage("fudosandb", _fl, getattr(cfg, "FUDOSAN_DAILY_BUDGET", None), _fd, _fday)
            _fu["stopped"] = "日次予算到達" if (results.get("fudosan_ingest") or {}).get("budget_stop") else None
            _re = build_real_estate_alert(results.get("fudosan_ingest") or {}, sector_results, _fu)
            if _re:
                alerts.append(_re)
        except Exception as _ree:
            print(f"    ⚠️ 不動産DB節の生成失敗: {str(_ree)[:80]}")

        # 政策DB（公的支出・経済安保）。出典表示は利用規約で必須
    if getattr(cfg, "ALERT_ON_SEISAKUDB", True):
        try:
            _sd = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
            _sday = datetime.now().strftime("%Y-%m-%d")
            try:
                _sl = json.loads((_sd / "seisakudb_usage.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _sl = None
            _skr = results.get("seisakudb") or {}
            _su = load_db_usage("seisakudb", _sl, _skr.get("budget") or getattr(cfg, "SEISAKUDB_DAILY_BUDGET", None), _sd, _sday)
            _su["stopped"] = _skr.get("stopped") if _skr.get("stopped") not in (None, "", "budget") else None
            _sk = build_seisakudb_alert(_skr, _su)
            if _sk:
                alerts.append(_sk)
        except Exception as _ske:
            print(f"    ⚠️ 政策DB節の生成失敗: {str(_ske)[:80]}")

        # コードだけで出ている所に名前を補う（日本株ティッカー・業種コード）
    try:
        from name_resolver import enrich_text
        for a in alerts:
            a["title"] = enrich_text(a.get("title", ""))
            a["body"] = enrich_text(a.get("body", ""))
    except Exception as _ne:
        print(f"    ⚠️ 名前補完をスキップ: {str(_ne)[:80]}")
    return alerts


def collect_high_dividend(cfg, results: dict, sector_results: dict | None = None) -> dict:
    """高配当銘柄を日米で集める（純関数）。利回りは比率（0.04 = 4%）。"""
    min_dy = float(getattr(cfg, "ALERT_MIN_DIVIDEND_YIELD", 0.04))
    max_dy = float(getattr(cfg, "ALERT_MAX_DIVIDEND_YIELD", 0.15))   # 超える値はデータ異常とみなす
    seen: dict = {}

    def _add(t, name, dy, pr, sector, market):
        try:
            dy = float(dy)
        except (TypeError, ValueError):
            return
        if not t or not (min_dy <= dy <= max_dy):
            return
        try:
            pr = float(pr) if pr is not None else None
        except (TypeError, ValueError):
            pr = None
        if pr is not None and not (0.0 <= pr <= 2.0):
            pr = None
        if t not in seen or dy > seen[t]["dy"]:
            seen[t] = {"ticker": t, "name": str(name or t), "dy": dy, "payout": pr,
                       "sector": sector or "", "market": market}

    for c in ((results.get("screener") or {}).get("candidates") or []):
        _add(c.get("ticker"), _jname(c), c.get("dividend_yield"), c.get("payout_ratio"),
             c.get("sector_name"), "JP")
    for c in ((results.get("us_screener") or {}).get("candidates") or []):
        _add(c.get("ticker"), _jname(c), c.get("div_yield"), c.get("payout_ratio"),
             c.get("sector"), "US")
    for code, r in (sector_results or {}).items():
        if isinstance(r, dict):
            for c in r.get("top_companies", []) or []:
                if c.get("fetch_ok"):
                    _add(c.get("ticker"), _jname(c), c.get("dividend_yield"),
                         c.get("payout_ratio"), r.get("sector_name_ja", code), "JP")
    out = {"JP": [], "US": []}
    for v in sorted(seen.values(), key=lambda x: -x["dy"]):
        out[v["market"]].append(v)
    return out


def build_high_dividend_alert(cfg, results: dict, sector_results: dict | None = None):
    found = collect_high_dividend(cfg, results, sector_results)
    n_jp, n_us = len(found["JP"]), len(found["US"])
    if not n_jp and not n_us:
        return None
    min_dy = float(getattr(cfg, "ALERT_MIN_DIVIDEND_YIELD", 0.04))
    lines = []
    for mk, label, top in (("JP", "日本株", 12), ("US", "米国株", 8)):
        rows = found[mk]
        if not rows:
            continue
        lines.append(f"  【{label}】{len(rows)}社（上位{min(top, len(rows))}社）")
        for v in rows[:top]:
            pr = f" | 配当性向{v['payout']*100:.0f}%" if v["payout"] is not None else ""
            warn = " ⚠️性向100%超" if v["payout"] is not None and v["payout"] > 1.0 else ""
            lines.append(f"    {v['name'][:16]}（{v['ticker']}）: {v['dy']*100:.1f}%"
                         f"{pr} | {v['sector'][:12]}{warn}")
    return {
        "trigger":  "high_dividend",
        "title":    f"💰 高配当銘柄 日本{n_jp}社・米国{n_us}社（{min_dy*100:.0f}%以上）",
        "body":     "\n".join(lines),
        "severity": "info",
    }


def db_usage_lines(usage: dict | None) -> list:
    """不動産DB・政策DB の API 使用回数と使い道（呼び出し元・種類・結果）の行（純関数）。

    usage = {"used": 本日の使用回数, "budget": 日次の上限, "calls": api_call_log.summarize の戻り値, "stopped": 停止理由}
    """
    if not isinstance(usage, dict) or usage.get("used") is None:
        return []
    lines = [f"  【API使用】{usage['used']}/{usage.get('budget', '—')}回"
             + (f"（停止: {usage['stopped']}）" if usage.get("stopped") else "")]
    u_lines, _ = api_usage_lines(usage.get("calls"), None, int(usage.get("used") or 0))
    return lines + u_lines


def load_db_usage(source: str, ledger: dict | None, budget, data_dir=None, day: str | None = None) -> dict:
    """日次台帳（{"calls_used"} か {"calls"}）と api_call_log の記録から usage を作る。"""
    used = None
    if isinstance(ledger, dict) and (not day or str(ledger.get("date", day)) == day):
        used = ledger.get("calls_used", ledger.get("calls"))
    calls = None
    try:
        import api_call_log
        calls = api_call_log.summarize(source, day, data_dir)
    except Exception:
        pass
    if used is None and calls and calls.get("total"):
        used = calls["total"]
    return {"used": used, "budget": budget, "calls": calls}


def build_real_estate_alert(fudo: dict, sector_results: dict, usage: dict | None = None) -> dict | None:
    """不動産DB（fudosandb.jp）の取り込み結果と、業種スコアへの反映をメール1件にまとめる（純関数）。

    不動産ボーナス（修正PBR・資産バリュー倍率・災害/人口リスクから −2〜+3点）が付いた銘柄も出す。
    """
    from name_resolver import edinet_cols, sector_name
    lines = []
    if isinstance(fudo, dict) and not fudo.get("skipped"):
        lines.append(f"  【取り込み】{fudo.get('success', 0)}社 / 保有不動産 {fudo.get('holdings', 0):,}件"
                     f" / 地域プロファイル {fudo.get('area_profiles', 0)}件"
                     + (f" / 失敗 {fudo['failure']}社" if fudo.get("failure") else "")
                     + ("（API上限で打ち切り）" if fudo.get("budget_stop") else ""))
        lines.extend(db_usage_lines(usage))
        tg = [edinet_cols(c) for c in (fudo.get("targets") or [])][:12]
        if tg:
            lines.append("    対象: " + "、".join(f"{s} | {n}" for s, n in tg)
                         + ("…" if len(fudo.get("targets") or []) > 12 else ""))
    bon = []
    for code, r in (sector_results or {}).items():
        if not isinstance(r, dict):
            continue
        for tk, v in (r.get("real_estate_by_ticker") or {}).items():
            if isinstance(v, dict) and v.get("bonus"):
                bon.append((float(v["bonus"]), tk, v))
    if bon:
        bon.sort(key=lambda b: b[0], reverse=True)
        lines.append(f"  【銘柄スコアへの反映】不動産ボーナスが付いた銘柄 {len(bon)}")

        def _fmt(v, tk, d):
            extra = []
            if d.get("adj_pbr") is not None:
                extra.append(f"修正PBR {d['adj_pbr']:.2f}")
            if d.get("asset_value_ratio") is not None:
                extra.append(f"資産バリュー倍率 {d['asset_value_ratio']:.2f}")
            label = f"{tk} {d['name']}" if d.get("name") and d.get("name") != tk else tk
            return f"{label}: {v:+.2f}点" + (f"（{'・'.join(extra)}）" if extra else "")
        for v, tk, d in [b for b in bon if b[0] > 0][:5]:
            lines.append(f"    ▲ {_fmt(v, tk, d)}")
        for v, tk, d in [b for b in bon if b[0] < 0][-3:]:
            lines.append(f"    ▼ {_fmt(v, tk, d)}")
        lines.append("    ※ 修正PBR・資産バリュー倍率（土地の含み益を税引後で足す）と、浸水・人口減少リスクから算出（−2〜+3点）")
    if not lines:
        return None
    return {"trigger": "real_estate", "title": f"🏢 不動産DB 取り込み{(fudo or {}).get('success', 0)}社・銘柄反映{len(bon)}",
            "body": "\n".join(lines), "severity": "info", "channel": "gmail"}


def build_seisakudb_alert(sk: dict, usage: dict | None = None) -> dict | None:
    """政策DB の日次結果（seisakudb_ingestor.run の戻り値）をメール1件にまとめる（純関数）。"""
    if not isinstance(sk, dict) or not sk or sk.get("skipped"):
        return None
    from name_resolver import name_of

    def _yen(v):
        if not v:
            return "—"
        return f"{v / 1e8:,.1f}億円" if v >= 1e8 else f"{v / 1e4:,.0f}万円"

    lines = [f"  【取り込み】直近の公的支出 {sk.get('recent', 0)}件 / 個社 {sk.get('companies_ok', 0)}社"
             f"（受給記録なし {sk.get('companies_empty', 0)}社）"
             + (f" / 経済安保認定 {sk['certs']}件" if sk.get("certs") else "")
             + ("" if usage else f" / API {sk.get('calls', 0)}/{sk.get('budget', 0)}回"
                + (f" / 停止: {sk['stopped']}" if sk.get("stopped") and sk["stopped"] != "budget" else ""))]
    lines.extend(db_usage_lines(usage))
    hits = sk.get("company_hits") or []
    if hits:
        lines.append("  【個社の公的支出（今回取得・5年合計の多い順）】")
        for h in hits[:8]:
            tk = f"{h['code']}.T"
            lines.append(f"    {tk} {name_of(tk, default='')}: {h.get('n', 0)}件・{_yen(h.get('amount_yen'))}")
    rec = [r for r in (sk.get("recent_sample") or []) if r.get("company_code")]
    if rec:
        lines.append("  【直近の公的支出イベント】")
        for r in rec[:8]:
            tk = f"{r['company_code']}.T"
            nm = r.get("company_name") or name_of(tk, default="")
            lines.append(f"    {r.get('event_date') or ''} {tk} {nm}: {(r.get('title') or '')[:40]}"
                         f"（{r.get('kind') or ''}{'・' + r['ministry'] if r.get('ministry') else ''}）"
                         f" {_yen(r.get('amount_yen'))}")
    rank = sk.get("rank_top") or []
    if rank:
        lines.append("  【公的支出の多い上場企業（上位5）】")
        for r in rank[:5]:
            tk = f"{r.get('code')}.T"
            lines.append(f"    {tk} {r.get('listed_name') or name_of(tk, default='')}: "
                         f"合計{(r.get('total_oku') or 0):,.1f}億円"
                         f"（補助金{(r.get('subsidy_oku') or 0):,.1f}・調達{(r.get('procurement_oku') or 0):,.1f}）")
    if sk.get("certs_mapped"):
        lines.append(f"  【経済安保認定のうち証券コードに対応できた企業】{sk['certs_mapped']}件")
    subs = sorted((sk.get("open_subsidies") or []), key=lambda x: x.get("acceptance_end") or "9999")[:3]
    if subs:
        lines.append("  【締切が近い補助金（3件）】")
        for x in subs:
            lines.append(f"    {x.get('acceptance_end') or '—'} {(x.get('title') or '')[:40]}"
                         f"（{x.get('institution_name') or ''}）")
    lines.append("    出典: 政策DB（seisakudb.jp）")
    return {"trigger": "seisakudb", "title": f"🏛️ 政策DB 公的支出 直近{sk.get('recent', 0)}件・個社{sk.get('companies_ok', 0)}社",
            "body": "\n".join(lines), "severity": "info", "channel": "gmail"}


_CAP_JA = {"config": "設定の日次予算", "daily_remaining": "EdinetDB 側の本日の残り",
           "monthly_remaining": "EdinetDB 側の今月の残り", "monthly_guard": "月の上限（設定）−今月の使用"}


def api_usage_lines(calls: dict | None, caps: dict | None, used: int, ing: dict | None = None) -> tuple[list, list]:
    """API の使い道（呼び出し元・種類・結果）と、本日の枠を決めた理由の行（純関数）。

    Returns: (lines, warnings)。warnings は件名・重要度を上げるための短い文。
    """
    lines, warns = [], []
    calls = calls or {}
    if calls.get("total"):
        lines.append(f"  【API の使い道】記録 {calls['total']}回（{calls.get('first') or '—'}〜{calls.get('last') or '—'}）")
        lines.append("    呼び出し元: " + " / ".join(f"{k} {v}回" for k, v in (calls.get("by_caller") or [])[:8]))
        lines.append("    種類: " + " / ".join(f"{k} {v}回" for k, v in (calls.get("by_path") or [])[:8]))
        lines.append("    結果: " + " / ".join(f"HTTP {k}×{v}" for k, v in (calls.get("by_status") or [])))
        n429 = sum(v for k, v in (calls.get("by_status") or []) if str(k) == "429")
        if n429:
            warns.append(f"HTTP 429（回数制限）{n429}回")
    caps = caps or {}
    if caps.get("caps"):
        c = caps["caps"]
        # 平常（設定の日次予算どおりに枠が決まった日）は内訳を出さない。読むべき情報が無く、毎日のノイズになるため。
        # 月の上限や EdinetDB 側の残りで枠が削られた日だけ、どれが効いたかを出す。
        if caps.get("limiter") not in (None, "config"):
            lines.append("  【本日の枠の内訳】" + " / ".join(f"{_CAP_JA.get(k, k)} {v}" for k, v in c.items())
                         + f" → 本日の枠 {caps.get('final')}回（決めたもの: {_CAP_JA.get(caps.get('limiter'), caps.get('limiter'))}）")
            if caps.get("monthly_used") is not None:
                lines.append(f"    今月の使用 {caps['monthly_used']}回 / 月の上限（設定）{caps.get('monthly_guard')}回")
        if caps.get("limiter") in ("monthly_remaining", "monthly_guard"):
            warns.append(f"月の上限に近いため本日の枠を {caps.get('final')}回に抑えた")
        elif caps.get("limiter") == "daily_remaining" and c.get("daily_remaining", 0) < c.get("config", 0):
            warns.append(f"本日すでに他で使われ、残り {c.get('daily_remaining')}回から開始")
        final = int(caps.get("final") or 0)
        if final and used < final * 0.5:
            why = []
            wsk = len((ing or {}).get("web_skipped") or [])
            if wsk:
                why.append(f"Web確認で財務なしの会社 {wsk}社を除いて候補が {len((ing or {}).get('targets') or []) if isinstance((ing or {}).get('targets'), list) else (ing or {}).get('targets', '?')}社に減った")
            if (ing or {}).get("missing"):
                why.append(f"EdinetDB にデータの無い会社 {ing['missing']}社")
            warns.append(f"枠 {final}回に対し {used}回しか使っていない" + (f"（{'・'.join(why)}）" if why else ""))
    for w in warns:
        lines.append(f"  ⚠️ {w}")
    return lines, warns


def build_edinetdb_ingest_alert(ing: dict, web: dict | None = None, ledger: dict | None = None,
                                daily_limit: int = 100, calls: dict | None = None) -> dict:
    """EdinetDB の日次結果をメール用アラート1件にまとめる（純関数）。

    年次財務は会社ページ（Web）、API は有報本文・セグメント・四半期財務と役割が分かれているので、
    ① 年次財務（Web）② 有報本文・セグメント（API）③ 四半期財務（API）④ API使用回数 を分けて出す。
    """
    def _row(c: dict) -> str:
        code = c.get("sec_code") or c.get("edinet_code") or "?"
        return f"{code} | {c.get('name') or '—'}"

    lines = []
    web = web or {}
    if web:
        lines.append(f"  【年次財務（会社ページ）】更新 {web.get('ok', 0)}社 / 財務なし {web.get('no_data', 0)}社"
                     f" / 失敗 {web.get('error', 0)}件（財務 {web.get('financials', 0)}行・細目 {web.get('items', 0):,}行）")
        for c in (web.get("companies") or [])[:15]:
            lines.append(f"    ✅ {_row(c)}" + (f" — {c['fiscal_year']}年度まで" if c.get("fiscal_year") else ""))
    done = ing.get("ingested") or []
    lines.append(f"  【有報本文・セグメント（API）】{len(done)}社")
    for c in done[:15]:
        lines.append(f"    ✅ {_row(c)}" + (f" — {c['fiscal_year']}年度・本文{c.get('text_rows', 0)}件"
                                            if c.get("fiscal_year") else ""))
    if not done:
        lines.append("    （本日はありません）")
    miss = ing.get("missing_companies") or []
    if miss:
        lines.append(f"    ― EdinetDBにまだデータが無い {len(miss)}社: "
                     + "、".join(_row(c) for c in miss[:8]) + ("…" if len(miss) > 8 else ""))
    wsk = ing.get("web_skipped") or []
    if wsk:
        lines.append(f"    ― Webで財務なしのため対象外 {len(wsk)}社: "
                     + "、".join(_row(c) for c in wsk[:8]) + ("…" if len(wsk) > 8 else ""))
    if ing.get("quarterly_companies") is not None:
        lines.append(f"  【四半期財務（API）】{ing.get('quarterly_companies', 0)}社 / {ing.get('quarterly_rows', 0):,}行")
    if ing.get("failure"):
        lines.append(f"  ⚠️ 失敗 {ing['failure']}社（内訳: {ing.get('reasons', {})}）")
    used = (ledger or {}).get("calls_used", ing.get("api_calls", 0))
    rem = (ledger or {}).get("remote_daily_remaining")
    lines.append(f"  【API使用】{used}/{daily_limit}回" + (f"（EdinetDB側の残り {rem}回）" if rem is not None else "")
                 + f" / 書類一覧 {ing.get('doc_index_rows', 0)}件")
    u_lines, warns = api_usage_lines(calls, ing.get("budget_caps"), int(used or 0), ing)
    lines += u_lines
    n = len(done) + int(web.get("ok", 0) or 0)
    return {
        "trigger":  "edinetdb_ingest",
        "title":    f"🗂 EDINET DB 本日 年次{web.get('ok', 0)}社・本文{len(done)}社・四半期{ing.get('quarterly_companies', 0)}社・API {used}/{daily_limit}回"
                    + ("（0社・要確認）" if n == 0 and ing.get("targets") else "")
                    + (f"（⚠️ {warns[0]}）" if warns else ""),
        "body":     "\n".join(lines),
        "severity": "medium" if (n == 0 and ing.get("targets")) or ing.get("failure") or warns else "info",
        "channel":  "gmail",
    }


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
                # 本文中の保存ファイル名をGitHubへのリンクにし、末尾に本日の保存ファイル一覧（GitHub・Drive検索）を付ける
        _files, _fidx, _files_html = [], {}, ""
        try:
            import os as _os
            from pathlib import Path as _P
            import file_links as _fl
            _base = _P(_os.environ.get("STOCK_ANALYZER_BASE") or _P(__file__).resolve().parent / "stock_data")
            _files = _fl.todays_files(_base / "insights")
            _fidx = _fl.index_files(_files)
            _files_html = _fl.files_section_html(_files)
        except Exception as _e:
            print(f"    ⚠️ 保存ファイルのリンク作成失敗: {_e}")
        rows = ""
        for al in alerts:
            color = severity_color.get(al["severity"], "#6b7280")
            label = severity_label.get(al["severity"], "")
            # body の改行を <br> に変換（pre タグより視認性が高い）
            body_html = al["body"].replace("\n", "<br>").replace("    ", "&nbsp;&nbsp;&nbsp;&nbsp;")
            if _fidx:
                body_html = _fl.linkify(body_html, _fidx)
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

  {_files_html}

  <!-- フッター -->
  <div style="padding:16px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;">
    <p style="margin:0;font-size:11px;color:#9ca3af;">
      stock_analyzer_main.py より自動送信 | データソース: yfinance / EdinetDB / IMF
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

    # ── チャネル指定アラートを分離 ────────────────────────────
    # "channel": "line"  → LINE のみ即時送信
    # "channel": "gmail" → Gmail のみ送信
    # "channel" なし     → 両方に送信
    _line_only  = [a for a in alerts if a.get("channel") == "line"]
    _gmail_only = [a for a in alerts if a.get("channel") == "gmail"]
    _both       = [a for a in alerts if not a.get("channel")]

    # LINE 送信（LINE専用 + 両方送信）
    if has_line:
        _line_alerts = _line_only + _both
        if _line_alerts:
            line_msg = build_message(_line_alerts, mode="line")
            ok, err  = send_line(line_token, line_msg)
            if ok:
                channels.append("LINE")
                messages.append(line_msg)
                sent += 1
                print(f"    ✅ LINE 通知 送信成功 "
                      f"({len(_line_alerts)}件 / うち即時{len(_line_only)}件)")
            else:
                errors.append(f"LINE: {err}")
                print(f"    ⚠️  LINE: {err}")

    # Gmail 送信（Gmail専用 + 両方送信）
    if has_gmail:
        _gmail_alerts = _gmail_only + _both
        if _gmail_alerts:
            now_str   = datetime.now().strftime("%Y-%m-%d")
            subject   = (
                f"【投資AIエージェント】{len(_gmail_alerts)}件のアラート ({now_str})"
            )
            html_body = build_message(_gmail_alerts, mode="email")
            ok, err   = send_gmail(gmail_from, gmail_pwd, gmail_to, subject, html_body)
            if ok:
                channels.append("Gmail")
                messages.append(subject)
                sent += 1
                print(f"    ✅ Gmail 通知 送信成功 "
                      f"({len(_gmail_alerts)}件 / うちGmail専用{len(_gmail_only)}件)")
            else:
                errors.append(f"Gmail: {err}")
                print(f"    ⚠️  Gmail: {err}")

    # 少なくとも1チャネルに届いた後でのみ通知済み状態を更新する。
    if sent > 0:
        _sent_score_jumps = [
            item
            for alert in alerts if alert.get("trigger") == "score_jump"
            for item in alert.get("_score_jump_items", [])
        ]
        if _sent_score_jumps:
            try:
                from anomaly_detector import mark_score_jumps_notified as _mark_score_jumps
                _mark_score_jumps(cfg, _sent_score_jumps)
            except Exception:
                pass

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
