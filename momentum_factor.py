# ==============================================================
# momentum_factor.py  v1.0
# ==============================================================
# 【概要】
#   score_history.json から業種別モメンタムを計算し、
#   composite_score に ±5〜±10点を加算する。
#
#   Fama-French 12M モメンタムの業種スコア版として実装。
#   原典の「12M-1M」に相当するのは「4週分のスコア変化速度」。
#
# 【アルゴリズム】
#   1. score_history.json を読み込む
#   2. 直近 LOOKBACK_DAYS 日のスコアを時系列に並べ、
#      最小二乗法で線形回帰スロープ（点/日）を算出
#   3. スロープと絶対水準からモメンタム強度を 5段階分類
#   4. composite_score に加算（上限/下限クランプ付き）
#
# 【統合方法】
#   stock_analyzer_main.py の run_all_tasks() 内で
#   sector_results 確定後・score_history 保存前 に呼び出す。
#   詳細は変更指示書を参照。
#
# 【依存】
#   - score_history.json が DATA_DIR/score_history.json に存在すること
#   - stock_analyzer_main.py の 1-6 実装（_append_score_history）が完了済みであること
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# 定数（チューニング用）
# ==============================================================

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_MOMENTUM_FACTOR_LOOKBACK_DAYS': 'LOOKBACK_DAYS',
    'B6_MOMENTUM_FACTOR_MIN_DATA_POINTS': 'MIN_DATA_POINTS',
}

LOOKBACK_DAYS   = 28     # 4週分（Fama-French12Mの業種版として短期化）
MIN_DATA_POINTS = 5      # 最低データ点数（これ未満はモメンタム計算スキップ）
SCORE_CEILING   = 100.0  # composite_score 上限
SCORE_FLOOR     = -30.0  # composite_score 下限（ペナルティ込み）

# モメンタム調整幅（点/週スロープ → 加算点数）のしきい値
# 2026-09-17: 表の値（2.5 / 1.0）を点/週のスロープと比較しており、各行の
# コメント（週7点 / 週3.5点）の半分以下の変化で「強い加速」になっていた。
# 33業種中25〜28業種が毎日 ±10点の補正を受けていたため、コメントどおり点/週に揃える。
MOMENTUM_TIERS = [
    #  (slope_per_week_threshold, adjustment, label)
    ( 7.0,  10, "強い上昇加速"),   # 週7点超のペース
    ( 3.5,   5, "上昇継続"),       # 週3.5〜7点のペース
    (-3.5,   0, "横ばい"),         # ±週3.5点未満
    (-7.0,  -5, "下落継続"),       # 週3.5〜7点下落のペース
    (-999, -10, "強い下落加速"),   # 週7点超の下落ペース
]


# ==============================================================
# BLOCK 1: データ読み込み
# ==============================================================

def load_score_history(path: Path | str) -> dict:
    """
    score_history.json を読み込む。

    Args:
        path: score_history.json のパス

    Returns:
        {"YYYY-MM-DD": {"sector_code": {"score": float, "risk": str}, ...}, ...}
        読み込み失敗時は空辞書 {} を返す
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠️  score_history 読み込みエラー: {e}")
        return {}


def _extract_series(history: dict, sector_code: str,
                    days: int = LOOKBACK_DAYS) -> list[tuple[float, float]]:
    """
    score_history から指定業種コードの時系列を抽出する。

    Args:
        history:     load_score_history() の戻り値
        sector_code: 業種コード（例: "1016"）
        days:        遡る日数

    Returns:
        [(days_ago: float, score: float), ...] 日付昇順
        days_ago は今日からの経過日数（今日=0, 昨日=1, ...）
    """
    today   = datetime.now().date()
    cutoff  = today - timedelta(days=days)
    series  = []
    net_series = []   # モメンタム補正を差し引いたスコア（2026-09-17 以降に記録）

    for date_str, day_data in history.items():
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            continue

        # score_history.json の形式は {"code": {"score": float}} または
        # enrich後に {"code": float} の場合もあるため両対応
        entry = day_data.get(sector_code)
        if entry is None:
            continue

        momentum_adj = None
        if isinstance(entry, dict):
            score = entry.get("score")
            if score is None:
                score = entry.get("composite_score")
            momentum_adj = entry.get("momentum_adj")
        elif isinstance(entry, (int, float)):
            score = float(entry)
        else:
            continue

        if score is None:
            continue

        days_ago = (today - d).days
        series.append((float(days_ago), float(score)))
        if momentum_adj is not None:
            net_series.append((float(days_ago), float(score) - float(momentum_adj)))

    # 2026-09-17: 保存スコアには前日までのモメンタム補正（±10点）が含まれ、補正が
    # 翌日の傾きを押し上げる自己強化になっていた。補正額を記録した日が十分あれば、
    # 補正を除いた系列だけで傾きを測る（記録前と混ぜると段差が偽の傾きになるため混ぜない）。
    if len(net_series) >= MIN_DATA_POINTS:
        series = net_series

    # 古い順にソート（days_ago 降順 = 日付昇順）
    series.sort(key=lambda x: -x[0])
    return series


# ==============================================================
# BLOCK 2: モメンタム指標計算
# ==============================================================

def calc_sector_velocity(history: dict, sector_code: str,
                         days: int = LOOKBACK_DAYS) -> Optional[float]:
    """
    線形回帰スロープで業種スコアの変化速度（点/日）を計算する。

    正の値 → スコアが上昇中
    負の値 → スコアが下落中

    Args:
        history:     load_score_history() の戻り値
        sector_code: 業種コード
        days:        遡る日数

    Returns:
        slope (点/日)。データ不足の場合は None。
    """
    series = _extract_series(history, sector_code, days)

    if len(series) < MIN_DATA_POINTS:
        return None

    # 2026-09-17: 従来は「データ点の番号」を時間軸にした最小二乗法だった。
    #   - 休場・欠測日（例: 8/27→9/7 の11日空き）を1日として数え、傾きを過大に出す
    #   - 週1回の一時的な落ち込み（9/9・9/12 に電気機器 88→75点など）に引きずられる
    # 実日数を時間軸にした Theil–Sen 推定（2点間の傾きの中央値）に変更する。
    slopes = []
    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            dt = series[i][0] - series[j][0]   # days_ago の差 = 経過日数
            if dt > 0:
                slopes.append((series[j][1] - series[i][1]) / dt)
    if not slopes:
        return 0.0
    slopes.sort()
    mid = len(slopes) // 2
    slope = slopes[mid] if len(slopes) % 2 else (slopes[mid - 1] + slopes[mid]) / 2
    return round(slope, 4)


def calc_momentum_adjustment(
    slope:         Optional[float],
    current_score: float,
    history_mean:  Optional[float] = None,
) -> tuple[float, str]:
    """
    スロープからモメンタム調整点数とラベルを決定する。

    Fama-French のモメンタムファクターは「過去リターンが高い資産は
    今後も高リターン傾向」というアノマリーを業種スコアに適用。
    スロープに加えて「現在スコアが過去平均より上か下か」を
    強度調整の補正として使う。

    Args:
        slope:         calc_sector_velocity() の戻り値
        current_score: 今日の composite_score
        history_mean:  直近LOOKBACK_DAYS間のスコア平均（optional）

    Returns:
        (adjustment: float, label: str)
        adjustment: 加算する点数（正=追い風、負=逆風）
        label:      モメンタム状態の説明文字列
    """
    if slope is None:
        return 0.0, "データ不足"

    # スロープ週換算（可読性のため）
    slope_per_week = slope * 7

    # MOMENTUM_TIERS から該当する調整幅を選択
    adj   = 0.0
    label = "横ばい"
    for threshold, adjustment, tier_label in MOMENTUM_TIERS:
        if slope_per_week >= threshold:
            adj   = float(adjustment)
            label = tier_label
            break

    # 補正: 過去平均との乖離が大きい場合、反転リスクを考慮して強度を緩和
    # 「スコアが過去平均より20点以上高いのに上昇加速」は過熱気味 → +10→+5に緩和
    # 「スコアが過去平均より20点以上低いのに下落加速」は売られすぎ → -10→-5に緩和
    if history_mean is not None:
        deviation = current_score - history_mean
        if adj >= 10 and deviation > 20:
            adj   = 5.0
            label = f"{label}（過熱気味・緩和）"
        elif adj <= -10 and deviation < -20:
            adj   = -5.0
            label = f"{label}（売られ過ぎ・緩和）"

    return adj, label


def _calc_mean(series: list[tuple[float, float]]) -> Optional[float]:
    """時系列データのスコア平均を計算する。"""
    scores = [s for (_, s) in series]
    return sum(scores) / len(scores) if scores else None


# ==============================================================
# BLOCK 3: 全業種一括計算
# ==============================================================

def calc_all_sector_momentum(
    history:        dict,
    sector_results: dict,
    lookback_days:  int = LOOKBACK_DAYS,
) -> dict:
    """
    全業種のモメンタム指標を一括計算する。

    Args:
        history:        load_score_history() の戻り値
        sector_results: {sector_code: result_dict} の形式
                        result_dict に "composite_score" が含まれること
        lookback_days:  遡る日数

    Returns:
        {
          sector_code: {
            "velocity":      float,   # 点/日のスロープ
            "velocity_week": float,   # 点/週のスロープ（可読性）
            "adjustment":    float,   # composite_score への加算点数
            "label":         str,     # モメンタム状態ラベル
            "data_points":   int,     # 計算に使ったデータ点数
            "history_mean":  float,   # 期間内スコア平均
          }, ...
        }
    """
    result = {}

    for code, res in sector_results.items():
        if not isinstance(res, dict):
            continue

        series = _extract_series(history, code, lookback_days)
        n_pts  = len(series)

        if n_pts < MIN_DATA_POINTS:
            result[code] = {
                "velocity":      None,
                "velocity_week": None,
                "adjustment":    0.0,
                "label":         "データ不足",
                "data_points":   n_pts,
                "history_mean":  None,
            }
            continue

        current_score = float(res.get("composite_score", 0))
        history_mean  = _calc_mean(series)
        velocity      = calc_sector_velocity(history, code, lookback_days)
        adj, label    = calc_momentum_adjustment(velocity, current_score, history_mean)

        result[code] = {
            "velocity":      velocity,
            "velocity_week": round(velocity * 7, 2) if velocity is not None else None,
            "adjustment":    adj,
            "label":         label,
            "data_points":   n_pts,
            "history_mean":  round(history_mean, 1) if history_mean else None,
        }

    return result


# ==============================================================
# BLOCK 4: sector_results への適用
# ==============================================================

def apply_momentum_to_results(
    sector_results:  dict,
    momentum_data:   dict,
    sector_names:    Optional[dict] = None,
) -> dict:
    """
    sector_results の composite_score にモメンタム調整を加算する。

    元のスコアを "composite_score_base" として保存し、
    調整後を "composite_score" に上書きする（破壊的変更）。

    Args:
        sector_results: {code: result_dict}
        momentum_data:  calc_all_sector_momentum() の戻り値
        sector_names:   {code: name_ja} （ログ出力用・optional）

    Returns:
        sector_results（in-place 変更済み）
    """
    for code, res in sector_results.items():
        if not isinstance(res, dict):
            continue

        mom = momentum_data.get(code)
        if mom is None:
            continue

        adj = mom.get("adjustment", 0.0)
        if adj == 0.0:
            continue

        base_score = float(res.get("composite_score", 0))
        new_score  = max(SCORE_FLOOR, min(SCORE_CEILING, base_score + adj))

        res["composite_score_base"] = base_score
        res["composite_score"]      = round(new_score, 1)
        res["momentum_adjustment"]  = adj
        res["momentum_label"]       = mom.get("label", "")

        name = (sector_names or {}).get(code, code)
        sign = "+" if adj >= 0 else ""
        print(f"    📈 モメンタム調整: {name} {base_score:.0f}→{new_score:.0f}点 "
              f"({sign}{adj:.0f}, {mom.get('label', '')})")

    return sector_results


# ==============================================================
# BLOCK 5: D-4 モメンタムファクターランキング
# ==============================================================

def rank_by_momentum(
    sector_results: dict,
    momentum_data:  dict,
    sector_names:   Optional[dict] = None,
    top_n:          int = 10,
) -> dict:
    """
    モメンタムファクターによる業種ランキングを生成する（D-4）。

    Fama-French 12M モメンタムを業種スコアに適用:
      - 上位: 直近4週で加速上昇中の業種（買いシグナル）
      - 下位: 直近4週で加速下落中の業種（売り/回避シグナル）

    Args:
        sector_results: apply_momentum_to_results() 適用済みの結果
        momentum_data:  calc_all_sector_momentum() の戻り値
        sector_names:   {code: name_ja} （optional）
        top_n:          上位/下位それぞれ何業種を返すか

    Returns:
        {
          "top":    [ランキング辞書, ...],   # velocity上位（買い候補）
          "bottom": [ランキング辞書, ...],   # velocity下位（回避候補）
          "all":    [全業種ランキング辞書, ...],
          "generated_at": str,
        }

        各ランキング辞書の形式:
        {
          "rank":            int,
          "sector_code":     str,
          "sector_name_ja":  str,
          "composite_score": float,
          "base_score":      float,
          "adjustment":      float,
          "velocity_week":   float,   # 点/週
          "label":           str,
          "data_points":     int,
          "history_mean":    float,
        }
    """
    names = sector_names or {}
    rows  = []

    for code, mom in momentum_data.items():
        vel = mom.get("velocity_week")
        if vel is None:
            continue  # データ不足業種はランキング除外

        res        = sector_results.get(code, {})
        base_score = res.get("composite_score_base", res.get("composite_score", 0))
        adj_score  = res.get("composite_score", base_score)

        rows.append({
            "sector_code":     code,
            "sector_name_ja":  names.get(code, code),
            "composite_score": round(float(adj_score), 1),
            "base_score":      round(float(base_score), 1),
            "adjustment":      mom.get("adjustment", 0.0),
            "velocity_week":   vel,
            "label":           mom.get("label", ""),
            "data_points":     mom.get("data_points", 0),
            "history_mean":    mom.get("history_mean"),
        })

    # velocity_week 降順でソートし rank を付与
    rows.sort(key=lambda x: x["velocity_week"], reverse=True)
    for i, row in enumerate(rows, 1):
        row["rank"] = i

    return {
        "top":          rows[:top_n],
        "bottom":       rows[-top_n:][::-1],  # 下位を下落が大きい順に
        "all":          rows,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ==============================================================
# BLOCK 6: テキスト整形（アラート・レポート用）
# ==============================================================

def format_momentum_ranking_text(ranking: dict, top_n: int = 5) -> str:
    """
    rank_by_momentum() の戻り値をテキスト表形式に整形する。

    出力例:
      ━━ モメンタムランキング（直近4週） ━━
      🚀 上昇加速 TOP5
        1. 電気機器    +72点 [+10]  週+5.2pt  上昇加速中
        2. 精密機器    +68点 [+5]   週+2.1pt  上昇継続
        ...
      ⚠️  下落加速 BOTTOM5
        1. 不動産      +18点 [-10]  週-4.8pt  強い下落加速
        ...
    """
    top    = ranking.get("top",    [])[:top_n]
    bottom = ranking.get("bottom", [])[:top_n]
    lines  = [f"━━ モメンタムランキング（直近{LOOKBACK_DAYS//7}週） ━━", ""]

    lines.append(f"🚀 上昇加速 TOP{top_n}")
    if top:
        for r in top:
            adj_str  = f"[{'+' if r['adjustment']>=0 else ''}{r['adjustment']:.0f}]"
            vel_str  = f"週{'+' if r['velocity_week']>=0 else ''}{r['velocity_week']:.1f}pt"
            lines.append(
                f"  {r['rank']:2}. {r['sector_name_ja']:<8} "
                f"{r['composite_score']:+.0f}点 {adj_str:<6} {vel_str}  {r['label']}"
            )
    else:
        lines.append("  （データなし）")

    lines.append("")
    lines.append(f"⚠️  下落加速 BOTTOM{top_n}")
    if bottom:
        for i, r in enumerate(bottom, 1):
            adj_str  = f"[{'+' if r['adjustment']>=0 else ''}{r['adjustment']:.0f}]"
            vel_str  = f"週{'+' if r['velocity_week']>=0 else ''}{r['velocity_week']:.1f}pt"
            lines.append(
                f"  {i:2}. {r['sector_name_ja']:<8} "
                f"{r['composite_score']:+.0f}点 {adj_str:<6} {vel_str}  {r['label']}"
            )
    else:
        lines.append("  （データなし）")

    lines.append("")
    return "\n".join(lines)


# ==============================================================
# BLOCK 7: メインエントリーポイント（stock_analyzer_main.py から呼ぶ）
# ==============================================================

def run_momentum_factor(
    sector_results: dict,
    data_dir:       Path | str,
    sector_names:   Optional[dict] = None,
    top_n:          int = 5,
    lookback_days:  int = LOOKBACK_DAYS,
) -> dict:
    """
    モメンタムファクター計算の全処理を実行するエントリーポイント。

    stock_analyzer_main.py から以下のように呼び出す:
        from momentum_factor import run_momentum_factor
        mom_result = run_momentum_factor(
            sector_results = results["sectors"],
            data_dir       = cfg.DATA_DIR,
            sector_names   = {c: cfg.JAPAN_SECTORS[c]["name_ja"]
                              for c in cfg.JAPAN_SECTORS},
        )
        results["momentum"] = mom_result

    Args:
        sector_results: {code: result_dict} ← apply_momentum後に更新される
        data_dir:       score_history.json が置かれているディレクトリ
        sector_names:   {code: name_ja}
        top_n:          ランキング上位/下位の件数
        lookback_days:  遡る日数

    Returns:
        {
          "ranking":    rank_by_momentum() の戻り値,
          "momentum":   calc_all_sector_momentum() の戻り値,
          "text":       format_momentum_ranking_text() の出力,
          "applied":    モメンタム調整済みの業種コードリスト,
        }
    """
    import config as cfg
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    history_path = Path(data_dir) / "score_history.json"
    history      = load_score_history(history_path)

    if not history:
        print("  ⏩ momentum_factor: score_history.json なし → スキップ")
        return {"ranking": {}, "momentum": {}, "text": "", "applied": []}

    print(f"  📊 momentum_factor: {len(history)}日分の履歴を読み込み")

    # ① 全業種モメンタム計算
    momentum_data = calc_all_sector_momentum(history, sector_results, lookback_days)

    skipped = sum(1 for m in momentum_data.values() if m["label"] == "データ不足")
    if skipped:
        print(f"    ⚠️  データ不足業種: {skipped}業種（スキップ）")

    # ② composite_score に加算
    apply_momentum_to_results(sector_results, momentum_data, sector_names)
    applied = [c for c, m in momentum_data.items() if m.get("adjustment", 0) != 0]

    # ③ ランキング生成
    ranking = rank_by_momentum(sector_results, momentum_data, sector_names, top_n)

    # ④ テキスト整形
    text = format_momentum_ranking_text(ranking, top_n)
    print(text)

    print(f"  ✅ momentum_factor: {len(applied)}業種に調整適用")

    return {
        "ranking": ranking,
        "momentum": momentum_data,
        "text":    text,
        "applied": applied,
    }
