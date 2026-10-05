# ==============================================================
# scenario_analyzer.py  v1.0  (フェーズ3)
# マクロシナリオ再計算 + ポートフォリオ損益シミュレーション
# ==============================================================
# 【役割】
#   「利上げ+0.5%」「円160円」「原油$100」などの仮想シナリオで
#   composite_score を再計算し、保有ポートフォリオへの影響を定量化する。
#
# 【設計思想】
#   既存 MACRO_RULES エンジンを完全再利用する。
#   新しいロジックは一切書かない——シナリオ値を差し込んで
#   evaluate_macro_signals() を再評価するだけ。
#
# 【使い方】
#   3パターンの利用形態:
#     A. 週次レポートに「シナリオ分析セクション」として自動追加
#     B. stock_analyzer_main.py から手動シナリオで呼び出し
#     C. 単体スクリプトとして実行（python scenario_analyzer.py）
#
# 【出力スキーマ（run_scenario_analysis の戻り値）】
#   {
#     "scenarios": {
#       "bear_case": ScenarioResult,
#       "base_case": ScenarioResult,
#       "bull_case": ScenarioResult,
#       "custom":    ScenarioResult,  # 任意追加
#     },
#     "portfolio_impact": PortfolioImpact,  # portfolio_holdings.csv がある場合
#     "summary_text":     str,
#     "timestamp":        str,
#   }
#
#   ScenarioResult = {
#     "label":        str,   # "ベアケース（利上げ+円高）"
#     "assumptions":  dict,  # {"usdjpy": 130, "us10y": 5.5, ...}
#     "sector_scores": {sector_code: {"base": float, "scenario": float, "delta": float}},
#     "top_gainers":  [sector_code, ...],  # シナリオで上昇する上位業種
#     "top_losers":   [sector_code, ...],  # シナリオで下落する上位業種
#   }
#
#   PortfolioImpact = {
#     "holdings": [HoldingImpact, ...],
#     "total_value_jpy":    float,  # 現在の総評価額（円）
#     "scenario_pnl": {
#       "bear_case": {"pnl_jpy": float, "pnl_pct": float},
#       "bull_case": {"pnl_jpy": float, "pnl_pct": float},
#     }
#   }
#
# 【依存】config.py / stock_analyzer_main.py の evaluate_macro_signals()
# 【呼出】from scenario_analyzer import run_scenario_analysis
# ==============================================================

from __future__ import annotations

import csv
import json
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: プリセットシナリオ定義
# ==============================================================
# 各シナリオは "macro_snapshot を上書きする値" として定義する。
# 指定のないキーはベースライン実績値をそのまま使う。

PRESET_SCENARIOS = {
    "bear_case": {
        "label":       "ベアケース（利上げ + 円高 + 景気後退）",
        "description": "FRBが想定外の追加利上げ。リスクオフで円高進行。VIX急騰。",
        "overrides": {
            "usdjpy":  130.0,   # 円高
            "eurjpy":  140.0,
            "audjpy":  85.0,    # AUD安（リスクオフ）
            "us10y":    5.5,    # 米長期金利高止まり
            "oil_wti":  65.0,   # 景気後退で原油安
            "vix":      35.0,   # 恐怖指数上昇
            "us_unrate": 5.0,   # 失業率上昇
            "us_hy_spread": 6.5, # 信用収縮
        },
        "color": "🔴",
    },
    "base_case": {
        "label":       "ベースケース（現状維持）",
        "description": "現在のマクロ環境が概ね継続する想定。",
        "overrides":   {},   # 実績値をそのまま使う
        "color":       "🟡",
    },
    "bull_case": {
        "label":       "ブルケース（利下げ + 円安 + 景気拡大）",
        "description": "FRBが利下げ開始。ドル安・円安。リスクオン継続。",
        "overrides": {
            "usdjpy":  160.0,   # 円安
            "eurjpy":  172.0,
            "audjpy":  105.0,   # AUD高（リスクオン）
            "us10y":    3.8,    # 金利低下
            "oil_wti":  90.0,   # 景気拡大で原油高
            "vix":      14.0,   # 安定
            "us_unrate": 3.8,   # 完全雇用
            "us_hy_spread": 2.8, # 信用良好
        },
        "color": "🟢",
    },
    "rate_shock": {
        "label":       "金利ショック（急速利上げ）",
        "description": "インフレ再燃でFRBが0.5%緊急利上げを複数回実施。",
        "overrides": {
            "us10y":    6.0,
            "usdjpy":  135.0,   # 円高（リスクオフ）
            "us_hy_spread": 7.0,
            "vix":      40.0,
        },
        "color": "🔴",
    },
    "china_shock": {
        "label":       "中国ショック（人民元安 + 地政学リスク）",
        "description": "中国経済失速 + 台湾情勢緊張。CNY安・供給網混乱。",
        "overrides": {
            "cnyjpy":  18.5,    # 人民元安
            "usdcny":   7.6,
            "oil_wti":  95.0,   # 有事で原油高
            "audjpy":  88.0,    # リスクオフ
            "vix":      30.0,
        },
        "color": "🔴",
    },
    "weak_yen": {
        "label":       "超円安（USD/JPY 165円）",
        "description": "日米金利差継続で円安が加速。輸出企業の採算は改善するが輸入コスト増。",
        "overrides": {
            "usdjpy":  165.0,
            "eurjpy":  178.0,
            "audjpy":  108.0,
            "oil_wti":  88.0,   # 円建て輸入コスト急騰
        },
        "color": "🟡",
    },
}


# ==============================================================
# BLOCK 2: シナリオ評価エンジン
# ==============================================================

def _apply_scenario_overrides(base_snapshot: dict, overrides: dict) -> dict:
    """ベーススナップショットにシナリオ値を上書きして返す。"""
    scenario_snapshot = deepcopy(base_snapshot)
    scenario_snapshot.update(overrides)
    return scenario_snapshot


def _evaluate_sector_scores_under_scenario(
    scenario_snapshot: dict,
    sector_results:    dict,
    cfg,
) -> dict:
    """
    シナリオスナップショット下でのセクタースコアを計算する。

    stock_analyzer_main.py の evaluate_macro_signals() を再利用して
    シグナルを再評価し、スコア差分（delta）を算出する。

    Returns:
        {sector_code: {"base": float, "scenario": float, "delta": float}}
    """
    # evaluate_macro_signals を動的ロード
    try:
        import sys, importlib.util
        main_candidates = [
            Path(__file__).parent / "stock_analyzer_main.py",
            Path("/content/stock_analyzer_main.py"),
        ]
        main_mod = None
        for p in main_candidates:
            if p.exists():
                spec = importlib.util.spec_from_file_location("stock_analyzer_main", str(p))
                main_mod = importlib.util.module_from_spec(spec)
                main_mod.cfg = cfg
                spec.loader.exec_module(main_mod)
                break
        if main_mod is None:
            return {}
    except Exception as e:
        print(f"    ⚠️  stock_analyzer_main ロード失敗: {e}")
        return {}

    try:
        scenario_signals = main_mod.evaluate_macro_signals(scenario_snapshot)
    except Exception as e:
        print(f"    ⚠️  evaluate_macro_signals 失敗: {e}")
        return {}

    # シナリオシグナルから業種ごとのマクロスコアを計算
    scenario_macro: dict[str, float] = {}
    for sig in scenario_signals:
        sector_sigs = sig.get("sector_signals", {})
        for code, direction in sector_sigs.items():
            if direction == "tailwind":
                scenario_macro[code] = scenario_macro.get(code, 0) + 10
            elif direction == "headwind":
                scenario_macro[code] = scenario_macro.get(code, 0) - 10

    # 現在（ベース）の業種スコアとの差分を算出
    result = {}
    for code, res in sector_results.items():
        if not isinstance(res, dict):
            continue
        base_score = float(res.get("composite_score", 0))

        # ベースのマクロシグナル contribution を逆算
        base_macro = 0.0
        for sig in res.get("macro_signals", []):
            if sig.get("signal") == "tailwind":
                base_macro += 10
            elif sig.get("signal") == "headwind":
                base_macro -= 10

        # シナリオ差分（マクロスコアの変化分 × 0.30 で composite_scoreへの寄与を計算）
        # composite_score = sector×0.4 + value×0.3 + quality×0.3
        # マクロはsector_score内に含まれるが、近似として macro_delta × 0.40 で換算
        macro_delta       = scenario_macro.get(code, 0) - base_macro
        composite_delta   = round(macro_delta * 0.40, 1)
        scenario_score    = round(max(-30, min(100, base_score + composite_delta)), 1)

        result[code] = {
            "base":         base_score,
            "scenario":     scenario_score,
            "delta":        round(composite_delta, 1),
            "sector_name":  res.get("sector_name_ja", code),
        }

    return result


# ==============================================================
# BLOCK 3: ポートフォリオ影響計算
# ==============================================================

def load_portfolio_holdings(cfg) -> list:
    """
    portfolio_holdings.csv を読み込む。

    Returns:
        [{"ticker", "name", "sector_code", "quantity", "avg_cost_jpy", ...}, ...]
    """
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "portfolio_holdings.csv",
        Path(__file__).parent / "data" / "portfolio_holdings.csv",
        Path(__file__).parent / "portfolio_holdings.csv",
    ]
    for p in candidates:
        if p.exists():
            try:
                holdings = []
                with open(p, encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        holdings.append({
                            "ticker":       row.get("ticker", ""),
                            "name":         row.get("name", ""),
                            "sector_code":  row.get("sector_code", ""),
                            "sector_name":  row.get("sector_name", ""),
                            "market":       row.get("market", "JP"),
                            "quantity":     float(row.get("quantity", 0) or 0),
                            "avg_cost_jpy": float(row.get("avg_cost_jpy", 0) or 0),
                            "current_price": float(row.get("current_price", 0) or 0),
                            "market_value_jpy": float(row.get("market_value_jpy", 0) or 0),
                            "memo":         row.get("memo", ""),
                        })
                print(f"    📂 portfolio_holdings.csv 読み込み: {len(holdings)}銘柄（{p}）")
                return holdings
            except Exception as e:
                print(f"    ⚠️  portfolio_holdings.csv 読み込み失敗: {e}")
    return []


def _fetch_current_prices(holdings: list) -> list:
    """yfinance で現在価格を取得して holdings に注入する。"""
    try:
        import yf_cache as yf
    except ImportError:
        return holdings

    updated = []
    for h in holdings:
        ticker = h.get("ticker", "")
        if not ticker:
            updated.append(h)
            continue
        try:
            info  = yf.Ticker(ticker).info or {}
            price = info.get("regularMarketPrice") or info.get("currentPrice") or h["current_price"]
            h = dict(h)
            h["current_price"] = float(price) if price else h["current_price"]
            time.sleep(0.2)
        except Exception:
            pass
        updated.append(h)
    return updated


def calc_portfolio_impact(
    holdings:  list,
    scenarios: dict,
    usdjpy:    float = 150.0,
) -> dict:
    """
    ポートフォリオへのシナリオ影響を計算する。

    米国株は USD → JPY 換算（usdjpy レートを使用）。
    業種スコアの delta を「株価へのアルファ係数（0.3）」でポジション影響に変換する。
    （composite_score 10pt上昇 ≈ 期待リターン +3%という経験則的仮定）

    Returns:
        PortfolioImpact dict
    """
    ALPHA_COEFF = 0.003   # score 1pt ≈ 0.3%リターン変化

    total_value = 0.0
    holding_details = []

    for h in holdings:
        qty   = h.get("quantity", 0)
        price = h.get("current_price")
        cost  = h.get("avg_cost_jpy", 0)
        mkt   = h.get("market", "JP")

        # 評価額（円）
        # 2026-10-05: 株価が取れない米国株で「取得単価（円）」を代わりに使い、それにも
        # 為替を掛けて約150倍になっていた（週次で総評価額 35,577万円）。
        # 株価（現地通貨）があれば×為替、無ければ保有銘柄CSVの円評価額→取得額の順で使う
        from sync_holdings import holding_value_jpy
        value_jpy = holding_value_jpy(h, price, usdjpy)
        cost_jpy = qty * cost

        unrealized = value_jpy - cost_jpy
        total_value += value_jpy

        # シナリオごとの予測影響
        scenario_impacts = {}
        for sc_name, sc_data in scenarios.items():
            sector_code = h.get("sector_code", "")
            score_info  = sc_data.get("sector_scores", {}).get(sector_code)
            if score_info:
                delta_score = score_info.get("delta", 0)
                pnl_jpy     = round(value_jpy * delta_score * ALPHA_COEFF, 0)
                pnl_pct     = round(delta_score * ALPHA_COEFF * 100, 2)
            else:
                pnl_jpy = 0.0
                pnl_pct = 0.0
            scenario_impacts[sc_name] = {"pnl_jpy": pnl_jpy, "pnl_pct": pnl_pct}

        holding_details.append({
            **h,
            "value_jpy":       round(value_jpy, 0),
            "unrealized_pnl":  round(unrealized, 0),
            "scenario_impacts": scenario_impacts,
        })

    # ポートフォリオ全体のシナリオ損益
    portfolio_pnl = {}
    for sc_name in scenarios:
        total_pnl = sum(h["scenario_impacts"][sc_name]["pnl_jpy"] for h in holding_details)
        portfolio_pnl[sc_name] = {
            "pnl_jpy": round(total_pnl, 0),
            "pnl_pct": round(total_pnl / total_value * 100, 2) if total_value > 0 else 0,
        }

    return {
        "holdings":        holding_details,
        "total_value_jpy": round(total_value, 0),
        "scenario_pnl":    portfolio_pnl,
    }


# ==============================================================
# BLOCK 4: テキスト整形
# ==============================================================

def _fmt_jpy(v: float) -> str:
    """円金額を億・万単位で表示する。"""
    if abs(v) >= 1e8:
        return f"{v/1e8:+.2f}億円"
    elif abs(v) >= 1e4:
        return f"{v/1e4:+.0f}万円"
    return f"{v:+.0f}円"


def format_scenario_report(result: dict) -> str:
    """週次レポート向けのMarkdownを生成する。"""
    lines = ["## 📊 マクロシナリオ分析", ""]

    scenarios = result.get("scenarios", {})

    for sc_name, sc_data in scenarios.items():
        color = PRESET_SCENARIOS.get(sc_name, {}).get("color", "⚪")
        label = sc_data.get("label", sc_name)
        assumptions = sc_data.get("assumptions", {})
        sector_scores = sc_data.get("sector_scores", {})

        lines.append(f"### {color} {label}")

        # 主要前提値
        assumption_parts = []
        for k, v in assumptions.items():
            if k in ("usdjpy", "eurjpy", "audjpy"):
                assumption_parts.append(f"{k.upper()}: {v:.1f}")
            elif k == "us10y":
                assumption_parts.append(f"米10年債: {v:.2f}%")
            elif k == "oil_wti":
                assumption_parts.append(f"WTI: ${v:.0f}")
            elif k == "vix":
                assumption_parts.append(f"VIX: {v:.0f}")
        if assumption_parts:
            lines.append(f"前提: {' | '.join(assumption_parts[:5])}")
        lines.append("")

        # 上位3業種（上昇・下落）
        sorted_sectors = sorted(
            [(c, d) for c, d in sector_scores.items() if d["delta"] != 0],
            key=lambda x: x[1]["delta"],
        )
        losers  = sorted_sectors[:3]
        gainers = sorted_sectors[-3:][::-1]

        if gainers:
            lines.append("追い風業種:")
            for code, d in gainers:
                if d["delta"] > 0:
                    lines.append(f"  📈 {d['sector_name']}: {d['base']:.0f}→{d['scenario']:.0f}点 ({d['delta']:+.0f}pt)")
        if losers:
            lines.append("逆風業種:")
            for code, d in losers:
                if d["delta"] < 0:
                    lines.append(f"  📉 {d['sector_name']}: {d['base']:.0f}→{d['scenario']:.0f}点 ({d['delta']:+.0f}pt)")
        lines.append("")

    # ポートフォリオへの影響
    pf = result.get("portfolio_impact")
    if pf and pf.get("total_value_jpy", 0) > 0:
        total = pf["total_value_jpy"]
        lines += [
            f"### 💼 ポートフォリオへの影響（総評価額: {total/1e4:.0f}万円）",
            "",
            "| シナリオ | 予測損益 | リターン |",
            "|---------|---------|---------|",
        ]
        for sc_name, pnl_data in pf.get("scenario_pnl", {}).items():
            sc_label = PRESET_SCENARIOS.get(sc_name, {}).get("label", sc_name)
            color    = PRESET_SCENARIOS.get(sc_name, {}).get("color", "⚪")
            pnl_jpy  = pnl_data["pnl_jpy"]
            pnl_pct  = pnl_data["pnl_pct"]
            sign     = "+" if pnl_jpy >= 0 else ""
            lines.append(
                f"| {color} {sc_label[:20]} | {_fmt_jpy(pnl_jpy)} | {sign}{pnl_pct:.2f}% |"
            )
        lines.append("")
        lines.append(
            "> ⚠️ 予測は composite_score の変化から推計した参考値です。"
            "実際の株価変動は多くの要因に依存します。"
        )
        lines.append("")

    return "\n".join(lines)


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_scenario_analysis(
    cfg,
    sector_results:      dict,
    macro_snapshot:      dict,
    custom_scenarios:    Optional[dict] = None,
    scenario_keys:       Optional[list] = None,
    fetch_portfolio_prices: bool = False,
) -> dict:
    """
    シナリオ分析を実行する。

    stock_analyzer_main.py から以下のように呼び出す:
        _sa = _load_local_module("scenario_analyzer", ...)
        sa_result = _sa.run_scenario_analysis(
            cfg, results["sectors"], results["macro_snapshot"]
        )
        results["scenario_analysis"] = sa_result

    Args:
        cfg:                    config モジュール
        sector_results:         results["sectors"]
        macro_snapshot:         results["macro_snapshot"]（ベースライン）
        custom_scenarios:       独自シナリオを追加する場合 {"key": {...}} 形式
        scenario_keys:          実行するシナリオのキーリスト（省略時: bear/base/bull の3つ）
        fetch_portfolio_prices: True の場合、yfinance で現在価格を取得（時間がかかる）

    Returns:
        シナリオ分析結果 dict
    """
    print("\n  📊 シナリオ分析開始...")

    # 実行シナリオを決定
    all_scenarios = {**PRESET_SCENARIOS}
    if custom_scenarios:
        all_scenarios.update(custom_scenarios)

    keys = scenario_keys or ["bear_case", "base_case", "bull_case"]
    target_scenarios = {k: all_scenarios[k] for k in keys if k in all_scenarios}

    # 各シナリオを評価
    evaluated = {}
    for sc_name, sc_def in target_scenarios.items():
        print(f"    {sc_def.get('color', '⚪')} {sc_def['label']} 評価中...")
        scenario_snapshot = _apply_scenario_overrides(
            macro_snapshot, sc_def.get("overrides", {})
        )
        sector_scores = _evaluate_sector_scores_under_scenario(
            scenario_snapshot, sector_results, cfg
        )
        gainers = sorted(
            [c for c, d in sector_scores.items() if d["delta"] > 0],
            key=lambda c: sector_scores[c]["delta"], reverse=True,
        )[:5]
        losers = sorted(
            [c for c, d in sector_scores.items() if d["delta"] < 0],
            key=lambda c: sector_scores[c]["delta"],
        )[:5]

        evaluated[sc_name] = {
            "label":         sc_def["label"],
            "description":   sc_def.get("description", ""),
            "assumptions":   {**macro_snapshot, **sc_def.get("overrides", {})},
            "sector_scores": sector_scores,
            "top_gainers":   gainers,
            "top_losers":    losers,
        }

    # ポートフォリオ影響計算
    portfolio_impact = None
    holdings = load_portfolio_holdings(cfg)
    if holdings:
        if fetch_portfolio_prices:
            print(f"    💼 ポートフォリオ価格取得中（{len(holdings)}銘柄）...")
            holdings = _fetch_current_prices(holdings)
        usdjpy = float(macro_snapshot.get("usdjpy") or 150.0)
        portfolio_impact = calc_portfolio_impact(holdings, evaluated, usdjpy)
        total = portfolio_impact["total_value_jpy"]
        print(f"    💼 ポートフォリオ: {total/1e4:.0f}万円 / {len(holdings)}銘柄")

    result = {
        "scenarios":        evaluated,
        "portfolio_impact": portfolio_impact,
        "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["summary_text"] = format_scenario_report(result)

    print(f"    ✅ シナリオ分析完了: {len(evaluated)}シナリオ")
    return result


# ==============================================================
# 単体テスト
# ==============================================================
if __name__ == "__main__":
    print("=== シナリオアナライザー 単体テスト ===")

    # モックデータでテスト
    mock_snapshot = {
        "usdjpy": 149.2, "eurjpy": 161.5, "cnyjpy": 20.8, "audjpy": 97.3,
        "us10y": 4.52, "oil_wti": 78.0, "vix": 18.5,
        "us_unrate": 4.1, "us_hy_spread": 3.4,
    }
    mock_sectors = {
        "1028": {"sector_name_ja": "銀行業",   "composite_score": 55.0,
                 "macro_signals": [{"signal": "tailwind"}, {"signal": "tailwind"}]},
        "1032": {"sector_name_ja": "不動産業", "composite_score": 42.0,
                 "macro_signals": [{"signal": "headwind"}]},
        "1017": {"sector_name_ja": "輸送用機器","composite_score": 68.0,
                 "macro_signals": [{"signal": "tailwind"}, {"signal": "tailwind"},
                                   {"signal": "headwind"}]},
        "1008": {"sector_name_ja": "医薬品",   "composite_score": 40.0,
                 "macro_signals": []},
    }

    from copy import deepcopy
    mock_cfg = type("cfg", (), {
        "DATA_DIR": Path("./test_output"),
        "MACRO_RULES": {},
    })()

    # シナリオ適用テスト（stock_analyzer_main.pyなしでの動作確認）
    sc = _apply_scenario_overrides(mock_snapshot, {"usdjpy": 130.0, "us10y": 5.5})
    print(f"  ベアケース適用後 usdjpy={sc['usdjpy']} us10y={sc['us10y']}")
    assert sc["usdjpy"] == 130.0 and sc["us10y"] == 5.5
    assert mock_snapshot["usdjpy"] == 149.2  # 元スナップショットは不変

    # ポートフォリオ影響テスト
    mock_holdings = [
        {"ticker": "8306.T", "name": "三菱UFJ", "sector_code": "1028",
         "market": "JP", "quantity": 500, "avg_cost_jpy": 1200, "current_price": 1500},
        {"ticker": "7203.T", "name": "トヨタ", "sector_code": "1017",
         "market": "JP", "quantity": 200, "avg_cost_jpy": 2800, "current_price": 3200},
    ]
    mock_scenarios = {
        "bear_case": {
            "label": "ベアケース",
            "sector_scores": {
                "1028": {"base": 55.0, "scenario": 43.0, "delta": -12.0, "sector_name": "銀行業"},
                "1017": {"base": 68.0, "scenario": 64.0, "delta": -4.0,  "sector_name": "輸送用機器"},
            }
        },
        "bull_case": {
            "label": "ブルケース",
            "sector_scores": {
                "1028": {"base": 55.0, "scenario": 61.0, "delta":  6.0,  "sector_name": "銀行業"},
                "1017": {"base": 68.0, "scenario": 76.0, "delta":  8.0,  "sector_name": "輸送用機器"},
            }
        },
    }
    pf = calc_portfolio_impact(mock_holdings, mock_scenarios, usdjpy=149.2)
    print(f"  ポートフォリオ評価額: {pf['total_value_jpy']/1e4:.0f}万円")
    print(f"  ベアケース損益: {_fmt_jpy(pf['scenario_pnl']['bear_case']['pnl_jpy'])}")
    print(f"  ブルケース損益: {_fmt_jpy(pf['scenario_pnl']['bull_case']['pnl_jpy'])}")
    assert pf["total_value_jpy"] == 1390000.0
    print("  ✅ OK")

    # 日付ユーティリティ
    bear_overrides = PRESET_SCENARIOS["bear_case"]["overrides"]
    print(f"\n  ベアケース前提: usdjpy={bear_overrides['usdjpy']} vix={bear_overrides['vix']}")
    assert bear_overrides["usdjpy"] == 130.0

    print("\n全テスト完了 ✅")
