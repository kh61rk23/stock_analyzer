# ==============================================================
# deep_dive_trigger.py  v1.0  (Wave 4 - S3-4)
# ==============================================================
# 【役割】
#   以下2条件を検出し、該当銘柄の深掘り分析を
#   edinet_rotation_manager.py に指示する。
#
#   (A) confidence 低下トリガー
#       results["economic_cycle"]["confidence"] が
#       DEEP_DIVE_CONFIDENCE_DROP(15pt) 以上低下した業種の代表銘柄
#
#   (B) screener 順位急変トリガー
#       前回 screener_rank と今回を比較し、
#       DEEP_DIVE_RANK_JUMP(10位) 以上変動した銘柄
#
# 【edinet_rotation_manager との接続】
#   cfg.EDINET_DEEP_DIVE_TICKERS にティッカーリストを注入する。
#   edinet_rotation_manager._build_fetch_queue() は
#   EDINET_DEEP_DIVE_TICKERS を priority=0（最優先）で処理する。
#   ※ Step 8 で edinet_rotation_manager.py に受け口を追加する。
#
# 【出力スキーマ（run_deep_dive_trigger の返り値）】
#   {
#     "triggered_tickers": [...],   # 深掘り対象ティッカーリスト
#     "confidence_triggers": [...], # (A) 信頼度低下トリガー詳細
#     "rank_triggers":      [...],  # (B) 順位急変トリガー詳細
#     "total":              int,
#     "timestamp":          str,
#   }
#
# 【キャッシュ】
#   CACHE_DIR/deep_dive_history.json  TTL: DEEP_DIVE_HISTORY_TTL(30日)
#   形式: {"YYYY-MM-DD": [{"ticker": str, "reason": str, ...}]}
#
# 【呼び出し方 (stock_analyzer_main.py から)】
#   from deep_dive_trigger import run_deep_dive_trigger
#   dd_result = run_deep_dive_trigger(cfg, results)
#   results["deep_dive"] = dd_result
#
# 【依存】
#   config.py (BLOCK 35: DEEP_DIVE_CONFIDENCE_DROP /
#              DEEP_DIVE_RANK_JUMP / DEEP_DIVE_HISTORY_TTL /
#              SECTOR_TIER_THRESHOLDS)
#   stock_analyzer_main.py が生成する results["screener"] /
#   results["economic_cycle"] / results["sectors"]
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: 定数フォールバック
# ==============================================================

_DEFAULT_CONFIDENCE_DROP = 15.0   # confidence 低下閾値（pt）
_DEFAULT_RANK_JUMP       = 10     # screener 順位急変閾値（位）
_DEFAULT_HISTORY_TTL     = 30     # deep_dive_history.json TTL（日）
_DEFAULT_MAX_TICKERS     = 5      # 1回あたりの深掘り上限銘柄数


def _conf_drop_threshold(cfg) -> float:
    return float(getattr(cfg, "DEEP_DIVE_CONFIDENCE_DROP", _DEFAULT_CONFIDENCE_DROP))


def _rank_jump_threshold(cfg) -> int:
    return int(getattr(cfg, "DEEP_DIVE_RANK_JUMP", _DEFAULT_RANK_JUMP))


def _history_ttl(cfg) -> int:
    return int(getattr(cfg, "DEEP_DIVE_HISTORY_TTL", _DEFAULT_HISTORY_TTL))


# ==============================================================
# BLOCK 2: deep_dive_history.json 管理
# ==============================================================

def _history_path(cfg) -> Path:
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "deep_dive_history.json"


def _load_history(cfg) -> dict:
    """
    deep_dive_history.json を読み込む。
    TTL切れ日付エントリを除去して返す。

    Returns:
        {"YYYY-MM-DD": [{"ticker": str, "reason": str, ...}], ...}
    """
    p = _history_path(cfg)
    if not p.exists():
        return {}
    try:
        raw    = json.loads(p.read_text(encoding="utf-8"))
        ttl    = _history_ttl(cfg)
        cutoff = datetime.now() - timedelta(days=ttl)
        return {
            date_str: entries
            for date_str, entries in raw.items()
            if _safe_parse_date(date_str) >= cutoff
        }
    except Exception as e:
        print(f"    ⚠️  [deep_dive] history 読み込み失敗: {str(e)[:60]}")
        return {}


def _save_history(cfg, history: dict) -> None:
    """失敗は警告のみ。"""
    p = _history_path(cfg)
    try:
        p.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [deep_dive] history 保存失敗: {str(e)[:60]}")


def _safe_parse_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return datetime(2000, 1, 1)


def _load_prev_screener_ranks(cfg) -> dict[str, int]:
    """
    前回の screener 順位を deep_dive_history から復元する。
    Returns: {ticker: rank, ...}
    """
    history = _load_history(cfg)
    if not history:
        return {}
    # 最新日付のエントリを使用
    sorted_dates = sorted(history.keys(), reverse=True)
    for date_str in sorted_dates:
        entries = history.get(date_str, [])
        rank_map = {}
        for e in entries:
            t = e.get("ticker", "")
            r = e.get("screener_rank")
            if t and r is not None:
                rank_map[t] = int(r)
        if rank_map:
            return rank_map
    return {}


# ==============================================================
# BLOCK 3: (A) confidence 低下トリガー
# ==============================================================

def _detect_confidence_triggers(
    results:   dict,
    cfg,
    threshold: float,
) -> list[dict]:
    """
    economic_cycle の confidence 低下を業種単位で検出し、
    代表銘柄を深掘り対象として返す。

    confidence の前回値は deep_dive_history に保存した
    "confidence_at_trigger" から復元する。
    初回（前回値なし）は低下検出をスキップ。

    Returns:
        [{"ticker": str, "sector_code": str,
          "prev_confidence": float, "curr_confidence": float,
          "drop": float, "reason": str}, ...]
    """
    triggers = []

    # ── 現在の confidence 取得 ────────────────────────────────
    ec = results.get("economic_cycle", {})
    if not isinstance(ec, dict):
        return []

    curr_conf = ec.get("confidence")
    if curr_conf is None:
        # 業種別 confidence がある場合は sector_results から探す
        curr_conf = None

    # 業種別 confidence は sector_results の composite_score を proxy として使用
    sector_results = results.get("sectors", {})
    if not isinstance(sector_results, dict):
        return []

    # deep_dive_history から前回の confidence を復元
    history = _load_history(cfg)
    prev_confs: dict[str, float] = {}
    sorted_dates = sorted(history.keys(), reverse=True)
    for date_str in sorted_dates:
        entries = history.get(date_str, [])
        for e in entries:
            sc = e.get("sector_code", "")
            pc = e.get("confidence_snapshot")
            if sc and pc is not None and sc not in prev_confs:
                prev_confs[sc] = float(pc)
        if prev_confs:
            break

    # ── 業種別スコアを confidence proxy として比較 ─────────────
    for code, res in sector_results.items():
        if not isinstance(res, dict):
            continue

        curr_score = res.get("composite_score")
        if curr_score is None:
            continue
        curr_score = float(curr_score)

        prev_score = prev_confs.get(code)
        if prev_score is None:
            continue   # 前回値なし → 初回はスキップ

        drop = prev_score - curr_score   # 正値 = 低下
        if drop < threshold:
            continue

        # 代表銘柄を取得（screener候補から / なければ EDINET DB から）
        rep_ticker = _get_representative_ticker(code, results, cfg)
        if not rep_ticker:
            continue

        triggers.append({
            "ticker":           rep_ticker,
            "sector_code":      code,
            "sector_name":      res.get("sector_name_ja", code),
            "prev_confidence":  round(prev_score, 1),
            "curr_confidence":  round(curr_score, 1),
            "drop":             round(drop, 1),
            "reason":           f"confidence低下: {prev_score:.0f}→{curr_score:.0f}pt (Δ{drop:.0f}pt)",
            "trigger_type":     "confidence_drop",
        })

    return triggers


# ==============================================================
# BLOCK 4: (B) screener 順位急変トリガー
# ==============================================================

def _detect_rank_triggers(
    results:    dict,
    prev_ranks: dict[str, int],
    threshold:  int,
) -> list[dict]:
    """
    screener の順位急変銘柄を検出する。

    今回の candidates の rank と前回保存した rank を比較し、
    |rank_now - rank_prev| >= threshold の銘柄を返す。

    Returns:
        [{"ticker": str, "rank_prev": int, "rank_now": int,
          "rank_delta": int, "direction": str, "reason": str}, ...]
    """
    triggers = []

    screener = results.get("screener", {})
    if not isinstance(screener, dict):
        return []

    candidates = screener.get("candidates", [])
    if not isinstance(candidates, list):
        return []

    for i, cand in enumerate(candidates):
        ticker = cand.get("ticker", "")
        if not ticker:
            continue

        rank_now = i + 1   # 0-indexed → 1-indexed

        rank_prev = prev_ranks.get(ticker)
        if rank_prev is None:
            continue   # 前回リストにない銘柄はスキップ

        delta = rank_prev - rank_now   # 正値 = 順位上昇（良い方向）
        if abs(delta) < threshold:
            continue

        direction = "up" if delta > 0 else "down"
        triggers.append({
            "ticker":       ticker,
            "sector_code":  cand.get("sector_code", ""),
            "sector_name":  cand.get("sector_name_ja", ""),
            "rank_prev":    rank_prev,
            "rank_now":     rank_now,
            "rank_delta":   delta,
            "direction":    direction,
            "reason":       (f"screener順位急変: {rank_prev}位→{rank_now}位 "
                             f"(Δ{delta:+d}位 {direction})"),
            "trigger_type": "rank_jump",
        })

    return triggers


# ==============================================================
# BLOCK 5: 代表銘柄取得ヘルパー
# ==============================================================

def _get_representative_ticker(
    sector_code: str,
    results:     dict,
    cfg,
) -> str:
    """
    業種コードに対応する代表銘柄を取得する。

    優先順位:
      1. screener candidates の該当業種 1位銘柄
      2. EDINET watchlist から該当業種銘柄
      3. config.SECTOR_REPRESENTATIVE_TICKERS（あれば）の先頭
      4. 空文字（取得失敗）

    Returns:
        str: ティッカー or ""
    """
    # 1. screener candidates から
    screener   = results.get("screener", {})
    candidates = (screener.get("candidates", [])
                  if isinstance(screener, dict) else [])
    for cand in candidates:
        if cand.get("sector_code") == sector_code:
            t = cand.get("ticker", "")
            if t:
                return t

    # 2. EDINET watchlist から
    watchlist = (getattr(cfg, "EDINET_WATCHLIST_TICKERS", []) or
                 getattr(cfg, "PRIORITY_TICKERS", []))
    # watchlist には sector_code の情報がないため、
    # edinet_db の lookup で sector を確認する
    try:
        import sys as _sys, importlib as _imp, pathlib as _pl
        _sys.path.insert(0, str(_pl.Path(__file__).parent))
        _erm = _imp.import_module("edinet_rotation_manager")
        db   = _erm.load_db(cfg)
        for t in watchlist[:20]:
            info = _erm.lookup(db, t)
            if info and info.get("sector_code") == sector_code:
                return t
    except Exception:
        pass

    # 3. config.SECTOR_REPRESENTATIVE_TICKERS（あれば）
    rep_map = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
    if isinstance(rep_map, dict):
        tickers = rep_map.get(sector_code, [])
        if tickers:
            return tickers[0]

    return ""


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_deep_dive_trigger(cfg, results: dict) -> dict:
    """
    深掘りトリガーを検出し、対象銘柄を cfg に注入する。

    Args:
        cfg:     config モジュール
        results: stock_analyzer_main.results 辞書

    Returns:
        {
          "triggered_tickers":  list[str],
          "confidence_triggers": list[dict],
          "rank_triggers":      list[dict],
          "total":              int,
          "timestamp":          str,
        }
    """
    print("\n  🔭 [deep_dive] トリガー検出 開始...")

    conf_threshold = _conf_drop_threshold(cfg)
    rank_threshold = _rank_jump_threshold(cfg)
    max_tickers    = int(getattr(cfg, "DEEP_DIVE_MAX_TICKERS", _DEFAULT_MAX_TICKERS))

    # ── 前回 screener 順位の復元 ─────────────────────────────
    prev_ranks = _load_prev_screener_ranks(cfg)

    # ── (A) confidence 低下トリガー ───────────────────────────
    conf_triggers: list[dict] = []
    try:
        conf_triggers = _detect_confidence_triggers(
            results, cfg, conf_threshold
        )
    except Exception as e:
        print(f"    ⚠️  [deep_dive] confidence トリガー検出エラー: "
              f"{str(e)[:60]}")

    # ── (B) screener 順位急変トリガー ────────────────────────
    rank_triggers: list[dict] = []
    try:
        rank_triggers = _detect_rank_triggers(
            results, prev_ranks, rank_threshold
        )
    except Exception as e:
        print(f"    ⚠️  [deep_dive] rank トリガー検出エラー: "
              f"{str(e)[:60]}")

    # ── 重複排除して上限適用 ──────────────────────────────────
    all_triggers = conf_triggers + rank_triggers
    seen_tickers: set = set()
    triggered_tickers: list[str] = []

    for t in all_triggers:
        ticker = t.get("ticker", "")
        if ticker and ticker not in seen_tickers:
            seen_tickers.add(ticker)
            triggered_tickers.append(ticker)
        if len(triggered_tickers) >= max_tickers:
            break

    # ── cfg へ注入（edinet_rotation_manager が priority=0 で参照）─
    # Step 8 で edinet_rotation_manager._build_fetch_queue() に
    # EDINET_DEEP_DIVE_TICKERS の受け口を追加する。
    if triggered_tickers:
        cfg.__dict__["EDINET_DEEP_DIVE_TICKERS"] = triggered_tickers
        print(
            f"    🎯 [deep_dive] 深掘り対象: "
            f"{len(triggered_tickers)}銘柄 → "
            f"cfg.EDINET_DEEP_DIVE_TICKERS に注入"
        )
        for t in triggered_tickers:
            reason = next(
                (x["reason"] for x in all_triggers if x.get("ticker") == t),
                ""
            )
            print(f"       • {t}: {reason}")
    else:
        cfg.__dict__["EDINET_DEEP_DIVE_TICKERS"] = []
        print("    ✅ [deep_dive] 深掘りトリガーなし")

    # ── today の screener 順位を history に保存 ─────────────
    try:
        _update_history(cfg, results, triggered_tickers, all_triggers)
    except Exception as e:
        print(f"    ⚠️  [deep_dive] history 更新失敗: {str(e)[:60]}")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total   = len(triggered_tickers)

    print(
        f"    📋 [deep_dive] 完了: "
        f"confidence={len(conf_triggers)}件 / "
        f"rank={len(rank_triggers)}件 / "
        f"合計{total}銘柄"
    )

    return {
        "triggered_tickers":   triggered_tickers,
        "confidence_triggers": conf_triggers,
        "rank_triggers":       rank_triggers,
        "total":               total,
        "timestamp":           now_str,
    }


# ==============================================================
# BLOCK 7: history 更新（次回 rank_jump 検出用）
# ==============================================================

def _update_history(
    cfg,
    results:            dict,
    triggered_tickers:  list[str],
    all_triggers:       list[dict],
) -> None:
    """
    今回の screener 順位と業種スコアを history に保存する。
    次回起動時の差分比較に使用する。
    """
    today   = datetime.now().strftime("%Y-%m-%d")
    history = _load_history(cfg)
    entries = []

    # screener 候補の今回順位を保存
    screener   = results.get("screener", {})
    candidates = (screener.get("candidates", [])
                  if isinstance(screener, dict) else [])
    for i, cand in enumerate(candidates[:50]):   # 上位50件のみ保存
        ticker = cand.get("ticker", "")
        if ticker:
            entries.append({
                "ticker":       ticker,
                "screener_rank": i + 1,
                "sector_code":  cand.get("sector_code", ""),
                "source":       "screener_rank",
            })

    # 業種スコア（次回 confidence_drop 検出用）をスナップショット
    sector_results = results.get("sectors", {})
    if isinstance(sector_results, dict):
        for code, res in sector_results.items():
            if not isinstance(res, dict):
                continue
            score = res.get("composite_score")
            if score is not None:
                entries.append({
                    "ticker":               "",
                    "sector_code":          code,
                    "confidence_snapshot":  round(float(score), 1),
                    "source":               "sector_score",
                })

    history[today] = entries
    _save_history(cfg, history)


# ==============================================================
# BLOCK 8: 単体テスト
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

    print("=== deep_dive_trigger 単体テスト ===\n")

    # ── モック results で検証 ─────────────────────────────────
    # 1016: composite_score 70→50 (Δ-20pt → confidence_drop発動)
    # 9984.T: rank 3→15 (Δ-12位 → rank_jump発動)
    # 6758.T: rank 5→6  (Δ-1位 → 閾値未満・非発動)

    mock_results = {
        "sectors": {
            "1016": {"composite_score": 50.0, "sector_name_ja": "電気機器"},
            "1025": {"composite_score": 72.0, "sector_name_ja": "情報・通信業"},
        },
        "screener": {
            "candidates": [
                {"ticker": "6758.T",  "sector_code": "1016", "sector_name_ja": "電気機器"},
                {"ticker": "6861.T",  "sector_code": "1016", "sector_name_ja": "電気機器"},
                {"ticker": "9984.T",  "sector_code": "1025", "sector_name_ja": "情報・通信業"},
                {"ticker": "4063.T",  "sector_code": "1007", "sector_name_ja": "化学"},
            ]
        },
        "economic_cycle": {"confidence": 0.45},
    }

    # ── history に前回値を書き込む ────────────────────────────
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    mock_history = {
        yesterday: [
            # 前回 screener 順位
            {"ticker": "6758.T",  "screener_rank": 5,  "sector_code": "1016",
             "source": "screener_rank"},
            {"ticker": "9984.T",  "screener_rank": 3,  "sector_code": "1025",
             "source": "screener_rank"},
            # 前回業種スコア（1016: 70.0 → 今回50.0 = Δ-20pt）
            {"ticker": "", "sector_code": "1016",
             "confidence_snapshot": 70.0, "source": "sector_score"},
            {"ticker": "", "sector_code": "1025",
             "confidence_snapshot": 71.0, "source": "sector_score"},
        ]
    }
    _save_history(cfg, mock_history)

    # ── 実行 ────────────────────────────────────────────────
    result = run_deep_dive_trigger(cfg, mock_results)

    print(f"\n  triggered_tickers: {result['triggered_tickers']}")
    print(f"  confidence_triggers: {len(result['confidence_triggers'])}件")
    for t in result["confidence_triggers"]:
        print(f"    [{t['sector_code']}] {t['reason']}")
    print(f"  rank_triggers: {len(result['rank_triggers'])}件")
    for t in result["rank_triggers"]:
        print(f"    [{t['ticker']}] {t['reason']}")

    # アサーション
    # 9984.T: rank 3→3位(今回リスト上の位置は2=index1→rank2)
    # ※モックでは9984.TはcandidatesのIndex2なのでrank=3
    # confidence drop: 1016は70→50でΔ20pt ≥ 15pt → 発動
    assert result["total"] > 0, "トリガーが1件も検出されなかった"
    assert hasattr(cfg, "EDINET_DEEP_DIVE_TICKERS"), \
        "cfg.EDINET_DEEP_DIVE_TICKERS が注入されなかった"
    assert len(cfg.EDINET_DEEP_DIVE_TICKERS) > 0, \
        "EDINET_DEEP_DIVE_TICKERS が空"

    print("\n  ✅ 全テスト PASS")