"""run_headless は yfinance 内部の inplace 非推奨警告だけを消し、自前コードの警告は残す。"""
import importlib
import warnings

import run_headless


def _emit(module_name: str, text: str):
    g = {"__name__": module_name, "__warningregistry__": {}}
    exec(compile(f"import warnings\nwarnings.warn({text!r}, FutureWarning)", "x", "exec"), g)


def test_yfinance_inplace_warning_is_silenced_but_others_remain():
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        # run_headless が import 時に入れたフィルタを再現するため、モジュールを読み直す
        importlib.reload(run_headless)
        _emit("yfinance.utils", "The inplace keyword in DataFrame.drop is deprecated")
        _emit("stock_analyzer_main", "The inplace keyword in DataFrame.drop is deprecated")
    msgs = [str(w.message) for w in seen]
    assert len(msgs) == 1
