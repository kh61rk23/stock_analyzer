# ==============================================================
# signal_culling_report.py — シグナル自動淘汰レポート（Wave R1-4 / E4）
# ==============================================================
# 【役割】月次で22カテゴリ各シグナルの IC を算出し、重み変更「提案」を出す。
#   IC = 当日カテゴリスコアと20日先リターンの Spearman 相関（業種横断プール）
#   t  = IC×sqrt(N-2)/sqrt(1-IC²)
#   判定: N<60 →「判定不能」/ t<=-2.0 →「重みゼロ化提案」/
#         t>=+2.0 →「重み+20%提案」/ それ以外 → 現状維持
# 【原則】自動では変更しない。提案MDを健太が承認したものだけ
#   次セッションで signal_aggregator.DEFAULT_WEIGHTS を変更し
#   config_audit / pytest 通過を確認する（要件定義書 §R1-4）。
#
# 使い方（月初にローカルMac/Actionsで）:
#   python3 signal_culling_report.py            # 実データ（要ネットワーク）
#   python3 signal_culling_report.py --dry-run  # パネル読込と件数のみ
# 入力: stock_data/data/category_panel.jsonl（alpha_attributer が日次蓄積）
# 出力: insights/signal_culling_YYYYMM.md
# ==============================================================

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_SIGNAL_CULLING_REPORT_FWD_DAYS': 'FWD_DAYS',
    'B6_SIGNAL_CULLING_REPORT_T_PROMOTE': 'T_PROMOTE',
}

FWD_DAYS = 20          # 先リターンの営業日数（ML_TARGET_HORIZON_D と同値）
N_MIN = 60             # これ未満は「判定不能」
T_PROMOTE = 2.0        # 重み+20%提案
T_DEMOTE = -2.0        # 重みゼロ化提案


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def _rank(values: list) -> list:
    """平均順位（タイ対応）のランク列。"""
    idx = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and values[idx[j + 1]] == values[idx[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[idx[k]] = avg
        i = j + 1
    return ranks


def spearman(x: list, y: list):
    """Spearman順位相関。長さ不一致/分散ゼロは None。"""
    if len(x) != len(y) or len(x) < 3:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def t_stat(ic: float, n: int):
    if ic is None or n < 3 or abs(ic) >= 1.0:
        return None
    return ic * ((n - 2) ** 0.5) / ((1 - ic * ic) ** 0.5)


def judge_category(ic, n: int, current_weight: float) -> dict:
    """判定（要件定義書 §R1-4 の確定閾値）。"""
    if n < N_MIN or ic is None:
        return {"ic": ic, "t": None, "n": n, "verdict": "判定不能",
                "proposed_weight": current_weight}
    t = t_stat(ic, n)
    if t is None:
        return {"ic": ic, "t": None, "n": n, "verdict": "判定不能",
                "proposed_weight": current_weight}
    if t <= T_DEMOTE:
        return {"ic": round(ic, 4), "t": round(t, 2), "n": n,
                "verdict": "重みゼロ化提案", "proposed_weight": 0.0}
    if t >= T_PROMOTE:
        return {"ic": round(ic, 4), "t": round(t, 2), "n": n,
                "verdict": "重み+20%提案",
                "proposed_weight": round(current_weight * 1.2, 4)}
    return {"ic": round(ic, 4), "t": round(t, 2), "n": n,
            "verdict": "現状維持", "proposed_weight": current_weight}


def build_judgments(panel: list, fwd_returns: dict, weights: dict) -> dict:
    """
    panel: [{"date": d, "sectors": {code: {cat: score}}}, ...]
    fwd_returns: {(date, code): 20日先リターン}
    weights: {cat: 現行重み}
    Returns: {cat: judge_category(...)}
    """
    obs: dict = {}                                # {cat: ([scores], [rets])}
    for row in panel:
        d = row.get("date")
        for code, comps in (row.get("sectors") or {}).items():
            ret = fwd_returns.get((d, code))
            if ret is None:
                continue
            for cat, score in comps.items():
                xs, ys = obs.setdefault(cat, ([], []))
                xs.append(float(score))
                ys.append(float(ret))
    out = {}
    for cat, w in weights.items():
        xs, ys = obs.get(cat, ([], []))
        ic = spearman(xs, ys) if xs else None
        out[cat] = judge_category(ic, len(xs), w)
    return out


def render_report(judgments: dict, weights: dict, yyyymm: str,
                  note: str = "") -> str:
    lines = [f"# シグナル淘汰月次レポート {yyyymm[:4]}-{yyyymm[4:]}（E4）",
             "",
             "> **自動では変更しない。** 承認された行のみ次セッションで",
             "> signal_aggregator.DEFAULT_WEIGHTS を変更し、重み合計=1.00の再正規化と",
             "> config_audit / pytest 通過を確認すること（要件定義書 §R1-4）。",
             ""]
    if note:
        lines += [f"> {note}", ""]
    lines += ["| カテゴリ | 現行重み | 提案重み | IC | t | N | 判定 | 承認 |",
              "|---|---|---|---|---|---|---|---|"]
    for cat in sorted(weights, key=lambda c: -weights[c]):
        j = judgments.get(cat, {})
        ic = j.get("ic")
        t = j.get("t")
        lines.append(
            f"| {cat} | {weights[cat]:.2f} | {j.get('proposed_weight', '')} | "
            f"{ic if ic is not None else '-'} | {t if t is not None else '-'} | "
            f"{j.get('n', 0)} | {j.get('verdict', '判定不能')} |  |")
    changed = [c for c, j in judgments.items()
               if j.get("verdict") in ("重みゼロ化提案", "重み+20%提案")]
    lines += ["", f"提案あり: {len(changed)}件 / 判定不能: "
              f"{sum(1 for j in judgments.values() if j.get('verdict') == '判定不能')}件"]
    return "\n".join(lines) + "\n"


# ── I/O層 ────────────────────────────────────────────────────

def load_panel(cfg) -> list:
    base = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    p = base / "category_panel.jsonl"
    panel = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    panel.append(json.loads(line))
                except Exception:
                    continue
    return panel


def fetch_forward_returns(panel: list, cfg) -> dict:
    """
    各業種の代表銘柄（SECTOR_REPRESENTATIVE_TICKERS 先頭）の調整済み終値から
    20営業日先リターンを算出する（要ネットワーク・yfinance）。
    """
    sector_map = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {}) or {}
    codes = sorted({c for row in panel for c in (row.get("sectors") or {})})
    dates = sorted({row["date"] for row in panel})
    if not codes or not dates:
        return {}
    out: dict = {}
    try:
        import yf_cache as yf
    except ImportError:
        return {}
    for code in codes:
        reps = sector_map.get(code) or []
        if not reps:
            continue
        try:
            h = yf.Ticker(reps[0]).history(start=dates[0], period=None,
                                           auto_adjust=True)
            closes = [(idx.strftime("%Y-%m-%d"), float(r["Close"]))
                      for idx, r in h.iterrows()]
        except Exception:
            continue
        date_idx = {d: i for i, (d, _) in enumerate(closes)}
        for d in dates:
            i = date_idx.get(d)
            if i is None or i + FWD_DAYS >= len(closes):
                continue
            c0, c1 = closes[i][1], closes[i + FWD_DAYS][1]
            if c0 > 0:
                out[(d, code)] = c1 / c0 - 1
    return out


def main():
    try:
        import config as cfg
    except Exception:
        cfg = None
    import config as cfg
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    try:
        from signal_aggregator import DEFAULT_WEIGHTS as weights
    except Exception:
        weights = {}

    panel = load_panel(cfg)
    # 2026-07-25 B-5: レポート月を引数で指定できるようにした。
    # monthly_runner が「前月」を渡し、monthly_strategy_review が同じ月の
    # signal_culling_<yyyymm>.md を読む。today 固定のままだと月初実行時に
    # 当月ラベルになり、集約側の参照とズレて常に「レポートなし」になっていた。
    _args = [a for a in sys.argv[1:] if not a.startswith("-")]
    yyyymm = _args[0] if _args else date.today().strftime("%Y%m")
    print(f"📊 カテゴリパネル: {len(panel)}日分 / カテゴリ: {len(weights)}"
          f" / 対象月: {yyyymm}")

    if "--dry-run" in sys.argv:
        print("（--dry-run のためここで終了）")
        return

    fwd = fetch_forward_returns(panel, cfg)
    note = ""
    if not fwd:
        note = ("⚠️ 先リターンが取得できなかったため全カテゴリ判定不能"
                "（ネットワーク/パネル蓄積不足）。")
        print(f"    {note}")
    judgments = build_judgments(panel, fwd, weights)
    md = render_report(judgments, weights, yyyymm, note)

    out_dir = Path(getattr(cfg, "INSIGHT_DIR", REPO / "stock_data" / "insights"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"signal_culling_{yyyymm}.md"
    out.write_text(md, encoding="utf-8")
    print(f"✅ レポート出力: {out}")


if __name__ == "__main__":
    main()
