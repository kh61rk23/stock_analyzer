# ==============================================================
# sync_holdings.py — PC の保有銘柄CSV（投資部）→ リポジトリの data/portfolio_holdings.csv
#
# 【なぜ】週次の「ポートフォリオへの影響」「ポジションサイジング」は data/portfolio_holdings.csv を読む。
#   PC の正本CSVと同期しないと売却済み銘柄が残り・新規保有が欠け、評価額が狂う。
# 【入力】Vault2/CEO室/departments/B-investment/data/保有銘柄.csv（手で更新する正本・SBI等の明細）
# 【出力】data/portfolio_holdings.csv … 日本株・米国株のみ（口座違いは合算）。既存の列互換＋market_value_jpy
#         data/portfolio_summary.json … 総資産（現金・投信・DC含む）と資産クラス別の内訳・USDJPY
# 【取得単価】日本株は 取引履歴.csv から口座別の移動平均で計算し直す。
#   保有銘柄.csv（Excel由来）の取得原価は、同じ銘柄の売却益を差し引いた「実質原価」になっている銘柄がある
#   （例: 三菱UFJ 実際の買付 1,289.5円・1,550.5円＝平均1,420円 → CSVは特定口座の売却益74,700円を引いた1,046.5円）。
#   実質原価のままだと出口戦略（恩株化の+100%判定）が実際より高い含み益で発火する。
#   履歴で数量が一致しない銘柄（持株会・新規上場など）は保有銘柄.csvの値のまま。
#   Excel側の値は excel_avg_cost_jpy 列に残す。
# 【実行】python3 sync_holdings.py [--src <csv>] [--commit]
#   --commit: 変化があれば2ファイルだけ git commit & push（obsidian_sync の Mac 側定期実行から呼ぶ）
#   --root  : 出力先と会社マスタの読み先をリポジトリ直下の代わりにこのフォルダにする
#             （STATE_STORE_MODE=bq の Mac は ~/.stock_analyzer_sync/state_root に書き、BigQuery へ保存する）
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
TX_NAME = "取引履歴.csv"   # 保有銘柄.csv と同じフォルダ
OUT_CSV = REPO / "data" / "portfolio_holdings.csv"
OUT_SUMMARY = REPO / "data" / "portfolio_summary.json"
FIELDS = ["ticker", "name", "sector_code", "sector_name", "market", "quantity", "avg_cost_jpy",
          "current_price", "purchase_date", "memo", "market_value_jpy", "excel_avg_cost_jpy",
          "account_type"]


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


def cost_from_trades(tx: list[dict]) -> dict:
    """取引履歴 → {(銘柄名, 口座): (数量, 取得原価円)}（口座別の移動平均・純関数）。

    売却は平均単価で原価を減らす（売却益は原価に混ぜない）。信用取引は対象外。
    金額が入っていない行（米国株のドル建て・持株会など）は数量だけ動かし、原価は不明（None）にする。
    """
    pos: dict = {}
    for r in sorted(tx, key=lambda r: r.get("Date") or ""):
        if r.get("Asset_Class") not in ("日本株", "米国株"):
            continue
        act = r.get("Action") or ""
        if "信用" in act:
            continue
        q, tot = _num(r.get("Quantity")), _num(r.get("Total_JPY"))
        if q is None:
            continue
        k = ((r.get("Name") or "").strip(), (r.get("Account") or "").strip())
        qty, cost = pos.get(k, (0.0, 0.0))
        if "買" in act:
            qty, cost = qty + q, (cost + tot) if (cost is not None and tot) else None
        elif "売" in act and qty > 0:
            take = min(q, qty)
            cost = cost * (qty - take) / qty if cost is not None else None
            qty -= take
        pos[k] = (qty, cost)
    return pos


def convert(rows: list[dict], old: dict | None = None, trades: dict | None = None) -> tuple[list[dict], dict]:
    """保有銘柄.csv の行 → (portfolio_holdings 行, 資産サマリ)。純関数（old＝既存CSVの ticker→行）。

    trades: cost_from_trades() の結果。日本株で数量が一致すれば取得原価をこちらで置き換える。
    """
    old = old or {}
    trades = trades or {}
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
        cost = excel_cost = _num(r.get("Total_Cost_JPY")) or 0.0
        if market == "JP":
            tq, tc = trades.get(((r.get("Name") or "").strip(), (r.get("Account") or "").strip()), (None, None))
            if tq is not None and tc and abs(tq - qty) < 1e-6:
                cost = tc
        price = _num(r.get("Current_Price"))
        if market == "US" and price and mv and qty:
            usd_jpy = usd_jpy or (mv / (price * qty))
        s = stocks.setdefault(ticker, {"ticker": ticker, "name": name, "market": market, "quantity": 0.0,
                                       "cost": 0.0, "excel_cost": 0.0, "current_price": price,
                                       "market_value_jpy": 0.0})
        s["quantity"] += qty
        s["cost"] += cost
        s["excel_cost"] += excel_cost
        s.setdefault("accounts", set()).add((r.get("Account") or "").strip())
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
            "excel_avg_cost_jpy": round(s["excel_cost"] / q, 2) if q else 0,
            # 口座（NISA・特定・持株会）。複数口座なら「/」区切り（tax_optimizer の損出し判定用）
            "account_type": "/".join(sorted(a for a in s.get("accounts", ()) if a)),
        })
    summary = {
        "as_of": as_of, "generated": date.today().isoformat(),
        "total_assets_jpy": round(total), "equity_jpy": round(sum(x["market_value_jpy"] for x in out)),
        "by_asset_class": {k: round(v) for k, v in sorted(classes.items(), key=lambda kv: -kv[1])},
        "usd_jpy": round(usd_jpy, 3) if usd_jpy else None,
        "source": "B-investment/data/保有銘柄.csv",
    }
    return out, summary


def keep_generated(old: dict | None, new: dict) -> dict:
    """中身（generated 以外）が前回と同じなら generated を前回の値に戻す（純関数）。

    generated は作った日なので毎日変わり、保有が同じでも毎日 auto コミットが入って
    「同期できている」ように見えてしまう。中身が変わった日だけ差分が出るようにする。"""
    if not old:
        return new
    strip = lambda d: {k: v for k, v in d.items() if k != "generated"}
    if strip(old) == strip(new) and old.get("generated"):
        return {**new, "generated": old["generated"]}
    return new


def _read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC_DEFAULT))
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--root", default="")
    a = ap.parse_args(argv)
    if a.root:
        global REPO, OUT_CSV, OUT_SUMMARY
        REPO = Path(a.root).expanduser().resolve()
        OUT_CSV, OUT_SUMMARY = REPO / "data" / OUT_CSV.name, REPO / "data" / OUT_SUMMARY.name
        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    src = Path(a.src)
    if not src.exists():
        print(f"⚠️ 保有銘柄CSVが無い: {src}")
        return 1
    old = {}
    if OUT_CSV.exists():
        old = {r["ticker"]: r for r in _read(OUT_CSV)}
    trades = {}
    if (src.parent / TX_NAME).exists():
        trades = cost_from_trades(_read(src.parent / TX_NAME))
    rows, summary = convert(_read(src), old, trades)
    for r in rows:
        if r["excel_avg_cost_jpy"] and abs(r["avg_cost_jpy"] - r["excel_avg_cost_jpy"]) > 0.5:
            print(f"  ℹ️ 取得単価を取引履歴で補正: {r['ticker']} {r['name']} "
                  f"{r['excel_avg_cost_jpy']:,} → {r['avg_cost_jpy']:,}円")
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    try:
        summary = keep_generated(json.loads(OUT_SUMMARY.read_text(encoding="utf-8")), summary)
    except (OSError, ValueError):
        pass
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


def holding_value_jpy(h: dict, price_local, usdjpy: float) -> float:
    """保有1銘柄の円評価額（純関数）。

    株価（現地通貨）があれば 数量×株価×（米国株なら為替）。無ければ保有銘柄CSVの
    円評価額（market_value_jpy）、それも無ければ 数量×取得単価（円）。
    円の取得単価に為替を掛けて約150倍になる事故（シナリオ分析・ストレステスト）を防ぐ。
    """
    qty = float(h.get("quantity") or 0)
    if price_local:
        return qty * float(price_local) * (usdjpy if h.get("market") == "US" else 1.0)
    if h.get("market_value_jpy"):
        return float(h["market_value_jpy"])
    return qty * float(h.get("avg_cost_jpy") or 0)


def usd_jpy(cfg=None) -> float:
    """ドル円（portfolio_summary.json → 為替レート取得 → 150 の順）。"""
    try:
        v = json.loads(OUT_SUMMARY.read_text(encoding="utf-8")).get("usd_jpy")
        if v:
            return float(v)
    except (OSError, ValueError, TypeError):
        pass
    try:
        from data_reliability import fetch_price_safe
        v = fetch_price_safe(cfg, "JPY=X").get("close")
        if v:
            return float(v)
    except Exception:
        pass
    return 150.0


def price_in_jpy(h: dict, price_local, fx: float):
    """株価（現地通貨）→ 円。米国株だけ為替を掛ける（純関数）。None はそのまま。"""
    if price_local is None:
        return None
    is_us = (h.get("market") == "US") or not str(h.get("ticker") or "").endswith(".T")
    return float(price_local) * (fx if is_us else 1.0)
