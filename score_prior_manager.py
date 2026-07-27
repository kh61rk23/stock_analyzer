# ==============================================================
# score_prior_manager.py  v1.0  (Wave 4 - S4/F)
# ==============================================================
# 【役割】
#   業種別 composite_score の事前確率（過去スコア分布）を管理し、
#   鮮度係数（freshness）による重み付きスコアを計算する。
#
# 【用語定義】
#   score_prior  : 過去スコア履歴から推定した「スコアの期待値」
#   freshness    : 情報源の鮮度。経過日数に応じて指数減衰する係数
#   prior_weight : freshness と観測数から算出した信頼度重み
#
# 【鮮度係数の数式】
#   $freshness = exp(-\lambda \times days\_elapsed)$
#   $\lambda = \ln(2) / half\_life$
#   half_life = SCORE_PRIOR_FRESHNESS_HALF_LIFE (デフォルト14日)
#   例: 7日経過 → freshness≈0.707 / 14日 → 0.500 / 28日 → 0.250
#
# 【prior_weight の数式】
#   $prior\_weight = freshness \times \min(1.0, n\_obs / MIN\_OBS)$
#   n_obs: 有効観測数（TTL内のスコア日数）
#   MIN_OBS: 信頼度1.0になる最低観測数（デフォルト7日）
#
# 【出力スキーマ（get_prior の返り値）】
#   {
#     "sector_code":   str,
#     "prior_mean":    float,   # 有効期間内のスコア加重平均
#     "prior_std":     float,   # 有効期間内のスコア標準偏差
#     "prior_weight":  float,   # [0.0, 1.0] 信頼度重み
#     "freshness":     float,   # 最新スコアの鮮度係数
#     "n_obs":         int,     # 有効観測数
#     "last_score":    float,   # 最新スコア
#     "last_date":     str,     # 最新スコア日付
#   }
#
# 【キャッシュパス】
#   CACHE_DIR/score_prior/ ディレクトリ
#   score_prior_{sector_code}.json  TTL: SCORE_PRIOR_TTL_DAYS (90日)
#
# 【データソース】
#   DATA_DIR/score_history.json
#   形式: {"YYYY-MM-DD": {"sector_code": {"score": float, "risk": str}}}
#
# 【呼び出し方】
#   from score_prior_manager import ScorePriorManager
#   spm = ScorePriorManager(cfg)
#   prior = spm.get_prior("1016")
#   # -> {"prior_mean": 68.2, "prior_weight": 0.83, ...}
#
#   # stock_analyzer_main.py Phase 0 での一括取得
#   all_priors = spm.get_all_priors(sector_codes=["1016", "1025", ...])
#
# 【依存】
#   config.py (BLOCK 35: SCORE_PRIOR_TTL_DAYS / SCORE_PRIOR_FRESHNESS_HALF_LIFE)
#   stock_analyzer_main.py が生成する DATA_DIR/score_history.json
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
# config.py BLOCK 35 の定数を参照。未定義時はここのデフォルト値を使用。

_DEFAULT_TTL_DAYS        = 90
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B6_SCORE_PRIOR_MANAGER_DEFAULT_HALF_LIFE': '_DEFAULT_HALF_LIFE',
    'B6_SCORE_PRIOR_MANAGER_DEFAULT_MIN_OBS': '_DEFAULT_MIN_OBS',
}

_DEFAULT_HALF_LIFE       = 14    # 2週間で鮮度が半減
_DEFAULT_MIN_OBS         = 7     # 7観測日で prior_weight=1.0
_DEFAULT_SCORE_HISTORY   = "score_history.json"


# ==============================================================
# BLOCK 2: キャッシュ管理
# ==============================================================

def _prior_cache_dir(cfg) -> Path:
    """score_prior キャッシュディレクトリを返す（なければ作成）。"""
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    d = base / "score_prior"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_prior_cache(sector_code: str, cfg) -> Optional[dict]:
    """
    TTL 内のキャッシュがあれば返す。なければ None。
    TTL: SCORE_PRIOR_TTL_DAYS（デフォルト90日）
    """
    p = _prior_cache_dir(cfg) / f"score_prior_{sector_code}.json"
    if not p.exists():
        return None
    try:
        data    = json.loads(p.read_text(encoding="utf-8"))
        ttl     = int(getattr(cfg, "SCORE_PRIOR_TTL_DAYS", _DEFAULT_TTL_DAYS))
        cached  = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached <= timedelta(days=ttl):
            return data
    except Exception:
        pass
    return None


def _save_prior_cache(sector_code: str, cfg, payload: dict) -> None:
    """キャッシュ保存。失敗は警告のみ。"""
    p = _prior_cache_dir(cfg) / f"score_prior_{sector_code}.json"
    try:
        payload["cached_at"] = datetime.now().isoformat()
        p.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [score_prior] キャッシュ保存失敗 ({sector_code}): {e}")


# ==============================================================
# BLOCK 3: score_history.json 読み込み
# ==============================================================

def _load_score_history(cfg) -> dict:
    """
    DATA_DIR/score_history.json を読み込む。

    Returns:
        {"YYYY-MM-DD": {"sector_code": {"score": float, "risk": str}}, ...}
        読み込み失敗時は {}
    """
    try:
        path = Path(getattr(cfg, "DATA_DIR", "stock_data")) / _DEFAULT_SCORE_HISTORY
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))

        # 形式チェック: dict 形式のみ対応
        # （旧リスト形式は stock_analyzer_main.py 側で変換済みのため不要）
        if isinstance(raw, dict):
            return raw

        # 旧リスト形式 [{"date": str, "scores": {...}}, ...] への後方互換
        if isinstance(raw, list):
            return {
                h["date"]: h.get("scores", {})
                for h in raw
                if isinstance(h, dict) and h.get("date")
            }
    except Exception as e:
        print(f"    ⚠️  [score_prior] score_history 読み込み失敗: {str(e)[:60]}")
    return {}


def _extract_sector_history(
    sector_code:  str,
    history_all:  dict,
    ttl_days:     int,
) -> list[dict]:
    """
    全履歴から特定業種の有効期間内データを抽出する。

    Returns:
        [{"date": str, "score": float}, ...]  日付昇順・TTL内のみ
    """
    cutoff = datetime.now() - timedelta(days=ttl_days)
    result = []

    for date_str, day_data in history_all.items():
        if not isinstance(day_data, dict):
            continue
        # 日付パース（TTL外は除外）
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if dt < cutoff:
                continue
        except Exception:
            continue

        entry = day_data.get(sector_code)
        if entry is None:
            continue

        # スコア抽出（dict形式 / float直接 の両対応）
        if isinstance(entry, dict):
            score = entry.get("score")
        else:
            try:
                score = float(entry)
            except (TypeError, ValueError):
                score = None

        if score is not None:
            result.append({"date": date_str, "score": float(score)})

    # 日付昇順にソート
    result.sort(key=lambda x: x["date"])
    return result


# ==============================================================
# BLOCK 4: 鮮度係数・統計量計算
# ==============================================================

def _calc_freshness(days_elapsed: float, half_life: float) -> float:
    """
    鮮度係数を計算する。

    $freshness = exp(-\\lambda \times days\\_elapsed)$
    $\\lambda = \\ln(2) / half\\_life$

    Args:
        days_elapsed: 最新スコアからの経過日数
        half_life:    鮮度が 0.5 になる日数

    Returns:
        float: [0.0, 1.0]
    """
    if half_life <= 0:
        return 1.0
    lam = math.log(2.0) / half_life
    return round(math.exp(-lam * max(0.0, days_elapsed)), 4)


def _calc_weighted_stats(
    history:    list[dict],
    half_life:  float,
) -> tuple[float, float, list[float]]:
    """
    鮮度係数による加重平均・加重標準偏差を計算する。
    直近のスコアほど高い重みを付与する。

    $w_t = exp(-\\lambda \times (T - t))$  t: 観測日, T: 最新日

    Returns:
        (weighted_mean, weighted_std, weights_list)
        データが1件以下の場合は (score, 0.0, [1.0])
    """
    if not history:
        return 0.0, 0.0, []

    if len(history) == 1:
        return history[0]["score"], 0.0, [1.0]

    # 最新日付を基準に重みを計算
    latest_date = datetime.strptime(history[-1]["date"], "%Y-%m-%d")
    lam = math.log(2.0) / half_life if half_life > 0 else 0.0

    weights = []
    scores  = []
    for h in history:
        dt   = datetime.strptime(h["date"], "%Y-%m-%d")
        days = (latest_date - dt).days
        w    = math.exp(-lam * days)
        weights.append(w)
        scores.append(h["score"])

    total_w = sum(weights)
    if total_w == 0:
        return scores[-1], 0.0, weights

    # 加重平均
    w_mean = sum(w * s for w, s in zip(weights, scores)) / total_w

    # 加重標準偏差
    w_var = sum(w * (s - w_mean) ** 2 for w, s in zip(weights, scores)) / total_w
    w_std = math.sqrt(w_var)

    return round(w_mean, 2), round(w_std, 2), weights


def _calc_prior_weight(
    freshness: float,
    n_obs:     int,
    min_obs:   int = _DEFAULT_MIN_OBS,
) -> float:
    """
    prior_weight（信頼度重み）を計算する。

    $prior\\_weight = freshness \times \\min(1.0, n\\_obs / min\\_obs)$

    Args:
        freshness: 鮮度係数 [0.0, 1.0]
        n_obs:     有効観測数
        min_obs:   prior_weight=1.0 となる最低観測数

    Returns:
        float: [0.0, 1.0]
    """
    obs_factor = min(1.0, n_obs / max(1, min_obs))
    return round(freshness * obs_factor, 4)


# ==============================================================
# BLOCK 5: メインマネージャクラス
# ==============================================================

class ScorePriorManager:
    """
    業種別スコア事前確率の計算・管理クラス。

    使い方:
        spm = ScorePriorManager(cfg)

        # 1業種の事前確率を取得
        prior = spm.get_prior("1016")

        # 全業種を一括取得（stock_analyzer_main.py Phase 0 用）
        all_priors = spm.get_all_priors(["1001","1002",...,"1033"])

        # 鮮度係数によるスコア補正（Phase 0 での利用）
        adjusted = spm.apply_freshness_weight(current_score=72.0, prior=prior)
    """

    def __init__(self, cfg):
        self._cfg       = cfg
        self._ttl       = int(getattr(cfg, "SCORE_PRIOR_TTL_DAYS",
                                      _DEFAULT_TTL_DAYS))
        self._half_life = float(getattr(cfg, "SCORE_PRIOR_FRESHNESS_HALF_LIFE",
                                        _DEFAULT_HALF_LIFE))
        self._min_obs   = int(getattr(cfg, "CONFIDENCE_PRIOR_MIN_HIST",
                                     _DEFAULT_MIN_OBS))
        # score_history は初回アクセス時に遅延ロード（全業種共有）
        self._history_all: Optional[dict] = None

    def _get_history(self) -> dict:
        """score_history.json を遅延ロードして返す（同一インスタンス内でキャッシュ）。"""
        if self._history_all is None:
            self._history_all = _load_score_history(self._cfg)
        return self._history_all

    def get_prior(
        self,
        sector_code: str,
        use_cache:   bool = True,
    ) -> dict:
        """
        1業種のスコア事前確率を計算して返す。

        Args:
            sector_code: 33業種コード (例: "1016")
            use_cache:   True のとき TTL 内のキャッシュを使用

        Returns:
            {
              "sector_code":  str,
              "prior_mean":   float,
              "prior_std":    float,
              "prior_weight": float,
              "freshness":    float,
              "n_obs":        int,
              "last_score":   float,
              "last_date":    str,
            }
            データ不足・失敗時は prior_weight=0.0 のデフォルト辞書を返す。
        """
        try:
            # ── キャッシュ確認 ─────────────────────────────────
            if use_cache:
                cached = _load_prior_cache(sector_code, self._cfg)
                if cached is not None:
                    return {k: v for k, v in cached.items() if k != "cached_at"}

            # ── score_history から業種データ抽出 ────────────────
            history_all = self._get_history()
            hist = _extract_sector_history(sector_code, history_all, self._ttl)

            if not hist:
                return self._default_prior(sector_code, reason="no_history")

            n_obs = len(hist)

            # ── 加重統計量計算 ────────────────────────────────
            w_mean, w_std, _ = _calc_weighted_stats(hist, self._half_life)

            # ── 鮮度係数計算（最新スコアからの経過日数）───────
            last_date  = hist[-1]["date"]
            last_score = hist[-1]["score"]
            try:
                last_dt       = datetime.strptime(last_date, "%Y-%m-%d")
                days_elapsed  = (datetime.now() - last_dt).days
            except Exception:
                days_elapsed = 0

            freshness    = _calc_freshness(days_elapsed, self._half_life)
            prior_weight = _calc_prior_weight(freshness, n_obs, self._min_obs)

            result = {
                "sector_code":  sector_code,
                "prior_mean":   w_mean,
                "prior_std":    round(w_std, 2),
                "prior_weight": prior_weight,
                "freshness":    freshness,
                "n_obs":        n_obs,
                "last_score":   round(last_score, 1),
                "last_date":    last_date,
            }

            # ── キャッシュ保存 ────────────────────────────────
            _save_prior_cache(sector_code, self._cfg, dict(result))
            return result

        except Exception as e:
            print(
                f"    ⚠️  [score_prior] get_prior エラー ({sector_code}): "
                f"{str(e)[:60]}"
            )
            return self._default_prior(sector_code, reason=str(e)[:30])

    def get_all_priors(
        self,
        sector_codes: list[str],
        use_cache:    bool = True,
    ) -> dict[str, dict]:
        """
        全業種のスコア事前確率を一括取得する。
        stock_analyzer_main.py の Phase 0 から呼び出す。

        Args:
            sector_codes: 33業種コードのリスト
            use_cache:    True のとき TTL 内のキャッシュを使用

        Returns:
            {sector_code: prior_dict, ...}
        """
        result = {}
        for code in sector_codes:
            result[code] = self.get_prior(code, use_cache=use_cache)
        valid = sum(1 for p in result.values() if p["prior_weight"] > 0)
        print(
            f"    📦 [score_prior] 全業種取得完了: "
            f"{len(result)}業種 / 有効 {valid}業種"
        )
        return result

    def apply_freshness_weight(
        self,
        current_score: float,
        prior:         dict,
        alpha:         float = 0.15,
    ) -> float:
        """
        鮮度係数によるスコアの事後更新を行う。
        stock_analyzer_main.py Phase 0 での利用。

        ベイズ的加重平均:
        $posterior = (1 - w) \times current + w \times prior\\_mean$
        $w = prior\\_weight \times alpha$

        prior_weight が低い（データ不足・古い）ほど current_score を重視する。
        prior_weight=0.0 の場合は current_score をそのまま返す。

        Args:
            current_score: 今回の calc_composite_score の結果
            prior:         get_prior() の返り値
            alpha:         prior への最大引き寄せ率（デフォルト: 0.15）
                           CONFIDENCE_PRIOR_ALPHA と対応

        Returns:
            float: 補正後のスコア（クランプなし・呼び出し元でクランプ）
        """
        try:
            pw         = float(prior.get("prior_weight", 0.0))
            prior_mean = float(prior.get("prior_mean",   current_score))

            if pw <= 0.0:
                return current_score

            # $w = pw \times alpha$ （最大 alpha=0.15 の引き寄せ）
            w         = pw * alpha
            posterior = (1.0 - w) * current_score + w * prior_mean
            return round(posterior, 2)

        except Exception as e:
            print(
                f"    ⚠️  [score_prior] apply_freshness_weight エラー: "
                f"{str(e)[:60]}"
            )
            return current_score

    def get_prior_summary(self, sector_codes: list[str]) -> list[dict]:
        """
        全業種の事前確率サマリーをコンソール表示用に整形して返す。
        drift_detector.py / alert_notifier.py との連携用。
        """
        rows = []
        for code in sector_codes:
            p = self.get_prior(code)
            rows.append({
                "sector_code":  code,
                "prior_mean":   p["prior_mean"],
                "prior_std":    p["prior_std"],
                "prior_weight": p["prior_weight"],
                "freshness":    p["freshness"],
                "n_obs":        p["n_obs"],
                "last_score":   p["last_score"],
                "last_date":    p["last_date"],
            })
        return rows

    @staticmethod
    def _default_prior(sector_code: str, reason: str = "") -> dict:
        """データ不足・エラー時のデフォルト prior を返す。"""
        return {
            "sector_code":  sector_code,
            "prior_mean":   50.0,   # 中間値をデフォルト
            "prior_std":    0.0,
            "prior_weight": 0.0,    # 信頼度 0 → apply_freshness_weight が無効化
            "freshness":    0.0,
            "n_obs":        0,
            "last_score":   50.0,
            "last_date":    "",
            "reason":       reason,
        }


# ==============================================================
# BLOCK 6: モジュールレベル便利関数
# ==============================================================

def get_prior(sector_code: str, cfg) -> dict:
    """
    ScorePriorManager を直接インスタンス化せずに使う便利関数。
    stock_analyzer_main.py の単発呼び出し用。
    """
    return ScorePriorManager(cfg).get_prior(sector_code)


def apply_freshness_weight_batch(
    sector_results: dict,
    cfg,
    alpha: float = None,
) -> dict:
    """
    sector_results 全業種に鮮度係数補正を一括適用する。
    stock_analyzer_main.py Phase 0 から呼び出す。

    Args:
        sector_results: {sector_code: {"composite_score": float, ...}, ...}
        cfg:            config モジュール
        alpha:          None のとき CONFIDENCE_PRIOR_ALPHA を使用

    Returns:
        補正済みの sector_results（in-place でも更新）
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    try:
        if alpha is None:
            alpha = float(getattr(cfg, "CONFIDENCE_PRIOR_ALPHA", 0.15))

        spm    = ScorePriorManager(cfg)
        codes  = [c for c, r in sector_results.items() if isinstance(r, dict)]
        priors = spm.get_all_priors(codes)

        adjusted_count = 0
        for code, res in sector_results.items():
            if not isinstance(res, dict):
                continue
            current = res.get("composite_score")
            if current is None:
                continue
            prior = priors.get(code, ScorePriorManager._default_prior(code))
            if prior["prior_weight"] <= 0.0:
                continue

            adjusted = spm.apply_freshness_weight(
                current_score = float(current),
                prior         = prior,
                alpha         = alpha,
            )
            # 元スコアを保持しつつ補正値を反映
            res["composite_score_raw"]  = round(float(current), 2)
            res["composite_score"]      = adjusted
            res["score_prior_weight"]   = prior["prior_weight"]
            res["score_prior_mean"]     = prior["prior_mean"]
            res["score_freshness"]      = prior["freshness"]
            adjusted_count += 1

        print(
            f"    ✅ [score_prior] 鮮度係数補正完了: "
            f"{adjusted_count}/{len(codes)}業種"
        )
        return sector_results

    except Exception as e:
        print(
            f"    ⚠️  [score_prior] apply_freshness_weight_batch エラー: "
            f"{str(e)[:60]}"
        )
        return sector_results   # 失敗時は未補正のまま返す


# ==============================================================
# BLOCK 7: 単体テスト
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

    print("=== score_prior_manager 単体テスト ===\n")

    # ── 鮮度係数の数値検証 ─────────────────────────────────────
    half_life = 14.0
    test_days = [0, 7, 14, 21, 28, 56]
    print("  ── 鮮度係数テーブル (half_life=14日) ──")
    for d in test_days:
        f = _calc_freshness(d, half_life)
        print(f"    {d:3d}日経過 → freshness={f:.4f}")

    # 期待値: 0日=1.000 / 7日≈0.707 / 14日=0.500 / 28日=0.250
    assert abs(_calc_freshness(0,  14) - 1.000) < 0.001
    assert abs(_calc_freshness(14, 14) - 0.500) < 0.001
    assert abs(_calc_freshness(28, 14) - 0.250) < 0.001
    print("  ✅ 鮮度係数 数値検証 PASS")

    # ── prior_weight の検証 ────────────────────────────────────
    print("\n  ── prior_weight テーブル ──")
    for n in [0, 3, 7, 14]:
        pw = _calc_prior_weight(freshness=0.8, n_obs=n, min_obs=7)
        print(f"    n_obs={n:2d} → prior_weight={pw:.4f}")
    # n_obs=0 → 0.0, n_obs=7 → 0.800, n_obs=14 → 0.800（上限1.0）
    assert _calc_prior_weight(0.8, 0,  7) == 0.0
    assert _calc_prior_weight(0.8, 7,  7) == 0.8
    assert _calc_prior_weight(0.8, 14, 7) == 0.8
    print("  ✅ prior_weight 数値検証 PASS")

    # ── 加重統計量の検証 ──────────────────────────────────────
    print("\n  ── 加重統計量テスト ──")
    mock_hist = [
        {"date": "2026-05-01", "score": 60.0},
        {"date": "2026-05-10", "score": 65.0},
        {"date": "2026-05-20", "score": "70.0"},   # 文字列耐性チェック用
        {"date": "2026-05-28", "score": 72.0},
    ]
    # 文字列スコアは _extract_sector_history で変換されるため
    # ここでは直接 float 変換して検証
    mock_hist_clean = [
        {"date": h["date"], "score": float(h["score"])} for h in mock_hist
    ]
    w_mean, w_std, weights = _calc_weighted_stats(mock_hist_clean, half_life=14)
    print(f"    加重平均={w_mean:.2f} / 加重std={w_std:.2f}")
    print(f"    weights={[round(w,3) for w in weights]}")
    # 直近ほど重いため、単純平均(66.75)より高い値になるはず
    assert w_mean > 66.75, f"加重平均 {w_mean} > 66.75 であるべき"
    print("  ✅ 加重統計量 数値検証 PASS")

    # ── apply_freshness_weight の検証 ─────────────────────────
    print("\n  ── apply_freshness_weight テスト ──")
    spm = ScorePriorManager(cfg)
    mock_prior = {
        "prior_mean":   65.0,
        "prior_weight": 1.0,
    }
    # alpha=0.15, current=80.0, prior=65.0
    # posterior = 0.85*80 + 0.15*65 = 68.0 + 9.75 = 77.75
    result = spm.apply_freshness_weight(80.0, mock_prior, alpha=0.15)
    expected = 0.85 * 80.0 + 0.15 * 65.0
    print(f"    current=80.0 prior=65.0 alpha=0.15 → posterior={result}")
    assert abs(result - expected) < 0.01, f"期待値 {expected:.2f} ≠ {result}"
    print("  ✅ apply_freshness_weight 数値検証 PASS")

    # prior_weight=0.0 → current_score をそのまま返す
    zero_prior = {"prior_mean": 65.0, "prior_weight": 0.0}
    assert spm.apply_freshness_weight(80.0, zero_prior) == 80.0
    print("  ✅ prior_weight=0.0 時の無効化 PASS")

    print("\n  ✅ 全テスト完了")
