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
           "theme": "テーマ株", "market": "株価・スクリーニング", "us": "米国株", "research": "論文（arXiv）", "macro": "マクロ・業種レポート"}
MART_DESC = {
    "m_sector_scores": "業種スコアの日次推移（日本33業種＋米国11セクター・2026-07〜）。前日差・5日差・順位つき",
    "m_macro_indicators": "マクロ指標（ドル円・米10年債・原油・金・VIX・S&P500・日経平均）と米国債の年限別利回りの日次",
    "m_macro_signals": "マクロシグナル（金利上昇・円安など）×業種の追い風／逆風／注意の日次",
    "m_reports": "マクロ・業種の md レポート（直近31日・本文は先頭3,000字）。全期間・全文は analytics.report_texts",
    "m_arxiv_papers": "arXiv の論文（金融・計量経済の5分野・週1回取得・2026-07-27〜）。1論文1行。日本語タイトル・要旨・点数・キーワード・URL",
    "m_arxiv_keywords": "arXiv の論文×採点キーワード。キーワードの流行を見る",
    "m_stock_snapshot": "スクリーナー通過銘柄の日次記録（銘柄レポート md の正本・2026-09-09〜）。1日1銘柄1行。スコア・財務・テクニカル・シグナル文と5/20営業日後のリターン",
    "m_us_fin_annual": "米国株の通期財務。1銘柄1通期1行（直近5年・USD）。売上・利益・FCF・還元・貸借と前年比",
    "m_us_segments": "米国株のセグメント別売上・営業利益。1社×軸（事業／製品・サービス／地域）×セグメント×通期1行。構成比は全社売上に対する比率（合計行が混ざることがある）",
    "m_screener_us": "米国株スクリーナー。1銘柄1行（NYSE・Nasdaq）。金額はUSD。SEC の財務×株価、ROIC・EV/EBITDA・Fスコア・総還元など多面指標つき",
    "m_theme": "テーマ×銘柄。業種ごとの代表銘柄にその業種のテーマが付く（同じ業種のテーマは同じ銘柄集合）",
    "m_theme_def": "テーマ定義。業種サブテーマの一覧（米国を含む）",
    "m_theme_signal": "テーマ×日×シグナル種別。days_before_latest で直近N日に絞れる",
    "m_screener": "企業スクリーナー。1社1行（最新の決算期×全上場の最新株価）。ROIC・EV/EBITDA・土地含み益倍率・Fスコア・総還元など多面指標つき",
    "m_tax_node": "技術分類ツリー。1ノード1行（階層・領域・紐づく企業数）",
    "m_company_tax": "企業×技術分類。1社1ノード1行＋直近年度の財務（紐づくのは一部の会社のみ・自動推定）",
    "m_company": "上場会社マスタ。1社1行",
    "m_fin_annual": "年次財務。1社1年度1行（fiscal_year＝決算期末の年）",
    "m_country_profile": "国プロファイル。1か国1行（民主主義・汚職・法の支配・GDP・制裁など）",
    "m_country_risk": "国別リスクの日次。1国1日1行（総合リスク・GDELT不安定度・渡航レベル）",
    "m_company_country": "企業×国の権益・依存。1社1国1行（最新の国リスク付き）",
    "m_re_area": "市区町村の地価・取引・災害リスク。1市区町村1行",
    "m_re_holdings": "企業の保有不動産。1社×市区町村×年度1行（時価の目安・含み益の目安は概算）",
    "m_re_holdings_pref": "企業の保有不動産の都道府県別集約。1社×都道府県×年度1行",
}
LABELS = {
    "report_date": "日付", "sector_code": "業種コード", "sector_name": "業種", "score": "スコア", "risk_level": "リスク", "score_chg_1d": "前日差", "score_chg_5d": "5日差", "score_rank": "順位", "indicator": "指標", "value": "値", "change_1d": "前日差", "change_1m": "1か月差", "signal": "シグナル", "trigger_value": "きっかけ", "judgement_ja": "判定", "reason": "理由", "report_type": "種類", "body_head": "本文（先頭）",
    "paper_id": "arXiv ID", "title_ja": "日本語タイトル", "title_ja_or_en": "タイトル", "investment_score": "点数", "is_idea": "投資アイデア候補", "keywords": "キーワード", "keyword": "キーワード", "primary_category": "分野", "fetched_date": "取得日", "published_date": "公開日", "summary": "要旨", "authors": "著者",
    "snapshot_date": "記録日", "fwd_return_5d": "5営業日後リターン", "fwd_return_20d": "20営業日後リターン", "signal_text": "シグナル", "technical_score": "テクニカルスコア", "base_close": "起点の終値",
    "axis_ja": "軸", "segment_name": "セグメント", "member": "セグメント（原名）", "is_latest": "最新期", "revenue_share": "売上構成比",
    "revenue_growth_1y": "売上成長率(1年)", "company_revenue_usd": "全社売上(USD)", "tickers_all": "ティッカー（全種類）",
    "gross_profit_usd": "売上総利益(USD)", "operating_cf_usd": "営業CF(USD)", "capex_usd": "設備投資(USD)", "depreciation_usd": "減価償却(USD)",
    "debt_usd": "有利子負債(USD)", "cash_usd": "現金(USD)", "total_liabilities_usd": "総負債(USD)",
    "roic": "ROIC", "roic_tax_basis": "ROICの税率の出所", "tax_rate": "税率", "nopat": "NOPAT", "invested_capital": "投下資本",
    "ev": "EV（企業価値）", "ebitda": "EBITDA", "ev_ebitda": "EV/EBITDA", "ev_sales": "EV/売上", "psr": "PSR", "peg": "PEG",
    "fcf": "フリーCF", "earnings_yield": "益回り", "ocf_margin": "営業CF率", "gross_margin_chg_1y": "粗利率の前年差",
    "re_fiscal_year": "不動産の年度", "re_facility_count": "主要設備数", "re_est_excluded_count": "目安を除いた施設数",
    "re_land_value_est": "土地の時価目安", "re_unrealized_gain": "土地含み益(目安)", "re_gain_flag": "含み益の注記",
    "re_gain_to_mcap": "含み益÷時価総額", "re_land_value_to_mcap": "土地時価÷時価総額", "re_gain_after_tax": "税引後含み益",
    "adj_equity": "修正純資産", "adj_pbr": "修正PBR", "asset_value_ratio": "資産バリュー倍率", "interest_coverage": "インタレストカバレッジ",
    "current_ratio": "流動比率", "goodwill_to_equity": "のれん÷純資産", "accrual_ratio": "アクルーアル", "f_score": "Fスコア",
    "f_score_items": "Fスコア算出項目数", "buyback_est": "自社株買い(推定)", "total_payout_yield": "総還元利回り", "doe": "DOE",
    "op_income_cagr_3y": "営業益CAGR(3年)", "net_income_cagr_3y": "純益CAGR(3年)", "eps_cagr_3y": "EPS CAGR(3年)",
    "consec_up_years": "増収増益の連続期数", "ebitda_usd": "EBITDA(USD)", "ev_usd": "EV(USD)", "invested_capital_usd": "投下資本(USD)",
    "dividends_paid_usd": "配当支払額(USD)", "shares_outstanding": "発行済株式数", "total_assets_usd": "総資産(USD)", "equity_usd": "株主資本(USD)",
    "cik": "CIK", "has_fundamentals": "財務あり", "revenue_usd": "売上(USD)", "operating_income_usd": "営業利益(USD)",
    "net_income_usd": "純利益(USD)", "fcf_usd": "フリーCF(USD)", "buyback_usd": "自社株買い(USD)", "eps_usd": "EPS(USD)",
    "fcf_margin": "FCFマージン", "eps_growth_1y": "EPS成長率(1年)", "shares_est": "推定株数", "current_price_usd": "株価(USD)",
    "market_cap_usd": "時価総額(USD)", "market_cap_bn_usd": "時価総額(十億USD)", "ps_ratio": "PSR", "fcf_yield": "FCF利回り",
    "buyback_yield": "自社株買い利回り", "valuation_flag": "注意フラグ", "high_52w_usd": "52週高値(USD)", "low_52w_usd": "52週安値(USD)",
    "avg_turnover_20d_usd": "平均売買代金20日(USD)", "price_days": "株価の日数", "price_break_date": "分割の疑い日",
    "high_52w": "52週高値", "low_52w": "52週安値", "avg_turnover_20d": "平均売買代金20日", "shares_out": "株数",
    "return_1m": "騰落率1か月", "return_3m": "騰落率3か月", "return_1y": "騰落率1年",
    "ticker": "ティッカー", "employees": "従業員数", "interest_bearing_debt_basis": "有利子負債の算出方法",
    "net_cash": "ネットキャッシュ", "de_ratio": "D/Eレシオ", "sales_growth_1y": "売上成長率(1年)",
    "op_income_growth_1y": "営業利益成長率(1年)", "net_income_growth_1y": "純利益成長率(1年)", "sales_cagr_3y": "売上CAGR(3年)",
    "shares_issued": "発行済株式数", "price_source": "株価の出所", "current_price": "株価", "price_date": "株価の日付",
    "market_cap": "時価総額", "market_cap_b": "時価総額(億円)", "dividend_yield": "配当利回り", "net_cash_to_mcap": "ネットキャッシュ÷時価総額",
    "composite_score": "総合スコア", "value_score": "割安スコア", "quality_score": "質スコア", "snapshot_date": "スナップショット日",
    "rsi14": "RSI(14)", "week52_position_pct": "52週レンジ内の位置", "above_ma200": "200日線より上", "technical_date": "テクニカルの日付",
    "node_id": "技術分類ID", "parent_id": "親ノードID", "node_name_ja": "技術分類名", "node_name_en": "技術分類名（英）",
    "node_level": "階層", "node_type": "ノード種別", "supply_chain_pos": "サプライチェーン上の位置", "domain_id": "領域ID",
    "domain_name_ja": "技術領域", "path_name": "分類パス", "child_count": "子ノード数", "company_count": "企業数",
    "company_count_rollup": "企業数（下位含む）", "company_id": "企業ID", "entity_type": "主体の種別",
    "relevance_score": "関連度", "confidence": "確からしさ", "confidence_type": "確からしさの種別",
    "auto_derived": "自動推定", "verified_flag": "確認済み",
    "country_code": "国コード", "country_name_ja": "国名", "region": "地域", "risk_date": "日付",
    "composite_risk_score": "総合リスク", "gdelt_instability": "GDELT不安定度", "foreign_ministry_level": "外務省危険レベル",
    "state_dept_level": "米国務省レベル", "sanction_flag": "制裁対象", "exposure_type": "権益の種類",
    "exposure_strength": "依存度", "revenue_share_est": "売上依存の推定", "risk_weighted_score": "リスク加重スコア",
    "municipality_name": "市区町村", "prefecture": "都道府県", "prefecture_name": "都道府県",
    "land_price_yoy_change": "地価の前年比", "flood_risk_area_pct": "洪水リスク面積率", "facility_count": "施設数",
    "land_area_m2": "土地面積(m2)", "book_value_land_m_yen": "土地簿価(百万円)", "land_value_est_m_yen": "土地時価目安(百万円)",
    "unrealized_gain_est_m_yen": "含み益目安(百万円)", "population_change_pct": "人口増減率(2050)",
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
    "country_code": "国コード", "country_name_ja": "国名", "region": "地域", "risk_date": "日付",
    "sanction_flag": "制裁対象", "gdelt_instability": "GDELT不安定度", "foreign_ministry_level": "外務省レベル",
    "state_dept_level": "国務省レベル", "composite_risk_score": "総合リスク",
    "democracy_index": "民主主義指数", "corruption_index": "汚職認識指数", "rule_of_law": "法の支配",
    "gdp_usd_bn": "GDP(十億ドル)", "export_concentration": "輸出集中度", "is_landlocked": "内陸国",
    "trade_dependency_jp": "対日貿易依存", "ticker": "ティッカー", "market": "市場",
    "exposure_type": "権益の種類", "exposure_type_count": "権益の種類数", "exposure_strength": "依存度",
    "revenue_share_est": "売上比率(推定)", "review_flag": "要レビュー", "latest_risk_score": "最新の国リスク",
    "latest_risk_date": "リスク日付", "risk_weighted_score": "依存度×国リスク",
    "municipality_code": "市区町村コード", "prefecture_name": "都道府県", "prefecture": "都道府県",
    "municipality_name": "市区町村", "median_price": "取引価格の中央値", "avg_land_price_commercial": "商業地の平均地価",
    "avg_land_price_residential": "住宅地の平均地価", "land_price_yoy_change": "地価の前年比(%)",
    "flood_risk_area_pct": "洪水リスク地域率", "latest_avg_price_condo": "マンション平均価格",
    "hospital_count": "病院数", "facility_count": "施設数", "municipality_count": "市区町村数",
    "land_area_m2": "土地面積(m2)", "book_value_land_m_yen": "土地簿価(百万円)",
    "book_value_buildings_m_yen": "建物簿価(百万円)", "land_price_ref_per_m2": "参照地価(円/m2)",
    "land_value_est_m_yen": "土地時価の目安(百万円)", "unrealized_gain_est_m_yen": "含み益の目安(百万円)",
    "population_current": "現在人口", "population_2050": "2050年人口", "population_change_pct": "人口増減(%)",
    "landslide_risk_area_pct": "土砂災害リスク地域率", "est_excluded_count": "時価目安から除外した施設数", "area_land_price_yoy_change": "地域の地価前年比(%)",
}


def main() -> Path:
    manifest = json.loads((ROOT / "stock_data" / "bi" / "manifest.json").read_text(encoding="utf-8"))
    cols = {m["mart"]: m["columns"] for m in manifest["marts"]}
    # bi データセットに実体化済みのマートは表を直接読む（ダッシュボードの読み取り量を小さくするため）
    tables = {m["mart"]: f"{PROJECT}.bi.{m['mart']}" for m in manifest["marts"]
              if m.get("table_bytes") or m.get("bq_table") or m.get("table")}
    marts = {}
    for f in sorted((BI / "marts").glob("*.sql")):
        name = f.stem
        marts[name] = {"sql": f.read_text(encoding="utf-8"), "desc": MART_DESC.get(name, ""),
                       "columns": [{**c, "label": LABELS.get(c["name"], "")} for c in cols.get(name, [])],
                       "table": tables.get(name, "")}
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
