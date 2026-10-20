# ==============================================================
# actions_log.py — Actions の各ステップの出力をファイルへ回し、ログには要約だけを出す
# ==============================================================
# 使い方: python actions_log.py --workflow daily_run --step analyzer -- python -u run_headless.py --mode daily
#   子プロセスの標準出力・標準エラーは stock_data/logs/actions_<workflow>_<日付>.log に追記する
#   （state_store が BQ に保存する。git には入れない＝.gitignore）。
#   Actions のログと GITHUB_STEP_SUMMARY には「成功/失敗・終了コード・所要時間・出力行数・✅⚠️❌の数」だけを出す。
#   失敗したときだけ、末尾のエラーらしい行を数本、長い数字（金額・銘柄コード）を伏せて出す。
#   子の GITHUB_STEP_SUMMARY はログファイル側へ回す（保有・スコアが要約欄に出ないように）。
#   GITHUB_OUTPUT・GITHUB_ENV はそのまま渡す（後続ステップの判定に使う）。
#   終了コードは子のものをそのまま返す。
# ==============================================================
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ERROR_HINTS = ("Traceback", "Error", "Exception", "❌", "失敗", "FAILED", "fatal")
MAX_ERROR_LINES = 8
TAIL_LINES = 200
_LONG_NUM = re.compile(r"(?<![A-Za-z_])(?:\d[\d,]{3,}(?:\.\d+)?|\d+\.\d{2,})")
_LINE_NO = re.compile(r"(line \d+)")


def log_path(base: Path, workflow: str, day: str | None = None) -> Path:
    day = day or datetime.now().strftime("%Y-%m-%d")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", workflow) or "workflow"
    return Path(base) / "logs" / f"actions_{safe}_{day}.log"


def mask_line(line: str, width: int = 200) -> str:
    """長い数字（4桁以上・小数）を ### に置き換える。Traceback の line N は残す。"""
    keep = {}

    def _hold(m):
        key = f"\x00{len(keep)}\x00"
        keep[key] = m.group(1)
        return key
    s = _LINE_NO.sub(_hold, line.rstrip("\n"))
    s = _LONG_NUM.sub("###", s)
    for k, v in keep.items():
        s = s.replace(k, v)
    return s[:width]


def summarize(lines: list, rc: int, seconds: float, step: str) -> list:
    """出力の行 → 要約の行（中身は出さない）。"""
    ok = sum(1 for l in lines if "✅" in l)
    warn = sum(1 for l in lines if "⚠" in l)
    err = sum(1 for l in lines if "❌" in l)
    head = (f"{'✅' if rc == 0 else '❌'} {step}: {'成功' if rc == 0 else '失敗'}（終了コード {rc}）"
            f"・{seconds:.0f}秒・出力 {len(lines)}行・✅{ok} ⚠️{warn} ❌{err}")
    out = [head]
    if rc != 0:
        tail = lines[-TAIL_LINES:]
        picked = [mask_line(l) for l in tail if any(h in l for h in ERROR_HINTS)][-MAX_ERROR_LINES:]
        if picked:
            out.append("  末尾のエラー行（数字は伏せる）:")
            out += [f"    {l}" for l in picked]
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("usage: actions_log.py --workflow W --step S -- command ...")
        return 2
    i = argv.index("--")
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--step", required=True)
    ap.add_argument("--base", default=os.environ.get("STOCK_ANALYZER_BASE") or "stock_data")
    a = ap.parse_args(argv[:i])
    cmd = argv[i + 1:]
    if not cmd:
        print("usage: actions_log.py --workflow W --step S -- command ...")
        return 2
    lp = log_path(Path(a.base), a.workflow)
    lp.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    real_summary = env.get("GITHUB_STEP_SUMMARY")
    fd, child_summary = tempfile.mkstemp(prefix="step_summary_", suffix=".md")
    os.close(fd)
    env["GITHUB_STEP_SUMMARY"] = child_summary
    env.setdefault("PYTHONUNBUFFERED", "1")
    t0 = time.time()
    start = lp.stat().st_size if lp.exists() else 0
    with open(lp, "a", encoding="utf-8") as fh:
        fh.write(f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{a.step}] {' '.join(cmd)}\n")
        fh.flush()
        try:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env).returncode
        except OSError as e:
            fh.write(f"❌ 起動できない: {e}\n")
            rc = 127
        sec = time.time() - t0
        try:
            extra = Path(child_summary).read_text(encoding="utf-8")
        except OSError:
            extra = ""
        if extra.strip():
            fh.write(f"----- [{a.step}] step summary -----\n{extra}\n")
        fh.write(f"===== [{a.step}] 終了コード {rc}・{sec:.0f}秒\n")
    Path(child_summary).unlink(missing_ok=True)
    # この回の出力だけを数える（同じ日の前のステップの分は数えない）
    with open(lp, "rb") as fh:
        fh.seek(start)
        body = fh.read().decode("utf-8", errors="replace").splitlines()[2:-1]
    cut = f"----- [{a.step}] step summary -----"
    if cut in body:
        body = body[:body.index(cut)]
    lines = summarize(body, rc, sec, a.step)
    print("\n".join(lines))
    if real_summary:
        try:
            with open(real_summary, "a", encoding="utf-8") as fh:
                fh.write("- " + lines[0] + "\n")
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
