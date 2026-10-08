# ==============================================================
# file_links.py — メールから保存ファイル（GitHub・Google Drive）へ飛ぶリンク
#
# 日次メールは解析の途中で送られ、Google Drive への同期（run_headless の最後）はその後なので、
# 送信時点ではDriveのファイルIDがまだ決まらない。そこでリンクは次の2つにする:
#   GitHub : https://github.com/<repo>/blob/main/<repoからの相対パス>（同じジョブの最後にpushされる・mdは整形表示）
#   Drive  : ファイル名でのDrive検索URL（IDが無くても開ける）＋ 同期先フォルダ（GOOGLE_DRIVE_FOLDER_ID）
# ==============================================================
from __future__ import annotations

import html
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent
GITHUB_BLOB = os.environ.get("STOCK_ANALYZER_GITHUB_BLOB",
                             "https://github.com/kh61rk23/stock_analyzer/blob/main/")
_FILE_RE = re.compile(r"(?<![\w/.-])([\w.-]+_\d{4}-?\d{2}(?:-?\d{2})?[\w.-]*\.(?:md|csv|xlsx|json|html))")


def github_url(path: Path | str) -> str:
    p = Path(path)
    try:
        rel = p.resolve().relative_to(REPO)
    except ValueError:
        rel = p
    return GITHUB_BLOB + quote(rel.as_posix())


def drive_search_url(name: str) -> str:
    return "https://drive.google.com/drive/search?q=" + quote(f'"{name}"')


def drive_folder_url() -> str:
    fid = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    return f"https://drive.google.com/drive/folders/{fid}" if fid else ""


def todays_files(insight_dir: Path | str, today: date | None = None, limit: int = 400) -> list[Path]:
    """名前に今日の日付（YYYY-MM-DD / YYYYMMDD）を含む保存ファイル（新しい順）。"""
    today = today or date.today()
    keys = (today.isoformat(), today.strftime("%Y%m%d"))
    base = Path(insight_dir)
    if not base.exists():
        return []
    out = []
    for f in base.rglob("*"):
        if f.is_file() and any(k in f.name or k in f.parent.name for k in keys):
            # 銘柄別の詳細（detail_YYYY-MM-DD/xxxx.md）は数が多いのでフォルダ単位で1件にまとめる
            out.append(f.parent if f.parent.name.startswith("detail_") else f)
    uniq = sorted(set(out), key=lambda p: (p.is_dir(), str(p)))
    return uniq[:limit]


def index_files(paths: list[Path]) -> dict[str, Path]:
    return {p.name: p for p in paths}


def linkify(body_html: str, index: dict[str, Path]) -> str:
    """本文中のファイル名（例 screener_2026-10-06.md）をGitHubへのリンクにする（純関数）。"""
    def _sub(m):
        name = m.group(1)
        p = index.get(name)
        if not p:
            return name
        return f'<a href="{html.escape(github_url(p))}">{html.escape(name)}</a>'
    return _FILE_RE.sub(_sub, body_html)


def files_section_html(paths: list[Path]) -> str:
    """メール末尾の「保存ファイル」節（GitHub・Drive検索のリンク付き一覧）。"""
    if not paths:
        return ""
    # 同じフォルダに本日分が多い（業種別レポート33件など）ときはフォルダ1行にまとめる
    from collections import Counter
    per_dir = Counter(p.parent for p in paths if p.is_file())
    rows, shown_dirs = [], set()
    for p in paths:
        if p.is_file() and per_dir[p.parent] > 5:
            if p.parent in shown_dirs:
                continue
            shown_dirs.add(p.parent)
            gh = github_url(p.parent).replace("/blob/", "/tree/")
            rows.append(f'<li>{html.escape(p.parent.name)}/（本日{per_dir[p.parent]}件） — '
                        f'<a href="{html.escape(gh)}">GitHub</a></li>')
            continue
        name = p.name + ("/" if p.is_dir() else "")
        gh = github_url(p).replace("/blob/", "/tree/") if p.is_dir() else github_url(p)
        rows.append(
            f'<li>{html.escape(name)} — <a href="{html.escape(gh)}">GitHub</a>'
            f' / <a href="{html.escape(drive_search_url(p.name))}">Drive</a></li>')
    folder = drive_folder_url()
    head = "📎 本日の保存ファイル"
    if folder:
        head += f'（<a href="{html.escape(folder)}">Driveの保存先フォルダ</a>）'
    return (f'<div style="padding:12px 16px;font-size:12px;color:#374151;">'
            f'<div style="font-weight:bold;margin-bottom:4px;">{head}</div>'
            f'<ul style="margin:0;padding-left:18px;">{"".join(rows)}</ul>'
            f'<div style="color:#9ca3af;margin-top:4px;">GitHubは実行の最後にpushされてから・'
            f'Driveは同期の後（実行終了から数分）に開けます。</div></div>')
