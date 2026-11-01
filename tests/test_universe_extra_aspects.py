"""一次採点の追加観点（地政学・大株主の動き・需給）：11観点の点を変えず、小さな重みで重ねる。"""
import screener_universe as su
import positioning_signals as ps

W = {a: 1.0 for a in su.ASPECTS}
EXTRA_W = {"geo": 0.03, "holder": 0.02, "supply": 0.02}


def _records():
    out = []
    for i in range(6):
        out.append({"ticker": f"{i}.T", "cols": {"r1m": float(i), "ud_vol": float(i)}, "scores": {"technical": [50.0 + i]},
                    "extras": {"geo": None, "holder": 90.0 if i == 2 else None}})
    return out


def test_aspects_unchanged_by_extras():
    a = su.score_universe(_records(), W)
    b = su.score_universe(_records(), W, extra_weights=EXTRA_W)
    assert {r["ticker"]: r["aspects"] for r in a} == {r["ticker"]: r["aspects"] for r in b}
    assert all(r["extra_scores"] == {} for r in a)
    assert set(b[0]["extra_scores"]) == {"geo", "holder", "supply"}


def test_extra_overlay_neutral_when_missing_and_bounded():
    s, used = su.extra_overlay(80.0, {}, EXTRA_W)
    assert used == {"geo": 50.0, "holder": 50.0, "supply": 50.0}
    assert abs(s - (0.93 * 80.0 + 0.07 * 50.0)) < 0.01
    assert su.extra_overlay(80.0, {"geo": 100}, {"geo": 1.0}) == (80.0, {})
    assert su.extra_overlay(80.0, {"geo": 100}, None) == (80.0, {})


def test_holder_raises_score_by_small_amount():
    base = {r["ticker"]: r for r in su.score_universe(_records(), W)}
    ext = {r["ticker"]: r for r in su.score_universe(_records(), W, extra_weights=EXTRA_W)}
    d = ext["2.T"]["universe_score"] - base["2.T"]["universe_score"]
    # 大株主が重みの0.02ぶん・50→90 で押し上げるのは小さい
    assert -2.0 < d < 2.0
    assert ext["2.T"]["extra_scores"]["holder"] == 90.0


def test_updown_volume():
    closes = [10, 11, 10, 11, 12, 11]
    vols = [0, 100, 50, 100, 100, 20]
    assert su.updown_volume(closes, vols, n=5) == 300 / 70
    assert su.updown_volume([10, 11], [1, 1], n=20) is None
    assert su.updown_volume([10, 11, 12, 13], [1, 1, 1, 1], n=4) is None   # 下げた日が無い


def test_holder_pts_jp_active_only():
    rows = [{"issuer_sec_code": "72030", "holder_name": "A", "holder_kind": "法人", "prior_ratio": 0.05,
             "holding_ratio": 0.08, "purpose": "重要提案行為を行うこと"},
            {"issuer_sec_code": "67580", "holder_name": "B", "holder_kind": "法人", "prior_ratio": 0.05,
             "holding_ratio": 0.08, "purpose": "資産管理"},
            {"issuer_sec_code": "99840", "holder_name": "C", "holder_kind": "法人", "prior_ratio": 0.05,
             "holding_ratio": 0.055, "purpose": "重要提案"}]
    pts = ps.holder_pts_jp(rows)
    assert list(pts) == ["7203.T"] and abs(pts["7203.T"] - (1.0 + 0.5 * 3)) < 1e-9


def test_us_signs_default_off():
    assert ps.us_13d_pts([{}, {}]) == 0.0
    assert ps.short_interest_pts({"days_to_cover": 8}) is None
    assert ps.short_interest_pts({"days_to_cover": 20}, sign=-1) == -10.0
