# ==============================================================
# latent_pearl_manager.py  v1.0  (Wave 7 - Step 8)
# 潜伏銘柄（Latent Pearl）管理モジュール
# ==============================================================
# 【役割】
#   通常のスクリーニング網（質スコア閾値・流動性閾値等）に
#   引っかかりにくいが、独自の評価軸で高ポテンシャルを持つ
#   「潜伏銘柄」を検出・管理し、latent_pearl_cache.json に保存する。
#   一定条件を満たした銘柄は通常のPearlキャッシュへ「昇格」させる。
#
# 【潜伏銘柄の定義】
#   以下のいずれかの条件を複数同時に満たす銘柄：
#     (A) independence_score >= 65 かつ catalyst_score >= 3.0
#     (B) cross_size_signal が "upward_spillover" または "jp_us_cross" で
#         target_ticker に含まれており strength >= 3.0
#     (C) Nano 銘柄かつ Nano優先キューの上位 10% 以内
#     (D) midterm_plan trajectory == "ahead" かつ overall_score >= 80
#
# 【昇格ロジック】
#   $latent\\_pearl\\_score = w_1 \\times indep + w_2 \\times cross\\_signal$
#                           $+ w_3 \\times catalyst + w_4 \\times midterm$
#   latent_pearl_score >= 70.0 → 通常 Pearl として昇格
#   昇格した銘柄は latent_pearl_cache.json の "promoted" フィールドに記録
#
# 【主要出力キャッシュ】
#   CACHE_DIR/latent_pearl_cache.json
#   TTL: 30日（日次更新）
#   スキーマ:
#     {
#       "last_updated": "YYYY-MM-DD",
#       "version": "1.0",
#       "latent_pearls": {
#         ticker: {
#           "ticker": str,
#           "detection_conditions": list,  # 検出条件 A/B/C/D
#           "independence_score": float,
#           "catalyst_score": float,
#           "cross_signal_strength": float,
#           "midterm_score": float,
#           "midterm_trajectory": str,
#           "latent_pearl_score": float,   # 0.0 ~ 100.0
#           "promotion_eligible": bool,
#           "promoted": bool,
#           "promoted_at": str,
#           "first_detected": str,
#           "last_updated": str,
#         }
#       },
#       "promoted": [ticker, ...],
#       "stats": { ... }
#     }
#
# 【依存】
#   independence_score_calculator.py（get_independence_score / get_catalyst_flags）
#   cross_size_signal.py（get_signals_for_ticker / get_target_tickers_by_signal_type）
#   midterm_plan_tracker.py（get_midterm_score / get_midterm_trajectory）
#   size_decay_manager.py（get_nano_queue）
#   config.py（Wave 7追加定数）
# 【役割】
#   independence_score_calculator.py が検出した Latent Pearl 候補を
#   latent_pearl_cache.json に登録・更新・TTL管理し、
#   プロモーション条件を満たした銘柄を screener 先頭に挿入するための
#   インタフェースを提供する。
#
# 【latent_pearl_cache.json 構造】
#   {
#     "TICKER": {
#       "first_detected":    "YYYY-MM-DD",
#       "last_updated":      "YYYY-MM-DD",
#       "days_in_cache":     int,
#       "score_history":     [{"date": str, "score": float}, ...],  # 直近30件
#       "latest_score":      float,   # 最新 independence_score
#       "promoted_score":    float,   # プロモーション判定スコア（0〜1.0）
#       "is_promoted":       bool,
#       "promoted_at":       str | None,
#       "sector_code":       str,
#       "prev_sector_phase": str | None,  # 前回のセクターローテーション局面
#       "phase":             str,
#       "beta_sector":       float,
#       "epsilon_mean":      float,
#     }
#   }
#
# 【promoted_score 計算式】
#   $promoted\_score = (independence\_score / 100) \times confidence$
#   $confidence = 0.70 + 0.30 \times \min(1.0, days\_in\_cache / 30)$
#
#   日数効果の例:
#     day  0: confidence=0.70 → score=100pt 必要（即時昇格）
#     day 15: confidence=0.85 → score≥82pt で promoted
#     day 30: confidence=1.00 → score≥70pt で promoted
#
# 【プロモーション条件（alert_notifier.py トリガー⑰の発動条件）】
#   promoted_score ≥ LATENT_PROMOTION_THRESHOLD（デフォルト 0.70）
#
# 【セクター反転検出（alert_notifier.py トリガー⑱の発動条件）】
#   前回局面が LAGGING / WEAKENING かつ現在局面が IMPROVING / LEADING
#   の変化が検出され、かつ該当セクターに latent pearl 候補が存在する場合。
#
# 【生成キャッシュ】
#   latent_pearl_cache.json  TTL: 90日
#
# 【呼び出しパターン（stock_screener.py から）】
#   from latent_pearl_manager import run_latent_pearl_manager, get_promoted_tickers
#   lpm_result = run_latent_pearl_manager(ind_candidates, results, cfg)
#   promoted   = get_promoted_tickers(cfg)   # screener 先頭挿入用
#
# 【依存】
#   independence_score_calculator.py（候補リスト受取）
#   追加ライブラリなし（標準ライブラリのみ）
#   ★ 全関数を try/except で保護。失敗時は空dict/listを返す。
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from config_sync import sync_module_constants

# ==============================================================
# BLOCK 0: デフォルト定数
# ==============================================================

_CACHE_FILENAME   = "latent_pearl_cache.json"
_DEFAULT_TTL_DAYS = 30

# 潜伏銘柄スコア計算の重み
_LATENT_SCORE_WEIGHTS = {
    "independence": 0.35,
    "cross_signal": 0.30,
    "catalyst":     0.20,
    "midterm":      0.15,
}

# 昇格閾値
_PROMOTION_THRESHOLD = 70.0

# 各検出条件の閾値
_CONDITION_A_INDEP_MIN    = 65.0
_CONDITION_A_CATALYST_MIN = 3.0
_CONDITION_B_STRENGTH_MIN = 3.0
_CONDITION_D_MIDTERM_MIN  = 80.0

# クロスシグナル強度の正規化上限（10.0 → 100pt に換算）
_CROSS_SIGNAL_NORM_MAX = 10.0

_CFG_CONST_MAP = {
    "LATENT_PEARL_SCORE_WEIGHTS": "_LATENT_SCORE_WEIGHTS",
    "LATENT_PEARL_PROMOTION_THRESHOLD": "_PROMOTION_THRESHOLD",
    "LATENT_PEARL_CONDITION_A_INDEP_MIN": "_CONDITION_A_INDEP_MIN",
    "LATENT_PEARL_CONDITION_A_CATALYST_MIN": "_CONDITION_A_CATALYST_MIN",
    "LATENT_PEARL_CONDITION_B_STRENGTH_MIN": "_CONDITION_B_STRENGTH_MIN",
    "LATENT_PEARL_CONDITION_D_MIDTERM_MIN": "_CONDITION_D_MIDTERM_MIN",
    "LATENT_PEARL_CROSS_SIGNAL_NORM_MAX": "_CROSS_SIGNAL_NORM_MAX",
}


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / _CACHE_FILENAME


def _load_cache(cfg) -> Optional[dict]:
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        last_updated = data.get("last_updated", "")
        ttl = int(getattr(cfg, "LATENT_PEARL_TTL_DAYS", _DEFAULT_TTL_DAYS))
        if last_updated:
            age = (datetime.now() - datetime.strptime(last_updated, "%Y-%m-%d")).days
            if age < ttl:
                return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    try:
        data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        p = _cache_path(cfg)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  [latent_pearl] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: 依存モジュールからのデータ取得
# ==============================================================

def _get_independence_data(ticker: str, cfg) -> tuple:
    """independence_score と catalyst_score を取得する。"""
    indep_score    = 0.0
    catalyst_score = 0.0
    catalyst_flags = []
    try:
        from independence_score_calculator import (
            get_independence_score,
            get_combined_score,
            get_catalyst_flags,
        )
        indep_score    = get_independence_score(ticker, cfg) or 0.0
        catalyst_score = 0.0
        catalyst_flags = get_catalyst_flags(ticker, cfg) or []

        # catalyst_score は flags の件数×重みから再算出
        # （キャッシュに直接 catalyst_score が入っていない場合の対応）
        cache_dir = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
        ind_cache_p = cache_dir / "independence_scores.json"
        if ind_cache_p.exists():
            ind_cache = json.loads(ind_cache_p.read_text(encoding="utf-8"))
            entry = ind_cache.get("scores", {}).get(ticker, {})
            catalyst_score = float(entry.get("catalyst_score", 0.0))
    except ImportError:
        # フォールバック: independence_scores.json を直接読む
        try:
            cache_dir = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
            p = cache_dir / "independence_scores.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                entry = data.get("scores", {}).get(ticker, {})
                indep_score    = float(entry.get("independence_score", 0.0))
                catalyst_score = float(entry.get("catalyst_score", 0.0))
                catalyst_flags = entry.get("catalyst_flags", [])
        except Exception:
            pass
    except Exception:
        pass
    return indep_score, catalyst_score, catalyst_flags


def _get_cross_signal_data(ticker: str, cfg) -> tuple:
    """クロスサイズシグナルの最大強度と関連シグナルリストを取得する。"""
    max_strength = 0.0
    signal_types = []
    try:
        from cross_size_signal import get_signals_for_ticker
        signals = get_signals_for_ticker(ticker, cfg)
        for sig in signals:
            s_type    = sig.get("signal_type", "")
            strength  = float(sig.get("strength", 0.0))
            direction = sig.get("direction", "")
            if direction == "bullish" and strength > max_strength:
                max_strength = strength
            if s_type not in signal_types:
                signal_types.append(s_type)
    except ImportError:
        # フォールバック: cross_size_signal_cache.json を直接読む
        try:
            cache_dir = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
            p = cache_dir / "cross_size_signal_cache.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                sig_ids = data.get("ticker_signals", {}).get(ticker, [])
                all_sigs = {s["signal_id"]: s for s in data.get("signals", [])}
                for sid in sig_ids:
                    sig = all_sigs.get(sid, {})
                    s_type   = sig.get("signal_type", "")
                    strength = float(sig.get("strength", 0.0))
                    if sig.get("direction") == "bullish" and strength > max_strength:
                        max_strength = strength
                    if s_type not in signal_types:
                        signal_types.append(s_type)
        except Exception:
            pass
    except Exception:
        pass
    return max_strength, signal_types


def _get_midterm_data(ticker: str, cfg) -> tuple:
    """中計スコアと軌道を取得する。"""
    midterm_score    = 0.0
    midterm_trajectory = None
    try:
        from midterm_plan_tracker import get_midterm_score, get_midterm_trajectory
        midterm_score      = get_midterm_score(ticker, cfg) or 0.0
        midterm_trajectory = get_midterm_trajectory(ticker, cfg)
    except ImportError:
        try:
            cache_dir = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
            p = cache_dir / "midterm_plan_cache.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                entry = data.get("plans", {}).get(ticker, {})
                midterm_score      = float(entry.get("overall_score", 0.0))
                midterm_trajectory = entry.get("trajectory")
        except Exception:
            pass
    except Exception:
        pass
    return midterm_score, midterm_trajectory


def _get_nano_queue_rank(ticker: str, cfg) -> Optional[float]:
    """
    Nano評価キュー内での順位（上位10%以内なら True）を返す。
    Returns: float 0.0〜1.0（パーセンタイル、小さいほど上位）または None
    """
    try:
        from size_decay_manager import get_nano_queue
        queue = get_nano_queue(cfg)
        if not queue:
            return None
        tickers = [item.get("ticker") for item in queue]
        if ticker not in tickers:
            return None
        rank = tickers.index(ticker)
        return round(rank / max(1, len(tickers)), 4)
    except Exception:
        return None


# ==============================================================
# BLOCK 3: 潜伏銘柄検出条件の評価
# ==============================================================

def _eval_detection_conditions(
    ticker: str,
    indep_score: float,
    catalyst_score: float,
    cross_strength: float,
    cross_signal_types: list,
    midterm_score: float,
    midterm_trajectory: Optional[str],
    nano_rank: Optional[float],
) -> list:
    """
    潜伏銘柄の検出条件（A/B/C/D）を評価し、
    満たした条件のリストを返す。

    Returns:
        ["A", "B", ...] 形式のリスト（空リストは非潜伏）
    """
    conditions = []

    # 条件A: 独立性高 × カタリスト有
    if indep_score >= _CONDITION_A_INDEP_MIN and catalyst_score >= _CONDITION_A_CATALYST_MIN:
        conditions.append("A")

    # 条件B: クロスシグナルのターゲット銘柄（上方シグナル）
    bullish_signal_types = {"upward_spillover", "jp_us_cross", "contrarian_nano"}
    if (cross_strength >= _CONDITION_B_STRENGTH_MIN
            and bool(bullish_signal_types & set(cross_signal_types))):
        conditions.append("B")

    # 条件C: Nano銘柄でキュー上位10%以内
    if nano_rank is not None and nano_rank <= 0.10:
        conditions.append("C")

    # 条件D: 中計 ahead × 高達成スコア
    if (midterm_trajectory == "ahead"
            and midterm_score >= _CONDITION_D_MIDTERM_MIN):
        conditions.append("D")

    return conditions


# ==============================================================
# BLOCK 4: 潜伏Pearlスコア計算
# ==============================================================

def _calc_latent_pearl_score(
    indep_score: float,
    cross_strength: float,
    catalyst_score: float,
    midterm_score: float,
    conditions: list,
    cfg=None,
) -> float:
    """
    潜伏Pearlスコアを計算する。

    $latent\\_pearl\\_score = w_1 \\times indep + w_2 \\times cross\\_norm$
                            $+ w_3 \\times cat\\_norm + w_4 \\times midterm$
    $cross\\_norm = (cross\\_strength / 10) \\times 100$
    $cat\\_norm = (catalyst\\_score / 10) \\times 100$

    複数条件を満たすほど +5pt のボーナスが加算される（最大 +15pt）。

    Returns:
        float 0.0 ~ 100.0
    """
    try:
        # 単体利用時に渡された cfg も尊重する。通常経路では run_* 冒頭の
        # config_sync により _LATENT_SCORE_WEIGHTS と同値になっている。
        w = getattr(cfg, "LATENT_PEARL_SCORE_WEIGHTS", None) or _LATENT_SCORE_WEIGHTS

        cross_norm   = (cross_strength / _CROSS_SIGNAL_NORM_MAX) * 100.0
        cat_norm     = (catalyst_score / 10.0) * 100.0
        midterm_norm = midterm_score  # 既に 0〜100

        base_score = (
            w["independence"] * indep_score
            + w["cross_signal"] * cross_norm
            + w["catalyst"]     * cat_norm
            + w["midterm"]      * midterm_norm
        )

        # 複数条件ボーナス（条件数 -1 × 5pt、上限 15pt）
        bonus = min(15.0, max(0, len(conditions) - 1) * 5.0)
        total = min(100.0, max(0.0, base_score + bonus))
        return round(total, 2)

    except Exception:
        return 0.0


# ==============================================================
# BLOCK 5: 昇格ロジック
# ==============================================================

def _check_promotion(
    ticker: str,
    latent_score: float,
    prev_entry: dict,
    cfg,
) -> tuple:
    """
    昇格条件を判定し、昇格する場合は pearl_candidates.json を更新する。

    昇格条件:
      - latent_pearl_score >= _PROMOTION_THRESHOLD (70.0)
      - 前回未昇格（promoted == False）

    Returns:
        (promotion_eligible: bool, promoted: bool, promoted_at: str)
    """
    promotion_eligible = latent_score >= _PROMOTION_THRESHOLD
    already_promoted   = prev_entry.get("promoted", False)
    promoted_at        = prev_entry.get("promoted_at", "")
    today_str          = datetime.now().strftime("%Y-%m-%d")

    if promotion_eligible and not already_promoted:
        # pearl_candidates.json への昇格フラグを追記
        try:
            cache_dir = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
            pc_path   = cache_dir / "pearl_candidates.json"
            if pc_path.exists():
                pc_data = json.loads(pc_path.read_text(encoding="utf-8"))
                if ticker in pc_data:
                    pc_data[ticker]["latent_pearl_promoted"] = True
                    pc_data[ticker]["latent_pearl_score"]    = latent_score
                    pc_data[ticker]["promoted_at"]           = today_str
                    pc_path.write_text(
                        json.dumps(pc_data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        except Exception:
            pass
        return True, True, today_str

    return promotion_eligible, already_promoted, promoted_at


# ==============================================================
# BLOCK 6: 候補銘柄の収集
# ==============================================================

def _collect_candidate_tickers(cfg) -> list:
    """
    潜伏銘柄の評価対象となるティッカーリストを収集する。

    以下のソースから統合して重複排除：
      1. cross_size_signal_cache.json のターゲット銘柄
      2. independence_scores.json の HIGH/MID 銘柄
      3. nano_evaluation_queue.json の上位銘柄
      4. midterm_plan_cache.json の "ahead" 銘柄

    Returns:
        [{"ticker": str, "market": str}, ...]
    """
    tickers_seen: set = set()
    candidates   = []
    cache_dir    = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))

    def _add(ticker: str, market: str = "JP"):
        if ticker and ticker not in tickers_seen:
            tickers_seen.add(ticker)
            candidates.append({"ticker": ticker, "market": market})

    # ① クロスシグナルのターゲット銘柄
    try:
        from cross_size_signal import get_target_tickers_by_signal_type
        for sig_type in ("upward_spillover", "jp_us_cross"):
            for ticker in get_target_tickers_by_signal_type(sig_type, min_strength=2.0, cfg=cfg):
                market = "US" if not ticker.endswith(".T") else "JP"
                _add(ticker, market)
    except ImportError:
        try:
            p = cache_dir / "cross_size_signal_cache.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                for sig in data.get("signals", []):
                    if sig.get("direction") == "bullish":
                        for t in sig.get("target_tickers", []):
                            _add(t, "US" if not t.endswith(".T") else "JP")
        except Exception:
            pass

    # ② independence HIGH/MID 銘柄
    try:
        p = cache_dir / "independence_scores.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for ticker, entry in data.get("scores", {}).items():
                if entry.get("independence_tier") in ("HIGH", "MID"):
                    _add(ticker, entry.get("market", "JP"))
    except Exception:
        pass

    # ③ Nano評価キューの上位銘柄
    try:
        p = cache_dir / "nano_evaluation_queue.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for item in data.get("queue", [])[:20]:
                t = item.get("ticker", "")
                _add(t, item.get("market", "JP"))
    except Exception:
        pass

    # ④ midterm "ahead" 銘柄
    try:
        p = cache_dir / "midterm_plan_cache.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for ticker, plan in data.get("plans", {}).items():
                if plan.get("trajectory") == "ahead":
                    _add(ticker, "JP")
    except Exception:
        pass

    return candidates


# ==============================================================
# BLOCK 7: 外部参照用アクセサ
# ==============================================================

def get_latent_pearl_score(ticker: str, cfg=None) -> Optional[float]:
    """
    銘柄の潜伏Pearlスコアを latent_pearl_cache.json から取得する。
    pearl_score_calculator.py の D3/D4 補強に使用。

    Returns:
        float 0.0〜100.0 または None
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return None
        return cache.get("latent_pearls", {}).get(ticker, {}).get("latent_pearl_score")
    except Exception:
        return None


def get_latent_pearls_list(cfg=None, min_score: float = 0.0) -> list:
    """
    潜伏Pearls の全リストを返す。

    Returns:
        [entry_dict, ...] 形式（スコア降順）
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return []
        entries = list(cache.get("latent_pearls", {}).values())
        entries = [e for e in entries if float(e.get("latent_pearl_score", 0)) >= min_score]
        entries.sort(key=lambda x: x.get("latent_pearl_score", 0), reverse=True)
        return entries
    except Exception:
        return []


def get_promoted_tickers(cfg=None) -> list:
    """
    昇格済み銘柄のティッカーリストを返す。

    Returns:
        [ticker, ...]
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return []
        return cache.get("promoted", [])
    except Exception:
        return []


def is_latent_pearl(ticker: str, cfg=None, min_score: float = 30.0) -> bool:
    """
    銘柄が潜伏Pearlとして登録されているか確認する。

    Returns:
        bool
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return False
        entry = cache.get("latent_pearls", {}).get(ticker)
        if not entry:
            return False
        return float(entry.get("latent_pearl_score", 0)) >= min_score
    except Exception:
        return False


# ==============================================================
# BLOCK 8: メイン実行フロー
# ==============================================================

def run_latent_pearl_manager(cfg=None, force_refresh: bool = False) -> dict:
    """
    stock_analyzer_main.py の DAILY_MODULES から呼ばれるエントリポイント。
    independence_score_calculator / cross_size_signal の実行後に呼ぶこと。

    処理フロー:
      1. キャッシュ有効 → 返却
      2. 複数ソースから候補銘柄を収集
      3. 各銘柄の検出条件（A〜D）を評価
      4. 潜伏Pearlスコアを計算
      5. 昇格判定
      6. latent_pearl_cache.json に保存

    Returns:
        {
          "latent_pearls": dict,
          "promoted":      list,
          "stats":         dict,
          "cached":        bool,
          "timestamp":     str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  💎 潜伏銘柄管理（Wave 7 - Step 8）実行中...")

    # ── キャッシュ確認 ──────────────────────────────────────────
    if not force_refresh:
        cached = _load_cache(cfg)
        if cached:
            stats = cached.get("stats", {})
            print(
                f"    ✅ キャッシュ使用: "
                f"潜伏={stats.get('total_latent', 0)} / "
                f"昇格対象={stats.get('promotion_eligible', 0)} / "
                f"昇格済={stats.get('promoted_count', 0)}"
            )
            return {
                "latent_pearls": cached.get("latent_pearls", {}),
                "promoted":      cached.get("promoted", []),
                "stats":         stats,
                "cached":        True,
                "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    today_str = datetime.now().strftime("%Y-%m-%d")

    # ── 既存キャッシュ（promoted フィールド継承用）──────────────
    prev_cache   = _load_cache(cfg) or {}
    prev_entries = prev_cache.get("latent_pearls", {})

    # ── 候補銘柄収集 ─────────────────────────────────────────
    print("    📋 候補銘柄収集中...")
    candidates = _collect_candidate_tickers(cfg)
    print(f"       候補数: {len(candidates)} 銘柄")

    latent_pearls: dict = {}
    promoted_list: list = list(prev_cache.get("promoted", []))

    for item in candidates:
        ticker = item.get("ticker", "")
        market = item.get("market", "JP")
        if not ticker:
            continue
        try:
            # ── データ収集 ────────────────────────────────────
            indep_score, catalyst_score, catalyst_flags = _get_independence_data(ticker, cfg)
            cross_strength, cross_signal_types          = _get_cross_signal_data(ticker, cfg)
            midterm_score, midterm_trajectory            = _get_midterm_data(ticker, cfg)
            nano_rank = _get_nano_queue_rank(ticker, cfg)

            # ── 検出条件評価 ──────────────────────────────────
            conditions = _eval_detection_conditions(
                ticker, indep_score, catalyst_score,
                cross_strength, cross_signal_types,
                midterm_score, midterm_trajectory, nano_rank,
            )

            # 条件が1つも満たせない場合はスキップ
            if not conditions:
                continue

            # ── 潜伏Pearlスコア計算 ───────────────────────────
            lp_score = _calc_latent_pearl_score(
                indep_score, cross_strength, catalyst_score,
                midterm_score, conditions, cfg,
            )

            # ── 昇格判定 ──────────────────────────────────────
            prev_entry = prev_entries.get(ticker, {})
            promo_eligible, promoted, promoted_at = _check_promotion(
                ticker, lp_score, prev_entry, cfg
            )
            if promoted and ticker not in promoted_list:
                promoted_list.append(ticker)

            # ── エントリ構築 ──────────────────────────────────
            latent_pearls[ticker] = {
                "ticker":               ticker,
                "market":               market,
                "detection_conditions": conditions,
                "independence_score":   round(indep_score, 2),
                "catalyst_score":       round(catalyst_score, 2),
                "catalyst_flags":       catalyst_flags[:5],
                "cross_signal_strength": round(cross_strength, 4),
                "cross_signal_types":   cross_signal_types,
                "midterm_score":        round(midterm_score, 2),
                "midterm_trajectory":   midterm_trajectory,
                "nano_queue_rank":      nano_rank,
                "latent_pearl_score":   lp_score,
                "promotion_eligible":   promo_eligible,
                "promoted":             promoted,
                "promoted_at":          promoted_at,
                "first_detected":       prev_entry.get("first_detected", today_str),
                "last_updated":         today_str,
            }

        except Exception as e:
            print(f"    ⚠️  [{ticker}] 評価エラー: {str(e)[:60]}")
            continue

    # ── 統計集計 ─────────────────────────────────────────────
    promo_eligible_count = sum(
        1 for e in latent_pearls.values() if e.get("promotion_eligible")
    )
    condition_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    for entry in latent_pearls.values():
        for cond in entry.get("detection_conditions", []):
            condition_counts[cond] = condition_counts.get(cond, 0) + 1

    top5 = sorted(
        latent_pearls.values(),
        key=lambda x: x.get("latent_pearl_score", 0),
        reverse=True,
    )[:5]

    stats = {
        "total_latent":      len(latent_pearls),
        "promotion_eligible": promo_eligible_count,
        "promoted_count":    len(promoted_list),
        "condition_distribution": condition_counts,
        "top_5": [
            {"ticker": e["ticker"], "score": e["latent_pearl_score"]}
            for e in top5
        ],
    }

    # ── 保存 ─────────────────────────────────────────────────
    cache_data = {
        "latent_pearls": latent_pearls,
        "promoted":      promoted_list,
        "stats":         stats,
        "version":       "1.0",
    }
    _save_cache(cfg, cache_data)

    print(
        f"    ✅ 潜伏管理完了: "
        f"潜伏={len(latent_pearls)} / "
        f"昇格対象={promo_eligible_count} / "
        f"昇格済={len(promoted_list)}"
    )
    if top5:
        top_str = " / ".join(
            f"{e['ticker']}:{e['latent_pearl_score']:.1f}" for e in top5[:3]
        )
        print(f"    💎 潜伏Pearls上位3: {top_str}")

    return {
        "latent_pearls": latent_pearls,
        "promoted":      promoted_list,
        "stats":         stats,
        "cached":        False,
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 9: 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  💎 latent_pearl_manager.py v1.0 単体テスト")
    print("=" * 60)

    # ── 検出条件評価テスト ────────────────────────────────────
    print("\n  【検出条件評価テスト】")
    test_cases = [
        # (indep, cat, cross_str, cross_types, midterm, trajectory, nano_rank)
        (70.0, 4.0, 5.0, ["upward_spillover"], 85.0, "ahead",    0.05),
        (60.0, 2.0, 2.0, ["jp_us_cross"],      60.0, "on_track", None),
        (40.0, 1.0, 1.0, [],                   50.0, "behind",   0.08),
        (75.0, 5.0, 7.0, ["upward_spillover",
                           "jp_us_cross"],     90.0, "ahead",    0.05),
    ]
    for i, (indep, cat, cross_s, cross_t, mid, traj, nano) in enumerate(test_cases):
        conds = _eval_detection_conditions(
            f"TEST{i}", indep, cat, cross_s, cross_t, mid, traj, nano
        )
        score = _calc_latent_pearl_score(indep, cross_s, cat, mid, conds)
        print(
            f"    ケース{i+1}: 条件={conds} "
            f"score={score:.1f} "
            f"昇格={'✅' if score >= _PROMOTION_THRESHOLD else '❌'}"
        )

    # ── スコア計算詳細テスト ─────────────────────────────────
    print("\n  【スコア計算詳細】")
    for cond_len in [1, 2, 3, 4]:
        score = _calc_latent_pearl_score(70.0, 6.0, 4.0, 80.0, ["A"] * cond_len)
        print(f"    条件数={cond_len}: score={score:.1f}")

    # ── run テスト（依存キャッシュなし想定）──────────────────
    print("\n  【run テスト（キャッシュなし環境）】")
    result = run_latent_pearl_manager(cfg=None, force_refresh=True)
    print(f"    潜伏銘柄数  : {result['stats'].get('total_latent', 0)}")
    print(f"    昇格対象数  : {result['stats'].get('promotion_eligible', 0)}")
    print(f"    条件分布    : {result['stats'].get('condition_distribution', {})}")

    # ── アクセサテスト ────────────────────────────────────────
    print("\n  【アクセサテスト（キャッシュなし → None/[] 返却確認）】")
    print(f"    get_latent_pearl_score('6758.T'): {get_latent_pearl_score('6758.T')}")
    print(f"    get_promoted_tickers()          : {get_promoted_tickers()}")
    print(f"    is_latent_pearl('NVDA')         : {is_latent_pearl('NVDA')}")

    print(f"\n  ✅ 単体テスト完了")
