# ==============================================================
# score_calculator.py — JP/US共通composite計算（Wave R5-2 Phase A / E10）
# ==============================================================
# 【役割】SPEC_core §8-2 の統合方針どおり、市場別に別実装だった
#   composite_score を adapter 経由で共通形式へ統一する第一段階。
#   Phase A の対象 = 保有銘柄 ＋ us_screener 上位50 のみ（要件 §R5-2）。
# 【設計】既存ロジックの「再実装はしない」。JP は screener/sectors、
#   US は us_screener の算出済みスコアを adapter が共通スキーマへ写像する。
#   （スコア式の本体移植は Phase B/C。ここで二重実装すると SSOT が割れる）
# 共通出力スキーマ（1銘柄）:
#   {"ticker","market","sector","composite_score","score_source","components"}
# 使い方:
#   （日次）stock_analyzer_main が run_score_calculator(cfg, results) を呼ぶ
# ==============================================================

from __future__ import annotations

import csv
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
                    isinstance(c.get("composite_score"), (int, float)):
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
                isinstance(sec.get("composite_score"), (int, float)):
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
                        isinstance(c.get("composite_score"), (int, float)):
                    return {"composite_score": float(c["composite_score"]),
                            "score_source": "us_screener",
                            "components": {
                                "sector": c.get("sector"),
                                "value_score": c.get("value_score"),
                                "quality_score": c.get("quality_score"),
                            }}
        return None


_ADAPTERS = {"JP": JpSectorAdapter(), "US": UsSectorAdapter()}


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
                         results: dict) -> dict | None:
    """
    1銘柄の共通composite（Phase A: adapter写像＋正規化のみ）。
    Phase B以降で DCF/technical/geo の共通計算をここへ段階移植する（§8-2）。
    """
    adapter = _ADAPTERS.get(market)
    if adapter is None:
        return None
    raw = adapter.extract(ticker, str(sector_code), results)
    if raw is None:
        return None
    return {
        "ticker": ticker, "market": market, "sector": str(sector_code),
        "composite_score": normalize_score(raw["composite_score"], market),
        "score_source": raw["score_source"],
        "components": {k: v for k, v in raw["components"].items()
                       if v is not None},
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
    holdings = _load_holdings(cfg)
    us_top = ((results.get("us_screener") or {}).get("top_picks")
              or (results.get("us_screener") or {}).get("candidates") or [])
    universe = build_universe(holdings, us_top,
                              int(getattr(cfg, "UNIFIED_SCORE_US_TOP_N", 50)))
    if not universe:
        return {"skipped": True, "scores": {}, "covered": 0, "missing": []}

    scores, missing = {}, []
    for ticker, market, sector in universe:
        s = calc_composite_score(ticker, market, sector, results)
        if s is None:
            missing.append(ticker)
        else:
            scores[ticker] = s
    return {"skipped": False, "scores": scores,
            "covered": len(scores), "missing": missing}
