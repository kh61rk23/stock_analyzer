# ==============================================================
# regime_detector.py  v1.0  (RESEARCH-1 / ダリオ × ベイズ統合)
# HMMによる市場レジーム検知
# ==============================================================
# 【役割】
#   D-1（経済サイクル）の「点推定」の弱点を補う確率的レジーム推定。
#   4状態HMM（GaussianHMM）でマクロ指標から市場レジームを確率分布として出力し、
#   GAP-1（ベイズ更新）の confidence_prior を補強する。
#
# 【4状態定義】
#   State 0: リスクオン強  （象限B相当 / 成長↑×インフレ低）
#   State 1: リスクオン弱  （象限A相当 / 成長↑×インフレ高）
#   State 2: リスクオフ    （象限D相当 / 成長↓×インフレ低）
#   State 3: スタグフレーション（象限C相当 / 成長↓×インフレ高）
#
# 【観測変数（7次元）】
#   us10y / spread_2y10y / vix / usdjpy / oil_wti / sp500_mom / recession_score
#
# 【D-1との統合】
#   HMMのstate_probs[phase_state] が高い → confidence を補強
#   HMMが異なる状態を指す → confidence を抑制（不一致シグナル）
#
# 【重要な設計原則】
#   - ENABLE_REGIME_DETECTOR = False がデフォルト（30日データ蓄積後に有効化）
#   - hmmlearn が存在しない場合は graceful degradation（スキップ）
#   - モデルは state_matrix.json として永続化（transitions/means のみ保存）
#   - GitHub Actions では hmmlearn>=0.3.0 を requirements.txt に追記
#
# 【依存】hmmlearn / config.py / score_history.json / bond_commodity
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

N_STATES            = 4       # HMM の状態数
N_FEATURES          = 7       # 観測変数の次元数
MIN_TRAIN_DAYS      = 30      # 最低学習日数
STATE_FILE          = "regime_state.json"
MODEL_FILE          = "hmm_model_state.json"

# 状態ラベル（D-1 象限との対応）
STATE_LABELS = [
    "リスクオン強（象限B）",
    "リスクオン弱（象限A）",
    "リスクオフ（象限D）",
    "スタグフレーション（象限C）",
]

# D-1フェーズ → HMM状態の対応マップ
PHASE_TO_STATE = {"B": 0, "A": 1, "D": 2, "C": 3}
STATE_TO_PHASE = {0: "B", 1: "A", 2: "D", 3: "C"}

# confidence 補正の上限
CONF_ADJ_MAX =  0.20   # HMMが一致する場合の最大加算補正
CONF_ADJ_MIN = -0.15   # HMMが不一致の場合の最大減算補正

# 遷移アラートの閾値（前回と状態が変わった場合）
TRANSITION_ALERT_PROB_THRESHOLD = 0.60   # 新状態の確率がこれ以上で遷移確定


# ==============================================================
# BLOCK 1: 観測変数の正規化（StandardScaler 自前実装）
# ==============================================================

def _normalize_features(obs_matrix: list[list]) -> list[list]:
    """
    観測変数行列を各次元で正規化する（Z-score 標準化）。

    hmmlearn の GaussianHMM は生スケールの差が大きいと学習が不安定になるため、
    外部ライブラリに依存せずに正規化を実装する。

    Args:
        obs_matrix: [[f1,f2,...,f7], ...] 形式の観測値行列（日次）

    Returns:
        正規化後の行列（同形式）
    """
    if not obs_matrix:
        return []

    n_rows = len(obs_matrix)
    n_cols = len(obs_matrix[0])
    result = [[0.0] * n_cols for _ in range(n_rows)]

    for j in range(n_cols):
        col_vals = [obs_matrix[i][j] for i in range(n_rows)
                    if obs_matrix[i][j] is not None]
        if not col_vals:
            continue
        mu  = sum(col_vals) / len(col_vals)
        var = sum((v - mu) ** 2 for v in col_vals) / max(1, len(col_vals) - 1)
        std = math.sqrt(var) if var > 0 else 1.0

        for i in range(n_rows):
            v = obs_matrix[i][j]
            result[i][j] = (v - mu) / std if v is not None else 0.0

    return result


# ==============================================================
# BLOCK 2: 観測変数の構築
# ==============================================================

def _build_observation_matrix(
    score_history: dict,
    macro_snapshot: dict,
    bond_commodity: dict,
) -> list[list]:
    """
    score_history と macro_snapshot から HMM の観測変数行列を構築する。

    観測変数（7次元）:
      0: us10y          （米10年債利回り）
      1: spread_2y10y   （2y-10y スプレッド。逆イールドは負）
      2: vix            （恐怖指数）
      3: usdjpy         （為替）
      4: oil_wti        （原油価格）
      5: sp500_mom      （S&P500の前月比モメンタム%）
      6: recession_score（0〜100）

    score_history には macro_snapshot が含まれない場合も多いため、
    score_history の日付インデックスで並べ、値が取れない次元は
    直近の既知値で前方補完（forward fill）する。

    Returns:
        [[f0,f1,...,f6], ...] 形式（日付昇順）、取得できた日付数
    """
    dates = sorted(score_history.keys())
    if not dates:
        return []

    # 現在のスナップショットをベースにデフォルト値を設定
    curr_us10y    = macro_snapshot.get("us10y",   4.0)
    curr_usdjpy   = macro_snapshot.get("usdjpy",  150.0)
    curr_oil      = macro_snapshot.get("oil_wti",  80.0)
    curr_vix      = macro_snapshot.get("vix",      20.0)
    curr_sp500    = macro_snapshot.get("sp500",  5000.0)

    bc            = bond_commodity if isinstance(bond_commodity, dict) else {}
    ca            = bc.get("curve_analysis", {})
    spread_2y10y  = ca.get("spread_2y10y", -0.3)
    rec_score     = bc.get("recession_score", 30)

    # S&P500モメンタム（過去30日のリターン%として近似）
    # score_history から業種平均スコア変化を代理変数として使用
    prev_sp500    = curr_sp500

    matrix = []
    prev_vals = [curr_us10y, spread_2y10y, curr_vix,
                 curr_usdjpy, curr_oil, 0.0, float(rec_score)]

    for i, d in enumerate(dates):
        day_data = score_history.get(d, {})

        # score_history にマクロ値が保存されている場合は使用
        # （_add_macro_deltas を適用した場合）
        us10y = day_data.get("us10y") or prev_vals[0]
        sp    = day_data.get("spread_2y10y") or prev_vals[1]
        vix   = day_data.get("vix") or prev_vals[2]
        usdj  = day_data.get("usdjpy") or prev_vals[3]
        oil   = day_data.get("oil_wti") or prev_vals[4]
        rec   = day_data.get("recession_score") or prev_vals[6]

        # sp500 モメンタム: 全業種平均スコア変化率で近似
        if i > 0:
            prev_d   = dates[i - 1]
            prev_day = score_history.get(prev_d, {})
            scores_now  = [v.get("score", 50) for v in day_data.values()
                           if isinstance(v, dict) and "score" in v]
            scores_prev = [v.get("score", 50) for v in prev_day.values()
                           if isinstance(v, dict) and "score" in v]
            if scores_now and scores_prev:
                avg_now  = sum(scores_now) / len(scores_now)
                avg_prev = sum(scores_prev) / len(scores_prev)
                sp500_mom = (avg_now - avg_prev) / max(1, abs(avg_prev)) * 100
            else:
                sp500_mom = prev_vals[5]
        else:
            sp500_mom = 0.0

        vals = [
            float(us10y), float(sp), float(vix),
            float(usdj),  float(oil), float(sp500_mom), float(rec),
        ]
        matrix.append(vals)
        prev_vals = vals

    # 末尾に現在のスナップショット値を追加（当日分）
    today_vals = [
        curr_us10y, spread_2y10y, curr_vix,
        curr_usdjpy, curr_oil, 0.0, float(rec_score),
    ]
    matrix.append(today_vals)

    return matrix


# ==============================================================
# BLOCK 3: GaussianHMM のラッパー
# ==============================================================

def _try_import_hmmlearn():
    """hmmlearn をインポートする。失敗時は None を返す。"""
    try:
        from hmmlearn.hmm import GaussianHMM
        return GaussianHMM
    except ImportError:
        return None


def _fit_hmm(obs_normalized: list[list], n_states: int = N_STATES) -> Optional[object]:
    """
    正規化済み観測行列で GaussianHMM を学習する。

    Args:
        obs_normalized: _normalize_features() の出力
        n_states:       状態数（デフォルト4）

    Returns:
        学習済み GaussianHMM | None（学習失敗 or hmmlearn 未インストール）
    """
    GaussianHMM = _try_import_hmmlearn()
    if GaussianHMM is None:
        return None

    if len(obs_normalized) < MIN_TRAIN_DAYS:
        return None

    try:
        import numpy as np
        X = np.array(obs_normalized, dtype=float)

        model = GaussianHMM(
            n_components  = n_states,
            covariance_type = "diag",
            n_iter         = 100,
            random_state   = 42,
            tol            = 1e-4,
        )
        model.fit(X)
        return model

    except Exception as e:
        print(f"    ⚠️  HMM学習エラー: {str(e)[:60]}")
        return None


def _predict_state_probs(
    model,
    obs_normalized: list[list],
) -> tuple[int, list[float]]:
    """
    学習済みモデルで最終タイムステップの状態確率を推定する。

    Returns:
        (most_likely_state: int, state_probs: [float, ...])
    """
    try:
        import numpy as np
        X = np.array(obs_normalized, dtype=float)

        # Viterbi パスでは最終状態のみ取得
        log_prob, hidden_states = model.decode(X, algorithm="viterbi")
        current_state = int(hidden_states[-1])

        # 事後確率（Forward-Backward）で確率分布を取得
        _, posteriors = model.score_samples(X)
        last_posterior = posteriors[-1].tolist()

        return current_state, last_posterior

    except Exception as e:
        print(f"    ⚠️  HMM推定エラー: {str(e)[:60]}")
        return 0, [0.25, 0.25, 0.25, 0.25]   # フォールバック: 均等


# ==============================================================
# BLOCK 4: 遷移アラート検知
# ==============================================================

def _detect_regime_transition(
    current_state: int,
    state_probs:   list,
    prev_state:    Optional[int],
) -> bool:
    """
    前回状態から変化し、新状態の確率が閾値以上の場合を「遷移」とみなす。
    """
    if prev_state is None:
        return False
    if current_state == prev_state:
        return False
    # 新状態の確率が閾値以上ならば遷移確定
    return state_probs[current_state] >= TRANSITION_ALERT_PROB_THRESHOLD


# ==============================================================
# BLOCK 5: D-1との統合（confidence補正値を出力）
# ==============================================================

def calc_d1_confidence_adj(
    d1_phase:    str,
    state_probs: list,
) -> float:
    """
    D-1フェーズと HMM 状態確率を比較し、confidence の補正量を計算する。

    ロジック:
      D-1フェーズに対応する HMM 状態の確率が高い
        → 両者が一致 → confidence を上げる（+CONF_ADJ_MAX まで）
      HMM が異なる状態を指す
        → 不一致 → confidence を下げる（CONF_ADJ_MIN まで）

    Returns:
        confidence への補正量（負 = 下げる、正 = 上げる）
    """
    expected_state = PHASE_TO_STATE.get(d1_phase, 0)
    if expected_state >= len(state_probs):
        return 0.0

    d1_compatible_prob = state_probs[expected_state]

    # 最大確率状態
    max_state      = max(range(len(state_probs)), key=lambda i: state_probs[i])
    max_state_prob = state_probs[max_state]

    if max_state == expected_state:
        # 一致: 確率に比例して confidence を上げる（最大 +0.20）
        adj = CONF_ADJ_MAX * d1_compatible_prob
    else:
        # 不一致: D-1対応状態の確率が低いほど confidence を下げる
        adj = CONF_ADJ_MIN * (1.0 - d1_compatible_prob) * max_state_prob

    return round(max(CONF_ADJ_MIN, min(CONF_ADJ_MAX, adj)), 4)


# ==============================================================
# BLOCK 6: モデル永続化（state_matrix.json）
# ==============================================================

def _data_dir(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data"))


def _save_regime_state(cfg, state_dict: dict) -> None:
    p = _data_dir(cfg) / STATE_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  regime_state.json 保存失敗: {e}")


def _load_regime_state(cfg) -> dict:
    p = _data_dir(cfg) / STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_model_state(cfg, model) -> None:
    """HMM の遷移行列・発行行列を JSON で永続化する。"""
    try:
        import numpy as np
        p = _data_dir(cfg) / MODEL_FILE
        state_dict = {
            "transmat":  model.transmat_.tolist(),
            "means":     model.means_.tolist(),
            "covars":    model.covars_.tolist(),
            "startprob": model.startprob_.tolist(),
            "saved_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        p.write_text(json.dumps(state_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  hmm_model_state.json 保存失敗: {e}")


def _load_and_restore_model(cfg, n_features: int = N_FEATURES):
    """保存済みモデルパラメータから GaussianHMM を復元する。"""
    GaussianHMM = _try_import_hmmlearn()
    if GaussianHMM is None:
        return None

    p = _data_dir(cfg) / MODEL_FILE
    if not p.exists():
        return None

    try:
        import numpy as np
        saved = json.loads(p.read_text(encoding="utf-8"))
        model = GaussianHMM(n_components=N_STATES, covariance_type="diag")
        model.transmat_  = np.array(saved["transmat"])
        model.means_     = np.array(saved["means"])
        model.covars_    = np.array(saved["covars"])
        model.startprob_ = np.array(saved["startprob"])
        # fit_params で行列を固定（再フィット不要の場合）
        return model
    except Exception:
        return None


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_regime_detector(cfg, results: dict = None) -> dict:
    """
    HMM による市場レジーム検知を実行する。

    stock_analyzer_main.py から D-1 直後に呼び出す:
        if getattr(cfg, "ENABLE_REGIME_DETECTOR", False):
            _rd = _load_local_module("regime_detector", ...)
            rd_result = _rd.run_regime_detector(cfg, results)
            results["regime"] = rd_result
            # D-1のconfidenceを補正
            if results.get("economic_cycle"):
                adj = rd_result.get("d1_confidence_adj", 0)
                results["economic_cycle"]["confidence"] = round(
                    min(1.0, max(0.0,
                        results["economic_cycle"].get("confidence", 0.5) + adj
                    )), 3,
                )

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の results dict

    Returns:
        {
          "current_state":    int,        # 0〜3
          "state_probs":      [float, ...],# 各状態の確率
          "state_labels":     [str, ...],  # 状態ラベル
          "state_label":      str,         # 現在状態のラベル
          "transition_alert": bool,
          "prev_state":       int | None,
          "d1_confidence_adj":float,       # D-1 confidence への補正量
          "d1_phase":         str,         # D-1フェーズ
          "hmm_phase":        str,         # HMMが示すフェーズ
          "agreement":        bool,        # D-1とHMMが一致しているか
          "train_days":       int,
          "timestamp":        str,
        }
    """
    if not getattr(cfg, "ENABLE_REGIME_DETECTOR", False):
        print("  ⏩ レジーム検知: スキップ（ENABLE_REGIME_DETECTOR=False）")
        return {
            "skipped":          True,
            "current_state":    0,
            "state_probs":      [0.7, 0.1, 0.1, 0.1],
            "d1_confidence_adj": 0.0,
        }

    print("\n  🧬 HMMレジーム検知 開始...")
    results = results or {}

    # ── データ準備 ───────────────────────────────────────────
    score_history = {}
    sh_path = _data_dir(cfg) / "score_history.json"
    if sh_path.exists():
        try:
            score_history = json.loads(sh_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    train_days = len(score_history)
    min_days   = getattr(cfg, "HMM_MIN_TRAIN_DAYS", MIN_TRAIN_DAYS)
    if train_days < min_days:
        print(f"    ⚠️  学習データ不足（{train_days}日 < {min_days}日）→ スキップ")
        return {
            "skipped":          True,
            "train_days":       train_days,
            "current_state":    0,
            "state_probs":      [0.25, 0.25, 0.25, 0.25],
            "d1_confidence_adj": 0.0,
            "note":             f"データ蓄積中（{train_days}/{min_days}日）",
        }

    macro_snapshot  = results.get("macro_snapshot", {})
    bond_commodity  = results.get("bond_commodity", {})
    ec_result       = results.get("economic_cycle", {})
    d1_phase        = ec_result.get("phase", "B") if isinstance(ec_result, dict) else "B"

    # ── 観測行列の構築 ───────────────────────────────────────
    obs_matrix = _build_observation_matrix(
        score_history, macro_snapshot, bond_commodity
    )
    if len(obs_matrix) < min_days:
        return {
            "skipped":          True,
            "train_days":       len(obs_matrix),
            "d1_confidence_adj": 0.0,
        }

    obs_normalized = _normalize_features(obs_matrix)

    # ── モデル学習（週次）or 前回モデル再利用（日次）────────
    is_weekly = getattr(cfg, "HMM_REFIT_WEEKLY", True)
    today     = date.today()
    is_monday = (today.weekday() == 0)

    model = None
    if is_weekly and is_monday:
        print(f"    📊 HMM 再学習中（{len(obs_normalized)}日分）...")
        model = _fit_hmm(obs_normalized)
        if model:
            _save_model_state(cfg, model)
            print(f"    ✅ HMM 学習完了 → モデル保存")
    else:
        model = _load_and_restore_model(cfg)
        if model is None:
            print(f"    📊 保存済みモデルなし → 初回学習...")
            model = _fit_hmm(obs_normalized)
            if model:
                _save_model_state(cfg, model)

    if model is None:
        print("    ⚠️  HMM モデル取得失敗（hmmlearn未インストール or データ不足）")
        return {
            "skipped":          True,
            "d1_confidence_adj": 0.0,
            "note":             "hmmlearn 未インストール or 学習失敗",
        }

    # ── 状態確率の推定 ───────────────────────────────────────
    current_state, state_probs = _predict_state_probs(model, obs_normalized)

    # ── 前回状態の読み込みと遷移検知 ─────────────────────────
    prev_state_data = _load_regime_state(cfg)
    prev_state      = prev_state_data.get("current_state")
    transition_alert = _detect_regime_transition(current_state, state_probs, prev_state)

    # ── D-1との統合 ──────────────────────────────────────────
    d1_confidence_adj = calc_d1_confidence_adj(d1_phase, state_probs)
    hmm_phase  = STATE_TO_PHASE.get(current_state, "B")
    agreement  = (d1_phase == hmm_phase)

    # ── 永続保存 ─────────────────────────────────────────────
    state_dict = {
        "current_state":     current_state,
        "state_probs":       [round(p, 4) for p in state_probs],
        "state_labels":      STATE_LABELS,
        "state_label":       STATE_LABELS[current_state],
        "transition_alert":  transition_alert,
        "prev_state":        prev_state,
        "d1_phase":          d1_phase,
        "hmm_phase":         hmm_phase,
        "agreement":         agreement,
        "d1_confidence_adj": d1_confidence_adj,
        "train_days":        train_days,
        "timestamp":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_regime_state(cfg, state_dict)

    # ── ログ出力 ─────────────────────────────────────────────
    icon = "✅" if agreement else "⚠️"
    print(f"    {icon} HMMレジーム: State{current_state} {STATE_LABELS[current_state]}")
    print(f"       確率: {[f'{p:.2f}' for p in state_probs]}")
    print(f"       D-1象限{d1_phase} ↔ HMM象限{hmm_phase}: "
          f"{'一致' if agreement else '不一致'} / confidence補正{d1_confidence_adj:+.2f}")
    if transition_alert:
        print(f"    ⚡ レジーム遷移アラート: "
              f"State{prev_state} → State{current_state}")

    return state_dict
