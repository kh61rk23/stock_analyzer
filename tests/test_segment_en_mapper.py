"""segment_en_mapper：英語の区分名 → ノード（規則）と、売上比率つきの行。"""
import segment_en_mapper as sm


def test_rules_map_known_names():
    exact, rules = sm.load_rules()
    f = lambda n: sm.node_for(n, exact, rules)
    assert f("Financial Services") == ("nonbank_finance", False)
    assert f("Automotive") == ("auto_oem", False)
    assert f("Digital Systems And Services") == ("system_integrator", False)
    assert f("Textile Business")[0] == "textile_fiber"            # tile（セメント）に当たらない
    assert f("Delivery Service")[0] == "logistics"                 # live（音楽）に当たらない
    assert f("Value Creating") == (None, False)                    # eating（外食）に当たらない
    assert f("Japan") == (None, True) and f("Operating Segments Not Included In Reportable Segments And Other") == (None, True)


def test_rule_nodes_are_approved():
    import json
    from taxonomy_loader import load_seed_batches, select_approved_nodes
    known = {n["node_id"] for n in select_approved_nodes(load_seed_batches())[0]}
    d = json.load(open(sm.RULES_PATH, encoding="utf-8"))
    assert {r["node"] for r in d["rules"] if r.get("node")} | {v for v in d["exact"].values() if v} <= known


def test_rows_share_by_revenue():
    exact, rules = sm.load_rules()
    m = "jpcrp030000-asr_E1-000{}ReportableSegmentMember"
    recs = [{"code": "7203", "member": m.format("Automotive"), "value": 90.0},
            {"code": "7203", "member": m.format("FinancialServices"), "value": 9.0},
            {"code": "7203", "member": m.format("Japan"), "value": 500.0},
            {"code": "7203", "member": m.format("Tiny"), "value": 1.0}]
    rows, st = sm.build_rows(recs, exact, rules, as_of="2026-10-30")
    assert [(r["node_id"], r["relevance_score"]) for r in rows] == [("auto_oem", 0.9), ("nonbank_finance", 0.09)]
    assert rows[0]["confidence_type"] == "gov_segment" and st["skipped"] == 1 and st["unmapped"] == 1


def test_total_rows_are_not_in_denominator():
    exact, rules = sm.load_rules()
    recs = [{"code": "9984", "member": "jpcrp030000-asr_E1-000SoftBankReportableSegmentMember", "value": 60.0},
            {"code": "9984", "member": "jpcrp030000-asr_E1-000AIComputingReportableSegmentMember", "value": 40.0},
            {"code": "9984", "member": "ReportableSegmentsMember", "value": 100.0}]
    rows, _ = sm.build_rows(recs, exact, rules, as_of="2026-10-30")
    assert [(r["node_id"], r["relevance_score"]) for r in rows] == [("telecom_carrier", 0.6), ("ai_software", 0.4)]
