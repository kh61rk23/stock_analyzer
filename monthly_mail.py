# ==============================================================
# monthly_mail.py — 月次レポートをまとめてメールで送る
#
# monthly_runner.py の最後のステップ（monthly_mail）として月初3日以内に1回だけ動く。
# 対象月（前月）の各レポート md を1通にまとめて Gmail で送る:
#   月次戦略レビュー（E12）/ シグナル淘汰（E4）/ ペーパー取引の月次成績（S2）/
#   資産配分の提案（S11）/ コモディティ配分（S6・当月分）/ 税・NISA（S10・実施月のみ）
# 送信先・認証は日次メールと同じ（ALERT_GMAIL_FROM / ALERT_GMAIL_PASSWORD / ALERT_GMAIL_TO）。
# 送るのは GitHub Actions だけ。monthly_runner は Mac（司令塔シェル invest-monthly）からも起動されるが、
# Mac の月次の状態ファイルは Actions とは別なので、Mac でも送ると同じ月の月次メールが2通になる。
# 実行: python3 monthly_mail.py <YYYYMM> [--dry-run] [--send-anyway]
# ==============================================================
from __future__ import annotations

import argparse
import html
import os
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:])
    return f"{y + (m == 12)}{(m % 12) + 1:02d}"


def report_files(insights: Path, ym: str, tax_months: tuple = (1, 4, 7, 10, 11)) -> list[tuple[str, Path]]:
    """対象月のレポート（題名, パス）。存在するものだけ。

    税・NISA（tax_report_<年>.md）は年に1本を上書きするファイルで、tax_optimizer が動くのは
    TAX_OPTIMIZER_MONTHS の月だけ。それ以外の月に同封すると前回の古い版を新しいレポートとして送るので、
    送る月（対象月の翌月）が実施月のときだけ入れる。"""
    nm = _next_month(ym)
    cands = [
        ("📋 月次戦略レビュー（E12）", insights / f"strategy_review_{ym}.md"),
        ("✂️ シグナル淘汰（E4）", insights / f"signal_culling_{ym}.md"),
        ("📒 ペーパー取引の月次成績（S2）", insights / f"paper_{ym}.md"),
        ("⚖️ 資産配分の提案（S11）", insights / f"allocation_proposal_{ym}.md"),
        ("🛢️ コモディティ配分（S6・当月）", insights / f"commodity_alloc_{nm}.md"),
    ]
    if int(nm[4:]) in tuple(tax_months):
        cands.append(("🧾 税・NISA（S10）", insights / f"tax_report_{nm[:4]}.md"))
    return [(t, p) for t, p in cands if p.exists()]


def md_to_html(md: str) -> str:
    """最低限の Markdown → HTML（見出し・表・箇条書き・引用・強調）（純関数）。"""
    out, in_table = [], False
    for raw in md.split("\n"):
        line = raw.rstrip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c or "--") for c in cells):
                continue
            if not in_table:
                out.append('<table style="border-collapse:collapse;font-size:13px">')
                in_table = True
            out.append("<tr>" + "".join(
                f'<td style="border:1px solid #ddd;padding:3px 6px">{_inline(c)}</td>' for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if not line.strip():
            out.append("<br>")
        elif line.startswith("#"):
            lvl = min(4, len(line) - len(line.lstrip("#")) + 1)
            out.append(f"<h{lvl}>{_inline(line.lstrip('#').strip())}</h{lvl}>")
        elif line.lstrip().startswith(("- ", "* ")):
            out.append(f"<div>・{_inline(line.lstrip()[2:])}</div>")
        elif line.startswith(">"):
            out.append(f'<div style="color:#666">{_inline(line[1:].strip())}</div>')
        else:
            out.append(f"<div>{_inline(line)}</div>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    # レポートは強調に *…* も使う（signal_culling・tax 等）。** を先に処理してから単独の * を斜体に
    return re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)


def build(ym: str, insights: Path, tax_months: tuple = (1, 4, 7, 10, 11), late_note: str = "",
          gave_up: list | None = None) -> tuple[str, str, int]:
    files = report_files(insights, ym, tax_months)
    subject = f"📅 月次レポート {ym[:4]}-{ym[4:]}（{len(files)}本）" + ("（遅れて送信）" if late_note else "")
    parts = [f"<h2>📅 月次レポート {ym[:4]}年{int(ym[4:])}月</h2>",
             "<div>月次処理（monthly_runner）が作成した各レポートです。数値の一次情報は各元ファイルを正本とします。</div>",
             *([f'<div style="color:#b45309">⚠️ {html.escape(late_note)}</div>'] if late_note else []),
             *([f'<div style="color:#b91c1c">❌ 次のステップは失敗が続いたため今月は作れていません（レポートに含まれない）: '
                f'{html.escape(", ".join(gave_up))}</div>'] if gave_up else []),
             "<ul>" + "".join(f"<li>{html.escape(t)}</li>" for t, _ in files) + "</ul>"]
    for title, p in files:
        try:
            body = p.read_text(encoding="utf-8")
        except OSError:
            continue
        try:   # 元ファイルへのリンク（GitHub・Drive検索）
            import file_links as fl
            link = (f' — <a href="{html.escape(fl.github_url(p))}">GitHub</a>'
                    f' / <a href="{html.escape(fl.drive_search_url(p.name))}">Drive</a>')
        except Exception:
            link = ""
        parts.append(f'<hr><h2>{html.escape(title)}</h2>'
                     f'<div style="font-size:12px;color:#888">{p.name}{link}</div>')
        parts.append(md_to_html(body))
    if not files:
        parts.append("<div>⚠️ 対象月のレポートが見つかりませんでした。</div>")
    return subject, "\n".join(parts), len(files)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("month", help="対象月 YYYYMM")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--send-anyway", action="store_true", help="GitHub Actions の外でも送る（手元での試し送信用）")
    ap.add_argument("--gave-up", default="", help="monthly_runner が諦めたステップ（カンマ区切り）。本文の冒頭に出す")
    a = ap.parse_args(argv)
    try:
        sys.path.insert(0, str(REPO))
        import config as cfg
        insights = Path(getattr(cfg, "INSIGHT_DIR", REPO / "stock_data" / "insights"))
    except Exception:
        cfg, insights = None, REPO / "stock_data" / "insights"
    tax_months = tuple(getattr(cfg, "TAX_OPTIMIZER_MONTHS", (1, 4, 7, 10, 11)))
    today = date.today()
    max_day = int(getattr(cfg, "MONTHLY_RUN_MAX_DAY", 3))
    late = (f"月初{max_day}日までに送れず、{today.month}月{today.day}日に追いつきで送っています"
            if today.strftime("%Y%m") == _next_month(a.month) and today.day > max_day else "")
    subject, body, n = build(a.month, insights, tax_months, late,
                             [x for x in a.gave_up.split(",") if x.strip()])
    print(f"📅 月次メール: {subject}")
    if a.dry_run:
        return 0
    if n == 0:
        # 空のメールを送って「成功」にすると、月次が全部失敗した月に気づけない
        print("  ❌ 対象月のレポートが1本も無いので送らない（月次の各ステップを確認）")
        return 1
    if os.environ.get("GITHUB_ACTIONS") != "true" and not a.send_anyway:
        print("  ⏩ GitHub Actions の外なので送らない（月次メールは Actions が送る・試すときは --send-anyway）")
        return 0
    from alert_notifier import send_gmail
    frm = os.environ.get("ALERT_GMAIL_FROM") or getattr(cfg, "ALERT_GMAIL_FROM", "")
    pw = os.environ.get("ALERT_GMAIL_PASSWORD") or getattr(cfg, "ALERT_GMAIL_PASSWORD", "")
    to = os.environ.get("ALERT_GMAIL_TO") or getattr(cfg, "ALERT_GMAIL_TO", "")
    ok, err = send_gmail(frm, pw, to, subject, body)
    print("  ✅ 送信" if ok else f"  ❌ 送信失敗: {err}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
