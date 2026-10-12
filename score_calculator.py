# ==============================================================
# score_calculator.py — JP/US共通composite計算
#   （R5-2 Phase A / E10 → Phase B / U-9）
# ==============================================================
# 【役割】SPEC_core §8-2 の統合方針どおり、市場別に別実装だった
#   composite_score を adapter 経由で共通形式へ統一する。
#   対象 = 保有銘柄 ＋ us_screener 上位50 のみ（要件 §R5-2 / 決定 J-03=a。
#   Phase B でも拡大しない）。
# 【設計】既存ロジックの「再実装はしない」。JP は screener/sectors、
#   US は us_screener の算出済みスコアを adapter が共通スキーマへ写像する
#   （= base）。ここで二重実装すると SSOT が割れる。
# 【Phase B（U-9）】base に共通成分の調整を載せる:
#     composite = clamp(base + dcf_adj + tech_adj + geo_adj + re_adj, 0, 100)
#   各成分は results を「読むだけ」で取得する（データ源は下表）。
#   ⚠️ score_calculator は yfinance / BigQuery / 外部APIを一切叩かない。
#     日次バッチ終盤（run_all_tasks の後半）で走るため。
#   ┌ 成分     │ 一次ソース                                   │ 代替
#   │ tech_adj │ results["technical_scorer"]["results"][t]["score"] │ 候補の technical_score
#   │ dcf_adj  │ results["dcf_valuation"]["results"][t]["dcf_bonus"]│ 候補の dcf_bonus
#   │ geo_adj  │ 候補の geo_impact_bonus                       │ results["sectors"][業種]["geo_impact_bonus"]
#   └ re_adj   │ 候補の real_estate_bonus                      │ results["sectors"][業種]["real_estate_by_ticker"][t]["bonus"]
#   取得できない成分は「未算出」として 0 扱い → その銘柄は Phase A と同値。
#   ⚠️ base に既に含まれている成分は加算しない（二重計上の排除）。
#     どの成分を加算するかは score_source 単位で _PHASE_B_APPLICABLE が決める:
#       jp_screener        → re（tech/dcf/geo は base に含む）
#       jp_sector_fallback → tech / dcf / re（geo は業種 composite に含む）
#       us_screener        → tech / dcf / geo / re（base はいずれも含まない。US は保有不動産データが無く re は未算出）
#       未知の score_source → なし（安全側）
#   config.UNIFIED_SCORE_PHASE_B_ENABLED=False で Phase A 出力へ完全復帰。
# 共通出力スキーマ（1銘柄・Phase A から変えない）:
#   {"ticker","market","sector","composite_score","score_source","components"}
#   components に dcf_adj/tech_adj/geo_adj を追加（取得できたもののみ）。
#   score_source は Phase B が効いた場合 "…+phaseB" となる。
# 使い方:
#   （日次）stock_analyzer_main が run_score_calculator(cfg, results) を呼ぶ
# ==============================================================

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── adapter（純粋ロジック・pytest対象） ──────────────────────

class JpSectorAdapter:
    """JP株: screener候補 → 無ければ業種composite（base_agent）でフォールバック。"""

    market = "JP"

    def extract(self, ticker: str, sector_code: str, results: dict) -> dict | None:
        # 1) 銘柄レベル: screener候補に composite_score があれば最優先
        for c in (results.get("screener") or {}).get("candidates", []) or []:
            if c.get("ticker") == ticker and \
                    _num(c.get("composite_score")) is not None:
                return {"composite_score": float(c["composite_score"]),
                        "score_source": "jp_screener",
                        "components": {
                            "sector_score": c.get("sector_score"),
                            "value_score": c.get("value_score"),
                            "quality_score": c.get("quality_score"),
                            "technical_score": c.get("technical_score"),
                        }}
        # 2) 業種レベル: sectors[sector_code].composite_score（-30..100）
        sec = (results.get("sectors") or {}).get(str(sector_code))
        if isinstance(sec, dict) and \
                _num(sec.get("composite_score")) is not None:
            return {"composite_score": float(sec["composite_score"]),
                    "score_source": "jp_sector_fallback",
                    "components": {"sector_score": sec.get("composite_score")}}
        return None


class UsSectorAdapter:
    """US株: us_screener候補の composite_score を写像。"""

    market = "US"

    def extract(self, ticker: str, sector_code: str, results: dict) -> dict | None:
        us = results.get("us_screener") or {}
        for pool in (us.get("top_picks"), us.get("candidates")):
            for c in pool or []:
                if c.get("ticker") == ticker and \
                        _num(c.get("composite_score")) is not None:
                    return {"composite_score": float(c["composite_score"]),
                            "score_source": "us_screener",
                            "components": {
                                "sector": c.get("sector"),
                                "value_score": c.get("value_score"),
                                "quality_score": c.get("quality_score"),
                            }}
        return None


_ADAPTERS = {"JP": JpSectorAdapter(), "US": UsSectorAdapter()}


# ── Phase B: 共通成分（U-9） ────────────────────
# 【鉄則】ここは results を「読むだけ」。yfinance / BigQuery / 外部APIを
#   新たに叩かない（score_calculator は日次バッチ終盤で走るため）。
#   results に無い成分は None（＝未算出）とし、その銘柄では 0 として扱う。

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _num(v) -> float | None:
    """有限な数値だけを float で返す（bool・NaN・Inf を弾く）。

    isinstance(v, (int, float)) では numpy.float32 / numpy.int64（Python の
    int/float のサブクラスではない）を弾いてしまうため、判定は
    finance_logic.as_number（float()変換の成否）に委譲する。
    """
    from finance_logic import as_number
    return as_number(v)


def _iter_candidates(results: dict, market: str):
    """market に対応する screener 候補プールを順に返す。"""
    if market == "JP":
        yield from ((results.get("screener") or {}).get("candidates") or [])
    elif market == "US":
        us = results.get("us_screener") or {}
        yield from (us.get("top_picks") or [])
        yield from (us.get("candidates") or [])


def _candidate_field(ticker: str, market: str, results: dict,
                     field: str) -> float | None:
    for c in _iter_candidates(results, market):
        if isinstance(c, dict) and c.get("ticker") == ticker:
            v = _num(c.get(field))
            if v is not None:
                return v
    return None


def _max_adj(cfg, name: str, default: float) -> float:
    """config の上限値を非負 float として取り出す（不正値は default）。"""
    v = _num(getattr(cfg, name, default))
    if v is None:
        v = default
    return abs(v)


def extract_tech_score(ticker: str, market: str, results: dict) -> float | None:
    """
    0..100（50=中立）のテクニカルスコアを results から取得する。
    ① results["technical_scorer"]["results"][ticker]["score"]（本命）
    ② screener候補の "technical_score"（JP screener が保持）
    """
    entry = ((results.get("technical_scorer") or {}).get("results")
             or {}).get(ticker)
    if isinstance(entry, dict):
        v = _num(entry.get("score"))
        if v is not None:
            return v
    return _candidate_field(ticker, market, results, "technical_score")


def calc_tech_adj(ticker: str, market: str, results: dict,
                  max_adj: float) -> float | None:
    """テクニカル調整。50点を中立として ±max_adj へ線形写像する。"""
    s = extract_tech_score(ticker, market, results)
    if s is None:
        return None
    return round(_clamp((s - 50.0) / 50.0 * max_adj, -max_adj, max_adj), 2)


def extract_dcf_bonus(ticker: str, market: str, results: dict) -> float | None:
    """
    DCF ボーナス（pt・dcf_valuation.calc_dcf_bonus は -6..+8）を取得する。
    ① results["dcf_valuation"]["results"][ticker]["dcf_bonus"]（本命）
    ② screener候補の "dcf_bonus"
    ※ DCF は週次モジュールなので日次では ① が空のことがある（＝未算出）。
    """
    entry = ((results.get("dcf_valuation") or {}).get("results")
             or {}).get(ticker)
    if isinstance(entry, dict):
        v = _num(entry.get("dcf_bonus"))
        if v is not None:
            return v
    return _candidate_field(ticker, market, results, "dcf_bonus")


def calc_dcf_adj(ticker: str, market: str, results: dict,
                 max_adj: float) -> float | None:
    """DCF調整。ボーナスは既に pt 単位なので上限でクリップするだけ。"""
    b = extract_dcf_bonus(ticker, market, results)
    if b is None:
        return None
    return round(_clamp(b, -max_adj, max_adj), 2)


def extract_geo_bonus(ticker: str, market: str, sector_code: str,
                      results: dict) -> float | None:
    """
    geo_impact_bonus（pt・config GEO_IMPACT_MIN..MAX = -15..15）を取得する。
    ① screener候補の "geo_impact_bonus"（銘柄粒度・将来 screener が持てば最優先）
    ② results["sectors"][業種コード]["geo_impact_bonus"]（base_agent が格納・業種粒度）
    ※ geo_signal.calc_geo_impact_bonus_* は BigQuery を叩くため直接呼ばない。
      US は業種粒度の geo も results に無いため、現状ほぼ未算出になる。
    """
    v = _candidate_field(ticker, market, results, "geo_impact_bonus")
    if v is not None:
        return v
    sec = (results.get("sectors") or {}).get(str(sector_code))
    if isinstance(sec, dict):
        return _num(sec.get("geo_impact_bonus"))
    return None


def calc_geo_adj(ticker: str, market: str, sector_code: str, results: dict,
                 max_adj: float) -> float | None:
    """地政学調整。ボーナスは pt 単位なので上限でクリップするだけ。"""
    b = extract_geo_bonus(ticker, market, sector_code, results)
    if b is None:
        return None
    return round(_clamp(b, -max_adj, max_adj), 2)


def extract_re_bonus(ticker: str, market: str, sector_code: str,
                     results: dict) -> float | None:
    """
    銘柄単位の不動産ボーナス（pt・RE_BONUS_MIN..MAX = -2..3）を取得する。
    ① screener候補の "real_estate_bonus"
    ② results["sectors"][業種コード]["real_estate_by_ticker"][ticker]["bonus"]（base_agent が格納）
    業種の real_estate_bonus（平均）は銘柄の値ではないので使わない。
    """
    v = _candidate_field(ticker, market, results, "real_estate_bonus")
    if v is not None:
        return v
    sec = (results.get("sectors") or {}).get(str(sector_code))
    entry = ((sec or {}).get("real_estate_by_ticker") or {}).get(ticker) if isinstance(sec, dict) else None
    if isinstance(entry, dict):
        return _num(entry.get("bonus"))
    for other in (results.get("sectors") or {}).values():
        entry = ((other or {}).get("real_estate_by_ticker") or {}).get(ticker) if isinstance(other, dict) else None
        if isinstance(entry, dict):
            return _num(entry.get("bonus"))
    return None


def calc_re_adj(ticker: str, market: str, sector_code: str, results: dict,
                max_adj: float) -> float | None:
    """不動産調整。ボーナスは pt 単位なので上限でクリップするだけ。"""
    b = extract_re_bonus(ticker, market, sector_code, results)
    if b is None:
        return None
    return round(_clamp(b, -max_adj, max_adj), 2)


# ── Phase B 適用可否テーブル（二重計上の防止） ──
#
# 【なぜ要るか】base（＝Phase A の写像元）に既に織り込まれている成分を
#   Phase B が再加算すると二重計上になり SSOT が割れる。E10 の目的は
#   「US を JP と同等の粒度に引き上げること」であって JP を膨らませる
#   ことではないため、base に含まれる成分は score_source 単位で除外する。
#
# 【各判定の根拠】
#   jp_screener:
#     tech / dcf … stock_screener.apply_valuation_to_candidate（L1162-1179）が
#       candidate["composite_score"] を value_score 差分として再計算しており、
#       その value_score には dcf_bonus が統合済み。technical_score も
#       同関数が value_weight の分岐に使うほか後段補正で composite に反映済み。
#     geo        … 候補の sector_score が base_agent 由来で、base_agent は
#       業種 composite に geo_impact_bonus を織り込んでいる（base_agent L885-）。
#     → 3成分とも base に含まれるので Phase B は何も加算しない（Phase A と同値）。
#   jp_sector_fallback:
#     geo のみ base_agent が業種 composite に織り込み済み → geo を除外。
#     tech / dcf は業種スコアに一切入っていない → 加算する。
#   us_screener:
#     us_stock_screener._calc_score（L500-）は収益性/成長性/バリュ/財務健全性/
#     アナリスト/モメンタムのみ。後段の補正も insider/pearl/catalyst/revision/
#     contraction 等で、technical_scorer・DCF・geo_impact_bonus はいずれも
#     composite に入っていない（US候補は生の geo_exposures を持つだけ）。
#     → 3成分とも加算する。
#   re（不動産）:
#     base_agent は不動産ボーナスを業種 composite に入れず、銘柄ごとに
#     real_estate_by_ticker へ置くだけ。stock_screener / us_stock_screener も使っていない。
#     → どの score_source でも base に含まれないので加算する。
#
# 【将来 base の計算式を変えたときは必ずここを更新すること】
_PHASE_B_APPLICABLE: dict[str, frozenset] = {
    "jp_screener":        frozenset({"re"}),
    "jp_sector_fallback": frozenset({"tech", "dcf", "re"}),
    "us_screener":        frozenset({"tech", "dcf", "geo", "re"}),
}
_ADJ_KEYS = ("dcf_adj", "tech_adj", "geo_adj", "re_adj")
_SKIP_ALREADY_IN_BASE = "already_in_base"
_SKIP_UNKNOWN_SOURCE = "unknown_score_source"


def calc_phase_b_adjustments(ticker: str, market: str, sector_code: str,
                             results: dict, cfg=None,
                             score_source: str = "") -> dict:
    """
    加算する成分と、除外した成分の理由をまとめて返す。

    Returns:
        {"tech_adj": 3.0,                       # 実際に加算する調整（float）
         "geo_adj_skipped": "already_in_base"}  # 除外した成分の理由（str）
        Phase B 無効（config）なら常に {}（＝Phase A と完全同値）。

    未知の score_source は安全側に倒し、何も加算しない（Phase A 相当）。
    """
    if not getattr(cfg, "UNIFIED_SCORE_PHASE_B_ENABLED", True):
        return {}
    allowed = _PHASE_B_APPLICABLE.get(score_source)
    if allowed is None:
        # 契約外の score_source。base に何が入っているか判らない以上、
        # 加算しないのが安全側（黙って素通しすると二重計上が復活する）。
        return {f"{k}_skipped": _SKIP_UNKNOWN_SOURCE for k in _ADJ_KEYS}

    adj: dict = {}
    for name, key, value in (
        ("dcf", "dcf_adj",
         lambda: calc_dcf_adj(ticker, market, results,
                              _max_adj(cfg, "UNIFIED_SCORE_DCF_MAX_ADJ", 5.0))),
        ("tech", "tech_adj",
         lambda: calc_tech_adj(ticker, market, results,
                               _max_adj(cfg, "UNIFIED_SCORE_TECH_MAX_ADJ", 5.0))),
        ("geo", "geo_adj",
         lambda: calc_geo_adj(ticker, market, str(sector_code), results,
                              _max_adj(cfg, "UNIFIED_SCORE_GEO_MAX_ADJ", 3.0))),
        ("re", "re_adj",
         lambda: calc_re_adj(ticker, market, str(sector_code), results,
                             _max_adj(cfg, "UNIFIED_SCORE_RE_MAX_ADJ", 3.0))),
    ):
        if name not in allowed:
            adj[f"{key}_skipped"] = _SKIP_ALREADY_IN_BASE
            continue
        v = value()
        if v is not None:
            adj[key] = v
    return adj


def normalize_score(raw: float, market: str) -> float:
    """
    市場別レンジを共通 0..100 へ正規化。
    JP業種スコアは -30..100（SPEC_core §5-1）、その他は 0..100 前提。
    """
    if market == "JP" and raw < 0:
        # -30..0 を 0..23 へ線形写像（100点側のスケールは変えない）
        return round(max(0.0, (raw + 30.0) / 130.0 * 100.0), 1)
    return round(min(100.0, max(0.0, raw)), 1)


def calc_composite_score(ticker: str, market: str, sector_code: str,
                         results: dict, cfg=None) -> dict | None:
    """
    1銘柄の共通composite（SPEC_core §8-2）。

        composite = clamp(base + dcf_adj + tech_adj + geo_adj + re_adj, 0, 100)

    base は Phase A の adapter 写像＋正規化。加算するのは
    「base に含まれていない」かつ「results から読める」成分だけ
    （_PHASE_B_APPLICABLE 参照）。加算されない成分はその銘柄で 0 扱いとなり、
    結果として Phase A と同値になる（劣化しないことの保証）。
    出力キー（ticker/market/sector/composite_score/score_source/components）は
    Phase A から変えない（後方互換）。成分は components に追加し、
    二重計上回避のために除外した成分は "<成分>_skipped" として理由を残す。
    """
    adapter = _ADAPTERS.get(market)
    if adapter is None:
        return None
    raw = adapter.extract(ticker, str(sector_code), results)
    if raw is None:
        return None
    base = normalize_score(raw["composite_score"], market)
    comps = {k: v for k, v in raw["components"].items() if v is not None}

    adj = calc_phase_b_adjustments(ticker, market, str(sector_code),
                                   results, cfg, raw["score_source"])
    total, applied = base, False
    for k in _ADJ_KEYS:
        if k in adj:
            comps[k] = adj[k]
            total += adj[k]
            applied = True
        elif f"{k}_skipped" in adj:
            comps[f"{k}_skipped"] = adj[f"{k}_skipped"]

    return {
        "ticker": ticker, "market": market, "sector": str(sector_code),
        "composite_score": round(_clamp(total, 0.0, 100.0), 1),
        # サフィックスは「実際に1成分でも加算された」ときだけ付ける
        "score_source": raw["score_source"] + ("+phaseB" if applied else ""),
        "components": comps,
    }


def build_universe(holdings: list, us_top: list, top_n: int = 50) -> list:
    """
    Phase A対象ユニバース = 保有銘柄 ＋ us_screener上位N（重複除去・順序保持）。
    holdings: [{"ticker","market","sector_code"}] / us_top: [candidate dict]
    Returns: [(ticker, market, sector_code), ...]
    """
    out, seen = [], set()
    for h in holdings:
        tk = h.get("ticker")
        if tk and tk not in seen:
            seen.add(tk)
            out.append((tk, h.get("market", "JP"), str(h.get("sector_code", ""))))
    for c in us_top[:top_n]:
        tk = c.get("ticker")
        if tk and tk not in seen:
            seen.add(tk)
            out.append((tk, "US", str(c.get("sector", ""))))
    return out


# ── I/O層 ────────────────────────────────────────────────────

def _load_holdings(cfg) -> list:
    path = Path(getattr(cfg, "PORTFOLIO_HOLDINGS_PATH",
                        REPO / "data" / "portfolio_holdings.csv"))
    if not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [{"ticker": r.get("ticker"),
                     "market": (r.get("market") or "JP").upper(),
                     "sector_code": r.get("sector_code", "")}
                    for r in csv.DictReader(f) if r.get("ticker")]
    except Exception:
        return []


def run_score_calculator(cfg, results: dict) -> dict:
    """
    日次バッチ入口。保有＋us_screener上位50へ共通compositeを付与。
    Returns: {"scores": {ticker: {...}}, "covered", "missing", "skipped"}
    """
    phase = ("B" if getattr(cfg, "UNIFIED_SCORE_PHASE_B_ENABLED", True)
             else "A")
    holdings = _load_holdings(cfg)
    us_top = ((results.get("us_screener") or {}).get("top_picks")
              or (results.get("us_screener") or {}).get("candidates") or [])
    universe = build_universe(holdings, us_top,
                              int(getattr(cfg, "UNIFIED_SCORE_US_TOP_N", 50)))
    if not universe:
        return {"skipped": True, "scores": {}, "covered": 0, "missing": [],
                "phase": phase,
                "component_coverage": {k: 0 for k in _ADJ_KEYS}}

    scores, missing = {}, []
    coverage = {k: 0 for k in _ADJ_KEYS}
    for ticker, market, sector in universe:
        s = calc_composite_score(ticker, market, sector, results, cfg)
        if s is None:
            missing.append(ticker)
        else:
            scores[ticker] = s
            for k in _ADJ_KEYS:
                if k in s["components"]:
                    coverage[k] += 1
    return {"skipped": False, "scores": scores,
            "covered": len(scores), "missing": missing,
            "phase": phase, "component_coverage": coverage}
