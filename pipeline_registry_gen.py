# ==============================================================
# pipeline_registry_gen.py  v1.0  (Wave PIPE-1)
# パイプライン・ステージレジストリ生成 + 整合監査
# ==============================================================
# 【役割】
#   stock_analyzer_main.run_all_tasks() を AST 解析し、各実行ステージの
#   宣言（モジュール・ENABLEフラグ・モードキー・produces キー・実行順）を
#   pipeline_registry.json として機械可読化する。
#
# 【PIPE-1 第1段の位置づけ】
#   本ツールは「宣言レイヤー＋整合検査」であり、run_all_tasks の実行
#   そのものはまだ置き換えない（第2段で registry 駆動ループへ移行予定）。
#   生成された registry は以下の用途にすぐ使える:
#     - AIエージェントが改修前にパイプライン構造を1ファイルで把握する
#     - audit モードで「registry と実コードの乖離」を検出する
#       （モジュール削除・フラグ消滅・produces 変更の検知）
#     - PIPE-2 results_schema との突合
#
# 【実行】
#   python3 pipeline_registry_gen.py            # 生成
#   python3 pipeline_registry_gen.py --audit    # 既存 registry と現コードの差分検査
#                                                （乖離あり= exit 1 / CIゲート対応）
# ==============================================================

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent
MAIN = REPO / "stock_analyzer_main.py"
OUT = REPO / "pipeline_registry.json"


def _enable_flags(test: ast.expr) -> list[str]:
    """if 条件式から ENABLE_* フラグ名を抽出する。"""
    flags = []
    for n in ast.walk(test):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "getattr" and len(n.args) >= 2
                and isinstance(n.args[1], ast.Constant)
                and str(n.args[1].value).startswith("ENABLE_")):
            flags.append(n.args[1].value)
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "cfg" and n.attr.startswith("ENABLE_")):
            flags.append(n.attr)
    return sorted(set(flags))


def _mode_keys(test: ast.expr) -> list[str]:
    """if 条件式から _in_mode("key") のキーを抽出する。"""
    keys = []
    for n in ast.walk(test):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_in_mode" and n.args
                and isinstance(n.args[0], ast.Constant)):
            keys.append(n.args[0].value)
    return sorted(set(keys))


def _loaded_modules(body_nodes: list) -> list[str]:
    """ブロック内で動的ロードされるモジュール名を抽出する。"""
    mods = []
    for stmt in body_nodes:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call):
                if (isinstance(n.func, ast.Name)
                        and n.func.id == "_load_local_module"
                        and n.args and isinstance(n.args[0], ast.Constant)):
                    mods.append(n.args[0].value)
                if (isinstance(n.func, ast.Attribute)
                        and n.func.attr == "import_module"
                        and n.args and isinstance(n.args[0], ast.Constant)):
                    mods.append(n.args[0].value)
    return sorted(set(mods))


def _produces(body_nodes: list) -> list[str]:
    """ブロック内で書かれる results キーを抽出する。"""
    keys = []
    for stmt in body_nodes:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if (isinstance(t, ast.Subscript)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "results"
                            and isinstance(t.slice, ast.Constant)):
                        keys.append(t.slice.value)
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "setdefault"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "results"
                    and n.args and isinstance(n.args[0], ast.Constant)):
                keys.append(n.args[0].value)
    return sorted(set(keys))


def build_registry() -> dict:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    run_fn = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "run_all_tasks"),
        None)
    if run_fn is None:
        raise SystemExit("❌ run_all_tasks が見つかりません")

    stages = []
    # run_all_tasks 直下の if 文を「ステージ」とみなす
    for node in run_fn.body:
        if not isinstance(node, ast.If):
            continue
        block = node.body
        mods = _loaded_modules(block)
        produces = _produces(block + (node.orelse or []))
        flags = _enable_flags(node.test)
        modes = _mode_keys(node.test)
        if not (mods or produces):
            continue  # 実行ステージではない制御if
        stages.append({
            "order": node.lineno,
            "modules": mods,
            "enable_flags": flags,
            "mode_keys": modes,
            "produces": produces,
        })

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "stock_analyzer_main.run_all_tasks（AST自動生成・手動編集禁止）",
        "stage_count": len(stages),
        "stages": stages,
    }


def audit() -> int:
    """既存 registry と現コード・config の乖離を検査する。"""
    if not OUT.exists():
        print("❌ pipeline_registry.json なし — 先に生成してください")
        return 1
    old = json.loads(OUT.read_text(encoding="utf-8"))
    new = build_registry()
    problems = []

    # ① ステージ構成の乖離（モジュール集合で比較）
    def sig(r):
        return [(tuple(s["modules"]), tuple(s["enable_flags"]),
                 tuple(s["produces"])) for s in r["stages"]]
    if sig(old) != sig(new):
        old_set = set(sig(old))
        new_set = set(sig(new))
        for s in new_set - old_set:
            problems.append(f"未登録ステージ（コードにあるが registry になし）: {s[0]}")
        for s in old_set - new_set:
            problems.append(f"消失ステージ（registry にあるがコードになし）: {s[0]}")

    # ② モジュールファイル存在
    import importlib.util as _ilu
    _spec0 = _ilu.spec_from_file_location("config", REPO / "config.py")
    _cfg0 = _ilu.module_from_spec(_spec0)
    _spec0.loader.exec_module(_cfg0)
    for s in new["stages"]:
        for m in s["modules"]:
            # backtest/ paper/ 等のサブディレクトリ配置を許可
            # （要件定義書_統合改修_20260705.md 共通規約8）
            _found = (REPO / f"{m}.py").exists() or bool(
                [p for p in REPO.glob(f"*/{m}.py")
                 if p.parent.name not in ("tests", "__pycache__")])
            if not _found:
                # 全ENABLEフラグが False のステージは意図的無効化 → 警告止まり
                disabled = (s["enable_flags"] and all(
                    getattr(_cfg0, f, True) is False
                    for f in s["enable_flags"]))
                tag = "無効化済みステージのモジュール未配置（許容）" if disabled                       else "モジュール不在"
                problems.append(f"{tag}: {m}.py (L{s['order']})")

    # ③ ENABLE フラグが config に存在するか
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", REPO / "config.py")
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    for s in new["stages"]:
        for f in s["enable_flags"]:
            if not hasattr(cfg, f):
                problems.append(
                    f"ENABLEフラグ未定義（getattrデフォルト動作）: {f} (L{s['order']})")

    # ④ 宣言された実行順と、実コードの実行順の整合
    #    WAVE7_DAILY_MODULE_ORDER / WAVE7_WEEKLY_MODULE_ORDER は
    #    「この順で回す」という宣言だが、実行時にはどこからも読まれず、実際の実行順は
    #    stock_analyzer_main のコード順（= registry の order）で決まる。
    #    宣言と実装のズレに気づけるよう、ここで突き合わせる。
    #    registry の order は AST の行番号ベースなので、実行順の代理として使える。
    _order_of = {}
    for s in new["stages"]:
        for m in s["modules"]:
            _order_of.setdefault(m, s["order"])
    for _decl_name in ("WAVE7_DAILY_MODULE_ORDER", "WAVE7_WEEKLY_MODULE_ORDER"):
        declared = list(getattr(cfg, _decl_name, []) or [])
        present = [m for m in declared if m in _order_of]
        missing = [m for m in declared if m not in _order_of]
        for m in missing:
            problems.append(
                f"{_decl_name} に宣言されているがパイプライン未配線: {m}")
        actual = sorted(present, key=lambda m: _order_of[m])
        if present and actual != present:
            problems.append(
                f"{_decl_name} の宣言順と実コードの実行順が不一致: "
                f"宣言={present} / 実際={actual}")

    print(f"パイプライン監査: ステージ{new['stage_count']} / 問題{len(problems)}件")
    for p in problems[:30]:
        print(f"  ⚠️  {p}")
    return 1 if any("不在" in p or "消失" in p or "未登録" in p
                    for p in problems) else 0


if __name__ == "__main__":
    if "--audit" in sys.argv:
        sys.exit(audit())
    reg = build_registry()
    OUT.write_text(json.dumps(reg, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    n_mods = len({m for s in reg["stages"] for m in s["modules"]})
    print(f"✅ pipeline_registry.json 生成: "
          f"ステージ{reg['stage_count']} / モジュール{n_mods}種")
