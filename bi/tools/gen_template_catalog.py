"""BIテンプレートの一覧表（TEMPLATE_CATALOG.md）を bi/templates/*.json から生成する。

資料側（02_Configuration_Management/06_BI基盤/TEMPLATE_CATALOG.md）は手で書かず、このスクリプトの出力を置く。
テンプレを足した・直したら再実行する（読むだけ。テンプレ JSON や stock_data は変更しない）。
使い方: python3 bi/tools/gen_template_catalog.py            … 既定の置き場へ書く
        python3 bi/tools/gen_template_catalog.py --out -    … 標準出力へ
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = ROOT / "bi" / "templates"
DEFAULT_OUT = ROOT.parent / "02_Configuration_Management" / "06_BI基盤" / "TEMPLATE_CATALOG.md"

# 領域の表示順・日本語名。テンプレが無い領域は「枠だけ」の行を出す（状態は STATUS に書く）
DOMAINS = [
    ("finance", "① 財務・業績"),
    ("geo", "② 地政学"),
    ("realestate", "② 不動産"),
    ("taxonomy", "③ 技術分類"),
    ("theme", "④ テーマ株"),
    ("screener", "⑤ 企業スクリーナー"),
    ("screener_us", "⑥ 米国株スクリーナー"),
    ("market", "⑦ 株価・市場（月次・業種・市場の広がり）"),
    ("us", "⑧ 米国株（通期財務・セグメント）"),
    ("research", "⑨ 論文（arXiv）"),
    ("macro", "⑩ マクロ・業種レポート"),
    ("news", "⑪ ニュース・テーマETF"),
]
# 実装中の領域は表を出さず枠だけ置く（未コミットの試作を資料に載せない）。完成してコミットしたらここから外して再生成する
IN_PROGRESS: set[str] = set()
STATUS: dict[str, str] = {}


def md(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def fmt_param(p: dict) -> str:
    rng = ""
    if "min" in p or "max" in p:
        rng = f" [{p.get('min', '')}〜{p.get('max', '')}]"
    return f"`{p['name']}`:{p['type']}={p.get('default')}{rng}"


def fmt_chart(c: dict) -> str:
    y = c.get("y")
    ys = ", ".join(y) if isinstance(y, list) else (y or "")
    s = f"{c.get('type')}（x=`{c.get('x')}`, y=`{ys}`"
    if c.get("series"):
        s += f", series=`{c['series']}`"
    return s + "）"


def load() -> list[dict]:
    out: list[dict] = []
    for f in sorted(TEMPLATE_DIR.glob("*.json")):
        out.extend(json.loads(f.read_text(encoding="utf-8")))
    return out


def render(all_templates: list[dict]) -> str:
    wip = [t for t in all_templates if t["domain"] in IN_PROGRESS]
    wip_marts = {t["mart"] for t in wip}
    templates = [t for t in all_templates if t["domain"] not in IN_PROGRESS]
    by_dom: dict[str, list[dict]] = {}
    for t in templates:
        by_dom.setdefault(t["domain"], []).append(t)
    marts = sorted({t["mart"] for t in templates})
    L = [
        "# TEMPLATE_CATALOG.md",
        "",
        "> **このファイルは生成物です。手で編集しないでください。** 正本は `01_System/bi/templates/*.json`。",
        "> 再生成: `cd 01_System && python3 bi/tools/gen_template_catalog.py`（生成スクリプト: `bi/tools/gen_template_catalog.py`）。",
        f"> 生成日: {date.today().isoformat()} ／ テンプレ {len(templates)} 本 ／ 使用マート {len(marts)} 本",
        "",
        "書き方・方言ルールは `bi/templates/_schema.md`、全体像は `SPEC_bi_core.md`、マートの列は `SPEC_bi_marts.md` を参照。",
        "",
        "## 0. 領域別の本数",
        "",
        "| 領域 | domain | 本数 | 状態 |",
        "|---|---|---:|---|",
    ]
    for key, label in DOMAINS:
        n = len(by_dom.get(key, []))
        st = "稼働中" if n else STATUS.get(key, "未着手")
        L.append(f"| {label} | `{key}` | {n if n else '-'} | {st} |")
    for key in by_dom:
        if key not in dict(DOMAINS):
            L.append(f"| （未登録の領域） | `{key}` | {len(by_dom[key])} | DOMAINS に追加してください |")
    if wip:
        L += ["", "- 実装中の領域は表を出さず枠だけ置いてある。完成後に生成スクリプトの `IN_PROGRESS` から外して再生成すると表が出る。"]
    L.append("")
    n = 0
    for key, label in DOMAINS:
        items = by_dom.get(key, [])
        n += 1
        L += [f"## {n}. {label}（`{key}`・{len(items)}本）", ""]
        if not items:
            L += [f"{STATUS.get(key, '未着手')}。", ""]
            continue
        L += ["| id | タイトル | 問い（question） | マート | パラメータ（名:型=既定 [範囲]） | グラフ |", "|---|---|---|---|---|---|"]
        for t in items:
            ps = "<br>".join(fmt_param(p) for p in t["params"]) or "なし"
            L.append(f"| `{t['id']}` | {md(t['title'])} | {md(t['question'])} | `{t['mart']}` | {ps} | {md(fmt_chart(t['chart']))} |")
        L += ["", "読み方の注意（テンプレの `note`）:", ""]
        for t in items:
            L.append(f"- `{t['id']}`: {md(t.get('note'))}")
        L.append("")
    L += ["## マート別の使われ方", "", "| マート | 使うテンプレ数 | テンプレ id |", "|---|---:|---|"]
    for m in marts:
        ids = [t["id"] for t in templates if t["mart"] == m]
        L.append(f"| `{m}` | {len(ids)} | {', '.join(f'`{i}`' for i in ids)} |")
    unused = sorted(p.stem for p in (ROOT / "bi" / "marts").glob("*.sql")
                    if p.stem not in marts and p.stem not in wip_marts)
    if unused:
        L += ["", f"テンプレから参照されていないマート: {', '.join(f'`{m}`' for m in unused)}（自由分析・SQL欄・「Claudeに聞く」では使える）"]
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="書き出し先（- で標準出力）")
    args = ap.parse_args(argv)
    text = render(load())
    if args.out == "-":
        sys.stdout.write(text)
    else:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"{p}  ({len(text.splitlines())}行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
