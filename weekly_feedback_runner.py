# ==============================================================
# weekly_feedback_runner.py  v1.0  (E2)
# 審議→成績→デビル校正 クローズドループの週次自動実行
# ==============================================================
# 【役割】
#   launchd（毎週日曜21:00・setup/com.user.invest-weekly.plist）から起動し、
#     1) obsidian_sync.py journal   … 承認済みBuyの台帳記帳（冪等）
#     2) obsidian_sync.py feedback  … 成績還流（performance_tracker連携）
#     3) devil_calibration.py       … 25_デビル校正ノート.md 更新
#   を連続実行する。結果は <STOCK_ANALYZER_BASE>/weekly_loop_status.json に残す（Mac では作業コピーの外の
#   ~/.stock_analyzer_sync/stock_data_mirror。未設定ならリポジトリの stock_data）。
#   このファイルは Mac にしか無いので、obsidian_sync.py all が mac_sync_status.json の
#   weekly_loop に載せて push し、日次メール（ops_health）が最終成功の古さを見る。
#
# 【実行】python3 weekly_feedback_runner.py [--dry-run] [--if-stale]
#   --if-stale: 前回「成功」から7日未満なら何もしない（追いつき用ガード）
#
# 【二重の起動経路】
#   本線     : launchd com.user.invest-weekly（毎週日曜21:00）
#              → 司令塔シェル run_apps.sh invest-weekly → 無条件実行
#   バックアップ: Claude予定タスク（毎週月曜10:00）→ --if-stale で実行
#              日曜21時にMacが落ちていた回を翌日拾う。本線が走っていれば
#              「7日未満」で自動スキップするので二重実行にならない。
#   ※ この処理は vault（ローカルObsidian）へ書くため GitHub Actions では
#     代替できない。バックアップも必ず Mac 側に置く必要がある。
#
# 【前提】Mac上でvaultへの到達可能性（obsidian_sync.py の設定に従う）
# ==============================================================

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

STEPS = [
    ("journal",     [sys.executable, "obsidian_sync.py", "journal"]),
    ("feedback",    [sys.executable, "obsidian_sync.py", "feedback"]),
    ("calibration", [sys.executable, "devil_calibration.py"]),
]

STATUS_PATH = Path("stock_data/weekly_loop_status.json")


def status_file(base: Path = None) -> Path:
    """週次ループの記録ファイル。base を渡したら base/stock_data、無ければ STOCK_ANALYZER_BASE、それも無ければリポジトリの stock_data。"""
    if base is None and os.environ.get("STOCK_ANALYZER_BASE"):
        return Path(os.environ["STOCK_ANALYZER_BASE"]).expanduser() / STATUS_PATH.name
    return (base or Path(__file__).resolve().parent) / STATUS_PATH

# --if-stale で「前回成功からこの日数以上」経っていたら実行する
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_WEEKLY_FEEDBACK_RUNNER_STALE_DAYS': 'STALE_DAYS',
}

STALE_DAYS = 7


def last_success_at(base: Path = None) -> datetime | None:
    """前回「成功した」実行の日時。記録が無ければ None。"""
    try:
        st = json.loads(status_file(base).read_text(encoding="utf-8"))
        s = st.get("last_success") or (st.get("run_at") if st.get("ok") else None)
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M") if s else None
    except Exception:
        return None


def is_stale(now: datetime = None, base: Path = None,
             stale_days: int = STALE_DAYS) -> tuple:
    """実行すべきか判定する。Returns: (実行するか, 理由)

    launchd（日曜21:00）が本線。Macが落ちていた等で回が飛んだ場合に、
    バックアップ経路から --if-stale で呼んで追いつくために使う。
    前回成功の記録が無い初回は必ず実行する。
    """
    now = now or datetime.now()
    last = last_success_at(base)
    if last is None:
        return True, "前回成功の記録なし（初回 or 未実行）"
    elapsed = (now - last).days
    if elapsed >= stale_days:
        return True, f"前回成功から{elapsed}日経過（しきい値{stale_days}日）"
    return False, f"前回成功から{elapsed}日（しきい値{stale_days}日に未達）"


def main(dry_run: bool = False, if_stale: bool = False) -> int:
    import config as cfg
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    base = Path(__file__).resolve().parent

    # バックアップ経路（Claude予定タスク等）から頻繁に呼ばれても
    # 二重実行しないためのガード。本線の launchd は無条件で走る。
    if if_stale and not dry_run:
        run, why = is_stale()
        if not run:
            print(f"⏩ 週次ループ: スキップ（{why}）")
            return 0
        print(f"🔁 週次ループを実行: {why}")

    status = {"run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "steps": {}, "ok": True}

    for name, cmd in STEPS:
        if dry_run:
            print(f"[dry-run] {name}: {' '.join(cmd)}")
            status["steps"][name] = "dry-run"
            continue
        try:
            r = subprocess.run(cmd, cwd=base, capture_output=True,
                               text=True, timeout=600)
            ok = (r.returncode == 0)
            status["steps"][name] = "success" if ok else f"exit {r.returncode}"
            if not ok:
                status["ok"] = False
                tail = (r.stderr or r.stdout or "")[-300:]
                status["steps"][name] += f" / {tail}"
                print(f"  ❌ {name}: exit {r.returncode}\n{tail}")
            else:
                print(f"  ✅ {name}")
        except Exception as e:
            status["ok"] = False
            status["steps"][name] = f"error: {str(e)[:120]}"
            print(f"  ❌ {name}: {e}")

    # 成功した回だけ last_success を進める。失敗した回で更新すると
    # --if-stale が「最近走った」と誤認し、追いつきが効かなくなる。
    if status["ok"] and not dry_run:
        status["last_success"] = status["run_at"]
    else:
        prev = last_success_at()
        if prev:
            status["last_success"] = prev.strftime("%Y-%m-%d %H:%M")

    out = status_file()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(("✅ 週次ループ完了" if status["ok"] else "⚠️ 週次ループ一部失敗") + f" → {out}")
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv,
                          if_stale="--if-stale" in sys.argv))
