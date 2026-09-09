# ==============================================================
# tests/conftest.py  (Wave TEST-1)
# 共通フィクスチャ — 外部API・ネットワーク接続ゼロで完結する
# ==============================================================
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """プロセス内キャッシュをテストごとに捨てる。

    残したままだと前のテストが掴んだ偽オブジェクトや結果が次のテストにも
    返ってしまい、monkeypatch した別の偽物が効かなくなる。
      - yf_cache: Ticker インスタンスと .info をモジュール変数に溜める
      - edinet_bq_client._RE_HOLDINGS_CACHE: 証券コード→保有不動産
    """
    def _clear():
        try:
            import yf_cache
            yf_cache.clear()
        except Exception:
            pass
        try:
            import edinet_bq_client
            edinet_bq_client._RE_HOLDINGS_CACHE.clear()
        except Exception:
            pass

    _clear()
    yield
    _clear()


def load_module(name: str):
    """リポジトリ直下のモジュールを importlib でロードする。"""
    spec = importlib.util.spec_from_file_location(name, str(REPO / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tmp_cfg(tmp_path):
    """ディスクI/Oを tmp_path に隔離した最小 config もどき。"""
    base = tmp_path / "stock_data"
    (base / "data").mkdir(parents=True)
    (base / "insights").mkdir()
    (base / "logs").mkdir()
    (base / "cache").mkdir()

    class FakeCfg:
        DRIVE_BASE = base
        DATA_DIR = base / "data"
        INSIGHT_DIR = base / "insights"
        LOG_DIR = base / "logs"
        CACHE_DIR = base / "cache"
        DAILY_MODULES = ["gov", "macro", "news", "screener"]
        LEDGER_CONSEC_FAIL_DAYS = 3
        LEDGER_HISTORY_DAYS = 14
        ENABLE_ALERT = False
        SCORE_MAX_TOTAL_ADJ_POS = 15.0
        SCORE_MAX_TOTAL_ADJ_NEG = -15.0
        MAX_SAME_DIRECTION_SIGNALS = 3
        INVESTMENT_DEPT_VAULT_DIR = str(tmp_path / "vault")

    (tmp_path / "vault").mkdir()
    return FakeCfg()
