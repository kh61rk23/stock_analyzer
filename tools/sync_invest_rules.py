#!/usr/bin/env python3
"""投資部の部則・ツール・審議スキルを vault から repo へ同期して push する（vault が正本・repo は読み取り専用コピー）。

同期元（vault: Vault2_ClaudeCode_Local/CEO室/departments/B-investment）:
  B1-bull.md / B2-bear.md / B3-devil.md / B4-analyst.md / _運用ルール.md / CLAUDE.md / README.md /
  F補遺_作成指示.md / 25_デビル校正ノート.md          → investment_dept/
  tools/fundamental_packet.py                       → investment_dept/tools/
  skills/invest-*/（SKILL.md ほかテキスト）           → .claude/skills/invest-*/   （クラウド routine が読む）

既定は dry-run（コピー・commit・push をしない）。実行するには --apply。--no-push で commit まで。

安全策（sync_config_docs.py と同じ）:
- 認証情報らしいファイル名・バックアップ（*.bak*）・dist/（.skill の zip）は同期しない
- 同期候補と同期先の全テキストを秘密情報パターンでスキャンし、1件でも検出したら何もコピーせず中止
- git add は investment_dept/ と .claude/skills/invest-* だけ。他にステージ済みの変更があれば中止
- main 以外のブランチでは push しない
- vault 側に無くなったファイルは --prune を付けたときだけ repo 側から消す
- commit 前に対象の index を作り直す（macOS の日本語ファイル名 NFD/NFC 差で未追跡に見えるのを防ぐ）
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

REPO = Path(__file__).resolve().parent.parent            # 01_System（git リポジトリ）
SRC = (Path.home() / "Documents" / "Obsidian_Local" / "Vault2_ClaudeCode_Local" / "CEO室"
       / "departments" / "B-investment")
DST_RULES = "investment_dept"
DST_SKILLS = ".claude/skills"

RULE_FILES = ["B1-bull.md", "B2-bear.md", "B3-devil.md", "B4-analyst.md", "_運用ルール.md", "CLAUDE.md",
              "README.md", "F補遺_作成指示.md", "25_デビル校正ノート.md"]
TOOL_FILES = ["tools/fundamental_packet.py"]
SKILL_PREFIX = "invest-"
SKILL_EXCLUDE_DIRS = {"dist"}

EXCLUDE_NAME_PATTERNS = [          # 小文字で比較する
    ".ds_store", ".env*", "*.pem", "*.p12", "*.bak*", "*.skill", "*.zip",
    "*_key.json", "*gcp_key*", "*credential*", "*pass.json", "*-pass*",
    "o-auth*", "client_secret*", "github_secrets_ledger.md",
]
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".py", ".yaml", ".yml", ".toml"}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r'"private_key"\s*:\s*"'),
    re.compile(r'"type"\s*:\s*"service_account"'),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r'"client_secret"\s*:\s*"'),
]

COMMIT_TRAILERS = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>\n"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=REPO, text=True, capture_output=True)


def is_excluded(rel: str) -> bool:
    """同期しないパスか。ファイル名の除外パターン、または除外フォルダ（dist 等）配下。"""
    parts = Path(unicodedata.normalize("NFC", rel)).parts
    if any(p in SKILL_EXCLUDE_DIRS for p in parts[:-1]):
        return True
    low = parts[-1].lower()
    return any(fnmatch.fnmatch(low, pat) for pat in EXCLUDE_NAME_PATTERNS)


def collect(src: Path = SRC) -> tuple:
    """Returns: (同期候補 [(元パス, 先の相対パス)], 除外した相対パス, 非テキストでスキップした相対パス)"""
    pairs, excluded, skipped = [], [], []

    def consider(p: Path, dst_rel: str, rel: str):
        if is_excluded(rel):
            excluded.append(rel)
        elif p.suffix.lower() not in TEXT_SUFFIXES:
            skipped.append(rel)
        else:
            pairs.append((p, dst_rel))

    for rel in RULE_FILES:
        p = src / rel
        if p.is_file():
            consider(p, f"{DST_RULES}/{rel}", rel)
    for rel in TOOL_FILES:
        p = src / rel
        if p.is_file():
            consider(p, f"{DST_RULES}/{rel}", rel)
    skills = src / "skills"
    if skills.is_dir():
        for d in sorted(skills.iterdir()):
            if not (d.is_dir() and d.name.startswith(SKILL_PREFIX)):
                continue
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    rel = str(p.relative_to(skills))
                    consider(p, f"{DST_SKILLS}/{rel}", f"skills/{rel}")
    return pairs, excluded, skipped


def scan(path: Path) -> list:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return [f"読めない: {e}"]
    return [pat.pattern for pat in SECRET_PATTERNS if pat.search(text)]


def _dst_files() -> list:
    """repo 側の同期先に今あるファイル（相対パス）。"""
    out = []
    roots = [REPO / DST_RULES]
    skills = REPO / DST_SKILLS
    if skills.is_dir():
        roots += [d for d in sorted(skills.iterdir()) if d.is_dir() and d.name.startswith(SKILL_PREFIX)]
    for r in roots:
        if r.is_dir():
            out += [str(p.relative_to(REPO)) for p in sorted(r.rglob("*"))
                    if p.is_file() and p.name != ".DS_Store"]
    return out


def add_targets() -> list:
    """git add / index 再作成の対象（investment_dept と .claude/skills/invest-*）。"""
    t = [DST_RULES]
    skills = REPO / DST_SKILLS
    if skills.is_dir():
        t += [f"{DST_SKILLS}/{d.name}" for d in sorted(skills.iterdir())
              if d.is_dir() and d.name.startswith(SKILL_PREFIX)]
    return t


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="コピー・commit・push を実行（既定は dry-run）")
    ap.add_argument("--no-push", action="store_true", help="commit までで止める")
    ap.add_argument("--prune", action="store_true", help="vault に無い repo 側のファイルを削除する")
    args = ap.parse_args()

    if not SRC.is_dir():
        print(f"中止: 同期元がない: {SRC}")
        return 1

    pairs, excluded, skipped = collect()
    nfc = lambda s: unicodedata.normalize("NFC", s)
    todo = []
    for p, dst_rel in pairs:
        target = REPO / dst_rel
        if not target.exists():
            todo.append((p, dst_rel, "+"))
        elif not filecmp.cmp(p, target, shallow=False):
            todo.append((p, dst_rel, "~"))
    keep = {nfc(d) for _, d in pairs}
    orphans = [f for f in _dst_files() if nfc(f) not in keep]

    hits = {}
    for p, dst_rel, _ in todo:
        h = scan(p)
        if h:
            hits[f"[vault] {dst_rel}"] = h
    for f in _dst_files():
        p = REPO / f
        if p.suffix.lower() in TEXT_SUFFIXES:
            h = scan(p)
            if h:
                hits[f"[repo] {f}"] = h
    if hits:
        print("中止: 秘密情報らしき記述を検出した。何もコピーしていない。")
        for k, v in hits.items():
            print(f"  {k}: {v}")
        return 2

    print(f"同期元: {SRC}")
    print(f"同期先: {REPO}/{DST_RULES} と {DST_SKILLS}/{SKILL_PREFIX}*")
    print(f"同期対象: {len(pairs)}件 / 追加・更新: {len(todo)}件 / 除外: {len(excluded)}件 / "
          f"非テキストでスキップ: {len(skipped)}件 / repoのみ（{'削除する' if args.prune else '削除しない'}）: {len(orphans)}件")
    for _, dst_rel, mark in todo:
        print(f"  {mark} {dst_rel}")
    for rel in excluded:
        print(f"  excluded: {rel}")
    for rel in skipped:
        print(f"  skip(非テキスト): {rel}")
    for rel in orphans:
        print(f"  {'削除予定' if args.prune else 'only-in-repo'}: {rel}")

    if not args.apply:
        print("\ndry-run です。実行するには --apply を付ける。")
        return 0

    branch = git("branch", "--show-current").stdout.strip()
    if branch != "main":
        print(f"中止: 現在のブランチは {branch}。main でのみ実行する。")
        return 3
    targets = add_targets()
    staged = git("diff", "--cached", "--name-only").stdout.splitlines()
    foreign = [f for f in staged if not any(f == t or f.startswith(t + "/") for t in targets)]
    if foreign:
        print("中止: 同期対象以外のステージ済み変更がある。先に解除すること:")
        for f in foreign[:20]:
            print(f"  {f}")
        return 4

    for p, dst_rel, _ in todo:
        target = REPO / dst_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    print(f"コピー: {len(todo)}件")
    if args.prune:
        for rel in orphans:
            (REPO / rel).unlink()
        print(f"削除: {len(orphans)}件")

    # index を作り直す。追加対象は「同期したファイル」と「既に追跡済みで残すファイル」だけ
    # （routine 実行で生まれるキャッシュ等の未追跡ファイルは add しない）
    tracked = [f for f in git("ls-files", "--", *targets).stdout.splitlines() if (REPO / f).exists()]
    rm = git("rm", "-r", "--cached", "--quiet", "--ignore-unmatch", "--", *targets)
    if rm.returncode != 0:
        print("index の作り直しに失敗:\n" + rm.stderr)
        return 5
    to_add = sorted({d for _, d in pairs} | set(tracked))
    add = git("add", "--", *to_add)
    if add.returncode != 0:
        print("git add 失敗:\n" + add.stderr)
        return 5
    if git("diff", "--cached", "--quiet", "--", *targets).returncode == 0:
        print("変更なし。commit・push しない。")
        return 0

    msg = f"docs: 投資部の部則・審議スキルをvaultから同期（{dt.date.today().isoformat()}）\n\n{COMMIT_TRAILERS}"
    commit = git("commit", "-m", msg)
    if commit.returncode != 0:
        print("commit 失敗:\n" + commit.stderr)
        return 5
    print("commit 完了")

    if args.no_push:
        print("--no-push のため push していない。")
        return 0
    push = git("-c", "http.postBuffer=524288000", "push", "origin", "main")
    if push.returncode != 0:
        print("push 失敗:\n" + push.stderr)
        return 6
    print("push 完了: origin/main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
