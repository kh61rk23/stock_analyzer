#!/usr/bin/env python3
"""投資部の部則・ツール・審議スキルを vault から BigQuery（private.state_files）へ保存する（vault が正本）。

部則は公開しないため git には入れない。vault から写し先（--root。既定は作業コピー）の下の
investment_dept/・.claude/skills/invest-*/（git 管理外）へ写し、state_store.save_scoped(source="invest-rules")
で BQ へ保存する（load job だけ・無料）。クラウドの routine は
`python3 state_store.py restore --prefix investment_dept/ .claude/skills/invest- --force` で取り出して読む。

同期元（vault: Vault2_ClaudeCode_Local/CEO室/departments/B-investment）:
  B1-bull.md / B2-bear.md / B3-devil.md / B4-analyst.md / _運用ルール.md / CLAUDE.md / README.md /
  F補遺_作成指示.md / 25_デビル校正ノート.md          → investment_dept/
  tools/fundamental_packet.py                       → investment_dept/tools/
  skills/invest-*/（SKILL.md ほかテキスト）           → .claude/skills/invest-*/

既定は dry-run（コピー・保存をしない）。実行するには --apply。

安全策（sync_config_docs.py と同じ）:
- 認証情報らしいファイル名・バックアップ（*.bak*）・dist/（.skill の zip）は同期しない
- 同期候補と写し先の全テキストを秘密情報パターンでスキャンし、1件でも検出したら何もコピーせず中止
- vault 側に無くなったファイルは --prune を付けたときだけ写し先から消し、BQ に削除の印を付ける
  （既定は追加・更新だけ。写し先が空でも BQ の部則は消えない）
- 同期対象が0件なら中止する（空のまま保存すると BQ の部則すべてに削除の印が付くため）
- 写し先のファイル名は NFC にそろえてから保存する（クラウドの前方一致・版の照合を外さないため）
- BQ の認証が無いときは、コピーまでして「BQ に保存できなかった」と exit 7 で知らせる
"""
from __future__ import annotations

import argparse
import fnmatch
import filecmp
import importlib.util
import re
import shutil
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent            # 写し先（--root で差し替える）。BQ のパスはここからの相対
SRC = (Path.home() / "Documents" / "Obsidian_Local" / "Vault2_ClaudeCode_Local" / "CEO室"
       / "departments" / "B-investment")
DST_RULES = "investment_dept"
DST_SKILLS = ".claude/skills"
BQ_SOURCE = "invest-rules"

RULE_FILES = ["B1-bull.md", "B2-bear.md", "B3-devil.md", "B4-analyst.md", "_運用ルール.md", "CLAUDE.md",
              "README.md", "F補遺_作成指示.md", "25_デビル校正ノート.md"]
TOOL_FILES = ["tools/fundamental_packet.py"]
SKILL_PREFIX = "invest-"
# vault から一時的に消えても、BQ の版に削除の印を付けられるよう範囲に入れておくスキル
KNOWN_SKILLS = ("invest-dept", "invest-b1-bull", "invest-b2-bear", "invest-b3-devil", "invest-b4-reporter")
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


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def is_excluded(rel: str) -> bool:
    """同期しないパスか。ファイル名の除外パターン、または除外フォルダ（dist 等）配下。"""
    parts = Path(nfc(rel)).parts
    if any(p in SKILL_EXCLUDE_DIRS for p in parts[:-1]):
        return True
    low = parts[-1].lower()
    return any(fnmatch.fnmatch(low, pat) for pat in EXCLUDE_NAME_PATTERNS)


def collect(src: Path | None = None) -> tuple:
    """Returns: (同期候補 [(元パス, 先の相対パス NFC)], 除外した相対パス, 非テキストでスキップした相対パス)"""
    src = src or SRC
    pairs, excluded, skipped = [], [], []

    def consider(p: Path, dst_rel: str, rel: str):
        if is_excluded(rel):
            excluded.append(rel)
        elif p.suffix.lower() not in TEXT_SUFFIXES:
            skipped.append(rel)
        else:
            pairs.append((p, nfc(dst_rel)))

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


def _skill_dirs(root: Path) -> list:
    skills = Path(root) / DST_SKILLS
    if not skills.is_dir():
        return []
    return [d.name for d in sorted(skills.iterdir()) if d.is_dir() and d.name.startswith(SKILL_PREFIX)]


def _dst_files(root: Path) -> list:
    """写し先に今あるファイル（root からの相対パス）。"""
    out = []
    roots = [Path(root) / DST_RULES] + [Path(root) / DST_SKILLS / n for n in _skill_dirs(root)]
    for r in roots:
        if r.is_dir():
            out += [str(p.relative_to(root)) for p in sorted(r.rglob("*"))
                    if p.is_file() and p.name != ".DS_Store"]
    return out


def save_paths(root: Path, pairs) -> tuple:
    """BQ へ保存する範囲（root からの相対・フォルダは末尾 /）。investment_dept/ と invest-* のスキルごとのフォルダ。
    state_store の範囲はフォルダ単位なので、.claude/skills/ 全体ではなく invest-* だけを並べる
    （他のスキルを範囲に入れると、手元に無いだけで削除の印が付く）。"""
    names = set(KNOWN_SKILLS) | set(_skill_dirs(root))
    for _, d in pairs:
        parts = d.split("/")
        if d.startswith(DST_SKILLS + "/") and len(parts) > 3:
            names.add(parts[2])
    return (f"{DST_RULES}/",) + tuple(f"{DST_SKILLS}/{n}/" for n in sorted(names))


def _load_sync_config_docs():
    spec = importlib.util.spec_from_file_location("sync_config_docs", str(Path(__file__).with_name("sync_config_docs.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv=None) -> int:
    global REPO
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="コピーと BQ への保存を実行（既定は dry-run）")
    ap.add_argument("--prune", action="store_true", help="vault に無い写し先のファイルを削除し、BQ に削除の印を付ける")
    ap.add_argument("--root", "--repo", dest="root", default="",
                    help="写し先（リポジトリ直下に当たるフォルダ。既定はこのファイルのある作業コピー）。"
                         "obsidian_sync rules は ~/.stock_analyzer_sync/invest_rules を渡す")
    args = ap.parse_args(argv)
    if args.root:
        REPO = Path(args.root).expanduser().resolve()

    if not SRC.is_dir():
        print(f"中止: 同期元がない: {SRC}")
        return 1

    pairs, excluded, skipped = collect()
    if not pairs:
        print(f"中止: 同期対象が0件: {SRC}")
        return 1
    todo = []
    for p, dst_rel in pairs:
        target = REPO / dst_rel
        if not target.exists():
            todo.append((p, dst_rel, "+"))
        elif not filecmp.cmp(p, target, shallow=False):
            todo.append((p, dst_rel, "~"))
    keep = {d for _, d in pairs}
    orphans = [f for f in _dst_files(REPO) if nfc(f) not in keep]

    hits = {}
    for p, dst_rel, _ in todo:
        h = scan(p)
        if h:
            hits[f"[vault] {dst_rel}"] = h
    for f in _dst_files(REPO):
        p = REPO / f
        if p.suffix.lower() in TEXT_SUFFIXES:
            h = scan(p)
            if h:
                hits[f"[写し先] {f}"] = h
    if hits:
        print("中止: 秘密情報らしき記述を検出した。何もコピーしていない。")
        for k, v in hits.items():
            print(f"  {k}: {v}")
        return 2

    print(f"同期元: {SRC}")
    print(f"写し先: {REPO}/{DST_RULES} と {DST_SKILLS}/{SKILL_PREFIX}*（git 管理外）→ BQ private.state_files")
    print(f"同期対象: {len(pairs)}件 / 追加・更新: {len(todo)}件 / 除外: {len(excluded)}件 / "
          f"非テキストでスキップ: {len(skipped)}件 / 写し先のみ（{'削除する' if args.prune else '削除しない'}）: {len(orphans)}件")
    for _, dst_rel, mark in todo:
        print(f"  {mark} {dst_rel}")
    for rel in excluded:
        print(f"  excluded: {rel}")
    for rel in skipped:
        print(f"  skip(非テキスト): {rel}")
    for rel in orphans:
        print(f"  {'削除予定' if args.prune else 'only-in-写し先'}: {rel}")

    if not args.apply:
        print("\ndry-run です。実行するには --apply を付ける。")
        return 0

    for p, dst_rel, _ in todo:
        target = REPO / dst_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    print(f"コピー: {len(todo)}件")
    if args.prune:
        for rel in orphans:
            (REPO / rel).unlink()
        print(f"削除: {len(orphans)}件")
    scd = _load_sync_config_docs()
    renamed = sum(scd.normalize_names(REPO / r) for r in [DST_RULES] + [f"{DST_SKILLS}/{n}" for n in _skill_dirs(REPO)])
    if renamed:
        print(f"ファイル名を NFC にそろえた: {renamed}件")
    # 削除の印は --prune のときだけ（既定は追加・更新だけ）
    return scd.save_to_bq(REPO, save_paths(REPO, pairs), BQ_SOURCE, delete_missing=args.prune)


if __name__ == "__main__":
    sys.exit(main())
