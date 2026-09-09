# ==============================================================
# pead_monitor.py  v1.0  (Wave 3 - FE-5)
# Post-Earnings Announcement Drift 日次監視エンジン
# ==============================================================
# 【役割】
#   決算サプライズ後の株価ドリフト候補を日次検出・スコアリングし、
#   TTL管理付きの pead_candidates.json に蓄積する。
#   陰性PEAD（ネガティブサプライズ後の下落ドリフト）にも対応する。
#
# 【PEAD スコア設計（0〜10点）】
#   $pead\_score = surprise\_score + volume\_score + price\_score$
#
#   surprise_score （max 4pt）:
#     screener の surprise_bonus / revision_score から算出
#     $surprise\_score = \min(4.0, |surprise\_bonus| \times 0.8)$
#
#   volume_score  （max 3pt）:
#     出来高比率（vs 20日平均）が 1.5倍以上で加点
#     $volume\_score = \min(3.0, (volume\_ratio - 1.0) \times 3.0)$
#
#   price_score   （max 3pt）:
#     決算後の株価ドリフト率
#     $price\_score = \min(3.0, |price\_drift| \times 10.0)$（10%ドリフトで3pt）
#
# 【日次減衰】
#   $pead\_score_{t+1} = pead\_score_t \times PEAD\_DECAY\_DAILY^{days}$
#   デフォルト: $PEAD\_DECAY\_DAILY = 0.85$
#
# 【陰性PEAD（negative）】
#   ネガティブサプライズ後の下落ドリフトを direction="negative" で追跡。
#   週次レポートで「回避候補」として表示。アラートは発報しない（設計方針）。
#
# 【アラート条件（alert_notifier.py トリガー㉒）】
#   $pead\_score \ge PEAD\_SCORE\_ALERT\_THRESHOLD (5.0)$
#   かつ $volume\_ratio \ge PEAD\_VOLUME\_MIN\_RATIO (1.5)$
#
# 【生成キャッシュ】
#   pead_candidates.json  TTL: 60日（PEAD_MAX_HOLDING_DAYS_JP）
#
# 【呼び出しパターン（stock_analyzer_main.py DAILY_MODULES から）】
#   from pead_monitor import run_pead_monitor
#   pead_result = run_pead_monitor(cfg, results=results)
#   results["pead"] = pead_result
#
# 【依存】
#   yfinance（出来高・株価取得）
#   config.py（BLOCK 32 定数参照）
#   ★ 全関数を try/except で保護。失敗時は空dict/0.0を返す。
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: デフォルト定数
# ==============================================================

_CACHE_TTL_DAYS        = 60    # pead_candidates.json TTL（日）= PEAD_MAX_HOLDING_DAYS_JP

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'PM_PEAD_SCORE_ALERT': '_PEAD_SCORE_ALERT',
    'PM_PEAD_VOLUME_MIN': '_PEAD_VOLUME_MIN',
    'PM_MIN_SURPRISE_BONUS': '_MIN_SURPRISE_BONUS',
    'B4_PEAD_MONITOR_CACHE_TTL_DAYS': '_CACHE_TTL_DAYS',
    'B4_PEAD_MONITOR_VOLUME_WINDOW_DAYS': '_VOLUME_WINDOW_DAYS',
    'B4_PEAD_MONITOR_PRICE_DRIFT_WINDOW': '_PRICE_DRIFT_WINDOW',
    'B5_PEAD_MONITOR_FETCH_DELAY': '_FETCH_DELAY',
    'B6_PEAD_MONITOR_PEAD_DECAY_DAILY': '_PEAD_DECAY_DAILY',
    'B6_PEAD_MONITOR_MAX_CANDIDATES': '_MAX_CANDIDATES',
}

_PEAD_SCORE_ALERT      = 5.0   # アラート閾値（config BLOCK 32 で上書き可能）
_PEAD_DECAY_DAILY      = 0.85  # 日次減衰係数
_PEAD_VOLUME_MIN       = 1.5   # 出来高倍率アラート閾値
_VOLUME_WINDOW_DAYS    = 20    # 出来高平均計算ウィンドウ（日）
_PRICE_DRIFT_WINDOW    = 5     # 株価ドリフト計算ウィンドウ（日）
_FETCH_DELAY           = 0.5   # yfinance API 間隔（秒）
_MAX_CANDIDATES        = 200   # キャッシュ最大保持件数

# 最小サプライズ閾値（これ未満はPEAD候補としない）
_MIN_SURPRISE_BONUS    = 0.5   # surprise_bonus の絶対値


# ==============================================================
# BLOCK 1: キャッシュ I/O
# ==============================================================

def _cache_dir(cfg=None) -> Path:
    try:
        return Path(getattr(cfg, "CACHE_DIR", "/tmp"))
    except Exception:
        return Path("/tmp")


def load_pead_candidates(cfg=None) -> dict:
    """
    pead_candidates.json を読み込む。TTL切れエントリを除外して返す。

    Returns:
        {
          "TICKER": {
            "first_detected":     str,
            "last_updated":       str,
            "pead_score":         float,
            "pead_score_peak":    float,
            "direction":          str,    # "positive" | "negative"
            "days_since_detect":  int,
            "surprise_magnitude": float,
            "volume_ratio":       float,
            "price_drift":        float,
            "sector_code":        str,
            "is_alert":           bool,
            "volume_trigger":     bool,
            "pead_expiry":        str,
          }, ...
        }
    """
    try:
        p = _cache_dir(cfg) / "pead_candidates.json"
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}

        ttl_days = int(getattr(cfg, "PEAD_MAX_HOLDING_DAYS_JP", _CACHE_TTL_DAYS))
        cutoff   = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
        return {
            t: v for t, v in raw.items()
            if isinstance(v, dict)
            and v.get("first_detected", "0000") >= cutoff
        }
    except Exception:
        return {}


def save_pead_candidates(candidates: dict, cfg=None) -> None:
    """pead_candidates.json を保存する（最大件数クランプあり）。"""
    try:
        # pead_score 降順に上位 MAX 件のみ保存
        if len(candidates) > _MAX_CANDIDATES:
            candidates = dict(
                sorted(
                    candidates.items(),
                    key=lambda x: x[1].get("pead_score", 0.0),
                    reverse=True,
                )[:_MAX_CANDIDATES]
            )
        p = _cache_dir(cfg) / "pead_candidates.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(candidates, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ==============================================================
# BLOCK 2: 補助データ取得（yfinance）
# ==============================================================

def _fetch_volume_ratio(ticker: str) -> Optional[float]:
    """
    直近出来高 / 20日平均出来高 の比率を返す。

    Returns:
        float | None（取得失敗時は None）
    """
    try:
        import yf_cache as yf
        time.sleep(_FETCH_DELAY)
        hist = yf.Ticker(ticker).history(period="30d", auto_adjust=True)
        if hist is None or len(hist) < 5:
            return None

        vols = list(hist["Volume"].dropna().values)
        if len(vols) < 5:
            return None

        recent_vol  = float(vols[-1])
        window_vols = vols[-min(_VOLUME_WINDOW_DAYS, len(vols)):-1]
        avg_vol     = sum(window_vols) / len(window_vols) if window_vols else 0.0

        if avg_vol <= 0:
            return None

        return round(recent_vol / avg_vol, 3)
    except Exception:
        return None


def _fetch_price_drift(ticker: str, window_days: int = _PRICE_DRIFT_WINDOW) -> float:
    """
    直近 window_days 日間の株価変動率（ドリフト）を返す。

    Returns:
        float: 変動率（例: 0.05 = +5%）
    """
    try:
        import yf_cache as yf
        time.sleep(_FETCH_DELAY * 0.5)
        hist = yf.Ticker(ticker).history(
            period=f"{window_days + 5}d", auto_adjust=True
        )
        if hist is None or len(hist) < 2:
            return 0.0

        closes = list(hist["Close"].dropna().values)
        if len(closes) < 2:
            return 0.0

        end_price   = float(closes[-1])
        start_price = float(closes[-min(window_days, len(closes) - 1)])
        if start_price <= 0:
            return 0.0

        return round((end_price - start_price) / start_price, 5)
    except Exception:
        return 0.0


# ==============================================================
# BLOCK 3: PEAD スコア計算
# ==============================================================

def _calc_surprise_score(
    surprise_bonus:  float,
    revision_score:  float,
    eps_surprise:    Optional[float] = None,
) -> float:
    """
    決算サプライズの強さから surprise_score（0〜4pt）を算出する。

    Args:
        surprise_bonus:  screener の surprise_bonus フィールド
        revision_score:  screener の revision_score フィールド
        eps_surprise:    EPS サプライズ率（小数表記 / None 可）

    Returns:
        float: 0.0〜4.0
    """
    try:
        base = abs(float(surprise_bonus)) * 0.8

        # revision_score が強い場合は加点
        rev_bonus = min(1.0, abs(float(revision_score)) * 2.0)
        base += rev_bonus

        # EPS サプライズ率がある場合はさらに加点
        if eps_surprise is not None:
            base += min(1.0, abs(float(eps_surprise)) * 5.0)

        return round(min(4.0, max(0.0, base)), 2)
    except Exception:
        return 0.0


def _calc_volume_score(volume_ratio: Optional[float]) -> float:
    """
    出来高倍率から volume_score（0〜3pt）を算出する。

    $volume\\_score = \\min(3.0, (ratio - 1.0) \times 3.0)$ if ratio > 1.0

    Returns:
        float: 0.0〜3.0
    """
    try:
        if volume_ratio is None or float(volume_ratio) <= 1.0:
            return 0.0
        score = (float(volume_ratio) - 1.0) * 3.0
        return round(min(3.0, max(0.0, score)), 2)
    except Exception:
        return 0.0


def _calc_price_score(
    price_drift: float,
    direction:   str,
) -> float:
    """
    株価ドリフト率から price_score（0〜3pt）を算出する。

    方向（positive/negative）と合致したドリフトのみ加点する。

    $price\\_score = \\min(3.0, |drift| \times 10.0)$ （10%で3pt）

    Returns:
        float: 0.0〜3.0
    """
    try:
        drift = float(price_drift)
        if direction == "positive" and drift <= 0:
            return 0.0   # 正PEADなのに下落 → 加点なし
        if direction == "negative" and drift >= 0:
            return 0.0   # 負PEADなのに上昇 → 加点なし

        score = abs(drift) * 10.0
        return round(min(3.0, max(0.0, score)), 2)
    except Exception:
        return 0.0


def calc_pead_score(
    surprise_bonus:  float,
    revision_score:  float,
    volume_ratio:    Optional[float],
    price_drift:     float,
    direction:       str,
    eps_surprise:    Optional[float] = None,
) -> tuple[float, dict]:
    """
    PEAD スコアを計算する。

    Returns:
        (pead_score: float, breakdown: dict)
    """
    try:
        surprise_sc = _calc_surprise_score(surprise_bonus, revision_score, eps_surprise)
        volume_sc   = _calc_volume_score(volume_ratio)
        price_sc    = _calc_price_score(price_drift, direction)

        total = round(min(10.0, max(0.0,
                          surprise_sc + volume_sc + price_sc)), 2)

        breakdown = {
            "surprise_score": surprise_sc,
            "volume_score":   volume_sc,
            "price_score":    price_sc,
        }
        return total, breakdown
    except Exception:
        return 0.0, {}


# ==============================================================
# BLOCK 4: 日次減衰
# ==============================================================

def apply_pead_decay(candidates: dict, cfg=None) -> dict:
    """
    既存の PEAD 候補に日次減衰を適用する。

    $pead\\_score_{t} = peak \times PEAD\\_DECAY\\_DAILY^{days\\_since\\_detect}$

    スコアが 0.1 未満になった候補は自動除外する。

    Args:
        candidates: load_pead_candidates() の返り値
        cfg:        config モジュール（None 可）

    Returns:
        減衰適用後の candidates dict（TTL切れエントリ除外済み）
    """
    try:
        decay_daily = float(getattr(cfg, "PEAD_DECAY_DAILY", _PEAD_DECAY_DAILY))
        today       = datetime.now().strftime("%Y-%m-%d")
        alert_thr   = float(getattr(cfg, "PEAD_SCORE_ALERT_THRESHOLD",
                                     _PEAD_SCORE_ALERT))
        vol_min     = float(getattr(cfg, "PEAD_VOLUME_MIN_RATIO", _PEAD_VOLUME_MIN))
        updated     = {}

        for ticker, entry in candidates.items():
            try:
                first = entry.get("first_detected", today)
                first_dt = datetime.strptime(first[:10], "%Y-%m-%d")
                days     = max(0, (datetime.now() - first_dt).days)

                peak     = float(entry.get("pead_score_peak", 0.0))
                new_score = round(peak * (decay_daily ** days), 3)

                if new_score < 0.1:
                    continue   # TTL切れとみなして除外

                vol_ratio = float(entry.get("volume_ratio", 0.0))
                is_alert  = (
                    new_score >= alert_thr
                    and vol_ratio >= vol_min
                    and entry.get("direction") == "positive"
                )

                updated[ticker] = {
                    **entry,
                    "pead_score":        new_score,
                    "days_since_detect": days,
                    "is_alert":          is_alert,
                    "last_updated":      today,
                }
            except Exception:
                updated[ticker] = entry

        return updated

    except Exception:
        return candidates


# ==============================================================
# BLOCK 5: 候補検出（screener 結果から）
# ==============================================================

def _determine_direction(
    surprise_bonus:  float,
    revision_score:  float,
) -> Optional[str]:
    """
    サプライズの方向（positive/negative）を判定する。

    Returns:
        "positive" | "negative" | None（シグナル弱すぎ）
    """
    try:
        # surprise_bonus と revision_score の合算で判定
        net = float(surprise_bonus) + float(revision_score)
        if net > _MIN_SURPRISE_BONUS:
            return "positive"
        elif net < -_MIN_SURPRISE_BONUS:
            return "negative"
        return None
    except Exception:
        return None


def detect_pead_candidates(
    candidates: list[dict],
    existing:   dict,
    cfg=None,
) -> tuple[list[dict], list[str]]:
    """
    screener 通過銘柄から PEAD 候補を検出する。

    処理:
      1. `pead_trigger=True` または surprise/revision が閾値以上の銘柄を抽出
      2. 各銘柄の出来高比率・株価ドリフトを取得（yfinance）
      3. PEAD スコアを算出
      4. 既存キャッシュにない新規候補のみ返す

    Args:
        candidates: screener 通過銘柄リスト
        existing:   load_pead_candidates() の返り値
        cfg:        config モジュール（None 可）

    Returns:
        (new_entries: list[dict], newly_detected_tickers: list[str])
    """
    new_entries: list[dict] = []
    new_tickers: list[str]  = []
    today = datetime.now().strftime("%Y-%m-%d")
    ttl_days = int(getattr(cfg, "PEAD_MAX_HOLDING_DAYS_JP", _CACHE_TTL_DAYS))
    alert_thr = float(getattr(cfg, "PEAD_SCORE_ALERT_THRESHOLD", _PEAD_SCORE_ALERT))
    vol_min   = float(getattr(cfg, "PEAD_VOLUME_MIN_RATIO", _PEAD_VOLUME_MIN))

    for c in candidates:
        ticker = str(c.get("ticker", ""))
        if not ticker or ticker in existing:
            continue   # 既存キャッシュ済みはスキップ

        surprise_bonus = float(c.get("surprise_bonus", 0.0) or 0.0)
        revision_score = float(c.get("revision_score", 0.0) or 0.0)
        pead_trigger   = bool(c.get("pead_trigger", False))

        # PEAD 候補フィルタ
        net_signal = abs(surprise_bonus) + abs(revision_score)
        if not pead_trigger and net_signal < _MIN_SURPRISE_BONUS:
            continue

        # 方向判定
        direction = _determine_direction(surprise_bonus, revision_score)
        if direction is None:
            continue

        # 出来高比率取得
        try:
            volume_ratio = _fetch_volume_ratio(ticker)
        except Exception:
            volume_ratio = None

        # 株価ドリフト取得
        try:
            price_drift = _fetch_price_drift(ticker)
        except Exception:
            price_drift = 0.0

        # PEAD スコア算出
        pead_score, breakdown = calc_pead_score(
            surprise_bonus = surprise_bonus,
            revision_score = revision_score,
            volume_ratio   = volume_ratio,
            price_drift    = price_drift,
            direction      = direction,
        )

        if pead_score < 0.5:
            continue   # スコアが低すぎる場合は除外

        vol_ratio_f = float(volume_ratio) if volume_ratio is not None else 0.0
        is_alert    = (
            pead_score >= alert_thr
            and vol_ratio_f >= vol_min
            and direction == "positive"
        )
        volume_trigger = vol_ratio_f >= vol_min

        entry = {
            "first_detected":     today,
            "last_updated":       today,
            "pead_score":         pead_score,
            "pead_score_peak":    pead_score,   # 初回が peak
            "direction":          direction,
            "days_since_detect":  0,
            "surprise_magnitude": round(net_signal, 3),
            "surprise_bonus":     surprise_bonus,
            "revision_score":     revision_score,
            "volume_ratio":       vol_ratio_f,
            "price_drift":        price_drift,
            "sector_code":        str(c.get("sector_code", "")),
            "is_alert":           is_alert,
            "volume_trigger":     volume_trigger,
            "pead_expiry":        (
                datetime.now() + timedelta(days=ttl_days)
            ).strftime("%Y-%m-%d"),
            "score_breakdown":    breakdown,
            "ticker":             ticker,
        }

        new_entries.append(entry)
        new_tickers.append(ticker)

    return new_entries, new_tickers


# ==============================================================
# BLOCK 6: アラート候補取得
# ==============================================================

def get_alert_candidates(cfg=None) -> list[dict]:
    """
    アラート条件を満たす PEAD 候補を返す。
    alert_notifier.py トリガー㉒ から参照。

    条件: is_alert=True（pead_score≥threshold かつ volume_ratio≥min かつ direction=positive）

    Returns:
        [{ticker, pead_score, volume_ratio, sector_code, ...}, ...]
        pead_score 降順にソート済み
    """
    try:
        candidates = load_pead_candidates(cfg)
        alerts = [
            {"ticker": t, **v}
            for t, v in candidates.items()
            if v.get("is_alert") and v.get("direction") == "positive"
        ]
        return sorted(alerts, key=lambda x: x.get("pead_score", 0), reverse=True)
    except Exception:
        return []


def get_negative_pead_candidates(cfg=None) -> list[dict]:
    """
    陰性PEAD候補（direction=negative）を返す。
    weekly_report_generator.py の「回避候補」セクションで参照。

    Returns:
        [{ticker, pead_score, direction, sector_code, ...}, ...]
    """
    try:
        candidates = load_pead_candidates(cfg)
        negatives = [
            {"ticker": t, **v}
            for t, v in candidates.items()
            if v.get("direction") == "negative"
            and v.get("pead_score", 0) >= 1.0   # 最低限のスコア
        ]
        return sorted(negatives, key=lambda x: x.get("pead_score", 0), reverse=True)
    except Exception:
        return []


def get_pead_summary(cfg=None) -> dict:
    """
    PEAD 候補の集計サマリーを返す（weekly_report 用）。

    Returns:
        {
          "total":        int,
          "positive":     int,
          "negative":     int,
          "alert_count":  int,
          "top_positive": list[dict],  # score上位5件
          "top_negative": list[dict],  # 陰性上位3件
        }
    """
    try:
        candidates = load_pead_candidates(cfg)
        pos = [(t, v) for t, v in candidates.items()
               if v.get("direction") == "positive"]
        neg = [(t, v) for t, v in candidates.items()
               if v.get("direction") == "negative"]
        alerts = [v for _, v in pos if v.get("is_alert")]

        top_pos = sorted(
            [{"ticker": t, **v} for t, v in pos],
            key=lambda x: x.get("pead_score", 0), reverse=True
        )[:5]
        top_neg = sorted(
            [{"ticker": t, **v} for t, v in neg],
            key=lambda x: x.get("pead_score", 0), reverse=True
        )[:3]

        return {
            "total":        len(candidates),
            "positive":     len(pos),
            "negative":     len(neg),
            "alert_count":  len(alerts),
            "top_positive": top_pos,
            "top_negative": top_neg,
        }
    except Exception:
        return {"total": 0, "positive": 0, "negative": 0,
                "alert_count": 0, "top_positive": [], "top_negative": []}


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_pead_monitor(
    cfg=None,
    results: Optional[dict] = None,
) -> dict:
    """
    PEAD Monitor のメインエントリポイント。
    stock_analyzer_main.py の DAILY_MODULES から呼ばれる。

    処理フロー:
      1. pead_candidates.json を読み込む
      2. 既存候補に日次減衰を適用
      3. screener 通過銘柄から新規 PEAD 候補を検出
      4. 新規候補をキャッシュに追加
      5. キャッシュを保存して結果を返す

    Args:
        cfg:     config モジュール（None 可）
        results: stock_analyzer_main.results 辞書（None 可）

    Returns:
        {
          "alert_candidates":    list[dict],
          "new_detections":      list[str],
          "negative_candidates": list[dict],
          "total_tracked":       int,
          "summary":             dict,
          "generated":           str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("  📊 [pead_monitor] PEAD日次監視実行中...")

    if results is None:
        results = {}

    # ── ① 既存キャッシュ読み込み ──────────────────────────────
    try:
        existing = load_pead_candidates(cfg)
        print(f"    既存追跡: {len(existing)}件")
    except Exception:
        existing = {}

    # ── ② 日次減衰適用 ───────────────────────────────────────
    try:
        decayed = apply_pead_decay(existing, cfg)
        expired = len(existing) - len(decayed)
        if expired > 0:
            print(f"    🗑️  TTL切れ除外: {expired}件")
    except Exception:
        decayed = existing

    # ── ③ screener 結果から新規候補を検出 ────────────────────
    new_entries:  list[dict] = []
    new_tickers:  list[str]  = []
    try:
        screener   = results.get("screener", {})
        passed     = (screener.get("passed")
                      or screener.get("candidates", []))

        if passed:
            new_entries, new_tickers = detect_pead_candidates(
                candidates = passed,
                existing   = decayed,
                cfg        = cfg,
            )
            if new_entries:
                print(f"    ✨ 新規PEAD候補検出: {len(new_entries)}件")
                for e in new_entries[:3]:
                    dir_mark = "📈" if e["direction"] == "positive" else "📉"
                    print(f"      {dir_mark} [{e['ticker']}] "
                          f"score={e['pead_score']:.1f}pt "
                          f"dir={e['direction']}")
    except Exception as e:
        print(f"    ⚠️  新規候補検出エラー: {str(e)[:60]}")

    # ── ④ キャッシュに統合して保存 ───────────────────────────
    try:
        for entry in new_entries:
            ticker = entry.pop("ticker", "")
            if ticker:
                decayed[ticker] = entry

        save_pead_candidates(decayed, cfg)
    except Exception as e:
        print(f"    ⚠️  キャッシュ保存エラー: {str(e)[:60]}")

    # ── ⑤ アラート候補を返す ─────────────────────────────────
    try:
        alert_candidates = get_alert_candidates(cfg)
        neg_candidates   = get_negative_pead_candidates(cfg)
        pead_summary     = get_pead_summary(cfg)
    except Exception:
        alert_candidates = []
        neg_candidates   = []
        pead_summary     = {}

    if alert_candidates:
        print(f"    🚨 アラート候補: {len(alert_candidates)}件"
              f"（score≥{_PEAD_SCORE_ALERT}かつ出来高{_PEAD_VOLUME_MIN}倍以上）")
        for a in alert_candidates[:3]:
            print(f"      [{a['ticker']}] score={a.get('pead_score',0):.1f}pt "
                  f"vol_ratio={a.get('volume_ratio',0):.2f}x")

    total_tracked = len(decayed)
    generated     = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"  ✅ PEAD監視完了: 追跡{total_tracked}件 "
          f"アラート{len(alert_candidates)}件 "
          f"陰性{len(neg_candidates)}件")

    return {
        "alert_candidates":    alert_candidates,
        "new_detections":      new_tickers,
        "negative_candidates": neg_candidates,
        "total_tracked":       total_tracked,
        "summary":             pead_summary,
        "generated":           generated,
    }


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print("  pead_monitor.py v1.0 単体テスト")
    print("=" * 60)

    class _TmpCfg:
        CACHE_DIR                  = Path(tempfile.mkdtemp())
        PEAD_SCORE_ALERT_THRESHOLD = 5.0
        PEAD_DECAY_DAILY           = 0.85
        PEAD_VOLUME_MIN_RATIO      = 1.5
        PEAD_MAX_HOLDING_DAYS_JP   = 60

    _cfg = _TmpCfg()

    # ── スコア計算テスト ──────────────────────────────────────
    print("\n── PEAD スコア計算テスト ──")
    test_cases = [
        # (surprise_bonus, revision_score, volume_ratio, price_drift, direction, label)
        (4.0, 0.5, 2.5, 0.08, "positive", "強い正PEAD"),
        (1.5, 0.3, 1.6, 0.03, "positive", "中程度正PEAD"),
        (0.5, 0.1, 1.2, 0.01, "positive", "弱い正PEAD"),
        (-3.0, -0.4, 2.0, -0.07, "negative", "強い陰性PEAD"),
        (2.0, 0.3, 2.0, -0.05, "positive", "正PEAD(逆方向ドリフト)"),
    ]
    for sb, rv, vr, pd, dr, label in test_cases:
        score, breakdown = calc_pead_score(sb, rv, vr, pd, dr)
        vol_min = _cfg.PEAD_VOLUME_MIN_RATIO
        alert   = "🚨" if score >= _cfg.PEAD_SCORE_ALERT_THRESHOLD and vr >= vol_min else "  "
        print(f"  {alert} {label}: score={score:.1f}pt "
              f"[surprise={breakdown.get('surprise_score',0):.1f} "
              f"vol={breakdown.get('volume_score',0):.1f} "
              f"price={breakdown.get('price_score',0):.1f}]")

    # ── 日次減衰テスト ────────────────────────────────────────
    print("\n── 日次減衰テスト ──")
    mock_cache = {}
    for days_ago in [0, 5, 10, 20, 40]:
        first_dt = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        mock_cache[f"TEST{days_ago}.T"] = {
            "first_detected":     first_dt,
            "last_updated":       first_dt,
            "pead_score":         8.0,
            "pead_score_peak":    8.0,
            "direction":          "positive",
            "volume_ratio":       2.0,
            "is_alert":           True,
            "volume_trigger":     True,
            "days_since_detect":  days_ago,
        }

    decayed = apply_pead_decay(mock_cache, _cfg)
    for ticker in sorted(mock_cache.keys()):
        days = int(ticker.replace("TEST","").replace(".T",""))
        orig  = 8.0
        new   = decayed.get(ticker, {}).get("pead_score", 0.0)
        exp   = round(orig * (0.85 ** days), 3)
        print(f"  day {days:2d}: {orig:.1f}pt → {new:.3f}pt (期待値: {exp:.3f})")

    # ── 候補検出テスト ────────────────────────────────────────
    print("\n── 候補検出テスト（yfinance不使用・モック）──")
    mock_candidates = [
        {"ticker": "6758.T", "sector_code": "1016",
         "surprise_bonus": 3.5, "revision_score": 0.4,
         "pead_trigger": True, "pass_filters": True},
        {"ticker": "6861.T", "sector_code": "1016",
         "surprise_bonus": -2.0, "revision_score": -0.3,
         "pead_trigger": False, "pass_filters": True},
        {"ticker": "9984.T", "sector_code": "1033",
         "surprise_bonus": 0.2, "revision_score": 0.1,
         "pead_trigger": False, "pass_filters": True},   # 弱すぎ→スキップ
    ]

    # volume/price fetchはスキップするため、mockで直接スコア計算
    for c in mock_candidates:
        direction = _determine_direction(
            c.get("surprise_bonus", 0), c.get("revision_score", 0)
        )
        score, bk = calc_pead_score(
            surprise_bonus = c.get("surprise_bonus", 0),
            revision_score = c.get("revision_score", 0),
            volume_ratio   = 1.8,   # モック値
            price_drift    = 0.04 if direction == "positive" else -0.04,
            direction      = direction or "positive",
        )
        if direction:
            print(f"  [{c['ticker']}] direction={direction} "
                  f"score={score:.1f}pt")

    print("\n✅ 単体テスト完了")
