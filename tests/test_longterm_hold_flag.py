# ==============================================================
# tests/test_longterm_hold_flag.py（2026-07-25 バッチ1）
# [P-3] 長期保有候補フラグ — 閾値が config から読まれること
# ==============================================================
# 【背景】判定ロジック（片山晃モデル「10年持てる銘柄」）は
# stock_screener._calc_longterm_hold_flag() に実装され稼働していたが、
# 閾値 0.60 / 10.0 / 3 を直書きしており、config.py BLOCK P-3 の
# LONGTERM_HOLD_* は誰にも読まれていなかった（SSOT違反）。
# さらに config 側の値（0.45 / 0.08）は実装と食い違っており、
# 「config を直したのに判定が変わらない」状態だった。
#
# 2026-07-25 に config 参照へ是正。健太の方針「実装時の値を正」に従い
# config を 0.60 / 0.10 へ書き換えたため、判定結果は従来どおり。
# ==============================================================
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import stock_screener as ss                                    # noqa: E402


def _cfg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(REPO / "config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _data(equity_pct, roe, growth=0.12):
    return {"stockholders_equity": equity_pct, "total_assets": 100,
            "earnings_growth": growth, "roe": roe}


class TestConfigIsSourceOfTruth:
    def test_config_values_match_implementation_defaults(self):
        """config の値が実装の既定値（従来の直書き値）と一致すること。

        ここがズレると「config を直したのに判定が変わらない」に逆戻りする。
        """
        cfg = _cfg()
        assert cfg.LONGTERM_HOLD_EQUITY_RATIO_MIN == 0.60
        assert cfg.LONGTERM_HOLD_ROE_MIN == 0.10
        assert cfg.LONGTERM_HOLD_MIN_CONDITIONS == 3

    def test_wiring_does_not_change_behaviour(self):
        """cfg あり／なしで判定が一致すること（配線が挙動を変えていない）。"""
        cfg = _cfg()
        for d in (_data(70, 0.15), _data(50, 0.15), _data(60, 0.10),
                  _data(59, 0.15), _data(70, 0.09)):
            assert (ss._calc_longterm_hold_flag(d, cfg=cfg)[0]
                    == ss._calc_longterm_hold_flag(d, cfg=None)[0])

    def test_threshold_is_actually_read(self):
        """config を緩めれば判定が変わること（＝本当に読んでいる証拠）。"""
        class Loose:
            LONGTERM_HOLD_EQUITY_RATIO_MIN = 0.45
            LONGTERM_HOLD_ROE_MIN = 0.08
            LONGTERM_HOLD_MIN_CONDITIONS = 3

        d = _data(50, 0.09)          # 自己資本50% / ROE9% → 既定では不合格
        assert ss._calc_longterm_hold_flag(d, cfg=None)[0] is False
        assert ss._calc_longterm_hold_flag(d, cfg=Loose)[0] is True

    def test_min_conditions_is_read(self):
        """有効条件数の下限も config から読むこと。"""
        class Strict:
            LONGTERM_HOLD_EQUITY_RATIO_MIN = 0.60
            LONGTERM_HOLD_ROE_MIN = 0.10
            LONGTERM_HOLD_MIN_CONDITIONS = 99   # 到達不能な下限

        assert ss._calc_longterm_hold_flag(_data(70, 0.15), cfg=Strict)[0] is False


class TestRetiredBlocksAreGone:
    """バッチ1で削除した定数が復活していないこと。"""

    def test_viewer_spreadsheet_constants_removed(self):
        cfg = _cfg()
        for name in ("VIEWER_SPREADSHEET_ID", "VIEWER_SERVICE_ACCOUNT_PATH",
                     "VIEWER_WATCHLIST_SHEET"):
            assert not hasattr(cfg, name), (
                f"{name} が復活している。スプレッドシート連携は廃止済み"
                f"（viewer は Streamlit 版へ移行）"
            )

    def test_edinet_tier_constants_removed(self):
        cfg = _cfg()
        for name in ("EDINET_TIER_TOP_BOOST", "EDINET_TIER_BOT_PENALTY"):
            assert not hasattr(cfg, name), (
                f"{name} が復活している。legacy業種別ローテーションは "
                f"D-① で退役し、現行はEdinetDB→BQ経路"
            )
