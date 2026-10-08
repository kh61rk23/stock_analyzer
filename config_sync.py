# ==============================================================
# config_sync.py  v1.0
# モジュール定数を config.py から上書きする共通ヘルパー
# ==============================================================
# 【役割】
#   各モジュールが持つモジュールレベル定数（閾値・重み）を、
#   config.py を正本として実行時に上書きする。
#
# 【なぜ必要か】
#   各モジュールが自分用の定数を持ち、config.py にも同じ設定がある
#   二重定義が多い。何もしないと config.py を編集しても実装は自分の定数を
#   読み続ける（SSOT違反）。
#
#   これを解消する方法は2つある:
#     (a) 参照箇所すべてを getattr(cfg, ...) に書き換える
#     (b) cfg を持つ入口で一度だけモジュール定数を上書きする ← 本モジュール
#   閾値の参照箇所が数十ある module では (a) は差分が過大でリスクが高い。
#   (b) なら参照箇所を1つも触らずに config を正本にできる。
#   anomaly_detector / credit_data で実績のある方式を共通化した。
#
# 【使い方】
#   # モジュール冒頭（定数定義の直後）
#   from config_sync import sync_module_constants
#
#   _CFG_CONST_MAP = {
#       "MB_SCORE_HEALTHY_TH": "SCORE_HEALTHY_THRESH",   # config名: モジュール定数名
#       ...
#   }
#
#   # cfg を持つ入口（run_xxx の冒頭）で1回だけ
#   def run_market_breadth(cfg, results):
#       sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
#       ...
#
# 【安全設計】
#   - cfg が None / 属性なし → モジュール既定値を保つ
#   - 型が既定値と食い違う → 取り込まず警告（設定ミスでスコアが壊れるのを防ぐ）
#   - 例外は握り潰さず警告して既定値を保つ
# ==============================================================

from __future__ import annotations


def sync_module_constants(module_globals: dict, cfg, mapping: dict,
                          *, label: str = "") -> dict:
    """config の値をモジュール定数へ取り込む。

    Args:
        module_globals: 呼び出し側の globals()
        cfg:            config モジュール（None 可）
        mapping:        {config側の定数名: モジュール側の定数名}
        label:          警告表示用のモジュール名（省略時は自動推定）

    Returns:
        {モジュール定数名: 採用した値} — 実際に何が使われたかの記録用

    型チェック: モジュール既定値と同系統の型のみ受け付ける。
      int/float は相互に許容（1 と 1.0 を区別しない）。
      bool は bool のみ（True/1 の混同を避ける）。
      list/tuple/set/dict はそれぞれ同種のみ。
    """
    label = label or str(module_globals.get("__name__", "module"))
    applied: dict = {}

    for cfg_name, mod_name in mapping.items():
        current = module_globals.get(mod_name)
        applied[mod_name] = current

        if cfg is None:
            continue
        raw = getattr(cfg, cfg_name, None)
        if raw is None:
            continue

        if not _type_ok(current, raw):
            print(f"  ⚠️  {label}: cfg.{cfg_name} の型が既定値と不一致"
                  f"（{type(raw).__name__} vs {type(current).__name__}）→ 既定値を使用")
            continue

        value = raw
        # 既定が float なら float へ寄せる（int 設定でも計算式を壊さない）
        if isinstance(current, float) and isinstance(raw, int) and not isinstance(raw, bool):
            value = float(raw)

        module_globals[mod_name] = value
        applied[mod_name] = value

    return applied


def _type_ok(current, raw) -> bool:
    """既定値と設定値の型が同系統か判定する。"""
    if current is None:
        return True                      # 既定が未設定なら何でも受ける
    if isinstance(current, bool):
        return isinstance(raw, bool)
    if isinstance(raw, bool):
        return False                     # 数値の既定に True/False は入れさせない
    if isinstance(current, (int, float)):
        return isinstance(raw, (int, float))
    if isinstance(current, dict):
        return isinstance(raw, dict)
    if isinstance(current, (list, tuple, set)):
        return isinstance(raw, (list, tuple, set))
    if isinstance(current, str):
        return isinstance(raw, str)
    return type(raw) is type(current)
