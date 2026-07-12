# ==============================================================
# tests/test_dd_controller.py — R3-3 DD制御の受入テスト
# ==============================================================
# 受入条件（要件定義書 §R3-3）:
#   状態遷移の全パス（6遷移）のpytest。dd_state.json破損時はNORMALから安全再開。
# ==============================================================
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from dd_controller import (_load_state, compute_dd, compute_portfolio_equity,   # noqa: E402
                           next_state, run_dd_controller, state_action)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """ネットワークゼロ（conftest規約）: fetch_price_safe を価格None返しに固定。
    → compute_portfolio_equity は簿価評価にフォールバックし、リトライ待ちゼロ。"""
    import data_reliability
    monkeypatch.setattr(data_reliability, "fetch_price_safe",
                        lambda cfg, ticker, **kw: {"close": None, "error": "test"})


class DDCfg:
    DD_CAUTION = 0.15
    DD_DEFENSE = 0.25
    DD_CAUTION_EXIT = 0.10
    DD_DEFENSE_EXIT = 0.20
    ENABLE_DD_CONTROL = True

    def __init__(self, tmp):
        self.DATA_DIR = tmp / "data"
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.PROJECT_DIR = tmp
        self.PORTFOLIO_HOLDINGS_PATH = self.DATA_DIR / "portfolio_holdings.csv"


class TestStateMachine:
    """6遷移の全パス（要件§R3-3）。"""
    def test_normal_to_caution(self):
        assert next_state("NORMAL", 0.15) == "CAUTION"     # ①
        assert next_state("NORMAL", 0.149) == "NORMAL"     # 境界内は維持

    def test_caution_to_defense(self):
        assert next_state("CAUTION", 0.25) == "DEFENSE"    # ②

    def test_caution_to_normal(self):
        assert next_state("CAUTION", 0.09) == "NORMAL"     # ③（ヒステリシス<0.10）
        assert next_state("CAUTION", 0.12) == "CAUTION"    # 0.10-0.25は維持

    def test_defense_to_caution(self):
        assert next_state("DEFENSE", 0.19) == "CAUTION"    # ④（<0.20）
        assert next_state("DEFENSE", 0.21) == "DEFENSE"    # 維持

    def test_hysteresis_no_flap(self):
        """dd=0.12 では NORMAL維持/CAUTION維持（振動しない）。"""
        assert next_state("NORMAL", 0.12) == "NORMAL"      # 0.15未満→上がらない
        assert next_state("CAUTION", 0.12) == "CAUTION"    # 0.10以上→下がらない

    def test_full_cycle(self):
        """NORMAL→CAUTION→DEFENSE→CAUTION→NORMAL の一巡（残り2遷移含む）。"""
        s = "NORMAL"
        s = next_state(s, 0.16); assert s == "CAUTION"
        s = next_state(s, 0.26); assert s == "DEFENSE"
        s = next_state(s, 0.18); assert s == "CAUTION"     # DEFENSE→CAUTION
        s = next_state(s, 0.05); assert s == "NORMAL"      # CAUTION→NORMAL

    def test_unknown_state_is_normal(self):
        assert next_state("GARBAGE", 0.05) == "NORMAL"
        assert next_state("", 0.16) == "CAUTION"


class TestCompute:
    def test_compute_dd(self):
        assert compute_dd(80, 100) == pytest.approx(0.20)
        assert compute_dd(120, 100) == 0.0                 # 最高値更新中
        assert compute_dd(50, 0) == 0.0                    # peak不正

    def test_state_action(self):
        assert state_action("NORMAL")["core_new_buy"] == "allow"
        assert state_action("CAUTION")["casino_new_buy"] == "half_size"
        assert state_action("DEFENSE")["core_new_buy"] == "halt"
        assert state_action("DEFENSE")["casino_new_buy"] == "halt"


class TestPersistence:
    def test_corrupt_state_recovers_to_normal(self, tmp_path):
        cfg = DDCfg(tmp_path)
        (cfg.DATA_DIR / "dd_state.json").write_text("{ broken json",
                                                    encoding="utf-8")
        st = _load_state(cfg)
        assert st["state"] == "NORMAL" and st["peak"] == 0.0

    def test_missing_state_is_normal(self, tmp_path):
        cfg = DDCfg(tmp_path)
        st = _load_state(cfg)
        assert st["state"] == "NORMAL"

    def test_valid_state_loads(self, tmp_path):
        cfg = DDCfg(tmp_path)
        (cfg.DATA_DIR / "dd_state.json").write_text(json.dumps(
            {"state": "CAUTION", "peak": 1_000_000}), encoding="utf-8")
        st = _load_state(cfg)
        assert st["state"] == "CAUTION" and st["peak"] == 1_000_000


class TestRunEndToEnd:
    def _write_holdings(self, cfg, rows):
        lines = ["ticker,quantity,avg_cost_jpy"]
        lines += [f"{t},{q},{c}" for t, q, c in rows]
        cfg.PORTFOLIO_HOLDINGS_PATH.write_text("\n".join(lines), encoding="utf-8")

    def test_empty_holdings_skips(self, tmp_path):
        cfg = DDCfg(tmp_path)
        r = run_dd_controller(cfg, {})
        assert r["skipped"] is True

    def test_equity_from_book_value_when_no_network(self, tmp_path):
        """価格取得不能時は簿価で評価（fetch_price_safeがNone/例外でも動く）。"""
        cfg = DDCfg(tmp_path)
        self._write_holdings(cfg, [("XXXX.T", 100, 1000)])  # 実在しないticker
        equity, priced = compute_portfolio_equity(cfg)
        # 価格取得できなくても簿価 100×1000=100,000 で評価される
        assert equity == pytest.approx(100_000)

    def test_peak_persists_and_dd_rises(self, tmp_path):
        cfg = DDCfg(tmp_path)
        # 初回: peak=equity=100,000, dd=0 → NORMAL
        self._write_holdings(cfg, [("XXXX.T", 100, 1000)])
        r1 = run_dd_controller(cfg, {})
        assert r1["state"] == "NORMAL" and r1["dd"] == 0.0
        assert r1["peak"] == pytest.approx(100_000)
        # 2回目: 簿価を下げ evaluation下落 → peak据置でDD発生
        self._write_holdings(cfg, [("XXXX.T", 100, 800)])   # -20%
        r2 = run_dd_controller(cfg, {})
        assert r2["peak"] == pytest.approx(100_000)          # peak維持
        assert r2["dd"] == pytest.approx(0.20)
        assert r2["state"] == "CAUTION"                       # dd>=0.15
