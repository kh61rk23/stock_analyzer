# ==============================================================
# score_explainer.py  v2.0
# ==============================================================
# 【変更履歴】
#   v1.0: build_reason_text() - 1行根拠文生成
#   v2.0: factor_breakdown()  - ツリー形式の詳細因子分解（S-0実装）
#
# 機能:
#   BLOCK 1: build_reason_text()              - 1行サマリー（後方互換）
#   BLOCK 2: enrich_result_with_reason()      - result辞書への自動付与
#   BLOCK 3: format_sector_summary_line()     - Gmailアラート用1行フォーマッタ
#            format_top_sectors_block()       - 上位N業種ブロック
#   BLOCK 4: factor_breakdown()               - 因子分解辞書生成（v2.0新規）
#   BLOCK 5: _calc_trend()                    - score_historyトレンド計算
#   BLOCK 6: format_factor_breakdown_text()   - ツリー形式テキスト整形
#            format_factor_breakdown_block()  - 複数業種まとめ出力
#
# 出力例 (format_factor_breakdown_text):
#   電気機器: 72点
#   ├ ROE優良(+15): 平均ROE=12.5% → 収益性確認
#   ├ 円安追い風(+10): USD/JPY=149.2 → 輸出採算改善
#   ├ 金利上昇逆風(-10): 米10年債=4.52%
#   └ 判断: 「3ヶ月前比+18点・上昇加速中」
# ==============================================================

from __future__ import annotations
import re
from datetime import datetime, timedelta
from typing import Optional


# ==============================================================
# 内部ユーティリティ
# ==============================================================

def _safe_avg(values: list) -> Optional[float]:
    """Noneを除いた平均。全Noneの場合はNoneを返す。"""
    valid = [v for v in values if v is not None]
    return sum(valid) / len(valid) if valid else None


# ==============================================================
# BLOCK 1: 根拠文の生成（v1.0後方互換）
# ==============================================================

def build_reason_text(
    sector_code:     str,
    composite_score: float,
    macro_signals:   list,
    gov_signals:     Optional[dict] = None,
    us_macro:        Optional[dict] = None,
    max_factors:     int = 3,
) -> str:
    """
    スコアに最も影響した要因を抽出して1行の根拠文を生成する。

    Args:
        sector_code:      業種コード（例: "1016"）
        composite_score:  総合スコア
        macro_signals:    マクロシグナルリスト
                          [{"factor":str, "label":str, "signal":"tailwind"/"headwind",
                            "trigger":str}, ...]
                          または evaluate_macro_signals() の戻り値形式
                          [{"factor":str, "label":str, "trigger_value":str,
                            "sector_signals": {code: "tailwind"/"headwind"}}, ...]
        gov_signals:      政府統計シグナル {"score":int, "tailwinds":[], "headwinds":[]}
        us_macro:         米国マクロ指標 {"us10y": float, "usdjpy": float, ...}
        max_factors:      表示する要因の最大数

    Returns:
        "主因: [追い風要因] / [逆風要因]" 形式の文字列
    """
    factors = []  # (重要度スコア, ラベル文字列, "tailwind"/"headwind")

    for sig in macro_signals:
        # base_agent形式 {"signal": "tailwind"/"headwind", "label": ..., "trigger": ...}
        if "signal" in sig and sig["signal"] in ("tailwind", "headwind"):
            direction = sig["signal"]
            label     = sig.get("label", sig.get("factor", ""))
            trigger   = sig.get("trigger", sig.get("trigger_value", ""))
            display   = f"{label}({trigger})" if trigger else label
            weight    = 1 if direction == "tailwind" else -1
            factors.append((weight, display, direction))
            continue
        # evaluate_macro_signals形式 {"sector_signals": {code: "tailwind"/"headwind"}}
        direction = sig.get("sector_signals", {}).get(sector_code)
        if direction:
            label   = sig.get("label", sig.get("factor", ""))
            trigger = sig.get("trigger_value", "")
            display = f"{label}({trigger})" if trigger else label
            weight  = 1 if direction == "tailwind" else -1
            factors.append((weight, display, direction))

    if gov_signals:
        for tw in gov_signals.get("tailwinds", []):
            name_part  = tw.split(":")[0].strip() if ":" in tw else tw
            value_part = tw.split(":", 1)[1].strip() if ":" in tw else ""
            short_val  = ""
            if "前年比" in value_part:
                m = re.search(r"前年比([+\-\d\.]+%)", value_part)
                if m:
                    short_val = m.group(1)
            display = f"{name_part}{short_val}" if short_val else name_part
            factors.append((1, display, "tailwind"))
        for hw in gov_signals.get("headwinds", []):
            name_part  = hw.split(":")[0].strip() if ":" in hw else hw
            value_part = hw.split(":", 1)[1].strip() if ":" in hw else ""
            short_val  = ""
            if "前年比" in value_part:
                m = re.search(r"前年比([+\-\d\.]+%)", value_part)
                if m:
                    short_val = m.group(1)
            display = f"{name_part}{short_val}" if short_val else name_part
            factors.append((-1, display, "headwind"))

    if us_macro:
        us10y = us_macro.get("us10y")
        if us10y is not None:
            if us10y > 4.0:
                factors.append((-0.5, f"米10年債{us10y:.2f}%", "headwind"))
            elif us10y < 3.5:
                factors.append((0.5, f"米10年債{us10y:.2f}%", "tailwind"))
        vix = us_macro.get("vix")
        if vix is not None and vix > 20:
            factors.append((-0.5, f"VIX{vix:.0f}", "headwind"))

    if not factors:
        return f"スコア{composite_score:.0f}点（シグナルなし）"

    factors.sort(key=lambda x: abs(x[0]), reverse=True)
    top        = factors[:max_factors]
    tail_parts = [f"△{f[1]}" for f in top if f[2] == "tailwind"]
    head_parts = [f"▽{f[1]}" for f in top if f[2] == "headwind"]
    all_parts  = tail_parts + head_parts

    if not all_parts:
        return f"スコア{composite_score:.0f}点"

    return "主因: " + " / ".join(all_parts)


# ==============================================================
# BLOCK 2: base_agent.py の build_result() に差し込む統合関数
# ==============================================================

def enrich_result_with_reason(
    result:             dict,
    gov_sector_signals: Optional[dict],
    macro_snapshot:     Optional[dict],
    score_history:      Optional[list] = None,
) -> dict:
    """
    build_result() の戻り値 result に reason_text / factor_breakdown を追加して返す。

    Args:
        result:             build_result() の戻り値辞書
        gov_sector_signals: aggregate_sector_signals() の全業種出力辞書
        macro_snapshot:     {"usdjpy":float, "us10y":float, "vix":float, ...}
        score_history:      [{"date":"YYYY-MM-DD", "composite_score":float}, ...]
                            渡すと factor_breakdown にトレンド文が付与される

    Returns:
        reason_text / factor_breakdown を追加した result
    """
    sector_code     = result.get("sector_code", "")
    composite_score = result.get("composite_score", 0)
    macro_signals   = result.get("macro_signals", [])

    gov_entry = (gov_sector_signals.get(sector_code, {})
                 if isinstance(gov_sector_signals, dict) else None)

    # reason_text（1行サマリー、v1.0互換）
    result["reason_text"] = build_reason_text(
        sector_code     = sector_code,
        composite_score = composite_score,
        macro_signals   = macro_signals,
        gov_signals     = gov_entry,
        us_macro        = macro_snapshot,
        max_factors     = 3,
    )

    # factor_breakdown（ツリー形式辞書、v2.0）
    result["factor_breakdown"] = factor_breakdown(
        result         = result,
        score_history  = score_history or [],
        macro_snapshot = macro_snapshot,
    )

    return result


# ==============================================================
# BLOCK 3: Gmailアラート・Markdownレポート用フォーマッタ
# ==============================================================

def format_sector_summary_line(result: dict) -> str:
    """
    1業種を1行でサマリー表示する文字列を生成する。

    例: 📈 電気機器 [45点] 主因: △機械受注+39.5% / △鉱工業生産+1.2% / ▽米10年債4.1%
    """
    score    = result.get("composite_score", 0)
    name     = result.get("sector_name_ja", result.get("sector_code", ""))
    reason   = result.get("reason_text", "")
    risk     = result.get("risk_level", "")
    icon     = "📈" if score >= 60 else ("📉" if score < 40 else "➡️")
    risk_str = f" [{risk}]" if risk else ""
    return f"{icon} {name} [{score:.0f}点{risk_str}] {reason}"


def format_top_sectors_block(results: list, top_n: int = 5) -> str:
    """
    上位N業種のサマリーブロックを生成する（Gmailアラート・レポート用）。
    """
    sorted_r = sorted(results, key=lambda x: x.get("composite_score", 0), reverse=True)
    lines    = [f"## 🏆 注目業種トップ{top_n}", ""]
    for i, r in enumerate(sorted_r[:top_n], 1):
        lines.append(f"{i}. {format_sector_summary_line(r)}")
    lines.append("")
    return "\n".join(lines)


# ==============================================================
# BLOCK 4: factor_breakdown() - 因子分解辞書生成（v2.0新規）
# ==============================================================

def factor_breakdown(
    result:         dict,
    score_history:  list,
    macro_snapshot: Optional[dict] = None,
) -> dict:
    """
    composite_score を因子ごとに分解した辞書を生成する。

    Args:
        result:         build_result() の戻り値辞書。以下キーを使用:
                          composite_score, sector_name_ja, sector_code,
                          macro_signals, top_companies, risk_level
        score_history:  [{date:"YYYY-MM-DD", composite_score:float}, ...]
                        直近90日分を渡すとトレンド文が生成される
        macro_snapshot: {"usdjpy":float, "us10y":float, "vix":float, ...}

    Returns:
        {
          "sector_name_ja": str,
          "composite_score": float,
          "risk_level": str,
          "items": [
            {
              "label":     str,   # 表示ラベル（例: "ROE優良"）
              "points":    float, # 寄与点数（正=追い風、負=逆風）
              "rationale": str,   # 根拠詳細（例: "平均ROE=12.5% → 収益性確認"）
              "category":  str,   # "financial" / "macro" / "analyst"
              "direction": str,   # "tailwind" / "headwind" / "neutral"
            }, ...
          ],
          "trend_text": str,      # 例: "3ヶ月前比+18点・上昇加速中"
          "generated_at": str,    # ISO8601
        }
    """
    sector_name     = result.get("sector_name_ja", result.get("sector_code", ""))
    composite_score = result.get("composite_score", 0.0)
    macro_signals   = result.get("macro_signals", [])
    companies       = result.get("top_companies", [])
    risk_level      = result.get("risk_level", "N/A")

    items: list[dict] = []

    # ── 財務スコア因子 ────────────────────────────────────────
    ok_companies = [c for c in companies if c.get("fetch_ok")]
    if ok_companies:
        avg_roe = _safe_avg([c.get("roe")        for c in ok_companies])
        avg_roa = _safe_avg([c.get("roa")        for c in ok_companies])
        avg_opm = _safe_avg([c.get("op_margin")  for c in ok_companies])

        # ROE (+15 / +8 / 0)
        if avg_roe is not None:
            if avg_roe >= 0.10:
                pts = 15.0
                lbl = "ROE優良"
                rat = f"平均ROE={avg_roe*100:.1f}% → 収益性確認（基準10%超）"
                drn = "tailwind"
            elif avg_roe >= 0.05:
                pts = 8.0
                lbl = "ROE標準"
                rat = f"平均ROE={avg_roe*100:.1f}% → 許容水準（5〜10%）"
                drn = "neutral"
            else:
                pts = 0.0
                lbl = "ROE低水準"
                rat = f"平均ROE={avg_roe*100:.1f}% → 5%未満・収益性懸念"
                drn = "headwind"
            items.append({"label": lbl, "points": pts, "rationale": rat,
                          "category": "financial", "direction": drn})

        # ROA (+10 / +5 / 0)
        if avg_roa is not None:
            if avg_roa >= 0.05:
                pts = 10.0
                lbl = "ROA優良"
                rat = f"平均ROA={avg_roa*100:.1f}% → 資産効率良好（基準5%超）"
                drn = "tailwind"
            elif avg_roa >= 0.02:
                pts = 5.0
                lbl = "ROA標準"
                rat = f"平均ROA={avg_roa*100:.1f}% → 許容水準（2〜5%）"
                drn = "neutral"
            else:
                pts = 0.0
                lbl = "ROA低水準"
                rat = f"平均ROA={avg_roa*100:.1f}% → 2%未満・資産効率懸念"
                drn = "headwind"
            items.append({"label": lbl, "points": pts, "rationale": rat,
                          "category": "financial", "direction": drn})

        # 営業利益率 (+15 / +8 / 0)
        if avg_opm is not None:
            if avg_opm >= 0.10:
                pts = 15.0
                lbl = "営業利益率優良"
                rat = f"平均OPM={avg_opm*100:.1f}% → 高収益構造（基準10%超）"
                drn = "tailwind"
            elif avg_opm >= 0.05:
                pts = 8.0
                lbl = "営業利益率標準"
                rat = f"平均OPM={avg_opm*100:.1f}% → 許容水準（5〜10%）"
                drn = "neutral"
            else:
                pts = 0.0
                lbl = "営業利益率低水準"
                rat = f"平均OPM={avg_opm*100:.1f}% → 5%未満・コスト構造懸念"
                drn = "headwind"
            items.append({"label": lbl, "points": pts, "rationale": rat,
                          "category": "financial", "direction": drn})

        # アクルーアル比率 (+8 / +4 / 0 / -8)
        ok_accrual = [c.get("accrual_ratio") for c in ok_companies
                      if c.get("accrual_ratio") is not None]
        if ok_accrual:
            avg_ac = sum(ok_accrual) / len(ok_accrual)
            if avg_ac < -0.02:
                pts = 8.0
                lbl = "利益の質：超優良"
                rat = f"平均アクルーアル={avg_ac:.3f} → キャッシュ裏付け十分"
                drn = "tailwind"
            elif avg_ac < 0.02:
                pts = 4.0
                lbl = "利益の質：良好"
                rat = f"平均アクルーアル={avg_ac:.3f} → 標準的な利益品質"
                drn = "neutral"
            elif avg_ac > 0.05:
                pts = -8.0
                lbl = "利益の質：要注意"
                rat = f"平均アクルーアル={avg_ac:.3f} → 利益水増し疑い（0.05超）"
                drn = "headwind"
            else:
                pts = 0.0
                lbl = "利益の質：許容範囲"
                rat = f"平均アクルーアル={avg_ac:.3f}"
                drn = "neutral"
            items.append({"label": lbl, "points": pts, "rationale": rat,
                          "category": "financial", "direction": drn})

        # アナリスト推奨 (+5/+3/0 + 上方修正+2)
        avg_rec    = _safe_avg([c.get("rec_mean")       for c in ok_companies])
        avg_upside = _safe_avg([c.get("analyst_upside") for c in ok_companies])
        analyst_pts = 0.0
        analyst_parts = []
        if avg_rec is not None:
            if avg_rec <= 2.0:
                analyst_pts += 5.0
                analyst_parts.append(f"推奨平均={avg_rec:.2f}（Buy相当）")
            elif avg_rec <= 2.5:
                analyst_pts += 3.0
                analyst_parts.append(f"推奨平均={avg_rec:.2f}（Outperform相当）")
            else:
                analyst_parts.append(f"推奨平均={avg_rec:.2f}（中立以下）")
        if avg_upside is not None and avg_upside >= 0.20:
            analyst_pts += 2.0
            analyst_parts.append(f"目標上振れ余地={avg_upside*100:.0f}%（+2点）")
        if analyst_parts:
            drn = "tailwind" if analyst_pts >= 4 else ("neutral" if analyst_pts > 0 else "headwind")
            items.append({
                "label":     "アナリスト評価",
                "points":    analyst_pts,
                "rationale": " / ".join(analyst_parts),
                "category":  "analyst",
                "direction": drn,
            })

    # ── マクロシグナル因子 ────────────────────────────────────
    for sig in macro_signals:
        direction = sig.get("signal", "neutral")
        if direction not in ("tailwind", "headwind"):
            continue
        pts     = 10.0 if direction == "tailwind" else -10.0
        label   = sig.get("label", sig.get("factor", ""))
        trigger = sig.get("trigger", sig.get("trigger_value", ""))
        rat     = sig.get("rationale", "")
        if trigger and rat:
            rationale = f"{trigger} → {rat}"
        elif trigger:
            rationale = trigger
        elif rat:
            rationale = rat
        else:
            rationale = ""
        # マクロスナップショットから実値を補完
        if macro_snapshot:
            factor = sig.get("factor", "")
            snap_map = {
                "yen_depreciation": ("usdjpy", "USD/JPY={:.1f}"),
                "yen_appreciation": ("usdjpy", "USD/JPY={:.1f}"),
                "interest_rate_rise": ("us10y", "米10年債={:.2f}%"),
                "interest_rate_fall": ("us10y", "米10年債={:.2f}%"),
                "oil_price_rise":    ("oil_wti", "WTI={:.1f}ドル"),
                "us_recession_risk": ("vix",    "VIX={:.0f}"),
            }
            if factor in snap_map:
                key, fmt_str = snap_map[factor]
                val = macro_snapshot.get(key)
                if val is not None:
                    snap_str = fmt_str.format(val)
                    rationale = f"{snap_str}" + (f" → {rat}" if rat else "")
        items.append({
            "label":     label,
            "points":    pts,
            "rationale": rationale,
            "category":  "macro",
            "direction": direction,
        })

    # ── 点数0の財務項目は出力から除外（不要ノイズ削減）────────
    # 寄与がゼロかつneutralな項目は折り畳む
    items = [it for it in items if not (it["points"] == 0.0 and it["direction"] == "neutral")]

    # 寄与の絶対値が大きい順にソート
    items.sort(key=lambda x: abs(x["points"]), reverse=True)

    # ── トレンド計算 ──────────────────────────────────────────
    trend_text = _calc_trend(composite_score, score_history)

    return {
        "sector_name_ja":  sector_name,
        "composite_score": composite_score,
        "risk_level":      risk_level,
        "items":           items,
        "trend_text":      trend_text,
        "generated_at":    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ==============================================================
# BLOCK 5: _calc_trend() - score_historyトレンド計算
# ==============================================================

def _calc_trend(current_score: float, score_history: list) -> str:
    """
    score_history から過去比較トレンド文を生成する。

    Args:
        current_score: 本日の composite_score
        score_history: [{"date":"YYYY-MM-DD", "composite_score":float}, ...]
                       日付降順・昇順どちらでも可

    Returns:
        例: "3ヶ月前比+18点・上昇加速中"
             "先月比-5点・下落傾向"
             "履歴データなし"
    """
    if not score_history:
        return "履歴データなし"

    # date文字列でソート（昇順）
    try:
        hist_sorted = sorted(
            [h for h in score_history if h.get("date") and h.get("composite_score") is not None],
            key=lambda h: h["date"]
        )
    except Exception:
        return "履歴データなし"

    if not hist_sorted:
        return "履歴データなし"

    today = datetime.now().date()

    def _find_closest(days_ago: int) -> Optional[float]:
        """days_ago日前に最も近いスコアを返す（±7日以内）"""
        target = today - timedelta(days=days_ago)
        best   = None
        best_d = 9999
        for h in hist_sorted:
            try:
                d = datetime.strptime(h["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            diff = abs((d - target).days)
            if diff < best_d and diff <= 7:
                best_d = diff
                best   = h["composite_score"]
        return best

    # 比較対象を優先順で探す: 3ヶ月前 → 1ヶ月前 → 2週間前 → 1週間前
    comparisons = [
        (90, "3ヶ月前"),
        (30, "先月"),
        (14, "2週間前"),
        (7,  "先週"),
    ]
    ref_score = None
    ref_label = None
    for days, label in comparisons:
        s = _find_closest(days)
        if s is not None:
            ref_score = s
            ref_label = label
            break

    if ref_score is None:
        return "比較対象データなし"

    diff = current_score - ref_score
    sign = "+" if diff >= 0 else ""

    # 傾向判定（直近3〜5件の方向性）
    recent = hist_sorted[-5:]
    if len(recent) >= 3:
        diffs = [recent[i+1]["composite_score"] - recent[i]["composite_score"]
                 for i in range(len(recent)-1)]
        avg_d = sum(diffs) / len(diffs)
        if avg_d >= 3:
            trend = "上昇加速中"
        elif avg_d >= 1:
            trend = "緩やか上昇"
        elif avg_d <= -3:
            trend = "下落加速中"
        elif avg_d <= -1:
            trend = "緩やか下落"
        else:
            trend = "横ばい"
    else:
        trend = "上昇中" if diff > 0 else ("下落中" if diff < 0 else "横ばい")

    return f"{ref_label}比{sign}{diff:.0f}点・{trend}"


# ==============================================================
# BLOCK 6: テキスト整形関数
# ==============================================================

def format_factor_breakdown_text(bd: dict, max_items: int = 6) -> str:
    """
    factor_breakdown() の戻り値辞書をツリー形式テキストに整形する。

    Args:
        bd:        factor_breakdown() の戻り値
        max_items: 表示する因子の最大数（デフォルト6）

    Returns:
        例:
          電気機器: 72点 [medium]
          ├ ROE優良(+15): 平均ROE=12.5% → 収益性確認
          ├ 円安追い風(+10): USD/JPY=149.2 → 輸出採算改善
          ├ 金利上昇逆風(-10): 米10年債=4.52%
          └ 判断: 「3ヶ月前比+18点・上昇加速中」
    """
    name    = bd.get("sector_name_ja", "")
    score   = bd.get("composite_score", 0)
    risk    = bd.get("risk_level", "")
    items   = bd.get("items", [])
    trend   = bd.get("trend_text", "")

    risk_str = f" [{risk}]" if risk else ""
    lines    = [f"{name}: {score:.0f}点{risk_str}"]

    show_items = items[:max_items]
    for i, it in enumerate(show_items):
        pts = it["points"]
        lbl = it["label"]
        rat = it["rationale"]
        sign     = "+" if pts >= 0 else ""
        pts_str  = f"({sign}{pts:.0f})"
        rat_part = f": {rat}" if rat else ""
        is_last  = (i == len(show_items) - 1) and not trend
        prefix   = "└" if is_last else "├"
        lines.append(f"  {prefix} {lbl}{pts_str}{rat_part}")

    if trend:
        lines.append(f"  └ 判断: 「{trend}」")

    return "\n".join(lines)


def format_factor_breakdown_block(results: list, top_n: int = 5,
                                   max_items: int = 4) -> str:
    """
    複数業種の factor_breakdown をスコア順に整形したブロックを返す。

    Args:
        results:   factor_breakdown() の戻り値リスト
        top_n:     表示する業種数
        max_items: 1業種あたりの因子最大表示数

    Returns:
        複数業種分のツリーテキストを空行区切りで結合した文字列
    """
    sorted_r = sorted(results, key=lambda x: x.get("composite_score", 0), reverse=True)
    blocks   = []
    for bd in sorted_r[:top_n]:
        blocks.append(format_factor_breakdown_text(bd, max_items=max_items))
    return "\n\n".join(blocks)

# ==============================================================
# BLOCK 7: GAP-1 confidence_prior / ベイズ的更新式
# ==============================================================
# 【設計思想】
#   score_history に蓄積されたデータ量に応じて「初期値への信頼度」を計算し、
#   今日のスコアをベイズ的に更新する。
#
#   new_score = prior_score × (1 - confidence) + today_score × confidence
#
#   蓄積日数が多い業種ほど confidence が高く（＝過去の評価を信頼する）、
#   今日のスコアが多少ブレても急激に変化しない（ノイズ耐性向上）。
#   逆に急変（乖離幅 >= DRIFT_THRESHOLD）が検出されたらアラートフラグを立てる。
#
# 【接続先】
#   score_explainer.calc_confidence_prior()  → factor_breakdown() の items に追加
#   score_explainer.apply_bayesian_update()  → main.py のセクターループ内で呼び出し
#   score_explainer.detect_drift()           → alert_notifier.py の score_jump アラートを補強

import json
import math
from pathlib import Path


def calc_confidence_prior(
    score_history: list,
    max_days: int = 30,
) -> float:
    """
    score_history の蓄積日数から confidence_prior（0.0〜1.0）を計算する。

    Args:
        score_history : [{date:str, composite_score:float}, ...] 昇順・降順どちらでも可
        max_days      : 信頼度が 1.0 になる基準日数（デフォルト30日）

    Returns:
        confidence_prior: 0.0〜1.0
          0日（データなし）→ 0.0（今日のスコアをそのまま採用）
          15日             → 0.5
          30日以上          → 1.0（初期値を強く信頼）

    使い方:
        confidence = calc_confidence_prior(score_history)
        new_score  = apply_bayesian_update(prior_score, today_score, confidence)
    """
    if not score_history:
        return 0.0

    valid = [h for h in score_history
             if h.get("date") and h.get("composite_score") is not None]
    if not valid:
        return 0.0

    # 最古〜最新の日数差を蓄積日数として使う
    try:
        dates = sorted(h["date"] for h in valid)
        oldest = datetime.strptime(dates[0], "%Y-%m-%d")
        newest = datetime.strptime(dates[-1], "%Y-%m-%d")
        accumulated_days = (newest - oldest).days
    except Exception:
        accumulated_days = len(valid)   # フォールバック: エントリ数で代替

    confidence = min(1.0, accumulated_days / max_days)
    return round(confidence, 3)


def apply_bayesian_update(
    prior_score:    float,
    today_score:    float,
    confidence:     float,
    score_history:  list = None,
) -> tuple[float, dict]:
    """
    ベイズ的更新式でスコアを更新する。

    new_score = prior_score × (1 - confidence) + today_score × confidence

    Args:
        prior_score   : 前回または過去平均のスコア
        today_score   : 今日の生スコア（補正前）
        confidence    : calc_confidence_prior() の出力（0〜1）
        score_history : 過去スコア（prior_score の算出に使った履歴）

    Returns:
        (new_score: float, meta: dict)
        meta = {
          "prior_score":   float,
          "today_score":   float,
          "confidence":    float,
          "new_score":     float,
          "drift":         float,   # |today - prior|
          "drift_alert":   bool,    # drift >= DRIFT_ALERT_THRESHOLD
        }
    """
    DRIFT_ALERT_THRESHOLD = 15.0   # この乖離幅以上でアラートフラグ

    new_score = round(
        prior_score * (1.0 - confidence) + today_score * confidence,
        1,
    )
    drift = abs(today_score - prior_score)

    meta = {
        "prior_score":     round(prior_score, 1),
        "today_score":     round(today_score, 1),
        "confidence":      confidence,
        "new_score":       new_score,
        "drift":           round(drift, 1),
        "drift_alert":     drift >= DRIFT_ALERT_THRESHOLD,
    }
    return new_score, meta


def detect_drift(
    code:          str,
    today_score:   float,
    score_history: list,
    threshold:     float = 15.0,
) -> Optional[dict]:
    """
    前回スコアから大幅に変化した場合に乖離情報を返す。
    変化がない場合は None を返す。

    Args:
        code          : 業種コード（ログ用）
        today_score   : 今日の composite_score（ベイズ更新後）
        score_history : [{date:str, composite_score:float}, ...]
        threshold     : 乖離アラートの閾値（デフォルト15pt）

    Returns:
        None | {
          "code":       str,
          "today":      float,
          "prev":       float,
          "drift":      float,
          "direction":  "up" | "down",
          "severity":   "high" | "medium",
        }
    """
    if not score_history:
        return None

    valid = sorted(
        [h for h in score_history
         if h.get("date") and h.get("composite_score") is not None],
        key=lambda h: h["date"],
    )
    if len(valid) < 2:
        return None

    prev_score = valid[-1]["composite_score"]
    drift      = today_score - prev_score

    if abs(drift) < threshold:
        return None

    return {
        "code":      code,
        "today":     round(today_score, 1),
        "prev":      round(prev_score, 1),
        "drift":     round(drift, 1),
        "direction": "up" if drift > 0 else "down",
        "severity":  "high" if abs(drift) >= threshold * 1.5 else "medium",
        "date":      datetime.now().strftime("%Y-%m-%d"),
    }


def apply_bayesian_to_sector_results(
    sector_results: dict,
    score_history_all: dict,
    cfg = None,
) -> tuple[dict, list]:
    """
    全セクターの composite_score にベイズ的更新を一括適用する。

    stock_analyzer_main.py の sector ループ後に呼び出す。

    Args:
        sector_results    : results["sectors"] の dict
        score_history_all : score_history.json の全内容
                            形式: {date: {code: {score:float, risk:str}}}
        cfg               : config モジュール（閾値取得用）

    Returns:
        (updated_sector_results: dict, drift_events: list)
        drift_events: [detect_drift() の戻り値, ...]
    """
    max_days  = getattr(cfg, "CONFIDENCE_PRIOR_MAX_DAYS", 30) if cfg else 30
    threshold = getattr(cfg, "DRIFT_ALERT_THRESHOLD",     15.0) if cfg else 15.0

    updated      = {}
    drift_events = []

    for code, res in sector_results.items():
        if not isinstance(res, dict):
            updated[code] = res
            continue

        today_score = res.get("composite_score", 50.0)

        # code 別の score_history を構築
        hist = []
        for date_key in sorted(score_history_all.keys()):
            entry = score_history_all[date_key]
            if isinstance(entry, dict) and code in entry:
                raw = entry[code]
                sc  = raw.get("score") if isinstance(raw, dict) else float(raw)
                if sc is not None:
                    hist.append({"date": date_key, "composite_score": float(sc)})

        confidence  = calc_confidence_prior(hist, max_days=max_days)

        # prior_score: 直近スコアの加重平均（直近2件: 70%/30%）
        if len(hist) >= 2:
            prior_score = hist[-1]["composite_score"] * 0.7 + hist[-2]["composite_score"] * 0.3
        elif len(hist) == 1:
            prior_score = hist[-1]["composite_score"]
        else:
            prior_score = today_score   # 履歴なし → 今日をそのまま採用

        new_score, meta = apply_bayesian_update(
            prior_score, today_score, confidence, hist
        )

        # 乖離検出（ベイズ更新後のスコアで比較）
        drift = detect_drift(code, new_score, hist, threshold=threshold)
        if drift:
            drift_events.append(drift)

        # sector_results に反映
        res = dict(res)   # コピー（元データを変更しない）
        res["composite_score"]       = new_score
        res["composite_score_raw"]   = round(today_score, 1)   # 生スコアも保存
        res["confidence_prior"]      = confidence
        res["bayesian_meta"]         = meta
        updated[code] = res

    return updated, drift_events