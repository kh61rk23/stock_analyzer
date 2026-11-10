# ==============================================================
# us_macro_pipeline.py — 米国マクロ拡張パイプライン（Wave US-2）
# ==============================================================
# 【役割】FRED拡張マクロ指標（失業率・CPI・HYスプレッド・失業保険申請・
#   住宅着工許可・逆イールド・鉱工業生産）を取得し、先行シグナルと
#   合成スコアを生成する。stock_analyzer_main の us_macro ステージが消費。
# 【呼び出し契約】run_us_macro_pipeline(cfg, results) → {
#     "macro_snapshot_ext":       {unrate, cpi_yoy, hy_spread, ...},
#     "leading_signals":          [{factor, label, signal, trigger_value, ...}],
#     "d2_supplement":            {hy_spread, comment}（債務サイクル補完）,
#     "composite_us_macro_score": float(0-100),
#     "cached":                   bool,
#   }
# 【設計正本】SPEC_core §3-2(12.11) / config BLOCK US-2。
#   FRED APIキーは POLICY_FRED_API_KEY を共用（無ければキャッシュ/空で継続）。
# 使い方: main日次ステージから自動 / python3 us_macro_pipeline.py（単体）
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def _clip_norm(val: float, lo: float, hi: float) -> float:
    """[lo,hi] を [0,100] に線形正規化してクリップ。"""
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (val - lo) / (hi - lo) * 100.0))


def cpi_yoy(cpi_series: list) -> float | None:
    """CPI水準系列から前年比(%)を算出（12ヶ月前と比較・月次前提）。"""
    vals = [o["value"] for o in cpi_series if o.get("value") is not None]
    if len(vals) < 13:
        return None
    return round((vals[-1] / vals[-13] - 1) * 100, 2)


def pct_change(series: list, periods: int = 1) -> float | None:
    """系列の直近 periods 期前比(%)。"""
    vals = [o["value"] for o in series if o.get("value") is not None]
    if len(vals) <= periods or vals[-1 - periods] == 0:
        return None
    return round((vals[-1] / vals[-1 - periods] - 1) * 100, 2)


def latest(series: list) -> float | None:
    vals = [o["value"] for o in series if o.get("value") is not None]
    return vals[-1] if vals else None


def mfg_survey_composite(series: dict, today=None, max_age_days: int = 75) -> dict:
    """地区連銀の製造業調査（0 中心の拡散指数）から製造業の景況を作る。

    series は {キー: [{"date": "YYYY-MM-DD", "value": float}, ...]}。最新の観測が
    max_age_days より古い系列は使わない（止まった系列を最新として混ぜない）。
    使える系列の最新値の平均を us_mfg_survey とし、1系列も使えなければ空の dict。
    """
    from datetime import date as _date, datetime as _dt
    today = today or _date.today()
    out = {}
    for key, obs in (series or {}).items():
        pts = [o for o in (obs or []) if o.get("value") is not None and o.get("date")]
        if not pts:
            continue
        last = max(pts, key=lambda o: o["date"])
        try:
            age = (today - _dt.strptime(last["date"][:10], "%Y-%m-%d").date()).days
        except ValueError:
            continue
        if age > max_age_days:
            continue
        out[key] = float(last["value"])
    if out:
        out["us_mfg_survey"] = round(sum(out.values()) / len(out), 2)
    return out


def fetch_mfg_survey(cfg) -> dict:
    """FRED から地区連銀の製造業調査を取り、macro_snapshot に足すキーを返す（失敗しても空）。

    横断産業ルール（us_ism_manufacturing_up/down）は macro 段の評価時点で値が要るため、
    us_macro 段（後ろ）ではなく macro 段から呼ぶ。月次データなので結果は20時間キャッシュする
    （日次の実行で FRED を 2 本叩くのは初回だけ）。
    """
    if not getattr(cfg, "ENABLE_US_MFG_SURVEY", True):
        return {}
    fred_key = getattr(cfg, "POLICY_FRED_API_KEY", "") or ""
    series_def = getattr(cfg, "US_MFG_SURVEY_SERIES", {}) or {}
    if not series_def:
        return {}
    cache_p = _cache_path(cfg).with_name("us_mfg_survey_cache.json")
    try:
        cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    except Exception:
        cache = {}
    if _is_fresh(cache):
        return {k: v for k, v in cache.items() if not k.startswith("_")}
    if not fred_key:
        return {}
    try:
        from policy_tracker import fetch_fred_data
    except Exception:
        return {}
    raw = {}
    for key, sid in series_def.items():
        try:
            raw[key] = fetch_fred_data(fred_key, sid, days=120)
        except Exception:
            raw[key] = []
    out = mfg_survey_composite(raw, max_age_days=int(getattr(cfg, "US_MFG_SURVEY_MAX_AGE_DAYS", 75)))
    if out:
        try:
            cache_p.write_text(json.dumps({**out, "_fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                          ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out


def build_leading_signals(snapshot: dict, cfg=None) -> list[dict]:
    """
    拡張マクロのスナップショットから先行シグナルを生成する。
    - HYスプレッド急拡大 → リスクオフ（headwind）
    - 逆イールド（10Y-2Y<0）→ 景気後退先行（headwind）
    - 失業保険申請の急増 → 雇用悪化先行（headwind）
    - 住宅着工許可の増加 → 景気先行改善（tailwind）
    """
    hy_th = getattr(cfg, "US_MACRO_HY_SPREAD_ALERT", 5.0)
    inv_th = getattr(cfg, "US_MACRO_T10Y2Y_INVERT", 0.0)
    icsa_th = getattr(cfg, "US_MACRO_ICSA_SPIKE_PCT", 15.0)
    sigs = []

    hy = snapshot.get("hy_spread")
    if hy is not None and hy >= hy_th:
        sigs.append({"factor": "us_hy_spread", "label": "米HYスプレッド拡大",
                     "signal": "headwind", "trigger_value": hy,
                     "detail": f"HYスプレッド {hy:.2f}% >= {hy_th}%（信用リスク上昇）"})

    t10y2y = snapshot.get("t10y2y")
    if t10y2y is not None and t10y2y < inv_th:
        sigs.append({"factor": "us_yield_inversion", "label": "米逆イールド",
                     "signal": "headwind", "trigger_value": t10y2y,
                     "detail": f"10Y-2Y {t10y2y:.2f}% < 0（景気後退先行指標）"})

    icsa_chg = snapshot.get("icsa_mom_pct")
    if icsa_chg is not None and icsa_chg >= icsa_th:
        sigs.append({"factor": "us_jobless_claims", "label": "米失業保険申請急増",
                     "signal": "headwind", "trigger_value": icsa_chg,
                     "detail": f"失業保険申請 前月比+{icsa_chg:.1f}%（雇用悪化先行）"})

    permit_chg = snapshot.get("permit_mom_pct")
    if permit_chg is not None and permit_chg >= 3.0:
        sigs.append({"factor": "us_building_permits", "label": "米住宅着工許可改善",
                     "signal": "tailwind", "trigger_value": permit_chg,
                     "detail": f"住宅着工許可 前月比+{permit_chg:.1f}%（景気先行改善）"})

    return sigs


def composite_us_macro_score(snapshot: dict, signals: list) -> float:
    """
    拡張マクロの総合スコア（0-100・高いほど景気追い風）。
    失業率(低いほど良)・逆イールド(順イールドが良)・HY(低いほど良)・
    住宅着工(高いほど良)を正規化平均し、先行シグナルで微調整。
    """
    parts = []
    if snapshot.get("unrate") is not None:
        parts.append(100 - _clip_norm(snapshot["unrate"], 3.0, 8.0))   # 3%=良/8%=悪
    if snapshot.get("t10y2y") is not None:
        parts.append(_clip_norm(snapshot["t10y2y"], -1.0, 2.0))        # 逆イールド=悪
    if snapshot.get("hy_spread") is not None:
        parts.append(100 - _clip_norm(snapshot["hy_spread"], 3.0, 8.0))  # 低い=良
    if snapshot.get("permit_mom_pct") is not None:
        parts.append(_clip_norm(snapshot["permit_mom_pct"], -10.0, 10.0))
    base = sum(parts) / len(parts) if parts else 50.0
    # 先行シグナルで調整（headwind -5pt / tailwind +3pt 各）
    for s in signals:
        base += -5 if s["signal"] == "headwind" else 3
    return round(max(0.0, min(100.0, base)), 1)


# ── I/O層 ────────────────────────────────────────────────────

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path("stock_data/cache")     # 本番パスが無い環境へのフォールバック
        d.mkdir(parents=True, exist_ok=True)
    return d / "us_macro_cache.json"


def _is_fresh(cache: dict, hours: int = 20) -> bool:
    try:
        ts = datetime.strptime(cache.get("_fetched_at", ""), "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - ts).total_seconds() < hours * 3600
    except Exception:
        return False


def run_us_macro_pipeline(cfg, results: dict = None) -> dict:
    """メインエントリ（main の us_macro ステージから呼ばれる）。"""
    series_def = getattr(cfg, "US_MACRO_FRED_SERIES", {})
    fred_key = getattr(cfg, "POLICY_FRED_API_KEY", "")
    cache_p = _cache_path(cfg)

    # キャッシュ鮮度チェック
    cache = {}
    if cache_p.exists():
        try:
            cache = json.loads(cache_p.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    if _is_fresh(cache):
        cache["cached"] = True
        return cache

    # FRED取得（policy_tracker のクライアントを共用）
    raw = {}
    try:
        from policy_tracker import fetch_fred_data
    except Exception:
        fetch_fred_data = None

    for key, spec in series_def.items():
        sid = spec[0] if isinstance(spec, (list, tuple)) else spec
        raw[key] = (fetch_fred_data(fred_key, sid, days=500)
                    if (fetch_fred_data and fred_key) else [])

    # スナップショット構築
    snapshot = {
        "unrate": latest(raw.get("unrate", [])),
        "cpi_yoy": cpi_yoy(raw.get("cpi", [])),
        "hy_spread": latest(raw.get("hy_spread", [])),
        "icsa": latest(raw.get("icsa", [])),
        "icsa_mom_pct": pct_change(raw.get("icsa", []), 4),   # 約1ヶ月（週次×4）
        "permit": latest(raw.get("permit", [])),
        "permit_mom_pct": pct_change(raw.get("permit", []), 1),
        "t10y2y": latest(raw.get("t10y2y", [])),
        "indpro_mom_pct": pct_change(raw.get("indpro", []), 1),
    }
    signals = build_leading_signals(snapshot, cfg)
    score = composite_us_macro_score(snapshot, signals)

    hy = snapshot.get("hy_spread")
    d2_supp = {"hy_spread": hy,
               "comment": (f"HYスプレッド{hy:.2f}%" if hy is not None
                           else "HYスプレッド取得不可")} if hy is not None else {}

    out = {
        "macro_snapshot_ext": {k: v for k, v in snapshot.items() if v is not None},
        "leading_signals": signals,
        "d2_supplement": d2_supp,
        "composite_us_macro_score": score,
        "cached": False,
        "_fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        cache_p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    n_data = len(out["macro_snapshot_ext"])
    print(f"  🇺🇸 [us_macro] スコア{score}/100 / 指標{n_data}件 / "
          f"先行シグナル{len(signals)}件"
          + ("（FREDキー無し＝空データ）" if not fred_key else ""))
    return out


def main():
    try:
        import config as cfg
    except Exception:
        cfg = None
    out = run_us_macro_pipeline(cfg, {})
    print(json.dumps({k: v for k, v in out.items() if not k.startswith("_")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
