# ==============================================================
# tests/test_quant_mail.py — quant/mail.py のテスト（合成の daily・registry。ネットワーク不使用）
# ==============================================================
# lines・build_alert が採用あり・採用なし・見送りで例外を出さず注意書きを含むこと、build_alert の形、
# research_markdown の表（状態・落ちた項目・パイプの無害化）を確かめる。
# ==============================================================
from __future__ import annotations

from quant import mail

CAUTION = "検証は過去の値動きに基づく見込みで、利益を約束するものではありません。発注はご自身の判断で"


def _daily_ok():
    return {
        "as_of": "2026-11-13", "registry_as_of": "2026-11-08", "status": "ok",
        "adopted": ["a", "b"], "watch": ["c"], "external_adopted": [],
        "portfolio": {"metrics": {"ann_return": 0.0812, "ann_vol": 0.07, "max_drawdown": 0.0934},
                      "p_profit": {"252": 0.8123}, "brake_now": 0.5, "scale": 1.2},
        "proposals": [
            {"asset_class": "fx", "label": "FX", "symbol": "X", "name": "米ドル/円", "side": "買い", "weight_pct": 12.34,
             "action": "新しく建てる", "exec_hint": "FX", "note": ""},
            {"asset_class": "options", "label": "オプション", "symbol": "SPY", "name": "S&P500", "side": "建てる",
             "weight_pct": 20.0, "action": "そのまま", "exec_hint": "SPY のオプション", "note": "プット売り 行使価格 500"},
            {"asset_class": "fx", "label": "FX", "symbol": "Y", "name": "ユーロ/円", "side": "売り", "weight_pct": 0.0,
             "action": "閉じる", "exec_hint": "", "note": ""},
        ],
        "suspended": [{"id": "d", "label": "戦略D", "reason": "実績が悪い", "until": "2027-01-05"}],
    }


def test_lines_with_adoption():
    ls = mail.lines(_daily_ok())
    text = "\n".join(ls)
    assert "11/8時点の検証：採用 2本・観察中 1本" in text
    assert "81.2%" in text and "+8.1%" in text and "9.3%" in text
    assert "米ドル/円（FX）：買い・資金の12.3%・新しく建てる" in text
    assert "ユーロ/円：閉じる（資金の0%）" in text
    assert "プット売り 行使価格 500" in text
    assert "戦略D" in text and "1/5" in text
    assert "0.5倍" in text
    assert CAUTION in text and "理論価格" in text


def test_lines_no_adoption():
    d = {"as_of": "2026-11-13", "registry_as_of": "2026-11-08", "status": "no_adoption", "adopted": [], "watch": ["c", "e"],
         "proposals": [], "top_reasons": [{"label": "DSR", "count": 2}]}
    text = "\n".join(mail.lines(d))
    assert "採用した戦略なし" in text and "観察中 2本" in text and "DSR（2本）" in text
    assert CAUTION in text


def test_lines_skipped_and_empty():
    ls = mail.lines({"skipped": True, "reason": "検証の結果がまだ無い"})
    assert "検証の結果がまだ無い" in ls[0] and CAUTION in "\n".join(ls)
    assert mail.lines(None) == [] and mail.lines({}) == []


def test_lines_tolerates_missing_values():
    d = {"status": "ok", "adopted": ["a"], "portfolio": {"metrics": {"ann_return": None}, "p_profit": {}}}
    text = "\n".join(mail.lines(d))
    assert "-" in text and CAUTION in text


def test_build_alert_shapes():
    a = mail.build_alert(_daily_ok())
    assert a["trigger"] == "quant_lab" and a["severity"] == "info"
    assert a["title"] == "🔬 検証ラボ（採用 2本・提案 3件）"
    assert a["body"] == "\n".join(mail.lines(_daily_ok()))
    s = mail.build_alert({"skipped": True, "reason": "古い"})
    assert s["title"] == "🔬 検証ラボ（見送り）" and s["body"] == "古い"
    assert mail.build_alert(None) is None and mail.build_alert({}) is None


def test_research_markdown_table():
    reg = {"as_of": "2026-11-08", "n_trials": 30, "data_quality": {"rates_source": "bis", "missing": [], "stale": ["Z"], "jumps": []},
           "portfolio": {"weights": {"a": 0.3}, "p_profit": {"252": 0.75}, "metrics": {"ann_return": 0.05}},
           "candidates": {
               "a": {"id": "a", "asset_class": "fx", "label": "戦略A", "status": "adopted", "checks": [],
                     "metrics": {"days": 900, "ann_return": 0.06, "sharpe": 0.8, "dsr": 0.97, "pbo": 0.1,
                                 "p_profit": {"252": 0.8}, "max_drawdown": 0.1}, "stress": {"sharpe": 0.5}},
               "b": {"id": "b", "asset_class": "index", "label": "戦略|B", "status": "rejected",
                     "reject_reason": "日数不足", "metrics": {}, "stress": {}},
               "c": {"id": "c", "asset_class": "fx", "label": "戦略C", "status": "watch",
                     "checks": [{"label": "最大の下げ", "passed": False}, {"label": "x", "passed": True}],
                     "metrics": {"days": 800, "sharpe": 0.6}, "stress": {}},
           }}
    md = mail.research_markdown(reg)
    rows = [r for r in md.splitlines() if r.startswith("| ") and "資産クラス" not in r]
    assert len(rows) == 3
    assert "採用" in rows[0] and "戦略A" in rows[0] and "+6.0%" in rows[0]
    assert "観察" in rows[1] and "最大の下げ" in rows[1]
    assert "不採用" in rows[2] and "日数不足" in rows[2] and "戦略／B" in rows[2]
    assert "採用 1・観察 1・不採用 1" in md


def test_no_adoption_lists_closest_watch_strategies():
    """採用なしの日は、基準に近い観察中の戦略と、基準までの距離を出す（提案は出さない）。"""
    from quant import mail
    daily = {"status": "no_adoption", "registry_as_of": "2026-11-13", "as_of": "2026-11-13", "adopted": [],
             "watch": ["a"], "proposals": [], "top_reasons": [],
             "watch_top": [{"id": "a", "label": "指数の移動平均", "dsr": 0.86, "p_profit_12m": 0.78,
                            "max_drawdown": 0.203, "failed": ["DSR"]}]}
    body = "\n".join(mail.lines(daily))
    assert "指数の移動平均" in body and "86.0%" in body and "78.0%" in body and "20.3%" in body
    assert "基準 95.0%" in body and "提案は出しません" in body
