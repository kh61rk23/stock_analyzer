# ==============================================================
# sync_holdings.py — PC の保有銘柄CSV（投資部）→ リポジトリの data/portfolio_holdings.csv
# 2026-10-05 新設（健太指示「保有銘柄がPC内でcsvとしてあるので GitHub Actions もしくは DB に格納して連携」）
#
# 【背景】data/portfolio_holdings.csv は 2026-03 に手でアップロードしたきりで、売却済み（BBAI・IDAI）が残り
#   INPEX・三菱重工・SPCX が無かった。週次の「ポートフォリオへの影響（総評価額 35,577万円）」
#   「ポジションサイジング（総資産1,000万円の既定値・米国株の株価を円と誤読）」が狂う一因だった。
# 【入力】Vault2/CEO室/departments/B-investment/data/保有銘柄.csv（健太が更新する正本・SBI等の明細）
# 【出力】data/portfolio_holdings.csv … 日本株・米国株のみ（口座違いは合算）。既存の列互換＋market_value_jpy
#         data/portfolio_summary.json … 総資産（現金・投信・DC含む）と資産クラス別の内訳・USDJPY
# 【実行】python3 sync_holdings.py [--src <csv>] [--commit]
#   --commit: 変化があれば2ファイルだけ git commit & push（obsidian_sync の Mac 側定期実行から呼ぶ）
# ==============================================================
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from collections import OrderedDict
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent
SRC_DEFAULT = (Path.home() / "Documents" / "Obsidian_Local" / "Vault2_ClaudeCode_Local" / "CEO室"
               / "departments" / "B-investment" / "data" / "保有銘柄.csv")
OUT_CSV = REPO / "data" / "portfolio_holdings.csv"
OUT_SUMMARY = REPO / "data" / "portfolio_summary.json"
FIELDS = ["ticker", "name", "sector_code", "sector_name", "market", "quantity", "avg_cost_jpy",
          "current_price", "purchase_date", "memo", "market_value_jpy"]


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _jp_index() -> tuple[dict, dict]:
    """社名→証券コード、証券コード→業種名（EdinetDB 会社マスタ）。"""
    by_name, industry = {}, {}
    try:
        cache = json.loads((REPO / "stock_data" / "data" / "edinetdb_company_cache.json").read_text(encoding="utf-8"))
        for c in cache.get("companies", []):
            code = str(c.get("sec_code") or "")[:4]
            nm = re.sub(r"(株式会社|\s|　)", "", str(c.get("name") or ""))
            if code and nm:
                by_name[nm] = code
                industry[code] = c.get("industry") or ""
    except (OSError, ValueError):
        pass
    return by_name, industry


def _sector_code(industry_name: str) -> str:
    try:
        import config
        for code, s in config.JAPAN_SECTORS.items():
            if s.get("name_ja") == industry_name:
                return code
    except Exception:
        pass
    return ""


def convert(rows: list[dict], old: dict | None = None) -> tuple[list[dict], dict]:
    """保有銘柄.csv の行 → (portfolio_holdings 行, 資産サマリ)。純関数（old＝既存CSVの ticker→行）。"""
    old = old or {}
    by_name, industry = _jp_index()
    stocks: "OrderedDict[str, dict]" = OrderedDict()
    classes: dict[str, float] = {}
    total = 0.0
    usd_jpy = None
    as_of = ""
    for r in rows:
        mv = _num(r.get("Market_Value_JPY"))
        cls = (r.get("Asset_Class") or "").strip() or "その他"
        if mv is not None:
            classes[cls] = classes.get(cls, 0.0) + mv
            total += mv
        as_of = max(as_of, str(r.get("Price_Date") or ""))
        if cls not in ("日本株", "米国株"):
            continue
        code = (r.get("Code") or "").strip()
        name = re.sub(r"\s+\S+\s*/\s*.+$", "", (r.get("Name") or "").strip())   # 「… NVDA / NASDAQ」を外す
        name = re.sub(r"\(RS\)$", "", name).strip()
        if cls == "日本株":
            m = re.match(r"^([0-9][0-9A-Z]{3})", code)
            sec = m.group(1) if m else by_name.get(re.sub(r"(株式会社|\s|　)", "", name), "")
            if not sec:
                continue
            ticker, market = f"{sec}.T", "JP"
        else:
            ticker, market = code.upper(), "US"
            if not ticker or ticker == "-":
                continue
        qty = _num(r.get("Quantity")) or 0.0
        cost = _num(r.get("Total_Cost_JPY")) or 0.0
        price = _num(r.get("Current_Price"))
        if market == "US" and price and mv and qty:
            usd_jpy = usd_jpy or (mv / (price * qty))
        s = stocks.setdefault(ticker, {"ticker": ticker, "name": name, "market": market, "quantity": 0.0,
                                       "cost": 0.0, "current_price": price, "market_value_jpy": 0.0})
        s["quantity"] += qty
        s["cost"] += cost
        s["market_value_jpy"] += mv or 0.0
    out = []
    for t, s in stocks.items():
        o = old.get(t, {})
        if s["market"] == "JP":
            ind = industry.get(t[:-2], "") or o.get("sector_name", "")
            sc = _sector_code(ind) or o.get("sector_code", "")
        else:
            ind, sc = o.get("sector_name", "米国株"), o.get("sector_code", "US_OTHER")
        q = s["quantity"]
        out.append({
            "ticker": t, "name": s["name"], "sector_code": sc, "sector_name": ind, "market": s["market"],
            "quantity": round(q, 3) if q % 1 else int(q),
            "avg_cost_jpy": round(s["cost"] / q, 2) if q else 0,
            "current_price": s["current_price"] or 0,
            "purchase_date": o.get("purchase_date", ""), "memo": o.get("memo", ""),
            "market_value_jpy": round(s["market_value_jpy"]),
        })
    summary = {
        "as_of": as_of, "generated": date.today().isoformat(),
        "total_assets_jpy": round(total), "equity_jpy": round(sum(x["market_value_jpy"] for x in out)),
        "by_asset_class": {k: round(v) for k, v in sorted(classes.items(), key=lambda kv: -kv[1])},
        "usd_jpy": round(usd_jpy, 3) if usd_jpy else None,
        "source": "B-investment/data/保有銘柄.csv",
    }
    return out, summary


def _read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC_DEFAULT))
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    src = Path(a.src)
    if not src.exists():
        print(f"⚠️ 保有銘柄CSVが無い: {src}")
        return 1
    old = {}
    if OUT_CSV.exists():
        old = {r["ticker"]: r for r in _read(OUT_CSV)}
    rows, summary = convert(_read(src), old)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    OUT_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✅ 保有銘柄 {len(rows)}銘柄 / 株式 {summary['equity_jpy']:,}円 / 総資産 {summary['total_assets_jpy']:,}円"
          f"（{summary['as_of']}時点）")
    if a.commit:
        rel = [str(OUT_CSV.relative_to(REPO)), str(OUT_SUMMARY.relative_to(REPO))]
        if subprocess.run(["git", "diff", "--quiet", "--", *rel], cwd=REPO).returncode == 0:
            print("  変化なし（コミットしない）")
            return 0
        subprocess.run(["git", "add", *rel], cwd=REPO, check=True)
        subprocess.run(["git", "commit", "-m", f"auto: 保有銘柄の同期（{summary['as_of']}時点）", "--", *rel],
                       cwd=REPO, check=True)
        for _ in range(3):
            if subprocess.run(["git", "pull", "--rebase", "--autostash", "-q"], cwd=REPO).returncode == 0 and \
               subprocess.run(["git", "push", "-q"], cwd=REPO).returncode == 0:
                print("  ✅ push 済み")
                return 0
        print("  ⚠️ push 失敗（次回再試行）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
