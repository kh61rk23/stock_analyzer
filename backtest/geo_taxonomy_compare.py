"""geo_signal の GEO_TAXONOMY_SOURCE（legacy / seed）の地政学ボーナス比較。

入力  : backtest/results/geo_taxonomy_compare_input.json
        （BQ から取った権益・taxonomy 関与度のスナップショット。未登録の銘柄は権益なし）
出力  : backtest/results/geo_taxonomy_compare.json
実行  : python3 backtest/geo_taxonomy_compare.py
BQ には接続しない（スナップショットだけで両方の版を計算する）。
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import geo_signal  # noqa: E402

RES = REPO / "backtest" / "results"


class _FakeBQ:
    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def get_taxonomy_scores_bulk(self, codes):
        self.calls += 1
        return {c: self.scores[c] for c in codes if c in self.scores}


def _sector_of(t, snap, jpx, name2code):
    c = snap["sector"].get(t)
    return c or name2code.get(jpx.get(t.split(".")[0]), "")


def _run(source, snap, tickers, sectors):
    """1つの版で全銘柄のボーナスを計算する。"""
    prev = getattr(config, "GEO_TAXONOMY_SOURCE", "legacy")
    config.GEO_TAXONOMY_SOURCE = source
    geo_signal._TAXONOMY_SCORE_CACHE.clear()
    geo_signal._TAXONOMY_SCORE_FETCHED.clear()
    fake = _FakeBQ(snap["taxonomy_scores"])
    geo_signal._BQ_WRAPPER_CACHE = fake
    geo_signal._CACHE_STAMP = 10 ** 12   # 日次リセットで空にしない
    geo_signal._CACHE_DATE = datetime.date.today().isoformat()
    orig = geo_signal._get_risk_scores_for_countries
    geo_signal._get_risk_scores_for_countries = lambda codes, *_a, **_k: {}
    try:
        geo_signal.prefetch_taxonomy_scores(tickers, config)
        out = {}
        for t in tickers:
            out[t] = geo_signal.calc_geo_impact_bonus_jp(
                t, sectors[t], config,
                prefetched_exposures=snap["exposures"].get(t, []),
            )
        return out, fake.calls
    finally:
        geo_signal._get_risk_scores_for_countries = orig
        geo_signal._BQ_WRAPPER_CACHE = None
        config.GEO_TAXONOMY_SOURCE = prev


def _stats(tks, a, b, with_exposure):
    d = [b[t] - a[t] for t in tks]
    n = len(tks)
    return {
        "銘柄数": n,
        "権益あり": sum(1 for t in tks if t in with_exposure),
        "差の平均": round(sum(d) / n, 4) if n else 0.0,
        "差の絶対値の平均": round(sum(abs(x) for x in d) / n, 4) if n else 0.0,
        "差の最大(絶対値)": round(max((abs(x) for x in d), default=0.0), 4),
        "差が0でない銘柄": sum(1 for x in d if abs(x) > 1e-9),
        "符号が変わった銘柄": [t for t in tks if a[t] * b[t] < 0],
        "0から非0になった銘柄": [t for t in tks if abs(a[t]) < 1e-9 and abs(b[t]) > 1e-9],
        "非0から0になった銘柄": [t for t in tks if abs(a[t]) > 1e-9 and abs(b[t]) < 1e-9],
    }


def main():
    snap = json.loads((RES / "geo_taxonomy_compare_input.json").read_text(encoding="utf-8"))
    jpx = json.loads((REPO / "stock_data/data/jpx_sectors.json").read_text(encoding="utf-8"))["sectors"]
    name2code = {v["name_ja"]: k for k, v in config.JAPAN_SECTORS.items()}
    exp_tk = sorted(snap["exposures"])
    tickers = sorted(set(snap["universe"]) | set(exp_tk))
    sectors = {t: _sector_of(t, snap, jpx, name2code) for t in tickers}
    legacy, _ = _run("legacy", snap, tickers, sectors)
    seed, calls = _run("seed", snap, tickers, sectors)
    with_exp = set(exp_tk)

    grp = {k: [t for t, g in snap["universe"].items() if k in g] for k in ("holding", "candidate")}
    groups = {
        "保有銘柄(紙の口座の全戦略)": grp["holding"],
        "スクリーナー候補(2026-10-19)": grp["candidate"],
        "参考:権益のある全JP銘柄": exp_tk,
    }
    result = {
        "as_of": snap["as_of"],
        "taxonomy_bq_queries_per_version": calls,
        "seed_commodity_nodes": {k: len(v) for k, v in geo_signal._seed_commodity_nodes().items()},
        "groups": {k: _stats(sorted(v), legacy, seed, with_exp) for k, v in groups.items()},
        "per_ticker_with_exposure": {
            t: {"sector": sectors[t], "legacy": legacy[t], "seed": seed[t],
                "diff": round(seed[t] - legacy[t], 4),
                "taxonomy_rows": len(snap["taxonomy_scores"].get(t.split(".")[0], {}))}
            for t in exp_tk
        },
    }
    out = RES / "geo_taxonomy_compare.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
