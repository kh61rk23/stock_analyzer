# ==============================================================
# position_sizer.py  v1.0  (3-3 / C-3)
# ATRベースのポジションサイズ計算
# ==============================================================
# 【役割】
#   「総資産の1%を1回の取引リスクとして許容する」という
#   プロのポジションサイジング原則を自動算出する。
#
# 【計算ロジック】
#   ATR（Average True Range）= 過去14日の価格変動幅の平均
#
#   ポジションサイズ（株数）=
#     (総資産 × リスク許容率) / (ATR × ATRマルチプライヤー)
#
#   逆指値価格 = 現在価格 - (ATR × ATRマルチプライヤー)
#   (ATRマルチプライヤー = デフォルト2.0 = 2ATR下に逆指値)
#
# 【計算例】
#   総資産1000万円、リスク1%、ATR=250円、マルチプライヤー2.0
#   → 1回の最大損失 = 1000万 × 0.01 = 10万円
#   → 損失幅 = 250円 × 2.0 = 500円/株
#   → ポジションサイズ = 10万 ÷ 500 = 200株
#   → 投資額 = 200株 × 現在株価
#
# 【出力スキーマ（run_position_sizer の戻り値）】
#   {
#     "total_assets_jpy":  float,   # 入力した総資産額
#     "risk_per_trade_pct": float,  # リスク許容率（デフォルト1%）
#     "positions": [
#       {
#         "ticker":          str,
#         "name":            str,
#         "current_price":   float,
#         "atr14":           float,   # 14日ATR
#         "atr_pct":         float,   # ATR / 現在価格 × 100
#         "stop_loss_price": float,   # 逆指値価格
#         "stop_loss_pct":   float,   # 逆指値距離%
#         "suggested_shares": int,    # 推奨株数
#         "suggested_lot":   int,     # 推奨単元数（100株単位）
#         "position_value":  float,   # 投資額（円）
#         "position_pct":    float,   # 総資産に対する比率%
#         "risk_amount":     float,   # 実際のリスク額（円）
#         "risk_reward_ratio": float, # リスクリワード（target設定時）
#         "sector_score":    float,   # セクタースコア（sector_results から）
#       },
#       ...
#     ],
#     "summary_text":  str,
#     "timestamp":     str,
#   }
#
# 【依存】yfinance / config.py / portfolio_holdings.csv
# 【呼出】from position_sizer import run_position_sizer
# ==============================================================

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── デフォルトパラメータ ─────────────────────────────────────
ATR_PERIOD         = 14     # ATR計算期間（日）
ATR_MULTIPLIER     = 2.0    # 逆指値 = 現在価格 - ATR × この倍率
RISK_PCT_DEFAULT   = 0.01   # 1トレードあたりのリスク許容率（1%）
MIN_POSITION_PCT   = 0.02   # 最小ポジション比率（2%）
MAX_POSITION_PCT   = 0.15   # 最大ポジション比率（15%）
LOT_SIZE_JP        = 100    # 日本株の単元株数
FETCH_DELAY        = 0.3    # yfinance レート制限


# ==============================================================
# BLOCK 1: ATR 計算
# ==============================================================

def calc_atr(ticker: str, period: int = ATR_PERIOD) -> Optional[dict]:
    """
    yfinance から ATR（Average True Range）を計算する。

    True Range = max(
        High - Low,
        |High - 前日Close|,
        |Low  - 前日Close|
    )
    ATR = 過去 period 日の True Range の単純平均

    Returns:
        {
          "atr14":         float,   # ATR（円 or ドル）
          "atr_pct":       float,   # ATR / 現在価格 × 100
          "current_price": float,
          "price_52w_high": float,
          "price_52w_low":  float,
        }
        取得失敗時は None
    """
    try:
        import yfinance as yf

        end   = datetime.now()
        start = end - timedelta(days=period * 3)  # 余裕を持って取得

        hist = yf.Ticker(ticker).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=True,
        )
        if hist is None or len(hist) < period + 1:
            return None

        highs  = hist["High"].tolist()
        lows   = hist["Low"].tolist()
        closes = hist["Close"].tolist()

        # True Range を計算
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i]  - lows[i],
                abs(highs[i]  - closes[i-1]),
                abs(lows[i]   - closes[i-1]),
            )
            tr_list.append(tr)

        if len(tr_list) < period:
            return None

        atr14 = round(sum(tr_list[-period:]) / period, 2)
        current_price = round(closes[-1], 2)
        atr_pct = round(atr14 / current_price * 100, 2) if current_price > 0 else None

        return {
            "atr14":          atr14,
            "atr_pct":        atr_pct,
            "current_price":  current_price,
            "price_52w_high": round(max(highs[-252:]), 2) if len(highs) >= 10 else None,
            "price_52w_low":  round(min(lows[-252:]), 2)  if len(lows) >= 10 else None,
        }
    except Exception:
        return None


# ==============================================================
# BLOCK 2: ポジションサイズ計算
# ==============================================================

def calc_position_size(
    atr_data:        dict,
    total_assets:    float,
    risk_pct:        float = RISK_PCT_DEFAULT,
    atr_multiplier:  float = ATR_MULTIPLIER,
    lot_size:        int   = LOT_SIZE_JP,
    target_price:    Optional[float] = None,
) -> dict:
    """
    ATR データからポジションサイズを計算する。

    Args:
        atr_data:       calc_atr() の戻り値
        total_assets:   総資産額（円）
        risk_pct:       1トレードのリスク許容率（0.01 = 1%）
        atr_multiplier: 逆指値 = 現在価格 - ATR × この倍率
        lot_size:       単元株数（日本株は100）
        target_price:   目標価格（指定時のみリスクリワード計算）

    Returns:
        計算結果辞書
    """
    price       = atr_data["current_price"]
    atr         = atr_data["atr14"]

    # 1トレードの最大リスク額
    risk_amount = total_assets * risk_pct

    # 1株あたりの損失幅
    loss_per_share = atr * atr_multiplier
    if loss_per_share <= 0:
        return {"error": "ATR計算エラー"}

    # 推奨株数（連続値）
    raw_shares = risk_amount / loss_per_share

    # 日本株は単元（100株）単位に切り捨て
    suggested_lot    = max(1, int(raw_shares / lot_size))
    suggested_shares = suggested_lot * lot_size

    # ポジション価値と比率
    position_value = suggested_shares * price
    position_pct   = round(position_value / total_assets * 100, 1)

    # 上限・下限クランプ
    if position_pct > MAX_POSITION_PCT * 100:
        suggested_lot    = max(1, int(total_assets * MAX_POSITION_PCT / price / lot_size))
        suggested_shares = suggested_lot * lot_size
        position_value   = suggested_shares * price
        position_pct     = round(position_value / total_assets * 100, 1)
        capped = True
    elif position_pct < MIN_POSITION_PCT * 100:
        suggested_lot    = max(1, int(total_assets * MIN_POSITION_PCT / price / lot_size))
        suggested_shares = suggested_lot * lot_size
        position_value   = suggested_shares * price
        position_pct     = round(position_value / total_assets * 100, 1)
        capped = True
    else:
        capped = False

    # 実際リスク額（クランプ後）
    actual_risk    = suggested_shares * loss_per_share
    actual_risk_pct = round(actual_risk / total_assets * 100, 2)

    # 逆指値価格
    stop_loss      = round(price - loss_per_share, 1)
    stop_loss_pct  = round(loss_per_share / price * 100, 1)

    # リスクリワード
    rrr = None
    if target_price and target_price > price:
        reward = target_price - price
        rrr    = round(reward / loss_per_share, 2)

    return {
        "stop_loss_price":    stop_loss,
        "stop_loss_pct":      stop_loss_pct,
        "suggested_shares":   suggested_shares,
        "suggested_lot":      suggested_lot,
        "position_value":     round(position_value, 0),
        "position_pct":       position_pct,
        "risk_amount":        round(actual_risk, 0),
        "risk_pct_actual":    actual_risk_pct,
        "risk_reward_ratio":  rrr,
        "capped":             capped,
        "loss_per_share":     round(loss_per_share, 1),
    }


# ==============================================================
# BLOCK 3: ポートフォリオ読み込み
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
                        "name":         row.get("name", t),
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
# BLOCK 4: テキスト整形
# ==============================================================

def _fmt_yen(v: float) -> str:
    if abs(v) >= 1e8:
        return f"{v/1e8:.1f}億円"
    elif abs(v) >= 1e4:
        return f"{v/1e4:.0f}万円"
    return f"{v:.0f}円"


def format_position_report(result: dict) -> str:
    """週次レポート・アラート向けテキストを生成する。"""
    total = result.get("total_assets_jpy", 0)
    risk  = result.get("risk_per_trade_pct", 1.0)
    positions = result.get("positions", [])

    lines = [
        "## 💼 ポジションサイジング（ATRベース）",
        "",
        f"**総資産**: {_fmt_yen(total)}  "
        f"**リスク許容**: {risk*100:.1f}%/トレード  "
        f"**ATR期間**: {ATR_PERIOD}日  "
        f"**倍率**: {ATR_MULTIPLIER}×",
        "",
        "| 銘柄 | 現在値 | ATR | 逆指値 | 推奨株数 | 投資額 | リスク額 |",
        "|------|--------|-----|--------|---------|--------|---------|",
    ]

    for pos in positions:
        if pos.get("error"):
            continue
        price  = pos.get("current_price", 0)
        atr    = pos.get("atr14", 0)
        stop   = pos.get("stop_loss_price", 0)
        shares = pos.get("suggested_shares", 0)
        val    = pos.get("position_value", 0)
        risk_a = pos.get("risk_amount", 0)
        atr_p  = pos.get("atr_pct", 0)
        stop_p = pos.get("stop_loss_pct", 0)
        cap    = "⚠️" if pos.get("capped") else ""

        lines.append(
            f"| {pos['name'][:14]}({pos['ticker']}) "
            f"| {price:,.0f}円 "
            f"| {atr:.0f}円({atr_p:.1f}%) "
            f"| {stop:,.0f}円(-{stop_p:.1f}%) "
            f"| {shares:,}株{cap} "
            f"| {_fmt_yen(val)} "
            f"| {_fmt_yen(risk_a)} |"
        )

    lines.append("")
    lines.append(
        "> 逆指値 = 現在価格 - ATR×2。推奨株数はリスク1%ルール基準。"
        "⚠️ は15%上限/2%下限でクランプ済み。"
    )

    return "\n".join(lines)


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_position_sizer(
    cfg,
    total_assets_jpy:  float,
    risk_pct:          float = RISK_PCT_DEFAULT,
    atr_multiplier:    float = ATR_MULTIPLIER,
    sector_results:    Optional[dict] = None,
    tickers_override:  Optional[list] = None,
) -> dict:
    """
    ポートフォリオ全銘柄のポジションサイズを計算する。

    Args:
        cfg:               config モジュール
        total_assets_jpy:  総資産額（円）。config.TOTAL_ASSETS_JPY があればそちらを使う
        risk_pct:          リスク許容率（デフォルト 0.01 = 1%）
        atr_multiplier:    逆指値 = 現在価格 - ATR × この倍率
        sector_results:    results["sectors"]（セクタースコア付与用）
        tickers_override:  ポートフォリオCSV以外のティッカーを使う場合

    Returns:
        ポジションサイジング結果 dict
    """
    # config からの総資産上書き
    total_assets = getattr(cfg, "TOTAL_ASSETS_JPY", total_assets_jpy)

    print(f"\n  💼 ポジションサイジング開始（総資産: {_fmt_yen(total_assets)}）...")

    # 対象銘柄を決定
    if tickers_override:
        holdings = [{"ticker": t, "name": t, "sector_code": "", "market": "JP", "quantity": 0, "avg_cost_jpy": 0}
                    for t in tickers_override]
    else:
        holdings = _load_portfolio(cfg)

    if not holdings:
        print("    ⚠️  portfolio_holdings.csv が見つかりません")
        return {
            "skipped":          True,
            "total_assets_jpy": total_assets,
            "positions":        [],
            "summary_text":     "💼 ポジションサイジング: portfolio_holdings.csv なし",
            "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    print(f"    対象: {len(holdings)}銘柄")

    positions = []
    for h in holdings:
        ticker = h["ticker"]
        print(f"      {ticker} ATR取得中...", end=" ")

        atr_data = calc_atr(ticker)
        if not atr_data:
            print("取得失敗")
            positions.append({
                "ticker": ticker, "name": h["name"],
                "error": "ATR取得失敗",
            })
            time.sleep(FETCH_DELAY)
            continue

        sizing = calc_position_size(
            atr_data       = atr_data,
            total_assets   = total_assets,
            risk_pct       = risk_pct,
            atr_multiplier = atr_multiplier,
            lot_size       = 100 if h.get("market") == "JP" else 1,
        )

        # セクタースコアを付与
        sector_score = None
        if sector_results and h.get("sector_code"):
            sec_data = sector_results.get(h["sector_code"], {})
            if isinstance(sec_data, dict):
                sector_score = sec_data.get("composite_score")

        position = {
            "ticker":         ticker,
            "name":           h["name"][:20],
            "sector_code":    h.get("sector_code", ""),
            "market":         h.get("market", "JP"),
            "quantity":       h.get("quantity", 0),
            "avg_cost_jpy":   h.get("avg_cost_jpy", 0),
            "sector_score":   sector_score,
            **atr_data,
            **sizing,
        }
        positions.append(position)

        print(
            f"ATR={atr_data['atr14']:.0f}円 "
            f"逆指値={sizing.get('stop_loss_price', 0):.0f}円 "
            f"推奨{sizing.get('suggested_shares', 0):,}株"
        )
        time.sleep(FETCH_DELAY)

    result = {
        "total_assets_jpy":  total_assets,
        "risk_per_trade_pct": risk_pct,
        "atr_multiplier":    atr_multiplier,
        "positions":         positions,
        "timestamp":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["summary_text"] = format_position_report(result)

    # CSV 出力
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        out_dir  = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "dashboard"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"position_sizing_{today}.csv"

        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            fieldnames = [
                "ticker", "name", "sector_code", "current_price",
                "atr14", "atr_pct", "stop_loss_price", "stop_loss_pct",
                "suggested_shares", "suggested_lot", "position_value",
                "position_pct", "risk_amount", "risk_pct_actual",
                "risk_reward_ratio", "sector_score",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows([p for p in positions if not p.get("error")])

        result["csv_path"] = str(out_path)
        print(f"    💾 position_sizing_{today}.csv 出力完了")
    except Exception as e:
        print(f"    ⚠️  CSV出力失敗: {e}")

    valid = [p for p in positions if not p.get("error")]
    print(f"    ✅ ポジションサイジング完了: {len(valid)}/{len(holdings)}銘柄")
    return result
