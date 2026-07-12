# ==============================================================
# weekly_feedback_runner.py  v1.0  (Wave R0-5 / E2)
# 審議→成績→デビル校正 クローズドループの週次自動実行
# ==============================================================
# 【役割】
#   launchd（毎週日曜21:00・setup/com.kenta.invest-weekly.plist）から起動し、
#     1) obsidian_sync.py journal   … 承認済みBuyの台帳記帳（冪等）
#     2) obsidian_sync.py feedback  … 成績還流（performance_tracker連携）
#     3) devil_calibration.py       … 25_デビル校正ノート.md 更新
#   を連続実行する。失敗時は結果ファイルに記録し、翌朝の統合ブリーフ
#   （alert_notifier / E11）が拾えるよう stock_data/weekly_loop_status.json に残す。
#
# 【実行】python3 weekly_feedback_runner.py [--dry-run]
# 【前提】Mac上でvaultへの到達可能性（obsidian_sync.py の設定に従う）
# ==============================================================

from __future__ import annotations

import json
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


def main(dry_run: bool = False) -> int:
    base = Path(__file__).resolve().parent
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

    (base / STATUS_PATH).parent.mkdir(parents=True, exist_ok=True)
    (base / STATUS_PATH).write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(("✅ 週次ループ完了" if status["ok"] else "⚠️ 週次ループ一部失敗")
          + f" → {STATUS_PATH}")
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
