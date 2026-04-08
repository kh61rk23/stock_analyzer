# ==============================================================
# midterm_plan_tracker.py  v1.0  (I-5 / 井村俊哉モデル)
# 中期経営計画達成率トラッカー
# ==============================================================
# 【役割】
#   企業の中期経営計画（中計）の目標 vs 実績を自動追跡し、
#   「公約を守る経営者かどうか」を数値化する。
#
# 【データ取得戦略】
#   ① edinet_rotation_manager の蓄積 DB から中計データを取得
#      （analysis.midterm_plan キーに格納されている場合）
#   ② yfinance の財務データで実績値を補完
#   ③ TDnet RSS の中計発表イベントと連携（M-4）
#
# 【中計DB（midterm_plans.json）の構造】
#   {ticker: {"plans": [...], "current_plan": {...},
#              "avg_achievement_rate": float, "updated_at": str}}
#
# 【接続先モジュール】
#   ceo_evaluator.py（I-3）: avg_achievement_rate → 軸② スコアに反映
#   alert_notifier.py:       高信頼性中計発表・中計未達リスクのアラート
#   weekly_report_generator: 「注目中計進捗」セクション
#
# 【依存】config.py / edinet_rotation_manager DB / yfinance
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

MIDTERM_DB_FILE      = "midterm_plans.json"
SCORES_RETAIN_YEARS  = 7    # 7年分保持（中計は3〜5年周期）

# 達成率閾値
ACHIEVEMENT_HIGH   = 0.90   # 90%以上 → 高達成
ACHIEVEMENT_MEDIUM = 0.70   # 70%以上 → 中達成
ACHIEVEMENT_RISK   = 0.70   # 70%未満（最終年度2年前時点）→ 未達リスク

# 年次進捗の超過達成閾値
OVERTRACK_THRESHOLD = 1.0   # 按分目標の100%超で「超過達成軌道」

# ceo_scoreの軸②スコアへの変換
CEO_AXIS2_HIGH   = 25.0   # avg_achievement_rate > 90% → 25pt
CEO_AXIS2_MEDIUM = 15.0   # 70〜90% → 15pt
CEO_AXIS2_LOW    =  0.0   # 70%未満 → 0pt


# ==============================================================
# BLOCK 1: DB I/O
# ==============================================================

def _db_path(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / MIDTERM_DB_FILE


def load_midterm_db(cfg) -> dict:
    """midterm_plans.json を読み込む。"""
    p = _db_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_midterm_db(cfg, db: dict) -> None:
    p = _db_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️  midterm_plans.json 保存失敗: {e}")


def get_achievement_rate(cfg, ticker: str) -> Optional[float]:
    """
    指定 ticker の過去中計平均達成率を返す。
    ceo_evaluator.py（I-3）の軸② から呼び出す。

    Returns:
        avg_achievement_rate（0.0〜1.0）または None
    """
    db = load_midterm_db(cfg)
    entry = db.get(ticker, {})
    return entry.get("avg_achievement_rate")


def get_ceo_axis2_score(cfg, ticker: str) -> float:
    """
    avg_achievement_rate から I-3 軸② スコアを返す。
    ceo_evaluator.py の _axis2_midterm_plan() が呼び出す。
    """
    rate = get_achievement_rate(cfg, ticker)
    if rate is None:
        return 12.0   # データなし → 中立
    if rate >= ACHIEVEMENT_HIGH:
        return CEO_AXIS2_HIGH
    elif rate >= ACHIEVEMENT_MEDIUM:
        return CEO_AXIS2_MEDIUM
    else:
        return CEO_AXIS2_LOW


# ==============================================================
# BLOCK 2: EDINET/yfinance からデータ取得
# ==============================================================

def _get_midterm_from_edinet(ticker: str, edinet_db: dict) -> Optional[dict]:
    """
    edinet_rotation_manager の analysis.midterm_plan から中計データを取得する。

    edinet_rotation_manager が蓄積する analysis JSON の想定キー:
    {
      "midterm_plan": {
        "plan_name":         str,
        "start_year":        int,
        "end_year":          int,
        "targets": {
          "revenue_cagr":      float,
          "operating_margin":  float,
          "roe":               float,
        },
        "as_of_date":        str,
      }
    }
    """
    analysis = edinet_db.get("analysis", {}).get(ticker, {})
    if not isinstance(analysis, dict):
        return None
    return analysis.get("midterm_plan")


def _get_actuals_from_yfinance(ticker: str, plan: dict) -> dict:
    """
    yfinance から実績値を取得して中計の targets と照合する。

    Returns:
        actuals dict（targets と同じキー構造）
    """
    actuals = {}
    targets = plan.get("targets", {})
    if not targets:
        return actuals

    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        info = t.info or {}

        # 売上CAGR（yfinance の revenueGrowth で近似）
        if "revenue_cagr" in targets:
            rev_growth = info.get("revenueGrowth")
            if rev_growth is not None:
                actuals["revenue_cagr"] = round(float(rev_growth), 4)

        # 営業利益率
        if "operating_margin" in targets:
            op_margin = info.get("operatingMargins")
            if op_margin is not None:
                actuals["operating_margin"] = round(float(op_margin), 4)

        # ROE
        if "roe" in targets:
            roe = info.get("returnOnEquity")
            if roe is not None:
                actuals["roe"] = round(float(roe), 4)

        # EPS
        if "eps" in targets:
            eps = info.get("trailingEps")
            if eps is not None:
                actuals["eps"] = round(float(eps), 4)

    except Exception:
        pass

    return actuals


# ==============================================================
# BLOCK 3: 達成率計算
# ==============================================================

def _calc_achievement_rate(targets: dict, actuals: dict) -> Optional[float]:
    """
    targets と actuals の各指標の達成率平均を計算する。

    達成率 = actual / target（各指標）の平均
    ※ ROEなど「高いほど良い」指標: actual/target
    ※ 0以下の target は計算をスキップ

    Returns:
        achievement_rate（0.0〜1.5程度 / 1.0 = 100%達成）
    """
    rates = []
    for key in targets:
        target = targets.get(key)
        actual = actuals.get(key)
        if target is None or actual is None:
            continue
        try:
            t_f = float(target)
            a_f = float(actual)
            if abs(t_f) < 1e-6:   # ゼロ除算回避
                continue
            rate = a_f / t_f
            # クランプ: -0.5〜2.0（外れ値除去）
            rate = max(-0.5, min(2.0, rate))
            rates.append(rate)
        except (TypeError, ValueError):
            continue

    if not rates:
        return None
    return round(sum(rates) / len(rates), 4)


def _calc_annual_progress(
    targets: dict,
    actuals: dict,
    start_year: int,
    end_year: int,
    current_year: Optional[int] = None,
) -> dict:
    """
    中計期間中の年次按分達成率を計算する。

    按分目標 = 最終年度目標 × (経過年数 / 総期間年数)

    Returns:
        {
          "elapsed_years":   float,
          "total_years":     float,
          "progress_rate":   float,  # 経過 / 総期間
          "prorated_rate":   float,  # 按分達成率（actual / prorated_target）
          "on_track":        bool,   # 按分目標を超えているか
          "overtrack":       bool,   # 3年連続超過達成軌道かどうか（将来拡張）
        }
    """
    if current_year is None:
        current_year = date.today().year

    total_years   = max(1, end_year - start_year)
    elapsed_years = max(0, min(total_years, current_year - start_year))
    progress_rate = elapsed_years / total_years

    if progress_rate <= 0 or not actuals:
        return {
            "elapsed_years": elapsed_years,
            "total_years":   total_years,
            "progress_rate": progress_rate,
            "prorated_rate": None,
            "on_track":      None,
            "overtrack":     False,
        }

    # 按分目標を算出して達成率を計算
    prorated_targets = {
        k: float(v) * progress_rate
        for k, v in targets.items()
        if v is not None
    }
    prorated_rate = _calc_achievement_rate(prorated_targets, actuals)

    on_track = (prorated_rate >= OVERTRACK_THRESHOLD) if prorated_rate is not None else None

    return {
        "elapsed_years":  elapsed_years,
        "total_years":    total_years,
        "progress_rate":  round(progress_rate, 3),
        "prorated_rate":  prorated_rate,
        "on_track":       on_track,
        "overtrack":      False,   # 3年連続チェックは将来拡張
    }


# ==============================================================
# BLOCK 4: フラグ生成
# ==============================================================

def _generate_flags(plan: dict, avg_achievement_rate: Optional[float]) -> list:
    """中計状態に関するフラグリストを生成する。"""
    flags = []
    status = plan.get("status", "active")

    if status == "completed":
        rate = plan.get("achievement_rate")
        if rate is not None:
            if rate >= ACHIEVEMENT_HIGH:
                flags.append(f"✅ 前中計達成率{rate*100:.0f}%（高達成）")
            elif rate >= ACHIEVEMENT_MEDIUM:
                flags.append(f"📊 前中計達成率{rate*100:.0f}%（中達成）")
            else:
                flags.append(f"⚠️ 前中計達成率{rate*100:.0f}%（低達成）")

    elif status == "active":
        progress = plan.get("annual_progress", {})
        prorated_rate  = progress.get("prorated_rate")
        elapsed_years  = progress.get("elapsed_years", 0)
        total_years    = progress.get("total_years", 3)
        years_to_end   = total_years - elapsed_years

        if prorated_rate is not None:
            if progress.get("on_track") and prorated_rate >= OVERTRACK_THRESHOLD:
                flags.append("🚀 公約超過達成軌道（按分目標超過）")
            elif years_to_end <= 2 and prorated_rate < ACHIEVEMENT_RISK:
                flags.append(f"⚠️ 中計未達リスク（按分達成率{prorated_rate*100:.0f}%）")

    if avg_achievement_rate is not None:
        if avg_achievement_rate >= ACHIEVEMENT_HIGH:
            flags.append(f"🌟 過去中計平均達成率{avg_achievement_rate*100:.0f}%")

    return flags


# ==============================================================
# BLOCK 5: 1銘柄の中計追跡
# ==============================================================

def track_midterm_plan(
    ticker:     str,
    edinet_db:  dict,
    cfg = None,
) -> Optional[dict]:
    """
    1銘柄の中期経営計画達成状況を追跡・更新する。

    Returns:
        midterm entry dict、またはデータなし時は None
    """
    # EDINETから中計データを取得
    midterm_data = _get_midterm_from_edinet(ticker, edinet_db)
    if not midterm_data:
        return None   # 中計データなし

    today_str = date.today().strftime("%Y-%m-%d")

    plan_name   = midterm_data.get("plan_name", "中期経営計画")
    start_year  = midterm_data.get("start_year")
    end_year    = midterm_data.get("end_year")
    targets     = midterm_data.get("targets", {})

    if not targets or start_year is None or end_year is None:
        return None

    # yfinance から実績値を取得
    actuals = _get_actuals_from_yfinance(ticker, midterm_data)
    time.sleep(0.3)   # レート制限対策

    # 達成率計算
    current_year = date.today().year
    status = "completed" if current_year > end_year else "active"

    achievement_rate = _calc_achievement_rate(targets, actuals) if actuals else None

    # 年次進捗（現在進行中の場合のみ）
    annual_progress = {}
    if status == "active":
        annual_progress = _calc_annual_progress(
            targets, actuals, start_year, end_year, current_year
        )

    plan_entry = {
        "plan_name":        plan_name,
        "start_year":       start_year,
        "end_year":         end_year,
        "targets":          targets,
        "actuals":          actuals,
        "achievement_rate": achievement_rate,
        "annual_progress":  annual_progress,
        "status":           status,
        "updated_at":       today_str,
    }

    return plan_entry


# ==============================================================
# BLOCK 6: バッチ処理
# ==============================================================

def run_midterm_plan_tracker(
    cfg,
    edinet_db:       dict,
    tickers:         list,
    tdnet_result:    dict = None,
) -> dict:
    """
    複数銘柄の中期経営計画達成率を一括追跡・更新する。

    stock_analyzer_main.py から screener 直後に呼び出す:
        _mt = _load_local_module("midterm_plan_tracker", ...)
        mt_result = _mt.run_midterm_plan_tracker(
            cfg, edinet_db, tickers, tdnet_result=results.get("tdnet"))
        results["midterm_tracker"] = mt_result

    Args:
        cfg         : config モジュール
        edinet_db   : edinet_rotation_manager.load_db(cfg)
        tickers     : 分析対象 ticker リスト
        tdnet_result: results["tdnet"]（中計発表イベント検知用）

    Returns:
        {
          "results":          {ticker: plan_entry},
          "overtrack_list":   [ticker, ...],  # 公約超過達成軌道
          "risk_list":        [ticker, ...],  # 中計未達リスク
          "new_plan_alert":   [ticker, ...],  # 新中計発表（高信頼性）
          "total":            int,
          "timestamp":        str,
        }
    """
    print("\n  📋 中期経営計画追跡 開始...")
    t0 = datetime.now()

    db = load_midterm_db(cfg)
    today_str = date.today().strftime("%Y-%m-%d")

    overtrack_list  = []
    risk_list       = []
    new_plan_alerts = []
    results_map     = {}

    for ticker in tickers:
        try:
            plan_entry = track_midterm_plan(ticker, edinet_db, cfg)
            if plan_entry is None:
                continue

            # DB の既存データと統合
            existing = db.get(ticker, {
                "plans":                [],
                "avg_achievement_rate": None,
            })

            # plans リストを更新（同一 plan_name は上書き）
            plans = existing.get("plans", [])
            plan_name = plan_entry.get("plan_name", "")
            plans = [p for p in plans if p.get("plan_name") != plan_name]
            plans.append(plan_entry)
            plans.sort(key=lambda x: x.get("start_year", 0))

            # avg_achievement_rate を完了中計から再計算
            completed_rates = [
                p["achievement_rate"] for p in plans
                if p.get("status") == "completed" and p.get("achievement_rate") is not None
            ]
            avg_rate = (
                round(sum(completed_rates) / len(completed_rates), 4)
                if completed_rates else None
            )

            # 現在の中計を current_plan に設定
            current_plan = next(
                (p for p in reversed(plans) if p.get("status") == "active"),
                plans[-1] if plans else {}
            )

            # フラグ生成
            flags = _generate_flags(current_plan, avg_rate)

            # フラグからリスト振り分け
            if any("公約超過達成軌道" in f for f in flags):
                overtrack_list.append(ticker)
            if any("中計未達リスク" in f for f in flags):
                risk_list.append(ticker)

            updated_entry = {
                "ticker":               ticker,
                "plans":                plans,
                "current_plan":         current_plan,
                "avg_achievement_rate": avg_rate,
                "flags":                flags,
                "updated_at":           today_str,
            }
            db[ticker]          = updated_entry
            results_map[ticker] = updated_entry

        except Exception as e:
            print(f"    ⚠️  {ticker} 中計追跡エラー: {str(e)[:60]}")

    # ── TDnet 新中計発表イベントの処理 ──────────────────────
    if tdnet_result and isinstance(tdnet_result, dict):
        # TDnet の事業計画発表を検知（MGMT_CHANGE に含まれる場合もある）
        for ev in tdnet_result.get("events", []):
            title = ev.get("title", "")
            if "中期経営計画" in title or "中計" in title or "長期ビジョン" in title:
                tk = ev.get("ticker", "")
                if tk:
                    entry = db.get(tk, {})
                    avg_rate = entry.get("avg_achievement_rate")
                    if avg_rate is not None and avg_rate >= ACHIEVEMENT_HIGH:
                        new_plan_alerts.append({
                            "ticker":          tk,
                            "event_title":     title,
                            "avg_achievement": avg_rate,
                            "alert_type":      "high_reliability",
                            "note": (
                                f"過去中計平均達成率{avg_rate*100:.0f}%の経営者が新中計を発表。"
                                "目標値の信頼性が高い。"
                            ),
                        })
                    elif avg_rate is not None and avg_rate < ACHIEVEMENT_MEDIUM:
                        new_plan_alerts.append({
                            "ticker":          tk,
                            "event_title":     title,
                            "avg_achievement": avg_rate,
                            "alert_type":      "low_reliability",
                            "note": (
                                f"過去中計平均達成率{avg_rate*100:.0f}%の経営者の新中計。"
                                "目標値を割り引いて評価を推奨。"
                            ),
                        })

    # DB保存
    save_midterm_db(cfg, db)

    elapsed = (datetime.now() - t0).seconds
    print(
        f"    ✅ 中計追跡完了（{elapsed}秒）: "
        f"{len(results_map)}銘柄 "
        f"| 超過達成軌道{len(overtrack_list)} "
        f"| 未達リスク{len(risk_list)}"
        + (f" | 新中計アラート{len(new_plan_alerts)}" if new_plan_alerts else "")
    )

    return {
        "results":          results_map,
        "overtrack_list":   overtrack_list,
        "risk_list":        risk_list,
        "new_plan_alerts":  new_plan_alerts,
        "total":            len(results_map),
        "timestamp":        t0.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 7: weekly_report 向けフォーマット
# ==============================================================

def format_midterm_section(results_map: dict) -> list:
    """週次レポート向け中計進捗セクションを生成する。"""
    if not results_map:
        return []

    lines = ["## 📋 中期経営計画 達成状況（I-5）", ""]

    # 超過達成軌道
    overtrack = [
        (tk, e) for tk, e in results_map.items()
        if any("公約超過達成軌道" in f for f in e.get("flags", []))
    ]
    if overtrack:
        lines.append("### 🚀 公約超過達成軌道（按分目標超過）")
        lines.append("")
        for tk, e in overtrack[:5]:
            cp = e.get("current_plan", {})
            prog = cp.get("annual_progress", {})
            pr = prog.get("prorated_rate")
            lines.append(
                f"- **{tk}**: {cp.get('plan_name','')} "
                f"按分達成率{pr*100:.0f}%" if pr else f"- **{tk}**"
            )
        lines.append("")

    # 未達リスク
    risk = [
        (tk, e) for tk, e in results_map.items()
        if any("中計未達リスク" in f for f in e.get("flags", []))
    ]
    if risk:
        lines.append("### ⚠️ 中計未達リスク")
        lines.append("")
        for tk, e in risk[:5]:
            cp  = e.get("current_plan", {})
            prog = cp.get("annual_progress", {})
            pr   = prog.get("prorated_rate")
            lines.append(
                f"- **{tk}**: {cp.get('plan_name','')} "
                f"按分達成率{pr*100:.0f}%（残{prog.get('total_years',0)-prog.get('elapsed_years',0):.0f}年）"
                if pr else f"- **{tk}**"
            )
        lines.append("")

    return lines
