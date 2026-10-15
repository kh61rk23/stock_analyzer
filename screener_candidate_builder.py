# ==============================================================
# screener_candidate_builder.py  v1.0  (問題D)
# スクリーニング対象銘柄リストの動的ビルダー
# ==============================================================
# 【役割】
#   スクリーニング対象銘柄を静的リスト + 動的追加の合算で生成する。
#   「EDINETで初めて把握した優良小型株」をscreenerが拾えるようにする。
#
# 【合算ソース（優先順）】
#   ① cfg.JAPAN_SECTOR_TICKERS（静的・基本リスト） ← 既存
#   ② edinet_rotation DB で credit_score ≥ 80 の銘柄（動的追加）
#   ③ 前日 TDnet 重要開示銘柄（JP）/ 8-K 提出銘柄（US）
#   ④ investment_journal.json の watch リスト
#
# 【出力】
#   cfg.DYNAMIC_SCREENER_TICKERS に格納 → stock_screener.py が参照
#   （screener 実行前に run_all_tasks() 内で呼び出す）
#
# 【設計原則】
#   - 既存の JAPAN_SECTOR_TICKERS を置き換えるのではなく「拡張」する
#   - 追加銘柄は discovery_score = 100 でフラグを立てる
#   - 追加銘柄数の上限は DYNAMIC_SCREENER_MAX_ADD で管理（デフォルト50社）
#
# 【依存】config.py / edinet_rotation DB / tdnet_monitor / us_8k_monitor
# ==============================================================

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

DYNAMIC_SCREENER_MAX_ADD = 50    # 動的追加の上限社数

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'SCB_EDINET_CREDIT_THRESHOLD': 'EDINET_CREDIT_THRESHOLD',
    'B6_SCREENER_CANDIDATE_BUILDER_DYNAMIC_SCREENER_MAX_ADD': 'DYNAMIC_SCREENER_MAX_ADD',
}

EDINET_CREDIT_THRESHOLD  = 80    # これ以上のcredit_scoreで動的追加
JOURNAL_WATCH_STATUS     = "watching"  # investment_journal の対象ステータス


# ==============================================================
# BLOCK 1: データロード関数群
# ==============================================================

def _data_dir(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data"))


def _load_edinet_high_credit(cfg) -> list[str]:
    """
    edinet_rotation DB から credit_score ≥ 閾値 の銘柄を返す。

    edinet_rotation_manager.py が保存する
    stock_data/data/edinet_db/{ticker}.json の credit_score を参照する。

    Returns:
        [ticker, ...] 高credit_score銘柄リスト
    """
    threshold = float(getattr(cfg, "EDINET_CREDIT_THRESHOLD", EDINET_CREDIT_THRESHOLD))
    db_dir    = _data_dir(cfg) / "edinet_db"

    if not db_dir.exists():
        return []

    high_credit = []
    try:
        for json_file in sorted(db_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                # credit_score の取得（edinet_client.py の出力形式に対応）
                credit = (
                    data.get("credit_score") or
                    data.get("edinet_credit_score") or
                    data.get("analysis", {}).get("credit_score")
                )
                if credit is None:
                    continue
                if float(credit) >= threshold:
                    ticker = (
                        data.get("ticker") or
                        json_file.stem.replace("_", ".")
                    )
                    if ticker:
                        high_credit.append(ticker)
            except Exception:
                continue
    except Exception:
        pass

    return high_credit


# TDnet のうちスクリーニング対象に足すカテゴリ（株主還元・支配権の変化）
TDNET_PRIORITY_CATEGORIES = ("BUYBACK", "CANCEL", "DIV_UP", "MBO", "LARGE_HOLDING")
TDNET_LOOKBACK_DAYS = 3


def _load_universe_top_tickers(cfg) -> list[str]:
    """一次採点（screener_universe）の上位のうち、静的リストに無い銘柄。

    個別キャッシュ（edinet_quality・comps・dcf・earnings_surprise 等）は動的候補のぶんだけ育つので、
    一次で浮上した小型株を翌日以降の動的候補に入れてキャッシュを広げる。3日より古い記録は使わない。
    """
    p = _data_dir(cfg) / "screener_universe_top.json"
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        age = (date.today() - date.fromisoformat(rec.get("date", "2000-01-01"))).days
        return [t for t in rec.get("tickers", []) if isinstance(t, str)] if age <= 3 else []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"    ⚠️ 一次採点の上位リストを読めない（スキップ）: {str(e)[:80]}")
        return []


def _load_tdnet_tickers(results: dict, cfg=None) -> list[str]:
    """
    TDnet の重要開示銘柄を返す。

    2026-09-19: results["tdnet"]["high_priority"] / ["alerts"] / ["disclosures"] を
    読んでいたが、tdnet_monitor が返すキーは "events" / "all_events" で毎回0件だった。
    さらに TDnet モニターは候補ビルダーより後に動くため、当日の results には
    まだ何も入っていない。前回実行が保存した通知済みキャッシュ
    （tdnet_notified.json の "{ticker}_{日付}_{カテゴリ}"）から直近の開示を拾う。
    """
    tickers: list[str] = []

    tdnet_result = results.get("tdnet") if isinstance(results, dict) else None
    if isinstance(tdnet_result, dict):
        for key in ("events", "all_events"):
            for item in tdnet_result.get(key, []) or []:
                if not isinstance(item, dict):
                    continue
                if item.get("category") not in TDNET_PRIORITY_CATEGORIES:
                    continue
                t = item.get("ticker") or item.get("code")
                if t:
                    t = str(t)
                    tickers.append(t if t.endswith(".T") else f"{t}.T")

    if not tickers:
        path = _data_dir(cfg) / "tdnet_notified.json"
        cutoff = (date.today() - timedelta(days=TDNET_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for nid in raw.get("notified", []):
                # カテゴリ名にも "_" が入る（DIV_UP / LARGE_HOLDING）ので後ろは結合する
                parts = str(nid).split("_")
                if len(parts) < 3:
                    continue
                ticker, day, category = parts[0], parts[1], "_".join(parts[2:])
                if day < cutoff or category not in TDNET_PRIORITY_CATEGORIES:
                    continue
                tickers.append(ticker if ticker.endswith(".T") else f"{ticker}.T")
        except (OSError, ValueError):
            return []

    return list(dict.fromkeys(tickers))   # 重複除去


def _load_8k_jp_bridge_tickers(results: dict) -> list[str]:
    """
    us_8k_monitor / us_sector_bridge_enhanced の結果から
    JP業種に波及シグナルのある米国銘柄の関連 JP 銘柄を返す。

    直接は JP ティッカーではないため、ここでは beat/miss 情報のフラグを
    _dynamic_metadata として保持するのみとする。
    """
    # us_sector_bridge の jp_affected 業種から代表銘柄を抽出
    usbe = results.get("us_sector_bridge", {})
    if not usbe:
        return []

    jp_affected_codes = usbe.get("jp_affected", [])
    if not jp_affected_codes:
        return []

    # 各影響業種の代表銘柄を返す（config の JAPAN_SECTORS から）
    # この関数は caller が cfg を渡せない設計のため、空を返す
    # → _load_from_config_sectors() で後処理する
    return []


def _load_journal_watch_tickers(cfg) -> list[str]:
    """
    investment_journal.json の watch ステータス銘柄を返す。
    """
    p = _data_dir(cfg) / "investment_journal.json"
    if not p.exists():
        return []

    try:
        journals = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(journals, list):
            return []

        watch_tickers = []
        for entry in journals:
            if not isinstance(entry, dict):
                continue
            status = entry.get("status", "")
            if status == JOURNAL_WATCH_STATUS:
                ticker = entry.get("ticker", "")
                if ticker:
                    watch_tickers.append(ticker)

        return list(dict.fromkeys(watch_tickers))
    except Exception:
        return []


def _load_us_bridge_jp_reps(results: dict, cfg) -> list[str]:
    """
    us_sector_bridge で影響が検知された JP業種の代表銘柄を返す。
    """
    usbe = results.get("us_sector_bridge", {})
    jp_affected = usbe.get("jp_affected", [])
    if not jp_affected:
        return []

    japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})
    reps = []
    for code in jp_affected:
        sec_info = japan_sectors.get(code, {})
        sec_reps = sec_info.get("representatives", [])
        if isinstance(sec_reps, list):
            reps.extend(sec_reps[:3])   # 各業種から最大3銘柄

    return list(dict.fromkeys(reps))


# ==============================================================
# BLOCK 2: 動的銘柄リストの構築
# ==============================================================

def build_dynamic_screener_tickers(
    cfg,
    results: dict = None,
) -> dict:
    """
    動的スクリーニング対象銘柄リストを構築する。

    Returns:
        {
          "base_tickers":    [ticker, ...],  # ① 静的基本リスト
          "edinet_tickers":  [ticker, ...],  # ② EDINET高credit
          "tdnet_tickers":   [ticker, ...],  # ③ TDnet重要開示
          "journal_tickers": [ticker, ...],  # ④ journalのwatch
          "universe_tickers": [ticker, ...], # 一次採点の上位（静的リスト外）
          "us_bridge_tickers": [ticker, ...], # US波及シグナル関連
          "all_tickers":     [ticker, ...],  # 合算・重複除去済み
          "dynamic_added":   [ticker, ...],  # 動的追加分のみ
          "dynamic_meta":    {ticker: {source, discovery_score}},
          "stats":           dict,
        }
    """
    results  = results or {}
    max_add  = getattr(cfg, "DYNAMIC_SCREENER_MAX_ADD", DYNAMIC_SCREENER_MAX_ADD)

    # ── ① 静的基本リスト ─────────────────────────────────────
    base_tickers: list[str] = []
    japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})
    for code, sec_info in japan_sectors.items():
        reps = sec_info.get("representatives", [])
        if isinstance(reps, list):
            base_tickers.extend(reps)
    base_set = set(base_tickers)

    # ── ② EDINET 高 credit_score 銘柄 ──────────────────────
    edinet_tickers = [
        t for t in _load_edinet_high_credit(cfg)
        if t not in base_set
    ]

    # ── ③ TDnet 重要開示銘柄 ─────────────────────────────────
    tdnet_tickers = [
        t for t in _load_tdnet_tickers(results, cfg)
        if t not in base_set
    ]

    # ④ investment_journal watch リスト ────────────────────
    journal_tickers = [
        t for t in _load_journal_watch_tickers(cfg)
        if t not in base_set
    ]

    universe_tickers = [
        t for t in _load_universe_top_tickers(cfg)
        if t not in base_set
    ]

    # ── US波及シグナル関連 JP 代表銘柄 ────────────────────────
    us_bridge_tickers = [
        t for t in _load_us_bridge_jp_reps(results, cfg)
        if t not in base_set
    ]

    # ── 動的追加銘柄の統合（優先順で上限まで） ───────────────
    dynamic_added: list[str] = []
    dynamic_meta:  dict      = {}
    seen_dynamic:  set       = set()

    def _add_dynamic(tickers: list, source: str, disc_score: int):
        for t in tickers:
            if t in seen_dynamic or len(dynamic_added) >= max_add:
                break
            seen_dynamic.add(t)
            dynamic_added.append(t)
            dynamic_meta[t] = {
                "source":          source,
                "discovery_score": disc_score,
                "added_date":      date.today().strftime("%Y-%m-%d"),
            }

    # 優先順: TDnet（最速） > journal > EDINET > US波及
    _add_dynamic(tdnet_tickers,     source="tdnet",       disc_score=100)
    _add_dynamic(journal_tickers,   source="journal",     disc_score=80)
    _add_dynamic(universe_tickers,  source="universe_top", disc_score=85)
    _add_dynamic(edinet_tickers,    source="edinet_high_credit", disc_score=90)
    _add_dynamic(us_bridge_tickers, source="us_bridge",   disc_score=70)

    # ── 全銘柄リスト（静的 + 動的） ──────────────────────────
    all_tickers = list(dict.fromkeys(base_tickers + dynamic_added))

    stats = {
        "base_count":      len(base_tickers),
        "edinet_count":    len(edinet_tickers),
        "tdnet_count":     len(tdnet_tickers),
        "journal_count":   len(journal_tickers),
        "us_bridge_count": len(us_bridge_tickers),
        "universe_count":  len(universe_tickers),
        "dynamic_added":   len(dynamic_added),
        "total":           len(all_tickers),
    }

    if dynamic_added:
        sources = {}
        for t in dynamic_added:
            src = dynamic_meta[t]["source"]
            sources[src] = sources.get(src, 0) + 1
        src_str = " / ".join(f"{s}:{n}" for s, n in sources.items())
        print(f"    📋 [問題D] 動的銘柄追加: {len(dynamic_added)}社 "
              f"（{src_str}）→ 合計{len(all_tickers)}社")

    return {
        "base_tickers":      base_tickers,
        "edinet_tickers":    edinet_tickers,
        "tdnet_tickers":     tdnet_tickers,
        "journal_tickers":   journal_tickers,
        "universe_tickers":  universe_tickers,
        "us_bridge_tickers": us_bridge_tickers,
        "all_tickers":       all_tickers,
        "dynamic_added":     dynamic_added,
        "dynamic_meta":      dynamic_meta,
        "stats":             stats,
    }


# ==============================================================
# BLOCK 3: メインエントリポイント
# ==============================================================

def run_screener_candidate_builder(cfg, results: dict = None) -> dict:
    """
    スクリーニング対象銘柄リストを動的に構築し、
    cfg にインジェクトして stock_screener が参照できるようにする。

    stock_analyzer_main.py の screener 呼び出し直前に実行:
        _scb = _load_local_module("screener_candidate_builder", ...)
        scb_result = _scb.run_screener_candidate_builder(cfg, results)
        results["screener_candidates"] = scb_result
        # cfg に動的注入（stock_screener が cfg.DYNAMIC_SCREENER_TICKERS を参照）
        cfg.__dict__["DYNAMIC_SCREENER_TICKERS"] = scb_result["all_tickers"]
        cfg.__dict__["DYNAMIC_SCREENER_META"]    = scb_result["dynamic_meta"]

    Returns:
        build_dynamic_screener_tickers() の出力 dict
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📋 スクリーニング対象銘柄ビルダー 開始...")
    result = build_dynamic_screener_tickers(cfg, results)
    # 個別キャッシュ（決算サプライズ・インサイダー等）の対象にも一次採点の上位を渡す
    if result.get("universe_tickers"):
        cfg.__dict__["EDINET_SCREENER_CANDIDATES"] = list(dict.fromkeys(
            result["universe_tickers"] + list(getattr(cfg, "EDINET_SCREENER_CANDIDATES", []) or [])))

    stats = result["stats"]
    print(
        f"    ✅ 候補構築完了: "
        f"静的{stats['base_count']} + "
        f"動的追加{stats['dynamic_added']} = "
        f"合計{stats['total']}社"
    )

    return result
