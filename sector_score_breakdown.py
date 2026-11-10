# ==============================================================
# sector_score_breakdown.py
# 業種の総合点が「素点からどの補正でいくつ動いたか」を業種ごとに残す。
# --------------------------------------------------------------
# 【なぜ】
#   業種の総合点は sector_agent の素点のあと、経済サイクル・債務サイクル・横断産業・先行指標・
#   モメンタム・シグナル重複排除・ベイズ更新・鮮度係数・米国先行・市場幅・季節・オプション・多通貨・
#   COT 商品・米国マクロ・リセッションが次々に足し引きする。どれが効いたかが残らないと、
#   最終点が日々動いたときに原因を切り分けられない。
#
# 【仕組み】
#   begin(res)         … 素点（sector_agent の composite_score）を控える
#   mark(sectors, 名前) … 直前の mark から composite_score が動いた分を、その名前の補正として積む
#   note_cut(res, 名前, 点) … 上限・累積余地で削られた分を積む
#   finalize(sectors)  … 業種ごとの {素点・補正の名前ごとの点・削られた分・最終} にまとめ、内部の控えを外す
#   各補正のコードには手を入れず、補正の直後に mark を呼ぶだけで、新しい補正も名前を付ければ載る。
#   mark は composite_score の差を見るので、補正が上下限（0〜100）で切られた分も「実際に動いた点」で残る。
# ==============================================================
from __future__ import annotations

_KEY = "_sbd"
_EPS = 0.05

# 補正の名前（mark の stage）→ 表示名。_apply_capped_adjustment を通るものは adj_key をそのまま stage にする。
STAGE_LABELS = {
    "debt_cycle_adjustment":    "債務サイクル",
    "cycle_phase_adjustment":   "経済サイクル",
    "cross_sector_adjustment":  "横断産業",
    "leading_adjustment":       "先行指標",
    "momentum_adjustment":      "モメンタム",
    "signal_dedup_adj":         "シグナル整理",
    "bayesian_update":          "ベイズ更新",
    "score_prior":              "鮮度係数",
    "us_lead_adjustment":       "米国先行",
    "breadth_adjustment":       "市場幅",
    "season_adjustment":        "季節",
    "options_adjustment":       "オプション",
    "currency_adjustment":      "多通貨",
    "cot_commodity_adjustment": "COT商品",
    "us_macro_adjustment":      "米国マクロ",
    "recession_adjustment":     "リセッション",
    "other":                    "その他",
}


def label(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage)


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def begin(res) -> None:
    """素点を控える。sector_agent の結果を sector_results に入れた直後に1回呼ぶ。"""
    if not isinstance(res, dict):
        return
    raw = _num(res.get("composite_score"))
    if raw is None:
        return
    res[_KEY] = {"raw": round(raw, 2), "last": raw, "steps": {}, "cuts": {}}


def mark_one(res, stage: str) -> None:
    """直前の mark からの composite_score の変化を stage の補正として積む。"""
    if not isinstance(res, dict):
        return
    bd = res.get(_KEY)
    cur = _num(res.get("composite_score"))
    if not isinstance(bd, dict) or cur is None:
        return
    d = cur - bd["last"]
    if abs(d) >= _EPS:
        bd["steps"][stage] = round(bd["steps"].get(stage, 0.0) + d, 2)
    bd["last"] = cur


def mark(sector_results, stage: str) -> None:
    """全業種に mark_one する。補正の直後に呼ぶ（動かなかった業種は何も積まない）。"""
    for res in (sector_results or {}).values():
        mark_one(res, stage)


def note_cut(res, stage: str, lost) -> None:
    """上限・累積余地で削られた点（意図した補正 − 実際に入れた補正）を積む。"""
    lost = _num(lost)
    if not isinstance(res, dict) or lost is None or abs(lost) < _EPS:
        return
    bd = res.get(_KEY)
    if isinstance(bd, dict):
        bd["cuts"][stage] = round(bd["cuts"].get(stage, 0.0) + lost, 2)


def finalize(sector_results) -> dict:
    """業種ごとの内訳にまとめ、各業種の内部の控えを外す。

    戻り値: {code: {"name", "raw", "steps": {stage: pt}, "cuts": {stage: pt}, "final", "total_adj"}}
    控えが無い業種（素点を取れなかった業種）は含めない。
    最後の mark のあとに動いた分（mark の付け忘れ）は「その他」として残し、素点＋補正＝最終を保つ。
    """
    out = {}
    for code, res in (sector_results or {}).items():
        if not isinstance(res, dict):
            continue
        bd = res.pop(_KEY, None)
        if not isinstance(bd, dict):
            continue
        final = _num(res.get("composite_score"))
        if final is None:
            continue
        steps = dict(bd["steps"])
        rest = final - bd["last"]
        if abs(rest) >= _EPS:
            steps["other"] = round(steps.get("other", 0.0) + rest, 2)
        out[str(code)] = {
            "name":      res.get("sector_name_ja") or str(code),
            "raw":       bd["raw"],
            "steps":     steps,
            "cuts":      dict(bd["cuts"]),
            "final":     round(final, 2),
            "total_adj": round(final - bd["raw"], 2),
        }
    return out


def summary_line(entry: dict, top: int = 3) -> str:
    """メール用の1行（例: 素点29.8 → 多通貨-10.0 / 米国マクロ+2.4 / モメンタム-5.0 ほか → 1.5）。"""
    if not entry:
        return ""
    steps = [(k, v) for k, v in (entry.get("steps") or {}).items() if abs(v) >= _EPS]
    steps.sort(key=lambda kv: -abs(kv[1]))
    parts = [f"{label(k)}{v:+.1f}" for k, v in steps[:top]]
    s = f"素点{entry['raw']:.1f}"
    if parts:
        s += " → " + " / ".join(parts)
        if len(steps) > top:
            s += f" ほか{len(steps) - top}件"
    cut = sum(abs(v) for v in (entry.get("cuts") or {}).values())
    if cut >= _EPS:
        s += f"（上限で{cut:.1f}点削減）"
    return s + f" → {entry['final']:.1f}"


def bq_rows(breakdown: dict, report_date: str, fetched_at: str) -> list:
    """BigQuery 用の行（業種1つ＝1行）。補正・削減は {名前: 点} の JSON 文字列。"""
    import json
    rows = []
    for code in sorted(breakdown or {}):
        e = breakdown[code]
        cuts = e.get("cuts") or {}
        rows.append({
            "report_date": report_date,
            "sector_code": code,
            "sector_name": e.get("name"),
            "raw_score":   e.get("raw"),
            "final_score": e.get("final"),
            "total_adj":   e.get("total_adj"),
            "cut_total":   round(sum(cuts.values()), 2) if cuts else 0.0,
            "steps_json":  json.dumps(e.get("steps") or {}, ensure_ascii=False, sort_keys=True),
            "cuts_json":   json.dumps(cuts, ensure_ascii=False, sort_keys=True),
            "fetched_at":  fetched_at,
        })
    return rows


def rank_corr(breakdown: dict) -> float | None:
    """素点と最終の順位相関（スピアマン）。業種が3つ未満なら None。"""
    items = [(e["raw"], e["final"]) for e in (breakdown or {}).values()
             if e.get("raw") is not None and e.get("final") is not None]
    if len(items) < 3:
        return None

    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0 + 1
            i = j + 1
        return r

    a, b = ranks([x for x, _ in items]), ranks([y for _, y in items])
    n = len(items)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return round(cov / (va * vb) ** 0.5, 3)
