# ==============================================================
# ops_health.py
# 日次本体（run_all_tasks）の外側で止まっても気づけないものを、日次メールに出す
# ==============================================================
# 【見るもの】（どれも Actions 上は緑のまま無音で止まりうる）
#   ① Actions の当月使用分数（actions_usage.py が LOG_DIR/actions_usage.json に書く）
#   ② Mac の同期（obsidian_sync.py all が LOG_DIR/mac_sync_status.json を push する）
#      承認の BQ 反映・部則のクラウド同期・vault ミラーは Mac が止まると全部止まる
#   ③ 週次ループ（weekly_feedback_runner の結果。②の中に weekly_loop として載る）
#   ④ 保有銘柄の鮮度（DATA_DIR/portfolio_summary.json の as_of＝家計簿締めの日）
#   ⑤ クラウド審議 routine（deliberation_queue.get_routine_health の BQ 集計）
#      「失敗」だけでなく、起動しない・途中で止まる（claimed のまま）も拾う
#
# 判定は純関数（*_issues）。読み込みは collect_file_issues にまとめる。
# 戻り値の issue は {"severity": "high"|"medium", "text": str}。
# ==============================================================

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

MAC_STATUS_FILE = "mac_sync_status.json"
ACTIONS_USAGE_FILE = "actions_usage.json"


def _cfg(cfg, name, default):
    return getattr(cfg, name, default) if cfg is not None else default


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s)[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _issue(sev: str, text: str) -> dict:
    return {"severity": sev, "text": text}


# ── ① Actions ────────────────────────────────────────────────

def actions_usage_issues(usage: dict | None, limit_min: int, warn_ratio: float,
                         today: date) -> list:
    """当月の使用分数が枠の warn_ratio を超えたら警告。月末までの見込み（日割り）も出す。"""
    if not usage or str(usage.get("month", "")) != today.strftime("%Y-%m"):
        return []
    used = int(usage.get("minutes") or 0)
    if limit_min <= 0:
        return []
    days_in_month = ((today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    projected = int(round(used / max(today.day, 1) * days_in_month))
    if used < limit_min * warn_ratio and projected < limit_min:
        return []
    sev = "high" if used >= limit_min * 0.95 or projected >= limit_min else "medium"
    top = ", ".join(f"{k} {v}分" for k, v in list((usage.get("by_workflow") or {}).items())[:3])
    return [_issue(sev, f"Actions 使用量 {used:,}/{limit_min:,}分（{used / limit_min:.0%}）・"
                        f"月末見込み {projected:,}分。枠を超えると月末まで全 workflow が止まる（内訳: {top}）")]


# ── ②③ Mac 同期・週次ループ ──────────────────────────────────

def mac_sync_issues(status: dict | None, now: datetime, stale_hours: float,
                    weekly_stale_days: float) -> list:
    if not status:
        return [_issue("medium", "Mac の同期状況（logs/mac_sync_status.json）が届いていない。"
                                 "承認の BQ 反映・部則のクラウド同期・vault ミラーが止まっていても分からない")]
    out = []
    last_ok = _parse_dt(status.get("last_success_at"))
    if last_ok is None or (now - last_ok).total_seconds() > stale_hours * 3600:
        age = f"{(now - last_ok).total_seconds() / 3600:.0f}時間前" if last_ok else "記録なし"
        failed = [k for k, v in (status.get("steps") or {}).items() if v is False]
        out.append(_issue("high", f"Mac の同期が最後に成功したのは {age}（許容 {stale_hours:.0f}時間）。"
                                  + (f"直近の失敗: {', '.join(failed)}。" if failed else "")
                                  + "Mac のスリープ・オフライン・電源を確認"))
    wl = status.get("weekly_loop") or {}
    wl_ok = _parse_dt(wl.get("last_success"))
    if wl and (wl_ok is None or (now - wl_ok).days > weekly_stale_days):
        out.append(_issue("medium", f"週次ループ（journal→feedback→デビル校正）の最終成功: "
                                    f"{wl.get('last_success') or '記録なし'}（許容 {weekly_stale_days:.0f}日）"))
    return out


# ── ④ 保有銘柄 ────────────────────────────────────────────────

def holdings_issues(summary: dict | None, today: date, stale_days: int) -> list:
    if not summary or not summary.get("as_of"):
        return []
    try:
        as_of = date.fromisoformat(str(summary["as_of"])[:10])
    except ValueError:
        return []
    age = (today - as_of).days
    if age <= stale_days:
        return []
    return [_issue("medium", f"保有銘柄が {as_of.isoformat()} 時点のまま {age}日（許容 {stale_days}日）。"
                             "ポジションサイジング・保有の判定が古い保有で計算されている（家計簿締めで更新）")]


# ── ⑤ クラウド審議 routine ────────────────────────────────────

def routine_issues(health: dict | None, now: datetime, interval_days: int, grace_days: int,
                   start_date: date | None = None) -> list:
    """health: deliberation_queue.get_routine_health の戻り値。

    last_activity（最後に成功した run か審議の登録）が interval+grace 日より前なら「起動していない」。
    routine 導入前（start_date より前）の空白は数えない。claimed・running の放置も拾う。"""
    if not health:
        return []
    out = []
    last = _parse_dt(health.get("last_activity"))
    limit = interval_days + grace_days
    ref = last
    if start_date is not None:
        st = datetime.combine(start_date, datetime.min.time())
        ref = max(last, st) if last else st
    if ref is not None and (now - ref).days > limit:
        out.append(_issue("high", f"投資審議が {(now - ref).days}日 行われていない（間隔 {interval_days}日＋猶予 {grace_days}日）。"
                                  f"最終: {str(health.get('last_activity') or '記録なし')[:16]}。"
                                  "routine の無効化・クラウド側の認証エラー（BQ_CREDENTIALS の差し替え漏れ）を疑う"))
    n_claim = int(health.get("stale_claimed") or 0)
    if n_claim:
        out.append(_issue("high", f"審議キューに claimed のまま放置された行が {n_claim}件"
                                  "（routine が途中で落ちて release されていない。次の routine はこの銘柄を取れない）。"
                                  "`python3 tools/invest_queue.py release --older-than-hours 24` で戻せる"))
    n_run = int(health.get("stale_running") or 0)
    if n_run:
        out.append(_issue("medium", f"running のまま終わっていない審議の実行記録が {n_run}件（途中停止の疑い）"))
    return out


# ── 読み込み ──────────────────────────────────────────────────

def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def collect_file_issues(cfg, now: datetime | None = None) -> list:
    """①〜④（ファイルで判定できるもの）。Mac の同期は Actions 上でだけ判定する
    （Mac 自身で動かすと、自分の同期状況を自分で見ることになり意味がない）。"""
    now = now or datetime.now()
    log_dir = Path(_cfg(cfg, "LOG_DIR", "stock_data/logs"))
    data_dir = Path(_cfg(cfg, "DATA_DIR", "stock_data/data"))
    issues = []
    issues += actions_usage_issues(_read_json(log_dir / ACTIONS_USAGE_FILE),
                                   int(_cfg(cfg, "OPS_ACTIONS_MONTHLY_MINUTES", 2000)),
                                   float(_cfg(cfg, "OPS_ACTIONS_WARN_RATIO", 0.8)), now.date())
    if os.environ.get("GITHUB_ACTIONS") == "true":
        issues += mac_sync_issues(_read_json(log_dir / MAC_STATUS_FILE), now,
                                  float(_cfg(cfg, "OPS_MAC_SYNC_STALE_HOURS", 48)),
                                  float(_cfg(cfg, "OPS_WEEKLY_LOOP_STALE_DAYS", 9)))
    issues += holdings_issues(_read_json(data_dir / "portfolio_summary.json"), now.date(),
                              int(_cfg(cfg, "OPS_HOLDINGS_STALE_DAYS", 14)))
    return issues


def build_alert(issues: list) -> dict | None:
    """issue のリスト → alert_notifier のアラート1件（無ければ None）。"""
    if not issues:
        return None
    return {
        "trigger":  "ops_health",
        "title":    f"🩺 運用の監視で{len(issues)}件（日次本体の外側）",
        "body":     "\n".join(f"・{i['text']}" for i in issues),
        "severity": "high" if any(i["severity"] == "high" for i in issues) else "medium",
    }


# ── 通知が届かないときの別経路 ────────────────────────────────

def flag_ci_failure(reason: str) -> None:
    """Gmail が使えない（キー失効・送信失敗）ときに GitHub の失敗通知という別経路へ回す。

    GITHUB_ENV に NOTIFY_FAILED を書き、daily_run.yml の最後のステップがジョブを赤にする
    （状態ファイルの commit より後なので、その日の保存は止めない）。Actions 外では何もしない。"""
    env_file = os.environ.get("GITHUB_ENV")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    text = str(reason).replace("\n", " ")[:300]
    try:
        if env_file:
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write(f"NOTIFY_FAILED={text}\n")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"\n### ❌ 通知が届いていない\n- {text}\n")
    except OSError:
        pass
