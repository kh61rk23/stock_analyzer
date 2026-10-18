# ==============================================================
# tests/conftest.py  (Wave TEST-1)
# 共通フィクスチャ — 外部API・ネットワーク接続ゼロで完結する
# ==============================================================
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

# テストが作業コピーの stock_data（キャッシュ・台帳）へ書かないよう、config を読む前に一時フォルダへ向ける。
# 明示的に STOCK_ANALYZER_BASE を渡した実行（CI の一部など）はそれを使う。
if not os.environ.get("STOCK_ANALYZER_BASE"):
    os.environ["STOCK_ANALYZER_BASE"] = tempfile.mkdtemp(prefix="stock_analyzer_test_base_")


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """プロセス内キャッシュをテストごとに捨てる。

    残したままだと前のテストが掴んだ偽オブジェクトや結果が次のテストにも
    返ってしまい、monkeypatch した別の偽物が効かなくなる。
      - yf_cache: Ticker インスタンスと .info をモジュール変数に溜める
      - edinet_bq_client._RE_HOLDINGS_CACHE: 証券コード→保有不動産
      - edinet_bq_client._FIN_LATEST_CACHE: 証券コード→最新期financials
      - edinet_bq_client._BUNDLE_CACHE: 証券コード→業種分析バンドル
      - geo_signal._EXPOSURE_CACHE: 銘柄→権益エクスポージャー
      - base_agent._SECTOR_TICKER_CACHE: 業種→ticker（先読みの起点）
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
            edinet_bq_client._FIN_LATEST_CACHE.clear()
            edinet_bq_client._BUNDLE_CACHE.clear()
        except Exception:
            pass
        try:
            import geo_signal
            geo_signal._EXPOSURE_CACHE.clear()
            geo_signal._EXPOSURE_BQ_DISABLED = False
        except Exception:
            pass
        try:
            import base_agent
            base_agent._SECTOR_TICKER_CACHE = None
        except Exception:
            pass

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _no_live_rates(request, monkeypatch):
    """メール組み立てのテストが国債利回りを実際に取りに行かないようにする。

    alert_notifier のメール組み立ては rates_monitor.get_rates を呼び、キャッシュが無いと
    財務省・FRED へ HTTP で取りに行く（テストが外部に出て、結果も日によって変わる）。
    rates_monitor 自体のテストは差し替えない。
    """
    if request.module.__name__.endswith("test_rates_monitor"):
        yield
        return
    try:
        import rates_monitor
        monkeypatch.setattr(rates_monitor, "get_rates",
                            lambda cfg=None, today=None: {"jp": {}, "us": {}, "policy": {},
                                                          "errors": []})
    except Exception:
        pass
    yield


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
