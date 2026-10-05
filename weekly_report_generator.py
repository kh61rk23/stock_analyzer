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
from datetime import datetime
from pathlib import Path


# ==============================================================
# BLOCK 1: ユーティリティ
# ==============================================================

def _pct(v, digits: int = 1) -> str:
    if v is None: return "N/A"
    try:    return f"{float(v)*100:.{digits}f}%"
    except (TypeError, ValueError): return "N/A"

def _score(v) -> str:
    if v is None: return "N/A"
    try:    return f"{float(v):.0f}点"
    except (TypeError, ValueError): return "N/A"

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
    except (TypeError, ValueError): return "→"


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
            try:
                from name_resolver import name_of as _nm
                cname = (_nm(c.get("ticker", "")) or c.get("name", ""))[:20]
            except Exception:
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
# BLOCK 2.5: セクション生成関数（Wave 6 - G-N：国別リスク）
# ==============================================================

def _section_geo_country_risk(results: dict, cfg=None) -> list:
    """
    国別リスク要約セクションを生成する（Wave 6 - G-N）。

    results["geo_risk"] から以下の情報を取得:
      - composite_risk_score が高い国のランキング
      - 外務省危険レベル・米国務省警告レベル
      - GDELT 不安定性スコア

    表示条件:
      - results["geo_risk"] が存在し skipped でないこと
      - high_risk（composite_risk_score >= 50）が1件以上あること

    Returns:
        list[str]: Markdown セクション行リスト
    """
    lines = ["## 🌐 国別地政学リスク（Wave 6 - G-N）", ""]

    geo_risk = results.get("geo_risk", {})
    if not geo_risk or geo_risk.get("skipped"):
        lines.append("*国別リスクデータなし（ENABLE_COUNTRY_RISK_UPDATE=False またはスキップ）*")
        lines.append("")
        return lines

    risk_scores  = geo_risk.get("risk_scores",  {})
    mofa_levels  = geo_risk.get("mofa_levels",  {})
    state_levels = geo_risk.get("state_levels", {})
    gdelt_scores = geo_risk.get("gdelt_scores", {})
    high_risk    = geo_risk.get("high_risk",    [])
    updated      = geo_risk.get("timestamp",    "N/A")

    # config から閾値を取得（未設定時はデフォルト値）
    _top_n = getattr(cfg, "GEO_REPORT_TOP_N_COUNTRIES", 10) if cfg else 10
    _high_thresh = getattr(cfg, "GEO_RISK_HIGH_THRESHOLD", 50.0) if cfg else 50.0
    _med_thresh  = getattr(cfg, "GEO_RISK_MED_THRESHOLD",  25.0) if cfg else 25.0

    # 更新日時
    try:
        _ts = updated[:10] if updated and updated != "N/A" else "N/A"
    except Exception:
        _ts = "N/A"
    lines.append(f"**更新日**: {_ts} | **監視国数**: {len(risk_scores)}カ国")
    lines.append("")

    # ── ① 高リスク国テーブル ──────────────────────────────────
    if high_risk:
        lines.append(f"### 🔴 高リスク国（スコア{_high_thresh:.0f}以上）")
        lines.append("")
        lines.append(
            "| 国コード | 総合スコア | GDELT不安定性 | 外務省レベル | 国務省レベル |"
        )
        lines.append("|---------|-----------|------------|------------|------------|")

        for code in high_risk[:_top_n]:
            composite = risk_scores.get(code, 0.0)
            gdelt     = gdelt_scores.get(code, -1.0)
            mofa      = mofa_levels.get(code, 0)
            state     = state_levels.get(code, 0)

            # スコアアイコン
            if composite >= 75:
                icon = "🔴"
            elif composite >= 50:
                icon = "🟠"
            else:
                icon = "🟡"

            # 外務省レベル表示
            mofa_str = {
                0: "情報なし",
                1: "Lv.1 注意",
                2: "Lv.2 不要不急",
                3: "Lv.3 渡航禁止",
                4: "Lv.4 退避勧告",
            }.get(mofa, f"Lv.{mofa}")

            # 国務省レベル表示
            state_str = {
                0: "情報なし",
                1: "Lv.1 通常注意",
                2: "Lv.2 注意強化",
                3: "Lv.3 再考慮",
                4: "Lv.4 渡航禁止",
            }.get(state, f"Lv.{state}")

            gdelt_str = (
                f"{gdelt:.1f}"
                if gdelt >= 0
                else "取得失敗"
            )

            lines.append(
                f"| {icon} `{code}` "
                f"| **{composite:.1f}**/100 "
                f"| {gdelt_str} "
                f"| {mofa_str} "
                f"| {state_str} |"
            )

        lines.append("")
    else:
        lines.append("*高リスク国（スコア50以上）の検出なし*")
        lines.append("")

    # ── ② 中リスク国サマリー（25〜49） ────────────────────────
    mid_risk = sorted(
        [
            (code, score)
            for code, score in risk_scores.items()
            if _med_thresh <= score < _high_thresh
        ],
        key=lambda x: -x[1],
    )[:5]   # 上位5件のみ表示

    if mid_risk:
        lines.append(f"### 🟡 中リスク国（スコア{_med_thresh:.0f}〜{_high_thresh:.0f}未満）")
        lines.append("")
        mid_parts = [
            f"`{code}` ({score:.0f}pt)"
            for code, score in mid_risk
        ]
        lines.append("注目国: " + " / ".join(mid_parts))
        lines.append("")

    # ── ③ シーレーン別リスク警告 ─────────────────────────────
    _sealanes = getattr(
        cfg, "GEO_MONITORED_SEALANES", []
    ) if cfg else []

    if _sealanes and high_risk:
        # 高リスク国が隣接するシーレーンを country_profile_builder から参照
        try:
            import importlib, sys as _sys
            from pathlib import Path as _Path
            for p in ["/content", str(_Path(__file__).parent)]:
                if p not in _sys.path:
                    _sys.path.insert(0, p)
            _cpb = importlib.import_module("country_profile_builder")

            alerted_sealanes: list[str] = []
            for lane in _sealanes:
                lane_countries = _cpb.get_countries_by_sealane(lane)
                # 高リスク国がシーレーン隣接国に含まれるか
                if any(c in high_risk for c in lane_countries):
                    alerted_sealanes.append(lane)

            if alerted_sealanes:
                lines.append("### ⚠️ シーレーンリスク警告")
                lines.append("")
                for lane in alerted_sealanes:
                    lane_hr = [
                        c for c in _cpb.get_countries_by_sealane(lane)
                        if c in high_risk
                    ]
                    lines.append(
                        f"- **{lane}**: 隣接高リスク国 → "
                        f"{', '.join(f'`{c}`' for c in lane_hr)}"
                    )
                lines.append("")

        except Exception:
            pass

    # ── ④ 資源別リスク影響サマリー ────────────────────────────
    _commodities = getattr(
        cfg, "GEO_MONITORED_COMMODITIES", []
    ) if cfg else []

    if _commodities and high_risk:
        try:
            import importlib, sys as _sys
            from pathlib import Path as _Path
            for p in ["/content", str(_Path(__file__).parent)]:
                if p not in _sys.path:
                    _sys.path.insert(0, p)
            _cpb = importlib.import_module("country_profile_builder")

            commodity_alerts: list[tuple] = []
            for com in _commodities[:8]:   # 主要8資源のみ
                com_countries = _cpb.get_countries_by_commodity(com)
                affected_hr   = [
                    c for c in com_countries if c in high_risk
                ]
                if affected_hr:
                    avg_risk = sum(
                        risk_scores.get(c, 0.0) for c in affected_hr
                    ) / len(affected_hr)
                    commodity_alerts.append((com, affected_hr, avg_risk))

            if commodity_alerts:
                commodity_alerts.sort(key=lambda x: -x[2])
                lines.append("### 🏭 資源別リスク影響")
                lines.append("")
                lines.append(
                    "| 資源コード | 高リスク産出国 | 平均リスクスコア |"
                )
                lines.append("|----------|------------|--------------|")
                for com, countries, avg in commodity_alerts[:6]:
                    countries_str = " / ".join(
                        f"`{c}`" for c in countries[:3]
                    )
                    if len(countries) > 3:
                        countries_str += f" 他{len(countries)-3}カ国"
                    lines.append(
                        f"| {com} "
                        f"| {countries_str} "
                        f"| {avg:.1f}/100 |"
                    )
                lines.append("")

        except Exception:
            pass

    return lines

def _section_geo_exposure(results: dict, cfg=None) -> list:
    """
    企業×海外権益マッピングと地政学影響セクションを生成する（Wave 6 - G-X）。

    表示内容:
      ① geo_impact_bonus 上位・下位銘柄ランキング
      ② 資源権益別リスク影響マトリクス
      ③ 地政学追い風・逆風サマリー

    Returns:
        list[str]: Markdown セクション行リスト
    """
    lines = ["## ⛏️ 資源権益・地政学影響（Wave 6 - G-X）", ""]

    # geo_signal モジュール経由でサマリーを取得
    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _gs = importlib.import_module("geo_signal")

        geo_summary = _gs.build_geo_weekly_summary(
            results = results,
            cfg     = cfg,
            top_n   = 5,
        )

        # ── ① 地政学シグナルサマリーテキスト（geo_signal が生成）────
        summary_text = geo_summary.get("summary_text", "")
        if summary_text:
            # "### 🌐 地政学リスク・資源権益サマリー" ヘッダーは除去
            # （本セクションのヘッダーと重複するため）
            for _line in summary_text.split("\n"):
                if _line.startswith("### 🌐"):
                    continue
                lines.append(_line)
            lines.append("")

        # ── ② geo_impact_bonus ランキングテーブル ─────────────────
        top_pos = geo_summary.get("top_positive_tickers", [])
        top_neg = geo_summary.get("top_negative_tickers", [])

        if top_pos or top_neg:
            lines.append("### 📊 geo_impact_bonus ランキング")
            lines.append("")
            lines.append("| 市場 | ティッカー | geo_impact_bonus | 方向 |")
            lines.append("|-----|----------|----------------|-----|")

            for d in top_pos[:5]:
                bonus = d.get("bonus", 0.0)
                lines.append(
                    f"| {d.get('market', '?')} "
                    f"| `{d.get('ticker', '?')}` "
                    f"| {bonus:+.2f}pt "
                    f"| 🟢 追い風 |"
                )
            for d in top_neg[:5]:
                bonus = d.get("bonus", 0.0)
                lines.append(
                    f"| {d.get('market', '?')} "
                    f"| `{d.get('ticker', '?')}` "
                    f"| {bonus:+.2f}pt "
                    f"| 🔴 逆風 |"
                )
            lines.append("")

        # ── ③ 資源権益マトリクス（BQ から集計）──────────────────────
        _pid = getattr(
            cfg, "GEO_BQ_PROJECT_ID", "stock-analyzer-edinet"
        ) if cfg else "stock-analyzer-edinet"

        try:
            _bq_mod = importlib.import_module("edinet_bq_client")
            _bq     = _bq_mod.EDINETBQClient()
            raw_bq  = _bq.client

            query = f"""
            SELECT
              e.commodity_code,
              COUNT(DISTINCT e.ticker) AS ticker_count,
              ROUND(AVG(r.composite_risk_score), 1) AS avg_risk,
              COUNT(DISTINCT e.country_code) AS country_count,
              COUNTIF(e.verified_flag = TRUE) AS verified_count
            FROM `{_pid}.geopolitics.company_country_exposure` AS e
            LEFT JOIN (
              SELECT country_code, composite_risk_score
              FROM (
                SELECT country_code, composite_risk_score,
                       ROW_NUMBER() OVER (
                         PARTITION BY country_code ORDER BY risk_date DESC
                       ) AS rn
                FROM `{_pid}.geopolitics.country_risk_daily`
              )
              WHERE rn = 1
            ) AS r ON e.country_code = r.country_code
            WHERE e.commodity_code IS NOT NULL
              AND (e.valid_to IS NULL OR e.valid_to > CURRENT_DATE())
            GROUP BY e.commodity_code
            ORDER BY avg_risk DESC
            LIMIT 10
            """
            rows = list(raw_bq.query(query).result())

            if rows:
                lines.append("### 🏭 資源別権益マトリクス")
                lines.append("")
                lines.append(
                    "| 資源コード | 関連銘柄数 | 平均リスクスコア "
                    "| 関連国数 | 確認済み |"
                )
                lines.append(
                    "|----------|----------|--------------|--------|------|"
                )
                for row in rows:
                    commodity     = row.get("commodity_code") or "?"
                    ticker_count  = int(row.get("ticker_count") or 0)
                    avg_risk      = float(row.get("avg_risk") or 0.0)
                    country_count = int(row.get("country_count") or 0)
                    verified      = int(row.get("verified_count") or 0)

                    risk_icon = (
                        "🔴" if avg_risk >= 50
                        else ("🟡" if avg_risk >= 25 else "🟢")
                    )
                    lines.append(
                        f"| {commodity} "
                        f"| {ticker_count}社 "
                        f"| {risk_icon} {avg_risk:.1f}/100 "
                        f"| {country_count}カ国 "
                        f"| {verified}件 |"
                    )
                lines.append("")

        except Exception:
            lines.append(
                "*資源権益マトリクス: BQ 未接続またはデータなし*"
            )
            lines.append("")

        # ── ④ 高リスク国への権益エクスポージャー上位銘柄 ──────────
        high_risk_countries = geo_summary.get("high_risk_countries", [])
        if high_risk_countries:
            lines.append("### ⚠️ 高リスク国への権益エクスポージャー")
            lines.append("")

            for hrc in high_risk_countries[:3]:   # 上位3カ国
                code         = hrc.get("country_code", "?")
                risk_score   = hrc.get("risk_score", 0.0)
                affected     = hrc.get("affected_tickers", [])

                lines.append(
                    f"**`{code}`** リスクスコア {risk_score:.0f}/100"
                )
                if affected:
                    lines.append(
                        "権益保有銘柄: "
                        + " / ".join(f"`{t}`" for t in affected[:5])
                    )
                else:
                    lines.append("権益保有銘柄: データなし")
                lines.append("")

    except ModuleNotFoundError:
        lines.append(
            "*geo_signal.py 未配置のため地政学シグナルセクションをスキップ*"
        )
        lines.append("")
    except Exception as e:
        lines.append(
            f"*地政学シグナルセクション生成エラー: {str(e)[:80]}*"
        )
        lines.append("")

    return lines


def _section_geo_signal_detail(results: dict, cfg=None) -> list:
    """
    signal_aggregator の geo_signal スコアを業種別に表示する（Wave 6 - G-X）。

    signal_aggregator の結果から geo_signal コンポーネントを抽出し、
    業種別の地政学シグナル強度をランキング表示する。

    Returns:
        list[str]: Markdown セクション行リスト
    """
    lines = ["### 📡 業種別 地政学シグナルスコア", ""]

    sa_result = results.get("signal_aggregator", {})
    if not sa_result or sa_result.get("skipped"):
        lines.append("*signal_aggregator データなし*")
        lines.append("")
        return lines

    sector_signals = sa_result.get("sector_signals", {})
    if not sector_signals:
        lines.append("*業種別シグナルデータなし*")
        lines.append("")
        return lines

    # geo_signal コンポーネントを持つ業種を抽出
    geo_entries: list[tuple] = []
    for code, d in sector_signals.items():
        if not isinstance(d, dict):
            continue
        geo_comp = (d.get("components") or {}).get("geo_signal", {})
        # 2026-10-05: 内訳が数値だけで入っている業種があり 'float' に .get して毎週エラーだった
        geo_score = geo_comp.get("score") if isinstance(geo_comp, dict) else geo_comp
        if geo_score is None:
            continue
        geo_entries.append((code, float(geo_score)))

    if not geo_entries:
        lines.append("*地政学シグナルを持つ業種なし（ENABLE_GEO_SIGNAL=False または BQデータ未蓄積）*")
        lines.append("")
        return lines

    geo_entries.sort(key=lambda x: -x[1])

    lines.append("| 業種コード | 業種名 | geo_score | シグナル |")
    lines.append("|----------|------|---------|--------|")

    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _cfg_mod = importlib.import_module("config")
        _sectors = getattr(_cfg_mod, "JAPAN_SECTORS", {})
    except Exception:
        _sectors = {}

    for code, score in geo_entries[:10]:
        name    = _sectors.get(str(code), {}).get("name_ja", code)
        icon    = (
            "🟢 追い風" if score >= 60
            else ("🔴 逆風" if score <= 40 else "⚪ 中立")
        )
        lines.append(
            f"| `{code}` | {name} | {score:.0f}/100 | {icon} |"
        )

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

    # 2026-09-14: top3/bottom3 は us_sector_pipeline がスコア順に並べたもので、
    # 週間パフォーマンス順ではない（見出しが実態と違っていた）。
    if top3:
        lines.append(f"**スコア上位**: {' ＞ '.join(top3[:3])}")
    if bottom3:
        lines.append(f"**スコア下位**: {' ＜ '.join(bottom3[:3])}")

    # 上位セクターの詳細
    # 2026-09-14: sector_list の要素は {"sector": {...}, "score_result": {...}} の
    # 入れ子だが、ここは name / composite_score / return_1w / trend を直接
    # 読んでいた。どれも存在しないため、9/14 の週次メールでは
    # 「|  | 0 | N/A |  |」の空行が並んでいた。週次リターンは算出されて
    # いないので、実在する1ヶ月リターン（%値）とリスク区分を出す。
    if sector_list:
        lines.append("")
        lines.append("| セクター | スコア | 1ヶ月リターン | リスク |")
        lines.append("|---------|-------|--------------|-------|")
        for s in sector_list[:5]:
            sec   = s.get("sector") if isinstance(s.get("sector"), dict) else {}
            sr    = s.get("score_result") if isinstance(s.get("score_result"), dict) else s
            etf   = sr.get("etf_data") or {}
            name  = sec.get("name_ja") or s.get("name", "")
            code  = sec.get("etf") or s.get("ticker", "")
            sname = f"{name}（{code}）" if name and code else (name or code)
            score = sr.get("score", s.get("composite_score", 0)) or 0
            ret1m = etf.get("ret_1m")
            risk  = sr.get("risk_level", "")
            ret_s = f"{ret1m:+.1f}%" if ret1m is not None else "N/A"
            lines.append(f"| {sname} | {score:.0f} | {ret_s} | {risk} |")
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

def _section_deliberation_queue(results: dict, cfg) -> list:
    """B部門審議（E3）へ次に上げる候補の一覧（2026-07-28 新設）。

    並び順は deliberation_scheduler.build_deliberation_queue と同一。
    先頭が隔週ジョブで実際に選定される銘柄になる。
    """
    lines = ["## 🗳️ 審議キュー候補（B部門 E3）", ""]
    try:
        from deliberation_scheduler import get_queue_for_report
        dq = get_queue_for_report(
            cfg, results,
            top_n=int(getattr(cfg, "DELIBERATION_QUEUE_TOP_N", 5)))
    except Exception as e:
        lines.append(f"*キュー生成エラー: {str(e)[:80]}*")
        lines.append("")
        return lines

    queue = dq.get("queue", [])
    if not queue:
        lines.append("*未審議の候補なし（screener 通過銘柄が全て審議済み"
                     " or screener データなし）*")
        lines.append("")
        return lines

    lines.append(f"次回審議（隔週 / {dq.get('interval_days', 14)}日間隔）では"
                 f"**先頭の1銘柄**が自動選定されます。")
    lines.append("")
    lines.append("| # | 銘柄 | コード | スコア | 業種 | 戦略 |")
    lines.append("|---|---|---|---|---|---|")
    for i, c in enumerate(queue, 1):
        mark = "**▶ 1**" if i == 1 else str(i)
        sc = c.get("composite_score")
        sc_s = f"{float(sc):.1f}" if sc is not None else "—"
        name = str(c.get("name") or c.get("ticker", ""))[:20]
        sector = c.get("sector_name") or c.get("sector_code") or "—"
        strat = c.get("strategy_label") or "—"
        lines.append(f"| {mark} | {name} | `{c.get('ticker','')}` | "
                     f"{sc_s} | {sector} | {strat} |")
    lines.append("")
    lines.append(f"- 審議済み除外: **{dq.get('already_count', 0)}銘柄**"
                 f"（出所: {dq.get('source', '不明')}）")
    if dq.get("partial"):
        lines.append("- ⚠️ 判断台帳（正本）を参照できていないため、"
                     "審議済み銘柄が混じる可能性があります")
    # 機械可読マーカー（日次メールと同一形式。詳細は alert_notifier 側の注記）
    try:
        from alert_notifier import DELIBERATION_QUEUE_MARKER as _mk
    except Exception:
        _mk = "DELIBERATION_QUEUE_V1:"
    lines.append("")
    lines.append(f"{_mk} "
                 + ",".join(str(c.get("ticker", "")) for c in queue))
    lines.append("")
    return lines


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
    lines.append("| 銘柄 | 業種 | スコア | ボラ調整後 | PER | PBR | ROE | 配当 | 推奨 |")
    lines.append("|------|------|-------|-----------|-----|-----|-----|-----|-----|")

    # composite_score 順に上位10件を表示
    top_candidates = sorted(
        passed, key=lambda x: x.get("composite_score", 0), reverse=True
    )[:10]
    for c in top_candidates:
        name  = (c.get("name", c.get("ticker", "")))[:12]
        sec   = c.get("sector_name", "")[:8]
        score = c.get("composite_score", 0)
        ra    = c.get("risk_adjusted_score", score)
        per_s = f"{c['per']:.1f}x"  if c.get("per")  else "N/A"
        pbr_s = f"{c['pbr']:.2f}x"  if c.get("pbr")  else "N/A"
        # 2026-09-14: stock_screener は roe を「%値」（14.3）で格納する
        # （round(roe * 100, 1)）。_pct() は比率→% 変換なので二重に100倍され、
        # Fuji Electric が「ROE 1430.0%」と表示されていた。
        _roe = c.get("roe")
        roe_s = f"{float(_roe):.1f}%" if _roe is not None else "N/A"
        dy_s  = _pct(c.get("dividend_yield"))
        rec   = c.get("rec_key", "N/A")
        # ボラ調整後スコアが元スコアより大幅に低い場合に ▼ マークを表示
        ra_mark = "▼" if (score - ra) > 5 else ""
        lines.append(
            f"| {name} | {sec} | {score:.0f} | {ra:.0f}{ra_mark}"
            f" | {per_s} | {pbr_s} | {roe_s} | {dy_s} | {rec} |"
        )

    lines.append("")

    # ── ボラ調整後ランキング（Wave 2 - FE-1） ──────────────────
    if any(c.get("risk_adjusted_score") for c in passed):
        lines.append("### 📉 ボラ調整後ランキング TOP10（FE-1）")
        lines.append("")
        lines.append("| 銘柄 | 業種 | ボラ調整後 | 元スコア | 差分 | size_tier |")
        lines.append("|------|------|-----------|---------|------|-----------|")
        ra_sorted = sorted(
            passed,
            key=lambda x: x.get("risk_adjusted_score",
                                  x.get("composite_score", 0)),
            reverse=True,
        )[:10]
        for c in ra_sorted:
            name  = (c.get("name", c.get("ticker", "")))[:12]
            sec   = c.get("sector_name", "")[:8]
            score = c.get("composite_score", 0)
            ra    = c.get("risk_adjusted_score", score)
            diff  = ra - score
            tier  = c.get("size_tier", "-")
            diff_s = f"{diff:+.0f}"
            lines.append(
                f"| {name} | {sec} | {ra:.0f} | {score:.0f} | {diff_s} | {tier} |"
            )
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

def _section_options_advisor(results: dict) -> list:
    """[OPT-1] オプション戦略提案セクション"""
    oa = results.get("options_advisor", {})
    sugg = oa.get("suggestions", [])
    if not sugg:
        return []
    lines = ["## 🎯 オプション戦略提案（defined-risk/カバードのみ・執行は人間）", ""]
    lines.append("| 銘柄 | 戦略 | 満期 | 期待変動 | 根拠 |")
    lines.append("|---|---|---|---|---|")
    for s in sugg[:8]:
        lines.append(
            f"| {s.get('ticker','')} | {s.get('strategy','')} "
            f"| {s.get('expiry','')} | {s.get('expected_move_pct','?')}% "
            f"| {str(s.get('rationale',''))[:40]} |")
    warns = {w for s in sugg for w in s.get("warnings", [])}
    for w in list(warns)[:3]:
        lines.append(f"- {w}")
    lines.append("")
    return lines


def _section_module_ledger(cfg) -> list:
    """[OBS-1] モジュール実行台帳（システム健全性）セクション"""
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, __file__.rsplit("/", 1)[0])
        _ml = _imp.import_module("module_ledger")
        return _ml.format_ledger_section(cfg)
    except Exception:
        return []


def _section_midterm_tracker(results: dict) -> list:
    """[I-5] 中期経営計画達成状況セクション"""
    mt_result = results.get("midterm_tracker", {})
    if not mt_result or mt_result.get("total", 0) == 0:
        return []
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, __file__.rsplit("/", 1)[0])
        _mt = _imp.import_module("midterm_plan_tracker")
        # ★ 2026-06-11 バグ修正: run_midterm_plan_tracker の戻りキーは "plans"
        return _mt.format_midterm_section(
            mt_result.get("plans", mt_result.get("results", {})))
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
        try:
            from name_resolver import name_of as _nm
            name = _nm(ticker) or d.get("name", ticker)
        except Exception:
            name = d.get("name", ticker)
        core = d.get("core_summary", "")
        if core:
            # 2026-07-28 バグ修正: ここは「原文が英語か翻訳済みか」を表す記号だったが、
            # 未翻訳の日本株にも 🇺🇸 が付き「米国銘柄」と誤読される状態だった。
            # 国旗は市場（.T なら JP）から決め、翻訳状態は別記号で示す。
            translated = d.get("translated", False)
            market_icon = "🇯🇵" if str(ticker).upper().endswith(".T") else "🇺🇸"
            trans_icon  = " 🌐" if translated else ""
            lines.append(f"**{name}** (`{ticker}`) {market_icon}{trans_icon}")
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
            # 2026-07-28 バグ修正: producer/consumer キー不整合。
            # policy_tracker.py L456 は {"label","bank","value","date"} を格納するが、
            # ここは "rate" / "current" を読んでいたため全行 "N/A%" になっていた。
            # 表示名も辞書キー（"^IRX" 等の yfinance ティッカー）をそのまま
            # 出していたため「主要中銀 政策金利」の見出しと内容が食い違っていた。
            lines.append("**主要政策金利・指標**")
            for ticker, info in rates.items():
                if not isinstance(info, dict):
                    continue
                val = info.get("value")
                if val is None:                      # 取得できた項目だけ出す
                    continue
                label = info.get("label") or info.get("bank") or ticker
                chg   = info.get("change_pt", 0)
                # 2026-09-14: 為替（JPY=X 等）にも一律で % を付けており
                # 「USD/JPY（JPY=X）: 154.482%」と出ていた。
                _is_fx = "=X" in str(ticker) or "/" in str(label)
                _unit, _chg_unit = ("", "") if _is_fx else ("%", "%pt")
                chg_s = f"（{chg:+.3f}{_chg_unit}）" if chg else ""
                asof  = info.get("date", "")
                asof_s = f" [{asof}]" if asof else ""
                lines.append(f"  {label}（{ticker}）: {val}{_unit}{chg_s}{asof_s}")
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
        # 2026-09-10: ここは name / date_estimate / days_until / boost_active を
        # 参照していたが、産出側（policy_radar._detect_events_30d）が返すのは
        # label / keyword / sectors / direction / lead_days / source / link /
        # detected_at で、参照キーが1つも存在しなかった。
        # そのため policy_radar がイベントを1件でも返した週は
        # KeyError: 'name' で週次レポート生成が丸ごと落ちていた
        # （台帳 2026-09-07 weekly_report [error] 'name'）。
        for ev in events[:5]:
            tailwind = str(ev.get("direction", "")) == "tailwind"
            icon = "⚡" if tailwind else "📌"
            lead = ev.get("lead_days")
            lead_txt = f" / 想定リード{lead}日" if lead not in (None, "") else ""
            sectors = [str(x) for x in (ev.get("sectors") or [])][:3]
            lines.append(
                f"- {icon} **{ev.get('label') or ev.get('keyword') or '(無題)'}** "
                f"（{'追い風' if tailwind else '逆風'}{lead_txt}）"
                + (f" → 対象業種: {', '.join(sectors)}" if sectors else "")
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

def _section_rule_optimizer_alert(results: dict, cfg=None) -> list:
    """
    Wave 6 - 第4セッション: 緊急縮退アラートセクション。

    出力内容:
      1. 現在の縮退モード（Normal / Warning / Emergency）とバッジ表示
      2. 発動条件一覧（VIX / 市場幅 / PCR / 地政学リスク 等）
      3. 統合スコア（total_signal_score）の上位/下位推移
      4. 防御セクターの防衛状況（スコア維持・相対優位）
      5. 縮退係数・補正内訳サマリー

    データ取得元:
      results["rule_optimizer"]["contraction_flag"]   → モード・係数・発動条件
      results["signal_aggregator"]["sector_signals"]  → 業種別スコア（防御判定）
      results["signal_aggregator"]["total_signal"]    → total_signal_score
    """
    try:
        ro          = results.get("rule_optimizer", {})
        contraction = ro.get("contraction_flag", {})
        mode        = contraction.get("mode", "Normal")

        # ── モードに応じたヘッダーアイコン ───────────────────────
        mode_meta = {
            "Normal":    ("✅", "通常モード", "#27ae60"),
            "Warning":   ("⚠️",  "警戒モード", "#f39c12"),
            "Emergency": ("🚨", "緊急縮退モード", "#c0392b"),
        }
        icon, label, _ = mode_meta.get(mode, ("✅", "通常モード", "#27ae60"))

        lines = [
            f"## {icon} 緊急縮退アラート（Wave 6 - 第4セッション）",
            "",
            f"**現在の縮退ステータス: {icon} {label}**",
            "",
        ]

        # ── 1. 縮退モードサマリーバッジ ──────────────────────────
        coeff       = float(contraction.get("coefficient", 1.00))
        trig_count  = int(contraction.get("triggered_count", 0))
        trig_flags  = contraction.get("triggered_flags", [])
        ts          = contraction.get("timestamp", "")

        # config から閾値を取得（cfg なし時はデフォルト）
        warn_count  = 2
        emerg_count = 4
        if cfg is not None:
            warn_count  = int(getattr(cfg, "CONTRACTION_WARNING_TRIGGER_COUNT",   2))
            emerg_count = int(getattr(cfg, "CONTRACTION_EMERGENCY_TRIGGER_COUNT", 4))

        lines.append("| 項目 | 値 |")
        lines.append("|---|---|")
        lines.append(f"| モード | **{label}** |")
        lines.append(f"| 縮退係数 | **{coeff:.2f}** （1.00=無補正 / 0.65=最大35%抑制）|")
        lines.append(f"| 発動条件数 | **{trig_count}** / Warning閾値:{warn_count} / Emergency閾値:{emerg_count} |")
        lines.append(f"| 最終判定時刻 | {ts} |")
        lines.append("")

        # ── 2. 発動条件一覧 ──────────────────────────────────────
        detail = contraction.get("detail", {})
        if trig_flags or detail:
            lines.append("### 📋 各条件の評価値")
            lines.append("")
            lines.append("| 条件 | 評価値 | 発動 |")
            lines.append("|---|---|---|")

            # 条件名・表示名マッピング
            condition_labels = {
                "vix":             ("VIX（恐怖指数）",             f">= {30.0}"),
                "us10y_rise":      ("米10年債利回り上昇幅(%/週)",  f">= {0.30}"),
                "breadth_score_jp":("市場幅スコア（0-100）",        f"<= {35.0}"),
                "pcr":             ("PCR（プット/コール比）",        f">= {1.40}"),
                "geo_risk_score":  ("地政学リスクスコア（0-100）",  f">= {70.0}"),
                "score_drop":      ("上位業種スコア平均週次急落幅", f">= {12.0}"),
            }
            if cfg is not None:
                condition_labels = {
                    "vix":             ("VIX（恐怖指数）",             f">= {getattr(cfg,'CONTRACTION_VIX_THRESHOLD',30.0)}"),
                    "us10y_rise":      ("米10年債利回り上昇幅(%/週)",  f">= {getattr(cfg,'CONTRACTION_US10Y_RISE_PCT',0.30)}"),
                    "breadth_score_jp":("市場幅スコア（0-100）",        f"<= {getattr(cfg,'CONTRACTION_BREADTH_SCORE_MIN',35.0)}"),
                    "pcr":             ("PCR（プット/コール比）",        f">= {getattr(cfg,'CONTRACTION_PCR_THRESHOLD',1.40)}"),
                    "geo_risk_score":  ("地政学リスクスコア（0-100）",  f">= {getattr(cfg,'CONTRACTION_GEO_RISK_SCORE',70.0)}"),
                    "score_drop":      ("上位業種スコア平均週次急落幅", f">= {getattr(cfg,'CONTRACTION_SCORE_DROP_THRESHOLD',12.0)}"),
                }

            for key, (cond_name, threshold_str) in condition_labels.items():
                val = detail.get(key)
                val_str = f"{val:.2f}" if isinstance(val, (int, float)) else (str(val) if val is not None else "N/A")
                if key == "geo_risk_score" and detail.get("geo_risk_country"):
                    val_str += f"（最高: {detail['geo_risk_country']}）"
                # 発動しているかどうかは triggered_flags の文字列前方一致で判定
                fired = any(
                    key.split("_")[0].lower() in f.lower() or
                    cond_name[:4] in f
                    for f in trig_flags
                )
                fired_icon = "🔴 発動" if fired else "🟢 正常"
                lines.append(f"| {cond_name}（閾値: {threshold_str}）| {val_str} | {fired_icon} |")
            lines.append("")

        # ── 3. 統合スコア上位/下位推移 ───────────────────────────
        sa_res        = results.get("signal_aggregator", {})
        sector_sigs   = sa_res.get("sector_signals", {})
        # ★ 2026-06-10 バグ修正: main の書き込みキーは "total_signal_score"
        total_sig_res = results.get("total_signal_score", results.get("total_signal", {}))
        tss_score     = total_sig_res.get("total_signal_score") if total_sig_res else None
        tss_coeff     = total_sig_res.get("contraction_coefficient", 1.00) if total_sig_res else 1.00

        if tss_score is not None:
            lines.append("### 📊 統合スコア（total_signal_score）")
            lines.append("")
            lines.append(f"- **現在値**: {tss_score:.1f}点 — {total_sig_res.get('signal_label', '')}"
                         f"（推奨: {total_sig_res.get('recommended_action', '—')}）")
            lines.append(f"- **縮退係数適用後**: 係数={tss_coeff:.2f}")
            # 2026-10-05: 根拠（要素ごとの点数×重み＝寄与）を出す
            _comp = total_sig_res.get("components") or {}
            _labels = {
                "macro_phase_score": f"景気局面（象限{total_sig_res.get('d1_phase', '?')}・債務サイクル補正込み）",
                "sector_composite": "上位業種の平均スコア",
                "momentum_score": "モメンタム",
                "earnings_acceleration": "利益の加速（スクリーナー通過銘柄の加速点平均）",
                "quality_score": "質（ROE・利益の質）",
                "technical_score": "テクニカル",
                "activist_potential": "アクティビスト余地",
                "geo_signal": "地政学シグナル",
            }
            if _comp:
                lines.append("")
                lines.append("| 要素 | 点数 | 重み | 寄与 |")
                lines.append("|---|---|---|---|")
                for _k, _v in sorted(_comp.items(), key=lambda kv: -(float((kv[1] or {}).get("weight") or 0))):
                    _sc, _w = (_v or {}).get("score"), float((_v or {}).get("weight") or 0)
                    if _w <= 0:
                        continue
                    _scv = 50.0 if _sc is None else float(_sc)
                    lines.append(f"| {_labels.get(_k, _k)} | {'—（中立50）' if _sc is None else f'{_scv:.1f}'}"
                                 f" | {_w:.0%} | {_scv * _w:.1f} |")
                lines.append("")
                lines.append("> 統合スコア＝各要素の点数×重みの合計（0〜100）。70以上=強い買い / 58以上=買い検討 /"
                             " 42以上=中立 / 30以上=軽減 / 30未満=回避。重みは rule_optimizer が月次で調整")
            lines.append("")

        # 業種別スコア上位/下位推移
        if sector_sigs:
            sorted_sigs = sorted(
                [(code, d) for code, d in sector_sigs.items() if isinstance(d, dict)],
                key=lambda x: x[1].get("signal_score", 50),
                reverse=True,
            )

            if sorted_sigs:
                lines.append("### 📈 業種別シグナルスコア上位5（縮退補正後）")
                lines.append("")
                lines.append("| 業種コード | 業種名 | シグナルスコア | 縮退モード | 防御セクター |")
                lines.append("|---|---|---|---|---|")
                from name_resolver import sector_name as _secn
                for code, d in sorted_sigs[:5]:
                    name      = d.get("sector_name_ja") or _secn(code)
                    score     = d.get("signal_score", 50)
                    c_mode    = d.get("contraction_mode", "Normal")
                    is_def    = d.get("is_defensive", False)
                    def_mark  = "🛡️ 防御" if is_def else "-"
                    lines.append(f"| {code} | {name} | **{score:.1f}点** | {c_mode} | {def_mark} |")
                lines.append("")

                lines.append("### 📉 業種別シグナルスコア下位5（縮退補正後）")
                lines.append("")
                lines.append("| 業種コード | 業種名 | シグナルスコア | 縮退モード | 防御セクター |")
                lines.append("|---|---|---|---|---|")
                for code, d in sorted_sigs[-5:]:
                    name      = d.get("sector_name_ja") or _secn(code)
                    score     = d.get("signal_score", 50)
                    c_mode    = d.get("contraction_mode", "Normal")
                    is_def    = d.get("is_defensive", False)
                    def_mark  = "🛡️ 防御" if is_def else "-"
                    lines.append(f"| {code} | {name} | **{score:.1f}点** | {c_mode} | {def_mark} |")
                lines.append("")

        # ── 4. 防御セクター防衛状況 ──────────────────────────────
        defensive_codes = sa_res.get("defensive_sectors", [])
        if defensive_codes and mode != "Normal":
            bonus_warning   = 3.0
            bonus_emergency = 7.0
            if cfg is not None:
                bonus_warning   = float(getattr(cfg, "DEFENSIVE_RELATIVE_BONUS_WARNING",   3.0))
                bonus_emergency = float(getattr(cfg, "DEFENSIVE_RELATIVE_BONUS_EMERGENCY", 7.0))
            bonus_applied = bonus_warning if mode == "Warning" else bonus_emergency

            lines.append("### 🛡️ 防御セクター防衛状況")
            lines.append("")
            lines.append(
                f"縮退モード: **{label}** のため、以下 **{len(defensive_codes)}** 業種に "
                f"相対ボーナス **+{bonus_applied:.1f}pt** を適用済み。"
            )
            lines.append("")
            lines.append("| 業種コード | 業種名 | 縮退前スコア | ボーナス | 補正後スコア |")
            lines.append("|---|---|---|---|---|")
            for code in defensive_codes:
                d          = sector_sigs.get(code, {})
                name       = d.get("sector_name_ja", code)
                score_now  = d.get("signal_score", 50)
                # 補正前スコアを逆算（bonus加算前）
                score_pre  = round(score_now - bonus_applied, 1)
                lines.append(
                    f"| {code} | {name} "
                    f"| {score_pre:.1f}点 "
                    f"| **+{bonus_applied:.1f}pt** "
                    f"| **{score_now:.1f}点** |"
                )
            lines.append("")
        elif mode == "Normal":
            lines.append(
                "> 縮退モード: Normal のため、全業種への縮退補正・防御セクターボーナスは未適用。"
            )
            lines.append("")

        # ── 5. 縮退履歴サマリー（直近7件）──────────────────────
        try:
            from pathlib import Path as _Path
            _cache_dir  = _Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) if cfg else _Path("stock_data/cache")
            _hist_fname = getattr(cfg, "CONTRACTION_HISTORY_PATH", "contraction_history.jsonl") if cfg else "contraction_history.jsonl"
            _hist_path  = _cache_dir / "contraction" / _hist_fname
            if _hist_path.exists():
                _records = []
                with _hist_path.open(encoding="utf-8") as _f:
                    for _line in _f:
                        _line = _line.strip()
                        if _line:
                            try:
                                _records.append(json.loads(_line))
                            except Exception:
                                pass
                # 直近7件のみ表示
                _recent = _records[-7:]
                if _recent:
                    lines.append("### 📅 縮退状態履歴（直近7週）")
                    lines.append("")
                    lines.append("| 日付 | モード | 発動数 | 係数 |")
                    lines.append("|---|---|---|---|")
                    for _r in reversed(_recent):
                        _r_icon = {
                            "Normal":    "✅",
                            "Warning":   "⚠️",
                            "Emergency": "🚨",
                        }.get(_r.get("mode", "Normal"), "✅")
                        lines.append(
                            f"| {_r.get('date', '')} "
                            f"| {_r_icon} {_r.get('mode', 'Normal')} "
                            f"| {_r.get('triggered_count', 0)} "
                            f"| {_r.get('coefficient', 1.00):.2f} |"
                        )
                    lines.append("")
        except Exception:
            pass   # 履歴読み込み失敗はセクション全体を止めない

        # ── US株縮退サマリー（us_stock_screener 結果から取得） ───
        us_screener = results.get("us_screener", results.get("screener_us", {}))
        if us_screener and isinstance(us_screener, dict):
            _us_c_mode = us_screener.get("contraction_mode", "Normal")
            _us_def_n  = us_screener.get("contraction_defensive_count", 0)
            _us_adj_n  = us_screener.get("contraction_adjusted_count",  0)
            if _us_c_mode != "Normal" or _us_def_n > 0:
                lines.append("### 🇺🇸 米国株スクリーニング縮退適用状況")
                lines.append("")
                lines.append(f"- 縮退モード: **{_us_c_mode}**")
                lines.append(f"- 防御セクター相対維持: **{_us_def_n}社**")
                lines.append(f"- 縮退係数適用（抑制）: **{_us_adj_n}社**")
                lines.append("")

        lines.append("---")
        lines.append("")
        return lines

    except Exception as _e:
        return [
            "## ⚠️ 緊急縮退アラート（Wave 6 - 第4セッション）",
            "",
            f"*セクション生成エラー（スキップ）: {str(_e)[:80]}*",
            "",
        ]

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
# BLOCK 8.5: Wave 0 新規セクション（Policy Ph1 / Idea2-A）
# ==============================================================

def _section_politician_highlight(results: dict) -> list:
    """[Wave0 Policy Ph1] 今週の議員取引ハイライト & JP波及シグナル"""
    pm = results.get("politician_monitor", {})
    if not pm or pm.get("skipped"):
        return []

    high_trades = pm.get("high_score_trades", [])
    jp_signals  = pm.get("jp_sector_signals", {})
    stats       = pm.get("stats", {})

    if not high_trades and not jp_signals:
        return []

    lines = ["## 🏛️ 今週の議員取引ハイライト（Policy Ph1）", ""]

    if high_trades:
        lines.append(f"**高スコア取引（閾値60pt以上）: {len(high_trades)}件**")
        lines.append("")
        lines.append("| スコア | 議員名 | 売買 | 資産名 | 委員会 |")
        lines.append("|-------|-------|-----|------|------|")
        for t in high_trades[:8]:
            direction = "🟢 買" if t.get("direction") == "buy" else "🔴 売"
            lines.append(
                f"| {t.get('score', 0):.0f}pt "
                f"| {t.get('politician', '')[:16]} "
                f"| {direction} "
                f"| {t.get('asset', '')[:25]} "
                f"| {t.get('committee', '')[:14]} |"
            )
        lines.append("")

    if jp_signals:
        lines.append("**🇯🇵 JP業種波及シグナル**")
        lines.append("")
        for code, delta in sorted(
            jp_signals.items(), key=lambda x: abs(x[1]), reverse=True
        )[:6]:
            icon = "📈" if delta > 0 else "📉"
            lines.append(f"  {icon} [{code}]: {delta:+.1f}pt")
        lines.append("")

    ts = pm.get("timestamp", "")
    lines.append(
        f"*新規検出: {stats.get('new_count', 0)}件 | "
        f"高スコア: {stats.get('high_score_count', 0)}件 | "
        f"更新: {ts}*"
    )
    lines.append("")
    return lines


def _section_jp_us_correlation(results: dict) -> list:
    """[Wave0 Idea2-A] JP×US相関マトリクス（直近60日）"""
    corr = results.get("jp_us_sector_correlation", {})
    if not corr or corr.get("stats", {}).get("insufficient_data"):
        return []

    pairs = corr.get("significant_pairs", [])
    stats = corr.get("stats", {})

    if not pairs:
        return []

    lines = ["## 🔗 JP×US相関マトリクス（直近60日）", ""]
    lines.append(
        f"**有意ペア（|r|≥0.7）: {stats.get('significant_count', 0)}件** "
        f"| 検証ペア数: {stats.get('total_pairs_checked', 0)} "
        f"| データ点数: {stats.get('data_points', 0)}日"
    )
    lines.append("")
    lines.append("| JP業種 | US ETF | 相関 | n | シグナル |")
    lines.append("|--------|--------|------|---|--------|")

    for p in pairs[:10]:
        r    = p.get("correlation", 0)
        sign = "+" if r >= 0 else ""
        sig  = {
            "aligned_up":   "📈 同調上昇",
            "aligned_down": "📉 同調下落",
            "mixed":        "↔ 乖離",
        }.get(p.get("signal", ""), "—")
        lines.append(
            f"| {p.get('jp_name', p.get('jp_code', ''))[:10]} "
            f"| {p.get('us_etf', '')}({p.get('us_sector_ja', '')[:6]}) "
            f"| **{sign}{r:.3f}** "
            f"| {p.get('n_observations', 0)} "
            f"| {sig} |"
        )

    lines.append("")
    lines.append(f"*更新: {corr.get('timestamp', '')}*")
    lines.append("")
    return lines

def _fmt(v) -> str:
    """スコア値を '99.9' 形式の文字列に変換する。None は '—' を返す。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return "—"


def _format_pearl_row_detail(c: dict) -> str:
    """
    Tier SS 銘柄の1行サマリを生成する。
    Wave 7: EDINETセグメント文脈・カタリストフラグ・潜伏Pearlスコアを追加表示。
    """
    ticker = c.get("ticker", c.get("code", ""))
    ps     = c.get("pearl_score", 0)
    d1     = c.get("d1_score")
    d3     = c.get("d3_score")
    d4     = c.get("d4_score")
    bonus  = c.get("pearl_bonus", 0)

    # ── Wave 7 追加フィールド ────────────────────────────────────
    lp_score    = float(c.get("latent_pearl_score", 0.0))
    cat_flags   = c.get("catalyst_flags", [])
    cat_preview = " / ".join(str(f).split("（")[0] for f in cat_flags[:2])
    mt_traj     = c.get("midterm_trajectory", "")
    mt_score    = c.get("midterm_overall_score", 0.0)

    # EDINETセグメント文脈サマリ（50字以内）
    seg_text = str(c.get("edinet_segment_text", "") or "")
    seg_preview = seg_text[:50].replace("\n", " ") if seg_text else ""

    # 軌道アイコン
    traj_icon = {
        "ahead":    "🚀", "on_track": "✅",
        "behind":   "⚠️", "critical": "🔴",
    }.get(mt_traj, "")

    base = (
        f"**⭐ {ticker}** "
        f"| Pearl: **{ps:.1f}pt** "
        f"| D1方向性={_fmt(d1)} "
        f"/ D3利益品質={_fmt(d3)} "
        f"/ D4発見Gap={_fmt(d4)} "
        f"| +{bonus:.1f}pt ボーナス"
    )
    # Wave 7 拡張行
    ext_parts = []
    if lp_score >= 30.0:
        ext_parts.append(f"💎潜伏Pearl={lp_score:.0f}pt")
    if cat_preview:
        ext_parts.append(f"カタリスト: {cat_preview}")
    if mt_traj:
        ext_parts.append(f"中計{traj_icon}{mt_score:.0f}pt")
    if seg_preview:
        ext_parts.append(f"セグメント: 「{seg_preview}…」")

    if ext_parts:
        return base + "\n  → " + " / ".join(ext_parts)
    return base


def _section_pearl_ranking(results: dict, cfg=None) -> list:
    """
    [Wave0 Pearl Ph1] 光る原石ランキング
    pearl_candidates.json（キャッシュ）を優先参照し、
    未生成の場合は results["screener"]["passed"] から pearl_tier のある銘柄を抽出する。
    """
    # ── データ取得（キャッシュ優先） ──────────────────────────────
    candidates: list = []
    try:
        from pearl_score_calculator import load_pearl_candidates as _load_pc
        candidates = _load_pc(cfg) or []
    except Exception:
        pass

    # ── フォールバック: results["screener"] から抽出 ───────────────
    if not candidates:
        try:
            screener = results.get("screener", {})
            passed   = screener.get("passed",
                       screener.get("candidates", []))
            for c in passed:
                if c.get("pearl_tier") and float(c.get("pearl_score", 0)) >= 50:
                    candidates.append({
                        "ticker":      c.get("ticker", c.get("code", "")),
                        "pearl_score": float(c.get("pearl_score", 0)),
                        "pearl_tier":  c.get("pearl_tier"),
                        "pearl_bonus": float(c.get("pearl_bonus", 0)),
                        "d1_score":    c.get("d1_score"),
                        "d3_score":    c.get("d3_score"),
                        "d4_score":    c.get("d4_score"),
                    })
            _tier_order = {"SS": 0, "S": 1, "A": 2, "B": 3}
            candidates.sort(key=lambda x: (
                _tier_order.get(x.get("pearl_tier", "B"), 9),
                -x.get("pearl_score", 0),
            ))
        except Exception:
            pass

    # 2026-09-14: pearl_candidates.json は日次で追記され TTL 7日で残るため、
    # 同じ銘柄が日付違いで複数行入る。9/14 の週次メールでは MSGM×2 /
    # HESAY×3 / IDEXY×3 / TCEHY×3 / RELY×2 と重複掲載され、下の
    # 「発見: 計261銘柄」も水増しされていた。銘柄ごとに1行へ畳む
    # （日付の新しい方を優先し、同日なら高スコア側）。
    _best: dict = {}
    for c in candidates:
        _t = c.get("ticker") or c.get("code")
        if not _t:
            continue
        _stamp = str(c.get("date") or c.get("scored_at") or c.get("updated_at")
                     or c.get("saved_at") or c.get("timestamp") or "")
        _key = (_stamp, float(c.get("pearl_score", 0) or 0))
        if _t not in _best or _key > _best[_t][0]:
            _best[_t] = (_key, c)
    if _best:
        _tier_order = {"SS": 0, "S": 1, "A": 2, "B": 3}
        candidates = sorted(
            (v[1] for v in _best.values()),
            key=lambda x: (_tier_order.get(x.get("pearl_tier", "B"), 9),
                           -float(x.get("pearl_score", 0) or 0)),
        )

    if not candidates:
        return []

    lines = ["## 💎 光る原石ランキング（Pearl）", ""]

    # ── Tier SS（≥80pt）: 特別強調 ────────────────────────────────
    ss_stocks = [c for c in candidates if c.get("pearl_tier") == "SS"]
    if ss_stocks:
        min_score_str = f"{getattr(cfg, 'PEARL_MIN_SCORE_ALERT', 80):.0f}" if cfg else "80"
        mcap_str      = f"{getattr(cfg, 'PEARL_MCAP_MAX_ALERT_B', 500):.0f}" if cfg else "500"
        lines.append(
            f"### ⭐⭐ Tier SS（Pearl≥{min_score_str}pt × 時価総額≤{mcap_str}億 → LINE即時通知対象）"
        )
        lines.append("")
        for c in ss_stocks[:5]:
            lines.append(_format_pearl_row_detail(c))
        lines.append("")

    # ── Tier S / A / B: 一覧表 ────────────────────────────────────
    other_stocks = [
        c for c in candidates if c.get("pearl_tier") in ("S", "A", "B")
    ]
    if other_stocks:
        lines.append("### 💎 Tier S / A / B（注目候補）")
        lines.append("")
        lines.append(
            "| # | Tier | 銘柄 | Pearl | D1 方向性 | D2 スマートマネー | D3 利益品質 | D4 発見Gap | D5 割安・逆張り | bonus |"
        )
        lines.append(
            "|---|---|---|---|---|---|---|---|---|---|"
        )
        _tier_icon = {"S": "💎", "A": "🔷", "B": "🔹"}
        for i, c in enumerate(other_stocks[:15], 1):
            tier   = c.get("pearl_tier", "")
            ticker = c.get("ticker", c.get("code", ""))
            ps     = c.get("pearl_score", 0)
            bonus  = c.get("pearl_bonus", 0)
            lines.append(
                f"| {i} "
                f"| {_tier_icon.get(tier, '')}{tier} "
                f"| {ticker} "
                f"| **{ps:.1f}pt** "
                f"| {_fmt(c.get('d1_score'))} "
                f"| {_fmt(c.get('d2_score'))} "
                f"| {_fmt(c.get('d3_score'))} "
                f"| {_fmt(c.get('d4_score'))} "
                f"| {_fmt(c.get('d5_score'))} "
                f"| +{bonus:.1f}pt |"
            )
        lines.append("")

    # ── 2026-10-05: 日本株（健太指示「光る原石ランキングに日本株も入れて」）──
    # 日本株スクリーナーは売上上位の企業が対象で D4（発見Gap＝小型・未発見）が低く、
    # Tier B（50点）に届かず上の表にほぼ出ない。Tier未満も含めて上位を別枠で出す。
    _jp = [c for c in (results.get("screener") or {}).get("candidates", [])
           if str(c.get("ticker", "")).endswith(".T") and c.get("pearl_score")]
    if _jp:
        _jp = sorted(_jp, key=lambda c: -float(c.get("pearl_score") or 0))[:10]
        lines.append("### 🇯🇵 日本株 上位10（Tier未満も含む）")
        lines.append("")
        lines.append("| # | 銘柄 | Pearl | Tier | 総合点 | 業種 |")
        lines.append("|---|---|---|---|---|---|")
        for i, c in enumerate(_jp, 1):
            lines.append(f"| {i} | {c.get('ticker')} | **{float(c.get('pearl_score') or 0):.1f}pt** "
                         f"| {c.get('pearl_tier') or '—'} | {float(c.get('composite_score') or 0):.0f} "
                         f"| {c.get('sector_name', '')} |")
        lines.append("")

    # ── フッター統計 ──────────────────────────────────────────────
    n_ss = len(ss_stocks)
    n_s  = sum(1 for c in candidates if c.get("pearl_tier") == "S")
    n_a  = sum(1 for c in candidates if c.get("pearl_tier") == "A")
    n_b  = sum(1 for c in candidates if c.get("pearl_tier") == "B")
    lines.append(
        f"*発見: SS={n_ss} / S={n_s} / A={n_a} / B={n_b}"
        f"（計{len(candidates)}銘柄）"
        f" | D1 方向性 / D2 スマートマネー（アナリスト目標・機関） / D3 利益品質 / D4 発見Gap（小型・未発見）"
        f" / D5 割安度（PER・PBR・FCF利回り）50%＋逆張り（市場比の割負け）50%*"
    )
    lines.append("")
    return lines

# ==============================================================
# BLOCK 8.8: Wave 2 新規セクション
# ==============================================================

def _section_analyst_revision(results: dict, cfg=None) -> list:
    """
    [Wave 2 - FE-2] アナリスト注目度変化セクション。

    screener 通過銘柄の revision_score を集計し、
    セクター別の注目度変化をランキング表示する。
    PEAD_TRIGGER_MIN_REVISION を超えるセクターを強調。
    """
    try:
        lines = ["## 📊 アナリスト注目度変化（Wave 2 - FE-2）", ""]
        screener  = results.get("screener", {})
        candidates = (
            screener.get("passed")
            or [c for c in screener.get("candidates", [])
                if c.get("pass_filters")]
        )
        if not candidates:
            lines.append("*スクリーナーデータなし*")
            lines.append("")
            return lines

        min_rev = float(getattr(cfg, "PEAD_TRIGGER_MIN_REVISION", 0.3)
                        if cfg else 0.3)

        # セクター別 revision_score 集計
        from collections import defaultdict as _dd
        _rev_by_sec: dict = _dd(list)
        _names: dict      = {}
        for c in candidates:
            sc = str(c.get("sector_code", "その他"))
            rv = c.get("revision_score")
            if rv is not None:
                _rev_by_sec[sc].append(float(rv))
                _names[sc] = c.get("sector_name", sc)

        if not _rev_by_sec:
            lines.append("*revision_score データなし*")
            lines.append("")
            return lines

        # セクター別平均を算出してソート
        _sec_avgs = [
            (sc, sum(rvs) / len(rvs), _names.get(sc, sc))
            for sc, rvs in _rev_by_sec.items()
        ]
        _sec_avgs.sort(key=lambda x: x[1], reverse=True)

        # テーブル出力
        lines.append("| 業種 | 平均修正スコア | 対象銘柄数 | PEAD発動 |")
        lines.append("|------|--------------|-----------|---------|")
        for sc, avg, name in _sec_avgs[:10]:
            n     = len(_rev_by_sec[sc])
            pead  = "🚀 Yes" if avg >= min_rev else "-"
            mark  = "⭐" if avg >= min_rev else ""
            lines.append(
                f"| {name[:10]} | {avg:+.2f}pt{mark} | {n}社 | {pead} |"
            )
        lines.append("")

        # 個別銘柄: 上位 / 下位
        by_rev = sorted(
            [c for c in candidates if c.get("revision_score") is not None],
            key=lambda x: x.get("revision_score", 0),
            reverse=True,
        )
        if by_rev:
            lines.append("**📈 アナリスト注目度UP（上位5銘柄）**")
            lines.append("")
            for c in by_rev[:5]:
                rv = c.get("revision_score", 0)
                if rv <= 0:
                    break
                lines.append(
                    f"- {c.get('name', c.get('ticker',''))[:14]}"
                    f"（{c.get('ticker','')}）: "
                    f"revision={rv:+.2f}pt "
                    f"composite={c.get('composite_score', 0):.0f}点"
                )
            lines.append("")

            dn = [c for c in by_rev if (c.get("revision_score") or 0) < 0]
            if dn:
                lines.append("**📉 アナリスト注目度DOWN（下位3銘柄）**")
                lines.append("")
                for c in sorted(dn, key=lambda x: x.get("revision_score", 0))[:3]:
                    rv = c.get("revision_score", 0)
                    lines.append(
                        f"- {c.get('name', c.get('ticker',''))[:14]}"
                        f"（{c.get('ticker','')}）: "
                        f"revision={rv:+.2f}pt"
                    )
                lines.append("")

        return lines

    except Exception:
        return ["## 📊 アナリスト注目度変化", "", "*生成エラー*", ""]


def _section_ignition_alert(results: dict, cfg=None) -> list:
    """
    [Wave 2 - FE-4] 今週の収束シグナル（Ignition Alert）セクション。

    results["ignition"] または ignition_alerts.json から
    アラートを読み込み、収束スコアとカテゴリ別内訳を表示する。
    """
    try:
        lines = ["## 🔥 今週の収束シグナル（Ignition Alert）", ""]

        # データ取得（results優先 → JSON fallback）
        ign_alerts: list = []
        ign_result = results.get("ignition", {})
        if ign_result:
            ign_alerts = ign_result.get("alerts", [])

        if not ign_alerts:
            try:
                from ignition_detector import load_current_alerts as _lca
                ign_alerts = _lca(cfg)
            except Exception:
                pass

        if not ign_alerts:
            lines.append("*今週の収束シグナルなし*")
            lines.append("")
            return lines

        min_sc   = float(getattr(cfg, "IGNITION_MIN_SCORE_LINE",       3.0)
                         if cfg else 3.0)

        # サマリー
        n_total  = len(ign_alerts)
        n_crit   = sum(1 for a in ign_alerts if a.get("is_critical"))
        lines.append(
            f"**検出数**: {n_total}業種"
            f"（うち最高警戒🚨: {n_crit}件）"
        )
        lines.append("")

        # 詳細テーブル
        lines.append("| 業種 | 収束スコア | Cat-A 資金 | Cat-B ナラ | Cat-C 価格 | 警戒 |")
        lines.append("|------|-----------|-----------|-----------|-----------|------|")
        for a in sorted(ign_alerts,
                        key=lambda x: x.get("convergence_score", 0),
                        reverse=True):
            name  = a.get("sector_name", a.get("sector_code", ""))[:10]
            csc   = a.get("convergence_score", 0.0)
            sa    = a.get("category_a", {}).get("score", 0.0)
            sb    = a.get("category_b", {}).get("score", 0.0)
            sc_   = a.get("category_c", {}).get("score", 0.0)
            crit  = "🚨最高警戒" if a.get("is_critical") else (
                    "🔥発火" if csc >= min_sc else "-")
            lines.append(
                f"| {name} | **{csc:.2f}** "
                f"| {sa:.2f} | {sb:.2f} | {sc_:.2f} | {crit} |"
            )
        lines.append("")

        # 最高警戒セクターの詳細
        crit_alerts = [a for a in ign_alerts if a.get("is_critical")]
        if crit_alerts:
            lines.append("### 🚨 最高警戒セクター 詳細")
            lines.append("")
            for a in crit_alerts:
                name = a.get("sector_name", a.get("sector_code", ""))
                lines.append(f"**{name}** — convergence_score: {a.get('convergence_score', 0):.2f}")
                for cat, label in [("category_a", "資金フロー"),
                                   ("category_b", "ナラティブ"),
                                   ("category_c", "価格")]:
                    cat_d = a.get(cat, {})
                    sigs  = cat_d.get("signals", [])
                    if sigs:
                        lines.append(
                            f"- {label}({cat_d.get('score',0):.2f}): "
                            + " / ".join(sigs[:3])
                        )
                lines.append("")

        return lines

    except Exception:
        return ["## 🔥 今週の収束シグナル（Ignition Alert）", "", "*生成エラー*", ""]

# ==============================================================
# BLOCK 8.9: Wave 3 新規セクション
# ==============================================================

def _section_latent_pearl_promotion(results: dict, cfg=None) -> list:
    """
    [Wave 3/7 - Independence] 潜伏銘柄プロモーションセクション。
    Wave 7: latent_pearl_cache.json を直接参照し、
    潜伏Pearlスコア・カタリストフラグ・EDINETセグメント文脈を詳細表示する。
    """
    try:
        lines = ["## 💎 潜伏銘柄プロモーション（Wave 7）", ""]

        # ── データ取得 ────────────────────────────────────────────
        bridge     = results.get("jp_us_bridge", {})
        rot_phases = bridge.get("rotation_phases", {})
        reversal_secs = {
            sc for sc, info in rot_phases.items()
            if info.get("phase") in ("IMPROVING", "LEADING")
        }
        cross_latent = bridge.get("cross_market_latent", [])

        # Wave 7: latent_pearl_cache から取得
        latent_entries: list = []
        promoted_tickers: list = []
        try:
            from latent_pearl_manager import (
                get_latent_pearls_list as _gll,
                get_promoted_tickers   as _gpt,
            )
            latent_entries   = _gll(cfg, min_score=30.0)
            promoted_tickers = _gpt(cfg)
        except Exception:
            # フォールバック: results から取得
            lp_res = results.get("latent_pearl", {})
            latent_entries = list(lp_res.get("latent_pearls", {}).values())
            promoted_tickers = lp_res.get("promoted", [])

        if not latent_entries and not cross_latent:
            lines.append("*潜伏銘柄なし*")
            lines.append("")
            return lines

        # ── 統計サマリー ──────────────────────────────────────────
        n_total    = len(latent_entries)
        n_promoted = len(promoted_tickers)
        n_eligible = sum(1 for e in latent_entries if e.get("promotion_eligible"))
        lines.append(
            f"**潜伏銘柄={n_total}件 / 昇格対象={n_eligible}件 / 昇格済={n_promoted}件**"
        )
        lines.append("")

        # ── 昇格対象銘柄テーブル ──────────────────────────────────
        eligible = [e for e in latent_entries if e.get("promotion_eligible")]
        if eligible:
            lines.append("### 🚀 昇格対象銘柄（latent_pearl_score ≥ 70）")
            lines.append("")
            lines.append(
                "| 銘柄 | LP Score | 独立性 | カタリスト | 中計 | 検出条件 |"
            )
            lines.append(
                "|------|---------|-------|----------|-----|---------|"
            )
            for e in sorted(eligible, key=lambda x: x.get("latent_pearl_score", 0), reverse=True)[:8]:
                ticker   = e.get("ticker", "")
                lp_score = float(e.get("latent_pearl_score", 0))
                ind      = float(e.get("independence_score", 0))
                cat      = float(e.get("catalyst_score", 0))
                mt_score = float(e.get("midterm_score", 0))
                mt_traj  = e.get("midterm_trajectory", "")
                conds    = "/".join(e.get("detection_conditions", []))
                traj_icon = {"ahead": "🚀", "on_track": "✅", "behind": "⚠️", "critical": "🔴"}.get(mt_traj, "")
                cat_flags = e.get("catalyst_flags", [])
                cat_str   = " ".join(str(f).split("（")[0] for f in cat_flags[:2])
                lines.append(
                    f"| **{ticker}** "
                    f"| **{lp_score:.1f}** "
                    f"| {ind:.0f}pt "
                    f"| {cat:.1f}pt {cat_str[:15]} "
                    f"| {traj_icon}{mt_score:.0f}pt "
                    f"| {conds} |"
                )
            lines.append("")

        # ── Wave 7: size×taxonomy コンテキスト付き潜伏候補 ────────
        tax_notable = [
            e for e in latent_entries
            if float(e.get("latent_pearl_score", 0)) >= 30.0
            and e.get("detection_conditions")
        ]
        if tax_notable:
            lines.append("### 📋 潜伏銘柄詳細（Wave 7 コンテキスト）")
            lines.append("")
            for e in tax_notable[:6]:
                ticker      = e.get("ticker", "")
                lp_score    = float(e.get("latent_pearl_score", 0))
                cross_str   = e.get("cross_signal_strength", 0)
                cross_types = " / ".join(e.get("cross_signal_types", [])[:2])
                cat_flags   = e.get("catalyst_flags", [])
                cat_detail  = " / ".join(str(f) for f in cat_flags[:3])
                mt_traj     = e.get("midterm_trajectory") or ""
                conds       = e.get("detection_conditions", [])
                promoted    = "✅昇格済" if ticker in promoted_tickers else ""
                lines.append(
                    f"**{ticker}** {promoted} — LP={lp_score:.1f}pt"
                )
                lines.append(
                    f"  検出条件: {'/'.join(conds)} | "
                    f"cross_signal: {cross_str:.2f}（{cross_types}）"
                )
                if cat_detail:
                    lines.append(f"  カタリスト: {cat_detail}")
                if mt_traj:
                    traj_icon = {"ahead": "🚀", "on_track": "✅", "behind": "⚠️", "critical": "🔴"}.get(mt_traj, "")
                    lines.append(f"  中計軌道: {traj_icon} {mt_traj}")
                lines.append("")

        # ── クロスマーケット潜伏銘柄 ──────────────────────────────
        if cross_latent:
            lines.append("### 🌐 クロスマーケット潜伏候補（JP/US独立性高）")
            lines.append("")
            for cl in cross_latent[:5]:
                lines.append(
                    f"- [{cl['ticker']}] "
                    f"独立性={cl.get('independence_score', 0):.0f}pt "
                    f"US先行adj={cl.get('us_lead_adj', 0):+.1f}pt "
                    f"cross_size={cl.get('cross_size_type', '-')} "
                    f"信頼度={cl.get('confidence', '-')} "
                    f"Tier={cl.get('size_tier', '-')}"
                )
            lines.append("")

        # ── セクター反転 × 潜伏銘柄の重複 ───────────────────────
        if reversal_secs:
            lines.append(f"**🔄 セクター反転検出: {', '.join(sorted(reversal_secs)[:5])}**")
            lines.append("")

        return lines

    except Exception:
        return ["## 💎 潜伏銘柄プロモーション", "", "*生成エラー*", ""]

# ==============================================================
# BLOCK 8.9-B: Wave 7 新規セクション（市場形成前シグナル / size×taxonomy）
# ==============================================================

def _section_wave7_premarket_signals(results: dict, cfg=None) -> list:
    """
    [Wave 7] 市場形成前シグナルセクション。
    学術発表・標準化動向・中計達成率の3インプット層を統合表示する。
    """
    try:
        lines = ["## 📡 市場形成前シグナル（Wave 7）", ""]
        trackers = results.get("wave7_trackers", {})

        # ── 標準化動向 ────────────────────────────────────────────
        std_data = trackers.get("standardization", {})
        if std_data and not std_data.get("cached") is False or std_data:
            top5_std = std_data.get("stats", {}).get("top_5_companies", [])
            total_std = std_data.get("stats", {}).get("total_standards_tracked", 0)
            lines.append(f"### 📋 標準化動向（3GPP / ISO / IEEE）— {total_std}標準追跡中")
            lines.append("")
            if top5_std:
                lines.append("| 銘柄 | 標準化スコア |")
                lines.append("|------|------------|")
                for e in top5_std[:5]:
                    lines.append(f"| {e.get('ticker', '')} | {e.get('score', 0):.1f}pt |")
            else:
                lines.append("*データ取得中*")
            lines.append("")

        # ── 学術カンファレンス ────────────────────────────────────
        ac_data = trackers.get("academic", {})
        if ac_data:
            top5_ac = ac_data.get("stats", {}).get("top_5_companies", [])
            total_papers = ac_data.get("stats", {}).get("total_papers", 0)
            lines.append(f"### 🎓 学術カンファレンス（ISSCC / IEDM / NeurIPS 等）— {total_papers}論文")
            lines.append("")
            if top5_ac:
                lines.append("| 銘柄 | 学術スコア |")
                lines.append("|------|----------|")
                for e in top5_ac[:5]:
                    lines.append(f"| {e.get('ticker', '')} | {e.get('score', 0):.1f}pt |")
            else:
                lines.append("*データ取得中*")
            lines.append("")

        # ── 中計達成率 ────────────────────────────────────────────
        mt_data = trackers.get("midterm", {})
        if mt_data:
            plans     = mt_data.get("plans", {})
            at_risk   = mt_data.get("stats", {}).get("at_risk_tickers", [])
            high_list = [
                (t, p.get("overall_score", 0), p.get("trajectory", ""))
                for t, p in plans.items()
                if p.get("achievement_tier") == "HIGH"
            ]
            lines.append(f"### 📊 中計達成率（{len(plans)}社追跡）")
            lines.append("")
            if high_list:
                lines.append("**HIGH達成銘柄:**")
                for ticker, score, traj in sorted(high_list, key=lambda x: -x[1])[:5]:
                    icon = {"ahead": "🚀", "on_track": "✅"}.get(traj, "")
                    lines.append(f"  - {ticker}: {score:.0f}pt {icon} {traj}")
                lines.append("")
            if at_risk:
                lines.append(f"**⚠️ AT_RISK銘柄: {', '.join(at_risk[:5])}**")
                lines.append("")

        if not std_data and not ac_data and not mt_data:
            lines.append("*Wave 7 トラッカー未実行（Feature Flag OFF）*")
            lines.append("")

        return lines

    except Exception:
        return ["## 📡 市場形成前シグナル", "", "*生成エラー*", ""]


def _section_wave7_size_taxonomy(results: dict, cfg=None) -> list:
    """
    [Wave 7 / Size Ph4] size × taxonomy 比較セクション。
    各規模帯の taxonomy 別補正係数と有効シグナルを表示する。
    """
    try:
        lines = ["## 📐 規模×業種（Size × Taxonomy）分析（Wave 7）", ""]

        # size_decay_state.json から読み込み
        import json as _j
        from pathlib import Path as _p
        cache_dir = _p(getattr(cfg, "CACHE_DIR", "stock_data/cache")) if cfg else _p("stock_data/cache")
        decay_p   = cache_dir / "size_decay_state.json"

        if not decay_p.exists():
            lines.append("*size_decay_state.json 未生成（size_decay_manager 未実行）*")
            lines.append("")
            return lines

        decay_data = _j.loads(decay_p.read_text(encoding="utf-8"))
        tickers_data = decay_data.get("tickers", {})

        # Tier別集計
        tier_order = ["Large", "Mid", "Small", "Micro", "Nano"]
        tier_stats: dict = {t: {"count": 0, "avg_eff": 0.0, "avg_corr": 0.0, "vals": []} for t in tier_order}

        for tk, entry in tickers_data.items():
            tier = entry.get("size_tier", "Mid")
            if tier not in tier_stats:
                continue
            eff  = float(entry.get("effective_signal", 0.0))
            corr = float(entry.get("taxonomy_size_correction", 1.0))
            tier_stats[tier]["count"] += 1
            tier_stats[tier]["vals"].append((eff, corr))

        lines.append("### Tier別 有効シグナル × taxonomy補正 サマリー")
        lines.append("")
        lines.append("| Tier | 銘柄数 | avg有効Signal | avg補正係数 | 半減期 |")
        lines.append("|------|-------|-------------|-----------|-------|")

        from size_decay_manager import _DECAY_HALF_LIFE  # 定数参照
        for tier in tier_order:
            st   = tier_stats[tier]
            vals = st["vals"]
            if not vals:
                continue
            avg_eff  = round(sum(v[0] for v in vals) / len(vals), 3)
            avg_corr = round(sum(v[1] for v in vals) / len(vals), 3)
            half     = _DECAY_HALF_LIFE.get(tier, 0)
            lines.append(
                f"| {tier} | {len(vals)} | {avg_eff:.3f} | {avg_corr:.3f}× | {half:.1f}日 |"
            )
        lines.append("")

        # taxonomy×size で補正係数が高い上位銘柄
        top_corr = sorted(
            [(tk, float(e.get("taxonomy_size_correction", 1.0)), e.get("size_tier", ""))
             for tk, e in tickers_data.items()],
            key=lambda x: -x[1]
        )[:8]
        if top_corr:
            lines.append("### taxonomy×size 補正上位銘柄（高ブースト）")
            lines.append("")
            lines.append("| 銘柄 | Tier | 補正係数 |")
            lines.append("|------|------|---------|")
            for tk, corr, tier in top_corr:
                if corr > 1.0:
                    lines.append(f"| {tk} | {tier} | **{corr:.3f}×** |")
            lines.append("")

        return lines

    except Exception:
        return ["## 📐 規模×業種分析", "", "*生成エラー*", ""]


def _section_wave7_independence(results: dict, cfg=None) -> list:
    """
    [Wave 7] 市場独立性スコアセクション。
    independence_scores.json から HIGH/MID 銘柄を表示する。
    """
    try:
        lines = ["## 📐 市場独立性スコア（Wave 7）", ""]

        import json as _j
        from pathlib import Path as _p
        cache_dir = _p(getattr(cfg, "CACHE_DIR", "stock_data/cache")) if cfg else _p("stock_data/cache")
        ind_p     = cache_dir / "independence_scores.json"

        if not ind_p.exists():
            lines.append("*independence_scores.json 未生成*")
            lines.append("")
            return lines

        ind_data = _j.loads(ind_p.read_text(encoding="utf-8"))
        scores   = ind_data.get("scores", {})
        stats    = ind_data.get("stats", {})

        lines.append(
            f"**追跡銘柄={stats.get('total_tickers', 0)} / "
            f"HIGH独立={stats.get('high_independence_count', 0)} / "
            f"平均|β|={stats.get('avg_beta_abs', 0):.3f}**"
        )
        lines.append("")

        # HIGH独立銘柄テーブル
        high_list = sorted(
            [e for e in scores.values() if e.get("independence_tier") == "HIGH"],
            key=lambda x: x.get("combined_score", 0),
            reverse=True,
        )[:8]

        if high_list:
            lines.append("### HIGH独立銘柄（β低 × α高 × カタリスト）")
            lines.append("")
            lines.append("| 銘柄 | 独立性 | β60d | α年率 | 複合スコア | カタリスト |")
            lines.append("|------|-------|------|------|----------|---------|")
            for e in high_list:
                ticker  = e.get("ticker", "")
                ind_sc  = e.get("independence_score", 0)
                beta    = e.get("beta_60d", 0)
                alpha_p = e.get("alpha_annual_pct", 0)
                comb    = e.get("combined_score", 0)
                cats    = " / ".join(
                    str(f).split("（")[0] for f in e.get("catalyst_flags", [])[:2]
                )
                lines.append(
                    f"| **{ticker}** | {ind_sc:.0f}pt "
                    f"| {beta:.3f} | {alpha_p:+.1f}% "
                    f"| **{comb:.0f}pt** | {cats[:20]} |"
                )
        else:
            lines.append("*HIGH独立銘柄なし（データ蓄積中）*")

        lines.append("")
        return lines

    except Exception:
        return ["## 📐 市場独立性スコア", "", "*生成エラー*", ""]

def _section_wave7_pearl_backtest(results: dict, cfg=None) -> list:
    """
    [Wave 7] Pearl / 潜伏Pearl バックテスト結果セクション。
    self_evaluator.py が生成した pearl_backtest_log.json を参照して表示する。
    """
    try:
        lines = ["## 💎 Pearl バックテスト検証（Wave 7）", ""]

        # pearl_backtest_log.json を直接読み込む
        import json as _j
        from pathlib import Path as _p
        cache_dir = _p(getattr(cfg, "CACHE_DIR", "stock_data/cache")) if cfg else _p("stock_data/cache")
        bt_path   = cache_dir / "pearl_backtest_log.json"

        # results["self_evaluator"] からも取得を試みる
        se_result    = results.get("self_evaluator", {})
        bt_summary   = se_result.get("pearl_bt_summary", {})
        latest_bt    = se_result.get("pearl_backtest", {})

        if bt_path.exists():
            try:
                bt_data    = _j.loads(bt_path.read_text(encoding="utf-8"))
                bt_summary = bt_summary or bt_data.get("summary", {})
                bt_entries = bt_data.get("entries", [])
                latest_bt  = latest_bt or (bt_entries[-1] if bt_entries else {})
            except Exception:
                bt_entries = []
        else:
            bt_entries = []

        if not bt_summary and not latest_bt:
            lines.append("*pearl_backtest_log.json 未生成（self_evaluator 未実行 or Pearl BT データ蓄積中）*")
            lines.append("")
            return lines

        # ── 生涯サマリー ────────────────────────────────────────
        if bt_summary:
            n_weeks   = bt_summary.get("total_weeks_evaluated", 0)
            lt_hr     = bt_summary.get("lifetime_latent_hr")
            lt_alp    = bt_summary.get("avg_latent_alpha")
            vs_non    = bt_summary.get("avg_vs_non_latent")
            trend     = bt_summary.get("trend", "—")
            trend_icon = {"improving": "📈", "degrading": "📉", "stable": "➡️", "accumulating": "📦"}.get(trend, "")

            lt_hr_str  = f"{lt_hr:.1%}"   if lt_hr  is not None else "—"
            lt_alp_str = f"{lt_alp:+.2f}%" if lt_alp is not None else "—"
            vs_str     = f"{vs_non:+.3f}%" if vs_non is not None else "—"

            lines.append(f"**生涯検証 {n_weeks}週分** | 潜伏Pearl的中率: **{lt_hr_str}** | 平均α: **{lt_alp_str}** | 非潜伏比較: **{vs_str}**")
            lines.append(f"精度トレンド: {trend_icon} {trend}")
            lines.append("")

        # ── 直近週の詳細 ────────────────────────────────────────
        if latest_bt:
            week_str = latest_bt.get("week", "—")
            lines.append(f"### 最新週（{week_str}）詳細")
            lines.append("")

            lp_stats = latest_bt.get("latent_pearl_stats", {})
            n_lat    = lp_stats.get("n_latent", 0) or latest_bt.get("n_latent_evaluated", 0)
            lp_hr    = lp_stats.get("hit_rate") or latest_bt.get("latent_hit_rate")
            lp_alp   = lp_stats.get("avg_alpha") or latest_bt.get("latent_avg_alpha")
            hi_hr    = lp_stats.get("high_indep_hit_rate") or latest_bt.get("high_indep_hr")
            cat_hr   = lp_stats.get("catalyst_hit_rate") or latest_bt.get("catalyst_hr")
            mt_hr    = lp_stats.get("midterm_ahead_hit_rate") or latest_bt.get("midterm_ahead_hr")

            lp_hr_str  = f"{lp_hr:.1%}"   if lp_hr  is not None else "—"
            lp_alp_str = f"{lp_alp:+.2f}%" if lp_alp is not None else "—"

            lines.append(f"潜伏Pearl評価件数: {n_lat}件 | 的中率: **{lp_hr_str}** | 平均α: **{lp_alp_str}**")
            lines.append("")

            # サブセット別的中率
            subset_rows = []
            if hi_hr  is not None: subset_rows.append(("高独立性（indep≥65）",  hi_hr))
            if cat_hr is not None: subset_rows.append(("カタリスト有り",        cat_hr))
            if mt_hr  is not None: subset_rows.append(("中計ahead",           mt_hr))

            if subset_rows:
                lines.append("| サブセット | 的中率 |")
                lines.append("|----------|-------|")
                for label, hr in subset_rows:
                    lines.append(f"| {label} | {hr:.1%} |")
                lines.append("")

            # Pearl Tier別
            p_stats = latest_bt.get("pearl_stats", {})
            tier_rows = [(t, p_stats.get(t, {})) for t in ["SS", "S", "A", "all_pearl"] if p_stats.get(t, {}).get("n", 0) > 0]
            if tier_rows:
                lines.append("| Pearl Tier | 件数 | 的中率 | 平均α |")
                lines.append("|-----------|------|-------|------|")
                for tier, ps in tier_rows:
                    n   = ps.get("n", 0)
                    hr  = ps.get("hit_rate")
                    avg = ps.get("avg_alpha")
                    lines.append(
                        f"| {'**全Pearl**' if tier == 'all_pearl' else tier} "
                        f"| {n} "
                        f"| {f'{hr:.1%}' if hr is not None else '—'} "
                        f"| {f'{avg:+.2f}%' if avg is not None else '—'} |"
                    )
                lines.append("")

        return lines

    except Exception:
        return ["## 💎 Pearl バックテスト検証", "", "*生成エラー*", ""]

def _section_pead_drift_candidates(results: dict, cfg=None) -> list:
    """
    [Wave 3 - FE-5] 決算後ドリフト候補セクション。

    PEAD Monitor の alert_candidates（正方向）と
    negative_candidates（陰性・回避候補）を表示する。
    """
    try:
        lines = ["## 📈 決算後ドリフト候補（PEAD Monitor）", ""]

        pead_result = results.get("pead", {})
        pead_summary = pead_result.get("summary", {})

        if not pead_result and not pead_summary:
            # フォールバック: キャッシュから直接取得
            try:
                from pead_monitor import get_pead_summary as _gps
                pead_summary = _gps(cfg)
            except Exception:
                pass

        total    = pead_summary.get("total", 0)
        pos      = pead_summary.get("positive", 0)
        neg      = pead_summary.get("negative", 0)
        n_alert  = pead_summary.get("alert_count", 0)

        lines.append(
            f"**追跡中**: {total}銘柄"
            f"（正方向{pos}件 / 陰性{neg}件 / アラート{n_alert}件）"
        )
        lines.append("")

        # 正 PEAD 上位（アラート候補）
        top_pos = pead_summary.get("top_positive", [])
        if top_pos:
            lines.append("### 📈 ドリフト継続候補（買い方向）")
            lines.append("")
            lines.append(
                "| 銘柄 | PEADスコア | 出来高倍率 | ドリフト率 | 検出日数 | アラート |"
            )
            lines.append(
                "|------|-----------|-----------|-----------|---------|---------|"
            )
            for c in top_pos:
                alert_mark = "🚨" if c.get("is_alert") else "-"
                lines.append(
                    f"| {c.get('ticker', '')} "
                    f"| {c.get('pead_score', 0):.1f}pt "
                    f"| {c.get('volume_ratio', 0):.2f}x "
                    f"| {c.get('price_drift', 0)*100:+.1f}% "
                    f"| {c.get('days_since_detect', 0)}日 "
                    f"| {alert_mark} |"
                )
            lines.append("")

        # 陰性 PEAD（回避候補）
        top_neg = pead_summary.get("top_negative", [])
        if top_neg:
            lines.append("### 📉 ドリフト継続懸念（回避候補）")
            lines.append("")
            for c in top_neg:
                lines.append(
                    f"- [{c.get('ticker', '')}] "
                    f"PEADスコア={c.get('pead_score', 0):.1f}pt "
                    f"ドリフト={c.get('price_drift', 0)*100:+.1f}%"
                )
            lines.append("")

        return lines

    except Exception:
        return ["## 📈 決算後ドリフト候補（PEAD Monitor）", "", "*生成エラー*", ""]


def _section_portfolio_concentration(results: dict, cfg=None) -> list:
    """
    [Wave 3 - FE-6] ポートフォリオ集中警告セクション。

    HHI・高相関ペア・Ignition 抑制状況を表示する。
    HHI が WARNING 未満かつ高相関ペアが少ない場合は省略表示。
    """
    try:
        lines = ["## 🔀 ポートフォリオ集中リスク（FE-6）", ""]

        # 2026-10-05: 実際の保有（円評価額）の集中度を先に出す
        try:
            from portfolio_diversity_filter import load_holdings_concentration as _lhc
            _hc = _lhc()
        except Exception:
            _hc = {}
        if _hc.get("total_jpy"):
            def _st(h, warn=0.25, crit=0.35):
                return "🚨 危険" if h >= crit else ("⚠️ 警告" if h >= warn else "✅ 正常")
            lines.append(f"### 💼 保有銘柄の集中度（株式評価額 {_hc['total_jpy']/1e4:,.0f}万円）")
            lines.append("")
            lines.append(f"- 業種HHI: **{_hc['hhi_sector']:.3f}** {_st(_hc['hhi_sector'])}"
                         f" / 銘柄HHI: **{_hc['hhi_stock']:.3f}** {_st(_hc['hhi_stock'])}"
                         f" / 上位3銘柄の比率: **{_hc['top3_share']:.0%}**")
            _mw = _hc.get("market_weights") or {}
            lines.append(f"- 日本株 {_mw.get('JP', 0):.0%} / 米国株 {_mw.get('US', 0):.0%}"
                         "（HHI: 0.25未満=分散十分 / 0.25〜0.35=警告 / 0.35以上=危険）")
            lines.append("")
            lines.append("| 業種 | 比率 |")
            lines.append("|---|---|")
            for _k, _v in list((_hc.get("sector_weights") or {}).items())[:6]:
                lines.append(f"| {_k} | {_v:.1%} |")
            lines.append("")
            lines.append("| ティッカー | 銘柄名 | 比率 | 評価額 |")
            lines.append("|---|---|---|---|")
            for _h in _hc.get("top_holdings") or []:
                lines.append(f"| {_h['ticker']} | {_h.get('name') or ''} | {_h['weight']:.1%} | {_h['value_jpy']/1e4:,.0f}万円 |")
            lines.append("")
            lines.append("### 🔎 スクリーナー候補の業種の偏り")
            lines.append("")

        div = results.get("diversity", {})
        if not div:
            lines.append("*多様性フィルターデータなし（週次実行時に生成）*")
            lines.append("")
            return lines

        hhi        = float(div.get("hhi", 0))
        hhi_status = div.get("hhi_status", "UNKNOWN")
        n_pairs    = int(div.get("high_corr_pair_count", 0))
        div_score  = div.get("diversity_score", 50.0)
        suppressed = div.get("alert_suppressed", False)

        # ステータス表示
        status_mark = (
            "🚨" if hhi_status == "CRITICAL"
            else "⚠️" if hhi_status == "WARNING"
            else "✅"
        )
        lines.append(
            f"**HHI**: {hhi:.3f} {status_mark}[{hhi_status}] "
            f"| 多様性スコア: {div_score:.0f}pt "
            f"| 高相関ペア: {n_pairs}組"
        )
        if suppressed:
            lines.append(
                "*Ignition収束検出中のため集中警告を抑制（意図的収束）*"
            )
        lines.append("")

        # 集中業種
        concentrated = div.get("concentrated_sectors", [])
        if concentrated:
            lines.append("**集中業種（ウェイト20%以上）:**")
            lines.append("")
            lines.append("| 業種コード | ウェイト | 銘柄数 | Ignition発火 |")
            lines.append("|-----------|---------|-------|------------|")
            ign_overlap = {
                s.get("sector_code", "") if isinstance(s, dict) else ""
                for s in div.get("ignition_overlap", [])
            }
            for s in concentrated:
                sc       = s.get("sector_code", "")
                ign_mark = "🔥" if sc in ign_overlap else "-"
                lines.append(
                    f"| {sc} "
                    f"| {s.get('weight', 0)*100:.1f}% "
                    f"| {s.get('count', 0)}社 "
                    f"| {ign_mark} |"
                )
            lines.append("")

        # 高相関ペア上位
        high_pairs = div.get("high_corr_pairs", [])[:5]
        if high_pairs:
            lines.append("**高相関ペア（上位5組）:**")
            lines.append("")
            for p in high_pairs:
                rho = p.get("correlation", 0)
                lines.append(
                    f"- {p.get('ticker1', '')} × {p.get('ticker2', '')} "
                    f"ρ={rho:.3f}"
                )
            lines.append("")

        return lines

    except Exception:
        return ["## 🔀 ポートフォリオ集中リスク（FE-6）", "", "*生成エラー*", ""]


def _section_catalyst_upcoming(results: dict, cfg=None) -> list:
    """
    [Wave 3 - FE-7] 今後2週間のカタリスト予定セクション。

    global_events（中央銀行・経済指標）と
    Triple Catalyst 銘柄を2週間以内に絞って表示する。
    """
    try:
        lines = ["## 📅 今後2週間のカタリスト予定（FE-7）", ""]

        cat = results.get("catalyst", {})
        if not cat:
            try:
                from catalyst_calendar import load_catalyst_calendar as _lcc
                cat = _lcc(cfg) or {}
            except Exception:
                pass

        if not cat:
            lines.append("*カタリストカレンダーデータなし（週次実行時に生成）*")
            lines.append("")
            return lines

        # ── 14日以内グローバルイベント ──────────────────────────
        upcoming = [
            e for e in cat.get("upcoming_14d", [])
            if 0 <= e.get("days_until", 9999) <= 14
        ]
        if upcoming:
            lines.append("### 🌐 グローバルカタリスト（14日以内）")
            lines.append("")
            lines.append("| 日付 | 残日数 | 種別 | 内容 | 市場 |")
            lines.append("|------|-------|------|-----|------|")
            for e in sorted(upcoming, key=lambda x: x.get("days_until", 999)):
                lines.append(
                    f"| {e.get('date', '')} "
                    f"| {e.get('days_until', '')}日後 "
                    f"| {e.get('subtype', e.get('type', ''))} "
                    f"| {e.get('description', '')[:30]} "
                    f"| {e.get('market', '')} |"
                )
            lines.append("")

        # ── Triple Catalyst 銘柄 ──────────────────────────────
        triple_t = cat.get("triple_tickers", [])
        if triple_t:
            lines.append(
                f"### ⭐ Triple Catalyst 銘柄（{len(triple_t)}件）"
            )
            lines.append("")
            lines.append(
                "*14日以内に3種以上のカタリストが重複している銘柄*"
            )
            lines.append("")
            lines.append(
                "| 銘柄 | カタリスト数 | 最短日数 | 種別 | proximity |"
            )
            lines.append(
                "|------|------------|---------|------|----------|"
            )
            for t in triple_t[:8]:
                types_str = "+".join(t.get("types", [])[:3])
                lines.append(
                    f"| {t.get('ticker', '')} "
                    f"| {t.get('catalyst_count', 0)}種 "
                    f"| {t.get('min_days', 0)}日後 "
                    f"| {types_str} "
                    f"| {t.get('max_prox', 0):.1f} |"
                )
            lines.append("")

        # ── Triple Catalyst セクター ──────────────────────────
        triple_s = cat.get("triple_sectors", [])
        if triple_s:
            lines.append(
                f"### 🏭 Triple Catalyst セクター（{len(triple_s)}件）"
            )
            lines.append("")
            for s in triple_s[:5]:
                lines.append(
                    f"- セクター{s.get('sector_code', '')} "
                    f"[{'+'.join(s.get('types', []))}] "
                    f"{s.get('catalyst_count', 0)}種重複"
                )
            lines.append("")

        return lines

    except Exception:
        return ["## 📅 今後2週間のカタリスト予定（FE-7）", "", "*生成エラー*", ""]

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
            # roe は %値（上の表と同じ理由）。0.10 判定だと ROE 0.2% でも「ROE高」になっていた。
            if c.get("roe") and c["roe"] >= 10:       reason_parts.append(f"ROE高({c['roe']:.0f}%)")
            if c.get("analyst_upside") and c["analyst_upside"] >= 0.15:
                reason_parts.append(f"上昇余地({c['analyst_upside']*100:+.0f}%)")
            if c.get("dividend_yield") and c["dividend_yield"] >= 0.03:
                reason_parts.append(f"高配当({c['dividend_yield']*100:.1f}%)")
            reason = " / ".join(reason_parts) if reason_parts else "スクリーニング通過"
            lines.append(f"- **{name}** スコア{score:.0f}点: {reason}")
        lines.append("")

    # [P-3] 長期保有候補（片山晃モデル「10年持てる銘柄」）
    # 2026-07-25 バッチ1: screener が longterm_top を生成していたのに
    # どのレポートにも載らず、成果物が誰にも届いていなかったため追加。
    longterm_top = screener.get("longterm_top", []) or []
    if longterm_top:
        lines.append("### 🌱 長期保有候補（P-3: 財務堅牢×増益×高ROE）")
        lines.append("")
        for c in longterm_top[:5]:
            name  = str(c.get("name", c.get("ticker", "")))[:16]
            score = c.get("composite_score", 0) or 0
            lt_flags = c.get("longterm_flags", []) or []
            ev = " / ".join(str(f) for f in lt_flags[:3]) if lt_flags else "条件充足"
            lines.append(f"- **{name}** スコア{score:.0f}点: {ev}")
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
# BLOCK 8.95: Wave 6 - B 新規セクション（技術クラスター）
# ==============================================================

def _section_technology_cluster(results: dict, cfg=None) -> list:
    """
    [Wave 6 - B] 技術クラスター × 萌芽テーマセクションを生成する。

    cluster_phase_cache / emerging_technology_cache の内容を表示する。
    データ未存在時・エラー時は空リストを返す（レポート全体を止めない）。
    """
    try:
        cluster_phase  = results.get("cluster_phase") or {}
        emerging_tech  = results.get("emerging_tech")  or {}
        cluster_export = results.get("cluster_export") or {}

        clusters = cluster_phase.get("clusters", []) or []
        themes   = (emerging_tech.get("emerging_themes", []) or [])[:5]

        # データが両方とも空ならセクション自体を省略
        if not clusters and not themes:
            return []

        lines = ["## 🔬 技術クラスター分析（Wave 6 - B）", ""]

        # ── 1. クラスターフェーズサマリー ──────────────────────────
        if clusters:
            # フェーズ内訳集計
            phase_count: dict[str, int] = {}
            for c in clusters:
                ph = c.get("phase", "unknown")
                phase_count[ph] = phase_count.get(ph, 0) + 1

            phase_labels = {
                "acceleration": "🚀 技術加速",
                "expansion":    "📈 テーマ拡張",
                "mature":       "🟡 成熟",
                "deceleration": "📉 減速",
                "unknown":      "❓ 不明",
            }

            lines.append("### クラスターフェーズ内訳")
            lines.append("")
            for phase, count in sorted(
                phase_count.items(),
                key=lambda x: {"acceleration":0,"expansion":1,
                               "mature":2,"deceleration":3,"unknown":4}.get(x[0],5)
            ):
                label = phase_labels.get(phase, phase)
                lines.append(f"- {label}: **{count}**クラスター")
            lines.append("")

            # 加速・拡張フェーズのクラスターを詳細表示（最大5件）
            accel_clusters = [
                c for c in clusters
                if c.get("phase") in ("acceleration", "expansion")
            ]
            accel_clusters.sort(key=lambda x: -x.get("phase_bonus", 0))

            if accel_clusters:
                lines.append("### 📈 注目クラスター（加速・拡張フェーズ）")
                lines.append("")
                lines.append(
                    "| クラスター | 市場 | 業種数 | フェーズ | ボーナス | 代表技術ノード |"
                )
                lines.append(
                    "|-----------|------|-------|---------|---------|-------------|"
                )
                for c in accel_clusters[:5]:
                    cid        = c.get("cluster_id", "")
                    market     = c.get("market", "JP")
                    sec_count  = c.get("sector_count", len(c.get("sectors", [])))
                    phase_lbl  = phase_labels.get(c.get("phase",""), c.get("phase",""))
                    bonus      = c.get("phase_bonus", 0)
                    centroid   = " / ".join(c.get("centroid_nodes", [])[:3])
                    bonus_str  = f"{bonus:+.1f}pt"
                    lines.append(
                        f"| {cid} | {market} | {sec_count}業種 "
                        f"| {phase_lbl} | {bonus_str} | {centroid} |"
                    )
                lines.append("")

            # upstream / downstream 差分（T-3: サプライチェーン視点）
            # cluster_master の meta から supply_chain_position を集計
            cluster_master = results.get("cluster_master") or {}
            markets_data   = cluster_master.get("markets", {}) or {}

            sc_breakdown: dict[str, list[str]] = {}
            for c in accel_clusters[:5]:
                market   = c.get("market", "JP")
                m_data   = markets_data.get(market, {})
                for sector_code in c.get("sectors", []):
                    meta = m_data.get(str(sector_code), {}).get("meta", {})
                    pos  = meta.get("supply_chain_position", "unknown")
                    sc_breakdown.setdefault(pos, []).append(sector_code)

            if sc_breakdown:
                lines.append("#### サプライチェーン分布（加速クラスター内）")
                lines.append("")
                sc_labels = {
                    "upstream":   "⬆️ 上流",
                    "midstream":  "↔️ 中流",
                    "downstream": "⬇️ 下流",
                    "service":    "🔧 サービス",
                    "financial":  "💴 金融",
                }
                for pos, codes in sc_breakdown.items():
                    lbl = sc_labels.get(pos, pos)
                    lines.append(
                        f"- {lbl}: {len(codes)}業種 "
                        f"（{', '.join(codes[:4])}{'…' if len(codes)>4 else ''}）"
                    )
                lines.append("")

        # ── 2. 萌芽テーマランキング（T-3 / emerging_detector）────────
        if themes:
            lines.append("### 🌱 注目技術テーマ（萌芽検知 TOP5）")
            lines.append("")
            lines.append(
                "| ランク | 技術ノード | 萌芽スコア | クラスター | ArXiv | ニュース | 影響業種数 |"
            )
            lines.append(
                "|--------|----------|----------|---------|------|--------|---------|"
            )
            for t in themes:
                rank    = t.get("rank", 0)
                node_id = t.get("node_id", "")
                escore  = t.get("emerging_score", 0)
                signals = t.get("signals", {})
                c_delta = signals.get("cluster_delta", 0)
                arxiv   = signals.get("arxiv", 0)
                news    = signals.get("news", 0)
                sec_cnt = len(t.get("affected_sectors", []))

                # スコアをシグナル強度バーに変換（最大5目盛り）
                def _bar(v: float) -> str:
                    n = round(v * 5)
                    return "█" * n + "░" * (5 - n)

                lines.append(
                    f"| {rank} | `{node_id}` | **{escore:.2f}** "
                    f"| {_bar(c_delta)} | {_bar(arxiv)} "
                    f"| {_bar(news)} | {sec_cnt}業種 |"
                )
            lines.append("")

            # キーワード急増テーマの注記
            surge_nodes = [
                t["node_id"] for t in themes
                if t.get("signals", {}).get("news", 0) >= 0.5
            ]
            if surge_nodes:
                lines.append(
                    f"⚡ **ニュースキーワード急増テーマ**: "
                    f"{', '.join(f'`{n}`' for n in surge_nodes[:3])}"
                )
                lines.append("")

        # ── 3. クラスターシグナル統計（cluster_export から）────────
        stats = cluster_export.get("stats", {}) or {}
        if stats:
            bq_count = cluster_export.get("taxonomy_rows_count", 0)
            lines.append("### 📊 クラスター統計")
            lines.append("")
            lines.append(
                f"- 総クラスター数: **{stats.get('total_clusters', 0)}**"
            )
            lines.append(
                f"- JP対象業種: **{stats.get('total_sectors_jp', 0)}**業種 "
                f"/ US対象セクター: **{stats.get('total_sectors_us', 0)}**セクター"
            )
            lines.append(
                f"- BQ taxonomy 書込: **{bq_count}**行"
            )
            lines.append("")

        return lines

    except Exception as _e:
        return [
            "## 🔬 技術クラスター分析（Wave 6 - B）",
            "",
            f"*生成エラー: {str(_e)[:80]}*",
            "",
        ]

# ==============================================================
# BLOCK 9-B: Wave 6 - I系 オルタナティブデータセクション
# ==============================================================

def _section_hiring_signal(results: dict, cfg=None) -> list:
    """
    Wave 6 - I-1: 人材投資急増セクション。
    job_posting_monitor の結果を Markdown 表形式で出力する。

    出力形式:
      ## 👥 人材投資シグナル（Wave 6 - I-1）
      急増カテゴリ一覧 + sector_boost 表
    """
    lines = ["## 👥 人材投資シグナル（Wave 6 - I-1）", ""]

    try:
        jpm = results.get("job_posting", {})
        if not jpm or jpm.get("skipped"):
            lines.append("*データなし（モニタースキップ or 未実行）*")
            lines.append("")
            return lines

        if jpm.get("cached"):
            lines.append("*（キャッシュ利用中）*")
            lines.append("")

        surge   = jpm.get("surge_detected", [])
        boosts  = jpm.get("sector_boosts", {})
        counts  = jpm.get("keyword_counts", {})
        prev    = jpm.get("prev_counts", {})
        total_p = jpm.get("total_postings", 0)
        ts      = jpm.get("timestamp", "")

        lines.append(f"取得求人票: **{total_p}件** | 更新: {ts}")
        lines.append("")

        # 急増カテゴリ
        if surge:
            lines.append("### 🚨 急増カテゴリ（前月比1.5倍以上）")
            lines.append("")
            lines.append("| カテゴリ | 今月 | 前月 | 倍率 |")
            lines.append("|---|---|---|---|")
            for cat in surge:
                curr_c = counts.get(cat, 0)
                prev_c = prev.get(cat, 0)
                ratio  = round(curr_c / max(prev_c, 1), 2)
                lines.append(
                    f"| **{cat}** | {curr_c}件 | {prev_c}件 | **{ratio}x** |"
                )
            lines.append("")
        else:
            lines.append("急増カテゴリ: なし（前月比1.5倍以上の急増なし）")
            lines.append("")

        # 全カテゴリカウント
        if counts:
            lines.append("### 📊 カテゴリ別求人票カウント")
            lines.append("")
            lines.append("| カテゴリ | 今月 | 前月 | 変化 |")
            lines.append("|---|---|---|---|")
            for cat, curr_c in sorted(counts.items(), key=lambda x: -x[1]):
                prev_c = prev.get(cat, 0)
                diff   = curr_c - prev_c
                arrow  = "↑" if diff > 0 else ("↓" if diff < 0 else "→")
                lines.append(
                    f"| {cat} | {curr_c}件 | {prev_c}件 | {arrow}{abs(diff)} |"
                )
            lines.append("")

        # sector_boost
        if boosts:
            lines.append("### 📈 業種別 sector_boost")
            lines.append("")
            lines.append("| 業種コード | boost_pt |")
            lines.append("|---|---|")
            for sc, pt in sorted(boosts.items(), key=lambda x: -x[1]):
                lines.append(f"| {sc} | **+{pt}pt** |")
            lines.append("")

    except Exception as e:
        lines.append(f"*生成エラー: {str(e)[:80]}*")
        lines.append("")

    return lines


def _section_executive_transition(results: dict, cfg=None) -> list:
    """
    Wave 6 - I-3: 役員体制転換セクション。
    executive_transition_tracker の結果を Markdown で出力する。

    出力形式:
      ## 👔 役員体制転換シグナル（Wave 6 - I-3）
      転換シグナル企業一覧 + スキルカテゴリ表
    """
    lines = ["## 👔 役員体制転換シグナル（Wave 6 - I-3）", ""]

    try:
        ett = results.get("executive_transition", {})
        if not ett or ett.get("skipped"):
            lines.append("*データなし（モニタースキップ or 未実行）*")
            lines.append("")
            return lines

        if ett.get("cached"):
            lines.append("*（キャッシュ利用中）*")
            lines.append("")

        alerts       = ett.get("transition_alerts", [])
        boosts       = ett.get("sector_boosts", {})
        total_a      = ett.get("total_analyzed", 0)
        total_t      = ett.get("total_transitions", 0)
        ts           = ett.get("timestamp", "")

        lines.append(
            f"分析: **{total_a}社** | 転換シグナル: **{total_t}社** | 更新: {ts}"
        )
        lines.append("")

        if alerts:
            lines.append("### 🚨 経営方針転換シグナル企業")
            lines.append("")
            lines.append("| ティッカー | 企業名 | 新任役員数 | 主なスキル | 決算期 |")
            lines.append("|---|---|---|---|---|")
            for alert in alerts[:10]:   # 上位10件
                ticker     = alert.get("ticker", "")
                company    = alert.get("company", "")[:15]
                hire_count = alert.get("new_hire_count", 0)
                skills     = list(alert.get("new_skills", {}).keys())
                skill_str  = " / ".join(skills[:3])
                fy_end     = alert.get("fiscal_year_end", "")
                lines.append(
                    f"| {ticker} | {company} | {hire_count}名 "
                    f"| {skill_str} | {fy_end} |"
                )
            lines.append("")

            # 新任役員の詳細（上位3社のみ）
            for alert in alerts[:3]:
                ticker  = alert.get("ticker", "")
                new_off = alert.get("new_officers", [])
                if not new_off:
                    continue
                lines.append(f"**{ticker} 新任役員詳細:**")
                lines.append("")
                for o in new_off[:3]:
                    title  = o.get("title",  "")
                    name   = o.get("name",   "")
                    skills = " / ".join(o.get("skills", []))
                    career = o.get("career_text", "")[:60]
                    lines.append(
                        f"  - {title}（{name}）スキル: {skills} "
                        f"— {career}..."
                    )
                lines.append("")
        else:
            lines.append(
                "転換シグナル: なし（外部人材登用2名以上の企業なし）"
            )
            lines.append("")

        # sector_boost
        if boosts:
            lines.append("### 📈 業種別 sector_boost")
            lines.append("")
            lines.append("| 業種コード | boost_pt |")
            lines.append("|---|---|")
            for sc, pt in sorted(boosts.items(), key=lambda x: -x[1]):
                lines.append(f"| {sc} | **+{pt}pt** |")
            lines.append("")

    except Exception as e:
        lines.append(f"*生成エラー: {str(e)[:80]}*")
        lines.append("")

    return lines


def _section_facility_expansion(results: dict, cfg=None) -> list:
    """
    Wave 6 - I-4: 施設着工シグナルセクション。
    facility_expansion_monitor の結果を Markdown で出力する。

    出力形式:
      ## 🏭 施設着工シグナル（Wave 6 - I-4）
      大規模着工案件一覧 + sector_boost 表
    """
    lines = ["## 🏭 施設着工シグナル（Wave 6 - I-4）", ""]

    try:
        fem = results.get("facility_expansion", {})
        if not fem or fem.get("skipped"):
            lines.append("*データなし（モニタースキップ or 未実行）*")
            lines.append("")
            return lines

        if fem.get("cached"):
            lines.append("*（キャッシュ利用中）*")
            lines.append("")

        signals     = fem.get("signal_projects", [])
        boosts      = fem.get("sector_boosts", {})
        total_f     = fem.get("total_fetched", 0)
        total_l     = fem.get("total_large", 0)
        total_s     = fem.get("total_signals", 0)
        ts          = fem.get("timestamp", "")

        lines.append(
            f"取得: **{total_f}件** | 大規模案件: **{total_l}件** "
            f"| シグナル: **{total_s}件** | 更新: {ts}"
        )
        lines.append("")

        if signals:
            lines.append("### 🏗️ 大規模着工シグナル案件")
            lines.append("")
            lines.append(
                "| 事業名 | 種別 | 事業者 | 都道府県 | 面積(㎡) | 対応業種 |"
            )
            lines.append("|---|---|---|---|---|---|")
            for sp in signals[:15]:   # 上位15件
                name    = sp.get("project_name", "")[:20]
                ftype   = sp.get("matched_type",  "")
                company = sp.get("company_name",  "")[:12]
                pref    = sp.get("prefecture",    "")
                area    = sp.get("area_sqm",      0.0)
                scs     = " / ".join(sp.get("sector_codes", [])[:3])
                area_str = f"{area:,.0f}" if area > 0 else "不明"
                lines.append(
                    f"| {name} | {ftype} | {company} "
                    f"| {pref} | {area_str} | {scs} |"
                )
            lines.append("")
        else:
            lines.append(
                "着工シグナル: なし（大規模施設着工案件の検知なし）"
            )
            lines.append("")

        # sector_boost
        if boosts:
            lines.append("### 📈 業種別 sector_boost")
            lines.append("")
            lines.append("| 業種コード | boost_pt |")
            lines.append("|---|---|")
            for sc, pt in sorted(boosts.items(), key=lambda x: -x[1]):
                lines.append(f"| {sc} | **+{pt}pt** |")
            lines.append("")

    except Exception as e:
        lines.append(f"*生成エラー: {str(e)[:80]}*")
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

    print("\n  📊 週次レポート生成中 ({today} {dow}曜)...")

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
    sections.append(_section_module_ledger(cfg))                  # Wave OBS-1: 実行台帳（健全性）
    sections.append(_section_rule_optimizer_alert(results, cfg))  # Wave 6 - 第4セッション: 縮退アラート
    sections.append(_section_macro(results))
    sections.append(_section_us_macro(results))     # ★ Phase US-2追加
    sections.append(_section_anomaly_season(results))
    sections.append(_section_market_breadth(results))
    sections.append(_section_jp_sectors(results))
    sections.append(_section_geo_country_risk(results, cfg))# Wave 6 - G-N: 国別リスクセクション追加(G-Xで作成)
    sections.append(_section_us_sectors(results))
    sections.append(_section_etf(results))
    sections.append(_section_risk_constraints(results))   # ★ C-1/C-2追加
    sections.append(_section_screener(results))
    sections.append(_section_deliberation_queue(results, cfg))   # 2026-07-28
    sections.append(_section_analyst_revision(results, cfg))        # Wave 2 - FE-2
    sections.append(_section_ignition_alert(results, cfg))          # Wave 2 - FE-4
    sections.append(_section_pead_drift_candidates(results, cfg))   # Wave 3 - FE-5
    sections.append(_section_portfolio_concentration(results, cfg)) # Wave 3 - FE-6
    sections.append(_section_pearl_ranking(results, cfg))           # Wave0 Pearl Ph1
    sections.append(_section_latent_pearl_promotion(results, cfg))  # Wave 3 - Independence Ph1
    sections.append(_section_wave7_premarket_signals(results, cfg))  # Wave 7: 市場形成前シグナル
    sections.append(_section_wave7_size_taxonomy(results, cfg))      # Wave 7: size×taxonomy比較
    sections.append(_section_wave7_independence(results, cfg))       # Wave 7: 市場独立性スコア
    sections.append(_section_strategy_breakdown(results))
    sections.append(_section_earnings_watchlist(results))   # ★ I-2追加
    sections.append(_section_midterm_tracker(results))     # ★ I-5追加
    sections.append(_section_scenario(results))
    sections.append(_section_longterm_scenario(results))   # ★ R-5追加
    sections.append(_section_performance(results))
    sections.append(_section_position_sizer(results))
    sections.append(_section_biz_summaries(results))
    sections.append(_section_news(results))
    sections.append(_section_policy_radar(results))           # ★ policy_radar追加
    sections.append(_section_catalyst_upcoming(results, cfg)) # Wave 3 - FE-7
    sections.append(_section_politician_highlight(results))   # Wave0 Policy Ph1
    sections.append(_section_finbert_sentiment(results))   # ★ RESEARCH-5追加
    sections.append(_section_technology_cluster(results, cfg))  # Wave 6 - B: 技術クラスター
    # 2026-10-05: 人材投資（求人票）・役員体制転換・施設着工は 2026-09-29 に機能を廃止したため節を出さない
    #   （毎週「データなし」が3節並んでいた）
    sections.append(_section_action_guide(results))
    sections.append(_section_stress_test(results))
    sections.append(_section_dividend_calendar(results))
    sections.append(_section_options_monitor(results))
    sections.append(_section_options_advisor(results))   # Wave OPT-1
    sections.append(_section_sr_vp(results))
    sections.append(_section_jp_us_bridge(results))
    sections.append(_section_jp_us_correlation(results))      # Wave0 Idea2-A
    sections.append(_section_rule_optimizer(results))
    sections.append(_section_alpha_attribution(results))    # ★ GAP-8追加
    sections.append(_section_backtest(results))
    sections.append(_section_comps(results))
    sections.append(_section_self_evaluator(results))
    sections.append(_section_wave7_pearl_backtest(results, cfg))  # Wave 7: Pearl BT
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

    # Wave 6 - G-X: 資源権益・地政学影響セクション
    # 2026-07-28 バグ修正: sections は「行リストのリスト」であり、末尾の結合が
    #   "\n".join(line for sec in sections for line in sec)
    # と2重ループになっている。ここだけ sections.append("文字列") や
    # sections += _section_*() を使っていたため、文字列が1文字ずつ
    # イテレートされ、レポートが縦書き（1文字1行）になっていた。
    # 他の全セクションと同じく「行リストを1個 append する」形に統一する。
    try:
        if getattr(cfg, "ENABLE_GEO_SIGNAL", False):
            sections.append([""])
            sections.append(_section_geo_exposure(results, cfg))
            sections.append(_section_geo_signal_detail(results, cfg))
        elif results.get("geo_risk") and not results["geo_risk"].get("skipped"):
            # ENABLE_GEO_SIGNAL=False でも国別リスクデータがあれば簡易表示
            sections.append([
                "",
                "## ⛏️ 資源権益・地政学影響（Wave 6 - G-X）",
                "",
                "*ENABLE_GEO_SIGNAL=False のため geo_impact_bonus 計算はスキップ。*",
                "*BQ に country_risk_daily / company_country_exposure "
                "のデータが蓄積されたら True に切り替えてください。*",
                "",
            ])
    except Exception as _gx_err:
        # G-X セクション生成失敗はレポート全体を止めない
        sections.append([
            "",
            f"*[G-X] 地政学セクション生成エラー（スキップ）: "
            f"{str(_gx_err)[:80]}*",
            "",
        ])

    # フッター
    # 2026-09-14: フッターを地政学（G-X）節より前に積んでいたため、
    # 週次メールでは「自動生成: …」の後ろに地政学節が付く並びになっていた。
    # 必ず最後に置く。
    sections.append([
        "---",
        f"*自動生成: stock_analyzer_main.py | {today}*",
        "",
    ])

    # Markdown全文結合
    report_md = "\n".join(line for sec in sections for line in sec)
    # 2026-10-05: 銘柄列を「ティッカー | 銘柄名」の2列に分け、文中のコードに名前を補う
    # （健太指示「共通修正事項：ティッカーと銘柄名は列を分ける」）
    try:
        from name_resolver import enrich_report
        report_md = enrich_report(report_md)
    except Exception as _ne:
        print(f"  ⚠️ 週次レポートの名前補完をスキップ: {str(_ne)[:80]}")

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
    today    = summary.get("report_date", datetime.now().strftime("%Y-%m-%d"))
    top3     = summary.get("top3_sectors", [])
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
      データソース: yfinance / EdinetDB / IMF / DeepL
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
        "📈 注目業種TOP3",
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
    import importlib.util
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
