# ==============================================================
# quant/runner.py — 検証ラボの入口（週次の検証・日次の提案）と保存
# ==============================================================
# 【役割】run_research＝価格の取得 → 全系統の検証（research）→ 採用の門（gate）→ 組み合わせ（portfolio）→ 保存。
#   run_daily＝保存済みの検証結果から、採用中の戦略の今日の建玉の提案・実績の追跡・見張り（monitor）を行う。
#   発注はしない（提案まで）。BigQuery は使わない。
# 【保存先】S.path(cfg, "QUANT_DIR")：registry.json・oos_returns.json・positions.json・track.jsonl・suspended.json。
#   S.path(cfg, "QUANT_INSIGHT_DIR")：research_<日付>.md。JSON は ensure_ascii=False・indent=1。
#   書き込みは一時ファイルに書いてから置き換える（途中で落ちても壊れたファイルを残さない）。
#   NaN・inf・numpy の型・Timestamp は保存の前に素の値に直す（NaN と inf は null）。
# 【提案の建玉】戦略の重み × 戦略への配分 × 組み合わせの倍率（scale）× 損失ブレーキ（brake_now）。
#   同じ資産クラス・同じ銘柄は足し合わせる（オプションは1セットごとに別の提案）。0.5% 未満は載せない。
#   組み合わせ（ensemble）の戦略は、候補の members（{系統の id: 配分}）で中の系統の目標を足す。
#   「そのまま」の銘柄は、次回の比較の基準を前の量のまま残す（帯より小さい変化の積み重ねを見逃さないため）。
# 【実績の追跡】前回の提案の戦略ごとの重み × 前回の日付より後の各営業日の収益 − 建てたときの売買コストの概算
#   （金利・スワップは含めない）。オプションは理論価格の検証と同じ計算で日次損益を出す。1日1行を track.jsonl に足す。
# 【組み合わせ】既存の仮想トレード（external）は門には通すが、組み合わせと提案には入れない（提案は仮想トレード側）。
#   提案を出せる採用が無い日は status="no_adoption"。
# 【停止】monitor が suspend にした戦略は suspended.json に書き、停止から SUSPEND_DAYS 日は adopted に戻さない。
# 【標準出力】CLI は件数と保存先だけ。戦略の id・個別の金額や保有は出さない（公開リポジトリの Actions のログに出るため）。
# ==============================================================
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quant import gate, instruments, mail, monitor, portfolio, research
from quant import settings as S

SUSPEND_DAYS = 60            # 停止してから提案に戻さない日数（暦日）
MIN_WEIGHT = 0.005           # これ未満の提案は載せない（資金の0.5%）
MIN_EXTERNAL_POINTS = 20     # 既存の仮想トレードの口座を検証に入れる最低の点の数


# ── 保存の道具 ──

def _key(k) -> str:
    if isinstance(k, (pd.Timestamp, datetime, date)):
        return k.strftime("%Y-%m-%d")
    return str(k)


def _clean(o):
    """JSON に書ける素の値にする。NaN・inf は None。扱えない型は例外（黙って文字列にしない）。"""
    if o is None or isinstance(o, (str, bool, np.bool_)):
        return o if o is None or isinstance(o, str) else bool(o)
    if o is pd.NaT or o is pd.NA:
        return None
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return o.strftime("%Y-%m-%d")
    if isinstance(o, dict):
        return {_key(k): _clean(v) for k, v in o.items()}
    if isinstance(o, pd.Series):
        return {_key(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset, np.ndarray)):
        seq = sorted(o, key=str) if isinstance(o, (set, frozenset)) else list(o)
        return [_clean(v) for v in seq]
    raise TypeError(f"保存できない型: {type(o).__name__}")


def _atomic_write(path: Path, text: str) -> None:
    """同じフォルダの一時ファイルに書いてから置き換える。失敗したら一時ファイルを消して例外を返す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _write_json(path: Path, obj) -> None:
    _atomic_write(path, json.dumps(_clean(obj), ensure_ascii=False, indent=1, allow_nan=False) + "\n")


def _read_json(path: Path):
    """無ければ None。壊れていれば（黙らず）例外。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"{path.name} が読めない（JSON が壊れている）: {e}") from e


def _read_track(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as e:
            raise ValueError(f"{path.name} の{i}行目が読めない: {e}") from e
    return rows


def _today(today) -> pd.Timestamp:
    return pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()


def _ds(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


# ── 外とのつなぎ目（テストでここを差し替える） ──

def _build_data(cfg, use_cache, today):
    """価格・金利・品質をまとめた MarketData を作る（ネットワークを使うのはここだけ）。"""
    from quant import data as D
    return D.build(cfg, use_cache=use_cache, today=today)


def _families(cfg) -> list:
    from quant import strategies
    return strategies.all_families(cfg)


# ── 既存の仮想トレード（external） ──

def _load_external(cfg, errors: list) -> dict:
    """paper/accounts.json の各口座の評価額の履歴から日次の損益率を作る。無ければ空。点が少ない口座は入れない。"""
    p = S.base_dir(cfg) / "paper" / "accounts.json"
    if not p.exists():
        return {}
    try:
        accounts = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        errors.append({"where": "external", "error": f"paper/accounts.json が読めない: {e}"})
        return {}
    out = {}
    for name, acc in accounts.items():
        hist = (acc or {}).get("equity_history") if isinstance(acc, dict) else None
        if not hist or len(hist) < MIN_EXTERNAL_POINTS:
            continue
        eq = pd.Series({pd.Timestamp(h["date"]): float(h["equity"]) for h in hist}).sort_index()
        ret = eq.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        if len(ret) < MIN_EXTERNAL_POINTS - 1:
            continue
        out[f"paper_{name}"] = {"label": f"既存の仮想トレード：{name}", "asset_class": "equity", "returns": ret}
    return out


# ── 停止 ──

def _suspension(susp: dict, sid: str, today_ts: pd.Timestamp):
    """停止中ならその記録（until 付き）。停止から SUSPEND_DAYS 日たっていれば None。"""
    e = susp.get(sid)
    if not e:
        return None
    start = pd.Timestamp(e["date"])
    if (today_ts - start).days >= SUSPEND_DAYS:
        return None
    return {**e, "until": _ds(start + pd.Timedelta(days=SUSPEND_DAYS))}


# ── 週次の検証 ──

def run_research(cfg, today=None, use_cache=True) -> dict:
    """価格を取って全系統を検証し、門・組み合わせを通して保存する。候補 0 件・データが取れないは例外。"""
    today_ts = _today(today)
    qdir = S.path(cfg, "QUANT_DIR")
    idir = S.path(cfg, "QUANT_INSIGHT_DIR")
    errors: list = []

    data = _build_data(cfg, use_cache, today)
    external = _load_external(cfg, errors)
    res = research.run(data, cfg, families=_families(cfg), external=external)
    errors += list(res.get("errors") or [])
    cands = res["candidates"]
    # 組み合わせは中身の系統が無いと全部「不採用」で並ぶだけなので、系統か外部の実績が1本も無ければ失敗にする
    if not [c for c in cands if not c.get("ensemble")]:
        raise ValueError("検証の候補が 0 件（戦略の一覧が空）")

    as_of = res.get("as_of") or (data.quality or {}).get("as_of") or _ds(today_ts)
    susp = _read_json(qdir / "suspended.json") or {}
    old_prob = _read_json(qdir / "probation.json") or {}
    prob = {}
    registry_cands = {}
    oos_store = {}
    for c in cands:
        g = gate.evaluate(c, cfg)
        status, reasons = g["status"], list(g["reasons"])
        entry = {k: v for k, v in c.items() if k != "oos_returns"}
        if status == "adopted":
            # 試用：全項目を満たした日（since）より後の損益は、戦略と門の作りを決めた後に生まれた前向きの実績。
            # それが決めた日数たまり、悪化していなければ採用する。項目を1つでも落とした回で数え直す（prob に残さない）
            # 数え始めは、その戦略の検証の損益の最後の日（それより後に増える損益だけが前向きの実績）
            pairs_c = c.get("oos_returns") or []
            last_day = pairs_c[-1][0] if pairs_c else as_of
            since = old_prob.get(c["id"]) or last_day
            p = _probation(pairs_c, since, cfg)
            entry["probation"] = p
            if p["state"] == "restart":
                since = last_day
                entry["probation"] = {**p, "since": since}
            prob[c["id"]] = since
            if p["state"] != "passed":
                status = "watch"
                reasons.append(p["reason"])
        s = _suspension(susp, c["id"], today_ts) if status == "adopted" else None
        if s:
            status = "watch"
            reasons.append(f"実績の悪化で停止中（{s['until']} まで）")
            entry["suspended_until"] = s["until"]
        entry.update(status=status, checks=g["checks"], reasons=reasons)
        registry_cands[c["id"]] = entry
        oos_store[c["id"]] = c.get("oos_returns") or []
    _write_json(qdir / "probation.json", prob)

    # 組み合わせに入れるのは提案を出せる戦略だけ。既存の仮想トレードは提案を仮想トレード側で出すので入れない
    # （入れると、提案に出ない戦略の配分ぶん、利益の確率・倍率が実際に建てる玉とずれる）
    adopted_ids = [i for i, c in registry_cands.items() if c["status"] == "adopted" and not c.get("external")]
    adopted = {}
    for i in adopted_ids:
        pairs = oos_store[i]
        adopted[i] = {"asset_class": registry_cands[i]["asset_class"],
                      "ensemble": bool(registry_cands[i].get("ensemble")),
                      "class_mix": _class_mix(registry_cands[i], registry_cands),
                      "returns": pd.Series([v for _, v in pairs], index=pd.to_datetime([d for d, _ in pairs]))}
    try:
        port = portfolio.build(adopted, cfg)
    except ValueError as e:
        # 組み合わせを作れないなら、採用のまま置かず観察に下げる（理由を残す）
        errors.append({"where": "portfolio", "error": str(e)})
        for i in adopted_ids:
            registry_cands[i]["status"] = "watch"
            registry_cands[i]["reasons"].append(f"組み合わせを作れなかった（{e}）")
        port = portfolio.build({}, cfg)
    port = {k: v for k, v in port.items() if k != "returns"}

    reg = {"as_of": as_of, "created": _ds(today_ts), "data_quality": data.quality, "n_trials": res["n_trials"],
           "candidates": registry_cands, "portfolio": port, "errors": errors}
    _write_json(qdir / "oos_returns.json", oos_store)
    _write_json(qdir / "registry.json", reg)
    report = idir / f"research_{as_of}.md"
    _atomic_write(report, mail.research_markdown(_clean(reg)))

    def ids(st):
        return [i for i, c in registry_cands.items() if c["status"] == st]

    return {"as_of": as_of, "n_trials": res["n_trials"], "n_candidates": len(cands), "adopted": ids("adopted"),
            "watch": ids("watch"), "rejected": ids("rejected"), "portfolio": port,
            "data_quality": data.quality, "report_path": str(report), "errors": errors}


# ── 日次の提案 ──

def _probation(oos_pairs: list, since: str, cfg) -> dict:
    """試用の状態。{"state": "waiting"|"passed"|"restart", "since", "days", "need", "reason"}。

    since より後の検証の損益（前向きの実績）が QUANT_PROBATION_DAYS 営業日に満たなければ waiting。
    たまっていて、それ以前の損益の分布に照らして悪化していなければ passed、悪化していれば restart（数え直し）。
    """
    need = int(S.get(cfg, "QUANT_PROBATION_DAYS"))
    ser = pd.Series([v for _, v in oos_pairs], index=[d for d, _ in oos_pairs], dtype=float)
    fwd, before = ser[ser.index > since], ser[ser.index <= since]
    out = {"since": since, "days": int(len(fwd)), "need": need}
    if len(fwd) < need:
        return {**out, "state": "waiting",
                "reason": f"基準は全て満たした（{since} から）。採用の前に、その後の実績を{need}営業日ぶん確かめています（いま{len(fwd)}日）"}
    chk = monitor.check(fwd, before, cfg)
    if chk["state"] == "suspend":
        return {**out, "state": "restart",
                "reason": f"基準を満たした後の実績が検証より悪い（{chk['reason']}）。確かめ直します"}
    return {**out, "state": "passed", "reason": ""}


def _class_mix(entry: dict, cands: dict) -> dict:
    """戦略の配分が資産クラスごとにどう分かれるか（合計 1）。組み合わせは中身の戦略の資産クラスで分ける。"""
    if not entry.get("ensemble"):
        return {entry.get("asset_class"): 1.0}
    mix: dict = {}
    for mid, w in (entry.get("members") or {}).items():
        cls = (cands.get(mid) or {}).get("asset_class") or "ensemble"
        mix[cls] = mix.get(cls, 0.0) + float(w)
    return mix or {"ensemble": 1.0}


def _top_reasons(cands: dict, n: int = 3) -> list:
    """採用に足りなかった点を、落ちた本数の多い順に。観察があれば観察だけ・無ければ不採用も数える。"""
    pool = [c for c in cands.values() if c.get("status") == "watch"] or \
           [c for c in cands.values() if c.get("status") != "adopted"]
    cnt: Counter = Counter()
    for c in pool:
        labels = [ch.get("label") or ch.get("key") for ch in c.get("checks") or [] if not ch.get("passed")]
        if not labels and c.get("reject_reason"):
            labels = [str(c["reject_reason"]).split("（")[0]]
        cnt.update(set(labels))
    return [{"label": k, "count": v} for k, v in cnt.most_common(n)]


def _watch_top(cands: dict, n: int = 3) -> list:
    """観察中の戦略のうち、確からしさ（DSR）が高い順に n 本。採用の基準までの距離をメールに出すための要約。"""
    pool = [c for c in cands.values() if c.get("status") == "watch" and c.get("metrics")]
    pool.sort(key=lambda c: -(c["metrics"].get("dsr") or 0.0))
    return [{"id": c["id"], "label": c.get("label"), "dsr": c["metrics"].get("dsr"),
             "p_profit_12m": (c["metrics"].get("p_profit") or {}).get("252"),
             "max_drawdown": c["metrics"].get("max_drawdown"),
             "failed": [k["label"] for k in c.get("checks") or [] if not k.get("passed")]} for c in pool[:n]]


def _cost_bps(data, sym) -> float:
    inst = data.instruments.get(sym)
    return float(inst.cost_bps) if inst is not None else 0.0


def _track_new_days(prev: dict, data, new_days, families: dict, cfg, errors: list) -> dict:
    """前回の提案の戦略ごとに、new_days の日次損益 {sid: Series} を出す。

    戦略は members（{系統の id: 中身}）の和。組み合わせでない戦略は自分1本だけの members を持つ。
    weights の中身は組み合わせの中の配分（scale）を掛けた後の重み、sim は理論価格の損益に scale を掛ける。
    """
    out = {}
    for sid, st in (prev.get("strategies") or {}).items():
        total = pd.Series(0.0, index=new_days)
        for mid, e in (st.get("members") or {}).items():
            if e.get("mode") == "sim":
                fam = families.get(mid)
                if fam is None:
                    errors.append({"where": "track", "id": sid, "member": mid, "error": "系統が見つからない"})
                    continue
                total = total + fam.run(data, e["params"], cfg).net.reindex(new_days).fillna(0.0) * float(e.get("scale", 1.0))
                continue
            w = pd.Series(e.get("weights") or {}, dtype=float)
            syms = [s for s in w.index if s in data.returns.columns]
            if syms:
                total = total + data.returns.reindex(new_days)[syms].fillna(0.0) @ w[syms]
            if len(total):
                total.iloc[0] -= float(e.get("cost") or 0.0)
        out[sid] = total
    return out


def _action(prev_w: float, new_w: float, band: float) -> str:
    if abs(new_w) < MIN_WEIGHT:
        return "閉じる"
    if abs(prev_w) < MIN_WEIGHT or prev_w * new_w < 0:
        return "新しく建てる"
    if abs(new_w - prev_w) < band * abs(prev_w):
        return "そのまま"
    return "増やす" if abs(new_w) > abs(prev_w) else "減らす"


def run_daily(cfg, results=None, today=None, use_cache=True) -> dict:
    """保存済みの検証結果から今日の提案を作る。results は今は使わない（呼び出し側の形をそろえるための引数）。

    registry が無い・古いときは {"skipped": True, "reason": …}。採用 0 件は status="no_adoption"。
    """
    today_ts = _today(today)
    qdir = S.path(cfg, "QUANT_DIR")
    reg = _read_json(qdir / "registry.json")
    if reg is None:
        return {"skipped": True, "reason": "検証の結果がまだ無い（週1回の検証の後に出ます）"}
    created = pd.Timestamp(reg.get("created") or reg["as_of"])
    age = (today_ts - created).days
    max_age = int(S.get(cfg, "QUANT_REGISTRY_MAX_AGE_DAYS"))
    if age > max_age:
        return {"skipped": True,
                "reason": f"検証の結果が古い（{_ds(created)} の検証で{age}日前・{max_age}日を超えたので提案を出しません）"}

    data = _build_data(cfg, use_cache, today)
    as_of = (data.quality or {}).get("as_of") or _ds(data.returns.index[-1])
    families = {f.family_id: f for f in _families(cfg)}
    errors: list = []
    cands = reg["candidates"]
    port = reg.get("portfolio") or {}
    oos_all = _read_json(qdir / "oos_returns.json") or {}
    susp = _read_json(qdir / "suspended.json") or {}

    # 前回の提案（同じ日の2回目なら、その日の最初の前の状態を基準にする）
    prev = _read_json(qdir / "positions.json")
    if prev and prev["as_of"] == as_of:
        base_strat, base_agg = prev.get("prev_strategies") or {}, prev.get("prev_agg") or {}
    elif prev:
        base_strat, base_agg = prev.get("strategies") or {}, prev.get("agg") or {}
    else:
        base_strat, base_agg = {}, {}

    # 実績の追跡（前回の日付より後〜今日の営業日を全部。すでに書いた日は足さない）
    track_path = qdir / "track.jsonl"
    track = _read_track(track_path)
    if prev and prev["as_of"] < as_of:
        last_done = max([r["date"] for r in track], default="")
        days = [d for d in data.returns.index if prev["as_of"] < _ds(d) <= as_of and _ds(d) > last_done]
        if days:
            rets = _track_new_days(prev, data, pd.DatetimeIndex(days), families, cfg, errors)
            new_rows = [{"date": _ds(d), "returns": {sid: float(s.loc[d]) for sid, s in rets.items()}} for d in days]
            track += new_rows
            _atomic_write(track_path, "".join(json.dumps(_clean(r), ensure_ascii=False) + "\n" for r in track))

    adopted_ids = [i for i, c in cands.items() if c["status"] == "adopted"]
    watch_ids = [i for i, c in cands.items() if c["status"] == "watch"]
    external_ids = [i for i in adopted_ids if cands[i].get("external")]
    live_ids = [i for i in adopted_ids if i not in external_ids]

    # 見張り：実績が検証より悪ければ停止
    mon = {}
    for sid in live_ids:
        if _suspension(susp, sid, today_ts):
            continue
        since = (susp.get(sid) or {}).get("date", "")
        live = pd.Series({pd.Timestamp(r["date"]): r["returns"][sid] for r in track
                          if sid in r["returns"] and r["date"] > since}, dtype=float)
        pairs = oos_all.get(sid)
        if not pairs:
            errors.append({"where": "monitor", "id": sid, "error": "検証の損益が保存されていない"})
            continue
        oos = pd.Series([v for _, v in pairs], index=pd.to_datetime([d for d, _ in pairs]), dtype=float)
        mon[sid] = monitor.check(live, oos, cfg)
        if mon[sid]["state"] == "suspend":
            susp[sid] = {"date": as_of, "reason": mon[sid]["reason"]}
    if any(m["state"] == "suspend" for m in mon.values()):
        _write_json(qdir / "suspended.json", susp)
    suspended = []
    for sid in live_ids:
        s = _suspension(susp, sid, today_ts)
        if s:
            suspended.append({"id": sid, "label": cands[sid]["label"], "reason": s.get("reason"),
                              "date": s["date"], "until": s["until"]})
    active = [i for i in live_ids if not _suspension(susp, i, today_ts)]

    # 今日の建玉
    alloc = port.get("weights") or {}
    mult = float(port.get("scale") or 0.0) * float(port.get("brake_now", 1.0))
    labels = S.get(cfg, "QUANT_ASSET_CLASS_LABEL")
    # オプションの建て方は options の定義から引く（data.instruments は symbol で1本なので、SPY は指数の定義になる）
    opt_hint = {i.symbol: i.exec_hint for i in instruments.by_class(cfg).get("options", [])}
    strategies, agg = {}, {}
    for sid in active:
        # 組み合わせ（research が付けた members＝{系統の id: 配分}）は中の系統ごとの目標を配分で足す
        if cands[sid].get("ensemble"):
            parts = [(mid, float(mw)) for mid, mw in (cands[sid].get("members") or {}).items()]
            if not parts:
                errors.append({"where": "targets", "id": sid, "error": "組み合わせの中身（members）が無い"})
                continue
        else:
            parts = [(sid, 1.0)]
        a = float(alloc.get(sid, 0.0)) * mult
        old_members = (base_strat.get(sid) or {}).get("members") or {}
        members = {}
        for mid, mw in parts:
            fam = families.get(mid)
            params = (cands.get(mid) or {}).get("current_params")
            if fam is None or params is None:
                errors.append({"where": "targets", "id": sid, "member": mid,
                               "error": "系統が見つからない" if fam is None else "現在の設定が無い"})
                continue
            tg = fam.targets(data, params, cfg)
            is_opt = fam.asset_class == "options"
            wts = {t["symbol"]: float(t["weight"]) * mw for t in tg}
            old = (old_members.get(mid) or {}).get("weights") or {}
            cost = 0.0 if is_opt else sum(abs(wts.get(s, 0.0) - old.get(s, 0.0)) * _cost_bps(data, s) / 1e4
                                          for s in set(wts) | set(old))
            members[mid] = {"weights": wts, "cost": cost, "mode": "sim" if is_opt else "weights",
                            "params": params, "scale": mw}
            for t in tg:
                inst = data.instruments.get(t["symbol"])
                cls = "options" if is_opt else (inst.asset_class if inst else fam.asset_class)
                # オプションは1セットごとに別の提案（同じ系統なら、別の戦略から来ても同じセット）
                k = f"{cls}|{t['symbol']}|{mid}" if is_opt else f"{cls}|{t['symbol']}"
                hint = (opt_hint.get(t["symbol"]) if is_opt else None) or t.get("exec_hint") or ""
                e = agg.setdefault(k, {"weight": 0.0, "asset_class": cls, "label": labels.get(cls, cls),
                                       "symbol": t["symbol"], "name": t.get("name") or t["symbol"],
                                       "exec_hint": hint, "strategy_ids": [],
                                       "strategy": [], "notes": []})
                e["weight"] += float(t["weight"]) * mw * a
                if sid not in e["strategy_ids"]:
                    e["strategy_ids"].append(sid)
                    e["strategy"].append(cands[sid]["label"])
                if t.get("note") and t["note"] not in e["notes"]:
                    e["notes"].append(t["note"])
        strategies[sid] = {"members": members}
    agg = {k: v for k, v in agg.items() if abs(v["weight"]) >= MIN_WEIGHT}

    band = float(S.get(cfg, "QUANT_REBALANCE_BAND"))
    proposals = []
    held = {}     # 次回の比較の基準＝提案どおりに動かした後の建玉
    for k, e in agg.items():
        w = e["weight"]
        prev_w = float((base_agg.get(k) or {}).get("weight", 0.0))
        action = _action(prev_w, w, band)
        # 「そのまま」は玉を動かさないので、基準も前の量のまま残す（新しい目標を基準にすると、
        # 帯より小さい変化が毎日積み重なっても一度も「増やす・減らす」にならない）
        held[k] = {**e, "weight": prev_w} if action == "そのまま" else e
        side = "建てる" if e["asset_class"] == "options" else ("買い" if w > 0 else "売り")
        proposals.append({"asset_class": e["asset_class"], "label": e["label"], "symbol": e["symbol"],
                          "name": e["name"], "side": side, "weight_pct": round(abs(w) * 100, 2),
                          "action": action,
                          "exec_hint": e["exec_hint"], "strategy": "／".join(dict.fromkeys(e["strategy"])),
                          "strategy_ids": e["strategy_ids"], "note": "／".join(e["notes"])})
    for k, b in base_agg.items():
        if k not in agg:
            side = "建てる" if b["asset_class"] == "options" else ("買い" if b["weight"] > 0 else "売り")
            proposals.append({"asset_class": b["asset_class"], "label": b["label"], "symbol": b["symbol"],
                              "name": b["name"], "side": side, "weight_pct": 0.0, "action": "閉じる",
                              "exec_hint": b.get("exec_hint", ""), "strategy": "／".join(dict.fromkeys(b["strategy"])),
                              "strategy_ids": b["strategy_ids"], "note": ""})
    order = {c: i for i, c in enumerate(labels)}
    proposals.sort(key=lambda p: (order.get(p["asset_class"], 99), -p["weight_pct"], p["symbol"]))

    _write_json(qdir / "positions.json", {"as_of": as_of, "strategies": strategies, "agg": held,
                                          "prev_strategies": base_strat, "prev_agg": base_agg})

    # 提案を出せる採用（既存の仮想トレードを除く）が無ければ「採用なし」
    no_adoption = not live_ids
    daily = {"as_of": as_of, "registry_as_of": reg["as_of"],
             "status": "no_adoption" if no_adoption else "ok",
             "adopted": adopted_ids, "active": active, "watch": watch_ids, "external_adopted": external_ids,
             "proposals": proposals, "portfolio": port, "monitor": mon, "suspended": suspended,
             "top_reasons": _top_reasons(cands) if no_adoption else [],
             "watch_top": _watch_top(cands),
             "data_quality": data.quality, "errors": errors}
    daily["lines"] = mail.lines(daily)
    return daily


# ── CLI ──

def _summary_research(r: dict) -> list:
    return [f"検証: 候補 {r['n_candidates']}件・試した設定 {r['n_trials']}件・採用 {len(r['adopted'])}件"
            f"・観察 {len(r['watch'])}件・不採用 {len(r['rejected'])}件",
            f"保存先: {r['report_path']}"]


def _summary_daily(d: dict) -> list:
    if d.get("skipped"):
        return [f"見送り: {d['reason']}"]
    return [f"提案: {len(d['proposals'])}件・採用 {len(d['adopted'])}件・観察 {len(d['watch'])}件"
            f"・停止 {len(d['suspended'])}件（状態: {d['status']}）"]


def main(argv=None, cfg=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="quant.runner")
    ap.add_argument("command", choices=["research", "daily"])
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)
    if cfg is None:
        import config as cfg
    try:
        if args.command == "research":
            out = _summary_research(run_research(cfg, use_cache=not args.no_cache))
        else:
            out = _summary_daily(run_daily(cfg, use_cache=not args.no_cache))
    except Exception as e:  # CLI の最上位：理由を1行出して exit 1 にするため
        msg = str(e).strip().splitlines()[0][:200] if str(e).strip() else ""
        print(f"失敗: {type(e).__name__}: {msg}")
        return 1
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
