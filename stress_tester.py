# ==============================================================
# stress_tester.py  v1.0  (3-6 / C-5)
# 最大ドローダウン・損失シナリオ + VaR 自動計算
# ==============================================================
# 【役割】
#   「リーマンショック相当の暴落が来たら何円失うか」を
#   ベータ値ベースで自動算出し、月次 Gmail で通知する。
#
# 【計算ロジック】
#   A. 歴史的暴落シナリオ（ベータ加重）
#      想定損失 = ポジション評価額 × ベータ × 市場下落率
#      ※ ベータ > 1 の株は市場より大きく動く
#
#   B. VaR（Value at Risk）95%信頼区間
#      過去 N 日の日次リターン分布から
#      「95%の確率でこの額を超えない1日損失額」を算出
#      → 正規分布仮定: VaR = μ - 1.645σ（95%片側）
#      → 歴史的シミュレーション: 下位5パーセンタイル
#
#   C. 最大ドローダウン（Max Drawdown）
#      yfinance の過去 2 年の株価から実際のピーク→谷の最大下落率を計算
#
# 【歴史的暴落シナリオ】
#   リーマンショック  (2008-09〜2009-03): 日経 -57% / S&P500 -57%
#   コロナショック    (2020-02〜2020-03): 日経 -34% / S&P500 -34%
#   ITバブル崩壊      (2000-04〜2002-10): 日経 -60% / ナスダック -78%
#   ブラックマンデー  (1987-10):          日経 -15% / S&P500 -23% （1日）
#   東日本大震災      (2011-03):          日経 -20%（1週間）
#
# 【出力スキーマ】
#   {
#     "portfolio_value":   float,  # 現在の評価額合計（円）
#     "var_95_1day":       float,  # VaR95% 1日最大損失（円）
#     "var_95_pct":        float,  # VaR95% （%）
#     "max_drawdown_worst": float, # ポートフォリオ最悪ドローダウン（%）
#     "scenarios":         {scenario_name: ScenarioResult},
#     "positions_detail":  [PositionStress, ...],
#     "alert_summary":     str,    # Gmail 通知本文
#     "timestamp":         str,
#   }
#
# 【月次通知の仕組み】
#   毎月1日の週次実行時に「月次ストレステスト」として Gmail 送信。
#   平常時は results["stress_test"] に格納のみ。
#
# 【依存】yfinance / config.py / portfolio_holdings.csv
# 【呼出】from stress_tester import run_stress_test
# ==============================================================

from __future__ import annotations

import csv
import json
import math
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

# ── 歴史的暴落シナリオ定義 ─────────────────────────────────
CRASH_SCENARIOS = {
    "lehman": {
        "label":           "リーマンショック（2008-09〜2009-03）",
        "nikkei_drawdown": -0.57,   # 日経225の最大下落率
        "sp500_drawdown":  -0.57,
        "duration_days":   186,     # 暴落継続日数
        "recovery_years":  4.5,     # 底からの回復年数
        "color":           "🔴",
    },
    "corona": {
        "label":           "コロナショック（2020-02〜2020-03）",
        "nikkei_drawdown": -0.34,
        "sp500_drawdown":  -0.34,
        "duration_days":   33,
        "recovery_years":  0.5,
        "color":           "🟠",
    },
    "it_bubble": {
        "label":           "ITバブル崩壊（2000-04〜2002-10）",
        "nikkei_drawdown": -0.60,
        "sp500_drawdown":  -0.49,
        "duration_days":   916,
        "recovery_years":  7.0,
        "color":           "🔴",
    },
    "black_monday": {
        "label":           "ブラックマンデー（1987-10 / 1日）",
        "nikkei_drawdown": -0.15,
        "sp500_drawdown":  -0.23,
        "duration_days":   1,
        "recovery_years":  2.0,
        "color":           "🟡",
    },
    "custom_30pct": {
        "label":           "仮想シナリオ（市場-30%）",
        "nikkei_drawdown": -0.30,
        "sp500_drawdown":  -0.30,
        "duration_days":   90,
        "recovery_years":  2.0,
        "color":           "🟡",
    },
}

FETCH_DELAY      = 0.25
VAR_LOOKBACK     = 252    # VaR計算期間（1年営業日）
MDD_LOOKBACK     = 504    # 最大ドローダウン計算期間（2年）


# ==============================================================
# BLOCK 1: ポートフォリオ読み込み（position_sizer.py と共通）
# ==============================================================

def _load_portfolio(cfg) -> list:
    """portfolio_holdings.csv を読み込む。"""
    candidates = [
        Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "portfolio_holdings.csv",
        Path(__file__).parent / "data" / "portfolio_holdings.csv",
        Path(__file__).parent / "portfolio_holdings.csv",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            holdings = []
            with open(p, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    t = row.get("ticker", "").strip()
                    if not t:
                        continue
                    holdings.append({
                        "ticker":       t,
                        "name":         row.get("name", t)[:20],
                        "sector_code":  row.get("sector_code", ""),
                        "market":       row.get("market", "JP"),
                        "quantity":     float(row.get("quantity", 0) or 0),
                        "avg_cost_jpy": float(row.get("avg_cost_jpy", 0) or 0),
                        "market_value_jpy": float(row.get("market_value_jpy", 0) or 0),
                    })
            return holdings
        except Exception:
            pass
    return []


# ==============================================================
# BLOCK 2: yfinance でベータ・価格・リターン取得
# ==============================================================

def _fetch_stock_data(ticker: str, usdjpy: float = 150.0) -> Optional[dict]:
    """
    1銘柄のベータ値・現在価格・過去リターン系列を取得する。

    Returns:
        {
          "current_price":  float,
          "beta":           float,
          "daily_returns":  [float, ...],   # 日次リターン（%）
          "max_drawdown":   float,          # 過去2年の最大ドローダウン（%）
        }
    """
    try:
        import yf_cache as yf

        t    = yf.Ticker(ticker)
        info = t.info or {}

        # 現在価格
        price = (info.get("regularMarketPrice") or
                 info.get("currentPrice") or
                 info.get("previousClose"))
        if not price:
            return None
        price = float(price)

        # ベータ値（取得できない場合は市場ベータ=1.0 と仮定）
        beta = info.get("beta")
        if beta is None or str(beta) == "None":
            beta = 1.0   # データなし → 市場平均と仮定
        else:
            try:
                beta = float(beta)
                # 異常値クリップ（-2〜5の範囲に収める）
                beta = max(-2.0, min(5.0, beta))
            except (TypeError, ValueError):
                beta = 1.0

        # 過去2年の価格履歴
        end   = datetime.now()
        start = end - timedelta(days=MDD_LOOKBACK + 60)

        hist = t.history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=True,
        )
        if hist is None or len(hist) < 20:
            return {
                "current_price": price,
                "beta":          beta,
                "daily_returns": [],
                "max_drawdown":  None,
            }

        closes = hist["Close"].dropna().tolist()

        # 日次リターン（%）
        daily_returns = [
            (closes[i] - closes[i-1]) / closes[i-1] * 100
            for i in range(1, len(closes))
        ]

        # 最大ドローダウン計算
        peak = closes[0]
        max_dd = 0.0
        for c in closes[1:]:
            if c > peak:
                peak = c
            dd = (c - peak) / peak
            if dd < max_dd:
                max_dd = dd

        return {
            "current_price": price,
            "beta":          round(beta, 3),
            "daily_returns": daily_returns[-VAR_LOOKBACK:],
            "max_drawdown":  round(max_dd * 100, 2),   # %表示
        }

    except Exception:
        return None


# ==============================================================
# BLOCK 3: VaR 計算
# ==============================================================

def calc_var(
    portfolio_value: float,
    daily_returns_pct: list,
    confidence: float = 0.95,
) -> dict:
    """
    ポートフォリオの VaR（バリューアットリスク）を計算する。

    2手法を併用:
      1. 正規分布仮定: VaR = -（μ - z × σ）× portfolio_value
      2. 歴史的シミュレーション: 下位 (1-confidence) パーセンタイル

    Args:
        portfolio_value:     現在の評価額（円）
        daily_returns_pct:   日次リターン（%）のリスト（全保有銘柄の加重平均）
        confidence:          信頼水準（0.95 = 95%）

    Returns:
        {
          "var_normal":  float,   # 正規分布仮定 VaR（円）
          "var_hist":    float,   # 歴史的シミュレーション VaR（円）
          "var_use":     float,   # 採用値（より保守的な方）
          "var_pct":     float,   # VaR / 評価額 × 100
          "mean_return": float,
          "std_return":  float,
        }
    """
    if not daily_returns_pct or len(daily_returns_pct) < 20:
        return {
            "var_normal": None, "var_hist": None,
            "var_use": None, "var_pct": None,
            "mean_return": None, "std_return": None,
        }

    n    = len(daily_returns_pct)
    mean = sum(daily_returns_pct) / n
    var_s = sum((r - mean) ** 2 for r in daily_returns_pct) / (n - 1)
    std  = math.sqrt(var_s)

    # 正規分布仮定（95%の場合 z=1.645）
    z_map = {0.90: 1.282, 0.95: 1.645, 0.99: 2.326}
    z = z_map.get(confidence, 1.645)
    var_normal_pct = -(mean - z * std)
    var_normal     = portfolio_value * var_normal_pct / 100

    # 歴史的シミュレーション（下位5%）
    sorted_ret  = sorted(daily_returns_pct)
    idx         = max(0, int((1 - confidence) * n) - 1)
    var_hist_pct = -sorted_ret[idx]
    var_hist     = portfolio_value * var_hist_pct / 100

    # 保守的な方（大きい損失）を採用
    var_use = max(var_normal, var_hist)
    var_pct = var_use / portfolio_value * 100

    return {
        "var_normal":  round(var_normal, 0),
        "var_hist":    round(var_hist, 0),
        "var_use":     round(var_use, 0),
        "var_pct":     round(var_pct, 2),
        "mean_return": round(mean, 4),
        "std_return":  round(std, 4),
    }


# ==============================================================
# BLOCK 4: ストレスシナリオ計算
# ==============================================================

def calc_stress_scenario(
    holdings_with_data: list,
    scenario:           dict,
    usdjpy:             float,
) -> dict:
    """
    1つの歴史的暴落シナリオでのポートフォリオ損失を計算する。

    Args:
        holdings_with_data: [{holding, stock_data, position_value_jpy}, ...]
        scenario:           CRASH_SCENARIOS の1エントリ
        usdjpy:             USD/JPY レート（米国株の円換算用）

    Returns:
        {
          "total_loss_jpy":   float,
          "total_loss_pct":   float,
          "positions":        [PositionStress, ...],
          "worst_3":          [PositionStress, ...],
        }
    """
    positions = []
    total_value = sum(h["position_value_jpy"] for h in holdings_with_data)

    for item in holdings_with_data:
        h    = item["holding"]
        sd   = item.get("stock_data")
        val  = item["position_value_jpy"]
        mkt  = h.get("market", "JP")

        # 市場下落率（JP株はnikkei, US株はsp500）
        base_drawdown = (scenario["nikkei_drawdown"] if mkt == "JP"
                         else scenario["sp500_drawdown"])

        # ベータ加重（beta=2なら市場の2倍動く）
        beta = (sd.get("beta", 1.0) if sd else 1.0) or 1.0
        stock_drawdown = base_drawdown * beta
        stock_drawdown = max(-0.99, stock_drawdown)   # -99%下限

        loss_jpy = val * stock_drawdown   # 負の値

        positions.append({
            "ticker":         h["ticker"],
            "name":           h["name"],
            "market":         mkt,
            "beta":           round(beta, 2),
            "position_value": round(val, 0),
            "base_drawdown":  round(base_drawdown * 100, 1),
            "stock_drawdown": round(stock_drawdown * 100, 1),
            "loss_jpy":       round(loss_jpy, 0),
            "loss_pct":       round(stock_drawdown * 100, 1),
        })

    total_loss = sum(p["loss_jpy"] for p in positions)
    total_loss_pct = total_loss / total_value * 100 if total_value > 0 else 0

    worst_3 = sorted(positions, key=lambda x: x["loss_jpy"])[:3]

    return {
        "total_loss_jpy": round(total_loss, 0),
        "total_loss_pct": round(total_loss_pct, 1),
        "positions":      positions,
        "worst_3":        worst_3,
    }

# ==============================================================
# BLOCK 4.5: [A-2] コスト控除後リターン計算
# ==============================================================

def calc_net_return(
    gross_return_pct: float,
    position_value_jpy: float,
    cfg = None,
) -> dict:
    """
    [A-2] 売買コスト（スプレッド + 手数料）を控除した実質リターンを計算する。

    コスト構成:
      ① 証券会社手数料:     往復 0.2%（デフォルト。SBI/楽天等の定額制を考慮）
      ② 売買スプレッド:     流動性によって異なる
         大型株（流動性高）: 往復 0.05%
         中型株:             往復 0.10%
         小型株（流動性低）: 往復 0.20%
      ③ 税金（譲渡益税）:   20.315%（NISA枠外の場合）
                             0%（NISA枠内の場合）

    Args:
        gross_return_pct:   粗リターン（%）
        position_value_jpy: ポジション評価額（円）
        cfg:                config（コスト設定の参照）

    Returns:
        {
          "gross_return_pct":   float,
          "commission_pct":     float,  # 往復手数料%
          "spread_pct":         float,  # 往復スプレッド%
          "total_cost_pct":     float,  # 合計コスト%
          "tax_pct":            float,  # 税率（利益時のみ）
          "net_return_pct":     float,  # コスト・税引後リターン%
          "net_return_jpy":     float,  # 実質損益（円）
          "breakeven_return_pct": float,  # コスト回収に必要な最低リターン%
        }
    """
    # コスト設定（config から読み込み / デフォルト値使用）
    commission_pct = getattr(cfg, "TRADE_COMMISSION_PCT", 0.002)   # 往復0.2%
    spread_pct     = getattr(cfg, "TRADE_SPREAD_PCT",     0.001)   # 往復0.1%
    tax_rate       = getattr(cfg, "CAPITAL_GAINS_TAX",    0.20315) # 20.315%
    is_nisa        = getattr(cfg, "USE_NISA_ACCOUNT",     False)   # NISA口座

    total_cost_pct = commission_pct + spread_pct

    # 税引前リターン（コスト控除後）
    after_cost_pct = gross_return_pct - total_cost_pct

    # 税引後リターン（利益時のみ課税）
    if after_cost_pct > 0 and not is_nisa:
        net_return_pct = after_cost_pct * (1 - tax_rate)
    else:
        net_return_pct = after_cost_pct   # 損失・NISA口座は非課税

    net_return_jpy      = position_value_jpy * net_return_pct / 100
    breakeven_return_pct = total_cost_pct  # コスト回収に必要な最低リターン

    return {
        "gross_return_pct":     round(gross_return_pct, 4),
        "commission_pct":       round(commission_pct * 100, 4),
        "spread_pct":           round(spread_pct * 100, 4),
        "total_cost_pct":       round(total_cost_pct * 100, 4),
        "tax_pct":              round(tax_rate * 100, 3) if not is_nisa else 0.0,
        "net_return_pct":       round(net_return_pct, 4),
        "net_return_jpy":       round(net_return_jpy, 0),
        "breakeven_return_pct": round(breakeven_return_pct * 100, 4),
        "is_nisa":              is_nisa,
    }


def enrich_with_net_returns(result: dict, cfg = None) -> dict:
    """
    [A-2] run_stress_test の result にコスト控除後リターンを追記する。

    positions_detail の各ポジションに net_return を付与。
    performance_tracker の gross リターンと比較できる形式で出力。
    """
    portfolio_value = result.get("portfolio_value", 0)
    positions_detail = result.get("positions_detail", [])

    net_returns = []
    for pos in positions_detail:
        gross_pct = pos.get("week_return_pct", 0) or 0
        pos_val   = pos.get("position_value_jpy", 0) or 0
        nr = calc_net_return(gross_pct, pos_val, cfg)
        net_returns.append({**pos, "net_return": nr})

    if net_returns:
        result["positions_net_returns"] = net_returns
        # ポートフォリオ全体の加重平均コスト控除後リターン
        weighted_net = sum(
            p["net_return"]["net_return_jpy"]
            for p in net_returns
            if p.get("net_return")
        )
        result["portfolio_net_return_jpy"] = round(weighted_net, 0)
        result["portfolio_net_return_pct"] = (
            round(weighted_net / portfolio_value * 100, 3)
            if portfolio_value > 0 else None
        )

    return result

# ==============================================================
# BLOCK 4.6: [C-1] VaR制約付きスクリーニング
# ==============================================================

def apply_var_constraint_to_screener(
    candidates:      list,
    stress_result:   dict,
    cfg = None,
) -> dict:
    """
    [C-1] ポートフォリオ全体のVaRが閾値を超えた場合、
    Buy候補を上位1〜2銘柄に制限するフィルターを適用する。

    設計思想:
      VaRが高い = 既存ポートフォリオのリスクが高い状態。
      この状況でさらに新規ポジションを追加するのは
      ダリオの「リスクパリティ」に反する。
      → Buy候補を絞ることでポートフォリオ全体のリスクを制御。

    Args:
        candidates:    screener の pass_filters = True な候補リスト
        stress_result: run_stress_test() の戻り値
        cfg:           config

    Returns:
        {
          "constrained":       bool,   # 制約が発動したか
          "var_pct":           float,  # 現在のVaR%
          "threshold_pct":     float,  # 制約発動閾値%
          "max_buy_count":     int,    # 制約後の最大Buy推奨数
          "filtered_candidates": list, # 制約後の候補リスト
          "reason":            str,
        }
    """
    # VaR 閾値（config から取得 / デフォルト 3%）
    var_threshold_pct = getattr(cfg, "VAR_CONSTRAINT_THRESHOLD_PCT", 3.0)
    max_buy_normal    = getattr(cfg, "SCREENER_MAX_BUY_NORMAL",      10)
    max_buy_constrained = getattr(cfg, "SCREENER_MAX_BUY_CONSTRAINED", 2)

    # stress_result から VaR% を取得
    var_data   = stress_result.get("var_95_1day", {}) if isinstance(stress_result, dict) else {}
    var_pct    = var_data.get("var_pct") if isinstance(var_data, dict) else None

    # VaR データがない（stress_test 未実行）= 制約なし
    if var_pct is None or stress_result.get("skipped"):
        return {
            "constrained":        False,
            "var_pct":            None,
            "threshold_pct":      var_threshold_pct,
            "max_buy_count":      max_buy_normal,
            "filtered_candidates": candidates,
            "reason":             "stress_test未実行 → 制約なし",
        }

    # VaR が閾値未満 → 制約なし
    if var_pct < var_threshold_pct:
        return {
            "constrained":        False,
            "var_pct":            var_pct,
            "threshold_pct":      var_threshold_pct,
            "max_buy_count":      max_buy_normal,
            "filtered_candidates": candidates,
            "reason":             f"VaR {var_pct:.2f}% < 閾値 {var_threshold_pct:.1f}%",
        }

    # ── VaR が閾値超 → Buy候補を制限 ─────────────────────────
    # composite_score 降順で上位 max_buy_constrained 件のみ残す
    passed = [c for c in candidates if c.get("pass_filters")]
    passed_sorted = sorted(
        passed,
        key=lambda x: x.get("composite_score", 0),
        reverse=True,
    )
    filtered = passed_sorted[:max_buy_constrained]

    # 制限された候補にフラグを追加
    for c in filtered:
        c["var_constrained"] = True

    reason = (
        f"⚠️ VaR {var_pct:.2f}% ≥ 閾値 {var_threshold_pct:.1f}% "
        f"→ Buy候補を上位{max_buy_constrained}銘柄に制限"
    )
    print(f"    🛡️ [C-1] {reason}")

    return {
        "constrained":        True,
        "var_pct":            var_pct,
        "threshold_pct":      var_threshold_pct,
        "max_buy_count":      max_buy_constrained,
        "filtered_candidates": filtered,
        "reason":             reason,
        "original_count":     len(passed),
    }

# ==============================================================
# BLOCK 4.7: [C-2] 相関制約（同一クラスタから2銘柄以上を出さない）
# ==============================================================

# デフォルトの高相関クラスタ（correlation_heatmap の結果で動的に更新される）
DEFAULT_HIGH_CORR_CLUSTERS = [
    # 金融クラスタ（銀行・証券・保険は相関0.8以上）
    {"name": "金融クラスタ", "sectors": ["1028", "1029", "1030", "1031"]},
    # 素材クラスタ（鉄鋼・非鉄・金属製品）
    {"name": "素材クラスタ", "sectors": ["1012", "1013", "1014"]},
    # インフラクラスタ（陸運・海運・空運）
    {"name": "インフラクラスタ", "sectors": ["1021", "1022", "1023"]},
    # 内需消費クラスタ（食料品・小売・サービス）
    {"name": "内需消費クラスタ", "sectors": ["1004", "1027", "1033"]},
    # エネルギー素材（石油・化学）
    {"name": "エネルギー素材クラスタ", "sectors": ["1009", "1007"]},
]

# 相関閾値（これ以上の相関を持つ業種ペアを同一クラスタとみなす）

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'ST_CORR_CLUSTER_THRESHOLD': 'CORR_CLUSTER_THRESHOLD',
    'B4_STRESS_TESTER_VAR_LOOKBACK': 'VAR_LOOKBACK',
    'B4_STRESS_TESTER_MDD_LOOKBACK': 'MDD_LOOKBACK',
    'B5_STRESS_TESTER_FETCH_DELAY': 'FETCH_DELAY',
}

CORR_CLUSTER_THRESHOLD = 0.75


def _build_clusters_from_heatmap(corr_heatmap_result: dict) -> list:
    """
    correlation_heatmap.py の結果から高相関クラスタを動的に構築する。

    Returns:
        [{name, sectors}, ...]
    """
    # correlation_heatmap の出力形式に応じてパース
    if not isinstance(corr_heatmap_result, dict):
        return DEFAULT_HIGH_CORR_CLUSTERS

    corr_matrix = corr_heatmap_result.get("correlation_matrix")
    if not corr_matrix or not isinstance(corr_matrix, dict):
        return DEFAULT_HIGH_CORR_CLUSTERS

    # Union-Find で高相関業種をクラスタリング
    parent = {}
    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent.get(x, x), x)
            x = parent.get(x, x)
        return x
    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    sectors = set(corr_matrix.keys())
    for sec_a in sectors:
        for sec_b, corr_val in corr_matrix.get(sec_a, {}).items():
            if sec_a != sec_b:
                try:
                    if abs(float(corr_val)) >= CORR_CLUSTER_THRESHOLD:
                        union(sec_a, sec_b)
                except (TypeError, ValueError):
                    pass

    # 同一クラスタにまとめる
    cluster_map: dict[str, list] = {}
    for sec in sectors:
        root = find(sec)
        cluster_map.setdefault(root, []).append(sec)

    clusters = [
        {"name": f"相関クラスタ_{root[:4]}", "sectors": sorted(secs)}
        for root, secs in cluster_map.items()
        if len(secs) >= 2   # 1銘柄のみのクラスタは除外
    ]

    return clusters if clusters else DEFAULT_HIGH_CORR_CLUSTERS


def apply_correlation_constraint(
    candidates:            list,
    correlation_heatmap:   dict = None,
    max_per_cluster:       int  = 1,
    cfg = None,
) -> dict:
    """
    [C-2] 同一相関クラスタから max_per_cluster 銘柄以上の Buy候補を出さない。

    設計思想:
      銀行と保険は相関0.8以上 → どちらかが暴落したら両方暴落する。
      同一クラスタから2銘柄以上買うことはリスクの分散になっていない。
      → 相関の高いクラスタ内では composite_score 最上位のみを残す。

    Args:
        candidates:          screener の pass_filters = True な候補リスト
        correlation_heatmap: correlation_heatmap.py の戻り値（動的クラスタ構築用）
        max_per_cluster:     1クラスタあたりの最大銘柄数（デフォルト1）
        cfg:                 config

    Returns:
        {
          "constrained":          bool,
          "clusters_applied":     [{name, sectors, selected, excluded}, ...],
          "filtered_candidates":  list,   # 制約後の候補リスト
          "excluded_tickers":     list,
          "reason":               str,
        }
    """
    if not getattr(cfg, "ENABLE_CORR_CONSTRAINT", True):
        return {
            "constrained":         False,
            "clusters_applied":    [],
            "filtered_candidates": candidates,
            "excluded_tickers":    [],
            "reason":              "ENABLE_CORR_CONSTRAINT=False",
        }

    max_per = getattr(cfg, "MAX_CANDIDATES_PER_CORR_CLUSTER", max_per_cluster)

    # クラスタ定義（動的 or デフォルト）
    clusters = _build_clusters_from_heatmap(correlation_heatmap or {})

    passed         = [c for c in candidates if c.get("pass_filters")]
    excluded_tickers = []
    clusters_applied = []

    # 処理済み ticker を追跡（一度除外した銘柄を重複除外しない）
    already_selected: set[str] = set()
    already_excluded: set[str] = set()

    for cluster in clusters:
        cluster_sectors = set(cluster["sectors"])
        cluster_name    = cluster["name"]

        # このクラスタに属する候補を composite_score 降順で取得
        in_cluster = [
            c for c in passed
            if c.get("sector_code") in cluster_sectors
            and c.get("ticker") not in already_excluded
        ]
        if not in_cluster:
            continue

        in_cluster.sort(key=lambda x: x.get("composite_score", 0), reverse=True)

        # 上位 max_per 銘柄を選択、残りを除外
        selected  = in_cluster[:max_per]
        to_exclude = in_cluster[max_per:]

        if to_exclude:
            selected_tickers  = [c["ticker"] for c in selected]
            excluded_tickers_ = [c["ticker"] for c in to_exclude]

            for c in to_exclude:
                c["corr_excluded"]       = True
                c["corr_cluster"]        = cluster_name
                c["corr_exclude_reason"] = (
                    f"同一相関クラスタ内で{selected_tickers}より低スコア"
                )
                already_excluded.add(c["ticker"])
                excluded_tickers.append(c["ticker"])

            clusters_applied.append({
                "name":          cluster_name,
                "sectors":       list(cluster_sectors),
                "selected":      selected_tickers,
                "excluded":      excluded_tickers_,
                "cluster_size":  len(in_cluster),
            })

        for c in selected:
            already_selected.add(c["ticker"])

    constrained = bool(excluded_tickers)
    if constrained:
        print(
            f"    🔗 [C-2] 相関制約適用: "
            f"{len(excluded_tickers)}銘柄を重複除外 "
            f"（{len(clusters_applied)}クラスタ）"
        )

    reason = (
        f"相関制約: {len(clusters_applied)}クラスタで"
        f"{len(excluded_tickers)}銘柄を除外"
        if constrained else "相関制約: 除外なし"
    )

    # 除外銘柄を除いた最終候補リスト
    filtered = [
        c for c in candidates
        if c.get("ticker") not in already_excluded
    ]

    return {
        "constrained":         constrained,
        "clusters_applied":    clusters_applied,
        "filtered_candidates": filtered,
        "excluded_tickers":    excluded_tickers,
        "reason":              reason,
    }

# ==============================================================
# BLOCK 5: テキスト整形
# ==============================================================

def _fmt_yen(v: float) -> str:
    sign = "+" if v >= 0 else ""
    if abs(v) >= 1e8:
        return f"{sign}{v/1e8:.2f}億円"
    elif abs(v) >= 1e4:
        return f"{sign}{v/1e4:.0f}万円"
    return f"{sign}{v:.0f}円"


def format_stress_report(result: dict) -> str:
    """週次レポート・月次 Gmail 向けのテキストを生成する。"""
    pv     = result.get("portfolio_value", 0)
    var_d  = result.get("var_95_1day", {})
    mdd    = result.get("max_drawdown_portfolio")

    lines = [
        "## 🛡️ ストレステスト結果",
        "",
        f"**評価額**: {_fmt_yen(pv)}  "
        f"**VaR(95% 1日)**: {_fmt_yen(-(var_d.get('var_use') or 0))}  "
        f"({var_d.get('var_pct', 0):.2f}%)",
        "",
    ]
    if mdd is not None:
        lines.append(f"**過去2年最大ドローダウン**: {mdd:.1f}%")
        lines.append("")

    lines += [
        "### 歴史的暴落シナリオ別 想定損失",
        "",
        "| シナリオ | 市場下落 | 想定損失 | 損失率 | 最大被害3銘柄 |",
        "|---------|---------|---------|-------|------------|",
    ]

    for sc_name, sc_data in result.get("scenarios", {}).items():
        sc_def    = CRASH_SCENARIOS.get(sc_name, {})
        color     = sc_def.get("color", "⚪")
        label     = sc_def.get("label", sc_name)[:18]
        mkt_dd    = f"{sc_def.get('nikkei_drawdown', 0)*100:.0f}%"
        loss_jpy  = _fmt_yen(sc_data["total_loss_jpy"])
        loss_pct  = f"{sc_data['total_loss_pct']:.1f}%"
        worst     = " / ".join(
            f"{p['ticker']}({p['loss_pct']:.0f}%)"
            for p in sc_data["worst_3"][:2]
        )
        lines.append(f"| {color} {label} | {mkt_dd} | {loss_jpy} | {loss_pct} | {worst} |")

    lines.append("")
    lines.append(
        "> ⚠️ 想定損失は「ベータ × 市場下落率」の線形近似。"
        "実際の下落率は相関・流動性等により異なります。"
    )

    return "\n".join(lines)


def format_stress_gmail_html(result: dict) -> str:
    """月次 Gmail 通知用の HTML を生成する。"""
    pv     = result.get("portfolio_value", 0)
    var_d  = result.get("var_95_1day", {})
    now    = datetime.now().strftime("%Y年%m月")

    rows = ""
    for sc_name, sc_data in result.get("scenarios", {}).items():
        sc_def  = CRASH_SCENARIOS.get(sc_name, {})
        color   = "#c0392b" if sc_data["total_loss_pct"] < -30 else (
                   "#e67e22" if sc_data["total_loss_pct"] < -20 else "#f39c12")
        rows += (
            f"<tr>"
            f"<td>{sc_def.get('label','')[:22]}</td>"
            f"<td style='color:{color};font-weight:bold'>"
            f"  {_fmt_yen(sc_data['total_loss_jpy'])}"
            f"</td>"
            f"<td style='color:{color}'>{sc_data['total_loss_pct']:.1f}%</td>"
            f"</tr>"
        )

    var_jpy = _fmt_yen(-(var_d.get("var_use") or 0))
    var_pct = f"{var_d.get('var_pct', 0):.2f}%"

    html = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:auto;">
<h2 style="color:#2c3e50">🛡️ 月次ストレステスト報告 — {now}</h2>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
  <tr style="background:#ecf0f1">
    <td style="padding:8px"><b>ポートフォリオ評価額</b></td>
    <td style="padding:8px;font-size:18px;font-weight:bold">{_fmt_yen(pv)}</td>
  </tr>
  <tr>
    <td style="padding:8px">VaR 95% 1日最大損失</td>
    <td style="padding:8px;color:#e74c3c">{var_jpy}（{var_pct}）</td>
  </tr>
</table>

<h3 style="color:#2c3e50">📊 歴史的暴落シナリオ別 想定損失</h3>
<table style="width:100%;border-collapse:collapse;border:1px solid #bdc3c7">
  <tr style="background:#2c3e50;color:white">
    <th style="padding:8px;text-align:left">シナリオ</th>
    <th style="padding:8px">想定損失（円）</th>
    <th style="padding:8px">損失率</th>
  </tr>
  {rows}
</table>

<p style="color:#7f8c8d;font-size:12px;margin-top:20px">
※ 想定損失はベータ×市場下落率の線形近似。実際の結果とは異なります。<br>
※ 投資判断の参考情報であり、損失を保証するものではありません。<br>
stock_analyzer — 自動生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}
</p>
</body></html>
"""
    return html


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_stress_test(
    cfg,
    macro_snapshot:    Optional[dict] = None,
    force_monthly_mail: bool = False,
) -> dict:
    """
    ストレステストを実行する。

    stock_analyzer_main.py から以下のように呼び出す（週次）:
        _st = _load_local_module("stress_tester", ...)
        st_result = _st.run_stress_test(cfg, results.get("macro_snapshot", {}))
        results["stress_test"] = st_result

    毎月1日（または force_monthly_mail=True）のとき Gmail 送信。

    Args:
        cfg:               config モジュール
        macro_snapshot:    為替レート参照用
        force_monthly_mail: True で強制 Gmail 送信

    Returns:
        ストレステスト結果 dict
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🛡️ ストレステスト開始...")

    holdings = _load_portfolio(cfg)
    if not holdings:
        print("    ⚠️  portfolio_holdings.csv なし → スキップ")
        return {"skipped": True, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    usdjpy = float((macro_snapshot or {}).get("usdjpy") or
                   getattr(cfg, "USDJPY_DEFAULT", 150.0))

    # 各銘柄のデータ取得
    print(f"    {len(holdings)}銘柄のデータ取得中...")
    holdings_with_data = []
    portfolio_returns_weighted = []   # 加重平均リターン用

    for h in holdings:
        ticker = h["ticker"]
        sd = _fetch_stock_data(ticker, usdjpy)
        time.sleep(FETCH_DELAY)

        # 株価が取れない米国株では円の取得単価に為替を掛けない（評価額が膨張するため）
        from sync_holdings import holding_value_jpy
        value_jpy = holding_value_jpy(h, sd["current_price"] if sd else None, usdjpy)

        holdings_with_data.append({
            "holding":            h,
            "stock_data":         sd,
            "position_value_jpy": value_jpy,
        })

        beta = (sd["beta"] if sd else 1.0) or 1.0
        print(f"      {ticker}: beta={beta:.2f} value={value_jpy/1e4:.0f}万円")

    total_value = sum(h["position_value_jpy"] for h in holdings_with_data)
    print(f"    ポートフォリオ評価額: {total_value/1e4:.0f}万円")

    if total_value <= 0:
        return {"skipped": True, "reason": "評価額ゼロ", "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # ポートフォリオの加重平均リターンを計算（VaR用）
    port_returns = []
    for item in holdings_with_data:
        sd  = item.get("stock_data")
        val = item["position_value_jpy"]
        if sd and sd.get("daily_returns") and total_value > 0:
            weight = val / total_value
            rets   = sd["daily_returns"]
            # 短いリターン系列を最長に合わせてゼロパディング
            while len(port_returns) < len(rets):
                port_returns.append(0.0)
            for i, r in enumerate(rets):
                if i < len(port_returns):
                    port_returns[i] += r * weight

    if not port_returns:
        # フォールバック: 全銘柄が取得失敗した場合、ベータ=1.0 市場リターンで代替
        port_returns = [0.0] * VAR_LOOKBACK

    # VaR 計算
    var_result = calc_var(total_value, port_returns)

    # ポートフォリオ全体の最大ドローダウン（加重平均）
    mdd_values = [
        item["stock_data"]["max_drawdown"] * item["position_value_jpy"] / total_value
        for item in holdings_with_data
        if item.get("stock_data") and item["stock_data"].get("max_drawdown") is not None
    ]
    portfolio_mdd = sum(mdd_values) if mdd_values else None

    # 各暴落シナリオ計算
    scenarios = {}
    for sc_name, sc_def in CRASH_SCENARIOS.items():
        scenarios[sc_name] = calc_stress_scenario(
            holdings_with_data, sc_def, usdjpy
        )

    result = {
        "portfolio_value":        round(total_value, 0),
        "var_95_1day":            var_result,
        "max_drawdown_portfolio": round(portfolio_mdd, 1) if portfolio_mdd else None,
        "scenarios":              scenarios,
        "timestamp":              datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["summary_text"]  = format_stress_report(result)
    result["gmail_html"]    = format_stress_gmail_html(result)

    # ── 月次 Gmail 送信 ────────────────────────────────────────
    is_first_of_month = (date.today().day == 1)
    should_send_mail  = force_monthly_mail or is_first_of_month

    if should_send_mail:
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text      import MIMEText

            frm = getattr(cfg, "ALERT_GMAIL_FROM",     "")
            pwd = getattr(cfg, "ALERT_GMAIL_PASSWORD",  "")
            to  = getattr(cfg, "ALERT_GMAIL_TO",        "")

            if frm and pwd and to:
                month_str = datetime.now().strftime("%Y年%m月")
                subject   = f"【月次ストレステスト】{month_str} — {_fmt_yen(total_value)}ポートフォリオ"
                worst_sc  = min(scenarios.items(),
                                key=lambda x: x[1]["total_loss_jpy"])
                worst_loss = _fmt_yen(worst_sc[1]["total_loss_jpy"])
                subject   = (f"【月次ストレステスト】最悪シナリオで{worst_loss}の損失 "
                              f"({worst_sc[1]['total_loss_pct']:.0f}%) — {month_str}")

                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = frm
                msg["To"]      = to
                msg.attach(MIMEText(result["gmail_html"], "html", "utf-8"))

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                    smtp.login(frm, pwd)
                    smtp.sendmail(frm, to, msg.as_string())

                print(f"    ✅ 月次ストレステスト Gmail 送信: {subject[:60]}")
                result["mail_sent"] = True
            else:
                print("    ⏩ Gmail 未設定のため送信スキップ")
                result["mail_sent"] = False
        except Exception as e:
            print(f"    ⚠️  Gmail 送信失敗: {str(e)[:80]}")
            result["mail_sent"] = False

    # 結果サマリー表示
    var_use = var_result.get("var_use")
    if var_use:
        print(f"    📊 VaR(95% 1日): {_fmt_yen(-var_use)} ({var_result.get('var_pct',0):.2f}%)")
    worst = min(scenarios.items(), key=lambda x: x[1]["total_loss_jpy"])
    print(f"    📉 最悪シナリオ: {CRASH_SCENARIOS[worst[0]]['label'][:20]} "
          f"→ {_fmt_yen(worst[1]['total_loss_jpy'])} ({worst[1]['total_loss_pct']:.0f}%)")
    print("    ✅ ストレステスト完了")

    return result
