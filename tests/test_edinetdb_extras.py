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
