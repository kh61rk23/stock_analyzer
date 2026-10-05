# ==============================================================
# rates_monitor.py — 国債利回り（日米 3/5/10年）と政策金利（米・欧・日）
# 2026-10-05 新設（健太指示「国債の利回りは10年・5年・3年で、前日・週次との比較も」
#   「政策金利も欧州・日本・米国を描画」）
#
# データ元（すべて無料・APIキー不要）
#   日本国債: 財務省「国債金利情報」CSV（当月 jgbcm.csv・月初は過去分 jgbcm_all.csv も読む）
#   米国債  : FRED DGS3 / DGS5 / DGS10（日次）
#   政策金利: 米 FRED DFEDTARU（FF金利誘導目標の上限）/ 欧 FRED ECBDFR（ECB預金ファシリティ金利）
#             日 FRED IRSTCI01JPM156N（無担保コール翌日物・月次＝日銀政策金利の実効値）
# 結果は stock_data/cache/rates_<日付>.json に1日1回キャッシュする。
# ==============================================================
from __future__ import annotations

import csv
import io
import json
import re
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

MOF_CUR = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
MOF_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
TENORS = ("3年", "5年", "10年")
POLICY = {   # (FRED系列, 表示名, 変更とみなす最小幅)
    "us": ("DFEDTARU", "米国 FF金利誘導目標（上限）", 0.01),
    "eu": ("ECBDFR", "欧州 ECB預金ファシリティ金利", 0.01),
    "jp": ("IRSTCI01JPM156N", "日本 無担保コール翌日物（月次・日銀政策金利の実効値）", 0.10),
}
_ERA = {"R": 2018, "H": 1988, "S": 1925}


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (stock_analyzer rates_monitor)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_wareki(s: str) -> date | None:
    """'R8.10.1' → 2026-10-01（純関数）。"""
    m = re.match(r"^([RHS])(\d+)\.(\d+)\.(\d+)$", str(s or "").strip())
    if not m:
        return None
    return date(_ERA[m.group(1)] + int(m.group(2)), int(m.group(3)), int(m.group(4)))


def parse_mof_csv(text: str) -> list[tuple[date, dict]]:
    """財務省CSV → [(日付, {'3年': x, '5年': y, '10年': z})]（純関数・重複日は1つに）。"""
    rows, header = {}, None
    for r in csv.reader(io.StringIO(text)):
        if r and r[0] == "基準日":
            header = r
            continue
        if not header or not r:
            continue
        d = parse_wareki(r[0])
        if not d:
            continue
        vals = {}
        for t in TENORS:
            try:
                vals[t] = float(r[header.index(t)])
            except (ValueError, IndexError):
                pass
        if vals:
            rows[d] = vals
    return sorted(rows.items())


def parse_fred_csv(text: str) -> list[tuple[date, float]]:
    out = []
    for r in list(csv.reader(io.StringIO(text)))[1:]:
        try:
            out.append((date.fromisoformat(r[0]), float(r[1])))
        except (ValueError, IndexError):
            continue
    return out


def change_summary(series: list[tuple[date, float]]) -> dict | None:
    """最新値・前日比・前週比（7日前以前で最も近い日）（純関数）。差はポイント（%pt）。"""
    if not series:
        return None
    d0, v0 = series[-1]
    prev = series[-2] if len(series) >= 2 else None
    wk = [x for x in series if x[0] <= d0 - timedelta(days=7)]
    return {"date": d0.isoformat(), "value": v0,
            "d1": round(v0 - prev[1], 3) if prev else None,
            "w1": round(v0 - wk[-1][1], 3) if wk else None,
            "w1_date": wk[-1][0].isoformat() if wk else None}


def last_change(series: list[tuple[date, float]], min_step: float = 1e-9) -> dict | None:
    """政策金利の直近の変更（いつ・何%→何%）（純関数）。

    min_step 未満の揺れは変更とみなさない（日本のコール翌日物は月次で0.001%単位で揺れる）。
    """
    base = series[-1][1] if series else None
    for i in range(len(series) - 1, 0, -1):
        if abs(series[i - 1][1] - base) >= min_step:
            return {"date": series[i][0].isoformat(), "from": series[i - 1][1], "to": series[i][1]}
    return None


def fetch(today: date | None = None) -> dict:
    today = today or date.today()
    out: dict = {"jp": {}, "us": {}, "policy": {}, "errors": []}
    # 日本国債
    try:
        cur = parse_mof_csv(_get(MOF_CUR).decode("shift_jis", "replace"))
        if len(cur) < 8:   # 月初は前週比のために過去分も読む
            allr = parse_mof_csv(_get(MOF_ALL, 60).decode("shift_jis", "replace"))
            cur = sorted(dict(allr[-40:] + cur).items())
        for t in TENORS:
            s = change_summary([(d, v[t]) for d, v in cur if t in v])
            if s:
                out["jp"][t] = s
    except Exception as exc:
        out["errors"].append(f"日本国債: {type(exc).__name__}: {str(exc)[:80]}")
    # 米国債
    start = (today - timedelta(days=40)).isoformat()
    for t, sid in (("3年", "DGS3"), ("5年", "DGS5"), ("10年", "DGS10")):
        try:
            s = change_summary(parse_fred_csv(_get(FRED.format(sid=sid, start=start)).decode()))
            if s:
                out["us"][t] = s
        except Exception as exc:
            out["errors"].append(f"米国債{t}: {type(exc).__name__}")
    # 政策金利（変更日を出すため長めに取る）
    pstart = (today - timedelta(days=800)).isoformat()
    for k, (sid, label, step) in POLICY.items():
        try:
            ser = parse_fred_csv(_get(FRED.format(sid=sid, start=pstart)).decode())
            if ser:
                out["policy"][k] = {"label": label, "date": ser[-1][0].isoformat(), "value": ser[-1][1],
                                    "last_change": last_change(ser, step)}
        except Exception as exc:
            out["errors"].append(f"政策金利{k}: {type(exc).__name__}")
    return out


def get_rates(cfg=None, today: date | None = None) -> dict:
    """1日1回キャッシュして返す。"""
    today = today or date.today()
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) if cfg else Path("stock_data/cache")
    p = base / f"rates_{today.isoformat()}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    r = fetch(today)
    try:
        base.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return r


def _pt(x) -> str:
    return "—" if x is None else f"{x * 100:+.1f}bp"


def format_lines(r: dict) -> list[str]:
    """メール・週次レポート用の行（純関数）。"""
    lines = []
    for key, title in (("jp", "🇯🇵 日本国債"), ("us", "🇺🇸 米国債")):
        if r.get(key):
            lines.append(f"  【{title}】（前日比 / 前週比）")
            for t in ("10年", "5年", "3年"):
                s = r[key].get(t)
                if s:
                    lines.append(f"    {t}: {s['value']:.3f}%  {_pt(s.get('d1'))} / {_pt(s.get('w1'))}"
                                 f"  [{s['date']}]")
    if r.get("policy"):
        lines.append("  【政策金利】")
        for k in ("us", "eu", "jp"):
            p = r["policy"].get(k)
            if not p:
                continue
            lc = p.get("last_change")
            ch = (f" — 直近の変更 {lc['date']}: {lc['from']:.2f}%→{lc['to']:.2f}%" if lc else "")
            lines.append(f"    {p['label']}: {p['value']:.2f}%（{p['date']}）{ch}")
    if r.get("errors"):
        lines.append("  ⚠️ 取得できなかったもの: " + " / ".join(r["errors"]))
    return lines
