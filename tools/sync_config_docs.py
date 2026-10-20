#!/usr/bin/env python3
"""構成管理資料を BigQuery（private.state_files）へ保存する（02_Configuration_Management → 構成管理資料/ → BQ）。

構成管理資料は公開しないため git には入れない。02（正本）を作業コピーの 構成管理資料/（git 管理外）へ写し、
state_store.save_scoped(paths=("構成管理資料/",), source="docs-sync") で BQ へ保存する（load job だけ・無料）。
クラウドの開発セッションは `python3 state_store.py restore --prefix 構成管理資料/ --force` で取り出す。

既定は dry-run（コピー・保存をしない）。実行するには --apply を付ける。
--prune を付けると、02 に無くなったファイル（フォルダ再編・改名の跡）を 構成管理資料/ からも消し、BQ には削除の印を付ける。

安全策:
- 認証情報らしいファイル名は同期しない（EXCLUDE_NAME_PATTERNS）
- 認証情報置き場のフォルダは丸ごと同期しない（EXCLUDE_DIRS）
- 同期候補と同期先の全テキストを秘密情報パターンでスキャンし、1件でも検出したら何もコピーせず中止する
- テキスト以外の形式（zip・numbers など）は同期しない（一覧に出す）
- 構成管理資料/ にしか無いファイルは --prune を付けたときだけ削除する（既定は一覧に出すだけ）。
  BQ の削除の印も --prune のときだけ付ける（既定は追加・更新だけ）
- 同期対象が0件なら中止する（空のまま保存すると BQ の資料すべてに削除の印が付くため）
- ファイル名は NFC にそろえてから保存する。macOS の日本語名は NFD のことがあり、そのまま保存すると
  クラウド（Linux）で `--prefix 構成管理資料/` の前方一致や他の版との照合が外れるため
- BQ の認証が無いときは、コピーまでして「BQ に保存できなかった」と exit 7 で知らせる
"""
from __future__ import annotations

import argparse
import fnmatch
import filecmp
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent            # 01_System（作業コピー）
SRC = REPO.parent / "02_Configuration_Management"         # vault 側（正本）
DST_NAME = "構成管理資料"
DST = REPO / DST_NAME
BQ_SOURCE = "docs-sync"

EXCLUDE_NAME_PATTERNS = [          # 小文字で比較する
    ".ds_store", ".env*", "*.pem", "*.p12",
    "*_key.json", "*gcp_key*", "*credential*",
    "*pass.json", "*-pass*",
    "o-auth*", "client_secret*",
    "github_secrets_ledger.md",
]

EXCLUDE_DIRS = {"09_認証情報_同期対象外"}   # 02 直下のフォルダ名（NFC）

TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".py", ".rtf", ".sql", ".yml", ".yaml", ".toml", ".html", ".svg"}

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


def is_excluded(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in EXCLUDE_NAME_PATTERNS)


def collect(src: Path | None = None) -> tuple[list[Path], list[str], list[str]]:
    """同期候補・除外（認証情報等）・スキップ（非テキスト）を返す。"""
    src = src or SRC
    copy, excluded, skipped = [], [], []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if ".git" in rel.parts:
            continue
        if nfc(rel.parts[0]) in EXCLUDE_DIRS or is_excluded(p.name):
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


def normalize_names(root: Path) -> int:
    """root 以下のファイル名・フォルダ名を NFC に改名する（深い方から）。改名した数を返す。
    APFS は正規化違いを同じ名前と見なすので、一度別名を経由して付け直す。"""
    n = 0
    if not root.is_dir():
        return 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames + dirnames:
            if name == nfc(name):
                continue
            p = Path(dirpath) / name
            tmp = p.with_name(nfc(name) + ".nfc_tmp")
            os.rename(p, tmp)
            os.rename(tmp, p.with_name(nfc(name)))
            n += 1
    return n


def save_to_bq(root: Path, paths: tuple, source: str, delete_missing: bool) -> int:
    """root（リポジトリ直下に当たるフォルダ）の paths を BQ へ保存する。成功 0・保存できなければ 7。"""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        import state_store as ss
        r = ss.save_scoped(root, paths, source=source, delete_missing=delete_missing)
    except (Exception, SystemExit) as e:   # 認証が無いと make_client は SystemExit を投げる
        print(f"❌ BQ に保存できなかった（{type(e).__name__}: {str(e)[:160]}）。"
              f"コピーは {root} に済んでいる。BQ の認証（BQ_CREDENTIALS・BQ_CREDENTIALS_PATH・"
              "GOOGLE_APPLICATION_CREDENTIALS）を確かめて --apply をやり直す")
        return 7
    print(f"{'✅' if r.ok else '❌'} BQ private.state_files へ保存: 走査{r.scanned}・保存{r.uploaded}"
          f"（{r.bytes_raw / 1024 ** 2:.1f}MB）・削除の印{r.deleted}・load job {r.jobs}本")
    return 0 if r.ok else 7


def main(argv=None) -> int:
    global SRC
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="コピーと BQ への保存を実行（既定は dry-run）")
    ap.add_argument("--prune", action="store_true",
                    help="02 に無い 構成管理資料/ のファイルを削除し、BQ に削除の印を付ける")
    ap.add_argument("--src", default="", help="同期元（既定は作業コピーの隣の 02_Configuration_Management）")
    args = ap.parse_args(argv)
    if args.src:
        SRC = Path(args.src).expanduser().resolve()

    if not SRC.is_dir():
        print(f"中止: 同期元がない: {SRC}")
        return 1

    copy, excluded, skipped = collect()
    if not copy:
        print(f"中止: 同期対象が0件: {SRC}")
        return 1
    todo = []
    for p in copy:
        target = DST / nfc(str(p.relative_to(SRC)))
        if not target.exists():
            todo.append((p, "+"))
        elif not filecmp.cmp(p, target, shallow=False):
            todo.append((p, "~"))

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
    print(f"同期先: {DST}（git 管理外）→ BQ private.state_files")
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

    for p, _ in todo:
        target = DST / nfc(str(p.relative_to(SRC)))
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
    renamed = normalize_names(DST)
    if renamed:
        print(f"ファイル名を NFC にそろえた: {renamed}件")
    # 削除の印は --prune のときだけ（既定は追加・更新だけ。02 に無いだけの資料を BQ から消さない）
    return save_to_bq(REPO, (DST_NAME + "/",), BQ_SOURCE, delete_missing=args.prune)


if __name__ == "__main__":
    sys.exit(main())
