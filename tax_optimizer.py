# ==============================================================
# tax_optimizer.py — 税・NISA最適化エンジン（Wave R3-5 / S10）
# ==============================================================
# 【役割】四半期＋11月末に、税務・NISA枠の最適化「レポート」を出す。
#   ①損出し候補: 特定口座の含み損銘柄 × 当年実現益>0 →
#      節税額 = min(Σ|含み損|, 実現益)×20.315% を銘柄別に列挙
#      （wash sale的な再取得の注意書きを定型文で添付）
#   ②口座区分推奨: 新規買い注文票に「NISA枠残があり想定保有>1年ならNISA推奨」の1行
#   ③旧NISA期限（2040年）保有分のカウントダウン一覧
#   ④NISA枠使用状況（成長投資枠残高）
# 【原則】**税務の最終判断は健太**（レポートに明記）。自動発注なし（D-01）。
#   入力: 実現損益.csv・portfolio_holdings.csv・取引履歴.csv（best-effort）。
# 使い方: python3 tax_optimizer.py [YYYY]
# ==============================================================

from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_WASH_NOTE = ("※同一銘柄を短期で買い戻すと『取得単価の平準化』で節税効果が"
              "薄れる場合がある（wash sale的注意・最終判断は健太/税理士）。")


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def calc_tax_harvest(positions: list, realized_gain_ytd: float,
                     rate: float = 0.20315) -> dict:
    """
    損出し（タックスロスハーベスティング）候補を計算する。
    positions: [{"ticker","unrealized_pnl","account_type"}]（account_type: NISA/特定）
    realized_gain_ytd: 当年の実現損益合計（>0 のとき損出しに意味がある）
    Returns:
      {"candidates":[{ticker, loss}], "total_loss":.., "harvestable":..,
       "tax_saving":.., "realized_gain": realized_gain_ytd}
    節税は「実現益と相殺できる範囲」= min(Σ含み損, 実現益) にのみ効く。
    """
    losers = [{"ticker": p.get("ticker", ""),
               "loss": abs(float(p.get("unrealized_pnl", 0) or 0))}
              for p in positions
              if (p.get("account_type") or "特定") == "特定"
              and float(p.get("unrealized_pnl", 0) or 0) < 0]
    losers.sort(key=lambda x: -x["loss"])
    total_loss = round(sum(x["loss"] for x in losers), 2)
    gain = max(0.0, float(realized_gain_ytd or 0))
    harvestable = round(min(total_loss, gain), 2)   # 相殺できる損失
    return {
        "candidates": losers,
        "total_loss": total_loss,
        "realized_gain": round(gain, 2),
        "harvestable": harvestable,
        "tax_saving": round(harvestable * rate, 2),
    }


def nisa_recommendation(order_hold_months: int, nisa_remaining: float,
                        order_value: float, min_months: int = None,
                        cfg=None) -> str:
    # 2026-07-25 バッチ6: 想定保有下限を config.NISA_LONG_HOLD_MONTHS から読む
    # （従来は既定引数12の直書きで config 側が未参照だった）。
    # 既定引数に置かず None 判定にしているのは、def 実行時に値が固定されて
    # config の変更が反映されなくなるのを避けるため。
    """新規買い注文への口座区分推奨1行。"""
    if min_months is None:
        min_months = int(getattr(cfg, "NISA_LONG_HOLD_MONTHS", 12))
    if nisa_remaining >= order_value and order_hold_months >= min_months:
        return "NISA推奨（成長投資枠残あり・想定保有>1年＝非課税メリット大）"
    if nisa_remaining < order_value:
        return "特定口座（NISA成長投資枠が不足）"
    return "特定口座（短期想定＝NISAの非課税メリットが限定的）"


def old_nisa_countdown(positions: list, current_year: int,
                       expiry_year: int = 2040) -> list:
    """旧NISA保有分の非課税期限カウントダウン。"""
    out = []
    for p in positions:
        if (p.get("account_type") or "") in ("旧NISA", "old_nisa", "一般NISA"):
            out.append({"ticker": p.get("ticker", ""),
                        "years_left": max(0, expiry_year - current_year),
                        "expiry_year": expiry_year})
    return sorted(out, key=lambda x: x["years_left"])


def nisa_usage(positions: list, annual_limit: int, total_limit: int) -> dict:
    """NISA成長投資枠の使用状況（簿価ベース）。"""
    used = sum(float(p.get("cost_basis", 0) or 0)
               for p in positions
               if (p.get("account_type") or "") in ("NISA", "成長投資枠"))
    return {"used": round(used, 0),
            "total_limit": total_limit,
            "total_remaining": round(max(0, total_limit - used), 0),
            "annual_limit": annual_limit}


def build_report(harvest: dict, countdown: list, usage: dict,
                 year: int) -> str:
    lines = [f"# 税・NISA最適化レポート {year}年（S10）",
             "",
             "> **税務の最終判断は健太（必要に応じ税理士）。** 本レポートは"
             "候補提示のみ・自動発注なし（D-01）。",
             "",
             "## ① 損出し候補（特定口座・当年実現益との相殺）"]
    lines.append(f"- 当年実現益: ¥{harvest['realized_gain']:,.0f} / "
                 f"特定口座の含み損合計: ¥{harvest['total_loss']:,.0f}")
    lines.append(f"- **相殺可能額: ¥{harvest['harvestable']:,.0f} → "
                 f"節税額の目安: ¥{harvest['tax_saving']:,.0f}**（20.315%）")
    if harvest["candidates"]:
        lines += ["", "| 銘柄 | 含み損 |", "|---|---|"]
        for c in harvest["candidates"][:15]:
            lines.append(f"| {c['ticker']} | ¥{c['loss']:,.0f} |")
        lines.append(f"\n{_WASH_NOTE}")
    else:
        lines.append("- 損出し候補: なし（特定口座に含み損銘柄なし）")

    lines += ["", "## ② 口座区分推奨",
              "- 新規買い注文票には position_sizer/注文票生成時に"
              " nisa_recommendation() の1行が自動付記される"
              "（NISA枠残×想定保有期間で判定）。"]

    lines += ["", "## ③ 旧NISA非課税期限カウントダウン"]
    if countdown:
        lines += ["| 銘柄 | 残り年数 | 期限 |", "|---|---|---|"]
        for c in countdown:
            lines.append(f"| {c['ticker']} | {c['years_left']}年 | {c['expiry_year']} |")
    else:
        lines.append("- 旧NISA保有分: なし（データなし）")

    lines += ["", "## ④ NISA成長投資枠 使用状況",
              f"- 使用済（簿価）: ¥{usage['used']:,.0f} / 生涯上限 ¥{usage['total_limit']:,.0f}",
              f"- **残り: ¥{usage['total_remaining']:,.0f}**（年間上限 ¥{usage['annual_limit']:,.0f}）"]
    return "\n".join(lines) + "\n"


# ── I/O層（best-effort） ─────────────────────────────────────

def _read_csv(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _realized_gain_ytd(cfg, year: int) -> float:
    """実現損益.csv から当年の実現損益合計を集計（列名に頑健）。"""
    vault = Path(getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", "")) if cfg else Path("")
    candidates = [vault.parent / "data" / "実現損益.csv",
                  vault / "data" / "実現損益.csv",
                  Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "実現損益.csv"]
    for p in candidates:
        rows = _read_csv(p)
        if not rows:
            continue
        total = 0.0
        for r in rows:
            d = str(r.get("date") or r.get("約定日") or r.get("決済日") or "")
            if not d.startswith(str(year)):
                continue
            for k in ("realized_pnl", "実現損益", "損益", "pnl"):
                if r.get(k) not in (None, ""):
                    try:
                        total += float(str(r[k]).replace(",", ""))
                    except Exception:
                        pass
                    break
        return total
    return 0.0


def _load_positions(cfg) -> list:
    """portfolio_holdings.csv を tax_optimizer 用に整形（含み損はbest-effort）。"""
    base_dir = Path(getattr(cfg, "PROJECT_DIR", "."))
    path = Path(getattr(cfg, "PORTFOLIO_HOLDINGS_PATH",
                        base_dir / "data" / "portfolio_holdings.csv"))
    try:
        from data_reliability import fetch_price_safe
    except Exception:
        fetch_price_safe = None
    out = []
    for h in _read_csv(path):
        ticker = (h.get("ticker") or "").strip()
        qty = float(h.get("quantity", 0) or 0)
        avg = float(h.get("avg_cost_jpy", 0) or 0)
        if not ticker or qty <= 0:
            continue
        price = None
        if fetch_price_safe:
            try:
                price = fetch_price_safe(cfg, ticker).get("close")
            except Exception:
                price = None
        unrealized = (float(price) - avg) * qty if price is not None else 0.0
        out.append({"ticker": ticker, "unrealized_pnl": unrealized,
                    "account_type": (h.get("account_type") or "特定").strip(),
                    "cost_basis": avg * qty})
    return out


def main():
    year = int(sys.argv[1]) if len(sys.argv) > 1 else date.today().year
    try:
        import config as cfg
    except Exception:
        cfg = None
    rate = getattr(cfg, "TAX_RATE_NISA", 0.20315)
    positions = _load_positions(cfg)
    harvest = calc_tax_harvest(positions, _realized_gain_ytd(cfg, year), rate)
    countdown = old_nisa_countdown(
        positions, year, getattr(cfg, "OLD_NISA_EXPIRY_YEAR", 2040))
    usage = nisa_usage(positions,
                       getattr(cfg, "NISA_GROWTH_ANNUAL_LIMIT", 2_400_000),
                       getattr(cfg, "NISA_GROWTH_TOTAL_LIMIT", 12_000_000))
    md = build_report(harvest, countdown, usage, year)

    insight_dir = Path(getattr(cfg, "INSIGHT_DIR", REPO / "stock_data" / "insights"))
    out = insight_dir / f"tax_report_{year}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"✅ 税・NISAレポート出力: {out}（節税目安 ¥{harvest['tax_saving']:,.0f}）")


if __name__ == "__main__":
    main()
