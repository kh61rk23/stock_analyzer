# ==============================================================
# screener_trace.py  (2026-09-29)
# 銘柄ごとの「選別の経路」を残す（健太の要望⑤）
# --------------------------------------------------------------
#   ・通過銘柄   : 点数の経路（score_trail）＋ 9指標（candidate_kpi）
#   ・落ちた銘柄 : どのフィルターで落ちたか（stock_screener._apply_filters の内訳）
# を stock_data/data/screener_rejections.json（日付→落選リスト）と
# insights/screener/screener_trace_<日付>.md に書く。board の「銘柄選別」
# タブもこの2つ（と screener_history.json）を読む。
# ==============================================================
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

RETAIN_DAYS = 60
_FILTER_LABELS = {"per": "PER", "pbr": "PBR", "roe": "ROE", "market_cap": "時価総額",
                  "volume": "出来高"}


def _thresholds(cfg) -> dict:
    return {
        "per": f"0超〜{getattr(cfg, 'SCREEN_PER_MAX', 30.0):g}倍",
        "pbr": f"0超〜{getattr(cfg, 'SCREEN_PBR_MAX', 5.0):g}倍",
        "roe": f"{getattr(cfg, 'SCREEN_ROE_MIN', 0.08) * 100:g}%以上",
        "market_cap": f"{getattr(cfg, 'SCREEN_MCAP_MIN_B', 100.0):g}億円以上",
        "volume": f"{getattr(cfg, 'SCREEN_VOL_MIN', 100_000):,}株以上",
    }


def rejection_reasons(detail: dict, cfg=None) -> list[str]:
    """filter_detail から落ちた理由を人が読める形で返す（純関数）。"""
    th = _thresholds(cfg)
    out = []
    for key, label in _FILTER_LABELS.items():
        v = str((detail or {}).get(key, ""))
        if v.startswith("NG"):
            out.append(f"{label} {v[3:-1]}（条件 {th[key]}）")
    return out


def rejected_list(results: dict, cfg=None) -> list[dict]:
    sc = (results or {}).get("screener") or {}
    out = []
    for c in sc.get("candidates") or []:
        if not isinstance(c, dict) or c.get("pass_filters"):
            continue
        out.append({
            "ticker": c.get("ticker", ""), "name": c.get("name", ""),
            "sector_code": c.get("sector_code", ""), "sector_name": c.get("sector_name", ""),
            "reasons": rejection_reasons(c.get("filter_detail") or {}, cfg),
            "filter_detail": c.get("filter_detail") or {},
            "composite_score": c.get("composite_score"),
        })
    return out


def save_rejections(cfg, rejected: list[dict], today: str | None = None) -> Path:
    today = today or date.today().isoformat()
    p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "screener_rejections.json"
    try:
        hist = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(hist, dict):
            hist = {}
    except (OSError, ValueError):
        hist = {}
    hist[today] = rejected
    cutoff = (date.fromisoformat(today) - timedelta(days=RETAIN_DAYS)).isoformat()
    hist = {d: v for d, v in hist.items() if d >= cutoff}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


def _x(v):
    return "—" if v is None else f"{v:.1f}倍"


def _oku(v):
    return "—" if v is None else f"{v / 1e8:,.0f}億"


def kpi_cells(k: dict | None) -> list[str]:
    """9指標を表のセルにする（①PBR〜⑨IRR）。"""
    k = k or {}
    stock = "—"
    if k.get("years"):
        sd = k.get("sales_growth_sd")
        stock = (f"減収{k.get('down_sales_years', 0)}/減益{k.get('down_oi_years', 0)}/"
                 f"赤字{k.get('op_loss_years', 0)}（{k['years']}期）"
                 + (f" σ{sd * 100:.0f}%" if sd is not None else ""))
    nc = k.get("net_cash")
    ncs = "—" if nc is None else f"{_oku(nc)}（{_pct(k.get('net_cash_ratio'))}）"
    return [
        _x(k.get("pbr")), _x(k.get("per")),
        f"{_pct(k.get('fcf_yield'))} / {_pct(k.get('fcf_yield_avg'))}",
        _x(k.get("ev_ebitda")),
        f"{_pct(k.get('roic'))} / {_pct(k.get('roic_avg'))}",
        ncs, stock, "要開示転記", _pct(k.get("irr")),
    ]


def build_md(passed: list[dict], rejected: list[dict], today: str, cfg=None) -> str:
    from score_trail import summary
    L = [f"# 選別の経路（{today}）", "",
         "スクリーナーを通った銘柄の**点数の経路**（基本点→各モジュールの加減点→上限）と"
         "**9指標**、落ちた銘柄の**理由**。9指標は表示専用で点数には使っていない。", ""]
    L += [f"## ✅ 通過 {len(passed)}銘柄（総合点順）", "",
          "| 銘柄 | 総合点 | 加点前 | 点数の経路 |", "|---|---|---|---|"]
    ps = sorted(passed, key=lambda c: (-(c.get("composite_score") or 0),
                                       -(c.get("base_score") or 0)))
    for c in ps:
        L.append(f"| {c.get('ticker')} {str(c.get('name', ''))[:18]} | "
                 f"{c.get('composite_score')} | {c.get('base_score', '—')} | "
                 f"{summary(c, width=8) or '—'} |")
    L += ["", "## 📐 9指標（① PBR ② PER ③ FCF Yield 直近/5期 ④ EV/EBITDA ⑤ ROIC 直近/5期 "
          "⑥ ネットキャッシュ（時価比）⑦ ストック性 ⑧ 投資先EY ⑨ 投資資本IRR）", "",
          "| 銘柄 | ① | ② | ③ | ④ | ⑤ | ⑥ | ⑦ | ⑧ | ⑨ | 注記 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in ps:
        k = c.get("kpi") or {}
        L.append(f"| {c.get('ticker')} | " + " | ".join(kpi_cells(k)) + " | "
                 + "・".join(k.get("notes") or []) + " |")
    L += ["", f"## ❌ 落ちた {len(rejected)}銘柄", "", "| 銘柄 | 業種 | 落ちた理由 |",
          "|---|---|---|"]
    for r in rejected:
        L.append(f"| {r['ticker']} {str(r.get('name', ''))[:18]} | {r.get('sector_name', '')} | "
                 f"{' / '.join(r['reasons']) or '（フィルター以外）'} |")
    return "\n".join(L) + "\n"


def run(cfg, results: dict) -> dict:
    """落選理由の保存と経路レポートの出力。"""
    today = date.today().isoformat()
    sc = (results or {}).get("screener") or {}
    passed = [c for c in sc.get("candidates") or [] if isinstance(c, dict) and c.get("pass_filters")]
    rejected = rejected_list(results, cfg)
    save_rejections(cfg, rejected, today)
    md_dir = Path(getattr(cfg, "INSIGHT_DIR", "stock_data/insights")) / "screener"
    md_dir.mkdir(parents=True, exist_ok=True)
    md = md_dir / f"screener_trace_{today}.md"
    md.write_text(build_md(passed, rejected, today, cfg), encoding="utf-8")
    capped = sum(1 for c in passed if any("切り捨て" in (t.get("note") or "")
                                           for t in c.get("score_trail") or []))
    print(f"  🧭 [screener_trace] 通過{len(passed)} / 落選{len(rejected)} / "
          f"上限で切り捨て{capped}銘柄 → {md.name}")
    return {"passed": len(passed), "rejected": len(rejected), "capped": capped,
            "path": str(md)}
