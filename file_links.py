# ==============================================================
# file_links.py — メール末尾の「本日の保存ファイル」一覧
#
# 保存ファイル（stock_data/ 配下）は公開しないので git には無い。置き場は BigQuery の private.state_files で、
# md の全文は report_store が analytics.report_texts に入れる（種類・日付で検索できる）。メールには名前だけを並べる。
# github_url は git にあるファイル（コード等）を指すときだけ使う。
# ==============================================================
from __future__ import annotations

import html
import os
from datetime import date
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent
GITHUB_BLOB = os.environ.get("STOCK_ANALYZER_GITHUB_BLOB",
                             "https://github.com/kh61rk23/stock_analyzer/blob/main/")


def github_url(path: Path | str) -> str:
    p = Path(path)
    try:
        rel = p.resolve().relative_to(REPO)
    except ValueError:
        rel = p
    return GITHUB_BLOB + quote(rel.as_posix())


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


def files_section_html(paths: list[Path]) -> str:
    """メール末尾の「保存ファイル」節（名前の一覧）。"""
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
            rows.append(f'<li>{html.escape(p.parent.name)}/（本日{per_dir[p.parent]}件）</li>')
            continue
        rows.append(f'<li>{html.escape(p.name + ("/" if p.is_dir() else ""))}</li>')
    return (f'<div style="padding:12px 16px;font-size:12px;color:#374151;">'
            f'<div style="font-weight:bold;margin-bottom:4px;">📎 本日の保存ファイル</div>'
            f'<ul style="margin:0;padding-left:18px;">{"".join(rows)}</ul>'
            f'<div style="color:#9ca3af;margin-top:4px;">md の全文は BigQuery analytics.report_texts'
            f'（kind・report_date で検索）にある。</div></div>')
