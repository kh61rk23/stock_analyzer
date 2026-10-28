# 一次採点（全上場）の純粋ロジック: 順位化・観点平均・欠損の中立扱い・重み正規化・テーマ熱度・既存部品との共用
from datetime import date

import pytest

import screener_universe as su


class TestPctRank:
    def test_orders_and_scales_to_0_100(self):
        r = su.pct_rank({"a": 1, "b": 2, "c": 3})
        assert r == {"a": 0.0, "b": 50.0, "c": 100.0}

    def test_lower_is_better_reverses(self):
        r = su.pct_rank({"a": 1, "b": 2, "c": 3}, higher_better=False)
        assert r["a"] == 100.0 and r["c"] == 0.0

    def test_ties_get_average_rank(self):
        r = su.pct_rank({"a": 1, "b": 1, "c": 3})
        assert r["a"] == r["b"] == 25.0 and r["c"] == 100.0

    def test_none_and_nan_are_omitted(self):
        r = su.pct_rank({"a": 1, "b": None, "c": float("nan"), "d": 5})
        assert set(r) == {"a", "d"}

    def test_single_value_is_neutral(self):
        assert su.pct_rank({"a": 7}) == {"a": 50.0}

    def test_empty(self):
        assert su.pct_rank({}) == {}


class TestBonusToScore:
    def test_zero_is_neutral_and_saturates(self):
        assert su.bonus_to_score(0, 10) == 50.0
        assert su.bonus_to_score(10, 10) == 100.0
        assert su.bonus_to_score(-30, 10) == 0.0

    def test_missing_or_bad_cap(self):
        assert su.bonus_to_score(None, 10) is None
        assert su.bonus_to_score(3, 0) is None


class TestWeights:
    def test_normalized_to_one(self):
        w = su.normalize_weights({a: 2.0 for a in su.ASPECTS})
        assert sum(w.values()) == pytest.approx(1.0)
        assert w["move"] == pytest.approx(1 / len(su.ASPECTS))

    def test_all_invalid_falls_back_to_equal(self):
        w = su.normalize_weights({"move": -1, "bogus": 5})
        assert all(v == pytest.approx(1 / len(su.ASPECTS)) for v in w.values())

    def test_config_weights_are_valid(self):
        import config
        assert set(config.SCREEN_UNIVERSE_WEIGHTS) == set(su.ASPECTS)
        assert sum(config.SCREEN_UNIVERSE_WEIGHTS.values()) == pytest.approx(1.0)


class TestPriceFeatures:
    def test_short_series_gives_none(self):
        f = su.price_features([100.0])
        assert all(v is None for v in f.values())

    def test_returns_and_distance(self):
        closes = [100.0] * 40 + [110.0] * 5 + [121.0]
        f = su.price_features(closes)
        assert f["ret_1d"] == pytest.approx(0.1)
        assert f["dist_high20"] == pytest.approx(0.0)
        assert f["r1m"] == pytest.approx(0.21)
        assert f["r3m"] is None  # 64本に届かない

    def test_momentum_12_1_needs_253(self):
        closes = [50.0] * 31 + [100.0] * 221 + [110.0]
        assert len(closes) == 253
        f = su.price_features(closes)
        assert f["mom_12_1"] == pytest.approx(100 / 50 - 1)
        assert f["near_high52"] == pytest.approx(1.0)


class TestClassify:
    base = {"closes": [1.0], "last_date": date(2026, 10, 14), "max_d": date(2026, 10, 14), "turnover_20d": 2e7}

    def test_ok(self):
        assert su.classify(self.base, 1e7) is None

    def test_no_price(self):
        assert su.classify({**self.base, "closes": None}, 1e7) == "no_price"

    def test_stale_over_14_days(self):
        assert su.classify({**self.base, "last_date": date(2026, 9, 29)}, 1e7) == "stale"
        assert su.classify({**self.base, "last_date": date(2026, 9, 30)}, 1e7) is None

    def test_low_turnover_but_never_market_cap(self):
        assert su.classify({**self.base, "turnover_20d": 5e6}, 1e7) == "low_turnover"
        assert su.classify({**self.base, "turnover_20d": None}, 1e7) == "low_turnover"
        assert su.classify({**self.base, "market_cap_b": 1.0}, 1e7) is None


class TestThemeValue:
    hot = {"n1": {"combined": 80.0}, "n2": {"combined": 40.0}}

    def test_max_of_relevance_times_heat(self):
        v = su.theme_value([{"node_id": "n1", "relevance": 0.5}, {"node_id": "n2", "relevance": 1.0}], self.hot, [])
        assert v == pytest.approx(40.0)

    def test_leading_only_adds_to_heat_capped(self):
        v = su.theme_value([{"node_id": "n1", "relevance": 1.0}], self.hot, ["n1"], leading_bonus=50.0)
        assert v == pytest.approx(100.0)

    def test_unknown_nodes_give_none(self):
        assert su.theme_value([{"node_id": "zzz", "relevance": 1.0}], self.hot, []) is None
        assert su.theme_value(None, self.hot, []) is None


class TestAspectScores:
    def test_mean_of_available_elements(self):
        assert su.aspect_scores([80.0, 60.0], [70.0]) == (70.0, True)

    def test_no_value_is_neutral_50_and_flagged(self):
        assert su.aspect_scores([], []) == (50.0, False)


def _rec(t, cols=None, scores=None):
    return {"ticker": t, "cols": cols or {}, "scores": scores or {}}


class TestScoreUniverse:
    W = {a: 1.0 for a in su.ASPECTS}

    def test_missing_aspects_are_neutral_and_coverage_recorded(self):
        out = su.score_universe([_rec("A", {"roic": 0.3}), _rec("B", {"roic": 0.1})], self.W)
        a = next(r for r in out if r["ticker"] == "A")
        assert a["aspects"]["quality"] == 100.0
        assert a["aspects"]["growth"] == 50.0
        assert a["coverage"] == pytest.approx(round(1 / len(su.ASPECTS), 3))

    def test_rank_order_and_rank_numbers(self):
        out = su.score_universe([_rec("A", {"roic": 0.1}), _rec("B", {"roic": 0.3}), _rec("C", {"roic": 0.2})], self.W)
        assert [r["ticker"] for r in out] == ["B", "C", "A"]
        assert [r["universe_rank"] for r in out] == [1, 2, 3]

    def test_low_is_better_columns(self):
        out = su.score_universe([_rec("cheap", {"ev_ebitda": 3.0}), _rec("dear", {"ev_ebitda": 30.0})], self.W)
        assert out[0]["ticker"] == "cheap"

    def test_existing_component_scores_blend_with_ranks(self):
        recs = [_rec("A", {"roic": 0.1}, {"quality": [100.0]}), _rec("B", {"roic": 0.3})]
        out = {r["ticker"]: r for r in su.score_universe(recs, self.W)}
        assert out["A"]["aspects"]["quality"] == 50.0   # (rank 0 + component 100) / 2
        assert out["B"]["aspects"]["quality"] == 100.0

    def test_market_cap_is_not_an_input(self):
        small = {**_rec("S", {"roic": 0.2}), "market_cap_b": 5.0}
        big = {**_rec("L", {"roic": 0.2}), "market_cap_b": 50000.0}
        out = su.score_universe([small, big], self.W)
        assert out[0]["universe_score"] == out[1]["universe_score"]

    def test_distribution_has_all_aspects(self):
        out = su.score_universe([_rec("A"), _rec("B")], self.W)
        assert set(su.distribution(out)) == set(su.ASPECTS)


class TestPlanAndBlend:
    def test_group_by_sector_top_n_and_unknown(self):
        recs = [{"ticker": "A", "sector_code": "1016"}, {"ticker": "B", "sector_code": None},
                {"ticker": "C", "sector_code": "1016"}, {"ticker": "D", "sector_code": "1017"}]
        assert su.group_by_sector(recs, 3) == {"1016": ["A", "C"], "DYNAMIC": ["B"]}

    def test_blend_final(self):
        assert su.blend_final(80, 40, 0.5) == 60.0
        assert su.blend_final(None, 40, 0.5) == 40.0
        assert su.blend_final(80, 40, 1.0) == 80.0

    def test_blend_is_clamped(self):
        assert su.blend_final(80, 40, 5) == 80.0


class TestBuildRecords:
    def _cfg(self):
        import config
        return config

    def _row(self, t, **kw):
        base = {"ticker": t, "edinet_code": "E" + t, "name": t, "industry": "電気機器", "market_cap_b": 20.0,
                "closes": [100.0 + i for i in range(80)], "last_date": date(2026, 10, 14),
                "max_d": date(2026, 10, 14), "turnover_20d": 3e7, "vol_1d": 200.0, "vol_avg20": 100.0,
                "accrual_ratio": -0.06, "roic": 0.2, "themes": [{"node_id": "n1", "relevance": 0.8}]}
        base.update(kw)
        return base

    def _ctx(self):
        return {
            "re_by_ticker": {"A.T": {"bonus": 3.0, "sector_code": "1016"}},
            "accruals": {}, "sector_scores": {"1016": 70.0},
            "hotness": {"n1": {"combined": 60.0}}, "leading_only": [],
            "caches": {"edinet": {"A.T": {"edinet_bonus": 10.0}}},
            "bonus_getters": {"edinet": lambda t, c: c[t]["edinet_bonus"]},
            "accrual_fn": lambda acr, mean: ("AAA", 20.0),
            "tech_fn": lambda c, w, h, l: {"score": 62.0},
            "weekly_fn": lambda c: c[4::5],
        }

    def test_excluded_counts_and_existing_components_attached(self):
        rows = [self._row("A.T"), self._row("B.T", turnover_20d=1e6), self._row("C.T", closes=None)]
        recs, excluded = su.build_records(rows, self._cfg(), self._ctx())
        assert excluded == {"low_turnover": 1, "no_price": 1}
        (a,) = recs
        assert a["sector_code"] == "1016" and a["scores"]["technical"] == [62.0]
        assert a["scores"]["asset"] == [100.0]            # re bonus 3.0 / cap 3.0
        assert a["scores"]["quality"] == [100.0, 100.0]   # accrual tier + edinet cache
        assert a["cols"]["vol_ratio"] == 2.0 and a["cols"]["sector_score"] == 70.0
        assert a["cols"]["theme_value"] == pytest.approx(48.0)

    def test_ticker_missing_from_cache_gets_no_element(self):
        rows = [self._row("B.T")]
        recs, _ = su.build_records(rows, self._cfg(), self._ctx())
        assert recs[0]["scores"]["quality"] == [100.0]    # accrual のみ。キャッシュ無しを 0pt 扱いにしない


class TestOutputRows:
    def test_row_shape_matches_table_schema(self):
        scored = su.score_universe([_rec("A"), _rec("B")], {a: 1.0 for a in su.ASPECTS})
        rows = su.output_rows(scored, "2026-10-15", {"A"})
        names = {f.name for f in su._schema()}
        assert set(rows[0]) == names
        assert {r["ticker"]: r["stage2_selected"] for r in rows} == {"A": True, "B": False}


class TestSqlTemplate:
    def test_format_leaves_valid_regex_and_params(self):
        q = su._SQL.format(project="p")
        assert "{4}" in q and "@asof" in q and "@price_days" in q
        assert "trade_date < @asof" in q


class TestSharedWithExistingParts:
    def test_accrual_adjustment_matches_calc_scores_pieces(self):
        import stock_screener as ss
        tier, bonus = ss._calc_accrual_tier(-0.06)
        assert ss.accrual_adjustment(-0.06, None) == (tier, bonus)
        assert ss.accrual_adjustment(-0.06, 0.0) == (tier, bonus + 5.0)
        assert ss.accrual_adjustment(None, 0.0) == ("N/A", 0.0)

    def test_weekly_closes_is_every_fifth(self):
        import technical_scorer as ts
        assert ts.weekly_closes(list(range(12))) == [4, 9]

    def test_filters_do_not_cut_by_market_cap_and_use_turnover(self):
        import config
        import stock_screener as ss
        data = {"per": 10, "pbr": 1, "roe": 0.1, "market_cap_b": 5.0, "avg_volume": 200_000, "current_price": 100.0}
        ok, detail = ss._apply_filters(data, config)
        assert ok and detail["market_cap"].startswith("OK")
        data["avg_volume"] = 50_000   # 売買代金500万円/日 < 1,000万円
        ok, detail = ss._apply_filters(data, config)
        assert not ok and detail["volume"].startswith("NG")

    def test_attach_universe_blends_and_sorts(self):
        import stock_screener as ss
        cands = [{"ticker": "A", "composite_score": 40.0}, {"ticker": "B", "composite_score": 50.0},
                 {"ticker": "Z", "composite_score": 45.0}]
        uni = {"records": [{"ticker": "A", "universe_score": 90.0, "universe_rank": 1, "aspects": {}, "coverage": 1.0},
                           {"ticker": "B", "universe_score": 10.0, "universe_rank": 2, "aspects": {}, "coverage": 1.0}]}
        ss._attach_universe(cands, uni, 0.5)
        assert [c["ticker"] for c in cands] == ["A", "Z", "B"]
        assert cands[0]["final_score"] == 65.0 and cands[1]["final_score"] == 45.0
        fresh = [{"ticker": "A", "composite_score": 40.0}]
        ss._attach_universe(fresh, None, 0.5)
        assert fresh[0]["final_score"] == 40.0


class TestIdioFeatures:
    """銘柄独自の値動き・マッピングに応じた値動きと財務。"""

    @staticmethod
    def _days(n):
        from datetime import date, timedelta
        return [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]

    def test_pure_market_stock_has_zero_idio_and_beta(self):
        import random
        random.seed(1)
        ds = self._days(130)
        m = [100.0]
        for _ in ds[1:]:
            m.append(m[-1] * (1 + random.gauss(0, 0.01)))
        market = dict(zip(ds, m))
        twice = [100.0]
        for a, b in zip(m, m[1:]):
            twice.append(twice[-1] * (1 + 2 * (b / a - 1)))
        out = su.idio_features({"X": (ds, twice)}, market, {}, {})
        assert out["X"]["beta"] == pytest.approx(2.0, abs=1e-6)
        assert out["X"]["idio_vol_60"] == pytest.approx(0.0, abs=1e-9)

    def test_needs_enough_days_and_skips_gaps(self):
        ds = self._days(130)
        market = {d: 100.0 + i for i, d in enumerate(ds)}
        short = su.idio_features({"S": (ds[-50:], [100.0 + i for i in range(50)])}, market, {}, {})
        assert "S" not in short
        gappy = [d for i, d in enumerate(ds) if i % 2 == 0]       # 1日おき＝前の日の値が無い
        assert "G" not in su.idio_features({"G": (gappy, [100.0] * len(gappy))}, market, {}, {})

    def test_vs_theme_and_theme_sales_growth(self):
        import random
        random.seed(2)
        ds = self._days(130)
        m = [100.0]
        for _ in ds[1:]:
            m.append(m[-1] * (1 + random.gauss(0, 0.01)))
        market = dict(zip(ds, m))
        series, themes, sg = {}, {}, {}
        for k in range(6):
            c = [100.0]
            for a, b in zip(m, m[1:]):
                c.append(c[-1] * (1 + (b / a - 1) + random.gauss(0, 0.005)))
            series[f"T{k}"] = (ds, c)
            themes[f"T{k}"] = [("semi", 1.0)]
            sg[f"T{k}"] = 0.1 * k
        out = su.idio_features(series, market, themes, sg)
        avg = sum(out[t]["idio_mom_20"] for t in series) / 6
        assert out["T0"]["vs_theme_idio"] == pytest.approx(out["T0"]["idio_mom_20"] - avg)
        assert out["T3"]["theme_sales_g"] == pytest.approx(0.25)
        few = su.idio_features({t: series[t] for t in ("T0", "T1")}, market, {t: themes[t] for t in ("T0", "T1")}, sg)
        assert "vs_theme_idio" not in few["T0"]                    # 会社が5社未満のノードは使わない

    def test_idio_aspect_prefers_calm_and_lagging(self):
        def rec(t, vol, vs):
            return {"ticker": t, "cols": {"idio_vol_60": vol, "vs_theme_idio": vs}, "scores": {}}
        out = su.score_universe([rec("calm", 0.01, -0.05), rec("wild", 0.05, 0.05)], {"idio": 1.0})
        assert out[0]["ticker"] == "calm" and out[0]["aspects"]["idio"] == 100.0
