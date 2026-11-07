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
    sh = um.segment_shares(segs, _rules(), KNOWN | {"cloud_infrastructure"})
    assert round(sh["nodes"]["cloud_infrastructure"], 2) == 0.1


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
    assert used <= known, used - known


def _all_seed_nodes():
    import glob
    import json
    from taxonomy_loader import SEED_DIR
    ids = set()
    for p in glob.glob(str(SEED_DIR / "batch*.json")):
        ids |= {n["node_id"] for n in json.load(open(p, encoding="utf-8"))["nodes"]}
    return ids


def test_us_rule_and_sic_nodes_exist_in_seed():
    import json
    ids = _all_seed_nodes()
    d = json.load(open(um.US_RULES_PATH, encoding="utf-8"))
    used = {r["node"] for r in d["rules"] if r.get("node")} | {v for v in d["exact"].values() if v}
    m = json.load(open(um.MAP_PATH, encoding="utf-8"))
    used |= set(m["sic_to_node"].values()) | set(m["major_to_node"].values())
    assert used <= ids, used - ids


def test_consumer_electronics_and_cloud_nodes_are_approved():
    import json
    from taxonomy_loader import SEED_DIR, load_seed_batches, select_approved_nodes
    b = json.load(open(SEED_DIR / "batch13_consumer_electronics_cloud_draft.json", encoding="utf-8"))
    assert b["_meta"]["status"].startswith("APPROVED")
    assert all(n["verified_flag"] is True and n["is_active"] is True for n in b["nodes"])
    ids = {n["node_id"] for n in b["nodes"]}
    approved = {n["node_id"] for n in select_approved_nodes(load_seed_batches())[0]}
    assert ids <= approved                           # taxonomy_loader が読み、master.tax_node へ入る
    parents = {n["parent_id"] for n in b["nodes"]}
    assert parents <= approved


def test_rules_hit_approved_batch13_nodes():
    from taxonomy_loader import load_seed_batches, select_approved_nodes
    approved = {n["node_id"] for n in select_approved_nodes(load_seed_batches())[0]}
    rules = _rules()
    for name, node in (("I Phone", "smartphone_device"), ("Mac", "pc_tablet_device"),
                       ("Amazon Web Services", "cloud_infrastructure")):
        assert um.classify_member(name, rules) == ("node", node)
        assert node in approved
    by_code, by_major = um.load_map()
    assert um.node_for_sic("3571", by_code, by_major) in approved


def test_devices_and_cloud_segments_map_to_new_nodes():
    new = {"consumer_electronics", "smartphone_device", "pc_tablet_device", "wearable_device", "cloud_infrastructure",
           "iaas_compute", "paas_platform"}
    rules = _rules()
    f = lambda n: um.classify_member(n, rules)
    assert f("I Phone") == ("node", "smartphone_device")
    assert f("Mac") == ("node", "pc_tablet_device") and f("I Pad") == ("node", "pc_tablet_device")
    assert f("Wearables Home And Accessories") == ("generic", None)      # 混ざった区分は付けない
    for n in ("Amazon Web Services", "Google Cloud", "Intelligent Cloud", "AWS"):
        assert f(n) == ("node", "cloud_infrastructure"), n
    assert f("Infrastructure As A Service") == ("node", "iaas_compute")
    assert f("Platform As A Service") == ("node", "paas_platform")
    # known に無いノードは当たらない扱い、known にあれば売上の比率で付く
    segs = [{"axis": "ProductOrService", "member": "a:IPhoneMember", "val": 60.0},
            {"axis": "ProductOrService", "member": "a:MacMember", "val": 20.0},
            {"axis": "ProductOrService", "member": "a:WearablesHomeAndAccessoriesMember", "val": 20.0}]
    assert um.segment_shares(segs, rules, KNOWN)["nodes"] == {}
    sh = um.segment_shares(segs, rules, KNOWN | new)
    assert round(sh["nodes"]["smartphone_device"], 2) == 0.6 and round(sh["nodes"]["pc_tablet_device"], 2) == 0.2


def test_apple_sic_goes_to_consumer_electronics():
    by_code, by_major = um.load_map()
    assert um.node_for_sic("3571", by_code, by_major) == "consumer_electronics"
    assert um.node_for_sic("3634", by_code, by_major) == "home_appliance"


def test_added_us_names():
    rules = _rules()
    f = lambda n: um.classify_member(n, rules)
    assert f("Single") == ("skip", None) and f("External Revenues") == ("skip", None)
    assert f("Walmart US") == ("node", "discount_general")
    assert f("Opdivo") == ("node", "drug_modality")
    assert f("Taco Bell Global Division") == ("node", "fast_food_cafe")
    assert f("Perishables") == ("generic", None)
    assert f("Alaska") == ("generic", None)
    # 「人気ブランド名」で他社の別の区分を巻き込まない
    assert f("Life Insurance")[1] == "life_insurance"
