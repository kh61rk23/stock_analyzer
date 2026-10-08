# ==============================================================
# politician_signal_scorer.py  v1.0  (Policy Ph2)
# 議員取引スコア完全版・Level 4 Override エンジン
# ==============================================================
# 【役割】
#   politician_monitor.py（下院のみ・単純スコア）を拡張し、
#   ① 上院データ取得ロジックの追加
#   ② 上院・下院両院の委員会マッピング完全版
#   ③ 重要投票日との近接距離によるスコア補正
#   ④ スコア≥80 時の Level 4 Override（composite_score に直接上乗せ）
#   を実装する。
#
# 【スコア構造（Ph2拡張版）】
#   base_score     = size_score（20〜100pt）
#   committee_mult = 委員会所属重み（1.0〜1.8）    ← 上院は最大1.8
#   sector_bonus   = JP業種整合時 +10pt
#   buy_bonus      = 買い取引 +8pt
#   vote_proximity = 投票日との近接ボーナス（0〜+15pt）← Ph2新設
#   chamber_bonus  = 上院議員 +5pt                   ← Ph2新設
#   $final\_score = \min(100, base \times committee\_mult$
#                 $+ sector\_bonus + buy\_bonus$
#                 $+ vote\_proximity + chamber\_bonus)$
#
# 【Level 4 Override 発動条件】
#   $final\_score \ge 80$ → Level 4 Override
#   $score\_adj = \min(SCORE\_MAX\_POLICY\_L4\_ADJ,$
#                $(score - 80) \times 0.5)$
#   composite\_score に score_adj を直接加算（jp_us_sector_bridge.py 経由）
#
# 【上院データソース】
#   上院データの新規取得は行わない
#   （efts.senate.gov は実在しないホスト。実在する efdsearch.senate.gov は
#     利用規約同意のセッションCookieを要求するため同じ作りでは取得不可）。
#   politician_history.json に残る既存 Senate データの再スコアのみ行う。
#
# 【依存】
#   politician_monitor.py（スコアリング関数を拡張して再利用）
#   config.py（BLOCK 26/31定数参照）
#   ★ 全関数を try/except で保護。失敗時は score=0.0 / override=False を返す。
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config_sync import sync_module_constants

# ==============================================================
# BLOCK 0: 定数
# ==============================================================

# 上院 eFD への通信は行わない（efts.senate.gov は実在しないホスト）。
# politician_history.json に残る既存の上院データを再スコアする経路
# （get_senate_transactions）だけを使う。

# 上院委員会 → committee_multiplier（最大 1.8）
_SENATE_COMMITTEE_MULTIPLIER: dict[str, float] = {
    # 金融・経済
    "finance":                              1.8,   # 財政委員会（税制・貿易）
    "banking, housing, and urban affairs":  1.7,
    "banking":                              1.7,
    "budget":                               1.5,
    "appropriations":                       1.5,
    # 防衛・安全保障
    "armed services":                       1.6,
    "intelligence":                         1.6,
    "homeland security and governmental affairs": 1.4,
    # 産業・技術
    "commerce, science, and transportation": 1.5,
    "commerce":                             1.5,
    "energy and natural resources":         1.4,
    "environment and public works":         1.3,
    # 国際・外交
    "foreign relations":                    1.4,
    # 司法
    "judiciary":                            1.3,
    # その他
    "health, education, labor, and pensions": 1.2,
    "agriculture, nutrition, and forestry": 1.2,
    "small business and entrepreneurship":  1.1,
}

# 下院委員会 → committee_multiplier（既存 politician_monitor.py と同一・拡張）
_HOUSE_COMMITTEE_MULTIPLIER: dict[str, float] = {
    "ways and means":                        1.6,
    "financial services":                    1.5,
    "intelligence":                          1.5,
    "armed services":                        1.4,
    "energy and commerce":                   1.4,
    "foreign affairs":                       1.3,
    "judiciary":                             1.2,
    "appropriations":                        1.2,
    "science, space, and technology":        1.2,
    "natural resources":                     1.2,
    "agriculture":                           1.1,
    "transportation and infrastructure":     1.1,
    "oversight and accountability":          1.1,
    "homeland security":                     1.2,
    "rules":                                 1.1,
}

# 重要投票カテゴリ → 近接ボーナスの最大値
_VOTE_CATEGORY_MAX_BONUS: dict[str, float] = {
    "tax":           15.0,   # 税制改革
    "semiconductor": 12.0,   # 半導体規制（CHIPS法等）
    "defense":       12.0,   # 防衛予算
    "healthcare":    10.0,   # 医療保険
    "energy":        10.0,   # エネルギー政策
    "trade":         10.0,   # 通商政策
    "financial":      8.0,   # 金融規制
    "other":          5.0,   # その他
}

# 投票日との近接日数 → 補正乗数
_VOTE_PROXIMITY_TIERS: list[tuple[int, float]] = [
    (3,  1.0),   # 3日以内 → 100%ボーナス
    (7,  0.75),  # 7日以内 → 75%
    (14, 0.50),  # 14日以内 → 50%
    (30, 0.25),  # 30日以内 → 25%
]

# Level 4 Override 閾値（config.py BLOCK 31 で上書き可能）
_L4_SCORE_THRESHOLD = 80.0
_L4_MULTIPLIER      = 0.5    # (score - 80) × 0.5 = adj
_L4_MAX_ADJ         = 25.0   # SCORE_MAX_POLICY_L4_ADJ

# 上院議員チャンバーボーナス
_SENATE_CHAMBER_BONUS = 5.0

_CFG_CONST_MAP = {
    "PSS_SENATE_COMMITTEE_MULTIPLIER": "_SENATE_COMMITTEE_MULTIPLIER",
    "PSS_HOUSE_COMMITTEE_MULTIPLIER": "_HOUSE_COMMITTEE_MULTIPLIER",
    "PSS_VOTE_CATEGORY_MAX_BONUS": "_VOTE_CATEGORY_MAX_BONUS",
    "PSS_L4_SCORE_THRESHOLD": "_L4_SCORE_THRESHOLD",
    "PSS_L4_MULTIPLIER": "_L4_MULTIPLIER",
    "PSS_SENATE_CHAMBER_BONUS": "_SENATE_CHAMBER_BONUS",
    'B6_POLITICIAN_SIGNAL_SCORER_L4_MAX_ADJ': '_L4_MAX_ADJ',
}


# ==============================================================
# BLOCK 1: 委員会スコア取得
# ==============================================================

def get_committee_multiplier(
    committees:   list[str],
    chamber:      str = "house",
) -> float:
    """
    議員の委員会所属リストから最大の committee_multiplier を返す。

    Args:
        committees: ["Ways and Means", "Budget", ...] 形式の委員会名リスト
        chamber:    "house" | "senate"

    Returns:
        float: 1.0〜1.8（最大値）
    """
    try:
        lookup = (
            _SENATE_COMMITTEE_MULTIPLIER
            if chamber.lower() == "senate"
            else _HOUSE_COMMITTEE_MULTIPLIER
        )
        max_mult = 1.0
        for com in (committees or []):
            cl = str(com).lower().strip()
            for key, mult in lookup.items():
                if key in cl or cl in key:
                    max_mult = max(max_mult, mult)
                    break
        return round(max_mult, 2)
    except Exception:
        return 1.0


# ==============================================================
# BLOCK 2: 投票日近接ボーナス
# ==============================================================

def calc_vote_proximity_bonus(
    tx_date:       str,
    vote_calendar: list[dict],
    categories:    list[str],
    cfg=None,
) -> float:
    """
    取引日と重要投票日の近接距離からボーナスを算出する（Ph2新設）。

    Args:
        tx_date:       "YYYY-MM-DD" 形式の取引日
        vote_calendar: [{"date": "YYYY-MM-DD", "category": str, "title": str}, ...]
        categories:    取引対象銘柄の関連カテゴリ（例: ["semiconductor", "defense"]）
        cfg:           config モジュール（None 可）

    Returns:
        float: vote_proximity_bonus（0.0〜15.0）
    """
    try:
        if not vote_calendar or not tx_date:
            return 0.0

        tx_dt = datetime.strptime(tx_date[:10], "%Y-%m-%d")
        max_bonus = 0.0

        for vote in vote_calendar:
            try:
                vote_date = vote.get("date", "")
                vote_cat  = vote.get("category", "other").lower()
                if not vote_date:
                    continue

                v_dt = datetime.strptime(vote_date[:10], "%Y-%m-%d")
                days_diff = abs((tx_dt - v_dt).days)

                # カテゴリが取引と関連しているか確認
                cat_relevant = (
                    not categories
                    or any(vote_cat in c.lower() or c.lower() in vote_cat
                           for c in categories)
                )
                if not cat_relevant:
                    continue

                # 近接タイアーから補正係数を取得
                proximity_factor = 0.0
                for max_days, factor in _VOTE_PROXIMITY_TIERS:
                    if days_diff <= max_days:
                        proximity_factor = factor
                        break

                if proximity_factor <= 0:
                    continue

                cat_max = _VOTE_CATEGORY_MAX_BONUS.get(vote_cat,
                          _VOTE_CATEGORY_MAX_BONUS["other"])
                bonus = cat_max * proximity_factor
                max_bonus = max(max_bonus, bonus)

            except Exception:
                continue

        return round(min(15.0, max_bonus), 2)

    except Exception:
        return 0.0


# ==============================================================
# BLOCK 3: Ph2 拡張スコアリング
# ==============================================================

def score_transaction_ph2(
    tx:            dict,
    politician:    str,
    chamber:       str  = "house",
    committees:    Optional[list[str]] = None,
    vote_calendar: Optional[list[dict]] = None,
    categories:    Optional[list[str]]  = None,
    cfg=None,
) -> dict:
    """
    Ph2 拡張スコアリングを実行する。

    Ph1（politician_monitor.py の score_transaction）に対して、
    ① 上院チャンバーボーナス
    ② 投票日近接ボーナス
    ③ Level 4 Override 判定
    を追加する。

    Args:
        tx:            取引辞書（politician_monitor の parse 結果準拠）
        politician:    議員名
        chamber:       "house" | "senate"
        committees:    議員の所属委員会リスト
        vote_calendar: 重要投票カレンダー
        categories:    銘柄の関連カテゴリリスト
        cfg:           config モジュール（None 可）

    Returns:
        {
          "score":             float,   # 0〜100
          "base_score":        float,
          "committee_mult":    float,
          "sector_bonus":      float,
          "buy_bonus":         float,
          "vote_proximity":    float,   # Ph2新設
          "chamber_bonus":     float,   # Ph2新設
          "is_level4_override": bool,
          "level4_adj":        float,   # composite_score への加算量
          "politician":        str,
          "chamber":           str,
          "asset":             str,
          "direction":         str,
          "jp_sector_codes":   list[str],
          "transaction_date":  str,
        }
    """
    _default = {
        "score": 0.0, "base_score": 0.0, "committee_mult": 1.0,
        "sector_bonus": 0.0, "buy_bonus": 0.0, "vote_proximity": 0.0,
        "chamber_bonus": 0.0, "is_level4_override": False, "level4_adj": 0.0,
        "politician": politician, "chamber": chamber, "asset": "",
        "direction": "unknown", "jp_sector_codes": [], "transaction_date": "",
    }

    try:
        # ── 基本スコア取得（Ph1ロジックを再利用） ────────────────
        from politician_monitor import (
            _SIZE_SCORE_MAP, _SIZE_LABEL_MAP, _GICS_TO_JP_CODES
        )

        tx_type   = str(tx.get("type", tx.get("transaction_type", ""))).upper()
        direction = "buy" if tx_type in ("P", "BUY", "PURCHASE") else (
                    "sell" if tx_type in ("S", "SALE", "SELL") else "unknown")

        size_raw  = str(tx.get("amount", tx.get("size", "1")))
        base_score = float(
            _SIZE_SCORE_MAP.get(size_raw)
            or next(
                (v for k, v in _SIZE_LABEL_MAP.items()
                 if k.lower() in size_raw.lower()),
                20.0,
            )
        )

        # ── 委員会重み ─────────────────────────────────────────
        committee_mult = get_committee_multiplier(committees or [], chamber)

        # ── セクターボーナス ───────────────────────────────────
        gics = str(tx.get("gics_sector", tx.get("sector", "")))
        jp_codes = []
        sector_bonus = 0.0
        for key, codes in _GICS_TO_JP_CODES.items():
            if key.lower() in gics.lower() or gics.lower() in key.lower():
                jp_codes = codes
                sector_bonus = 10.0
                break

        # ── 買いボーナス ───────────────────────────────────────
        buy_bonus = 8.0 if direction == "buy" else 0.0

        # ── 上院チャンバーボーナス（Ph2新設） ──────────────────
        chamber_bonus = _SENATE_CHAMBER_BONUS if chamber.lower() == "senate" else 0.0

        # ── 投票日近接ボーナス（Ph2新設） ──────────────────────
        tx_date   = str(tx.get("transaction_date", tx.get("date", "")))
        vote_prox = calc_vote_proximity_bonus(
            tx_date, vote_calendar or [], categories or [], cfg
        )

        # ── 最終スコア計算 ─────────────────────────────────────
        raw_score = (
            base_score * committee_mult
            + sector_bonus + buy_bonus
            + chamber_bonus + vote_prox
        )
        final_score = round(min(100.0, max(0.0, raw_score)), 1)

        # ── Level 4 Override 判定 ──────────────────────────────
        l4_threshold = float(getattr(cfg, "POLITICIAN_L4_SCORE_THRESHOLD",
                                      _L4_SCORE_THRESHOLD))
        l4_max_adj   = float(getattr(cfg, "SCORE_MAX_POLICY_L4_ADJ", _L4_MAX_ADJ))

        is_l4  = final_score >= l4_threshold
        l4_adj = 0.0
        if is_l4:
            # $score\_adj = \min(L4\_MAX, (score - 80) \times 0.5)$
            l4_adj = round(min(l4_max_adj,
                               (final_score - l4_threshold) * _L4_MULTIPLIER), 2)

        return {
            "score":              final_score,
            "base_score":         base_score,
            "committee_mult":     committee_mult,
            "sector_bonus":       sector_bonus,
            "buy_bonus":          buy_bonus,
            "vote_proximity":     vote_prox,
            "chamber_bonus":      chamber_bonus,
            "is_level4_override": is_l4,
            "level4_adj":         l4_adj,
            "politician":         politician,
            "chamber":            chamber,
            "asset":              str(tx.get("asset", tx.get("ticker", ""))),
            "direction":          direction,
            "jp_sector_codes":    jp_codes,
            "transaction_date":   tx_date,
        }

    except Exception as e:
        return {**_default,
                "score": 0.0,
                "error": str(e)[:60]}


# ==============================================================
# BLOCK 4: 上院データ取得
# ==============================================================

def get_senate_transactions(
    days:   int   = 30,
    cfg=None,
) -> list[dict]:
    """
    直近 days 日分の上院 PTR 取引を返す。
    API 失敗時は politician_history.json の Senate データを返す。

    Returns:
        list[dict]: 上院取引リスト（空の場合あり）
    """
    try:
        start_dt = datetime.now() - timedelta(days=days)
        start    = start_dt.strftime("%Y-%m-%d")

        # ── politician_history.json の Senate データを再スコア ──
        # eFD API からの取得は行わず、既存履歴のみを扱う。
        try:
            cache_dir = Path(getattr(cfg, "CACHE_DIR", "/tmp"))
            p = cache_dir / "politician_history.json"
            if p.exists():
                history = json.loads(p.read_text(encoding="utf-8"))
                all_txs = history.get("transactions", [])
                return [
                    t for t in all_txs
                    if str(t.get("chamber", "house")).lower() == "senate"
                    and t.get("transaction_date", "") >= start
                ]
        except Exception:
            pass

        return []

    except Exception:
        return []


# ==============================================================
# BLOCK 5: Level 4 Override 管理
# ==============================================================

def extract_level4_overrides(
    scored_trades: list[dict],
    cfg=None,
) -> list[dict]:
    """
    スコア済み取引リストから Level 4 Override 対象を抽出する。

    policy_signal_applier.py が composite_score への直接加算に使用。

    Args:
        scored_trades: score_transaction_ph2() の返り値リスト
        cfg:           config モジュール（None 可）

    Returns:
        [
          {
            "jp_sector_codes": list[str],
            "level4_adj":      float,       # composite_score への加算量
            "score":           float,
            "politician":      str,
            "asset":           str,
            "direction":       str,
          }, ...
        ]
        level4_adj 降順にソート済み
    """
    try:
        l4_trades = [
            {
                "jp_sector_codes": t.get("jp_sector_codes", []),
                "level4_adj":      t.get("level4_adj", 0.0),
                "score":           t.get("score", 0.0),
                "politician":      t.get("politician", ""),
                "asset":           t.get("asset", ""),
                "direction":       t.get("direction", ""),
            }
            for t in scored_trades
            if t.get("is_level4_override") and t.get("level4_adj", 0) > 0
        ]
        return sorted(l4_trades, key=lambda x: x["level4_adj"], reverse=True)
    except Exception:
        return []


def build_l4_sector_boosts(
    l4_overrides: list[dict],
) -> dict[str, float]:
    """
    Level 4 Override リストからセクター別の加算量を集計する。

    同一セクターに複数の L4 Override がある場合は最大値を採用。
    sell 方向の場合は加算量を負に変換。

    Returns:
        {sector_code: adj_pt, ...}
    """
    try:
        boosts: dict[str, float] = {}
        for override in l4_overrides:
            adj  = float(override.get("level4_adj", 0.0))
            direction = str(override.get("direction", "buy"))
            if direction == "sell":
                adj = -adj

            for sc in override.get("jp_sector_codes", []):
                existing = boosts.get(str(sc), 0.0)
                # 同方向なら加算、逆方向なら上書きしない
                if abs(adj) > abs(existing):
                    boosts[str(sc)] = round(adj, 2)
        return boosts
    except Exception:
        return {}


# ==============================================================
# BLOCK 6: メインエントリポイント
# ==============================================================

def run_politician_signal_scorer(
    politician_results: Optional[dict] = None,
    results:            Optional[dict] = None,
    cfg=None,
) -> dict:
    """
    Policy Ph2 のメインスコアリングエントリポイント。
    policy_signal_applier.py から呼ばれる。

    処理フロー:
      1. 上院取引を取得
      2. 既存の下院高スコア取引に Ph2 スコアを再適用
      3. 全取引を score_transaction_ph2() でスコアリング
      4. Level 4 Override を抽出
      5. セクター別ブースト量を集計
      6. 結果を返す

    Args:
        politician_results: run_politician_monitor() の返り値（None 可）
        results:            stock_analyzer_main.results 辞書（None 可）
        cfg:                config モジュール（None 可）

    Returns:
        {
          "all_scored_trades":   list[dict],
          "level4_overrides":    list[dict],
          "sector_boosts":       dict[str, float],   # {sector_code: adj_pt}
          "senate_count":        int,
          "house_count":         int,
          "l4_count":            int,
          "summary":             str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("  🏛️  [politician_signal_scorer] Ph2スコアリング実行中...")

    if politician_results is None:
        politician_results = {}
    if results is None:
        results = {}

    all_scored: list[dict] = []

    # ── ① 下院高スコア取引を Ph2 で再スコアリング ────────────
    house_trades = politician_results.get("high_score_trades", [])
    house_count  = 0
    for tx in house_trades:
        try:
            scored = score_transaction_ph2(
                tx        = tx,
                politician = tx.get("politician", ""),
                chamber    = "house",
                committees = tx.get("committees", []),
                cfg        = cfg,
            )
            all_scored.append(scored)
            house_count += 1
        except Exception:
            pass

    # ── ② 上院取引を取得してスコアリング ─────────────────────
    senate_count = 0
    try:
        senate_txs = get_senate_transactions(
            days=int(getattr(cfg, "POLITICIAN_SENATE_WINDOW_DAYS", 30)),
            cfg=cfg,
        )
        for tx in senate_txs:
            try:
                scored = score_transaction_ph2(
                    tx        = tx,
                    politician = tx.get("politician", ""),
                    chamber    = "senate",
                    committees = tx.get("committees", []),
                    cfg        = cfg,
                )
                all_scored.append(scored)
                senate_count += 1
            except Exception:
                pass
        if senate_count:
            print(f"    🏛️  上院取引: {senate_count}件スコアリング完了")
    except Exception as e:
        print(f"    ⚠️  上院データ取得エラー（スキップ）: {str(e)[:60]}")

    # ── ③ Level 4 Override 抽出 ──────────────────────────────
    l4_overrides  = extract_level4_overrides(all_scored, cfg)
    sector_boosts = build_l4_sector_boosts(l4_overrides)

    l4_count = len(l4_overrides)
    if l4_count:
        print(f"    ⭐ Level 4 Override: {l4_count}件")
        for ov in l4_overrides[:3]:
            print(f"      [{ov['score']:.0f}pt] {ov['politician']} "
                  f"{ov['direction']} {ov['asset'][:30]}"
                  f" adj=+{ov['level4_adj']:.1f}pt")

    summary = (
        f"下院{house_count}件 / 上院{senate_count}件 / "
        f"L4 Override {l4_count}件 / "
        f"セクターブースト{len(sector_boosts)}業種"
    )
    print(f"  ✅ Ph2スコアリング完了: {summary}")

    return {
        "all_scored_trades": all_scored,
        "level4_overrides":  l4_overrides,
        "sector_boosts":     sector_boosts,
        "senate_count":      senate_count,
        "house_count":       house_count,
        "l4_count":          l4_count,
        "summary":           summary,
    }
