# ==============================================================
# institutional_signal_scorer.py  v1.0  (Policy Ph3)
# ==============================================================
# 【役割】
#   institutional_13f_monitor.py の出力を受け取り、
#   機関投資家シグナルのスコアリングと Level 3 Override 判定を行う。
#
# 【Level 3 Override とは】
#   screener 候補リストの順位を直接変更する強制介入。
#   通常の composite_score 加算（L4以下）とは異なり、
#   対象業種の銘柄を強制的にリスト先頭またはリスト末尾に移動する。
#
# 【Level 3 Override 発動条件】
#   Tier 1（Buffett）の新規取得 → 無条件で L3 Override 発動
#   Tier 2 の新規取得 + adj_pt >= 8.0 → L3 Override 発動
#   Tier 3 の新規取得 + value >= 10億ドル → L3 Override 発動
#
# 【スコアリング出力スキーマ】
#   {
#     "sector_boosts":     {...},   # {sector_code: adj_pt}
#     "override_tickers":  [...],   # L3 Override 対象ティッカー
#     "override_sectors":  [...],   # L3 Override 対象業種コード
#     "l3_triggered":      bool,
#     "scored":            [...],   # スコアリング詳細リスト
#     "summary":           str,
#   }
#
# 【依存】
#   config.py (BLOCK 35: INSTITUTION_TIER_MAP /
#              SCORE_MAX_INSTITUTION_ADJ /
#              INSTITUTION_MIN_NEW_POSITION_USD)
#   institutional_13f_monitor.py (run_institutional_13f_monitor の出力)
#   policy_signal_applier.py (register_signals / load_policy_short_cache)
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: L3 Override 判定定数
# ==============================================================

# Tier 別 L3 Override 発動閾値
_L3_TRIGGER: dict = {
    # Tier1/2 も1億ドル未満は対象外（config.INSTITUTION_MIN_NEW_POSITION_USD_ACTIVE）
    1: {"new_position": True,  "min_adj": 0.0,  "min_usd": 100_000_000},
    2: {"new_position": True,  "min_adj": 8.0,  "min_usd": 100_000_000},
    3: {"new_position": True,  "min_adj": 0.0,  "min_usd": 1_000_000_000}, # Tier3は10億ドル超
}


# ==============================================================
# BLOCK 2: スコアリング
# ==============================================================

def _score_institutional_signals(
    monitor_result: dict,
    cfg,
) -> list[dict]:
    """
    institutional_13f_monitor の出力を受け取り、
    各変化にスコアを付与した詳細リストを返す。

    Returns:
        [{"ticker": str, "institution": str, "tier": int,
          "change_type": str, "adj_pt": float,
          "value_usd": int, "sector_code": str,
          "l3_trigger": bool}, ...]
    """
    scored_raw = monitor_result.get("scored", [])
    if not scored_raw:
        return []

    max_adj  = float(getattr(cfg, "SCORE_MAX_INSTITUTION_ADJ", 15.0))
    min_usd3 = float(getattr(cfg, "INSTITUTION_MIN_NEW_POSITION_USD",
                              1_000_000_000))

    result = []
    for s in scored_raw:
        tier        = int(s.get("tier", 3))
        change_type = s.get("change_type", "unchanged")
        adj_pt      = float(s.get("adj_pt", 0.0))
        value_usd   = int(s.get("value_usd", 0))

        # L3 Override 判定
        l3 = False
        trigger_rule = _L3_TRIGGER.get(tier, {})
        if (change_type == "new_position"
                and trigger_rule.get("new_position")
                and abs(adj_pt) >= trigger_rule.get("min_adj", 0.0)
                and value_usd  >= trigger_rule.get("min_usd", 0)):
            l3 = True

        result.append({
            "ticker":      s.get("ticker", ""),
            "institution": s.get("institution", ""),
            "tier":        tier,
            "change_type": change_type,
            "adj_pt":      adj_pt,
            "value_usd":   value_usd,
            "name":        s.get("name", ""),
            "detected_at": s.get("detected_at", ""),
            "l3_trigger":  l3,
        })

    # adj_pt 絶対値降順でソート
    result.sort(key=lambda x: abs(x["adj_pt"]), reverse=True)
    return result


def _build_sector_boosts(
    scored:    list[dict],
    results:   dict,
    cfg,
) -> dict[str, float]:
    """
    ticker → sector_code マッピングを screener candidates から構築し、
    業種別ブーストを集計する。

    Returns:
        {sector_code: total_adj_pt}
    """
    # ticker → sector_code マッピング
    ticker_sector: dict[str, str] = {}
    screener = results.get("screener", {})
    for cand in (screener.get("candidates", [])
                 if isinstance(screener, dict) else []):
        t  = cand.get("ticker", "")
        sc = cand.get("sector_code", "")
        if t and sc:
            ticker_sector[t] = sc

    # 13f monitor 側の sector_boosts を優先使用（ticker→sector変換不要）
    monitor_boosts = results.get("institutional_13f", {}).get("sector_boosts", {})
    if monitor_boosts:
        return monitor_boosts

    max_adj = float(getattr(cfg, "SCORE_MAX_INSTITUTION_ADJ", 15.0))
    boosts: dict[str, float] = {}

    for s in scored:
        ticker = s.get("ticker", "")
        sc     = ticker_sector.get(ticker, "")
        if not sc:
            continue
        adj       = s.get("adj_pt", 0.0)
        boosts[sc] = round(
            max(-max_adj, min(max_adj, boosts.get(sc, 0.0) + adj)), 2
        )

    return boosts


# ==============================================================
# BLOCK 3: Level 3 Override 適用
# ==============================================================

def _apply_l3_override(
    scored:    list[dict],
    results:   dict,
    cfg,
) -> tuple[list[str], list[str]]:
    """
    L3 Override 対象のティッカー・業種コードを特定し、
    screener candidates の順位を直接変更する。

    L3 Override の動作:
      - tailwind（adj_pt > 0）: 候補リストの先頭グループ（index 0〜4）に挿入
      - headwind（adj_pt < 0）: 候補リストの末尾に移動

    Args:
        scored:  _score_institutional_signals() の出力
        results: stock_analyzer_main.results
        cfg:     config モジュール

    Returns:
        (override_tickers, override_sectors)
    """
    l3_items = [s for s in scored if s.get("l3_trigger")]
    if not l3_items:
        return [], []

    screener   = results.get("screener", {})
    candidates = (screener.get("candidates", [])
                  if isinstance(screener, dict) else [])

    override_tickers: list[str] = []
    override_sectors: list[str] = []

    # ticker → sector_code マッピング
    ticker_sector: dict[str, str] = {}
    for cand in candidates:
        t  = cand.get("ticker", "")
        sc = cand.get("sector_code", "")
        if t and sc:
            ticker_sector[t] = sc

    for item in l3_items:
        ticker = item.get("ticker", "")
        adj_pt = item.get("adj_pt", 0.0)
        if not ticker:
            continue

        # 対象銘柄を candidates から検索
        target_idx = next(
            (i for i, c in enumerate(candidates)
             if c.get("ticker") == ticker),
            None,
        )

        if target_idx is None:
            # candidates にない場合は新規エントリとして先頭に追加
            if adj_pt > 0:
                candidates.insert(0, {
                    "ticker":              ticker,
                    "sector_code":         ticker_sector.get(ticker, ""),
                    "composite_score":     item.get("adj_pt", 0.0),
                    "l3_override":         True,
                    "l3_institution":      item.get("institution", ""),
                    "l3_tier":             item.get("tier", 3),
                    "l3_override_adj":     adj_pt,
                })
        else:
            target = candidates.pop(target_idx)
            target["l3_override"]       = True
            target["l3_institution"]    = item.get("institution", "")
            target["l3_tier"]           = item.get("tier", 3)
            target["l3_override_adj"]   = adj_pt

            if adj_pt > 0:
                # 先頭グループに挿入（既存の L3 Override の後ろ）
                insert_pos = sum(
                    1 for c in candidates if c.get("l3_override")
                )
                candidates.insert(min(insert_pos, 4), target)
            else:
                # 末尾に追加
                candidates.append(target)

        override_tickers.append(ticker)
        sc = ticker_sector.get(ticker, "")
        if sc and sc not in override_sectors:
            override_sectors.append(sc)

        print(
            f"    ⭐ [L3 Override] {ticker} "
            f"({item['institution']}) "
            f"adj={adj_pt:+.1f}pt "
            f"→ {'先頭' if adj_pt > 0 else '末尾'}へ移動"
        )

    # candidates を更新
    if isinstance(screener, dict):
        if "candidates" in screener:
            screener["candidates"] = candidates
        elif "passed" in screener:
            screener["passed"] = candidates
        results["screener"] = screener

    return override_tickers, override_sectors


# ==============================================================
# BLOCK 4: policy_medium_cache 登録
# ==============================================================

def _register_to_medium_cache(
    sector_boosts: dict[str, float],
    scored:        list[dict],
    cfg,
) -> int:
    """
    機関投資家シグナルを policy_medium_cache.json (L3) に登録する。
    Tier 1 / Tier 2 の新規取得のみ対象。

    Returns:
        int: 登録件数
    """
    registered = 0
    today      = datetime.now().strftime("%Y-%m-%d")
    cache_dir  = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    path       = cache_dir / "policy_medium_cache.json"

    try:
        if path.exists():
            medium = json.loads(path.read_text(encoding="utf-8"))
        else:
            medium = {"signals": [], "last_updated": ""}

        existing_keys = {
            s.get("policy_key") for s in medium.get("signals", [])
        }

        for s in scored:
            if not s.get("l3_trigger"):
                continue
            if s.get("tier", 3) > 2:
                continue   # Tier 3 以下は medium_cache 不要

            ticker = s.get("ticker", "")
            inst   = s.get("institution", "")
            key    = f"institutional_l3_{ticker}_{inst[:10]}"

            if key in existing_keys:
                continue

            medium.setdefault("signals", []).append({
                "policy_key":    key,
                "ticker":        ticker,
                "institution":   inst,
                "tier":          s.get("tier"),
                "adj_pt":        s.get("adj_pt", 0.0),
                "change_type":   s.get("change_type", ""),
                "registered_at": today,
                "source":        "institutional_l3",
                "label":         f"{inst} 新規取得 ({ticker})",
            })
            existing_keys.add(key)
            registered += 1

        medium["last_updated"] = today
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(medium, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    except Exception as e:
        print(f"    ⚠️  [inst_scorer] medium_cache 登録失敗: "
              f"{str(e)[:60]}")

    return registered


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_institutional_signal_scorer(
    monitor_result: dict,
    results:        dict,
    cfg,
) -> dict:
    """
    機関投資家シグナルスコアリングのメイン関数。
    stock_analyzer_main.py [17.7] から institutional_13f_monitor の
    直後に呼ばれる。

    Args:
        monitor_result: run_institutional_13f_monitor() の返り値
        results:        stock_analyzer_main.results
        cfg:            config モジュール

    Returns:
        {
          "sector_boosts":     dict,
          "override_tickers":  list,
          "override_sectors":  list,
          "l3_triggered":      bool,
          "scored":            list,
          "medium_registered": int,
          "summary":           str,
        }
    """
    print("  🏦 [inst_scorer] 機関投資家シグナルスコアリング...")

    try:
        # ── スコアリング ──────────────────────────────────────
        scored = _score_institutional_signals(monitor_result, cfg)

        # ── 業種別ブースト計算 ────────────────────────────────
        sector_boosts = _build_sector_boosts(scored, results, cfg)

        # ── L3 Override 適用 ──────────────────────────────────
        override_tickers, override_sectors = [], []
        l3_triggered = any(s.get("l3_trigger") for s in scored)

        if l3_triggered:
            print(
                f"    ⭐ [L3 Override] 発動: "
                f"{sum(1 for s in scored if s.get('l3_trigger'))}件"
            )
            override_tickers, override_sectors = _apply_l3_override(
                scored, results, cfg
            )

        # ── policy_medium_cache (L3) 登録 ────────────────────
        medium_registered = _register_to_medium_cache(
            sector_boosts, scored, cfg
        )
        if medium_registered:
            print(
                f"    📋 [inst_scorer] medium_cache 登録: "
                f"{medium_registered}件"
            )

        # ── policy_short_cache への登録 ───────────────────────
        if sector_boosts:
            try:
                import sys as _sys, importlib as _imp, pathlib as _pl
                _sys.path.insert(0, str(_pl.Path(__file__).parent))
                _psa   = _imp.import_module("policy_signal_applier")
                _cache = _psa.load_policy_short_cache(cfg)
                _cache = _psa.register_signals(
                    cache         = _cache,
                    sector_boosts = sector_boosts,
                    source        = "institutional_13f",
                    politician    = "institutional_signal_scorer",
                    asset         = "",
                    direction     = "buy",
                    cfg           = cfg,
                )
                _psa.save_policy_short_cache(_cache, cfg)
                print(
                    f"    📋 [inst_scorer] policy_short_cache 登録: "
                    f"{len(sector_boosts)}業種"
                )
            except Exception as e:
                print(f"    ⚠️  [inst_scorer] short_cache 登録失敗: "
                      f"{str(e)[:60]}")

        summary = (
            f"スコア済み{len(scored)}件 / "
            f"業種boost{len(sector_boosts)}業種 / "
            f"L3={'発動' if l3_triggered else 'なし'} "
            f"({len(override_tickers)}銘柄) / "
            f"medium_cache+{medium_registered}件"
        )
        print(f"  ✅ [inst_scorer] {summary}")

        return {
            "sector_boosts":     sector_boosts,
            "override_tickers":  override_tickers,
            "override_sectors":  override_sectors,
            "l3_triggered":      l3_triggered,
            "scored":            scored[:10],
            "medium_registered": medium_registered,
            "summary":           summary,
        }

    except Exception as e:
        msg = f"[inst_scorer] 致命的エラー: {str(e)[:80]}"
        print(f"  ⚠️  {msg}")
        return {
            "sector_boosts":     {},
            "override_tickers":  [],
            "override_sectors":  [],
            "l3_triggered":      False,
            "scored":            [],
            "medium_registered": 0,
            "summary":           msg,
        }


# ==============================================================
# BLOCK 6: 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util

    cfg = None
    for p in ["/content/config.py", "config.py"]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            if spec and spec.loader:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    if cfg is None:
        print("ERROR: config.py が見つかりません")
        raise SystemExit(1)

    print("=== institutional_signal_scorer 単体テスト ===\n")

    # ── L3 Override 発動検証 ──────────────────────────────────
    mock_monitor = {
        "scored": [
            # Tier1 new_position → L3無条件発動
            {"ticker": "AAPL",  "institution": "Berkshire Hathaway Inc",
             "tier": 1, "change_type": "new_position",
             "adj_pt": 15.0, "value_usd": 5_000_000_000,
             "name": "Apple Inc", "detected_at": "2026-05-31 10:00:00"},
            # Tier2 new_position adj=10 → L3発動（>=8.0）
            {"ticker": "NVDA",  "institution": "Pershing Square Capital Management",
             "tier": 2, "change_type": "new_position",
             "adj_pt": 10.0, "value_usd": 800_000_000,
             "name": "Nvidia", "detected_at": "2026-05-31 10:00:00"},
            # Tier3 new_position 5億ドル → L3非発動（<10億ドル）
            {"ticker": "MSFT",  "institution": "BlackRock Inc",
             "tier": 3, "change_type": "new_position",
             "adj_pt": 5.0, "value_usd": 500_000_000,
             "name": "Microsoft", "detected_at": "2026-05-31 10:00:00"},
            # Tier3 new_position 15億ドル → L3発動（>=10億ドル）
            {"ticker": "GOOGL", "institution": "Vanguard Group",
             "tier": 3, "change_type": "new_position",
             "adj_pt": 5.0, "value_usd": 1_500_000_000,
             "name": "Alphabet", "detected_at": "2026-05-31 10:00:00"},
        ],
        "sector_boosts": {"1016": 12.0, "1025": 8.0},
    }

    mock_results = {
        "screener": {
            "candidates": [
                {"ticker": "6758.T", "sector_code": "1016",
                 "composite_score": 72.0},
                {"ticker": "9984.T", "sector_code": "1025",
                 "composite_score": 68.0},
                {"ticker": "6861.T", "sector_code": "1016",
                 "composite_score": 65.0},
            ]
        },
        "institutional_13f": mock_monitor,
    }

    scored = _score_institutional_signals(mock_monitor, cfg)
    print("  ── L3 トリガー判定 ──")
    for s in scored:
        l3 = "✅L3" if s["l3_trigger"] else "  -"
        print(f"  {l3} [{s['ticker']}] tier={s['tier']} "
              f"{s['change_type']} adj={s['adj_pt']:+.1f}pt "
              f"usd={s['value_usd']:,}")

    # AAPL（Tier1）と GOOGL（Tier3 15億ドル）はL3発動
    assert any(s["ticker"] == "AAPL"  and s["l3_trigger"] for s in scored), \
        "AAPL L3未発動"
    assert any(s["ticker"] == "NVDA"  and s["l3_trigger"] for s in scored), \
        "NVDA L3未発動"
    assert any(s["ticker"] == "GOOGL" and s["l3_trigger"] for s in scored), \
        "GOOGL L3未発動"
    # MSFT（5億ドル）はL3非発動
    assert not any(s["ticker"] == "MSFT" and s["l3_trigger"] for s in scored), \
        "MSFT L3誤発動"
    print("  ✅ L3 トリガー判定 PASS")

    result = run_institutional_signal_scorer(mock_monitor, mock_results, cfg)
    print(f"\n  summary: {result['summary']}")
    print(f"  override_tickers: {result['override_tickers']}")
    print("\n  ✅ 全テスト PASS")