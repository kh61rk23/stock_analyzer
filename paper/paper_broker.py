# ==============================================================
# paper/paper_broker.py — 仮想口座ブローカー（S2）
# ==============================================================
# 日次処理（stock_analyzer_main の日次末尾ステージから呼ばれる・通知より前）:
#   1) 約定待ちの注文を約定日の始値（BQ analytics.price_daily_latest）で仮想約定（共通スキーマ② = backtest/cost_model 共用）
#   2) BQ の最新終値で評価 → results["paper_summary"]（戦略別の評価額・前日比・開始来・約定・新規注文）
#   3) strategy_adapters で注文を生成し約定待ちへ（約定の時点は下の「約定の時点」）
# 全イベントは events.jsonl に追記され、口座はイベントから再構築可能
# （rebuild_accounts_from_events / 受入条件）。
# ==============================================================

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backtest.cost_model import commission, fill_price, is_jp_ticker   # noqa: E402


# ── 永続化 ───────────────────────────────────────────────────

def _paper_dir(cfg) -> Path:
    p = getattr(cfg, "PAPER_LEDGER_PATH", "stock_data/paper")
    d = Path(p)
    if not d.is_absolute():
        base = Path(getattr(cfg, "DRIVE_BASE", "."))
        # Actions では DRIVE_BASE 自体が <workspace>/stock_data のため、
        # base / "stock_data/paper" だと stock_data/stock_data/paper になり、
        # コミット対象（git add stock_data/paper/）の外に書いてしまう
        # （口座と注文が毎回消える）。
        rel = d.parts[1:] if (d.parts and d.parts[0] == "stock_data"
                              and base.name == "stock_data") else d.parts
        cand = base.joinpath(*rel)
        d = cand if base.exists() else Path(p)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_accounts(cfg) -> dict:
    p = _paper_dir(cfg) / "accounts.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_accounts(cfg, accounts: dict) -> None:
    p = _paper_dir(cfg) / "accounts.json"
    p.write_text(json.dumps(accounts, ensure_ascii=False, indent=1),
                 encoding="utf-8")


def append_event(cfg, event: dict) -> None:
    p = _paper_dir(cfg) / "events.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_events(cfg) -> list:
    p = _paper_dir(cfg) / "events.jsonl"
    if not p.exists():
        return []
    events = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return events


# ── イベントソーシング（受入条件: 再構築テスト） ──────────────

def rebuild_accounts_from_events(events: list) -> dict:
    """
    events.jsonl の内容から口座状態（cash/positions/equity_history）を再構築する。
    イベント種別:
      init : {type, strategy, cash}
      fill : {type, strategy, ticker, side, qty, price, fee}
      mark : {type, strategy, date, equity, day_pnl}
    """
    accounts: dict = {}
    for ev in events:
        s = ev.get("strategy")
        if not s:
            continue
        t = ev.get("type")
        if t == "init":
            accounts[s] = {"cash": float(ev["cash"]), "positions": {},
                           "equity_history": []}
        elif t == "fill" and s in accounts:
            acc = accounts[s]
            qty, price = float(ev["qty"]), float(ev["price"])
            fee = float(ev.get("fee", 0))
            pos = acc["positions"].get(ev["ticker"],
                                       {"qty": 0.0, "avg_cost": 0.0})
            if ev["side"] == "BUY":
                acc["cash"] -= price * qty + fee
                total = pos["qty"] + qty
                pos["avg_cost"] = ((pos["avg_cost"] * pos["qty"] + price * qty)
                                   / total) if total > 0 else 0.0
                pos["qty"] = total
                acc["positions"][ev["ticker"]] = pos
            else:
                sell = min(qty, pos["qty"])
                acc["cash"] += price * sell - fee
                pos["qty"] -= sell
                if pos["qty"] <= 0:
                    acc["positions"].pop(ev["ticker"], None)
                else:
                    acc["positions"][ev["ticker"]] = pos
        elif t == "mark" and s in accounts:
            accounts[s]["equity_history"].append(
                {"date": ev["date"], "equity": float(ev["equity"]),
                 "day_pnl": float(ev.get("day_pnl", 0))})
    return accounts


# ── 約定の時点（純関数） ─────────────────────────────────────
# 注文は朝の分析で生まれる。その時点から現実に出せる最初の成行で約定したことにする。
#   日本株: 09:00 JST より前に生まれた注文はその日の寄付、09:00 以降は翌営業日の寄付（寄付成行）
#   米国株: 22:00 JST より前ならその日（日本の日付と同じ日付）の米国の寄付、以降は翌営業日の寄付
# 「その日以降で BQ に始値がある最初の取引日」を約定日にするので、土日・祝日は自動で次の営業日になる。
# 価格は analytics.price_daily_latest。実行日（JST）当日の行は場中に書かれた途中の値のことがあるため使わず、
# 前日までの確定した行だけを読む。だから約定は「約定日の翌朝の実行」で付く（それまでは約定待ち）。

JST = timezone(timedelta(hours=9))
JP_CUTOFF_HOUR = 9
US_CUTOFF_HOUR = 22


def market_of(ticker: str) -> str:
    return "JP" if is_jp_ticker(ticker) else "US"


def session_label(ticker: str) -> str:
    return "東証の寄付" if market_of(ticker) == "JP" else "米国の寄付"


def eligible_date(ticker: str, created_at: datetime) -> str:
    """注文が出せる最初の日（この日以降で始値のある最初の取引日に約定する）。created_at は JST。"""
    cut = JP_CUTOFF_HOUR if market_of(ticker) == "JP" else US_CUTOFF_HOUR
    d = created_at.date()
    if created_at.hour >= cut:
        d += timedelta(days=1)
    return d.isoformat()


def fill_bar(bars: list, eligible: str, before: str):
    """bars=[(日付, 始値, 終値)]。eligible 以降・before より前で始値のある最初の行。無ければ None。"""
    for d, o, c in sorted(bars or []):
        if eligible <= d < before and o and o > 0:
            return d, float(o), float(c or o)
    return None


def last_close(bars: list, before: str):
    """before より前の最後の終値 (日付, 終値)。無ければ None。"""
    rows = [(d, c) for d, o, c in sorted(bars or []) if d < before and c and c > 0]
    return (rows[-1][0], float(rows[-1][1])) if rows else None


SPLIT_RATIOS = (2, 3, 4, 5, 10, 20, 25, 50)


def _int_ratio(r: float) -> float:
    """日をまたいだ終値の比が分割・併合の比率（SPLIT_RATIOS）かその逆数に近ければその倍率、そうでなければ 1。

    分割・併合の日にも株価そのものが動くので、比はちょうどの整数にならない（10:1 の併合で -6% 動けば 9.4）。
    倍率が5以上なら ±12% まで、2〜4倍は ±6% まで許す（2倍は値動きと区別しにくいので狭くする）。
    """
    if not r or r <= 0:
        return 1.0
    big = r if r >= 1 else 1 / r
    # 実際の分割・併合の比率はほぼこの中にある（9倍・7倍などは無い）。いちばん近い比率に寄せる
    n = min(SPLIT_RATIOS, key=lambda k: abs(big - k) / k)
    tol = 0.12 if n >= 5 else 0.06
    if abs(big - n) / n < tol:
        return float(n) if r >= 1 else 1.0 / n
    return 1.0


def split_factor(rows: list, after: str, upto: str) -> float:
    """after より後・upto より前の日の終値の跳ねのうち、株式分割・併合とみられるもの（整数倍）の積（純関数）。

    analytics.price_daily_latest は日付ごとに最後に取り込んだ行を採るので、分割・併合の前後で未調整の値と
    調整済みの値が日によって入れ替わることがある（10倍と1/10が交互に出る）。跳ねを全部掛け合わせれば、
    その打ち消し分を除いた正味の倍率になる。1 なら分割・併合なし。
    """
    f, prev = 1.0, None
    for d, _o, c in sorted(rows or []):
        if not c or c <= 0:
            continue
        if prev is not None and after < d < upto:
            f *= _int_ratio(c / prev)
        prev = c
    return f


def lot_size(ticker: str) -> int:
    """売買単位。日本株は100株（単元）、米国株は1株。"""
    return 100 if is_jp_ticker(ticker) else 1


def lot_qty(ticker: str, raw_qty: float, price: float, equity: float, max_mult: float = 3.0,
            max_pct: float = 0.20) -> int:
    """目標の株数を売買単位に切り下げる（純関数）。

    切り下げて0になるときは、1単位の金額が目標額の max_mult 倍以内かつ口座の max_pct 以内なら1単位買い、
    そうでなければ買わない（0）。単元未満の端株で約定させると、実際には出せない注文の成績になる。
    """
    lot = lot_size(ticker)
    q = int(raw_qty // lot) * lot
    if q > 0:
        return q
    one = lot * price
    target = raw_qty * price
    if raw_qty > 0 and one <= target * max_mult and one <= equity * max_pct:
        return lot
    return 0


def fetch_bars(tickers, start: str, end_exclusive: str) -> dict:
    """BQ から銘柄の始値・終値を1クエリで読む。{ticker: [(日付, 始値, 終値)]}。
    price_daily は trade_date でパーティションされ、ビューを通しても日付の絞り込みが効く（2週間・数銘柄で 4MB 前後）。"""
    tickers = sorted({t for t in tickers if t})
    if not tickers:
        return {}
    from google.cloud import bigquery
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    sql = f"""
      SELECT ticker, trade_date, open, close
      FROM `{client.project}.analytics.price_daily_latest`
      WHERE trade_date >= @start AND trade_date < @end AND ticker IN UNNEST(@tickers)
    """
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("start", "DATE", start),
        bigquery.ScalarQueryParameter("end", "DATE", end_exclusive),
        bigquery.ArrayQueryParameter("tickers", "STRING", tickers)]))
    out: dict = {}
    for r in job.result():
        out.setdefault(r.ticker, []).append(
            (r.trade_date.isoformat(), float(r.open) if r.open is not None else None,
             float(r.close) if r.close is not None else None))
    return out


PLAYBOOK_EXIT_WORDS = (("time_exit", "保有期限に達した"), ("take_profit", "利確"), ("stop_loss", "損切り"))


def reason_of(sid: str, od: dict) -> dict:
    """注文の理由。adapter が付けていなければ（プレイブック戦略など）タグから「どの戦略のどの条件か」を組み立てる。"""
    r = od.get("reason")
    if isinstance(r, dict) and r.get("text"):
        return r
    tag = od.get("tag") or ""
    if od.get("side") == "SELL":
        why = next((w for k, w in PLAYBOOK_EXIT_WORDS if k in tag), "売りの条件に合致")
        return {"text": f"{sid}：{why}（{tag}）" if tag else f"{sid}：{why}"}
    return {"text": f"{sid}：イベント戦略の買い条件に合致（{tag}）" if tag else f"{sid}：買いのシグナル"}


def timing_text(od: dict, fill_date: str | None = None, price_kind: str = "open") -> str:
    """「いつのシグナルで、いつのどの値で付けるか」の1行。"""
    sig = (od.get("created_at") or od.get("created") or "")[:16].replace("T", " ")
    kind = "始値" if price_kind == "open" else "終値"
    when = fill_date or od.get("eligible") or ""
    return f"{sig} のシグナル → {when} の{od.get('session') or session_label(od['ticker'])}の{kind}で約定" + ("" if fill_date else "予定")


def _mtd_pnl(history: list, equity: float, today: str) -> float:
    month = today[:7]
    base = None
    for h in history:
        if h["date"][:7] == month:
            base = h["equity"]
            break
    return equity - base if base is not None else 0.0


def _now_jst(today: str | None, now: datetime | None) -> datetime:
    if now is not None:
        return now if now.tzinfo else now.replace(tzinfo=JST)
    real = datetime.now(JST)
    if today and today != real.date().isoformat():
        # 日付だけ渡されたとき（テスト・再計算）は、日次の分析が寄付のあとに終わる普段の時刻とみなす
        return datetime.fromisoformat(today).replace(hour=10, tzinfo=JST)
    return real


def run_paper_trading(cfg, results: dict, bars: dict = None,
                      adapters_override: dict = None,
                      today: str = None, now: datetime = None) -> dict:
    """
    メインエントリ（stock_analyzer_main から呼ばれる）。
    1) 約定待ちの注文を、約定日の始値（BQ）×(1±スリッページ)で約定する。始値がまだ無ければ待ち、
       PAPER_ORDER_EXPIRE_DAYS 日を過ぎても無ければ失効
    2) 保有を BQ の最新終値で評価する（終値が無い銘柄は前回の終値→取得単価）
    3) adapter で新しい注文を作り、約定待ちに足す（同じ銘柄・同じ売買の注文が待っていれば足さない）
    bars はテスト用（{ticker: [(日付, 始値, 終値)]}）。省略時は BQ を1クエリだけ読む。
    Returns: {strategy_id: {...}, "skipped": False, "price": {...}}
    """
    strategies = list(getattr(cfg, "PAPER_STRATEGIES",
                              ["screener_topN", "ignition", "pead"]))
    initial = float(getattr(cfg, "PAPER_INITIAL_CAPITAL", 2_000_000))
    expire_days = int(getattr(cfg, "PAPER_ORDER_EXPIRE_DAYS", 10))
    now = _now_jst(today, now)
    today = today or now.date().isoformat()

    if adapters_override is None:
        from paper import strategy_adapters as _sa
        # R4-1: 組込adapter＋playbooks/*.yaml のイベントドリブン戦略を統合
        adapters = _sa.get_all_adapters(cfg)
    else:
        adapters = adapters_override

    # ── playbook 戦略の自動編入 ────────────────────
    # get_all_adapters() は playbooks/*.yaml（status=paper かつ side=BUY）を
    # adapter として登録するが、PAPER_STRATEGIES に id が無いと下のループに
    # 入らず、口座が作られない。
    # プレイブックは「YAMLを置けば増える」のが設計意図なので、config への
    # 追記を都度要求せず、登録済み adapter を自動で対象に加える。
    # 明示的に外したい場合は PAPER_INCLUDE_PLAYBOOK_STRATEGIES=False にする。
    if (adapters_override is None
            and getattr(cfg, "PAPER_INCLUDE_PLAYBOOK_STRATEGIES", True)):
        builtin = set(getattr(_sa, "ADAPTERS", {}))
        for sid in adapters:
            if sid not in builtin and sid not in strategies:
                strategies.append(sid)

    accounts = load_accounts(cfg)
    pending_all = accounts.get("_pending", {})
    summary: dict = {}

    # 約定の時点を持たない注文（この形より前に積まれた約定待ち）は、口座の最後の記帳日の朝の分析で生まれたとみなす
    for sid, ods in pending_all.items():
        hist = (accounts.get(sid) or {}).get("equity_history") or []
        born = hist[-1]["date"] if hist else (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        for od in ods or []:
            if not od.get("eligible"):
                od.setdefault("created", born)
                od["eligible"] = eligible_date(od["ticker"], datetime.fromisoformat(od["created"]).replace(hour=10, tzinfo=JST))

    # ── 価格（BQ を1クエリ）──────────────────────
    price_info = {"source": "bq", "error": None, "asof": None, "expire_days": expire_days}
    if bars is None:
        need = {od["ticker"] for ods in pending_all.values() for od in ods or []}
        for sid in strategies:
            need |= set(((accounts.get(sid) or {}).get("positions") or {}))
        starts = [od["eligible"] for ods in pending_all.values() for od in ods or []]
        # 分割・併合の確認のため、保有の前回確認日（無ければ取得日）からの値も読む（最長 PAPER_SPLIT_LOOKBACK_DAYS 日）
        floor = (date.fromisoformat(today) - timedelta(days=int(getattr(cfg, "PAPER_SPLIT_LOOKBACK_DAYS", 45)))).isoformat()
        for sid in strategies:
            for p in ((accounts.get(sid) or {}).get("positions") or {}).values():
                since = p.get("split_checked") or p.get("entry_date")
                if since:
                    starts.append(max(str(since), floor))
        start = min(starts + [(date.fromisoformat(today) - timedelta(days=14)).isoformat()])
        try:
            bars = fetch_bars(need, start, today)
        except Exception as e:
            # 失敗しても1銘柄ずつ引き直さない（本数がそのまま費用になる）。約定は翌日に回し、評価は前回の終値で付ける
            price_info["error"] = f"{type(e).__name__}: {str(e)[:80]}"
            print(f"    ⚠️ [paper] BQ の価格を読めない（約定は次回へ・評価は前回の終値）: {price_info['error']}")
            bars = {}
    else:
        price_info["source"] = "given"
    dates = [d for rows in bars.values() for d, _, _ in rows if d < today]
    price_info["asof"] = max(dates) if dates else None

    for sid in strategies:
        acc = accounts.get(sid)
        if acc is None:
            acc = {"cash": initial, "positions": {}, "equity_history": [],
                   "meta": {}}
            accounts[sid] = acc
            append_event(cfg, {"type": "init", "strategy": sid, "cash": initial,
                               "date": today})

        # 同日再実行の冪等ガード（同じ日のmarkが既にあれば約定・発注をスキップ）
        already = any(h["date"] == today for h in acc["equity_history"])

        def mark_of(t, p):
            lc = last_close(bars.get(t), today)
            if lc:
                p["last_close"], p["last_close_date"] = lc[1], lc[0]
                return lc[1]
            return p.get("last_close") or p["avg_cost"]   # 取れない→前回の終値→簿価

        fills, expired, waiting = [], [], []
        # 0) 株式分割・併合：保有の株数と取得単価を、前回確認した日より後の終値の整数倍の跳ねで直す
        if not already and price_info["asof"]:
            for t, p in list(acc["positions"].items()):
                rows = bars.get(t)
                if not rows:
                    continue
                since = str(p.get("split_checked") or p.get("entry_date") or "")
                f = split_factor(rows, since, today)
                if f != 1.0:
                    p["qty"] = p["qty"] / f
                    p["avg_cost"] = p["avg_cost"] * f
                    append_event(cfg, {"type": "split_adjust", "strategy": sid, "date": today, "ticker": t,
                                       "factor": f, "qty": round(p["qty"], 4), "avg_cost": round(p["avg_cost"], 4),
                                       "since": since})
                    print(f"    🔀 [paper:{sid}] {t} 分割・併合を反映（価格×{f:g} → 株数÷{f:g}）")
                last_bar = max((d for d, _o, c in rows if d < today and c), default=None)
                if last_bar:
                    p["split_checked"] = last_bar
        if not already:
            # 1) 約定待ちの仮想約定（約定日の古い順・同じ日は売りを先に＝売った現金で買える）
            queue = []
            for od in pending_all.get(sid, []) or []:
                fb = fill_bar(bars.get(od["ticker"]), od["eligible"], today)
                if fb:
                    queue.append((fb, od))
                elif (date.fromisoformat(today) - date.fromisoformat(od["eligible"])).days > expire_days:
                    expired.append(od)
                    append_event(cfg, {"type": "expire", "strategy": sid, "date": today, "reason": reason_of(sid, od),
                                       "ticker": od["ticker"], "side": od["side"],
                                       "created": od.get("created"), "eligible": od["eligible"],
                                       "tag": od.get("tag", "")})
                else:
                    waiting.append(od)
            queue.sort(key=lambda x: (x[0][0], x[1]["side"] != "SELL"))
            for (bar_date, open_px, _close), od in queue:
                qty = od.get("qty")
                if qty is None:
                    eq_est = acc["cash"] + sum(mark_of(t, p) * p["qty"]
                                               for t, p in acc["positions"].items())
                    raw_q = max(0.0, od.get("weight", 0)) * eq_est / open_px if open_px > 0 else 0
                    qty = lot_qty(od["ticker"], raw_q, open_px, eq_est,
                                  float(getattr(cfg, "PAPER_LOT_MAX_MULT", 3.0)),
                                  float(getattr(cfg, "PAPER_LOT_MAX_PCT", 0.20)))
                    if qty <= 0:
                        append_event(cfg, {"type": "skip", "strategy": sid, "date": today, "ticker": od["ticker"],
                                           "side": od["side"], "reason": "単元（100株）の金額が目標額・口座の上限を超える",
                                           "open": round(open_px, 4), "target_yen": round(raw_q * open_px, 0)})
                        continue
                qty = float(qty)
                if qty <= 0:
                    continue
                price = fill_price(open_px, od["side"], od["ticker"], cfg)
                fee = commission(price * qty, cfg)
                pos = acc["positions"].get(od["ticker"],
                                           {"qty": 0.0, "avg_cost": 0.0,
                                            "entry_date": bar_date})
                if od["side"] == "BUY":
                    cost = price * qty + fee
                    if cost > acc["cash"]:
                        lot = lot_size(od["ticker"])
                        qty = int((acc["cash"] - fee) / price / lot) * lot if price > 0 else 0
                        if qty <= 0:
                            continue
                        cost = price * qty + fee
                    acc["cash"] -= cost
                    total = pos["qty"] + qty
                    pos["avg_cost"] = (pos["avg_cost"] * pos["qty"]
                                       + price * qty) / total
                    pos["qty"] = total
                    pos.setdefault("entry_date", bar_date)
                    acc["positions"][od["ticker"]] = pos
                else:
                    qty = min(qty, pos["qty"])      # ペーパーはショート不可
                    if qty <= 0:
                        continue
                    acc["cash"] += price * qty - fee
                    pos["qty"] -= qty
                    if pos["qty"] <= 0:
                        acc["positions"].pop(od["ticker"], None)
                    else:
                        acc["positions"][od["ticker"]] = pos
                ev = {"type": "fill", "strategy": sid, "date": bar_date,
                      "ticker": od["ticker"], "side": od["side"], "qty": qty,
                      "price": round(price, 4), "fee": round(fee, 4),
                      "tag": od.get("tag", ""), "basis": "open", "open": round(open_px, 4),
                      "session": session_label(od["ticker"]),
                      "created": od.get("created"), "created_at": od.get("created_at"), "booked": today,
                      "reason": reason_of(sid, od), "timing": timing_text(od, bar_date)}
                fills.append(ev)
                append_event(cfg, ev)
            pending_all[sid] = waiting

        # 2) equity 更新（BQ の終値）
        invested = sum(mark_of(t, p) * p["qty"] for t, p in acc["positions"].items())
        equity = acc["cash"] + invested
        prev_h = acc["equity_history"][-1] if acc["equity_history"] else {}
        prev = prev_h.get("equity", initial)
        day_pnl = equity - prev
        # 評価に使う終値の日が前回の評価と同じ（土曜・休場明けの朝の実行）なら、前日比は値動きではない
        price_stale = bool(prev_h) and bool(price_info["asof"]) and prev_h.get("price_asof") == price_info["asof"]
        if not already:
            acc["equity_history"].append({"date": today,
                                          "equity": round(equity, 2),
                                          "day_pnl": round(day_pnl, 2),
                                          "price_asof": price_info["asof"]})
            acc["equity_history"] = acc["equity_history"][-750:]   # 約3年分
            append_event(cfg, {"type": "mark", "strategy": sid, "date": today,
                               "equity": round(equity, 2),
                               "day_pnl": round(day_pnl, 2),
                               "price_asof": price_info["asof"]})

        # 3) 新しい注文（adapter）→ 約定待ちに足す
        new_orders = []
        if not already:
            adapter = adapters.get(sid)
            raw = []
            if adapter:
                try:
                    raw = list(adapter(cfg, results, acc, today) or [])
                except Exception as e:
                    print(f"    ⚠️ [paper:{sid}] adapter例外: {str(e)[:60]}")
            held = {(o["ticker"], o["side"]) for o in pending_all.get(sid, [])}
            for od in raw:
                if not od.get("ticker") or (od["ticker"], od.get("side")) in held:
                    continue
                od = dict(od, created=today, created_at=now.isoformat(timespec="minutes"),
                          eligible=eligible_date(od["ticker"], now), session=session_label(od["ticker"]))
                od["reason"] = reason_of(sid, od)
                od["timing"] = timing_text(od)
                held.add((od["ticker"], od["side"]))
                new_orders.append(od)
                append_event(cfg, {"type": "order", "strategy": sid, "date": today, "ticker": od["ticker"],
                                   "side": od["side"], "qty": od.get("qty"), "weight": od.get("weight"),
                                   "tag": od.get("tag", ""), "created_at": od["created_at"],
                                   "eligible": od["eligible"], "session": od["session"],
                                   "reason": od["reason"], "timing": od["timing"]})
            pending_all[sid] = list(pending_all.get(sid, [])) + new_orders

        summary[sid] = {"equity": round(equity, 2),
                        "day_pnl": round(day_pnl, 2),
                        "price_stale": price_stale,
                        "mtd_pnl": round(_mtd_pnl(acc["equity_history"],
                                                  equity, today), 2),
                        "total_pnl": round(equity - initial, 2),
                        "initial": initial,
                        "fills": len(fills),
                        "new_orders": len(new_orders),
                        "fill_list": fills,
                        "new_list": new_orders,
                        "expired": len(expired),
                        "pending": len(pending_all.get(sid, [])),
                        "positions": len(acc["positions"]),
                        "already": already}

    accounts["_pending"] = pending_all
    save_accounts(cfg, accounts)
    summary["skipped"] = False
    summary["price"] = price_info
    return summary
