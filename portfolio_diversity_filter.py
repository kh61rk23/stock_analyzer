# ==============================================================
# portfolio_diversity_filter.py  v1.0  (FE-6)
# ポートフォリオ相関管理・集中度（HHI）フィルター
# ==============================================================
# 【役割】
#   screener 通過銘柄の60日リターン相関行列と
#   セクター別HHI（ハーフィンダール・ハーシュマン指数）を計算し、
#   ポートフォリオ集中リスクを定量化する。
#   Ignition Detector が同一セクターの収束を検出している場合は
#   集中警告を抑制する（意図的収束との区別）。
#
# 【HHI（Herfindahl-Hirschman Index）設計】
#   $HHI = \sum_{i=1}^{n} w_i^2$
#   $w_i$ = セクター $i$ の候補数比率
#
#   HHI < 0.25               : 正常（分散十分）
#   $0.25 \leq HHI < 0.35$   : 警告（DIVERSITY_HHI_WARNING）
#   $HHI \geq 0.35$           : 危険（DIVERSITY_HHI_CRITICAL）
#
# 【高相関ペア検出】
#   60日リターン相関 $\rho \geq DIVERSITY\_CORRELATION\_THRESHOLD(0.70)$ のペアを抽出。
#   トリガー㉓: $HHI > 0.30$ かつ 高相関ペア $\geq 3$ 組 → Gmail週次警告
#
# 【Ignition 抑制ロジック】
#   Ignition Detector が検出した収束セクターと高集中セクターが重複する場合、
#   そのセクターの集中警告を suppress=True にして出力のみ（アラートは発報しない）。
#   意図的なテーマ集中を誤検知しないための安全機構。
#
# 【呼び出しパターン（stock_analyzer_main.py WEEKLY_MODULES から）】
#   from portfolio_diversity_filter import run_portfolio_diversity_filter
#   div_result = run_portfolio_diversity_filter(cfg, results=results)
#   results["diversity"] = div_result
#
# 【依存】
#   yfinance（60日株価取得）、標準ライブラリのみ（相関計算は純粋実装）
#   config.py（BLOCK 33定数参照）
#   ※ 全関数を try/except で保護。失敗時は empty結果を返す。
# ==============================================================

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: デフォルト定数
# ==============================================================


from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'PDF_CORR_THRESHOLD_DEFAULT': '_CORR_THRESHOLD_DEFAULT',
    'PDF_HHI_ALERT_THRESHOLD': '_HHI_ALERT_THRESHOLD',
    'B4_PORTFOLIO_DIVERSITY_FILTER_CORR_WINDOW_DAYS': '_CORR_WINDOW_DAYS',
    'B5_PORTFOLIO_DIVERSITY_FILTER_FETCH_DELAY': '_FETCH_DELAY',
    'B6_PORTFOLIO_DIVERSITY_FILTER_HHI_WARNING_DEFAULT': '_HHI_WARNING_DEFAULT',
    'B6_PORTFOLIO_DIVERSITY_FILTER_HHI_CRITICAL_DEFAULT': '_HHI_CRITICAL_DEFAULT',
    'B6_PORTFOLIO_DIVERSITY_FILTER_MIN_DATA_POINTS': '_MIN_DATA_POINTS',
    'B6_PORTFOLIO_DIVERSITY_FILTER_MIN_CORR_PAIRS_FOR_ALERT': '_MIN_CORR_PAIRS_FOR_ALERT',
}

_CORR_THRESHOLD_DEFAULT  = 0.70   # 高相関とみなす閾値
_HHI_WARNING_DEFAULT     = 0.25
_HHI_CRITICAL_DEFAULT    = 0.35
_CORR_WINDOW_DAYS        = 60     # 相関計算ウィンドウ（日）
_MIN_DATA_POINTS         = 20     # OLS最低データ数
_MAX_TICKERS_FOR_CORR    = 30     # 相関行列計算の最大銘柄数（API負荷制限）
_FETCH_DELAY             = 0.4    # yfinance API 間隔（秒）
_HHI_ALERT_THRESHOLD     = 0.30   # トリガー㉓ HHI 閾値
_MIN_CORR_PAIRS_FOR_ALERT = 3     # トリガー㉓ 最低高相関ペア数


# ==============================================================
# BLOCK 1: キャッシュ I/O
# ==============================================================

def _cache_dir(cfg=None) -> Path:
    try:
        return Path(getattr(cfg, "CACHE_DIR", "/tmp"))
    except Exception:
        return Path("/tmp")


def _load_corr_cache(cfg=None) -> dict:
    """相関行列キャッシュを読み込む。TTL(1日)切れは空 dict。"""
    try:
        p = _cache_dir(cfg) / "diversity_corr_cache.json"
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        date = raw.get("date", "")
        today = datetime.now().strftime("%Y-%m-%d")
        return raw if date == today else {}
    except Exception:
        return {}


def _save_corr_cache(data: dict, cfg=None) -> None:
    """相関行列キャッシュを保存する。"""
    try:
        p = _cache_dir(cfg) / "diversity_corr_cache.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"date": datetime.now().strftime("%Y-%m-%d"), **data},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ==============================================================
# BLOCK 2: リターン系列取得
# ==============================================================

def _fetch_returns(ticker: str, window_days: int = _CORR_WINDOW_DAYS) -> list[float]:
    """
    yfinance から日次リターンを取得する。

    Returns:
        日次リターンのリスト（最新日が末尾）
        ※ 取得失敗時は空リスト
    """
    try:
        import yf_cache as yf
        time.sleep(_FETCH_DELAY)
        hist = yf.Ticker(ticker).history(
            period=f"{window_days + 10}d", auto_adjust=True
        )
        if hist is None or len(hist) < _MIN_DATA_POINTS:
            return []

        closes = list(hist["Close"].dropna().values)
        if len(closes) < 2:
            return []

        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
        # 最新 window_days 件に絞る
        return returns[-window_days:]
    except Exception:
        return []


def fetch_returns_batch(
    tickers:     list[str],
    window_days: int   = _CORR_WINDOW_DAYS,
    cfg=None,
) -> dict[str, list[float]]:
    """
    複数ティッカーのリターン系列を一括取得する。

    Args:
        tickers:     ["6758.T", "6861.T", ...] 形式
        window_days: 取得日数
        cfg:         config モジュール（None 可）

    Returns:
        {ticker: returns_list, ...}
        ※ 取得失敗ティッカーはキーなし
    """
    print(f"    📊 相関計算用リターン取得中（{len(tickers)}銘柄）...")
    result: dict[str, list[float]] = {}

    for t in tickers:
        try:
            r = _fetch_returns(t, window_days)
            if len(r) >= _MIN_DATA_POINTS:
                result[t] = r
        except Exception:
            pass

    print(f"    取得完了: {len(result)}/{len(tickers)}件")
    return result


# ==============================================================
# BLOCK 3: 相関行列計算（純粋実装）
# ==============================================================

def _pearson_corr(x: list[float], y: list[float]) -> Optional[float]:
    """
    2系列のピアソン相関係数を計算する。

    $\rho = \frac{\\sum(x_i - \bar{x})(y_i - \bar{y})}$
           ${\\sqrt{\\sum(x_i - \bar{x})^2 \\cdot \\sum(y_i - \bar{y})^2}}$

    Returns:
        float: -1.0〜1.0 | None（計算不可）
    """
    try:
        n = min(len(x), len(y))
        if n < _MIN_DATA_POINTS:
            return None

        x = x[-n:]
        y = y[-n:]
        x_mean = sum(x) / n
        y_mean = sum(y) / n

        num    = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
        den_x  = sum((xi - x_mean) ** 2 for xi in x)
        den_y  = sum((yi - y_mean) ** 2 for yi in y)
        den    = math.sqrt(den_x * den_y)

        if abs(den) < 1e-12:
            return None

        return round(num / den, 4)
    except Exception:
        return None


def calc_correlation_matrix(
    returns_dict: dict[str, list[float]],
) -> dict[str, dict[str, float]]:
    """
    全ティッカーペアの相関行列を計算する。

    Args:
        returns_dict: {ticker: returns_list, ...}

    Returns:
        {ticker_i: {ticker_j: corr_value, ...}, ...}
        上三角のみ格納（i < j）
    """
    tickers = sorted(returns_dict.keys())
    matrix:  dict[str, dict[str, float]] = {t: {} for t in tickers}

    for i, t1 in enumerate(tickers):
        for t2 in tickers[i + 1:]:
            corr = _pearson_corr(
                returns_dict[t1], returns_dict[t2]
            )
            if corr is not None:
                matrix[t1][t2] = corr

    return matrix


def find_high_corr_pairs(
    corr_matrix: dict[str, dict[str, float]],
    threshold:   float = _CORR_THRESHOLD_DEFAULT,
) -> list[dict]:
    """
    閾値以上の高相関ペアを抽出する。

    Returns:
        [{"ticker1": str, "ticker2": str, "correlation": float}, ...]
        correlation 降順にソート済み
    """
    try:
        pairs = []
        for t1, row in corr_matrix.items():
            for t2, corr in row.items():
                if abs(corr) >= threshold:
                    pairs.append({
                        "ticker1":     t1,
                        "ticker2":     t2,
                        "correlation": corr,
                    })
        return sorted(pairs, key=lambda x: abs(x["correlation"]), reverse=True)
    except Exception:
        return []


# ==============================================================
# BLOCK 4: HHI 計算
# ==============================================================

def calc_hhi(
    candidates:   list[dict],
    sector_field: str = "sector_code",
) -> tuple[float, dict[str, float]]:
    """
    screener 通過銘柄のセクター分布から HHI を計算する。

    Args:
        candidates:   screener 通過銘柄リスト
        sector_field: セクターコードのフィールド名

    Returns:
        (hhi: float, sector_weights: dict[str, float])
    """
    try:
        total = len(candidates)
        if total == 0:
            return 0.0, {}

        # セクター別カウント
        counts: dict[str, int] = {}
        for c in candidates:
            sc = str(c.get(sector_field, "unknown"))
            counts[sc] = counts.get(sc, 0) + 1

        # ウェイト計算 → HHI
        weights: dict[str, float] = {
            sc: round(cnt / total, 4)
            for sc, cnt in counts.items()
        }
        hhi = round(sum(w ** 2 for w in weights.values()), 4)
        return hhi, weights

    except Exception:
        return 0.0, {}


def classify_hhi(hhi: float, cfg=None) -> str:
    """
    HHI 値を分類ラベルに変換する。

    Returns:
        "NORMAL" | "WARNING" | "CRITICAL"
    """
    try:
        warn_thr = float(getattr(cfg, "DIVERSITY_HHI_WARNING",  _HHI_WARNING_DEFAULT))
        crit_thr = float(getattr(cfg, "DIVERSITY_HHI_CRITICAL", _HHI_CRITICAL_DEFAULT))
        if hhi >= crit_thr:
            return "CRITICAL"
        elif hhi >= warn_thr:
            return "WARNING"
        return "NORMAL"
    except Exception:
        return "UNKNOWN"


# ==============================================================
# BLOCK 5: Ignition 抑制ロジック
# ==============================================================

def get_ignition_sector_codes(results: Optional[dict]) -> set[str]:
    """
    Ignition Detector の発火セクターコードセットを返す。

    Args:
        results: stock_analyzer_main.results 辞書

    Returns:
        set[str]: 発火セクターコードのセット（"1016" 形式）
    """
    try:
        if not results:
            return set()
        ign = results.get("ignition", {})
        alerts = ign.get("alerts", [])
        return {str(a.get("sector_code", "")) for a in alerts if a.get("is_ignition")}
    except Exception:
        return set()


def should_suppress_warning(
    sector_code:    str,
    ignition_codes: set[str],
) -> bool:
    """
    指定セクターの集中警告を Ignition により抑制すべきか判定する。

    Ignition が発火しているセクターへの集中は
    意図的なテーマ収束と判断し、警告をアラートではなく
    「参考情報」として出力のみに留める。

    Returns:
        bool: True → 抑制（アラートは発報しない）
    """
    return str(sector_code) in ignition_codes


# ==============================================================
# BLOCK 6: 多様性レポート生成
# ==============================================================

def build_diversity_report(
    hhi:          float,
    hhi_status:   str,
    sector_weights: dict[str, float],
    high_corr_pairs: list[dict],
    ignition_codes:  set[str],
    candidates:   list[dict],
    cfg=None,
) -> dict:
    """
    多様性フィルターの詳細レポートを構築する。

    Returns:
        {
          "hhi":                  float,
          "hhi_status":           str,
          "sector_weights":       dict,
          "high_corr_pairs":      list,
          "high_corr_pair_count": int,
          "should_alert":         bool,   # トリガー㉓発動条件
          "alert_suppressed":     bool,   # Ignition による抑制
          "concentrated_sectors": list,   # HHI 寄与の高いセクター
          "ignition_overlap":     list,   # Ignition 発火中かつ集中のセクター
          "option_b_enabled":     bool,
        }
    """
    try:
        alert_thr    = float(getattr(cfg, "DIVERSITY_HHI_WARNING",
                                      _HHI_ALERT_THRESHOLD))
        corr_thr     = float(getattr(cfg, "DIVERSITY_CORRELATION_THRESHOLD",
                                      _CORR_THRESHOLD_DEFAULT))
        min_pairs    = _MIN_CORR_PAIRS_FOR_ALERT
        option_b     = bool(getattr(cfg, "DIVERSITY_OPTION_B_ENABLED", False))

        n_high_pairs = len(high_corr_pairs)

        # 集中セクター（weight > 0.20）を抽出
        concentrated = [
            {"sector_code": sc, "weight": w, "count": round(w * len(candidates))}
            for sc, w in sorted(sector_weights.items(),
                                 key=lambda x: -x[1])
            if w >= 0.20
        ]

        # Ignition 発火中かつ集中しているセクター
        ignition_overlap = [
            sc for sc in concentrated
            if sc["sector_code"] in ignition_codes
        ]

        # アラート条件（トリガー㉓）
        raw_alert = (
            hhi >= _HHI_ALERT_THRESHOLD
            and n_high_pairs >= min_pairs
        )

        # Ignition 抑制: 集中セクターが全て Ignition 発火中なら抑制
        concentrated_scs = {s["sector_code"] for s in concentrated}
        all_suppressed   = (
            len(concentrated_scs) > 0
            and concentrated_scs.issubset(ignition_codes)
        )
        alert_suppressed = raw_alert and all_suppressed
        should_alert     = raw_alert and not alert_suppressed

        return {
            "hhi":                  hhi,
            "hhi_status":           hhi_status,
            "sector_weights":       sector_weights,
            "high_corr_pairs":      high_corr_pairs[:10],   # 上位10ペア
            "high_corr_pair_count": n_high_pairs,
            "should_alert":         should_alert,
            "alert_suppressed":     alert_suppressed,
            "concentrated_sectors": concentrated,
            "ignition_overlap":     ignition_overlap,
            "option_b_enabled":     option_b,
            "corr_threshold_used":  corr_thr,
        }
    except Exception:
        return {
            "hhi": hhi, "hhi_status": hhi_status,
            "sector_weights": sector_weights,
            "high_corr_pairs": [], "high_corr_pair_count": 0,
            "should_alert": False, "alert_suppressed": False,
            "concentrated_sectors": [], "ignition_overlap": [],
            "option_b_enabled": False,
        }


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_portfolio_diversity_filter(
    cfg=None,
    results: Optional[dict] = None,
) -> dict:
    """
    ポートフォリオ多様性フィルターのメインエントリポイント。
    stock_analyzer_main.py の WEEKLY_MODULES から呼ばれる。

    処理フロー:
      1. screener 通過銘柄から上位 MAX_TICKERS_FOR_CORR 件を取得
      2. HHI をセクター分布から計算
      3. 60日相関行列を計算（キャッシュ優先）
      4. 高相関ペアを抽出
      5. Ignition 発火セクターを確認して抑制判定
      6. 多様性レポートを構築して返す

    Args:
        cfg:     config モジュール（None 可）
        results: stock_analyzer_main.results 辞書（None 可）

    Returns:
        {
          "hhi":              float,
          "hhi_status":       str,
          "high_corr_pairs":  list,
          "should_alert":     bool,
          "alert_suppressed": bool,
          "concentrated_sectors": list,
          "ignition_overlap": list,
          "diversity_score":  float,   # 0〜100（高いほど分散）
          "summary":          str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("  🔀 [portfolio_diversity_filter] 多様性分析実行中...")

    if results is None:
        results = {}

    _empty = {
        "hhi": 0.0, "hhi_status": "UNKNOWN",
        "high_corr_pairs": [], "high_corr_pair_count": 0,
        "should_alert": False, "alert_suppressed": False,
        "concentrated_sectors": [], "ignition_overlap": [],
        "diversity_score": 50.0, "summary": "データ不足",
    }

    # ── ① screener 通過銘柄を取得 ────────────────────────────
    try:
        screener   = results.get("screener", {})
        passed     = (screener.get("passed")
                      or screener.get("candidates", []))
        if not passed:
            print("    ⚠️  screenerデータなし → スキップ")
            return _empty
    except Exception:
        return _empty

    # ── ② HHI 計算（データ取得不要・即時）───────────────────
    try:
        hhi, sector_weights = calc_hhi(passed)
        hhi_status = classify_hhi(hhi, cfg)
        print(f"    HHI = {hhi:.3f} [{hhi_status}] "
              f"({len(sector_weights)}セクター)")
    except Exception:
        hhi, sector_weights, hhi_status = 0.0, {}, "UNKNOWN"

    # ── ③ Ignition 発火セクターを確認 ───────────────────────
    try:
        ignition_codes = get_ignition_sector_codes(results)
        if ignition_codes:
            print(f"    🔥 Ignition発火セクター: {len(ignition_codes)}件"
                  f" → 集中警告の抑制判定あり")
    except Exception:
        ignition_codes = set()

    # ── ④ 相関行列計算（上位 MAX_TICKERS_FOR_CORR 件）────────
    high_corr_pairs: list[dict] = []
    try:
        # キャッシュ確認
        corr_cache = _load_corr_cache(cfg)
        cached_pairs = corr_cache.get("high_corr_pairs")

        if cached_pairs is not None:
            print(f"    キャッシュ使用: 高相関ペア{len(cached_pairs)}組")
            high_corr_pairs = cached_pairs
        else:
            # 上位スコア銘柄を選択
            window_days = int(getattr(cfg, "DIVERSITY_CORR_WINDOW_DAYS",
                                       _CORR_WINDOW_DAYS))
            top_cands   = sorted(
                passed,
                key=lambda x: x.get("composite_score", 0),
                reverse=True,
            )[:_MAX_TICKERS_FOR_CORR]
            tickers = [
                str(c.get("ticker", "")) for c in top_cands
                if c.get("ticker")
            ]

            if len(tickers) >= 2:
                returns_dict = fetch_returns_batch(tickers, window_days, cfg)

                if len(returns_dict) >= 2:
                    corr_thr = float(getattr(cfg,
                                              "DIVERSITY_CORRELATION_THRESHOLD",
                                              _CORR_THRESHOLD_DEFAULT))
                    corr_matrix = calc_correlation_matrix(returns_dict)
                    high_corr_pairs = find_high_corr_pairs(corr_matrix, corr_thr)

                    # キャッシュに保存
                    _save_corr_cache(
                        {"high_corr_pairs": high_corr_pairs,
                         "tickers_computed": tickers},
                        cfg,
                    )
                    print(f"    相関計算完了: 高相関ペア{len(high_corr_pairs)}組 "
                          f"(閾値ρ≥{corr_thr:.2f})")
            else:
                print("    ⚠️  相関計算対象が不足（2銘柄未満）")
    except Exception as e:
        print(f"    ⚠️  相関計算エラー（スキップ）: {str(e)[:60]}")

    # ── ⑤ 多様性レポート構築 ────────────────────────────────
    try:
        report = build_diversity_report(
            hhi             = hhi,
            hhi_status      = hhi_status,
            sector_weights  = sector_weights,
            high_corr_pairs = high_corr_pairs,
            ignition_codes  = ignition_codes,
            candidates      = passed,
            cfg             = cfg,
        )
    except Exception:
        report = {
            "hhi": hhi, "hhi_status": hhi_status,
            "sector_weights": sector_weights,
            "high_corr_pairs": high_corr_pairs,
            "high_corr_pair_count": len(high_corr_pairs),
            "should_alert": False, "alert_suppressed": False,
            "concentrated_sectors": [], "ignition_overlap": [],
        }

    # ── ⑥ 多様性スコア（0〜100）────────────────────────────
    try:
        # HHI が低く、高相関ペアが少ないほど高スコア
        hhi_score  = max(0.0, (1.0 - hhi / 0.5) * 60.0)
        pair_penalty = min(30.0, len(high_corr_pairs) * 3.0)
        diversity_score = round(min(100.0, max(0.0,
                                    hhi_score + 40.0 - pair_penalty)), 1)
    except Exception:
        diversity_score = 50.0

    # ── ⑦ サマリー ───────────────────────────────────────────
    suppress_str = "（Ignitionにより抑制）" if report.get("alert_suppressed") else ""
    alert_str    = f"⚠️ アラート{suppress_str}" if report.get("should_alert") else "正常"
    summary = (
        f"HHI={hhi:.3f}[{hhi_status}] "
        f"高相関ペア{len(high_corr_pairs)}組 "
        f"多様性スコア={diversity_score:.0f}pt "
        f"| {alert_str}"
    )

    if report.get("should_alert"):
        print(f"    ⚠️  トリガー㉓条件充足: HHI={hhi:.3f} "
              f"高相関ペア={len(high_corr_pairs)}組")
    elif report.get("alert_suppressed"):
        print(f"    🔥 Ignition抑制: 集中警告をGmail送信しない（意図的収束）")

    print(f"  ✅ 多様性分析完了: {summary}")

    return {
        **report,
        "diversity_score": diversity_score,
        "summary":         summary,
        "generated":       datetime.now().strftime("%Y-%m-%d"),
    }


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import tempfile, random

    print("=" * 60)
    print("  portfolio_diversity_filter.py v1.0 単体テスト")
    print("=" * 60)

    class _TmpCfg:
        CACHE_DIR                       = Path(tempfile.mkdtemp())
        DIVERSITY_CORRELATION_THRESHOLD = 0.70
        DIVERSITY_HHI_WARNING           = 0.25
        DIVERSITY_HHI_CRITICAL          = 0.35
        DIVERSITY_OPTION_B_ENABLED      = False
        DIVERSITY_CORR_WINDOW_DAYS      = 60

    _cfg = _TmpCfg()
    random.seed(42)

    # ── HHI 計算テスト ────────────────────────────────────────
    print("\n── HHI 計算テスト ──")
    test_portfolios = [
        # (portfolio_description, candidates)
        ("分散型（6セクター均等）", [
            {"sector_code": str(1010 + i), "ticker": f"T{i}.T"}
            for i in range(18)   # 各セクター3銘柄
        ]),
        ("集中型（1セクター70%）", (
            [{"sector_code": "1016", "ticker": f"E{i}.T"} for i in range(7)]
            + [{"sector_code": "1017", "ticker": f"A{i}.T"} for i in range(3)]
        )),
    ]
    for label, cands in test_portfolios:
        hhi, weights = calc_hhi(cands)
        status = classify_hhi(hhi, _cfg)
        print(f"  {label}: HHI={hhi:.3f} [{status}]")
        top_secs = sorted(weights.items(), key=lambda x: -x[1])[:3]
        for sc, w in top_secs:
            print(f"    {sc}: {w*100:.1f}%")

    # ── 相関計算テスト ────────────────────────────────────────
    print("\n── ピアソン相関テスト ──")
    base   = [random.gauss(0, 0.01) for _ in range(60)]
    high_y = [x * 0.9 + random.gauss(0, 0.003) for x in base]   # ρ≈0.97
    low_y  = [random.gauss(0, 0.01) for _ in range(60)]          # ρ≈0.00
    neg_y  = [-x + random.gauss(0, 0.003) for x in base]         # ρ≈-0.97

    for label, y in [("高相関(期待ρ≈0.97)", high_y),
                      ("無相関(期待ρ≈0.00)", low_y),
                      ("逆相関(期待ρ≈-0.97)", neg_y)]:
        rho = _pearson_corr(base, y)
        print(f"  {label}: ρ={rho:.3f}")

    # ── 相関行列 + 高相関ペア検出テスト ──────────────────────
    print("\n── 相関行列・高相関ペア検出テスト ──")
    returns_dict = {
        "A.T": base,
        "B.T": high_y,
        "C.T": low_y,
        "D.T": neg_y,
    }
    matrix = calc_correlation_matrix(returns_dict)
    pairs  = find_high_corr_pairs(matrix, threshold=0.70)
    print(f"  検出ペア数（閾値0.70）: {len(pairs)}組")
    for p in pairs:
        print(f"    {p['ticker1']} × {p['ticker2']}: ρ={p['correlation']:.3f}")

    # ── Ignition 抑制テスト ───────────────────────────────────
    print("\n── Ignition 抑制テスト ──")
    mock_results = {
        "ignition": {
            "alerts": [
                {"sector_code": "1016", "is_ignition": True,
                 "convergence_score": 3.8},
            ]
        }
    }
    ig_codes = get_ignition_sector_codes(mock_results)
    print(f"  発火セクター: {ig_codes}")
    print(f"  1016 の警告抑制: {should_suppress_warning('1016', ig_codes)} (True期待)")
    print(f"  1017 の警告抑制: {should_suppress_warning('1017', ig_codes)} (False期待)")

    # ── build_diversity_report テスト ─────────────────────────
    print("\n── diversity_report 構築テスト ──")
    mock_cands = (
        [{"sector_code": "1016", "ticker": f"E{i}.T"} for i in range(8)]
        + [{"sector_code": "1017", "ticker": f"A{i}.T"} for i in range(2)]
    )
    hhi_t, weights_t = calc_hhi(mock_cands)
    report = build_diversity_report(
        hhi             = hhi_t,
        hhi_status      = classify_hhi(hhi_t, _cfg),
        sector_weights  = weights_t,
        high_corr_pairs = pairs,
        ignition_codes  = ig_codes,
        candidates      = mock_cands,
        cfg             = _cfg,
    )
    print(f"  HHI={report['hhi']:.3f} [{report['hhi_status']}]")
    print(f"  should_alert={report['should_alert']}")
    print(f"  alert_suppressed={report['alert_suppressed']}")
    print(f"  concentrated: {[s['sector_code'] for s in report['concentrated_sectors']]}")
    print(f"  ignition_overlap: {[s['sector_code'] for s in report['ignition_overlap']]}")

    print("\n✅ 単体テスト完了")


# ==============================================================
# 実際の保有銘柄の集中度
#   上の HHI はスクリーナー通過銘柄の業種の偏りで、保有は見ない（通過0件の日は HHI 0）。
#   こちらは保有銘柄CSV（投資部の保有銘柄.csv から同期）の円評価額で、業種別・銘柄別の HHI を出す。
# ==============================================================

def holdings_concentration(holdings: list[dict]) -> dict:
    """保有の円評価額から集中度を計算する（純関数）。

    holdings: [{"ticker","name","sector_name","market","market_value_jpy"}...]
    """
    rows = [h for h in holdings if float(h.get("market_value_jpy") or 0) > 0]
    total = sum(float(h["market_value_jpy"]) for h in rows)
    if total <= 0:
        return {"total_jpy": 0}
    by_sec: dict[str, float] = {}
    by_mkt: dict[str, float] = {}
    for h in rows:
        v = float(h["market_value_jpy"])
        by_sec[h.get("sector_name") or "不明"] = by_sec.get(h.get("sector_name") or "不明", 0.0) + v
        by_mkt[h.get("market") or "?"] = by_mkt.get(h.get("market") or "?", 0.0) + v
    w_stock = sorted(((float(h["market_value_jpy"]) / total, h) for h in rows), key=lambda x: -x[0])
    return {
        "total_jpy": round(total),
        "hhi_sector": round(sum((v / total) ** 2 for v in by_sec.values()), 4),
        "hhi_stock": round(sum(w ** 2 for w, _ in w_stock), 4),
        "top3_share": round(sum(w for w, _ in w_stock[:3]), 4),
        "sector_weights": {k: round(v / total, 4) for k, v in sorted(by_sec.items(), key=lambda kv: -kv[1])},
        "market_weights": {k: round(v / total, 4) for k, v in by_mkt.items()},
        "top_holdings": [{"ticker": h.get("ticker"), "name": h.get("name"), "weight": round(w, 4),
                          "value_jpy": round(float(h["market_value_jpy"]))} for w, h in w_stock[:5]],
    }


def load_holdings_concentration(base_dir=None) -> dict:
    import csv as _csv
    from pathlib import Path as _P
    p = _P(base_dir or _P(__file__).parent) / "data" / "portfolio_holdings.csv"
    try:
        with open(p, encoding="utf-8-sig") as fh:
            return holdings_concentration(list(_csv.DictReader(fh)))
    except OSError:
        return {"total_jpy": 0}
