"""positioning_signals：大量保有・13D・8-K・空売り残をメールと審議パケットへ（BQ は読まずに形だけ）。"""
import positioning_signals as ps


def _data():
    jp = [{"submit_date": "2026-10-20", "issuer_sec_code": "77430", "issuer_name": "シード", "holder_name": "Luxshare",
           "holder_kind": "法人（外国法人）", "prior_ratio": None, "holding_ratio": 0.433, "purpose": "非公開化を目的とした重要提案行為"},
          {"submit_date": "2026-10-19", "issuer_sec_code": "93340", "issuer_name": "アイビス", "holder_name": "資産管理会社",
           "holder_kind": "法人（株式会社）", "prior_ratio": None, "holding_ratio": 0.84, "purpose": "代表取締役の資産管理会社であり安定株主"}]
    us = [{"kind": "dg", "filed_date": "2026-10-20", "ticker": "MAT", "form": "SCHEDULE 13D", "who": "EdgePoint", "percent": 17.68, "items": None},
          {"kind": "k8", "filed_date": "2026-10-22", "ticker": "MAT", "form": "8-K", "who": None, "percent": None, "items": "1.01,2.03,9.01"},
          {"kind": "k8", "filed_date": "2026-10-22", "ticker": "MAT", "form": "8-K", "who": None, "percent": None, "items": "2.02,9.01"}]
    si = [{"symbol": "MAT", "settlement_date": "2026-09-30", "short_qty": 1000, "change_pct": 40.0, "days_to_cover": 2.0}]
    return ps.group(jp, us, si)


def test_k8_flags_pick_material_items_only():
    assert ps.k8_flags("1.01,2.03,9.01") == ["重要な契約"] and ps.k8_flags("2.02,7.01,9.01") == []


def test_alerts_and_packet(monkeypatch):
    monkeypatch.setattr(ps, "_CACHE", _data())
    monkeypatch.setattr(ps, "_SI_ASKED", {"MAT"})
    alerts = ps.build_alerts(None, {"us_screener": {"candidates": [{"ticker": "MAT"}]}})
    jp, us = alerts[0]["body"], alerts[1]["body"]
    assert "シード（7743.T）" in jp and "アイビス" not in jp          # 安定株主の資産管理会社は外す
    assert "13D EdgePoint 17.7%" in us and "8-K 重要な契約" in us and "空売り残 1,000株" in us
    assert any("13D: EdgePoint" in x for x in ps.packet_lines("MAT"))
    assert any("Luxshare" in x for x in ps.packet_lines("7743.T"))
    assert ps.packet_lines("9999.T")[-1].startswith("- 動きなし")
