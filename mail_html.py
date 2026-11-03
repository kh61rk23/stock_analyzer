# ==============================================================
# mail_html.py — 週次・月次メール共通の HTML 部品
#
# 日次メール（alert_notifier.build_message）と同じ見た目（青のグラデーション見出し・
# 幅 640px の白いカード・重要度ごとの左罫線）で、週次・月次の本文を組み立てる。
#   page()         : 見出し + KPI 帯 + 本文 + フッターの1ページ
#   section_card() : 見出し付きの節カード
#   md_to_html()   : Markdown → メール向け HTML（週次・月次で共有する唯一の変換器）
#   strip_dev_codes() / relabel() : 画面に出す前に内部の開発コード・内部ラベルを落とす
#
# メール用の制約: Gmail は 102KB を超える本文を途中で切る。そのためスタイルは短い
# インライン指定にし、flex は使わず table でレイアウトする（Gmail で崩れやすい）。
# ==============================================================
from __future__ import annotations

import html
import re

NAVY = "#1e3a5f"
BLUE = "#2563eb"
SEVERITY_COLOR = {"high": "#ef4444", "medium": "#f59e0b", "info": "#3b82f6"}

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Hiragino Sans',Meiryo,sans-serif"

# 数値セルの判定。先頭の増減記号・通貨記号と末尾の単位を許す（右寄せにする）
_NUM_RE = re.compile(
    r"^[+\-−▲△▼↑↓→]?[¥$￥]?\d[\d,]*(?:\.\d+)?\s*"
    r"(?:%|x|倍|pt|点|円|億円|万円|兆円|社|件|銘柄|週|日|年|ヶ月|▼)?$"
)

# 内部の開発コード（Wave 番号・案件コード）。括弧内のコードだけを落とし、残る説明は活かす
_DEV_TOKEN = (r"(?:Wave\s*\d+(?:\s*-\s*[A-Z](?![A-Za-z0-9-]))?|第\d+セッション|(?<![A-Za-z0-9])[A-Z]{1,4}-[A-Z0-9]{1,2}(?![A-Za-z0-9])"
              r"|(?<![A-Za-z0-9])[A-Z]\d{1,2}(?![A-Za-z0-9])|B部門)")
_DEV_SEP = r"[\s/\-–・:：]*"
_DEV_TOKEN_RE = re.compile(_DEV_TOKEN + _DEV_SEP)
_PAREN_RE = re.compile(r"[（(]([^（）()]*)[）)]")
_DEV_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,4}-\d+[:：]\s*")

# 内部ラベル → 表示名
_LABELS = (("↑DYNAMIC", "↑自動追加"), ("↓DYNAMIC", "↓自動追加"), ("→DYNAMIC", "→自動追加"),
           ("DYNAMIC", "自動追加"), ("動的追加", "自動追加"))


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def strip_dev_codes(text: str) -> str:
    """見出しなどから開発コード（Wave 7・FE-1・C-1/C-2・E3 等）を落とす。

    括弧の中身がコードだけなら括弧ごと消し、説明が残るなら説明だけ残す。"""
    def _cb(m):
        inner = m.group(1)
        if not _DEV_TOKEN_RE.search(inner):
            return m.group(0)
        rest = _DEV_TOKEN_RE.sub("", inner).strip(" -–/・:：")
        return f"（{rest}）" if rest else ""
    out = _PAREN_RE.sub(_cb, text)
    out = _DEV_PREFIX_RE.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def relabel(text: str) -> str:
    """内部ラベル（動的追加・DYNAMIC）を読み手向けの語に置き換える。"""
    for a, b in _LABELS:
        text = text.replace(a, b)
    return text


def fmt_yen(v) -> str:
    """円の金額を 億円 / 万円 / 円 に整える（数値でなければそのまま返す）。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v) if v not in (None, "") else "—"
    if n >= 1e8:
        x = n / 1e8
        return f"{x:,.0f}億円" if x >= 100 or x == int(x) else f"{x:,.1f}億円"
    if n >= 1e4:
        x = n / 1e4
        return f"{x:,.0f}万円" if x == int(x) else f"{x:,.1f}万円"
    return f"{n:,.0f}円"


# ---------------------------------------------------------------- inline
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _inline(s: str) -> str:
    """エスケープしたうえで `code` / **太字** / *斜体* / [リンク](url) を変換する。"""
    s = esc(s)
    s = re.sub(r"`([^`]+)`",
               r'<code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;font-size:92%">\1</code>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = _LINK_RE.sub(r'<a href="\2" style="color:#2563eb">\1</a>', s)
    return s


def _is_num(cell: str) -> bool:
    c = re.sub(r"\*\*|<[^>]+>", "", cell).strip()
    return bool(c) and bool(_NUM_RE.match(c))


# ---------------------------------------------------------------- md → html
_P = 'style="margin:3px 0;font-size:13px;line-height:1.65;color:#374151"'
_TD = "padding:4px 6px;border-bottom:1px solid #eee;"


def _render_table(rows: list) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    cells = [r for r in cells if not all(re.fullmatch(r":?-{2,}:?|", c) for c in r)]
    if not cells:
        return ""
    head, body = cells[0], cells[1:]
    ncol = len(head)
    aligns = []
    for j in range(ncol):
        col = [r[j] for r in body if j < len(r) and r[j].strip()]
        aligns.append(bool(col) and all(_is_num(c) for c in col))
    out = ['<div style="overflow-x:auto;margin:6px 0"><table style="border-collapse:collapse;width:100%;font-size:12px">',
           '<thead><tr style="background:#eff6ff">']
    for j, c in enumerate(head):
        al = "right" if aligns[j] else "left"
        out.append(f'<th style="padding:4px 6px;border-bottom:2px solid #bfdbfe;text-align:{al};'
                   f'color:#1e3a5f;white-space:nowrap">{_inline(relabel(c))}</th>')
    out.append("</tr></thead><tbody>")
    for i, r in enumerate(body):
        bg = ' style="background:#f9fafb"' if i % 2 else ""
        out.append(f"<tr{bg}>")
        for j in range(ncol):
            c = r[j] if j < len(r) else ""
            al = "text-align:right;white-space:nowrap" if aligns[j] else ""
            out.append(f'<td style="{_TD}{al}">{_inline(relabel(c))}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def md_to_html(md: str) -> str:
    """Markdown → メール向け HTML（純関数）。

    見出し(# ～ ####)・表・箇条書き・引用・水平線・太字/斜体/コード/リンクを扱う。
    本文は必ずエスケープする。空行は詰める（余白は各要素の margin で取る）。
    "- [ ]" は ☐、"- [x]" は ☑ にする。"""
    out: list = []
    lines = md.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip()
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            j = i + 1
            buf = []
            while j < n and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            out.append('<pre style="background:#f3f4f6;padding:8px;border-radius:4px;font-size:11px;'
                       f'overflow-x:auto;white-space:pre-wrap">{esc(chr(10).join(buf))}</pre>')
            i = j + 1
            continue
        if s.startswith("|"):
            j = i
            while j < n and lines[j].strip().startswith("|"):
                j += 1
            out.append(_render_table(lines[i:j]))
            i = j
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
            out.append('<hr style="border:none;border-top:1px solid #e5e7eb;margin:12px 0">')
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl = len(m.group(1))
            txt = _inline(relabel(m.group(2)))
            if lvl <= 3:
                out.append(f'<h4 style="color:#2563eb;margin:14px 0 6px;font-size:14px">{txt}</h4>')
            else:
                out.append(f'<h5 style="color:#1e3a5f;margin:10px 0 4px;font-size:13px">{txt}</h5>')
            i += 1
            continue
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(_inline(relabel(lines[i].strip().lstrip(">").strip())))
                i += 1
            buf = [b for b in buf if b]
            out.append('<blockquote style="border-left:3px solid #93c5fd;margin:6px 0;padding:4px 10px;'
                       'color:#374151;background:#f0f7ff;font-size:12px;line-height:1.6">'
                       + "<br>".join(buf) + "</blockquote>")
            continue
        if re.match(r"^[-*+]\s+", s):
            items = []
            while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                t = re.sub(r"^\s*[-*+]\s+", "", lines[i]).rstrip()
                t = re.sub(r"^\[ \]\s*", "☐ ", t)
                t = re.sub(r"^\[[xX]\]\s*", "☑ ", t)
                items.append(f'<li style="margin:2px 0;font-size:13px;line-height:1.6;color:#374151">'
                             f"{_inline(relabel(t))}</li>")
                i += 1
            out.append('<ul style="margin:4px 0;padding-left:20px">' + "".join(items) + "</ul>")
            continue
        out.append(f"<p {_P}>{_inline(relabel(s))}</p>")
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------- 部品
def section_card(title: str, body_html: str, severity: str | None = None) -> str:
    """節カード。severity を渡すと日次メールと同じ色の左罫線と見出し色になる。"""
    color = SEVERITY_COLOR.get(severity or "", "")
    border = f"border-left:3px solid {color};" if color else "border-left:3px solid #dbeafe;"
    tcolor = color or NAVY
    return (f'<div style="margin:12px 0;padding:2px 12px 8px;{border}background:#fff">'
            f'<div style="color:{tcolor};font-weight:bold;font-size:15px;margin:8px 0 4px">{esc(title)}</div>'
            f"{body_html}</div>")


def _kpi_strip(kpis: list) -> str:
    """kpis = [(ラベル, 値, 色), ...] を table の1行にする（flex は使わない）。"""
    if not kpis:
        return ""
    w = f"{100 // len(kpis)}%"
    cells = []
    for k in kpis:
        label, value = k[0], k[1]
        color = k[2] if len(k) > 2 and k[2] else BLUE
        cells.append(
            f'<td class="kpi" width="{w}" style="padding:10px 6px;text-align:center;border-right:1px solid #dbeafe;'
            f'vertical-align:top"><div style="font-size:11px;color:#6b7280">{esc(label)}</div>'
            f'<div style="font-size:14px;font-weight:bold;color:{color};word-break:break-all">{esc(value)}</div></td>')
    return ('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;background:#f0f7ff;border-bottom:1px solid #dbeafe"><tr>'
            + "".join(cells) + "</tr></table>")


def page(title: str, subtitle: str, kpis: list, body_html: str, footer_html: str = "") -> str:
    """日次メールと同じ外枠（グラデーション見出し・KPI 帯・白カード・フッター）。"""
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
@media only screen and (max-width:480px){{
 .wrap{{padding:8px !important}}
 .body{{padding:4px 12px 16px !important}}
 .kpi{{display:inline-block !important;width:50% !important;box-sizing:border-box;border-right:none !important;border-bottom:1px solid #dbeafe}}
}}
</style></head>
<body style="font-family:{_FONT};background:#f9fafb;margin:0;padding:0">
<div class="wrap" style="padding:16px">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1);overflow:hidden">
<div style="background:#1e3a5f;background:linear-gradient(135deg,{NAVY},{BLUE});padding:20px 24px;color:#fff">
<h2 style="margin:0;font-size:18px;color:#fff">{esc(title)}</h2>
<p style="margin:6px 0 0;font-size:12px;opacity:.85;color:#fff">{esc(subtitle)}</p>
</div>
{_kpi_strip(kpis)}
<div class="body" style="padding:4px 24px 20px">
{body_html}
</div>
<div style="padding:14px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;font-size:11px;color:#9ca3af;line-height:1.6">
{footer_html}
</div>
</div>
</div>
</body></html>
"""
