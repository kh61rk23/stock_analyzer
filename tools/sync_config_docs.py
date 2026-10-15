#!/usr/bin/env python3
"""構成管理資料の同期と push（02_Configuration_Management → 01_System/構成管理資料）。

既定は dry-run（コピー・commit・push をしない）。実行するには --apply を付ける。
--no-push を付けると commit までで止まる。
--prune を付けると、02 に無くなったファイル（フォルダ再編・改名の跡）を 01 側からも消す。

安全策:
- 認証情報らしいファイル名は同期しない（EXCLUDE_NAME_PATTERNS）
- 認証情報置き場のフォルダは丸ごと同期しない（EXCLUDE_DIRS）
- 同期候補と同期先の全テキストを秘密情報パターンでスキャンし、1件でも検出したら何もコピーせず中止する
- テキスト以外の形式（zip・numbers など）は同期しない（一覧に出す）
- git add は 構成管理資料/ だけに限る。index に他のステージ済み変更があれば中止する
- main 以外のブランチでは push しない
- 01 側にしか無いファイルは --prune を付けたときだけ削除する（既定は一覧に出すだけ）
- commit 前に 構成管理資料/ の index を作り直す。macOS の日本語ファイル名は NFD/NFC の
  2通りで index に載ることがあり、片方が「未追跡」に見えて同期漏れになるため
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import filecmp
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent            # 01_System（gitリポジトリ）
SRC = REPO.parent / "02_Configuration_Management"         # vault 側（正本）
DST_NAME = "構成管理資料"
DST = REPO / DST_NAME

EXCLUDE_NAME_PATTERNS = [          # 小文字で比較する
    ".ds_store", ".env*", "*.pem", "*.p12",
    "*_key.json", "*gcp_key*", "*credential*",
    "*pass.json", "*-pass*",
    "o-auth*", "client_secret*",
    "github_secrets_ledger.md",
]

EXCLUDE_DIRS = {"09_認証情報_同期対象外"}   # 02 直下のフォルダ名（NFC）

TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".py", ".rtf", ".sql", ".yml", ".yaml", ".toml", ".html"}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r'"private_key"\s*:\s*"'),
    re.compile(r'"type"\s*:\s*"service_account"'),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r'"client_secret"\s*:\s*"'),
]

COMMIT_TRAILERS = (
    "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>\n"
    "Claude-Session: https://claude.ai/code/session_01QVnwQZtSo5nAgbCJmyZ4oH\n"
)


def git(*args: str) -> subprocess.CompletedProcess:
    # core.quotepath=off: 日本語のファイル名が octal エスケープされて比較が狂うのを防ぐ
    return subprocess.run(["git", "-c", "core.quotepath=off", *args],
                          cwd=REPO, text=True, capture_output=True)


def is_excluded(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in EXCLUDE_NAME_PATTERNS)


def collect() -> tuple[list[Path], list[str], list[str]]:
    """同期候補・除外（認証情報等）・スキップ（非テキスト）を返す。"""
    copy, excluded, skipped = [], [], []
    for p in sorted(SRC.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(SRC)
        if ".git" in rel.parts:
            continue
        if unicodedata.normalize("NFC", rel.parts[0]) in EXCLUDE_DIRS or is_excluded(p.name):
            excluded.append(str(rel))
        elif p.suffix.lower() not in TEXT_SUFFIXES:
            skipped.append(str(rel))
        else:
            copy.append(p)
    return copy, excluded, skipped


def scan(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return [f"読めない: {e}"]
    return [pat.pattern for pat in SECRET_PATTERNS if pat.search(text)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="コピー・commit・push を実行（既定は dry-run）")
    ap.add_argument("--no-push", action="store_true", help="commit までで止める")
    ap.add_argument("--prune", action="store_true", help="02 に無い 01 側のファイルを削除する")
    args = ap.parse_args()

    if not SRC.is_dir():
        print(f"中止: 同期元がない: {SRC}")
        return 1

    copy, excluded, skipped = collect()
    todo = []
    for p in copy:
        target = DST / p.relative_to(SRC)
        if not target.exists():
            todo.append((p, "+"))
        elif not filecmp.cmp(p, target, shallow=False):
            todo.append((p, "~"))

    nfc = lambda s: unicodedata.normalize("NFC", s)
    src_rel = {nfc(str(p.relative_to(SRC))) for p in copy}
    # 非テキストでスキップしたものは 02 に実在するので、--prune でも消さない
    keep_rel = src_rel | {nfc(s) for s in skipped}
    orphans = []
    if DST.is_dir():
        for p in sorted(DST.rglob("*")):
            if p.is_file() and ".git" not in p.relative_to(DST).parts:
                rel = str(p.relative_to(DST))
                if nfc(rel) not in keep_rel and p.name != ".DS_Store":
                    orphans.append(rel)

    hits: dict[str, list[str]] = {}
    for p, _ in todo:
        h = scan(p)
        if h:
            hits[f"[02] {p.relative_to(SRC)}"] = h
    if DST.is_dir():
        for p in DST.rglob("*"):
            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES and ".git" not in p.relative_to(DST).parts:
                h = scan(p)
                if h:
                    hits[f"[01] {p.relative_to(DST)}"] = h
    if hits:
        print("中止: 秘密情報らしき記述を検出した。何もコピーしていない。")
        for k, v in hits.items():
            print(f"  {k}: {v}")
        return 2

    print(f"同期元: {SRC}")
    print(f"同期先: {DST}")
    print(f"同期対象: {len(copy)}件 / 追加・更新: {len(todo)}件 / "
          f"除外（認証情報等）: {len(excluded)}件 / 非テキストでスキップ: {len(skipped)}件 / "
          f"01のみ（{'削除する' if args.prune else '削除しない'}）: {len(orphans)}件")
    for p, mark in todo:
        print(f"  {mark} {p.relative_to(SRC)}")
    for rel in excluded:
        print(f"  excluded: {rel}")
    for rel in skipped:
        print(f"  skip(非テキスト): {rel}")
    for rel in orphans:
        print(f"  {'削除予定' if args.prune else 'only-in-01'}: {rel}")

    if not args.apply:
        print("\ndry-run です。実行するには --apply を付ける。")
        return 0

    branch = git("branch", "--show-current").stdout.strip()
    if branch != "main":
        print(f"中止: 現在のブランチは {branch}。main でのみ実行する。")
        return 3
    staged = git("diff", "--cached", "--name-only").stdout.splitlines()
    foreign = [f for f in staged if not f.startswith(DST_NAME + "/")]
    if foreign:
        print("中止: 構成管理資料以外のステージ済み変更がある。先に解除すること:")
        for f in foreign[:20]:
            print(f"  {f}")
        return 4

    for p, _ in todo:
        target = DST / p.relative_to(SRC)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    print(f"コピー: {len(todo)}件")
    if args.prune:
        for rel in orphans:
            (DST / rel).unlink()
        for d in sorted((p for p in DST.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            left = [c for c in d.iterdir() if c.name != ".DS_Store"]
            if not left:
                shutil.rmtree(d)
        print(f"削除: {len(orphans)}件")

    rm = git("rm", "-r", "--cached", "--quiet", "--ignore-unmatch", "--", DST_NAME)
    if rm.returncode != 0:
        print("index の作り直しに失敗:\n" + rm.stderr)
        return 5
    add = git("add", "--", DST_NAME)
    if add.returncode != 0:
        print("git add 失敗:\n" + add.stderr)
        return 5
    if git("diff", "--cached", "--quiet", "--", DST_NAME).returncode == 0:
        print("変更なし。commit・push しない。")
        return 0

    msg = f"docs: 構成管理資料を02から同期（{dt.date.today().isoformat()}）\n\n{COMMIT_TRAILERS}"
    commit = git("commit", "-m", msg)
    if commit.returncode != 0:
        print("commit 失敗:\n" + commit.stderr)
        return 5
    print("commit 完了")

    if args.no_push:
        print("--no-push のため push していない。")
        return 0
    # Actions が1日に何度も stock_data/ を自動コミットするので、先にリベースで取り込む
    # （履歴は書き換えない。衝突したら中止して報告する）
    pull = git("pull", "--rebase", "--autostash", "origin", "main")
    if pull.returncode != 0:
        git("rebase", "--abort")
        print("push 前の取り込み（pull --rebase）に失敗:\n" + pull.stderr)
        return 6
    # 資料の一括移動など送信量が大きいと既定バッファでは HTTP 408 で切れるため広げる
    push = git("-c", "http.postBuffer=524288000", "push", "origin", "main")
    if push.returncode != 0:
        print("push 失敗:\n" + push.stderr)
        return 6
    print("push 完了: origin/main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
