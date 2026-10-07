"""コード内の修正履歴コメントを抜き出す・コード部分が変わっていないことを確かめる。

使い方:
  python3 tools/history_comments.py scan [ファイル...]      # 履歴コメントのブロック一覧（JSON）
  python3 tools/history_comments.py archive --out <md>     # 全ブロックをファイル別に Markdown へ書き出す
  python3 tools/history_comments.py same-code <git-rev>    # 指定リビジョンとコード（AST・docstring除く）が同じか

修正の経緯は開発ログ（board の 40_開発ログ.jsonl）に置き、コードのコメントには
「今どう動くか」と「なぜそうしているか（知らずに直すと壊れる理由）」だけを書く（CLAUDE.md）。
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = re.compile(r"20\d{2}-\d{2}-\d{2}|20\d{2}/\d{1,2}/\d{1,2}|旧実装|従来は|以前は|是正|バッチ\d|Wave ?[0-9R]|★")


def blocks(path: Path) -> list[dict]:
    """履歴らしい行を含む「連続したコメント行のかたまり」。"""
    lines = path.read_text(encoding="utf-8").split("\n")
    out, i = [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith("#"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("#"):
                j += 1
            text = "\n".join(lines[i:j])
            if HIST.search(text):
                dates = re.findall(r"20\d{2}-\d{2}-\d{2}", text)
                out.append({"file": path.name, "start": i + 1, "end": j, "dates": sorted(set(dates)),
                            "text": text})
            i = j
        else:
            i += 1
    return out


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body[0].value.value = ""
    return tree


def same_code(rev: str, files: list[str]) -> list[str]:
    """rev 時点と今とで、コメント・docstring 以外が変わったファイル。"""
    bad = []
    for f in files:
        try:
            old = subprocess.run(["git", "show", f"{rev}:{f}"], cwd=ROOT, capture_output=True,
                                 text=True, check=True).stdout
        except subprocess.CalledProcessError:
            continue
        new = (ROOT / f).read_text(encoding="utf-8")
        a = ast.dump(_strip_docstrings(ast.parse(old)))
        b = ast.dump(_strip_docstrings(ast.parse(new)))
        if a != b:
            bad.append(f)
    return bad


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "scan"
    if cmd == "same-code":
        rev = argv[1]
        changed = subprocess.run(["git", "diff", "--name-only", rev, "--", "*.py"], cwd=ROOT,
                                 capture_output=True, text=True).stdout.split()
        bad = same_code(rev, changed)
        print(f"コメント以外の変更: {len(bad)}件 / 対象{len(changed)}ファイル")
        for b in bad:
            print("  ❌", b)
        return 1 if bad else 0
    files = [ROOT / a for a in argv[1:] if a.endswith(".py")] or sorted(ROOT.glob("*.py"))
    allb = [b for f in files for b in blocks(f)]
    if cmd == "archive":
        out = Path(argv[argv.index("--out") + 1])
        by: dict = {}
        for b in allb:
            by.setdefault(b["file"], []).append(b)
        parts = ["# stock_analyzer コード内の修正履歴（移設アーカイブ）\n",
                 "コードのコメントから開発ログへ移したときの原文。行番号は移設前のもの。\n"]
        for f, bs in sorted(by.items()):
            parts.append(f"\n## {f}（{len(bs)}か所）\n")
            for b in bs:
                parts.append(f"\n### L{b['start']}-{b['end']} {' '.join(b['dates'])}\n\n```\n{b['text']}\n```\n")
        out.write_text("".join(parts), encoding="utf-8")
        print(f"✅ {len(allb)}ブロック / {len(by)}ファイル → {out}")
        return 0
    print(json.dumps(allb, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
