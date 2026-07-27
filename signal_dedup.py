# ==============================================================
# signal_dedup.py  v1.0  (③ シグナル重複計上防止)
# ==============================================================
# 【役割】
#   MACRO_RULES / CROSS_SECTOR_RULES / D-1 / D-2 の全シグナルを
#   1つのリストに集約し、同一業種への重複加算を防ぐ。
#
# 【問題の具体例（修正前）】
#   円安 (MACRO_RULES "yen_depreciation") → 輸出業種 +10pt
#   米国ISM拡大 (CROSS_SECTOR_RULES)    → 輸出業種 +5pt
#   D-1 象限B                            → 輸出業種 +5pt
#   → 3つ同時発動で同一業種に +20pt の重複加算
#
# 【解決ロジック】
#   1. 全シグナルを1つのリストに収集（ソース・方向・業種コード・pts）
#   2. 同一業種に同一方向のシグナルが3件以上 → 上位2件のみ採用
#   3. 業種ごとの合計補正量に上限を適用
#      +補正合計 ≤ SCORE_MAX_TOTAL_ADJ_POS（デフォルト: +20pt）
#      -補正合計 ≥ SCORE_MAX_TOTAL_ADJ_NEG（デフォルト: -20pt）
#
# 【適用タイミング】
#   main.py の sector 分析完了後・apply_bayesian_to_sector_results() の直前
#   → 全シグナルが揃ったタイミングで一括適用
#
# 【出力】
#   sector_results の composite_score を dedup 済みの補正量で調整する
#   + results["signal_dedup_log"] に除外されたシグナルを記録
#
# 【依存】config.py
# ==============================================================

from __future__ import annotations

from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

# 業種ごとの合計補正上限（全ソース合算）

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'SD_SCORE_MAX_TOTAL_ADJ_POS': 'SCORE_MAX_TOTAL_ADJ_POS',
    'SD_SCORE_MAX_TOTAL_ADJ_NEG': 'SCORE_MAX_TOTAL_ADJ_NEG',
}

SCORE_MAX_TOTAL_ADJ_POS = 20.0   # +方向の上限
SCORE_MAX_TOTAL_ADJ_NEG = -20.0  # -方向の下限

# 同一業種・同一方向のシグナルの採用上位件数
MAX_SAME_DIRECTION_SIGNALS = 2

# シグナルソースの優先順位（同一業種・同一方向で採用上限を超えた場合に優先度が高い方を残す）
SOURCE_PRIORITY = {
    "macro_rules":    5,   # 最高優先度（MACRO_RULES: 直接・確定）
    "d1_cycle":       4,   # 経済サイクル判定
    "d2_debt":        3,   # 債務サイクル
    "cross_sector":   2,   # 横断産業シグナル
    "leading_indicator": 1, # 先行指標
    "google_trends":  1,
    "finbert":        1,
    "policy_radar":   1,
    "us_macro":       1,
}


# ==============================================================
# BLOCK 1: シグナル収集
# ==============================================================

def collect_all_signals(results: dict, cfg=None) -> list[dict]:
    """
    results から全ソースのシグナルを収集して統一形式に変換する。

    Returns:
        [
          {
            "source":      "macro_rules" / "d1_cycle" 等,
            "sector_code": str,
            "direction":   "tailwind" / "headwind",
            "pts":         float,
            "label":       str,
            "priority":    int,
          }, ...
        ]
    """
    signals = []

    # ── macro_signals（MACRO_RULES から） ───────────────────
    for sig in results.get("macro_signals", []):
        for code, direction in sig.get("sector_signals", {}).items():
            if direction not in ("tailwind", "headwind"):
                continue
            pts = 10.0 if direction == "tailwind" else -10.0
            # rule_weights による重み補正を考慮
            weight = sig.get("weight", 1.0) or 1.0
            signals.append({
                "source":      "macro_rules",
                "sector_code": code,
                "direction":   direction,
                "pts":         round(pts * weight, 2),
                "label":       sig.get("label", ""),
                "priority":    SOURCE_PRIORITY["macro_rules"],
            })

    # ── cross_sector_signals ────────────────────────────────
    for cs in results.get("cross_sector_signals", []):
        for code in cs.get("sectors_tailwind", []):
            signals.append({
                "source":      "cross_sector",
                "sector_code": code,
                "direction":   "tailwind",
                "pts":         float(cs.get("pts_adj", 5.0)),
                "label":       cs.get("label", cs.get("rule_id", "")),
                "priority":    SOURCE_PRIORITY["cross_sector"],
            })
        for code in cs.get("sectors_headwind", []):
            signals.append({
                "source":      "cross_sector",
                "sector_code": code,
                "direction":   "headwind",
                "pts":         float(cs.get("pts_adj", -5.0)),
                "label":       cs.get("label", cs.get("rule_id", "")),
                "priority":    SOURCE_PRIORITY["cross_sector"],
            })

    # ── leading_indicator_signals ───────────────────────────
    for li in results.get("leading_indicator_signals", []):
        code      = li.get("sector_code", "")
        direction = li.get("signal", "neutral")
        pts       = float(li.get("pts_adj", 0.0))
        src       = li.get("source", "leading_indicator")
        if code and direction in ("tailwind", "headwind") and abs(pts) > 0:
            signals.append({
                "source":      src if src in SOURCE_PRIORITY else "leading_indicator",
                "sector_code": code,
                "direction":   direction,
                "pts":         pts,
                "label":       li.get("label", ""),
                "priority":    SOURCE_PRIORITY.get(src, 1),
            })

    # ── D-1 経済サイクル補正 ─────────────────────────────────
    ec = results.get("economic_cycle", {})
    for code, adj_pts in ec.get("sector_adjustments", {}).items():
        if abs(adj_pts) < 0.5:
            continue
        signals.append({
            "source":      "d1_cycle",
            "sector_code": code,
            "direction":   "tailwind" if adj_pts > 0 else "headwind",
            "pts":         float(adj_pts),
            "label":       f"D-1象限{ec.get('phase','?')}補正",
            "priority":    SOURCE_PRIORITY["d1_cycle"],
        })

    # ── D-2 債務サイクル補正 ─────────────────────────────────
    dc = results.get("debt_cycle", {})
    _dc_sector_adj = dc.get("sector_adj", 0.0)
    # sector_adj は float（全業種一律）または dict（業種別）の両形式に対応
    if isinstance(_dc_sector_adj, dict):
        _dc_items = list(_dc_sector_adj.items())
    elif isinstance(_dc_sector_adj, (int, float)) and abs(_dc_sector_adj) >= 0.5:
        # float → results["sectors"] にある全業種コードに一律適用
        _all_sector_codes = list(results.get("sectors", {}).keys())
        _dc_items = [(_c, float(_dc_sector_adj)) for _c in _all_sector_codes]
    else:
        _dc_items = []

    for code, adj_pts in _dc_items:
        if abs(adj_pts) < 0.5:
            continue
        signals.append({
            "source":      "d2_debt",
            "sector_code": code,
            "direction":   "tailwind" if adj_pts > 0 else "headwind",
            "pts":         float(adj_pts),
            "label":       "D-2債務サイクル補正",
            "priority":    SOURCE_PRIORITY["d2_debt"],
        })

    return signals


# ==============================================================
# BLOCK 2: 重複排除と上限適用
# ==============================================================

def deduplicate_signals(
    signals:       list[dict],
    cfg = None,
) -> tuple[dict, dict, list]:
    """
    シグナルを業種ごとに集約し、重複排除と上限適用を行う。

    Args:
        signals : collect_all_signals() の出力
        cfg     : config（上限値のカスタマイズ用）

    Returns:
        (
          approved_by_sector:   {sector_code: [approved_signal, ...]},
          rejected_by_sector:   {sector_code: [rejected_signal, ...]},
          net_adj_by_sector:    [{sector_code, net_pts, accepted_n, rejected_n}, ...]
        )
    """
    max_pos = getattr(cfg, "SCORE_MAX_TOTAL_ADJ_POS", SCORE_MAX_TOTAL_ADJ_POS) if cfg else SCORE_MAX_TOTAL_ADJ_POS
    max_neg = getattr(cfg, "SCORE_MAX_TOTAL_ADJ_NEG", SCORE_MAX_TOTAL_ADJ_NEG) if cfg else SCORE_MAX_TOTAL_ADJ_NEG
    max_same = getattr(cfg, "MAX_SAME_DIRECTION_SIGNALS", MAX_SAME_DIRECTION_SIGNALS) if cfg else MAX_SAME_DIRECTION_SIGNALS

    # 業種 × 方向ごとにシグナルをグループ化
    from collections import defaultdict
    grouped: dict = defaultdict(lambda: {"tailwind": [], "headwind": []})
    for sig in signals:
        code = sig["sector_code"]
        direction = sig["direction"]
        grouped[code][direction].append(sig)

    approved_by_sector = {}
    rejected_by_sector = {}
    net_adj_list       = []

    all_codes = set(sig["sector_code"] for sig in signals)
    for code in sorted(all_codes):
        approved = []
        rejected = []

        for direction in ("tailwind", "headwind"):
            sigs_dir = grouped[code][direction]
            if not sigs_dir:
                continue

            # 優先度降順・pts絶対値降順でソート
            sigs_sorted = sorted(
                sigs_dir,
                key=lambda x: (x["priority"], abs(x["pts"])),
                reverse=True,
            )

            # 同一方向の上位 max_same 件のみ採用
            for i, s in enumerate(sigs_sorted):
                if i < max_same:
                    approved.append(s)
                else:
                    s["reject_reason"] = f"同方向シグナル上限（{max_same}件）超過"
                    rejected.append(s)

        # 全採用シグナルの合算
        net_pts = sum(s["pts"] for s in approved)

        # 合計上限チェック
        if net_pts > max_pos:
            # 超過分をヘッドウィンドシグナルから比例削減
            tail_sigs = [s for s in approved if s["direction"] == "tailwind"]
            tail_sigs_sorted = sorted(tail_sigs, key=lambda x: x["priority"])
            excess = net_pts - max_pos
            for s in tail_sigs_sorted:
                if excess <= 0:
                    break
                reduction = min(abs(s["pts"]), excess)
                s["pts_original"] = s["pts"]
                s["pts"]         = round(s["pts"] - reduction, 2)
                excess           -= reduction
                if abs(s["pts"]) < 0.1:
                    s["reject_reason"] = "合計上限によるトリム"
                    rejected.append(s)
                    approved.remove(s)
            net_pts = max_pos

        elif net_pts < max_neg:
            head_sigs = [s for s in approved if s["direction"] == "headwind"]
            head_sigs_sorted = sorted(head_sigs, key=lambda x: x["priority"])
            excess = net_pts - max_neg   # 負の数
            for s in head_sigs_sorted:
                if excess >= 0:
                    break
                reduction = min(abs(s["pts"]), abs(excess))
                s["pts_original"] = s["pts"]
                s["pts"]         = round(s["pts"] + reduction, 2)
                excess           += reduction
                if abs(s["pts"]) < 0.1:
                    s["reject_reason"] = "合計下限によるトリム"
                    rejected.append(s)
                    approved.remove(s)
            net_pts = max_neg

        approved_by_sector[code] = approved
        rejected_by_sector[code] = rejected
        net_adj_list.append({
            "sector_code": code,
            "net_pts":     round(net_pts, 2),
            "accepted_n":  len(approved),
            "rejected_n":  len(rejected),
        })

    return approved_by_sector, rejected_by_sector, net_adj_list


# ==============================================================
# BLOCK 3: sector_results への適用
# ==============================================================

def apply_deduped_signals(
    sector_results:  dict,
    net_adj_list:    list,
    cfg = None,
) -> tuple[dict, list]:
    """
    dedup 済みの net_adj を sector_results の composite_score に反映する。

    重要: 既に sector_agents が composite_score を計算済みであり、
    macro_signals による加算も済んでいる場合は「差分のみ」を適用する。
    そのため、この関数は「各ソースの生のスコア補正を受け取ってそのまま適用する」
    ベイズ更新（GAP-1）の直前に挿入する設計とする。

    Returns:
        (updated_sector_results, dedup_log)
    """
    dedup_log = []
    for item in net_adj_list:
        code    = item["sector_code"]
        net_pts = item["net_pts"]
        if net_pts == 0 or code not in sector_results:
            continue

        res = sector_results.get(code, {})
        if not isinstance(res, dict):
            continue

        old_score = res.get("composite_score", 50.0)
        new_score = round(max(0.0, min(100.0, old_score + net_pts)), 1)
        res["composite_score"]       = new_score
        res["signal_dedup_adj"]      = net_pts
        res["signal_dedup_accepted"] = item["accepted_n"]
        res["signal_dedup_rejected"] = item["rejected_n"]

        if item["rejected_n"] > 0 or abs(net_pts) > 10:
            dedup_log.append({
                "code":       code,
                "net_pts":    net_pts,
                "rejected_n": item["rejected_n"],
                "old_score":  old_score,
                "new_score":  new_score,
            })

    return sector_results, dedup_log


# ==============================================================
# BLOCK 4: メインエントリポイント
# ==============================================================

def run_signal_dedup(
    sector_results: dict,
    results:        dict,
    cfg = None,
) -> tuple[dict, dict]:
    """
    全シグナルの重複排除と上限適用を一括実行する。

    main.py の apply_bayesian_to_sector_results() の直前に呼び出す:
        _sd = _load_local_module("signal_dedup", ...)
        if _sd:
            sector_results, dedup_info = _sd.run_signal_dedup(
                sector_results, results, cfg
            )
            results["signal_dedup"] = dedup_info

    Returns:
        (updated_sector_results, dedup_info_dict)
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_SIGNAL_DEDUP", True) if cfg else True:
        pass

    # Step1: 全シグナルを収集
    all_signals = collect_all_signals(results, cfg)

    if not all_signals:
        return sector_results, {"skipped": True, "reason": "シグナルなし"}

    # Step2: 重複排除と上限適用
    approved, rejected, net_adj_list = deduplicate_signals(all_signals, cfg)

    # Step3: sector_results に反映
    updated, dedup_log = apply_deduped_signals(sector_results, net_adj_list, cfg)

    # 統計
    total_rejected = sum(len(v) for v in rejected.values())
    total_accepted = sum(len(v) for v in approved.values())
    sectors_capped = sum(
        1 for item in net_adj_list
        if item["rejected_n"] > 0
    )

    if dedup_log:
        print(f"    🔧 [signal_dedup] 重複除去: "
              f"採用{total_accepted}件 / 除外{total_rejected}件 / "
              f"上限適用{sectors_capped}業種")

    return updated, {
        "total_signals":    len(all_signals),
        "total_accepted":   total_accepted,
        "total_rejected":   total_rejected,
        "sectors_capped":   sectors_capped,
        "net_adj_list":     net_adj_list,
        "dedup_log":        dedup_log,
    }
