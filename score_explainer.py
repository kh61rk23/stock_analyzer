# ==============================================================
# score_explainer.py  v1.0
# ==============================================================
# 【ステップ2: 判断根拠の説明強化】
#
# 機能:
#   セクタースコアの「主因」を自動抽出し、1行の根拠文を生成する。
#
# 出力例:
#   電気機器 [スコア:45点]
#   主因: 鉱工業生産+1.2%(追い風) / 機械受注+39.5%(追い風) / 米10年債4.1%(逆風)
#
# 組み込み方法:
#   stock_analyzer_main.py の run_sector_analysis() の戻り値に
#   "reason_text" フィールドを追加する。
#   レポートやGmailアラートにそのまま使える。
# ==============================================================

from typing import Optional


# ==============================================================
# BLOCK 1: 根拠文の生成
# ==============================================================

def build_reason_text(
    sector_code:    str,
    composite_score: float,
    macro_signals:  list,
    gov_signals:    Optional[dict] = None,
    us_macro:       Optional[dict] = None,
    max_factors:    int = 3,
) -> str:
    """
    スコアに最も影響した要因を抽出して1行の根拠文を生成する。

    Args:
        sector_code:      業種コード（例: "1016"）
        composite_score:  総合スコア
        macro_signals:    evaluate_macro_signals() の戻り値リスト
                          [{"factor":str, "label":str, "trigger_value":str,
                            "sector_signals": {code: "tailwind"/"headwind"}}, ...]
        gov_signals:      aggregate_sector_signals() の戻り値から
                          該当sector_codeのエントリ
                          {"score":int, "tailwinds":[], "headwinds":[]}
        us_macro:         米国マクロ指標 {"us10y": float, "usdjpy": float, ...}
        max_factors:      表示する要因の最大数

    Returns:
        "主因: [追い風要因] / [逆風要因]" 形式の文字列
    """
    factors = []  # (重要度スコア, ラベル文字列, tailwind/headwind)

    # ── マクロシグナルから要因を収集 ─────────────────────────
    for sig in macro_signals:
        direction = sig.get("sector_signals", {}).get(sector_code)
        if not direction:
            continue
        label     = sig.get("label", sig.get("factor", ""))
        trigger   = sig.get("trigger_value", "")
        display   = f"{label}({trigger})" if trigger else label
        # 追い風は+1、逆風は-1
        weight    = 1 if direction == "tailwind" else -1
        factors.append((weight, display, direction))

    # ── 政府統計シグナルから要因を収集 ───────────────────────
    if gov_signals:
        for tw in gov_signals.get("tailwinds", []):
            # "消費者物価指数（CPI）: 前年比+1.6%（上昇＝逆風）" 形式
            name_part = tw.split(":")[0].strip() if ":" in tw else tw
            value_part = tw.split(":", 1)[1].strip() if ":" in tw else ""
            # 値部分を短縮（"前年比+X.X%（上昇＝追い風）" → "+X.X%"）
            short_val = ""
            if "前年比" in value_part:
                import re
                m = re.search(r"前年比([+\-\d\.]+%)", value_part)
                if m:
                    short_val = m.group(1)
            display = f"{name_part}{short_val}" if short_val else name_part
            factors.append((1, display, "tailwind"))
        for hw in gov_signals.get("headwinds", []):
            name_part = hw.split(":")[0].strip() if ":" in hw else hw
            value_part = hw.split(":", 1)[1].strip() if ":" in hw else ""
            short_val = ""
            if "前年比" in value_part:
                import re
                m = re.search(r"前年比([+\-\d\.]+%)", value_part)
                if m:
                    short_val = m.group(1)
            display = f"{name_part}{short_val}" if short_val else name_part
            factors.append((-1, display, "headwind"))

    # ── 米国マクロから主要指標を追加 ─────────────────────────
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

    # 重要度でソート（追い風はプラス、逆風はマイナス、絶対値大が上位）
    factors.sort(key=lambda x: abs(x[0]), reverse=True)

    # 上位max_factors件を選択
    top = factors[:max_factors]

    # 追い風・逆風を分類して表示
    tail_parts = [f"△{f[1]}" for f in top if f[2] == "tailwind"]
    head_parts = [f"▽{f[1]}" for f in top if f[2] == "headwind"]

    all_parts = tail_parts + head_parts
    if not all_parts:
        return f"スコア{composite_score:.0f}点"

    return "主因: " + " / ".join(all_parts)


# ==============================================================
# BLOCK 2: base_agent.py の build_result() に差し込む統合関数
# ==============================================================

def enrich_result_with_reason(result: dict, gov_sector_signals: Optional[dict],
                               macro_snapshot: Optional[dict]) -> dict:
    """
    build_result() の戻り値 result に "reason_text" を追加して返す。

    使い方（base_agent.py の build_result 末尾に追加）:
        from score_explainer import enrich_result_with_reason
        result = enrich_result_with_reason(result, gov_signals, macro_snapshot)
        return result
    """
    sector_code     = result.get("sector_code", "")
    composite_score = result.get("composite_score", 0)
    macro_signals   = result.get("macro_signals", [])

    # gov_sector_signals は aggregate_sector_signals() の出力から該当コードを取り出す
    gov_entry = (gov_sector_signals.get(sector_code, {})
                 if isinstance(gov_sector_signals, dict) else None)

    reason = build_reason_text(
        sector_code     = sector_code,
        composite_score = composite_score,
        macro_signals   = macro_signals,
        gov_signals     = gov_entry,
        us_macro        = macro_snapshot,
        max_factors     = 3,
    )
    result["reason_text"] = reason
    return result


# ==============================================================
# BLOCK 3: Gmailアラート・Markdownレポート用フォーマッタ
# ==============================================================

def format_sector_summary_line(result: dict) -> str:
    """
    1業種を1行でサマリー表示する文字列を生成する。

    例: 📈 電気機器 [45点] 主因: △機械受注+39.5% / △鉱工業生産+1.2% / ▽米10年債4.1%
    """
    score     = result.get("composite_score", 0)
    name      = result.get("sector_name_ja", result.get("sector_code", ""))
    reason    = result.get("reason_text", "")
    risk      = result.get("risk_level", "")
    icon      = "📈" if score >= 60 else ("📉" if score < 40 else "➡️")
    risk_str  = f" [{risk}]" if risk else ""
    return f"{icon} {name} [{score:.0f}点{risk_str}] {reason}"


def format_top_sectors_block(results: list, top_n: int = 5) -> str:
    """
    上位N業種のサマリーブロックを生成する（Gmailアラート・レポート用）。
    """
    sorted_r = sorted(results, key=lambda x: x.get("composite_score", 0),
                      reverse=True)
    lines = ["## 🏆 注目業種トップ{}".format(top_n), ""]
    for i, r in enumerate(sorted_r[:top_n], 1):
        lines.append(f"{i}. {format_sector_summary_line(r)}")
    lines.append("")
    return "\n".join(lines)
