# ==============================================================
# options_strategy_advisor.py  v1.0  (Wave OPT-1)
# オプション戦略アドバイザー — IV環境×シグナル×イベントの統合提案
# ==============================================================
# 【役割】
#   現物シグナル（composite score）・ボラティリティ環境（IV/HV）・
#   イベント接近（event_horizon）を統合し、ルールベースで
#   オプション戦略の「提案」を生成する（執行は常に人間）。
#
# 【ガードレール（B-investment 鉄則8と同期・変更禁止）】
#   - 提案するのは defined-risk（買い = 最大損失がプレミアムに限定）
#     または カバード戦略（covered call / cash-secured put）のみ
#   - 裸売り（naked short）・ショートは提案禁止
#   - 「提案」止まり。発注・建玉操作は一切しない
#
# 【戦略ルール】
#   IV/HV >= OPTIONS_IV_RICH_RATIO（割高 = 売り手有利環境）:
#     - 保有銘柄        → covered call（保有株が担保）
#     - 高スコア候補    → cash-secured put（現金が担保・指値買いの代替）
#   IV/HV <= OPTIONS_IV_CHEAP_RATIO（割安 = 買い手有利環境）:
#     - 高スコア候補    → コール買い（defined-risk）
#     - ドリフト/弱気環境の保有銘柄 → プロテクティブプット（保険）
#   イベント（決算/FOMC等）が満期前にある場合:
#     - 「跨ぎ警告」を必ず付与（IVクラッシュ/ギャップリスク）
#
# 【対象ユニバース（yfinance API節約のため上限あり）】
#   保有銘柄のUS銘柄 + OPTIONS_INDEX_ETFS + screener通過のUS上位
#   ※ JP個別オプションは流動性が極端に薄いため対象外。
#     日経225オプションはSQ通知（event_horizon）でカバー。
#
# 【出力】
#   results["options_advisor"] = {"suggestions": [...], "universe": [...]}
#   INSIGHT_DIR/options/options_advisor_latest.md
# ==============================================================

from __future__ import annotations

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: 純粋ロジック（テスト対象・ネットワーク不要）
# ==============================================================

def calc_hv(closes: list[float], window: int = 20) -> Optional[float]:
    """年率ヒストリカル・ボラティリティ（対数リターン標準偏差×√252）。"""
    try:
        if len(closes) < window + 1:
            return None
        rets = [math.log(closes[i] / closes[i - 1])
                for i in range(len(closes) - window, len(closes))
                if closes[i - 1] > 0]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return round(math.sqrt(var) * math.sqrt(252), 4)
    except Exception:
        return None


def classify_iv_regime(iv: float, hv: float,
                       rich: float = 1.3, cheap: float = 0.8) -> str:
    """IV/HV比からボラ環境を分類: rich（割高）/ cheap（割安）/ neutral。"""
    if not iv or not hv or hv <= 0:
        return "unknown"
    ratio = iv / hv
    if ratio >= rich:
        return "rich"
    if ratio <= cheap:
        return "cheap"
    return "neutral"


def expected_move_pct(straddle_price: float, spot: float) -> Optional[float]:
    """ATMストラドル価格から満期までの期待変動率（%）を概算。"""
    try:
        if spot <= 0 or straddle_price <= 0:
            return None
        return round(straddle_price / spot * 100, 2)
    except Exception:
        return None


def suggest_strategies(m: dict, cfg=None) -> list[dict]:
    """
    1銘柄のメトリクスから戦略提案リストを返す（純粋関数）。

    Args:
        m: {ticker, spot, iv, hv, regime, is_portfolio, composite_score,
            event_before_expiry: {label, date} | None,
            expected_move_pct, expiry, bearish_context: bool}
    Returns:
        [{ticker, strategy, direction, rationale, warnings, guardrail_ok}]
    """
    out = []
    rich = float(getattr(cfg, "OPTIONS_IV_RICH_RATIO", 1.3)) if cfg else 1.3
    csp_min = float(getattr(cfg, "OPTIONS_MIN_SCORE_FOR_CSP", 70.0)) if cfg else 70.0
    regime = m.get("regime", "unknown")
    score = float(m.get("composite_score") or 0)
    event = m.get("event_before_expiry")
    warnings = []
    if event:
        warnings.append(
            f"⚠️ 満期前イベント跨ぎ: {event.get('label','')}（{event.get('date','')}）"
            "— 買いはIVクラッシュ、カバード売りはギャップに注意")

    def add(strategy, direction, rationale):
        out.append({
            "ticker": m["ticker"], "strategy": strategy,
            "direction": direction, "rationale": rationale,
            "warnings": list(warnings),
            "expiry": m.get("expiry", ""),
            "expected_move_pct": m.get("expected_move_pct"),
            "iv": m.get("iv"), "hv": m.get("hv"),
            "guardrail_ok": True,   # 本関数は defined-risk/covered しか出さない
        })

    if regime == "rich":
        if m.get("is_portfolio"):
            add("covered_call", "neutral_bullish",
                f"IV/HV={m.get('iv',0):.0%}/{m.get('hv',0):.0%}（割高域≥{rich}倍）。"
                "保有株を担保にプレミアム収入")
        if score >= csp_min and not m.get("bearish_context"):
            add("cash_secured_put", "bullish",
                f"composite {score:.0f}点の買い候補×IV割高。"
                "現金担保プットで指値買いの代替（権利行使なら望んだ価格で取得）")
    elif regime == "cheap":
        if score >= csp_min and not m.get("bearish_context"):
            add("long_call", "bullish",
                f"composite {score:.0f}点×IV割安。"
                "最大損失プレミアム限定のコール買い")
        if m.get("is_portfolio") and m.get("bearish_context"):
            add("protective_put", "hedge",
                "保有銘柄に弱気シグナル×IV割安。保険コストが安い局面")
    # neutral/unknown は提案なし（無理にポジションを取らない）
    return out


# ==============================================================
# BLOCK 2: データ取得（yfinance・best-effort）
# ==============================================================

def _build_universe(cfg, results: dict = None) -> list[dict]:
    """{ticker, is_portfolio, composite_score} のユニバースを構築。"""
    uni: dict[str, dict] = {}
    for etf in getattr(cfg, "OPTIONS_INDEX_ETFS", ["SPY", "QQQ"]):
        uni[etf] = {"ticker": etf, "is_portfolio": False, "composite_score": 0}
    # 保有銘柄のUS分
    hp = Path(__file__).parent / "data" / "portfolio_holdings.csv"
    if hp.exists():
        for enc in ("utf-8", "cp932"):
            try:
                with open(hp, encoding=enc) as f:
                    for r in csv.DictReader(f):
                        tk = r.get("ticker", "")
                        if tk and not tk.endswith(".T"):
                            uni[tk] = {"ticker": tk, "is_portfolio": True,
                                       "composite_score": 0}
                break
            except UnicodeDecodeError:
                continue
    # US screener 上位
    us = (results or {}).get("us_screener", {}) or {}
    cands = sorted(
        (c for c in us.get("candidates", [])
         if isinstance(c, dict) and c.get("pass_filters")),
        key=lambda c: -float(c.get("composite_score", 0) or 0))
    for c in cands[:5]:
        tk = str(c.get("ticker", ""))
        if tk:
            e = uni.setdefault(tk, {"ticker": tk, "is_portfolio": False})
            e["composite_score"] = float(c.get("composite_score", 0) or 0)
    cap = int(getattr(cfg, "OPTIONS_MAX_UNIVERSE", 10))
    return list(uni.values())[:cap]


def _fetch_metrics(cfg, entry: dict, horizon: dict) -> Optional[dict]:
    """yfinance からチェーン/HVを取得しメトリクスを組む（失敗はNone）。"""
    try:
        import yfinance as yf
        tk = entry["ticker"]
        t = yf.Ticker(tk)
        hist = t.history(period="3mo")
        closes = list(hist["Close"].dropna()) if len(hist) else []
        spot = float(closes[-1]) if closes else 0
        if spot <= 0:
            return None
        hv = calc_hv(closes, int(getattr(cfg, "OPTIONS_HV_WINDOW_DAYS", 20)))
        expiries = list(t.options or [])
        if not expiries:
            return None
        # 最も近い「20日以上先」の満期（短すぎる満期はノイズ）
        expiry = next(
            (e for e in expiries
             if (datetime.strptime(e, "%Y-%m-%d") - datetime.now()).days >= 20),
            expiries[-1])
        chain = t.option_chain(expiry)
        calls, puts = chain.calls, chain.puts
        atm_c = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
        atm_p = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]]
        iv = float((atm_c["impliedVolatility"].iloc[0]
                    + atm_p["impliedVolatility"].iloc[0]) / 2)
        straddle = float(atm_c["lastPrice"].iloc[0] + atm_p["lastPrice"].iloc[0])
        # 満期前イベント（同一ticker決算 or 高重要度マクロ）
        event = None
        for e in (horizon or {}).get("upcoming", []):
            if e.get("date", "9999") <= expiry and (
                    e.get("ticker") == tk
                    or (e.get("importance") == "high"
                        and e.get("type") == "macro")):
                event = {"label": e.get("label", ""), "date": e.get("date", "")}
                break
        return {
            "ticker": tk, "spot": round(spot, 2), "iv": round(iv, 4),
            "hv": hv,
            "regime": classify_iv_regime(
                iv, hv or 0,
                float(getattr(cfg, "OPTIONS_IV_RICH_RATIO", 1.3)),
                float(getattr(cfg, "OPTIONS_IV_CHEAP_RATIO", 0.8))),
            "is_portfolio": entry.get("is_portfolio", False),
            "composite_score": entry.get("composite_score", 0),
            "event_before_expiry": event,
            "expected_move_pct": expected_move_pct(straddle, spot),
            "expiry": expiry,
            "bearish_context": False,
        }
    except Exception:
        return None


# ==============================================================
# BLOCK 3: エントリポイント
# ==============================================================

def run_options_advisor(cfg, results: dict = None) -> dict:
    """stock_analyzer_main.py から日次で呼ばれる。"""
    print("\n  🎯 オプション戦略アドバイザー実行中...")
    horizon = (results or {}).get("event_horizon", {})
    universe = _build_universe(cfg, results)
    suggestions, metrics_list = [], []
    for entry in universe:
        m = _fetch_metrics(cfg, entry, horizon)
        if not m:
            continue
        # 弱気文脈: 縮退モード or 当該業種ドリフト（簡易判定）
        ro = (results or {}).get("rule_optimizer", {}) or {}
        m["bearish_context"] = (
            str(ro.get("contraction_mode", "Normal")) != "Normal")
        metrics_list.append(m)
        suggestions += suggest_strategies(m, cfg)

    # Markdown 出力
    try:
        out_dir = Path(getattr(cfg, "INSIGHT_DIR", "stock_data/insights")) / "options"
        out_dir.mkdir(parents=True, exist_ok=True)
        lines = [f"# オプション戦略提案（{datetime.now().strftime('%Y-%m-%d')}）",
                 "", "> defined-risk / カバード戦略のみ。執行判断は人間。", ""]
        if not suggestions:
            lines.append("- 本日は提案なし（IV環境が中立 or データ取得不可）")
        for s in suggestions:
            lines.append(f"## {s['ticker']} — {s['strategy']}（{s['direction']}）")
            lines.append(f"- 満期: {s['expiry']} / 期待変動: "
                         f"{s.get('expected_move_pct','?')}%")
            lines.append(f"- 根拠: {s['rationale']}")
            for w in s["warnings"]:
                lines.append(f"- {w}")
            lines.append("")
        (out_dir / "options_advisor_latest.md").write_text(
            "\n".join(lines), encoding="utf-8")
    except Exception:
        pass

    print(f"    ✅ ユニバース{len(universe)} / 取得成功{len(metrics_list)} "
          f"/ 提案{len(suggestions)}件")
    return {
        "suggestions": suggestions,
        "universe": [u["ticker"] for u in universe],
        "metrics": metrics_list,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


if __name__ == "__main__":
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "config", str(Path(__file__).parent / "config.py"))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    r = run_options_advisor(cfg, {})
    print(json.dumps(r["suggestions"], ensure_ascii=False, indent=1)[:800])
