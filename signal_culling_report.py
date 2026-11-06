# ==============================================================
# signal_culling_report.py — シグナル自動淘汰レポート（R1-4 / E4）
# ==============================================================
# 【役割】月次で22カテゴリ各シグナルの IC を算出し、重み変更「提案」を出す。
#   IC = 日ごとに、その日のカテゴリスコアと20日先リターンの業種横断 Spearman 相関を取り、その平均
#   t  = 平均IC / (日ごとのICの標準偏差 / sqrt(独立な日数))。独立な日数 = 評価日数 ÷ 先の営業日数
#        （先リターンの窓が重なるので、日数をそのまま数えると t が約 sqrt(20) 倍に膨らむ）
#   業種で値が同じカテゴリ（市場共通の指標）は業種横断の相関が取れないので判定不能
#   判定: 独立な日数 < B4_SIGNAL_CULLING_REPORT_MIN_EFFECTIVE_DAYS →「判定不能」/ t<=-2.0 →「重みゼロ化提案」/
#         t>=+2.0 →「重み+20%提案」/ それ以外 → 現状維持
# 【原則】自動では変更しない。提案MDのうち承認されたものだけ
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

import bisect
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
    'B4_SIGNAL_CULLING_REPORT_MIN_EFFECTIVE_DAYS': 'MIN_EFFECTIVE_DAYS',
    'B4_SIGNAL_CULLING_REPORT_MIN_SECTORS': 'MIN_SECTORS',
}

FWD_DAYS = 20          # 先リターンの営業日数（ML_TARGET_HORIZON_D と同値）
T_PROMOTE = 2.0        # 重み+20%提案
T_DEMOTE = -2.0        # 重みゼロ化提案
MIN_EFFECTIVE_DAYS = 3.0   # 独立な日数（評価日数÷FWD_DAYS）の下限（config B4_SIGNAL_CULLING_REPORT_MIN_EFFECTIVE_DAYS）
MIN_SECTORS = 10           # 1日の業種横断の相関に要る業種数（config B4_SIGNAL_CULLING_REPORT_MIN_SECTORS）


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


def judge_daily_ics(ics: list, current_weight: float, fwd_days: int | None = None,
                    min_effective: float | None = None) -> dict:
    """日ごとの業種横断 IC の列 → 判定（純関数）。

    独立な日数 = len(ics) / fwd_days。これが min_effective 未満、または IC のばらつきが 0 なら判定不能。"""
    fwd = fwd_days or FWD_DAYS
    need = MIN_EFFECTIVE_DAYS if min_effective is None else min_effective
    n = len(ics)
    undecided = {"ic": None, "t": None, "n": n, "verdict": "判定不能", "proposed_weight": current_weight}
    if n < 2 or n / fwd < need:
        return undecided
    mean = sum(ics) / n
    sd = (sum((x - mean) ** 2 for x in ics) / (n - 1)) ** 0.5
    if sd <= 0:
        return undecided
    t = mean / (sd / (n / fwd) ** 0.5)
    base = {"ic": round(mean, 4), "t": round(t, 2), "n": n}
    if t <= T_DEMOTE:
        return {**base, "verdict": "重みゼロ化提案", "proposed_weight": 0.0}
    if t >= T_PROMOTE:
        return {**base, "verdict": "重み+20%提案", "proposed_weight": round(current_weight * 1.2, 4)}
    return {**base, "verdict": "現状維持", "proposed_weight": current_weight}


def build_judgments(panel: list, fwd_returns: dict, weights: dict, min_sectors: int | None = None) -> dict:
    """
    panel: [{"date": d, "sectors": {code: {cat: score}}}, ...]
    fwd_returns: {(date, code): 20日先リターン}
    weights: {cat: 現行重み}
    1日ごとに業種横断の Spearman を取る（業種数が min_sectors 未満、またはスコアが全業種同じ日は取れない）。
    Returns: {cat: judge_daily_ics(...)}
    """
    need = MIN_SECTORS if min_sectors is None else min_sectors
    daily: dict = {}                              # {cat: [日ごとのIC]}
    for row in panel:
        d = row.get("date")
        per_cat: dict = {}
        for code, comps in (row.get("sectors") or {}).items():
            ret = fwd_returns.get((d, code))
            if ret is None:
                continue
            for cat, score in comps.items():
                if score is None:
                    continue
                xs, ys = per_cat.setdefault(cat, ([], []))
                xs.append(float(score))
                ys.append(float(ret))
        for cat, (xs, ys) in per_cat.items():
            if len(xs) < need:
                continue
            ic = spearman(xs, ys)
            if ic is not None:
                daily.setdefault(cat, []).append(ic)
    return {cat: judge_daily_ics(daily.get(cat, []), w) for cat, w in weights.items()}


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
    lines += ["| カテゴリ | 現行重み | 提案重み | IC | t | 評価日数 | 判定 | 承認 |",
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
        # パネルの日付が休場日（土日・祝日）なら、直前の営業日の終値を起点にする
        cdates = [d for d, _ in closes]
        for d in dates:
            i = bisect.bisect_right(cdates, d) - 1
            if i < 0 or i + FWD_DAYS >= len(closes):
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
    # B-5: レポート月は引数で受ける。monthly_runner が「前月」を渡し、
    # monthly_strategy_review が同じ月の signal_culling_<yyyymm>.md を読む。
    # today 固定だと月初実行時に当月ラベルになり、集約側が「レポートなし」になる。
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
        note = ("⚠️ カテゴリパネルが0日分（stock_data/data/category_panel.jsonl が無い）のため全カテゴリ判定不能。"
                "パネルは日次の signal_aggregator の後に1日1行たまる" if not panel else
                f"⚠️ 先リターンが取れないため全カテゴリ判定不能（パネル {len(panel)}日分・"
                f"先リターンには各日から{FWD_DAYS}営業日後の終値が要る。蓄積不足か株価の取得失敗）。")
        print(f"    {note}")
    judgments = build_judgments(panel, fwd, weights)
    if fwd and not note and all(j["verdict"] == "判定不能" for j in judgments.values()):
        days = len({d for d, _ in fwd})
        note = (f"⚠️ 先リターンが取れた日は {days}日分。判定には独立な日数（評価日数÷{FWD_DAYS}）が"
                f"{MIN_EFFECTIVE_DAYS:g}以上（評価日数 {int(MIN_EFFECTIVE_DAYS * FWD_DAYS)}日）要る。"
                "市場共通の指標（業種で値が同じもの）は業種横断の相関が取れず、この方式では判定できない。")
    md = render_report(judgments, weights, yyyymm, note)

    out_dir = Path(getattr(cfg, "INSIGHT_DIR", REPO / "stock_data" / "insights"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"signal_culling_{yyyymm}.md"
    out.write_text(md, encoding="utf-8")
    print(f"✅ レポート出力: {out}")


if __name__ == "__main__":
    main()
