# ==============================================================
# price_history_writer.py  v1.0
# yf_cache が集めた日足を BQ analytics.price_daily へ永続化
# ==============================================================
# 【役割】
#   実行中に yfinance から取得した日足OHLCVを、実行の最後にまとめて
#   BigQuery へ書き出す。取得のためだけの追加アクセスは行わない。
#
# 【なぜ必要か】
#   2026-09-09 時点で株価はどこにも保存されていなかった。50モジュールが
#   個別に yfinance を叩いては捨てており、BQに時系列テーブルが無いため
#   「RSIが30を割った翌月のリターン」のような事後検証ができなかった。
#
# 【重複の扱い】
#   同じ (ticker, trade_date) は毎日重複しうる。全期間を毎回入れると
#   肥大するため、直近 PRICE_WRITE_LOOKBACK_DAYS 日だけを対象にする。
#   BQ側の重複は分析時に QUALIFY ROW_NUMBER() で最新を採る前提
#   （ストリーミングINSERT直後の行はMERGEできないため、MERGEは使わない）。
#
# 【依存】yf_cache / edinet_bq_client / config.py
# ==============================================================

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B5_PRICE_HISTORY_WRITER_LOOKBACK_DAYS': 'LOOKBACK_DAYS',
    'B5_PRICE_HISTORY_WRITER_MAX_ROWS': 'MAX_ROWS',
}

# 直近何日分をBQへ書くか。毎日実行するので数日あれば欠損しない。
LOOKBACK_DAYS = 7
# 1回のINSERT上限。事故時の暴発を防ぐ。
MAX_ROWS = 20000

_TABLE = "analytics.price_daily"

# yfinance の列名 → BQ列名
_COLS = {"Open": "open", "High": "high", "Low": "low",
         "Close": "close", "Volume": "volume"}


def _f(value):
    """数値化。NaN/Inf は BQ が受け付けないため None にする。"""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def build_price_rows(
    prices: dict,
    since: date,
    now: str | None = None,
    max_rows: int = MAX_ROWS,
) -> list[dict]:
    """{ticker: DataFrame} から BQ 行を組み立てる（純粋関数）。

    DataFrame は index が日付、列に Open/High/Low/Close/Volume を持つ
    yfinance の標準形を想定する。列が欠けていても取れる分だけ入れる。
    """
    now = now or datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    seen: set = set()

    for ticker, df in (prices or {}).items():
        if df is None or getattr(df, "empty", True):
            continue
        cols = {c: _COLS[c] for c in getattr(df, "columns", []) if c in _COLS}
        if not cols:
            continue
        try:
            records = df.to_dict("index")
        except Exception:
            continue

        for idx, row in records.items():
            d = getattr(idx, "date", None)
            d = d() if callable(d) else idx
            if not isinstance(d, date):
                continue
            if d < since:
                continue
            key = (ticker, d)
            if key in seen:
                continue
            seen.add(key)

            # 欠けている列も None で必ず埋める（BQスキーマと列を揃え、
            # 呼び出し側が KeyError を気にせず参照できるようにする）
            values = {bq: None for bq in _COLS.values()}
            values.update({bq: _f(row.get(src)) for src, bq in cols.items()})
            # 終値すら無い行は保存しても意味がない
            if values.get("close") is None:
                continue
            rows.append({
                "trade_date": d.isoformat(),
                "ticker": str(ticker),
                "source": "yfinance",
                "ingested_at": now,
                **values,
            })
            if len(rows) >= max_rows:
                return rows
    return rows


def run_price_history_writer(cfg, results: dict = None) -> dict:
    """yf_cache が集めた日足を BQ へ書き出す。

    Returns:
        {"skipped": bool, "rows": int, "tickers": int,
         "inserted": bool, "reason": str}
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)

    try:
        import yf_cache
        prices = yf_cache.collected_prices()
        cache_stats = yf_cache.stats()
    except Exception as exc:
        return {"skipped": True, "rows": 0, "tickers": 0,
                "inserted": False, "reason": f"yf_cache不可: {type(exc).__name__}"}

    if cache_stats.get("saved"):
        print(f"    ♻️ yfinance重複取得の節約: {cache_stats['saved']}回 "
              f"(実取得 {cache_stats['info_miss'] + cache_stats['history_miss'] + cache_stats['download_miss']}回)")

    if not prices:
        return {"skipped": True, "rows": 0, "tickers": 0,
                "inserted": False, "reason": "取得済み日足なし"}

    since = date.today() - timedelta(
        days=max(1, int(LOOKBACK_DAYS)))
    rows = build_price_rows(prices, since, max_rows=int(MAX_ROWS))

    if not rows:
        return {"skipped": True, "rows": 0, "tickers": len(prices),
                "inserted": False, "reason": f"直近{LOOKBACK_DAYS}日の行なし"}

    print(f"    💾 株価履歴: {len(prices)}銘柄 / 直近{LOOKBACK_DAYS}日 {len(rows)}行")

    try:
        from edinet_bq_client import insert_rows
        ok = insert_rows(rows=rows, table_ref=_TABLE)
    except Exception as exc:
        print(f"    ⚠️  株価履歴BQ格納エラー: {str(exc)[:80]}")
        return {"skipped": False, "rows": len(rows), "tickers": len(prices),
                "inserted": False, "reason": type(exc).__name__}

    if ok:
        print(f"    ✅ BQ `{_TABLE}` 格納: {len(rows)}行")
    return {"skipped": False, "rows": len(rows), "tickers": len(prices),
            "inserted": bool(ok), "reason": ""}
