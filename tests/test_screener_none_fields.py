"""全上場の二次評価では財務の欠けた銘柄（total_cash=None）も入る。演算で落ちないこと。"""
import ast
from pathlib import Path


def test_no_arithmetic_on_get_with_default_zero():
    """data.get("x", 0) は値が None のとき 0 にならない。演算に使うなら (data.get("x") or 0) と書く。"""
    src = Path(__file__).resolve().parent.parent.joinpath("stock_screener.py").read_text(encoding="utf-8")
    bad = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.BinOp):
            for side in (node.left, node.right):
                if (isinstance(side, ast.Call) and getattr(side.func, "attr", "") == "get" and len(side.args) == 2
                        and isinstance(side.args[1], ast.Constant) and side.args[1].value == 0):
                    bad.append(node.lineno)
    assert bad == [], f"None で落ちる演算: stock_screener.py 行 {bad}"
