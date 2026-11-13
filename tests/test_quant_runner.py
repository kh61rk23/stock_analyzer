# ==============================================================
# tests/test_quant_runner.py — quant/runner.py のテスト（合成データ・ネットワーク不使用）
# ==============================================================
# データの取得（_build_data）と戦略の一覧（_families）を差し替え、偽の系統で research → daily の一通りを確かめる。
# 保存の形（NaN なし）・採用なしの文・古い registry の見送り・前回との差の action・同じ日の2回目で追跡が増えないこと・
# 停止と再検証での復帰なし・外部の仮想トレード・CLI の標準出力に数値の列が出ないことを見る。
# ==============================================================
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quant import runner
from quant.types import Instrument, MarketData, SimResult

N = 700
N_EXTRA = 12
IDX = pd.bdate_range("2021-01-04", periods=N + N_EXTRA)


def _inst(sym, cls):
    return Instrument(symbol=sym, asset_class=cls, name=f"名前{sym}", funding="fx", cost_bps=10.0, exec_hint=f"建て方{sym}")


class FakeFamily:
    """日次損益が seed 固定の乱数（設定 mu で平均が違う）で、targets が weights の固定の系統。"""

    model_priced = False
    needs_rate_history = False

    def __init__(self, fid, weights, asset_class="fx", mus=(3.0, 2.8), seed=1):
        self.family_id = fid
        self.asset_class = asset_class
        self.label_ja = f"偽の系統{fid}"
        self.weights = dict(weights)
        self._mus = mus
        self._seed = seed

    def param_grid(self, cfg):
        return [{"mu": m} for m in self._mus]

    def run(self, data, params, cfg, cost_mult=1.0):
        rng = np.random.default_rng(self._seed)
        sd = 0.005
        net = pd.Series(rng.normal(params["mu"] * sd / np.sqrt(252) + 0.0003, sd, len(IDX)), index=IDX)
        net = net - 0.00005 * cost_mult
        z = pd.Series(0.0, index=IDX)
        return SimResult(net=net, gross=net, cost=z, financing=z, turnover=z,
                         leverage=pd.Series(1.0, index=IDX), weights=pd.DataFrame(index=IDX))

    def targets(self, data, params, cfg):
        return [{"symbol": s, "name": f"名前{s}", "weight": w, "exec_hint": f"建て方{s}", "note": f"注{s}"}
                for s, w in self.weights.items()]


def _cfg(tmp_path, **over):
    base = dict(DRIVE_BASE=tmp_path, QUANT_WFO_FOLDS=4, QUANT_WFO_MIN_TRAIN_DAYS=250, QUANT_WFO_EMBARGO_DAYS=5,
                QUANT_BOOT_N=200, QUANT_PBO_BLOCKS=4, QUANT_GATE_MIN_OOS_DAYS=100, QUANT_MONITOR_MIN_DAYS=5,
                QUANT_PROBATION_DAYS=0,          # 既存のテストは採用後の流れを見るので、試用期間は無しにする
                QUANT_PORT_MAX_STRATEGY_WEIGHT=1.0, QUANT_PORT_MAX_CLASS_WEIGHT=1.0, QUANT_ENSEMBLES={})
    base.update(over)
    return SimpleNamespace(**base)


class World:
    """差し替えた取得と系統の状態。end は「今日」の位置（データの行数）。shock は A の急落の日次の収益率。"""

    def __init__(self, fams):
        self.fams = fams
        self.end = N
        self.shock_from = None
        rng = np.random.default_rng(5)
        self.ret = pd.DataFrame(rng.normal(0.0002, 0.004, (len(IDX), 3)), index=IDX, columns=["A", "B", "C"])

    def build(self, cfg, use_cache, today):
        ret = self.ret.copy()
        if self.shock_from is not None:
            ret.iloc[self.shock_from:, 0] = -0.03
        ret = ret.iloc[:self.end]
        close = 100.0 * (1 + ret).cumprod()
        insts = {"A": _inst("A", "fx"), "B": _inst("B", "fx"), "C": _inst("C", "commodity")}
        return MarketData(close=close, returns=ret, rates=pd.DataFrame({"USD": 0.0}, index=ret.index),
                          instruments=insts, quality={"rates_source": "bis", "missing": [], "stale": [], "jumps": [],
                                                      "as_of": ret.index[-1].strftime("%Y-%m-%d")})


@pytest.fixture
def world(monkeypatch):
    w = World([FakeFamily("fa", {"A": 0.6}), FakeFamily("fb", {"A": 0.3, "C": -0.4}, asset_class="commodity", seed=2)])
    monkeypatch.setattr(runner, "_build_data", w.build)
    monkeypatch.setattr(runner, "_families", lambda cfg: w.fams)
    return w


def _today(w):
    return IDX[w.end - 1]


def _qdir(cfg):
    return cfg.DRIVE_BASE / "quant"


def _strict_load(path):
    def bad(x):
        raise ValueError(f"NaN/inf が保存されている: {x}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=bad)


def test_research_then_daily_full_flow(tmp_path, world):
    cfg = _cfg(tmp_path)
    r = runner.run_research(cfg, today=_today(world))
    assert set(r["adopted"]) == {"fa", "fb"} and r["n_candidates"] == 2 and r["n_trials"] == 4
    assert r["portfolio"]["weights"] and "returns" not in r["portfolio"]
    for name in ("registry.json", "oos_returns.json"):
        _strict_load(_qdir(cfg) / name)
    assert (tmp_path / "insights/quant" / f"research_{r['as_of']}.md").exists()
    assert r["report_path"].endswith(".md")
    reg = _strict_load(_qdir(cfg) / "registry.json")
    assert reg["candidates"]["fa"]["status"] == "adopted" and reg["candidates"]["fa"]["checks"]

    d = runner.run_daily(cfg, today=_today(world))
    assert d["status"] == "ok" and set(d["adopted"]) == {"fa", "fb"}
    keys = {"asset_class", "label", "symbol", "name", "side", "weight_pct", "action", "exec_hint", "strategy", "note"}
    assert d["proposals"] and all(keys <= set(p) for p in d["proposals"])
    assert {p["action"] for p in d["proposals"]} == {"新しく建てる"}
    a = [p for p in d["proposals"] if p["symbol"] == "A"]
    assert len(a) == 1 and a[0]["side"] == "買い" and "fa" in a[0]["strategy_ids"] and "fb" in a[0]["strategy_ids"]
    c = [p for p in d["proposals"] if p["symbol"] == "C"][0]
    assert c["side"] == "売り" and c["asset_class"] == "commodity"
    assert d["lines"] == runner.mail.lines(d) and any("注意" in x for x in d["lines"])


def test_proposal_weight_is_product_of_allocation_scale_and_brake(tmp_path, world):
    cfg = _cfg(tmp_path)
    runner.run_research(cfg, today=_today(world))
    reg = _strict_load(_qdir(cfg) / "registry.json")
    p = reg["portfolio"]
    d = runner.run_daily(cfg, today=_today(world))
    exp = (p["weights"]["fa"] * 0.6 + p["weights"]["fb"] * 0.3) * p["scale"] * p["brake_now"] * 100
    got = [x for x in d["proposals"] if x["symbol"] == "A"]
    if exp / 100 < runner.MIN_WEIGHT:
        assert not got
    else:
        assert got[0]["weight_pct"] == pytest.approx(round(exp, 2), abs=0.01)


def test_no_adoption_sentence(tmp_path, world):
    cfg = _cfg(tmp_path, QUANT_GATE_MIN_DSR=1.5)   # DSR だけ届かない＝観察になる
    r = runner.run_research(cfg, today=_today(world))
    assert r["adopted"] == [] and r["watch"]
    d = runner.run_daily(cfg, today=_today(world))
    assert d["status"] == "no_adoption" and not d.get("skipped")
    assert d["watch"] and d["top_reasons"] and "DSR" in d["top_reasons"][0]["label"]
    text = "\n".join(d["lines"])
    assert "採用した戦略なし" in text and f"観察中 {len(d['watch'])}本" in text


def test_daily_skips_without_or_with_old_registry(tmp_path, world):
    cfg = _cfg(tmp_path)
    d = runner.run_daily(cfg, today=_today(world))
    assert d["skipped"] and "まだ無い" in d["reason"]
    runner.run_research(cfg, today=_today(world))
    old = _today(world) + pd.Timedelta(days=30)
    d = runner.run_daily(cfg, today=old)
    assert d["skipped"] and "古い" in d["reason"]
    assert runner.mail.build_alert(d)["title"] == "🔬 検証ラボ（見送り）"


def test_actions_from_previous_positions(tmp_path, world):
    cfg = _cfg(tmp_path)
    runner.run_research(cfg, today=_today(world))
    runner.run_daily(cfg, today=_today(world))
    # 翌営業日：fa は A を倍にし・fb は C を外して B を新しく持つ
    world.end += 1
    world.fams[0].weights = {"A": 1.2}
    world.fams[1].weights = {"A": 0.3, "B": 0.5}
    d = runner.run_daily(cfg, today=_today(world))
    act = {p["symbol"]: p["action"] for p in d["proposals"]}
    assert act["A"] == "増やす" and act["B"] == "新しく建てる" and act["C"] == "閉じる"
    closed = [p for p in d["proposals"] if p["symbol"] == "C"][0]
    assert closed["weight_pct"] == 0.0
    # 少しだけ減らす・ほぼ同じ
    world.end += 1
    world.fams[0].weights = {"A": 0.6}
    d2 = runner.run_daily(cfg, today=_today(world))
    assert {p["symbol"]: p["action"] for p in d2["proposals"]}["A"] == "減らす"
    d3 = runner.run_daily(cfg, today=_today(world))   # 同じ日の2回目は前日の状態との差のまま
    assert {p["symbol"]: p["action"] for p in d3["proposals"]}["A"] == "減らす"


def test_track_appends_each_business_day_once(tmp_path, world):
    cfg = _cfg(tmp_path)
    runner.run_research(cfg, today=_today(world))
    runner.run_daily(cfg, today=_today(world))
    assert not (_qdir(cfg) / "track.jsonl").exists()
    world.end += 3                                     # 3営業日ぶん進む
    runner.run_daily(cfg, today=_today(world))
    rows = [json.loads(x) for x in (_qdir(cfg) / "track.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["date"] for r in rows] == [IDX[i].strftime("%Y-%m-%d") for i in (N, N + 1, N + 2)]
    # 手計算：fa は A に 0.6 → 日次 0.6×A の収益（最初の日だけ建てた費用 0.6×10bps を引く）
    a = world.ret["A"]
    assert rows[1]["returns"]["fa"] == pytest.approx(0.6 * a.iloc[N + 1])
    assert rows[0]["returns"]["fa"] == pytest.approx(0.6 * a.iloc[N] - 0.6 * 10 / 1e4)
    runner.run_daily(cfg, today=_today(world))         # 同じ日の2回目
    assert len((_qdir(cfg) / "track.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_suspend_removes_proposal_and_blocks_readoption(tmp_path, world):
    cfg = _cfg(tmp_path)
    world.fams[1].weights = {"B": 0.3}
    runner.run_research(cfg, today=_today(world))
    runner.run_daily(cfg, today=_today(world))
    world.shock_from = N                               # A が毎日 3% 下がる（fa だけが A を持つ）
    world.end = N + 10
    d = runner.run_daily(cfg, today=_today(world))
    susp = _strict_load(_qdir(cfg) / "suspended.json")
    assert "fa" in susp and "fb" not in susp
    assert {s["id"] for s in d["suspended"]} == {"fa"}
    assert all(p["symbol"] != "A" or p["action"] == "閉じる" for p in d["proposals"])
    assert "停止した戦略" in "\n".join(d["lines"])
    r = runner.run_research(cfg, today=_today(world))
    assert "fa" not in r["adopted"] and "fa" in r["watch"]
    reg = _strict_load(_qdir(cfg) / "registry.json")
    assert any("実績の悪化で停止中" in x for x in reg["candidates"]["fa"]["reasons"])
    # 60日たてば戻れる
    later = _today(world) + pd.Timedelta(days=61)
    r2 = runner.run_research(cfg, today=later)
    assert "fa" in r2["adopted"]


def test_saved_json_has_no_nan_or_numpy(tmp_path):
    p = tmp_path / "x.json"
    obj = {"a": float("nan"), "b": np.float64("inf"), "c": np.int64(3), "d": pd.Timestamp("2026-01-02"),
           "e": [np.nan, (1, 2)], pd.Timestamp("2026-01-03"): np.bool_(True), "f": pd.Series([1.0, np.nan], index=[0, 1])}
    runner._write_json(p, obj)
    back = _strict_load(p)
    assert back["a"] is None and back["b"] is None and back["c"] == 3 and back["d"] == "2026-01-02"
    assert back["e"] == [None, [1, 2]] and back["2026-01-03"] is True and back["f"] == {"0": 1.0, "1": None}
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(TypeError):
        runner._write_json(p, {"x": object()})
    assert _strict_load(p)["c"] == 3 and not list(tmp_path.glob("*.tmp"))   # 失敗しても元のファイルは壊れない


def test_external_paper_accounts(tmp_path, world):
    cfg = _cfg(tmp_path)
    (tmp_path / "paper").mkdir()
    hist = [{"date": d.strftime("%Y-%m-%d"), "equity": 1e6 * (1.001 + 0.0002 * (i % 3)) ** i, "day_pnl": 0.0}
            for i, d in enumerate(IDX[:300])]
    acc = {"alpha": {"cash": 0, "positions": {}, "equity_history": hist},
           "short": {"equity_history": hist[:10]}, "empty": {"equity_history": []}}
    (tmp_path / "paper" / "accounts.json").write_text(json.dumps(acc), encoding="utf-8")
    ext = runner._load_external(cfg, [])
    assert list(ext) == ["paper_alpha"]
    e = ext["paper_alpha"]
    assert e["asset_class"] == "equity" and e["label"] == "既存の仮想トレード：alpha" and len(e["returns"]) == 299
    assert e["returns"].iloc[0] == pytest.approx(hist[1]["equity"] / hist[0]["equity"] - 1)
    r = runner.run_research(cfg, today=_today(world))
    assert r["n_candidates"] == 3 and r["n_trials"] == 5
    errs = []
    assert runner._load_external(_cfg(tmp_path / "none"), errs) == {} and errs == []


def test_research_raises_without_candidates_or_data(tmp_path, world, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(runner, "_families", lambda c: [])
    with pytest.raises(ValueError, match="0 件"):
        runner.run_research(cfg, today=_today(world))

    def boom(*a):
        raise RuntimeError("価格が取れない")
    monkeypatch.setattr(runner, "_build_data", boom)
    with pytest.raises(RuntimeError):
        runner.run_research(cfg)


def test_cli_research_prints_summary_only(tmp_path, world, capsys):
    cfg = _cfg(tmp_path)
    assert runner.main(["research"], cfg=cfg) == 0
    out = capsys.readouterr().out
    assert "採用" in out and "保存先" in out and "fa" not in out   # 戦略の id も出さない
    assert not re.search(r"\d+\.\d+|%", out)           # 金額・率・小数の列を出さない
    assert runner.main(["daily"], cfg=cfg) == 0
    out = capsys.readouterr().out
    assert "提案" in out and not re.search(r"\d+\.\d+|%", out) and "名前A" not in out


def test_cli_failure_exits_1(tmp_path, monkeypatch, capsys):
    def boom(*a):
        raise RuntimeError("価格が取れない")
    monkeypatch.setattr(runner, "_build_data", boom)
    assert runner.main(["research"], cfg=_cfg(tmp_path)) == 1
    out = capsys.readouterr().out
    assert out.startswith("失敗:") and "価格が取れない" in out and len(out.strip().splitlines()) == 1


def test_external_adopted_is_kept_out_of_portfolio_and_proposals(tmp_path, world):
    cfg = _cfg(tmp_path, QUANT_GATE_MIN_FOLD_POSITIVE=0.0)
    world.fams = []
    (tmp_path / "paper").mkdir()
    rng = np.random.default_rng(3)
    eq = 1e6 * np.cumprod(1 + rng.normal(0.003, 0.002, N))
    hist = [{"date": d.strftime("%Y-%m-%d"), "equity": float(v)} for d, v in zip(IDX[:N], eq)]
    (tmp_path / "paper" / "accounts.json").write_text(json.dumps({"alpha": {"equity_history": hist}}), encoding="utf-8")
    r = runner.run_research(cfg, today=_today(world))
    assert r["adopted"] == ["paper_alpha"] and r["portfolio"]["weights"] == {}
    d = runner.run_daily(cfg, today=_today(world))
    assert d["status"] == "no_adoption" and d["external_adopted"] == ["paper_alpha"] and d["proposals"] == []
    text = "\n".join(d["lines"])
    assert "採用 0本" in text and "採用した戦略なし" in text and "paper_alpha" in text


def test_hold_keeps_previous_weight_as_reference(tmp_path, world):
    """帯より小さい変化が続いても、積み重なって帯を超えた日には「増やす」になる。"""
    cfg = _cfg(tmp_path)
    world.fams[1].weights = {"C": -0.4}
    runner.run_research(cfg, today=_today(world))
    runner.run_daily(cfg, today=_today(world))
    acts = []
    for w in (0.65, 0.70):                             # 0.6 → 0.65（+8%）→ 0.70（基準 0.6 から +17%）
        world.end += 1
        world.fams[0].weights = {"A": w}
        d = runner.run_daily(cfg, today=_today(world))
        acts.append({p["symbol"]: p["action"] for p in d["proposals"]}["A"])
    assert acts == ["そのまま", "増やす"]


def test_option_proposal_uses_option_exec_hint(tmp_path, world):
    cfg = _cfg(tmp_path)
    world.fams[0].asset_class = "options"
    world.fams[0].weights = {"SPY": 1.0}
    world.fams[1].weights = {"SPY": 0.5}
    real = world.build

    def build(cfg, use_cache, today):
        d = real(cfg, use_cache, today)
        d.returns["SPY"] = d.returns["A"]
        d.instruments["SPY"] = runner.instruments.load(None)["SPY"]   # symbol で1本＝指数の定義
        return d
    world.build = build
    runner._build_data = build
    runner.run_research(cfg, today=_today(world))
    d = runner.run_daily(cfg, today=_today(world))
    opt = [p for p in d["proposals"] if p["asset_class"] == "options"]
    idx = [p for p in d["proposals"] if p["asset_class"] == "index"]
    assert opt and opt[0]["side"] == "建てる" and "オプション" in opt[0]["exec_hint"]
    assert idx and "オプション" not in idx[0]["exec_hint"]


def test_all_suspended_sentence():
    d = {"as_of": "2026-11-13", "registry_as_of": "2026-11-08", "status": "ok", "adopted": ["a"], "active": [],
         "watch": [], "proposals": [], "portfolio": {"p_profit": {"252": 0.8}},
         "suspended": [{"id": "a", "label": "戦略A", "reason": "悪化", "until": "2027-01-05"}]}
    text = "\n".join(runner.mail.lines(d))
    assert "すべて停止中" in text and "80.0%" not in text and "今日の提案はありません" not in text


def test_ensemble_proposal_and_track_use_members(tmp_path, world):
    """組み合わせだけが採用された日：中の系統の目標を配分で足して提案し、追跡も配分で足す。"""
    cfg = _cfg(tmp_path, QUANT_ENSEMBLES={"ens_x": {"label": "組み合わせX", "members": ("f*",)}},
               QUANT_ENSEMBLE_VOL_LOOKBACK=40)
    runner.run_research(cfg, today=_today(world))
    reg = _strict_load(_qdir(cfg) / "registry.json")
    for fid in ("fa", "fb"):                          # 個別の系統は採用から外し、組み合わせだけを残す
        reg["candidates"][fid]["status"] = "watch"
    ens = reg["candidates"]["ens_x"]
    ens["status"] = "adopted"
    reg["portfolio"] = {"weights": {"ens_x": 1.0}, "scale": 1.0, "brake_now": 1.0, "p_profit": {}, "metrics": {}}
    (_qdir(cfg) / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    m = ens["members"]
    assert set(m) == {"fa", "fb"} and sum(m.values()) == pytest.approx(1.0)
    d = runner.run_daily(cfg, today=_today(world))
    assert d["status"] == "ok" and not d["errors"]
    a = [p for p in d["proposals"] if p["symbol"] == "A"][0]
    assert a["weight_pct"] == pytest.approx(round((0.6 * m["fa"] + 0.3 * m["fb"]) * 100, 2), abs=0.01)
    assert a["strategy_ids"] == ["ens_x"]
    world.end += 1
    runner.run_daily(cfg, today=_today(world))
    row = json.loads((_qdir(cfg) / "track.jsonl").read_text(encoding="utf-8").splitlines()[0])
    ra, rc = world.ret["A"].iloc[N], world.ret["C"].iloc[N]
    wa, wc = 0.6 * m["fa"] + 0.3 * m["fb"], -0.4 * m["fb"]
    assert row["returns"]["ens_x"] == pytest.approx(wa * ra + wc * rc - (abs(wa) + abs(wc)) * 10 / 1e4)


# ── 試用期間（基準を満たした後の前向きの実績を確かめてから採用） ──

def _pairs(n, mu=0.001, sd=0.004, seed=0, start="2015-01-01"):
    import numpy as np
    import pandas as pd
    idx = pd.bdate_range(start, periods=n)
    v = np.random.default_rng(seed).normal(mu, sd, n)
    return [(d.strftime("%Y-%m-%d"), float(x)) for d, x in zip(idx, v)]


def test_probation_waits_until_enough_forward_days():
    from quant import runner
    pairs = _pairs(1000, seed=1)
    p = runner._probation(pairs, pairs[-1][0], None)          # 今日はじめて満たした
    assert p["state"] == "waiting" and p["days"] == 0 and "確かめています" in p["reason"]
    p = runner._probation(pairs, pairs[-31][0], None)         # 30 営業日たった
    assert p["state"] == "waiting" and p["days"] == 30
    p = runner._probation(pairs, pairs[-61][0], None)         # 60 営業日たって、成績もそれまでと同じ分布
    assert p["state"] == "passed" and p["days"] == 60


def test_probation_restarts_when_forward_record_is_bad():
    from quant import runner
    good, bad = _pairs(900, mu=0.001), _pairs(80, mu=-0.006, seed=1, start="2018-06-15")
    p = runner._probation(good + bad, good[-1][0], None)
    assert p["state"] == "restart" and "悪い" in p["reason"]


def test_class_mix_of_ensemble_comes_from_members():
    from quant import runner
    cands = {"a": {"asset_class": "fx"}, "b": {"asset_class": "options"}, "c": {"asset_class": "options"}}
    assert runner._class_mix({"asset_class": "fx"}, cands) == {"fx": 1.0}
    mix = runner._class_mix({"ensemble": True, "members": {"a": 0.5, "b": 0.3, "c": 0.2}}, cands)
    assert mix == {"fx": 0.5, "options": 0.5}


def test_research_holds_eligible_strategy_in_probation(world, tmp_path):
    """全項目を満たしても、試用期間のあいだは採用にしない（観察のまま・理由と残りの日数を残す）。次の回も since を引き継ぐ。"""
    base = runner.run_research(_cfg(tmp_path / "a"), today=_today(world))
    assert base["adopted"], "前提：試用なしなら採用が出る合成データ"
    target = base["adopted"][0]

    cfg = _cfg(tmp_path / "b", QUANT_PROBATION_DAYS=60)
    r = runner.run_research(cfg, today=_today(world))
    assert target not in r["adopted"] and target in r["watch"]
    e = _strict_load(_qdir(cfg) / "registry.json")["candidates"][target]
    assert e["probation"]["state"] == "waiting" and e["probation"]["days"] == 0
    assert any("確かめています" in x for x in e["reasons"])
    since = _strict_load(_qdir(cfg) / "probation.json")[target]
    runner.run_research(cfg, today=_today(world))
    assert _strict_load(_qdir(cfg) / "probation.json")[target] == since
    # 採用が無いので、日次は提案を出さない
    d = runner.run_daily(cfg, today=_today(world))
    assert d["status"] == "no_adoption" and d["proposals"] == []
