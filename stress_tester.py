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
        import yfinance as yf

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

        qty   = h.get("quantity", 0)
        price = (sd["current_price"] if sd else h.get("avg_cost_jpy", 0))
        mkt   = h.get("market", "JP")

        if mkt == "US":
            value_jpy = qty * price * usdjpy
        else:
            value_jpy = qty * price

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
