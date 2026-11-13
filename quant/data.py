# ==============================================================
# quant/data.py — 価格（yfinance）と金利（BIS）の取得・キャッシュ・品質の検査
# ==============================================================
# 【役割】検証ラボに渡す MarketData を作る。ネットワークに出るのは _download（yfinance）と
#   _fetch_bis_zip（BIS の政策金利の一括 zip）の2関数だけで、テストはこの2つを差し替える。
# 【価格】調整後終値。group_by="ticker" の DataFrame は level0＝ticker（df[ticker]["Close"]）。
#   逆に引くと KeyError になるので、_extract_close が形の違い（1銘柄だけ単層・field が先の並び）も吸収する。
#   40 銘柄ずつ取り、取れなかった symbol は列を作らず missing に入れる（全滅は RuntimeError）。
# 【金利】BIS の政策金利（日次の行だけ）の％を小数にし、index へ前方補完して「1営業日ずらす」（日 t の値は t+1 から使う。
#   公表の時刻が終値より後のことがあり、当日の値を当日の損益に使うと先読みになるため）。
#   系列の始まりより前の日・取れなかった通貨は QUANT_RATE_FALLBACK の定数。全通貨が取れたら "bis"・
#   一部なら "partial"・全部だめなら "fallback"。最後の観測が RATE_STALE_DAYS より古い通貨は警告を出し partial 扱い。
# 【キャッシュ】QUANT_CACHE_DIR 配下。QUANT_CACHE_MAX_AGE_HOURS 以内で、同じ条件・同じ symbol 集合なら読むだけ。
#   金利は日次に直した後の表（rates.csv）で、全通貨が取れて新しい（source が "bis"）ときだけ保存する。
# ==============================================================
from __future__ import annotations

import csv
import io
import json
import logging
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from quant import instruments as I
from quant import settings as S
from quant.types import MarketData

log = logging.getLogger(__name__)

CHUNK_SIZE = 40
BIS_TIMEOUT_SEC = 60
STALE_DAYS = 7
JUMP_THRESHOLD = 0.40
SPIKE_THRESHOLD = 0.25         # 1日でこれを超えて動き、すぐ元の水準へ戻るものは飛び値として欠損にする
SPIKE_RETURN_TOL = 0.05        # 「元の水準へ戻った」とみなす差
SPLIT_RATIOS = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0)
SPLIT_TOLERANCE = 0.05
RATE_STALE_DAYS = 45           # BIS の最終観測がこれより古いと警告し partial 扱い（日次なので更新が止まったと見なす）


# ── ネットワークに出る関数（テストで差し替える） ──────────────────────────────

def _download(symbols, start, end):
    """yfinance で調整後終値を取る（group_by="ticker"）。yf_cache が使えればそれ、無ければ yfinance を直接。"""
    try:
        import yf_cache as yf
    except ImportError:
        import yfinance as yf
    return yf.download(list(symbols), start=start, end=end, auto_adjust=True,
                       group_by="ticker", progress=False, threads=True)


def _fetch_bis_zip(url: str) -> bytes:
    """BIS の政策金利の一括 zip（約4MB）を取る。取れなければ例外（OSError 系）を投げる。"""
    req = urllib.request.Request(url, headers={"User-Agent": "quant-lab"})
    with urllib.request.urlopen(req, timeout=BIS_TIMEOUT_SEC) as resp:
        return resp.read()


# ── 価格 ──────────────────────────────────────────────────────────────────

def _extract_close(raw, symbols) -> pd.DataFrame:
    """yfinance の戻りから終値（列＝symbol）を取り出す。取れた symbol だけの列になる。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    symbols = list(symbols)
    frames: dict = {}
    cols = raw.columns
    if isinstance(cols, pd.MultiIndex):
        lv0 = set(cols.get_level_values(0))
        if any(s in lv0 for s in symbols):
            # group_by="ticker"：level0＝ticker
            for s in symbols:
                if s in lv0:
                    sub = raw[s]
                    if "Close" in sub.columns:
                        frames[s] = sub["Close"]
        elif "Close" in lv0:
            # field が先の並び（1銘柄だけのときなどに出る）
            close = raw["Close"]
            for s in symbols:
                if s in close.columns:
                    frames[s] = close[s]
    else:
        if any(s in cols for s in symbols):
            for s in symbols:
                if s in cols:
                    frames[s] = raw[s]
        elif "Close" in cols and len(symbols) == 1:
            # 1銘柄だけで列が単層
            frames[symbols[0]] = raw["Close"]
    if not frames:
        return pd.DataFrame()
    df = pd.DataFrame(frames)
    return _normalize_index(df)


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """index を「時刻・タイムゾーンなしの日付・昇順・重複なし」にそろえる。"""
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out = df.copy()
    out.index = idx.normalize()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.astype(float)


def _cache_files(cfg):
    d = S.path(cfg, "QUANT_CACHE_DIR")
    return d / "prices.csv", d / "prices_meta.json"


def _read_price_cache(cfg, symbols, start, end):
    """条件が合う新しいキャッシュがあれば (終値, missing) を返す。無ければ None。"""
    csv_path, meta_path = _cache_files(cfg)
    if not csv_path.exists() or not meta_path.exists():
        return None
    age_h = (time.time() - csv_path.stat().st_mtime) / 3600.0
    if age_h > S.get(cfg, "QUANT_CACHE_MAX_AGE_HOURS"):
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("start") != start or meta.get("end") != end:
        return None
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    known = set(df.columns) | set(meta.get("missing", []))
    if not set(symbols) <= known:
        return None
    cols = [s for s in symbols if s in df.columns]
    if not cols:
        raise RuntimeError(f"価格が1本も取れなかった（キャッシュ上も全て欠損・{len(symbols)} symbol）")
    missing = [s for s in symbols if s not in df.columns]
    return df[cols], missing


def _write_price_cache(cfg, close, symbols, missing, start, end):
    csv_path, meta_path = _cache_files(cfg)
    close.to_csv(csv_path)
    meta_path.write_text(json.dumps({"start": start, "end": end, "symbols": list(symbols),
                                     "missing": list(missing)}, ensure_ascii=False), encoding="utf-8")


def load_prices(symbols, cfg, start=None, end=None, use_cache=True) -> pd.DataFrame:
    """調整後終値（列＝symbol）。取れなかった symbol は列を作らず、戻り値の attrs["missing"] に入れる。
    全部取れなければ RuntimeError。start 省略時は QUANT_HISTORY_START。"""
    symbols = list(dict.fromkeys(symbols))
    start = start or S.get(cfg, "QUANT_HISTORY_START")
    if use_cache:
        hit = _read_price_cache(cfg, symbols, start, end)
        if hit is not None:
            close, missing = hit
            close.attrs["missing"] = missing
            return close

    parts = []
    for i in range(0, len(symbols), CHUNK_SIZE):
        chunk = symbols[i:i + CHUNK_SIZE]
        part = _extract_close(_download(chunk, start, end), chunk)
        if not part.empty:
            parts.append(part)
    if not parts:
        raise RuntimeError(f"価格が1本も取れなかった（{len(symbols)} symbol）")
    close = pd.concat(parts, axis=1).sort_index()
    close = close.dropna(axis=1, how="all")        # 全部欠損の列は「取れなかった」とみなす
    got = [s for s in symbols if s in close.columns]
    if not got:
        raise RuntimeError(f"価格が1本も取れなかった（{len(symbols)} symbol）")
    close = close[got]
    missing = [s for s in symbols if s not in close.columns]
    if use_cache:
        _write_price_cache(cfg, close, symbols, missing, start, end)
    close.attrs["missing"] = missing
    return close


# ── 金利 ──────────────────────────────────────────────────────────────────

def _col(header, prefix):
    """ヘッダ行から、prefix で始まる列の位置を返す（"REF_AREA:Reference area" のような名前の後半の違いを吸収する）。"""
    for i, h in enumerate(header):
        if h.strip().lstrip("\ufeff").startswith(prefix):
            return i
    raise ValueError(f"BIS の CSV に列が無い: {prefix}")


def _parse_bis(raw_bytes: bytes, areas: dict) -> dict:
    """BIS の zip（中の CSV 1本）から、対象の地域の日次（"D: Daily"）の政策金利を取り出す。
    areas＝{通貨: 地域コード}。戻りは {通貨: 日付 index の Series（％・昇順）}。日次の行が無い通貨はキーを作らない。
    約80万行を1回だけ読む。REF_AREA は "US: United States" の形なので ":" の前をコードとする。値が空・NaN の行は捨てる。"""
    code_to_cur = {code: cur for cur, code in areas.items()}
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("BIS の zip に CSV が無い")
        found: dict = {cur: {} for cur in areas}
        with zf.open(names[0]) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))
            header = next(reader, None)
            if header is None:
                raise ValueError("BIS の CSV が空")
            i_freq = _col(header, "FREQ")
            i_area = _col(header, "REF_AREA")
            i_time = _col(header, "TIME_PERIOD")
            i_val = _col(header, "OBS_VALUE")
            need = max(i_freq, i_area, i_time, i_val)
            for row in reader:
                if len(row) <= need or not row[i_freq].startswith("D"):
                    continue
                cur = code_to_cur.get(row[i_area].split(":", 1)[0].strip())
                if cur is None:
                    continue
                v = row[i_val].strip()
                if not v or v.lower() == "nan":
                    continue
                found[cur][row[i_time].strip()] = float(v)
    out: dict = {}
    for cur, d in found.items():
        if not d:
            continue
        s = pd.Series(d, dtype=float)
        s.index = pd.DatetimeIndex(pd.to_datetime(s.index))
        out[cur] = s.sort_index().rename(cur)
    return out


def _daily_shifted(pct: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """％の日次系列を小数にして index へ前方補完し、index 上で1営業日ずらす（日 t の値は t+1 から使う）。
    index の最初の日には、それより前の最後の観測を使う。系列の始まりより前の日は NaN のまま返す。"""
    s = pct.dropna() / 100.0
    s = s[~s.index.duplicated(keep="last")].sort_index()
    full = s.reindex(s.index.union(index)).ffill()
    on_idx = full.reindex(index)
    shifted = on_idx.shift(1)
    if len(index):
        before = full.loc[:index[0] - pd.Timedelta(days=1)]
        shifted.iloc[0] = before.iloc[-1] if len(before) else float("nan")
    return shifted


def _rate_cache_path(cfg) -> Path:
    return S.path(cfg, "QUANT_CACHE_DIR") / "rates.csv"


def _read_rate_cache(cfg, currencies):
    """新しいキャッシュ（通貨別の日次の％）があれば返す。通貨が足りなければ None。"""
    p = _rate_cache_path(cfg)
    if not p.exists():
        return None
    if (time.time() - p.stat().st_mtime) / 3600.0 > S.get(cfg, "QUANT_CACHE_MAX_AGE_HOURS"):
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    if not set(currencies) <= set(df.columns):
        return None
    return df


def load_rates(cfg, index, use_cache=True):
    """通貨別の短期金利（年率・小数・日次）と source（"bis" / "partial" / "fallback"）を返す。
    日 t の値は t+1 営業日から効く（先読みの防止）。"""
    index = pd.DatetimeIndex(index)
    areas = S.get(cfg, "QUANT_RATE_BIS_AREAS")
    fallback = S.get(cfg, "QUANT_RATE_FALLBACK")
    currencies = list(areas)

    daily = _read_rate_cache(cfg, currencies) if use_cache else None
    fetched = daily is None
    if fetched:
        try:
            parsed = _parse_bis(_fetch_bis_zip(S.get(cfg, "QUANT_RATE_BIS_URL")), areas)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
            log.warning("BIS の金利の取得・解析に失敗（全通貨で定数の金利を使う）: %s", e)
            parsed = {}
        else:
            if not parsed:
                log.warning("BIS の CSV に対象の日次の金利が1通貨も無い（全通貨で定数の金利を使う）")
        for cur in currencies:
            if cur in parsed and parsed[cur].dropna().empty:
                parsed.pop(cur)
        if parsed:
            for cur in currencies:
                if cur not in parsed:
                    log.warning("BIS に日次の金利が無い（%s・定数の金利を使う）", cur)
        daily = pd.DataFrame(parsed) if parsed else pd.DataFrame()

    out = pd.DataFrame(index=index)
    n_ok = 0
    for cur in currencies:
        const = float(fallback.get(cur, 0.0))
        if cur in daily.columns and not daily[cur].dropna().empty:
            last_obs = daily[cur].dropna().index.max()
            if len(index) and (index[-1] - last_obs).days > RATE_STALE_DAYS:
                # 系列の更新が止まると最後の値を延ばし続けるので、警告を出して partial 扱いにする
                log.warning("BIS の金利が古い（%s・最終 %s）", cur, last_obs.strftime("%Y-%m-%d"))
            else:
                n_ok += 1
            out[cur] = _daily_shifted(daily[cur], index).fillna(const)   # 始まりより前の日だけ定数
        else:
            out[cur] = const
    if n_ok == len(currencies) and n_ok > 0:
        source = "bis"
    elif n_ok == 0 and daily.empty:
        source = "fallback"
    else:
        source = "partial"
    if use_cache and fetched and source == "bis":
        # 取り直したときだけ保存する（キャッシュの読み出しで書き直すと更新時刻が延び、期限が切れなくなる）
        daily[currencies].to_csv(_rate_cache_path(cfg))
    return out, source


# ── 価格の修復 ────────────────────────────────────────────────────────────

def _is_split_ratio(ratio: float) -> bool:
    """比が分割・併合でよくある比（2・3・4・5・10・1.5・2.5 とその逆数）に近いか（誤差 SPLIT_TOLERANCE）。"""
    r = ratio if ratio >= 1.0 else 1.0 / ratio
    return any(abs(r / c - 1.0) <= SPLIT_TOLERANCE for c in SPLIT_RATIOS)


def repair_prices(raw: pd.DataFrame, skip=(), threshold: float = None, lookahead: int = 3):
    """取得元の壊れた値（0以下の価格・分割の調整漏れ・1〜数日だけの飛び値）を直した価格と、直した内容の一覧を返す。

    対象は分散された ETF と主要通貨（skip の列＝予想変動率の指数などは触らない）。直さずに検証へ流すと、−100% を下回る日や
    数百倍の日が損益に入り、成績が意味を失う。逆に直しすぎると本物の暴落を消すので、直すのは次の形だけにする。
      nonpositive … 0以下の価格がある列は、その最後の日までを欠損にする（配当の調整が壊れた区間で、倍率を直しても比が信用できない）。
      spike       … 1日に SPIKE_THRESHOLD を超えて動き、lookahead 日以内に元の水準（差が SPIKE_RETURN_TOL 未満）へ戻る
                    → 飛んだ日だけ欠損にする。
      shift       … 1日に threshold を超えて動いて戻らず、比が分割の比に近い → 調整漏れとみなし、それより前の価格に比を掛けて
                    水準をつなぐ（その日の収益は 0 になる）。
      pending     … 末尾の lookahead 日以内の threshold 超え。戻るかどうかをまだ判定できないので、その日以降を欠損にして待つ
                    （本物の暴落なら次の日以降に unexplained として損益に入る。日次の運用で、判定の前に 0 の収益を確定させない）。
      unexplained … threshold を超えて動いて戻らず、分割の比でもない → 本物の値動きとして残す（記録だけ）。
    当日より後の値を見るのは「取得元の値が壊れているか」の判定だけ。
    """
    thr = JUMP_THRESHOLD if threshold is None else float(threshold)
    out = raw.copy()
    repairs = []
    for col in out.columns:
        if col in skip:
            continue
        s = out[col]
        bad = s[s <= 0]
        if len(bad):
            last = bad.index[-1]
            s = s.where(s.index > last)
            repairs.append({"symbol": col, "date": last.strftime("%Y-%m-%d"), "kind": "nonpositive", "ratio": None})
        v = s.dropna()
        if len(v) < 2:
            out[col] = s
            continue
        idx, x = v.index, v.to_numpy(dtype=float).copy()
        drop = np.zeros(len(x), dtype=bool)
        prev = 0                                   # 直前の「生きている」位置
        i = 1
        while i < len(x):
            ratio = x[i] / x[prev]
            move = abs(ratio - 1.0)
            if move <= min(thr, SPIKE_THRESHOLD):
                prev = i
                i += 1
                continue
            rec = {"symbol": col, "date": idx[i].strftime("%Y-%m-%d"), "ratio": float(ratio)}
            back = next((k for k in range(1, lookahead + 1)
                         if i + k < len(x) and abs(x[i + k] / x[prev] - 1.0) < SPIKE_RETURN_TOL), None)
            if back is not None:
                drop[i:i + back] = True
                repairs.append({**rec, "kind": "spike"})
                i += back                          # 戻った位置を、飛ぶ前の位置（prev）と比べ直す
            elif move <= thr:
                prev = i                           # 戻らない中くらいの動きは本物として残す
                i += 1
            elif i + lookahead >= len(x):
                drop[i:] = True
                repairs.append({**rec, "kind": "pending"})
                break
            elif _is_split_ratio(ratio):
                x[:i] *= ratio
                repairs.append({**rec, "kind": "shift"})
                prev = i
                i += 1
            else:
                repairs.append({**rec, "kind": "unexplained"})
                prev = i
                i += 1
        fixed = pd.Series(np.where(drop, np.nan, x), index=idx)
        out[col] = fixed.reindex(out.index)
    return out, repairs


# ── まとめ ────────────────────────────────────────────────────────────────

def build(cfg, use_cache=True, today=None) -> MarketData:
    """価格・収益率・金利・品質をまとめた MarketData を作る。
    quality＝{"rates_source","missing","stale","jumps","repairs","as_of"}。
    jumps は取得元の値のままの大きな動き（報告用）、repairs はそれを直した内容。close・returns は直した後の値。"""
    insts = I.load(cfg)
    symbols = I.all_symbols(cfg)
    today_ts = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    end = (today_ts + pd.Timedelta(days=1)).strftime("%Y-%m-%d") if today is not None else None

    raw = load_prices(symbols, cfg, end=end, use_cache=use_cache)
    missing = list(raw.attrs.get("missing", []))

    raw = raw[raw.index.dayofweek < 5]             # 営業日（土日を除く）の和集合
    last_valid = {c: raw[c].last_valid_index() for c in raw.columns}
    iv_syms = {i.iv_symbol for lst in I.by_class(cfg).values() for i in lst if i.iv_symbol}
    raw_returns = raw.ffill(limit=S.get(cfg, "QUANT_FFILL_LIMIT_DAYS")).pct_change(fill_method=None)
    repaired, repairs = repair_prices(raw, skip=iv_syms)
    close = repaired.ffill(limit=S.get(cfg, "QUANT_FFILL_LIMIT_DAYS"))
    returns = close.pct_change(fill_method=None)

    stale = [c for c, d in last_valid.items() if d is None or (today_ts - d).days > STALE_DAYS]
    jumps = []
    for c in raw_returns.columns:
        if c in iv_syms:
            continue                                # 予想変動率の指数は売買しないので対象外
        big = raw_returns[c][raw_returns[c].abs() > JUMP_THRESHOLD]
        for d, v in big.items():
            jumps.append({"symbol": c, "date": d.strftime("%Y-%m-%d"), "ret": float(v)})

    rates, source = load_rates(cfg, close.index, use_cache=use_cache)

    iv = pd.DataFrame(index=close.index)
    for lst in I.by_class(cfg).values():
        for inst in lst:
            if inst.iv_symbol and inst.iv_symbol in close.columns and inst.symbol in close.columns \
                    and inst.symbol not in iv.columns:
                iv[inst.symbol] = close[inst.iv_symbol] / 100.0

    quality = {
        "rates_source": source,
        "missing": missing,
        "stale": stale,
        "jumps": jumps,
        "repairs": repairs,
        "as_of": close.index[-1].strftime("%Y-%m-%d") if len(close) else today_ts.strftime("%Y-%m-%d"),
    }
    return MarketData(close=close, returns=returns, rates=rates, instruments=insts,
                      quality=quality, aux={"iv": iv})
