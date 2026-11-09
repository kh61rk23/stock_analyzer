from conftest import load_module


module = load_module("edinet_doc_indexer")


def test_build_doc_index_rows_maps_yuho_contract():
    rows = module.build_doc_index_rows(
        {"disclosures": [{
            "doc_id": "S100TEST",
            "type": "yuho",
            "published_at": "2026-06-20T09:00:00+09:00",
            "fiscal_year_end": "2026-03-31",
        }]},
        {
            "edinet_code": "E02144", "sec_code": "72030", "name": "トヨタ自動車",
            "accounting_standard": "JP",
        },
        now="2026-07-21T00:00:00+00:00",
    )
    assert rows == [{
        "doc_id": "S100TEST",
        "company_code": "7203",
        "edinet_code": "E02144",
        "company_name": "トヨタ自動車",
        "doc_type": "有価証券報告書",
        "fiscal_year_end": "2026-03-31",
        "disclosure_date": "2026-06-20",
        "xbrl_available": False,
        "html_available": False,
        "fetched_flag": True,
        "fetched_at": "2026-07-21T00:00:00+00:00",
        "ifrs_flag": False,
        "updated_at": "2026-07-21T00:00:00+00:00",
    }]


# ── 開示種別フィルタ ──────────────────────────────
#
# 呼び出し側は API に types=yuho を渡しており**サーバ側で既に絞られている**。
# 我々が管理していない語彙でホワイトリスト判定すると全件除外され、
# doc_index が1件も増えなくなる。それを防ぐテスト。
import pytest


class TestIsAnnualReport:
    @pytest.mark.parametrize("raw_type", [
        "yuho", "有価証券報告書", "annual_securities_report", "ASR",
    ])
    def test_known_annual_types_pass(self, raw_type):
        assert module.is_annual_report(raw_type) is True

    @pytest.mark.parametrize("raw_type", [
        "四半期報告書", "quarterly", "半期報告書", "臨時報告書",
        "訂正有価証券報告書", "大量保有報告書", "月次売上速報",
        "disclosure_statutory", "disclosure_correction",
    ])
    def test_known_non_annual_types_are_rejected(self, raw_type):
        assert module.is_annual_report(raw_type) is False

    @pytest.mark.parametrize("raw_type", ["030000", "", "unknown_slug"])
    def test_unknown_vocabulary_is_not_silently_dropped(self, raw_type):
        """サーバが types=yuho で絞った結果を、知らない語彙だからと捨てない"""
        assert module.is_annual_report(raw_type) is True

    def test_correction_beats_the_annual_token(self):
        """『訂正有価証券報告書』は有報語を含むが訂正なので除外側が勝つ"""
        assert module.is_annual_report("訂正有価証券報告書") is False


class TestSeenTypesAreCollected:
    def test_observed_vocabulary_is_reported(self):
        """語彙が想定と違うと静かに全滅するので実物を必ず拾えること"""
        seen = set()
        module.build_doc_index_rows(
            {"disclosures": [
                {"doc_id": "S1", "type": "030000"},
                {"doc_id": "S2", "type": "四半期報告書"},
                {"doc_id": "S3"},
            ]},
            {"edinet_code": "E1", "sec_code": "10000", "name": "テスト"},
            now="2026-09-11T00:00:00+00:00",
            seen_types=seen,
        )
        assert "030000" in seen
        assert "四半期報告書" in seen
        assert "(種別欄なし)" in seen

    def test_unknown_type_now_produces_a_row(self):
        rows = module.build_doc_index_rows(
            {"disclosures": [{"doc_id": "S1", "type": "030000",
                              "published_at": "2026-06-20T09:00:00+09:00"}]},
            {"edinet_code": "E1", "sec_code": "10000", "name": "テスト"},
            now="2026-09-11T00:00:00+00:00",
        )
        assert len(rows) == 1, "未知語彙で行が落ちている（2026-04-26以来の停止原因）"
        assert rows[0]["doc_id"] == "S1"


# ── 巡回の記録（EdinetDB に開示が無い会社を毎日取り直さない）──────────────

def test_not_found_company_is_recorded_so_rotation_moves_on(tmp_path, monkeypatch):
    import json
    import types

    (tmp_path / "edinetdb_company_cache.json").write_text(
        json.dumps({"companies": [{"edinet_code": "E1"}, {"edinet_code": "E2"}]}), encoding="utf-8")
    (tmp_path / "edinetdb_rotation_meta.json").write_text(
        json.dumps({"E1": {}, "E2": {}}), encoding="utf-8")

    class FakeClient:
        last_error = None

        def __init__(self, *a, **k):
            pass

        def get(self, path, params=None):
            if "/E1/" in path:
                self.last_error = "not_found"
                return None
            self.last_error = None
            return {"disclosures": []}

    class FakeBQ:
        dataset = "d"

        def merge_rows(self, *a, **k):
            return True

    monkeypatch.setattr(module, "DBRestClient", FakeClient)
    monkeypatch.setattr(module, "_load_doc_ids_from_financials", lambda bq, codes: {})
    import edinet_bq_client
    monkeypatch.setattr(edinet_bq_client, "EDINETBQClient", FakeBQ)
    cfg = types.SimpleNamespace(EDINETDB_API_KEY="k", DATA_DIR=str(tmp_path))
    res = module.run_doc_indexer(cfg=cfg)
    meta = json.loads((tmp_path / "edinetdb_rotation_meta.json").read_text(encoding="utf-8"))
    assert res["absent"] == 1 and res["visited"] == 1
    assert meta["_doc_index"]["E1"]["status"] == "no_data"
    assert meta["_doc_index"]["E1"]["last_indexed"]
