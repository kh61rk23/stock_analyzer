"""米国の技術マップ：セグメント別売上の比率つき複数タグ（us_taxonomy_mapper）のテスト。"""
import us_taxonomy_mapper as um

KNOWN = {"datacenter_ai_chip", "semiconductor_design", "saas_platform", "passenger_vehicle_oem", "banking"}


def _rules():
    return um.load_segment_rules()


def test_member_words_splits_camel_case():
    assert um.member_words("us-gaap:DataCenterHostingMember") == "Data Center Hosting"
    assert um.member_words("nvda:AIServices1Member") == "AI Services"


def test_segment_shares_ratio_and_residual():
    segs = [{"axis": "ProductOrService", "member": "nvda:DataCenterMember", "val": 60.0},
            {"axis": "ProductOrService", "member": "nvda:GamingMember", "val": 40.0}]
    sh = um.segment_shares(segs, _rules(), KNOWN)
    assert round(sh["nodes"]["datacenter_ai_chip"], 2) == 0.6
    assert round(sh["residual"], 2) == 0.4


def test_regions_stay_in_denominator():
    # 地域別の区分を分母から外すと、残った区分が100%になってしまう
    segs = [{"axis": "BusinessSegments", "member": "x:AwsMember", "val": 10.0},
            {"axis": "BusinessSegments", "member": "x:NorthAmericaMember", "val": 90.0}]
    sh = um.segment_shares(segs, _rules(), KNOWN)
    assert round(sh["nodes"]["saas_platform"], 2) == 0.1


def test_build_rows_segment_plus_sic_and_fallback():
    by_code, by_major = um.load_map()
    profiles = [{"ticker": "NVDA", "cik": 1, "sic": "3674", "sic_description": "Semis"},
                {"ticker": "ZZZ", "cik": 2, "sic": "3674"}]
    segs = {1: [{"axis": "ProductOrService", "member": "n:DataCenterMember", "val": 60.0},
                {"axis": "ProductOrService", "member": "n:GamingMember", "val": 40.0}]}
    rows = um.build_rows(profiles, by_code, by_major, KNOWN, as_of="2026-11-01", segments=segs, rules=_rules())
    nv = [r for r in rows if r["company_id"] == "NVDA"]
    assert [(r["node_id"], r["confidence_type"], r["item_no"], r["items"]) for r in nv] == [
        ("datacenter_ai_chip", "us_segment", 1, 2), ("semiconductor_design", "us_sic", 2, 2)]
    assert nv[0]["revenue_share_est"] == 0.6 and abs(nv[1]["relevance_score"] - 0.4) < 1e-9
    zz = [r for r in rows if r["company_id"] == "ZZZ"]
    assert len(zz) == 1 and zz[0]["confidence_type"] == "us_sic" and zz[0]["relevance_score"] == 1.0


def test_existing_columns_kept():
    names = [c[0] for c in um.COLUMNS]
    for c in ("map_id", "company_id", "node_id", "relevance_score", "confidence", "revenue_share_est",
              "confidence_type", "updated_at", "item_no", "items"):
        assert c in names


def test_rules_nodes_are_approved():
    import json
    from taxonomy_loader import load_seed_batches, select_approved_nodes
    known = {n["node_id"] for n in select_approved_nodes(load_seed_batches())[0]}
    d = json.loads(um.US_RULES_PATH.read_text(encoding="utf-8"))
    used = {r["node"] for r in d["rules"] if r.get("node")} | {v for v in d["exact"].values() if v}
    assert used <= known
