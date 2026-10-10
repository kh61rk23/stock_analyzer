# ==============================================================
# us_company_facts.py
# 全上場の米国株（NYSE・Nasdaq 普通株）の財務・企業情報を SEC の無料 API から取り、
# BigQuery の us.company_profile / us.financials_annual へ書く。
# ==============================================================
# 【役割】
#   us.fundamentals（us_fundamental_rotator.py が書く四半期の混在表）が
#   カバーするのは custom_universe の約800社だけで、売上・資産・負債・純資産・株数・
#   社名・業種を持たない。BI の米国スクリーナー（bi/marts/m_screener_us.sql）が
#   全上場の ROE・PBR・売上を出せるよう、年次を正本とした別表を作る。
#
# 【取得元】（APIキー不要・User-Agent 必須・秒間10件以下）
#   companyfacts : https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
#   submissions  : https://data.sec.gov/submissions/CIK##########.json（社名・SIC・業種名・提出日）
#   daily-index  : https://www.sec.gov/Archives/edgar/daily-index/（その日 10-K/10-Q を出した会社）
#   ユニバース   : https://www.sec.gov/files/company_tickers_exchange.json（price_universe と同じ絞り込み）
#
# 【方式】
#   backfill : 未取得の会社を全件取る（初回だけ。時間予算内で打ち切り、次回は続きから）。
#   update   : 日次。daily-index で「新しい 10-K/10-Q を出した会社」だけ取り直す
#              + 未取得 + 古い順の少数（取りこぼしの安全網）。
#   書込は読み出し（list_rows・無料）→ メモリ上で統合 → load job（WRITE_TRUNCATE・無料）。
#   クエリを使わないので BigQuery の課金は発生しない。
#
# 【タグの揺れ】
#   同じ項目でも会社・年度で使う US-GAAP タグが違う（MSFT は Revenues が 2010 年で止まり、
#   NVDA は逆に RevenueFromContract… が 2022 年で止まる）。そのため「最初に値があるタグ」を
#   固定せず、年度（期末日）ごとに優先順の最初にあるタグを採る。
#
# 【年次の見つけ方】
#   フロー項目は form が 10-K 系で期間が 350〜380 日のもの。同じ期末の重複（翌年の
#   10-K に載る比較列・訂正）は提出日が最新のものを採る。
#   ストック項目（貸借）は期間が無く、同じ形で期末日をキーに採る。
#
# 【TTM】 直近の通期 + 当期累計 - 前年同期累計（10-Q の累計は 3/6/9 か月）。10-Q が通期より後に無ければ通期そのもの。
# ==============================================================

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sec_client

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/master.{ymd}.idx"

BQ_LOCATION = "US"  # us データセットの location（asia の表とは JOIN できない）
PROFILE_TABLE = "company_profile"
ANNUAL_TABLE = "financials_annual"

# SEC の公平利用は秒間10件まで。余裕を見て 0.2 秒間隔（5件/秒）にする。
REQUEST_INTERVAL_SEC = 0.2
CONSECUTIVE_FAIL_LIMIT = 10     # 連続でこれだけ失敗したら SEC 側の制限とみなして打ち切る
ANNUAL_MIN_DAYS, ANNUAL_MAX_DAYS = 350, 380
YTD_MIN_DAYS, YTD_MAX_DAYS = 80, 290
KEEP_YEARS = 15
STALE_DAYS = 75                 # update で、これより古い会社を安全網として取り直す
NOFACTS_RETRY_DAYS = 30         # 財務を持たない会社（信託等）の再確認間隔
ROTATE_PER_RUN = 40
NEW_PER_RUN = 400
CHECKPOINT_EVERY = 500
SHRINK_GUARD_RATIO = 0.5        # 書込後の行数が前回のこの割合を下回るなら上書きしない

# 優先順（上ほど優先）。年度ごとに最初に値があるタグを採る。
FLOW_TAGS = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "RevenuesNetOfInterestExpense",
    ],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": [
        "NetIncomeLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLoss",
    ],
    "operating_cf": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
    ],
    "buyback": ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
}
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"]
DPS_TAGS = ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"]
STOCK_TAGS = {
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity"],
    "equity_incl_nci": ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
    ],
    "debt_current": ["DebtCurrent"],
    "short_borrowings": ["ShortTermBorrowings"],
    "commercial_paper": ["CommercialPaper"],
    "ltd_current": ["LongTermDebtCurrent"],
    "ltd_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "ltd_notes": ["LongTermNotesPayable"],
    "ltd_total": ["LongTermDebt"],
}
TTM_CONCEPTS = ["revenue", "operating_income", "net_income", "operating_cf", "capex"]
ANNUAL_COLUMNS = [
    "revenue", "operating_income", "net_income", "eps_diluted", "total_assets", "total_liabilities",
    "equity", "equity_incl_nci", "cash", "debt_short", "debt_long", "operating_cf", "capex",
    "buyback", "dividends_paid", "dividends_per_share",
]


# ==============================================================
# 純粋関数（テスト対象）
# ==============================================================

def _days(start, end):
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


def _is_annual_form(form) -> bool:
    return str(form or "").startswith("10-K")


def _is_periodic_form(form) -> bool:
    f = str(form or "")
    return f.startswith("10-K") or f.startswith("10-Q")


def flow_by_end(entries, min_days=ANNUAL_MIN_DAYS, max_days=ANNUAL_MAX_DAYS, annual_only=True) -> dict:
    """期間のある値を {期末: {val, filed, start}} にする。同じ期末は提出日が最新を採る。"""
    best: dict[str, dict] = {}
    for e in entries or []:
        form_ok = _is_annual_form(e.get("form")) if annual_only else _is_periodic_form(e.get("form"))
        if not form_ok or e.get("val") is None or not e.get("start") or not e.get("end"):
            continue
        d = _days(e["start"], e["end"])
        if d is None or not (min_days <= d <= max_days):
            continue
        filed = e.get("filed") or ""
        cur = best.get(e["end"])
        if cur is None or filed > cur["filed"] or (filed == cur["filed"] and e["start"] > cur["start"]):
            best[e["end"]] = {"val": float(e["val"]), "filed": filed, "start": e["start"]}
    return best


def stock_by_end(entries, annual_only=True) -> dict:
    """期間の無い値（貸借・株数）を {期末: {val, filed}} にする。"""
    best: dict[str, dict] = {}
    for e in entries or []:
        form_ok = _is_annual_form(e.get("form")) if annual_only else _is_periodic_form(e.get("form"))
        if not form_ok or e.get("val") is None or not e.get("end") or e.get("start"):
            continue
        filed = e.get("filed") or ""
        cur = best.get(e["end"])
        if cur is None or filed > cur["filed"]:
            best[e["end"]] = {"val": float(e["val"]), "filed": filed}
    return best


def _tag_entries(facts: dict, tag: str, unit: str) -> list:
    node = ((facts.get("facts") or {}).get("us-gaap") or {}).get(tag) or {}
    return (node.get("units") or {}).get(unit) or []


def pick_by_priority(per_tag: list) -> dict:
    """[(tag, {end: rec})] を優先順に見て、期末ごとに最初に値があるタグの値を採る。"""
    out: dict[str, dict] = {}
    for tag, by_end in per_tag:
        for end, rec in by_end.items():
            if end not in out:
                out[end] = {"val": rec["val"], "filed": rec["filed"], "tag": tag}
    return out


def resolve_flow(facts: dict, tags: list, unit="USD", **kw) -> dict:
    return pick_by_priority([(t, flow_by_end(_tag_entries(facts, t, unit), **kw)) for t in tags])


def resolve_stock(facts: dict, tags: list, unit="USD", annual_only=True) -> dict:
    return pick_by_priority([(t, stock_by_end(_tag_entries(facts, t, unit), annual_only)) for t in tags])


def _sum_present(*vals):
    present = [v for v in vals if v is not None]
    return sum(present) if present else None


def stock_values_at(stocks: dict, end: str) -> dict:
    """期末 end の貸借を、派生（有利子負債の合算・負債の差額計算）込みで返す。"""
    def g(name):
        rec = stocks.get(name, {}).get(end)
        return rec["val"] if rec else None

    equity = g("equity")
    equity_all = g("equity_incl_nci")
    if equity is None:
        equity = equity_all
    assets = g("total_assets")
    liabilities = g("total_liabilities")
    if liabilities is None and assets is not None:
        base = equity_all if equity_all is not None else equity
        if base is not None:
            liabilities = assets - base
    debt_short = g("debt_current")
    if debt_short is None:
        debt_short = _sum_present(g("short_borrowings"), g("commercial_paper"), g("ltd_current"))
    debt_long = g("ltd_noncurrent")
    if debt_long is None:
        debt_long = g("ltd_notes")
    if debt_long is None and g("ltd_total") is not None:
        debt_long = g("ltd_total") - (g("ltd_current") or 0.0)
    return {
        "total_assets": assets, "total_liabilities": liabilities, "equity": equity,
        "equity_incl_nci": equity_all if equity_all is not None else equity,
        "cash": g("cash"), "debt_short": debt_short, "debt_long": debt_long,
    }


def extract_concepts(facts: dict) -> dict:
    """companyfacts から、項目ごとの {期末: 値} を作る（年次のフロー・貸借と最新貸借）。"""
    flows = {name: resolve_flow(facts, tags) for name, tags in FLOW_TAGS.items()}
    flows["eps_diluted"] = resolve_flow(facts, EPS_TAGS, unit="USD/shares")
    flows["dividends_per_share"] = resolve_flow(facts, DPS_TAGS, unit="USD/shares")
    stocks = {name: resolve_stock(facts, tags) for name, tags in STOCK_TAGS.items()}
    latest_stocks = {name: resolve_stock(facts, tags, annual_only=False) for name, tags in STOCK_TAGS.items()}
    return {"flows": flows, "stocks": stocks, "latest_stocks": latest_stocks}


def build_annual_rows(ticker: str, cik: int, facts: dict, now_iso: str, today: date | None = None) -> list:
    """年次行（ticker×period_end）。10-K 系の通期だけ。古すぎる年度は捨てる。"""
    today = today or date.today()
    c = extract_concepts(facts)
    flows, stocks = c["flows"], c["stocks"]
    ends = set()
    for name in ("revenue", "operating_income", "net_income", "operating_cf", "eps_diluted"):
        ends |= set(flows[name])
    # 売上も利益も無い会社でも、通期の資産があれば行を残す
    ends |= {e for e in stocks["total_assets"]}
    cutoff = (today - timedelta(days=366 * KEEP_YEARS)).isoformat()
    rows = []
    for end in sorted(e for e in ends if e >= cutoff):
        sv = stock_values_at(stocks, end)
        row = {
            "ticker": ticker, "cik": int(cik), "period_end": end,
            "fiscal_year": int(end[:4]),
            "revenue_tag": flows["revenue"].get(end, {}).get("tag"),
            "filed": max([flows[n][end]["filed"] for n in flows if end in flows[n]]
                         + [stocks[n][end]["filed"] for n in stocks if end in stocks[n]] or [""]) or None,
            "updated_at": now_iso,
        }
        for col in ANNUAL_COLUMNS:
            if col in sv:
                row[col] = sv[col]
            else:
                row[col] = flows[col][end]["val"] if end in flows.get(col, {}) else None
        rows.append(row)
    return rows


def latest_balance(facts_concepts: dict) -> dict:
    """10-K / 10-Q の中で最も新しい期末の貸借。総資産がある最新期末を基準にする。"""
    ls = facts_concepts["latest_stocks"]
    ends = sorted(ls["total_assets"])
    if not ends:
        ends = sorted(ls["equity"])
    if not ends:
        return {"bs_period_end": None}
    end = ends[-1]
    sv = stock_values_at(ls, end)
    return {"bs_period_end": end, "bs_total_assets": sv["total_assets"],
            "bs_total_liabilities": sv["total_liabilities"], "bs_equity": sv["equity"],
            "bs_cash": sv["cash"], "bs_debt_short": sv["debt_short"], "bs_debt_long": sv["debt_long"]}


def _ttm_one_tag(entries):
    """1タグの TTM。(通期期末, 通期値, TTM値, TTM期末) または None。"""
    fy = flow_by_end(entries)
    if not fy:
        return None
    fy_end = max(fy)
    all_ytd = flow_by_end(entries, YTD_MIN_DAYS, YTD_MAX_DAYS, annual_only=False)
    ytd = {e: r for e, r in all_ytd.items() if e > fy_end}
    if not ytd:
        return (fy_end, fy[fy_end]["val"], fy[fy_end]["val"], fy_end)
    cur_end = max(ytd)
    cur = ytd[cur_end]
    span = _days(cur["start"], cur_end)
    for pend, prec in all_ytd.items():
        gap = _days(pend, cur_end)
        pspan = _days(prec["start"], pend)
        if gap is not None and 350 <= gap <= 380 and pspan is not None and abs(pspan - span) <= 12:
            return (fy_end, fy[fy_end]["val"], fy[fy_end]["val"] + cur["val"] - prec["val"], cur_end)
    return None


def compute_ttm(facts: dict, annual_ends: dict | None = None) -> dict:
    """直近4四半期合計 = 直近通期 + 当期累計 - 前年同期累計。

    10-Q が通期より後に無い、または前年同期が見つからないときは、通期そのものを返す。
    項目ごとに、通期・当期累計・前年同期累計が揃うタグのうち最も新しい期末まで組めるものを使う（1項目内でタグを混ぜない）。
    """
    out = {"ttm_period_end": None}
    per = {}
    for name in TTM_CONCEPTS + ["eps_diluted"]:
        tags = FLOW_TAGS.get(name) or EPS_TAGS
        unit = "USD/shares" if name == "eps_diluted" else "USD"
        cands = []
        for pri, tag in enumerate(tags):
            c = _ttm_one_tag(_tag_entries(facts, tag, unit))
            if c:
                cands.append((c[3], -pri, (tag,) + c))
        # 止まった古いタグではなく、最も新しい期末まで組めるタグを採る（同じなら優先順）
        per[name] = max(cands)[2] if cands else None
    ends = [v[4] for v in per.values() if v]
    if not ends:
        return out
    # 期末がそろわない項目は混ぜない: 最も多い期末（同数なら新しい方）の項目だけ採る
    end = max(set(ends), key=lambda e: (ends.count(e), e))
    out["ttm_period_end"] = end
    for name, v in per.items():
        out[f"ttm_{name}"] = v[3] if v and v[4] == end else None
    return out


def pick_shares(facts: dict) -> dict:
    """発行済株式数。dei の表紙値と us-gaap の貸借日値のうち日付が新しい方。無ければ加重平均株数（印を付ける）。"""
    cands = []
    dei = ((facts.get("facts") or {}).get("dei") or {}).get("EntityCommonStockSharesOutstanding") or {}
    for e in (dei.get("units") or {}).get("shares") or []:
        if e.get("val") and e.get("end"):
            cands.append((e["end"], 1, e.get("filed") or "", float(e["val"]), "dei_cover"))
    for e in _tag_entries(facts, "CommonStockSharesOutstanding", "shares"):
        if e.get("val") and e.get("end") and not e.get("start") and _is_periodic_form(e.get("form")):
            cands.append((e["end"], 0, e.get("filed") or "", float(e["val"]), "us_gaap_balance"))
    if cands:
        end, _, _, val, src = max(cands)
        return {"shares_outstanding": val, "shares_as_of": end, "shares_source": src}
    wa = []
    for e in _tag_entries(facts, "WeightedAverageNumberOfDilutedSharesOutstanding", "shares"):
        if e.get("val") and e.get("end") and _is_periodic_form(e.get("form")):
            wa.append((e["end"], e.get("filed") or "", float(e["val"])))
    if wa:
        end, _, val = max(wa)
        return {"shares_outstanding": val, "shares_as_of": end, "shares_source": "weighted_avg_diluted"}
    return {"shares_outstanding": None, "shares_as_of": None, "shares_source": None}


def latest_periodic_filing(subs: dict) -> str | None:
    recent = ((subs or {}).get("filings") or {}).get("recent") or {}
    forms, dates = recent.get("form") or [], recent.get("filingDate") or []
    ds = [d for f, d in zip(forms, dates) if _is_periodic_form(f)]
    return max(ds) if ds else None


def build_profile(ticker: str, cik: int, exchange: str | None, facts: dict | None,
                  subs: dict | None, annual_rows: list, now_iso: str) -> dict:
    """企業情報 + 発行済株式数 + 最新貸借 + TTM の1行。facts が無ければ status=no_facts。"""
    subs = subs or {}
    name = subs.get("name") or (facts or {}).get("entityName")
    fye = subs.get("fiscalYearEnd")
    prof = {
        "ticker": ticker, "cik": int(cik), "name": name, "exchange": exchange,
        "sic": str(subs.get("sic")) if subs.get("sic") not in (None, "") else None,
        "sic_description": subs.get("sicDescription") or None,
        "fiscal_year_end": str(fye) if fye else None,
        "latest_filing_date": latest_periodic_filing(subs),
        "latest_annual_period_end": max((r["period_end"] for r in annual_rows), default=None),
        "fetched_at": now_iso,
    }
    if not facts:
        prof.update({"status": "no_facts"})
        return prof
    concepts = extract_concepts(facts)
    prof.update(pick_shares(facts))
    prof.update(latest_balance(concepts))
    prof.update(compute_ttm(facts))
    prof["status"] = "ok" if annual_rows else "no_annual_usd"
    return prof


def parse_daily_index(text: str, forms=("10-K", "10-K/A", "10-KT", "10-Q", "10-Q/A", "10-QT")) -> set:
    """master.YYYYMMDD.idx（CIK|会社名|提出種別|提出日|ファイル名）から、対象種別を出した CIK の集合。"""
    ciks = set()
    started = False
    for line in text.splitlines():
        if line.startswith("-----"):
            started = True
            continue
        if not started:
            continue
        parts = line.split("|")
        if len(parts) >= 5 and parts[2] in forms and parts[0].isdigit():
            ciks.add(int(parts[0]))
    return ciks


def index_days_to_fetch(last_done: str | None, today: date) -> list:
    """取りに行く日付（古い順）。last_done の翌日から昨日まで。初回は10日前から。"""
    start = (date.fromisoformat(last_done) + timedelta(days=1)) if last_done else today - timedelta(days=10)
    return [start + timedelta(days=i) for i in range((today - start).days) if start + timedelta(days=i) < today]


def advance_pointer(results: dict, today: date) -> str | None:
    """日付→取得結果('ok'/'missing')から、次回の起点（取得済みの最後の日）を決める。

    平日で missing の日は、直近3日以内なら未公開の可能性があるのでそこで止める（翌回に再試行）。
    それより古い欠落は祝日とみなして進める。週末の欠落は常に進める。
    """
    done = None
    for d in sorted(results):
        day = date.fromisoformat(d)
        if results[d] == "missing" and day.weekday() < 5 and (today - day).days <= 3:
            break
        done = d
    return done


def select_queue(universe: dict, profiles: dict, changed_ciks: set, today: date,
                 mode: str, new_cap: int = NEW_PER_RUN, rotate: int = ROTATE_PER_RUN) -> list:
    """取得する CIK の順序。universe は {ticker: {cik, ...}}、profiles は {ticker: 行}。

    先頭から: 新しい提出があった会社 → 未取得 → 古い順（update は少数、backfill は未取得だけで打ち切り）。
    """
    by_cik: dict[int, list] = {}
    for t, u in universe.items():
        by_cik.setdefault(int(u["cik"]), []).append(t)

    def last_fetch(cik):
        fs = [profiles[t].get("fetched_at") if t in profiles else None for t in by_cik[cik]]
        return None if any(f is None for f in fs) else min(fs)  # 新規上場の ticker が混じる CIK は未取得扱い

    def age_days(ts):
        try:
            return (today - datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()).days
        except ValueError:
            return 10**6

    def no_facts_recent(cik):
        return all(profiles.get(t, {}).get("status") == "no_facts" and age_days(profiles[t]["fetched_at"]) < NOFACTS_RETRY_DAYS
                   for t in by_cik[cik])

    queue, seen = [], set()

    def add(cik, reason):
        if cik in by_cik and cik not in seen:
            seen.add(cik)
            queue.append({"cik": cik, "tickers": sorted(by_cik[cik]), "reason": reason})

    for cik in sorted(changed_ciks):
        add(cik, "new_filing")
    never = sorted(c for c in by_cik if last_fetch(c) is None)
    for cik in never[: (None if mode == "backfill" else new_cap)]:
        add(cik, "never_fetched")
    if mode != "backfill":
        stale = sorted((last_fetch(c), c) for c in by_cik
                       if last_fetch(c) is not None and age_days(last_fetch(c)) >= STALE_DAYS and not no_facts_recent(c))
        for _, cik in stale[:rotate]:
            add(cik, "stale_rotation")
    return queue


# ==============================================================
# SEC 取得
# ==============================================================

def _load_cfg():
    try:
        import config as cfg
        return cfg
    except Exception as e:
        print(f"⚠️ config 読込失敗（既定値で継続）: {str(e)[:100]}")
        return None


def _state_dir(cfg) -> Path:
    base = Path(getattr(cfg, "DRIVE_BASE", "stock_data")) if cfg else Path("stock_data")
    d = base / "data" / "us_company_facts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def configure_sec_rate() -> None:
    """sec_client の送信間隔を本モジュールの値にする（既定 2件/秒では全件に時間がかかり過ぎる）。"""
    sec_client.RATE_LIMIT_RPS = 1.0 / REQUEST_INTERVAL_SEC


def load_universe(cfg) -> dict:
    """全上場の普通株 {ticker: {cik, name, exchange}}。取得失敗時は前回のキャッシュ。"""
    import price_universe as pu
    path = _state_dir(cfg) / "universe.json"
    payload = sec_client.get_json(COMPANY_TICKERS_URL, cfg=cfg, timeout=60)
    try:
        records = pu.parse_sec_tickers(payload)
        tickers, _ = pu.filter_us_universe(records)
        keep = set(tickers)
        uni = {}
        for r in records:
            t = str(r.get("ticker") or "").strip().upper()
            if t in keep and r.get("cik") is not None and t not in uni:
                uni[t] = {"cik": int(r["cik"]), "name": r.get("name"), "exchange": r.get("exchange")}
        if len(uni) < 1000:
            raise RuntimeError(f"ユニバースが{len(uni)}件しかない（SECの形式変更を疑う）")
    except Exception as e:
        cached = _read_json(path, {})
        if cached:
            print(f"⚠️ ユニバース取得失敗（前回のキャッシュを使う）: {str(e)[:100]}")
            return cached
        raise
    if uni != _read_json(path, {}):
        _write_json(path, uni)
    return uni


def fetch_daily_index(day: date, cfg) -> set | None:
    """その日の 10-K/10-Q 提出会社の CIK 集合。未公開・休場は None。"""
    q = (day.month - 1) // 3 + 1
    url = DAILY_INDEX_URL.format(y=day.year, q=q, ymd=day.strftime("%Y%m%d"))
    resp = sec_client.get(url, cfg=cfg, accept="text/plain", timeout=60, max_retries=2)
    if resp is None or resp.status_code != 200:
        return None
    return parse_daily_index(resp.text)


def fetch_facts_and_subs(cik: int, cfg):
    """(facts, subs, outcome)。outcome: ok / no_facts（404＝財務データなし）/ error。"""
    resp = sec_client.get(FACTS_URL.format(cik=cik), cfg=cfg, timeout=60)
    facts = None
    if resp is None:
        return None, None, "error"
    if resp.status_code == 404:
        outcome = "no_facts"
    elif resp.status_code == 200:
        try:
            facts = resp.json()
        except ValueError:
            return None, None, "error"
        outcome = "ok"
    else:
        return None, None, "error"
    subs = sec_client.submissions(cik, cfg=cfg)
    if subs is None:
        return None, None, "error"
    return facts, subs, outcome


# ==============================================================
# BigQuery
# ==============================================================

def _schemas():
    from google.cloud import bigquery as bq
    F = bq.SchemaField
    profile = [
        F("ticker", "STRING", "REQUIRED"), F("cik", "INT64"), F("name", "STRING"), F("exchange", "STRING"),
        F("sic", "STRING"), F("sic_description", "STRING"), F("fiscal_year_end", "STRING"),
        F("shares_outstanding", "FLOAT64"), F("shares_as_of", "DATE"), F("shares_source", "STRING"),
        F("latest_filing_date", "DATE"), F("latest_annual_period_end", "DATE"),
        F("bs_period_end", "DATE"), F("bs_total_assets", "FLOAT64"), F("bs_total_liabilities", "FLOAT64"),
        F("bs_equity", "FLOAT64"), F("bs_cash", "FLOAT64"), F("bs_debt_short", "FLOAT64"), F("bs_debt_long", "FLOAT64"),
        F("ttm_period_end", "DATE"), F("ttm_revenue", "FLOAT64"), F("ttm_operating_income", "FLOAT64"),
        F("ttm_net_income", "FLOAT64"), F("ttm_eps_diluted", "FLOAT64"), F("ttm_operating_cf", "FLOAT64"),
        F("ttm_capex", "FLOAT64"), F("status", "STRING"), F("fetched_at", "TIMESTAMP"),
    ]
    annual = [
        F("ticker", "STRING", "REQUIRED"), F("cik", "INT64"), F("period_end", "DATE", "REQUIRED"),
        F("fiscal_year", "INT64"),
    ] + [F(c, "FLOAT64") for c in ANNUAL_COLUMNS] + [
        F("revenue_tag", "STRING"), F("filed", "DATE"), F("updated_at", "TIMESTAMP"),
    ]
    return profile, annual


def bq_client(cfg=None):
    from google.cloud import bigquery
    from google.oauth2 import service_account
    raw = os.environ.get("BQ_CREDENTIALS", "")
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        project = os.environ.get("BQ_PROJECT_ID") or info.get("project_id")
        return bigquery.Client(project=project, credentials=creds, location=BQ_LOCATION)
    path = str(getattr(cfg, "BQ_CREDENTIALS_PATH", "") or "")
    if path and Path(path).exists():
        creds = service_account.Credentials.from_service_account_file(path, scopes=scopes)
        project = os.environ.get("BQ_PROJECT_ID") or getattr(cfg, "BQ_PROJECT_ID", "") or creds.project_id
        return bigquery.Client(project=project, credentials=creds, location=BQ_LOCATION)
    raise RuntimeError("BQ認証情報がありません（BQ_CREDENTIALS か BQ_CREDENTIALS_PATH）")


def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if isinstance(v, (date, datetime)) else v
    return out


def read_table(client, table: str) -> list:
    from google.api_core.exceptions import NotFound
    ref = f"{client.project}.us.{table}"
    try:
        return [_jsonable(dict(r.items())) for r in client.list_rows(ref)]
    except NotFound:
        return []


def merge_rows(old_profiles: list, old_annual: list, new_profiles: list, new_annual: list) -> tuple:
    """取り直した ticker の行は丸ごと入れ替える（訂正で消えた年度を残さない）。"""
    fetched = {p["ticker"] for p in new_profiles}
    profiles = {p["ticker"]: p for p in old_profiles if p["ticker"] not in fetched}
    for p in new_profiles:
        profiles[p["ticker"]] = p
    annual = [r for r in old_annual if r["ticker"] not in fetched] + list(new_annual)
    return sorted(profiles.values(), key=lambda p: p["ticker"]), sorted(annual, key=lambda r: (r["ticker"], r["period_end"]))


def write_table(client, table: str, schema: list, rows: list, previous_count: int) -> None:
    from google.cloud import bigquery
    if previous_count and len(rows) < previous_count * SHRINK_GUARD_RATIO:
        raise RuntimeError(f"{table}: 行数が {previous_count} → {len(rows)} に急減したため上書きしない")
    cfg = bigquery.LoadJobConfig(schema=schema, write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    job = client.load_table_from_json(rows, f"{client.project}.us.{table}", job_config=cfg, location=BQ_LOCATION)
    job.result()


def coverage(profiles: list, annual: list, universe_n: int) -> dict:
    ann_t = {r["ticker"] for r in annual}
    with_rev = {r["ticker"] for r in annual if r.get("revenue") is not None}
    with_assets = {r["ticker"] for r in annual if r.get("total_assets") is not None}
    return {
        "universe": universe_n,
        "profile_rows": len(profiles),
        "with_name": sum(1 for p in profiles if p.get("name")),
        "with_sic": sum(1 for p in profiles if p.get("sic_description")),
        "with_shares": sum(1 for p in profiles if p.get("shares_outstanding")),
        "annual_rows": len(annual),
        "tickers_with_annual": len(ann_t),
        "tickers_with_revenue": len(with_rev),
        "tickers_with_assets": len(with_assets),
        "no_facts": sum(1 for p in profiles if p.get("status") == "no_facts"),
    }


# ==============================================================
# 実行
# ==============================================================

def run(mode: str, cfg, client, budget_minutes: float, limit: int = 0, dry_run: bool = False) -> dict:
    t0 = time.monotonic()
    today = datetime.now(timezone.utc).date()
    configure_sec_rate()
    universe = load_universe(cfg)
    state_path = _state_dir(cfg) / "state.json"
    state = _read_json(state_path, {})

    old_profiles = read_table(client, PROFILE_TABLE) if client else []
    old_annual = read_table(client, ANNUAL_TABLE) if client else []
    profiles = {p["ticker"]: p for p in old_profiles}

    changed: set = set()
    pointer = state.get("last_index_date")
    if mode == "update":
        results = {}
        for day in index_days_to_fetch(pointer, today):
            ciks = fetch_daily_index(day, cfg)
            results[day.isoformat()] = "ok" if ciks is not None else "missing"
            changed |= ciks or set()
        pointer = advance_pointer(results, today) or pointer
        print(f"📑 daily-index {len(results)}日分 → 新しい提出のあった会社 {len(changed)} 社")

    queue = select_queue(universe, profiles, changed, today, mode)
    if limit:
        queue = queue[:limit]
    print(f"📋 取得キュー: {len(queue)}社（mode={mode}）")

    new_profiles: list = []
    new_annual: list = []
    stats = {"ok": 0, "no_facts": 0, "error": 0, "attempted": 0}
    consecutive_fail = 0
    aborted = ""
    saved = {"profiles": old_profiles, "annual": old_annual}

    def checkpoint(final=False):
        if dry_run or not client or not new_profiles:
            return
        merged_p, merged_a = merge_rows(saved["profiles"], saved["annual"], new_profiles, new_annual)
        sp, sa = _schemas()
        write_table(client, PROFILE_TABLE, sp, merged_p, len(saved["profiles"]))
        write_table(client, ANNUAL_TABLE, sa, merged_a, len(saved["annual"]))
        saved["profiles"], saved["annual"] = merged_p, merged_a
        new_profiles.clear()
        new_annual.clear()
        print(f"💾 BQ書込: profile {len(merged_p)} 行 / annual {len(merged_a)} 行" + ("（最終）" if final else ""))

    for i, item in enumerate(queue, 1):
        if (time.monotonic() - t0) / 60 > budget_minutes:
            aborted = f"時間予算 {budget_minutes:g} 分で打ち切り（残り {len(queue) - i + 1} 社は次回）"
            break
        cik = item["cik"]
        stats["attempted"] += 1
        try:
            facts, subs, outcome = fetch_facts_and_subs(cik, cfg)
        except Exception as e:
            facts, subs, outcome = None, None, "error"
            print(f"  ⚠️ CIK {cik}: {type(e).__name__}: {str(e)[:80]}")
        if outcome == "error":
            stats["error"] += 1
            consecutive_fail += 1
            if consecutive_fail >= CONSECUTIVE_FAIL_LIMIT:
                aborted = f"連続{consecutive_fail}件失敗（SEC側の制限の疑い）で打ち切り"
                break
            continue
        consecutive_fail = 0
        stats[outcome] += 1
        now_iso = datetime.now(timezone.utc).isoformat()
        for t in item["tickers"]:
            u = universe[t]
            arows = build_annual_rows(t, cik, facts, now_iso, today) if facts else []
            new_annual.extend(arows)
            new_profiles.append(build_profile(t, cik, u.get("exchange"), facts, subs, arows, now_iso))
        if i % 100 == 0:
            print(f"  [{i}/{len(queue)}] ok={stats['ok']} no_facts={stats['no_facts']} error={stats['error']}")
        if len(new_profiles) >= CHECKPOINT_EVERY:
            checkpoint()

    checkpoint(final=True)
    if not dry_run and mode == "update" and pointer and pointer != state.get("last_index_date"):
        state["last_index_date"] = pointer
        _write_json(state_path, state)
    cov = coverage(saved["profiles"], saved["annual"], len(universe))
    return {"mode": mode, "queue": len(queue), **stats, "aborted": aborted, "coverage": cov,
            "elapsed_sec": int(time.monotonic() - t0)}


def _notify(cfg, subject: str, body: str) -> None:
    try:
        frm = os.environ.get("ALERT_GMAIL_FROM", "")
        pwd = os.environ.get("ALERT_GMAIL_PASSWORD", "")
        to = os.environ.get("ALERT_GMAIL_TO", "")
        if not (frm and pwd and to):
            return
        from alert_notifier import send_gmail
        ok, err = send_gmail(frm, pwd, to, subject, f"<pre>{body}</pre>")
        print("📧 アラート送信" if ok else f"⚠️ アラート送信失敗: {err}")
    except Exception as e:
        print(f"⚠️ アラート送信失敗: {str(e)[:80]}")


def judge(info: dict) -> tuple:
    """(status, note)。対象があるのに1社も取れなかった・打ち切りは success にしない。"""
    if info["attempted"] and not (info["ok"] + info["no_facts"]):
        return "error", f"{info['attempted']}社試して全滅（error={info['error']}）"
    if info["aborted"] and info["error"]:
        return "warn", info["aborted"]
    note = f"ok={info['ok']} no_facts={info['no_facts']} error={info['error']}"
    if info["aborted"]:
        note += f" / {info['aborted']}"
    return "success", note


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["backfill", "update"], default="update")
    ap.add_argument("--budget-minutes", type=float, default=0, help="0 なら mode の既定（backfill 100 / update 25）")
    ap.add_argument("--limit", type=int, default=0, help="取得社数の上限（試験用）")
    ap.add_argument("--dry-run", action="store_true", help="BQに書かない・状態も更新しない")
    args = ap.parse_args(argv)
    cfg = _load_cfg()
    budget = args.budget_minutes or (100 if args.mode == "backfill" else 25)
    client = None
    try:
        client = bq_client(cfg)
    except Exception:
        if not args.dry_run:
            raise
    name = f"us_company_facts_{args.mode}"
    try:
        info = run(args.mode, cfg, client, budget, args.limit, args.dry_run)
        status, note = judge(info)
    except Exception as e:
        info, status, note = {}, "error", f"{type(e).__name__}: {str(e)[:150]}"
    print(f"📒 {name}: {status} {note}")
    if info:
        print(json.dumps(info, ensure_ascii=False, default=str))
    if not args.dry_run and not args.limit:
        try:
            from module_ledger import record_external
            record_external(cfg, name, status, note)
        except Exception as e:
            print(f"⚠️ 台帳への記録に失敗: {str(e)[:80]}")
    if status == "error":
        _notify(cfg, "🚨 米国株の財務取得に問題", f"{name}: {note}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
