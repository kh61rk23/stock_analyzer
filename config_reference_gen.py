# ==============================================================
# config_reference_gen.py — 構成管理資料 CONFIG_REFERENCE.md を config.py から再生成する
# 【用途】CONFIG_REFERENCE.md の唯一の生成手段。手で直すと設定追加が反映されなくなるため、
#   改修のたびに本スクリプトで作り直す（stock-analyzer-refit スキル §6 の完了手順に組み込む）。
# 【実行】python3 config_reference_gen.py [--out <path>]
#   既定の出力先: ../02_Configuration_Management/02_ファイル構成/CONFIG_REFERENCE.md
# ==============================================================
from __future__ import annotations

import argparse
import ast
import importlib
import re
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEFAULT_OUT = REPO.parent / "02_Configuration_Management" / "02_ファイル構成" / "CONFIG_REFERENCE.md"
NAME_RE = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
SECRET_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.I)
BLOCK_RE = re.compile(r"^#\s*(?:★\s*)?(BLOCK\s*[^:：]+)[:：]\s*(.*)$")


def _blocks_and_comments(lines: list[str]):
    """行番号→(所属BLOCK見出し, 直前のコメント)。"""
    block, out = "（BLOCK見出しなし）", {}
    comment: list[str] = []
    for i, line in enumerate(lines, 1):
        st = line.strip()
        m = BLOCK_RE.match(st)
        if m:
            block = f"{m.group(1).strip()}: {m.group(2).strip()}".rstrip(": ")
            comment = []
            continue
        if st.startswith("#"):
            text = st.lstrip("#").strip(" ─=-")
            if text:
                comment.append(text)
            continue
        out[i] = (block, comment[-1] if comment else "")
        if st:
            comment = []
    return out


def _references(names: set[str]) -> dict[str, dict[str, list[str]]]:
    """各設定名が出現するファイル（本体/テスト）。単語境界での出現＝近似。"""
    refs = {n: {"本体": [], "テスト": []} for n in names}
    word = re.compile(r"\b[_A-Z][A-Z0-9_]{2,}\b")
    for f in sorted(REPO.rglob("*.py")):
        rel = f.relative_to(REPO).as_posix()
        if rel == "config.py" or rel.startswith(("stock_data/", ".")) or "/." in rel:
            continue
        try:
            found = set(word.findall(f.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue
        kind = "テスト" if rel.startswith("tests/") else "本体"
        for n in found & names:
            refs[n][kind].append(rel)
    return refs


def _fmt_value(name: str, value) -> str:
    if SECRET_RE.search(name):
        return "（機密・実値非掲載）: " + ("設定あり" if value else "未設定")
    text = repr(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 70 else text[:67] + "…"


def build(out: Path = DEFAULT_OUT) -> int:
    src = (REPO / "config.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    meta = _blocks_and_comments(lines)
    tree = ast.parse(src)
    entries = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        for name in targets:
            if NAME_RE.match(name):
                entries.append((name, node.lineno))
    cfg = importlib.import_module("config")
    names = {n for n, _ in entries}
    refs = _references(names)

    by_block: dict[str, list[str]] = {}
    seen = set()
    for name, line in entries:
        if name in seen:
            continue
        seen.add(name)
        block, comment = meta.get(line, ("（BLOCK見出しなし）", ""))
        value = getattr(cfg, name, None)
        r = refs[name]
        ref = "; ".join(
            f"{k}: " + ", ".join(f"`{x}`" for x in v[:6]) + (f" 他{len(v) - 6}" if len(v) > 6 else "")
            for k, v in r.items() if v) or "静的参照なし（外部・動的参照候補）"
        desc = (comment or block.split(": ", 1)[-1]).replace("|", "\\|")[:90]
        by_block.setdefault(block, []).append(
            f"| `{name}` | `{type(value).__name__}` | `{_fmt_value(name, value)}` | {desc} | {ref} | {line} |")

    today = date.today().isoformat()
    head = [
        f"<!-- 最終更新日: {today} -->",
        f"<!-- 生成: 01_System/config_reference_gen.py（{today}）。手で編集せず、config.py を直してから再生成する。 -->",
        "# CONFIG_REFERENCE — config.py 全設定一覧", "",
        "`01_System/config.py` を正本として、モジュールレベルの定数・設定変数を機械抽出した参照表。",
        "本書は値の変更場所を探すための索引であり、設定変更は必ず `config.py` 側で行う。", "",
        "## 収録基準", "",
        f"- 収録数: **{len(seen)} 件**（英大文字・数字・アンダースコアで構成されるモジュールレベル代入名）",
        f"- 現在値: {today} 時点の checkout で評価（Path などの派生値も実効値）",
        "- 機密保護: 名前に KEY/SECRET/TOKEN/PASSWORD/CREDENTIAL を含むものは実値を載せず設定有無のみ",
        "- 機能: 定義行の直前コメント（無ければ所属BLOCKの見出し）",
        "- 参照元: 各 .py に設定名が単語として出現するか（近似）。「静的参照なし」は即削除可を意味しない",
        "", "## 変更時の検証", "", "```bash",
        "cd ~/Documents/Obsidian_Local/Stock_Analyzer/01_System",
        "python3 config_audit.py", "python3 pipeline_registry_gen.py --audit",
        "python3 results_schema.py --audit", "python3 -m pytest tests/ -q",
        "python3 config_reference_gen.py   # 本書の再生成", "```", ""]
    body = []
    for block, rows in by_block.items():
        body += [f"## {block}", "", "| 設定名 | 型 | 現在値 | 機能 | 参照元 | 定義行 |",
                 "|---|---|---|---|---|---:|", *rows, ""]
    out.write_text("\n".join(head + body), encoding="utf-8")
    print(f"✅ CONFIG_REFERENCE.md 再生成: {len(seen)}件 / {len(by_block)}ブロック → {out}")
    return len(seen)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    build(Path(ap.parse_args().out))
