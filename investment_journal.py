# ==============================================================
# investment_journal.py  v1.0  (R-3 / 両部篤モデル)
# 知識蓄積DB — インベストメントジャーナル
# ==============================================================
# 【役割】
#   「なぜ買ったか」「なぜ外れたか」を構造化して永続保存し、
#   外れパターンを rule_optimizer.py の重み調整に接続する。
#   人間の学習とシステムの進化を相互強化するループの核心。
#
# 【自動記録（システムが書き込む）】
#   ① screener_history.json に銘柄が初登場 → entry セクション自動生成
#   ② performance_tracker.py が週次集計 → result セクション自動更新
#   ③ 銘柄が screener_history から消えた → exit 日を自動推定
#
# 【手動記録（週次レポートの「記入待ち」から記入）】
#   entry.reason.catalyst_expected / risk_noted
#   post_mortem.learning / manual_note
#
# 【investment_journal.json の完全構造】→ docstring 参照
#
# 【依存】performance_tracker.py / screener_history.json
# 【呼出】from investment_journal import (
#             auto_create_entries, update_results,
#             load_journal, save_journal,
#             get_pending_entries, get_pattern_analysis,
#             format_journal_section, format_pattern_section,
#         )
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

JOURNAL_FILENAME    = "investment_journal.json"
AUTO_EXIT_WEEKS_DEF = 12   # cfg.JOURNAL_AUTO_EXIT_WEEKS のデフォルト


# ==============================================================
# BLOCK 1: ファイル I/O
# ==============================================================

def _journal_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / JOURNAL_FILENAME


def load_journal(cfg) -> dict:
    """
    investment_journal.json を読み込む。
    存在しない場合は空のジャーナル構造を返す。
    """
    p = _journal_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠️  investment_journal 読み込み失敗: {e}")
    return {
        "entries":          [],
        "pattern_analysis": {
            "miss_factor_frequency": {},
            "hit_factor_frequency":  {},
            "avg_alpha_by_conviction": {},
        },
    }


def save_journal(cfg, journal: dict) -> None:
    """investment_journal.json を保存する。"""
    p = _journal_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  investment_journal 保存失敗: {e}")


# ==============================================================
# BLOCK 2: エントリID生成
# ==============================================================

def _make_entry_id(journal: dict, ticker: str, entry_date: str) -> str:
    """
    "J-YYYY-NNN" 形式のユニークIDを生成する。
    同一年内で連番を付与する。
    """
    year = entry_date[:4]
    # 当年の最大連番を探す
    max_n = 0
    for e in journal.get("entries", []):
        eid = e.get("id", "")
        if eid.startswith(f"J-{year}-"):
            try:
                n = int(eid.split("-")[-1])
                max_n = max(max_n, n)
            except ValueError:
                pass
    return f"J-{year}-{max_n+1:03d}"


# ==============================================================
# BLOCK 3: 自動エントリ生成
# ==============================================================

def auto_create_entries(cfg, screener_result: dict, results: dict = None) -> int:
    """
    screener 通過銘柄のうち、journal に未登録のものを自動エントリする。

    screener_history.json に銘柄が初登場したタイミングで呼ばれる想定。
    stock_screener.py の run_stock_screener() 後に呼び出す。

    Args:
        cfg             : config モジュール
        screener_result : run_stock_screener() の戻り値
        results         : run_all_tasks() の results dict（macro_phase 等の取得用）

    Returns:
        新規作成したエントリ数
    """
    journal = load_journal(cfg)
    today   = date.today().strftime("%Y-%m-%d")

    # 既存エントリのticker集合（exitなしのもの）
    active_tickers = {
        e["ticker"] for e in journal["entries"]
        if not e.get("exit", {}).get("date")
    }

    candidates = [
        c for c in screener_result.get("candidates", [])
        if c.get("pass_filters") and c.get("ticker")
    ]

    results = results or {}
    macro_phase = results.get("economic_cycle", {}).get("phase", "?")
    regime_state = 0  # inference_engine の phase_code から変換（将来拡張）
    ie = results.get("inference", {})
    phase_code = ie.get("phase_code", "neutral")
    regime_map = {"risk_on_strong": 2, "risk_on": 1, "neutral": 0,
                  "risk_off": -1, "crisis": -2}
    regime_state = regime_map.get(phase_code, 0)

    created = 0
    for c in candidates:
        ticker = c.get("ticker", "")
        if not ticker or ticker in active_tickers:
            continue

        entry_id = _make_entry_id(journal, ticker, today)

        # conviction_level: composite_score から自動付与
        score = c.get("composite_score", 50.0) or 50.0
        if   score >= 80: conviction = 5
        elif score >= 70: conviction = 4
        elif score >= 60: conviction = 3
        elif score >= 50: conviction = 2
        else:             conviction = 1

        # cross_sector_signal の記録
        cross_adjs = c.get("cross_sector_adjustments", [])
        cross_label = (
            " / ".join(adj.get("rule", "") for adj in cross_adjs[:2])
            if cross_adjs else ""
        )

        new_entry = {
            "id":          entry_id,
            "ticker":      ticker,
            "company":     c.get("name", ticker),
            "sector_code": c.get("sector_code", ""),
            "entry": {
                "date":  today,
                "price": c.get("current_price"),
                "reason": {
                    "composite_score":     round(score, 1),
                    "cycle_phase":         macro_phase,
                    "regime_state":        regime_state,
                    "conviction_level":    conviction,
                    "key_factors": _infer_key_factors(c),
                    "catalyst_expected":   "",    # 手動記入待ち
                    "risk_noted":          [],    # 手動記入待ち
                    "cross_sector_signal": cross_label,
                    # I-2
                    "conservatism_score":  c.get("conservatism_score"),
                    # I-1
                    "acc_bonus":           c.get("acc_bonus"),
                },
            },
            "exit":       {},
            "result":     {},
            "post_mortem": {
                "hit_factors":       [],
                "miss_factors":      [],
                "unexpected_factor": "",
                "learning":          "",   # 手動記入待ち
                "system_feedback":   "",
                "manual_note":       "",   # 手動記入待ち
            },
            "_pending_manual": True,   # 手動記入待ちフラグ
            "_created_at": datetime.now().isoformat(),
        }
        journal["entries"].append(new_entry)
        active_tickers.add(ticker)
        created += 1

    if created > 0:
        save_journal(cfg, journal)
        print(f"  📔 investment_journal: {created}件 新規エントリ自動生成")

    return created


def _infer_key_factors(c: dict) -> list:
    """candidate dict からキーファクターを自動推論する。"""
    factors = []
    if (c.get("acc_bonus") or 0) >= 7:
        factors.append("EPS加速")
    if (c.get("conservatism_score") or 0) >= 70:
        factors.append("経営者保守的")
    if (c.get("is_activist_target") or False):
        factors.append("アクティビスト候補")
    if (c.get("is_net_cash_rich") or False):
        factors.append("ネットキャッシュリッチ")
    if (c.get("is_longterm_hold") or False):
        factors.append("長期保有候補")
    if (c.get("discovery_score") or 0) >= 75:
        factors.append("未発見銘柄")
    if (c.get("bq_bonus") or 0) >= 10:
        factors.append("事業モデル高品質")
    return factors[:5]


# ==============================================================
# BLOCK 4: リターン結果の自動更新
# ==============================================================

def update_results(cfg, performance_result: dict) -> int:
    """
    performance_tracker.py の週次集計結果を journal の result セクションに反映する。
    run_performance_tracker() 後に呼び出す。

    Args:
        cfg                : config モジュール
        performance_result : run_performance_tracker() の戻り値

    Returns:
        更新したエントリ数
    """
    if performance_result.get("skipped"):
        return 0

    journal  = load_journal(cfg)
    returns  = performance_result.get("returns", [])
    ret_map  = {r["ticker"]: r for r in returns if r.get("ticker")}

    updated = 0
    for entry in journal["entries"]:
        ticker = entry.get("ticker", "")
        if ticker not in ret_map:
            continue
        if entry.get("result", {}).get("return_pct") is not None:
            continue  # 既に更新済み

        r = ret_map[ticker]
        entry_date = entry.get("entry", {}).get("date", "")
        exit_date  = r.get("exit_date", "")
        ret_pct    = r.get("return_pct")

        if ret_pct is None:
            continue

        entry["result"] = {
            "return_pct":       round(ret_pct, 3),
            "topix_return_pct": None,   # 将来拡張: TOPIX比較
            "alpha":            None,
            "holding_days":     (
                (datetime.strptime(exit_date, "%Y-%m-%d")
                 - datetime.strptime(entry_date, "%Y-%m-%d")).days
                if entry_date and exit_date else None
            ),
        }

        # 自動 exit 日を設定（まだ exit がない場合）
        if not entry.get("exit", {}).get("date"):
            entry["exit"] = {
                "date":   exit_date,
                "price":  r.get("exit_price"),
                "reason": "performance_tracker 自動記録",
            }

        updated += 1

    # 自動 exit 推定（screener_history から消えた銘柄）
    updated += _auto_exit_stale_entries(cfg, journal)

    if updated > 0:
        # pattern_analysis を更新
        journal["pattern_analysis"] = _calc_pattern_analysis(journal)
        save_journal(cfg, journal)
        print(f"  📔 investment_journal: {updated}件 result自動更新")

    return updated


def _auto_exit_stale_entries(cfg, journal: dict) -> int:
    """
    JOURNAL_AUTO_EXIT_WEEKS 週以上 screener を通過していない銘柄を
    自動的に exit とみなす。
    """
    auto_exit_weeks = getattr(cfg, "JOURNAL_AUTO_EXIT_WEEKS", AUTO_EXIT_WEEKS_DEF)
    cutoff = (date.today() - timedelta(weeks=auto_exit_weeks)).strftime("%Y-%m-%d")

    updated = 0
    for entry in journal["entries"]:
        if entry.get("exit", {}).get("date"):
            continue   # 既に exit あり
        entry_date = entry.get("entry", {}).get("date", "")
        if entry_date and entry_date <= cutoff:
            entry["exit"] = {
                "date":   date.today().strftime("%Y-%m-%d"),
                "price":  None,
                "reason": f"スクリーナー未通過{auto_exit_weeks}週超 → 自動exit",
            }
            updated += 1

    return updated


# ==============================================================
# BLOCK 5: パターン分析
# ==============================================================

def _calc_pattern_analysis(journal: dict) -> dict:
    """
    全エントリから miss/hit ファクター頻度と確信度別αを集計する。

    rule_optimizer.py がこの結果を参照して重みを調整する（フェーズ4）。
    """
    miss_freq: dict[str, int] = {}
    hit_freq:  dict[str, int] = {}
    alpha_by_conv: dict[str, list] = {}

    for entry in journal["entries"]:
        pm = entry.get("post_mortem", {})
        # miss_factors
        for mf in pm.get("miss_factors", []):
            if mf:
                miss_freq[mf] = miss_freq.get(mf, 0) + 1
        # hit_factors
        for hf in pm.get("hit_factors", []):
            if hf:
                hit_freq[hf] = hit_freq.get(hf, 0) + 1
        # alpha by conviction
        result = entry.get("result", {})
        alpha  = result.get("alpha") or result.get("return_pct")
        conv   = entry.get("entry", {}).get("reason", {}).get("conviction_level")
        if alpha is not None and conv is not None:
            key = f"level_{conv}"
            alpha_by_conv.setdefault(key, []).append(float(alpha))

    avg_alpha = {
        k: round(sum(v) / len(v), 2)
        for k, v in alpha_by_conv.items()
        if v
    }

    return {
        "miss_factor_frequency": dict(sorted(
            miss_freq.items(), key=lambda x: -x[1]
        )),
        "hit_factor_frequency": dict(sorted(
            hit_freq.items(), key=lambda x: -x[1]
        )),
        "avg_alpha_by_conviction": avg_alpha,
    }


# ==============================================================
# BLOCK 6: 参照用インターフェース
# ==============================================================

def get_pending_entries(cfg) -> list:
    """
    手動記入待ちエントリの一覧を返す（weekly_report 向け）。

    Returns:
        [entry_dict, ...]  _pending_manual = True のエントリ
    """
    journal = load_journal(cfg)
    return [
        e for e in journal["entries"]
        if e.get("_pending_manual") and not e.get("exit", {}).get("date")
    ]


def get_pattern_analysis(cfg) -> dict:
    """pattern_analysis セクションを返す（rule_optimizer 連携用）。"""
    journal = load_journal(cfg)
    return journal.get("pattern_analysis", {})


def get_miss_factor_alerts(cfg) -> list:
    """
    miss_factor の頻度が閾値を超えた場合にアラートリストを返す。
    alert_notifier.py が参照する。
    """
    threshold = getattr(cfg, "MISS_FACTOR_ALERT_THRESHOLD", 3)
    pa = get_pattern_analysis(cfg)
    freq = pa.get("miss_factor_frequency", {})
    return [
        {"factor": f, "count": n, "severity": "high" if n >= threshold * 2 else "medium"}
        for f, n in freq.items()
        if n >= threshold
    ]


# ==============================================================
# BLOCK 7: weekly_report 向けフォーマット
# ==============================================================

def format_journal_section(cfg) -> list:
    """
    [R-3] 記入待ちエントリを週次レポート用 Markdown に変換する。

    Returns:
        Markdownラインリスト（空なら[]）
    """
    pending = get_pending_entries(cfg)
    if not pending:
        return []

    lines = [
        "## 📔 投資ジャーナル — 記入待ちエントリ",
        "",
        f"> 記入待ち: {len(pending)}件 "
        f"| investment_journal.json を直接編集 or stock_viewer から入力",
        "",
    ]
    lines.append("| ID | 銘柄 | エントリ日 | スコア | キーファクター |")
    lines.append("|-----|------|----------|--------|--------------|")
    for e in pending[:8]:
        reason = e.get("entry", {}).get("reason", {})
        kf     = " / ".join(reason.get("key_factors", [])[:3])
        lines.append(
            f"| {e['id']} "
            f"| {e.get('company','')[:14]}（{e['ticker']}）"
            f"| {e.get('entry',{}).get('date','')} "
            f"| {reason.get('composite_score','?')} "
            f"| {kf} |"
        )
    lines.append("")
    return lines


def format_pattern_section(cfg) -> list:
    """
    [R-3] 月次パターン分析を週次レポート用 Markdown に変換する。
    月初（1〜7日）のみ詳細版、それ以外はスキップ。
    """
    today = date.today()
    if today.day > 7:
        return []   # 月初以外は出力しない

    pa = get_pattern_analysis(cfg)
    miss_freq = pa.get("miss_factor_frequency", {})
    hit_freq  = pa.get("hit_factor_frequency",  {})
    alpha_cv  = pa.get("avg_alpha_by_conviction", {})

    if not miss_freq and not hit_freq:
        return []

    threshold  = getattr(cfg, "MISS_FACTOR_ALERT_THRESHOLD", 3)
    lines = ["## 📊 月次パターン分析（投資ジャーナル）", ""]

    if miss_freq:
        lines.append("### 🔴 外れパターン（miss_factor 頻度上位）")
        lines.append("")
        for f, n in list(miss_freq.items())[:5]:
            warn = " ⚠️ ルール見直し推奨" if n >= threshold else ""
            lines.append(f"- **{f}**: {n}回{warn}")
        lines.append("")

    if hit_freq:
        lines.append("### 💚 勝因パターン（hit_factor 頻度上位）")
        lines.append("")
        for f, n in list(hit_freq.items())[:5]:
            lines.append(f"- **{f}**: {n}回")
        lines.append("")

    if alpha_cv:
        lines.append("### 📐 確信度 vs 実績α")
        lines.append("")
        lines.append("| 確信度レベル | 平均α |")
        lines.append("|------------|------|")
        for k in sorted(alpha_cv.keys(), reverse=True):
            v = alpha_cv[k]
            lines.append(f"| {k} | {v:+.2f}% |")
        lines.append("")

    return lines


# ==============================================================
# BLOCK 8: rule_optimizer との連携
# ==============================================================

def get_miss_factor_weight_hints(cfg) -> dict:
    """
    [フェーズ4用] miss_factor_frequency を rule_optimizer の重み調整ヒントに変換する。

    Returns:
        {factor_name: weight_adjustment}
        例: {"為替リスク過小評価": -0.1} → yen_depreciation ルールの重みを下げるヒント

    この関数の結果を rule_optimizer.py の run_rule_optimizer() が参照することで
    人間の「なぜ外れたか」認識がシステムの重み自動調整に反映される。
    """
    threshold = getattr(cfg, "MISS_FACTOR_ALERT_THRESHOLD", 3)
    freq = get_pattern_analysis(cfg).get("miss_factor_frequency", {})

    hints = {}
    # miss_factor 名称 → MACRO_RULES ファクター名 の簡易マッピング
    FACTOR_MAP = {
        "為替リスク過小評価":     "yen_depreciation",
        "円高リスク":             "yen_appreciation",
        "金利上昇リスク":         "interest_rate_rise",
        "地政学リスク":           "geopolitical_risk",
        "原油価格リスク":         "oil_price_rise",
        "景気後退リスク":         "us_recession_risk",
    }
    for miss_f, count in freq.items():
        if count >= threshold:
            factor_key = FACTOR_MAP.get(miss_f)
            if factor_key:
                # 頻度が多いほど重みを下げる（最大 -0.2）
                adj = max(-0.20, -0.05 * (count - threshold + 1))
                hints[factor_key] = round(adj, 3)

    return hints


# ==============================================================
# BLOCK 9: メインエントリポイント
# ==============================================================

def run_investment_journal(cfg, screener_result: dict = None,
                           performance_result: dict = None,
                           results: dict = None) -> dict:
    """
    投資ジャーナルの自動更新を実行する。
    stock_analyzer_main.py の screener 実行後・performance_tracker 後に呼ぶ。

    Returns:
        {
          "created_entries":  int,
          "updated_results":  int,
          "pending_count":    int,
          "miss_alerts":      list,
          "timestamp":        str,
        }
    """
    print("\n  📔 投資ジャーナル 更新中...")

    created = 0
    updated = 0

    # ① screener 結果から新規エントリ生成
    if screener_result:
        created = auto_create_entries(cfg, screener_result, results)

    # ② performance_tracker 結果からリターン更新
    if performance_result and not performance_result.get("skipped"):
        updated = update_results(cfg, performance_result)

    # ③ 手動記入待ち件数
    pending = get_pending_entries(cfg)

    # ④ miss_factor アラート
    miss_alerts = get_miss_factor_alerts(cfg)
    if miss_alerts:
        print(f"    ⚠️  miss_factor閾値超: "
              f"{', '.join(a['factor'] for a in miss_alerts[:3])}")

    print(f"    ✅ journal更新: 新規{created}件 / 結果更新{updated}件 "
          f"/ 記入待ち{len(pending)}件")

    return {
        "created_entries": created,
        "updated_results": updated,
        "pending_count":   len(pending),
        "miss_alerts":     miss_alerts,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
