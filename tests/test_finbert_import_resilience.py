# transformers のメジャー更新で FinBERT が黙って無効化されないこと
#
# 2026-09-11: requirements.txt が `transformers>=4.30.0`（上限なし）だったため
# CI が 6.0.1 を取得し、v6 でトップレベルの `pipeline` エクスポートが
# 無くなったことで FinBERT が丸ごと無効化されていた。
#   ⚠️ [finbert] analyze_sections: pipeline初期化失敗
#      → cannot import name 'pipeline' from 'transformers'
# 実行自体は success のまま進むため、出力が中立値で埋まるだけで
# 気づけるのはログの警告1行だけだった。
import re
import sys
import types

import pytest

import finbert_analyzer as fa


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(fa, "_IMPORT_ERROR", "", raising=False)
    yield


def _fake_transformers(*, toplevel: bool, submodule: bool):
    mod = types.ModuleType("transformers")
    if toplevel:
        mod.pipeline = lambda *a, **k: "TOPLEVEL"
    subs = types.ModuleType("transformers.pipelines")
    if submodule:
        subs.pipeline = lambda *a, **k: "SUBMODULE"
    mod.pipelines = subs
    return mod, subs


class TestImportFallback:
    def test_toplevel_export_is_used_when_present(self, monkeypatch):
        mod, subs = _fake_transformers(toplevel=True, submodule=True)
        monkeypatch.setitem(sys.modules, "transformers", mod)
        monkeypatch.setitem(sys.modules, "transformers.pipelines", subs)
        assert fa._try_import_transformers()() == "TOPLEVEL"

    def test_falls_back_to_submodule_when_toplevel_is_gone(self, monkeypatch):
        """v6 でトップレベルから消えた形を再現する"""
        mod, subs = _fake_transformers(toplevel=False, submodule=True)
        monkeypatch.setitem(sys.modules, "transformers", mod)
        monkeypatch.setitem(sys.modules, "transformers.pipelines", subs)
        assert fa._try_import_transformers()() == "SUBMODULE"

    def test_returns_none_when_neither_exists(self, monkeypatch):
        mod, subs = _fake_transformers(toplevel=False, submodule=False)
        monkeypatch.setitem(sys.modules, "transformers", mod)
        monkeypatch.setitem(sys.modules, "transformers.pipelines", subs)
        assert fa._try_import_transformers() is None

    def test_real_reason_is_recorded_not_guessed(self, monkeypatch):
        """『未インストール』と決めつけると調査が空振りする"""
        mod, subs = _fake_transformers(toplevel=False, submodule=False)
        monkeypatch.setitem(sys.modules, "transformers", mod)
        monkeypatch.setitem(sys.modules, "transformers.pipelines", subs)
        fa._try_import_transformers()
        assert fa._IMPORT_ERROR, "失敗理由が記録されていない"


class TestRequirementsPin:
    def test_transformers_has_an_upper_bound(self):
        """上限なしだとメジャー更新を自動で踏む"""
        import pathlib
        req = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"
        line = next(l for l in req.read_text(encoding="utf-8").splitlines()
                    if re.match(r"^\s*transformers\s*[><=]", l))
        assert "<" in line, f"transformers に上限が無い: {line!r}"
