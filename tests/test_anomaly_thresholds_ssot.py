# ==============================================================
# tests/test_anomaly_thresholds_ssot.py（2026-07-25 バッチ2）
# 異常検知の閾値が config.py を正本として読まれること
# ==============================================================
# 【背景】config.py BLOCK 21 の ANOMALY_* と anomaly_detector.py の
# モジュール定数が**同じ意味・同じ値で別名の二重定義**になっており、
# 実装はモジュール側しか読んでいなかった。そのため config.py の
# ANOMALY_* を編集しても検知閾値は一切変わらなかった（SSOT違反）。
#
# 検知関数は cfg を引数に取らない設計なので、cfg を持つ
# run_anomaly_scan() の冒頭で sync_thresholds_from_config() を呼び、
# モジュール定数へ取り込む方式にした。
# ==============================================================
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import anomaly_detector as ad                                   # noqa: E402


def _cfg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(REPO / "config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _restore_module_defaults():
    """グローバルを書き換えるテストなので、毎回元へ戻す。"""
    saved = {m: getattr(ad, m) for m in ad._CFG_THRESHOLD_MAP.values()}
    yield
    for name, value in saved.items():
        setattr(ad, name, value)


class TestConfigIsSourceOfTruth:
    def test_config_values_match_module_defaults(self):
        """config の値がモジュール既定値と一致すること。

        ここがズレたまま取り込むと検知結果が黙って変わる。
        """
        cfg = _cfg()
        for cfg_name, mod_name in ad._CFG_THRESHOLD_MAP.items():
            assert float(getattr(cfg, cfg_name)) == float(getattr(ad, mod_name)), (
                f"{cfg_name} と {mod_name} の値が食い違っている"
            )

    def test_sync_applies_config_values(self):
        """config の値がモジュール定数へ反映されること。"""
        class Cfg:
            ANOMALY_Z_HIGH = 9.9
            ANOMALY_PRICE_CHANGE_HIGH = 0.42

        ad.sync_thresholds_from_config(Cfg)
        assert ad.Z_THRESHOLD_HIGH == 9.9
        assert ad.PRICE_CHANGE_HIGH == 0.42

    def test_missing_attrs_keep_module_defaults(self):
        """cfg に無い項目は既定値を保つこと（部分定義でも壊れない）。"""
        before = ad.VOLUME_SPIKE_RATIO

        class Partial:
            ANOMALY_Z_HIGH = 3.3

        ad.sync_thresholds_from_config(Partial)
        assert ad.Z_THRESHOLD_HIGH == 3.3
        assert ad.VOLUME_SPIKE_RATIO == before

    def test_cfg_none_is_safe(self):
        before = {m: getattr(ad, m) for m in ad._CFG_THRESHOLD_MAP.values()}
        ad.sync_thresholds_from_config(None)
        for name, value in before.items():
            assert getattr(ad, name) == value

    def test_non_numeric_is_ignored(self):
        """数値でない設定値は無視して既定値を守ること。"""
        before = ad.Z_THRESHOLD_HIGH

        class Bad:
            ANOMALY_Z_HIGH = "とても高い"

        ad.sync_thresholds_from_config(Bad)
        assert ad.Z_THRESHOLD_HIGH == before

    def test_map_covers_all_anomaly_constants(self):
        """config の ANOMALY_* 閾値が取り込み対象から漏れていないこと。

        新しい閾値を config に足して対応表に入れ忘れると、
        また「config を直しても効かない」に戻る。
        """
        cfg = _cfg()
        numeric_anomaly = {
            n for n in dir(cfg)
            if n.startswith("ANOMALY_")
            and isinstance(getattr(cfg, n), (int, float))
            and not isinstance(getattr(cfg, n), bool)
        }
        assert numeric_anomaly <= set(ad._CFG_THRESHOLD_MAP), (
            f"取り込み対象から漏れている: "
            f"{numeric_anomaly - set(ad._CFG_THRESHOLD_MAP)}"
        )


class TestDetectionUsesSyncedThresholds:
    @staticmethod
    def _hist():
        """最終日の変動が +1% の系列。

        Zスコア法とIQR法が誤発火しないよう、日々の値動きに幅を持たせている
        （全て同値だと std=0・IQR=0 になり、閾値と無関係に異常判定される）。
        """
        closes = [100.0 + (i % 7) for i in range(28)]
        closes.append(round(closes[-1] * 1.01, 4))       # 最終日 +1%
        dates = [f"2026-06-{i + 1:02d}" for i in range(len(closes))]
        return {"close": closes, "dates": dates,
                "volume": [1000] * len(closes)}

    def test_price_anomaly_follows_config(self):
        """閾値を緩めれば、従来は検知されない変動が検知されること。

        既定は MEDIUM=3% なので +1% は非検知。0.2% まで緩めれば検知される。
        ＝ config の値が実際に判定へ効いている証拠。
        """
        hist = self._hist()

        class Loose:
            ANOMALY_PRICE_CHANGE_HIGH = 0.005    # 0.5%
            ANOMALY_PRICE_CHANGE_MEDIUM = 0.002  # 0.2%

        assert ad.detect_price_anomaly("X.T", hist) is None
        ad.sync_thresholds_from_config(Loose)
        result = ad.detect_price_anomaly("X.T", hist)
        assert result is not None
        assert result.get("severity") in ("medium", "high")


class TestRetiredConstantsAreGone:
    def test_bond_commodity_constants_removed(self):
        """キャッシュ・補正上限は実装が無いため削除済み。"""
        cfg = _cfg()
        for name in ("BOND_COMMODITY_CACHE_DAYS", "BOND_COMMODITY_DELTA_CAP"):
            assert not hasattr(cfg, name), f"{name} が復活している"

    def test_module_side_cache_constant_removed(self):
        import bond_commodity_pipeline as bcp
        assert not hasattr(bcp, "COMMODITY_CACHE_DAYS"), (
            "COMMODITY_CACHE_DAYS が復活している。"
            "実装する場合は config を正本にすること"
        )
