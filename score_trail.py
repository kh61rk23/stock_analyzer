# ==============================================================
# score_trail.py  (2026-09-29)
# スクリーナー候補の「点数の経路」を記録する。
# --------------------------------------------------------------
# 【なぜ】
#   総合点（composite_score）はスクリーナーで基本点が付いたあと、評価額補正・
#   政策シグナル・議会L4 などが次々に加減点し、上限100点で切り捨てられる。
#   どこで何点動いたかが残らないため、9/29 は上位5銘柄がすべて100.0点で
#   並び「ロジックが機能しているのか分からない」状態だった（健太の指摘）。
#
# 【仕組み】
#   run_all_tasks が results を watch() に渡し、実行台帳（RunLogger.add）に
#   1行記録されるたびに checkpoint(モジュール名) を呼ぶ。候補の総合点が前回の
#   checkpoint から変わっていれば「そのモジュールが動かした」として
#   c["score_trail"] に1行積む。各モジュールには手を入れないので、
#   新しい加点が増えても自動で記録される。
#
#   c["score_trail"] = [
#     {"stage": "stock_screener", "score": 66.9, "delta": None, "note": "基本点 …"},
#     {"stage": "policy_signal_applier", "score": 91.9, "delta": 25.0,
#      "note": "政策+25.0"},
#     {"stage": "…", "score": 100.0, "delta": 8.1, "note": "議会L4+10.0（上限100で1.9点切り捨て）"},
#   ]
#   c["base_score"] = 最初に観測した点（加点前の順位付けに使う）
# ==============================================================
from __future__ import annotations

_RESULTS: dict | None = None

# 各モジュールが候補に残す「何を足したか」の目印 → 表示名
_ADJ_KEYS = [
    ("policy_signal_adj", "政策"),
    ("policy_l4_adj", "政策L4"),
    ("congress_l4_adj", "議会L4"),
    ("momentum_adj", "モメンタム"),
    ("ml_adj", "ML補正"),
    ("valuation_bonus", "評価額"),
    ("dcf_bonus", "DCF"),
]


def watch(results: dict | None) -> None:
    """この実行で追跡する results を登録する（run_all_tasks の冒頭で呼ぶ）。"""
    global _RESULTS
    _RESULTS = results


def _candidates(results: dict | None) -> list[dict]:
    sc = (results or {}).get("screener") or {}
    if not isinstance(sc, dict):
        return []
    seen, out = set(), []
    for key in ("candidates", "passed"):
        for c in sc.get(key) or []:
            if isinstance(c, dict) and id(c) not in seen:
                seen.add(id(c))
                out.append(c)
    return out


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def base_note(c: dict) -> str:
    """基本点の内訳（業種・割安・質の加重）を1行にする。"""
    w = c.get("size_weights") or {"sector_score": 0.40, "value_score": 0.30,
                                   "quality_score": 0.30}
    parts, total = [], 0.0
    for key, label in (("sector_score", "業種"), ("value_score", "割安"),
                       ("quality_score", "質")):
        v, wt = _num(c.get(key)), _num(w.get(key))
        if v is None or wt is None:
            continue
        total += v * wt
        parts.append(f"{label}{v:.0f}×{wt:.0%}")
    s = "基本点 " + " + ".join(parts) if parts else "基本点"
    cur = _num(c.get("composite_score"))
    if parts and cur is not None and abs(cur - total) >= 0.1:
        s += f"（その他 {cur - total:+.1f}）"
    return s


def _adj_note(c: dict, prev_adj: dict) -> str:
    notes = []
    for key, label in _ADJ_KEYS:
        v = _num(c.get(key))
        if v is not None and v != 0 and v != prev_adj.get(key):
            notes.append(f"{label}{v:+.1f}")
    return " ".join(notes)


def checkpoint(stage: str, results: dict | None = None) -> int:
    """候補の総合点の変化を記録する。戻り値は変化を記録した候補数。"""
    results = results if results is not None else _RESULTS
    changed = 0
    for c in _candidates(results):
        cur = _num(c.get("composite_score"))
        if cur is None:
            continue
        trail = c.setdefault("score_trail", [])
        last = _num(c.get("_trail_last"))
        prev_adj = c.get("_trail_adj") or {}
        if last is None:
            c["base_score"] = cur
            trail.append({"stage": stage, "score": round(cur, 1), "delta": None,
                          "note": base_note(c)})
        elif abs(cur - last) >= 0.05:
            delta = round(cur - last, 1)
            note = _adj_note(c, prev_adj)
            intended = sum(_num(c.get(k)) or 0 for k, _ in _ADJ_KEYS
                           if (_num(c.get(k)) or 0) != (prev_adj.get(k) or 0))
            if cur >= 100.0 and intended and last + intended > 100.0 + 0.05:
                note += f"（上限100で{last + intended - 100.0:.1f}点切り捨て）"
            elif cur <= 0.0 and intended and last + intended < -0.05:
                note += f"（下限0で{-(last + intended):.1f}点切り捨て）"
            trail.append({"stage": stage, "score": round(cur, 1), "delta": delta,
                          "note": note.strip()})
            changed += 1
        else:
            continue
        c["_trail_last"] = cur
        c["_trail_adj"] = {k: _num(c.get(k)) for k, _ in _ADJ_KEYS if _num(c.get(k))}
    return changed


def summary(c: dict, width: int = 4) -> str:
    """md・メール用の短い内訳（例: 66.9 →政策+25.0→ 91.9 →議会L4+8.1→ 100.0）。"""
    trail = c.get("score_trail") or []
    if not trail:
        return ""
    s = f"{trail[0]['score']:.1f}"
    for t in trail[1:][-width:]:
        label = (t.get("note") or t.get("stage") or "").split("（")[0] or t.get("stage")
        s += f" →{label or t['stage']} {t['delta']:+.1f}→ {t['score']:.1f}"
        if "切り捨て" in (t.get("note") or ""):
            s += "（上限）"
    return s


def slim(c: dict) -> list[dict]:
    """履歴保存用（stage・score・delta・note だけ）。"""
    return [{k: t.get(k) for k in ("stage", "score", "delta", "note")}
            for t in (c.get("score_trail") or [])]
