# ==============================================================
# tests/test_sector_modules.py  (U-10: JP/US横断セクター系モジュールの統廃合)
# ==============================================================
# 【対象】
#   jp_us_sector_bridge.py / jp_us_sector_correlation.py /
#   sector_rotation_detector.py / us_sector_bridge_enhanced.py
#
# 【検証内容】
#   1. 4モジュールが import できること
#   2. 各モジュールの run_* エントリポイントが存在し、呼び出し可能であること
#      （score_history.json 等の入力データが無い場合でも例外を投げず、
#       "skipped" / "insufficient_data" 等の安全なデフォルト値を返すこと）
#   3. 各モジュール冒頭のコメントヘッダ（本リポジトリの docstring 慣習）に
#      「入力」「出力」「消費者」の記述が含まれること（R10-1 の機械検査）
#
# 【U-10 のスコープ注記】
#   R10-2（重複の共通化）は、唯一の実質的な重複候補（_calc_trend /
#   _calc_slope の線形回帰スロープ計算）が finance_logic.py への
#   委譲を要するところ、finance_logic.py は本タスクでは「読むだけ」
#   （別作業の対象外ファイル）に指定されているため、本セッションでは
#   commonize を実施していない。したがって「委譲前後で同じ値を返すこと」
#   を検証するテストはここには無い（対象がゼロのため）。
# ==============================================================

from __future__ import annotations

from pathlib import Path

import jp_us_sector_bridge
import jp_us_sector_correlation
import sector_rotation_detector
import us_sector_bridge_enhanced

REPO = Path(__file__).resolve().parent.parent

MODULES = {
    "jp_us_sector_bridge":       jp_us_sector_bridge,
    "jp_us_sector_correlation":  jp_us_sector_correlation,
    "sector_rotation_detector":  sector_rotation_detector,
    "us_sector_bridge_enhanced": us_sector_bridge_enhanced,
}

RUN_FUNC_NAMES = {
    "jp_us_sector_bridge":       "run_jp_us_bridge",
    "jp_us_sector_correlation":  "run_jp_us_sector_correlation",
    "sector_rotation_detector":  "run_sector_rotation_detector",
    "us_sector_bridge_enhanced": "run_us_sector_bridge_enhanced",
}


# ==============================================================
# 1. import できること
# ==============================================================

class TestImports:
    def test_all_four_modules_importable(self):
        for name, mod in MODULES.items():
            assert mod is not None, f"{name} が import できない"


# ==============================================================
# 2. run_* が存在し、呼び出し可能であること
# ==============================================================

class TestRunEntrypointsExist:
    def test_run_functions_exist_and_callable(self):
        for name, func_name in RUN_FUNC_NAMES.items():
            mod = MODULES[name]
            assert hasattr(mod, func_name), f"{name} に {func_name} が無い"
            assert callable(getattr(mod, func_name)), f"{name}.{func_name} が呼び出し不可"


class TestRunEntrypointsSafeWithEmptyData(object):
    """
    score_history.json 等が存在しない環境（tmp_cfg）で呼んでも
    例外を投げずに安全なデフォルト値を返すことを確認する。
    実データでの相関計算・yfinance 取得までは検証しない
    （ネットワークI/Oゼロで完結させる方針は conftest.py の設計に合わせる）。

    ★ jp_us_sector_correlation._load_jp_score_history() と
      sector_rotation_detector._load_score_history() は、DATA_DIR 経由の
      候補に加えて相対パス固定の候補（"stock_data/data/score_history.json" 等・
      カレントディレクトリ基準）を持つ（U-7 判定により本タスクでは変更禁止の
      既存挙動）。pytest 実行時の cwd がリポジトリ直下だと本物の
      score_history.json を拾ってしまい、後者のテストは yfinance への
      実ネットワークアクセスまで発生してテストが不安定になる。
      monkeypatch.chdir(tmp_path) で cwd を隔離し、相対パス候補も
      空の tmp_path 配下を指すようにする。
    """

    def test_jp_us_bridge_returns_skipped_without_score_history(
        self, tmp_cfg, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        result = jp_us_sector_bridge.run_jp_us_bridge(tmp_cfg, {})
        assert isinstance(result, dict)
        assert result.get("skipped") is True

    def test_jp_us_sector_correlation_returns_insufficient_data(
        self, tmp_cfg, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        result = jp_us_sector_correlation.run_jp_us_sector_correlation(tmp_cfg)
        assert isinstance(result, dict)
        assert result.get("stats", {}).get("insufficient_data") is True
        assert result.get("significant_pairs") == []

    def test_sector_rotation_detector_returns_insufficient_data(
        self, tmp_cfg, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        result = sector_rotation_detector.run_sector_rotation_detector(tmp_cfg)
        assert isinstance(result, dict)
        assert result.get("summary", {}).get("insufficient_data") is True
        assert result.get("quadrants") == {}

    def test_us_sector_bridge_enhanced_returns_empty_without_earnings(self, tmp_cfg):
        result = us_sector_bridge_enhanced.run_us_sector_bridge_enhanced(tmp_cfg, {})
        assert isinstance(result, dict)
        assert result.get("cross_signals") == []
        assert result.get("mapping_updated") == 0

    def test_us_sector_bridge_enhanced_respects_enable_flag(self, tmp_cfg):
        tmp_cfg.ENABLE_US_SECTOR_BRIDGE = False
        result = us_sector_bridge_enhanced.run_us_sector_bridge_enhanced(tmp_cfg, {})
        assert result == {"skipped": True}


# ==============================================================
# 3. docstring（ファイル冒頭コメントヘッダ）機械検査
# ==============================================================

class TestDocstringCoverage:
    """
    R10-1: 4モジュールのファイル冒頭ヘッダに「入力」「出力」「消費者」の
    記述があることを機械検査する。本リポジトリは実際の python docstring
    ではなく `#` コメントブロックをファイル冒頭の説明として使う慣習
    （他の全モジュール同様）のため、__doc__ ではなくソース先頭を直接読む。
    """

    REQUIRED_MARKERS = ["入力", "出力", "消費者"]

    FILES = [
        "jp_us_sector_bridge.py",
        "jp_us_sector_correlation.py",
        "sector_rotation_detector.py",
        "us_sector_bridge_enhanced.py",
    ]

    def _header(self, filename: str, n_lines: int = 200) -> str:
        path = REPO / filename
        lines = path.read_text(encoding="utf-8").splitlines()[:n_lines]
        return "\n".join(lines)

    def test_required_markers_present(self):
        for filename in self.FILES:
            header = self._header(filename)
            for marker in self.REQUIRED_MARKERS:
                assert marker in header, (
                    f"{filename} のファイル冒頭ヘッダに『{marker}』の記述が無い"
                )

    def test_bridge_vs_correlation_disambiguation_documented(self):
        """
        jp_us_sector_bridge と jp_us_sector_correlation は名前が紛らわしいため、
        互いを名指しした違いの説明があることを機械検査する。
        """
        bridge_header = self._header("jp_us_sector_bridge.py")
        corr_header = self._header("jp_us_sector_correlation.py")
        assert "jp_us_sector_correlation" in bridge_header
        assert "jp_us_sector_bridge" in corr_header

    def test_known_state_documented(self):
        """
        R10-1: 実効出力ゼロ・原因・J-04保留中であることの明記を機械検査する。
        """
        for filename in self.FILES:
            header = self._header(filename)
            assert "J-04" in header, f"{filename} に J-04 の言及が無い"
