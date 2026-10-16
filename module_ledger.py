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
# 連続失敗として数える状態。alert は別ワークフロー（price_universe 等）が「取得率が下限割れ」で付ける失敗。
_FAIL_STATUSES = ("error", "alert")

# ── mode-key → 実際に logger.add() で使われる記録キー ──
# DAILY_MODULES の mode-key と記録キーが一致せず、かつ接頭辞派生でもない
# ケースは、接頭辞ルールでは救えず「記録なし」に誤検出されるためここで対応づける。
#   key_health → api_key_health（記録側が前に付く＝接頭辞が逆）
#   screener   → stock_screener（同上）
# 新しいステージを追加して mode-key と記録キーがズレる場合はここに追記する。
# cfg.LEDGER_MODE_KEY_ALIASES を定義すればマージ上書きできる。
_DEFAULT_MODE_KEY_ALIASES: dict = {
    "key_health":       ["api_key_health"],
    "screener":         ["stock_screener"],
    "excel_export":     ["correlation_heatmap"],   # Excel出力は削除済み
    "ml_meta":          ["ml_meta_dataset", "ml_meta_correction"],
    "us_sector_bridge": ["us_sector_bridge"],
    # mode-key "size_tier" で走るのは size_decay_manager / cross_size_signal で、
    # どちらも "size_tier_" 接頭辞を持たないため接頭辞ルールで救えない。
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

def _external_path(cfg, date_str: str) -> Path:
    return _log_dir(cfg) / f"module_ledger_external_{date_str}.json"


def record_external(cfg, name: str, status: str, note: str = "",
                    date_str: str = None) -> None:
    """日次本体とは別の実行（price_universe など）の結果を台帳へ記録する。

    日次台帳は本体の終端で RunLogger のログから作り直して上書きされるため、
    別ワークフローの記録は専用ファイルに残し、finalize_daily_ledger が取り込む。
    その日の日次台帳が既にあれば、そちらにも直接反映する（本体より後に走る場合）。
    """
    now = datetime.now()
    date_str = date_str or now.strftime("%Y-%m-%d")
    entry = {"status": str(status), "note": str(note)[:200],
             "datetime": now.strftime("%Y-%m-%d %H:%M:%S")}
    ext_path = _external_path(cfg, date_str)
    try:
        ext = json.loads(ext_path.read_text(encoding="utf-8")) if ext_path.exists() else {}
    except Exception:
        ext = {}
    ext[name] = entry
    ext_path.write_text(json.dumps(ext, ensure_ascii=False, indent=2), encoding="utf-8")

    daily = _load_daily(cfg, date_str)
    if daily is not None:
        daily.setdefault("modules", {})[name] = entry
        payload = json.dumps(daily, ensure_ascii=False, indent=2)
        _daily_path(cfg, date_str).write_text(payload, encoding="utf-8")
        latest = _log_dir(cfg) / _LATEST_FILENAME
        if latest.exists():
            try:
                if json.loads(latest.read_text(encoding="utf-8")).get("date") == date_str:
                    latest.write_text(payload, encoding="utf-8")
            except Exception:
                pass


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

    # 別ワークフローで走ったモジュール（record_external）の当日分を取り込む
    try:
        ext = json.loads(_external_path(cfg, date_str).read_text(encoding="utf-8"))
        for name, entry in ext.items():
            modules.setdefault(name, entry)
    except Exception:
        pass

    # warn / alert は success でも error でもないため、数えないと「失敗0」に見え、
    # 成果ゼロで alert を出しているモジュールが健全に見えてしまう。
    # 要確認として明示的に数える。
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
    # DAILY_MODULES は _in_mode() 用の mode-key（例 "gov" / "sector" / "tdnet"）
    #   だが、各モジュールが logger.add() で記録するキーは実装名（"gov_pipeline" /
    #   "sector_agent" / "tdnet_monitor" 等）で食い違う。mode-key の接頭辞派生（mode-key + "_..."）で記録されて
    #   いれば「記録済み」とみなす。ただし記録キー自体が別の登録 mode-key（例
    #   "policy_radar"）の場合は、その別モジュールの記録なので接頭辞一致から除外する
    #   （"policy" を "policy_radar" の記録で誤って満たさないため）。
    registered = list(getattr(cfg, "DAILY_MODULES", []) or [])
    registered_set = set(registered)
    aliases = _mode_key_aliases(cfg)

    def _is_recorded(mode_key: str) -> bool:
        if mode_key in modules:
            return True
        # ① 明示エイリアス（接頭辞ルールで救えないズレ）
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
        if m["status"] in _FAIL_STATUSES
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
            if st in _FAIL_STATUSES:
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

def _calc_unrecorded_streak(cfg, today_ledger: dict) -> dict:
    """当日「記録なし」のモジュールが、何日続けて記録なしか（実行の無かった日は数えない）。"""
    history_days = int(getattr(cfg, "LEDGER_HISTORY_DAYS", 14))
    today = datetime.strptime(today_ledger["date"], "%Y-%m-%d")
    streak = {name: 1 for name in today_ledger.get("unrecorded") or []}
    active = set(streak)
    for i in range(1, history_days + 1):
        if not active:
            break
        past = _load_daily(cfg, (today - timedelta(days=i)).strftime("%Y-%m-%d"))
        if past is None:
            continue
        missing = set(past.get("unrecorded") or [])
        for name in list(active):
            if name in missing:
                streak[name] += 1
            else:
                active.discard(name)
    return streak


def _calc_external_stale(cfg, today_ledger: dict) -> dict:
    """別ワークフロー（record_external）で毎日入るはずの記録が、何日来ていないか。
    LEDGER_EXTERNAL_EXPECT={名前: 許容日数} を超えたものだけ返す（ワークフローの停止・無効化に気づくため）。"""
    expect = dict(getattr(cfg, "LEDGER_EXTERNAL_EXPECT", {}) or {})
    if not expect:
        return {}
    history_days = max(int(getattr(cfg, "LEDGER_HISTORY_DAYS", 14)), max(expect.values()) + 1)
    today = datetime.strptime(today_ledger["date"], "%Y-%m-%d")
    out = {}
    for name, limit in expect.items():
        last = None
        for i in range(0, history_days + 1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            day = today_ledger if i == 0 else (_load_daily(cfg, d) or {})
            ext = {}
            try:
                p = _external_path(cfg, d)
                ext = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            except Exception:
                ext = {}
            if name in (day.get("modules") or {}) or name in ext:
                last = i
                break
        if last is None or last > int(limit):
            out[name] = last if last is not None else history_days
    return out


def finalize_daily_ledger(cfg, logs: list, send_alert: bool = True) -> dict:
    """
    日次台帳を確定して保存し、サマリーを返す。
    連続失敗が閾値以上のモジュールがあれば Gmail アラートを送る
    （ENABLE_ALERT と Gmail 設定が有効な場合のみ・失敗しても本体に影響なし）。
    """
    ledger = build_daily_ledger(cfg, logs)
    ledger["consecutive_failures"] = _calc_consecutive_failures(cfg, ledger)
    ledger["last_success"]         = _calc_last_success(cfg, ledger)

    ledger["unrecorded_streak"]    = _calc_unrecorded_streak(cfg, ledger)
    ledger["external_stale"]       = _calc_external_stale(cfg, ledger)

    threshold = int(getattr(cfg, "LEDGER_CONSEC_FAIL_DAYS", 3))
    unrec_days = int(getattr(cfg, "LEDGER_UNRECORDED_DAYS", 2))
    ledger["alerts"] = [
        {
            "kind":   "consecutive_failure",
            "module": name,
            "days":   days,
            "note":   ledger["modules"].get(name, {}).get("note", ""),
        }
        for name, days in sorted(
            ledger["consecutive_failures"].items(),
            key=lambda x: -x[1],
        )
        if days >= threshold
    ] + [
        # 1日だけの記録なしはモードの切り替わり等でも起きるため、続いたときだけ通知する
        {"kind": "unrecorded", "module": name, "days": days,
         "note": "DAILY_MODULES に登録済みなのに実行記録が無い（配線漏れ・ログ未配線の疑い）"}
        for name, days in sorted(ledger["unrecorded_streak"].items(), key=lambda x: -x[1])
        if days >= unrec_days
    ] + [
        {"kind": "external_stale", "module": name, "days": days,
         "note": "別ワークフローの記録が来ていない（ワークフローの停止・無効化・失敗の疑い）"}
        for name, days in sorted(ledger["external_stale"].items(), key=lambda x: -x[1])
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
    _KIND = {"consecutive_failure": "連続失敗", "unrecorded": "記録なし", "external_stale": "記録が来ない"}
    for a in ledger["alerts"]:
        print(f"    🚨 {_KIND.get(a.get('kind'), '')} {a['days']}日: {a['module']} ({a['note'][:50]})")
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
        kind_ja = {"consecutive_failure": "連続失敗", "unrecorded": "記録なし（配線漏れ疑い）",
                   "external_stale": "別ワークフローの記録が来ない"}
        rows = "".join(
            f"<tr><td>{kind_ja.get(a.get('kind'), '連続失敗')}</td><td>{a['module']}</td><td>{a['days']}日</td>"
            f"<td>{a['note'][:80]}</td></tr>"
            for a in ledger["alerts"]
        )
        html = (
            f"<p>実行台帳で次の異常を見つけました（{ledger['date']}時点）。</p>"
            f"<table border='1' cellpadding='4'>"
            f"<tr><th>種類</th><th>モジュール</th><th>日数</th><th>内容</th></tr>"
            f"{rows}</table>"
            f"<p>詳細: stock_data/logs/module_ledger_latest.json</p>"
        )
        send_gmail(frm, pwd, to,
                   f"🚨【実行台帳】異常 {len(ledger['alerts'])}件（連続失敗・記録なし・別ワークフロー）",
                   html)
        print(f"    📧 実行台帳の異常アラート送信: {len(ledger['alerts'])}件")
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
    # 週次レポートは当日の台帳確定（実行の最後）より前に作られる
    # ため、ここに出るのは常に「前回実行」の台帳。日付だけだと当日分に見えるので
    # 「実行分」と明示する。
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
