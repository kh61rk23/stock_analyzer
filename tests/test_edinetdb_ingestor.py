"""EdinetDBの優先選定と既存BQスキーマ写像を検証する。"""

from datetime import date
from types import SimpleNamespace

from conftest import load_module


module = load_module("edinetdb_ingestor")


def test_select_ingest_targets_prioritizes_and_deduplicates_with_budget():
    selected = module.select_ingest_targets(
        ["E00001", "E00002", "E00003"],
        ["E00003", "E00004"],
        ["E00005"],
        ["E00006"],
        {},
        budget=7,
        calls_per_company=3,
        today=date(2026, 7, 21),
    )
    assert selected == ["E00001", "E00002"]


def test_budget_smaller_than_one_company_issues_no_target():
    assert module.select_ingest_targets(
        ["E00001"], [], [], [], {}, budget=2, calls_per_company=3
    ) == []


def test_w3_reserves_usage_and_doc_index_calls():
    cfg = SimpleNamespace(EDINETDB_DOC_INDEX_RESERVE=11)
    assert module._w3_company_budget(cfg, 90) == 77


def test_explicit_budget_five_plans_one_company_without_network(tmp_path):
    (tmp_path / "portfolio_holdings.csv").write_text(
        "ticker,market\n7203.T,JP\n6758.T,JP\n", encoding="utf-8"
    )
    cfg = SimpleNamespace(
        ENABLE_EDINETDB_INGEST=True,
        EDINETDB_DAILY_BUDGET=90,
        EDINETDB_REFRESH_DAYS=180,
        DATA_DIR=tmp_path,
        SECTOR_REPRESENTATIVE_TICKERS={},
    )

    result = module.run_edinetdb_ingest(cfg, budget=5, dry_run=True)

    assert result["targets"] == ["7203.T"]
    assert result["budget"] == 5


def test_select_ingest_targets_skips_fresh_company():
    selected = module.select_ingest_targets(
        ["E00001", "E00002"], [], [], [],
        {"E00001": {"last_fetched": "2026-07-20"}},
        budget=6,
        calls_per_company=3,
        refresh_days=180,
        today=date(2026, 7, 21),
    )
    assert selected == ["E00002"]


def test_p4_rotation_uses_oldest_fetch_first():
    selected = module.select_ingest_targets(
        [], [], [], ["E_NEW", "E_OLD"],
        {
            "E_NEW": {"last_fetched": "2025-10-01"},
            "E_OLD": {"last_fetched": "2025-01-01"},
        },
        budget=3,
        calls_per_company=3,
        refresh_days=1,
        today=date(2026, 7, 21),
    )
    assert selected == ["E_OLD"]


def test_financial_mapping_uses_openapi_fields_without_guessing_fye():
    row = module.map_edinetdb_financials(
        {
            "fiscal_year": 2025,
            "submit_date": "2025-06-20",
            "revenue": 1000,
            "operating_income": 100,
            "ordinary_income": 90,
            "net_income": 60,
            "total_assets": 2000,
            "net_assets": 800,
            "cash": 200,
            "cf_operating": 150,
            "cf_investing": -40,
            "ibd_current": 10,
            "ibd_noncurrent": 90,
            "num_employees": 20,
            "roe_official": 0.075,
        },
        edinet_code="E02144",
        company_code="7203",
        now="2026-07-21T00:00:00+00:00",
    )
    assert row["fin_id"] == "E02144_2025"
    assert row["fiscal_year"] == 2025
    assert row["fiscal_year_end"] is None
    assert row["net_sales"] == 1000.0
    assert row["interest_bearing_debt"] == 100.0
    assert row["free_cf"] == 110.0
    assert row["source"] == "edinetdb"


def test_segment_mapping_writes_both_existing_tables():
    segments, finance = module.map_edinetdb_segments(
        "E02144",
        [{
            "segmentName": "自動車",
            "segmentType": "reportable",
            "fiscalYear": 2025,
            "revenue": 500,
            "operatingIncome": 50,
            "revenueShare": 50.0,
            "oiMargin": 10.0,
        }],
        2025,
        company_code="7203",
        now="2026-07-21T00:00:00+00:00",
    )
    assert len(segments) == len(finance) == 1
    assert segments[0]["segment_name_norm"] == "自動車"
    assert finance[0]["segment_id"] == segments[0]["segment_id"]
    assert finance[0]["revenue_ratio"] == 0.5
    assert finance[0]["operating_margin"] == 0.1


def test_text_block_mapping_preserves_raw_text():
    rows = module.map_edinetdb_text_blocks(
        "E02144", [{"section": "事業等のリスク", "text": "原材料価格が変動する。"}], 2025,
        company_code="7203", now="2026-07-21T00:00:00+00:00",
    )
    assert rows[0]["raw_text"] == "原材料価格が変動する。"
    assert rows[0]["source"] == "edinetdb"


def test_text_block_request_fetches_all_sections_in_full():
    assert module.text_block_params(2025) == {"full": "true", "fiscal_year": 2025}
    assert module.text_block_params(None) == {"full": "true"}


def test_ingest_calls_real_individual_contracts_and_merges_all_tables(
    tmp_path, monkeypatch
):
    class FakeRest:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0
            self.requests = []
            captured["rest"] = self

        def usage(self):
            self.calls += 1
            return {"daily_remaining": 20, "monthly_remaining": 200}

        def get(self, path, params=None):
            self.calls += 1
            self.requests.append((path, params))
            if path == "/companies":
                return [{"edinet_code": "E02144", "sec_code": "72030", "name": "トヨタ"}]
            if path.endswith("/financials"):
                return [{"fiscal_year": 2025, "revenue": 100, "net_income": 10}]
            if path.endswith("/segments"):
                return [{"segmentName": "自動車", "fiscalYear": 2025, "revenue": 100}]
            if path.endswith("/text-blocks"):
                return [{"section": "事業等のリスク", "text": "半導体不足"}]
            raise AssertionError(path)

    class FakeBQ:
        def __init__(self):
            self.merges = []
            captured["bq"] = self

        def merge_rows(self, table, rows, keys, dataset="edinet"):
            self.merges.append((dataset, table, rows, keys))
            return True

    captured = {}
    monkeypatch.setattr(module, "DBRestClient", FakeRest)
    monkeypatch.setattr("edinet_bq_client.EDINETBQClient", FakeBQ)
    cfg = SimpleNamespace(
        ENABLE_EDINETDB_INGEST=True,
        EDINETDB_API_KEY="key",
        EDINETDB_DAILY_BUDGET=20,
        EDINETDB_MONTHLY_GUARD=200,
        EDINETDB_REFRESH_DAYS=180,
        EDINETDB_TEXTBLOCK_EVERY_N=1,
        EDINETDB_ROTATION_META="rotation.json",
        EDINETDB_BASE_URL="https://example.test/v1",
        DATA_DIR=tmp_path,
        SECTOR_REPRESENTATIVE_TICKERS={"auto": ["7203.T"]},
    )

    result = module.run_edinetdb_ingest(cfg, target_limit=1)

    assert result["success"] == 1
    requests = captured["rest"].requests
    assert [path for path, _params in requests] == [
        "/companies",
        "/companies/E02144/financials",
        "/companies/E02144/segments",
        "/companies/E02144/text-blocks",
    ]
    assert requests[-1][1] == {"full": "true", "fiscal_year": 2025}
    assert [table for _dataset, table, _rows, _keys in captured["bq"].merges] == [
        "company", "financials", "segments", "segment_finance", "text_analysis"
    ]


def test_ingest_does_not_advance_rotation_when_full_text_fetch_fails(
    tmp_path, monkeypatch
):
    class FakeRest:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0

        def usage(self):
            return {"daily_remaining": 20}

        def get(self, path, params=None):
            if path == "/companies":
                return [{"edinet_code": "E02144", "sec_code": "72030", "name": "トヨタ"}]
            if path.endswith("/financials"):
                return [{"fiscal_year": 2025, "revenue": 100}]
            if path.endswith("/segments"):
                return []
            if path.endswith("/text-blocks"):
                return None
            raise AssertionError(path)

    class FakeBQ:
        def merge_rows(self, *_args, **_kwargs):
            return True

    monkeypatch.setattr(module, "DBRestClient", FakeRest)
    monkeypatch.setattr("edinet_bq_client.EDINETBQClient", FakeBQ)
    cfg = SimpleNamespace(
        ENABLE_EDINETDB_INGEST=True,
        EDINETDB_API_KEY="key",
        EDINETDB_DAILY_BUDGET=20,
        EDINETDB_REFRESH_DAYS=180,
        EDINETDB_TEXTBLOCK_EVERY_N=1,
        EDINETDB_ROTATION_META="rotation.json",
        DATA_DIR=tmp_path,
        SECTOR_REPRESENTATIVE_TICKERS={"auto": ["7203.T"]},
    )

    result = module.run_edinetdb_ingest(cfg, target_limit=1)

    assert result["success"] == 0
    assert result["failure"] == 1
    # 2026-09-10: 以前は「台帳に一切書かない」ことで再試行を担保していたが、
    # それだと毎回失敗する企業が永久に優先度上位へ居座り、11枠のうち8枠を
    # 同じ失敗の再試行が占め続けていた（2026-09-07/09/10 の3実行とも
    # 対象の先頭6社が同一だった）。
    # いまは失敗も記録するが **last_fetched は書かない**。
    # 「取得済み扱いにしない」という本来の意図はこちらで担保する。
    entry = module._load_json(tmp_path / "rotation.json", {}).get("E02144", {})
    assert "last_fetched" not in entry, "失敗したのに取得済み扱いになっている"
    assert entry.get("last_attempt"), "失敗が記録されず次回も同じ枠を消費する"
    assert entry.get("fail_count") == 1
    assert result["reasons"]["text_blocks"] == 1


# ── BQ照合によるローテ台帳の補完（2026-07-30）────────────────────
#
# 台帳はローカルJSONのキャッシュに過ぎず BQ より遅れることがある。
# 実測で BQ 114社 vs 台帳 69社の乖離があり、既に取得済みの約45社を
# 毎日再取得して API 予算を空費していた。BQ を正として補完する。

from edinetdb_ingestor import reconcile_meta_with_bq  # noqa: E402


_COMPANIES = [
    {"edinet_code": "E03006", "sec_code": "30760"},   # sec_codeは5桁（末尾0）
    {"edinet_code": "E01225", "sec_code": "86240"},
    {"edinet_code": "E99999", "sec_code": "99990"},
]


def test_reconcile_fills_missing_entries_from_bq():
    meta = {}
    meta, filled = reconcile_meta_with_bq(
        meta, _COMPANIES, {"3076": "2026-07-30", "8624": "2026-07-29"})
    assert filled == 2
    assert meta["E03006"]["last_fetched"] == "2026-07-30"
    assert meta["E01225"]["last_fetched"] == "2026-07-29"
    assert meta["E03006"]["source"] == "bq"


def test_reconcile_keeps_newer_local_entry():
    """台帳の方が新しいときは BQ で上書きしないこと。"""
    meta = {"E03006": {"last_fetched": "2026-07-31"}}
    meta, filled = reconcile_meta_with_bq(
        meta, _COMPANIES, {"3076": "2026-07-30"})
    assert filled == 0
    assert meta["E03006"]["last_fetched"] == "2026-07-31"


def test_reconcile_matches_on_first_four_digits():
    """BQ は4桁の証券コード、company cache は5桁。先頭4桁で突合すること。"""
    meta, filled = reconcile_meta_with_bq({}, _COMPANIES, {"8624": "2026-07-30"})
    assert filled == 1
    assert "E01225" in meta


def test_reconcile_ignores_unknown_codes():
    meta, filled = reconcile_meta_with_bq({}, _COMPANIES, {"0000": "2026-07-30"})
    assert filled == 0
    assert meta == {}


def test_reconcile_noop_when_bq_empty():
    """BQが使えない環境では台帳をそのまま返すこと（従来動作を維持）。"""
    original = {"E03006": {"last_fetched": "2026-07-01"}}
    meta, filled = reconcile_meta_with_bq(dict(original), _COMPANIES, {})
    assert filled == 0
    assert meta == original


# ── 失敗企業がローテーションを塞がないこと（2026-09-10）────────────
#
# last_fetched は成功時にしか書かれないため、毎回失敗する企業は
# 永久に fresh にならず優先度P1/P2の先頭に居座っていた。実際
# 2026-09-07 / 09-09 / 09-10 の3実行とも対象の先頭6社
# （E01719/E36174/E02324/E03693/E04275/E37577）が同一で、
# 11枠のうち8枠が同じ失敗の再試行に消えていた。

from datetime import date, timedelta   # noqa: E402


def _meta(code, *, last_attempt=None, fail_count=None, last_fetched=None):
    info = {}
    if last_attempt:
        info["last_attempt"] = last_attempt
    if fail_count is not None:
        info["fail_count"] = fail_count
    if last_fetched:
        info["last_fetched"] = last_fetched
    return {code: info}


class TestFailureBackoff:
    def test_recent_failure_is_skipped(self):
        import edinetdb_ingestor as m
        today = date(2026, 9, 10)
        meta = _meta("E1", last_attempt="2026-09-10", fail_count=1)
        got = m.select_ingest_targets(
            [], [], [], ["E1", "E2"], meta, budget=30, today=today)
        assert got == ["E2"], "直近で失敗した企業が再び枠を取っている"

    def test_backoff_expires_and_the_company_returns(self):
        import edinetdb_ingestor as m
        today = date(2026, 9, 10)
        meta = _meta("E1", last_attempt="2026-09-08", fail_count=1)
        got = m.select_ingest_targets(
            [], [], [], ["E1"], meta, budget=30, today=today)
        assert got == ["E1"], "待機日数を過ぎても復帰していない"

    def test_backoff_grows_with_consecutive_failures(self):
        import edinetdb_ingestor as m
        today = date(2026, 9, 10)
        # 4連続失敗 → 2^3 = 8日待つ
        meta = _meta("E1", last_attempt="2026-09-06", fail_count=4)
        assert m.select_ingest_targets(
            [], [], [], ["E1"], meta, budget=30, today=today) == []

    def test_backoff_is_capped(self):
        import edinetdb_ingestor as m
        today = date(2026, 9, 10)
        old = (today - timedelta(days=31)).isoformat()
        meta = _meta("E1", last_attempt=old, fail_count=99)
        assert m.select_ingest_targets(
            [], [], [], ["E1"], meta, budget=30, today=today,
            fail_backoff_max_days=30) == ["E1"], "上限を超えても復帰しない"

    def test_success_still_uses_refresh_days_not_backoff(self):
        import edinetdb_ingestor as m
        today = date(2026, 9, 10)
        meta = _meta("E1", last_fetched="2026-09-09")
        assert m.select_ingest_targets(
            [], [], [], ["E1"], meta, budget=30, today=today) == []

    def test_priority_company_does_not_starve_the_queue(self):
        """P1が常に失敗しても後続が回ること（今回の実害そのもの）"""
        import edinetdb_ingestor as m
        today = date(2026, 9, 10)
        meta = {c: {"last_attempt": "2026-09-10", "fail_count": 3}
                for c in ("E1", "E2", "E3")}
        got = m.select_ingest_targets(
            ["E1", "E2", "E3"], [], [], ["E4", "E5"], meta,
            budget=30, today=today)
        assert got == ["E4", "E5"], "失敗中のP1が枠を占有し続けている"


# ── データ無しを障害として数えない（2026-09-12）────────────────
#
# EdinetDB側にそもそも当該企業のデータが無い（404）のはこちらの障害では
# ないのに failure に混ぜていたため、実問題が0でも台帳が alert になり
# （2026-09-12 実測: failure 2 = missing 2 / bq_merge 0）、
# 本当に直すべき失敗が埋もれていた。

class TestMissingIsNotAFailure:
    def _run(self, tmp_path, monkeypatch, *, error):
        import edinetdb_ingestor as module

        class FakeRest:
            def __init__(self, *a, **k):
                self.calls = 0
                self.last_error = None

            def usage(self):
                return {"daily_remaining": 20, "monthly_remaining": 200}

            def get(self, path, params=None):
                self.calls += 1
                if path == "/companies":
                    return [{"edinet_code": "E1", "sec_code": "10000", "name": "T"}]
                self.last_error = error
                return None

        class FakeBQ:
            def merge_rows(self, *a, **k):
                return True

        monkeypatch.setattr(module, "DBRestClient", FakeRest)
        monkeypatch.setattr("edinet_bq_client.EDINETBQClient", FakeBQ)
        cfg = SimpleNamespace(
            ENABLE_EDINETDB_INGEST=True, EDINETDB_API_KEY="key",
            EDINETDB_DAILY_BUDGET=20, EDINETDB_REFRESH_DAYS=180,
            EDINETDB_TEXTBLOCK_EVERY_N=1, EDINETDB_ROTATION_META="rotation.json",
            DATA_DIR=tmp_path, SECTOR_REPRESENTATIVE_TICKERS={"a": ["1000.T"]},
        )
        return module.run_edinetdb_ingest(cfg, target_limit=1)

    def test_not_found_counts_as_missing_not_failure(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch, error="not_found")
        assert r["missing"] == 1
        assert r["failure"] == 0, "データ無しを障害に数えている"
        assert r["reasons"]["missing"] == 1

    def test_real_error_still_counts_as_failure(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch, error="server_error")
        assert r["failure"] == 1
        assert r["missing"] == 0

    def test_ledger_status_is_success_when_only_missing(self, tmp_path, monkeypatch):
        """台帳の判定式（stock_analyzer_main と同じ形）で success になること"""
        r = self._run(tmp_path, monkeypatch, error="not_found")
        status = ("skipped" if r.get("skipped")
                  else ("success" if not r.get("failure") else "alert"))
        assert status == "success"
