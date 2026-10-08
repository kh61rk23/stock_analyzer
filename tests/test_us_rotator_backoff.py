# us_fundamental_rotator — 連続失敗銘柄の再試行バックオフ
#
# 財務XBRLを持たない銘柄（例: Grayscale信託）は last_fetched 無し＝
# 999日経過扱いで毎日キュー先頭に入り、1枠を潰し続けるため、バックオフで抑える。
import types
from datetime import date, timedelta

import us_fundamental_rotator as ufr


def _fail(t="MANA"):
    return {"ticker": t, "ok": False, "error": "SEC XBRLおよびYahoo財務の取得失敗"}


def test_failure_backoff_doubles():
    meta = {}
    dues = []
    for _ in range(4):
        ufr._update_meta(meta, _fail())
        dues.append(meta["MANA"]["next_due"])
    today = date.today()
    assert dues == [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in (1, 2, 4, 8)]


def test_success_clears_streak():
    meta = {}
    ufr._update_meta(meta, _fail())
    ufr._update_meta(meta, {"ticker": "MANA", "ok": True, "data": {}})
    assert "fail_streak" not in meta["MANA"]


def test_queue_skips_ticker_in_backoff(tmp_path, monkeypatch):
    udir = tmp_path / "u"
    udir.mkdir()
    (udir / "a.json").write_text('{"tickers": ["MANA", "JCI"]}', encoding="utf-8")
    cfg = types.SimpleNamespace(US_CUSTOM_UNIVERSE_DIR=str(udir),
                                US_WATCHLIST_TICKERS=[], US_DAILY_FETCH_LIMIT=20)
    meta = {}
    ufr._update_meta(meta, _fail())
    meta["MANA"]["next_due"] = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")
    q = ufr._build_fetch_queue(cfg, meta, {"MANA": "1852023", "JCI": "833444"})
    assert [x["ticker"] for x in q] == ["JCI"]

    meta["MANA"]["next_due"] = date.today().strftime("%Y-%m-%d")
    q = ufr._build_fetch_queue(cfg, meta, {"MANA": "1852023", "JCI": "833444"})
    assert "MANA" in [x["ticker"] for x in q]
