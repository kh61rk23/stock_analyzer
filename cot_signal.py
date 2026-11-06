# ==============================================================
# cot_signal.py — CFTC の建玉明細（COT）の偏りを、逆張りのシグナルとしてマクロの判断に渡す
# ==============================================================
# 【指標】市場ごとに、投機筋（tff＝Leveraged Funds／legacy＝Non-Commercial）の買い越し
#   （買い建玉－売り建玉）を建玉の総数で割った割合（net_pct）を作り、
#   ・位置  : 過去 COT_SIGNAL_YEARS 年の週次の中でのパーセンタイル（同値は中央順位）
#   ・変化  : COT_SIGNAL_CHANGE_WEEKS 週前からの net_pct の差（%pt）と買い越し枚数の差
#   を出す。位置が COT_SIGNAL_PCT_HI 以上＝買い過ぎ、COT_SIGNAL_PCT_LO 以下＝売り過ぎ（どちらも極端）。
#   建玉の総数で割るのは、建玉が増減する市場でも過去と比べられるようにするため。
# 【逆張りの読み】買い過ぎ → その資産は下げやすい、売り過ぎ → 上げやすい。資産ごとの risk_sign
#   （資産が上がるとリスクオンか）を掛けて、リスク選好への向き（risk_tilt：+1 強気／-1 弱気）にする。
#   composite はその市場の重み(w)つき平均（-1〜+1）。極端が無ければ 0。
# 【流れ】週1回、market_positioning の取り込み後に refresh() が BQ を1本だけ読み（dryRun の見積もりが
#   COT_SIGNAL_MAX_BYTES 以下のときだけ）、stock_data/data/cot_signals.json を書く。日次のメール・
#   レジーム判定・シナリオは load_signals() でそのファイルだけを読む（BQ は引かない）。
# 【商品】COT_COMMODITY_MARKETS の商品（legacy・エネルギー／貴金属／金属／穀物／ソフト／畜産／木材）にも同じ位置・変化を出す
#   （signals["commodities"]）。composite には入れない。極端は逆張りで価格の向き（買い過ぎ→下げやすい）にし、
#   ・sector_adjustments : 影響先の業種（日本33業種・米国業種ETF）へ、向き×大きさ(1〜3)/3×重み×COT_COMMODITY_PT を足し上げる
#     （1業種の合計は ±COT_COMMODITY_SECTOR_MAX_PT）。点の内訳（どの商品が何点）も返し、後で効きを測れるようにする
#   ・mail_lines         : メールの節に商品の極端を COT_MAIL_COMMODITY_MAX_LINES 行まで足す
#   影響先は bond_commodity_pipeline.SECTOR_IMPACT_MAP（"+"＝価格上昇が追い風）を既定にし、商品ごとの jp／us で上書きできる。
# 【欠けの扱い】履歴が COT_SIGNAL_MIN_WEEKS 週未満・最新の値が欠けた市場は判定せず、合成の分母にも入れない。
#   ファイルが無い・最新の週が COT_SIGNAL_STALE_DAYS 日より古い場合は None（呼び出し側は何も足さない）。
# ==============================================================
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

SIGNAL_FILE = "cot_signals.json"
_TABLE = "analytics.cot_positions"


def _param(cfg, name: str, default):
    return getattr(cfg, name, default) if cfg is not None else default


def _markets(cfg) -> dict:
    return _param(cfg, "COT_SIGNAL_MARKETS", {}) or {}


def commodity_markets(cfg) -> dict:
    """COT_COMMODITY_MARKETS を {key: {report, code, name, impact, jp, us, w}} に解く（純関数）。
    signal を持つ項目は COT_SIGNAL_MARKETS の同じキーからコード・名前を取る。解けない項目は除く。"""
    if not _param(cfg, "ENABLE_COT_COMMODITY", True):
        return {}
    base, out = _markets(cfg), {}
    for key, c in (_param(cfg, "COT_COMMODITY_MARKETS", {}) or {}).items():
        ref = base.get(c.get("signal")) if c.get("signal") else None
        code, name = (ref.get("code"), ref.get("name")) if ref else (c.get("code"), c.get("name"))
        if not code or (ref and ref.get("report") != "legacy"):
            continue
        out[key] = {"report": "legacy", "code": str(code), "name": name or key, "impact": c.get("impact"),
                    "jp": c.get("jp"), "us": c.get("us"), "w": float(c.get("w", 1.0)), "signal": c.get("signal")}
    return out


def legacy_codes(cfg) -> set:
    """legacy で使う市場コード（COT_SIGNAL_MARKETS と商品）。market_positioning が BQ に残す市場の元になる。"""
    codes = {str(m["code"]) for m in _markets(cfg).values() if m.get("report") == "legacy" and m.get("code")}
    return codes | {m["code"] for m in commodity_markets(cfg).values()}


# ── 純関数 ───────────────────────────────────────────────

def net_pct(long_, short_, open_interest) -> float | None:
    """買い越しの建玉に対する割合（%）。どれかが欠け・建玉が 0 以下なら None。"""
    try:
        long_, short_, oi = float(long_), float(short_), float(open_interest)
    except (TypeError, ValueError):
        return None
    if oi <= 0:
        return None
    return (long_ - short_) / oi * 100.0


def percentile_rank(window: list, x: float) -> float:
    """window の中での x の位置（0〜100）。同値は半分を数える。空なら 50。"""
    if not window:
        return 50.0
    less = sum(1 for v in window if v < x)
    equal = sum(1 for v in window if v == x)
    return (less + 0.5 * equal) / len(window) * 100.0


def extreme_of(pct: float, hi: float, lo: float) -> str | None:
    """'long'（買い過ぎ）／'short'（売り過ぎ）／None。"""
    if pct >= hi:
        return "long"
    if pct <= lo:
        return "short"
    return None


def risk_tilt(extreme: str | None, risk_sign: int) -> int:
    """極端 → リスク選好への向き（逆張り）。買い過ぎ＝資産は下げやすい＝ risk_sign の逆。"""
    if extreme is None:
        return 0
    price_dir = -1 if extreme == "long" else 1
    return price_dir * (1 if risk_sign >= 0 else -1)


def market_signal(series: list, cfg=None, risk_sign: int = 1) -> dict | None:
    """1市場の指標。series＝[(date, net_pct, net)] の日付昇順（欠けた週は含めない）。履歴不足は None。"""
    years = int(_param(cfg, "COT_SIGNAL_YEARS", 3))
    min_weeks = int(_param(cfg, "COT_SIGNAL_MIN_WEEKS", 52))
    hi = float(_param(cfg, "COT_SIGNAL_PCT_HI", 90.0))
    lo = float(_param(cfg, "COT_SIGNAL_PCT_LO", 10.0))
    weeks = int(_param(cfg, "COT_SIGNAL_CHANGE_WEEKS", 4))
    if not series:
        return None
    d0, p0, n0 = series[-1]
    window = [p for d, p, _ in series if d > d0 - timedelta(days=365 * years)]
    if len(window) < min_weeks:
        return None
    pct = percentile_rank(window, p0)
    prev = [(d, p, n) for d, p, n in series if abs((d - (d0 - timedelta(days=7 * weeks))).days) <= 3]
    ex = extreme_of(pct, hi, lo)
    return {"date": d0.isoformat(), "net_pct": round(p0, 2), "net": round(n0),
            "percentile": round(pct, 1), "weeks": len(window),
            "chg_pct": round(p0 - prev[-1][1], 2) if prev else None,
            "chg_net": round(n0 - prev[-1][2]) if prev else None,
            "extreme": ex, "risk_tilt": risk_tilt(ex, risk_sign)}


def _series(rows: list, report: str, code: str) -> list:
    """rows から 1 市場の [(date, net_pct, net)]（日付昇順・欠けた週は含めない）。"""
    series = {}
    for r in rows or []:
        if r.get("report") != report or str(r.get("market_code", "")).strip() != code:
            continue
        p = net_pct(r.get("spec_long"), r.get("spec_short"), r.get("open_interest"))
        if p is None:
            continue
        d = r["report_date"] if isinstance(r["report_date"], date) else date.fromisoformat(str(r["report_date"])[:10])
        series[d] = (p, float(r["spec_long"]) - float(r["spec_short"]))
    return [(d, p, n) for d, (p, n) in sorted(series.items())]


def price_tilt(extreme: str | None) -> int:
    """極端 → 価格の向き（逆張り）。買い過ぎ＝-1（下げやすい）・売り過ぎ＝+1（上げやすい）・極端なし＝0。"""
    return {"long": -1, "short": 1}.get(extreme, 0)


def build_signals(rows: list, cfg=None, today: date | None = None) -> dict:
    """BQ の行 → 市場ごとの指標と合成。

    rows＝[{report_date, report, market_code, open_interest, spec_long, spec_short}]。
    返り値＝{as_of, markets:{key:指標}, composite, extremes:[key…], commodities:{key:指標}, commodity_extremes:[key…]}。
    判定できた市場が無ければ markets・commodities は空。
    """
    today = today or date.today()
    out, w_sum, tilt_sum = {}, 0.0, 0.0
    for key, m in _markets(cfg).items():
        sig = market_signal(_series(rows, m["report"], m["code"]), cfg, int(m.get("risk_sign", 1)))
        if sig is None:
            continue
        sig.update(name=m["name"], w=float(m.get("w", 1.0)))
        out[key] = sig
        w_sum += sig["w"]
        tilt_sum += sig["w"] * sig["risk_tilt"]
    com = {}
    for key, m in commodity_markets(cfg).items():
        sig = out.get(m["signal"]) if m.get("signal") else market_signal(_series(rows, m["report"], m["code"]), cfg, 1)
        if sig is None:
            continue
        com[key] = {k: v for k, v in sig.items() if k not in ("risk_tilt", "w")}
        com[key].update(name=m["name"], w=m["w"], price_tilt=price_tilt(sig["extreme"]))
    dates = [s["date"] for s in list(out.values()) + list(com.values())]
    by_pos = lambda d: [k for k, s in sorted(d.items(), key=lambda kv: -abs(kv[1]["percentile"] - 50)) if s["extreme"]]
    return {"as_of": max(dates) if dates else None, "generated": today.isoformat(), "markets": out,
            "composite": round(tilt_sum / w_sum, 3) if w_sum else 0.0, "extremes": by_pos(out),
            "commodities": com, "commodity_extremes": by_pos(com)}


def is_fresh(signals: dict | None, cfg=None, today: date | None = None) -> bool:
    """最新の週が COT_SIGNAL_STALE_DAYS 日以内か。"""
    if not signals or not signals.get("as_of"):
        return False
    today = today or date.today()
    try:
        age = (today - date.fromisoformat(signals["as_of"])).days
    except ValueError:
        return False
    return age <= int(_param(cfg, "COT_SIGNAL_STALE_DAYS", 21))


def regime_adj(signals: dict | None, cfg=None) -> float:
    """レジーム判定の平均（0〜100）へ足す点数。信号が無い・古い・極端が無ければ 0。"""
    if not is_fresh(signals, cfg):
        return 0.0
    return round(float(signals.get("composite") or 0.0) * float(_param(cfg, "COT_REGIME_MAX_PT", 4.0)), 3)


def scenario_factor(shock_pct: float, signals: dict | None, cfg=None) -> float:
    """シナリオの市場変動（%）に掛ける倍率。下げ想定は composite<0（買い過ぎ）で増幅、上げ想定は composite>0 で増幅。"""
    if not is_fresh(signals, cfg) or not shock_pct:
        return 1.0
    c = float(signals.get("composite") or 0.0)
    w = float(_param(cfg, "COT_SCENARIO_SHOCK_WEIGHT", 0.15))
    return round(1.0 + w * c * (1 if shock_pct > 0 else -1), 4)


_EX_JA = {"long": ("買い越し", "買い過ぎ→下げやすい"), "short": ("売り越し", "売り過ぎ→上げやすい")}


def _impact_map() -> dict:
    try:
        from bond_commodity_pipeline import SECTOR_IMPACT_MAP
        return SECTOR_IMPACT_MAP
    except Exception:
        return {}


def _targets(m: dict, impact_map: dict) -> dict:
    """商品の影響先 {"jp": [(コード, "+"/"-", 1〜3)], "us": [...]}。項目の jp／us が impact より優先。"""
    base = impact_map.get(m.get("impact")) or {}
    return {"jp": m.get("jp") if m.get("jp") is not None else base.get("jp", []),
            "us": m.get("us") if m.get("us") is not None else base.get("us", [])}


def sector_adjustments(signals: dict | None, cfg=None, impact_map: dict | None = None) -> dict:
    """商品の極端 → 業種への加点。{"jp": {コード: {"delta", "detail"}}, "us": {ETF: …}, "as_of", "extremes"}。

    1つの極端が 1業種に入れる点 = 価格の向き(逆張り) × 影響の向き(+1/-1) × 大きさ/3 × 重み × COT_COMMODITY_PT。
    delta は業種ごとに ±COT_COMMODITY_SECTOR_MAX_PT で打ち止め、detail は打ち止め前の商品別の点（内訳）。
    信号が無い・古い・極端が無ければ jp／us とも空。
    """
    res = {"jp": {}, "us": {}, "as_of": None, "extremes": []}
    if not is_fresh(signals, cfg) or not _param(cfg, "ENABLE_COT_COMMODITY", True):
        return res
    impact_map = _impact_map() if impact_map is None else impact_map
    unit = float(_param(cfg, "COT_COMMODITY_PT", 1.0))
    cap = float(_param(cfg, "COT_COMMODITY_SECTOR_MAX_PT", 2.0))
    ms = commodity_markets(cfg)
    com = signals.get("commodities") or {}
    for key in signals.get("commodity_extremes") or []:
        s, m = com.get(key), ms.get(key)
        if not s or not m or not s.get("price_tilt"):
            continue
        res["extremes"].append(key)
        for area, rows in _targets(m, impact_map).items():
            for code, direction, mag in rows:
                d = s["price_tilt"] * (1 if direction == "+" else -1) * float(mag) / 3.0 * m["w"] * unit
                e = res[area].setdefault(str(code), {"delta": 0.0, "detail": []})
                e["detail"].append({"key": key, "name": m["name"], "delta": round(d, 3)})
                e["delta"] += d
    for area in ("jp", "us"):
        for e in res[area].values():
            e["delta"] = round(max(-cap, min(cap, e["delta"])), 2)
    res["as_of"] = signals.get("as_of")
    return res


def _jp_name(code: str, cfg) -> str:
    v = (_param(cfg, "JAPAN_SECTORS", {}) or {}).get(code) or {}
    return v.get("name_ja") or code


def _commodity_line(key: str, s: dict, m: dict | None, cfg, impact_map: dict) -> str:
    side, _ = _EX_JA[s["extreme"]]
    view = "価格は下げやすい" if s["price_tilt"] < 0 else "価格は上げやすい"
    chg = f"・直近{int(_param(cfg, 'COT_SIGNAL_CHANGE_WEEKS', 4))}週 {s['chg_pct']:+.1f}%pt" if s.get("chg_pct") is not None else ""
    eff = ""
    if m:
        pos, neg = [], []
        for code, direction, mag in sorted(_targets(m, impact_map)["jp"], key=lambda t: -t[2]):
            (pos if s["price_tilt"] * (1 if direction == "+" else -1) > 0 else neg).append(_jp_name(str(code), cfg))
        eff = "".join(f" {lab}:{'・'.join(names[:3])}" for lab, names in (("追い風", pos), ("逆風", neg)) if names)
    return (f"  [商品] {s['name']}: 投機筋の{side}が過去{int(_param(cfg, 'COT_SIGNAL_YEARS', 3))}年の"
            f"{s['percentile']:.0f}%点{chg}（逆張りでは{view}）{('→' + eff) if eff else ''}")


def mail_lines(signals: dict | None, cfg=None) -> list[str]:
    """メールの節の行（市場の極端を COT_MAIL_MAX_LINES 行・商品の極端を COT_MAIL_COMMODITY_MAX_LINES 行まで、
    いずれも位置が両端に近い順）。無ければ []。"""
    if not is_fresh(signals, cfg):
        return []
    mk = signals.get("markets") or {}
    lines = []
    for key in signals.get("extremes") or []:
        s = mk.get(key)
        if not s or not s.get("extreme"):
            continue
        side, view = _EX_JA[s["extreme"]]
        chg = f"・直近{int(_param(cfg, 'COT_SIGNAL_CHANGE_WEEKS', 4))}週 {s['chg_pct']:+.1f}%pt" if s.get("chg_pct") is not None else ""
        lines.append(f"  {s['name']}: 投機筋の{side}が過去{int(_param(cfg, 'COT_SIGNAL_YEARS', 3))}年の"
                     f"{s['percentile']:.0f}%点{chg}（逆張りでは{view}）")
        if len(lines) >= int(_param(cfg, "COT_MAIL_MAX_LINES", 3)):
            break
    if _param(cfg, "ENABLE_COT_COMMODITY", True):
        com, ms, imap, n = signals.get("commodities") or {}, commodity_markets(cfg), _impact_map(), 0
        for key in signals.get("commodity_extremes") or []:
            s = com.get(key)
            if not s or not s.get("extreme"):
                continue
            lines.append(_commodity_line(key, s, ms.get(key), cfg, imap))
            n += 1
            if n >= int(_param(cfg, "COT_MAIL_COMMODITY_MAX_LINES", 4)):
                break
    if lines:
        lines.append(f"  （CFTC {signals['as_of']} 時点・週次）")
    return lines


# ── ファイル ─────────────────────────────────────────────

def _path(cfg=None) -> Path:
    base = _param(cfg, "DATA_DIR", None)
    if not base:
        env = os.environ.get("STOCK_ANALYZER_BASE", "").strip()
        base = (Path(env).expanduser() if env else Path("stock_data")) / "data"
    return Path(base) / SIGNAL_FILE


def load_signals(cfg=None, today: date | None = None) -> dict | None:
    """週次で作ったファイルを読む。無い・読めない・古いなら None。"""
    if not _param(cfg, "ENABLE_COT_SIGNAL", True):
        return None
    try:
        s = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return s if is_fresh(s, cfg, today) else None


# ── BQ（週1回） ──────────────────────────────────────────

def build_query(cfg=None) -> str:
    """要る市場の直近の週だけを1本で読む SQL（日付とクラスタ列で絞る）。"""
    years = int(_param(cfg, "COT_SIGNAL_YEARS", 3))
    by_report: dict = {}
    for m in list(_markets(cfg).values()) + list(commodity_markets(cfg).values()):
        if m["code"] not in by_report.setdefault(m["report"], []):
            by_report[m["report"]].append(m["code"])
    cond = " OR ".join(
        f"(report = '{rep}' AND market_code IN ({', '.join(repr(c) for c in codes)}))"
        for rep, codes in sorted(by_report.items()))
    # 3年分に加え、4週前の比較・週のずれの余裕として 30 日を足す
    return (
        "SELECT report_date, report, market_code, open_interest, "
        "SAFE_CAST(IF(report = 'tff', JSON_VALUE(positions, '$.lev_money_positions_long'), "
        "JSON_VALUE(positions, '$.noncomm_positions_long_all')) AS FLOAT64) AS spec_long, "
        "SAFE_CAST(IF(report = 'tff', JSON_VALUE(positions, '$.lev_money_positions_short'), "
        "JSON_VALUE(positions, '$.noncomm_positions_short_all')) AS FLOAT64) AS spec_short "
        f"FROM `{{project}}.{_TABLE}` "
        f"WHERE report_date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL {365 * years + 30} DAY) AND ({cond}) "
        "ORDER BY report, market_code, report_date")


def refresh(cfg, client=None) -> dict:
    """BQ を1本だけ読んで cot_signals.json を作り直す。見積もりが上限を超えたら読まない。"""
    if not _param(cfg, "ENABLE_COT_SIGNAL", True):
        return {"skipped": True, "reason": "ENABLE_COT_SIGNAL=False"}
    from google.cloud import bigquery
    if client is None:
        import state_store
        client = state_store.make_client(cfg)
    sql = build_query(cfg).replace("{project}", client.project)
    limit = int(_param(cfg, "COT_SIGNAL_MAX_BYTES", 100 * 1024 ** 2))
    est = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
                       location="asia-northeast1").total_bytes_processed
    if est > limit:
        return {"skipped": True, "reason": f"見積もり {est / 1024 ** 2:.1f}MB が上限 {limit / 1024 ** 2:.0f}MB 超"}
    rows = [dict(r) for r in client.query(
        sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=limit), location="asia-northeast1").result()]
    sig = build_signals(rows, cfg)
    sig["generated_at"] = datetime.now().isoformat(timespec="seconds")
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sig, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return {"rows": len(rows), "markets": len(sig["markets"]), "commodities": len(sig["commodities"]),
            "commodity_extremes": sig["commodity_extremes"], "extremes": sig["extremes"],
            "composite": sig["composite"], "as_of": sig["as_of"], "mb": round(est / 1024 ** 2, 1)}
