# ==============================================================
# quant/gate.py — 採用の門
# ==============================================================
# 【役割】research が作った候補の成績（検証区間＝学習に使っていない期間のもの）を、設定の基準に1項目ずつ照らして
#   adopted（採用）/ watch（観察）/ rejected（不採用）に分ける。純関数で、stats にも依存しない（数値は受け取るだけ）。
# 【判定】全項目を通れば adopted。落ちた項目が QUANT_GATE_WATCH_MAX_FAILS 以下で、必須の項目（QUANT_GATE_CRITICAL）を
#   全部通っていれば watch。それ以外は rejected（必須を落とせば、落ちた数が少なくても rejected）。
# 【値が無いとき】計算できなかった値（None・NaN）は不合格。ただし pbo（設定が1個だと計算できない）と
#   fold_positive（外部の実績には区間が無い）は「対象外」として通し、note に理由を書く。
# 【基準ちょうど】基準と同じ値は合格（浮動小数の丸め誤差ぶんだけ許す）。
# 【読むキー】candidate["metrics"]（days・sharpe・gate_sharpe・dsr・pbo・adj_p・p_profit{"252"}・max_drawdown・
#   fold_positive）・candidate["stress"]["sharpe"]・candidate["external"]・candidate["reject_reason"]。
# ==============================================================
from __future__ import annotations

import math

from quant import settings as S

_EPS = 1e-12

# (key, 表示名, 比較の向き（"ge"＝基準以上で合格・"le"＝基準以下で合格）, 設定名, 値の表示（"pct"・"num"・"int"）)
_SPECS = (
    ("oos_days", "検証の日数", "ge", "QUANT_GATE_MIN_OOS_DAYS", "int"),
    ("oos_sharpe", "シャープ（リスクあたりの利益）", "ge", "QUANT_GATE_MIN_OOS_SHARPE", "num"),
    ("dsr", "試した数を割り引いた確からしさ（DSR）", "ge", "QUANT_GATE_MIN_DSR", "num"),
    ("pbo", "過剰適合の確率（PBO）", "le", "QUANT_GATE_MAX_PBO", "pct"),
    ("adj_p", "補正後の p 値", "le", "QUANT_GATE_MAX_ADJ_P", "num"),
    ("p_profit_12m", "12か月後に利益が出ている確率", "ge", "QUANT_GATE_MIN_P_PROFIT_12M", "pct"),
    ("stress_sharpe", "コスト2倍でのシャープ", "ge", "QUANT_GATE_MIN_STRESS_SHARPE", "num"),
    ("max_drawdown", "最大の下げ", "le", "QUANT_GATE_MAX_DRAWDOWN", "pct"),
    ("fold_positive", "利益が出た区間の割合", "ge", "QUANT_GATE_MIN_FOLD_POSITIVE", "pct"),
)

# 値が無くても通す項目と、その理由
_OPTIONAL_NOTE = {
    "pbo": "設定が1個のため計算できないので対象外",
    "fold_positive": "外部の実績または区間なしのため対象外",
}


def _num(v):
    """数値ならそのまま float で返し、None・NaN・inf・数値でないものは None にする。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fmt(v, kind: str) -> str:
    if v is None:
        return "なし"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "int":
        return f"{v:.0f}日"
    return f"{v:.3g}"


def _values(candidate: dict) -> dict:
    """門が読む9つの値を取り出す（無いものは None）。シャープは理論価格の割引後（gate_sharpe）を優先する。"""
    m = candidate.get("metrics") or {}
    stress = candidate.get("stress") or {}
    p_profit = m.get("p_profit") or {}
    sharpe = _num(m.get("gate_sharpe"))
    if sharpe is None:
        sharpe = _num(m.get("sharpe"))
    return {
        "oos_days": _num(m.get("days")),
        "oos_sharpe": sharpe,
        "dsr": _num(m.get("dsr")),
        "pbo": _num(m.get("pbo")),
        "adj_p": _num(m.get("adj_p")),
        "p_profit_12m": _num(p_profit.get("252")),
        "stress_sharpe": _num(stress.get("sharpe")),
        "max_drawdown": _num(m.get("max_drawdown")),
        "fold_positive": _num(m.get("fold_positive")),
    }


def evaluate(candidate: dict, cfg) -> dict:
    """候補を門に通す。戻りは {"status", "checks": [{"key","label","value","threshold","passed", ("note")}], "reasons": [日本語]}。

    research が先に落とした候補（reject_reason あり）は、項目を調べずに rejected とし、その理由を reasons に入れる。
    """
    pre = candidate.get("reject_reason")
    if pre:
        return {"status": "rejected", "checks": [], "reasons": [str(pre)]}

    values = _values(candidate)
    external = bool(candidate.get("external"))
    checks, reasons = [], []
    for key, label, op, setting, kind in _SPECS:
        threshold = float(S.get(cfg, setting))
        value = values[key]
        check = {"key": key, "label": label, "value": value, "threshold": threshold}
        if key == "fold_positive" and external:
            check.update(value=None, passed=True, note=_OPTIONAL_NOTE[key])
        elif value is None:
            if key in _OPTIONAL_NOTE:
                check.update(passed=True, note=_OPTIONAL_NOTE[key])
            else:
                check["passed"] = False
                reasons.append(f"{label}が計算できない（基準 {_fmt(threshold, kind)}）")
        else:
            ok = value >= threshold - _EPS if op == "ge" else value <= threshold + _EPS
            check["passed"] = bool(ok)
            if not ok:
                verb = "が基準に届かない" if op == "ge" else "が基準を超えている"
                reasons.append(f"{label}{verb}（値 {_fmt(value, kind)}・基準 {_fmt(threshold, kind)}）")
        checks.append(check)

    failed = {c["key"] for c in checks if not c["passed"]}
    critical = set(S.get(cfg, "QUANT_GATE_CRITICAL"))
    if not failed:
        status = "adopted"
    elif len(failed) <= int(S.get(cfg, "QUANT_GATE_WATCH_MAX_FAILS")) and not (failed & critical):
        status = "watch"
    else:
        status = "rejected"
    return {"status": status, "checks": checks, "reasons": reasons}
