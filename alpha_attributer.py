# ==============================================================
# alpha_attributer.py  v1.0  (GAP-8)
# シグナル別アルファ帰属分析（Attribution Log）
# ==============================================================
# 【役割】
#   「どのシグナルが実際のアルファに貢献したか」を定量化する。
#   performance_history（実績リターン）と signal_history（シグナル状態）を
#   紐付けし、シグナル別の平均アルファと情報係数（IC）を計算する。
#
# 【情報係数（IC）とは】
#   IC = シグナルスコアと翌週リターンのPearson相関係数（-1〜+1）
#   IC > 0.05: 有意なα生成力あり
#   IC > 0.10: 優秀なシグナル
#   IC < 0:    逆効果（使わない方が良い）
#
# 【入力データ】
#   performance_history.json: 週次勝率・リターン実績
#   signal_history.json:      各週のシグナル発動状態（本ファイルが新規保存）
#   screener_history.json:    screener通過銘柄のスコア履歴
#   score_history.json:       業種別スコア時系列
#
# 【出力スキーマ】
#   {
#     "signals": {
#       "yen_depreciation": {
#         "ic":              float,   # 情報係数（-1〜+1）
#         "avg_alpha_pct":   float,   # 発動週の平均アルファ%
#         "fire_count":      int,     # 分析期間内の発動回数
#         "win_rate_when_fired": float,  # 発動時の勝率
#         "significance":    str,     # "high"/"medium"/"low"/"negative"
#         "suggestion":      str,     # ルール見直し提案
#       }, ...
#     },
#     "top_signals":     [signal_name, ...],  # IC上位5
#     "weak_signals":    [signal_name, ...],  # IC<0のシグナル
#     "period_weeks":    int,
#     "timestamp":       str,
#   }
#
# 【信号履歴の保存形式（signal_history.json）】
#   {
#     "2026-W12": {
#       "fired":    ["yen_depreciation", "oil_price_rise"],
#       "scores":   {"yen_depreciation": 1.2, "oil_price_rise": 0.8},
#       "snapshot": {"usdjpy": 152.3, "oil_wti": 87.5, ...}
#     }, ...
#   }
#
# 【依存】config.py / rule_optimizer.py（FACTOR_THRESHOLDS参照）
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

SIGNAL_HISTORY_FILE  = "signal_history.json"
ATTRIBUTION_FILE     = "alpha_attribution.json"

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'AA_IC_HIGH_THRESHOLD': 'IC_HIGH_THRESHOLD',
    'AA_IC_MID_THRESHOLD': 'IC_MID_THRESHOLD',
    'AA_IC_NEG_THRESHOLD': 'IC_NEG_THRESHOLD',
    'B4_ALPHA_ATTRIBUTER_LOOKBACK_WEEKS': 'LOOKBACK_WEEKS',
    'B6_ALPHA_ATTRIBUTER_MIN_FIRE_COUNT': 'MIN_FIRE_COUNT',
}

IC_HIGH_THRESHOLD    = 0.10   # IC > 0.10: 優秀
IC_MID_THRESHOLD     = 0.05   # IC > 0.05: 有意
IC_NEG_THRESHOLD     = 0.00   # IC < 0: 逆効果
MIN_FIRE_COUNT       = 3      # IC計算に最低必要な発動回数
LOOKBACK_WEEKS       = 26     # 分析対象期間（半年 = 26週）


# ==============================================================
# BLOCK 1: ファイル I/O
# ==============================================================

def _data_dir(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data"))


def load_signal_history(cfg) -> dict:
    """signal_history.json を読み込む。"""
    p = _data_dir(cfg) / SIGNAL_HISTORY_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_signal_history(cfg, history: dict) -> None:
    p = _data_dir(cfg) / SIGNAL_HISTORY_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  signal_history 保存失敗: {e}")


def load_performance_history(cfg) -> list:
    """performance_history.json を読み込む（週次リターン実績）。"""
    p = _data_dir(cfg) / "performance_history.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


def load_score_history(cfg) -> dict:
    """score_history.json を読み込む（業種別スコア時系列）。"""
    p = _data_dir(cfg) / "score_history.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def load_attribution(cfg) -> dict:
    """alpha_attribution.json を読み込む（前回の計算結果キャッシュ）。"""
    p = _data_dir(cfg) / ATTRIBUTION_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_attribution(cfg, result: dict) -> None:
    p = _data_dir(cfg) / ATTRIBUTION_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  alpha_attribution 保存失敗: {e}")


# ==============================================================
# BLOCK 2: シグナル履歴の記録（run_all_tasks から呼び出す）
# ==============================================================

def record_signal_state(cfg, results: dict) -> None:
    """
    当日のシグナル発動状態を signal_history.json に記録する。

    stock_analyzer_main.py の macro ブロック完了後に呼び出す:
        _aa = _load_local_module("alpha_attributer", ...)
        if _aa:
            _aa.record_signal_state(cfg, results)

    記録内容:
      - macro_signals の発動ファクターリスト
      - evaluate_cross_sector_signals の発動ルール
      - D-1 経済サイクルフェーズ
      - 各シグナルの重み（rule_weights）
    """
    today   = date.today().strftime("%Y-%m-%d")
    iso_cal = date.today().isocalendar()
    week_key = f"{iso_cal[0]}-W{iso_cal[1]:02d}"

    history = load_signal_history(cfg)

    # macro_signals から発動ファクターを収集
    macro_signals = results.get("macro_signals", [])
    fired_factors  = []
    factor_scores  = {}

    for sig in macro_signals:
        factor = sig.get("factor", "")
        weight = sig.get("weight", 1.0) or 1.0
        if factor:
            fired_factors.append(factor)
            factor_scores[factor] = round(float(weight), 4)

    # CROSS_SECTOR_RULES の発動シグナル
    cross_signals = results.get("cross_sector_signals", [])
    for cs in cross_signals:
        rule_id = cs.get("rule_id", "")
        if rule_id:
            fired_factors.append(f"cross_{rule_id}")
            factor_scores[f"cross_{rule_id}"] = 1.0

    # 経済サイクルフェーズ
    ec = results.get("economic_cycle", {})
    ec_phase = ec.get("phase")
    if ec_phase:
        factor_scores[f"phase_{ec_phase}"] = float(ec.get("confidence", 0.5))
        fired_factors.append(f"phase_{ec_phase}")

    # 債務サイクル
    dc = results.get("debt_cycle", {})
    dc_score = dc.get("debt_cycle_score")
    if dc_score is not None:
        factor_scores["debt_cycle"] = round(float(dc_score) / 100, 3)

    # macro_snapshot を保存（週末の最新値を上書き）
    macro_snap = results.get("macro_snapshot", {})
    # deltaフィールドを除いたクリーンなスナップショット
    snap_clean = {k: v for k, v in macro_snap.items()
                  if not k.startswith("_") and not k.endswith("_delta")
                  and not k.endswith("_alert") and not k.endswith("_delta_pct")}

    history[week_key] = {
        "date":     today,
        "fired":    fired_factors,
        "scores":   factor_scores,
        "snapshot": snap_clean,
    }

    # 2年分（104週）保持
    cutoff_year = date.today().year - 2
    history = {
        k: v for k, v in history.items()
        if int(k[:4]) >= cutoff_year
    }

    save_signal_history(cfg, history)


def record_category_panel(cfg, results: dict) -> int:
    """
    [R1-4 / E4] 22カテゴリ×業種の当日スコアパネルを日次で蓄積する。

    signal_aggregator の sector_signals[code]["components"]（カテゴリ別スコア）を
    DATA_DIR/category_panel.jsonl に1日1行で追記する。
    signal_culling_report.py（月次）がこのパネルと20日先リターンを突合して
    カテゴリ別ICを算出する（判定の正本は要件定義書 §R1-4）。

    Returns: 記録した業種数（0 = データなし/同日記録済み）
    """
    sa = results.get("signal_aggregator") or {}
    sector_signals = sa.get("sector_signals") or {}
    if not sector_signals:
        return 0

    today = date.today().strftime("%Y-%m-%d")
    p = _data_dir(cfg) / "category_panel.jsonl"

    lines = []
    if p.exists():
        try:
            lines = [l for l in p.read_text(encoding="utf-8").splitlines()
                     if l.strip()]
        except Exception:
            lines = []
        for l in lines[-3:]:                       # 同日再実行の冪等ガード
            try:
                if json.loads(l).get("date") == today:
                    return 0
            except Exception:
                continue

    sectors = {}
    for code, rec in sector_signals.items():
        comps = (rec or {}).get("components") or {}
        clean = {cat: round(float(v), 2) for cat, v in comps.items()
                 if v is not None}
        if clean:
            sectors[code] = clean
    if not sectors:
        return 0

    lines.append(json.dumps({"date": today, "sectors": sectors},
                            ensure_ascii=False))
    lines = lines[-400:]                            # 約1.5年分を保持
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(sectors)


# ==============================================================
# BLOCK 3: 情報係数（IC）計算
# ==============================================================

def _pearson_ic(x_list: list, y_list: list) -> Optional[float]:
    """
    Pearson相関係数を計算する。

    Args:
        x_list: シグナルスコア（発動=1/非発動=0、または重み値）
        y_list: 対応する週次リターン（%）

    Returns:
        IC値（-1〜+1）。計算不可の場合は None。
    """
    n = len(x_list)
    if n < 3 or len(y_list) != n:
        return None

    mx = sum(x_list) / n
    my = sum(y_list) / n

    cov = sum((x - mx) * (y - my) for x, y in zip(x_list, y_list)) / n
    sx  = math.sqrt(sum((x - mx) ** 2 for x in x_list) / n)
    sy  = math.sqrt(sum((y - my) ** 2 for y in y_list) / n)

    if sx < 1e-9 or sy < 1e-9:
        return None   # 分散ゼロ（全シグナルが同じ）

    return round(cov / (sx * sy), 4)


def _calc_weekly_alpha(performance_history: list, weeks_back: int = LOOKBACK_WEEKS) -> dict:
    """
    performance_history から週次アルファ（avg_return）を抽出する。

    Returns:
        {week_key: avg_return_pct}
    """
    result = {}
    for rec in performance_history:
        week = rec.get("week", "")
        ret  = rec.get("avg_return")
        if week and ret is not None:
            result[week] = float(ret)
    return result


def _calc_sector_alpha(score_history: dict, weeks_back: int = LOOKBACK_WEEKS) -> dict:
    """
    score_history から週次の業種スコア変化（アルファ代理変数）を計算する。

    Returns:
        {week_key: avg_score_change}  全業種平均スコア変化率
    """
    dates = sorted(score_history.keys())
    if len(dates) < 2:
        return {}

    result = {}
    for i in range(1, len(dates)):
        prev_data = score_history[dates[i - 1]]
        curr_data = score_history[dates[i]]
        if not isinstance(prev_data, dict) or not isinstance(curr_data, dict):
            continue

        changes = []
        for code in curr_data:
            p = prev_data.get(code)
            c = curr_data.get(code)
            p_sc = p.get("score") if isinstance(p, dict) else p
            c_sc = c.get("score") if isinstance(c, dict) else c
            if p_sc is not None and c_sc is not None and abs(p_sc) > 0.01:
                changes.append((float(c_sc) - float(p_sc)) / abs(float(p_sc)) * 100)

        if changes:
            # 週キーに変換
            try:
                d = datetime.strptime(dates[i], "%Y-%m-%d").date()
                iso = d.isocalendar()
                week_key = f"{iso[0]}-W{iso[1]:02d}"
                result[week_key] = round(sum(changes) / len(changes), 3)
            except Exception:
                pass

    return result


# ==============================================================
# BLOCK 4: シグナル別アルファ帰属計算
# ==============================================================

def calc_signal_attribution(cfg) -> dict:
    """
    シグナル別の情報係数（IC）と平均アルファを計算する。

    処理フロー:
      1. signal_history.json から各週の発動シグナルを取得
      2. performance_history.json から各週の実績リターンを取得
      3. シグナル発動フラグ × 週次リターンの相関 = IC
      4. 発動週のみの平均リターン = avg_alpha

    Returns:
        attribution dict（上記スキーマ）
    """
    signal_history    = load_signal_history(cfg)
    performance_hist  = load_performance_history(cfg)
    score_history     = load_score_history(cfg)

    # 週次アルファの取得（performance_history 優先、なければ score_history から近似）
    weekly_alpha = _calc_weekly_alpha(performance_hist)
    if not weekly_alpha:
        weekly_alpha = _calc_sector_alpha(score_history)

    if not weekly_alpha or not signal_history:
        return {
            "signals":      {},
            "top_signals":  [],
            "weak_signals": [],
            "period_weeks": 0,
            "note":         "データ不足（performance_history または signal_historyが空）",
            "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # 共通する週のみ分析対象
    common_weeks = sorted(set(weekly_alpha.keys()) & set(signal_history.keys()))

    # 分析期間と最低発動回数は config.py BLOCK GAP-8（ATTRIBUTION_LOOKBACK_W /
    # ATTRIBUTION_MIN_FIRE）が正本。モジュール定数は config に無いときの既定値。
    _lookback = int(getattr(cfg, "ATTRIBUTION_LOOKBACK_W", LOOKBACK_WEEKS))
    _min_fire = int(getattr(cfg, "ATTRIBUTION_MIN_FIRE", MIN_FIRE_COUNT))

    # 直近 _lookback 週に限定
    common_weeks = common_weeks[-_lookback:]

    if len(common_weeks) < _min_fire:
        return {
            "signals":      {},
            "top_signals":  [],
            "weak_signals": [],
            "period_weeks": len(common_weeks),
            "note":         f"共通週が不足（{len(common_weeks)}週 < {_min_fire}週）",
            "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # 全シグナルのリスト（signal_history 内で1回以上発動したもの全て）
    all_signals: set[str] = set()
    for wk in common_weeks:
        for f in signal_history[wk].get("fired", []):
            all_signals.add(f)

    # シグナル別に IC・平均アルファを計算
    signal_results = {}

    for signal_name in sorted(all_signals):
        x_fired  = []   # 発動フラグ（0/1）
        x_weight = []   # 発動重み（スコア値）
        y_alpha  = []   # 対応週のアルファ

        fired_alphas   = []   # 発動週のアルファ（勝率計算用）
        notfired_alphas= []   # 非発動週のアルファ

        for wk in common_weeks:
            sig_data = signal_history[wk]
            fired    = signal_name in sig_data.get("fired", [])
            weight   = sig_data.get("scores", {}).get(signal_name, 0.0) if fired else 0.0
            alpha    = weekly_alpha.get(wk)
            if alpha is None:
                continue

            x_fired.append(1.0 if fired else 0.0)
            x_weight.append(float(weight))
            y_alpha.append(float(alpha))

            if fired:
                fired_alphas.append(float(alpha))
            else:
                notfired_alphas.append(float(alpha))

        fire_count = sum(1 for x in x_fired if x > 0)
        if fire_count < _min_fire:
            continue   # 発動回数が少なすぎる

        ic_binary = _pearson_ic(x_fired, y_alpha)    # 発動0/1 × リターン
        ic_weight = _pearson_ic(x_weight, y_alpha)   # 重み × リターン
        ic         = ic_weight if ic_weight is not None else ic_binary

        avg_alpha_when_fired    = (sum(fired_alphas)    / len(fired_alphas)
                                   if fired_alphas    else None)
        avg_alpha_when_notfired = (sum(notfired_alphas) / len(notfired_alphas)
                                   if notfired_alphas else None)
        alpha_diff = (
            round(avg_alpha_when_fired - avg_alpha_when_notfired, 3)
            if avg_alpha_when_fired is not None and avg_alpha_when_notfired is not None
            else None
        )

        win_rate_fired = (
            sum(1 for a in fired_alphas if a > 0) / len(fired_alphas)
            if fired_alphas else None
        )

        # 有意性ラベル
        if ic is None:
            significance = "unknown"
        elif ic >= IC_HIGH_THRESHOLD:
            significance = "high"
        elif ic >= IC_MID_THRESHOLD:
            significance = "medium"
        elif ic >= IC_NEG_THRESHOLD:
            significance = "low"
        else:
            significance = "negative"

        # ルール見直し提案
        if significance == "negative":
            suggestion = (
                f"⚠️ IC={ic:.3f}（逆効果）: このシグナルは発動時にアルファが低下する傾向。"
                "rule_optimizer で重みを下げることを推奨。"
            )
        elif significance == "low" and fire_count >= 10:
            suggestion = (
                f"📊 IC={ic:.3f}（低有意）: 効果は小さい。"
                "発動閾値の見直しを検討。"
            )
        elif significance in ("high", "medium"):
            suggestion = (
                f"✅ IC={ic:.3f}（有効）: このシグナルは継続活用を推奨。"
                f"発動時の平均α={avg_alpha_when_fired:.2f}%。"
                if avg_alpha_when_fired is not None else ""
            )
        else:
            suggestion = ""

        signal_results[signal_name] = {
            "ic":                       ic,
            "ic_binary":                ic_binary,
            "avg_alpha_when_fired_pct": round(avg_alpha_when_fired, 3) if avg_alpha_when_fired else None,
            "avg_alpha_notfired_pct":   round(avg_alpha_when_notfired, 3) if avg_alpha_when_notfired else None,
            "alpha_diff_pct":           alpha_diff,
            "win_rate_when_fired":      round(win_rate_fired, 3) if win_rate_fired is not None else None,
            "fire_count":               fire_count,
            "total_weeks":              len(common_weeks),
            "significance":             significance,
            "suggestion":               suggestion,
        }

    # ランキング
    sorted_by_ic   = sorted(signal_results.items(),
                             key=lambda x: x[1].get("ic") or -99, reverse=True)
    top_signals    = [s for s, d in sorted_by_ic[:5] if (d.get("ic") or 0) > 0]
    weak_signals   = [s for s, d in sorted_by_ic if (d.get("ic") or 0) < IC_NEG_THRESHOLD]

    return {
        "signals":      signal_results,
        "top_signals":  top_signals,
        "weak_signals": weak_signals,
        "period_weeks": len(common_weeks),
        "analyzed_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 5: weekly_report 向けフォーマット
# ==============================================================

def format_attribution_section(cfg) -> list:
    """
    週次レポート向けのアトリビューションセクションを生成する（月初のみ詳細版）。
    """
    today = date.today()
    # 月初のみ詳細版（月1〜7日）
    is_monthly = (today.day <= 7)

    result = load_attribution(cfg)
    if not result or not result.get("signals"):
        return []

    lines = ["## 📐 シグナル別アルファ帰属分析（GAP-8）", ""]
    period = result.get("period_weeks", 0)
    lines.append(f"**分析期間**: 直近{period}週 | **更新**: {result.get('analyzed_at', '')[:10]}")
    lines.append("")

    # TOP シグナル
    top = result.get("top_signals", [])
    if top:
        lines.append("### ✅ 有効シグナル（IC > 0）")
        lines.append("")
        lines.append("| シグナル | IC | 発動時平均α | 勝率 | 発動回数 |")
        lines.append("|---------|-----|-----------|------|---------|")
        for sig_name in top[:5]:
            d = result["signals"].get(sig_name, {})
            ic    = d.get("ic")
            alpha = d.get("avg_alpha_when_fired_pct")
            wr    = d.get("win_rate_when_fired")
            cnt   = d.get("fire_count", 0)
            lines.append(
                f"| {sig_name[:25]} "
                f"| {ic:.3f} "
                f"| {alpha:+.2f}% "
                f"| {wr*100:.0f}% "
                f"| {cnt}回 |"
                if ic is not None and alpha is not None and wr is not None
                else f"| {sig_name[:25]} | N/A | N/A | N/A | {cnt}回 |"
            )
        lines.append("")

    # 逆効果シグナル（月初のみ）
    weak = result.get("weak_signals", [])
    if weak and is_monthly:
        lines.append("### ⚠️ 要見直しシグナル（IC < 0）")
        lines.append("")
        for sig_name in weak[:3]:
            d = result["signals"].get(sig_name, {})
            ic  = d.get("ic")
            sug = d.get("suggestion", "")
            lines.append(f"- **{sig_name}**: IC={ic:.3f} | {sug[:60]}")
        lines.append("")

    return lines


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_alpha_attributer(cfg, results: dict | None = None) -> dict:
    """
    シグナル状態を記録し、月末には IC 計算を実行する。

    stock_analyzer_main.py から以下のように呼び出す:
        _aa = _load_local_module("alpha_attributer", ...)
        if _aa:
            aa_result = _aa.run_alpha_attributer(cfg, results)
            results["alpha_attribution"] = aa_result

    処理:
      1. 毎日: 当日のシグナル状態を signal_history.json に記録
      2. 月末のみ: IC 計算を実行し alpha_attribution.json に保存

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の results dict（シグナル記録用）

    Returns:
        {"recorded": bool, "calculated": bool, "attribution": dict}
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_ALPHA_ATTRIBUTION", True):
        return {"skipped": True}

    print("\n  📐 アルファ帰属分析 処理中...")

    # ── Step 1: 当日シグナル状態を記録 ──────────────────────
    recorded = False
    if results:
        try:
            record_signal_state(cfg, results)
            recorded = True
            print("    ✅ シグナル状態を記録")
        except Exception as e:
            print(f"    ⚠️  シグナル記録エラー: {e}")
        # [R1-4 / E4] カテゴリ別スコアパネルの日次蓄積
        try:
            n_panel = record_category_panel(cfg, results)
            if n_panel:
                print(f"    ✅ カテゴリパネル記録: {n_panel}業種")
        except Exception as e:
            print(f"    ⚠️  カテゴリパネル記録エラー: {e}")

    # ── Step 2: 月末のみ IC 計算 ─────────────────────────────
    calculated = False
    today = date.today()
    # 週次（月曜）の回からしか呼ばれないので、月末日ちょうどではなく「月の最後の7日間」で判定する
    # （月末日ちょうどだと月末が月曜の月しか動かない）。IC は上書きなので同じ月に2回走っても同じ結果。
    is_month_end = (today + timedelta(days=int(getattr(cfg, "RULE_OPTIMIZER_MONTH_END_WINDOW_DAYS", 7)))).month != today.month

    if is_month_end or getattr(cfg, "FORCE_ATTRIBUTION_CALC", False):
        try:
            print("    📊 IC計算実行中...")
            attribution = calc_signal_attribution(cfg)
            save_attribution(cfg, attribution)
            calculated = True

            top = attribution.get("top_signals", [])
            weak = attribution.get("weak_signals", [])
            print(f"    ✅ IC計算完了: "
                  f"有効シグナル{len(top)}件 / 逆効果{len(weak)}件")
            if top:
                sigs = attribution.get("signals", {})
                for t in top[:3]:
                    ic = sigs.get(t, {}).get("ic")
                    print(f"       {t}: IC={ic:.3f}" if ic else f"       {t}")
        except Exception as e:
            attribution = {}
            print(f"    ⚠️  IC計算エラー: {e}")
    else:
        attribution = load_attribution(cfg)

    return {
        "recorded":    recorded,
        "calculated":  calculated,
        "attribution": attribution,
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# ==============================================================
# BLOCK 7: [A-1] 週次ICトラッカー（ic_tracker.json）
# ==============================================================

IC_TRACKER_FILE = "ic_tracker.json"


def load_ic_tracker(cfg) -> dict:
    """ic_tracker.json を読み込む。"""
    p = _data_dir(cfg) / IC_TRACKER_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_ic_tracker(cfg, tracker: dict) -> None:
    p = _data_dir(cfg) / IC_TRACKER_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(tracker, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  ic_tracker 保存失敗: {e}")


def update_ic_tracker(cfg, attribution: dict) -> dict:
    """
    [A-1] 週次でICを記録し、一貫してプラスのシグナルを特定する。

    処理:
      1. attribution の signals を ic_tracker.json に週次で追記
      2. 直近12週でIC > 0 が継続しているシグナル = 「安定有効シグナル」
      3. IC < 0 が3週連続 = 「rule_optimizer 見直し推奨」

    Returns:
        {
          "stable_signals":     [signal_name, ...],  # 12週連続IC > 0
          "review_signals":     [signal_name, ...],  # 3週連続IC < 0
          "weekly_ic_history":  {signal: [ic,...] / 週次系列},
          "recommendation":     str,
        }
    """
    tracker  = load_ic_tracker(cfg)
    today    = date.today()
    iso_cal  = today.isocalendar()
    week_key = f"{iso_cal[0]}-W{iso_cal[1]:02d}"

    signals = attribution.get("signals", {})
    if not signals:
        return {}

    # 今週のICを記録
    tracker.setdefault("weekly", {})
    tracker["weekly"][week_key] = {
        sig: round(d.get("ic", 0) or 0, 4)
        for sig, d in signals.items()
    }

    # 2年分保持（104週）
    cutoff_year = today.year - 2
    tracker["weekly"] = {
        k: v for k, v in tracker["weekly"].items()
        if int(k[:4]) >= cutoff_year
    }

    # 全シグナルの週次IC系列を構築（直近12週）
    sorted_weeks = sorted(tracker["weekly"].keys())[-12:]
    all_signals  = set()
    for wk_data in tracker["weekly"].values():
        all_signals.update(wk_data.keys())

    weekly_ic_history = {}
    for sig in sorted(all_signals):
        ic_series = [
            tracker["weekly"].get(wk, {}).get(sig)
            for wk in sorted_weeks
        ]
        # None を除いた実値のみ
        ic_valid = [v for v in ic_series if v is not None]
        weekly_ic_history[sig] = ic_valid

    # 安定有効シグナル（直近12週で IC >= 0 だった週の比率が閾値以上）
    # 閾値は config.py BLOCK A-1（IC_STABLE_MIN_RATE / IC_REVIEW_CONSECUTIVE_NEG）が正本。
    # ここに直書きしない（SSOT）。
    _stable_rate = float(getattr(cfg, "IC_STABLE_MIN_RATE", 0.80))
    _neg_weeks   = int(getattr(cfg, "IC_REVIEW_CONSECUTIVE_NEG", 3))
    stable_signals = []
    review_signals = []
    for sig, ic_series in weekly_ic_history.items():
        # 判定に足る週数が無いものは対象外（見直し窓と同じ週数を最低要件とする）
        if len(ic_series) < _neg_weeks:
            continue
        pos_count = sum(1 for v in ic_series if v >= 0)
        pos_rate  = pos_count / len(ic_series)

        # 直近 _neg_weeks 週が全て負 → 見直し推奨
        last_neg = all(v < 0 for v in ic_series[-_neg_weeks:])

        if pos_rate >= _stable_rate:
            stable_signals.append(sig)
        if last_neg:
            review_signals.append(sig)

    # rule_optimizer への推薦文
    rec_lines = []
    if review_signals:
        rec_lines.append(
            f"⚠️ rule_optimizer 見直し推奨シグナル: "
            f"{', '.join(review_signals[:5])} → 重みを下げることを検討"
        )
    if stable_signals:
        rec_lines.append(
            f"✅ 安定有効シグナル（継続活用推奨）: {', '.join(stable_signals[:5])}"
        )

    result = {
        "stable_signals":    stable_signals,
        "review_signals":    review_signals,
        "weekly_ic_history": weekly_ic_history,
        "recommendation":    "\n".join(rec_lines),
        "analyzed_weeks":    len(sorted_weeks),
        "updated_at":        today.strftime("%Y-%m-%d"),
    }
    tracker["latest"] = result
    save_ic_tracker(cfg, tracker)
    return result
