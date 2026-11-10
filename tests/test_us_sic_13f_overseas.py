"""SIC→日本の業種の対応・13F の米国銘柄の業種加点・米国スクリーナーの海外株分離を固定する。"""

import json
import types

import config
import institutional_13f_monitor as m
import us_sector_bridge_enhanced as br
import us_stock_screener as scr


# ── SIC → 日本の業種 ─────────────────────────────────────────

def test_oil_gas_extraction_is_mining_and_refining_is_oil_products():
    assert br._sic_to_jp_sectors(1311) == ["1002"]
    assert br._sic_to_jp_sectors(1389) == ["1002"]
    assert br._sic_to_jp_sectors(2911) == ["1009"]


def test_pharma_is_not_chemicals_and_group_fallbacks_do_not_misfile():
    assert br._sic_to_jp_sectors(2834) == ["1008"]          # 2桁 28（化学）に落ちない
    assert br._sic_to_jp_sectors(2800) == ["1007"]
    assert br._sic_to_jp_sectors(3572) == ["1016"]          # 2桁 35（機械）に落ちない
    assert br._sic_to_jp_sectors(3331) == ["1013"]          # 2桁 33（鉄鋼）に落ちない
    assert "1016" in br._sic_to_jp_sectors(3559)            # 半導体製造装置は電気機器にも


def test_finance_split_and_services():
    assert br._sic_to_jp_sectors(6021) == ["1028"]          # 銀行
    assert br._sic_to_jp_sectors(6211) == ["1029"]          # 証券
    assert br._sic_to_jp_sectors(6331) == ["1030"]          # 保険
    assert br._sic_to_jp_sectors(6141) == ["1031"]          # その他金融
    assert br._sic_to_jp_sectors(6798) == ["1032"]          # REIT
    assert br._sic_to_jp_sectors(4953) == ["1033"]          # 廃棄物はサービス業
    assert br._sic_to_jp_sectors(7812) == ["1025"]


def test_every_mapped_jp_sector_exists():
    codes = set(config.JAPAN_SECTORS)
    for v in list(br.SIC_TO_JP_SECTORS.values()) + list(br.SIC_2DIGIT_TO_JP.values()):
        assert set(v) <= codes


# ── 13F の業種加点 ───────────────────────────────────────────

def _cfg(tmp_path, **kw):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return types.SimpleNamespace(DATA_DIR=str(d), SCORE_MAX_INSTITUTION_ADJ=15.0, **kw)


def _mapping(cfg, rows):
    (m.Path(cfg.DATA_DIR) / br.MAPPING_FILE).write_text(json.dumps(rows), encoding="utf-8")


def _buy(t, adj=10.0, ct="new_position", inst="X"):
    return {"ticker": t, "adj_pt": adj, "change_type": ct, "institution": inst}


def test_us_buys_boost_japan_sector_via_sic_with_cap(tmp_path):
    cfg = _cfg(tmp_path, I13F_US_BOOST_WEIGHT=0.1, I13F_US_BOOST_SECTOR_CAP=1.5)
    _mapping(cfg, {"XOM": {"sic": 1311, "jp_sectors": ["1002"]},
                   "COP": {"sic": 1311, "jp_sectors": ["1002"]},
                   "AAPL": {"sic": 3571, "jp_sectors": ["1016"]}})
    scored = [_buy("XOM"), _buy("COP"), _buy("XOM", inst="Y"), _buy("AAPL", 5.0)]
    boosts, detail, etf = m._build_sector_boost_detail(scored, {}, cfg)
    assert boosts["1002"] == 1.5                       # 3件 ×1.0 → 上限
    assert boosts["1016"] == 0.5
    assert detail["1002"]["us_13f_pt"] == 1.5 and len(detail["1002"]["us_items"]) == 3
    assert etf["XLE"] == 1.5 and etf["XLK"] == 0.5     # 米国 ETF 側にも記録
    assert m._build_sector_boosts(scored, {}, cfg) == boosts


def test_sells_decreases_and_overseas_do_not_boost(tmp_path):
    cfg = _cfg(tmp_path, I13F_US_BOOST_SIC_LOOKUP_MAX=0)
    _mapping(cfg, {"XOM": {"sic": 1311, "jp_sectors": ["1002"]}})
    scored = [_buy("XOM", -10.0, "sold"), _buy("XOM", 5.0, "decreased"), _buy("0005.HK")]
    boosts, detail, etf = m._build_sector_boost_detail(scored, {}, cfg)
    assert boosts == {} and etf == {}


def test_screener_path_still_adds_and_unmapped_us_ticker_is_skipped(tmp_path):
    cfg = _cfg(tmp_path, I13F_US_BOOST_SIC_LOOKUP_MAX=0)
    results = {"screener": {"candidates": [{"ticker": "1605.T", "sector_code": "1002"}]}}
    scored = [_buy("1605.T", 6.0), _buy("ZZZZ")]
    boosts, detail, _ = m._build_sector_boost_detail(scored, results, cfg)
    assert boosts == {"1002": 6.0} and detail["1002"]["screener_pt"] == 6.0


def test_sic_to_us_etf():
    assert m._sic_to_us_etf(1311) == "XLE"
    assert m._sic_to_us_etf(2834) == "XLV"
    assert m._sic_to_us_etf(3674) == "XLK"
    assert m._sic_to_us_etf(6021) == "XLF"
    assert m._sic_to_us_etf(9999) == ""


# ── 米国スクリーナーの海外株分離 ─────────────────────────────

def test_split_overseas_keeps_us_share_class_and_flags_region():
    # 米国の取引所で取引できる ADR（接尾辞なし）は外国企業でも米国側に残す
    rows = [{"ticker": t} for t in ["AAPL", "BRK-B", "TSM", "BABA", "SONY", "TM", "005930.KS", "LUN.TO", "601958.SS", "6285.TW", "9766.T"]]
    us, ov = scr.split_overseas(rows)
    assert [c["ticker"] for c in us] == ["AAPL", "BRK-B", "TSM", "BABA", "SONY", "TM"]
    assert [c["ticker"] for c in ov] == ["005930.KS", "LUN.TO", "601958.SS", "6285.TW", "9766.T"]
    assert all(c["region"] == "overseas" for c in ov)


def test_load_universe_drops_japan_tickers(tmp_path):
    ud = tmp_path / "u"
    ud.mkdir()
    (ud / "theme.json").write_text(json.dumps(
        {"tickers": ["NVDA", "9766.T", "005930.KS", "7203.T"], "business_model": "mixed"}), encoding="utf-8")
    cfg = types.SimpleNamespace(US_CUSTOM_UNIVERSE_DIR=str(ud), DRIVE_BASE="", US_SCREENER_CATEGORIES=["theme"],
                                US_SCREENER_MAX_TICKERS=100)
    got = [e["ticker"] for e in scr.load_universe(cfg)]
    assert got == ["NVDA", "005930.KS"]
