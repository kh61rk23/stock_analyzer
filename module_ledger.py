# ==============================================================
# module_ledger.py  v1.0  (Wave OBS-1)
# モジュール実行台帳 — サイレント失敗の可視化
# ==============================================================
# 【役割】
#   RunLogger（stock_analyzer_main.py）が蓄積するフラットな実行ログを
#   「日次台帳」に集約し、以下を可視化する:
#     ① 当日の全モジュール成否（success / error / skipped を区別）
#     ② モジュール別の連続失敗日数（N日以上で Gmail アラート）
#     ③ モジュール別の最終成功日（=データ鮮度。投資部デビルの検証材料）
#     ④ DAILY_MODULES に登録されているのに記録が無いモジュール
#        （配線漏れ・ログ未配線の検出 — news_pipeline 配線漏れの再発防止）
#
# 【設計の背景】
#   2026-06 の全数バグ掃討で「数ヶ月間サイレントに死んでいた機能」が
#   多数発見された（news配線漏れ・推論エンジン常時失敗・幻のAPI等）。
#   本モジュールはその再発を「翌日に検知」するための観測層である。
#
# 【出力】
#   LOG_DIR/module_ledger_YYYY-MM-DD.json   日次台帳（履歴）
#   LOG_DIR/module_ledger_latest.json       最新台帳（weekly_report / 投資部が参照）
#
# 【呼び出し】
#   stock_analyzer_main.run_all_tasks() の終端:
#     summary = finalize_daily_ledger(cfg, logger._logs)
#     results["module_ledger"] = summary
#   weekly_report_generator:
#     lines = format_ledger_section(cfg)
#
# 【設定】config.py BLOCK 38
#   ENABLE_MODULE_LEDGER      : 有効フラグ
#   LEDGER_CONSEC_FAIL_DAYS   : 連続失敗アラート閾値（日）
#   LEDGER_HISTORY_DAYS       : 連続失敗の遡及日数
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_LATEST_FILENAME = "module_ledger_latest.json"

# ── mode-key → 実際に logger.add() で使われる記録キー（2026-07-25 B-3）──
# DAILY_MODULES の mode-key と記録キーが一致せず、かつ接頭辞派生でもない
# ケースは、接頭辞ルールでは救えず「記録なし」に誤検出されていた。
#   key_health → api_key_health（記録側が前に付く＝接頭辞が逆）
#   screener   → stock_screener（同上）
# 新しいステージを追加して mode-key と記録キーがズレる場合はここに追記する。
# cfg.LEDGER_MODE_KEY_ALIASES を定義すればマージ上書きできる。
_DEFAULT_MODE_KEY_ALIASES: dict = {
    "key_health":       ["api_key_health"],
    "screener":         ["stock_screener"],
    "excel_export":     ["excel_export", "correlation_heatmap"],
    "ml_meta":          ["ml_meta_dataset", "ml_meta_correction"],
    "us_sector_bridge": ["us_sector_bridge"],
    # mode-key "size_tier" で走るのは size_decay_manager / cross_size_signal で、
    # どちらも "size_tier_" 接頭辞を持たないため接頭辞ルールで救えず、
    # 正常実行されているのに毎回「記録なし（配線漏れ疑い）」に出ていた。
    # （週次の "size_tier_classifier" は別の登録mode-keyなので含めない）
    "size_tier":        ["size_decay_manager", "cross_size_signal"],
}


def _mode_key_aliases(cfg) -> dict:
    """mode-key → 記録キー候補。cfg 側の定義で上書きマージする。"""
    aliases = dict(_DEFAULT_MODE_KEY_ALIASES)
    for k, v in (getattr(cfg, "LEDGER_MODE_KEY_ALIASES", None) or {}).items():
        aliases[k] = list(v)
    return aliases


def _log_dir(cfg) -> Path:
    d = Path(getattr(cfg, "LOG_DIR", "stock_data/logs"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _daily_path(cfg, date_str: str) -> Path:
    return _log_dir(cfg) / f"module_ledger_{date_str}.json"


def _load_daily(cfg, date_str: str) -> Optional[dict]:
    p = _daily_path(cfg, date_str)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ==============================================================
# BLOCK 1: 日次台帳の構築
# ==============================================================

def build_daily_ledger(cfg, logs: list, date_str: str = None) -> dict:
    """
    RunLogger のフラットログから当日分を抽出し、モジュール別台帳を作る。
    同一モジュールが複数回記録された場合は最後の記録を採用する。
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    modules: dict = {}
    for e in logs or []:
        if not isinstance(e, dict):
            continue
        if not str(e.get("datetime", "")).startswith(date_str):
            continue
        name = str(e.get("module", "")) or "unknown"
        modules[name] = {
            "status":   str(e.get("status", "")),
            "note":     str(e.get("note", ""))[:200],
            "datetime": e.get("datetime", ""),
        }

    # warn / alert は success でも error でもないため、旧集計ではどこにも
    # 計上されず「失敗0」と表示されていた。成果ゼロで alert を出している
    # モジュール（2026-08-27 の edinetdb_ingest 0/11・fudosan_ingest 0/45）が
    # 健全に見えてしまうため、要確認として明示的に数える。
    stats = {
        "success": sum(1 for m in modules.values() if m["status"] == "success"),
        "error":   sum(1 for m in modules.values() if m["status"] == "error"),
        "skipped": sum(1 for m in modules.values() if m["status"] == "skipped"),
        "warn":    sum(1 for m in modules.values()
                       if m["status"] in ("warn", "alert")),
        "total":   len(modules),
    }

    # DAILY_MODULES に登録されているのに当日記録が無いモジュール
    # （ENABLE=False の意図的スキップはログに "skipped" が残る想定のため、
    #   ここに出るものは「配線漏れ or ログ未配線」の疑い）
    #
    # ★ DAILY_MODULES は _in_mode() 用の mode-key（例 "gov" / "sector" / "tdnet"）
    #   だが、各モジュールが logger.add() で記録するキーは実装名（"gov_pipeline" /
    #   "sector_agent" / "tdnet_monitor" 等）で、両者が食い違うため、実際は実行・記録
    #   されているモジュールまで「記録なし」に誤検出されていた（gov/sector/tdnet/
    #   imf/anomaly 等）。mode-key の接頭辞派生（mode-key + "_..."）で記録されて
    #   いれば「記録済み」とみなす。ただし記録キー自体が別の登録 mode-key（例
    #   "policy_radar"）の場合は、その別モジュールの記録なので接頭辞一致から除外する
    #   （"policy" を "policy_radar" の記録で誤って満たさないため）。
    registered = list(getattr(cfg, "DAILY_MODULES", []) or [])
    registered_set = set(registered)
    aliases = _mode_key_aliases(cfg)

    def _is_recorded(mode_key: str) -> bool:
        if mode_key in modules:
            return True
        # ① 明示エイリアス（接頭辞ルールで救えないズレ・2026-07-25 B-3）
        for alias in aliases.get(mode_key, ()):
            if alias in modules:
                return True
        prefix = mode_key + "_"
        for rec in modules:
            if rec in registered_set:
                continue  # 別の登録モジュールの記録は流用しない
            if rec.startswith(prefix):
                return True
        return False

    unrecorded = [m for m in registered if not _is_recorded(m)]

    return {
        "date":       date_str,
        "modules":    modules,
        "stats":      stats,
        "unrecorded": unrecorded,
    }


def _calc_consecutive_failures(cfg, today_ledger: dict) -> dict:
    """
    過去の日次台帳を遡り、モジュール別の連続失敗日数を計算する。
    「今日 error で、昨日以前も error が連続している」日数を返す。
    台帳が存在しない日（実行なし）は連続にカウントせずスキップする。
    """
    history_days = int(getattr(cfg, "LEDGER_HISTORY_DAYS", 14))
    today = datetime.strptime(today_ledger["date"], "%Y-%m-%d")

    failing = {
        name: 1 for name, m in today_ledger["modules"].items()
        if m["status"] == "error"
    }
    if not failing:
        return {}

    active = set(failing.keys())
    for i in range(1, history_days + 1):
        if not active:
            break
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        past = _load_daily(cfg, d)
        if past is None:
            continue  # 実行が無かった日は判定対象外
        for name in list(active):
            st = past.get("modules", {}).get(name, {}).get("status")
            if st == "error":
                failing[name] += 1
            else:
                active.discard(name)  # 成功/スキップ/記録なしで連続打ち切り
    return failing


def _calc_last_success(cfg, today_ledger: dict) -> dict:
    """
    モジュール別の最終成功日を返す（=データ鮮度。投資部デビルの検証材料）。
    当日成功はそのまま当日日付。それ以外は過去台帳を遡って探す。
    """
    history_days = int(getattr(cfg, "LEDGER_HISTORY_DAYS", 14))
    today = datetime.strptime(today_ledger["date"], "%Y-%m-%d")
    last_success: dict = {}

    names = set(today_ledger["modules"].keys())
    for name, m in today_ledger["modules"].items():
        if m["status"] == "success":
            last_success[name] = today_ledger["date"]

    pending = names - set(last_success.keys())
    for i in range(1, history_days + 1):
        if not pending:
            break
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        past = _load_daily(cfg, d)
        if past is None:
            continue
        for name in list(pending):
            if past.get("modules", {}).get(name, {}).get("status") == "success":
                last_success[name] = d
                pending.discard(name)
    for name in pending:
        last_success[name] = None  # 遡及範囲内に成功なし
    return last_success


# ==============================================================
# BLOCK 2: 確定・永続化・アラート
# ==============================================================

def finalize_daily_ledger(cfg, logs: list, send_alert: bool = True) -> dict:
    """
    日次台帳を確定して保存し、サマリーを返す。
    連続失敗が閾値以上のモジュールがあれば Gmail アラートを送る
    （ENABLE_ALERT と Gmail 設定が有効な場合のみ・失敗しても本体に影響なし）。
    """
    ledger = build_daily_ledger(cfg, logs)
    ledger["consecutive_failures"] = _calc_consecutive_failures(cfg, ledger)
    ledger["last_success"]         = _calc_last_success(cfg, ledger)

    threshold = int(getattr(cfg, "LEDGER_CONSEC_FAIL_DAYS", 3))
    ledger["alerts"] = [
        {
            "module": name,
            "days":   days,
            "note":   ledger["modules"].get(name, {}).get("note", ""),
        }
        for name, days in sorted(
            ledger["consecutive_failures"].items(),
            key=lambda x: -x[1],
        )
        if days >= threshold
    ]

    # 保存（日次 + latest）
    try:
        payload = json.dumps(ledger, ensure_ascii=False, indent=2)
        _daily_path(cfg, ledger["date"]).write_text(payload, encoding="utf-8")
        (_log_dir(cfg) / _LATEST_FILENAME).write_text(payload, encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  [ledger] 保存失敗: {str(e)[:60]}")

    # コンソールサマリー
    s = ledger["stats"]
    print(
        f"\n  📒 実行台帳: 成功{s['success']} / 失敗{s['error']} / "
        f"要確認{s.get('warn', 0)} / スキップ{s['skipped']}"
        + (f" / 記録なし{len(ledger['unrecorded'])}" if ledger["unrecorded"] else "")
    )
    _warned = [n for n, m in ledger.get("modules", {}).items()
               if m.get("status") in ("warn", "alert")]
    if _warned:
        print(f"    ⚠️  要確認: {', '.join(sorted(_warned)[:8])}")
    for a in ledger["alerts"]:
        print(f"    🚨 連続失敗 {a['days']}日: {a['module']} ({a['note'][:50]})")
    if ledger["unrecorded"]:
        print(f"    👻 記録なし（配線漏れ疑い）: {', '.join(ledger['unrecorded'][:8])}")

    if send_alert and ledger["alerts"]:
        _send_failure_alert(cfg, ledger)

    return ledger


def _send_failure_alert(cfg, ledger: dict) -> None:
    """連続失敗アラートを Gmail で送信する（best-effort）。"""
    try:
        if not getattr(cfg, "ENABLE_ALERT", False):
            return
        frm = getattr(cfg, "ALERT_GMAIL_FROM", "")
        pwd = getattr(cfg, "ALERT_GMAIL_PASSWORD", "")
        to  = getattr(cfg, "ALERT_GMAIL_TO", "")
        if not (frm and pwd and to):
            return
        from alert_notifier import send_gmail
        rows = "".join(
            f"<tr><td>{a['module']}</td><td>{a['days']}日</td>"
            f"<td>{a['note'][:80]}</td></tr>"
            for a in ledger["alerts"]
        )
        html = (
            f"<p>以下のモジュールが連続失敗しています（{ledger['date']}時点）。</p>"
            f"<table border='1' cellpadding='4'>"
            f"<tr><th>モジュール</th><th>連続失敗</th><th>直近エラー</th></tr>"
            f"{rows}</table>"
            f"<p>詳細: stock_data/logs/module_ledger_latest.json</p>"
        )
        send_gmail(frm, pwd, to,
                   f"🚨【実行台帳】モジュール連続失敗 {len(ledger['alerts'])}件",
                   html)
        print(f"    📧 連続失敗アラート送信: {len(ledger['alerts'])}件")
    except Exception as e:
        print(f"    ⚠️  [ledger] アラート送信失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 3: 参照API（weekly_report / 投資部 packet exporter 用）
# ==============================================================

def load_latest_ledger(cfg) -> Optional[dict]:
    """最新台帳を読み込む。"""
    p = _log_dir(cfg) / _LATEST_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def format_ledger_section(cfg) -> list:
    """週次レポート用のモジュール健全性セクション（Markdown行リスト）。"""
    ledger = load_latest_ledger(cfg)
    if not ledger:
        return []
    s = ledger.get("stats", {})
    lines = ["## 📒 モジュール実行台帳（システム健全性）", ""]
    # 2026-09-14: 週次レポートは当日の台帳確定（実行の最後）より前に作られる
    # ため、ここに出るのは常に「前回実行」の台帳。日付だけだと当日分に見え、
    # 9/14 のメールでは「2026-09-12: 成功66」と古い数字が当日分のように出ていた。
    lines.append(
        f"- 直近の確定台帳（{ledger.get('date', '')} 実行分）: "
        f"成功 **{s.get('success', 0)}** / 失敗 **{s.get('error', 0)}** / "
        f"スキップ {s.get('skipped', 0)}"
    )
    alerts = ledger.get("alerts", [])
    if alerts:
        lines.append("")
        lines.append("| 連続失敗モジュール | 日数 | 直近エラー |")
        lines.append("|---|---|---|")
        for a in alerts[:10]:
            lines.append(f"| {a['module']} | {a['days']}日 | {a['note'][:60]} |")
    errors = [
        (n, m) for n, m in ledger.get("modules", {}).items()
        if m.get("status") == "error"
    ]
    if errors and not alerts:
        lines.append("")
        lines.append("当日失敗: " + ", ".join(n for n, _ in errors[:10]))
    if ledger.get("unrecorded"):
        lines.append("")
        lines.append(
            "👻 記録なし（配線漏れ疑い）: "
            + ", ".join(ledger["unrecorded"][:10])
        )
    if not alerts and not errors and not ledger.get("unrecorded"):
        lines.append("- ✅ 全モジュール正常")
    lines.append("")
    return lines


def get_module_freshness(cfg) -> dict:
    """
    モジュール別の最終成功日を返す（投資部デビルのデータ鮮度検証用）。
    Returns: {module: "YYYY-MM-DD" | None}
    """
    ledger = load_latest_ledger(cfg)
    if not ledger:
        return {}
    return dict(ledger.get("last_success", {}))
