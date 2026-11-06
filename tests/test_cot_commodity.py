"""商品の COT: 位置の算出・逆張りの向き・業種への写し・上限・欠けの扱い・選んだ市場だけ残す・取り直しの判定・メール。"""
from datetime import date, timedelta
from types import SimpleNamespace

import cot_signal as cs
import market_positioning as mp

LAST = date(2026, 10, 27)
IMPACT = {"OILX": {"jp": [("1009", "+", 3), ("1022", "-", 3)], "us": [("XLE", "+", 3)]},
          "FOODX": {"jp": [("1004", "-", 3)], "us": [("XLP", "-", 3)]}}
CFG = SimpleNamespace(
    COT_SIGNAL_YEARS=3, COT_SIGNAL_MIN_WEEKS=52, COT_SIGNAL_PCT_HI=90.0, COT_SIGNAL_PCT_LO=10.0,
    COT_SIGNAL_CHANGE_WEEKS=4, COT_SIGNAL_STALE_DAYS=21, COT_MAIL_MAX_LINES=3, COT_MAIL_COMMODITY_MAX_LINES=2,
    COT_COMMODITY_PT=1.0, COT_COMMODITY_SECTOR_MAX_PT=2.0, COT_COMMODITY_BACKFILL_YEARS=3,
    JAPAN_SECTORS={"1009": {"name_ja": "石油・石炭製品"}, "1022": {"name_ja": "海運業"}, "1004": {"name_ja": "食料品"}},
    COT_LEGACY_EXTRA_CODES=("999001",),
    COT_SIGNAL_MARKETS={"wti": {"report": "legacy", "code": "067651", "name": "WTI原油先物", "risk_sign": 1, "w": 1.0},
                        "sp": {"report": "tff", "code": "13874A", "name": "S&P", "risk_sign": 1, "w": 1.0}},
    COT_COMMODITY_MARKETS={
        "wti": {"signal": "wti", "impact": "OILX"},
        "corn": {"code": "002602", "name": "コーン", "impact": "FOODX", "w": 0.5},
        "gold": {"code": "088691", "name": "金", "impact": "NOPE",
                 "jp": [("1013", "+", 2)], "us": [("GDX", "+", 3)]},
        "bad": {"signal": "sp", "impact": "OILX"},            # tff は商品ではない → 除く
        "ghost": {"signal": "unknown", "impact": "OILX"},      # 参照が解けない → 除く
    })


def _rows(code, nets, oi=1000.0):
    n = len(nets)
    return [{"report_date": LAST - timedelta(days=7 * (n - 1 - i)), "report": "legacy", "market_code": code,
             "open_interest": oi, "spec_long": 500.0 + v, "spec_short": 500.0} for i, v in enumerate(nets)]


UP, DOWN, FLAT = list(range(100)), list(range(99, -1, -1)), [50] * 100


def _sig(**nets):
    rows = []
    for code, v in nets.items():
        rows += _rows(code, v)
    return cs.build_signals(rows, CFG, today=LAST)


def test_commodity_markets_resolve_and_skip_bad():
    ms = cs.commodity_markets(CFG)
    assert set(ms) == {"wti", "corn", "gold"}
    assert ms["wti"]["code"] == "067651" and ms["wti"]["name"] == "WTI原油先物"   # COT_SIGNAL_MARKETS から引く
    assert ms["corn"]["w"] == 0.5
    assert cs.commodity_markets(SimpleNamespace(ENABLE_COT_COMMODITY=False)) == {}


def test_signals_contrarian_price_tilt_and_not_in_composite():
    s = _sig(**{"067651": UP, "002602": DOWN, "088691": FLAT})
    c = s["commodities"]
    assert c["wti"]["extreme"] == "long" and c["wti"]["price_tilt"] == -1       # 買い過ぎ → 価格は下げやすい
    assert c["corn"]["extreme"] == "short" and c["corn"]["price_tilt"] == 1
    assert c["gold"]["extreme"] is None and c["gold"]["price_tilt"] == 0
    assert s["commodity_extremes"][:2] and set(s["commodity_extremes"]) == {"wti", "corn"}
    assert set(s["markets"]) == {"wti"}                                          # 合成の市場は従来のまま
    assert s["markets"]["wti"]["risk_tilt"] == -1 and s["composite"] == -1.0


def test_missing_or_short_history_commodity_is_skipped():
    s = _sig(**{"067651": UP, "002602": UP[:30]})
    assert set(s["commodities"]) == {"wti"}
    assert cs.build_signals([], CFG, today=LAST)["commodities"] == {}


def test_sector_adjustments_direction_size_and_detail():
    s = _sig(**{"067651": UP, "002602": DOWN, "088691": FLAT})
    r = cs.sector_adjustments(s, CFG, IMPACT)
    # WTI 買い過ぎ → 価格↓: 石油(+3) は -1.0、海運(-3) は +1.0。コーン売り過ぎ → 価格↑: 食料品(-3)・w0.5 → -0.5
    assert r["jp"]["1009"]["delta"] == -1.0 and r["jp"]["1022"]["delta"] == 1.0
    assert r["jp"]["1004"]["delta"] == -0.5
    assert r["us"]["XLE"]["delta"] == -1.0 and r["us"]["XLP"]["delta"] == -0.5
    assert r["jp"]["1009"]["detail"] == [{"key": "wti", "name": "WTI原油先物", "delta": -1.0}]
    assert "1013" not in r["jp"] and r["as_of"] == s["as_of"]


def test_sector_adjustments_override_and_cap():
    s = _sig(**{"088691": DOWN})
    r = cs.sector_adjustments(s, CFG, IMPACT)           # 金: 売り過ぎ → 価格↑ → 非鉄 +2/3、GDX +1.0
    assert r["jp"]["1013"]["delta"] == 0.67 and r["us"]["GDX"]["delta"] == 1.0
    big = SimpleNamespace(**{**vars(CFG), "COT_COMMODITY_PT": 10.0})
    assert cs.sector_adjustments(s, big, IMPACT)["us"]["GDX"]["delta"] == 2.0   # 上限で打ち止め
    assert cs.sector_adjustments(s, big, IMPACT)["us"]["GDX"]["detail"][0]["delta"] == 10.0   # 内訳は打ち止め前


def test_sector_adjustments_empty_when_stale_none_or_disabled():
    s = _sig(**{"067651": UP})
    empty = {"jp": {}, "us": {}, "as_of": None, "extremes": []}
    assert cs.sector_adjustments(None, CFG, IMPACT) == empty
    assert cs.sector_adjustments(s, CFG, IMPACT)["jp"]
    old = dict(s, as_of=(LAST - timedelta(days=60)).isoformat())
    assert cs.sector_adjustments(old, CFG, IMPACT) == empty
    off = SimpleNamespace(**{**vars(CFG), "ENABLE_COT_COMMODITY": False})
    assert cs.sector_adjustments(s, off, IMPACT) == empty
    assert cs.sector_adjustments({"as_of": s["as_of"]}, CFG, IMPACT)["jp"] == {}     # 旧形式のファイル（commodities 無し）
    # 極端が無ければ何も足さない
    assert cs.sector_adjustments(_sig(**{"067651": FLAT}), CFG, IMPACT)["jp"] == {}


def test_mail_lines_commodity_section_and_limit(monkeypatch):
    monkeypatch.setattr(cs, "_impact_map", lambda: IMPACT)
    s = _sig(**{"067651": UP, "002602": DOWN, "088691": UP})
    lines = cs.mail_lines(s, CFG)
    com = [l for l in lines if "[商品]" in l]
    assert len(com) == 2                                    # COT_MAIL_COMMODITY_MAX_LINES
    assert any("WTI原油先物" in l and "価格は下げやすい" in l and "追い風:海運業" in l and "逆風:石油・石炭製品" in l for l in com)
    assert lines[-1].startswith("  （CFTC")
    off = SimpleNamespace(**{**vars(CFG), "ENABLE_COT_COMMODITY": False})
    assert not [l for l in cs.mail_lines(s, off) if "[商品]" in l]
    assert cs.mail_lines(dict(s, as_of="2020-01-01"), CFG) == []


def test_build_query_includes_commodity_codes_once():
    q = cs.build_query(CFG)
    assert "'002602'" in q and "'088691'" in q and q.count("'067651'") == 1 and "13874A" in q


def test_keep_only_selected_legacy_markets():
    rows = [{"report": "legacy", "market_code": c} for c in ("067651", "002602", "088691", "999001", "111111")]
    rows += [{"report": "tff", "market_code": "ZZZZZZ"}]
    kept = [r["market_code"] for r in mp.keep_cot_markets(rows, CFG)]
    assert kept == ["067651", "002602", "088691", "999001", "ZZZZZZ"]
    assert mp.cot_keep_codes(CFG) == {"067651", "002602", "088691", "999001"}


def test_commodity_backfill_only_when_new_markets_or_forced():
    today = date(2026, 11, 6)
    codes, weeks = mp.commodity_backfill({}, CFG, today)
    assert codes == {"067651", "002602", "088691", "999001"} and weeks
    assert min(weeks) <= today - timedelta(days=365 * 3) and max(weeks) == date(2026, 11, 2)
    state = {"cot": {"legacy_codes": sorted(codes)}}
    assert mp.commodity_backfill(state, CFG, today)[1] == []                     # 増えていなければ取り直さない
    assert mp.commodity_backfill(state, CFG, today, force=True)[1]               # 強制
    state["cot"]["legacy_codes"] = ["067651"]
    assert mp.commodity_backfill(state, CFG, today)[1]                           # 足した市場がある
    # 外した週は weeks_to_run で再び対象になる
    done = {"cot": {"both": [w.isoformat() for w in weeks]}}
    assert not [w for w in mp.weeks_to_run(done, "both", today, 0, today - timedelta(days=365))]
    done["cot"]["both"] = sorted(set(done["cot"]["both"]) - {w.isoformat() for w in weeks[:3]})
    assert set(mp.weeks_to_run(done, "both", today, 0, today - timedelta(days=365))) == set(weeks[:3])


def test_real_config_is_consistent():
    import config
    ms = cs.commodity_markets(config)
    assert 28 <= len(ms) <= 40
    codes = [m["code"] for m in ms.values()]
    assert len(codes) == len(set(codes)), "同じ市場コードが重複"
    from bond_commodity_pipeline import SECTOR_IMPACT_MAP
    for k, m in ms.items():
        assert m["impact"] in SECTOR_IMPACT_MAP, k
        for code, d, mag in cs._targets(m, SECTOR_IMPACT_MAP)["jp"]:
            assert code in config.JAPAN_SECTORS and d in "+-" and 1 <= mag <= 3, (k, code)
    assert {"wti", "gold", "copper"} <= set(ms) and all(ms[k].get("signal") == k for k in ("wti", "gold", "copper"))
    assert cs.legacy_codes(config) >= {m["code"] for m in ms.values()}
