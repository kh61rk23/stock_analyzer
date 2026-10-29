# ==============================================================
# positioning_signals.py — 大量保有・13D/13G・8-K の項目・空売り残を、日次メールと審議パケットへ
# ==============================================================
# 【入力】（どれも溜める側は別モジュール。ここは読むだけ）
#   bi.m_large_holdings（日本の大量保有・変更報告の1保有者1行）
#   us.sec_13dg（米国の 13D/13G）・us.sec_filings（8-K の項目）・us.company_profile（CIK → ティッカー）
#   analytics.short_interest（FINRA の空売り残・半月ごと）
# 【読み方】1回の実行で fetch() を1度だけ呼び、日本1本・米国1本・空売り残1本のクエリでまとめて読む。
#   銘柄ごとには引かない（packet_lines は fetch の結果から引く）。
# 【出力】build_alerts(cfg, results) → メールの節（日本の買い増し・米国の 13D と重要な 8-K・空売り残）。
#   packet_lines(ticker) → 審議パケットの節「⑭ 大株主・需給の動き」。点数には入れない（重みは健太が決める）。
# ==============================================================
from __future__ import annotations

from datetime import date, timedelta

LOOKBACK_DAYS = 14
MIN_UP = 0.01             # 1ポイント以上の買い増し（新規の報告は 5% 以上なので必ず入る）
# 日本の大量保有で「経営に関わる」買い手とみなす保有目的の言い回し
ACTIVE_PURPOSE = r"重要提案|経営参加|経営への関与|支配|子会社化|非公開化|資本業務提携|業務提携|提案"
# 創業者・役員の資産管理会社など、売り買いの意図が薄い保有（経営に関わる言い回しが無ければ外す）
STABLE_PURPOSE = r"資産管理|安定株主|安定的に保有|長期的に保有|長期保有"
# 8-K の項目のうち、株価に効きやすいもの（よく出る 2.02 決算・5.02 役員・7.01/8.01/9.01 は除く）
K8_ITEMS = {
    "1.01": "重要な契約", "1.02": "重要な契約の終了", "1.03": "破産・更生", "2.01": "買収・売却の完了",
    "2.05": "撤退・リストラ費用", "2.06": "減損", "3.01": "上場廃止の通知", "4.01": "監査人の交代",
    "4.02": "過去の決算は信頼できない", "5.01": "支配権の変更",
}

JP_SQL = """
SELECT submit_date, issuer_sec_code, issuer_name, holder_name, holder_kind, prior_ratio, holding_ratio,
       LEFT(IFNULL(purpose, ''), 60) purpose
FROM `{p}.bi.m_large_holdings`
WHERE submit_date >= @since AND holding_ratio IS NOT NULL
  AND holding_ratio - IFNULL(prior_ratio, 0) >= @min_up
QUALIFY ROW_NUMBER() OVER (PARTITION BY issuer_sec_code, holder_name ORDER BY submit_date DESC) = 1
"""

US_SQL = """
WITH t AS (SELECT cik, ANY_VALUE(ticker) ticker, ANY_VALUE(UPPER(REGEXP_REPLACE(name, r'[^A-Za-z0-9]', ''))) nm
           FROM `{p}.us.company_profile` WHERE ticker IS NOT NULL GROUP BY cik),
d AS (   -- 発行会社の CIK が空の行（13D の XML のタグ名の違いで取り損ねた分）は会社名で引く
  SELECT 'dg' kind, s.filed_date, t.ticker, s.form, s.person_name AS who, s.percent, CAST(NULL AS STRING) items
  FROM `{p}.us.sec_13dg` s
  JOIN t ON t.cik = s.issuer_cik
         OR (s.issuer_cik IS NULL AND t.nm = UPPER(REGEXP_REPLACE(s.issuer_name, r'[^A-Za-z0-9]', '')))
  WHERE s.filed_date >= @since AND STARTS_WITH(s.form, 'SCHEDULE 13D')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY s.accession, s.person_name ORDER BY t.ticker) = 1
), k AS (
  SELECT 'k8' kind, f.filed_date, t.ticker, f.form, CAST(NULL AS STRING) who, CAST(NULL AS FLOAT64) percent, f.items
  FROM `{p}.us.sec_filings` f JOIN t ON t.cik = f.cik
  WHERE f.filed_date >= @since AND f.form IN ('8-K', '8-K/A') AND f.items IS NOT NULL
)
SELECT * FROM d UNION ALL SELECT * FROM k
"""

SI_SQL = """
SELECT symbol, settlement_date, short_qty, change_pct, days_to_cover
FROM `{p}.analytics.short_interest`
WHERE settlement_date = (SELECT MAX(settlement_date) FROM `{p}.analytics.short_interest`)
  AND symbol IN UNNEST(@symbols)
"""

_CACHE: dict | None = None


def _jp_code(ticker: str) -> str:
    return str(ticker or "").split(".")[0] if str(ticker or "").endswith(".T") else ""


def k8_flags(items: str | None) -> list[str]:
    """8-K の items（"1.01,2.03,9.01" など）→ 効きやすい項目の名前（純関数）。"""
    out = []
    for raw in str(items or "").replace(";", ",").split(","):
        code = raw.strip()
        if code in K8_ITEMS and K8_ITEMS[code] not in out:
            out.append(K8_ITEMS[code])
    return out


def group(jp_rows: list[dict], us_rows: list[dict], si_rows: list[dict]) -> dict:
    """クエリの行 → {"jp": {code: [..]}, "us": {ticker: {"dg": [..], "k8": [..]}}, "si": {ticker: {..}}}（純関数）。"""
    jp: dict = {}
    for r in jp_rows:
        if r.get("issuer_sec_code"):
            jp.setdefault(str(r["issuer_sec_code"])[:4], []).append(r)
    us: dict = {}
    for r in us_rows:
        t = r.get("ticker")
        if not t:
            continue
        slot = us.setdefault(t, {"dg": [], "k8": []})
        if r.get("kind") == "dg":
            slot["dg"].append(r)
        else:
            flags = k8_flags(r.get("items"))
            if flags:
                slot["k8"].append({**r, "flags": flags})
    si = {r["symbol"]: r for r in si_rows if r.get("symbol")}
    return {"jp": jp, "us": us, "si": si}


def fetch(client, us_symbols: list[str], today: date | None = None) -> dict:
    """3本のクエリでまとめて読み、group の形で返してキャッシュする。"""
    global _CACHE
    from google.cloud import bigquery
    since = (today or date.today()) - timedelta(days=LOOKBACK_DAYS)
    p = client.project
    sp = [bigquery.ScalarQueryParameter("since", "DATE", since)]
    jp = [dict(r.items()) for r in client.query(JP_SQL.format(p=p), location="asia-northeast1",
                                                    job_config=bigquery.QueryJobConfig(query_parameters=sp + [
                                                        bigquery.ScalarQueryParameter("min_up", "FLOAT64", MIN_UP)])).result()]
    us = [dict(r.items()) for r in client.query(US_SQL.format(p=p), location="US",
                                                    job_config=bigquery.QueryJobConfig(query_parameters=sp)).result()]
    syms = sorted({s for s in us_symbols if s})
    si = [] if not syms else [dict(r.items()) for r in client.query(
        SI_SQL.format(p=p), location="asia-northeast1", job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("symbols", "STRING", syms)])).result()]
    _CACHE = group(jp, us, si)
    return _CACHE


def _client():
    from edinet_bq_client import EDINETBQClient
    return EDINETBQClient().client


_SI_ASKED: set = set()


def ensure(us_symbols: list[str]) -> dict:
    """まだ読んでいなければ1度だけ読む。読めなければ空（メールと審議パケットは節を出さない）。
    読んだ後に新しい米国の銘柄が来たら、その分の空売り残だけを1本で足す。"""
    global _CACHE
    want = {s for s in us_symbols if s}
    try:
        if _CACHE is None:
            fetch(_client(), sorted(want))
            _SI_ASKED.update(want)
        elif want - _SI_ASKED:
            from google.cloud import bigquery
            client, more = _client(), sorted(want - _SI_ASKED)
            rows = client.query(SI_SQL.format(p=client.project), location="asia-northeast1",
                                job_config=bigquery.QueryJobConfig(query_parameters=[
                                    bigquery.ArrayQueryParameter("symbols", "STRING", more)])).result()
            _CACHE["si"].update({r["symbol"]: dict(r.items()) for r in rows})
            _SI_ASKED.update(more)
    except Exception as e:
        print(f"  ⚠️ 大株主・需給の材料を読めない: {type(e).__name__}: {str(e)[:80]}")
        if _CACHE is None:
            _CACHE = {"jp": {}, "us": {}, "si": {}}
    return _CACHE


def _pct(x) -> str:
    return "新規" if x is None else f"{float(x) * 100:.1f}%"


def jp_lines(rows: list[dict], limit: int = 8) -> list[str]:
    import re
    act = [r for r in rows if re.search(ACTIVE_PURPOSE, r.get("purpose") or "")
           or (str(r.get("holder_kind") or "").startswith("法人") and not re.search(STABLE_PURPOSE, r.get("purpose") or ""))]
    act.sort(key=lambda r: -(float(r["holding_ratio"]) - float(r.get("prior_ratio") or 0)))
    return [f"{r['issuer_name']}（{str(r['issuer_sec_code'])[:4]}.T）: {r['holder_name']} "
            f"{_pct(r.get('prior_ratio'))} → {float(r['holding_ratio']) * 100:.1f}%（{r.get('purpose') or '目的の記載なし'}・{r['submit_date']}）"
            for r in act[:limit]]


def build_alerts(cfg, results: dict) -> list[dict]:
    """日次メールの節。米国はスクリーナー通過と保有の銘柄だけ見る。"""
    us_pass = [c.get("ticker") for c in ((results.get("us_screener") or {}).get("candidates") or []) if c.get("ticker")]
    data = ensure(us_pass)
    out = []
    jp_all = [r for rows in data["jp"].values() for r in rows]
    lines = jp_lines(jp_all)
    if lines:
        out.append({"trigger": "large_holding_buyers", "severity": "info",
                    "title": f"🏦 大量保有の買い増し（法人・経営に関わる目的・直近{LOOKBACK_DAYS}日）",
                    "body": "\n".join(lines)})
    us_lines = []
    for t in us_pass:
        ev = data["us"].get(t) or {}
        for d in ev.get("dg", [])[:2]:
            pct = "" if d.get("percent") is None else f" {float(d['percent']):.1f}%"
            us_lines.append(f"{t}: {d['form'].replace('SCHEDULE ', '')} {d.get('who') or ''}{pct}（{d['filed_date']}）")
        for k in ev.get("k8", [])[:2]:
            us_lines.append(f"{t}: 8-K {'・'.join(k['flags'])}（{k['filed_date']}）")
        si = data["si"].get(t)
        if si and ((si.get("days_to_cover") or 0) >= 5 or (si.get("change_pct") or 0) >= 30):
            us_lines.append(f"{t}: 空売り残 {int(si['short_qty']):,}株（前回比 {si.get('change_pct') or 0:+.0f}%・"
                            f"返済に {si.get('days_to_cover') or 0:.1f}日・{si['settlement_date']}）")
    if us_lines:
        out.append({"trigger": "us_positioning", "severity": "info",
                    "title": f"🦅 米国の通過銘柄の大株主・需給（13D・重要な 8-K・空売り残・直近{LOOKBACK_DAYS}日）",
                    "body": "\n".join(us_lines[:15])})
    return out


def packet_lines(ticker: str) -> list[str]:
    """審議パケットの節。ensure を先に呼んでおくこと（呼んでいなければ、その場で1度だけ読む）。"""
    data = ensure([ticker] if not _jp_code(ticker) else [])
    head = ["", f"## ⑭ 大株主・需給の動き（直近{LOOKBACK_DAYS}日）"]
    code = _jp_code(ticker)
    body = []
    if code:
        for r in data["jp"].get(code, [])[:6]:
            body.append(f"- 大量保有: {r['holder_name']}（{r.get('holder_kind') or '-'}）{_pct(r.get('prior_ratio'))} → "
                        f"{float(r['holding_ratio']) * 100:.1f}%・目的「{r.get('purpose') or '記載なし'}」（{r['submit_date']}）")
    else:
        ev = data["us"].get(ticker) or {}
        for d in ev.get("dg", [])[:4]:
            pct = "" if d.get("percent") is None else f" {float(d['percent']):.1f}%"
            body.append(f"- {d['form'].replace('SCHEDULE ', '')}: {d.get('who') or '-'}{pct}（{d['filed_date']}）")
        for k in ev.get("k8", [])[:4]:
            body.append(f"- 8-K: {'・'.join(k['flags'])}（{k['filed_date']}）")
        si = data["si"].get(ticker)
        if si:
            body.append(f"- 空売り残: {int(si['short_qty']):,}株（前回比 {si.get('change_pct') or 0:+.0f}%・"
                        f"返済に {si.get('days_to_cover') or 0:.1f}日・{si['settlement_date']}）")
    return head + (body or ["- 動きなし（大量保有・13D・重要な 8-K の提出なし）"])
