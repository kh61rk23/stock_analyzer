# ==============================================================
# ml/dataset_builder.py — 特徴量スナップショット蓄積（Wave R5-1 / S7）
# ==============================================================
# 【役割】日次バッチで (date, ticker) の特徴量行を features.jsonl へ追記する。
#   特徴量 = 23カテゴリスコア（signal_aggregator の sector components）＋
#            レジーム（economic_cycle 4象限）＋ size_tier ＋ 業種。
#   目的変数（20日先の対TOPIX超過リターン）は蓄積時点では未知なので空にし、
#   Colab の train_meta.ipynb が価格履歴と突合して後付けする（label_rows）。
# 【設計正本】要件定義書 §R5-1（データセット・リーク防止が最重要）。
# 【原則】ここは「蓄積」のみ。学習・補正適用は行わない（corrector_applier / notebook）。
# 使い方:
#   （日次）stock_analyzer_main が snapshot(cfg, results) を呼ぶ
#   （手動）python3 -m ml.dataset_builder            → 現在のデータセット統計を表示
# ==============================================================

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    from config import JST  # type: ignore
except Exception:  # pragma: no cover - config 無し環境
    JST = None


# ── 純粋ロジック（pytest対象） ────────────────────────────────

def build_feature_rows(cfg, results: dict, today: str) -> list:
    """
    results から (date, ticker) 行のリストを構築する（純粋・副作用なし）。

    - 23カテゴリは results["signal_aggregator"]["sector_signals"][code]["components"]。
    - レジームは economic_cycle.phase（A/B/C/D）。無ければ market_regime。
    - ユニバースは config.SECTOR_REPRESENTATIVE_TICKERS（業種→代表銘柄）。
    - size_tier は size_tier_lookup（{ticker: tier}）を best-effort 参照。
    Returns: [{"date","ticker","sector","regime","size_tier","f_<cat>":..}, ...]
    """
    sa = (results.get("signal_aggregator") or {})
    sector_signals = sa.get("sector_signals") or {}
    regime = ((results.get("economic_cycle") or {}).get("phase")
              or sa.get("market_regime") or "NA")
    rep = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {}) or {}
    size_lookup = _size_tier_lookup(cfg, results)

    rows = []
    seen = set()
    for sector_code, sig in sector_signals.items():
        if not isinstance(sig, dict):
            continue
        comps = sig.get("components") or {}
        # 有効なカテゴリ（None でない）が1つも無ければ蓄積しない
        feat = {f"f_{cat}": v for cat, v in comps.items()
                if isinstance(v, (int, float))}
        if not feat:
            continue
        for ticker in rep.get(str(sector_code), []):
            key = (today, ticker, str(sector_code))
            if key in seen:
                continue
            seen.add(key)
            row = {
                "date": today,
                "ticker": ticker,
                "sector": str(sector_code),
                "regime": regime,
                "size_tier": size_lookup.get(ticker),
                "target": None,   # 20日先超過リターン（後付け）
            }
            row.update(feat)
            rows.append(row)
    return rows


def _size_tier_lookup(cfg, results: dict) -> dict:
    """size_tier を {ticker: tier} で best-effort 取得（results→cacheの順）。"""
    # 1) results 内に載っていれば優先
    st = results.get("size_tier") or results.get("size_tiers") or {}
    if isinstance(st, dict) and st:
        out = {}
        for k, v in st.items():
            if isinstance(v, dict):
                out[k] = v.get("size_tier") or v.get("tier")
            else:
                out[k] = v
        if out:
            return out
    # 2) size_tier_master.json（週次キャッシュ）
    try:
        cache_dir = Path(getattr(cfg, "CACHE_DIR", REPO / "cache"))
        p = cache_dir / "size_tier_master.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            out = {}
            for k, v in data.items():
                if isinstance(v, dict):
                    out[k] = v.get("size_tier") or v.get("tier")
                else:
                    out[k] = v
            return out
    except Exception:
        pass
    return {}


def forward_excess_returns(sorted_dates: list, price_by_date: dict,
                           bench_by_date: dict, horizon: int = None) -> dict:
    """
    目的変数 = H営業日先リターン − ベンチ同期間リターン（超過リターン）。

    ★リーク防止の中核: date i の目的変数は i と i+H（未来）のみに依存し、
      過去や当日確定情報は一切使わない。i+H が範囲外なら算出不能（除外）。

    sorted_dates: 昇順の取引日リスト（銘柄・ベンチ共通の営業日軸）
    price_by_date / bench_by_date: {date: close}
    horizon: 省略時は config.ML_META_TARGET_HORIZON_D（既定20）
             ※2026-07-25 バッチ6: 従来は呼び出し側の直書きのみで、
               config 側の定数は誰にも読まれていなかった
    Returns: {date: 超過リターン}（算出できた日付のみ）
    """
    if horizon is None:
        from ml.purged_cv import _cfg_default
        horizon = _cfg_default("ML_META_TARGET_HORIZON_D", 20)
    out = {}
    n = len(sorted_dates)
    for i in range(n):
        j = i + horizon
        if j >= n:
            break
        d0, dH = sorted_dates[i], sorted_dates[j]
        p0, pH = price_by_date.get(d0), price_by_date.get(dH)
        b0, bH = bench_by_date.get(d0), bench_by_date.get(dH)
        if not p0 or not pH or not b0 or not bH:
            continue
        out[d0] = (pH / p0 - 1.0) - (bH / b0 - 1.0)
    return out


# ── I/O層 ────────────────────────────────────────────────────

def _dataset_path(cfg) -> Path:
    return Path(getattr(cfg, "ML_META_DATASET_PATH",
                        REPO / "data" / "ml_dataset" / "features.jsonl"))


def snapshot(cfg, results: dict) -> dict:
    """
    今日の特徴量行を features.jsonl へ追記し、保持日数で間引く。
    Returns: {"rows","date","total_rows","path","skipped"}
    """
    today = (datetime.now(JST) if JST else datetime.now()).strftime("%Y-%m-%d")
    rows = build_feature_rows(cfg, results, today)
    path = _dataset_path(cfg)
    if not rows:
        return {"skipped": True, "rows": 0, "date": today, "path": str(path)}

    path.parent.mkdir(parents=True, exist_ok=True)
    # 既存を読み、同一日付の行は置き換え（冪等・再実行安全）
    existing = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("date") != today:
                existing.append(r)
    existing.extend(rows)

    # 保持日数で間引き（古い日付から削除）
    max_days = int(getattr(cfg, "ML_META_DATASET_MAX_DAYS", 500))
    all_dates = sorted({r.get("date") for r in existing})
    if len(all_dates) > max_days:
        keep = set(all_dates[-max_days:])
        existing = [r for r in existing if r.get("date") in keep]

    with open(path, "w", encoding="utf-8") as f:
        for r in existing:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {"skipped": False, "rows": len(rows), "date": today,
            "total_rows": len(existing), "path": str(path)}


def load_dataset(cfg) -> list:
    """features.jsonl を読み込み、行のリストで返す（notebook/検査用）。"""
    path = _dataset_path(cfg)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def main():
    try:
        import config as cfg
    except Exception:
        print("⚠️  config を読めません")
        return
    rows = load_dataset(cfg)
    if not rows:
        print(f"（データセット未生成: {_dataset_path(cfg)}）")
        return
    dates = sorted({r.get("date") for r in rows})
    tickers = sorted({r.get("ticker") for r in rows})
    labeled = sum(1 for r in rows if r.get("target") is not None)
    print(f"✅ ML dataset: {len(rows)}行 / {len(dates)}日 "
          f"({dates[0]}〜{dates[-1]}) / {len(tickers)}銘柄 / "
          f"ラベル付与済み{labeled}行")


if __name__ == "__main__":
    main()
