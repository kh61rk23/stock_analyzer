# ==============================================================
# ml_score_corrector.py  v1.0  (RESEARCH-4)
# XGBoostによる非線形ファクター交互作用補正レイヤー
# ==============================================================
# 【役割】
#   composite_score の線形加重式が捉えられない「ファクター交互作用」を
#   XGBoost で学習し、±5pt の補正量を算出する。
#   rule_optimizer.py と並走する形で月次で再学習する。
#
# 【重要な設計原則】
#   - ENABLE_ML_CORRECTION = False がデフォルト（6ヶ月データ蓄積後に有効化）
#   - ML補正量の上限は ±5pt（composite_scoreを支配させない）
#   - 時系列クロスバリデーション（未来データリーク防止）
#   - 学習は月次のみ・予測は日次（軽量）
#
# 【特徴量（X）】
#   基本スコア: sector_score, value_score, quality_score, technical_score
#   マクロ:     vix, usdjpy, us10y, spread_2y10y, phase_code, recession_score
#   個別:       acc_bonus（I-1）, conservatism_score（I-2）, activist_score（M-3）
#   交互作用項: usdjpy × us10y, vix × sector_score 等（手動追加）
#
# 【目的変数（y）】
#   performance_tracker.py の週次平均リターン（%）
#
# 【出力スキーマ】
#   {
#     "corrections": {ticker: correction_pt},   # ±5pt 制限済み
#     "feature_importance": {feature: importance},
#     "model_ic":     float,   # テストセットのIC
#     "train_weeks":  int,
#     "trained_at":   str,
#   }
#
# 【依存】xgboost / config.py / screener_history.json / performance_history.json
# 【呼出】from ml_score_corrector import run_ml_score_corrector,
#                                        load_ml_corrections
# ==============================================================

from __future__ import annotations

import json
import math
import pickle
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 定数
# ==============================================================

MODEL_FILE        = "ml_model.pkl"
IMPORTANCE_FILE   = "ml_feature_importance.json"
CORRECTIONS_FILE  = "ml_corrections_latest.json"


from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'MLSC_ML_CORRECTION_MAX': 'ML_CORRECTION_MAX',
    'MLSC_TRAIN_WEEKS_MIN': 'TRAIN_WEEKS_MIN',
    'B4_ML_SCORE_CORRECTOR_LOOKBACK_WEEKS': 'LOOKBACK_WEEKS',
    'B6_ML_SCORE_CORRECTOR_TEST_SPLIT_WEEKS': 'TEST_SPLIT_WEEKS',
    'B6_ML_SCORE_CORRECTOR_XGB_PARAMS': 'XGB_PARAMS',
}

ML_CORRECTION_MAX = 5.0    # 補正量の上限（pt）
TRAIN_WEEKS_MIN   = 26     # 最低学習週数（6ヶ月）
LOOKBACK_WEEKS    = 26     # 直近6ヶ月のみ学習（古い市場環境は不使用）
TEST_SPLIT_WEEKS  = 4      # テスト用直近4週

# XGBoost のハイパーパラメータ（過学習対策で浅め）
XGB_PARAMS = {
    "max_depth":        3,
    "n_estimators":     100,
    "learning_rate":    0.05,
    "subsample":        0.8,
    "colsample_bytree": 0.8,
    "reg_lambda":       1.0,   # L2正規化
    "reg_alpha":        0.5,   # L1正規化
    "random_state":     42,
    "n_jobs":           -1,
}

# マクロフェーズ → 数値マッピング
PHASE_NUMERIC = {"B": 1.0, "A": 0.5, "D": -0.5, "C": -1.0}

# 特徴量名リスト（インデックス順）
FEATURE_NAMES = [
    # 基本スコア（4次元）
    "sector_score", "value_score", "quality_score", "technical_score",
    # マクロ（7次元）
    "vix", "usdjpy", "us10y", "spread_2y10y",
    "phase_numeric", "recession_score", "debt_cycle_score",
    # 個別（3次元）
    "acc_bonus", "conservatism_score", "activist_score",
    # 交互作用項（4次元）
    "usdjpy_x_us10y",      # 円安 × 金利（輸出株の影響）
    "vix_x_sector_score",  # リスクオフ × 業種スコア
    "acc_x_conserv",       # EPS加速 × 経営者保守度（I-1×I-2）
    "phase_x_sector",      # サイクル × 業種スコア
]


# ==============================================================
# BLOCK 1: 学習データの構築
# ==============================================================

def _data_dir(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data"))


def _load_screener_history(cfg) -> dict:
    p = _data_dir(cfg) / "screener_history.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_performance_history(cfg) -> list:
    p = _data_dir(cfg) / "performance_history.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, list) else []
        except Exception:
            pass
    return []


def _load_score_history(cfg) -> dict:
    p = _data_dir(cfg) / "score_history.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}


def _extract_features(candidate: dict, macro_ctx: dict) -> Optional[list]:
    """
    候補銘柄1件 + マクロコンテキストから特徴量ベクトルを生成する。

    Args:
        candidate : screener_history の1エントリ
        macro_ctx : {"vix", "usdjpy", "us10y", "spread_2y10y",
                     "phase", "recession_score", "debt_cycle_score"}

    Returns:
        FEATURE_NAMES と対応する長さ14のfloatリスト、またはNone
    """
    def _f(key, default=0.0):
        v = candidate.get(key)
        return float(v) if v is not None else default

    def _mf(key, default=0.0):
        v = macro_ctx.get(key)
        return float(v) if v is not None else default

    # 基本スコア
    sector  = _f("sector_score", 50.0)
    value   = _f("value_score", 50.0)
    quality = _f("quality_score", 50.0)
    tech    = _f("technical_score", 50.0)

    # マクロ
    vix           = _mf("vix", 20.0)
    usdjpy        = _mf("usdjpy", 150.0)
    us10y         = _mf("us10y", 4.0)
    spread_2y10y  = _mf("spread_2y10y", -0.3)
    phase         = PHASE_NUMERIC.get(str(macro_ctx.get("phase", "B")), 0.5)
    recession_sc  = _mf("recession_score", 30.0)
    debt_cycle    = _mf("debt_cycle_score", 50.0)

    # 個別ファクター
    acc_bonus    = _f("acc_bonus", 0.0)
    conservatism = _f("conservatism_score", 50.0)
    activist     = _f("activist_score", 0.0)

    # 交互作用項
    usdjpy_x_us10y    = (usdjpy - 140.0) * (us10y - 3.5)   # 円安×金利の交互作用
    vix_x_sector      = vix * sector / 100.0
    acc_x_conserv     = acc_bonus * (conservatism - 50.0) / 50.0
    phase_x_sector    = phase * sector / 100.0

    features = [
        sector, value, quality, tech,
        vix, usdjpy, us10y, spread_2y10y,
        phase, recession_sc, debt_cycle,
        acc_bonus, conservatism, activist,
        usdjpy_x_us10y, vix_x_sector, acc_x_conserv, phase_x_sector,
    ]

    # NaN/inf チェック
    for v in features:
        if v is None or math.isnan(v) or math.isinf(v):
            return None

    return features


def build_training_data(cfg, results: dict = None) -> tuple[list, list]:
    """
    screener_history + performance_history + score_history を統合して
    (X_train, y_train) を生成する。

    Returns:
        (X_all: [[feature,...], ...], y_all: [return_pct, ...])
    """
    screener_history = _load_screener_history(cfg)
    perf_history     = _load_performance_history(cfg)
    score_history    = _load_score_history(cfg)

    if not screener_history or not perf_history:
        return [], []

    # 週次リターン辞書: {week_key: avg_return_pct}
    weekly_returns = {r["week"]: r["avg_return"]
                      for r in perf_history
                      if r.get("week") and r.get("avg_return") is not None}

    # 直近 LOOKBACK_WEEKS 週のデータのみ使用
    cutoff = (date.today() - timedelta(weeks=LOOKBACK_WEEKS)).strftime("%Y-%m-%d")

    X_all, y_all = [], []

    # score_history からマクロ情報（フェーズ等）を取得するマッピング
    # score_history: {date_str: {sector_code: {score, risk}}}
    # macro情報は score_history に含まれない → results から補完
    results = results or {}
    macro_snap = results.get("macro_snapshot", {})
    ec_result  = results.get("economic_cycle", {})
    bc_result  = results.get("bond_commodity", {})

    macro_ctx_global = {
        "vix":            macro_snap.get("vix", 20.0),
        "usdjpy":         macro_snap.get("usdjpy", 150.0),
        "us10y":          macro_snap.get("us10y", 4.0),
        "spread_2y10y":   (bc_result.get("curve_analysis", {}).get("spread_2y10y", -0.3)
                           if isinstance(bc_result, dict) else -0.3),
        "phase":          ec_result.get("phase", "B") if isinstance(ec_result, dict) else "B",
        "recession_score": (bc_result.get("recession_score", 30)
                            if isinstance(bc_result, dict) else 30),
        "debt_cycle_score": results.get("debt_cycle", {}).get("debt_cycle_score", 50)
                            if isinstance(results.get("debt_cycle"), dict) else 50,
    }

    # screener_history の各日のデータと performance_history を紐付け
    for date_str, candidates in screener_history.items():
        if date_str < cutoff:
            continue
        if not candidates:
            continue

        # この週に対応するリターンを検索
        try:
            d       = datetime.strptime(date_str, "%Y-%m-%d").date()
            iso_cal = d.isocalendar()
            week_key = f"{iso_cal[0]}-W{iso_cal[1]:02d}"
        except Exception:
            continue

        # 翌週のリターンを取得（予測対象）
        next_week = (d + timedelta(weeks=1)).isocalendar()
        next_week_key = f"{next_week[0]}-W{next_week[1]:02d}"
        target_return = weekly_returns.get(next_week_key)

        if target_return is None:
            continue   # 対応するリターンがない週はスキップ

        for c in candidates:
            features = _extract_features(c, macro_ctx_global)
            if features is None:
                continue
            X_all.append(features)
            y_all.append(float(target_return))

    return X_all, y_all


# ==============================================================
# BLOCK 2: 特徴量エンジニアリング（交互作用項）
# ==============================================================

# 交互作用項の計算は _extract_features() 内に統合済み


# ==============================================================
# BLOCK 3: XGBoostモデルの学習
# ==============================================================

def _try_import_xgb():
    """xgboost をインポートする。失敗時は None。"""
    try:
        import xgboost as xgb
        return xgb
    except ImportError:
        try:
            import lightgbm as lgb
            return lgb
        except ImportError:
            return None


def train_model(X_train: list, y_train: list, use_lightgbm: bool = False):
    """
    XGBoost（またはLightGBM）モデルを学習する。
    時系列クロスバリデーションで過学習を評価する。

    Returns:
        (model, ic_test: float, feature_importances: list)
    """
    xgb = _try_import_xgb()
    if xgb is None:
        return None, None, None

    try:
        import numpy as np
        X = np.array(X_train, dtype=float)
        y = np.array(y_train, dtype=float)

        n = len(X)
        if n < TRAIN_WEEKS_MIN:
            return None, None, None

        # 時系列スプリット（最後の TEST_SPLIT_WEEKS 分をテスト）
        split = max(int(n * 0.8), n - TEST_SPLIT_WEEKS * 5)
        X_tr, X_te = X[:split], X[split:]
        y_tr, y_te = y[:split], y[split:]

        # XGBoost か LightGBM を自動判定
        module_name = str(type(xgb)).lower()
        is_lightgbm = "lightgbm" in str(xgb.__name__) if hasattr(xgb, "__name__") else False

        if is_lightgbm:
            model = xgb.LGBMRegressor(
                num_leaves    = 15,
                n_estimators  = XGB_PARAMS["n_estimators"],
                learning_rate = XGB_PARAMS["learning_rate"],
                subsample     = XGB_PARAMS["subsample"],
                reg_lambda    = XGB_PARAMS["reg_lambda"],
                reg_alpha     = XGB_PARAMS["reg_alpha"],
                random_state  = XGB_PARAMS["random_state"],
                n_jobs        = XGB_PARAMS["n_jobs"],
                verbose       = -1,
            )
        else:
            model = xgb.XGBRegressor(
                **XGB_PARAMS,
                verbosity=0,
            )

        model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)],
                  verbose=False)

        # テストセットでの IC 計算
        y_pred = model.predict(X_te)
        ic_test = _pearson_ic(y_pred.tolist(), y_te.tolist())

        # 特徴量重要度
        if hasattr(model, "feature_importances_"):
            importances = model.feature_importances_.tolist()
        else:
            importances = [0.0] * len(FEATURE_NAMES)

        print(f"    📊 ML学習完了: 学習{split}サンプル / テスト{len(X_te)}サンプル")
        print(f"       テストIC: {ic_test:.4f}" if ic_test else "       テストIC: N/A")

        return model, ic_test, importances

    except Exception as e:
        print(f"    ⚠️  ML学習エラー: {str(e)[:80]}")
        return None, None, None


def _pearson_ic(pred: list, actual: list) -> Optional[float]:
    """予測値と実績値の Pearson 相関（IC）を計算する。"""
    n = len(pred)
    if n < 3 or len(actual) != n:
        return None
    mp = sum(pred) / n
    ma = sum(actual) / n
    cov = sum((p - mp) * (a - ma) for p, a in zip(pred, actual)) / n
    sp  = math.sqrt(sum((p - mp) ** 2 for p in pred) / n)
    sa  = math.sqrt(sum((a - ma) ** 2 for a in actual) / n)
    if sp < 1e-9 or sa < 1e-9:
        return None
    return round(cov / (sp * sa), 4)


# ==============================================================
# BLOCK 4: モデル評価（IC・シャープレシオ）
# ==============================================================

# IC計算は train_model() 内に統合済み


# ==============================================================
# BLOCK 5: 予測実行（当日候補銘柄への補正量算出）
# ==============================================================

def predict_corrections(
    model,
    candidates:   list,
    results:      dict = None,
    correction_max: float = ML_CORRECTION_MAX,
) -> dict:
    """
    学習済みモデルで screener 候補銘柄の補正量を予測する。

    Args:
        model:          train_model() で学習したモデル
        candidates:     screener の candidates リスト
        results:        マクロ情報取得用
        correction_max: 補正量の上限（デフォルト ±5pt）

    Returns:
        {ticker: correction_pt}
    """
    if model is None:
        return {}

    try:
        import numpy as np
        results   = results or {}
        macro_snap = results.get("macro_snapshot", {})
        ec_result  = results.get("economic_cycle", {})
        bc_result  = results.get("bond_commodity", {})

        macro_ctx = {
            "vix":            macro_snap.get("vix", 20.0),
            "usdjpy":         macro_snap.get("usdjpy", 150.0),
            "us10y":          macro_snap.get("us10y", 4.0),
            "spread_2y10y":   (bc_result.get("curve_analysis", {}).get("spread_2y10y", -0.3)
                               if isinstance(bc_result, dict) else -0.3),
            "phase":          ec_result.get("phase", "B") if isinstance(ec_result, dict) else "B",
            "recession_score": (bc_result.get("recession_score", 30)
                                if isinstance(bc_result, dict) else 30),
            "debt_cycle_score": results.get("debt_cycle", {}).get("debt_cycle_score", 50)
                                if isinstance(results.get("debt_cycle"), dict) else 50,
        }

        corrections = {}
        for c in candidates:
            ticker   = c.get("ticker", "")
            if not ticker:
                continue
            features = _extract_features(c, macro_ctx)
            if features is None:
                continue

            X = np.array([features], dtype=float)
            pred_return = float(model.predict(X)[0])

            # 予測リターンを補正量に変換
            # 予測リターン +2% → +2pt / -2% → -2pt（1%:1ptで変換）
            # 上限クランプ
            correction = max(-correction_max, min(correction_max, pred_return))
            corrections[ticker] = round(correction, 2)

        return corrections

    except Exception as e:
        print(f"    ⚠️  ML補正予測エラー: {str(e)[:80]}")
        return {}


# ==============================================================
# BLOCK 6: モデル永続化
# ==============================================================

def save_model(cfg, model, importances: list) -> None:
    """モデルと特徴量重要度を保存する。"""
    p = _data_dir(cfg) / MODEL_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            pickle.dump(model, f)
    except Exception as e:
        print(f"    ⚠️  ML モデル保存失敗: {e}")

    # 特徴量重要度JSON
    imp_p = _data_dir(cfg) / IMPORTANCE_FILE
    try:
        imp_data = {
            "feature_importance": dict(zip(FEATURE_NAMES, importances or [])),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        imp_p.write_text(json.dumps(imp_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  特徴量重要度保存失敗: {e}")


def load_model(cfg):
    """保存済みモデルをロードする。"""
    p = _data_dir(cfg) / MODEL_FILE
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def load_ml_corrections(cfg) -> dict:
    """
    ml_corrections_latest.json から前回の補正量を読み込む。
    stock_screener.py の _calc_scores() から参照する。
    """
    p = _data_dir(cfg) / CORRECTIONS_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("corrections", {})
    except Exception:
        return {}


def save_corrections(cfg, corrections: dict, ic_test: Optional[float]) -> None:
    p = _data_dir(cfg) / CORRECTIONS_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        out = {
            "corrections":  corrections,
            "model_ic":     ic_test,
            "updated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  ML補正保存失敗: {e}")


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_ml_score_corrector(cfg, results: dict = None) -> dict:
    """
    XGBoost補正レイヤーを実行する。

    処理モード:
      月末 or force_train=True: 学習 + 予測
      それ以外:                 既存モデルで予測のみ（軽量）

    stock_analyzer_main.py から rule_optimizer の直後に呼び出す:
        _ml = _load_local_module("ml_score_corrector", ...)
        ml_result = _ml.run_ml_score_corrector(cfg, results)
        results["ml_correction"] = ml_result
        # screener candidates に補正を適用
        if ml_result.get("corrections") and getattr(cfg, "ENABLE_ML_CORRECTION", False):
            corrections = ml_result["corrections"]
            for c in results["screener"]["candidates"]:
                tk = c.get("ticker", "")
                if tk in corrections:
                    adj = corrections[tk]
                    c["composite_score"] = round(
                        min(100.0, max(0.0,
                            (c.get("composite_score") or 50.0) + adj
                        )), 1,
                    )
                    c["ml_correction"] = adj

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の results dict

    Returns:
        {
          "corrections":          {ticker: correction_pt},
          "feature_importance":   {feature: importance},
          "model_ic":             float | None,
          "train_weeks":          int,
          "trained":              bool,   # 今回学習したか
          "skipped":              bool,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    if not getattr(cfg, "ENABLE_ML_CORRECTION", False):
        print("  ⏩ ML補正: スキップ（ENABLE_ML_CORRECTION=False）")
        return {"skipped": True, "corrections": {}}

    print("\n  🤖 MLスコア補正 実行中...")
    results = results or {}

    # 月末判定
    today = date.today()
    is_month_end = (
        today == (today.replace(day=1) + timedelta(days=32)).replace(day=1)
                 - timedelta(days=1)
    )
    force_train = getattr(cfg, "FORCE_ML_TRAIN", False)
    do_train = is_month_end or force_train

    model    = None
    ic_test  = None
    importances = []
    trained  = False

    # ── 学習フェーズ（月末 or 強制） ─────────────────────────
    if do_train:
        print("    📊 学習データ構築中...")
        X, y = build_training_data(cfg, results)
        print(f"    学習サンプル数: {len(X)}")

        if len(X) < TRAIN_WEEKS_MIN:
            print(f"    ⚠️  学習データ不足（{len(X)}サンプル < {TRAIN_WEEKS_MIN}）")
            print(f"    → あと{TRAIN_WEEKS_MIN - len(X)}週間のデータ蓄積が必要")
        else:
            model, ic_test, importances = train_model(X, y)
            if model:
                save_model(cfg, model, importances)
                trained = True
                print(f"    ✅ ML学習完了: IC={ic_test:.4f}" if ic_test else
                      "    ✅ ML学習完了")
    else:
        # 既存モデルをロード
        model = load_model(cfg)
        if model:
            print("    📂 保存済みMLモデルをロード")
        else:
            print("    ⚠️  保存済みモデルなし（月末に自動学習されます）")
            return {
                "skipped":       False,
                "trained":       False,
                "corrections":   {},
                "model_ic":      None,
                "note":          "月末まで待機中",
            }

    # ── 予測フェーズ（候補銘柄への補正量算出） ────────────────
    if model is None:
        return {
            "skipped":     False,
            "trained":     trained,
            "corrections": {},
            "model_ic":    ic_test,
        }

    screener_result = results.get("screener", {})
    candidates = [
        c for c in screener_result.get("candidates", [])
        if c.get("pass_filters")
    ]

    correction_max = getattr(cfg, "ML_CORRECTION_MAX_PT", ML_CORRECTION_MAX)
    corrections = predict_corrections(model, candidates, results, correction_max)

    # 保存
    save_corrections(cfg, corrections, ic_test)

    # 特徴量重要度をdict形式に変換
    imp_dict = dict(zip(FEATURE_NAMES, importances)) if importances else {}
    if not imp_dict:
        # 保存済み重要度を読み込む
        imp_p = _data_dir(cfg) / IMPORTANCE_FILE
        if imp_p.exists():
            try:
                saved = json.loads(imp_p.read_text(encoding="utf-8"))
                imp_dict = saved.get("feature_importance", {})
            except Exception:
                pass

    pos_corrections = sum(1 for v in corrections.values() if v > 0.1)
    neg_corrections = sum(1 for v in corrections.values() if v < -0.1)
    print(
        f"    ✅ ML補正完了: {len(corrections)}銘柄 "
        f"| +補正{pos_corrections} / -補正{neg_corrections}"
    )

    # 上位補正銘柄を表示
    top_cor = sorted(corrections.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
    for tk, adj in top_cor:
        print(f"       {tk}: {adj:+.2f}pt")

    return {
        "corrections":        corrections,
        "feature_importance": imp_dict,
        "model_ic":           ic_test,
        "train_weeks":        len(candidates),
        "trained":            trained,
        "skipped":            False,
        "timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
