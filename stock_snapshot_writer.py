# ==============================================================
# stock_snapshot_writer.py  v1.0  (DB移管 Phase 1)
# スクリーナー結果の日次スナップショットを BQ analytics.stock_snapshot へ
# ==============================================================
# 【役割】
#   JP/US スクリーナーの通過銘柄について、スコア・財務・テクニカルを
#   1行/銘柄/日で BigQuery に蓄積する。
#
# 【なぜ必要か】
#   銘柄mdに出ている指標のうち、technical系（RSI/MACD/MA200/VWAP/52週位置）、
#   FCFマージン、D/E比率、自己資本比率、配当利回り、sector_score、
#   比率の列（roe・roic・fcf_margin・equity_ratio・dividend_yield）は小数（0.22＝22%）、debt_equity は倍（0.30）。
#   stock_detail_report の detail は % で持つので、そこから拾うときは 100 で割る。
#   technical_score は performance_tracker.append_screener_history() が
#   保存する14項目に含まれない。ここで残さないと**その日のmdを開く以外に
#   参照する方法がなく**、時系列で追えないためシグナルの事後検証も
#   バックテストもできない。
#
# 【md との関係】
#   stock_detail_report.py の md 生成には依存しない。md は上位30銘柄・
#   JPのみが対象で、通過銘柄の全件を残せないため。同じ results から
#   独立して行を組み立てる。
#
# 【入力】
#   results["screener"]              JP スクリーナー結果
#   results["us_screener"]           US スクリーナー結果
#   results["technical_scorer"]      RSI / MACD / MA200（あれば）
#   results["support_resistance"]    VWAP / 52週位置（あれば）
#
# 【出力】BQ analytics.stock_snapshot（日付パーティション / ticker+market クラスタ）
# 【依存】edinet_bq_client / config.py
# ==============================================================

from __future__ import annotations

from datetime import datetime, timezone

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B5_STOCK_SNAPSHOT_WRITER_MAX_ROWS': 'MAX_ROWS',
}

# 1回のINSERTで送る上限。JP十数件＋US数百件を想定し、事故時の暴発を防ぐ。
MAX_ROWS = 2000

_TABLE = "analytics.stock_snapshot"


def _f(value):
    """数値化。空文字・None・非数は None（BQのNULL）にする。"""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # NaN/Inf は BQ が受け付けないため落とす
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _b(value):
    """真偽値化。未取得（None）は None のまま残し、False と区別する。"""
    if value is None:
        return None
    return bool(value)


def _s(value, limit: int = 200):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def build_snapshot_rows(
    candidates: list,
    market: str,
    snapshot_date: str,
    tech_cache: dict | None = None,
    sr_cache: dict | None = None,
    now: str | None = None,
    detail_cache: dict | None = None,
) -> list[dict]:
    """候補リストから BQ 行を組み立てる（純粋関数・ネットワークなし）。

    通過銘柄のみを対象にする。pass_filters が候補に無い場合は
    「載っている＝通過」とみなす（履歴側に同キーが無い形式への互換）。

    detail_cache は stock_detail_report が yfinance から取得済みの
    {ticker: {roic, fcf_margin, debt_equity, equity_ratio, div_yield, ...}}。
    候補dictに無い財務指標をここから補完する（無ければ NULL）。
    """
    tech_cache   = tech_cache or {}
    sr_cache     = sr_cache or {}
    detail_cache = detail_cache or {}
    now = now or datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    seen: set = set()

    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        if "pass_filters" in c and not c.get("pass_filters"):
            continue
        ticker = _s(c.get("ticker"), 32)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)

        tech = tech_cache.get(ticker) or {}
        sr   = sr_cache.get(ticker) or {}
        det  = detail_cache.get(ticker) or {}
        macd = tech.get("macd") or {}
        ma   = tech.get("ma_cross") or {}

        def pick(key, *fallbacks, det_pct=False):
            """候補dict優先、無ければ detail（yfinance由来）から拾う。

            候補の比率は小数（ROE 0.22）、detail の比率は %（22.3）で入っている。DB は小数にそろえるため、
            det_pct=True の列は detail から拾ったときだけ 100 で割る。
            """
            for k in (key, *fallbacks):
                if c.get(k) is not None:
                    return c[k]
            for k in (key, *fallbacks):
                if det.get(k) is not None:
                    try:
                        return float(det[k]) / 100 if det_pct else det[k]
                    except (TypeError, ValueError):
                        return None
            return None

        signals = tech.get("signals") or c.get("signals") or []
        if isinstance(signals, (list, tuple)):
            signal_text = " / ".join(str(s) for s in signals[:4])
        else:
            signal_text = str(signals)

        rows.append({
            "snapshot_date":  snapshot_date,
            "ticker":         ticker,
            "market":         market,
            "name":           _s(c.get("name")),
            "sector_code":    _s(c.get("sector_code"), 32),
            "sector_name":    _s(c.get("sector_name"), 64),
            "composite_score": _f(c.get("composite_score")),
            "sector_score":   _f(c.get("sector_score")),
            "value_score":    _f(c.get("value_score")),
            "quality_score":  _f(c.get("quality_score")),
            # technical_score は候補に無い場合 technical_scorer 側を使う
            "technical_score": _f(c.get("technical_score",
                                        tech.get("score"))),
            "discovery_score": _f(c.get("discovery_score")),
            "per":            _f(pick("per")),
            "pbr":            _f(pick("pbr")),
            "roe":            _f(pick("roe", det_pct=True)),
            "roic":           _f(pick("roic", det_pct=True)),
            "fcf_margin":     _f(pick("fcf_margin", det_pct=True)),
            "debt_equity":    _f(pick("debt_equity", det_pct=True)),
            # detail の自己資本比率は yfinance の1株純資産×株数÷総資産で、通貨・株数の食い違いで100%超になることがある
            "equity_ratio":   (lambda v: v if v is not None and 0 <= v <= 1 else None)(_f(pick("equity_ratio", det_pct=True))),
            "dividend_yield": _f(pick("div_yield", "dividend_yield", det_pct=True)),
            "gross_margin":   _f(pick("gross_margin")),
            "op_margin":      _f(pick("op_margin")),
            "market_cap_b":   _f(pick("market_cap_b")),
            "avg_volume":     _f(pick("avg_volume")),
            "current_price":  _f(pick("current_price", "price")),
            "rsi14":          _f(tech.get("rsi_d", tech.get("rsi"))),
            "macd_crossup":   _b(macd.get("crossup")
                                 if macd else None),
            "above_ma200":    _b(ma.get("above_ma200") if ma else None),
            "above_vwap":     _b(sr.get("above_vwap")),
            "vwap_20d":       _f(sr.get("vwap_20d")),
            "week52_position_pct": _f(sr.get("position_pct")),
            "accrual_tier":   _s(c.get("accrual_tier"), 32),
            "size_tier":      _s(c.get("size_tier"), 32),
            "strategy_label": _s(c.get("strategy_label"), 64),
            "analyst_reco":   _s(pick("recommendation", "analyst_reco"), 32),
            "signal_text":    _s(signal_text, 500),
            "pass_filters":   True,
            "ingested_at":    now,
        })

    return rows


def run_stock_snapshot_writer(cfg, results: dict) -> dict:
    """JP/US 両スクリーナーの結果を BQ へ日次INSERTする。

    Returns:
        {"skipped": bool, "rows": int, "jp": int, "us": int,
         "inserted": bool, "reason": str}
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)

    snapshot_date = datetime.now().strftime("%Y-%m-%d")
    tech_cache = (results.get("technical_scorer") or {}).get("results") or {}
    sr_cache   = (results.get("support_resistance") or {}).get("results") or {}
    # stock_detail_report が yfinance から取得済みの財務指標（上位N銘柄分）。
    # 候補dictに無い ROIC / FCFマージン / D-E比率 / 自己資本比率 / 配当利回りを補う。
    detail_cache = (results.get("stock_detail_report") or {}).get("details") or {}

    jp_rows = build_snapshot_rows(
        (results.get("screener") or {}).get("candidates", []),
        "JP", snapshot_date, tech_cache, sr_cache,
        detail_cache=detail_cache,
    )
    us_rows = build_snapshot_rows(
        (results.get("us_screener") or {}).get("candidates", []),
        "US", snapshot_date, tech_cache, sr_cache,
        detail_cache=detail_cache,
    )
    rows = (jp_rows + us_rows)[:MAX_ROWS]

    if not rows:
        print("    ⏩ スナップショット: 通過銘柄なし（skip）")
        return {"skipped": True, "rows": 0, "jp": 0, "us": 0,
                "inserted": False, "reason": "通過銘柄なし"}

    print(f"    📸 スナップショット: JP {len(jp_rows)} / US {len(us_rows)} 件")

    try:
        from edinet_bq_client import insert_rows
        ok = insert_rows(rows=rows, table_ref=_TABLE)
    except Exception as exc:
        print(f"    ⚠️  スナップショットBQ格納エラー: {str(exc)[:80]}")
        return {"skipped": False, "rows": len(rows),
                "jp": len(jp_rows), "us": len(us_rows),
                "inserted": False, "reason": type(exc).__name__}

    if ok:
        print(f"    ✅ BQ `{_TABLE}` 格納: {len(rows)}行")
    else:
        print(f"    ⚠️  BQ `{_TABLE}` 格納失敗")

    return {"skipped": False, "rows": len(rows),
            "jp": len(jp_rows), "us": len(us_rows),
            "inserted": bool(ok), "reason": ""}
