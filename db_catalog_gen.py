# ==============================================================
# db_catalog_gen.py — 自然言語→SQL 用の「DB構造台帳」を作る
# 【目的】利用者が「この会社の研究開発費の推移は？」のように日本語で聞いたとき、AI が
#   当て推量でなく実在の表・列・項目名で SQL を組めるよう、1ファイルに必要な事実を集める。
#   - 全表の列・型・行数・最終更新（BQ の INFORMATION_SCHEMA / __TABLES__ ＝課金ほぼ0）
#   - 表の用途と結合キー（下の TABLE_NOTES。SPEC_db_schema.md の要約）
#   - 財務細目（edinet.financial_items）の項目キー×日本語名×単位（edinetdb_fields.py）
#   - よく使う集計の例
# 【出力】stock_data/data/db_catalog.md（Actions が日次で更新→obsidian_sync で Mac へ）
# 【実行】python3 db_catalog_gen.py            … BQ から採取（BQ_CREDENTIALS が必要）
#         python3 db_catalog_gen.py --snapshot <bq_all_schemas_*.json>  … 認証なしで生成
# 【使い手】skill `bq-sql`（Claude Code のチャット）と、依頼箱 kind: sql-request（README 3-i）
# ==============================================================
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent
OUT = REPO / "stock_data" / "data" / "db_catalog.md"
PROJECT = "stock-analyzer-edinet"
DATASETS_ASIA = ["edinet", "analytics", "real_estate", "geopolitics", "taxonomy"]
DATASETS_US = ["us", "ontology"]

# 表の用途・粒度・結合キー（SPEC_db_schema.md の要約。表を足したらここにも1行）
TABLE_NOTES = {
    "edinet.company": "上場会社マスタ（EdinetDB）。1社1行。edinet_code ⇔ sec_code（証券コード5桁・先頭4桁が銘柄コード）",
    "edinet.financials": "年次財務の主要約25項目。1社1年度1行。キー (edinet_code, fiscal_year)。fiscal_year＝決算期末の年（3月決算 FY2026＝2026年3月期）。金額は円",
    "edinet.financial_items": "財務三表の細目（縦持ち）。1社1年度1項目1行。statement=PL/BS/CF/株式/人的資本/環境/指標、item=項目キー（下の一覧）、value=値。company_code は証券コード4桁",
    "edinet.financial_items_q": "単四半期の財務細目（EdinetDB period=quarterly_standalone）。1社1年度1四半期1項目1行。quarter='Q1'〜'Q4'。保有・スクリーナー候補を30日ごとに更新",
    "edinet.doc_index": "有価証券報告書の一覧。disclosure_date＝提出日、fiscal_year_end＝決算期末",
    "edinet.segments": "セグメント（事業・地域）。company_code 単位。segment_finance と segment_id で結合",
    "edinet.segment_finance": "セグメント別の売上・利益・資産。segments.segment_id と結合",
    "edinet.text_analysis": "有報本文（raw_text）と FinBERT の感情スコア。section_type＝章（事業等のリスク等）",
    "edinet.policy_support": "NEDO 等の政策支援の採択（securities_code＝証券コード）",
    "edinet.sub_theme_master": "業種ごとのサブテーマ定義",
    "edinet.ticker_subtheme_mapping": "銘柄×サブテーマ",
    "analytics.stock_snapshot": "スクリーナー対象銘柄の日次スナップショット（総合点・PER・PBR・ROE・配当利回り等）。1日1銘柄1行。ticker は '7203.T' 形式（JP）/ 'AAPL'（US）",
    "analytics.price_daily": "日足 OHLCV。trade_date×ticker。重複がありうるので QUALIFY ROW_NUMBER() で最新を採る",
    "geopolitics.country_risk_daily": "国別リスクの日次（ISO3 国コード）",
    "geopolitics.country_profile": "国別の静的プロファイル（59か国）",
    "geopolitics.company_country_exposure": "企業×国の権益・売上依存（ticker は '1605.T' 形式）",
    "real_estate.corporate_holdings_p1": "企業の保有不動産（有報の設備の状況）。edinet_code で結合",
    "real_estate.area_profile_p1": "市区町村の地価・取引・災害リスク",
    "taxonomy.taxonomy_node": "技術分類ツリー",
    "taxonomy.company_taxonomy_map": "企業×技術分類",
    "us.fundamentals": "米国株の財務（SEC XBRL）。ticker×period_end。location=US のため asia の表と JOIN 不可",
    "us.institutional_positions": "13F 機関投資家の保有変化（四半期）",
    "ontology.signal_event": "シグナル発火イベント（週次）。location=US",
    "ontology.company_entity": "企業エンティティ（週次）。location=US",
    "ontology.theme_entity": "テーマ（週次）。location=US",
}

EXAMPLES = [
    ("ある会社の販管費・研究開発費の推移（証券コード指定）",
     """SELECT fiscal_year, item, label_ja, value
FROM `stock-analyzer-edinet.edinet.financial_items`
WHERE company_code = '7203' AND item IN ('sga', 'rnd_expenses', 'revenue')
ORDER BY fiscal_year DESC, item"""),
    ("研究開発費率（研究開発費÷売上高）が高い会社 上位20（最新年度）",
     """WITH latest AS (
  SELECT edinet_code, MAX(fiscal_year) fy FROM `stock-analyzer-edinet.edinet.financial_items` GROUP BY 1)
SELECT c.name, i.company_code, i.fiscal_year,
       MAX(IF(i.item='rnd_expenses', i.value, NULL)) / NULLIF(MAX(IF(i.item='revenue', i.value, NULL)), 0) AS rnd_ratio
FROM `stock-analyzer-edinet.edinet.financial_items` i
JOIN latest l ON i.edinet_code = l.edinet_code AND i.fiscal_year = l.fy
JOIN `stock-analyzer-edinet.edinet.company` c ON c.edinet_code = i.edinet_code
WHERE i.item IN ('rnd_expenses', 'revenue')
GROUP BY 1, 2, 3 HAVING rnd_ratio IS NOT NULL
ORDER BY rnd_ratio DESC LIMIT 20"""),
    ("直近の日次スナップショットで PBR1倍未満・ROE10%以上",
     """SELECT ticker, name, sector_name, pbr, roe, composite_score
FROM `stock-analyzer-edinet.analytics.stock_snapshot`
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `stock-analyzer-edinet.analytics.stock_snapshot`)
  AND market = 'JP' AND pbr < 1 AND roe >= 0.10
ORDER BY composite_score DESC"""),
    ("年度ごとの売上原価率の変化（1社）",
     """SELECT fiscal_year,
       MAX(IF(item='cost_of_sales', value, NULL)) / NULLIF(MAX(IF(item='revenue', value, NULL)), 0) AS cogs_ratio
FROM `stock-analyzer-edinet.edinet.financial_items`
WHERE company_code = '1332' GROUP BY 1 ORDER BY 1"""),
]


def _from_bq() -> dict:
    from edinet_bq_client import EDINETBQClient
    c = EDINETBQClient().client
    out: dict = {}
    for ds in DATASETS_ASIA + DATASETS_US:
        try:
            rows = c.query(
                f"SELECT table_name, column_name, data_type FROM `{PROJECT}.{ds}.INFORMATION_SCHEMA.COLUMNS` "
                "ORDER BY table_name, ordinal_position").result()
            for r in rows:
                out.setdefault(ds, {}).setdefault(r.table_name, {"columns": [], "row_count": None, "last_mod": None})
                out[ds][r.table_name]["columns"].append({"name": r.column_name, "type": r.data_type})
            meta = c.query(
                f"SELECT table_id, row_count, DATE(TIMESTAMP_MILLIS(last_modified_time)) d "
                f"FROM `{PROJECT}.{ds}.__TABLES__`").result()
            for r in meta:
                if r.table_id in out.get(ds, {}):
                    out[ds][r.table_id]["row_count"] = r.row_count
                    out[ds][r.table_id]["last_mod"] = str(r.d)
        except Exception as exc:
            print(f"  ⚠️ {ds}: {type(exc).__name__}: {str(exc)[:120]}")
    return out


def _from_snapshot(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    return {ds: {t: {"columns": v["columns"], "row_count": v.get("row_count"), "last_mod": None}
                 for t, v in tables.items()} for ds, tables in d["datasets"].items()}


def render(schemas: dict) -> str:
    from edinetdb_fields import FINANCIAL_ITEMS
    today = date.today().isoformat()
    L = [f"<!-- 生成: db_catalog_gen.py（{today}）。手で編集しない -->",
         "# DB構造台帳（自然言語→SQL 用）", "",
         f"- プロジェクト: `{PROJECT}`（BigQuery / GoogleSQL）。表は必ず `{PROJECT}.<dataset>.<table>` で書く",
         "- ロケーション: `us` と `ontology` は **US**、それ以外は **asia-northeast1**。両者を1本のSQLで JOIN できない",
         "- 課金: 参照する表1つにつき最低10MB。無料枠は月1TB。SELECT * を避け、必要な列と LIMIT を付ける",
         "- 銘柄コードの形: edinet.* は `company_code`='7203'（4桁・英字可 '285A'）、analytics.* は `ticker`='7203.T'",
         "- 会社名から探すとき: `edinet.company` の `name` を LIKE で引き、`edinet_code` か `sec_code` の先頭4桁で結合",
         "- 年度: `fiscal_year` は決算期末の年（3月決算 FY2026＝2026年3月期、12月決算 FY2025＝2025年12月期）",
         "", "## 表の一覧", "", "| 表 | 行数 | 最終更新 | 用途・粒度・結合キー |", "|---|---:|---|---|"]
    for ds in sorted(schemas):
        for t in sorted(schemas[ds]):
            v = schemas[ds][t]
            n = v.get("row_count")
            if n == 0:
                continue
            L.append(f"| `{ds}.{t}` | {n if n is not None else '—':,} | {v.get('last_mod') or '—'} | "
                     f"{TABLE_NOTES.get(f'{ds}.{t}', '')} |" if isinstance(n, int) else
                     f"| `{ds}.{t}` | — | {v.get('last_mod') or '—'} | {TABLE_NOTES.get(f'{ds}.{t}', '')} |")
    L += ["", "（0行の表は省略。全表は SPEC_db_schema.md §0-1）", "", "## 列"]
    for ds in sorted(schemas):
        for t in sorted(schemas[ds]):
            v = schemas[ds][t]
            if v.get("row_count") == 0:
                continue
            cols = ", ".join(f"{c['name']} {c['type']}" for c in v["columns"])
            L += ["", f"### `{ds}.{t}`", "", cols]
    L += ["", "## 財務細目 edinet.financial_items の項目（item / 日本語名 / 単位）", ""]
    for st in ["PL", "BS", "CF", "株式", "人的資本", "環境", "指標"]:
        items = [f"`{k}` {lab}（{u}）" if u else f"`{k}` {lab}" for k, s, lab, u in FINANCIAL_ITEMS if s == st]
        L += [f"- **{st}**: " + " / ".join(items)]
    L += ["", "## よく使う集計の例"]
    for title, sql in EXAMPLES:
        L += ["", f"### {title}", "", "```sql", sql, "```"]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    schemas = _from_snapshot(Path(a.snapshot)) if a.snapshot else _from_bq()
    if not schemas:
        print("⚠️ スキーマが取れなかったため台帳を更新しない")
        return 1
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(schemas), encoding="utf-8")
    print(f"✅ DB構造台帳: {sum(len(v) for v in schemas.values())}表 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
