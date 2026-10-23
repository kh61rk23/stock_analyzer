"""edinetdb_extras: 追加の窓口を取る順番と、本体の取り込みが枠を残すこと（API・BigQuery には触らない）。"""
from datetime import date
from types import SimpleNamespace

import edinetdb_extras as ex


def test_plan_priority_due_and_limit():
    tiers = [["E1"], ["E2", "E1"], ["E3"]]
    state = {"earnings": {"E1": {"at": "2026-10-20"}},            # まだ新しい → 飛ばす
             "cross-shareholdings": {"E1": {"at": "2026-01-01"}}}  # 180日以上前 → 取り直す
    out = ex.plan(tiers, state, date(2026, 10, 24), limit=6)
    assert out[:3] == [("cross-shareholdings", "E1"), ("subsidiaries", "E1"), ("main-customers", "E1")]
    assert out[3:] == [("earnings", "E2"), ("cross-shareholdings", "E2"), ("subsidiaries", "E2")]   # 重複した E1 は1回
    assert len(ex.plan(tiers, {}, date(2026, 10, 24), limit=2)) == 2


def test_overlapping_endpoints_are_not_taken():
    assert "shareholders" not in ex.ENDPOINTS and "facilities" not in ex.ENDPOINTS


def test_ingestor_leaves_budget_for_extras():
    import edinetdb_ingestor as ing
    cfg = SimpleNamespace(EDINETDB_DOC_INDEX_RESERVE=11, EDINETDB_EXTRAS_DAILY_CALLS=20)
    assert ing._w3_company_budget(cfg, 97) == 97 - 2 - 11 - 20
    assert ing._w3_company_budget(SimpleNamespace(EDINETDB_DOC_INDEX_RESERVE=11), 97) == 84


def test_new_annual_filings_come_first_even_if_fresh():
    import edinetdb_ingestor as ing
    today = date(2026, 10, 24)
    meta = {"E1": {"last_fetched": "2026-10-01"}, "E2": {"last_fetched": "2026-10-23"}, "E9": {"last_fetched": "2026-10-20"}}
    got = ing.select_ingest_targets(["E9"], [], [], ["E1", "E2"], meta, 9, calls_per_company=3, today=today,
                                    new_filings={"E1": "2026-10-20", "E2": "2026-10-20"})
    # E1: 有報が出た後に取っていない → 先頭。E2: 提出の翌日より後に取った → 鮮度どおり飛ばす。E9: 180日以内 → 飛ばす
    assert got == ["E1"]
