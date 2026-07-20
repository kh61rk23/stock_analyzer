# segment パイプライン実スキーマ是正（純粋ロジック）
# 2026-07-19追加:
#   ④ segment_signal が「幻のAPI」（存在しない bq.get_segment_data / bq.query）と
#      実在しないテーブル（taxonomy.segments_master / edinet.segment_financials）を
#      参照して常に0件だった問題の是正。
#   - 書込側: edinet_direct_fetcher.build_financials_row / build_segment_rows が
#     実スキーマ（MCP直queryで2026-07-19確認）の列名で行を組むこと
#   - 読取側: segment_signal.build_segment_view が最新期抽出＋growth_rate計算を
#     正しく行うこと
#   ネットワーク・BQ接続には依存しない。
from conftest import load_module

seg_sig = load_module("segment_signal")
fetcher = load_module("edinet_direct_fetcher")


# ==============================================================
# 読取側: build_segment_view
# ==============================================================

def _row(name, period, revenue, ratio=None, margin=None):
    return {
        "segment_name":      name,
        "segment_name_norm": name.lower(),
        "report_period":     period,
        "revenue":           revenue,
        "revenue_ratio":     ratio,
        "operating_margin":  margin,
    }


class TestBuildSegmentView:
    def test_empty_returns_empty(self):
        assert seg_sig.build_segment_view([]) == []

    def test_latest_period_only(self):
        rows = [
            _row("Game", "2026-03-31", 120.0, 0.6, 0.15),
            _row("Game", "2025-03-31", 100.0, 0.6, 0.12),
            _row("Music", "2026-03-31", 80.0, 0.4, 0.10),
        ]
        view = seg_sig.build_segment_view(rows)
        assert len(view) == 2                      # 最新期の2セグメントのみ
        names = {v["segment_raw_name"] for v in view}
        assert names == {"Game", "Music"}

    def test_growth_rate_from_previous_period(self):
        rows = [
            _row("Game", "2026-03-31", 120.0),
            _row("Game", "2025-03-31", 100.0),
        ]
        view = seg_sig.build_segment_view(rows)
        # (120 - 100) / |100| = 0.2
        assert view[0]["growth_rate"] == 0.2

    def test_growth_none_when_no_previous(self):
        view = seg_sig.build_segment_view([_row("Game", "2026-03-31", 120.0)])
        assert view[0]["growth_rate"] is None

    def test_growth_none_when_prev_revenue_zero(self):
        rows = [
            _row("Game", "2026-03-31", 120.0),
            _row("Game", "2025-03-31", 0.0),
        ]
        assert seg_sig.build_segment_view(rows)[0]["growth_rate"] is None

    def test_field_mapping_and_verified_flag(self):
        view = seg_sig.build_segment_view(
            [_row("Game", "2026-03-31", 120.0, ratio=0.65, margin=0.18)])
        v = view[0]
        assert v["revenue_share"] == 0.65
        assert v["op_margin"] == 0.18
        assert v["verified_flag"] is True          # EDINET実数値由来＝割引対象外


# ==============================================================
# 書込側: build_financials_row / build_segment_rows
# ==============================================================

# edinet.financials 実スキーマ（stock-analyzer-edinet・2026-07-19 MCP確認）
_FINANCIALS_COLUMNS = {
    "fin_id", "doc_id", "company_code", "fiscal_year_end", "report_period",
    "net_sales", "operating_income", "ordinary_income", "net_income",
    "total_assets", "equity", "interest_bearing_debt", "cash_and_equiv",
    "operating_cf", "investing_cf", "free_cf", "employee_count",
    "avg_age_years", "avg_annual_salary", "total_director_comp",
    "max_single_comp", "capex", "depreciation", "roe", "roa",
    "operating_margin", "accrual_ratio", "fcf_yield", "labor_productivity",
    "currency", "source", "updated_at",
}
_SEGMENTS_COLUMNS = {
    "segment_id", "doc_id", "company_code", "segment_name",
    "segment_name_norm", "segment_type", "parent_segment_id",
    "report_period", "source_id", "updated_at",
}
_SEGMENT_FINANCE_COLUMNS = {
    "sf_id", "segment_id", "revenue", "revenue_ratio", "operating_profit",
    "operating_margin", "assets", "geography", "currency",
    "report_period", "updated_at",
}

_DOC_META = {
    "doc_id": "S100TEST",
    "company_code": "6758",
    "edinet_code": "E01777",
    "fiscal_year_end": "2026-03-31",
}


class TestBuildFinancialsRow:
    def test_columns_subset_of_real_schema(self):
        row = fetcher.build_financials_row(_DOC_META, {}, "2026-07-19T00:00:00")
        unknown = set(row) - _FINANCIALS_COLUMNS
        assert unknown == set()                    # 実在しない列でのINSERT禁止

    def test_required_keys_and_mapping(self):
        fin = {
            "net_sales": 1000.0, "operating_income": 100.0,
            "net_income": 60.0, "equity": 600.0, "total_assets": 2000.0,
            "cash": 300.0, "num_employees": 500,
            "operating_cf": 150.0, "investing_cf": -50.0,
        }
        row = fetcher.build_financials_row(_DOC_META, fin, "2026-07-19T00:00:00")
        assert row["fin_id"] == "S100TEST_fin"     # REQUIRED
        assert row["doc_id"] == "S100TEST"         # REQUIRED
        assert row["company_code"] == "6758"
        assert row["fiscal_year_end"] == "2026-03-31"
        assert row["cash_and_equiv"] == 300.0      # cash → cash_and_equiv
        assert row["employee_count"] == 500        # num_employees → employee_count
        assert row["free_cf"] == 100.0             # 150 + (-50)
        assert row["operating_margin"] == 0.1
        assert row["roe"] == 0.1
        assert row["roa"] == 0.03

    def test_missing_values_stay_none(self):
        row = fetcher.build_financials_row(_DOC_META, {}, "2026-07-19T00:00:00")
        assert row["net_sales"] is None
        assert row["free_cf"] is None
        assert row["roe"] is None


class TestBuildSegmentRows:
    def test_columns_subset_of_real_schema(self):
        segs = [{"raw_segment_name": "Game", "segment_sales": 120.0,
                 "segment_profit": 20.0, "segment_assets": 400.0}]
        seg_rows, fin_rows = fetcher.build_segment_rows(
            _DOC_META, segs, "2026-07-19T00:00:00")
        assert set(seg_rows[0]) - _SEGMENTS_COLUMNS == set()
        assert set(fin_rows[0]) - _SEGMENT_FINANCE_COLUMNS == set()

    def test_ids_link_and_ratios(self):
        segs = [
            {"raw_segment_name": "Game",  "segment_sales": 120.0,
             "segment_profit": 24.0, "segment_assets": None},
            {"raw_segment_name": "Music", "segment_sales": 80.0,
             "segment_profit": None, "segment_assets": None},
        ]
        seg_rows, fin_rows = fetcher.build_segment_rows(
            _DOC_META, segs, "2026-07-19T00:00:00")
        assert len(seg_rows) == len(fin_rows) == 2
        # segment_id で正しく結合できること（REQUIRED列）
        assert fin_rows[0]["segment_id"] == seg_rows[0]["segment_id"]
        assert seg_rows[0]["segment_id"] != seg_rows[1]["segment_id"]
        # revenue_ratio = 120/200, operating_margin = 24/120
        assert fin_rows[0]["revenue_ratio"] == 0.6
        assert fin_rows[0]["operating_margin"] == 0.2
        assert fin_rows[1]["operating_margin"] is None

    def test_empty_records(self):
        seg_rows, fin_rows = fetcher.build_segment_rows(_DOC_META, [], "t")
        assert seg_rows == [] and fin_rows == []
