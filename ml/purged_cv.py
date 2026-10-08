# ==============================================================
# ml/purged_cv.py — Purged Walk-Forward CV / rank IC（R5-1 / S7）
# ==============================================================
# 【役割】時系列リーク防止のための交差検証分割と評価指標（純粋ロジック）。
#   train と test の間に embargo（既定20営業日）を空け、目的変数の観測窓
#   （20日先リターン）が test 期間へ染み出す train 行を purge する。
# 【設計正本】要件定義書 §R5-1:
#   「Purged Walk-Forward CV（訓練と検証の間に20営業日のembargo・
#     リーク防止が最重要）。評価 = fold平均 rank IC」
# 【重要】ここは Colab の train_meta.ipynb と本番 pytest の共通中核。
#   numpy/pandas に依存しない純Pythonで書く（テスト容易・環境非依存）。
# ==============================================================

from __future__ import annotations


def _cfg_default(name: str, fallback: int) -> int:
    """config.py の値を既定として読む。

    本モジュールの学習は Colab（ml/train_meta.ipynb）から呼ばれるため、
    引数省略時は config.ML_META_* を正本とする（既定引数に直書きしない）。
    """
    try:
        import importlib.util
        from pathlib import Path as _P
        spec = importlib.util.spec_from_file_location(
            "config", str(_P(__file__).resolve().parent.parent / "config.py"))
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        return int(getattr(cfg, name, fallback))
    except Exception:
        return fallback


def purged_walk_forward_splits(sorted_dates: list, n_folds: int = None,
                               embargo_d: int = None, horizon_d: int = None):
    """
    ユニークな昇順日付リストを Walk-Forward 分割し、各 fold につき
    (train_date_set, test_date_set) を返す。

    引数を省略すると config.py BLOCK 51 の値を使う:
      n_folds   ← ML_META_CV_FOLDS（既定5）
      embargo_d ← ML_META_EMBARGO_D（既定20・リーク防止の最重要パラメータ）
      horizon_d ← ML_META_TARGET_HORIZON_D（既定20）

    - test は時系列で train より後（walk-forward）。
    - purge+embargo: test ブロックの開始位置から見て
      (horizon_d + embargo_d) 営業日以内に位置する train 行を除外する。
      → 20日先リターンの観測窓が test 期間に重なる直前の train を落とす。

    sorted_dates: ["YYYY-MM-DD", ...]（昇順・重複なしを想定）
    Returns: [(set(train_dates), set(test_dates)), ...]（長さ n_folds）
    """
    if n_folds is None:
        n_folds = _cfg_default("ML_META_CV_FOLDS", 5)
    if embargo_d is None:
        embargo_d = _cfg_default("ML_META_EMBARGO_D", 20)
    if horizon_d is None:
        horizon_d = _cfg_default("ML_META_TARGET_HORIZON_D", 20)

    dates = list(sorted_dates)
    n = len(dates)
    if n_folds < 1 or n < n_folds + 1:
        return []

    # 先頭ブロックを最初の train 種にし、残りを n_folds 個の test ブロックへ等分。
    fold_size = n // (n_folds + 1)
    if fold_size < 1:
        return []

    gap = horizon_d + embargo_d
    splits = []
    for k in range(1, n_folds + 1):
        test_start = k * fold_size
        test_end = (k + 1) * fold_size if k < n_folds else n
        test_idx = range(test_start, test_end)
        # train = test 開始より前、かつ gap 分の緩衝を空けた位置まで
        train_cutoff = test_start - gap
        train_idx = range(0, max(0, train_cutoff))
        train_dates = {dates[i] for i in train_idx}
        test_dates = {dates[i] for i in test_idx}
        if train_dates and test_dates:
            splits.append((train_dates, test_dates))
    return splits


def _rank(values: list) -> list:
    """平均順位（tie は平均順位）を返す。0始まり。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = sum(range(i, j + 1)) / (j - i + 1)
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def rank_ic(preds: list, actuals: list) -> float:
    """
    Spearman 順位相関（rank IC）。長さ不一致/データ不足/無分散は 0.0。
    """
    if len(preds) != len(actuals) or len(preds) < 2:
        return 0.0
    rp = _rank(list(preds))
    ra = _rank(list(actuals))
    n = len(rp)
    mp = sum(rp) / n
    ma = sum(ra) / n
    cov = sum((rp[i] - mp) * (ra[i] - ma) for i in range(n))
    vp = sum((x - mp) ** 2 for x in rp) ** 0.5
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    if vp == 0 or va == 0:
        return 0.0
    return cov / (vp * va)


def ic_t_stat(fold_ics: list) -> float:
    """
    fold 別 rank IC の系列 → t統計量 = mean / (std/sqrt(n))。
    昇格判定（|t| >= 2.0）に用いる。n<2 or std=0 は 0.0。
    """
    n = len(fold_ics)
    if n < 2:
        return 0.0
    mean = sum(fold_ics) / n
    var = sum((x - mean) ** 2 for x in fold_ics) / (n - 1)
    std = var ** 0.5
    if std == 0:
        return 0.0
    return mean / (std / (n ** 0.5))
