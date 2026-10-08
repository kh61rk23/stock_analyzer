# ==============================================================
# drift_detector.py  v1.0  (S4/F)
# ==============================================================
# 【役割】
#   業種別 composite_score の「急変ドリフト」と「緩慢な劣化」を検出し、
#   アラートを生成する。
#
# 【既存との役割分担】
#   score_explainer.apply_bayesian_to_sector_results()
#     → 前日比の単純乖離検出（_drifts: code/prev/today/drift/severity）
#       results["drift_events"] に格納済み
#
#   drift_detector（本モジュール）
#     → 週次の急変・連続低下を追加検出
#     → score_prior_manager と連携した prior_weight ベースの信頼度付き判定
#     → 永続ドリフト履歴（drift_history.json）の管理
#     → alert_notifier.extract_alerts() が参照する results["drift_report"] を生成
#
# 【2種類のドリフト検出】
#   (A) 急変ドリフト（Spike）
#       1週間（7日）でスコアが DRIFT_ALERT_THRESHOLD(20pt) 以上変化
#       $|score\_today - score\_7days\_ago| \geq DRIFT\_ALERT\_THRESHOLD$
#
#   (B) 緩慢な劣化（Decay）
#       DRIFT_DECAY_THRESHOLD(4) 週連続でスコアが低下
#       $\forall i \in [1,n]: score_i < score_{i-1}$, $n \geq DRIFT\_DECAY\_THRESHOLD$
#
# 【出力スキーマ（run_drift_detector の返り値）】
#   {
#     "spike_drifts":  [...],  # 急変ドリフトリスト
#     "decay_drifts":  [...],  # 緩慢な劣化リスト
#     "all_drifts":    [...],  # 全ドリフト（spike + decay）
#     "total":         int,
#     "timestamp":     str,
#   }
#
# 【drift スキーマ（1件）】
#   {
#     "code":       str,     # 業種コード
#     "name":       str,     # 業種名
#     "drift_type": "spike" | "decay",
#     "direction":  "up" | "down",
#     "severity":   "critical" | "high" | "medium",
#     "current":    float,   # 最新スコア
#     "baseline":   float,   # 比較基準スコア（7日前 or 4週前）
#     "delta":      float,   # current - baseline
#     "weeks":      int,     # 連続低下週数（decayのみ）
#     "prior_weight": float, # score_prior_manager の信頼度
#     "detected_at": str,
#   }
#
# 【キャッシュ】
#   INSIGHT_DIR/drift/drift_history.json  TTL: DRIFT_HISTORY_TTL_DAYS (180日)
#
# 【呼び出し方】
#   from drift_detector import run_drift_detector
#   drift_report = run_drift_detector(cfg, sector_results, score_history_all)
#   # → results["drift_report"] に格納
#
# 【依存】
#   config.py (BLOCK 35: DRIFT_ALERT_THRESHOLD / DRIFT_DECAY_THRESHOLD /
#              DRIFT_HISTORY_TTL_DAYS)
#   ※ DRIFT_ALERT_THRESHOLD は BLOCK 35 の 20.0 が唯一の定義（SSOT）。
#     他BLOCKに同名定義を置かない。
#   score_prior_manager.py (prior_weight による信頼度フィルタ)
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: 定数フォールバック
# ==============================================================


from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'DD_DEFAULT_SPIKE_THRESHOLD': '_DEFAULT_SPIKE_THRESHOLD',
    'B6_DRIFT_DETECTOR_DEFAULT_DECAY_WEEKS': '_DEFAULT_DECAY_WEEKS',
    'B6_DRIFT_DETECTOR_DEFAULT_HISTORY_TTL': '_DEFAULT_HISTORY_TTL',
}

_DEFAULT_SPIKE_THRESHOLD = 20.0   # 急変判定閾値（pt）
_DEFAULT_DECAY_WEEKS     = 4      # 緩慢な劣化判定（連続低下週数）
_DEFAULT_HISTORY_TTL     = 180    # drift_history.json TTL（日）


def _get_spike_threshold(cfg) -> float:
    """DRIFT_ALERT_THRESHOLD を取得。BLOCK 35（単一定義）→ デフォルト。"""
    return float(getattr(cfg, "DRIFT_ALERT_THRESHOLD", _DEFAULT_SPIKE_THRESHOLD))


def _get_decay_weeks(cfg) -> int:
    return int(getattr(cfg, "DRIFT_DECAY_THRESHOLD", _DEFAULT_DECAY_WEEKS))


def _get_history_ttl(cfg) -> int:
    return int(getattr(cfg, "DRIFT_HISTORY_TTL_DAYS", _DEFAULT_HISTORY_TTL))


# ==============================================================
# BLOCK 2: drift_history.json 管理
# ==============================================================

def _drift_history_path(cfg) -> Path:
    """drift_history.json のパスを返す。"""
    insight = Path(getattr(cfg, "INSIGHT_DIR", "stock_data/insight"))
    d = insight / "drift"
    d.mkdir(parents=True, exist_ok=True)
    return d / "drift_history.json"


def _load_drift_history(cfg) -> list[dict]:
    """
    drift_history.json を読み込む。TTL切れエントリを除去して返す。

    Returns:
        [{drift スキーマ}, ...] (detected_at 昇順)
    """
    path = _drift_history_path(cfg)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        ttl     = _get_history_ttl(cfg)
        cutoff  = datetime.now() - timedelta(days=ttl)
        cleaned = []
        for entry in raw:
            ts_str = entry.get("detected_at", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts >= cutoff:
                    cleaned.append(entry)
            except Exception:
                pass
        return cleaned
    except Exception as e:
        print(f"    ⚠️  [drift_detector] drift_history 読み込み失敗: "
              f"{str(e)[:60]}")
        return []


def _save_drift_history(cfg, history: list[dict]) -> None:
    """drift_history.json を保存する。失敗は警告のみ。"""
    path = _drift_history_path(cfg)
    try:
        # 上限 1000件（古い順に削除）
        if len(history) > 1000:
            history = sorted(
                history,
                key=lambda x: x.get("detected_at", ""),
            )[-1000:]
        path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [drift_detector] drift_history 保存失敗: "
              f"{str(e)[:60]}")


# ==============================================================
# BLOCK 3: score_history から週次スコア系列を抽出
# ==============================================================

def _extract_weekly_scores(
    sector_code:  str,
    history_all:  dict,
    n_weeks:      int = 8,
) -> list[dict]:
    """
    score_history から指定業種の週次スコア系列を抽出する。
    直近 n_weeks 週分（1週=7日）のデータを返す。

    Returns:
        [{"date": str, "score": float}, ...] 日付昇順
        週次なので最大 n_weeks 件
    """
    if not history_all:
        return []

    # 最新日から7日刻みで遡り、各時点「以前で最も近い日」を取る。
    # ISO 週ごとに「その週の最新日」を取ると、比較相手が「前の週の最終日」になり
    # 曜日で間隔が1〜13日ぶれ、週の間は毎日同じ基準日と比べ続けてしまう。
    dated = []
    for date_str in history_all.keys():
        try:
            dated.append((datetime.strptime(date_str, "%Y-%m-%d"), date_str))
        except Exception:
            continue
    if not dated:
        return []
    dated.sort(reverse=True)
    cutoff = datetime.now() - timedelta(weeks=n_weeks)
    latest_dt = dated[0][0]

    def _score_of(date_str):
        day_data = history_all.get(date_str, {})
        if not isinstance(day_data, dict):
            return None
        entry = day_data.get(sector_code)
        if entry is None:
            return None
        if isinstance(entry, dict):
            entry = entry.get("score")
        try:
            return float(entry)
        except (TypeError, ValueError):
            return None

    results = []
    used: set = set()
    for k in range(n_weeks + 1):
        target = latest_dt - timedelta(days=7 * k)
        if target < cutoff:
            break
        # 目標日以前で最も近い日（3日以内）。休場日・欠測日を吸収する
        for dt, date_str in dated:
            if dt <= target and (target - dt).days <= 3 and date_str not in used:
                score = _score_of(date_str)
                if score is not None:
                    results.append({"date": date_str, "score": score})
                    used.add(date_str)
                    break

    # 昇順に戻す
    results.sort(key=lambda x: x["date"])
    return results


# ==============================================================
# BLOCK 4: 急変ドリフト（Spike）検出
# ==============================================================

def _detect_spike(
    sector_code:  str,
    weekly_scores: list[dict],
    threshold:    float,
) -> Optional[dict]:
    """
    直近7日間のスコア急変を検出する。

    $delta = score\\_today - score\\_7days\\_ago$
    $|delta| \\geq threshold$ のとき急変と判定。

    Args:
        sector_code:   業種コード
        weekly_scores: 週次スコア系列（昇順）
        threshold:     急変判定閾値（pt）

    Returns:
        spike drift dict or None
    """
    if len(weekly_scores) < 2:
        return None

    # 最新と1週前を比較
    latest   = weekly_scores[-1]
    baseline = weekly_scores[-2]

    try:
        curr  = float(latest["score"])
        base  = float(baseline["score"])
        delta = curr - base
    except (TypeError, ValueError):
        return None

    if abs(delta) < threshold:
        return None

    direction = "up" if delta > 0 else "down"

    # severity: |delta| >= 2×threshold → critical / >= threshold → high
    if abs(delta) >= threshold * 2:
        severity = "critical"
    elif abs(delta) >= threshold * 1.5:
        severity = "high"
    else:
        severity = "medium"

    return {
        "code":        sector_code,
        "drift_type":  "spike",
        "direction":   direction,
        "severity":    severity,
        "current":     round(curr, 1),
        "baseline":    round(base, 1),
        "delta":       round(delta, 1),
        "weeks":       1,
        "date_current":  latest["date"],
        "date_baseline": baseline["date"],
        "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 5: 緩慢な劣化（Decay）検出
# ==============================================================

def _detect_decay(
    sector_code:   str,
    weekly_scores: list[dict],
    min_weeks:     int,
) -> Optional[dict]:
    """
    連続低下週数が min_weeks 以上の緩慢な劣化を検出する。

    条件: $\forall i \\in [1, n]: score_i < score_{i-1}$, $n \\geq min\\_weeks$

    Args:
        sector_code:   業種コード
        weekly_scores: 週次スコア系列（昇順）
        min_weeks:     最低連続低下週数

    Returns:
        decay drift dict or None
    """
    if len(weekly_scores) < min_weeks + 1:
        return None

    # 直近 min_weeks+1 件で連続低下をチェック
    recent = weekly_scores[-(min_weeks + 1):]
    scores = [float(r["score"]) for r in recent]

    # 連続低下カウント（末尾から遡る）
    consecutive = 0
    for i in range(len(scores) - 1, 0, -1):
        if scores[i] < scores[i - 1]:
            consecutive += 1
        else:
            break

    if consecutive < min_weeks:
        return None

    curr     = scores[-1]
    baseline = scores[-consecutive - 1]   # 連続低下の起点スコア
    delta    = curr - baseline            # 負の値

    # severity: 連続低下週数に応じて判定
    if consecutive >= min_weeks * 2:
        severity = "critical"
    elif consecutive >= int(min_weeks * 1.5):
        severity = "high"
    else:
        severity = "medium"

    return {
        "code":        sector_code,
        "drift_type":  "decay",
        "direction":   "down",
        "severity":    severity,
        "current":     round(curr, 1),
        "baseline":    round(baseline, 1),
        "delta":       round(delta, 1),
        "weeks":       consecutive,
        "date_current":  recent[-1]["date"],
        "date_baseline": recent[-consecutive - 1]["date"],
        "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 6: prior_weight フィルタ・業種名付与
# ==============================================================

def _enrich_drift(
    drift:          dict,
    sector_results: dict,
    priors:         dict,
) -> dict:
    """
    drift dict に業種名・prior_weight を付与する。

    Args:
        drift:          spike/decay drift dict
        sector_results: base_agent.build_result() の結果辞書
        priors:         score_prior_manager.get_all_priors() の結果

    Returns:
        enriched drift dict
    """
    code = drift["code"]

    # 業種名
    res  = sector_results.get(code, {})
    name = (res.get("sector_name_ja") or
            res.get("sector_name")    or
            res.get("name", code))
    drift["name"] = name

    # prior_weight（信頼度）
    prior = priors.get(code, {})
    drift["prior_weight"] = prior.get("prior_weight", 0.0)
    drift["prior_mean"]   = prior.get("prior_mean",   50.0)

    return drift


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_drift_detector(
    cfg,
    sector_results:   dict,
    score_history_all: Optional[dict] = None,
) -> dict:
    """
    全業種のドリフト検出を実行する。
    stock_analyzer_main.py から呼び出す。

    Args:
        cfg:               config モジュール
        sector_results:    {sector_code: build_result_dict, ...}
        score_history_all: score_history.json の中身（None の場合は自動読み込み）

    Returns:
        {
          "spike_drifts":  list,
          "decay_drifts":  list,
          "all_drifts":    list,
          "total":         int,
          "timestamp":     str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  🔍 [drift_detector] ドリフト検出 開始...")
    t0 = datetime.now()

    # ── score_history 読み込み ────────────────────────────────
    if score_history_all is None:
        try:
            sh_path = Path(getattr(cfg, "DATA_DIR", "stock_data")) \
                      / "score_history.json"
            if sh_path.exists():
                raw = json.loads(sh_path.read_text(encoding="utf-8"))
                score_history_all = raw if isinstance(raw, dict) else {}
            else:
                score_history_all = {}
        except Exception as e:
            print(f"    ⚠️  [drift_detector] score_history 読み込み失敗: "
                  f"{str(e)[:60]}")
            score_history_all = {}

    # ── score_prior_manager から prior_weight 一括取得 ─────────
    priors: dict = {}
    try:
        import sys as _sys, importlib as _imp, pathlib as _pl
        _sys.path.insert(0, str(_pl.Path(__file__).parent))
        _spm = _imp.import_module("score_prior_manager")
        codes   = list(sector_results.keys())
        priors  = _spm.ScorePriorManager(cfg).get_all_priors(
            codes, use_cache=True
        )
    except ModuleNotFoundError:
        print("    ⚠️  [drift_detector] score_prior_manager 未配置 → "
              "prior_weight=0 で継続")
    except Exception as e:
        print(f"    ⚠️  [drift_detector] score_prior_manager エラー: "
              f"{str(e)[:60]}")

    # ── 閾値設定 ──────────────────────────────────────────────
    spike_threshold = _get_spike_threshold(cfg)
    decay_weeks     = _get_decay_weeks(cfg)
    n_weeks_history = max(decay_weeks + 1, 8)   # 最低 decay_weeks+1 週分必要

    spike_drifts: list[dict] = []
    decay_drifts: list[dict] = []

    # ── 全業種をスキャン ──────────────────────────────────────
    for code, res in sector_results.items():
        if not isinstance(res, dict):
            continue

        try:
            weekly = _extract_weekly_scores(
                code, score_history_all, n_weeks=n_weeks_history
            )

            if not weekly:
                continue

            # (A) 急変ドリフト
            spike = _detect_spike(code, weekly, spike_threshold)
            if spike:
                spike = _enrich_drift(spike, sector_results, priors)
                spike_drifts.append(spike)

            # (B) 緩慢な劣化
            decay = _detect_decay(code, weekly, decay_weeks)
            if decay:
                decay = _enrich_drift(decay, sector_results, priors)
                decay_drifts.append(decay)

        except Exception as e:
            print(f"    ⚠️  [drift_detector] {code} スキャンエラー: "
                  f"{str(e)[:50]}")

    # ── severity 降順ソート ───────────────────────────────────
    _sev_order = {"critical": 0, "high": 1, "medium": 2}
    spike_drifts.sort(key=lambda x: (
        _sev_order.get(x["severity"], 9), -abs(x["delta"])
    ))
    decay_drifts.sort(key=lambda x: (
        _sev_order.get(x["severity"], 9), -x.get("weeks", 0)
    ))

    all_drifts = spike_drifts + decay_drifts

    # ── drift_history.json に永続記録 ────────────────────────
    try:
        history = _load_drift_history(cfg)
        today   = datetime.now().strftime("%Y-%m-%d")

        # 今日分は上書き（同日・同業種・同タイプの重複排除）
        existing_keys = {
            f"{d['code']}_{d['drift_type']}_{d.get('detected_at','')[:10]}"
            for d in history
        }
        for d in all_drifts:
            key = f"{d['code']}_{d['drift_type']}_{today}"
            if key not in existing_keys:
                history.append(dict(d))
                existing_keys.add(key)

        _save_drift_history(cfg, history)
    except Exception as e:
        print(f"    ⚠️  [drift_detector] drift_history 更新失敗: "
              f"{str(e)[:60]}")

    # ── コンソール出力 ────────────────────────────────────────
    elapsed = (datetime.now() - t0).seconds
    total   = len(all_drifts)

    if total:
        print(f"    ⚡ [drift_detector] "
              f"急変={len(spike_drifts)}件 / "
              f"劣化={len(decay_drifts)}件 ({elapsed}秒)")
        for d in all_drifts[:5]:
            icon = ("🚨" if d["severity"] == "critical" else
                    "⚠️"  if d["severity"] == "high"     else "📉")
            t    = "急変" if d["drift_type"] == "spike" else f"{d.get('weeks','')}週連続低下"
            print(f"       {icon} [{d['code']}] {d.get('name','')} "
                  f"{d['baseline']:.0f}→{d['current']:.0f}pt "
                  f"(Δ{d['delta']:+.0f} / {t} / {d['severity']})")
    else:
        print(f"    ✅ [drift_detector] ドリフト検出なし ({elapsed}秒)")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "spike_drifts": spike_drifts,
        "decay_drifts": decay_drifts,
        "all_drifts":   all_drifts,
        "total":        total,
        "timestamp":    now_str,
    }


# ==============================================================
# BLOCK 8: alert_notifier 向けアラート整形
# ==============================================================

def format_drift_alert(drift_report: dict, top_n: int = 5) -> str:
    """
    drift_report をアラート本文テキストに整形する。
    alert_notifier.extract_alerts() から呼び出す。

    Args:
        drift_report: run_drift_detector() の返り値
        top_n:        表示件数上限

    Returns:
        str: アラート本文
    """
    lines = []
    all_d = drift_report.get("all_drifts", [])

    for d in all_d[:top_n]:
        icon = ("🚨" if d["severity"] == "critical" else
                "⚠️"  if d["severity"] == "high"     else "📉")
        name = d.get("name", d["code"])

        if d["drift_type"] == "spike":
            dir_str = "急騰" if d["direction"] == "up" else "急落"
            lines.append(
                f"  {icon} [{d['code']}] {name}: "
                f"{d['baseline']:.0f}→{d['current']:.0f}pt "
                f"({dir_str} Δ{d['delta']:+.0f}pt)"
            )
        else:
            lines.append(
                f"  {icon} [{d['code']}] {name}: "
                f"{d['weeks']}週連続低下 "
                f"{d['baseline']:.0f}→{d['current']:.0f}pt "
                f"(累計Δ{d['delta']:+.0f}pt)"
            )

    return "\n".join(lines) if lines else "詳細なし"


# ==============================================================
# BLOCK 9: 単体テスト
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

    print("=== drift_detector 単体テスト ===\n")

    # ── モック score_history で検証 ──────────────────────────
    # 業種 1016: 急変（30pt → 52pt = +22pt 急騰）
    # 業種 1025: 緩慢な劣化（4週連続低下）
    # 業種 1001: 正常（変動なし）

    from datetime import timedelta
    base_date  = datetime.now()
    mock_hist  = {}

    # 1016: 1週前=30pt、今週=52pt → 急変+22pt
    week_dates_1016 = [
        (base_date - timedelta(weeks=1)).strftime("%Y-%m-%d"),
        base_date.strftime("%Y-%m-%d"),
    ]
    for i, d in enumerate(week_dates_1016):
        mock_hist.setdefault(d, {})
        mock_hist[d]["1016"] = {"score": [30.0, 52.0][i], "risk": "medium"}

    # 1025: 5週連続低下（70→67→64→61→58→55）
    for w in range(6):
        d = (base_date - timedelta(weeks=5 - w)).strftime("%Y-%m-%d")
        mock_hist.setdefault(d, {})
        mock_hist[d]["1025"] = {"score": 70.0 - w * 3, "risk": "medium"}

    # 1001: 安定（60pt 横ばい）
    for w in range(4):
        d = (base_date - timedelta(weeks=3 - w)).strftime("%Y-%m-%d")
        mock_hist.setdefault(d, {})
        mock_hist[d]["1001"] = {"score": 60.0, "risk": "low"}

    mock_sector_results = {
        "1016": {"sector_name_ja": "電気機器"},
        "1025": {"sector_name_ja": "情報・通信業"},
        "1001": {"sector_name_ja": "水産・農林業"},
    }

    report = run_drift_detector(cfg, mock_sector_results, mock_hist)

    print(f"\n  spike_drifts: {len(report['spike_drifts'])}件")
    for d in report["spike_drifts"]:
        print(f"    [{d['code']}] {d['direction']} "
              f"Δ{d['delta']:+.1f}pt severity={d['severity']}")

    print(f"\n  decay_drifts: {len(report['decay_drifts'])}件")
    for d in report["decay_drifts"]:
        print(f"    [{d['code']}] {d['weeks']}週連続低下 "
              f"Δ{d['delta']:+.1f}pt severity={d['severity']}")

    # アサーション
    assert any(d["code"] == "1016" for d in report["spike_drifts"]), \
        "1016 の急変が検出されなかった"
    assert any(d["code"] == "1025" for d in report["decay_drifts"]), \
        "1025 の緩慢劣化が検出されなかった"
    assert not any(d["code"] == "1001" for d in report["all_drifts"]), \
        "1001 が誤検知された"

    print("\n  format_drift_alert:")
    print(format_drift_alert(report))

    print("\n  ✅ 全テスト PASS")
