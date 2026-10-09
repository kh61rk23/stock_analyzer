"""Claudeダッシュボード（claude.ai の Artifact）用のページを組み立てる。

bi/marts/*.sql と bi/templates/*.json、stock_data/bi/manifest.json の列定義を template.html に埋め込み、
bi/dashboard/株式DWH分析室.html を書き出す。ページは閲覧者の BigQuery コネクタで毎回 SQL を実行するため、
ここにはデータを含めない（SQL とテンプレと列名だけ）。テンプレやマートを変えたら再実行して再公開する。
使い方: python3 bi/dashboard/build_claude_dashboard.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BI = ROOT / "bi"
OUT = Path(__file__).with_name("株式DWH分析室.html")
PROJECT = "stock-analyzer-edinet"

DOMAINS = {"finance": "財務・業績", "geo": "地政学", "realestate": "不動産", "taxonomy": "技術分類",
           "theme": "テーマ株", "market": "株価・スクリーニング", "us": "米国株"}
MART_DESC = {"m_company": "上場会社マスタ。1社1行", "m_fin_annual": "年次財務。1社1年度1行（fiscal_year＝決算期末の年）"}
LABELS = {
    "edinet_code": "EDINETコード", "sec_code4": "証券コード", "name": "社名", "industry": "業種",
    "fiscal_year": "年度", "fiscal_year_end": "決算期末", "net_sales": "売上高", "operating_income": "営業利益",
    "ordinary_income": "経常利益", "net_income": "純利益", "total_assets": "総資産", "equity": "自己資本",
    "interest_bearing_debt": "有利子負債", "cash_and_equiv": "現金同等物", "operating_cf": "営業CF",
    "investing_cf": "投資CF", "free_cf": "フリーCF", "employee_count": "従業員数", "avg_annual_salary": "平均年収",
    "capex": "設備投資", "depreciation": "減価償却費", "roe": "ROE", "roa": "ROA", "operating_margin": "営業利益率",
    "gross_profit": "売上総利益", "sga": "販管費", "rnd_expenses": "研究開発費", "cost_of_sales": "売上原価",
    "goodwill": "のれん", "inventories": "棚卸資産", "trade_receivables": "売上債権", "total_liabilities": "負債合計",
    "net_assets": "純資産", "shareholders_equity": "株主資本", "eps": "EPS", "bps": "BPS",
    "dividend_per_share": "1株配当", "payout_ratio": "配当性向", "per": "PER", "pbr": "PBR",
    "num_employees": "従業員数（細目）", "female_manager_ratio": "女性管理職比率", "avg_tenure_years": "平均勤続年数",
    "cash_dividends_paid": "支払配当金", "gross_margin": "粗利率", "sga_ratio": "販管費率", "rnd_ratio": "研究開発費率",
    "equity_ratio": "自己資本比率", "net_margin": "純利益率", "accounting_standard": "会計基準",
    "listing_status": "上場状態", "is_delisted": "上場廃止",
}


def main() -> Path:
    manifest = json.loads((ROOT / "stock_data" / "bi" / "manifest.json").read_text(encoding="utf-8"))
    cols = {m["mart"]: m["columns"] for m in manifest["marts"]}
    marts = {}
    for f in sorted((BI / "marts").glob("*.sql")):
        name = f.stem
        marts[name] = {"sql": f.read_text(encoding="utf-8"), "desc": MART_DESC.get(name, ""),
                       "columns": [{**c, "label": LABELS.get(c["name"], "")} for c in cols.get(name, [])]}
    templates = []
    for f in sorted((BI / "templates").glob("*.json")):
        templates += json.loads(f.read_text(encoding="utf-8"))
    data = {"project": PROJECT, "marts": marts, "templates": templates, "domains": DOMAINS,
            "built_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = (Path(__file__).with_name("template.html")).read_text(encoding="utf-8")
    html, n = re.subn(r"/\*__BI_DATA__\*/.*?/\*__END__\*/", lambda _: blob, html, count=1, flags=re.S)
    if n != 1:
        raise SystemExit("template.html に埋め込み位置（/*__BI_DATA__*/…/*__END__*/）がありません")
    OUT.write_text(html, encoding="utf-8")
    print(f"{OUT}  marts={len(marts)} templates={len(templates)} size={OUT.stat().st_size // 1024}KB")
    return OUT


if __name__ == "__main__":
    main()
