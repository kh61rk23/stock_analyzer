# ==============================================================
# dd_controller.py — ドローダウン制御（R3-3 / E9）
# ==============================================================
# 【役割】ポートフォリオ全体の投資部分のドローダウンを日次監視し、
#   NORMAL / CAUTION / DEFENSE の3状態をヒステリシス付きで遷移させる。
#   状態は results["dd_state"] に格納しブリーフ⑤・ダッシュボード②に表示。
#   **売却の強制はしない**（推奨レポートのみ・売買の実行は人が行う＝D-01）。
# 【設計正本】要件定義書_統合改修_20260705.md §R3-3（D-16=c 確定・変更禁止）:
#   equity = portfolio_holdings×当日価格（現金は対象外＝投資部分のみ / DR3-2推奨案=単一state）
#   peak   = 過去最高値（stock_data/dd_state.json に永続化）
#   dd     = 1 - equity/peak
#   NORMAL  → dd>=DD_CAUTION(0.15)  → CAUTION
#   CAUTION → dd>=DD_DEFENSE(0.25)  → DEFENSE
#   CAUTION → dd< DD_CAUTION_EXIT(0.10) → NORMAL
#   DEFENSE → dd< DD_DEFENSE_EXIT(0.20) → CAUTION
# dd_state.json 破損時は NORMAL・peak=当日equity から安全に再開。
# 既存の縮退モード（rule_optimizer）とは独立。両方発火時はDEFENSE優先表示は呼び手側。
# ==============================================================

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path


# ── 純粋ロジック（pytest対象・状態機械） ──────────────────────

def next_state(current: str, dd: float, cfg=None) -> str:
    """
    現状態と現ドローダウンから次状態を決める（ヒステリシス付き・確定仕様）。
    未知状態は NORMAL 扱いで安全に再開。
    """
    caution = getattr(cfg, "DD_CAUTION", 0.15)
    defense = getattr(cfg, "DD_DEFENSE", 0.25)
    caution_exit = getattr(cfg, "DD_CAUTION_EXIT", 0.10)
    defense_exit = getattr(cfg, "DD_DEFENSE_EXIT", 0.20)

    state = current if current in ("NORMAL", "CAUTION", "DEFENSE") else "NORMAL"

    if state == "NORMAL":
        return "CAUTION" if dd >= caution else "NORMAL"
    if state == "CAUTION":
        if dd >= defense:
            return "DEFENSE"
        if dd < caution_exit:
            return "NORMAL"
        return "CAUTION"
    # DEFENSE
    if dd < defense_exit:
        return "CAUTION"
    return "DEFENSE"


def state_action(state: str) -> dict:
    """状態ごとの推奨アクション（注文票発行制御のヒント・実行は人が行う）。"""
    return {
        "NORMAL": {"core_new_buy": "allow", "casino_new_buy": "allow",
                   "note": "通常運転"},
        "CAUTION": {"core_new_buy": "halt", "casino_new_buy": "half_size",
                    "note": "コア新規買い停止・カジノ新規は半サイズ"},
        "DEFENSE": {"core_new_buy": "halt", "casino_new_buy": "halt",
                    "note": "全新規買い停止・カジノ縮小推奨レポート・防御セクター添付"},
    }.get(state, {"core_new_buy": "allow", "casino_new_buy": "allow",
                  "note": "通常運転"})


def compute_dd(equity: float, peak: float) -> float:
    """dd = 1 - equity/peak（peak<=0 は 0.0）。"""
    if peak <= 0:
        return 0.0
    return max(0.0, 1.0 - equity / peak)


# ── I/O層 ────────────────────────────────────────────────────

def _read_csv(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _state_path(cfg) -> Path:
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "dd_state.json"


def _load_state(cfg) -> dict:
    """dd_state.json を読む。破損/不在時は安全な初期状態。"""
    p = _state_path(cfg)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and "state" in d and "peak" in d:
                return {"state": d.get("state", "NORMAL"),
                        "peak": float(d.get("peak", 0.0) or 0.0)}
        except Exception:
            pass
    return {"state": "NORMAL", "peak": 0.0}


def _save_state(cfg, state: str, peak: float, equity: float, dd: float) -> None:
    p = _state_path(cfg)
    try:
        p.write_text(json.dumps(
            {"state": state, "peak": round(peak, 2),
             "equity": round(equity, 2), "dd": round(dd, 4),
             "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️ [dd_controller] 状態保存失敗: {str(e)[:60]}")


def compute_portfolio_equity(cfg) -> tuple[float, int]:
    """
    portfolio_holdings.csv × 当日価格（fetch_price_safe）で投資部分の評価額を算出。
    Returns: (equity, 評価できた銘柄数)。取得不能銘柄は簿価で代替。
    """
    base_dir = Path(getattr(cfg, "PROJECT_DIR", "."))
    holdings_path = Path(getattr(cfg, "PORTFOLIO_HOLDINGS_PATH",
                                 base_dir / "data" / "portfolio_holdings.csv"))
    holdings = _read_csv(holdings_path)
    if not holdings:
        return 0.0, 0
    try:
        from data_reliability import fetch_price_safe
    except Exception:
        fetch_price_safe = None

    from sync_holdings import usd_jpy, price_in_jpy
    fx = usd_jpy(cfg)
    equity, n = 0.0, 0
    for h in holdings:
        ticker = (h.get("ticker") or "").strip()
        qty = float(h.get("quantity", 0) or 0)
        avg = float(h.get("avg_cost_jpy", 0) or 0)
        if not ticker or qty <= 0:
            continue
        price = None
        if fetch_price_safe:
            try:
                price = fetch_price_safe(cfg, ticker).get("close")
            except Exception:
                price = None
        if price is None:
            price = avg          # 取得不能は簿価で代替（保守的）
        else:
            price = price_in_jpy(h, price, fx)   # 米国株はドル→円
            n += 1
        equity += float(price) * qty
    return equity, n


def run_dd_controller(cfg, results: dict) -> dict:
    """
    メインエントリ（stock_analyzer_main の日次ステージ）。
    Returns: results["dd_state"] に入る dict。
    """
    if not getattr(cfg, "ENABLE_DD_CONTROL", False):
        return {"skipped": True}

    equity, priced = compute_portfolio_equity(cfg)
    if equity <= 0:
        print("    ⏩ [dd_controller] portfolio_holdings なし/空 → スキップ")
        return {"state": "NORMAL", "dd": 0.0, "equity": 0.0,
                "peak": 0.0, "priced": 0, "skipped": True}

    prev = _load_state(cfg)
    peak = max(prev["peak"], equity)          # peak は当日equityで更新
    dd = compute_dd(equity, peak)
    new_state = next_state(prev["state"], dd, cfg)
    action = state_action(new_state)
    transitioned = new_state != prev["state"]

    _save_state(cfg, new_state, peak, equity, dd)

    icon = {"NORMAL": "🟢", "CAUTION": "🟡", "DEFENSE": "🔴"}.get(new_state, "⚪")
    arrow = f"（{prev['state']}→{new_state}）" if transitioned else ""
    print(f"    {icon} [dd_controller] {new_state}{arrow} "
          f"dd={dd:.1%} equity=¥{equity:,.0f} peak=¥{peak:,.0f} "
          f"評価{priced}銘柄")

    return {
        "state": new_state,
        "prev_state": prev["state"],
        "transitioned": transitioned,
        "dd": round(dd, 4),
        "equity": round(equity, 2),
        "peak": round(peak, 2),
        "priced": priced,
        "action": action,
        "note": action["note"],
        "skipped": False,
    }
