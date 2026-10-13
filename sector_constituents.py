# ==============================================================
# sector_constituents.py
# 東証33業種ごとの全構成銘柄（時価総額つき）と、時価総額で重みづけした業種の財務指標
# ==============================================================
# 【使い道】
#   base_agent の業種スコアの財務部分（ROE・ROA・営業利益率・アクルーアル）を、代表数社の単純平均ではなく
#   業種の全銘柄の時価総額加重平均で出す。業種スコアが、たまたま選ばれた数社の値で振れないようにするため。
#   yfinance で個社を取る代表銘柄（アナリスト評価・株価系・個社ボーナス用）も、業種内の時価総額の大きい順に選ぶ。
# 【データ】
#   銘柄と業種コード：edinet.company_master（stock_real_estate が整える。EdinetDB の業種名＋JPX 一覧で補完）
#   時価総額・財務比率：bi.m_screener（日次処理の最後に作るため、日次処理の中では前日分）。比率は小数（0.12＝12%）。
#   絶対値が1を超える比率（赤字の小型株などの極端な値）は、その指標から外す。
# 【費用】1プロセス1クエリ（キャッシュ）。失敗時は空を返し、呼び出し側は従来の代表銘柄の単純平均に戻る。
# ==============================================================
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_CACHE: dict | None = None

_SQL = """
SELECT m.sector_code, x.ticker, x.name, x.market_cap,
  IF(ABS(x.roe) <= 1, x.roe, NULL) AS roe,
  IF(ABS(x.roa) <= 1, x.roa, NULL) AS roa,
  IF(ABS(x.operating_margin) <= 1, x.operating_margin, NULL) AS op_margin,
  IF(ABS(x.accrual_ratio) <= 1, x.accrual_ratio, NULL) AS accrual_ratio
FROM `{project}.bi.m_screener` AS x
JOIN `{project}.edinet.company_master` AS m USING (edinet_code)
WHERE x.market_cap > 0 AND m.sector_code IS NOT NULL AND x.ticker IS NOT NULL
"""

METRICS = ("roe", "roa", "op_margin", "accrual_ratio")


def group_rows(rows) -> dict:
    """{業種コード: [行（時価総額の大きい順）]}（純関数）。"""
    out: dict = {}
    for r in rows or []:
        r = dict(r)
        try:
            r["market_cap"] = float(r.get("market_cap") or 0)
        except (TypeError, ValueError):
            continue
        if r["market_cap"] <= 0 or not r.get("sector_code"):
            continue
        out.setdefault(str(r["sector_code"]), []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r["market_cap"], reverse=True)
    return out


def weighted(rows: list, key: str) -> float | None:
    """時価総額加重平均。値の無い銘柄は分母からも外す（純関数）。"""
    num = den = 0.0
    for r in rows or []:
        v, w = r.get(key), r.get("market_cap") or 0
        if v is None or w <= 0:
            continue
        num += float(v) * w
        den += w
    return num / den if den > 0 else None


def fundamentals(rows: list) -> dict:
    """業種の時価総額加重の財務指標と、件数・時価総額合計（純関数）。"""
    out = {k: weighted(rows, k) for k in METRICS}
    out["n"] = len(rows or [])
    out["mcap_total"] = sum(r.get("market_cap") or 0 for r in rows or [])
    return out


def load(cfg=None, client=None) -> dict:
    """{業種コード: [行]}。1プロセス1クエリ。失敗時は空。"""
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            if client is None:
                from edinet_bq_client import EDINETBQClient
                client = EDINETBQClient().client
            _CACHE = group_rows(client.query(_SQL.format(project=client.project)).result())
            print(f"    🏭 業種の全構成銘柄: {sum(len(v) for v in _CACHE.values())}銘柄 / {len(_CACHE)}業種（1クエリ）")
        except Exception as e:
            print(f"    ⚠️  業種の全構成銘柄の取得失敗（代表銘柄の単純平均に戻る）: {str(e)[:100]}")
            _CACHE = {}
        return _CACHE


def top_tickers(sector_code: str, limit: int, cfg=None, client=None) -> list[str]:
    return [r["ticker"] for r in load(cfg, client).get(str(sector_code), [])[:max(0, int(limit))]]


def fundamentals_for(sector_code: str, cfg=None, client=None) -> dict | None:
    rows = load(cfg, client).get(str(sector_code))
    return fundamentals(rows) if rows else None
